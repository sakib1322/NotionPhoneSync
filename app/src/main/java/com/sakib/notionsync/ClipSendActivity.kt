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
        val auto = intent?.getBooleanExtra("auto", false) == true
        val got = ClipSend.read(this)
        if (got == null) {
            if (!auto) Toast.makeText(this, "Nothing copied yet. Copy something first.", Toast.LENGTH_SHORT).show()
        } else if (!ClipSend.sendIfNew(this, got.first, got.second, quiet = auto) && !auto) {
            Toast.makeText(this, "Already sent.", Toast.LENGTH_SHORT).show()
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
