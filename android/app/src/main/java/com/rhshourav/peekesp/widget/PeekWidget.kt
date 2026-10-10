package com.rhshourav.peekesp.widget

import android.content.Context
import androidx.compose.runtime.Composable
import androidx.compose.runtime.remember
import androidx.glance.GlanceId
import androidx.glance.GlanceModifier
import androidx.glance.Image
import androidx.glance.ImageProvider
import androidx.glance.LocalContext
import androidx.glance.LocalSize
import androidx.glance.action.ActionParameters
import androidx.glance.action.clickable
import androidx.glance.appwidget.GlanceAppWidget
import androidx.glance.appwidget.GlanceAppWidgetReceiver
import androidx.glance.appwidget.SizeMode
import androidx.glance.appwidget.action.ActionCallback
import androidx.glance.appwidget.action.actionRunCallback
import androidx.glance.appwidget.provideContent
import androidx.glance.appwidget.updateAll
import androidx.glance.layout.ContentScale
import androidx.glance.layout.fillMaxSize
import com.rhshourav.peekesp.data.WidgetCache
import kotlin.math.sqrt

/** The home-screen widget: one machine, drawn like the device's own screen. */
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

        // RemoteViews has a bitmap memory ceiling; stay far below it.
        var w = (size.width.value * density).toInt().coerceAtLeast(1)
        var h = (size.height.value * density).toInt().coerceAtLeast(1)
        if (w.toLong() * h > MAX_PIXELS) {
            val k = sqrt(MAX_PIXELS.toDouble() / (w.toLong() * h)).toFloat()
            w = (w * k).toInt().coerceAtLeast(1)
            h = (h * k).toInt().coerceAtLeast(1)
        }

        val frame = remember { FrameBuilder.build(ctx, System.currentTimeMillis()) }
        val bitmap = remember(w, h) { WidgetRenderer.render(ctx, w, h, frame) }

        Image(
            provider = ImageProvider(bitmap),
            contentDescription = frame.describe(),
            contentScale = ContentScale.Fit,
            modifier = GlanceModifier.fillMaxSize().clickable(actionRunCallback<NextMachineAction>()),
        )
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

/** Tap: show the next machine from what is already cached, then ask for fresh numbers. */
class NextMachineAction : ActionCallback {
    override suspend fun onAction(context: Context, glanceId: GlanceId, parameters: ActionParameters) {
        WidgetCache(context).advance()
        PeekWidget().updateAll(context)
        RefreshWorker.refreshNow(context)
    }
}
