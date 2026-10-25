package app.tubeview

import android.Manifest
import android.app.Activity
import android.content.pm.PackageManager
import android.graphics.Color
import android.location.Location
import android.location.LocationListener
import android.location.LocationManager
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.view.Display
import android.view.Gravity
import android.view.ViewGroup
import android.widget.TextView

/**
 * On a car's screen (Android Auto shows TubeView there on Android 15 and later; or a car with Google built in), TubeView's screens are for when
 * you're parked: while the car moves (the phone's GPS speed), [onChange] is told to stop, and a notice covers the
 * screen. Without location permission it can't tell, so it treats the car as moving. Music keeps playing either way,
 * and the car's own media screen (CarMusicService) is there for driving.
 */
class ParkedGuard(private val activity: Activity, private val onChange: (parked: Boolean) -> Unit) {
    companion object {
        private const val MOVING = 2.5f   // m/s (9 km/h): moving
        private const val STOPPED = 0.8f  // m/s: stopped, once it's stayed this slow for STILL_MS
        private const val STILL_MS = 3000L
        private const val ASK = 41
    }

    var parked = true
        private set
    private var slowSince = 0L
    private var asked = false  // the location question is asked once (asking again after a "no" would loop)
    private var notice: TextView? = null
    private val ui = Handler(Looper.getMainLooper())
    private val locations by lazy { activity.getSystemService(LocationManager::class.java) }  // not before the activity's onCreate
    private val listener = LocationListener { update(it) }

    /** Whether this activity is on a car's screen: Android Auto's (a second display) or a car with Google built in. */
    fun onCarScreen(): Boolean = activity.packageManager.hasSystemFeature(PackageManager.FEATURE_AUTOMOTIVE) ||
        Build.VERSION.SDK_INT >= 30 && (activity.display?.displayId ?: Display.DEFAULT_DISPLAY) != Display.DEFAULT_DISPLAY

    fun start() {
        if (!onCarScreen()) return
        if (activity.checkSelfPermission(Manifest.permission.ACCESS_FINE_LOCATION) != PackageManager.PERMISSION_GRANTED) {
            set(false, "Videos play only when you're parked.\nAllow location so TubeView can tell when you are.")
            if (!asked) {
                asked = true
                activity.requestPermissions(arrayOf(Manifest.permission.ACCESS_FINE_LOCATION), ASK)
            }
            return
        }
        try {
            locations.requestLocationUpdates(LocationManager.GPS_PROVIDER, 1000L, 0f, listener, Looper.getMainLooper())
            locations.getLastKnownLocation(LocationManager.GPS_PROVIDER)?.let { update(it) }
        } catch (e: SecurityException) {
            set(false, "Videos play only when you're parked.")
        }
    }

    fun stop() {
        locations.removeUpdates(listener)
        ui.removeCallbacksAndMessages(null)
    }

    /** From the activity's onRequestPermissionsResult. */
    fun permissionResult(requestCode: Int) {
        if (requestCode == ASK) start()
    }

    private fun update(l: Location) {
        val speed = if (l.hasSpeed()) l.speed else 0f
        val now = System.currentTimeMillis()
        when {
            speed >= MOVING -> { slowSince = 0; set(false, "Videos play only when you're parked.\nMusic keeps playing.") }
            speed <= STOPPED -> {
                if (slowSince == 0L) slowSince = now
                if (now - slowSince >= STILL_MS) set(true, null)
            }
            else -> slowSince = 0  // between the two: no change
        }
    }

    private fun set(nowParked: Boolean, message: String?) {
        val changed = nowParked != parked
        parked = nowParked
        showNotice(message.takeIf { !nowParked })
        if (changed) onChange(nowParked)
    }

    private fun showNotice(message: String?) {
        val root = activity.window.decorView as ViewGroup
        if (message == null) {
            notice?.let { root.removeView(it) }
            notice = null
            return
        }
        val v = notice ?: TextView(activity).apply {
            setBackgroundColor(0xF20B0B0F.toInt())
            setTextColor(Color.WHITE)
            textSize = 26f
            gravity = Gravity.CENTER
            setPadding(64, 64, 64, 64)
            isClickable = true  // the screen under it can't be used
            isFocusable = true
            root.addView(this, ViewGroup.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT))
            notice = this
        }
        v.text = message
    }
}
