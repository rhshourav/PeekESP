package com.rhshourav.peekesp.widget

import android.content.Context
import android.util.TypedValue
import android.widget.RemoteViews
import com.rhshourav.peekesp.R
import com.rhshourav.peekesp.data.WidgetStyle

/** The live TextClock that sits over the clock page's gap. Sized from the same geometry as the bitmap. */
object ClockOverlay {
    fun build(ctx: Context, w: Int, h: Int, look: WidgetStyle): RemoteViews {
        val m = WidgetRenderer.clockMetrics(w, h)
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
        }
    }
}