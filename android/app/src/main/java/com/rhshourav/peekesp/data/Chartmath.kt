package com.rhshourav.peekesp.data

import kotlin.math.abs
import kotlin.math.ceil
import kotlin.math.floor
import kotlin.math.log10
import kotlin.math.max
import kotlin.math.min
import kotlin.math.pow

/** The windows the dashboard can show. Longer ones need a longer retention. */
enum class HistoryRange(val label: String, val ms: Long) {
    H1("1h", 3_600_000L),
    H6("6h", 6 * 3_600_000L),
    D1("24h", 86_400_000L),
    D7("7d", 7 * 86_400_000L),
    D30("30d", 30 * 86_400_000L);

    companion object {
        /** Ranges that fit inside what is kept. A 1-day history cannot draw a 7-day chart. */
        fun availableFor(retentionDays: Int): List<HistoryRange> =
            values().filter { it.ms <= retentionDays * 86_400_000L }.ifEmpty { listOf(H1) }
    }
}

/** The arithmetic behind the charts, kept free of Android so it can be tested. */
object ChartMath {
    /** About this many points per chart: enough to look smooth, few enough to draw instantly. */
    const val TARGET_POINTS = 150
    const val MIN_BUCKET_MS = 15_000L

    /**
     * The widget logs in the background every 15 minutes, the platform's floor.
     * Readings that far apart are one run, not a gap: a line joins them.
     */
    const val BACKGROUND_GAP_MS = 20 * 60_000L

    fun bucketMs(range: HistoryRange): Long = max(MIN_BUCKET_MS, range.ms / TARGET_POINTS)

    /** Further apart than this and the machine was off or unreachable: break the line. */
    fun gapMs(bucketMs: Long): Long = max(bucketMs * 3, BACKGROUND_GAP_MS)

    /**
     * Splits a series into runs that should be drawn as one line. A null (no sensor,
     * or no reading), a long silence, and a known outage ([cuts]) all end a run, so a
     * chart never joins the last reading before an outage to the first one after it.
     */
    fun segments(
        points: List<Pair<Long, Double?>>,
        gapMs: Long,
        cuts: List<Pair<Long, Long>> = emptyList(),
    ): List<List<Pair<Long, Double>>> {
        val out = mutableListOf<List<Pair<Long, Double>>>()
        var run = mutableListOf<Pair<Long, Double>>()
        var lastT: Long? = null
        for ((t, v) in points) {
            if (v == null) {
                if (run.isNotEmpty()) out += run
                run = mutableListOf()
                lastT = null
                continue
            }
            val prev = lastT
            // A known outage between two readings (the phone had no internet, the machine was off)
            // cuts the line even when the readings are close together.
            val cut = prev != null && cuts.any { (a, b) -> a < t && b > prev }
            if (prev != null && (t - prev > gapMs || cut) && run.isNotEmpty()) {
                out += run
                run = mutableListOf()
            }
            run += t to v
            lastT = t
        }
        if (run.isNotEmpty()) out += run
        return out
    }

    /** The value nearest to [t], or null if nothing was logged within [maxDistMs] of it. */
    fun valueAt(points: List<Pair<Long, Double?>>, t: Long, maxDistMs: Long): Double? {
        var best: Pair<Long, Double>? = null
        for ((pt, v) in points) {
            if (v == null) continue
            if (best == null || abs(pt - t) < abs(best.first - t)) best = pt to v
        }
        val b = best ?: return null
        return if (abs(b.first - t) <= maxDistMs) b.second else null
    }

    /** 1, 2, 5, 10, 20, 50 ... at or above [v]: a ceiling a person would pick for an axis. */
    fun niceCeil(v: Double): Double {
        if (v <= 0.0) return 1.0
        val e = floor(log10(v))
        val f = v / 10.0.pow(e)
        val n = when {
            f <= 1.0 -> 1.0
            f <= 2.0 -> 2.0
            f <= 5.0 -> 5.0
            else -> 10.0
        }
        return n * 10.0.pow(e)
    }

    /** Bounds for a quantity with no natural zero, like a temperature: padded, on multiples of 5. */
    fun axisFor(min: Double, max: Double): Pair<Double, Double> {
        val pad = max((max - min) * 0.15, 2.0)
        val lo = floor((min - pad) / 5.0) * 5.0
        var hi = ceil((max + pad) / 5.0) * 5.0
        if (hi - lo < 10.0) hi = lo + 10.0
        return lo to hi
    }

    /** Runs of true values as [start, end] time spans, for shading (charging, for instance). */
    fun flagBands(points: List<Pair<Long, Boolean>>, halfMs: Long, gapMs: Long): List<Pair<Long, Long>> {
        val out = mutableListOf<Pair<Long, Long>>()
        var start: Long? = null
        var last = 0L
        for ((t, on) in points) {
            val s = start
            if (on) {
                if (s != null && t - last > gapMs) {
                    out += (s - halfMs) to (last + halfMs)
                    start = t
                } else if (s == null) {
                    start = t
                }
                last = t
            } else if (s != null) {
                out += (s - halfMs) to (last + halfMs)
                start = null
            }
        }
        start?.let { out += (it - halfMs) to (last + halfMs) }
        return out
    }
}

/**
 * What the phone knew about its own path to the relay at a moment. This is separate from
 * whether any machine is on: a machine can be perfectly well while the phone has no signal.
 */
enum class LinkKind(val code: Int) {
    OK(0),
    PHONE_OFFLINE(1),
    RELAY_DOWN(2);

    companion object {
        fun of(code: Int): LinkKind = values().firstOrNull { it.code == code } ?: OK
    }
}

data class LinkEvent(val t: Long, val kind: LinkKind)

/**
 * The reasons a stretch of a chart can be empty or unreliable, strongest last: when two
 * apply, the later one wins. The order is the priority.
 */
enum class Coverage { ONLINE, MACHINE_OFFLINE, RELAY_DOWN, PHONE_OFFLINE }

data class Span(val kind: Coverage, val start: Long, val end: Long)

data class Shares(
    val online: Double,
    val machineOffline: Double,
    val relayDown: Double,
    val phoneOffline: Double,
    val noData: Double,
)

/**
 * Turns three kinds of evidence into one timeline of what was happening:
 *
 *  - readings we have        -> the machine was ONLINE between them
 *  - the relay said a machine had been silent for a minute or more -> MACHINE_OFFLINE
 *    (only learnt when we could reach the relay, so it is trustworthy)
 *  - the phone could not reach the relay -> PHONE_OFFLINE or RELAY_DOWN. Here the machine
 *    is simply unknown, so this beats everything else.
 *
 * Whatever none of them covers is "no data": the app was not running and nothing was asked.
 */
object Availability {
    /** A machine silent this long counts as offline: the same rule the widget and device use. */
    const val OFFLINE_AFTER_MS = 60_000L

    /** A phone-side outage is only assumed to last this long past the last time it was observed. */
    const val LINK_HOLD_MS = 20 * 60_000L

    private data class Iv(val start: Long, val end: Long, val kind: Coverage)

    fun build(
        from: Long,
        to: Long,
        times: List<Long>,
        joinGapMs: Long,
        tailMs: Long,
        link: List<LinkEvent>,
        machineOffline: List<Pair<Long, Long>>,
    ): List<Span> {
        val ivs = ArrayList<Iv>()

        // Readings close enough together are one run of "online". The last one holds a little
        // past itself, since "now" is always a moment after the newest reading.
        for (i in 0 until times.size - 1) {
            if (times[i + 1] - times[i] <= joinGapMs) ivs += Iv(times[i], times[i + 1], Coverage.ONLINE)
        }
        times.lastOrNull()?.let { ivs += Iv(it, min(to, it + tailMs), Coverage.ONLINE) }

        machineOffline.forEach { (a, b) -> ivs += Iv(a, b, Coverage.MACHINE_OFFLINE) }

        // A failure holds until the next time the phone looked, but no longer than LINK_HOLD_MS.
        val events = link.sortedBy { it.t }
        events.forEachIndexed { i, e ->
            if (e.kind == LinkKind.OK) return@forEachIndexed
            val next = events.getOrNull(i + 1)?.t
            val end = if (next != null) min(next, e.t + LINK_HOLD_MS) else min(to, e.t + LINK_HOLD_MS)
            ivs += Iv(e.t, end, if (e.kind == LinkKind.PHONE_OFFLINE) Coverage.PHONE_OFFLINE else Coverage.RELAY_DOWN)
        }

        return paint(from, to, ivs)
    }

    /** Sweeps the intervals left to right, and in each stretch keeps the strongest kind covering it. */
    private fun paint(from: Long, to: Long, all: List<Iv>): List<Span> {
        val ivs = all.map { Iv(max(it.start, from), min(it.end, to), it.kind) }.filter { it.end > it.start }
        if (ivs.isEmpty()) return emptyList()

        val points = (ivs.flatMap { listOf(it.start, it.end) } + from + to).distinct().sorted()
        val out = ArrayList<Span>()
        for (i in 0 until points.size - 1) {
            val a = points[i]
            val b = points[i + 1]
            val kind = ivs.filter { it.start <= a && it.end >= b }.maxByOrNull { it.kind.ordinal }?.kind ?: continue
            val last = out.lastOrNull()
            if (last != null && last.kind == kind && last.end == a) {
                out[out.size - 1] = Span(kind, last.start, b)
            } else {
                out += Span(kind, a, b)
            }
        }
        return out
    }

    fun shares(spans: List<Span>, from: Long, to: Long): Shares {
        val total = (to - from).coerceAtLeast(1L).toDouble()
        fun of(k: Coverage) = spans.filter { it.kind == k }.sumOf { it.end - it.start } / total
        val on = of(Coverage.ONLINE)
        val m = of(Coverage.MACHINE_OFFLINE)
        val r = of(Coverage.RELAY_DOWN)
        val p = of(Coverage.PHONE_OFFLINE)
        return Shares(on, m, r, p, (1.0 - on - m - r - p).coerceAtLeast(0.0))
    }
}