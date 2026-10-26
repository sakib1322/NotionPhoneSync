package com.sakib.notionsync

import android.content.ActivityNotFoundException
import android.content.Intent
import android.graphics.BitmapFactory
import android.net.Uri
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.widget.ImageView
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.FileProvider
import androidx.media3.common.MediaItem
import androidx.media3.common.Player
import androidx.media3.exoplayer.ExoPlayer
import androidx.media3.ui.PlayerView
import androidx.recyclerview.widget.RecyclerView
import androidx.viewpager2.widget.ViewPager2
import com.davemorrissey.labs.subscaleview.ImageSource
import com.davemorrissey.labs.subscaleview.SubsamplingScaleImageView
import com.google.android.material.button.MaterialButton
import com.google.android.material.dialog.MaterialAlertDialogBuilder
import java.io.File
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/** What the viewer shows (set just before opening it). */
object ViewerData {
    var items: List<FilesPage.FileItem> = emptyList()
    var start = 0
}

/** Full-screen photos (pinch to zoom), videos and audio; swipe between them. */
class ViewerActivity : AppCompatActivity() {

    private lateinit var pager: ViewPager2
    private lateinit var topBar: View
    private lateinit var title: TextView
    private lateinit var sub: TextView
    private val items = ViewerData.items
    private val main = Handler(Looper.getMainLooper())
    private var player: ExoPlayer? = null
    private var playerHolder: Holder? = null
    private val thumbs by lazy { Thumbs(this) }
    private val dateFmt = SimpleDateFormat("d MMM yyyy, HH:mm", Locale.US)

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        if (items.isEmpty()) {
            finish()
            return
        }
        setContentView(R.layout.activity_viewer)
        window.statusBarColor = 0xFF000000.toInt()
        window.navigationBarColor = 0xFF000000.toInt()
        pager = findViewById(R.id.pager)
        topBar = findViewById(R.id.top_bar)
        title = findViewById(R.id.v_title)
        sub = findViewById(R.id.v_sub)
        findViewById<MaterialButton>(R.id.v_back).setOnClickListener { finish() }
        findViewById<MaterialButton>(R.id.v_share).setOnClickListener { withFile(current()) { f, file -> share(f, file) } }
        findViewById<MaterialButton>(R.id.v_more).setOnClickListener { more(current()) }

        pager.adapter = Adapter()
        pager.offscreenPageLimit = 1
        pager.setCurrentItem(ViewerData.start.coerceIn(0, items.size - 1), false)
        pager.registerOnPageChangeCallback(object : ViewPager2.OnPageChangeCallback() {
            override fun onPageSelected(position: Int) {
                showInfo(position)
                startMediaFor(position)
                // Photos are downloaded only when you actually look at them.
                if (items[position].type == "image") holderAt(position)?.let { h ->
                    if (!h.loaded) (pager.adapter as Adapter).loadImage(h, items[position])
                }
            }
        })
        showInfo(pager.currentItem)
        pager.post { startMediaFor(pager.currentItem) }
    }

    private fun current() = items[pager.currentItem]

    private fun showInfo(pos: Int) {
        val f = items[pos]
        title.text = f.name
        val parts = listOfNotNull(
            if (f.mtime > 0) dateFmt.format(Date(f.mtime)) else f.dateText.ifEmpty { null },
            if (f.size > 0) SpeedMeter.size(f.size) else null,
            "${pos + 1} of ${items.size}"
        )
        sub.text = parts.joinToString(" · ")
    }

    private fun toggleBar() {
        topBar.visibility = if (topBar.visibility == View.VISIBLE) View.GONE else View.VISIBLE
    }

    // ---- media playback ---------------------------------------------------------------------

    private fun holderAt(pos: Int): Holder? =
        (pager.getChildAt(0) as? RecyclerView)?.findViewHolderForAdapterPosition(pos) as? Holder

    private fun startMediaFor(pos: Int) {
        player?.pause()
        playerHolder?.playerView?.player = null
        val f = items[pos]
        if (f.type != "video" && f.type != "audio") return
        val h = holderAt(pos) ?: return
        h.loading.visibility = View.VISIBLE
        h.status.text = "Getting ready…"
        Thread {
            val uri: Uri? = try {
                CloudFiles.local(f)?.let { Uri.fromFile(it) }
                    ?: CloudFiles.cacheFile(this, f).takeIf { it.exists() && it.length() > 0 }?.let { Uri.fromFile(it) }
                    ?: CloudFiles.freshUrl(this, f)?.let { Uri.parse(it) }
            } catch (e: Exception) {
                null
            }
            main.post {
                if (isFinishing || pager.currentItem != pos) return@post
                h.loading.visibility = View.GONE
                if (uri == null) {
                    Toast.makeText(this, "Couldn't play this file (no internet?).", Toast.LENGTH_LONG).show()
                    return@post
                }
                val p = player ?: ExoPlayer.Builder(this).build().also { player = it }
                h.playerView.player = p
                playerHolder = h
                p.setMediaItem(MediaItem.fromUri(uri))
                p.repeatMode = Player.REPEAT_MODE_OFF
                p.prepare()
                p.play()
            }
        }.start()
    }

    override fun onStop() {
        super.onStop()
        player?.pause()
    }

    override fun onDestroy() {
        super.onDestroy()
        player?.release()
        player = null
    }

    // ---- actions ------------------------------------------------------------------------------

    private fun withFile(f: FilesPage.FileItem, then: (FilesPage.FileItem, File) -> Unit) {
        val dialog = MaterialAlertDialogBuilder(this).setTitle("Getting file…").setMessage("Starting…").setCancelable(true).show()
        Thread {
            try {
                val file = CloudFiles.obtain(this, f) { done, total ->
                    if (total > 0) main.post { dialog.setMessage("${done * 100 / total}% of ${SpeedMeter.size(total)}") }
                }
                main.post {
                    if (dialog.isShowing) {
                        dialog.dismiss()
                        then(f, file)
                    }
                }
            } catch (e: Exception) {
                main.post {
                    dialog.dismiss()
                    Toast.makeText(this, "Couldn't get the file: ${e.message}", Toast.LENGTH_LONG).show()
                }
            }
        }.start()
    }

    private fun uriFor(file: File): Uri = FileProvider.getUriForFile(this, "$packageName.files", file)

    private fun share(f: FilesPage.FileItem, file: File) {
        startActivity(
            Intent.createChooser(
                Intent(Intent.ACTION_SEND).setType(FileTypes.mime(f.name)).putExtra(Intent.EXTRA_STREAM, uriFor(file))
                    .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION), "Share"
            )
        )
    }

    private fun openWith(f: FilesPage.FileItem, file: File) {
        try {
            startActivity(
                Intent.createChooser(
                    Intent(Intent.ACTION_VIEW).setDataAndType(uriFor(file), FileTypes.mime(f.name))
                        .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION), "Open with"
                )
            )
        } catch (e: ActivityNotFoundException) {
            Toast.makeText(this, "No app can open this file type.", Toast.LENGTH_LONG).show()
        }
    }

    private fun more(f: FilesPage.FileItem) {
        MaterialAlertDialogBuilder(this)
            .setItems(arrayOf("Open with…", "Details")) { _, i ->
                if (i == 0) withFile(f) { a, b -> openWith(a, b) }
                else MaterialAlertDialogBuilder(this)
                    .setTitle(f.name)
                    .setMessage(
                        listOfNotNull(
                            f.row?.let { "Folder: " + Sources.title(Prefs(this), it.root) + (if (it.path.isEmpty()) "" else " › " + it.path.replace("/", " › ")) },
                            if (f.size > 0) "Size: ${SpeedMeter.size(f.size)}" else null,
                            if (f.mtime > 0) "Date: ${dateFmt.format(Date(f.mtime))}" else null,
                            f.row?.device?.let { if (it.isNotEmpty()) "From: $it" else null },
                            if (CloudFiles.local(f) != null) "Also on this phone" else null
                        ).joinToString("\n")
                    )
                    .setPositiveButton("OK", null)
                    .show()
            }
            .show()
    }

    // ---- pages ---------------------------------------------------------------------------------

    class Holder(v: View) : RecyclerView.ViewHolder(v) {
        val thumb: ImageView = v.findViewById(R.id.p_thumb)
        val image: SubsamplingScaleImageView = v.findViewById(R.id.p_image)
        val playerView: PlayerView = v.findViewById(R.id.p_player)
        val other: View = v.findViewById(R.id.p_other)
        val icon: ImageView = v.findViewById(R.id.p_icon)
        val name: TextView = v.findViewById(R.id.p_name)
        val open: MaterialButton = v.findViewById(R.id.p_open)
        val loading: View = v.findViewById(R.id.p_loading)
        val status: TextView = v.findViewById(R.id.p_status)
        var boundBlock: String? = null
        var loaded = false
    }

    inner class Adapter : RecyclerView.Adapter<Holder>() {
        override fun getItemCount() = items.size

        override fun onCreateViewHolder(parent: ViewGroup, viewType: Int) =
            Holder(LayoutInflater.from(parent.context).inflate(R.layout.item_viewer_page, parent, false))

        override fun onBindViewHolder(h: Holder, position: Int) {
            val f = items[position]
            h.boundBlock = f.block
            h.loaded = false
            h.image.recycle()
            h.image.visibility = View.GONE
            h.playerView.visibility = View.GONE
            h.other.visibility = View.GONE
            h.loading.visibility = View.GONE
            h.thumb.setImageDrawable(null)
            h.thumb.visibility = View.VISIBLE
            h.thumb.setOnClickListener { toggleBar() }
            when (f.type) {
                "image" -> bindImage(h, f, position == pager.currentItem)
                "video", "audio" -> {
                    h.playerView.visibility = View.VISIBLE
                    if (f.type == "video") thumbs.load(f.block, CloudFiles.local(f), null, true, false, h.thumb)
                    else {
                        h.thumb.visibility = View.GONE
                        h.playerView.setDefaultArtwork(getDrawable(R.drawable.ic_audio))
                    }
                    h.playerView.setOnClickListener { toggleBar() }
                }
                else -> {
                    h.thumb.visibility = View.GONE
                    h.other.visibility = View.VISIBLE
                    h.icon.setImageResource(if (f.type == "pdf") R.drawable.ic_file else R.drawable.ic_file)
                    h.name.text = f.name
                    h.open.setOnClickListener { withFile(f) { a, b -> openWith(a, b) } }
                }
            }
        }

        private fun bindImage(h: Holder, f: FilesPage.FileItem, isCurrent: Boolean) {
            thumbs.load(f.block, CloudFiles.local(f), null, false, false, h.thumb)
            h.thumb.scaleType = ImageView.ScaleType.FIT_CENTER
            val onPhone = CloudFiles.local(f) != null || CloudFiles.cacheFile(this@ViewerActivity, f).let { it.exists() && it.length() > 0 }
            if (onPhone || isCurrent) loadImage(h, f)
        }

        fun loadImage(h: Holder, f: FilesPage.FileItem) {
            h.loaded = true
            h.loading.visibility = View.VISIBLE
            h.status.text = ""
            Thread {
                val file = try {
                    CloudFiles.obtain(this@ViewerActivity, f) { done, total ->
                        if (total > 0) main.post { if (h.boundBlock == f.block) h.status.text = "${done * 100 / total}%" }
                    }
                } catch (e: Exception) {
                    null
                }
                main.post {
                    if (h.boundBlock != f.block) return@post
                    h.loading.visibility = View.GONE
                    if (file == null) {
                        h.status.text = "Couldn't load (no internet?)"
                        h.loading.visibility = View.VISIBLE
                        return@post
                    }
                    h.image.orientation = SubsamplingScaleImageView.ORIENTATION_USE_EXIF
                    h.image.setOnImageEventListener(object : SubsamplingScaleImageView.DefaultOnImageEventListener() {
                        override fun onImageLoaded() {
                            h.thumb.visibility = View.GONE
                        }

                        override fun onImageLoadError(e: Exception?) {
                            // Formats the zoom view can't read (e.g. some GIF/HEIC): show a plain picture instead.
                            h.image.visibility = View.GONE
                            val o = BitmapFactory.Options().apply { inSampleSize = 2 }
                            BitmapFactory.decodeFile(file.path, o)?.let { h.thumb.setImageBitmap(it) }
                        }
                    })
                    h.image.setOnClickListener { toggleBar() }
                    h.image.visibility = View.VISIBLE
                    h.image.setImage(ImageSource.uri(Uri.fromFile(file)))
                }
            }.start()
        }
    }
}
