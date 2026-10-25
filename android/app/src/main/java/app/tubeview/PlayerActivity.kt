package app.tubeview

import android.app.AlertDialog
import android.app.PendingIntent
import android.app.PictureInPictureParams
import android.app.RemoteAction
import android.content.BroadcastReceiver
import android.content.Context
import android.content.IntentFilter
import android.content.pm.PackageManager
import android.graphics.drawable.Icon
import android.text.SpannableString
import android.text.Spanned
import android.text.style.ForegroundColorSpan
import android.os.Build
import android.app.UiModeManager
import android.content.Intent
import android.content.res.Configuration
import android.graphics.Color
import android.graphics.Typeface
import android.net.Uri
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.util.Rational
import android.util.TypedValue
import android.view.Gravity
import android.view.KeyEvent
import android.view.View
import android.view.ViewGroup
import android.view.ViewGroup.LayoutParams.MATCH_PARENT
import android.view.ViewGroup.LayoutParams.WRAP_CONTENT
import android.widget.Button
import android.widget.FrameLayout
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import android.widget.Toast
import androidx.activity.ComponentActivity
import androidx.activity.OnBackPressedCallback
import androidx.annotation.OptIn
import androidx.core.content.ContextCompat
import androidx.core.view.WindowCompat
import androidx.lifecycle.Lifecycle
import androidx.core.view.WindowInsetsCompat
import androidx.core.view.WindowInsetsControllerCompat
import androidx.media3.common.AudioAttributes
import androidx.media3.common.C
import androidx.media3.common.VideoSize
import androidx.media3.common.MediaItem
import androidx.media3.common.MimeTypes
import androidx.media3.common.PlaybackException
import androidx.media3.common.Player
import androidx.media3.common.util.UnstableApi
import androidx.media3.datasource.DefaultHttpDataSource
import androidx.media3.exoplayer.DefaultLoadControl
import androidx.media3.exoplayer.ExoPlayer
import androidx.media3.exoplayer.hls.HlsMediaSource
import androidx.media3.exoplayer.source.DefaultMediaSourceFactory
import androidx.media3.exoplayer.source.MediaSource
import androidx.media3.exoplayer.source.MergingMediaSource
import androidx.media3.exoplayer.source.ProgressiveMediaSource
import androidx.media3.ui.PlayerView
import org.json.JSONArray
import org.json.JSONObject
import java.net.URL
import kotlin.concurrent.thread

/**
 * Plays videos natively (hardware decoding, exact seeking) from the stream URLs the in-app server resolves.
 * YouTube serves video and audio separately; ExoPlayer plays both together, so no ffmpeg is needed on the phone.
 * Also: autoplay through the list it was opened from, SponsorBlock, subtitles, resume, picture-in-picture.
 * Background play: leaving the app (Home, the screen going off) keeps the video's sound going, with controls in the
 * notification; on phones, picture-in-picture instead (automatic on Android 12 and later, or the PiP button).
 * Lyrics: the Lyrics button fills the screen with the song's lyrics (word by word when timed so) over the dimmed
 * video, the controls staying along the bottom.
 * Quality: "Auto" starts a step below the best one (it starts sooner) and steps up once playback has been smooth
 * for a while, or down if it stalls; the quality button picks one height instead.
 */
@OptIn(UnstableApi::class)
class PlayerActivity : ComponentActivity() {
    companion object {
        const val REQUEST = "req"
        const val RESULT = "result"
        private const val PIP_TOGGLE = "app.tubeview.PIP_TOGGLE"
        private const val API = "http://127.0.0.1:8765/api/"

        /** The open player, for Remote control. The page reads [remoteState] from its bridge thread, so it's a snapshot. */
        @Volatile var active: PlayerActivity? = null
        @Volatile var remoteState = "null"
    }

    private lateinit var player: ExoPlayer
    private lateinit var view: PlayerView
    private lateinit var title: TextView
    private lateinit var skipButton: Button
    private lateinit var req: JSONObject
    private val list = mutableListOf<JSONObject>()
    private var index = 0
    private val played = JSONArray()
    private var segs: List<JSONObject> = emptyList()
    private val skipped = mutableSetOf<Int>()
    private val ui = Handler(Looper.getMainLooper())
    private var loadToken = 0
    private lateinit var qualityButton: Button
    private var prefQ = 1080         // the preferred quality from Settings
    private var maxQ = 1080          // Auto never goes above it (a quality picked by hand replaces it)
    private var autoQ = true         // false once a quality is picked by hand
    private var curQ = 0             // the height playing now
    private var heights: List<Int> = emptyList()  // this video's heights, best first
    private var capQ = Int.MAX_VALUE // Auto's ceiling for this video, lowered after a stall
    private var smooth = 0           // seconds played since the last stall or switch
    private var stallSince = 0L
    private var switching = false
    private var picked = 0           // a quality picked by hand, reported back so the app remembers it
    private var inController = false // the quality button is one of the controller's buttons
    private var background = true    // keep playing when TubeView leaves the screen (Settings)
    private var pipOn = true         // picture-in-picture on phones (Settings)
    private var inBackground = false // playing with TubeView off screen, controls in the notification
    private lateinit var lyricsView: ScrollView
    private lateinit var lyricsBox: LinearLayout
    private lateinit var playButton: Button
    private var lyricLines: List<JSONObject> = emptyList()  // {t, e, text, w: [[start, end, word], ...]} in ms
    private var lyricFor = ""        // the video the lyrics are for
    private var lyricIdx = -1
    // on the car's screen: videos only while parked (paused when the car moves, carried on when it stops)
    private lateinit var parkedGuard: ParkedGuard
    private var pausedForDriving = false
    // the play/pause button in the picture-in-picture window
    private val pipReceiver = object : BroadcastReceiver() {
        override fun onReceive(c: Context, i: Intent) { if (player.isPlaying) player.pause() else player.play() }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        // opened from the notification after the video already closed: nothing to show
        val request = intent.getStringExtra(REQUEST) ?: return finish()
        req = JSONObject(request)
        val first = req.getJSONObject("video")
        req.optJSONArray("list")?.let { a -> for (i in 0 until a.length()) list.add(a.getJSONObject(i)) }
        index = list.indexOfFirst { it.optString("id") == first.optString("id") }
        if (index < 0) { list.add(0, first); index = 0 }

        background = req.optBoolean("background", true)
        pipOn = req.optBoolean("pip", true)
        prefQ = req.optInt("quality", 1080).takeIf { it > 0 } ?: 1080
        maxQ = prefQ
        // start once 1s is buffered (the default waits for 2.5s)
        val buffering = DefaultLoadControl.Builder().setBufferDurationsMs(15_000, 50_000, 1_000, 2_000).build()
        player = ExoPlayer.Builder(this).setLoadControl(buffering)
            .setSeekBackIncrementMs(10_000).setSeekForwardIncrementMs(10_000)
            // pauses for calls and other apps' sound, and when headphones are unplugged; keeps the network up with the screen off
            .setAudioAttributes(AudioAttributes.Builder().setUsage(C.USAGE_MEDIA).setContentType(C.AUDIO_CONTENT_TYPE_MOVIE).build(), true)
            .setHandleAudioBecomingNoisy(true)
            .setWakeMode(C.WAKE_MODE_NETWORK)
            .build()
        view = PlayerView(this).apply {
            player = this@PlayerActivity.player
            keepScreenOn = true
            controllerShowTimeoutMs = 3500
            setShowSubtitleButton(true)
            setShowNextButton(false)
            setShowPreviousButton(false)
        }
        title = TextView(this).apply {
            setTextColor(Color.WHITE)
            textSize = 18f
            typeface = Typeface.DEFAULT_BOLD
            setPadding(56, 40, 56, 24)
            setShadowLayer(10f, 0f, 0f, Color.BLACK)
        }
        skipButton = Button(this).apply { visibility = View.GONE; isAllCaps = false }
        qualityButton = Button(this).apply {
            isAllCaps = false
            setTextColor(Color.WHITE)
            // the system's highlight, so a TV remote shows when it's focused
            setBackgroundResource(TypedValue().also { theme.resolveAttribute(android.R.attr.selectableItemBackground, it, true) }.resourceId)
            textSize = 15f
            visibility = View.GONE
            setOnClickListener { chooseQuality() }
        }
        view.setControllerVisibilityListener(PlayerView.ControllerVisibilityListener { title.visibility = it })
        // the quality button sits with the controller's own buttons (subtitles, settings), so it shows and hides with them
        val controls = view.findViewById<ViewGroup>(androidx.media3.ui.R.id.exo_basic_controls)
        setContentView(FrameLayout(this).apply {
            setBackgroundColor(Color.BLACK)
            addView(view, FrameLayout.LayoutParams(MATCH_PARENT, MATCH_PARENT))
            addView(title, FrameLayout.LayoutParams(MATCH_PARENT, WRAP_CONTENT, Gravity.TOP))
            if (controls == null) addView(qualityButton, FrameLayout.LayoutParams(WRAP_CONTENT, WRAP_CONTENT, Gravity.TOP or Gravity.END).apply { setMargins(0, 24, 40, 0) })
            addView(skipButton, FrameLayout.LayoutParams(WRAP_CONTENT, WRAP_CONTENT, Gravity.BOTTOM or Gravity.END).apply { setMargins(0, 0, 56, 180) })
        })
        controls?.addView(qualityButton, 0)
        inController = controls != null
        controls?.addView(textButton("Lyrics", "Lyrics") { toggleLyrics() }, 0)
        // the middle play button is hidden while lyrics are open (they'd sit under it): this one stands in for it
        playButton = textButton("Pause", "Play or pause") { if (player.isPlaying) player.pause() else player.play() }
            .apply { visibility = View.GONE }
        controls?.addView(playButton, 0)
        lyricsBox = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL; setPadding(72, 160, 72, 0) }
        lyricsView = ScrollView(this).apply {
            setBackgroundColor(0xE6000000.toInt())
            isVerticalScrollBarEnabled = false
            visibility = View.GONE
            addView(lyricsBox)
        }
        view.overlayFrameLayout?.addView(lyricsView, FrameLayout.LayoutParams(MATCH_PARENT, MATCH_PARENT))
        if (canPip()) controls?.addView(Button(this).apply {
            text = "PiP"
            isAllCaps = false
            setTextColor(Color.WHITE)
            setBackgroundResource(TypedValue().also { theme.resolveAttribute(android.R.attr.selectableItemBackground, it, true) }.resourceId)
            textSize = 15f
            contentDescription = "Picture in picture"
            setOnClickListener { enterPictureInPictureMode(pipParams()) }
        }, 0)
        ContextCompat.registerReceiver(this, pipReceiver, IntentFilter(PIP_TOGGLE), ContextCompat.RECEIVER_NOT_EXPORTED)
        if (controls == null) view.setControllerVisibilityListener(PlayerView.ControllerVisibilityListener {
            title.visibility = it
            qualityButton.visibility = if (heights.isEmpty()) View.GONE else it
        })
        immersive()
        parkedGuard = ParkedGuard(this) { parked ->
            if (!parked && player.isPlaying) { pausedForDriving = true; player.pause() }
            else if (parked && pausedForDriving) { pausedForDriving = false; player.play() }
        }
        player.addListener(object : Player.Listener {
            override fun onIsPlayingChanged(isPlaying: Boolean) {
                if (isPlaying && !parkedGuard.parked) return player.pause()
                playButton.text = if (isPlaying) "Pause" else "Play"
                updatePip()
                if (inBackground) showNotification()
            }
            override fun onVideoSizeChanged(videoSize: VideoSize) = updatePip()
            override fun onPlaybackStateChanged(state: Int) {
                if (state == Player.STATE_ENDED) next()
                stallSince = if (state == Player.STATE_BUFFERING && smooth > 0 && !switching) System.currentTimeMillis() else 0L
            }
            override fun onPositionDiscontinuity(old: Player.PositionInfo, new: Player.PositionInfo, reason: Int) {
                if (reason == Player.DISCONTINUITY_REASON_SEEK) smooth = 0  // buffering after a seek isn't a stall
            }
            override fun onPlayerError(error: PlaybackException) = toast("Couldn't play this video (${error.errorCodeName})")
        })
        load(req.optDouble("start", 0.0))
        active = this
        ui.post(remoteTick)
        ui.post(sponsorTick)
        ui.post(qualityTick)
        ui.post(lyricsTick)
        // Back closes the lyrics first
        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() = if (lyricsOpen()) toggleLyrics() else finish()
        })
        view.requestFocus()
    }

    private fun load(start: Double) {
        val v = list[index]
        val token = ++loadToken
        title.text = v.optString("title")
        played.put(v)
        segs = emptyList()
        skipped.clear()
        skipButton.visibility = View.GONE
        heights = emptyList()
        capQ = Int.MAX_VALUE
        smooth = 0
        showQuality()
        thread {
            try {
                val got = source(v)
                ui.post {
                    if (token != loadToken) return@post
                    heights = got.heights
                    curQ = got.height
                    player.setMediaSource(got.src, (start * 1000).toLong())
                    player.prepare()
                    player.play()
                    showQuality()
                }
                val sb = sponsorSegments(v)
                ui.post { if (token == loadToken) segs = sb }
                // look up the next video while this one plays, so autoplay starts quickly
                list.getOrNull(index + 1)?.takeIf { req.optBoolean("autoplay", true) && it.optString("local").isEmpty() }
                    ?.let { runCatching { get("formats?id=${it.getString("id")}&q=$maxQ") } }
            } catch (e: Exception) {
                ui.post { toast("Couldn't load this video: ${e.message}") }
            }
        }
    }

    /** Auto: one step below the best height (at most 720p), so the first frames arrive sooner. */
    private fun startHeight(heights: List<Int>): Int {
        val ok = heights.filter { it <= minOf(maxQ, capQ) }.ifEmpty { heights.takeLast(1) }
        val best = ok.firstOrNull() ?: return maxQ
        return ok.firstOrNull { it < best && it <= 720 } ?: ok.firstOrNull { it < best } ?: best
    }

    /** Auto's next step up, or 0 when it's already as high as it goes. */
    private fun upHeight(): Int = heights.filter { it > curQ && it <= minOf(maxQ, capQ) }.minOrNull() ?: 0

    // once a second: Auto steps up after 15s of smooth playback with plenty buffered, and down after a 2s stall
    private val qualityTick = object : Runnable {
        override fun run() {
            ui.postDelayed(this, 1000)
            if (!autoQ || switching || heights.isEmpty()) return
            if (stallSince > 0 && System.currentTimeMillis() - stallSince > 2000) {
                val down = heights.filter { it in 360 until curQ }.maxOrNull()
                stallSince = 0
                if (down != null) {
                    capQ = down  // don't climb back up to the height that stalled, for this video
                    switchTo(down)
                }
                return
            }
            if (player.isPlaying) smooth++
            val up = upHeight()
            if (up > 0 && smooth >= 15 && player.totalBufferedDuration >= 15_000) switchTo(up)
        }
    }

    /** Swaps in another height at the current position (a moment's buffering). */
    private fun switchTo(q: Int) {
        val v = list[index]
        val token = loadToken
        switching = true
        thread {
            try {
                val got = source(v, q)
                ui.post {
                    if (token == loadToken) {
                        curQ = got.height
                        player.setMediaSource(got.src, player.currentPosition)
                        player.prepare()
                    }
                    smooth = 0
                    switching = false
                    showQuality()
                }
            } catch (e: Exception) {
                ui.post { switching = false }
            }
        }
    }

    private fun showQuality() {
        qualityButton.text = if (curQ == 0) "" else if (autoQ) "Auto · ${curQ}p" else "${curQ}p"
        qualityButton.visibility = if (heights.isEmpty() || !inController && !view.isControllerFullyVisible) View.GONE else View.VISIBLE
    }

    private fun chooseQuality() {
        if (heights.isEmpty()) return
        val names = listOf("Auto" + if (autoQ) " (${curQ}p)" else "") + heights.map { "${it}p" }
        val checked = if (autoQ) 0 else heights.indexOf(curQ) + 1
        AlertDialog.Builder(this, android.R.style.Theme_DeviceDefault_Dialog_Alert)
            .setTitle("Quality")
            .setSingleChoiceItems(names.toTypedArray(), checked) { d, i ->
                d.dismiss()
                if (i == 0) {
                    autoQ = true
                    maxQ = prefQ
                    capQ = Int.MAX_VALUE
                    smooth = 0
                    picked = 0
                    showQuality()
                } else {
                    autoQ = false
                    picked = heights[i - 1]
                    maxQ = picked  // the next videos in the list play at this quality too
                    if (picked != curQ) switchTo(picked) else showQuality()
                }
            }
            .show()
    }

    private fun get(path: String) = URL(API + path).readText()  // background thread only

    /** A video's streams, its heights (best first; none for downloads and live) and the height chosen. */
    private class Loaded(val src: MediaSource, val heights: List<Int>, val height: Int)

    /** The video's streams at height q, or (q = 0) at the height Auto starts with. Background thread only. */
    private fun source(v: JSONObject, q: Int = 0): Loaded {
        val id = v.getString("id")
        val local = v.optString("local")
        if (local.isNotEmpty()) {  // a download, served by the in-app server
            return Loaded(ProgressiveMediaSource.Factory(DefaultHttpDataSource.Factory())
                .createMediaSource(MediaItem.fromUri("${API}file?id=$id&kind=$local")), emptyList(), 0)
        }
        // the server gives the best height <= q, and keeps the video's lookup, so asking again for another height is quick
        val limit = if (q > 0) q else maxQ
        val first = JSONObject(get("formats?id=$id&q=$limit"))
        val hs = if (first.optBoolean("live")) emptyList()
            else first.optJSONArray("heights")?.let { a -> (0 until a.length()).map { a.getInt(it) } } ?: emptyList()
        val top = hs.firstOrNull { it <= limit } ?: hs.lastOrNull() ?: 0
        val want = if (q == 0 && autoQ && hs.isNotEmpty()) startHeight(hs) else top
        val f = if (want != top) JSONObject(get("formats?id=$id&q=$want")) else first
        return Loaded(streams(id, f), hs, want)
    }

    private fun streams(id: String, f: JSONObject): MediaSource {
        val headers = f.optJSONObject("headers") ?: JSONObject()
        val http = DefaultHttpDataSource.Factory()
            .setAllowCrossProtocolRedirects(true)
            .setUserAgent(headers.optString("User-Agent").ifEmpty { null })
            .setDefaultRequestProperties(headers.keys().asSequence().filter { it != "User-Agent" }.associateWith { headers.getString(it) })
        if (f.optBoolean("live")) return HlsMediaSource.Factory(http).createMediaSource(MediaItem.fromUri(f.getString("hls")))
        // the video part carries the subtitle track (DefaultMediaSourceFactory parses WebVTT for us)
        val videoItem = MediaItem.Builder().setUri(f.getString("video")).setSubtitleConfigurations(captions(id, f.optJSONArray("captions"))).build()
        val parts = mutableListOf(DefaultMediaSourceFactory(http).createMediaSource(videoItem))
        if (!f.isNull("audio")) parts.add(ProgressiveMediaSource.Factory(http).createMediaSource(MediaItem.fromUri(f.getString("audio"))))
        return if (parts.size == 1) parts[0] else MergingMediaSource(*parts.toTypedArray())
    }

    /** All the video's subtitle tracks for the CC button; the remembered language (or "turn on automatically") is selected. */
    private fun captions(id: String, caps: JSONArray?): List<MediaItem.SubtitleConfiguration> {
        if (caps == null) return emptyList()
        val all = (0 until caps.length()).map { caps.getJSONObject(it) }
        val want = req.optString("cc")
        val lang = want.substringBefore(":")
        val pick = all.firstOrNull { want.isNotEmpty() && it.optString("lang") == lang && it.optBoolean("auto") == want.endsWith(":auto") }
            ?: all.firstOrNull { want.isNotEmpty() && it.optString("lang") == lang }
            ?: if (req.optBoolean("ccAuto")) all.firstOrNull { !it.optBoolean("auto") } ?: all.firstOrNull() else null
        return all.map { c ->
            val auto = if (c.optBoolean("auto")) 1 else 0
            MediaItem.SubtitleConfiguration.Builder(Uri.parse("${API}captions?id=$id&lang=${Uri.encode(c.optString("lang"))}&auto=$auto"))
                .setMimeType(MimeTypes.TEXT_VTT)
                .setLanguage(c.optString("lang"))
                .setLabel(c.optString("name"))
                .setSelectionFlags(if (c === pick) C.SELECTION_FLAG_DEFAULT else 0)
                .build()
        }
    }

    private fun strings(a: JSONArray?) = if (a == null) emptyList() else (0 until a.length()).map { a.getString(it) }

    private fun sponsorSegments(v: JSONObject): List<JSONObject> {
        val cats = (strings(req.optJSONArray("skip")) + strings(req.optJSONArray("ask"))).joinToString(",")
        if (cats.isEmpty() || v.optString("local").isNotEmpty()) return emptyList()
        return try {
            val a = JSONArray(get("sponsor?id=${v.getString("id")}&cats=$cats"))
            (0 until a.length()).map { a.getJSONObject(it) }
        } catch (e: Exception) {
            emptyList()  // SponsorBlock unreachable: just play
        }
    }

    // twice a second: skip segments set to "skip automatically", offer a button for "show a skip button"
    private val sponsorTick = object : Runnable {
        override fun run() {
            val pos = player.currentPosition / 1000.0
            val auto = strings(req.optJSONArray("skip"))
            var askAt = -1
            for ((i, s) in segs.withIndex()) {
                if (s.optString("type") != "skip" || i in skipped) continue
                if (pos < s.getDouble("s") || pos >= s.getDouble("e") - 0.3) continue
                if (s.optString("cat") in auto) {
                    skipped.add(i)
                    player.seekTo((s.getDouble("e") * 1000).toLong())
                    toast("Skipped ${s.optString("cat")}")
                } else askAt = i
            }
            skipButton.visibility = if (askAt >= 0) View.VISIBLE else View.GONE
            if (askAt >= 0) {
                val s = segs[askAt]
                skipButton.text = "Skip ${s.optString("cat")} ⏭"
                skipButton.setOnClickListener {
                    skipped.add(askAt)
                    player.seekTo((s.getDouble("e") * 1000).toLong())
                    skipButton.visibility = View.GONE
                }
            }
            ui.postDelayed(this, 500)
        }
    }

    // once a second: what's playing, for a phone using Remote control
    private val remoteTick = object : Runnable {
        override fun run() {
            val v = list[index]
            remoteState = JSONObject().put("id", v.optString("id")).put("title", v.optString("title"))
                .put("channel", v.optString("channel")).put("playing", player.isPlaying)
                .put("t", player.currentPosition / 1000.0).put("d", player.duration.coerceAtLeast(0) / 1000.0)
                .put("live", player.isCurrentMediaItemLive).toString()
            ui.postDelayed(this, 1000)
        }
    }

    /** A command from Remote control (any thread). */
    fun remote(action: String, t: Double) = ui.post {
        when (action) {
            "video_toggle" -> if (player.isPlaying) player.pause() else player.play()
            "video_seek" -> player.seekTo((t * 1000).toLong())
            "video_skip" -> player.seekTo((player.currentPosition + (t * 1000).toLong()).coerceAtLeast(0))
            "video_next" -> if (index + 1 < list.size) { index++; load(0.0) }
            "video_prev" -> notificationAction("prev")
            "video_close" -> notificationAction("stop")
            else -> {}
        }
    }

    private fun next() {
        if (req.optBoolean("autoplay", true) && index + 1 < list.size) {
            index++
            load(0.0)
        } else finish()
    }

    override fun finish() {
        if (!::player.isInitialized) return super.finish()
        // before the page hears back, so the music notification it puts back up isn't taken down after
        if (inBackground) hideNotification()
        val v = list[index]
        val last = JSONObject().put("id", v.optString("id"))
            .put("pos", player.currentPosition / 1000.0)
            .put("dur", player.duration.coerceAtLeast(0) / 1000.0)
        val result = JSONObject().put("played", played).put("last", last)
        if (picked > 0) result.put("quality", picked)
        setResult(RESULT_OK, Intent().putExtra(RESULT, result.toString()))
        super.finish()
    }

    // TV remotes: D-pad and media keys go to the player's controls
    override fun dispatchKeyEvent(event: KeyEvent) = view.dispatchKeyEvent(event) || super.dispatchKeyEvent(event)

    private fun isTv() = (getSystemService(UI_MODE_SERVICE) as UiModeManager).currentModeType == Configuration.UI_MODE_TYPE_TELEVISION

    private fun textButton(label: String, description: String, onClick: () -> Unit) = Button(this).apply {
        text = label
        isAllCaps = false
        setTextColor(Color.WHITE)
        setBackgroundResource(TypedValue().also { theme.resolveAttribute(android.R.attr.selectableItemBackground, it, true) }.resourceId)
        textSize = 15f
        contentDescription = description
        setOnClickListener { onClick() }
    }

    private fun lyricsOpen() = lyricsView.visibility == View.VISIBLE

    private fun toggleLyrics() {
        val open = !lyricsOpen()
        lyricsView.visibility = if (open) View.VISIBLE else View.GONE
        view.findViewById<View>(androidx.media3.ui.R.id.exo_center_controls)?.visibility = if (open) View.INVISIBLE else View.VISIBLE
        playButton.visibility = if (open) View.VISIBLE else View.GONE
        // the controls stay up while reading
        view.controllerShowTimeoutMs = if (open) 0 else 3500
        view.setControllerHideOnTouch(!open)
        title.visibility = View.VISIBLE
        view.showController()
        if (open && lyricFor != list[index].optString("id")) loadLyrics()
    }

    /** The song in a video's title ("Artist - Song (Official Video)"), else the title and the channel: (song, artist). */
    private fun songOf(v: JSONObject): Pair<String, String> {
        val t = v.optString("title")
        val channel = v.optString("channel").removeSuffix(" - Topic").removeSuffix("VEVO").trim()
        val m = Regex("^(.+?)\\s+[-–—|]\\s+(.+)$").find(t)
        return if (m != null) m.groupValues[2] to m.groupValues[1] else t to channel
    }

    private fun lyricsMessage(text: String) {
        lyricsBox.removeAllViews()
        lyricsBox.addView(TextView(this).apply { setTextColor(0xFF9A9AAB.toInt()); textSize = 18f; this.text = text })
    }

    private fun loadLyrics() {
        val v = list[index]
        lyricFor = v.optString("id")
        lyricLines = emptyList()
        lyricIdx = -1
        lyricsMessage("Looking for lyrics…")
        val (song, artist) = songOf(v)
        val dur = (v.optDouble("duration", 0.0).takeIf { it > 0 } ?: player.duration.coerceAtLeast(0) / 1000.0).toInt()
        thread {
            val r = runCatching {
                JSONObject(get("lyrics?track=${Uri.encode(song)}&artist=${Uri.encode(artist)}&album=&duration=$dur"))
            }.getOrNull()
            ui.post {
                if (lyricFor != v.optString("id")) return@post
                val lines = r?.optJSONArray("lines")?.let { a -> (0 until a.length()).map { a.getJSONObject(it) } } ?: emptyList()
                if (lines.isEmpty()) return@post lyricsMessage(if (r?.optBoolean("instrumental") == true) "Instrumental" else "No lyrics found for this song.")
                lyricLines = lines.filter { !r!!.optBoolean("synced") || it.optLong("t", -1) >= 0 }
                lyricsBox.removeAllViews()
                for ((i, l) in lyricLines.withIndex()) lyricsBox.addView(TextView(this).apply {
                    text = l.optString("text").ifEmpty { "♪" }
                    setTextColor(Color.WHITE)
                    alpha = if (r!!.optBoolean("synced")) 0.4f else 0.9f
                    textSize = if (isTv()) 30f else 24f
                    typeface = Typeface.DEFAULT_BOLD
                    setPadding(0, 0, 0, 28)
                    if (r.optBoolean("synced")) {
                        isFocusable = true
                        setOnClickListener { player.seekTo(lyricLines[i].optLong("t")) }
                    }
                })
                lyricsBox.addView(TextView(this).apply {
                    text = "Lyrics from ${r!!.optString("source")}"
                    setTextColor(0xFF9A9AAB.toInt())
                    textSize = 13f
                    setPadding(0, 24, 0, 420)  // room to scroll the last lines up past the controls
                })
            }
        }
    }

    // ten times a second while lyrics are open: the line being sung lit, its sung words filled in, kept in view
    private val lyricsTick = object : Runnable {
        override fun run() {
            ui.postDelayed(this, 100)
            if (!::lyricsView.isInitialized || !lyricsOpen() || lyricLines.isEmpty() || lyricLines[0].optLong("t", -1) < 0) return
            if (lyricFor != list[index].optString("id")) return loadLyrics()
            val t = player.currentPosition + 150
            val k = lyricLines.indexOfLast { it.optLong("t") <= t }
            val line = lyricsBox.getChildAt(k.coerceAtLeast(0)) as? TextView ?: return
            if (k != lyricIdx) {
                (lyricsBox.getChildAt(lyricIdx) as? TextView)?.let { it.alpha = 0.4f; it.text = lyricLines[lyricIdx].optString("text").ifEmpty { "♪" } }
                lyricIdx = k
                if (k < 0) return
                line.alpha = 1f
                lyricsView.smoothScrollTo(0, (line.top - lyricsView.height / 3).coerceAtLeast(0))
            }
            if (k < 0) return
            val words = lyricLines[k].optJSONArray("w") ?: return
            // the words sung so far white, the rest dimmed
            var sung = 0
            for (i in 0 until words.length()) {
                val w = words.getJSONArray(i)
                if (w.getLong(0) <= t) sung += w.getString(2).length
            }
            val text = (0 until words.length()).joinToString("") { words.getJSONArray(it).getString(2) }
            line.text = SpannableString(text).apply {
                setSpan(ForegroundColorSpan(0x66FFFFFF), sung.coerceAtMost(text.length), text.length, Spanned.SPAN_EXCLUSIVE_EXCLUSIVE)
            }
        }
    }

    private fun canPip() = pipOn && !isTv() && packageManager.hasSystemFeature(PackageManager.FEATURE_PICTURE_IN_PICTURE)

    /** The small window: the video's shape (within what Android allows), a play/pause button, automatic on Android 12+. */
    private fun pipParams(): PictureInPictureParams {
        val vs = player.videoSize
        val ratio = if (vs.width > 0 && vs.height > 0) (vs.width.toFloat() / vs.height).coerceIn(1 / 2.39f, 2.39f) else 16 / 9f
        val playing = player.isPlaying
        val toggle = RemoteAction(Icon.createWithResource(this, if (playing) android.R.drawable.ic_media_pause else android.R.drawable.ic_media_play),
            if (playing) "Pause" else "Play", if (playing) "Pause" else "Play",
            PendingIntent.getBroadcast(this, 0, Intent(PIP_TOGGLE).setPackage(packageName), PendingIntent.FLAG_IMMUTABLE))
        return PictureInPictureParams.Builder()
            .setAspectRatio(Rational((ratio * 1000).toInt(), 1000))
            .setActions(listOf(toggle))
            .apply { if (Build.VERSION.SDK_INT >= 31) setAutoEnterEnabled(playing).setSeamlessResizeEnabled(true) }
            .build()
    }

    private fun updatePip() {
        if (canPip()) setPictureInPictureParams(pipParams())
    }

    // phones before Android 12: going home keeps the video playing in a small window (12+ does it by itself)
    override fun onUserLeaveHint() {
        super.onUserLeaveHint()
        if (Build.VERSION.SDK_INT < 31 && player.isPlaying && canPip()) enterPictureInPictureMode(pipParams())
    }

    override fun onPictureInPictureModeChanged(isInPictureInPictureMode: Boolean, newConfig: Configuration) {
        super.onPictureInPictureModeChanged(isInPictureInPictureMode, newConfig)
        view.useController = !isInPictureInPictureMode
        title.visibility = if (isInPictureInPictureMode) View.GONE else View.VISIBLE
        // the small window was closed (not expanded): done watching
        if (!isInPictureInPictureMode && lifecycle.currentState == Lifecycle.State.CREATED) {
            player.pause()
            finish()
        }
    }

    // leaving while it plays (Home, the screen going off) keeps the sound going; the notification has the controls.
    // Started here, not in onStop: Android only lets a foreground service start while the app is still on screen.
    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<String>, grantResults: IntArray) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        parkedGuard.permissionResult(requestCode)
    }

    override fun onPause() {
        super.onPause()
        parkedGuard.stop()
        if (background && player.isPlaying && !isInPictureInPictureMode && !isFinishing) {
            inBackground = true
            showNotification()
        }
    }

    override fun onResume() {
        super.onResume()
        parkedGuard.start()
        if (inBackground) hideNotification()
    }

    override fun onStop() {
        super.onStop()
        if (!isInPictureInPictureMode && !inBackground) player.pause()
    }

    override fun onDestroy() {
        if (!::player.isInitialized) return super.onDestroy()  // closed straight away (see onCreate)
        ui.removeCallbacksAndMessages(null)
        if (active === this) { active = null; remoteState = "null" }
        if (inBackground) hideNotification()
        unregisterReceiver(pipReceiver)
        player.release()
        super.onDestroy()
    }

    /** The video in the media notification (and lock screen), its buttons working the player. */
    private fun showNotification() {
        val v = list[index]
        MusicService.videoListener = { action -> ui.post { notificationAction(action) } }
        MusicService.update(this, JSONObject().put("id", v.optString("id")).put("title", v.optString("title"))
            .put("artist", v.optString("channel")).put("album", "").put("video", true)
            .put("playing", player.isPlaying).put("pos", player.currentPosition / 1000.0)
            .put("dur", player.duration.coerceAtLeast(0) / 1000.0)
            .put("art", "https://i.ytimg.com/vi/${v.optString("id")}/hqdefault.jpg").toString())
    }

    private fun hideNotification() {
        inBackground = false
        MusicService.videoListener = null
        MusicService.update(this, "null")
    }

    private fun notificationAction(action: String) {
        when {
            action == "play" -> player.play()
            action == "pause" -> player.pause()
            action == "next" -> next()
            action == "prev" -> if (player.currentPosition > 5000 || index == 0) player.seekTo(0) else { index--; load(0.0) }
            action == "stop" -> { player.pause(); hideNotification(); finish() }
            action.startsWith("seek:") -> action.removePrefix("seek:").toDoubleOrNull()?.let { player.seekTo((it * 1000).toLong()) }
        }
        if (inBackground) showNotification()
    }

    private fun immersive() {
        WindowCompat.setDecorFitsSystemWindows(window, false)
        WindowInsetsControllerCompat(window, window.decorView).run {
            hide(WindowInsetsCompat.Type.systemBars())
            systemBarsBehavior = WindowInsetsControllerCompat.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE
        }
    }

    private fun toast(msg: String) = Toast.makeText(this, msg, Toast.LENGTH_SHORT).show()
}
