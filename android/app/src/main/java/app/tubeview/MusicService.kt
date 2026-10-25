package app.tubeview

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.media.MediaMetadata
import android.media.session.MediaSession
import android.media.session.PlaybackState
import android.os.Build
import android.os.IBinder
import org.json.JSONObject
import java.net.URL
import kotlin.concurrent.thread

/**
 * Music plays in the page's <audio> element; this foreground service keeps the app alive while it does
 * (phone home screen, lock screen, TV home screen) and exposes it to the system as a media session:
 * notification controls, lock screen, Bluetooth buttons and the Android TV "Now playing" card.
 */
class MusicService : Service() {
    companion object {
        /** Receives "play", "pause", "next", "prev", "stop" or "seek:<seconds>" for the page. */
        var listener: ((String) -> Unit)? = null
        /** Set while a video plays in the background: the buttons go to the video player instead, and tapping opens it. */
        var videoListener: ((String) -> Unit)? = null
        private var state: JSONObject? = null
        private var running: MusicService? = null
        private const val CHANNEL = "music"

        /** The page's music state as JSON, or "null" when music stopped. */
        fun update(ctx: Context, json: String) {
            state = if (json == "null") null else JSONObject(json)
            if (state?.optBoolean("playing") == true) CarMusicService.running?.pause()  // one thing plays at a time
            val svc = running
            when {
                state == null -> svc?.stopSelf()
                svc != null -> svc.refresh()
                else -> ctx.startForegroundService(Intent(ctx, MusicService::class.java))
            }
        }
    }

    private lateinit var session: MediaSession
    private var art: Bitmap? = null
    private var artId = ""

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        running = this
        getSystemService(NotificationManager::class.java).createNotificationChannel(
            NotificationChannel(CHANNEL, "Music", NotificationManager.IMPORTANCE_LOW).apply { setShowBadge(false) })
        session = MediaSession(this, "TubeView").apply {
            setCallback(object : MediaSession.Callback() {
                override fun onPlay() = send("play")
                override fun onPause() = send("pause")
                override fun onSkipToNext() = send("next")
                override fun onSkipToPrevious() = send("prev")
                override fun onStop() = send("stop")
                override fun onSeekTo(pos: Long) = send("seek:${pos / 1000.0}")
            })
            isActive = true
        }
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        intent?.action?.let { send(it) }  // a notification button
        refresh()
        return START_NOT_STICKY
    }

    override fun onDestroy() {
        running = null
        session.release()
        super.onDestroy()
    }

    private fun send(action: String) {
        (videoListener ?: listener)?.invoke(action)
    }

    fun refresh() {
        val st = state ?: return stopSelf()
        val id = st.optString("id")
        val playing = st.optBoolean("playing")
        if (id != artId) {
            artId = id
            art = null
            thread {
                val bmp = try {
                    URL(st.optString("art")).openStream().use { BitmapFactory.decodeStream(it) }
                } catch (e: Exception) {
                    null
                }
                // a song's cover is a 16:9 thumbnail with the album art in the middle: crop it square (a video's stays 16:9)
                art = if (st.optBoolean("video")) bmp
                    else bmp?.let { val side = minOf(it.width, it.height); Bitmap.createBitmap(it, (it.width - side) / 2, (it.height - side) / 2, side, side) }
                android.os.Handler(mainLooper).post { if (artId == id) refresh() }
            }
        }
        session.setMetadata(MediaMetadata.Builder()
            .putString(MediaMetadata.METADATA_KEY_TITLE, st.optString("title"))
            .putString(MediaMetadata.METADATA_KEY_ARTIST, st.optString("artist"))
            .putString(MediaMetadata.METADATA_KEY_ALBUM, st.optString("album"))
            .putLong(MediaMetadata.METADATA_KEY_DURATION, (st.optDouble("dur", 0.0) * 1000).toLong())
            .apply { art?.let { putBitmap(MediaMetadata.METADATA_KEY_ALBUM_ART, it) } }
            .build())
        session.setPlaybackState(PlaybackState.Builder()
            .setActions(PlaybackState.ACTION_PLAY or PlaybackState.ACTION_PAUSE or PlaybackState.ACTION_PLAY_PAUSE or
                PlaybackState.ACTION_SKIP_TO_NEXT or PlaybackState.ACTION_SKIP_TO_PREVIOUS or PlaybackState.ACTION_SEEK_TO or
                PlaybackState.ACTION_STOP)
            .setState(if (playing) PlaybackState.STATE_PLAYING else PlaybackState.STATE_PAUSED,
                (st.optDouble("pos", 0.0) * 1000).toLong(), if (playing) 1f else 0f)
            .build())

        fun action(icon: Int, title: String, act: String) = Notification.Action.Builder(
            android.graphics.drawable.Icon.createWithResource(this, icon), title,
            PendingIntent.getService(this, act.hashCode(), Intent(this, MusicService::class.java).setAction(act), PendingIntent.FLAG_IMMUTABLE)
        ).build()
        val open = if (videoListener != null)
            PendingIntent.getActivity(this, 1, Intent(this, PlayerActivity::class.java)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_REORDER_TO_FRONT), PendingIntent.FLAG_IMMUTABLE)
            else PendingIntent.getActivity(this, 0, Intent(this, MainActivity::class.java)
                .addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP), PendingIntent.FLAG_IMMUTABLE)
        val notification = Notification.Builder(this, CHANNEL)
            .setSmallIcon(R.drawable.ic_stat_tubeview)
            .setContentTitle(st.optString("title"))
            .setContentText(st.optString("artist"))
            .setLargeIcon(art)
            .setContentIntent(open)
            .setVisibility(Notification.VISIBILITY_PUBLIC)
            .addAction(action(android.R.drawable.ic_media_previous, "Previous", "prev"))
            .addAction(if (playing) action(android.R.drawable.ic_media_pause, "Pause", "pause")
                else action(android.R.drawable.ic_media_play, "Play", "play"))
            .addAction(action(android.R.drawable.ic_media_next, "Next", "next"))
            .setStyle(Notification.MediaStyle().setMediaSession(session.sessionToken).setShowActionsInCompactView(0, 1, 2))
            .build()
        if (Build.VERSION.SDK_INT >= 29) startForeground(1, notification, ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PLAYBACK)
        else startForeground(1, notification)
    }
}
