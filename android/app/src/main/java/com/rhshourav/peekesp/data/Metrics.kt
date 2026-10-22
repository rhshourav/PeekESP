package com.rhshourav.peekesp.data

import java.util.Locale
import kotlin.math.roundToInt

data class MetricDef(val key: String, val label: String, val unit: String)

/**
 * Everything a machine can report, and how to say it.
 *
 * Machines are not limited to the sensors PeekESP knows. Any reading may carry more, and they show
 * up in the app, in the charts and in the alert rules with no update here: a key is understood from
 * its ending (gpu_percent is a percentage, npu_temp_c is a temperature, fan_rpm is a speed). See
 * PROTOCOL.md for the contract.
 */
object Metrics {
    /** What every machine reports. The keys are the relay's own. */
    val BASE = listOf(
        MetricDef("cpu_percent", "CPU", "%"),
        MetricDef("ram_percent", "Memory", "%"),
        MetricDef("cpu_temp_c", "CPU temperature", "\u00B0C"),
        MetricDef("storage_percent", "Storage used", "%"),
        MetricDef("net_total_kbps", "Network (down + up)", "kbps"),
        MetricDef("net_rx_kbps", "Download", "kbps"),
        MetricDef("net_tx_kbps", "Upload", "kbps"),
        MetricDef("battery_percent", "Battery", "%"),
        MetricDef("battery_unplugged_percent", "Battery (while unplugged)", "%"),
    )

    /** Sensors worth a proper name. Anything else is named from its key. */
    private val NAMES = mapOf(
        "gpu_percent" to "GPU",
        "gpu_temp_c" to "GPU temperature",
        "gpu_mem_percent" to "GPU memory",
        "gpu_power_w" to "GPU power",
        "npu_percent" to "NPU",
        "npu_temp_c" to "NPU temperature",
        "power_w" to "Power draw",
        "fan_rpm" to "Fan",
        "swap_percent" to "Swap",
        "cpu_freq_mhz" to "CPU clock",
        "disk_read_kbps" to "Disk read",
        "disk_write_kbps" to "Disk write",
    )

    /** How a key's ending tells you its unit. Longest endings first matters not: none contains another. */
    private val UNITS = listOf(
        "_percent" to "%", "_pct" to "%", "_c" to "\u00B0C", "_w" to "W", "_mhz" to "MHz", "_ghz" to "GHz",
        "_rpm" to "rpm", "_kbps" to "kbps", "_mbps" to "Mbps", "_mb" to "MB", "_gb" to "GB",
        "_seconds" to "s", "_ms" to "ms", "_v" to "V", "_a" to "A",
    )

    private val ACRONYMS = setOf("cpu", "gpu", "npu", "tpu", "dsp", "ram", "vram", "io", "ssd", "hdd", "nvme", "usb", "psu", "ac", "dc", "fps")

    /** The unit a key's ending implies, or null if it does not end like a measurement. */
    fun unitFor(key: String): String? =
        UNITS.firstOrNull { key.endsWith(it.first) && key.length > it.first.length }?.second

    fun label(key: String): String {
        NAMES[key]?.let { return it }
        BASE.firstOrNull { it.key == key }?.let { return it.label }
        val stem = UNITS.firstOrNull { key.endsWith(it.first) && key.length > it.first.length }
            ?.let { key.removeSuffix(it.first) } ?: key
        return stem.split('_').filter { it.isNotEmpty() }.joinToString(" ") { w ->
            if (w in ACRONYMS) w.uppercase() else w.replaceFirstChar { it.uppercase() }
        }.ifEmpty { key }
    }

    fun def(key: String): MetricDef =
        BASE.firstOrNull { it.key == key } ?: MetricDef(key, label(key), unitFor(key) ?: "")

    /** The value of any metric on a machine, or null if it does not have that sensor. */
    fun valueOf(m: Machine, key: String): Double? = when (key) {
        "cpu_percent" -> m.cpu
        "ram_percent" -> m.ram
        "cpu_temp_c" -> m.tempC
        "storage_percent" -> m.storagePct
        "net_rx_kbps" -> m.rxKbps
        "net_tx_kbps" -> m.txKbps
        "net_total_kbps" -> if (m.rxKbps != null && m.txKbps != null) m.rxKbps + m.txKbps else m.rxKbps ?: m.txKbps
        "battery_percent" -> m.batteryPct?.toDouble()
        "battery_unplugged_percent" -> if (m.charging) null else m.batteryPct?.toDouble()
        else -> m.extras.firstOrNull { it.key == key }?.value
    }

    /**
     * How small a reading can be and still be "a number worth comparing": a machine idling at 2%
     * is not "unusual" at 8%, and a trickle of network is not an attack.
     */
    fun floorFor(key: String): Double = when (def(key).unit) {
        "%" -> 10.0
        "kbps" -> 1000.0
        else -> 0.0
    }

    /** SQL for the history table's own columns, or null for an extra sensor (those live in their own table). */
    fun sqlColumn(key: String): String? = when (key) {
        "cpu_percent" -> "cpu"
        "ram_percent" -> "ram"
        "cpu_temp_c" -> "temp"
        "storage_percent" -> "storage"
        "net_rx_kbps" -> "rx"
        "net_tx_kbps" -> "tx"
        "net_total_kbps" -> "(rx + tx)"
        "battery_percent" -> "bat"
        "battery_unplugged_percent" -> "(CASE WHEN charging = 0 THEN bat END)"
        else -> null
    }

    fun format(value: Double, unit: String): String = when (unit) {
        "%" -> "${value.roundToInt()}%"
        "\u00B0C" -> "${value.roundToInt()}\u00B0"
        "kbps" -> Fmt.rate(value)
        "Mbps" -> String.format(Locale.US, "%.1f Mbps", value)
        "W" -> String.format(Locale.US, if (value < 10) "%.1f W" else "%.0f W", value)
        "GHz" -> String.format(Locale.US, "%.2f GHz", value)
        "V" -> String.format(Locale.US, "%.2f V", value)
        "A" -> String.format(Locale.US, "%.2f A", value)
        "s" -> Fmt.ago(value.toLong())
        "" -> plain(value)
        else -> "${plain(value)} $unit"
    }

    private fun plain(v: Double): String =
        if (v == v.roundToInt().toDouble()) v.roundToInt().toString() else String.format(Locale.US, "%.1f", v)
}