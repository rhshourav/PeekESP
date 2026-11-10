package com.rhshourav.peekesp.data

import java.text.Normalizer
import java.time.DateTimeException
import java.time.Instant
import java.time.ZoneId
import java.util.Locale
import kotlin.math.abs

/** How the clock page writes the hour: whichever the phone uses, or one of the two, always. */
enum class HourFormat(val label: String) {
    SYSTEM("Phone"),
    H12("12-hour"),
    H24("24-hour");

    /** [phoneIs24] is the phone's own setting; it only matters for [SYSTEM]. */
    fun is24(phoneIs24: Boolean): Boolean = when (this) {
        SYSTEM -> phoneIs24
        H12 -> false
        H24 -> true
    }
}

/**
 * One place a clock can be set to. A zone is what a clock actually needs: a country can have many
 * (the US has six or more, Russia eleven), so the country is something to search by, not the choice.
 */
data class ZoneChoice(val id: String, val city: String, val country: String, val region: String)

/**
 * Zones, and the rules for trusting one.
 *
 * The saved zone is text read back from storage, then handed to Android's clock view. It is only ever
 * used if it is on the platform's own list of zones, so nothing else can get through.
 */
object ClockZones {
    private val known: Set<String> by lazy { ZoneId.getAvailableZoneIds() }

    /** The zone for [id], or null if it is blank or not a zone this phone knows: then the caller follows the phone. */
    fun resolve(id: String?): ZoneId? {
        val s = id?.trim().orEmpty()
        if (s.isEmpty() || s !in known) return null
        return try {
            ZoneId.of(s)
        } catch (e: DateTimeException) {
            null
        }
    }

    /** The zone to draw: the one picked, or the phone's own, which follows the user when they travel. */
    fun effective(id: String?): ZoneId = resolve(id) ?: ZoneId.systemDefault()

    /** "America/Port_of_Spain" -> "Port of Spain". Empty for ids that are not a place ("UTC", "EST"). */
    fun cityOf(id: String): String =
        if (!id.contains('/')) "" else id.substringAfterLast('/').replace('_', ' ')

    /** How far ahead (+) or behind (-) of [base] [zone] is at [at], in minutes. Moves with daylight saving. */
    fun deltaMinutes(zone: ZoneId, base: ZoneId, at: Instant): Int =
        (zone.rules.getOffset(at).totalSeconds - base.rules.getOffset(at).totalSeconds) / 60

    fun deltaLabel(minutes: Int): String {
        if (minutes == 0) return "same time as you"
        val a = abs(minutes)
        val h = a / 60
        val m = a % 60
        val span = when {
            h > 0 && m > 0 -> "${h}h ${m}m"
            h > 0 -> "${h}h"
            else -> "${m}m"
        }
        return span + if (minutes > 0) " ahead" else " behind"
    }
}

/** Finding a zone by what a person types: a city, a country, its code, or an offset like "UTC+6". */
object ZoneSearch {
    private val MARKS = Regex("\\p{M}+")

    /** What people type for a country code that is not the ISO one. */
    private val ALIASES = mapOf("uk" to "gb", "usa" to "us", "uae" to "ae")

    /** Lower case with accents removed, so "sao paulo" finds "São Paulo". */
    fun fold(s: String): String = Normalizer.normalize(s, Normalizer.Form.NFD).replace(MARKS, "").lowercase().trim()

    /**
     * Best matches first. A city that is the query beats one that starts with it, which beats a word that
     * starts with it, then country, then anywhere in the name, the id, or the offset. [offsetOf] gives a
     * zone's current offset label ("UTC+6"), which moves with daylight saving, so it is asked for, not stored.
     */
    fun search(query: String, all: List<ZoneChoice>, offsetOf: (ZoneChoice) -> String, limit: Int = 80): List<ZoneChoice> {
        val q = fold(query)
        if (q.isEmpty()) return all.take(limit)
        val code = ALIASES[q] ?: q
        return all.mapNotNull { z ->
            val city = fold(z.city)
            val country = fold(z.country)
            val score = when {
                city == q -> 0
                city.startsWith(q) -> 1
                city.split(' ').any { it.startsWith(q) } -> 2
                country == q || country.startsWith(q) -> 3
                z.region.isNotEmpty() && z.region.equals(code, ignoreCase = true) -> 3
                city.contains(q) -> 4
                country.contains(q) -> 5
                fold(z.id).contains(q) -> 6
                offsetOf(z).lowercase().contains(q) -> 7
                else -> return@mapNotNull null
            }
            score to z
        }
            .sortedWith(compareBy({ it.first }, { it.second.city }))
            .take(limit)
            .map { it.second }
    }
}
