package app.tubeview

import android.Manifest
import android.annotation.SuppressLint
import android.app.SearchManager
import android.app.UiModeManager
import android.content.ComponentName
import android.content.Intent
import android.content.pm.PackageManager
import android.content.res.Configuration
import android.os.Build
import android.os.Bundle
import android.provider.MediaStore
import android.webkit.JavascriptInterface
import android.webkit.WebChromeClient
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient
import androidx.activity.ComponentActivity
import android.widget.FrameLayout
import androidx.activity.addCallback
import androidx.core.view.ViewCompat
import androidx.core.view.WindowCompat
import androidx.core.view.WindowInsetsCompat
import androidx.activity.result.contract.ActivityResultContracts
import org.json.JSONArray
import org.json.JSONObject
import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.HttpURLConnection
import java.net.InetAddress
import java.net.NetworkInterface
import java.net.SocketTimeoutException
import java.net.URL
import kotlin.concurrent.thread

/** The whole interface is TubeView's web page; this activity hosts it and adds the native pieces. */
class MainActivity : ComponentActivity() {
    private lateinit var web: WebView
    /** A voice request ("play … on TubeView") the page hasn't picked up yet: "" means "play music", any song. */
    @Volatile private var voiceSearch: String? = null
    @Volatile private var pageReady = false
    // on the car's screen, TubeView's own screen is for when you're parked (music keeps playing)
    private val parkedGuard = ParkedGuard(this) {}

    // the native video player reports what was watched and where it stopped
    private val player = registerForActivityResult(ActivityResultContracts.StartActivityForResult()) { r ->
        r.data?.getStringExtra(PlayerActivity.RESULT)?.let { js("androidVideoDone(${JSONObject.quote(it)})") }
    }

    /** "Hey Google, play … on TubeView": the page searches the music and plays it (once it's loaded, if it isn't yet). */
    private fun takeVoice(intent: Intent?) {
        if (intent?.action != MediaStore.INTENT_ACTION_MEDIA_PLAY_FROM_SEARCH) return
        val q = intent.getStringExtra(SearchManager.QUERY).orEmpty()
        if (pageReady) js("androidPlaySearch(${JSONObject.quote(q)})") else voiceSearch = q
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        takeVoice(intent)
    }

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        takeVoice(intent)
        web = WebView(this).apply {
            setBackgroundColor(0xFF0B0B0F.toInt())
            settings.javaScriptEnabled = true
            settings.domStorageEnabled = true
            settings.mediaPlaybackRequiresUserGesture = false
            addJavascriptInterface(Bridge(), "TubeViewAndroid")
            webChromeClient = WebChromeClient()  // JS dialogs (confirm/prompt) for playlists and settings
            webViewClient = object : WebViewClient() {
                // TubeView itself and PCs on the local network stay in the app; everything else opens in the browser
                override fun shouldOverrideUrlLoading(view: WebView, req: WebResourceRequest): Boolean {
                    val host = req.url.host ?: return false
                    if (host == "127.0.0.1" || isLocalNetwork(host)) return false
                    startActivity(Intent(Intent.ACTION_VIEW, req.url))
                    return true
                }
            }
        }
        // Android 15+ draws apps under the status and navigation bars: keep the page inside them,
        // and above the keyboard (which also stops "adjustResize" from working on its own)
        val root = FrameLayout(this).apply { setBackgroundColor(0xFF0B0B0F.toInt()); addView(web) }
        ViewCompat.setOnApplyWindowInsetsListener(root) { v, insets ->
            val bars = insets.getInsets(WindowInsetsCompat.Type.systemBars() or WindowInsetsCompat.Type.displayCutout())
            val ime = insets.getInsets(WindowInsetsCompat.Type.ime())
            v.setPadding(bars.left, bars.top, bars.right, maxOf(bars.bottom, ime.bottom))
            WindowInsetsCompat.CONSUMED
        }
        setContentView(root)
        WindowCompat.getInsetsController(window, root).isAppearanceLightStatusBars = false  // light icons on the dark app
        web.loadData(SPLASH, "text/html", "utf-8")
        thread {
            while (!TubeViewApp.serverReady || !serverUp()) Thread.sleep(200)
            runOnUiThread { web.loadUrl(TubeViewApp.URL) }
        }

        onBackPressedDispatcher.addCallback(this) {
            val onRemotePage = web.url?.startsWith(TubeViewApp.URL) != true
            if (onRemotePage && web.canGoBack()) return@addCallback web.goBack()
            // let the page close menus/sheets first; at the top level, keep running in the background (music)
            web.evaluateJavascript("window.androidBack ? androidBack() : 0") { handled ->
                if (handled != "1") moveTaskToBack(true)
            }
        }
        MusicService.listener = { action ->
            runOnUiThread { js(if (action.startsWith("seek:")) "androidSeek(${action.substring(5)})" else "androidMedia('$action')") }
        }
        if (Build.VERSION.SDK_INT >= 33 && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), 1)  // for the music controls notification
        }
    }

    override fun onResume() {
        super.onResume()
        parkedGuard.start()
    }

    override fun onPause() {
        super.onPause()
        parkedGuard.stop()
    }

    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<String>, grantResults: IntArray) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        parkedGuard.permissionResult(requestCode)
    }

    override fun onDestroy() {
        MusicService.listener = null
        MusicService.update(this, "null")
        web.destroy()
        super.onDestroy()
    }

    private fun js(code: String) = web.evaluateJavascript(code, null)

    private fun serverUp() = try {
        (URL(TubeViewApp.URL + "api/me").openConnection() as HttpURLConnection).run {
            connectTimeout = 500; readTimeout = 2000
            responseCode == 200
        }
    } catch (e: Exception) {
        false
    }

    /** Called from the page as window.TubeViewAndroid. Methods run on a background thread. */
    inner class Bridge {
        /** The page asks once it's ready to play: a voice request that came before then, or null. */
        @JavascriptInterface
        fun takeVoiceSearch(): String? {
            pageReady = true
            return voiceSearch.also { voiceSearch = null }
        }

        /**
         * How Android Auto shows TubeView: "music" (its media screen, safe while driving) or "full" (TubeView's own
         * screen, videos when parked). Android Auto opens an app that has a music service as a music app, so for
         * "full" the service is switched off. Android Auto picks it up the next time the phone connects.
         */
        @JavascriptInterface
        fun setCarMode(mode: String) {
            packageManager.setComponentEnabledSetting(ComponentName(this@MainActivity, CarMusicService::class.java),
                if (mode == "full") PackageManager.COMPONENT_ENABLED_STATE_DISABLED else PackageManager.COMPONENT_ENABLED_STATE_DEFAULT,
                PackageManager.DONT_KILL_APP)
        }

        @JavascriptInterface
        fun isTv(): Boolean =
            (getSystemService(UI_MODE_SERVICE) as UiModeManager).currentModeType == Configuration.UI_MODE_TYPE_TELEVISION

        @JavascriptInterface
        fun playVideo(request: String) = runOnUiThread {
            PlayerActivity.active?.finish()  // a video started from Remote control replaces the one playing
            player.launch(Intent(this@MainActivity, PlayerActivity::class.java).putExtra(PlayerActivity.REQUEST, request))
        }

        /** The native player's video, for Remote control ("null" when none is open). */
        @JavascriptInterface
        fun videoState(): String = PlayerActivity.remoteState

        @JavascriptInterface
        fun videoCommand(action: String, t: Double) { PlayerActivity.active?.remote(action, t) }

        @JavascriptInterface
        fun musicState(state: String) = runOnUiThread { MusicService.update(this@MainActivity, state) }

        /** Finds TubeView on PCs on this network (they answer a UDP broadcast while Phone remote is on). */
        @JavascriptInterface
        fun discover(): String {
            val found = JSONArray()
            val seen = mutableSetOf<String>()
            try {
                DatagramSocket().use { s ->
                    s.broadcast = true
                    s.soTimeout = 400
                    val msg = "TUBEVIEW?".toByteArray()
                    for (addr in broadcastAddresses()) s.send(DatagramPacket(msg, msg.size, addr, 8766))
                    val buf = ByteArray(512)
                    val end = System.currentTimeMillis() + 1500
                    while (System.currentTimeMillis() < end) {
                        val p = DatagramPacket(buf, buf.size)
                        try { s.receive(p) } catch (e: SocketTimeoutException) { continue }
                        val text = String(p.data, 0, p.length)
                        val ip = p.address.hostAddress ?: continue
                        if (!text.startsWith("TUBEVIEW ") || !seen.add(ip)) continue
                        val info = JSONObject(text.removePrefix("TUBEVIEW "))
                        found.put(JSONObject().put("ip", ip).put("name", info.optString("name", ip)).put("port", info.optInt("port", 8766)))
                    }
                }
            } catch (e: Exception) {
                // no network: report nothing found
            }
            return found.toString()
        }
    }

    private fun broadcastAddresses(): List<InetAddress> {
        val out = mutableListOf(InetAddress.getByName("255.255.255.255"))
        for (nif in NetworkInterface.getNetworkInterfaces()) {
            if (!nif.isUp || nif.isLoopback) continue
            nif.interfaceAddresses.mapNotNullTo(out) { it.broadcast }
        }
        return out
    }

    companion object {
        private const val SPLASH = "<body style='margin:0;background:#0b0b0f;color:#9a9aab;font:16px system-ui;" +
            "display:grid;place-items:center;height:100vh'><div><b style='color:#f4f4f7;font-size:28px'>Tube</b>" +
            "<b style='font-size:28px'>View</b><p>Starting…</p></div></body>"

        /** 10.x, 172.16-31.x, 192.168.x and .local names: a PC on the same network. */
        fun isLocalNetwork(host: String): Boolean {
            if (host.endsWith(".local")) return true
            val p = host.split(".").mapNotNull { it.toIntOrNull() }
            if (p.size != 4) return false
            return p[0] == 10 || (p[0] == 172 && p[1] in 16..31) || (p[0] == 192 && p[1] == 168)
        }
    }
}
