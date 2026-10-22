package com.rhshourav.peekesp

import com.rhshourav.peekesp.data.AlertDefaults
import com.rhshourav.peekesp.data.AlertEngine
import com.rhshourav.peekesp.data.AlertSettings
import com.rhshourav.peekesp.data.AlertState
import com.rhshourav.peekesp.data.Anomaly
import com.rhshourav.peekesp.data.Baseline
import com.rhshourav.peekesp.data.EventKind
import com.rhshourav.peekesp.data.Machine
import com.rhshourav.peekesp.data.MachineMessage
import com.rhshourav.peekesp.data.Op
import com.rhshourav.peekesp.data.QuietHours
import com.rhshourav.peekesp.data.Rule
import com.rhshourav.peekesp.data.RuleState
import com.rhshourav.peekesp.data.Severity
import com.rhshourav.peekesp.data.Template
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.time.ZoneId
import java.time.ZonedDateTime

private class MemState : AlertState {
    val map = HashMap<String, RuleState>()
    override fun get(key: String): RuleState? = map[key]
    override fun put(key: String, s: RuleState) {
        map[key] = s
    }
}

class AlertEngineTest {
    private val min = 60_000L
    private val t0 = 1_700_000_000_000L

    private fun machine(
        host: String = "pc",
        age: Int = 0,
        cpu: Double? = null,
        rx: Double? = null,
        tx: Double? = null,
        messages: List<MachineMessage> = emptyList(),
    ) = Machine(
        host = host, ageS = age, cpu = cpu, ram = null, storagePct = null, storageTotalGb = null,
        storageFreeGb = null, tempC = null, batteryPct = null, batteryMin = null, charging = false, ac = false,
        rxKbps = rx, txKbps = tx, uptimeS = null, messages = messages,
    )

    /** Only the rules a test names, so the defaults cannot interfere. */
    private fun settings(vararg rules: Rule, tweak: (AlertSettings) -> AlertSettings = { it }) =
        tweak(AlertDefaults.settings().copy(rules = rules.toList(), offlineAfterMin = 2))

    private fun cpuRule(forMin: Int = 0, machine: String? = null, title: String = "", body: String = "") = Rule(
        "cpu", "CPU", true, "cpu_percent", Op.ABOVE, 90.0, forMin, Severity.WARNING, machine, title, body,
    )

    private fun engine(state: AlertState = MemState(), baseline: Baseline? = null) = AlertEngine(state) { _, _ -> baseline }

    // ---------------- machine status ----------------

    @Test fun firstSightOfAnOfflineMachineSaysNothing() {
        // Opening the app should not announce an old outage as news.
        val out = engine().evaluate(settings(), listOf(machine(age = 600)), t0)
        assertTrue(out.isEmpty())
    }

    @Test fun offlineThenBackIsOneAlertEachAndNeverRepeats() {
        val e = engine()
        val s = settings()
        assertTrue(e.evaluate(s, listOf(machine(age = 5)), t0).isEmpty())

        val down = e.evaluate(s, listOf(machine(age = 300)), t0 + 5 * min)
        assertEquals(1, down.size)
        assertEquals(EventKind.STATUS, down[0].kind)
        assertEquals(Severity.WARNING, down[0].severity)
        assertTrue(down[0].title.contains("went offline"))

        // Still down at the next look: nothing new to say.
        assertTrue(e.evaluate(s, listOf(machine(age = 360)), t0 + 6 * min).isEmpty())

        val up = e.evaluate(s, listOf(machine(age = 2)), t0 + 20 * min)
        assertEquals(1, up.size)
        assertEquals(Severity.INFO, up[0].severity)
        assertTrue(up[0].title.contains("back online"))
        assertTrue(up[0].body.contains("silent for"))
    }

    @Test fun aMachineIsOnlyOfflineOnceItHasBeenSilentLongEnough() {
        val e = engine()
        e.evaluate(settings(), listOf(machine(age = 5)), t0)
        // 90 s is under the 2-minute setting: a hiccup, not an outage.
        assertTrue(e.evaluate(settings(), listOf(machine(age = 90)), t0 + 90_000).isEmpty())
    }

    @Test fun mutedMachinesAreLoggedButNeverNotified() {
        val s = settings { it.copy(mutedMachines = setOf("pc")) }
        val e = engine()
        e.evaluate(s, listOf(machine(age = 5)), t0)
        val down = e.evaluate(s, listOf(machine(age = 300)), t0 + 5 * min)
        assertEquals(1, down.size)
        assertTrue(down[0].silent)
    }

    @Test fun theOfflineAndOnlineSwitchesAreIndependent() {
        val s = settings { it.copy(notifyOffline = false, notifyOnline = true) }
        val e = engine()
        e.evaluate(s, listOf(machine(age = 5)), t0)
        assertTrue(e.evaluate(s, listOf(machine(age = 300)), t0 + 5 * min)[0].silent)
        assertFalse(e.evaluate(s, listOf(machine(age = 2)), t0 + 9 * min)[0].silent)
    }

    // ---------------- rules ----------------

    @Test fun aThresholdMustHoldForTheWholeTimeBeforeItSpeaks() {
        val e = engine()
        val s = settings(cpuRule(forMin = 5))
        assertTrue(e.evaluate(s, listOf(machine(cpu = 95.0)), t0).isEmpty())
        assertTrue(e.evaluate(s, listOf(machine(cpu = 95.0)), t0 + 3 * min).isEmpty())
        val fired = e.evaluate(s, listOf(machine(cpu = 95.0)), t0 + 5 * min)
        assertEquals(1, fired.size)
        assertEquals(EventKind.ALERT, fired[0].kind)
        // And once it has spoken it does not nag.
        assertTrue(e.evaluate(s, listOf(machine(cpu = 96.0)), t0 + 9 * min).isEmpty())
    }

    @Test fun aSpikeThatClearsNeverFires() {
        val e = engine()
        val s = settings(cpuRule(forMin = 5))
        e.evaluate(s, listOf(machine(cpu = 95.0)), t0)
        e.evaluate(s, listOf(machine(cpu = 10.0)), t0 + 3 * min)       // cleared: the clock starts over
        e.evaluate(s, listOf(machine(cpu = 95.0)), t0 + 4 * min)
        assertTrue(e.evaluate(s, listOf(machine(cpu = 95.0)), t0 + 8 * min).isEmpty())   // only 4 minutes
    }

    @Test fun clearingRaisesAQuietResolvedNoteUnlessAskedToSpeak() {
        val quiet = settings(cpuRule()) { it.copy(notifyResolved = false) }
        val e = engine()
        e.evaluate(quiet, listOf(machine(cpu = 95.0)), t0)
        val cleared = e.evaluate(quiet, listOf(machine(cpu = 10.0)), t0 + min)
        assertEquals(1, cleared.size)
        assertEquals(Severity.INFO, cleared[0].severity)
        assertTrue(cleared[0].silent)

        val loud = settings(cpuRule()) { it.copy(notifyResolved = true) }
        val e2 = engine()
        e2.evaluate(loud, listOf(machine(cpu = 95.0)), t0)
        assertFalse(e2.evaluate(loud, listOf(machine(cpu = 10.0)), t0 + min)[0].silent)
    }

    @Test fun aRuleForOneMachineIgnoresTheOthers() {
        val s = settings(cpuRule(machine = "a"))
        val out = engine().evaluate(s, listOf(machine("a", cpu = 99.0), machine("b", cpu = 99.0)), t0)
        assertEquals(1, out.size)
        assertEquals("a", out[0].machine)
    }

    @Test fun anOfflineMachineIsNotJudgedByItsLastReading() {
        val e = engine()
        val s = settings(cpuRule())
        e.evaluate(s, listOf(machine(age = 5)), t0)
        // Its last reading said 99%, but that was long ago: no CPU alarm about a moment long gone.
        val out = e.evaluate(s, listOf(machine(age = 600, cpu = 99.0)), t0 + 10 * min)
        assertTrue(out.none { it.kind == EventKind.ALERT })
    }

    @Test fun yourOwnWordingReplacesTheDefault() {
        val s = settings(cpuRule(title = "{machine} is cooking at {value}", body = "Sent by {rule}"))
        val out = engine().evaluate(s, listOf(machine("kitchen-pc", cpu = 95.0)), t0)
        assertEquals("kitchen-pc is cooking at 95%", out[0].title)
        assertEquals("Sent by CPU", out[0].body)
    }

    @Test fun unusualNeedsABaselineAndFiresOnlyWhenFarAboveIt() {
        val rule = Rule("net", "Net", true, "net_total_kbps", Op.UNUSUAL, 6.0, 0, Severity.WARNING, null)
        val s = settings(rule)
        val normal = Baseline(median = 1000.0, mad = 100.0, n = 100)

        // No history: nothing can be called unusual.
        assertTrue(engine(baseline = null).evaluate(s, listOf(machine(rx = 20_000.0, tx = 0.0)), t0).isEmpty())
        // Busier than usual, but not remarkably.
        assertTrue(engine(baseline = normal).evaluate(s, listOf(machine(rx = 3_000.0, tx = 0.0)), t0).isEmpty())
        // Twenty times normal.
        val out = engine(baseline = normal).evaluate(s, listOf(machine(rx = 20_000.0, tx = 0.0)), t0)
        assertEquals(1, out.size)
        assertTrue(out[0].title.contains("unusual"))
    }

    // ---------------- messages ----------------

    @Test fun machineMessagesBecomeEventsWithAStableKey() {
        val msg = MachineMessage("m1", Severity.CRITICAL, "Intrusion", "New login", "Defender", t0 - min)
        val a = engine().evaluate(settings(), listOf(machine(messages = listOf(msg))), t0)
        val b = engine().evaluate(settings(), listOf(machine(messages = listOf(msg))), t0 + 5 * min)
        assertEquals(1, a.size)
        assertEquals(Severity.CRITICAL, a[0].severity)
        assertEquals("Defender", a[0].source)
        assertEquals(a[0].key, b[0].key)        // the store drops the second: the same message, seen again
        assertFalse(a[0].silent)
    }

    @Test fun anOldBacklogOfMessagesIsFiledNotRung() {
        val old = MachineMessage("m0", Severity.CRITICAL, "Old news", "", "x", t0 - 12 * 60 * min)
        val out = engine().evaluate(settings(), listOf(machine(messages = listOf(old))), t0)
        assertTrue(out[0].silent)
    }

    @Test fun messagesCanBeSwitchedOff() {
        val msg = MachineMessage("m1", Severity.WARNING, "Hi", "", "x", t0)
        val s = settings { it.copy(notifyMessages = false) }
        assertTrue(engine().evaluate(s, listOf(machine(messages = listOf(msg))), t0)[0].silent)
    }
}

class QuietHoursTest {
    private val utc = ZoneId.of("UTC")
    // 2026-10-19 is a Monday.
    private fun at(day: Int, h: Int, m: Int = 0) = ZonedDateTime.of(2026, 10, day, h, m, 0, 0, utc)
    private fun window(start: Int, end: Int, days: Int = QuietHours.ALL_DAYS, on: Boolean = true) =
        QuietHours(on, start, end, days, true)

    @Test fun aWindowWithinOneDayIncludesItsStartAndExcludesItsEnd() {
        val q = window(13 * 60, 15 * 60)
        assertFalse(q.contains(at(19, 12, 59)))
        assertTrue(q.contains(at(19, 13)))
        assertTrue(q.contains(at(19, 14, 59)))
        assertFalse(q.contains(at(19, 15)))
    }

    @Test fun aWindowThatCrossesMidnightCoversBothSides() {
        val q = window(23 * 60, 7 * 60)
        assertTrue(q.contains(at(19, 23, 30)))
        assertTrue(q.contains(at(20, 2)))
        assertFalse(q.contains(at(20, 7)))
        assertFalse(q.contains(at(20, 12)))
    }

    @Test fun anOvernightWindowBelongsToTheDayItStartedOn() {
        val mondayOnly = window(23 * 60, 7 * 60, days = 0b0000001)
        assertTrue(mondayOnly.contains(at(19, 23, 30)))     // Monday evening
        assertTrue(mondayOnly.contains(at(20, 2)))          // Tuesday small hours: the same night
        assertFalse(mondayOnly.contains(at(19, 2)))         // Monday small hours: Sunday's night, not set
        assertFalse(mondayOnly.contains(at(20, 23, 30)))    // Tuesday evening
    }

    @Test fun offAndZeroLengthWindowsHoldNothing() {
        assertFalse(window(23 * 60, 7 * 60, on = false).contains(at(19, 23, 30)))
        assertFalse(window(8 * 60, 8 * 60).contains(at(19, 8)))
    }
}

class AnomalyTest {
    @Test fun tooLittleHistoryMeansNoBaseline() {
        assertNull(Anomaly.baseline(List(35) { 100.0 }))
        assertNotNull(Anomaly.baseline(List(36) { 100.0 }))
    }

    @Test fun aFewSpikesDoNotMoveWhatIsNormal() {
        val values = List(40) { if (it % 14 == 0) 1000.0 else 100.0 }
        val b = Anomaly.baseline(values)!!
        assertEquals(100.0, b.median, 0.0)
    }

    @Test fun aRockSteadyMachineIsNotJumpyAboutAWobble() {
        val b = Baseline(median = 100.0, mad = 0.0, n = 40)
        assertFalse(Anomaly.isUnusual(115.0, b, 6.0, floorAbs = 10.0))
        assertTrue(Anomaly.isUnusual(300.0, b, 6.0, floorAbs = 10.0))
    }

    @Test fun belowTheFloorIsNeverUnusual() {
        val b = Baseline(median = 0.5, mad = 0.0, n = 40)
        assertFalse(Anomaly.isUnusual(5.0, b, 1.0, floorAbs = 1000.0))
    }
}

class VocabularyTest {
    @Test fun severityUnderstandsWhateverElseCallsBad() {
        assertEquals(Severity.CRITICAL, Severity.parse("RED"))
        assertEquals(Severity.CRITICAL, Severity.parse("high"))
        assertEquals(Severity.WARNING, Severity.parse(" Warn "))
        assertEquals(Severity.WARNING, Severity.parse("yellow"))
        assertEquals(Severity.INFO, Severity.parse("fine"))
        assertEquals(Severity.INFO, Severity.parse(""))
        assertEquals(Severity.INFO, Severity.parse(null))
    }

    @Test fun templatesFillInWhatTheyKnowAndLeaveTheRest() {
        assertEquals("pc is at 90%", Template.render("{machine} is at {value}", mapOf("machine" to "pc", "value" to "90%")))
        assertEquals("{oops}", Template.render("{oops}", mapOf("machine" to "pc")))
    }
}