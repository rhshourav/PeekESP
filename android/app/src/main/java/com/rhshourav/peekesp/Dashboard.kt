package com.rhshourav.peekesp

import android.content.Context
import android.text.format.DateFormat
import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.selection.selectable
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.produceState
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import com.rhshourav.peekesp.data.Battery
import com.rhshourav.peekesp.data.BatteryLevel
import com.rhshourav.peekesp.data.ChartData
import com.rhshourav.peekesp.data.ChartMath
import com.rhshourav.peekesp.data.Fmt
import com.rhshourav.peekesp.data.Freshness
import com.rhshourav.peekesp.data.History
import com.rhshourav.peekesp.data.HistoryConfig
import com.rhshourav.peekesp.data.HistoryRange
import com.rhshourav.peekesp.data.Machine
import com.rhshourav.peekesp.data.Parser
import com.rhshourav.peekesp.data.PowerState
import com.rhshourav.peekesp.data.Stat
import com.rhshourav.peekesp.data.WidgetCache
import com.rhshourav.peekesp.widget.Palette
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import kotlin.math.max
import kotlin.math.roundToInt

// ----------------------------------------------------------------------------
//  Live state
// ----------------------------------------------------------------------------

internal enum class LinkState { CONNECTING, LIVE, RECONNECTING, REJECTED }

/** What the dashboard is showing now: the latest reading of every machine, and how the link to the relay is doing. */
internal data class Snapshot(
    val machines: List<Machine>,
    val fetchedAtMs: Long,
    val nowMs: Long,
    val link: LinkState = LinkState.CONNECTING,
    val latencyMs: Long = 0,
    val error: String? = null,
)

/** The widget's cache, parsed: what to show before the first live fetch lands. */
internal fun readSnapshot(ctx: Context): Snapshot {
    val cached = WidgetCache(ctx).read()
    val machines = try {
        if (cached == null) emptyList() else Parser.parse(cached.raw)
    } catch (e: Exception) {
        emptyList()
    }
    return Snapshot(machines, cached?.fetchedAtMs ?: 0L, System.currentTimeMillis())
}

private fun linkLine(s: Snapshot): String = when (s.link) {
    LinkState.CONNECTING -> "Connecting\u2026"
    LinkState.LIVE -> "Live \u00B7 refreshes every 5 s" + if (s.latencyMs > 0) " \u00B7 ${Fmt.latency(s.latencyMs)}" else ""
    LinkState.RECONNECTING -> "Reconnecting\u2026" + (s.error?.let { " ($it)" } ?: "")
    LinkState.REJECTED -> "The relay rejected this code. Pair again in Configure."
}

private fun toneFor(info: com.rhshourav.peekesp.data.BatteryInfo, offline: Boolean): Int = when {
    offline || !info.present -> Palette.DIM
    info.state == PowerState.CHARGING -> Palette.GREEN
    else -> when (Battery.level(info)) {
        BatteryLevel.CRITICAL -> Palette.RED
        BatteryLevel.LOW -> Palette.AMBER
        BatteryLevel.OK -> Palette.GREEN
    }
}

// ----------------------------------------------------------------------------
//  Dashboard
// ----------------------------------------------------------------------------

/** The Machines card, then the selected machine's history. Emits into the caller's column. */
@Composable
internal fun DashboardPanel(
    snap: Snapshot,
    selected: String?,
    onSelect: (String) -> Unit,
    range: HistoryRange,
    onRange: (HistoryRange) -> Unit,
) {
    SectionCard(title = "Machines", subtitle = linkLine(snap)) {
        FleetList(snap, selected, onSelect)
    }
    val m = snap.machines.firstOrNull { it.host == selected } ?: return
    MachineHistory(m, snap, range, onRange)
}

@Composable
private fun FleetList(snap: Snapshot, selected: String?, onSelect: (String) -> Unit) {
    if (snap.machines.isEmpty()) {
        Text(
            if (snap.link == LinkState.CONNECTING) "Waiting for the first reading\u2026"
            else "No readings yet. Machines appear here once a device or agent pushes to your code.",
            style = MaterialTheme.typography.bodyMedium,
            color = Color(Palette.DIM),
        )
        return
    }
    snap.machines.forEach { m -> FleetRow(m, snap, m.host == selected) { onSelect(m.host) } }
    val reporting = snap.machines.count { Battery.of(it).present }
    Text(
        if (reporting == 0) {
            "None of these report a battery. A desktop never does; on a laptop, the agent has to send " +
                "battery_percent for it to appear here and on the widget."
        } else {
            "$reporting of ${snap.machines.size} report a battery. Tap a machine to chart it."
        },
        style = MaterialTheme.typography.bodySmall,
        color = Color(Palette.DIM),
    )
}

@Composable
private fun FleetRow(m: Machine, s: Snapshot, selected: Boolean, onClick: () -> Unit) {
    val age = Freshness.ageSeconds(m.ageS, s.fetchedAtMs, s.nowMs)
    val offline = age >= Freshness.offlineAfterSeconds(Freshness.DEFAULT_POLL_S)
    val bat = Battery.of(m)
    val tone = toneFor(bat, offline)
    val status = buildString {
        append(if (offline) "offline ${Fmt.ago(age)}" else "online")
        if (!offline) {
            m.cpu?.let { append(" \u00B7 CPU ${Fmt.pct(it)}%") }
            m.ram?.let { append(" \u00B7 RAM ${Fmt.pct(it)}%") }
        }
    }
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(12.dp))
            .background(if (selected) Color(Palette.CYAN).copy(alpha = 0.10f) else Color.Transparent)
            .selectable(selected = selected, role = Role.RadioButton, onClick = onClick)
            .padding(horizontal = 10.dp, vertical = 8.dp),
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        Column(modifier = Modifier.weight(1f)) {
            Text(m.host, style = MaterialTheme.typography.bodyLarge, maxLines = 1, overflow = TextOverflow.Ellipsis)
            Text(
                status,
                style = MaterialTheme.typography.bodySmall,
                color = Color(if (offline) Palette.AMBER else Palette.DIM),
                maxLines = 1,
                overflow = TextOverflow.Ellipsis,
            )
        }
        Column(horizontalAlignment = Alignment.End) {
            Text(
                if (bat.present) "${bat.pct}%" else "--",
                style = MaterialTheme.typography.titleMedium,
                fontWeight = FontWeight.Bold,
                color = Color(tone),
            )
            Text(bat.tail, style = MaterialTheme.typography.bodySmall, color = Color(Palette.DIM), maxLines = 1)
        }
    }
}

// ----------------------------------------------------------------------------
//  One machine's history
// ----------------------------------------------------------------------------

private val VIOLET = Color(0xFFB28DFF)   // chart-only: the second line on a two-line chart

@Composable
private fun MachineHistory(m: Machine, snap: Snapshot, range: HistoryRange, onRange: (HistoryRange) -> Unit) {
    val ctx = LocalContext.current
    val on = HistoryConfig.enabled(ctx)
    val ranges = HistoryRange.availableFor(HistoryConfig.retentionDays(ctx))
    val r = if (range in ranges) range else ranges.first()

    SectionCard(title = m.host, subtitle = "History") {
        Segmented(options = ranges.map { it to it.label }, selected = r, onSelect = onRange)
        if (!on) {
            Text(
                "History logging is off, so there is nothing to chart. Turn it on under Configure.",
                style = MaterialTheme.typography.bodyMedium,
                color = Color(Palette.AMBER),
            )
        }
    }
    if (!on) return

    // Reload when the machine, the range, or a fresh reading changes.
    val data by produceState<ChartData?>(null, m.host, r, snap.fetchedAtMs) {
        value = withContext(Dispatchers.IO) { runCatching { History.load(ctx, m.host, r) }.getOrNull() }
    }
    val d = data
    if (d == null) {
        Text("Loading\u2026", style = MaterialTheme.typography.bodyMedium, color = Color(Palette.DIM))
        return
    }
    if (d.stats.samples < 2) {
        SectionCard(title = "Collecting data") {
            Text(
                "Not enough readings yet to draw a chart. While this screen is open a reading is " +
                    "logged every 15 seconds; in the background the widget logs one every 15 minutes.",
                style = MaterialTheme.typography.bodyMedium,
                color = Color(Palette.DIM),
            )
        }
        return
    }

    val gap = ChartMath.gapMs(d.bucketMs)
    val pct: (Double) -> String = { "${it.roundToInt()}%" }

    MetricCard(
        title = "CPU", live = m.cpu?.let { "${Fmt.pct(it)}%" } ?: "--", accent = Color(Palette.CYAN), d = d,
        series = listOf(ChartSeries("CPU", "", Color(Palette.CYAN), d.buckets.map { it.t to it.cpu })),
        yMin = 0.0, yMax = 100.0, fmt = pct, stats = listOf("" to d.stats.cpu),
    )
    MetricCard(
        title = "Memory", live = m.ram?.let { "${Fmt.pct(it)}%" } ?: "--", accent = Color(Palette.MAGENTA), d = d,
        series = listOf(ChartSeries("RAM", "", Color(Palette.MAGENTA), d.buckets.map { it.t to it.ram })),
        yMin = 0.0, yMax = 100.0, fmt = pct, stats = listOf("" to d.stats.ram),
    )
    d.stats.temp?.let { t ->
        val (lo, hi) = ChartMath.axisFor(t.min, t.max)
        MetricCard(
            title = "Temperature", live = Fmt.temp(m.tempC), accent = Color(Palette.AMBER), d = d,
            series = listOf(ChartSeries("Temp", "", Color(Palette.AMBER), d.buckets.map { it.t to it.temp })),
            yMin = lo, yMax = hi, fmt = { "${it.roundToInt()}\u00B0" }, stats = listOf("" to t),
        )
    }
    val peak = max(d.stats.rx?.max ?: 0.0, d.stats.tx?.max ?: 0.0)
    MetricCard(
        title = "Network", live = "\u2193 ${Fmt.rate(m.rxKbps)}  \u2191 ${Fmt.rate(m.txKbps)}", accent = Color(Palette.GREEN), d = d,
        series = listOf(
            ChartSeries("download", "\u2193", Color(Palette.GREEN), d.buckets.map { it.t to it.rx }),
            ChartSeries("upload", "\u2191", VIOLET, d.buckets.map { it.t to it.tx }),
        ),
        yMin = 0.0, yMax = ChartMath.niceCeil(max(peak * 1.1, 10.0)), fmt = { Fmt.rate(it) },
        stats = listOf("\u2193" to d.stats.rx, "\u2191" to d.stats.tx),
    )
    MetricCard(
        title = "Storage used",
        live = m.storagePct?.let { "${Fmt.pct(it)}%" + (m.storageFreeGb?.let { f -> " \u00B7 ${Fmt.capacity(f)} free" } ?: "") } ?: "--",
        accent = Color(Palette.CYAN), d = d,
        series = listOf(ChartSeries("Storage", "", Color(Palette.CYAN), d.buckets.map { it.t to it.storage })),
        yMin = 0.0, yMax = 100.0, fmt = pct, stats = listOf("" to d.stats.storage),
    )
    d.stats.bat?.let { b ->
        val info = Battery.of(m)
        MetricCard(
            title = "Battery",
            live = if (info.present) "${info.pct}% \u00B7 ${info.tail}" else "--",
            accent = Color(toneFor(info, false).let { if (it == Palette.DIM) Palette.GREEN else it }), d = d,
            series = listOf(ChartSeries("Battery", "", Color(Palette.GREEN), d.buckets.map { it.t to it.bat })),
            yMin = 0.0, yMax = 100.0, fmt = pct, stats = listOf("" to b),
            bands = ChartMath.flagBands(d.buckets.map { it.t to it.charging }, d.bucketMs / 2, gap),
            note = "Green shading marks when it was charging.",
        )
    }
}

/** One metric: its name, the value now (or under your finger), min/avg/max, and the chart. */
@Composable
private fun MetricCard(
    title: String,
    live: String,
    accent: Color,
    d: ChartData,
    series: List<ChartSeries>,
    yMin: Double,
    yMax: Double,
    fmt: (Double) -> String,
    stats: List<Pair<String, Stat?>>,
    bands: List<Pair<Long, Long>> = emptyList(),
    note: String? = null,
) {
    val ctx = LocalContext.current
    var cursor by remember { mutableStateOf<Long?>(null) }
    val gap = ChartMath.gapMs(d.bucketMs)
    val long = d.range.ms > 86_400_000L

    val under = cursor?.let { t ->
        val parts = series.mapNotNull { s ->
            ChartMath.valueAt(s.points, t, gap)?.let { v -> (if (series.size > 1) "${s.tag} " else "") + fmt(v) }
        }
        timeText(t, long) + "  " + (if (parts.isEmpty()) "no data" else parts.joinToString("  "))
    }
    val hasData = series.any { s -> s.points.any { it.second != null } }

    Surface(
        color = Color(Palette.PANEL),
        shape = RoundedCornerShape(20.dp),
        border = BorderStroke(1.dp, Color(Palette.TRACK)),
        modifier = Modifier.fillMaxWidth(),
    ) {
        Column(modifier = Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween, verticalAlignment = Alignment.CenterVertically) {
                Text(title, style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.SemiBold)
                Text(
                    under ?: live,
                    style = MaterialTheme.typography.titleMedium,
                    fontFamily = FontFamily.Monospace,
                    color = accent,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis,
                )
            }
            if (series.size > 1) {
                Row(horizontalArrangement = Arrangement.spacedBy(14.dp)) {
                    series.forEach { s ->
                        Row(verticalAlignment = Alignment.CenterVertically) {
                            Box(Modifier.size(8.dp).background(s.color, CircleShape))
                            Text(
                                "  ${s.tag} ${s.name}",
                                style = MaterialTheme.typography.bodySmall,
                                color = Color(Palette.DIM),
                            )
                        }
                    }
                }
            }
            if (!hasData) {
                Text("No data in this range.", style = MaterialTheme.typography.bodyMedium, color = Color(Palette.DIM))
            } else {
                val summary = statLine(stats, fmt)
                LineChart(
                    series = series, fromMs = d.fromMs, toMs = d.toMs, yMin = yMin, yMax = yMax, gapMs = gap,
                    bands = bands, cursorT = cursor, onCursor = { cursor = it },
                    topLabel = fmt(yMax), bottomLabel = fmt(yMin),
                    description = "$title, last ${d.range.label}. $live. $summary",
                )
                Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
                    val dim = Color(Palette.DIM)
                    Text(timeText(d.fromMs, long), style = MaterialTheme.typography.labelSmall, color = dim)
                    Text(timeText((d.fromMs + d.toMs) / 2, long), style = MaterialTheme.typography.labelSmall, color = dim)
                    Text("now", style = MaterialTheme.typography.labelSmall, color = dim)
                }
                Text(summary, style = MaterialTheme.typography.bodySmall, color = Color(Palette.DIM))
                if (note != null) Text(note, style = MaterialTheme.typography.bodySmall, color = Color(Palette.DIM))
            }
        }
    }
}

private fun statLine(stats: List<Pair<String, Stat?>>, fmt: (Double) -> String): String {
    val present = stats.filter { it.second != null }
    return if (stats.size == 1) {
        val s = present.firstOrNull()?.second ?: return ""
        "min ${fmt(s.min)} \u00B7 avg ${fmt(s.avg)} \u00B7 max ${fmt(s.max)}"
    } else {
        present.joinToString("   ") { (tag, s) -> "$tag avg ${fmt(s!!.avg)} \u00B7 max ${fmt(s.max)}" }
    }
}

/** A time in the phone's own style: 12 or 24 hour as set, with the weekday once the window is over a day. */
private fun timeText(t: Long, withDay: Boolean): String {
    val pattern = DateFormat.getBestDateTimePattern(Locale.getDefault(), if (withDay) "EEEdjm" else "jm")
    return SimpleDateFormat(pattern, Locale.getDefault()).format(Date(t))
}

// ----------------------------------------------------------------------------
//  History settings (shown under Configure)
// ----------------------------------------------------------------------------

@Composable
internal fun HistoryPanel(notify: (String) -> Unit) {
    val ctx = LocalContext.current
    val scope = rememberCoroutineScope()
    var enabled by remember { mutableStateOf(HistoryConfig.enabled(ctx)) }
    var days by remember { mutableStateOf(HistoryConfig.retentionDays(ctx)) }
    var usage by remember { mutableStateOf<Pair<Long, Long>?>(null) }
    var confirmClear by remember { mutableStateOf(false) }
    var refresh by remember { mutableStateOf(0) }

    LaunchedEffect(enabled, days, refresh) {
        usage = withContext(Dispatchers.IO) { runCatching { History.count(ctx) to History.sizeBytes(ctx) }.getOrNull() }
    }

    ToggleRow(
        title = "Log history",
        subtitle = "Keeps each reading so the dashboard can chart it. It stays on this phone.",
        checked = enabled,
        onChange = { enabled = it; HistoryConfig.setEnabled(ctx, it) },
    )

    SubHeading("Keep for")
    Segmented(
        options = listOf(1 to "1 day", 7 to "7 days", 30 to "30 days"),
        selected = days,
        onSelect = {
            days = it
            HistoryConfig.setRetentionDays(ctx, it)
            scope.launch(Dispatchers.IO) { runCatching { History.prune(ctx) } }
        },
    )

    Text(
        usage?.let { (n, bytes) -> "${"%,d".format(n)} readings \u00B7 ${sizeLabel(bytes)}" } ?: "Counting\u2026",
        style = MaterialTheme.typography.bodyMedium,
    )
    Text(
        "At most one reading per machine every 15 seconds is kept. Charts are sharpest while the " +
            "app is open; in the background the widget adds one every 15 minutes, which is the " +
            "most Android allows.",
        style = MaterialTheme.typography.bodySmall,
        color = Color(Palette.DIM),
    )
    OutlinedButton(
        onClick = { confirmClear = true },
        modifier = Modifier.fillMaxWidth(),
    ) { Text("Clear history", color = Color(Palette.RED)) }

    if (confirmClear) {
        AlertDialog(
            onDismissRequest = { confirmClear = false },
            title = { Text("Clear history?") },
            text = { Text("Every logged reading is deleted from this phone. New readings start a fresh history.") },
            confirmButton = {
                TextButton(onClick = {
                    confirmClear = false
                    scope.launch {
                        withContext(Dispatchers.IO) { runCatching { History.clear(ctx) } }
                        refresh++
                        notify("History cleared.")
                    }
                }) { Text("Clear", color = Color(Palette.RED)) }
            },
            dismissButton = { TextButton(onClick = { confirmClear = false }) { Text("Keep") } },
        )
    }
}

private fun sizeLabel(bytes: Long): String = when {
    bytes >= 1_048_576 -> String.format(Locale.US, "%.1f MB", bytes / 1_048_576.0)
    bytes >= 1024 -> String.format(Locale.US, "%.0f KB", bytes / 1024.0)
    else -> "$bytes B"
}