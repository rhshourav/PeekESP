package com.rhshourav.peekesp

import com.rhshourav.peekesp.data.Pairing
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class PairingTest {
    // The vector pinned in the firmware, the Windows agent and the Worker's suite.
    private val stream = "4b907ba136d0a7f2"
    private val read = "ec3cb3699bd1284efb2fcfe056609e87edf4813b84e9ce84"

    @Test fun sharedVector() {
        val k = Pairing.derive("K7M2P4QX9R")
        assertEquals(stream, k.stream)
        assertEquals(read, k.read)
    }

    @Test fun dashesAndCaseAreIgnored() {
        assertEquals(Pairing.derive("K7M2P4QX9R"), Pairing.derive("k7m2-p4qx-9r"))
    }

    @Test fun rejectsWrongLengthAndAlphabet() {
        assertFalse(Pairing.isValid("K7M2P4QX9"))      // 9 characters
        assertFalse(Pairing.isValid("K7M2P4QX90"))     // 0 is not in the alphabet
        assertFalse(Pairing.isValid("K7M2P4QXIR"))     // nor is I
        assertTrue(Pairing.isValid("K7M2-P4QX-9R"))
    }

    @Test fun formatsInGroups() {
        assertEquals("K7M2-P4QX-9R", Pairing.format("k7m2p4qx9r"))
        assertEquals("K7M2-P4", Pairing.format("K7M2P4"))
    }
}
