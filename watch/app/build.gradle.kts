plugins {
    alias(libs.plugins.android.application)
    alias(libs.plugins.kotlin.compose)
}

android {
    namespace = "com.ssafy.simai.watch"
    // Wear Compose 1.7 이 37 이상을 요구한다. 실행 동작은 targetSdk 로 정한다
    compileSdk = 37

    defaultConfig {
        applicationId = "com.ssafy.simai.watch"
        // Wear OS 3(API 30) 이상. 보유 기기는 갤럭시워치5·6
        minSdk = 30
        targetSdk = 36
        versionCode = 1
        versionName = "0.1.0"
    }

    buildFeatures {
        compose = true
        buildConfig = true
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
}

dependencies {
    implementation(libs.androidx.activity.compose)
    implementation(libs.wear.compose.foundation)
    implementation(libs.wear.compose.material3)

    testImplementation(libs.junit)
}
