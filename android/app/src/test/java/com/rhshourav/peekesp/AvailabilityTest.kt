package com.rhshourav.peekesp

import com.rhshourav.peekesp.data.Availability
import com.rhshourav.peekesp.data.ChartMath
import com.rhshourav.peekesp.data.Coverage
import com.rhshourav.peekesp.data.LinkEvent
import com.rhshourav.peekesp.data.LinkKind
import com.rhshourav.peekesp.data.Net
import com.rhshourav.peekesp.data.Span
import org.junit.Assert.assertEquals
import org.junit.Test

class AvailabilityTest {
    private val m = 60_000L   // one minute, so the numbers below read as minutes

    private fun build(
        times: List<Long> = emptyList(),
        link: List<LinkEvent> = emptyList(),
        silent: List<Pair<Long, Long>> = emptyList(),
        to: Long = 100 * m,
        joinGap: Long = 60 * m,
        tail: Long = 0,
    ) = Availability.build(0, to, times, joinGap, tail, link, silent)

    @Test fun readingsCloseTogetherAreOneOnlineRunAndTheLastOneHoldsABit() {
        val spans = build(times = listOf(0, 10 * m, 20 * m), to = 30 * m, joinGap = 15 * m, tail = 5 * m)
        assertEquals(listOf(Span(Coverage.ONLINE, 0, 25 * m)), spans)
    }

    @Test fun aBigGapBetweenReadingsIsNotJoined() {
        val spans = build(times = listOf(0, 10 * m, 100 * m, 110 * m), to = 120 * m, joinGap = 15 * m)
        assertEquals(
            listOf(Span(Coverage.ONLINE, 0, 10 * m), Span(Coverage.ONLINE, 100 * m, 110 * m)),
            spans,
        )
    }

    @Test fun aMachineSilenceOverridesOnline() {
        val spans = build(times = listOf(0, 50 * m, 100 * m), silent = listOf(40 * m to 60 * m))
        assertEquals(
            listOf(
                Span(Coverage.ONLINE, 0, 40 * m),
                Span(Coverage.MACHINE_OFFLINE, 40 * m, 60 * m),
                Span(Coverage.ONLINE, 60 * m, 100 * m),
            ),
            spans,
        )
    }

    @Test fun aDeadPhoneSignalBeatsEverythingElse() {
        // The machine was reporting and was also silent; neither can be known while the phone had no internet.
        val spans = build(
            times = listOf(0, 50 * m, 100 * m),
            silent = listOf(40 * m to 60 * m),
            link = listOf(LinkEvent(40 * m, LinkKind.PHONE_OFFLINE), LinkEvent(50 * m, LinkKind.OK)),
        )
        assertEquals(
            listOf(
                Span(Coverage.ONLINE, 0, 40 * m),
                Span(Coverage.PHONE_OFFLINE, 40 * m, 50 * m),
                Span(Coverage.MACHINE_OFFLINE, 50 * m, 60 * m),
                Span(Coverage.ONLINE, 60 * m, 100 * m),
            ),
            spans,
        )
    }

    @Test fun relayDownIsItsOwnKind() {
        val spans = build(link = listOf(LinkEvent(10 * m, LinkKind.RELAY_DOWN), LinkEvent(20 * m, LinkKind.OK)))
        assertEquals(listOf(Span(Coverage.RELAY_DOWN, 10 * m, 20 * m)), spans)
    }

    @Test fun anOutageStillGoingOnRunsToNow() {
        val spans = build(link = listOf(LinkEvent(95 * m, LinkKind.PHONE_OFFLINE)), to = 100 * m)
        assertEquals(listOf(Span(Coverage.PHONE_OFFLINE, 95 * m, 100 * m)), spans)
    }

    @Test fun anOutageIsOnlyAssumedToLastSoLongPastTheLastTimeItWasSeen() {
        val spans = build(link = listOf(LinkEvent(0, LinkKind.PHONE_OFFLINE)), to = 100 * m)
        assertEquals(listOf(Span(Coverage.PHONE_OFFLINE, 0, Availability.LINK_HOLD_MS)), spans)
    }

    @Test fun backgroundProbesChainIntoOneOutage() {
        // The widget looks every 15 minutes: four looks, three of them failures, are one stretch.
        val spans = build(
            link = listOf(
                LinkEvent(0, LinkKind.PHONE_OFFLINE), LinkEvent(15 * m, LinkKind.PHONE_OFFLINE),
                LinkEvent(30 * m, LinkKind.PHONE_OFFLINE), LinkEvent(45 * m, LinkKind.OK),
            ),
        )
        assertEquals(listOf(Span(Coverage.PHONE_OFFLINE, 0, 45 * m)), spans)
    }

    @Test fun sharesAddUpAndWhatIsNotCoveredIsNoData() {
        val spans = build(times = listOf(0, 50 * m))
        val s = Availability.shares(spans, 0, 100 * m)
        assertEquals(0.5, s.online, 1e-9)
        assertEquals(0.5, s.noData, 1e-9)
        assertEquals(1.0, s.online + s.machineOffline + s.relayDown + s.phoneOffline + s.noData, 1e-9)
    }

    @Test fun anOutageCutsALineEvenWhenTheReadingsAreCloseTogether() {
        val pts = listOf<Pair<Long, Double?>>(0L to 1.0, 10L to 2.0, 20L to 3.0, 30L to 4.0)
        val segs = ChartMath.segments(pts, gapMs = 1_000, cuts = listOf(12L to 18L))
        assertEquals(2, segs.size)
        assertEquals(listOf(0L, 10L), segs[0].map { it.first })
        assertEquals(listOf(20L, 30L), segs[1].map { it.first })
    }

    @Test fun aFailedRequestIsBlamedOnTheRightParty() {
        assertEquals(LinkKind.PHONE_OFFLINE, Net.failureKind(false))
        assertEquals(LinkKind.RELAY_DOWN, Net.failureKind(true))
    }
}