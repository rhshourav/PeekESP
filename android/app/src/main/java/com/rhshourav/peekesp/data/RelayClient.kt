package com.rhshourav.peekesp.data

import com.rhshourav.peekesp.BuildConfig
import org.json.JSONException
import java.io.IOException
import java.net.HttpURLConnection
import java.net.URL

sealed interface FetchResult {
    data class Ok(val raw: String, val machines: List<Machine>, val latencyMs: Long) : FetchResult
    /** A valid code with nothing pushed to it yet (503/404). Correct, not a failure. */
    data object Empty : FetchResult
    /** 401: the stream's read token was claimed by something else. */
    data object AuthRejected : FetchResult
    data class Failed(val reason: String) : FetchResult
}

/** Plain HttpURLConnection: no dependency, default TLS verification, no redirects. */
object RelayClient {
    const val DEFAULT_BASE = "https://peek-relay.peekesp.workers.dev"

    // Cloudflare's edge rejects some generic client agents with error 1010 before
    // the Worker runs, so say who we are.
    fun userAgent() = "PeekESP-android/${BuildConfig.VERSION_NAME}"

    /** Blocking. Call from a background dispatcher. */
    fun fetch(base: String, keys: Pairing.Keys, timeoutMs: Int = 10_000): FetchResult {
        if (!base.startsWith("https://")) return FetchResult.Failed("the relay must be an https address")
        val url = "${base.trimEnd('/')}/telemetry/${keys.stream}"
        val conn = try {
            URL(url).openConnection() as HttpURLConnection
        } catch (e: IOException) {
            return FetchResult.Failed("bad relay address")
        }
        return try {
            conn.requestMethod = "GET"
            conn.connectTimeout = timeoutMs
            conn.readTimeout = timeoutMs
            conn.useCaches = false
            conn.instanceFollowRedirects = false      // never replay the token somewhere else
            conn.setRequestProperty("Authorization", "Bearer ${keys.read}")
            conn.setRequestProperty("User-Agent", userAgent())
            conn.setRequestProperty("Accept", "application/json")

            val t0 = System.nanoTime()
            when (val code = conn.responseCode) {
                200 -> {
                    val body = conn.inputStream.bufferedReader().use { it.readText() }
                    val ms = (System.nanoTime() - t0) / 1_000_000
                    FetchResult.Ok(body, Parser.parse(body), ms)
                }
                401 -> FetchResult.AuthRejected
                404, 503 -> FetchResult.Empty
                else -> FetchResult.Failed("the relay answered HTTP $code")
            }
        } catch (e: JSONException) {
            FetchResult.Failed("unreadable reply from the relay")
        } catch (e: IOException) {
            FetchResult.Failed(e.message ?: "network error")
        } finally {
            conn.disconnect()
        }
    }
}
