package com.sakib.notionsync

import android.app.Activity
import android.content.ClipboardManager
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.widget.Toast

/**
 * Invisible screen that sends to the laptop. Android only lets an app read the clipboard
 * while one of its windows has focus, so this flashes up for a moment, reads it and closes.
 * Also the "Copy to laptop" option in the Share menu.
 */
class ClipSendActivity : Activity() {
    private var done = false

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val a = intent?.action
        if (a == Intent.ACTION_SEND || a == Intent.ACTION_SEND_MULTIPLE) {
            done = true
            val uris = ArrayList<Uri>()
            if (a == Intent.ACTION_SEND) {
                stream(intent)?.let { uris.add(it) }
            } else {
                streams(intent)?.let { uris.addAll(it) }
            }
            val text = intent.getStringExtra(Intent.EXTRA_TEXT)
            ClipSend.send(this, text, uris)
            finish()
        }
    }

    override fun onWindowFocusChanged(hasFocus: Boolean) {
        super.onWindowFocusChanged(hasFocus)
        if (!hasFocus || done) return
        done = true
        val cm = getSystemService(ClipboardManager::class.java)
        val clip = try { cm.primaryClip } catch (e: Exception) { null }
        if (clip == null || clip.itemCount == 0) {
            Toast.makeText(this, "Nothing copied yet. Copy something first.", Toast.LENGTH_SHORT).show()
            finish()
            return
        }
        val uris = ArrayList<Uri>()
        val sb = StringBuilder()
        for (i in 0 until clip.itemCount) {
            val it = clip.getItemAt(i)
            if (it.uri != null) uris.add(it.uri)
            else it.coerceToText(this)?.toString()?.let { t -> if (t.isNotEmpty()) { if (sb.isNotEmpty()) sb.append('\n'); sb.append(t) } }
        }
        val text = sb.toString()
        val fp = if (uris.isEmpty()) ClipRelay.fingerprint("text", text, null) else ""
        if (fp.isNotEmpty() && fp == ClipService.lastFp) {
            Toast.makeText(this, "Already on the laptop.", Toast.LENGTH_SHORT).show()
        } else {
            ClipSend.send(this, text, uris)
        }
        finish()
    }

    @Suppress("DEPRECATION")
    private fun stream(i: Intent): Uri? =
        if (Build.VERSION.SDK_INT >= 33) i.getParcelableExtra(Intent.EXTRA_STREAM, Uri::class.java)
        else i.getParcelableExtra(Intent.EXTRA_STREAM)

    @Suppress("DEPRECATION")
    private fun streams(i: Intent): List<Uri>? =
        if (Build.VERSION.SDK_INT >= 33) i.getParcelableArrayListExtra(Intent.EXTRA_STREAM, Uri::class.java)
        else i.getParcelableArrayListExtra(Intent.EXTRA_STREAM)
}
