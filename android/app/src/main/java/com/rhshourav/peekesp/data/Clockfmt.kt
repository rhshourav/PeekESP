package com.rhshourav.peekesp.data

import java.time.Instant
import java.time.ZoneId
import java.time.format.TextStyle
import java.util.Locale

/**
 * What the device's clock face prints (clock_faces.h), worked out in one place so
 * the widget bitmap and the tests agree.
 */
object ClockFmt {
    data class Parts(
        val time: String,
        /** "AM" / "PM", or empty in 24-hour mode. */
        val ampm: String,
        val weekday: String,
        val date: String,
        val zone: String,
        /** "Dhaka" for Asia/Dhaka; empty for a zone that is not a place ("UTC"). */
        val place: String = "",
    ) {
        /** What the clock page's header says: "Dhaka UTC+6", or just "UTC+6" when there is no place. */
        val label: String get() = if (place.isEmpty()) zone else "$place $zone"
    }

    fun parts(nowMs: Long, zone: ZoneId, locale: Locale, is24h: Boolean): Parts {
        val t = Instant.ofEpochMilli(nowMs).atZone(zone)
        val h = t.hour
        val mm = t.minute.toString().padStart(2, '0')
        // 0 and 12 both map to 12, one AM and one PM: a plain % 12 prints "0:15" at a quarter past midnight.
        val time = if (is24h) {
            "${h.toString().padStart(2, '0')}:$mm"
        } else {
            "${if (h % 12 == 0) 12 else h % 12}:$mm"
        }
        return Parts(
            time = time,
            ampm = if (is24h) "" else if (h < 12) "AM" else "PM",
            weekday = t.dayOfWeek.getDisplayName(TextStyle.FULL, locale),
            date = "${t.dayOfMonth} ${t.month.getDisplayName(TextStyle.FULL, locale)} ${t.year}",
            zone = utcLabel(t.offset.totalSeconds),
            place = ClockZones.cityOf(zone.id),
        )
    }

    /** "UTC+6", "UTC+5:45", "UTC-3:30", "UTC". Minutes matter: Kathmandu is +5:45. */
    fun utcLabel(offsetSeconds: Int): String {
        if (offsetSeconds == 0) return "UTC"
        val sign = if (offsetSeconds < 0) "-" else "+"
        val abs = kotlin.math.abs(offsetSeconds)
        val h = abs / 3600
        val m = abs % 3600 / 60
        return if (m == 0) "UTC$sign$h" else "UTC$sign$h:${m.toString().padStart(2, '0')}"
    }
}