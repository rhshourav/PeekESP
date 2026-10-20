package com.rhshourav.peekesp

import com.rhshourav.peekesp.data.ChartMath
import com.rhshourav.peekesp.data.History
import com.rhshourav.peekesp.data.HistoryRange
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class ChartMathTest {
    @Test fun bucketsAimForAboutOneHundredFiftyPoints() {
        assertEquals(24_000L, ChartMath.bucketMs(HistoryRange.H1))
        assertEquals(4_032_000L, ChartMath.bucketMs(HistoryRange.D7))
    }

    @Test fun aGapIsAtLeastTwentyMinutesSoBackgroundReadingsJoin() {
        assertEquals(20 * 60_000L, ChartMath.gapMs(24_000L))
        assertEquals(3 * 4_032_000L, ChartMath.gapMs(4_032_000L))
    }

    @Test fun aSilenceBreaksTheLine() {
        val pts = listOf(0L to 1.0, 10L to 2.0, 1_000L to 3.0, 1_010L to 4.0)
        val segs = ChartMath.segments(pts, gapMs = 100)
        assertEquals(2, segs.size)
        assertEquals(listOf(0L, 10L), segs[0].map { it.first })
        assertEquals(listOf(1_000L, 1_010L), segs[1].map { it.first })
    }

    @Test fun noSensorBreaksTheLineToo() {
        val pts = listOf<Pair<Long, Double?>>(0L to 1.0, 10L to null, 20L to 3.0)
        val segs = ChartMath.segments(pts, gapMs = 100)
        assertEquals(2, segs.size)
        assertEquals(1, segs[0].size)   // a lone point: the chart draws a dot
    }

    @Test fun nothingLoggedMeansNoSegments() {
        assertTrue(ChartMath.segments(emptyList(), 100).isEmpty())
    }

    @Test fun valueAtTakesTheNearestPointOnlyIfItIsCloseEnough() {
        val pts = listOf<Pair<Long, Double?>>(0L to 10.0, 100L to 20.0)
        assertEquals(20.0, ChartMath.valueAt(pts, 90L, maxDistMs = 50)!!, 0.0)
        assertNull(ChartMath.valueAt(pts, 500L, maxDistMs = 50))
    }

    @Test fun valueAtSkipsMissingReadings() {
        val pts = listOf<Pair<Long, Double?>>(0L to null, 100L to 20.0)
        assertEquals(20.0, ChartMath.valueAt(pts, 5L, maxDistMs = 200)!!, 0.0)
    }

    @Test fun axesRoundUpToNumbersAPersonWouldPick() {
        assertEquals(2000.0, ChartMath.niceCeil(1331.0), 0.0)
        assertEquals(200.0, ChartMath.niceCeil(113.0), 0.0)
        assertEquals(100.0, ChartMath.niceCeil(61.0), 0.0)
        assertEquals(10.0, ChartMath.niceCeil(10.0), 0.0)
        assertEquals(1.0, ChartMath.niceCeil(0.0), 0.0)
    }

    @Test fun temperatureAxisIsPaddedOnMultiplesOfFive() {
        assertEquals(40.0 to 65.0, ChartMath.axisFor(45.0, 61.0))
        // A flat line still gets a window to sit in.
        assertEquals(45.0 to 55.0, ChartMath.axisFor(50.0, 50.0))
    }

    @Test fun chargingRunsBecomeShadedBands() {
        val pts = listOf(0L to false, 10L to true, 20L to true, 30L to false, 40L to true)
        assertEquals(listOf(5L to 25L, 35L to 45L), ChartMath.flagBands(pts, halfMs = 5, gapMs = 100))
    }

    @Test fun aSilenceSplitsABand() {
        val pts = listOf(0L to true, 1_000L to true)
        assertEquals(listOf(-5L to 5L, 995L to 1_005L), ChartMath.flagBands(pts, halfMs = 5, gapMs = 100))
    }

    @Test fun rangesFollowRetention() {
        assertEquals(listOf(HistoryRange.H1, HistoryRange.H6, HistoryRange.D1), HistoryRange.availableFor(1))
        assertEquals(HistoryRange.D7, HistoryRange.availableFor(7).last())
        assertEquals(HistoryRange.D30, HistoryRange.availableFor(30).last())
    }

    @Test fun aReadingIsLoggedOncePerGapAndNeverTwice() {
        assertTrue(History.shouldLog(null, 1_000, 15_000))
        // The same push seen again (a quiet machine is re-reported with the same stamp).
        assertFalse(History.shouldLog(1_000, 1_000, 15_000))
        assertFalse(History.shouldLog(1_000, 15_999, 15_000))
        assertTrue(History.shouldLog(1_000, 16_000, 15_000))
        // A clock that stepped back is not a new reading.
        assertFalse(History.shouldLog(50_000, 20_000, 15_000))
    }
}