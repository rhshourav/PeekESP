package com.rhshourav.peekesp.widget

import android.content.Context
import android.graphics.Bitmap
import android.graphics.Canvas
import android.graphics.LinearGradient
import android.graphics.Paint
import android.graphics.Path
import android.graphics.RadialGradient
import android.graphics.RectF
import android.graphics.Shader
import android.graphics.Typeface
import android.text.Layout
import android.text.StaticLayout
import android.text.TextPaint
import android.text.format.DateFormat
import com.rhshourav.peekesp.data.Battery
import com.rhshourav.peekesp.data.BatteryInfo
import com.rhshourav.peekesp.data.BatteryLevel
import com.rhshourav.peekesp.data.ClockFmt
import com.rhshourav.peekesp.data.Fmt
import com.rhshourav.peekesp.data.Freshness
import com.rhshourav.peekesp.data.Machine
import com.rhshourav.peekesp.data.PowerState
import com.rhshourav.peekesp.data.WidgetStyle
import java.time.ZoneId
import java.util.Locale
import kotlin.math.max
import kotlin.math.min
import kotlin.math.roundToInt

/**
 * Draws the device's pages (PeekESP.ino, layout_metrics.h, clock_faces.h) onto a glass card:
 * a machine, a machine's battery, and the clock.
 *
 * Every position below is a coordinate on the T-Display's 240x135 screen and is
 * scaled to whatever size the widget is, the same way layout_metrics.h does it:
 * x by width, y by height, anything round or square by whichever axis has less
 * room. At 16:9 it lands where the device puts it; at other shapes it is
 * proportional, not tuned.
 *
 * Why a bitmap: Glance renders through RemoteViews, which cannot draw arcs. The
 * widget shows this bitmap as one image and describes it for TalkBack.
 *
 * Why the clock's digits are not in it on the home screen: a bitmap is only redrawn
 * every 15 minutes, so a time drawn into it would be wrong most of the time. The
 * widget lays a live TextClock over the gap instead ([clockMetrics] says where).
 * The in-app preview has no such overlay, so it draws the time itself.
 *
 * Why "glass-like", not glass: RemoteViews cannot sample or blur the wallpaper.
 * Tint, light from two corners, a sheen and a specular rim are as far as a
 * widget can go. The tint is strong enough that text holds contrast over most
 * wallpapers; raise "Background opacity" in the app if you use very bright ones.
 *
 * Both knobs come from WidgetStyle (set in the app): opacity scales the tint and
 * the inner panel, glass scales every lighting layer.
 */
object WidgetRenderer {
    /** The tint is lighter at the top than the bottom, by this ratio (0xBC / 0xD6, as shipped). */
    private const val TOP_RATIO = 0xBC / 0xD6.toFloat()

    /** The opacity at which the inner panel has its original alpha (0xC8). */
    private const val PANEL_REF_OPACITY = 84f

    /** More machines than this and the battery page shows one fewer, then "+N more". */
    private const val MAX_BATTERY_ROWS = 5

    private class Geo(val ox: Float, val oy: Float, w: Float, h: Float) {
        val sx = w / 240f
        val sy = h / 135f
        val sm = min(sx, sy)
        fun x(v: Float) = ox + v * sx
        fun y(v: Float) = oy + v * sy
        fun m(v: Float) = v * sm
    }

    private fun geo(w: Float, h: Float): Geo {
        val pad = h * 0.06f
        return Geo(pad, pad, w - 2 * pad, h - 2 * pad)
    }

    private fun Int.alpha(a: Int) = (this and 0x00FFFFFF) or (a shl 24)

    /** Multiplies the colour's own alpha by k (0 = invisible). */
    private fun Int.scaleAlpha(k: Float) = alpha(((this ushr 24) * k).roundToInt().coerceIn(0, 255))

    /** Where and how big the live clock goes, in pixels of the widget as the launcher shows it. */
    class ClockMetrics(
        val timePx: Float,
        val ampmPx: Float,
        val rowTopPx: Int,
        val ampmGapPx: Int,
        val ampmTopPx: Int,
    )

    fun clockMetrics(w: Int, h: Int): ClockMetrics {
        val g = geo(max(1, w).toFloat(), max(1, h).toFloat())
        val timePx = g.m(36f)
        return ClockMetrics(
            timePx = timePx,
            ampmPx = g.m(12f),
            // The row is centred on y = 53, as on the device; a line is about 1.17 em tall.
            rowTopPx = (g.y(53f) - timePx * 0.585f).roundToInt().coerceAtLeast(0),
            ampmGapPx = g.m(6f).roundToInt(),
            ampmTopPx = (timePx * 0.08f).roundToInt(),
        )
    }

    /**
     * Draws one frame in the mode the widget is set to (dark, light, or whatever the phone is
     * using). The mode is forced for this thread only, so the app's own theme is untouched.
     */
    fun render(ctx: Context, w: Int, h: Int, f: Frame, look: WidgetStyle = WidgetStyle()): Bitmap =
        Palette.forMode(look.theme.isDark(ctx)) { draw(ctx, w, h, f, look) }

    private fun draw(ctx: Context, w: Int, h: Int, f: Frame, look: WidgetStyle): Bitmap {
        val bmp = Bitmap.createBitmap(max(1, w), max(1, h), Bitmap.Config.ARGB_8888)
        val c = Canvas(bmp)
        val wf = bmp.width.toFloat()
        val hf = bmp.height.toFloat()
        drawGlass(c, wf, hf, f, look)

        val g = geo(wf, hf)
        val reg = Fonts.get(ctx, 500)
        val bold = Fonts.get(ctx, 700)
        val m = f.machine

        // ---- header: PEEK  // host        1/2  42 ms  (dot) ----
        text(c, "PEEK", g.x(8f), g.y(3f), ink(bold, g.m(12f), Palette.CYAN))
        val rule = Paint(Paint.ANTI_ALIAS_FLAG).apply {
            color = Palette.CYAN.alpha(0x4D); strokeWidth = max(1f, g.m(1f))
        }
        c.drawLine(g.x(8f), g.y(19f), g.x(232f), g.y(19f), rule)
        // The battery page has no single machine but does have a link, so it keeps the coloured dot.
        dot(c, g, f.link.color.takeIf { m != null || f.kind == PageKind.POWER } ?: Palette.DIM)

        if (f.kind == PageKind.CLOCK) {
            clockPage(ctx, c, g, f, reg, bold)
            return bmp
        }
        if (f.kind == PageKind.POWER) {
            batteryPage(c, g, f, reg, bold)
            return bmp
        }
        if (m == null) {
            message(c, g, f, reg)
            return bmp
        }

        val host = ink(reg, g.m(12f), if (f.offline) Palette.AMBER else Palette.DIM)
        text(c, fit("// ${m.host}", host, 92f * g.sx), g.x(44f), g.y(3f), host)
        if (f.count > 1) {
            textRight(c, "${f.index + 1}/${f.count}", g.x(162f), g.y(3f), ink(reg, g.m(12f), Palette.CYAN))
        }
        textRight(c, Fmt.latency(f.latencyMs), g.x(206f), g.y(3f), ink(reg, g.m(12f), Palette.DIM))

        // Offline numbers go dim: "this is the last thing we heard", not "this is now".
        val value = if (f.offline) Palette.DIM else Palette.TEXT

        // ---- gauges ----
        gauge(c, g, g.x(6f), g.y(23f), m.cpu, Palette.CYAN, value, "CPU %", bold, reg)
        gauge(c, g, g.x(78f), g.y(23f), m.ram, Palette.MAGENTA, value, "RAM %", bold, reg)

        // ---- temperature / throughput panel ----
        val pl = g.x(150f)
        val pt = g.y(23f)
        val pw = 84f * g.sx
        val ph = g.m(66f)
        val box = RectF(pl, pt, pl + pw, pt + ph)
        val radius = g.m(6f)
        val panelAlpha = (0xC8 * look.opacity / PANEL_REF_OPACITY).roundToInt().coerceIn(0, 255)
        c.drawRoundRect(box, radius, radius, Paint(Paint.ANTI_ALIAS_FLAG).apply { color = Palette.PANEL.alpha(panelAlpha) })
        c.drawRoundRect(
            box, radius, radius,
            Paint(Paint.ANTI_ALIAS_FLAG).apply {
                style = Paint.Style.STROKE; strokeWidth = max(1f, g.m(1f)); color = Palette.TRACK
            },
        )
        val tx = pl + g.m(5f)
        val ty = pt + g.m(5f)
        text(c, "TEMP", tx, ty, ink(reg, g.m(12f), Palette.DIM))
        text(c, Fmt.temp(m.tempC), tx, ty + g.m(9f), ink(bold, g.m(20f), value))
        text(c, "RX ${Fmt.rate(m.rxKbps)}", tx, ty + g.m(33f), ink(reg, g.m(11f), Palette.DIM))

        // The device swaps TX for the battery when the machine has one: "BAT 64%+".
        val showBat = look.battery && m.batteryPct != null
        val lastLine = if (showBat) Fmt.batteryLine(m.batteryPct, m.charging) else "TX ${Fmt.rate(m.txKbps)}"
        val lastInk = if (showBat && !m.charging && (m.batteryPct ?: 100) <= 15) Palette.RED else Palette.DIM
        text(c, lastLine, tx, ty + g.m(45f), ink(reg, g.m(11f), lastInk))

        // ---- storage ----
        val cap = if (m.storageTotalGb != null && m.storageTotalGb > 0 && m.storageFreeGb != null) {
            "STORAGE ${Fmt.capacity(m.storageFreeGb)} FREE"
        } else {
            "STORAGE"
        }
        val capPaint = ink(reg, g.m(12f), Palette.DIM)
        text(c, fit(cap, capPaint, 150f * g.sx), g.x(8f), g.y(92f), capPaint)
        textRight(c, "${Fmt.pct(m.storagePct)}%", g.x(232f), g.y(92f), ink(reg, g.m(12f), value))

        val bl = g.x(7f)
        val bt = g.y(108f)
        val bw = 226f * g.sx
        val bh = 8f * g.sy
        val track = RectF(bl, bt, bl + bw, bt + bh)
        c.drawRoundRect(track, bh / 2, bh / 2, Paint(Paint.ANTI_ALIAS_FLAG).apply { color = Palette.TRACK })
        val frac = ((m.storagePct ?: 0.0) / 100.0).coerceIn(0.0, 1.0).toFloat()
        if (frac > 0f) {
            val fill = RectF(bl, bt, bl + max(bh, bw * frac), bt + bh)
            c.drawRoundRect(
                fill, bh / 2, bh / 2,
                Paint(Paint.ANTI_ALIAS_FLAG).apply { color = Palette.load(Palette.CYAN, m.storagePct) },
            )
        }

        val left = if (f.offline) "offline ${Fmt.ago(m.ageS + f.sinceFetchS)}" else Fmt.uptime(m.uptimeS)
        footer(c, g, f, left, m.ageS.toLong(), reg)
        return bmp
    }

    // ------------------------------------------------------------------------
    //  Pages
    // ------------------------------------------------------------------------

    /**
     * Every connected machine's battery on one page. Machines that have one come
     * first, then the rest, each group in the machine list's own (alphabetical)
     * order so rows do not reshuffle when a reading lands. Rows get taller when
     * there are few of them, and past [MAX_BATTERY_ROWS] the tail becomes "+N more".
     */
    private fun batteryPage(c: Canvas, g: Geo, f: Frame, reg: Typeface, bold: Typeface) {
        text(c, "// BATTERY", g.x(44f), g.y(3f), ink(reg, g.m(12f), Palette.DIM))
        if (f.count > 1) {
            textRight(c, "${f.index + 1}/${f.count}", g.x(206f), g.y(3f), ink(reg, g.m(12f), Palette.CYAN))
        }

        val ordered = f.machines.sortedBy { !Battery.of(it).present }   // stable: false sorts first
        val shown = if (ordered.size > MAX_BATTERY_ROWS) ordered.take(MAX_BATTERY_ROWS - 1) else ordered
        val hidden = ordered.size - shown.size
        val slots = shown.size + if (hidden > 0) 1 else 0
        val top = 25f
        val rowH = if (slots == 0) 0f else min(30f, 89f / slots)

        val divider = Paint(Paint.ANTI_ALIAS_FLAG).apply {
            color = Palette.TRACK; strokeWidth = max(1f, g.m(1f))
        }
        shown.forEachIndexed { i, m ->
            batteryRow(c, g, m, top + i * rowH, rowH, reg, bold)
            if (i < slots - 1) c.drawLine(g.x(8f), g.y(top + (i + 1) * rowH), g.x(232f), g.y(top + (i + 1) * rowH), divider)
        }
        if (hidden > 0) {
            val mp = ink(reg, g.m(11f), Palette.DIM)
            text(c, "+$hidden more", g.x(8f), g.y(top + shown.size * rowH + (rowH - 11f) / 2f), mp)
        }

        val reporting = ordered.count { Battery.of(it).present }
        val left = when {
            ordered.isEmpty() -> ""
            reporting == 0 -> "none have a battery"
            else -> "$reporting of ${ordered.size} have a battery"
        }
        footer(c, g, f, left, 0L, reg)
    }

    private fun levelInk(info: BatteryInfo, offline: Boolean): Int = when {
        !info.present || offline -> Palette.DIM
        info.state == PowerState.CHARGING -> Palette.GREEN
        else -> when (Battery.level(info)) {
            BatteryLevel.CRITICAL -> Palette.RED
            BatteryLevel.LOW -> Palette.AMBER
            BatteryLevel.OK -> Palette.GREEN
        }
    }

    /** A cell, then a name and what it is doing, then the level. Two lines when the row is tall enough. */
    private fun batteryRow(c: Canvas, g: Geo, m: Machine, top: Float, h: Float, reg: Typeface, bold: Typeface) {
        val info = Battery.of(m)
        // Offline is the machine's own silence when we fetched, as everywhere else.
        val offline = m.ageS >= Freshness.offlineAfterSeconds(Freshness.DEFAULT_POLL_S)
        val tone = levelInk(info, offline)
        val two = h >= 24f

        val cw = if (two) 38f else 30f
        val ch = if (two) 18f else 12f
        cell(c, g, 8f, top + (h - ch) / 2f, cw, ch, info, tone)

        val pctText = if (info.present) "${info.pct}%" else "--"
        val pctPaint = ink(bold, g.m(if (two) 16f else 12f), if (info.present && !offline) Palette.TEXT else Palette.DIM)
        val pctW = pctPaint.measureText(pctText)

        val hostPaint = ink(reg, g.m(if (two) 12f else 11f), if (offline) Palette.AMBER else Palette.TEXT)
        val tailInk = when {
            !info.present || offline -> Palette.DIM
            info.state == PowerState.CHARGING -> Palette.AMBER
            Battery.level(info) != BatteryLevel.OK -> tone
            else -> Palette.DIM
        }
        val tailPaint = ink(reg, g.m(if (two) 11f else 10f), tailInk)

        if (two) {
            val textX = g.x(54f)
            val room = g.x(232f) - pctW - g.m(8f) - textX
            val line1 = top + (h - 25f) / 2f
            text(c, fit(m.host, hostPaint, room), textX, g.y(line1), hostPaint)
            text(c, fit(info.tail, tailPaint, room), textX, g.y(line1 + 14f), tailPaint)
            textRight(c, pctText, g.x(232f), g.y(top + (h - 16f) / 2f - 1f), pctPaint)
        } else {
            val textTop = top + (h - 12f) / 2f
            text(c, fit(m.host, hostPaint, 74f * g.sx), g.x(44f), g.y(textTop), hostPaint)
            val tailX = g.x(124f)
            text(c, fit(info.tail, tailPaint, g.x(232f) - pctW - g.m(6f) - tailX), tailX, g.y(textTop + 1f), tailPaint)
            textRight(c, pctText, g.x(232f), g.y(textTop), pctPaint)
        }
    }

    /** A battery drawn small: shell, terminal, fill for the charge, and a bolt while charging. */
    private fun cell(c: Canvas, g: Geo, x: Float, y: Float, w: Float, h: Float, info: BatteryInfo, tone: Int) {
        val outline = Paint(Paint.ANTI_ALIAS_FLAG).apply {
            style = Paint.Style.STROKE; strokeWidth = max(1f, g.m(1.5f)); color = Palette.DIM
        }
        c.drawRoundRect(RectF(g.x(x), g.y(y), g.x(x + w), g.y(y + h)), g.m(3f), g.m(3f), outline)
        c.drawRoundRect(
            RectF(g.x(x + w), g.y(y + h * 0.3f), g.x(x + w + 2.5f), g.y(y + h * 0.7f)), g.m(1f), g.m(1f),
            Paint(Paint.ANTI_ALIAS_FLAG).apply { color = Palette.DIM },
        )
        val pct = info.pct ?: return              // no battery: an empty shell and nothing to fill
        val frac = pct / 100f
        if (frac > 0f) {
            val inset = 2f
            val fw = max(g.m(3f), (w - 2 * inset) * g.sx * frac)
            c.drawRoundRect(
                RectF(g.x(x + inset), g.y(y + inset), g.x(x + inset) + fw, g.y(y + h - inset)),
                g.m(1.5f), g.m(1.5f),
                Paint(Paint.ANTI_ALIAS_FLAG).apply { color = tone },
            )
        }
        if (info.state == PowerState.CHARGING) {
            val bw = h * 0.62f
            val bh = h * 0.86f
            val bx = x + w / 2f - bw / 2f
            val by = y + (h - bh) / 2f
            fun px(u: Float) = g.x(bx + u * bw)
            fun py(v: Float) = g.y(by + v * bh)
            val bolt = Path().apply {
                moveTo(px(0.704f), py(0f))
                lineTo(px(0f), py(0.5625f))
                lineTo(px(0.407f), py(0.5625f))
                lineTo(px(0.259f), py(1f))
                lineTo(px(1f), py(0.396f))
                lineTo(px(0.593f), py(0.396f))
                close()
            }
            // Dark on a mostly full cell, light on a mostly empty one: whichever contrasts with what is under it.
            c.drawPath(bolt, Paint(Paint.ANTI_ALIAS_FLAG).apply { color = if (frac >= 0.5f) Palette.BG else Palette.TEXT })
        }
    }

    /**
     * The clock page. On the home screen the big digits are a live TextClock laid
     * over the gap left here; with [Frame.drawTime] set, they are drawn instead.
     */
    private fun clockPage(ctx: Context, c: Canvas, g: Geo, f: Frame, reg: Typeface, bold: Typeface) {
        val p = ClockFmt.parts(f.nowMs, ZoneId.systemDefault(), Locale.getDefault(), DateFormat.is24HourFormat(ctx))

        text(c, "// ${p.zone}", g.x(44f), g.y(3f), ink(reg, g.m(12f), Palette.DIM))
        if (f.count > 1) {
            textRight(c, "${f.index + 1}/${f.count}", g.x(206f), g.y(3f), ink(reg, g.m(12f), Palette.CYAN))
        }

        if (f.drawTime) {
            val tp = ink(bold, g.m(36f), Palette.TEXT)
            val ap = ink(reg, g.m(12f), Palette.DIM)
            val gap = if (p.ampm.isEmpty()) 0f else g.m(6f)
            val tw = tp.measureText(p.time)
            val aw = if (p.ampm.isEmpty()) 0f else ap.measureText(p.ampm)
            val left = g.x(120f) - (tw + gap + aw) / 2f
            tp.textAlign = Paint.Align.LEFT
            c.drawText(p.time, left, g.y(53f) - (tp.descent() + tp.ascent()) / 2f, tp)
            if (p.ampm.isNotEmpty()) text(c, p.ampm, left + tw + gap, g.y(53f) - g.m(16f), ap)
        }

        centered(c, p.weekday, g.x(120f), g.y(94f), ink(reg, g.m(14f), Palette.TEXT))
        centered(c, p.date, g.x(120f), g.y(112f), ink(reg, g.m(12f), Palette.DIM))

        f.message?.let { msg ->
            val mp = ink(reg, g.m(10f), Palette.DIM)
            centered(c, fit(msg, mp, 224f * g.sx), g.x(120f), g.y(126f), mp)
        }
    }

    /** left text ... as of 12m ago ... LINK OK. [ageS] is how old the reading was when it was fetched. */
    private fun footer(c: Canvas, g: Geo, f: Frame, leftText: String, ageS: Long, reg: Typeface) {
        val foot = g.y(118f)
        val leftPaint = ink(reg, g.m(12f), Palette.DIM)
        text(c, fit(leftText, leftPaint, 150f * g.sx), g.x(8f), foot, leftPaint)

        val linkPaint = ink(reg, g.m(12f), f.link.color)
        textRight(c, f.link.label, g.x(232f), foot, linkPaint)

        // The widget's own age. Centred in whatever room the two ends leave, and
        // dropped rather than overlapped if there is none.
        val asOf = ink(reg, g.m(12f), Palette.DIM)
        val label = Fmt.asOf(f.sinceFetchS + ageS)
        val gap = g.m(8f)
        val from = g.x(8f) + leftPaint.measureText(fit(leftText, leftPaint, 150f * g.sx)) + gap
        val to = g.x(232f) - linkPaint.measureText(f.link.label) - gap
        val need = asOf.measureText(label)
        if (need <= to - from) {
            asOf.textAlign = Paint.Align.CENTER
            c.drawText(label, (from + to) / 2f, foot - asOf.ascent(), asOf)
        }
    }

    // ------------------------------------------------------------------------
    //  Glass
    // ------------------------------------------------------------------------
    private fun drawGlass(c: Canvas, w: Float, h: Float, f: Frame, look: WidgetStyle) {
        val bottomA = (255 * look.opacity / 100f).roundToInt().coerceIn(0, 255)
        val topA = (bottomA * TOP_RATIO).roundToInt().coerceIn(0, 255)
        val k = look.glass.coerceIn(0, 100) / 100f
        val inset = max(1f, h * 0.01f)
        val r = RectF(inset, inset, w - inset, h - inset)
        val rad = min(w, h) * 0.17f

        fun layer(shader: Shader) = c.drawRoundRect(r, rad, rad, Paint(Paint.ANTI_ALIAS_FLAG).apply { this.shader = shader })

        // Tint: the panel colour fading to the background colour, translucent.
        layer(
            LinearGradient(
                0f, 0f, 0f, h,
                intArrayOf(Palette.PANEL.alpha(topA), Palette.BG.alpha(bottomA)),
                null, Shader.TileMode.CLAMP,
            ),
        )
        // Light from the top-left, in the brand cyan.
        layer(
            RadialGradient(
                w * 0.12f, h * 0.02f, w * 0.65f,
                intArrayOf(Palette.CYAN.scaleAlpha(0x30 / 255f * k), Palette.CYAN.alpha(0)), null, Shader.TileMode.CLAMP,
            ),
        )
        // Light from the bottom-right. It warms toward amber and red when a reading
        // is in trouble, so the glass itself carries state.
        val warm = f.machine?.let { m ->
            val worst = listOfNotNull(m.cpu, m.ram, m.storagePct).maxOrNull()
            Palette.load(Palette.MAGENTA, worst)
        } ?: Palette.MAGENTA
        layer(
            RadialGradient(
                w * 0.96f, h, w * 0.55f,
                intArrayOf(warm.scaleAlpha(0x34 / 255f * k), warm.alpha(0)), null, Shader.TileMode.CLAMP,
            ),
        )
        // Sheen across the top.
        layer(
            LinearGradient(
                0f, 0f, 0f, h * 0.55f,
                intArrayOf(0x2AFFFFFF.scaleAlpha(k), 0x00FFFFFF), null, Shader.TileMode.CLAMP,
            ),
        )
        // Specular rim: bright where light enters, faint through the middle.
        c.drawRoundRect(
            r, rad, rad,
            Paint(Paint.ANTI_ALIAS_FLAG).apply {
                style = Paint.Style.STROKE
                strokeWidth = max(1.5f, h * 0.012f)
                shader = LinearGradient(
                    0f, 0f, w, h,
                    intArrayOf(
                        0xA0FFFFFF.toInt().scaleAlpha(k), 0x18FFFFFF.scaleAlpha(k),
                        0x10FFFFFF.scaleAlpha(k), 0x60FFFFFF.scaleAlpha(k),
                    ),
                    floatArrayOf(0f, 0.35f, 0.7f, 1f), Shader.TileMode.CLAMP,
                )
            },
        )
        if (!Palette.isDark) {
            // White highlights do not show on a white card, so give it a thin edge instead.
            c.drawRoundRect(
                r, rad, rad,
                Paint(Paint.ANTI_ALIAS_FLAG).apply {
                    style = Paint.Style.STROKE
                    strokeWidth = max(1f, h * 0.008f)
                    color = Palette.TRACK.alpha(0xC0)
                },
            )
        }
    }

    // ------------------------------------------------------------------------
    //  Pieces
    // ------------------------------------------------------------------------
    private fun dot(c: Canvas, g: Geo, color: Int) {
        val cx = g.x(227f)
        val cy = g.y(8f) + g.m(2f)
        c.drawCircle(cx, cy, g.m(8f), Paint(Paint.ANTI_ALIAS_FLAG).apply { this.color = color.alpha(0x40) })
        c.drawCircle(cx, cy, g.m(5f), Paint(Paint.ANTI_ALIAS_FLAG).apply { this.color = color })
    }

    private fun gauge(
        c: Canvas, g: Geo, left: Float, top: Float, pct: Double?, base: Int, valueInk: Int,
        caption: String, bold: Typeface, reg: Typeface,
    ) {
        val d = g.m(66f)
        val sw = d / 11f                       // 6 px at 66, as on the device
        val r = (d - sw) / 2f
        val cx = left + d / 2f
        val cy = top + d / 2f
        val oval = RectF(cx - r, cy - r, cx + r, cy + r)

        val track = Paint(Paint.ANTI_ALIAS_FLAG).apply {
            style = Paint.Style.STROKE; strokeWidth = sw; strokeCap = Paint.Cap.ROUND; color = Palette.TRACK
        }
        // 270 degrees starting at 135: opens at the bottom, like the device's arc.
        c.drawArc(oval, 135f, 270f, false, track)
        if (pct != null && pct > 0.0) {
            val sweep = 270f * (pct / 100.0).coerceIn(0.0, 1.0).toFloat()
            c.drawArc(oval, 135f, sweep, false, Paint(track).apply { color = Palette.load(base, pct) })
        }
        centered(c, Fmt.pct(pct), cx, cy - g.m(5f), ink(bold, g.m(20f), valueInk))
        centered(c, caption, cx, cy + g.m(14f), ink(reg, g.m(12f), Palette.DIM))
    }

    private fun message(c: Canvas, g: Geo, f: Frame, reg: Typeface) {
        val msg = f.message ?: return
        val tp = TextPaint(Paint.ANTI_ALIAS_FLAG).apply {
            typeface = reg; textSize = g.m(14f)
            color = when (f.messageTone) {
                MessageTone.DIM -> Palette.DIM
                MessageTone.AMBER -> Palette.AMBER
                MessageTone.RED -> Palette.RED
            }
        }
        val width = (g.x(232f) - g.x(8f)).toInt().coerceAtLeast(1)
        val layout = StaticLayout.Builder.obtain(msg, 0, msg.length, tp, width)
            .setAlignment(Layout.Alignment.ALIGN_CENTER).build()
        val top = g.y(22f) + (g.y(135f) - g.y(22f) - layout.height) / 2f
        c.save()
        c.translate(g.x(8f), top)
        layout.draw(c)
        c.restore()
    }

    // ------------------------------------------------------------------------
    //  Text
    // ------------------------------------------------------------------------
    private fun ink(face: Typeface, px: Float, color: Int) = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        typeface = face; textSize = px; this.color = color
    }

    /** LVGL positions a label by its top-left corner; so do we. */
    private fun text(c: Canvas, s: String, x: Float, top: Float, p: Paint) {
        p.textAlign = Paint.Align.LEFT
        c.drawText(s, x, top - p.ascent(), p)
    }

    private fun textRight(c: Canvas, s: String, right: Float, top: Float, p: Paint) {
        p.textAlign = Paint.Align.RIGHT
        c.drawText(s, right, top - p.ascent(), p)
    }

    private fun centered(c: Canvas, s: String, cx: Float, cy: Float, p: Paint) {
        p.textAlign = Paint.Align.CENTER
        c.drawText(s, cx, cy - (p.descent() + p.ascent()) / 2f, p)
    }

    /** The device clips a long label; we end it with an ellipsis instead. */
    private fun fit(s: String, p: Paint, maxPx: Float): String {
        if (p.measureText(s) <= maxPx) return s
        var n = s.length
        while (n > 1 && p.measureText(s, 0, n) + p.measureText("\u2026") > maxPx) n--
        return s.substring(0, n).trimEnd() + "\u2026"
    }
}