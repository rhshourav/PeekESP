package com.rhshourav.peekesp.data

import org.json.JSONObject

/** One machine's latest reading. Null means unknown or no such sensor - never zero. */
data class Machine(
    val host: String,
    val ageS: Int,
    val cpu: Double?,
    val ram: Double?,
    val storagePct: Double?,
    val storageTotalGb: Double?,
    val storageFreeGb: Double?,
    val tempC: Double?,
    val batteryPct: Int?,
    val batteryMin: Int?,
    val charging: Boolean,
    val ac: Boolean,
    val rxKbps: Double?,
    val txKbps: Double?,
    val uptimeS: Long?,
)

/**
 * Reads a relay reply with the rules the Pi display host uses (relay.py):
 * booleans, null, NaN and infinity are not numbers; any negative number means
 * "this machine has no such sensor"; a missing value is unknown, not zero; the
 * flat single-object shape of an older Worker is still read.
 */
object Parser {

    private fun num(raw: Any?): Double? {
        val v = when (raw) {
            null, JSONObject.NULL, is Boolean -> return null
            is Number -> raw.toDouble()
            is String -> raw.trim().toDoubleOrNull() ?: return null
            else -> return null
        }
        return if (v.isNaN() || v.isInfinite()) null else v
    }

    private fun sensor(raw: Any?): Double? = num(raw)?.takeIf { it >= 0 }

    private fun flag(raw: Any?): Boolean = raw == true

    private fun machine(o: JSONObject): Machine {
        val host = o.opt("host")
            ?.takeIf { it != JSONObject.NULL }
            ?.toString()
            ?.takeIf { it.isNotEmpty() }
            ?: "unknown"
        return Machine(
            host = host.take(32),
            ageS = num(o.opt("age_s"))?.toInt()?.coerceAtLeast(0) ?: 0,
            cpu = sensor(o.opt("cpu_percent")),
            ram = sensor(o.opt("ram_percent")),
            storagePct = sensor(o.opt("storage_percent")),
            storageTotalGb = sensor(o.opt("storage_total_gb")),
            storageFreeGb = sensor(o.opt("storage_free_gb")),
            tempC = sensor(o.opt("cpu_temp_c")),
            batteryPct = sensor(o.opt("battery_percent"))?.toInt(),
            batteryMin = sensor(o.opt("battery_minutes"))?.toInt(),
            charging = flag(o.opt("battery_charging")),
            ac = flag(o.opt("battery_ac")),
            rxKbps = sensor(o.opt("net_rx_kbps")),
            txKbps = sensor(o.opt("net_tx_kbps")),
            uptimeS = sensor(o.opt("uptime_seconds"))?.toLong(),
        )
    }

    /** Machines sorted by name, so the order never reshuffles when a push lands. */
    fun parse(body: String): List<Machine> {
        val payload = JSONObject(body)
        val rows = mutableListOf<JSONObject>()
        payload.optJSONArray("devices")?.let { arr ->
            for (i in 0 until arr.length()) arr.optJSONObject(i)?.let(rows::add)
        }
        // The freshest host is repeated at the top level for devices flashed
        // before the array existed; fall back to it rather than show nothing.
        if (rows.isEmpty() && payload.has("host")) rows += payload

        val seen = HashSet<String>()
        return rows.map(::machine)
            .filter { seen.add(it.host) }
            .sortedBy { it.host }
    }
}
