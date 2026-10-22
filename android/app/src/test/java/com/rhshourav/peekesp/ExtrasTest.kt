package com.rhshourav.peekesp

import com.rhshourav.peekesp.data.Machine
import com.rhshourav.peekesp.data.Metrics
import com.rhshourav.peekesp.data.Parser
import com.rhshourav.peekesp.data.Severity
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class ExtrasTest {
    private fun one(json: String) = Parser.parse(json).single()

    @Test fun plainKeysThatEndLikeAMeasurementBecomeExtraSensors() {
        val m = one("""{"host":"h","gpu_percent":42.5,"gpu_temp_c":61,"npu_percent":7,"fan_rpm":1200}""")
        val byKey = m.extras.associateBy { it.key }
        assertEquals("GPU", byKey.getValue("gpu_percent").label)
        assertEquals("%", byKey.getValue("gpu_percent").unit)
        assertEquals("GPU temperature", byKey.getValue("gpu_temp_c").label)
        assertEquals("\u00B0C", byKey.getValue("gpu_temp_c").unit)
        assertEquals("NPU", byKey.getValue("npu_percent").label)
        assertEquals("rpm", byKey.getValue("fan_rpm").unit)
        assertEquals(1200.0, byKey.getValue("fan_rpm").value, 0.0)
    }

    @Test fun keysWithNoRecognisableEndingAreNotGuessedAt() {
        // Relay bookkeeping must not turn into a sensor.
        val m = one("""{"host":"h","received_at":1700000000,"stream":"x","cpu_percent":5}""")
        assertTrue(m.extras.isEmpty())
    }

    @Test fun theStandardSensorsAreNotRepeatedAsExtras() {
        val m = one("""{"host":"h","cpu_percent":5,"ram_percent":6,"cpu_temp_c":50,"net_rx_kbps":9}""")
        assertTrue(m.extras.isEmpty())
    }

    @Test fun negativeMeansNoSensorForExtrasToo() {
        val m = one("""{"host":"h","gpu_percent":-1,"npu_percent":3}""")
        assertEquals(listOf("npu_percent"), m.extras.map { it.key })
    }

    @Test fun theMetricsObjectTakesPlainNumbersAndRichEntries() {
        val m = one(
            """{"host":"h","metrics":{"gpu":{"value":42,"label":"Graphics","unit":"%","max":100},"vram_gb":8.5}}""",
        )
        val gpu = m.extras.first { it.key == "gpu" }
        assertEquals("Graphics", gpu.label)
        assertEquals("%", gpu.unit)
        assertEquals(100.0, gpu.max!!, 0.0)
        val vram = m.extras.first { it.key == "vram_gb" }
        assertEquals("VRAM", vram.label)
        assertEquals("GB", vram.unit)
    }

    @Test fun junkKeysAreIgnored() {
        val m = one("""{"host":"h","metrics":{"Bad Key!":5,"ok_percent":1}}""")
        assertEquals(listOf("ok_percent"), m.extras.map { it.key })
    }

    @Test fun aRunawaySenderIsCapped() {
        val keys = (1..40).joinToString(",") { "\"s${it}_percent\":$it" }
        val m = one("""{"host":"h",$keys}""")
        assertEquals(24, m.extras.size)
    }

    // ---------------- messages ----------------

    @Test fun messagesCarrySeverityAndAreTrimmed() {
        val m = one(
            """{"host":"h","messages":[
                 {"id":"a1","severity":"red","title":"Intrusion","body":"New login","source":"Defender","ts":1700000000},
                 {"severity":"warn","message":"Disk is noisy"}]}""",
        )
        assertEquals(2, m.messages.size)
        val a = m.messages[0]
        assertEquals("a1", a.id)
        assertEquals(Severity.CRITICAL, a.severity)
        assertEquals("Defender", a.source)
        assertEquals(1_700_000_000_000L, a.tsMs)        // seconds were understood
        val b = m.messages[1]
        assertEquals(Severity.WARNING, b.severity)
        assertEquals("Disk is noisy", b.title)           // no title: the message leads
        assertEquals("machine", b.source)
    }

    @Test fun aMessageWithoutAnIdStillHasAStableOne() {
        val json = """{"host":"h","messages":[{"title":"Hello","body":"World","ts":1700000000}]}"""
        assertEquals(one(json).messages[0].id, one(json).messages[0].id)
        assertNotNull(one(json).messages[0].id.takeIf { it.isNotEmpty() })
    }

    @Test fun emptyMessagesAreDropped() {
        assertTrue(one("""{"host":"h","messages":[{"severity":"red"},{}]}""").messages.isEmpty())
    }

    @Test fun millisecondTimestampsAreUnderstoodToo() {
        val m = one("""{"host":"h","messages":[{"title":"x","ts":1700000000000}]}""")
        assertEquals(1_700_000_000_000L, m.messages[0].tsMs)
    }

    // ---------------- metrics ----------------

    private fun machine(charging: Boolean = false) = Machine(
        host = "h", ageS = 0, cpu = 10.0, ram = null, storagePct = null, storageTotalGb = null, storageFreeGb = null,
        tempC = null, batteryPct = 20, batteryMin = null, charging = charging, ac = false,
        rxKbps = 100.0, txKbps = 50.0, uptimeS = null,
    )

    @Test fun networkTotalIsDownPlusUp() {
        assertEquals(150.0, Metrics.valueOf(machine(), "net_total_kbps")!!, 0.0)
    }

    @Test fun batteryWhileUnpluggedGoesQuietOnTheCharger() {
        assertEquals(20.0, Metrics.valueOf(machine(charging = false), "battery_unplugged_percent")!!, 0.0)
        assertNull(Metrics.valueOf(machine(charging = true), "battery_unplugged_percent"))
    }

    @Test fun namesAndUnitsComeFromTheKey() {
        assertEquals("GPU memory", Metrics.label("gpu_mem_percent"))
        assertEquals("Custom Sensor", Metrics.label("custom_sensor_w"))
        assertEquals("W", Metrics.unitFor("custom_sensor_w"))
        assertEquals("rpm", Metrics.unitFor("fan_rpm"))
        assertNull(Metrics.unitFor("host"))
        assertNull(Metrics.unitFor("_c"))
    }

    @Test fun valuesAreSaidInTheirOwnUnits() {
        assertEquals("42%", Metrics.format(42.4, "%"))
        assertEquals("61\u00B0", Metrics.format(61.0, "\u00B0C"))
        assertEquals("7.5 W", Metrics.format(7.5, "W"))
        assertEquals("150 W", Metrics.format(150.0, "W"))
        assertEquals("1200 rpm", Metrics.format(1200.0, "rpm"))
    }
}