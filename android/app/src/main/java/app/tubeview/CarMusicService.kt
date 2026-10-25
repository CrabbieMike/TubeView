package app.tubeview

import android.app.PendingIntent
import android.content.Intent
import android.net.Uri
import androidx.annotation.OptIn
import androidx.media3.common.AudioAttributes
import androidx.media3.common.C
import androidx.media3.common.MediaItem
import androidx.media3.common.MediaMetadata
import androidx.media3.common.Player
import androidx.media3.common.util.UnstableApi
import androidx.media3.exoplayer.ExoPlayer
import androidx.media3.session.LibraryResult
import androidx.media3.session.MediaLibraryService
import androidx.media3.session.MediaLibraryService.LibraryParams
import androidx.media3.session.MediaLibraryService.MediaLibrarySession
import androidx.media3.session.MediaSession
import com.google.common.collect.ImmutableList
import com.google.common.util.concurrent.Futures
import com.google.common.util.concurrent.ListenableFuture
import org.json.JSONArray
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL
import java.util.concurrent.Callable
import java.util.concurrent.Executors

/**
 * Music for Android Auto, cars with Google built in, and voice ("Hey Google, play … on TubeView"): a media library
 * the car can browse and search (For You, Liked Songs, Recently Played, Explore), played by its own player so it
 * works with the phone locked and TubeView closed. Songs stream through the in-app server, as in the app.
 * Music playing in the app's own player pauses when this one starts, and the other way round.
 */
@OptIn(UnstableApi::class)
class CarMusicService : MediaLibraryService() {
    companion object {
        private const val API = "http://127.0.0.1:8765/api/"
        private const val ROOT = "root"
        /** The running service, so the app's own music player can pause it (and it the app's). */
        @Volatile var running: CarMusicService? = null
    }

    private lateinit var player: ExoPlayer
    private lateinit var session: MediaLibrarySession
    private val io = Executors.newCachedThreadPool()
    /** Song id -> the list it was shown in, so picking one song plays on through the rest of that list. */
    private val shownIn = HashMap<String, List<MediaItem>>()
    /** What the car's search found last, for its results page. */
    private var searchResults: List<MediaItem> = emptyList()

    override fun onCreate() {
        super.onCreate()
        running = this
        player = ExoPlayer.Builder(this)
            .setAudioAttributes(AudioAttributes.Builder().setUsage(C.USAGE_MEDIA).setContentType(C.AUDIO_CONTENT_TYPE_MUSIC).build(), true)
            .setHandleAudioBecomingNoisy(true)
            .setWakeMode(C.WAKE_MODE_NETWORK)
            .build()
        player.addListener(object : Player.Listener {
            override fun onIsPlayingChanged(isPlaying: Boolean) {
                if (isPlaying) MusicService.listener?.invoke("pause")  // one thing plays at a time
            }
            // when the queue is about to run out, carry on with YouTube Music's radio of the last song (as the app does)
            override fun onMediaItemTransition(item: MediaItem?, reason: Int) {
                if (item != null && player.currentMediaItemIndex == player.mediaItemCount - 1) extendWithRadio(item)
            }
        })
        val open = PendingIntent.getActivity(this, 0, Intent(this, MainActivity::class.java), PendingIntent.FLAG_IMMUTABLE)
        session = MediaLibrarySession.Builder(this, player, Library()).setSessionActivity(open).build()
    }

    override fun onGetSession(controllerInfo: MediaSession.ControllerInfo) = session

    override fun onDestroy() {
        running = null
        session.release()
        player.release()
        io.shutdown()
        super.onDestroy()
    }

    fun pause() = player.pause()

    // ---------------------------------------------------------------- the in-app server

    /** A GET to the in-app server, waiting for it to start (the car can wake TubeView before it's up). Background only. */
    private fun get(path: String): String {
        var last: Exception? = null
        repeat(60) {
            try {
                return (URL(API + path).openConnection() as HttpURLConnection).run {
                    connectTimeout = 1000; readTimeout = 30_000
                    if (responseCode != 200) throw IllegalStateException("TubeView answered $responseCode")
                    inputStream.bufferedReader().readText()
                }
            } catch (e: java.net.ConnectException) {
                last = e
                Thread.sleep(500)
            }
        }
        throw last ?: IllegalStateException("TubeView didn't start")
    }

    private fun songs(json: String?): List<JSONObject> =
        json?.let { JSONArray(it) }?.let { a -> (0 until a.length()).map { a.getJSONObject(it) } } ?: emptyList()

    private fun song(s: JSONObject): MediaItem {
        val id = s.getString("id")
        return MediaItem.Builder()
            .setMediaId("song:$id")
            .setUri("${API}audio?id=$id")
            .setMediaMetadata(MediaMetadata.Builder()
                .setTitle(s.optString("title"))
                .setArtist(s.optString("artist"))
                .setAlbumTitle(s.optString("album").ifEmpty { null })
                .setArtworkUri(Uri.parse("https://i.ytimg.com/vi/$id/hqdefault.jpg"))
                .setIsBrowsable(false).setIsPlayable(true)
                .setMediaType(MediaMetadata.MEDIA_TYPE_MUSIC)
                .build())
            .build()
    }

    private fun folder(id: String, title: String) = MediaItem.Builder()
        .setMediaId(id)
        .setMediaMetadata(MediaMetadata.Builder().setTitle(title).setIsBrowsable(true).setIsPlayable(false)
            .setMediaType(MediaMetadata.MEDIA_TYPE_FOLDER_MIXED).build())
        .build()

    /** A folder's contents. The first four folders are the car's tabs. */
    private fun children(parent: String): List<MediaItem> = when {
        parent == ROOT -> listOf(folder("foryou", "For You"), folder("liked", "Liked Songs"),
            folder("recent", "Recently Played"), folder("explore", "Explore"))
        parent == "foryou" -> {
            val r = JSONObject(get("music-foryou"))
            val mix = songs(r.optJSONArray("mix")?.toString())
            val rows = (r.optJSONArray("because")?.let { a -> (0 until a.length()).map { a.getJSONObject(it) } } ?: emptyList())
                .mapIndexed { i, b -> folder("because:$i", "Because you played ${b.getJSONObject("seed").optString("title")}") }
            if (mix.isEmpty()) children("explore") else listOf(folder("mix", "Mixed for You")) + rows
        }
        parent == "mix" -> songs(JSONObject(get("music-foryou")).optJSONArray("mix")?.toString()).map(::song)
        parent.startsWith("because:") -> JSONObject(get("music-foryou")).optJSONArray("because")
            ?.optJSONObject(parent.substringAfter(":").toInt())?.optJSONArray("songs")?.toString().let(::songs).map(::song)
        parent == "liked" -> songs(get("liked-music")).map(::song)
        parent == "recent" -> songs(JSONObject(get("data")).optJSONArray("musicHistory")?.toString()).map(::song)
        parent == "explore" -> JSONArray(get("music-rows")).let { a -> (0 until a.length()).map { a.getJSONArray(it) } }
            .map { folder("search:" + it.getString(1), it.getString(0)) }
        parent.startsWith("search:") -> search(parent.substringAfter(":"))
        else -> emptyList()
    }.also { items -> items.filter { it.mediaMetadata.isPlayable == true }.let { list -> list.forEach { shownIn[it.mediaId] = list } } }

    private fun search(q: String) = songs(get("msearch?q=" + Uri.encode(q))).map(::song)

    private fun extendWithRadio(last: MediaItem) {
        val id = last.mediaId.removePrefix("song:")
        io.execute {
            val more = runCatching { songs(get("radio?id=$id")).map(::song) }.getOrDefault(emptyList())
            val have = (0 until player.mediaItemCount).map { player.getMediaItemAt(it).mediaId }.toSet()
            android.os.Handler(mainLooper).post { player.addMediaItems(more.filter { it.mediaId !in have }.take(25)) }
        }
    }

    private fun <T> later(work: () -> T): ListenableFuture<T> =
        Futures.submit(Callable { work() }, io)

    /** What the car or the voice assistant asks for. */
    private inner class Library : MediaLibrarySession.Callback {
        override fun onGetLibraryRoot(session: MediaLibrarySession, browser: MediaSession.ControllerInfo,
                                      params: LibraryParams?): ListenableFuture<LibraryResult<MediaItem>> =
            Futures.immediateFuture(LibraryResult.ofItem(folder(ROOT, "TubeView"), params))

        override fun onGetItem(session: MediaLibrarySession, browser: MediaSession.ControllerInfo,
                               mediaId: String): ListenableFuture<LibraryResult<MediaItem>> = later {
            shownIn[mediaId]?.firstOrNull { it.mediaId == mediaId }?.let { LibraryResult.ofItem(it, null) }
                ?: LibraryResult.ofError(LibraryResult.RESULT_ERROR_BAD_VALUE)
        }

        override fun onGetChildren(session: MediaLibrarySession, browser: MediaSession.ControllerInfo, parentId: String,
                                   page: Int, pageSize: Int, params: LibraryParams?): ListenableFuture<LibraryResult<ImmutableList<MediaItem>>> = later {
            try {
                LibraryResult.ofItemList(ImmutableList.copyOf(children(parentId)), params)
            } catch (e: Exception) {
                LibraryResult.ofError(LibraryResult.RESULT_ERROR_IO)
            }
        }

        // the car's search box
        override fun onSearch(session: MediaLibrarySession, browser: MediaSession.ControllerInfo, query: String,
                              params: LibraryParams?): ListenableFuture<LibraryResult<Void>> = later {
            searchResults = runCatching { search(query) }.getOrDefault(emptyList())
            searchResults.forEach { shownIn[it.mediaId] = searchResults }
            session.notifySearchResultChanged(browser, query, searchResults.size, params)
            LibraryResult.ofVoid()
        }

        override fun onGetSearchResult(session: MediaLibrarySession, browser: MediaSession.ControllerInfo, query: String,
                                       page: Int, pageSize: Int, params: LibraryParams?): ListenableFuture<LibraryResult<ImmutableList<MediaItem>>> =
            Futures.immediateFuture(LibraryResult.ofItemList(ImmutableList.copyOf(searchResults), params))

        /**
         * Something to play: a song picked in the car (played on through the list it was in), or a voice request
         * ("play Bohemian Rhapsody on TubeView": a search; "play music on TubeView": For You).
         */
        override fun onSetMediaItems(mediaSession: MediaSession, controller: MediaSession.ControllerInfo, mediaItems: MutableList<MediaItem>,
                                     startIndex: Int, startPositionMs: Long): ListenableFuture<MediaSession.MediaItemsWithStartPosition> = later {
            val one = mediaItems.singleOrNull()
            val query = one?.requestMetadata?.searchQuery
            when {
                query != null -> {
                    val found = if (query.isBlank()) children("mix").ifEmpty { children("recent") } else search(query)
                    MediaSession.MediaItemsWithStartPosition(found, 0, C.TIME_UNSET)
                }
                one != null && shownIn[one.mediaId] != null -> {
                    val list = shownIn.getValue(one.mediaId)
                    MediaSession.MediaItemsWithStartPosition(list, list.indexOfFirst { it.mediaId == one.mediaId }.coerceAtLeast(0), startPositionMs)
                }
                else -> MediaSession.MediaItemsWithStartPosition(mediaItems.map(::resolve), startIndex, startPositionMs)
            }
        }

        override fun onAddMediaItems(mediaSession: MediaSession, controller: MediaSession.ControllerInfo,
                                     mediaItems: MutableList<MediaItem>): ListenableFuture<MutableList<MediaItem>> = later {
            mediaItems.flatMap { item ->
                item.requestMetadata.searchQuery?.let { q -> search(q).take(1) } ?: listOf(resolve(item))
            }.toMutableList()
        }
    }

    /** A song asked for by id only (the car remembers what it played) gets its stream again. */
    private fun resolve(item: MediaItem): MediaItem =
        if (item.localConfiguration != null || !item.mediaId.startsWith("song:")) item
        else item.buildUpon().setUri("${API}audio?id=${item.mediaId.removePrefix("song:")}").build()

    // keep playing with TubeView swiped away; stop when nothing's playing
    override fun onTaskRemoved(rootIntent: Intent?) {
        if (!player.playWhenReady || player.mediaItemCount == 0) stopSelf()
    }
}
