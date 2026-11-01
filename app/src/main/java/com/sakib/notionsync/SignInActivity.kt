package com.sakib.notionsync

import android.content.Intent
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.text.InputType
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.RadioButton
import android.widget.RadioGroup
import android.widget.ScrollView
import android.widget.TextView
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import com.google.android.material.button.MaterialButton
import com.google.android.material.dialog.MaterialAlertDialogBuilder
import com.google.android.material.progressindicator.LinearProgressIndicator
import com.google.android.material.textfield.TextInputEditText
import com.google.android.material.textfield.TextInputLayout
import org.json.JSONObject

/**
 * The app needs you to sign in first (Google, or a setup code from a device that's already
 * signed in). Then: continue as one of your phones (e.g. you replaced it), or add this one as
 * a new device with its own name and backup page.
 */
class SignInActivity : AppCompatActivity() {
    private lateinit var prefs: Prefs
    private lateinit var account: AccountTools
    private lateinit var box: LinearLayout
    private val main = Handler(Looper.getMainLooper())
    private var js: JSONObject? = null
    private var method = ""

    private val authLauncher = registerForActivityResult(ActivityResultContracts.StartIntentSenderForResult()) { r ->
        account.onAuthResult(r.data)
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        prefs = Prefs(this)
        account = AccountTools(this, authLauncher) { }
        val d = resources.displayMetrics.density
        box = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding((24 * d).toInt(), (56 * d).toInt(), (24 * d).toInt(), (32 * d).toInt())
        }
        setContentView(ScrollView(this).apply { isFillViewport = true; addView(box) })
        stepSignIn(null)
    }

    // ---- little view helpers ------------------------------------------------------------

    private fun dp(v: Int) = (v * resources.displayMetrics.density).toInt()

    private fun clear() = box.removeAllViews()

    private fun title(t: String, sub: String? = null) {
        box.addView(TextView(this).apply {
            text = t
            textSize = 26f
            setTypeface(typeface, android.graphics.Typeface.BOLD)
        })
        if (sub != null) box.addView(TextView(this).apply {
            text = sub
            textSize = 15f
            alpha = 0.8f
            setPadding(0, dp(8), 0, dp(20))
        })
    }

    private fun text(t: String, size: Float = 14f, alpha: Float = 0.75f, top: Int = 8) = TextView(this).apply {
        text = t
        textSize = size
        this.alpha = alpha
        setPadding(0, dp(top), 0, 0)
    }.also { box.addView(it) }

    private fun button(t: String, tonal: Boolean = false, onClick: () -> Unit) =
        MaterialButton(this, null, if (tonal) com.google.android.material.R.attr.materialButtonOutlinedStyle else com.google.android.material.R.attr.materialButtonStyle).apply {
            text = t
            setOnClickListener { onClick() }
            layoutParams = LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT).apply { topMargin = dp(10) }
        }.also { box.addView(it) }

    private fun field(hint: String, value: String = "", password: Boolean = false, parent: ViewGroup = box): EditText {
        val til = TextInputLayout(this, null, com.google.android.material.R.attr.textInputOutlinedStyle).apply {
            this.hint = hint
            layoutParams = LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT).apply { topMargin = dp(10) }
            if (password) endIconMode = TextInputLayout.END_ICON_PASSWORD_TOGGLE
        }
        val et = TextInputEditText(til.context).apply {
            setText(value)
            isSingleLine = true
            if (password) inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_PASSWORD
        }
        til.addView(et)
        parent.addView(til)
        return et
    }

    private fun busy(t: String) {
        clear()
        title(t)
        box.addView(LinearProgressIndicator(this).apply {
            isIndeterminate = true
            layoutParams = LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT).apply { topMargin = dp(16) }
        })
    }

    private fun bg(work: () -> Any?, done: (Any?, Exception?) -> Unit) {
        Thread {
            val r = try { Result.success(work()) } catch (e: Exception) { Result.failure(e) }
            main.post { done(r.getOrNull(), r.exceptionOrNull() as? Exception) }
        }.start()
    }

    private fun toast(t: String) = Toast.makeText(this, t, Toast.LENGTH_LONG).show()

    // ---- step 1: sign in -------------------------------------------------------------------

    private fun stepSignIn(note: String?) {
        clear()
        title("Sign in to Notion Sync",
            "Sign in to use the app. Your phones and laptops signed in to the same account share one cloud, " +
                "and each one keeps its own name and backup page.")
        if (note != null) text(note, 14f, 1f, 0).setTextColor(0xFFB3261E.toInt())
        button("Sign in with Google") {
            busy("Signing in…")
            account.signInGetSetup { cloud, err ->
                if (cloud == null) { stepSignIn(err ?: "Sign-in didn't work."); return@signInGetSetup }
                if (err != null && cloud.length() == 0 && prefs.token.isEmpty()) {
                    // Signed in, but the saved setup couldn't be read (e.g. Google Drive access).
                    stepSignIn("Signed in, but your saved setup couldn't be read: $err\nYou can use a setup code instead.")
                    return@signInGetSetup
                }
                js = cloud
                method = "google"
                afterSignIn()
            }
        }
        button("Use a setup code from another device", tonal = true) {
            account.pickCode { json ->
                js = json
                method = "code"
                afterSignIn()
            }
        }
        text("A setup code: on a phone that's signed in, Settings → Move to a new device; on a laptop, " +
            "Notion tab → Move to a new device.", 13f, 0.6f, 14)
    }

    // ---- step 2: which device is this? ----------------------------------------------------

    private fun afterSignIn() {
        val token = js?.optString("notion_token").orEmpty().ifEmpty { prefs.token }
        if (token.isEmpty()) { stepConnect(); return }
        val cloudToken = js?.optString("notion_token").orEmpty()
        // This phone was already backing up: it stays the same device.
        if (prefs.token.isNotEmpty() && prefs.rootPageId.isNotEmpty() && (cloudToken.isEmpty() || cloudToken == prefs.token)) {
            stepNameExisting()
            return
        }
        prefs.token = token
        busy("Looking up your devices…")
        bg({
            val reg = Registry(this)
            reg.all() to reg.candidateParents()
        }) { r, e ->
            if (e != null) { stepSignIn("Couldn't reach Notion: ${e.message}"); return@bg }
            @Suppress("UNCHECKED_CAST")
            val pair = r as Pair<List<Devices.Device>, List<Pair<String, String>>>
            val (devs, parents) = pair
            stepChoose(devs, parents)
        }
    }

    private fun stepNameExisting() {
        clear()
        title("Name this phone",
            "This phone keeps backing up to “${prefs.rootTitle.ifEmpty { "Phone Backup" }}”. Give it a name so you can tell your devices apart.")
        val name = field("Name", Devices.myName(this))
        button("Continue") {
            prefs.deviceName = name.text.toString().trim().ifEmpty { Devices.defaultName(this) }.take(60)
            js?.optString("notion_token")?.takeIf { it.isNotEmpty() }?.let { prefs.token = it }
            finishSetup()
        }
    }

    private fun stepChoose(devs: List<Devices.Device>, parents: List<Pair<String, String>>) {
        clear()
        val phones = devs.filter { it.type == "phone" && it.page.isNotEmpty() }
        title("Set up this phone",
            "Continue as one of your phones (e.g. you replaced it), or add this one as a new device with its own backup page.")
        val group = RadioGroup(this)
        box.addView(group)
        val idFor = HashMap<Int, Devices.Device>()
        for (dv in phones) {
            val rb = RadioButton(this).apply {
                id = View.generateViewId()
                text = "Continue as “${dv.name}” — backs up to “${dv.pageTitle.ifEmpty { "its page" }}” (last seen ${dv.seenAgo()})"
            }
            idFor[rb.id] = dv
            group.addView(rb)
        }
        val newRb = RadioButton(this).apply { id = View.generateViewId(); text = "Set up as a new phone" }
        group.addView(newRb)
        group.check(newRb.id)

        val newBox = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL; setPadding(dp(8), 0, 0, 0) }
        box.addView(newBox)
        val name = field("Name", Devices.defaultName(this), parent = newBox)
        newBox.addView(TextView(this).apply { text = "Put its backup page inside"; setPadding(0, dp(14), 0, 0) })
        val where = RadioGroup(this)
        newBox.addView(where)
        val whereFor = HashMap<Int, String>()
        parents.forEachIndexed { i, (pid, t) ->
            val rb = RadioButton(this).apply { id = View.generateViewId(); text = t }
            whereFor[rb.id] = pid
            where.addView(rb)
            if (i == 0) where.check(rb.id)
        }
        val link = field("…or paste a page link", parent = newBox)
        newBox.addView(TextView(this).apply {
            text = "For a page link: in Notion, make a page (e.g. “Backups”), then ••• → Connections → add your integration, and Share → Copy link."
            textSize = 12f; alpha = 0.6f; setPadding(0, dp(6), 0, 0)
        })
        group.setOnCheckedChangeListener { _, id -> newBox.visibility = if (id == newRb.id) View.VISIBLE else View.GONE }

        button("Continue") {
            val chosen = idFor[group.checkedRadioButtonId]
            if (chosen != null) {
                MaterialAlertDialogBuilder(this)
                    .setTitle("Continue as “${chosen.name}”?")
                    .setMessage("Only do this if that phone isn't in use any more — two phones acting as one would mix their backups.")
                    .setPositiveButton("Continue") { _, _ ->
                        SetupSync.applyDevice(this, js, chosen)
                        finishSetup()
                    }
                    .setNegativeButton("Cancel", null)
                    .show()
                return@button
            }
            val nm = name.text.toString().trim().ifEmpty { Devices.defaultName(this) }.take(60)
            val parent = NotionClient.parsePageId(link.text.toString()).takeIf { link.text.toString().isNotBlank() }
                ?: whereFor[where.checkedRadioButtonId]
            if (parent == null) { toast("Choose where to put this phone's backup page, or paste a page link."); return@button }
            createNew(nm, parent)
        }
    }

    private fun createNew(name: String, parent: String) {
        busy("Making “$name Backup” in Notion…")
        bg({
            val title = "$name Backup"
            Registry(this).createPage(parent, title) to title
        }) { r, e ->
            if (e != null) {
                val msg = if (e is NotionException && e.status in listOf(400, 403, 404))
                    "Notion wouldn't let the app make a page there. Open that page in Notion → ••• → Connections → add your integration, then try again."
                else "Couldn't make the page: ${e.message}"
                toast(msg)
                afterSignIn()
                return@bg
            }
            @Suppress("UNCHECKED_CAST")
            val pair = r as Pair<String, String>
            val (page, title) = pair
            SetupSync.newDevice(this, js, name, page, title)
            finishSetup()
        }
    }

    // ---- first device ever: connect Notion ---------------------------------------------------

    private fun stepConnect() {
        clear()
        title("Connect Notion", "This is your first device. Paste your Notion integration secret and the link to the page this phone should back up to.")
        val tok = field("Integration secret (notion.so/profile/integrations)", password = true)
        val link = field("Backup page link")
        text("Open the page in Notion → ••• → Connections → add your integration, then Share → Copy link.", 12f, 0.6f, 6)
        val name = field("Name this phone", Devices.defaultName(this))
        val msg = text("", 14f, 1f, 8)
        button("Continue") {
            val t = tok.text.toString().trim()
            val pid = NotionClient.parsePageId(link.text.toString())
            if (t.isEmpty() || pid == null) { msg.text = "Paste both the secret and the page link."; return@button }
            msg.text = "Checking…"
            bg({ NotionClient(t).pageTitle(pid) }) { title, e ->
                if (e != null) { msg.text = "Couldn't open that page: ${e.message}"; return@bg }
                prefs.token = t
                js = JSONObject(js?.toString() ?: "{}").put("notion_token", t)
                SetupSync.newDevice(this, js, name.text.toString().trim().ifEmpty { Devices.defaultName(this) }, pid, title as String)
                finishSetup()
            }
        }
    }

    // ---- done ----------------------------------------------------------------------------------

    private fun finishSetup() {
        prefs.signedIn = method.ifEmpty { "code" }
        busy("Finishing…")
        bg({
            val reg = Registry(this)
            reg.registerMe()
            reg.all()
            null
        }) { _, _ ->
            if (prefs.googleEmail.isNotEmpty()) account.saveNow(interactive = false)
            ClipService.start(this)
            if (prefs.autoSync) Scheduler.apply(this)
            startActivity(Intent(this, MainActivity::class.java))
            finish()
        }
    }
}
