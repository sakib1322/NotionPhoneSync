package com.sakib.notionsync

import android.app.Activity
import android.content.Context
import android.content.Intent
import android.os.Bundle
import android.view.WindowManager
import java.lang.ref.WeakReference

/**
 * An invisible screen placed on top during a call so the app counts as "on screen" (Android
 * only lets an app record during a call then). You can't see it and every tap goes straight
 * through to the call screen underneath. It closes when the call ends.
 */
class CallTopActivity : Activity() {
    companion object {
        private var current: WeakReference<CallTopActivity>? = null

        fun show(ctx: Context) {
            try {
                ctx.startActivity(Intent(ctx, CallTopActivity::class.java)
                    .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_NO_ANIMATION or Intent.FLAG_ACTIVITY_SINGLE_TOP))
            } catch (e: Exception) { }
        }

        fun hide() {
            current?.get()?.finish()
            current = null
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        window.addFlags(WindowManager.LayoutParams.FLAG_NOT_TOUCHABLE or WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE or
            WindowManager.LayoutParams.FLAG_LAYOUT_NO_LIMITS or WindowManager.LayoutParams.FLAG_SHOW_WHEN_LOCKED)
        window.setDimAmount(0f)
        current = WeakReference(this)
    }

    override fun onDestroy() {
        if (current?.get() === this) current = null
        super.onDestroy()
    }

    override fun finish() {
        super.finish()
        overridePendingTransition(0, 0)
    }
}
