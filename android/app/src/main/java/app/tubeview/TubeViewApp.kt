package app.tubeview

import android.app.Application
import android.os.Build
import android.os.Environment
import android.provider.Settings
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform
import java.io.File
import kotlin.concurrent.thread

/** Starts TubeView's Python server (the same tubeview.py as on the desktop) inside the app. */
class TubeViewApp : Application() {
    companion object {
        @Volatile var serverReady = false
        const val URL = "http://127.0.0.1:8765/"
    }

    override fun onCreate() {
        super.onCreate()
        // the server reads the interface from files/web; refresh it from the APK on every start
        val web = File(filesDir, "web").apply { mkdirs() }
        for (name in listOf("index.html", "remote.html")) {
            assets.open("web/$name").use { input -> File(web, name).outputStream().use { input.copyTo(it) } }
        }
        val downloads = getExternalFilesDir(Environment.DIRECTORY_DOWNLOADS) ?: File(filesDir, "downloads")
        // the name other phones see for this device in Remote control ("Living Room TV", "Pixel 9")
        val name = Settings.Global.getString(contentResolver, Settings.Global.DEVICE_NAME)?.takeIf { it.isNotBlank() } ?: Build.MODEL
        thread(name = "tubeview-server") {
            if (!Python.isStarted()) Python.start(AndroidPlatform(this))
            Python.getInstance().getModule("android_main")
                .callAttr("start", File(filesDir, "config").absolutePath, web.absolutePath, downloads.absolutePath, name)
            serverReady = true
        }
    }
}
