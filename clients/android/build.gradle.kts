// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors

// Plugins are declared here once and applied in the modules that need them.
plugins {
    alias(libs.plugins.android.application) apply false
    alias(libs.plugins.kotlin.android) apply false
    alias(libs.plugins.kotlin.compose) apply false
}
