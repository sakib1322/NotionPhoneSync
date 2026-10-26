package com.sakib.notionsync

import android.content.Context
import android.graphics.Canvas
import android.graphics.LinearGradient
import android.graphics.Paint
import android.graphics.Path
import android.graphics.Shader
import android.util.AttributeSet
import android.view.View
import com.google.android.material.color.MaterialColors

/** A small line chart of the last minute of upload speed. */
class SparkView @JvmOverloads constructor(ctx: Context, attrs: AttributeSet? = null) : View(ctx, attrs) {

    private val values = FloatArray(60)
    private var count = 0
    private val color = MaterialColors.getColor(this, androidx.appcompat.R.attr.colorPrimary)
    private val gridColor = MaterialColors.getColor(this, com.google.android.material.R.attr.colorOutlineVariant)
    private val line = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.STROKE
        strokeWidth = 2.5f * resources.displayMetrics.density
        strokeJoin = Paint.Join.ROUND
        strokeCap = Paint.Cap.ROUND
        this.color = this@SparkView.color
    }
    private val fill = Paint(Paint.ANTI_ALIAS_FLAG).apply { style = Paint.Style.FILL }
    private val grid = Paint().apply { this.color = gridColor; strokeWidth = resources.displayMetrics.density }
    private val path = Path()
    private val area = Path()

    fun push(v: Double) {
        System.arraycopy(values, 1, values, 0, values.size - 1)
        values[values.size - 1] = v.toFloat()
        if (count < values.size) count++
        invalidate()
    }

    override fun onDraw(canvas: Canvas) {
        val w = width.toFloat()
        val h = height.toFloat()
        val pad = line.strokeWidth
        canvas.drawLine(0f, h - 1, w, h - 1, grid)
        if (count < 2) return
        val max = (values.maxOrNull() ?: 0f).coerceAtLeast(1024f) * 1.15f
        val step = w / (values.size - 1)
        path.reset()
        area.reset()
        val first = values.size - count
        for (i in first until values.size) {
            val x = i * step
            val y = h - pad - (values[i] / max) * (h - 2 * pad)
            if (i == first) {
                path.moveTo(x, y)
                area.moveTo(x, h)
                area.lineTo(x, y)
            } else {
                path.lineTo(x, y)
                area.lineTo(x, y)
            }
        }
        area.lineTo(w, h)
        area.close()
        fill.shader = LinearGradient(
            0f, 0f, 0f, h,
            (color and 0x00FFFFFF) or 0x55000000, color and 0x00FFFFFF, Shader.TileMode.CLAMP
        )
        canvas.drawPath(area, fill)
        canvas.drawPath(path, line)
    }
}
