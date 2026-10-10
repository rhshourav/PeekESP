package com.rhshourav.peekesp.data

import java.security.MessageDigest

/**
 * Pairing code -> stream id and read token.
 *
 *   stream = SHA-256("peek-stream:" + CODE)  first 16 hex
 *   read   = SHA-256("peek-read:"   + CODE)  first 48 hex
 *
 * The sixth copy of these three lines, pinned by PairingTest against the vector
 * the firmware, the Windows agent and the Worker already share. A drift would
 * mean reading a stream nothing pushes to, with every request looking valid.
 *
 * The push token is deliberately not derived: a read-only app cannot forge
 * telemetry or queue commands.
 */
object Pairing {
    const val ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    const val CODE_LEN = 10

    data class Keys(val code: String, val stream: String, val read: String)

    /** Dashes and case are decoration: "k7m2-p4qx-9r" is "K7M2P4QX9R". */
    fun normalise(code: String?): String =
        (code ?: "").uppercase().filter { it in 'A'..'Z' || it in '0'..'9' }

    fun isValid(code: String?): Boolean {
        val c = normalise(code)
        return c.length == CODE_LEN && c.all { it in ALPHABET }
    }

    /** K7M2P4QX9R -> K7M2-P4QX-9R, as the device shows it. */
    fun format(code: String?): String {
        val c = normalise(code)
        return listOf(c.take(4), c.drop(4).take(4), c.drop(8).take(4))
            .filter { it.isNotEmpty() }
            .joinToString("-")
    }

    fun derive(code: String?): Keys {
        val c = normalise(code)
        require(isValid(c)) { "pairing code must be $CODE_LEN characters from $ALPHABET" }
        return Keys(c, hash("peek-stream:", c, 16), hash("peek-read:", c, 48))
    }

    private fun hash(prefix: String, code: String, hex: Int): String =
        MessageDigest.getInstance("SHA-256")
            .digest((prefix + code).toByteArray(Charsets.US_ASCII))
            .joinToString("") { "%02x".format(it.toInt() and 0xFF) }
            .take(hex)
}
