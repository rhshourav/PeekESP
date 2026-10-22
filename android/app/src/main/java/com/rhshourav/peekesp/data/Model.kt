package com.rhshourav.peekesp.data

import org.json.JSONObject

/**
 * A sensor beyond the standard set: a GPU, an NPU, a fan, a power meter, anything. Unit and label
 * come from the reading itself, or are worked out from the key (see Metrics).
 */
data class Extra(val key: String, val label: String, val unit: String, val value: Double, val max: Double?)

/** A message a machine (or a tool running on it) wants you to see. [id] is what stops a repeat becoming a second alert. */
data class MachineMessage(
    val id: String,
    val severity: Severity,
    val title: String,
    val body: String,
    val source: String,
    val tsMs: Long?,
)

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
    val extras: List<Extra> = emptyList(),
    val messages: List<MachineMessage> = emptyList(),
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

    /** The keys the protocol already owns. Everything else that looks like a measurement is an extra sensor. */
    private val OWN_KEYS = setOf(
        "host", "age_s", "cpu_percent", "ram_percent", "storage_percent", "storage_total_gb", "storage_free_gb",
        "cpu_temp_c", "battery_percent", "battery_minutes", "battery_charging", "battery_ac",
        "net_rx_kbps", "net_tx_kbps", "uptime_seconds", "devices", "metrics", "messages",
    )
    private val KEY_SHAPE = Regex("^[a-z0-9_]{1,32}$")
    private const val MAX_EXTRAS = 24
    private const val MAX_MESSAGES = 10

    private fun text(raw: Any?, max: Int): String = when (raw) {
        null, JSONObject.NULL -> ""
        else -> raw.toString().trim().take(max)
    }

    /**
     * Extra sensors, from two places so a machine can use whichever is easier:
     *  - a "metrics" object: {"gpu_percent": 42} or {"gpu": {"value": 42, "label": "GPU", "unit": "%", "max": 100}}
     *  - plain keys in the reading itself, if they end like a measurement (gpu_percent, npu_temp_c, fan_rpm).
     * The same rules as everything else: negative means no such sensor, and junk is ignored.
     */
    private fun extras(o: JSONObject): List<Extra> {
        val out = LinkedHashMap<String, Extra>()

        o.optJSONObject("metrics")?.let { mo ->
            val keys = mo.keys()
            while (keys.hasNext() && out.size < MAX_EXTRAS) {
                val raw = keys.next()
                val key = raw.trim().lowercase()
                if (!KEY_SHAPE.matches(key) || key in OWN_KEYS) continue
                val v = mo.opt(raw)
                val entry = v as? JSONObject
                val value = sensor(if (entry != null) entry.opt("value") else v) ?: continue
                val label = entry?.let { text(it.opt("label"), 24) }?.ifEmpty { null } ?: Metrics.label(key)
                val unit = if (entry != null && entry.has("unit")) text(entry.opt("unit"), 8) else (Metrics.unitFor(key) ?: "")
                out[key] = Extra(key, label, unit, value, entry?.let { sensor(it.opt("max")) })
            }
        }

        val keys = o.keys()
        while (keys.hasNext() && out.size < MAX_EXTRAS) {
            val raw = keys.next()
            val key = raw.trim().lowercase()
            if (key in OWN_KEYS || key in out || !KEY_SHAPE.matches(key)) continue
            val unit = Metrics.unitFor(key) ?: continue          // no recognisable ending: not ours to guess at
            val value = sensor(o.opt(raw)) ?: continue
            out[key] = Extra(key, Metrics.label(key), unit, value, null)
        }
        return out.values.toList()
    }

    /** Messages a machine wants shown. Capped and trimmed: a runaway sender must not flood a phone. */
    private fun messages(o: JSONObject): List<MachineMessage> {
        val arr = o.optJSONArray("messages") ?: return emptyList()
        val out = ArrayList<MachineMessage>()
        for (i in 0 until minOf(arr.length(), MAX_MESSAGES)) {
            val mo = arr.optJSONObject(i) ?: continue
            val title = text(mo.opt("title"), 80)
            val body = text(mo.opt("body") ?: mo.opt("message"), 400)
            if (title.isEmpty() && body.isEmpty()) continue
            // Seconds or milliseconds, whichever the sender uses.
            val ts = num(mo.opt("ts"))?.let { if (it > 1e11) it.toLong() else (it * 1000).toLong() }
            // No id: make one from the content, so the same message is still the same message next time.
            val id = text(mo.opt("id"), 64).ifEmpty { Integer.toHexString("$title|$body|${ts ?: 0}".hashCode()) }
            out += MachineMessage(
                id = id,
                severity = Severity.parse(mo.opt("severity")?.toString()),
                title = title.ifEmpty { body.take(60) },
                body = if (title.isEmpty()) "" else body,
                source = text(mo.opt("source"), 40).ifEmpty { "machine" },
                tsMs = ts,
            )
        }
        return out
    }

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
            extras = extras(o),
            messages = messages(o),
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