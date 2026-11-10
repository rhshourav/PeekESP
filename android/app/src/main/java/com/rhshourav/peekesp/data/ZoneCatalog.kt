package com.rhshourav.peekesp.data

import android.content.Context
import java.time.ZoneId
import java.util.IllformedLocaleException
import java.util.Locale

/**
 * Every place the clock can be set to, built from the phone's own time-zone data: no list to keep
 * up to date, and no network. Needs Android (android.icu, API 24+), so it is kept apart from the
 * pure logic in ClockZone.kt, which the unit tests cover.
 */
object ZoneCatalog {
    private val PLACE_ID = Regex("^(Africa|America|Antarctica|Arctic|Asia|Atlantic|Australia|Europe|Indian|Pacific)/.+$")

    /** Built on first use, A to Z by city. */
    val all: List<ZoneChoice> by lazy { build() }

    fun find(id: String): ZoneChoice? = all.firstOrNull { it.id == id }

    private fun build(): List<ZoneChoice> {
        val java = ZoneId.getAvailableZoneIds()
        // ICU's "canonical location" zones: one per place, none of the legacy aliases (US/Eastern, Asia/Calcutta...).
        val icu: Collection<String> = try {
            android.icu.util.TimeZone.getAvailableIDs(
                android.icu.util.TimeZone.SystemTimeZoneType.CANONICAL_LOCATION, null, null,
            )
        } catch (e: Throwable) {
            emptyList()
        }
        // Only zones the clock can actually use. If ICU says nothing, fall back to the platform's place ids.
        val ids = icu.filter { it in java }.ifEmpty { java.filter { PLACE_ID.matches(it) } }

        val out = ids.map { id ->
            val region = regionOf(id)
            ZoneChoice(id, ClockZones.cityOf(id).ifEmpty { id }, countryName(region), region)
        }.toMutableList()
        if ("UTC" in java) out += ZoneChoice("UTC", "UTC", "", "")
        return out.sortedBy { it.city }
    }

    private fun regionOf(id: String): String = try {
        val r = android.icu.util.TimeZone.getRegion(id)
        // "001" and "ZZ" mean "no country".
        if (r.length == 2 && r != "ZZ") r else ""
    } catch (e: Throwable) {
        ""
    }

    private fun countryName(region: String): String {
        if (region.isEmpty()) return ""
        return try {
            Locale.Builder().setRegion(region).build().getDisplayCountry(Locale.ENGLISH)
        } catch (e: IllformedLocaleException) {
            ""
        }
    }

    // ---- the last few regions picked, so flipping between home and work is one tap ----

    private const val MAX_RECENT = 4

    private fun prefs(ctx: Context) = ctx.getSharedPreferences("peek_clock", Context.MODE_PRIVATE)

    fun recents(ctx: Context): List<ZoneChoice> {
        val byId = all.associateBy { it.id }
        return prefs(ctx).getString("recent", "").orEmpty().split(',').mapNotNull { byId[it.trim()] }
    }

    fun noteUsed(ctx: Context, id: String) {
        if (ClockZones.resolve(id) == null) return
        val next = (listOf(id) + prefs(ctx).getString("recent", "").orEmpty().split(','))
            .map { it.trim() }
            .filter { it.isNotEmpty() }
            .distinct()
            .take(MAX_RECENT)
        prefs(ctx).edit().putString("recent", next.joinToString(",")).apply()
    }
}
