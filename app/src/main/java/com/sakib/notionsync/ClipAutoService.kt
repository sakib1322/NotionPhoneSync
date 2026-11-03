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

    override fun onServiceConnected() {
        super.onServiceConnected()
        running = this
        Logger.log(this, "Shared clipboard: automatic sending is on — copies go to your other devices with no tap.")
    }

    override fun onDestroy() {
        if (running === this) running = null
        removeOverlay()
        super.onDestroy()
    }

    override fun onInterrupt() {}

    override fun onAccessibilityEvent(e: AccessibilityEvent?) {
        e ?: return
        val pkg = e.packageName?.toString().orEmpty()
        if (pkg == packageName || pkg in SKIP_PACKAGES) return
        if (e.isPassword) return
        val p = Prefs(this)
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
        // Give the app a moment to actually put it on the clipboard.
        main.postDelayed({ grab() }, 350)
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
            fallback()
            return
        }
        // If focus never came (some phones), clean up and try the brief invisible screen instead.
        main.postDelayed({
            if (overlay === v) {
                removeOverlay()
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
        val got = ClipSend.read(this) ?: return
        ClipSend.sendIfNew(this, got.first, got.second, quiet = true)
    }
}
