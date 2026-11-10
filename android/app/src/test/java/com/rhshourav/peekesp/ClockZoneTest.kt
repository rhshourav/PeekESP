package com.rhshourav.peekesp

import com.rhshourav.peekesp.data.ClockFmt
import com.rhshourav.peekesp.data.ClockZones
import com.rhshourav.peekesp.data.HourFormat
import com.rhshourav.peekesp.data.ZoneChoice
import com.rhshourav.peekesp.data.ZoneSearch
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.time.Instant
import java.time.ZoneId
import java.util.Locale

class ClockZoneTest {
    private val choices = listOf(
        ZoneChoice("Asia/Dhaka", "Dhaka", "Bangladesh", "BD"),
        ZoneChoice("Asia/Kolkata", "Kolkata", "India", "IN"),
        ZoneChoice("Asia/Kathmandu", "Kathmandu", "Nepal", "NP"),
        ZoneChoice("America/New_York", "New York", "United States", "US"),
        ZoneChoice("America/Los_Angeles", "Los Angeles", "United States", "US"),
        ZoneChoice("America/Sao_Paulo", "Sao Paulo", "Brazil", "BR"),
        ZoneChoice("Europe/London", "London", "United Kingdom", "GB"),
        ZoneChoice("Pacific/Auckland", "Auckland", "New Zealand", "NZ"),
        ZoneChoice("UTC", "UTC", "", ""),
    )

    /** A fixed January offset table, so the offset search does not depend on today's date. */
    private val offsets = mapOf(
        "Asia/Dhaka" to "UTC+6", "Asia/Kolkata" to "UTC+5:30", "Asia/Kathmandu" to "UTC+5:45",
        "America/New_York" to "UTC-5", "America/Los_Angeles" to "UTC-8", "America/Sao_Paulo" to "UTC-3",
        "Europe/London" to "UTC", "Pacific/Auckland" to "UTC+13", "UTC" to "UTC",
    )

    private fun find(q: String) = ZoneSearch.search(q, choices, { offsets.getValue(it.id) }).map { it.id }

    // ---------------- trusting a saved zone ----------------

    @Test fun aKnownZoneResolves() {
        assertEquals("Asia/Dhaka", ClockZones.resolve("Asia/Dhaka")!!.id)
        assertEquals("Asia/Dhaka", ClockZones.resolve("  Asia/Dhaka ")!!.id)
    }

    @Test fun blankAndUnknownAndHostileValuesMeanFollowThePhone() {
        assertNull(ClockZones.resolve(null))
        assertNull(ClockZones.resolve(""))
        assertNull(ClockZones.resolve("   "))
        assertNull(ClockZones.resolve("Mars/Olympus_Mons"))
        assertNull(ClockZones.resolve("Asia/Dhaka; DROP TABLE samples"))
        assertNull(ClockZones.resolve("../../etc/passwd"))
        assertNull(ClockZones.resolve("+06:00"))          // an offset is not a zone this phone lists
        assertNull(ClockZones.resolve("a".repeat(5_000)))
    }

    @Test fun effectiveFallsBackToThePhonesOwnZone() {
        assertEquals(ZoneId.systemDefault(), ClockZones.effective(null))
        assertEquals(ZoneId.systemDefault(), ClockZones.effective("nonsense"))
        assertEquals("Asia/Tokyo", ClockZones.effective("Asia/Tokyo").id)
    }

    @Test fun cityNamesComeFromTheId() {
        assertEquals("Dhaka", ClockZones.cityOf("Asia/Dhaka"))
        assertEquals("Port of Spain", ClockZones.cityOf("America/Port_of_Spain"))
        assertEquals("Buenos Aires", ClockZones.cityOf("America/Argentina/Buenos_Aires"))
        assertEquals("", ClockZones.cityOf("UTC"))
    }

    // ---------------- how far from you ----------------

    @Test fun deltaIsSignedAndKeepsHalfHours() {
        val at = Instant.parse("2026-01-15T12:00:00Z")
        val dhaka = ZoneId.of("Asia/Dhaka")
        assertEquals(0, ClockZones.deltaMinutes(dhaka, dhaka, at))
        assertEquals(-30, ClockZones.deltaMinutes(ZoneId.of("Asia/Kolkata"), dhaka, at))
        assertEquals(-15, ClockZones.deltaMinutes(ZoneId.of("Asia/Kathmandu"), dhaka, at))
        assertEquals(-11 * 60, ClockZones.deltaMinutes(ZoneId.of("America/New_York"), dhaka, at))
    }

    @Test fun deltaMovesWithDaylightSaving() {
        val dhaka = ZoneId.of("Asia/Dhaka")
        val ny = ZoneId.of("America/New_York")
        assertEquals(-11 * 60, ClockZones.deltaMinutes(ny, dhaka, Instant.parse("2026-01-15T12:00:00Z")))
        assertEquals(-10 * 60, ClockZones.deltaMinutes(ny, dhaka, Instant.parse("2026-07-15T12:00:00Z")))
    }

    @Test fun deltaWords() {
        assertEquals("same time as you", ClockZones.deltaLabel(0))
        assertEquals("3h ahead", ClockZones.deltaLabel(180))
        assertEquals("5h 30m behind", ClockZones.deltaLabel(-330))
        assertEquals("45m ahead", ClockZones.deltaLabel(45))
    }

    // ---------------- hour format ----------------

    @Test fun phoneHoursFollowThePhoneAndTheOthersDoNot() {
        assertTrue(HourFormat.SYSTEM.is24(true))
        assertFalse(HourFormat.SYSTEM.is24(false))
        assertFalse(HourFormat.H12.is24(true))
        assertTrue(HourFormat.H24.is24(false))
    }

    // ---------------- searching ----------------

    @Test fun aCityIsFoundByItsNameWithOrWithoutAccents() {
        assertEquals("Asia/Dhaka", find("dhaka").first())
        assertEquals("America/Sao_Paulo", find("S\u00E3o Paulo").first())
        assertEquals("America/Sao_Paulo", find("sao paulo").first())
    }

    @Test fun anExactCityBeatsAPrefixAndAPrefixBeatsAWordInside() {
        val more = choices + ZoneChoice("Asia/Dhaka2", "Dhaka Annex", "Bangladesh", "BD") +
            ZoneChoice("Asia/OldDhaka", "Old Dhaka", "Bangladesh", "BD")
        val ids = ZoneSearch.search("dhaka", more, { "UTC+6" }).map { it.id }
        assertEquals(listOf("Asia/Dhaka", "Asia/Dhaka2", "Asia/OldDhaka"), ids)
    }

    @Test fun aCountryFindsEveryZoneInIt() {
        assertEquals(listOf("America/Los_Angeles", "America/New_York"), find("united states"))
        assertEquals(listOf("Asia/Dhaka"), find("bangladesh"))
    }

    @Test fun commonShortNamesForACountryWork() {
        assertEquals(listOf("Europe/London"), find("uk"))
        assertEquals(listOf("America/Los_Angeles", "America/New_York"), find("usa"))
        assertEquals(listOf("Asia/Dhaka"), find("bd"))
    }

    @Test fun anOffsetFindsZonesAtThatOffset() {
        assertTrue("Asia/Dhaka" in find("utc+6"))
        assertTrue("Asia/Kolkata" in find("utc+5:30"))
        assertFalse("Asia/Dhaka" in find("utc-5"))
    }

    @Test fun theIdAndAnythingInsideANameAreFound() {
        assertEquals(listOf("Asia/Kathmandu"), find("asia/kath"))
        assertTrue("America/Los_Angeles" in find("angeles"))
    }

    @Test fun nothingMatchesNothing() {
        assertTrue(find("zzzzzz").isEmpty())
    }

    @Test fun anEmptyQueryIsTheWholeListCapped() {
        assertEquals(choices.size, find("").size)
        assertEquals(3, ZoneSearch.search("", choices, { "" }, limit = 3).size)
    }

    // ---------------- the clock page's header ----------------

    private fun at(iso: String) = Instant.parse(iso).toEpochMilli()

    @Test fun theHeaderNamesThePlaceAndTheOffset() {
        val p = ClockFmt.parts(at("2026-10-19T00:15:00Z"), ZoneId.of("Asia/Dhaka"), Locale.ENGLISH, false)
        assertEquals("Dhaka", p.place)
        assertEquals("UTC+6", p.zone)
        assertEquals("Dhaka UTC+6", p.label)
        assertEquals("6:15", p.time)
        assertEquals("AM", p.ampm)
    }

    @Test fun aHalfHourZoneKeepsItsMinutesInTheHeader() {
        val p = ClockFmt.parts(at("2026-10-19T00:15:00Z"), ZoneId.of("Asia/Kathmandu"), Locale.ENGLISH, true)
        assertEquals("Kathmandu UTC+5:45", p.label)
        assertEquals("06:00", p.time)
    }

    @Test fun aZoneThatIsNotAPlaceShowsOnlyTheOffset() {
        val p = ClockFmt.parts(at("2026-10-19T00:15:00Z"), ZoneId.of("UTC"), Locale.ENGLISH, true)
        assertEquals("", p.place)
        assertEquals("UTC", p.label)
    }

    @Test fun theChosenRegionChangesTheDayNotJustTheHour() {
        // 20:00 on Monday in UTC is already Tuesday morning in Auckland.
        val utc = ClockFmt.parts(at("2026-10-19T20:00:00Z"), ZoneId.of("UTC"), Locale.ENGLISH, false)
        val nz = ClockFmt.parts(at("2026-10-19T20:00:00Z"), ZoneId.of("Pacific/Auckland"), Locale.ENGLISH, false)
        assertEquals("Monday", utc.weekday)
        assertEquals("Tuesday", nz.weekday)
        assertNotNull(nz.date)
    }
}
