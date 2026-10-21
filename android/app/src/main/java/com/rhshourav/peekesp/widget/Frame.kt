package com.rhshourav.peekesp.widget

import android.content.Context
import androidx.compose.runtime.mutableStateOf
import com.rhshourav.peekesp.data.Battery
import com.rhshourav.peekesp.data.ClockFmt
import com.rhshourav.peekesp.data.Fmt
import com.rhshourav.peekesp.data.Freshness
import com.rhshourav.peekesp.data.Machine
import com.rhshourav.peekesp.data.Parser
import com.rhshourav.peekesp.data.SetupStore
import com.rhshourav.peekesp.data.StyleStore
import com.rhshourav.peekesp.data.WidgetCache
import com.rhshourav.peekesp.data.WidgetStyle
import java.time.ZoneId
import java.util.Locale
import kotlin.math.roundToInt

/**
 * The colours, in two modes. Dark is the device's own palette (PeekESP.ino); light is the same
 * identity re-tuned for a white card, with every text colour checked to 4.5:1 against both
 * the page and the card (PaletteTest keeps it that way).
 *
 * Every colour is a getter that answers for the mode in force right now, so code that writes
 * Palette.TEXT does not have to know which mode it is in. Two things decide the mode:
 *
 *  - the app's own theme, held as Compose state: anything that reads a colour while composing
 *    is recomposed when the theme changes;
 *  - [forMode], which forces a mode for the current thread while something is being drawn. The
 *    widget uses it, so a light app can still draw a dark widget (and the reverse) without the
 *    two ever seeing each other's colours.
 */
object Palette {
    private val appDark = mutableStateOf(true)
    private val forced = ThreadLocal<Boolean>()

    val isDark: Boolean get() = forced.get() ?: appDark.value

    fun setAppDark(dark: Boolean) {
        if (appDark.value != dark) appDark.value = dark
    }

    /** Runs [block] with [dark] in force on this thread, then puts back whatever was there. */
    fun <T> forMode(dark: Boolean, block: () -> T): T {
        val previous = forced.get()
        forced.set(dark)
        try {
            return block()
        } finally {
            if (previous == null) forced.remove() else forced.set(previous)
        }
    }

    private fun pick(dark: Long, light: Long): Int = (if (isDark) dark else light).toInt()

    val BG: Int get() = pick(0xFF05070E, 0xFFF3F6FB)
    val PANEL: Int get() = pick(0xFF0B1220, 0xFFFFFFFF)
    val TRACK: Int get() = pick(0xFF1A2334, 0xFFD5DDEA)
    val CYAN: Int get() = pick(0xFF00E5FF, 0xFF00788C)
    val MAGENTA: Int get() = pick(0xFFFF2E7E, 0xFFC81E66)
    val AMBER: Int get() = pick(0xFFFFC145, 0xFF9A6200)
    val GREEN: Int get() = pick(0xFF35F2A0, 0xFF077A4B)
    val RED: Int get() = pick(0xFFFF4D6D, 0xFFC62840)
    val TEXT: Int get() = pick(0xFFE6EDF7, 0xFF0B1220)
    val VIOLET: Int get() = pick(0xFFB28DFF, 0xFF6B3FD4)

    /** Text and glyphs that sit on an accent colour (a selected tab, a primary button). */
    val ON_ACCENT: Int get() = pick(0xFF05070E, 0xFFFFFFFF)

    /** Lighter than the device's 5C6B82, which is only 3.7:1 on its own background. Glass needs more. */
    val DIM: Int get() = pick(0xFF93A3BA, 0xFF55657D)

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

enum class Link(val label: String) {
    OK("LINK OK"),
    STALE("STALE"),
    NONE("NO LINK");

    /** Read when drawn, so it is right for the mode being drawn. */
    val color: Int
        get() = when (this) {
            OK -> Palette.GREEN
            STALE -> Palette.AMBER
            NONE -> Palette.RED
        }
}

/** How a status message is coloured. A name, not a colour, because the colour depends on the mode. */
enum class MessageTone { DIM, AMBER, RED }

/** What a page of the widget shows. The device has the same three. */
enum class PageKind { MACHINE, POWER, CLOCK }

/** One stop in the tap cycle. [machine] is an index into the machine list, or -1 for the clock. */
data class PageRef(val kind: PageKind, val machine: Int)

object Pages {
    /**
     * Every machine, then one battery page for all of them (if enabled), then the
     * clock last.
     *
     * The battery page is one page however many machines there are, and it is
     * there even when none of them has a battery: that page then says so, per
     * machine, instead of vanishing and leaving you to wonder whether it is a bug.
     * The device does the same ("host  no battery").
     */
    fun build(machines: List<Machine>, look: WidgetStyle): List<PageRef> = buildList {
        machines.indices.forEach { add(PageRef(PageKind.MACHINE, it)) }
        if (look.battery && machines.isNotEmpty()) add(PageRef(PageKind.POWER, -1))
        if (look.clock) add(PageRef(PageKind.CLOCK, -1))
    }

    /** How many pages a tap cycles through right now, for the cache to wrap around. */
    fun count(ctx: Context): Int {
        val cached = WidgetCache(ctx).read() ?: return 1
        val machines = try {
            Parser.parse(cached.raw)
        } catch (e: Exception) {
            return 1
        }
        return build(machines, StyleStore(ctx).read()).size.coerceAtLeast(1)
    }
}

/** Everything the renderer needs, already decided. */
data class Frame(
    val machine: Machine?,
    /** Position in the tap cycle, and how many pages there are. */
    val index: Int = 0,
    val count: Int = 0,
    /** Seconds since this widget last got an answer from the relay. */
    val sinceFetchS: Long = 0,
    val offline: Boolean = false,
    val link: Link = Link.STALE,
    val latencyMs: Long = 0,
    val message: String? = null,
    val messageTone: MessageTone = MessageTone.DIM,
    val kind: PageKind = PageKind.MACHINE,
    /** Wall clock when this frame was built; the clock page prints the date from it. */
    val nowMs: Long = 0,
    /**
     * True: the renderer draws the time itself (the in-app preview).
     * False: it leaves a gap, because on the home screen a live TextClock sits
     * there and keeps the time right between redraws.
     */
    val drawTime: Boolean = true,
    val showBattery: Boolean = true,
    /** Every machine, for the battery page. */
    val machines: List<Machine> = emptyList(),
) {
    /** The widget is a bitmap, so everything on it has to be said here for TalkBack. */
    fun describe(): String {
        if (kind == PageKind.CLOCK) {
            // No time here: it would be stale. The live clock on the widget is read out by itself.
            val p = ClockFmt.parts(nowMs, ZoneId.systemDefault(), Locale.getDefault(), false)
            val parts = mutableListOf("PeekESP clock", "${p.weekday}, ${p.date}")
            message?.let { parts += it }
            if (count > 1) parts += "tap for the next page"
            return parts.joinToString(". ")
        }
        if (kind == PageKind.POWER) {
            val parts = mutableListOf("PeekESP battery page")
            machines.forEach { mm ->
                val b = Battery.of(mm)
                parts += if (b.present) "${mm.host}: ${b.pct} percent, ${b.tail}" else "${mm.host}: no battery"
            }
            parts += Fmt.asOf(sinceFetchS)
            if (count > 1) parts += "tap for the next page"
            return parts.joinToString(". ")
        }
        val m = machine ?: return "PeekESP. ${message ?: "No data."}"
        val parts = mutableListOf<String>()
        parts += if (count > 1) "${m.host}, page ${index + 1} of $count" else m.host
        if (offline) parts += "offline, last heard ${Fmt.ago(m.ageS + sinceFetchS)} ago"
        if (kind == PageKind.MACHINE) {
            m.cpu?.let { parts += "CPU ${it.roundToInt()} percent" }
            m.ram?.let { parts += "RAM ${it.roundToInt()} percent" }
            m.tempC?.let { parts += "temperature ${it.roundToInt()} degrees" }
            m.storagePct?.let {
                val free = if (m.storageFreeGb != null) ", ${Fmt.capacity(m.storageFreeGb)} free" else ""
                parts += "storage ${it.roundToInt()} percent used$free"
            }
            m.rxKbps?.let { parts += "download ${Fmt.rate(it)}" }
            m.txKbps?.let { parts += "upload ${Fmt.rate(it)}" }
        }
        if (showBattery) {
            val b = Battery.of(m)
            if (b.present) parts += "battery ${b.pct} percent, ${b.tail}"
        }
        if (!offline) Fmt.uptime(m.uptimeS).takeIf { it.isNotEmpty() }?.let { parts += it }
        parts += "link ${link.label.lowercase()}"
        parts += Fmt.asOf(sinceFetchS + m.ageS)
        if (count > 1) parts += "tap for the next page"
        return parts.joinToString(". ")
    }
}

object FrameBuilder {
    private const val NOTHING = "Nothing is pushing to this code yet."

    fun build(ctx: Context, nowMs: Long): Frame {
        if (!SetupStore(ctx).has()) {
            return Frame(null, message = "Not paired. Open PeekESP and enter your code.", messageTone = MessageTone.AMBER)
        }
        val cached = WidgetCache(ctx).read()
            ?: return Frame(null, message = "Waiting for the first reading...")
        if (cached.status == WidgetCache.Status.AUTH) {
            return Frame(null, message = "Code rejected. Pair again in the app.", messageTone = MessageTone.RED)
        }
        val machines = try {
            Parser.parse(cached.raw)
        } catch (e: Exception) {
            return Frame(null, message = "Unreadable reply from the relay.", messageTone = MessageTone.RED)
        }

        val look = StyleStore(ctx).read()
        val pages = Pages.build(machines, look)
        if (pages.isEmpty()) return Frame(null, message = NOTHING)

        val index = cached.index.coerceIn(0, pages.size - 1)
        val page = pages[index]
        val since = ((nowMs - cached.fetchedAtMs).coerceAtLeast(0)) / 1000

        if (page.kind == PageKind.CLOCK) {
            return Frame(
                machine = null, index = index, count = pages.size, sinceFetchS = since,
                kind = PageKind.CLOCK, nowMs = nowMs, drawTime = false, showBattery = look.battery,
                // A clock with no machines would otherwise hide why there is nothing else to see.
                message = if (machines.isEmpty()) NOTHING else null,
            )
        }

        if (page.kind == PageKind.POWER) {
            val link = when {
                cached.status == WidgetCache.Status.FAILED -> Link.NONE
                since > Freshness.WIDGET_STALE_S -> Link.STALE
                else -> Link.OK
            }
            return Frame(
                machine = null, index = index, count = pages.size, sinceFetchS = since, link = link,
                kind = PageKind.POWER, nowMs = nowMs, drawTime = false, showBattery = look.battery,
                machines = machines,
            )
        }

        val m = machines[page.machine]

        // "Offline" is the machine's own silence when we fetched. The widget's age
        // is a separate fact, printed in the footer, so the two never blur together.
        val offline = m.ageS >= Freshness.offlineAfterSeconds(Freshness.DEFAULT_POLL_S)
        val link = when {
            cached.status == WidgetCache.Status.FAILED -> Link.NONE
            offline || since > Freshness.WIDGET_STALE_S -> Link.STALE
            else -> Link.OK
        }
        return Frame(
            machine = m, index = index, count = pages.size, sinceFetchS = since,
            offline = offline, link = link, latencyMs = cached.latencyMs,
            kind = page.kind, nowMs = nowMs, drawTime = false, showBattery = look.battery,
        )
    }
}