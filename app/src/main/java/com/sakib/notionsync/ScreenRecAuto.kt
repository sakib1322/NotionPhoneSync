package com.sakib.notionsync

import android.accessibilityservice.AccessibilityService
import android.os.Handler
import android.os.Looper
import android.view.accessibility.AccessibilityNodeInfo

/**
 * Starts Samsung's own Screen recorder when a call starts and stops it when the call ends (Samsung's
 * recorder can capture both voices of a call, which normal apps can't). The helper does what you would:
 * opens Quick settings, taps "Screen recorder", taps "Start recording" — and at the end taps its Stop
 * button. Choose "Media sounds and mic" in the recorder's sound setting once; it remembers that.
 */
class ScreenRecAuto(private val svc: AccessibilityService) {
    private val main = Handler(Looper.getMainLooper())
    @Volatile var recording = false
        private set

    private fun log(m: String) = Logger.log(svc, "Screen recorder: $m")

    private fun allNodes(): List<AccessibilityNodeInfo> {
        val roots = ArrayList<AccessibilityNodeInfo>()
        try { svc.windows?.forEach { w -> w.root?.let { roots.add(it) } } } catch (e: Exception) { }
        svc.rootInActiveWindow?.let { roots.add(it) }
        val out = ArrayList<AccessibilityNodeInfo>()
        val stack = ArrayDeque(roots)
        var n = 0
        while (stack.isNotEmpty() && n < 4000) {
            val node = stack.removeLast(); n++
            out.add(node)
            for (i in 0 until node.childCount) node.getChild(i)?.let { stack.add(it) }
        }
        return out
    }

    private fun label(n: AccessibilityNodeInfo) = ((n.text ?: "").toString() + " " + (n.contentDescription ?: "").toString()).lowercase()

    private fun find(vararg words: String, exact: Boolean = false): AccessibilityNodeInfo? =
        allNodes().firstOrNull { n ->
            val l = label(n).trim()
            words.any { w -> if (exact) l == w || l.startsWith("$w ") || l.startsWith("$w,") else l.contains(w) }
        }

    private fun click(n: AccessibilityNodeInfo): Boolean {
        var x: AccessibilityNodeInfo? = n
        var hops = 0
        while (x != null && hops < 6) {
            if (x.isClickable && x.performAction(AccessibilityNodeInfo.ACTION_CLICK)) return true
            x = x.parent; hops++
        }
        return false
    }

    /** Call started. */
    fun startRecording() {
        if (recording) return
        recording = true
        log("call started — starting Samsung's Screen recorder…")
        svc.performGlobalAction(AccessibilityService.GLOBAL_ACTION_QUICK_SETTINGS)
        findTile(0)
    }

    private fun findTile(page: Int) {
        main.postDelayed({
            val tile = find("screen recorder")
            if (tile != null && click(tile)) {
                main.postDelayed({ pressStart(0) }, 900)
                return@postDelayed
            }
            if (page < 3) {
                // Not on this page of Quick settings: swipe to the next one.
                allNodes().firstOrNull { it.isScrollable }?.performAction(AccessibilityNodeInfo.ACTION_SCROLL_FORWARD)
                findTile(page + 1)
            } else {
                svc.performGlobalAction(AccessibilityService.GLOBAL_ACTION_BACK)
                recording = false
                log("couldn't find the “Screen recorder” button in Quick settings. Add it there (pencil/edit button) and it will work next call.")
            }
        }, 800)
    }

    private fun pressStart(tries: Int) {
        // Prefer "Media sounds and mic" if the recorder asks what to record.
        find("media sounds and mic", "media sound and mic")?.let { click(it) }
        val start = find("start recording", exact = false)
        if (start != null && click(start)) {
            log("recording the call (Samsung Screen recorder).")
            main.postDelayed({ svc.performGlobalAction(AccessibilityService.GLOBAL_ACTION_BACK) }, 4500)
            return
        }
        if (tries < 4) { main.postDelayed({ pressStart(tries + 1) }, 600); return }
        recording = false
        svc.performGlobalAction(AccessibilityService.GLOBAL_ACTION_BACK)
        log("the recorder didn't show its “Start recording” button; recording wasn't started.")
    }

    /** Call ended. */
    fun stopRecording() {
        if (!recording) return
        log("call ended — stopping the Screen recorder…")
        // 1) Its floating stop button, if it's on screen.
        val floating = allNodes().firstOrNull { n ->
            val l = label(n); (l.contains("stop recording") || l.trim() == "stop") && n.packageName?.toString()?.contains("screenrecorder") == true
        }
        if (floating != null && click(floating)) { done(); return }
        // 2) Otherwise its notification's Stop button.
        svc.performGlobalAction(AccessibilityService.GLOBAL_ACTION_NOTIFICATIONS)
        stopFromShade(0)
    }

    private fun stopFromShade(tries: Int) {
        main.postDelayed({
            val stop = find("stop recording") ?: allNodes().firstOrNull { label(it).trim() == "stop" }
            if (stop != null && click(stop)) {
                main.postDelayed({ svc.performGlobalAction(AccessibilityService.GLOBAL_ACTION_BACK) }, 600)
                done()
            } else if (tries < 4) stopFromShade(tries + 1)
            else {
                svc.performGlobalAction(AccessibilityService.GLOBAL_ACTION_BACK)
                recording = false
                log("couldn't find the recorder's Stop button — stop it yourself from the notification.")
            }
        }, 700)
    }

    private fun done() {
        recording = false
        log("stopped. The video is in Gallery (Screen recordings).")
    }
}
