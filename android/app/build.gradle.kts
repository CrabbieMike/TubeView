plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("com.chaquo.python")
}

// release signing comes from the build workflow (a keystore kept in GitHub secrets); local builds use the debug key
val keystore: String? = System.getenv("ANDROID_KEYSTORE_FILE")

android {
    namespace = "app.tubeview"
    compileSdk = 36

    defaultConfig {
        applicationId = "app.tubeview"
        minSdk = 26  // Android 8.0: notification channels and foreground media services
        targetSdk = 36  // Android 16, as AA Browser targets (Android Auto may only show newer apps on the car's screen)
        versionCode = (System.getenv("VERSION_CODE") ?: "1").toInt()
        versionName = System.getenv("VERSION_NAME") ?: "dev"
        // phones and most TVs are ARM (many TVs run 32-bit); x86_64 is for the emulator
        ndk { abiFilters += listOf("arm64-v8a", "armeabi-v7a", "x86_64") }
    }

    signingConfigs {
        create("release") {
            if (keystore != null) {
                storeFile = file(keystore)
                storeType = "pkcs12"
                storePassword = System.getenv("ANDROID_KEYSTORE_PASSWORD")
                keyAlias = "tubeview"
                keyPassword = System.getenv("ANDROID_KEYSTORE_PASSWORD")
            }
        }
    }
    buildTypes {
        release {
            isMinifyEnabled = false
            signingConfig = signingConfigs.getByName(if (keystore != null) "release" else "debug")
        }
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions { jvmTarget = "17" }
    buildFeatures { buildConfig = true }
    // the web interface is shared with the desktop app (copied in by copyWeb below)
    sourceSets["main"].assets.srcDir("build/shared-assets")
}

chaquopy {
    defaultConfig {
        version = "3.11"  // the newest Python Chaquopy builds for 32-bit ARM, which many TVs run
        pip {
            install("yt-dlp")
            install("certifi")
        }
    }
    sourceSets {
        getByName("main") {
            srcDir("src/main/python")
            srcDir("build/shared-python")  // tubeview.py, shared with the desktop app
        }
    }
}

val copyShared by tasks.registering(Copy::class) {
    from(rootProject.file("../tubeview.py"))
    into("build/shared-python")
}
val copyWeb by tasks.registering(Copy::class) {
    from(rootProject.file("../index.html"), rootProject.file("../remote.html"))
    into("build/shared-assets/web")
}
tasks.named("preBuild") { dependsOn(copyShared, copyWeb) }
// the tasks that read those folders must also say so, or Gradle refuses to run them
tasks.matching { it.name.startsWith("merge") && it.name.endsWith("PythonSources") }.configureEach { dependsOn(copyShared) }
tasks.matching { it.name.startsWith("merge") && it.name.endsWith("Assets") }.configureEach { dependsOn(copyWeb) }
tasks.matching { it.name.startsWith("lintVital") || it.name.startsWith("generate") && it.name.endsWith("LintModel") }
    .configureEach { dependsOn(copyShared, copyWeb) }

dependencies {
    implementation("androidx.core:core-ktx:1.15.0")
    implementation("androidx.activity:activity-ktx:1.9.3")
    implementation("androidx.media3:media3-exoplayer:1.5.1")
    implementation("androidx.media3:media3-exoplayer-hls:1.5.1")
    implementation("androidx.media3:media3-ui:1.5.1")
    // the music library Android Auto, cars with Google built in and voice requests play from
    implementation("androidx.media3:media3-session:1.5.1")
    implementation("com.google.guava:guava:33.3.1-android")
}
