package com.rhshourav.peekesp.data

import android.content.Context

/** How the app picks light or dark. "System" is the default: it follows the phone. */
enum class AppTheme(val label: String) {
    SYSTEM("System"),
    LIGHT("Light"),
    DARK("Dark");
}

object ThemeConfig {
    private fun p(ctx: Context) = ctx.getSharedPreferences("peek_theme", Context.MODE_PRIVATE)

    fun get(ctx: Context): AppTheme =
        AppTheme.values().firstOrNull { it.name == p(ctx).getString("app", null) } ?: AppTheme.SYSTEM

    fun set(ctx: Context, theme: AppTheme) = p(ctx).edit().putString("app", theme.name).apply()
}