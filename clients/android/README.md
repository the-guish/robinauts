<!--
SPDX-License-Identifier: Apache-2.0
Copyright The Robinauts Authors
-->

# Robinauts Android client

Native Android client for Robinauts, built with Kotlin and Jetpack Compose.

This is a minimal scaffold: a single activity that shows `hello agent` and
nothing else. No navigation, no UI component library, no networking yet.

## Requirements

- Android Studio (Ladybug or newer), or the Android SDK command-line tools
- JDK 17
- Android SDK Platform 35

## Build and run

Point Gradle at your SDK with a `local.properties` file in this directory
(Android Studio writes this for you; it is deliberately git-ignored):

    sdk.dir=/path/to/Android/sdk

Then, from `clients/android`:

    ./gradlew :app:assembleDebug    # build the debug APK
    ./gradlew :app:installDebug     # install on a running device or emulator

Or open `clients/android` in Android Studio and run the `app` configuration.

## Layout

    app/
      src/main/
        java/com/robinauts/client/MainActivity.kt   the one screen
        res/values/strings.xml                       the app name
        AndroidManifest.xml
    gradle/libs.versions.toml                        version catalog (pins)

The application id and package are `com.robinauts.client`. To use a different
namespace, change `namespace`/`applicationId` in `app/build.gradle.kts` and
rename the package directory and declaration under `app/src/main/java`.
