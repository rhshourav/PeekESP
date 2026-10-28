package com.rhshourav.peekesp.data

import android.content.Context
import android.content.SharedPreferences
import java.net.URI
import java.net.URISyntaxException

/**
 * Where the app talks to. Defaults to the public PeekESP relay; anyone running
 * their own Worker can point the app at it from the "Relay address" dialog.
 * Agents and devices must push to the same relay the app reads from.
 */
object RelayConfig {
    const val DEFAULT_BASE = "https://peek-relay-b9a8.onrender.com"

    // Spelling is historical. Kept so an address saved by an earlier build survives an update.
    private const val KEY = "realy_url"

    private fun prefs(context: Context): SharedPreferences =
        context.getSharedPreferences("peekesp", Context.MODE_PRIVATE)

    fun get(context: Context): String =
        prefs(context).getString(KEY, null)?.takeIf { it.isNotBlank() } ?: DEFAULT_BASE

    fun isCustom(context: Context): Boolean = get(context) != DEFAULT_BASE

    /**
     * Trims whitespace and trailing slashes. Returns null unless the result is an
     * https address with a host: the read token travels in a header, so plain
     * http is never accepted.
     */
    fun clean(url: String): String? {
        val c = url.trim().trimEnd('/')
        if (!c.startsWith("https://")) return null
        val host = try {
            URI(c).host
        } catch (e: URISyntaxException) {
            null
        }
        return if (host.isNullOrBlank()) null else c
    }

    /** True if the address was valid and saved. */
    fun save(context: Context, url: String): Boolean {
        val c = clean(url) ?: return false
        prefs(context).edit().putString(KEY, c).apply()
        return true
    }

    fun reset(context: Context) {
        prefs(context).edit().remove(KEY).apply()
    }
}