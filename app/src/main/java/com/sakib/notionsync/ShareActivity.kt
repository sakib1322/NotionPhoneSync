package com.sakib.notionsync

import android.content.Intent
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import com.google.android.material.dialog.MaterialAlertDialogBuilder

/** "Save to cloud" in the phone's Share menu. */
class ShareActivity : AppCompatActivity() {

    @Suppress("DEPRECATION")
    private fun incoming(): List<Uri> {
        val i = intent ?: return emptyList()
        return when (i.action) {
            Intent.ACTION_SEND -> listOfNotNull(
                if (Build.VERSION.SDK_INT >= 33) i.getParcelableExtra(Intent.EXTRA_STREAM, Uri::class.java)
                else i.getParcelableExtra(Intent.EXTRA_STREAM)
            )
            Intent.ACTION_SEND_MULTIPLE ->
                (if (Build.VERSION.SDK_INT >= 33) i.getParcelableArrayListExtra(Intent.EXTRA_STREAM, Uri::class.java)
                else i.getParcelableArrayListExtra(Intent.EXTRA_STREAM)) ?: emptyList()
            else -> emptyList()
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val prefs = Prefs(this)
        val uris = incoming()
        if (uris.isEmpty()) {
            toast("Nothing to upload.")
            finish()
            return
        }
        if (prefs.token.isBlank() || Db.get(this).indexDbId(prefs.rootPageId) == null) {
            toast("Open Notion Phone Sync and finish setup (and one sync) first.")
            finish()
            return
        }
        val label = if (uris.size == 1) "1 file" else "${uris.size} files"
        FolderPicker(this, "Save $label to", "Save here", { root, path ->
            val dialog = MaterialAlertDialogBuilder(this)
                .setTitle("Preparing $label…")
                .setMessage("Copying into the app")
                .setCancelable(false)
                .show()
            Thread {
                val n = UploadWorker.queue(this, uris, root, path) { done ->
                    runOnUiThread { dialog.setMessage("Copying $done of ${uris.size}") }
                }
                runOnUiThread {
                    dialog.dismiss()
                    toast(if (n > 0) "Uploading $n file(s) to ${Sources.title(prefs, root)}${if (path.isEmpty()) "" else " › $path"}." else "Couldn't read those files.")
                    finish()
                }
            }.start()
        }, { finish() }).show(prefs.rootPageId, "")
    }

    private fun toast(t: String) = Toast.makeText(this, t, Toast.LENGTH_LONG).show()
}
