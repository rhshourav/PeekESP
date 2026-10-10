package com.rhshourav.peekesp.widget

import android.content.Context
import android.graphics.Bitmap
import android.graphics.Canvas
import android.graphics.LinearGradient
import android.graphics.Paint
import android.graphics.RadialGradient
import android.graphics.RectF
import android.graphics.Shader
import android.graphics.Typeface
import android.text.Layout
import android.text.StaticLayout
import android.text.TextPaint
import com.rhshourav.peekesp.data.Fmt
import kotlin.math.max
import kotlin.math.min

/**
 * Draws the device's dashboard (PeekESP.ino, layout_metrics.h) onto a glass card.
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
 * Why "glass-like", not glass: RemoteViews cannot sample or blur the wallpaper.
 * Tint, light from two corners, a sheen and a specular rim are as far as a
 * widget can go. The tint is strong enough that text holds contrast over most
 * wallpapers; raise GLASS_*_ALPHA if you use very bright ones.
 */
object WidgetRenderer {
    private const val GLASS_TOP_ALPHA = 0xBC
    private const val GLASS_BOTTOM_ALPHA = 0xD6

    private class Geo(val ox: Float, val oy: Float, w: Float, h: Float) {
        val sx = w / 240f
        val sy = h / 135f
        val sm = min(sx, sy)
        fun x(v: Float) = ox + v * sx
        fun y(v: Float) = oy + v * sy
        fun m(v: Float) = v * sm
    }

    private fun Int.alpha(a: Int) = (this and 0x00FFFFFF) or (a shl 24)

    fun render(ctx: Context, w: Int, h: Int, f: Frame): Bitmap {
        val bmp = Bitmap.createBitmap(max(1, w), max(1, h), Bitmap.Config.ARGB_8888)
        val c = Canvas(bmp)
        val wf = bmp.width.toFloat()
        val hf = bmp.height.toFloat()
        drawGlass(c, wf, hf, f)

        val pad = hf * 0.06f
        val g = Geo(pad, pad, wf - 2 * pad, hf - 2 * pad)
        val reg = Fonts.get(ctx, 500)
        val bold = Fonts.get(ctx, 700)
        val m = f.machine

        // ---- header: PEEK  // host        1/2  42 ms  (dot) ----
        text(c, "PEEK", g.x(8f), g.y(3f), ink(bold, g.m(12f), Palette.CYAN))
        val rule = Paint(Paint.ANTI_ALIAS_FLAG).apply {
            color = Palette.CYAN.alpha(0x4D); strokeWidth = max(1f, g.m(1f))
        }
        c.drawLine(g.x(8f), g.y(19f), g.x(232f), g.y(19f), rule)
        dot(c, g, f.link.color.takeIf { m != null } ?: Palette.DIM)

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
        c.drawRoundRect(box, radius, radius, Paint(Paint.ANTI_ALIAS_FLAG).apply { color = Palette.PANEL.alpha(0xC8) })
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
        text(c, "TX ${Fmt.rate(m.txKbps)}", tx, ty + g.m(45f), ink(reg, g.m(11f), Palette.DIM))

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

        // ---- footer: uptime (or "offline 5m") ... as of 12m ago ... LINK OK ----
        val foot = g.y(118f)
        val leftText = if (f.offline) "offline ${Fmt.ago(m.ageS + f.sinceFetchS)}" else Fmt.uptime(m.uptimeS)
        val leftPaint = ink(reg, g.m(12f), Palette.DIM)
        text(c, leftText, g.x(8f), foot, leftPaint)

        val linkPaint = ink(reg, g.m(12f), f.link.color)
        textRight(c, f.link.label, g.x(232f), foot, linkPaint)

        // The widget's own age. Centred in whatever room the two ends leave, and
        // dropped rather than overlapped if there is none.
        val asOf = ink(reg, g.m(12f), Palette.DIM)
        val label = Fmt.asOf(f.sinceFetchS + m.ageS)
        val gap = g.m(8f)
        val from = g.x(8f) + leftPaint.measureText(leftText) + gap
        val to = g.x(232f) - linkPaint.measureText(f.link.label) - gap
        val need = asOf.measureText(label)
        if (need <= to - from) {
            asOf.textAlign = Paint.Align.CENTER
            c.drawText(label, (from + to) / 2f, foot - asOf.ascent(), asOf)
        }
        return bmp
    }

    // ------------------------------------------------------------------------
    //  Glass
    // ------------------------------------------------------------------------
    private fun drawGlass(c: Canvas, w: Float, h: Float, f: Frame) {
        val inset = max(1f, h * 0.01f)
        val r = RectF(inset, inset, w - inset, h - inset)
        val rad = min(w, h) * 0.17f

        fun layer(shader: Shader) = c.drawRoundRect(r, rad, rad, Paint(Paint.ANTI_ALIAS_FLAG).apply { this.shader = shader })

        // Tint: the panel colour fading to the background colour, translucent.
        layer(
            LinearGradient(
                0f, 0f, 0f, h,
                intArrayOf(Palette.PANEL.alpha(GLASS_TOP_ALPHA), Palette.BG.alpha(GLASS_BOTTOM_ALPHA)),
                null, Shader.TileMode.CLAMP,
            ),
        )
        // Light from the top-left, in the brand cyan.
        layer(
            RadialGradient(
                w * 0.12f, h * 0.02f, w * 0.65f,
                intArrayOf(Palette.CYAN.alpha(0x30), Palette.CYAN.alpha(0)), null, Shader.TileMode.CLAMP,
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
                intArrayOf(warm.alpha(0x34), warm.alpha(0)), null, Shader.TileMode.CLAMP,
            ),
        )
        // Sheen across the top.
        layer(
            LinearGradient(
                0f, 0f, 0f, h * 0.55f,
                intArrayOf(0x2AFFFFFF, 0x00FFFFFF), null, Shader.TileMode.CLAMP,
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
                    intArrayOf(0xA0FFFFFF.toInt(), 0x18FFFFFF, 0x10FFFFFF, 0x60FFFFFF),
                    floatArrayOf(0f, 0.35f, 0.7f, 1f), Shader.TileMode.CLAMP,
                )
            },
        )
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
            typeface = reg; textSize = g.m(14f); color = f.messageColor
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
