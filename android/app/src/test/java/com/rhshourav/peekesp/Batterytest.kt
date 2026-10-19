package com.rhshourav.peekesp

import com.rhshourav.peekesp.data.Battery
import com.rhshourav.peekesp.data.BatteryLevel
import com.rhshourav.peekesp.data.Machine
import com.rhshourav.peekesp.data.PowerState
import com.rhshourav.peekesp.data.WidgetStyle
import com.rhshourav.peekesp.widget.PageKind
import com.rhshourav.peekesp.widget.PageRef
import com.rhshourav.peekesp.widget.Pages
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class BatteryTest {
    private fun machine(
        host: String = "h",
        pct: Int? = null,
        minutes: Int? = null,
        charging: Boolean = false,
        ac: Boolean = false,
    ) = Machine(
        host = host, ageS = 1, cpu = null, ram = null, storagePct = null, storageTotalGb = null,
        storageFreeGb = null, tempC = null, batteryPct = pct, batteryMin = minutes,
        charging = charging, ac = ac, rxKbps = null, txKbps = null, uptimeS = null,
    )

    @Test fun noBatteryIsSaidPlainly() {
        val b = Battery.of(machine(pct = null))
        assertFalse(b.present)
        assertEquals(PowerState.NONE, b.state)
        assertEquals("no battery", b.tail)
    }

    @Test fun chargingWinsOverAnEstimate() {
        val b = Battery.of(machine(pct = 64, minutes = 190, charging = true, ac = true))
        assertEquals(PowerState.CHARGING, b.state)
        assertEquals("charging", b.tail)
    }

    @Test fun anEstimateBeatsPluggedIn() {
        // The firmware checks minutes before AC, so an OS that gives one is believed.
        val b = Battery.of(machine(pct = 40, minutes = 130, ac = true))
        assertEquals("2h 10m left", b.tail)
    }

    @Test fun onMainsNotChargingIsAFullBatteryNotACharging() {
        val b = Battery.of(machine(pct = 100, ac = true))
        assertEquals(PowerState.AC, b.state)
        assertEquals("on AC", b.tail)
    }

    @Test fun otherwiseOnBattery() {
        assertEquals("on battery", Battery.of(machine(pct = 55)).tail)
    }

    @Test fun anEstimateThatIsZeroOrNegativeIsNoEstimate() {
        assertEquals("on battery", Battery.of(machine(pct = 55, minutes = 0)).tail)
        assertEquals("on battery", Battery.of(machine(pct = 55, minutes = -1)).tail)
    }

    @Test fun percentIsClamped() {
        assertEquals(100, Battery.of(machine(pct = 130)).pct)
    }

    @Test fun levelsAndTheChargingException() {
        assertEquals(BatteryLevel.OK, Battery.level(Battery.of(machine(pct = 80))))
        assertEquals(BatteryLevel.LOW, Battery.level(Battery.of(machine(pct = 30))))
        assertEquals(BatteryLevel.CRITICAL, Battery.level(Battery.of(machine(pct = 15))))
        // 5 % and plugged in and charging is not an alarm: the number is going up.
        assertEquals(BatteryLevel.OK, Battery.level(Battery.of(machine(pct = 5, charging = true))))
        assertEquals(BatteryLevel.OK, Battery.level(Battery.of(machine(pct = null))))
    }

    @Test fun oneBatteryPageCoversEveryMachine() {
        val two = listOf(machine("a"), machine("b", pct = 64))
        val pages = Pages.build(two, WidgetStyle(battery = true, clock = true))
        assertEquals(
            listOf(
                PageRef(PageKind.MACHINE, 0), PageRef(PageKind.MACHINE, 1),
                PageRef(PageKind.POWER, -1), PageRef(PageKind.CLOCK, -1),
            ),
            pages,
        )
    }

    @Test fun theBatteryPageIsThereEvenWhenNothingHasABattery() {
        val none = listOf(machine("a"), machine("b"))
        assertTrue(Pages.build(none, WidgetStyle(battery = true, clock = false)).any { it.kind == PageKind.POWER })
    }

    @Test fun switchingPartsOffRemovesTheirPages() {
        val ms = listOf(machine("a", pct = 64))
        val pages = Pages.build(ms, WidgetStyle(battery = false, clock = false))
        assertEquals(listOf(PageRef(PageKind.MACHINE, 0)), pages)
    }
}