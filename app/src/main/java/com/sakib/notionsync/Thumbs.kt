package com.sakib.notionsync

import android.content.Context
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.graphics.Matrix
import android.media.ExifInterface
import android.media.ThumbnailUtils
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.provider.MediaStore
import android.util.LruCache
import android.util.Size
import android.widget.ImageView
import okhttp3.OkHttpClient
import okhttp3.Request
import java.io.File
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit

/**
 * Small previews for the Files tab. Uses the copy still on the phone when
 * there is one (no data used); otherwise downloads the photo from Notion once
 * and keeps a small preview on the phone.
 */
class Thumbs(ctx: Context) {
    private val app = ctx.applicationContext
    private val dir = File(app.cacheDir, "thumbs").apply { mkdirs() }
    private val pool = Executors.newFixedThreadPool(3)
    private val main = Handler(Looper.getMainLooper())
    private val mem = object : LruCache<String, Bitmap>(24 * 1024 * 1024) {
        override fun sizeOf(key: String, value: Bitmap) = value.byteCount
    }
    private val http = OkHttpClient.Builder()
        .connectTimeout(20, TimeUnit.SECONDS)
        .readTimeout(60, TimeUnit.SECONDS)
        .build()

    /** [url] is used only when [allowDownload] is true and no phone copy exists. */
    fun load(
        id: String, local: File?, url: String?, isVideo: Boolean, allowDownload: Boolean, target: ImageView,
        fetchUrl: (() -> String?)? = null
    ) {
        target.tag = id
        mem.get(id)?.let {
            show(target, it)
            return
        }
        pool.execute {
            if (target.tag != id) return@execute
            val bmp = try {
                fromDisk(id) ?: when {
                    local != null && local.exists() -> fromLocal(local, isVideo)?.also { save(id, it) }
                    !isVideo && allowDownload -> {
                        val u = url ?: try { fetchUrl?.invoke() } catch (e: Exception) { null }
                        if (u != null) fromUrl(id, u)?.also { save(id, it) } else null
                    }
                    else -> null
                }
            } catch (e: Throwable) {
                null
            }
            if (bmp != null) {
                mem.put(id, bmp)
                main.post { if (target.tag == id) show(target, bmp) }
            }
        }
    }

    private fun show(target: ImageView, bmp: Bitmap) {
        target.setPadding(0, 0, 0, 0)
        target.imageTintList = null
        target.scaleType = ImageView.ScaleType.CENTER_CROP
        target.setImageBitmap(bmp)
    }

    private fun fromDisk(id: String): Bitmap? {
        val f = File(dir, "$id.jpg")
        return if (f.exists()) BitmapFactory.decodeFile(f.path) else null
    }

    private fun save(id: String, bmp: Bitmap) {
        try {
            File(dir, "$id.jpg").outputStream().use { bmp.compress(Bitmap.CompressFormat.JPEG, 80, it) }
        } catch (e: Exception) {
        }
    }

    private fun fromLocal(f: File, isVideo: Boolean): Bitmap? {
        if (isVideo) {
            return if (Build.VERSION.SDK_INT >= 29) {
                ThumbnailUtils.createVideoThumbnail(f, Size(320, 320), null)
            } else {
                @Suppress("DEPRECATION")
                ThumbnailUtils.createVideoThumbnail(f.path, MediaStore.Images.Thumbnails.MINI_KIND)
            }
        }
        return decodeSampled(f)
    }

    private fun fromUrl(id: String, url: String): Bitmap? {
        val tmp = File(dir, "$id.part")
        try {
            http.newCall(Request.Builder().url(url).build()).execute().use { r ->
                if (!r.isSuccessful) return null
                r.body?.byteStream()?.use { input -> tmp.outputStream().use { input.copyTo(it) } } ?: return null
            }
            return decodeSampled(tmp)
        } finally {
            tmp.delete()
        }
    }

    private fun decodeSampled(f: File, want: Int = 320): Bitmap? {
        val o = BitmapFactory.Options().apply { inJustDecodeBounds = true }
        BitmapFactory.decodeFile(f.path, o)
        if (o.outWidth <= 0) return null
        var sample = 1
        while (o.outWidth / (sample * 2) >= want && o.outHeight / (sample * 2) >= want) sample *= 2
        var b = BitmapFactory.decodeFile(f.path, BitmapFactory.Options().apply { inSampleSize = sample }) ?: return null
        // Phone photos are often stored sideways with a "rotate me" note; apply it.
        val degrees = try {
            when (ExifInterface(f.path).getAttributeInt(ExifInterface.TAG_ORIENTATION, ExifInterface.ORIENTATION_NORMAL)) {
                ExifInterface.ORIENTATION_ROTATE_90 -> 90f
                ExifInterface.ORIENTATION_ROTATE_180 -> 180f
                ExifInterface.ORIENTATION_ROTATE_270 -> 270f
                else -> 0f
            }
        } catch (e: Exception) {
            0f
        }
        if (degrees != 0f) {
            b = Bitmap.createBitmap(b, 0, 0, b.width, b.height, Matrix().apply { postRotate(degrees) }, true)
        }
        val side = minOf(b.width, b.height)
        val sq = Bitmap.createBitmap(b, (b.width - side) / 2, (b.height - side) / 2, side, side)
        return if (side > want) Bitmap.createScaledBitmap(sq, want, want, true) else sq
    }
}
