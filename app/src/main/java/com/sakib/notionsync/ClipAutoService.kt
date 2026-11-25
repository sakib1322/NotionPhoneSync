package com.sakib.notionsync

import android.accessibilityservice.AccessibilityService
import android.content.ClipDescription
import android.content.ClipboardManager
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.graphics.PixelFormat
import android.net.Uri
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.provider.Settings
import android.view.Gravity
import android.view.View
import android.view.WindowManager
import android.view.accessibility.AccessibilityEvent
import android.widget.Toast

/**
 * Sends what you copy on the phone to your other devices with no tap.
 *
 * Android only lets the app on screen (or the keyboard) read the clipboard. This helper
 * notices when you copy something (you tap "Copy", or an app says "Copied"), then for a split
 * second shows an invisible 1-pixel window so the app has focus, reads the clipboard, removes
 * the window and sends it. It reads nothing on the screen: only the label of the button you
 * tapped (to see if it says "Copy") and short pop-up messages like "Copied to clipboard".
 */
class ClipAutoService : AccessibilityService() {

    companion object {
        @Volatile var running: ClipAutoService? = null

        /** Is the helper switched on in Android's Accessibility settings? */
        fun enabled(ctx: Context): Boolean {
            val on = Settings.Secure.getString(ctx.contentResolver, Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES) ?: return false
            val me = ComponentName(ctx, ClipAutoService::class.java)
            return on.split(':').any {
                ComponentName.unflattenFromString(it)?.let { c -> c == me } ?: false
            }
        }

        // What "Copy" looks like on buttons and pop-ups (English and a few common languages).
        private val COPY_WORDS = listOf(
            "copy", "copied", "copy text", "copy link", "copy to clipboard", "copied to clipboard",
            "কপি", "কপি করুন", "কপি করা হয়েছে", "kopieren", "copiar", "copier", "copia", "копировать", "复制", "コピー"
        )
        private val NOT_COPY = listOf("copyright", "copy of", "copies")
        // Password managers: never send what you copy there.
        private val SKIP_PACKAGES = setOf(
            "com.x8bit.bitwarden", "com.lastpass.lpandroid", "com.agilebits.onepassword", "com.dashlane",
            "keepass2android.keepass2android", "com.kunzisoft.keepass.free", "com.google.android.gms",
            "com.samsung.android.samsungpassautofill", "com.android.settings"
        )
    }

    private val main = Handler(Looper.getMainLooper())
    private var lastTrigger = 0L
    private var overlay: View? = null

    private var calls: CallRecorder? = null

    override fun onServiceConnected() {
        super.onServiceConnected()
        running = this
        Prefs(this).helperEverOn = true
        calls = try { CallRecorder(this).also { it.start() } } catch (t: Throwable) {
            Logger.log(this, "Call recording couldn't start (${t.javaClass.simpleName}); copying still works.")
            null
        }
        Logger.log(this, "Shared clipboard: automatic sending is on — copies go to your other devices with no tap.")
        main.postDelayed(probeTick, 60_000)
    }

    /** Every 2 hours: is a more powerful AI model available again? (Writing helper, Auto mode.) */
    private val probeTick = object : Runnable {
        override fun run() {
            val p = Prefs(this@ClipAutoService)
            if (p.writerOn && AiWriter.model(p) == AiWriter.AUTO && AiWriter.key(p).isNotBlank() &&
                System.currentTimeMillis() - p.writerProbeAt > 2 * 3600_000L) {
                Thread { try { AiWriter.probe(p) } catch (t: Throwable) { } }.start()
            }
            main.postDelayed(this, 10 * 60_000L)
        }
    }

    override fun onDestroy() {
        main.removeCallbacks(probeTick)
        try { calls?.stop() } catch (t: Throwable) { }
        calls = null
        if (running === this) running = null
        removeOverlay()
        super.onDestroy()
    }

    override fun onInterrupt() {}

    override fun onAccessibilityEvent(e: AccessibilityEvent?) {
        // Never let anything here crash: Android switches off a helper that crashes.
        try { handle(e) } catch (t: Throwable) { Logger.log(this, "Helper: skipped an event (${t.javaClass.simpleName}).") }
    }

    private fun handle(e: AccessibilityEvent?) {
        e ?: return
        val pkg = e.packageName?.toString().orEmpty()
        // Which app is on screen (to name call recordings after it).
        if (e.eventType == AccessibilityEvent.TYPE_WINDOW_STATE_CHANGED && pkg.isNotEmpty() &&
            pkg != packageName && pkg != "com.android.systemui") calls?.foreground = pkg
        if (pkg == packageName || pkg in SKIP_PACKAGES) return
        if (e.isPassword) return
        val p = Prefs(this)
        if (e.eventType == AccessibilityEvent.TYPE_VIEW_TEXT_CHANGED) {
            if (p.writerOn) writerCheck(e, p)
            return
        }
        if (p.signedIn.isEmpty() || !p.clipboardSync || !p.clipAuto) return
        val words = buildString {
            e.text?.forEach { append(it).append(' ') }
            e.contentDescription?.let { append(it) }
        }.trim().lowercase()
        if (words.isEmpty() || words.length > 60) return
        val isCopy = when (e.eventType) {
            // You tapped a button or menu item labelled "Copy…".
            AccessibilityEvent.TYPE_VIEW_CLICKED -> COPY_WORDS.any { w -> words == w || words.startsWith("$w ") } &&
                NOT_COPY.none { words.contains(it) }
            // A pop-up such as "Copied to clipboard" / "Link copied".
            AccessibilityEvent.TYPE_NOTIFICATION_STATE_CHANGED, AccessibilityEvent.TYPE_ANNOUNCEMENT,
            AccessibilityEvent.TYPE_WINDOW_STATE_CHANGED -> words.contains("copied") || words.contains("কপি করা")
            else -> false
        }
        if (!isCopy) return
        val now = System.currentTimeMillis()
        if (now - lastTrigger < 1200) return
        lastTrigger = now
        Logger.log(this, "Helper: noticed a copy in ${appLabel(pkg)} (“${words.take(30)}”).")
        // Give the app a moment to actually put it on the clipboard.
        main.postDelayed({ try { grab() } catch (t: Throwable) { removeOverlay() } }, 350)
    }

    /** Briefly takes focus with an invisible 1-pixel window, reads the clipboard, sends it. */
    private fun grab() {
        if (overlay != null) return
        val wm = getSystemService(WindowManager::class.java)
        val v = object : View(this) {
            override fun onWindowFocusChanged(hasWindowFocus: Boolean) {
                super.onWindowFocusChanged(hasWindowFocus)
                if (hasWindowFocus) {
                    readAndSend()
                    removeOverlay()
                }
            }
        }
        val lp = WindowManager.LayoutParams(
            1, 1, WindowManager.LayoutParams.TYPE_ACCESSIBILITY_OVERLAY,
            WindowManager.LayoutParams.FLAG_NOT_TOUCHABLE or WindowManager.LayoutParams.FLAG_LAYOUT_NO_LIMITS,
            PixelFormat.TRANSLUCENT
        ).apply {
            gravity = Gravity.TOP or Gravity.START
            alpha = 0f
        }
        try {
            wm.addView(v, lp)
            overlay = v
        } catch (e: Exception) {
            Logger.log(this, "Helper: no invisible window allowed; using the brief screen instead.")
            fallback()
            return
        }
        // If focus never came (some phones), clean up and try the brief invisible screen instead.
        main.postDelayed({
            if (overlay === v) {
                removeOverlay()
                Logger.log(this, "Helper: the invisible window didn't get focus; using the brief screen instead.")
                fallback()
            }
        }, 700)
    }

    private fun fallback() {
        try {
            startActivity(Intent(this, ClipSendActivity::class.java)
                .putExtra("auto", true)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_NO_ANIMATION))
        } catch (e: Exception) { }
    }

    private fun removeOverlay() {
        val v = overlay ?: return
        overlay = null
        try { getSystemService(WindowManager::class.java).removeView(v) } catch (e: Exception) { }
    }

    private fun readAndSend() {
        try {
            val got = ClipSend.read(this)
            if (got == null) {
                Logger.log(this, "Helper: couldn't read the clipboard (Android said no, or it was empty).")
                return
            }
            if (ClipSend.sendIfNew(this, got.first, got.second, quiet = true)) Logger.log(this, "Helper: sending it to your other devices.")
            else Logger.log(this, "Helper: that's what was just sent or received; not sending again.")
        } catch (t: Throwable) {
            Logger.log(this, "Helper: couldn't read the clipboard (${t.javaClass.simpleName}).")
        }
    }

    // ---- AI writing helper: text ending in /im (etc.) is rewritten in place ----

    @Volatile private var writing = false

    private fun writerCheck(e: AccessibilityEvent, p: Prefs) {
        if (writing) return
        val node = e.source ?: return
        if (node.isPassword) return
        val text = (node.text ?: e.text?.firstOrNull() ?: return).toString()
        // Some apps show their hint text when the box is empty; ignore that.
        if (node.isShowingHintText) return
        val (trig, cmd) = AiWriter.findTrigger(p, text) ?: return
        val original = text.dropLast(trig.length)
        if (original.isBlank()) return
        val instruction = AiWriter.commands(p)[cmd] ?: return
        if (AiWriter.key(p).isBlank()) {
            Toast.makeText(this, "Writing helper: add an AI key in Notion Sync › Settings first.", Toast.LENGTH_LONG).show()
            return
        }
        writing = true
        setText(node, original.trimEnd() + " ⏳")
        Toast.makeText(this, "✨ Writing…", Toast.LENGTH_SHORT).show()
        Thread {
            val result = try { AiWriter.rewrite(p, instruction, original.trimEnd()) } catch (t: Throwable) {
                main.post {
                    try { node.refresh(); setText(node, original) } catch (x: Throwable) { }
                    Toast.makeText(this, "Writing helper: ${t.message}", Toast.LENGTH_LONG).show()
                    writing = false
                }
                Logger.log(this, "Writing helper: ${t.message}")
                return@Thread
            }
            main.post {
                try {
                    node.refresh()
                    val now = node.text?.toString() ?: ""
                    // Only replace if you haven't typed something else meanwhile.
                    if (now == original.trimEnd() + " ⏳" || now.isEmpty() || now == original) setText(node, result)
                    else Toast.makeText(this, "Text changed while writing; left as it is.", Toast.LENGTH_SHORT).show()
                } catch (x: Throwable) { }
                writing = false
            }
        }.start()
    }

    private fun setText(node: android.view.accessibility.AccessibilityNodeInfo, t: String) {
        val args = android.os.Bundle()
        args.putCharSequence(android.view.accessibility.AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, t)
        node.performAction(android.view.accessibility.AccessibilityNodeInfo.ACTION_SET_TEXT, args)
        val sel = android.os.Bundle()
        sel.putInt(android.view.accessibility.AccessibilityNodeInfo.ACTION_ARGUMENT_SELECTION_START_INT, t.length)
        sel.putInt(android.view.accessibility.AccessibilityNodeInfo.ACTION_ARGUMENT_SELECTION_END_INT, t.length)
        node.performAction(android.view.accessibility.AccessibilityNodeInfo.ACTION_SET_SELECTION, sel)
    }

    private fun appLabel(pkg: String): String = try {
        packageManager.getApplicationLabel(packageManager.getApplicationInfo(pkg, 0)).toString()
    } catch (e: Exception) { pkg }
}
