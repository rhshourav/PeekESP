package com.rhshourav.peekesp

import android.content.Context
import android.text.format.DateFormat
import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.Canvas
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
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import com.rhshourav.peekesp.data.Availability
import com.rhshourav.peekesp.data.Battery
import com.rhshourav.peekesp.data.BatteryLevel
import com.rhshourav.peekesp.data.ChartData
import com.rhshourav.peekesp.data.ChartMath
import com.rhshourav.peekesp.data.Coverage
import com.rhshourav.peekesp.data.Fmt
import com.rhshourav.peekesp.data.Freshness
import com.rhshourav.peekesp.data.History
import com.rhshourav.peekesp.data.HistoryConfig
import com.rhshourav.peekesp.data.HistoryRange
import com.rhshourav.peekesp.data.KnownMachine
import com.rhshourav.peekesp.data.Machine
import com.rhshourav.peekesp.data.Metrics
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

/**
 * How the phone's path to the relay is doing. Deliberately not about any machine: a machine can be
 * fine while the phone has no signal, and a machine can be off while the phone is perfectly online.
 */
internal enum class LinkState { CONNECTING, LIVE, PHONE_OFFLINE, RELAY_DOWN, REJECTED }

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
    LinkState.PHONE_OFFLINE -> "No internet on this phone. What follows is the last reading it fetched."
    LinkState.RELAY_DOWN -> "This phone is online but the relay is not answering. What follows is the last reading it fetched."
    LinkState.REJECTED -> "The relay rejected this code. Pair again in Configure."
}

/** What can honestly be said about one machine right now, given how the phone's link is doing. */
private data class LiveStatus(val text: String, val tone: Int, val machineOnline: Boolean)

/**
 * "Offline" is only said of a machine when the relay itself answered and reported it silent. When the
 * phone cannot ask, the machine is unknown, not off: saying "offline" there would blame a machine for
 * a dead phone signal.
 */
private fun liveStatus(m: Machine, s: Snapshot): LiveStatus {
    val readingAge = Freshness.ageSeconds(m.ageS, s.fetchedAtMs, s.nowMs)
    return when (s.link) {
        LinkState.LIVE ->
            if (m.ageS >= Freshness.offlineAfterSeconds(Freshness.DEFAULT_POLL_S)) {
                LiveStatus("Machine offline \u00B7 silent for ${Fmt.ago(m.ageS.toLong())}", Palette.AMBER, false)
            } else {
                LiveStatus("Machine online", Palette.GREEN, true)
            }
        LinkState.PHONE_OFFLINE -> LiveStatus("Phone offline \u00B7 last reading ${Fmt.ago(readingAge)} ago", Palette.DIM, false)
        LinkState.RELAY_DOWN -> LiveStatus("Relay unreachable \u00B7 last reading ${Fmt.ago(readingAge)} ago", Palette.DIM, false)
        LinkState.REJECTED -> LiveStatus("Code rejected \u00B7 last reading ${Fmt.ago(readingAge)} ago", Palette.RED, false)
        LinkState.CONNECTING -> LiveStatus("Last reading ${Fmt.ago(readingAge)} ago", Palette.DIM, false)
    }
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

/**
 * The Machines card, then the selected machine's history. Emits into the caller's column.
 *
 * The list is the relay's live machines plus every machine this phone still holds history for. The relay
 * forgets a machine once it has been silent for a while; that is right for the widget, which should only show
 * what is there, but the history is the phone's own, so a machine that has gone quiet stays chartable.
 */
@Composable
internal fun DashboardPanel(
    snap: Snapshot,
    selected: String?,
    onSelect: (String) -> Unit,
    range: HistoryRange,
    onRange: (HistoryRange) -> Unit,
) {
    val ctx = LocalContext.current
    val liveHosts = snap.machines.map { it.host }
    var rev by remember { mutableStateOf(0) }       // bumped when a machine's history is forgotten

    // Read again only when the set of live machines changes (or one is forgotten), not on every 5 s reading.
    val known by produceState(emptyList<KnownMachine>(), liveHosts, rev) {
        value = withContext(Dispatchers.IO) { runCatching { History.roster(ctx) }.getOrDefault(emptyList()) }
    }
    // Until the first answer is in, a machine missing from the list means nothing: do not call it gone yet.
    val settled = snap.link != LinkState.CONNECTING || snap.machines.isNotEmpty()
    val archive = if (settled) known.filter { it.host !in liveHosts } else emptyList()

    val sel = selected?.takeIf { h -> h in liveHosts || archive.any { it.host == h } }
        ?: liveHosts.firstOrNull()
        ?: archive.firstOrNull()?.host

    SectionCard(title = "Machines", subtitle = linkLine(snap)) {
        FleetList(snap, archive, sel, onSelect)
    }
    val current = snap.machines.firstOrNull { it.host == sel }
    val lastSeen = archive.firstOrNull { it.host == sel }?.lastSeenMs
    if (sel != null && (current != null || lastSeen != null)) {
        MachineHistory(sel, current, lastSeen, snap, range, onRange, onForgotten = { rev++ })
    }
}

@Composable
private fun FleetList(snap: Snapshot, archive: List<KnownMachine>, selected: String?, onSelect: (String) -> Unit) {
    if (snap.machines.isEmpty() && archive.isEmpty()) {
        Text(
            if (snap.link == LinkState.CONNECTING) "Waiting for the first reading\u2026"
            else "No readings yet. Machines appear here once a device or agent pushes to your code.",
            style = MaterialTheme.typography.bodyMedium,
            color = Color(Palette.DIM),
        )
        return
    }
    snap.machines.forEach { m -> FleetRow(m, snap, m.host == selected) { onSelect(m.host) } }
    if (snap.machines.isEmpty()) {
        Text("No machine is reporting right now.", style = MaterialTheme.typography.bodyMedium, color = Color(Palette.DIM))
    } else {
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
    if (archive.isNotEmpty()) {
        SubHeading("Not reporting")
        archive.forEach { k -> ArchiveRow(k, snap.nowMs, k.host == selected) { onSelect(k.host) } }
        Text(
            "The relay no longer lists these. Their history stays on this phone; tap one to chart it.",
            style = MaterialTheme.typography.bodySmall,
            color = Color(Palette.DIM),
        )
    }
}

/** A machine the relay has dropped: calm, not alarming. It is not broken, it is just not here. */
@Composable
private fun ArchiveRow(k: KnownMachine, nowMs: Long, selected: Boolean, onClick: () -> Unit) {
    val ctx = LocalContext.current
    val keepMs = k.lastSeenMs + HistoryConfig.retentionDays(ctx) * DAY_MS - nowMs
    val silentFor = ((nowMs - k.lastSeenMs) / 1000).coerceAtLeast(0)
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
            Text(k.host, style = MaterialTheme.typography.bodyLarge, maxLines = 1, overflow = TextOverflow.Ellipsis)
            Text(
                "Last reading ${Fmt.ago(silentFor)} ago",
                style = MaterialTheme.typography.bodySmall,
                color = Color(Palette.DIM),
                maxLines = 1,
            )
        }
        Text(keptShort(keepMs), style = MaterialTheme.typography.bodySmall, color = Color(Palette.DIM), maxLines = 1)
    }
}

@Composable
private fun FleetRow(m: Machine, s: Snapshot, selected: Boolean, onClick: () -> Unit) {
    val status = liveStatus(m, s)
    val bat = Battery.of(m)
    // A battery level is a live number only while the machine is known to be reporting.
    val tone = toneFor(bat, !status.machineOnline)
    val detail = buildString {
        append(status.text)
        if (status.machineOnline) {
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
                detail,
                style = MaterialTheme.typography.bodySmall,
                color = Color(status.tone),
                maxLines = 2,
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

private const val HOUR_MS = 3_600_000L
private const val DAY_MS = 86_400_000L

private fun plural(n: Long, unit: String) = "$n $unit" + if (n == 1L) "" else "s"

/** Short, for a list row: how much longer the phone keeps a machine's history. */
private fun keptShort(leftMs: Long): String = when {
    leftMs <= 0 -> "expiring"
    leftMs >= DAY_MS -> "kept ${leftMs / DAY_MS}d more"
    leftMs >= HOUR_MS -> "kept ${leftMs / HOUR_MS}h more"
    else -> "kept <1h more"
}

private fun keptSentence(leftMs: Long): String = "Its history stays on this phone for " + when {
    leftMs <= 0 -> "less than an hour more"
    leftMs >= DAY_MS -> plural(leftMs / DAY_MS, "more day")
    leftMs >= HOUR_MS -> plural(leftMs / HOUR_MS, "more hour")
    else -> "less than an hour more"
}

/** What can honestly be said of a machine the relay no longer lists. Not an alarm: nothing is wrong with the phone. */
private fun retiredStatus(lastSeenMs: Long?, nowMs: Long): LiveStatus = LiveStatus(
    if (lastSeenMs == null) "Not reporting"
    else "Not reporting \u00B7 last reading ${Fmt.ago(((nowMs - lastSeenMs) / 1000).coerceAtLeast(0))} ago",
    Palette.DIM, false,
)

/** Stands in for a machine's values while its last stored reading is still being read. */
private fun unknownMachine(host: String) = Machine(
    host = host, ageS = 0, cpu = null, ram = null, storagePct = null, storageTotalGb = null, storageFreeGb = null,
    tempC = null, batteryPct = null, batteryMin = null, charging = false, ac = false,
    rxKbps = null, txKbps = null, uptimeS = null,
)

/**
 * One machine's history. [current] is its live reading, or null when the relay no longer lists it: then the
 * charts show it as it was when it went quiet (its last reading is the right-hand edge, and its last
 * values stand in for the live ones, marked "last"), and the card offers to forget it.
 */
@Composable
private fun MachineHistory(
    host: String,
    current: Machine?,
    lastSeenMs: Long?,
    snap: Snapshot,
    range: HistoryRange,
    onRange: (HistoryRange) -> Unit,
    onForgotten: () -> Unit,
) {
    val ctx = LocalContext.current
    val scope = rememberCoroutineScope()
    val on = HistoryConfig.enabled(ctx)
    val days = HistoryConfig.retentionDays(ctx)
    val ranges = HistoryRange.availableFor(days)
    val r = if (range in ranges) range else ranges.first()

    val retired = current == null
    val stored by produceState<Machine?>(null, host, retired) {
        value = if (retired) {
            withContext(Dispatchers.IO) { runCatching { History.lastReading(ctx, host) }.getOrNull() }
        } else {
            null
        }
    }
    val m = current ?: stored ?: unknownMachine(host)
    val status = if (retired) retiredStatus(lastSeenMs, snap.nowMs) else liveStatus(m, snap)
    var confirmForget by remember { mutableStateOf(false) }

    SectionCard(title = host, subtitle = status.text) {
        Segmented(options = ranges.map { it to it.label }, selected = r, onSelect = onRange)
        if (!on) {
            Text(
                "History logging is off, so there is nothing to chart. Turn it on under Configure.",
                style = MaterialTheme.typography.bodyMedium,
                color = Color(Palette.AMBER),
            )
        }
        if (retired) {
            val keepMs = (lastSeenMs ?: snap.nowMs) + days * DAY_MS - snap.nowMs
            Text(
                "The relay no longer lists this machine, so there is no live reading. ${keptSentence(keepMs)} " +
                    "(change that under Configure, History). Charts show the ${r.label} before it went quiet.",
                style = MaterialTheme.typography.bodySmall,
                color = Color(Palette.DIM),
            )
            OutlinedButton(onClick = { confirmForget = true }, modifier = Modifier.fillMaxWidth()) {
                Text("Forget this machine", color = Color(Palette.RED))
            }
        }
    }

    if (confirmForget) {
        AlertDialog(
            onDismissRequest = { confirmForget = false },
            containerColor = Color(Palette.PANEL),
            titleContentColor = Color(Palette.TEXT),
            textContentColor = Color(Palette.TEXT),
            title = { Text("Forget $host?") },
            text = {
                Text(
                    "Its charts and its availability record are deleted from this phone. If it ever reports " +
                        "again, it starts a fresh history.",
                )
            },
            confirmButton = {
                TextButton(onClick = {
                    confirmForget = false
                    scope.launch {
                        withContext(Dispatchers.IO) { runCatching { History.forget(ctx, host) } }
                        onForgotten()
                    }
                }) { Text("Forget", color = Color(Palette.RED)) }
            },
            dismissButton = { TextButton(onClick = { confirmForget = false }) { Text("Keep") } },
        )
    }
    if (!on) return

    // A machine that is not live is charted ending at its own last reading: "the last hour" of a machine that
    // went quiet three days ago is empty, and an empty chart reads as lost data.
    val endMs = if (retired && lastSeenMs != null) lastSeenMs + ChartMath.bucketMs(r) else null

    // Reload when the machine, the range, or a fresh reading changes. A machine that is not live gets no
    // fresh readings, so it loads once.
    val data by produceState<ChartData?>(null, host, r, if (retired) lastSeenMs else snap.fetchedAtMs) {
        value = withContext(Dispatchers.IO) { runCatching { History.load(ctx, host, r, endMs) }.getOrNull() }
    }
    val d = data
    if (d == null) {
        Text("Loading\u2026", style = MaterialTheme.typography.bodyMedium, color = Color(Palette.DIM))
        return
    }
    // The timeline comes first: it explains the gaps in everything below it.
    if (d.spans.isNotEmpty()) AvailabilityCard(status, d)
    if (d.stats.samples < 2) {
        SectionCard(title = if (retired) "Not enough history" else "Collecting data") {
            Text(
                if (retired) {
                    "Too few readings were logged in this window to draw a chart. Try a longer range."
                } else {
                    "Not enough readings yet to draw a chart. While this screen is open a reading is " +
                        "logged every 15 seconds; in the background the widget logs one every 15 minutes."
                },
                style = MaterialTheme.typography.bodyMedium,
                color = Color(Palette.DIM),
            )
        }
        return
    }

    // What stands in for "now" at the top right of a card: the last value this machine reported.
    val asLast: (String) -> String = { if (retired && it != "--") "last $it" else it }
    val gap = ChartMath.gapMs(d.bucketMs)
    val pct: (Double) -> String = { "${it.roundToInt()}%" }
    // Every chart is shaded where the phone or the machine was out, so a gap always has a reason beside it.
    val outages = d.spans.mapNotNull { sp -> sp.kind.toBand()?.let { Band(it, sp.start, sp.end) } }

    MetricCard(
        title = "CPU", live = asLast(m.cpu?.let { "${Fmt.pct(it)}%" } ?: "--"), accent = Color(Palette.CYAN), d = d,
        series = listOf(ChartSeries("CPU", "", Color(Palette.CYAN), d.buckets.map { it.t to it.cpu })),
        yMin = 0.0, yMax = 100.0, fmt = pct, stats = listOf("" to d.stats.cpu), bands = outages,
    )
    MetricCard(
        title = "Memory", live = asLast(m.ram?.let { "${Fmt.pct(it)}%" } ?: "--"), accent = Color(Palette.MAGENTA), d = d,
        series = listOf(ChartSeries("RAM", "", Color(Palette.MAGENTA), d.buckets.map { it.t to it.ram })),
        yMin = 0.0, yMax = 100.0, fmt = pct, stats = listOf("" to d.stats.ram), bands = outages,
    )
    d.stats.temp?.let { t ->
        val (lo, hi) = ChartMath.axisFor(t.min, t.max)
        MetricCard(
            title = "Temperature", live = asLast(Fmt.temp(m.tempC)), accent = Color(Palette.AMBER), d = d,
            series = listOf(ChartSeries("Temp", "", Color(Palette.AMBER), d.buckets.map { it.t to it.temp })),
            yMin = lo, yMax = hi, fmt = { "${it.roundToInt()}\u00B0" }, stats = listOf("" to t), bands = outages,
        )
    }
    val peak = max(d.stats.rx?.max ?: 0.0, d.stats.tx?.max ?: 0.0)
    MetricCard(
        title = "Network", live = asLast("\u2193 ${Fmt.rate(m.rxKbps)}  \u2191 ${Fmt.rate(m.txKbps)}"), accent = Color(Palette.GREEN), d = d,
        series = listOf(
            ChartSeries("download", "\u2193", Color(Palette.GREEN), d.buckets.map { it.t to it.rx }),
            ChartSeries("upload", "\u2191", Color(Palette.VIOLET), d.buckets.map { it.t to it.tx }),
        ),
        yMin = 0.0, yMax = ChartMath.niceCeil(max(peak * 1.1, 10.0)), fmt = { Fmt.rate(it) },
        stats = listOf("\u2193" to d.stats.rx, "\u2191" to d.stats.tx), bands = outages,
    )
    MetricCard(
        title = "Storage used",
        live = asLast(m.storagePct?.let { "${Fmt.pct(it)}%" + (m.storageFreeGb?.let { f -> " \u00B7 ${Fmt.capacity(f)} free" } ?: "") } ?: "--"),
        accent = Color(Palette.CYAN), d = d,
        series = listOf(ChartSeries("Storage", "", Color(Palette.CYAN), d.buckets.map { it.t to it.storage })),
        yMin = 0.0, yMax = 100.0, fmt = pct, stats = listOf("" to d.stats.storage), bands = outages,
    )
    d.stats.bat?.let { b ->
        val info = Battery.of(m)
        MetricCard(
            title = "Battery",
            live = asLast(if (info.present) "${info.pct}% \u00B7 ${info.tail}" else "--"),
            accent = Color(toneFor(info, false).let { if (it == Palette.DIM) Palette.GREEN else it }), d = d,
            series = listOf(ChartSeries("Battery", "", Color(Palette.GREEN), d.buckets.map { it.t to it.bat })),
            yMin = 0.0, yMax = 100.0, fmt = pct, stats = listOf("" to b),
            bands = outages + ChartMath.flagBands(d.buckets.map { it.t to it.charging }, d.bucketMs / 2, gap)
                .map { (a, b) -> Band(BandKind.CHARGING, a, b) },
            note = "Green shading marks when it was charging.",
        )
    }

    // Anything else this machine reports (a GPU, an NPU, a fan, a power meter...): one card each, with
    // no change to the app when a new kind of sensor appears.
    val tones = listOf(Palette.VIOLET, Palette.AMBER, Palette.GREEN, Palette.MAGENTA, Palette.CYAN)
    d.extras.forEachIndexed { i, x ->
        val st = x.stat ?: return@forEachIndexed
        val live = m.extras.firstOrNull { it.key == x.key }
        val def = Metrics.def(x.key)
        val unit = live?.unit ?: def.unit
        val label = live?.label ?: def.label
        val accent = Color(tones[i % tones.size])
        val (lo, hi) = when (unit) {
            "%" -> 0.0 to 100.0
            "\u00B0C" -> ChartMath.axisFor(st.min, st.max)
            else -> 0.0 to ChartMath.niceCeil(max(st.max * 1.1, 1.0))
        }
        MetricCard(
            title = label,
            live = asLast(live?.let { Metrics.format(it.value, unit) } ?: "--"),
            accent = accent, d = d,
            series = listOf(ChartSeries(label, "", accent, x.points)),
            yMin = lo, yMax = hi, fmt = { Metrics.format(it, unit) },
            stats = listOf("" to st), bands = outages,
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
    bands: List<Band> = emptyList(),
    note: String? = null,
) {
    val ctx = LocalContext.current
    var cursor by remember { mutableStateOf<Long?>(null) }
    val gap = ChartMath.gapMs(d.bucketMs)
    val long = d.range.ms > 86_400_000L

    val under = cursor?.let { t ->
        // Under an outage there is nothing to read; say why instead of "no data".
        val outage = bands.firstOrNull { it.kind != BandKind.CHARGING && t >= it.start && t <= it.end }
        val parts = series.mapNotNull { s ->
            ChartMath.valueAt(s.points, t, gap)?.let { v -> (if (series.size > 1) "${s.tag} " else "") + fmt(v) }
        }
        timeText(t, long) + "  " + when {
            outage != null && parts.isEmpty() -> outage.kind.label()
            parts.isEmpty() -> "no data"
            else -> parts.joinToString("  ")
        }
    }
    val hasOutage = bands.any { it.kind != BandKind.CHARGING }
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
                    Text(if (d.anchored) timeText(d.toMs, long) else "now", style = MaterialTheme.typography.labelSmall, color = dim)
                }
                Text(summary, style = MaterialTheme.typography.bodySmall, color = Color(Palette.DIM))
                if (hasOutage) {
                    Text(
                        "Shaded stretches are outages. Availability above says which kind.",
                        style = MaterialTheme.typography.bodySmall,
                        color = Color(Palette.DIM),
                    )
                }
                if (note != null) Text(note, style = MaterialTheme.typography.bodySmall, color = Color(Palette.DIM))
            }
        }
    }
}

private fun Coverage.toBand(): BandKind? = when (this) {
    Coverage.ONLINE -> null
    Coverage.MACHINE_OFFLINE -> BandKind.MACHINE_OFFLINE
    Coverage.RELAY_DOWN -> BandKind.RELAY_DOWN
    Coverage.PHONE_OFFLINE -> BandKind.PHONE_OFFLINE
}

private fun BandKind.label(): String = when (this) {
    BandKind.CHARGING -> "charging"
    BandKind.MACHINE_OFFLINE -> "machine offline"
    BandKind.RELAY_DOWN -> "relay unreachable"
    BandKind.PHONE_OFFLINE -> "phone had no internet"
}

private data class LegendItem(val kind: Coverage?, val label: String, val share: Double)

/**
 * The one chart that answers "why is there a gap?". Four causes, four looks:
 * green = the machine was reporting; red = it was silent and the relay said so;
 * grey hatching = this phone had no internet, so the machine is simply unknown;
 * amber hatching = the phone was online but the relay did not answer; empty = nobody was looking.
 */
@Composable
private fun AvailabilityCard(live: LiveStatus, d: ChartData) {
    val sh = Availability.shares(d.spans, d.fromMs, d.toMs)
    val long = d.range.ms > 86_400_000L

    fun pct(v: Double) = when {
        v <= 0.0 -> "0%"
        v < 0.01 -> "<1%"
        else -> "${(v * 100).roundToInt()}%"
    }
    val items = listOf(
        LegendItem(Coverage.ONLINE, "Machine online", sh.online),
        LegendItem(Coverage.MACHINE_OFFLINE, "Machine offline", sh.machineOffline),
        LegendItem(Coverage.PHONE_OFFLINE, "Phone offline", sh.phoneOffline),
        LegendItem(Coverage.RELAY_DOWN, "Relay unreachable", sh.relayDown),
        LegendItem(null, "No data", sh.noData),
    ).filter { it.share > 0.0 }
    val description = "Availability over the last ${d.range.label}. " + items.joinToString(". ") { "${it.label} ${pct(it.share)}" }

    Surface(
        color = Color(Palette.PANEL),
        shape = RoundedCornerShape(20.dp),
        border = BorderStroke(1.dp, Color(Palette.TRACK)),
        modifier = Modifier.fillMaxWidth(),
    ) {
        Column(modifier = Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
            Column(verticalArrangement = Arrangement.spacedBy(2.dp)) {
                Text("Availability", style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.SemiBold)
                Text(live.text, style = MaterialTheme.typography.bodySmall, color = Color(live.tone))
            }
            AvailabilityStrip(d, description)
            Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
                val dim = Color(Palette.DIM)
                Text(timeText(d.fromMs, long), style = MaterialTheme.typography.labelSmall, color = dim)
                Text(timeText((d.fromMs + d.toMs) / 2, long), style = MaterialTheme.typography.labelSmall, color = dim)
                Text(if (d.anchored) timeText(d.toMs, long) else "now", style = MaterialTheme.typography.labelSmall, color = dim)
            }
            items.chunked(2).forEach { pair ->
                Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                    pair.forEach { item ->
                        Row(modifier = Modifier.weight(1f), verticalAlignment = Alignment.CenterVertically) {
                            Swatch(item.kind)
                            Text(
                                "  ${item.label}  ${pct(item.share)}",
                                style = MaterialTheme.typography.bodySmall,
                                maxLines = 1,
                                overflow = TextOverflow.Ellipsis,
                            )
                        }
                    }
                    if (pair.size == 1) Box(Modifier.weight(1f))
                }
            }
            // Only the explanations that apply to what is on the chart.
            val dim = Color(Palette.DIM)
            if (sh.phoneOffline > 0.0) {
                Text(
                    "Phone offline: this phone had no internet, so nothing could be fetched. The machine may well have been running.",
                    style = MaterialTheme.typography.bodySmall, color = dim,
                )
            }
            if (sh.machineOffline > 0.0) {
                Text(
                    "Machine offline: the relay was reachable but heard nothing from the machine.",
                    style = MaterialTheme.typography.bodySmall, color = dim,
                )
            }
            if (sh.relayDown > 0.0) {
                Text(
                    "Relay unreachable: the phone was online but the relay did not answer.",
                    style = MaterialTheme.typography.bodySmall, color = dim,
                )
            }
            if (sh.noData > 0.0) {
                Text(
                    "No data: the app was not open and the widget did not get to check.",
                    style = MaterialTheme.typography.bodySmall, color = dim,
                )
            }
        }
    }
}

@Composable
private fun AvailabilityStrip(d: ChartData, description: String) {
    val track = Color(Palette.TRACK)
    val green = Color(Palette.GREEN)
    val red = Color(Palette.RED)
    val amber = Color(Palette.AMBER)
    val dim = Color(Palette.DIM)
    Canvas(
        Modifier
            .fillMaxWidth()
            .height(28.dp)
            .clip(RoundedCornerShape(8.dp))
            .semantics { contentDescription = description },
    ) {
        val w = size.width
        val h = size.height
        val total = (d.toMs - d.fromMs).coerceAtLeast(1L).toFloat()
        val min = 1.5.dp.toPx()
        drawRect(track.copy(alpha = 0.6f))                       // no data: the bare track
        d.spans.forEach { sp ->
            val l = ((sp.start - d.fromMs) / total) * w
            val r = maxOf(((sp.end - d.fromMs) / total) * w, l + min)
            when (sp.kind) {
                Coverage.ONLINE -> drawRect(green.copy(alpha = 0.85f), Offset(l, 0f), Size(r - l, h))
                Coverage.MACHINE_OFFLINE -> drawRect(red.copy(alpha = 0.85f), Offset(l, 0f), Size(r - l, h))
                Coverage.RELAY_DOWN -> {
                    drawRect(amber.copy(alpha = 0.18f), Offset(l, 0f), Size(r - l, h))
                    hatch(l, r, h, amber, 6.dp.toPx(), 1.5.dp.toPx())
                }
                Coverage.PHONE_OFFLINE -> {
                    drawRect(dim.copy(alpha = 0.22f), Offset(l, 0f), Size(r - l, h))
                    hatch(l, r, h, dim, 6.dp.toPx(), 1.5.dp.toPx())
                }
            }
        }
    }
}

/** The legend's little square: drawn the same way as the strip, so the key matches the lock. */
@Composable
private fun Swatch(kind: Coverage?) {
    val green = Color(Palette.GREEN)
    val red = Color(Palette.RED)
    val amber = Color(Palette.AMBER)
    val dim = Color(Palette.DIM)
    val track = Color(Palette.TRACK)
    Canvas(Modifier.size(14.dp).clip(RoundedCornerShape(3.dp))) {
        val w = size.width
        val h = size.height
        when (kind) {
            Coverage.ONLINE -> drawRect(green.copy(alpha = 0.85f))
            Coverage.MACHINE_OFFLINE -> drawRect(red.copy(alpha = 0.85f))
            Coverage.RELAY_DOWN -> {
                drawRect(amber.copy(alpha = 0.18f))
                hatch(0f, w, h, amber, 4.dp.toPx(), 1.2.dp.toPx())
            }
            Coverage.PHONE_OFFLINE -> {
                drawRect(dim.copy(alpha = 0.22f))
                hatch(0f, w, h, dim, 4.dp.toPx(), 1.2.dp.toPx())
            }
            null -> drawRect(track.copy(alpha = 0.6f))
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
            containerColor = Color(Palette.PANEL),
            titleContentColor = Color(Palette.TEXT),
            textContentColor = Color(Palette.TEXT),
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