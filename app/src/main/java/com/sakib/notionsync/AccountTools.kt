package com.sakib.notionsync

import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.os.Handler
import android.os.Looper
import android.widget.EditText
import android.widget.FrameLayout
import android.widget.ImageView
import android.widget.LinearLayout
import android.widget.TextView
import android.widget.Toast
import androidx.activity.result.ActivityResultLauncher
import androidx.activity.result.IntentSenderRequest
import androidx.appcompat.app.AppCompatActivity
import com.google.android.gms.auth.api.identity.AuthorizationRequest
import com.google.android.gms.auth.api.identity.AuthorizationResult
import com.google.android.gms.auth.api.identity.Identity
import com.google.android.gms.common.api.Scope
import com.google.android.material.dialog.MaterialAlertDialogBuilder
import com.google.mlkit.vision.codescanner.GmsBarcodeScanning
import org.json.JSONObject
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * Sign in with Google (your setup is kept in your Google account, so a new phone
 * picks up where you left off) and moving the setup with a code / QR.
 */
class AccountTools(
    private val act: AppCompatActivity,
    private val launcher: ActivityResultLauncher<IntentSenderRequest>,
    private val onChanged: () -> Unit
) {
    private val prefs = Prefs(act)
    private val main = Handler(Looper.getMainLooper())
    private var pending: ((String?) -> Unit)? = null
    private val scopes = listOf(Scope("https://www.googleapis.com/auth/drive.appdata"), Scope("email"))

    fun statusText(): String {
        val email = prefs.googleEmail
        if (email.isEmpty()) return "Sign in with Google to keep your setup in your Google account. " +
            "On a new phone, just sign in and everything comes back."
        val saved = prefs.googleSavedAt
        return "Signed in as $email" + if (saved > 0) " · setup saved ${SimpleDateFormat("d MMM, h:mm a", Locale.US).format(Date(saved))}" else ""
    }

    // ---- Google ----------------------------------------------------------------------------

    /** Gets a Google access token; shows Google's sign-in screen only when [interactive]. */
    private fun token(interactive: Boolean, then: (String?) -> Unit) {
        val req = AuthorizationRequest.builder().setRequestedScopes(scopes).build()
        Identity.getAuthorizationClient(act).authorize(req)
            .addOnSuccessListener { res ->
                if (res.hasResolution()) {
                    if (!interactive) then(null) else {
                        pending = then
                        launcher.launch(IntentSenderRequest.Builder(res.pendingIntent!!.intentSender).build())
                    }
                } else {
                    remember(res)
                    then(res.accessToken)
                }
            }
            .addOnFailureListener { e ->
                if (interactive) toast("Google sign-in didn't work: ${e.message}")
                then(null)
            }
    }

    /** Called by MainActivity with the result of Google's sign-in screen. */
    fun onAuthResult(data: android.content.Intent?) {
        val cb = pending ?: return
        pending = null
        try {
            val res = Identity.getAuthorizationClient(act).getAuthorizationResultFromIntent(data)
            remember(res)
            cb(res.accessToken)
        } catch (e: Exception) {
            toast("Sign-in was cancelled.")
            cb(null)
        }
    }

    private fun remember(res: AuthorizationResult) {
        res.toGoogleSignInAccount()?.email?.let { if (it.isNotEmpty()) prefs.googleEmail = it }
    }

    fun signInOrOut() {
        if (prefs.googleEmail.isNotEmpty()) {
            MaterialAlertDialogBuilder(act)
                .setTitle(prefs.googleEmail)
                .setItems(arrayOf("Save setup now", "Sign out")) { _, i ->
                    if (i == 0) saveNow(interactive = true) else {
                        prefs.googleEmail = ""
                        prefs.googleSavedAt = 0
                        onChanged()
                        toast("Signed out. Your files and setup on this phone are unchanged.")
                    }
                }
                .show()
            return
        }
        token(interactive = true) { t ->
            if (t == null) return@token
            Thread {
                val cloud = try { SetupSync.download(t) } catch (e: Exception) { null }
                main.post {
                    onChanged()
                    if (cloud != null && cloud.optJSONObject("phone")?.optString("root_page").orEmpty().isNotEmpty() &&
                        (prefs.token.isEmpty() || prefs.rootPageId != cloud.optJSONObject("phone")?.optString("root_page"))
                    ) {
                        askRestore(cloud)
                    } else if (prefs.token.isNotEmpty()) {
                        saveNow(interactive = false)
                        toast("Signed in. Your setup is saved to your Google account.")
                    } else {
                        toast("Signed in. Finish the Notion setup below; it will be saved to your Google account.")
                    }
                }
            }.start()
        }
    }

    private fun askRestore(cloud: JSONObject) {
        val title = cloud.optJSONObject("phone")?.optString("root_title") ?: "your backup"
        MaterialAlertDialogBuilder(act)
            .setTitle("Restore your setup?")
            .setMessage("Your Google account has a saved setup for “$title”. This phone will use it: same Notion " +
                "backup, same options. Files already in the cloud won't be uploaded again.")
            .setPositiveButton("Restore") { _, _ -> applySetup(cloud) }
            .setNegativeButton("Not now", null)
            .show()
    }

    /** Saves to Google only if something changed since the last save (used when leaving the app). */
    fun saveIfChanged() {
        if (prefs.googleEmail.isEmpty() || prefs.token.isEmpty()) return
        val sp = act.getSharedPreferences("account", Context.MODE_PRIVATE)
        val hash = SetupSync.payload(act).apply { remove("updated") }.toString().hashCode()
        if (sp.getInt("saved_hash", 0) == hash) return
        sp.edit().putInt("saved_hash", hash).apply()
        saveNow(interactive = false)
    }

    /** Saves the setup to Google quietly (e.g. after settings change). */
    fun saveNow(interactive: Boolean) {
        if (prefs.googleEmail.isEmpty() || prefs.token.isEmpty()) return
        token(interactive) { t ->
            if (t == null) return@token
            Thread {
                val ok = try {
                    SetupSync.upload(act, t)
                    true
                } catch (e: Exception) {
                    if (interactive) main.post { toast("Couldn't save to Google: ${e.message}") }
                    false
                }
                main.post {
                    onChanged()
                    if (ok && interactive) toast("Setup saved to your Google account.")
                }
            }.start()
        }
    }

    // ---- code / QR ------------------------------------------------------------------------

    fun showTransfer() {
        if (prefs.token.isEmpty()) {
            toast("Set up Notion first.")
            return
        }
        val code = SetupSync.transferCode(act)
        val d = act.resources.displayMetrics.density
        val size = (260 * d).toInt()
        val box = LinearLayout(act).apply {
            orientation = LinearLayout.VERTICAL
            setPadding((20 * d).toInt(), (8 * d).toInt(), (20 * d).toInt(), 0)
            addView(ImageView(act).apply { setImageBitmap(SetupSync.qr(code, size)) }, LinearLayout.LayoutParams(size, size))
            addView(TextView(act).apply {
                text = "On the new phone: Settings → Account → Set up from code → Scan.\n" +
                    "On the laptop: Notion tab → Set up from code → paste.\n\n" +
                    "This code contains your Notion secret: show it only to your own devices."
                textSize = 13f
                setPadding(0, (10 * d).toInt(), 0, 0)
            })
        }
        MaterialAlertDialogBuilder(act)
            .setTitle("Move to a new device")
            .setView(box)
            .setPositiveButton("Done", null)
            .setNeutralButton("Copy code") { _, _ ->
                (act.getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager)
                    .setPrimaryClip(ClipData.newPlainText("Notion Sync setup", code))
                toast("Code copied. Paste it on the other device, then clear your clipboard.")
            }
            .show()
    }

    fun receiveTransfer() {
        MaterialAlertDialogBuilder(act)
            .setTitle("Set up from another device")
            .setItems(arrayOf("Scan the QR code", "Paste a code")) { _, i -> if (i == 0) scan() else paste() }
            .show()
    }

    private fun scan() {
        GmsBarcodeScanning.getClient(act).startScan()
            .addOnSuccessListener { b -> useCode(b.rawValue ?: "") }
            .addOnFailureListener { toast("Couldn't open the scanner. Use Paste a code instead.") }
    }

    private fun paste() {
        val input = EditText(act).apply { hint = "NSYNC1:…" }
        val box = FrameLayout(act).apply {
            val p = (20 * resources.displayMetrics.density).toInt()
            setPadding(p, p / 2, p, 0)
            addView(input)
        }
        MaterialAlertDialogBuilder(act)
            .setTitle("Paste the code")
            .setView(box)
            .setPositiveButton("Use") { _, _ -> useCode(input.text.toString()) }
            .setNegativeButton("Cancel", null)
            .show()
    }

    private fun useCode(text: String) {
        val json = SetupSync.readCode(text)
        if (json == null) {
            toast("That isn't a Notion Sync setup code.")
            return
        }
        MaterialAlertDialogBuilder(act)
            .setTitle("Use this setup?")
            .setMessage("This phone will connect to the same Notion account. Files already in the cloud won't be uploaded again.")
            .setPositiveButton("Use it") { _, _ -> applySetup(json) }
            .setNegativeButton("Cancel", null)
            .show()
    }

    /** Applies a setup; if it has no phone backup page (e.g. from the laptop), asks which page to use. */
    private fun applySetup(json: JSONObject) {
        val complete = SetupSync.apply(act, json)
        if (complete) {
            done()
            return
        }
        val pages = SetupSync.sourcesIn(json)
        MaterialAlertDialogBuilder(act)
            .setTitle("Which page is this phone's backup?")
            .setItems((pages.map { it.second } + "Another page (paste its link below)").toTypedArray()) { _, i ->
                if (i < pages.size) {
                    prefs.rootPageId = pages[i].first
                    prefs.rootTitle = pages[i].second
                    done()
                } else {
                    onChanged()
                    toast("Paste the page link under Notion, then tap Save & test connection.")
                }
            }
            .show()
    }

    private fun done() {
        onChanged()
        if (prefs.autoSync) Scheduler.apply(act)
        toast("All set. Allow the permissions below if asked, then your backup continues.")
        if (prefs.googleEmail.isNotEmpty()) saveNow(interactive = false)
    }

    private fun toast(t: String) = Toast.makeText(act, t, Toast.LENGTH_LONG).show()
}
