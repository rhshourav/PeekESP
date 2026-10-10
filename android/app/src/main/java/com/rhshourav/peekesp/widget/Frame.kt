package com.rhshourav.peekesp.widget

import android.content.Context
import com.rhshourav.peekesp.data.Fmt
import com.rhshourav.peekesp.data.Freshness
import com.rhshourav.peekesp.data.Machine
import com.rhshourav.peekesp.data.Parser
import com.rhshourav.peekesp.data.SetupStore
import com.rhshourav.peekesp.data.WidgetCache
import kotlin.math.roundToInt

/** The device's own palette (PeekESP.ino). */
object Palette {
    val BG = 0xFF05070E.toInt()
    val PANEL = 0xFF0B1220.toInt()
    val TRACK = 0xFF1A2334.toInt()
    val CYAN = 0xFF00E5FF.toInt()
    val MAGENTA = 0xFFFF2E7E.toInt()
    val AMBER = 0xFFFFC145.toInt()
    val GREEN = 0xFF35F2A0.toInt()
    val RED = 0xFFFF4D6D.toInt()
    val TEXT = 0xFFE6EDF7.toInt()

    /** Lighter than the device's 5C6B82, which is only 3.7:1 on its own background. Glass needs more. */
    val DIM = 0xFF93A3BA.toInt()

    // The device's load_color(): the accent stays on-brand until a reading gets uncomfortable.
    const val WARN_FROM = 75.0
    const val CRIT_FROM = 90.0

    fun load(base: Int, pct: Double?): Int = when {
        pct == null -> base
        pct >= CRIT_FROM -> RED
        pct >= WARN_FROM -> AMBER
        else -> base
    }
}

enum class Link(val label: String, val color: Int) {
    OK("LINK OK", Palette.GREEN),
    STALE("STALE", Palette.AMBER),
    NONE("NO LINK", Palette.RED),
}

/** Everything the renderer needs, already decided. */
data class Frame(
    val machine: Machine?,
    val index: Int = 0,
    val count: Int = 0,
    /** Seconds since this widget last got an answer from the relay. */
    val sinceFetchS: Long = 0,
    val offline: Boolean = false,
    val link: Link = Link.STALE,
    val latencyMs: Long = 0,
    val message: String? = null,
    val messageColor: Int = Palette.DIM,
) {
    /** The widget is a bitmap, so everything on it has to be said here for TalkBack. */
    fun describe(): String {
        val m = machine ?: return "PeekESP. ${message ?: "No data."}"
        val parts = mutableListOf<String>()
        parts += if (count > 1) "${m.host}, machine ${index + 1} of $count" else m.host
        if (offline) parts += "offline, last heard ${Fmt.ago(m.ageS + sinceFetchS)} ago"
        m.cpu?.let { parts += "CPU ${it.roundToInt()} percent" }
        m.ram?.let { parts += "RAM ${it.roundToInt()} percent" }
        m.tempC?.let { parts += "temperature ${it.roundToInt()} degrees" }
        m.storagePct?.let {
            val free = if (m.storageFreeGb != null) ", ${Fmt.capacity(m.storageFreeGb)} free" else ""
            parts += "storage ${it.roundToInt()} percent used$free"
        }
        m.rxKbps?.let { parts += "download ${Fmt.rate(it)}" }
        m.txKbps?.let { parts += "upload ${Fmt.rate(it)}" }
        if (!offline) Fmt.uptime(m.uptimeS).takeIf { it.isNotEmpty() }?.let { parts += it }
        parts += "link ${link.label.lowercase()}"
        parts += Fmt.asOf(sinceFetchS + m.ageS)
        if (count > 1) parts += "tap for the next machine"
        return parts.joinToString(". ")
    }
}

object FrameBuilder {
    fun build(ctx: Context, nowMs: Long): Frame {
        if (!SetupStore(ctx).has()) {
            return Frame(null, message = "Not paired. Open PeekESP and enter your code.", messageColor = Palette.AMBER)
        }
        val cached = WidgetCache(ctx).read()
            ?: return Frame(null, message = "Waiting for the first reading...")
        if (cached.status == WidgetCache.Status.AUTH) {
            return Frame(null, message = "Code rejected. Pair again in the app.", messageColor = Palette.RED)
        }
        val machines = try {
            Parser.parse(cached.raw)
        } catch (e: Exception) {
            return Frame(null, message = "Unreadable reply from the relay.", messageColor = Palette.RED)
        }
        if (machines.isEmpty()) return Frame(null, message = "Nothing is pushing to this code yet.")

        val index = cached.index.coerceIn(0, machines.size - 1)
        val m = machines[index]
        val since = ((nowMs - cached.fetchedAtMs).coerceAtLeast(0)) / 1000

        // "Offline" is the machine's own silence when we fetched. The widget's age
        // is a separate fact, printed in the footer, so the two never blur together.
        val offline = m.ageS >= Freshness.offlineAfterSeconds(Freshness.DEFAULT_POLL_S)
        val link = when {
            cached.status == WidgetCache.Status.FAILED -> Link.NONE
            offline || since > Freshness.WIDGET_STALE_S -> Link.STALE
            else -> Link.OK
        }
        return Frame(m, index, machines.size, since, offline, link, cached.latencyMs)
    }
}
