package com.rhshourav.peekesp.data

import android.content.Context
import android.content.res.Configuration

/**
 * How the widget looks and what it shows. Nothing here is secret, so it lives in
 * plain SharedPreferences, apart from the pairing code.
 *
 *  - opacity: how solid the card is, 0..100 (0 = see-through, 100 = solid).
 *  - glass:   how strong the glass lighting is, 0..100: the corner lights, the
 *             sheen and the specular rim. 0 leaves a flat tinted card.
 *  - clock:   adds a clock page to the tap cycle.
 *  - battery: shows "BAT 64%+" on a machine's page and adds one battery page
 *             listing every machine.
 *  - theme:   a dark or light card, or whichever the phone is using when it is drawn.
 *  - clockZone:  the zone the clock page shows, as an IANA id ("Asia/Tokyo"). Null means "where I am":
 *                the phone's own zone, which the system keeps right when the user travels.
 *  - clockHours: 12-hour, 24-hour, or whichever the phone uses.
 *
 * The defaults reproduce the look the widget shipped with, plus both pages on.
 */
data class WidgetStyle(
    val opacity: Int = DEFAULT_OPACITY,
    val glass: Int = DEFAULT_GLASS,
    val clock: Boolean = true,
    val battery: Boolean = true,
    val theme: WidgetTheme = WidgetTheme.DARK,
    val clockZone: String? = null,
    val clockHours: HourFormat = HourFormat.SYSTEM,
) {
    companion object {
        const val DEFAULT_OPACITY = 84
        const val DEFAULT_GLASS = 100
    }
}

/**
 * The widget is a picture drawn by the app, so it cannot follow the phone's theme by
 * itself: "match system" is resolved when it is drawn, and changes at the next redraw
 * (the 15-minute refresh, a tap, or opening the app) rather than the instant the phone switches.
 */
enum class WidgetTheme(val label: String) {
    DARK("Dark"),
    LIGHT("Light"),
    SYSTEM("Match system");

    fun isDark(ctx: Context): Boolean = when (this) {
        DARK -> true
        LIGHT -> false
        SYSTEM -> (ctx.resources.configuration.uiMode and Configuration.UI_MODE_NIGHT_MASK) ==
            Configuration.UI_MODE_NIGHT_YES
    }
}

class StyleStore(context: Context) {
    private val p = context.getSharedPreferences("peek_style", Context.MODE_PRIVATE)

    fun read(): WidgetStyle = WidgetStyle(
        opacity = p.getInt("opacity", WidgetStyle.DEFAULT_OPACITY).coerceIn(0, 100),
        glass = p.getInt("glass", WidgetStyle.DEFAULT_GLASS).coerceIn(0, 100),
        clock = p.getBoolean("clock", true),
        battery = p.getBoolean("battery", true),
        theme = WidgetTheme.values().firstOrNull { it.name == p.getString("theme", null) } ?: WidgetTheme.DARK,
        // Kept only if it is a zone this phone knows; anything else means "follow the phone".
        clockZone = p.getString("clock_zone", null)?.takeIf { ClockZones.resolve(it) != null },
        clockHours = HourFormat.values().firstOrNull { it.name == p.getString("clock_hours", null) } ?: HourFormat.SYSTEM,
    )

    fun save(style: WidgetStyle) {
        val e = p.edit()
            .putInt("opacity", style.opacity.coerceIn(0, 100))
            .putInt("glass", style.glass.coerceIn(0, 100))
            .putBoolean("clock", style.clock)
            .putBoolean("battery", style.battery)
            .putString("theme", style.theme.name)
            .putString("clock_hours", style.clockHours.name)
        // Null is stored as "no entry", never as an empty string that something might mistake for a zone.
        val zone = style.clockZone?.takeIf { ClockZones.resolve(it) != null }
        if (zone == null) e.remove("clock_zone") else e.putString("clock_zone", zone)
        e.apply()
    }

    fun reset() = p.edit().clear().apply()
}