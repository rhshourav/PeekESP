package com.rhshourav.peekesp.data

import kotlin.math.abs
import kotlin.math.ceil
import kotlin.math.floor
import kotlin.math.log10
import kotlin.math.max
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
     * or no reading) and a long silence both end a run, so a chart never joins
     * the last reading before an outage to the first one after it.
     */
    fun segments(points: List<Pair<Long, Double?>>, gapMs: Long): List<List<Pair<Long, Double>>> {
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
            if (prev != null && t - prev > gapMs && run.isNotEmpty()) {
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