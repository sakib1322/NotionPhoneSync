package com.sakib.notionsync

import org.json.JSONArray
import org.json.JSONObject
import java.io.DataInputStream
import java.io.DataOutputStream
import java.io.File
import java.io.IOException
import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.InetSocketAddress
import java.net.ServerSocket
import java.net.Socket
import java.net.SocketTimeoutException
import java.nio.ByteBuffer
import java.security.MessageDigest
import java.security.SecureRandom
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import javax.crypto.Cipher
import javax.crypto.spec.GCMParameterSpec
import javax.crypto.spec.SecretKeySpec

/**
 * Direct Wi-Fi link for the shared clipboard (same protocol as the laptop's lan.py).
 * When the phone and laptop are on the same network, items go straight between them,
 * much faster than through Notion; Notion stays as the fallback.
 *
 * The laptop always dials the phone: it finds it with a UDP broadcast (port 47393) and keeps
 * one TCP connection open, which both sides then use. Everything is encrypted and
 * authenticated (AES-256-GCM) with a key derived from the Notion secret both apps share, and
 * each connection mixes in fresh random values from both sides so nothing can be replayed.
 */
object Lan {
    const val TCP_PORT = 47392
    const val UDP_PORT = 47393
    const val CHUNK = 1024 * 1024
    const val MAX_FILE = 4L * 1024 * 1024 * 1024
    val MAGIC = "NSL1".toByteArray()
    val rnd = SecureRandom()

    fun key(token: String): ByteArray =
        MessageDigest.getInstance("SHA-256").digest("notion-sync-lan-v1|$token".toByteArray())

    fun keyId(key: ByteArray): String =
        MessageDigest.getInstance("SHA-256").digest("id|".toByteArray() + key)
            .joinToString("") { "%02x".format(it) }.take(16)
}

class LanLink(val sock: Socket, key: ByteArray, initiator: Boolean) {
    private val keySpec = SecretKeySpec(key, "AES")
    private val input = DataInputStream(sock.getInputStream().buffered(1 shl 16))
    private val output = DataOutputStream(sock.getOutputStream().buffered(1 shl 16))
    private val sid: ByteArray
    private val outTag: Byte
    private val inTag: Byte
    private var outCtr = 0L
    private var inCtr = 0L
    val wlock = Any()

    init {
        val mine = ByteArray(16).also { Lan.rnd.nextBytes(it) }
        output.write(Lan.MAGIC); output.write(mine); output.flush()
        val head = ByteArray(20)
        input.readFully(head)
        if (!head.copyOfRange(0, 4).contentEquals(Lan.MAGIC)) throw IOException("not a Notion Sync device")
        val theirs = head.copyOfRange(4, 20)
        sid = if (initiator) theirs + mine else mine + theirs  // phone's then laptop's
        outTag = if (initiator) 'L'.code.toByte() else 'P'.code.toByte()
        inTag = if (initiator) 'P'.code.toByte() else 'L'.code.toByte()
    }

    private fun aad(tag: Byte, ctr: Long): ByteArray =
        sid + byteArrayOf(tag) + ByteBuffer.allocate(8).putLong(ctr).array()

    private fun sendFrame(data: ByteArray, off: Int = 0, len: Int = data.size) {
        val nonce = ByteArray(12).also { Lan.rnd.nextBytes(it) }
        val c = Cipher.getInstance("AES/GCM/NoPadding")
        c.init(Cipher.ENCRYPT_MODE, keySpec, GCMParameterSpec(128, nonce))
        c.updateAAD(aad(outTag, outCtr))
        val ct = c.doFinal(data, off, len)
        outCtr++
        output.writeInt(ct.size + 12)
        output.write(nonce)
        output.write(ct)
    }

    fun recvFrame(): ByteArray {
        val n = input.readInt()
        if (n < 28 || n > Lan.CHUNK + 64 + 28) throw IOException("bad frame")
        val raw = ByteArray(n)
        input.readFully(raw)
        val c = Cipher.getInstance("AES/GCM/NoPadding")
        c.init(Cipher.DECRYPT_MODE, keySpec, GCMParameterSpec(128, raw, 0, 12))
        c.updateAAD(aad(inTag, inCtr))
        val out = c.doFinal(raw, 12, n - 12)
        inCtr++
        return out
    }

    /** Sends a JSON message, then the bytes of each file in order. */
    fun sendMsg(meta: JSONObject, files: List<File> = emptyList(), progress: (Long) -> Unit = {}) {
        synchronized(wlock) {
            sendFrame(meta.toString().toByteArray())
            val buf = ByteArray(Lan.CHUNK)
            for (f in files) {
                f.inputStream().use { inp ->
                    while (true) {
                        var got = 0
                        while (got < buf.size) {
                            val r = inp.read(buf, got, buf.size - got)
                            if (r < 0) break
                            got += r
                        }
                        if (got == 0) break
                        sendFrame(buf, 0, got)
                        progress(got.toLong())
                        if (got < buf.size) break
                    }
                }
            }
            output.flush()
        }
    }

    fun recvMsg(): JSONObject = JSONObject(String(recvFrame()))

    /** Reads the bytes for files ([{name, size}]) into destFor(name); returns the files. */
    fun recvFiles(files: JSONArray, progress: (Long) -> Unit = {}, destFor: (String) -> File): List<File> {
        val out = ArrayList<File>()
        for (i in 0 until files.length()) {
            val f = files.getJSONObject(i)
            val dest = destFor(f.optString("name", "file"))
            var left = f.optLong("size")
            dest.outputStream().buffered(1 shl 20).use { o ->
                while (left > 0) {
                    val b = recvFrame()
                    if (b.size > left) throw IOException("too much data")
                    o.write(b)
                    left -= b.size
                    progress(b.size.toLong())
                }
            }
            out.add(dest)
        }
        return out
    }

    fun close() = try { sock.close() } catch (e: Exception) { }
}

/**
 * The phone's side: answers "where are you?" from your laptops and accepts a connection from
 * each. onItem(meta, link, laptopId) is called for each item a laptop sends; it must read the
 * item's files from the link and return true once it's on the clipboard. onNote(meta, laptopId)
 * gets small messages (offers, "can't send that").
 */
class LanServer(
    private val token: () -> String,
    private val enabled: () -> Boolean,
    private val onItem: (JSONObject, LanLink, String) -> Boolean,
    private val log: (String) -> Unit,
    private val onNote: (JSONObject, String) -> Unit = { _, _ -> },
    private val me: () -> Pair<String, String> = { "" to "" },
    private val tcpPort: Int = Lan.TCP_PORT,
    private val udpPort: Int = Lan.UDP_PORT,
) {
    private val links = ConcurrentHashMap<String, LanLink>()
    private val names = ConcurrentHashMap<String, String>()
    @Volatile private var running = false
    @Volatile var port = 0
        private set
    private var server: ServerSocket? = null
    private var udp: DatagramSocket? = null
    private val acks = ConcurrentHashMap<String, CountDownLatch>()

    val connected get() = links.isNotEmpty()
    /** (older callers) any link */
    val link: LanLink? get() = links.values.firstOrNull()
    fun connectedIds(): Set<String> = links.keys.toSet()
    fun connectedNames(): List<String> = links.keys.map { names[it] ?: "laptop" }
    fun isConnected(id: String) = links.containsKey(id)

    fun start() {
        if (running) return
        running = true
        Thread({ acceptLoop() }, "lan-accept").apply { isDaemon = true }.start()
        Thread({ udpLoop() }, "lan-udp").apply { isDaemon = true }.start()
    }

    fun stop() {
        running = false
        try { server?.close() } catch (e: Exception) { }
        try { udp?.close() } catch (e: Exception) { }
        links.values.forEach { it.close() }
        links.clear()
    }

    private fun udpLoop() {
        while (running) {
            try {
                val s = DatagramSocket(null).apply { reuseAddress = true; broadcast = true; soTimeout = 2000 }
                s.bind(InetSocketAddress(udpPort))
                udp = s
                val buf = ByteArray(2048)
                while (running) {
                    val pkt = DatagramPacket(buf, buf.size)
                    try { s.receive(pkt) } catch (e: SocketTimeoutException) { continue }
                    if (!enabled() || port == 0) continue
                    val t = token()
                    if (t.isEmpty()) continue
                    val j = try { JSONObject(String(pkt.data, 0, pkt.length)) } catch (e: Exception) { continue }
                    val kid = Lan.keyId(Lan.key(t))
                    if (j.optString("t") != "probe" || j.optString("id") != kid) continue
                    val reply = JSONObject().put("t", "here").put("id", kid).put("port", port).put("dev", me().first)
                        .toString().toByteArray()
                    s.send(DatagramPacket(reply, reply.size, pkt.socketAddress))
                }
                s.close()
            } catch (e: Exception) {
                if (running) Thread.sleep(3000)
            }
        }
    }

    private fun acceptLoop() {
        while (running) {
            try {
                val ss = ServerSocket()
                ss.reuseAddress = true
                try { ss.bind(InetSocketAddress(tcpPort)) } catch (e: IOException) { ss.bind(InetSocketAddress(0)) }
                server = ss
                port = ss.localPort
                while (running) {
                    val sock = ss.accept()
                    Thread({ handle(sock) }, "lan-link").apply { isDaemon = true }.start()
                }
            } catch (e: Exception) {
                port = 0
                if (running) Thread.sleep(3000)
            }
        }
    }

    private fun handle(sock: Socket) {
        val l: LanLink
        val hello: JSONObject
        try {
            if (!enabled()) { sock.close(); return }
            sock.tcpNoDelay = true
            sock.soTimeout = 10_000
            l = LanLink(sock, Lan.key(token()), false)
            hello = l.recvMsg()
            if (hello.optString("t") != "hello") throw IOException("unexpected")
            val (id, name) = me()
            l.sendMsg(JSONObject().put("t", "hello").put("from", "Phone").put("id", id).put("name", name).put("type", "phone"))
        } catch (e: Exception) {
            try { sock.close() } catch (x: Exception) { }
            return
        }
        val peer = hello.optString("id").ifEmpty { sock.inetAddress.hostAddress ?: "laptop" }
        val peerName = hello.optString("name").ifEmpty { "laptop" }
        links.put(peer, l)?.close()
        names[peer] = peerName
        log("Shared clipboard: $peerName connected over Wi-Fi (${sock.inetAddress.hostAddress}) — fast mode.")
        try {
            serve(l, peer)
        } finally {
            l.close()
            if (links.remove(peer, l)) log("Shared clipboard: Wi-Fi link to $peerName closed; using Notion for it until it's back.")
        }
    }

    private fun serve(l: LanLink, peer: String) {
        var missed = 0
        l.sock.soTimeout = 15_000
        while (running && enabled() && links[peer] === l) {
            val msg = try {
                l.recvMsg()
            } catch (e: SocketTimeoutException) {
                if (++missed >= 3) return
                try { l.sendMsg(JSONObject().put("t", "ping")) } catch (x: Exception) { return }
                continue
            } catch (e: Exception) {
                return
            }
            missed = 0
            when (msg.optString("t")) {
                "ping" -> try { l.sendMsg(JSONObject().put("t", "pong")) } catch (e: Exception) { return }
                "ack" -> acks[msg.optString("id")]?.countDown()
                "offer", "gone" -> try { onNote(msg, peer) } catch (e: Exception) { }
                "item" -> try {
                    l.sock.soTimeout = 60_000
                    val ok = onItem(msg, l, peer)
                    l.sock.soTimeout = 15_000
                    l.sendMsg(JSONObject().put("t", "ack").put("id", msg.optString("id")).put("ok", ok))
                } catch (e: Exception) {
                    log("Shared clipboard: couldn't take an item from ${names[peer] ?: "a laptop"} over Wi-Fi (${e.message}).")
                    return
                }
            }
        }
    }

    private fun targets(to: String?): List<Pair<String, LanLink>> =
        if (!to.isNullOrEmpty()) listOfNotNull(links[to]?.let { to to it }) else links.entries.map { it.key to it.value }

    /** Sends a small message (e.g. "fetch") to one laptop or all; returns the ids reached. */
    fun note(msg: JSONObject, to: String? = null): Set<String> {
        val reached = HashSet<String>()
        for ((id, l) in targets(to)) {
            try { l.sendMsg(msg); reached.add(id) } catch (e: Exception) { l.close() }
        }
        return reached
    }

    /** Sends over Wi-Fi to one laptop or all; returns the ids that confirmed it (empty = use Notion). */
    fun send(kind: String, text: String?, files: List<File>, to: String? = null, item: String = ""): Set<String> {
        val arr = JSONArray()
        var total = 0L
        for (f in files) { arr.put(JSONObject().put("name", f.name).put("size", f.length())); total += f.length() }
        val (myId, myName) = me()
        val reached = HashSet<String>()
        for ((peer, l) in targets(to)) {
            val id = ByteArray(8).also { Lan.rnd.nextBytes(it) }.joinToString("") { "%02x".format(it) }
            val latch = CountDownLatch(1)
            acks[id] = latch
            try {
                l.sendMsg(JSONObject().put("t", "item").put("id", id).put("kind", kind).put("text", text ?: "")
                    .put("files", arr).put("item", item.ifEmpty { id }).put("from_id", myId).put("from_name", myName), files)
                if (latch.await(4000 + total / 1024, TimeUnit.MILLISECONDS)) reached.add(peer)
                else l.close()  // no answer: drop it so the next item goes through Notion
            } catch (e: Exception) {
                l.close()
            } finally {
                acks.remove(id)
            }
        }
        return reached
    }
}
