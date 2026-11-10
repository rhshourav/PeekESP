package com.rhshourav.peekesp.widget

import android.content.Context
import android.text.format.DateFormat
import android.util.TypedValue
import android.widget.RemoteViews
import com.rhshourav.peekesp.R
import com.rhshourav.peekesp.data.ClockZones
import com.rhshourav.peekesp.data.HourFormat
import com.rhshourav.peekesp.data.WidgetStyle

/** The live TextClock that sits over the clock page's gap. Sized from the same geometry as the bitmap. */
object ClockOverlay {
    fun build(ctx: Context, w: Int, h: Int, look: WidgetStyle): RemoteViews {
        val m = WidgetRenderer.clockMetrics(w, h)
        // Only a zone this phone knows ever reaches the view; anything else means "follow the phone".
        val zone = ClockZones.resolve(look.clockZone)
        val is24 = look.clockHours.is24(DateFormat.is24HourFormat(ctx))
        return RemoteViews(ctx.packageName, R.layout.widget_clock).apply {
            setTextViewTextSize(R.id.clock_time, TypedValue.COMPLEX_UNIT_PX, m.timePx)
            setTextViewTextSize(R.id.clock_ampm, TypedValue.COMPLEX_UNIT_PX, m.ampmPx)
            setViewPadding(R.id.clock_row, 0, m.rowTopPx, 0, 0)
            setViewPadding(R.id.clock_ampm, m.ampmGapPx, m.ampmTopPx, 0, 0)
            // The layout's colours are for a dark card; set the real ones for the mode being drawn.
            Palette.forMode(look.theme.isDark(ctx)) {
                setTextColor(R.id.clock_time, Palette.TEXT)
                setTextColor(R.id.clock_ampm, Palette.DIM)
            }
            // The region. Left alone when the user has not picked one: a TextClock with no zone follows the
            // phone by itself, including when the phone changes zone while the widget is on screen.
            if (zone != null) {
                setString(R.id.clock_time, "setTimeZone", zone.id)
                setString(R.id.clock_ampm, "setTimeZone", zone.id)
            }
            // The hour format. A TextClock picks its 12 or 24-hour pattern from the phone's setting, so to
            // force one the same pattern goes into both slots and the phone's setting is never consulted.
            if (look.clockHours != HourFormat.SYSTEM) {
                val time = if (is24) "HH:mm" else "h:mm"
                val mark = if (is24) "" else "a"
                setCharSequence(R.id.clock_time, "setFormat12Hour", time)
                setCharSequence(R.id.clock_time, "setFormat24Hour", time)
                setCharSequence(R.id.clock_ampm, "setFormat12Hour", mark)
                setCharSequence(R.id.clock_ampm, "setFormat24Hour", mark)
            }
        }
    }
}
