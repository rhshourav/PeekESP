package com.rhshourav.peekesp.data

import java.time.DayOfWeek
import java.time.ZonedDateTime

/**
 * How much a message matters, and so how loudly it speaks:
 * INFO is a quiet line, WARNING makes a sound, CRITICAL rings like an alarm until it is silenced.
 */
enum class Severity(val label: String) {
    INFO("Info"),
    WARNING("Warning"),
    CRITICAL("Critical");

    companion object {
        /**
         * Lenient on purpose: whatever else sends a message (a security tool, a script) will have
         * its own words for "bad". Red means critical, because red is what rings.
         */
        fun parse(raw: String?): Severity = when (raw?.trim()?.lowercase()) {
            "critical", "crit", "red", "high", "alert", "emergency", "fatal", "error" -> CRITICAL
            "warning", "warn", "yellow", "amber", "orange", "medium" -> WARNING
            else -> INFO
        }
    }
}

/** Where an entry in the inbox came from. */
enum class EventKind(val code: Int) {
    /** Words sent by a machine, another app, or you. */
    MESSAGE(0),

    /** A rule you set tripped (or cleared). */
    ALERT(1),

    /** A machine went offline or came back. */
    STATUS(2);

    companion object {
        fun of(code: Int): EventKind = values().firstOrNull { it.code == code } ?: MESSAGE
    }
}

/**
 * Something that happened. [key] is what makes it the same thing the second time it is seen:
 * a machine re-sends its recent messages in every reading, and a rule stays tripped across many,
 * but each must reach you once. [silent] means "keep it in the inbox, do not notify".
 */
data class NewEvent(
    val key: String,
    val t: Long,
    val severity: Severity,
    val title: String,
    val body: String,
    val source: String,
    val machine: String?,
    val kind: EventKind,
    val silent: Boolean = false,
)

data class Event(
    val id: Long,
    val key: String,
    val t: Long,
    val severity: Severity,
    val title: String,
    val body: String,
    val source: String,
    val machine: String?,
    val kind: EventKind,
    val read: Boolean,
    val notified: Boolean,
)

enum class Op(val label: String) {
    ABOVE("is above"),
    BELOW("is below"),

    /** Far above what this machine normally does, judged from its own recent history. */
    UNUSUAL("is unusually high"),
}

/**
 * One thing to watch. [threshold] is a limit for ABOVE and BELOW, and a sensitivity for UNUSUAL
 * (how many "normal spreads" above normal counts as unusual: higher means fewer alerts).
 * [forMinutes] is how long it must stay true, so a spike is not an emergency.
 * [title] and [body] are optional wording of your own; see [Template] for what you can put in them.
 */
data class Rule(
    val id: String,
    val name: String,
    val enabled: Boolean,
    val metric: String,
    val op: Op,
    val threshold: Double,
    val forMinutes: Int,
    val severity: Severity,
    val machine: String?,
    val title: String = "",
    val body: String = "",
)

/**
 * A daily window when notifications are held back (they still land in the inbox).
 * The window may cross midnight; [days] says which days it *starts* on (bit 0 = Monday).
 */
data class QuietHours(
    val enabled: Boolean,
    val startMin: Int,
    val endMin: Int,
    val days: Int,
    val criticalBreaksThrough: Boolean,
) {
    fun contains(at: ZonedDateTime): Boolean {
        if (!enabled || startMin == endMin) return false
        val minute = at.hour * 60 + at.minute
        fun on(d: DayOfWeek) = days and (1 shl (d.value - 1)) != 0
        return when {
            startMin < endMin -> minute in startMin until endMin && on(at.dayOfWeek)
            minute >= startMin -> on(at.dayOfWeek)                 // the evening half: the day it started
            minute < endMin -> on(at.dayOfWeek.minus(1))           // the small hours: the day before
            else -> false
        }
    }

    companion object {
        const val ALL_DAYS = 0b1111111
    }
}

data class AlertSettings(
    val enabled: Boolean,
    val notifyOffline: Boolean,
    val notifyOnline: Boolean,
    val notifyResolved: Boolean,
    val notifyMessages: Boolean,
    /** A machine silent this long counts as offline. */
    val offlineAfterMin: Int,
    val quiet: QuietHours,
    val mutedMachines: Set<String>,
    val rules: List<Rule>,
    val externalEnabled: Boolean,
    val externalToken: String,
)

object AlertDefaults {
    fun rules(): List<Rule> = listOf(
        Rule("cpu-high", "CPU running hot", true, "cpu_percent", Op.ABOVE, 90.0, 5, Severity.WARNING, null),
        Rule("ram-high", "Memory almost full", true, "ram_percent", Op.ABOVE, 90.0, 5, Severity.WARNING, null),
        Rule("temp-high", "CPU too hot", true, "cpu_temp_c", Op.ABOVE, 85.0, 2, Severity.CRITICAL, null),
        Rule("storage-full", "Storage nearly full", true, "storage_percent", Op.ABOVE, 95.0, 0, Severity.WARNING, null),
        // "while unplugged" is a metric of its own, so a laptop on its charger at 4% is not an emergency.
        Rule("battery-low", "Battery low", true, "battery_unplugged_percent", Op.BELOW, 15.0, 0, Severity.WARNING, null),
        Rule("battery-critical", "Battery critically low", true, "battery_unplugged_percent", Op.BELOW, 5.0, 0, Severity.CRITICAL, null),
        Rule("net-unusual", "Unusual network activity", true, "net_total_kbps", Op.UNUSUAL, 6.0, 2, Severity.WARNING, null),
    )

    fun quiet(): QuietHours = QuietHours(false, 23 * 60, 7 * 60, QuietHours.ALL_DAYS, true)

    fun settings(): AlertSettings = AlertSettings(
        enabled = true,
        notifyOffline = true,
        notifyOnline = true,
        notifyResolved = false,
        notifyMessages = true,
        offlineAfterMin = 2,
        quiet = quiet(),
        mutedMachines = emptySet(),
        rules = rules(),
        externalEnabled = false,
        externalToken = "",
    )
}

/** {machine}, {metric}, {value} and friends, filled in. Anything unknown is left as typed, so a typo shows. */
object Template {
    fun render(text: String, vars: Map<String, String>): String {
        var out = text
        for ((k, v) in vars) out = out.replace("{$k}", v)
        return out
    }
}