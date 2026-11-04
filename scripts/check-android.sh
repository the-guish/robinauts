#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors
#
# Build the Android client (clients/android). Needs a JDK and the Android SDK;
# the wrapper pins Gradle. CI installs the SDK and sets ANDROID_HOME; locally,
# Android Studio writes clients/android/local.properties. Further arguments are
# passed on to Gradle.
set -eu

root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$root/clients/android"

# A clear message instead of Gradle's "SDK location not found" when neither the
# environment nor local.properties points at an SDK.
if [ -z "${ANDROID_HOME:-}" ] && [ -z "${ANDROID_SDK_ROOT:-}" ] && [ ! -f local.properties ]; then
    printf 'No Android SDK found. Set ANDROID_HOME, or write\n' >&2
    printf 'clients/android/local.properties with sdk.dir=/path/to/Android/sdk\n' >&2
    printf '(Android Studio writes that file for you).\n' >&2
    exit 1
fi

./gradlew --no-daemon :app:assembleDebug "$@"
