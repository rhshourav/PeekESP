package com.rhshourav.peekesp

import androidx.compose.foundation.Canvas
import androidx.compose.foundation.gestures.awaitEachGesture
import androidx.compose.foundation.gestures.awaitFirstDown
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.StrokeJoin
import androidx.compose.ui.graphics.drawscope.DrawScope
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.graphics.drawscope.clipRect
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.unit.dp
import com.rhshourav.peekesp.data.ChartMath
import com.rhshourav.peekesp.widget.Palette

/** One line on a chart. [tag] is the short mark used when values are read out ("↓", "↑"). */
internal data class ChartSeries(
    val name: String,
    val tag: String,
    val color: Color,
    val points: List<Pair<Long, Double?>>,
)

/** Why a stretch of a chart is shaded. Each has its own look, so colour is never the only clue. */
internal enum class BandKind {
    /** The machine was charging: a soft green wash. */
    CHARGING,

    /** The relay heard nothing from the machine: a solid red wash. */
    MACHINE_OFFLINE,

    /** The phone was online but the relay did not answer: amber hatching. */
    RELAY_DOWN,

    /** The phone had no internet, so nothing was fetched and the machine is unknown: grey hatching. */
    PHONE_OFFLINE,
}

internal data class Band(val kind: BandKind, val start: Long, val end: Long)

/** Diagonal lines inside a rectangle: the "we could not see" texture. */
internal fun DrawScope.hatch(left: Float, right: Float, height: Float, color: Color, spacing: Float, stroke: Float) {
    clipRect(left, 0f, right, height) {
        var x = left - height
        while (x < right) {
            drawLine(color, Offset(x, height), Offset(x + height, 0f), strokeWidth = stroke)
            x += spacing
        }
    }
}

/**
 * A line chart drawn straight onto a Canvas: no chart library, so nothing to
 * keep up to date and nothing that can disagree with the app's palette.
 *
 *  - The x axis is the whole window [fromMs, toMs], so a history that only
 *    started ten minutes ago looks like it did, instead of stretching to fill.
 *  - Lines break where the machine was silent ([gapMs]), and wherever a [Band] says the phone or
 *    the machine was out, rather than joining the last reading before an outage to the first after.
 *  - Outages are shaded by cause, so "my phone had no signal" never looks like "the machine died".
 *  - Touch and drag reads the chart: [onCursor] is told the time under the
 *    finger, and null when it lifts. The vertical scroll behind it still works.
 */
@Composable
internal fun LineChart(
    series: List<ChartSeries>,
    fromMs: Long,
    toMs: Long,
    yMin: Double,
    yMax: Double,
    gapMs: Long,
    bands: List<Band>,
    cursorT: Long?,
    onCursor: (Long?) -> Unit,
    topLabel: String,
    bottomLabel: String,
    description: String,
    modifier: Modifier = Modifier,
) {
    val grid = Color(Palette.TRACK)
    val dim = Color(Palette.DIM)
    val charging = Color(Palette.GREEN).copy(alpha = 0.14f)
    val machineOff = Color(Palette.RED).copy(alpha = 0.12f)
    val relayDown = Color(Palette.AMBER)
    val phoneOff = Color(Palette.DIM)
    // Outages cut the lines, so a gap in a line always has a shaded reason beside it.
    val cuts = bands.filter { it.kind != BandKind.CHARGING }.map { it.start to it.end }
    val span = (toMs - fromMs).coerceAtLeast(1L)
    val ySpan = (yMax - yMin).let { if (it <= 0.0) 1.0 else it }

    Box(
        modifier = modifier
            .fillMaxWidth()
            .height(132.dp)
            .semantics { contentDescription = description }
            .pointerInput(fromMs, toMs) {
                awaitEachGesture {
                    val down = awaitFirstDown(requireUnconsumed = false)
                    fun tAt(x: Float): Long =
                        fromMs + ((x / size.width.coerceAtLeast(1)) * span).toLong().coerceIn(0L, span)
                    onCursor(tAt(down.position.x))
                    do {
                        val event = awaitPointerEvent()
                        event.changes.firstOrNull()?.let { if (it.pressed) onCursor(tAt(it.position.x)) }
                    } while (event.changes.any { it.pressed })
                    onCursor(null)
                }
            },
    ) {
        Canvas(Modifier.fillMaxSize()) {
            val w = size.width
            val h = size.height
            fun x(t: Long): Float = ((t - fromMs).toFloat() / span) * w
            fun y(v: Double): Float = h - (((v - yMin) / ySpan).coerceIn(0.0, 1.0).toFloat() * h)

            // Grid: top, middle, bottom.
            for (i in 0..2) {
                val gy = h * i / 2f
                drawLine(grid, Offset(0f, gy), Offset(w, gy), strokeWidth = 1.dp.toPx())
            }

            // Shaded spans: what each one is decides how it is drawn.
            val spacing = 7.dp.toPx()
            bands.forEach { band ->
                val l = x(band.start.coerceIn(fromMs, toMs))
                val r = x(band.end.coerceIn(fromMs, toMs))
                if (r <= l) return@forEach
                when (band.kind) {
                    BandKind.CHARGING -> drawRect(charging, Offset(l, 0f), Size(r - l, h))
                    BandKind.MACHINE_OFFLINE -> {
                        drawRect(machineOff, Offset(l, 0f), Size(r - l, h))
                        drawLine(Color(Palette.RED), Offset(l, 0f), Offset(r, 0f), strokeWidth = 2.dp.toPx())
                    }
                    BandKind.RELAY_DOWN -> {
                        drawRect(relayDown.copy(alpha = 0.08f), Offset(l, 0f), Size(r - l, h))
                        hatch(l, r, h, relayDown.copy(alpha = 0.55f), spacing, 1.dp.toPx())
                    }
                    BandKind.PHONE_OFFLINE -> {
                        drawRect(phoneOff.copy(alpha = 0.10f), Offset(l, 0f), Size(r - l, h))
                        hatch(l, r, h, phoneOff.copy(alpha = 0.55f), spacing, 1.dp.toPx())
                    }
                }
            }

            val fill = series.size == 1
            series.forEach { s ->
                ChartMath.segments(s.points, gapMs, cuts).forEach { seg ->
                    if (seg.size == 1) {
                        // A lone reading between two silences: a dot, since a line needs two ends.
                        drawCircle(s.color, 2.5.dp.toPx(), Offset(x(seg[0].first), y(seg[0].second)))
                    } else {
                        val line = Path().apply {
                            moveTo(x(seg[0].first), y(seg[0].second))
                            for (i in 1 until seg.size) lineTo(x(seg[i].first), y(seg[i].second))
                        }
                        if (fill) {
                            val area = Path().apply {
                                moveTo(x(seg[0].first), h)
                                for (p in seg) lineTo(x(p.first), y(p.second))
                                lineTo(x(seg.last().first), h)
                                close()
                            }
                            drawPath(
                                area,
                                Brush.verticalGradient(
                                    listOf(s.color.copy(alpha = 0.28f), s.color.copy(alpha = 0f)),
                                    startY = 0f, endY = h,
                                ),
                            )
                        }
                        drawPath(
                            line, s.color,
                            style = Stroke(width = 2.dp.toPx(), cap = StrokeCap.Round, join = StrokeJoin.Round),
                        )
                    }
                }
            }

            // The reading cursor: a line and a dot on each series.
            cursorT?.let { ct ->
                val cx = x(ct)
                drawLine(dim, Offset(cx, 0f), Offset(cx, h), strokeWidth = 1.dp.toPx())
                series.forEach { s ->
                    ChartMath.valueAt(s.points, ct, gapMs)?.let { v ->
                        drawCircle(s.color, 4.dp.toPx(), Offset(cx, y(v)))
                    }
                }
            }
        }
        Text(topLabel, modifier = Modifier.align(Alignment.TopStart), style = MaterialTheme.typography.labelSmall, color = dim)
        Text(bottomLabel, modifier = Modifier.align(Alignment.BottomStart), style = MaterialTheme.typography.labelSmall, color = dim)
    }
}