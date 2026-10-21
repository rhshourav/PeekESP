package com.rhshourav.peekesp.widget

import android.content.Context
import androidx.compose.runtime.Composable
import androidx.compose.runtime.remember
import androidx.datastore.preferences.core.Preferences
import androidx.datastore.preferences.core.longPreferencesKey
import androidx.glance.GlanceId
import androidx.glance.GlanceModifier
import androidx.glance.Image
import androidx.glance.ImageProvider
import androidx.glance.LocalContext
import androidx.glance.LocalSize
import androidx.glance.action.ActionParameters
import androidx.glance.action.clickable
import androidx.glance.appwidget.AndroidRemoteViews
import androidx.glance.appwidget.GlanceAppWidget
import androidx.glance.appwidget.GlanceAppWidgetManager
import androidx.glance.appwidget.GlanceAppWidgetReceiver
import androidx.glance.appwidget.SizeMode
import androidx.glance.appwidget.action.ActionCallback
import androidx.glance.appwidget.action.actionRunCallback
import androidx.glance.appwidget.provideContent
import androidx.glance.appwidget.state.updateAppWidgetState
import androidx.glance.currentState
import androidx.glance.layout.Box
import androidx.glance.layout.ContentScale
import androidx.glance.layout.fillMaxSize
import com.rhshourav.peekesp.data.StyleStore
import com.rhshourav.peekesp.data.WidgetCache
import kotlin.math.sqrt

/** Bumped by [redrawWidgets]. The composition reads it, so a bump always forces a redraw. */
private val REV = longPreferencesKey("rev")

/**
 * Redraws every placed widget from the current cache and look settings.
 *
 * Glance keeps a widget's composition alive for a while after an update, and a
 * plain update() does not re-run code that read its inputs once inside
 * remember {}. Writing a new revision into the widget's own state makes the
 * composition depend on something that really changed. Use this instead of
 * updateAll() everywhere.
 */
suspend fun redrawWidgets(context: Context) {
    val widget = PeekWidget()
    val rev = System.currentTimeMillis()
    for (id in GlanceAppWidgetManager(context).getGlanceIds(PeekWidget::class.java)) {
        updateAppWidgetState(context, id) { it[REV] = rev }
        widget.update(context, id)
    }
}

/** The home-screen widget: one page at a time, drawn like the device's own screen. */
class PeekWidget : GlanceAppWidget() {
    // The renderer draws at the exact size the launcher gives, so the glass has
    // the right corner radius and the layout the right proportions at any size.
    override val sizeMode: SizeMode = SizeMode.Exact

    override suspend fun provideGlance(context: Context, id: GlanceId) {
        provideContent { Content() }
    }

    @Composable
    private fun Content() {
        val ctx = LocalContext.current
        val size = LocalSize.current
        val density = ctx.resources.displayMetrics.density
        val rev = currentState<Preferences>()[REV] ?: 0L

        // The size the launcher shows the widget at, in pixels. The live clock is laid out against this.
        val shownW = (size.width.value * density).toInt().coerceAtLeast(1)
        val shownH = (size.height.value * density).toInt().coerceAtLeast(1)

        // RemoteViews has a bitmap memory ceiling; stay far below it.
        var w = shownW
        var h = shownH
        if (w.toLong() * h > MAX_PIXELS) {
            val k = sqrt(MAX_PIXELS.toDouble() / (w.toLong() * h)).toFloat()
            w = (w * k).toInt().coerceAtLeast(1)
            h = (h * k).toInt().coerceAtLeast(1)
        }

        val frame = remember(rev) { FrameBuilder.build(ctx, System.currentTimeMillis()) }
        val look = remember(rev) { StyleStore(ctx).read() }
        val bitmap = remember(w, h, rev) { WidgetRenderer.render(ctx, w, h, frame, look) }

        Box(modifier = GlanceModifier.fillMaxSize()) {
            Image(
                provider = ImageProvider(bitmap),
                contentDescription = frame.describe(),
                contentScale = ContentScale.Fit,
                modifier = GlanceModifier.fillMaxSize().clickable(actionRunCallback<NextPageAction>()),
            )
            if (frame.kind == PageKind.CLOCK) {
                // Not clickable itself, so a tap on the digits falls through to the image under it.
                AndroidRemoteViews(
                    remoteViews = ClockOverlay.build(ctx, shownW, shownH, look),
                    modifier = GlanceModifier.fillMaxSize(),
                )
            }
        }
    }

    private companion object {
        const val MAX_PIXELS = 1_000_000L
    }
}

class PeekWidgetReceiver : GlanceAppWidgetReceiver() {
    override val glanceAppWidget: GlanceAppWidget = PeekWidget()

    override fun onEnabled(context: Context) {
        super.onEnabled(context)
        RefreshWorker.schedule(context)
        RefreshWorker.refreshNow(context)
    }

    override fun onDisabled(context: Context) {
        super.onDisabled(context)
        RefreshWorker.cancel(context)
    }
}

/** Tap: show the next page (a machine, its battery, or the clock) from what is cached, then ask for fresh numbers. */
class NextPageAction : ActionCallback {
    override suspend fun onAction(context: Context, glanceId: GlanceId, parameters: ActionParameters) {
        WidgetCache(context).advance(Pages.count(context))
        redrawWidgets(context)
        RefreshWorker.refreshNow(context)
    }
}