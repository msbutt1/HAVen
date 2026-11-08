plugins {
    id("com.android.application")
}

android {
    namespace = "com.vibeguard.app"
    compileSdk = 36

    defaultConfig {
        applicationId = "com.vibeguard.app"
        minSdk = 26          // Android 8.0
        targetSdk = 36
        versionCode = 1
        versionName = "0.1.0"
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    buildTypes {
        release {
            isMinifyEnabled = false
        }
    }

    // The offline demo is the repo's own dashboard, bundled as it is at build time (it runs on the
    // simulated feed when opened from the APK). The connected screens load the backend's copy instead.
    sourceSets {
        getByName("main") {
            assets.directories.add("../../dashboard")
        }
    }
    androidResources {
        // the default pattern, plus the dashboard's own notes; licence files (Chart.js, the fonts) stay in
        ignoreAssetsPattern = "!.svn:!.git:!.ds_store:!*.scc:.*:<dir>_*:!CVS:!thumbs.db:!picasa.ini:!*~:!README.md:!TESTLOG.md"
    }
}

dependencies {
    testImplementation("junit:junit:4.13.2")
}
