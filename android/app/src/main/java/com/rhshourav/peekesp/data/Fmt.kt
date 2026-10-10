package com.rhshourav.peekesp.data

import java.util.Locale

/** Number formatting, kept in step with the device's fmt_* helpers. */
object Fmt {
    fun pct(v: Double?): String = v?.let { (it + 0.5).toInt().coerceIn(0, 999).toString() } ?: "--"

    fun temp(c: Double?): String = c?.let { "${Math.round(it)}\u00B0" } ?: "--"

    fun rate(kbps: Double?): String = when {
        kbps == null -> "--"
        kbps >= 1000 -> String.format(Locale.US, "%.1fM", kbps / 1024)
        else -> String.format(Locale.US, "%.0fk", kbps)
    }

    fun capacity(gb: Double?): String = when {
        gb == null -> ""
        gb >= 1024 -> String.format(Locale.US, "%.1fT", gb / 1024)
        gb >= 10 -> String.format(Locale.US, "%.0fG", gb)
        else -> String.format(Locale.US, "%.1fG", gb)
    }

    fun uptime(s: Long?): String {
        if (s == null) return ""
        val d = s / 86400
        val h = s % 86400 / 3600
        val m = s % 3600 / 60
        return when {
            d > 0 -> "up ${d}d ${h}h"
            h > 0 -> "up ${h}h ${m}m"
            else -> "up ${m}m"
        }
    }

    /** "offline 5m": how long a machine has been silent. */
    fun ago(s: Long): String {
        val d = s / 86400
        val h = s % 86400 / 3600
        val m = s % 3600 / 60
        return when {
            d > 0 -> "${d}d ${h}h"
            h > 0 -> "${h}h ${m}m"
            m > 0 -> "${m}m"
            else -> "${s}s"
        }
    }

    /** What the widget prints about its own age: "as of 12m ago". */
    fun asOf(s: Long): String = when {
        s < 60 -> "as of just now"
        s < 3600 -> "as of ${s / 60}m ago"
        s < 86400 -> "as of ${s / 3600}h ago"
        else -> "as of ${s / 86400}d ago"
    }

    fun latency(ms: Long): String = when {
        ms <= 0 -> "-- ms"
        ms < 1000 -> "$ms ms"
        else -> String.format(Locale.US, "%.1f s", ms / 1000.0)
    }
}
