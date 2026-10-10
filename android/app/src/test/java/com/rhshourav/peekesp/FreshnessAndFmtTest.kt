package com.rhshourav.peekesp

import com.rhshourav.peekesp.data.Fmt
import com.rhshourav.peekesp.data.Freshness
import org.junit.Assert.assertEquals
import org.junit.Test

class FreshnessAndFmtTest {
    @Test fun lostConnectionAgesEveryMachine() {
        // relay said 5 s old; we fetched it 10 minutes ago and have heard nothing since
        assertEquals(605, Freshness.ageSeconds(5, fetchedAtMs = 0, nowMs = 600_000))
    }

    @Test fun aClockStepBackwardNeverMakesDataYounger() {
        assertEquals(5, Freshness.ageSeconds(5, fetchedAtMs = 10_000, nowMs = 2_000))
    }

    @Test fun offlineThresholdFloorsAtAMinuteAndFollowsSlowPolls() {
        assertEquals(60, Freshness.offlineAfterSeconds(1))
        assertEquals(60, Freshness.offlineAfterSeconds(5))
        assertEquals(150, Freshness.offlineAfterSeconds(30))
    }

    @Test fun formatsMatchTheDeviceScreen() {
        assertEquals("1.3M", Fmt.rate(1331.0))
        assertEquals("113k", Fmt.rate(113.0))
        assertEquals("436G", Fmt.capacity(436.2))
        assertEquals("2.5G", Fmt.capacity(2.5))
        assertEquals("up 3d 4h", Fmt.uptime(3 * 86400L + 4 * 3600))
        assertEquals("42", Fmt.pct(41.6))
        assertEquals("48\u00B0", Fmt.temp(48.2))
        assertEquals("--", Fmt.temp(null))
        assertEquals("as of 12m ago", Fmt.asOf(12 * 60L))
    }
}
