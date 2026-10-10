package com.rhshourav.peekesp

import com.rhshourav.peekesp.data.Parser
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class ParserTest {
    @Test fun devicesAreSortedByName() {
        val body = """{"host":"b","devices":[{"host":"zed","age_s":3},{"host":"alpha","age_s":9}]}"""
        assertEquals(listOf("alpha", "zed"), Parser.parse(body).map { it.host })
    }

    @Test fun flatLegacyShapeIsStillRead() {
        val m = Parser.parse("""{"host":"dietpi","cpu_percent":12.5,"age_s":4}""").single()
        assertEquals("dietpi", m.host)
        assertEquals(12.5, m.cpu!!, 0.0)
        assertEquals(4, m.ageS)
    }

    @Test fun negativeMeansNoSensorAndMissingIsUnknownNotZero() {
        val m = Parser.parse("""{"host":"h","cpu_temp_c":-1,"battery_percent":-1}""").single()
        assertNull(m.tempC)
        assertNull(m.batteryPct)
        assertNull(m.cpu)
        assertNull(m.storagePct)
    }

    @Test fun booleansNullsAndStringsAreHandled() {
        val m = Parser.parse(
            """{"host":"h","cpu_percent":true,"ram_percent":null,"storage_percent":"61.5","battery_charging":true}""",
        ).single()
        assertNull(m.cpu)
        assertNull(m.ram)
        assertEquals(61.5, m.storagePct!!, 0.0)
        assertTrue(m.charging)
        assertFalse(m.ac)
    }

    @Test fun duplicateHostsCollapse() {
        val body = """{"devices":[{"host":"a","cpu_percent":1},{"host":"a","cpu_percent":2}]}"""
        assertEquals(1, Parser.parse(body).size)
    }

    @Test fun emptyReplyHasNoMachines() {
        assertTrue(Parser.parse("{}").isEmpty())
    }
}
