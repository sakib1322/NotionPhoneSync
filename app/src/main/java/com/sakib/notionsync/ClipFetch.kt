package com.sakib.notionsync

import android.content.ClipData
import android.content.Context
import android.content.Intent
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.view.ViewGroup
import android.widget.LinearLayout
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.core.app.NotificationManagerCompat
import androidx.core.content.FileProvider
import com.google.android.material.dialog.MaterialAlertDialogBuilder
import com.google.android.material.progressindicator.LinearProgressIndicator
import org.json.JSONObject
import java.io.File
import java.util.Collections
import java.util.concurrent.ConcurrentHashMap

/**
 * Files and pictures copied on the laptop are only *offered* to the phone (name and size).
 * Nothing is downloaded until you tap Get or Share; this asks the laptop for them
 * (over Wi-Fi when possible, otherwise through Notion).
 */
object ClipFetch {
    class Job(val id: String, val total: Long) {
        @Volatile var got = 0L
        @Volatile var files: List<File>? = null
        @Volatile var error: String? = null
        @Volatile var viaNotion = false
        @Volatile var watched = true
        @Volatile var askedAt = System.currentTimeMillis()
        val done get() = files != null || error != null
    }

    val jobs = ConcurrentHashMap<String, Job>()
    /** Offers already delivered (so a second copy, e.g. Wi-Fi and Notion both answering, is ignored). */
    val finished: MutableSet<String> = Collections.newSetFromMap(ConcurrentHashMap())

    fun start(ctx: Context, offer: JSONObject): Job {
        val id = offer.optString("id")
        jobs[id]?.let { if (!it.done) { it.watched = true; return it } }
        val job = Job(id, offer.optLong("total"))
        jobs[id] = job
        ask(ctx, job, false)
        return job
    }

    /** Asks the laptop to send the files: Wi-Fi first, else a note through Notion. */
    fun ask(ctx: Context, job: Job, notionOnly: Boolean) {
        job.askedAt = System.currentTimeMillis()
        val app = ctx.applicationContext
        Thread {
            if (!notionOnly && ClipService.lan?.note(JSONObject().put("t", "fetch").put("id", job.id)) == true) return@Thread
            job.viaNotion = true
            try {
                ClipRelay(app).send("request", job.id, emptyList())
            } catch (e: Exception) {
                job.error = "Couldn't reach the laptop: ${e.message}"
            }
        }.start()
    }

    /** Called by the clipboard service once the files are saved and on the clipboard. */
    fun complete(id: String, files: List<File>): Boolean {
        finished.add(id)
        val j = jobs[id] ?: return false
        j.files = files
        return j.watched
    }

    fun fail(id: String, why: String): Boolean {
        val j = jobs[id] ?: return false
        if (j.done) return true
        j.error = why
        return j.watched
    }

    fun progress(id: String, n: Long) {
        jobs[id]?.let { it.got += n }
    }

    fun sizeText(b: Long): String = when {
        b >= 1L shl 30 -> "%.1f GB".format(b / (1024.0 * 1024 * 1024))
        b >= 1L shl 20 -> "%.1f MB".format(b / (1024.0 * 1024))
        b >= 1024 -> "%d KB".format(b / 1024)
        else -> "$b bytes"
    }

    fun describe(offer: JSONObject): String {
        val files = offer.optJSONArray("files")
        val count = offer.optInt("count", files?.length() ?: 0)
        val first = files?.optJSONObject(0)?.optString("name") ?: "a file"
        val what = if (count <= 1) first else "$count files"
        return "$what (${sizeText(offer.optLong("total"))})"
    }
}

/** The little "Getting from laptop…" window behind the Get and Share buttons. */
class FetchActivity : AppCompatActivity() {
    private val h = Handler(Looper.getMainLooper())
    private lateinit var offer: JSONObject
    private var share = false
    private var job: ClipFetch.Job? = null
    private var dialog: AlertDialog? = null
    private lateinit var msg: TextView
    private lateinit var bar: LinearProgressIndicator

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        offer = try { JSONObject(intent.getStringExtra("offer") ?: "{}") } catch (e: Exception) { JSONObject() }
        share = intent.getBooleanExtra("share", false)
        if (offer.optString("id").isEmpty()) { finish(); return }
        val pad = (20 * resources.displayMetrics.density).toInt()
        val box = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(pad, pad / 2, pad, 0)
        }
        msg = TextView(this).apply { text = ClipFetch.describe(offer) }
        bar = LinearProgressIndicator(this).apply {
            isIndeterminate = true
            layoutParams = LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT).apply { topMargin = pad / 2 }
        }
        box.addView(msg); box.addView(bar)
        dialog = MaterialAlertDialogBuilder(this)
            .setTitle(if (share) "Getting it to share…" else "Getting from laptop…")
            .setView(box)
            .setNegativeButton("Hide") { _, _ -> hide() }
            .setOnCancelListener { hide() }
            .show()
        job = ClipFetch.start(this, offer)
        h.post(tick)
    }

    private fun hide() {
        job?.watched = false
        if (job?.done != true) Toast.makeText(this, "Still getting it — you'll get a notification when it's ready.", Toast.LENGTH_SHORT).show()
        finish()
    }

    private val tick = object : Runnable {
        override fun run() {
            val j = job ?: return
            if (isFinishing) return
            j.files?.let { done(it); return }
            j.error?.let { failed(it); return }
            val total = offer.optLong("total")
            if (!j.viaNotion && total > 0 && j.got > 0) {
                bar.isIndeterminate = false
                bar.progress = (j.got * 100 / total).toInt().coerceIn(0, 100)
                msg.text = "${ClipFetch.describe(offer)}\n${ClipFetch.sizeText(j.got)} of ${ClipFetch.sizeText(total)} over Wi-Fi"
            } else if (j.viaNotion) {
                msg.text = "${ClipFetch.describe(offer)}\nThe laptop is sending it through Notion…"
            }
            // The Wi-Fi link dropped before anything arrived: ask through Notion instead.
            if (!j.viaNotion && ClipService.lan?.connected != true && j.got == 0L &&
                System.currentTimeMillis() - j.askedAt > 1500) {
                ClipFetch.ask(this@FetchActivity, j, true)
            }
            if (j.viaNotion && j.got == 0L && System.currentTimeMillis() - j.askedAt > 120_000) {
                failed("The laptop didn't answer. Make sure Notion Laptop Sync is running and online, then try again.")
                return
            }
            h.postDelayed(this, 300)
        }
    }

    private fun done(files: List<File>) {
        NotificationManagerCompat.from(this).cancel(ClipService.ID_OFFER)
        dialog?.dismiss()
        if (share) {
            val uris = files.map { FileProvider.getUriForFile(this, "$packageName.files", it) }
            val mimes = files.map { FileTypes.mime(it.name) }.distinct()
            val type = if (mimes.size == 1) mimes[0] else "*/*"
            val send = if (uris.size == 1) Intent(Intent.ACTION_SEND).setType(type).putExtra(Intent.EXTRA_STREAM, uris[0])
            else Intent(Intent.ACTION_SEND_MULTIPLE).setType(type).putParcelableArrayListExtra(Intent.EXTRA_STREAM, ArrayList(uris))
            val clip = ClipData.newRawUri(files[0].name, uris[0])
            for (u in uris.drop(1)) clip.addItem(ClipData.Item(u))
            send.clipData = clip
            send.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
            startActivity(Intent.createChooser(send, "Send to…"))
        } else {
            Toast.makeText(this, if (files.size == 1) "✓ ${files[0].name} copied — paste it anywhere" else "✓ ${files.size} files copied — paste them anywhere", Toast.LENGTH_SHORT).show()
        }
        finish()
    }

    private fun failed(why: String) {
        dialog?.dismiss()
        dialog = MaterialAlertDialogBuilder(this)
            .setTitle("Couldn't get it")
            .setMessage(why)
            .setPositiveButton("Try again") { _, _ ->
                ClipFetch.jobs.remove(offer.optString("id"))
                recreate()
            }
            .setNegativeButton("Close") { _, _ -> finish() }
            .setOnCancelListener { finish() }
            .show()
    }

    override fun onDestroy() {
        h.removeCallbacks(tick)
        if (job?.done != true) job?.watched = false
        dialog?.dismiss()
        super.onDestroy()
    }
}
