package com.rhshourav.peekesp.data

/**
 * What a machine's battery is doing, decided the way PeekESP.ino does it in
 * render_host_battery(), in the same order, for the same reasons:
 *
 *  1. No battery reported  -> "no battery". A desktop, or an agent that cannot read one.
 *  2. Charging             -> "charging".
 *  3. The OS gave an estimate -> "2h 10m left".
 *  4. On mains, not charging  -> "on AC". That is a FULL battery, not a flat one:
 *     calling it "charging" is how a battery readout stops being believed.
 *  5. Otherwise            -> "on battery".
 */
enum class PowerState { NONE, CHARGING, ESTIMATE, AC, DISCHARGING }

enum class BatteryLevel { OK, LOW, CRITICAL }

data class BatteryInfo(val pct: Int?, val state: PowerState, val minutes: Int?) {
    val present: Boolean get() = pct != null

    /** The words after the percentage, as the device prints them. */
    val tail: String
        get() = when (state) {
            PowerState.NONE -> "no battery"
            PowerState.CHARGING -> "charging"
            PowerState.ESTIMATE -> "${Fmt.remaining(minutes ?: 0)} left"
            PowerState.AC -> "on AC"
            PowerState.DISCHARGING -> "on battery"
        }
}

object Battery {
    /** Below this a battery is worth a glance; below CRITICAL it is worth getting up for. */
    const val LOW = 30
    const val CRITICAL = 15

    fun of(m: Machine): BatteryInfo {
        val pct = m.batteryPct?.coerceIn(0, 100) ?: return BatteryInfo(null, PowerState.NONE, null)
        // A zero or negative estimate means the OS would not give one.
        val minutes = m.batteryMin?.takeIf { it > 0 }
        val state = when {
            m.charging -> PowerState.CHARGING
            minutes != null -> PowerState.ESTIMATE
            m.ac -> PowerState.AC
            else -> PowerState.DISCHARGING
        }
        return BatteryInfo(pct, state, minutes)
    }

    /** Charging is never alarming, however low: the level is only going up. */
    fun level(info: BatteryInfo): BatteryLevel {
        val p = info.pct
        return when {
            p == null || info.state == PowerState.CHARGING -> BatteryLevel.OK
            p <= CRITICAL -> BatteryLevel.CRITICAL
            p <= LOW -> BatteryLevel.LOW
            else -> BatteryLevel.OK
        }
    }
}