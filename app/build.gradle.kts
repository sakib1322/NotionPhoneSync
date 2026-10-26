import org.jetbrains.kotlin.gradle.dsl.JvmTarget

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

val buildNumber = System.getenv("GITHUB_RUN_NUMBER")?.toIntOrNull() ?: 1

android {
    namespace = "com.sakib.notionsync"
    compileSdk = 35

    defaultConfig {
        applicationId = "com.sakib.notionsync"
        minSdk = 26
        targetSdk = 34
        versionCode = buildNumber
        versionName = "1.0.$buildNumber"
    }

    // The key file itself is never stored in this repository: the build
    // recreates it from the KEYSTORE_B64 GitHub secret. Its password is
    // useless without that file.
    val keystore = file("release.keystore")
    signingConfigs {
        create("release") {
            storeFile = keystore
            storePassword = "e11bd4b016ff00f53c9b0a20bde21606"
            keyAlias = "notionsync"
            keyPassword = "e11bd4b016ff00f53c9b0a20bde21606"
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            if (keystore.exists()) signingConfig = signingConfigs.getByName("release")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    lint {
        abortOnError = false
        checkReleaseBuilds = false
    }
}

kotlin {
    compilerOptions {
        jvmTarget.set(JvmTarget.JVM_17)
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.15.0")
    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("com.google.android.material:material:1.12.0")
    implementation("androidx.recyclerview:recyclerview:1.3.2")
    implementation("androidx.work:work-runtime-ktx:2.10.0")
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
}
