package com.rhshourav.peekesp

import android.app.TimePickerDialog
import android.content.ClipData
import android.content.ClipDescription
import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
import android.os.Build
import android.os.PersistableBundle
import android.provider.Settings
import android.text.format.DateFormat
import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.selection.toggleable
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Surface
import androidx.compose.material3.Switch
import androidx.compose.material3.SwitchDefaults
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.rhshourav.peekesp.data.AlertConfig
import com.rhshourav.peekesp.data.AlertDefaults
import com.rhshourav.peekesp.data.AlertRunner
import com.rhshourav.peekesp.data.AlertSettings
import com.rhshourav.peekesp.data.EventKind
import com.rhshourav.peekesp.data.Machine
import com.rhshourav.peekesp.data.MetricDef
import com.rhshourav.peekesp.data.Metrics
import com.rhshourav.peekesp.data.NewEvent
import com.rhshourav.peekesp.data.Notifier
import com.rhshourav.peekesp.data.Op
import com.rhshourav.peekesp.data.QuietHours
import com.rhshourav.peekesp.data.Rule
import com.rhshourav.peekesp.data.Severity
import com.rhshourav.peekesp.widget.Palette
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import java.text.SimpleDateFormat
import java.util.Calendar
import java.util.Locale

private fun trimNum(v: Double): String = if (v == Math.rint(v)) v.toLong().toString() else v.toString()

private fun timeLabel(minutes: Int): String {
    val cal = Calendar.getInstance().apply {
        set(Calendar.HOUR_OF_DAY, minutes / 60)
        set(Calendar.MINUTE, minutes % 60)
    }
    val pattern = DateFormat.getBestDateTimePattern(Locale.getDefault(), "jm")
    return SimpleDateFormat(pattern, Locale.getDefault()).format(cal.time)
}

private val DAY_NAMES = listOf("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")

private fun daysLabel(days: Int): String = when (days and QuietHours.ALL_DAYS) {
    QuietHours.ALL_DAYS -> "every day"
    0b0011111 -> "weekdays"
    0b1100000 -> "weekends"
    0 -> "no days"
    else -> DAY_NAMES.filterIndexed { i, _ -> days and (1 shl i) != 0 }.joinToString(", ") { it.take(3) }
}

private fun summary(r: Rule): String {
    val def = Metrics.def(r.metric)
    val what = when (r.op) {
        Op.UNUSUAL -> "${def.label} is unusually high (sensitivity ${trimNum(r.threshold)})"
        else -> "${def.label} ${r.op.label} ${Metrics.format(r.threshold, def.unit)}"
    }
    val dur = if (r.forMinutes > 0) " for ${r.forMinutes} min" else ""
    return what + dur + " \u00B7 " + r.severity.label + (r.machine?.let { " \u00B7 $it" } ?: "")
}

/**
 * Configure, Alerts page: everything about when and how loudly the phone speaks up.
 * Settings are saved the moment they change; there is no Save button to forget.
 */
@Composable
internal fun AlertSettingsPanel(machines: List<Machine>, foreground: Boolean, notify: (String) -> Unit) {
    val ctx = LocalContext.current
    val scope = rememberCoroutineScope()
    var s by remember { mutableStateOf(AlertConfig.load(ctx)) }
    fun update(n: AlertSettings) {
        s = n
        AlertConfig.save(ctx, n)
    }

    // Coming back from the system's settings page flips `foreground`, which re-checks the permission.
    var canPost by remember { mutableStateOf(Notifier.canPost(ctx)) }
    LaunchedEffect(foreground) { canPost = Notifier.canPost(ctx) }
    val enable = rememberNotificationEnabler()

    fun test(sev: Severity) {
        scope.launch(Dispatchers.IO) {
            AlertRunner.send(
                ctx,
                NewEvent(
                    key = "test:${System.nanoTime()}", t = System.currentTimeMillis(), severity = sev,
                    title = "Test ${sev.label.lowercase()} alert",
                    body = when (sev) {
                        Severity.INFO -> "This is how a quiet message looks."
                        Severity.WARNING -> "This should make a sound."
                        Severity.CRITICAL -> "This should ring like an alarm until you silence it."
                    },
                    source = "Test", machine = null, kind = EventKind.MESSAGE,
                ),
            )
        }
    }

    // ============================ Notifications ============================
    SectionCard(
        title = "Notifications",
        subtitle = "Info is quiet, Warning makes a sound, Critical rings like an alarm until you silence it.",
    ) {
        ToggleRow(
            title = "Alerts",
            subtitle = "Tell me when machines go offline, rules trip, or something sends a message.",
            checked = s.enabled,
            onChange = { update(s.copy(enabled = it)); if (it && !canPost) enable() },
        )
        if (!canPost) {
            Text(
                "This phone is not letting PeekESP show notifications. Alerts will still appear in the Alerts tab.",
                style = MaterialTheme.typography.bodySmall,
                color = Color(Palette.AMBER),
            )
            Button(onClick = enable, modifier = Modifier.fillMaxWidth()) { Text("Allow notifications") }
        }
        SubHeading("Try them")
        Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            OutlinedButton(onClick = { test(Severity.INFO) }, modifier = Modifier.weight(1f)) { Text("Info", maxLines = 1) }
            OutlinedButton(onClick = { test(Severity.WARNING) }, modifier = Modifier.weight(1f)) { Text("Warning", maxLines = 1) }
            OutlinedButton(onClick = { test(Severity.CRITICAL) }, modifier = Modifier.weight(1f)) {
                Text("Critical", maxLines = 1, color = Color(Palette.RED))
            }
        }
        OutlinedButton(
            onClick = {
                Notifier.ensureChannels(ctx)
                ctx.startActivity(
                    Intent(Settings.ACTION_CHANNEL_NOTIFICATION_SETTINGS)
                        .putExtra(Settings.EXTRA_APP_PACKAGE, ctx.packageName)
                        .putExtra(Settings.EXTRA_CHANNEL_ID, Notifier.CH_CRITICAL)
                        .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK),
                )
            },
            modifier = Modifier.fillMaxWidth(),
        ) { Text("Critical sound and Do Not Disturb\u2026") }
        Text(
            "Critical alerts use the alarm volume, so they sound even on silent. To let them through Do Not " +
                "Disturb as well, open that page and switch on \u201COverride Do Not Disturb\u201D.",
            style = MaterialTheme.typography.bodySmall,
            color = Color(Palette.DIM),
        )
        Text(
            "While the app is closed your phone is checked about every 15 minutes, the fastest Android allows, " +
                "so a background alert can be that late. With the app open it is within seconds.",
            style = MaterialTheme.typography.bodySmall,
            color = Color(Palette.DIM),
        )
    }

    // ============================ Machine status ============================
    SectionCard(title = "Machine status", subtitle = "When a machine goes quiet, or comes back.") {
        ToggleRow(
            title = "When a machine goes offline",
            subtitle = "After it has been silent for the time below.",
            checked = s.notifyOffline,
            onChange = { update(s.copy(notifyOffline = it)) },
        )
        ToggleRow(
            title = "When it comes back",
            subtitle = "A quiet note: no sound.",
            checked = s.notifyOnline,
            onChange = { update(s.copy(notifyOnline = it)) },
        )
        SubHeading("Count a machine as offline after")
        Segmented(
            options = listOf(1, 2, 5, 10).map { it to "$it min" },
            selected = s.offlineAfterMin,
            onSelect = { update(s.copy(offlineAfterMin = it)) },
        )
        Text(
            "Only said when the relay answered. If this phone has no internet you hear nothing, because " +
                "then the machine is unknown, not off.",
            style = MaterialTheme.typography.bodySmall,
            color = Color(Palette.DIM),
        )
        SubHeading("Notify me about")
        if (machines.isEmpty()) {
            Text(
                "Machines appear here once they report.",
                style = MaterialTheme.typography.bodyMedium,
                color = Color(Palette.DIM),
            )
        }
        machines.forEach { m ->
            val muted = m.host in s.mutedMachines
            ToggleRow(
                title = m.host,
                subtitle = if (muted) "Muted: still listed in Alerts, never notified." else "Notifications on.",
                checked = !muted,
                onChange = { on -> update(s.copy(mutedMachines = if (on) s.mutedMachines - m.host else s.mutedMachines + m.host)) },
            )
        }
    }

    // ============================ Quiet hours ============================
    SectionCard(title = "Quiet hours", subtitle = "Hold notifications while you sleep. They still land in Alerts.") {
        val q = s.quiet
        ToggleRow(
            title = "Quiet hours",
            subtitle = if (q.enabled) "${timeLabel(q.startMin)} to ${timeLabel(q.endMin)}, ${daysLabel(q.days)}." else "Off.",
            checked = q.enabled,
            onChange = { update(s.copy(quiet = q.copy(enabled = it))) },
        )
        if (q.enabled) {
            val is24 = DateFormat.is24HourFormat(ctx)
            fun pick(current: Int, onPick: (Int) -> Unit) {
                TimePickerDialog(ctx, { _, h, m -> onPick(h * 60 + m) }, current / 60, current % 60, is24).show()
            }
            Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                OutlinedButton(
                    onClick = { pick(q.startMin) { update(s.copy(quiet = q.copy(startMin = it))) } },
                    modifier = Modifier.weight(1f),
                ) { Text("From ${timeLabel(q.startMin)}", maxLines = 1) }
                OutlinedButton(
                    onClick = { pick(q.endMin) { update(s.copy(quiet = q.copy(endMin = it))) } },
                    modifier = Modifier.weight(1f),
                ) { Text("To ${timeLabel(q.endMin)}", maxLines = 1) }
            }
            if (q.startMin == q.endMin) {
                Text(
                    "Start and end are the same, which is no window at all. Pick two different times.",
                    style = MaterialTheme.typography.bodySmall,
                    color = Color(Palette.AMBER),
                )
            }
            SubHeading("Days it starts on")
            Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                listOf("M", "T", "W", "T", "F", "S", "S").forEachIndexed { i, label ->
                    val on = q.days and (1 shl i) != 0
                    Box(
                        contentAlignment = Alignment.Center,
                        modifier = Modifier
                            .weight(1f)
                            .heightIn(min = 44.dp)
                            .clip(RoundedCornerShape(10.dp))
                            .background(if (on) Color(Palette.CYAN) else Color(Palette.BG))
                            .semantics { contentDescription = DAY_NAMES[i] }
                            .toggleable(
                                value = on, role = Role.Checkbox,
                                onValueChange = { update(s.copy(quiet = q.copy(days = q.days xor (1 shl i)))) },
                            ),
                    ) {
                        Text(
                            label,
                            fontWeight = if (on) FontWeight.Bold else FontWeight.Medium,
                            color = if (on) Color(Palette.ON_ACCENT) else Color(Palette.TEXT),
                        )
                    }
                }
            }
            ToggleRow(
                title = "Critical alerts still ring",
                subtitle = "Recommended: this is how you hear about a real emergency at 3 a.m.",
                checked = q.criticalBreaksThrough,
                onChange = { update(s.copy(quiet = q.copy(criticalBreaksThrough = it))) },
            )
        }
    }

    // ============================ Rules ============================
    var editing by remember { mutableStateOf<Rule?>(null) }
    var creating by remember { mutableStateOf(false) }
    val known: List<MetricDef> = (
        Metrics.BASE +
            machines.flatMap { it.extras }.distinctBy { it.key }.map { MetricDef(it.key, it.label, it.unit) } +
            s.rules.map { Metrics.def(it.metric) }
        ).distinctBy { it.key }

    SectionCard(
        title = "Rules",
        subtitle = "What to watch, how long it must last, and how loudly to say so.",
    ) {
        s.rules.forEach { r ->
            Row(
                modifier = Modifier
                    .fillMaxWidth()
                    .clip(RoundedCornerShape(12.dp))
                    .clickable(onClickLabel = "Edit rule") { editing = r }
                    .padding(vertical = 6.dp, horizontal = 4.dp),
                verticalAlignment = Alignment.CenterVertically,
                horizontalArrangement = Arrangement.spacedBy(12.dp),
            ) {
                Column(modifier = Modifier.weight(1f)) {
                    Text(r.name, style = MaterialTheme.typography.bodyLarge)
                    Text(summary(r), style = MaterialTheme.typography.bodySmall, color = Color(Palette.DIM))
                }
                Switch(
                    checked = r.enabled,
                    onCheckedChange = { on -> update(s.copy(rules = s.rules.map { if (it.id == r.id) it.copy(enabled = on) else it })) },
                    colors = SwitchDefaults.colors(uncheckedTrackColor = Color(Palette.TRACK)),
                )
            }
        }
        Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            Button(onClick = { creating = true }, modifier = Modifier.weight(1f)) { Text("Add a rule", maxLines = 1) }
            OutlinedButton(
                onClick = { update(s.copy(rules = AlertDefaults.rules())); notify("Default rules restored.") },
                modifier = Modifier.weight(1f),
            ) { Text("Restore defaults", maxLines = 1) }
        }
        Text(
            "\u201CUnusually high\u201D rules learn what is normal for each machine from its history, so they " +
                "need History logging on and about six hours of data before they can speak.",
            style = MaterialTheme.typography.bodySmall,
            color = Color(Palette.DIM),
        )
        Text(
            "Machines can report more than the standard sensors (a GPU, an NPU, a fan): once one does, " +
                "it appears in the list of things you can watch.",
            style = MaterialTheme.typography.bodySmall,
            color = Color(Palette.DIM),
        )
    }

    editing?.let { r ->
        RuleDialog(
            initial = r, isNew = false, metrics = known, machines = machines.map { it.host },
            onSave = { n -> update(s.copy(rules = s.rules.map { if (it.id == n.id) n else it })); editing = null },
            onDelete = { update(s.copy(rules = s.rules.filter { it.id != r.id })); editing = null },
            onDismiss = { editing = null },
        )
    }
    if (creating) {
        RuleDialog(
            initial = Rule(
                id = "rule-" + System.currentTimeMillis().toString(36), name = "", enabled = true,
                metric = "cpu_percent", op = Op.ABOVE, threshold = 90.0, forMinutes = 5,
                severity = Severity.WARNING, machine = null,
            ),
            isNew = true, metrics = known, machines = machines.map { it.host },
            onSave = { n -> update(s.copy(rules = s.rules + n)); creating = false },
            onDelete = null,
            onDismiss = { creating = false },
        )
    }

    // ============================ Other apps ============================
    SectionCard(
        title = "Other apps",
        subtitle = "Let a security tool, Tasker or a script put messages in front of you through PeekESP.",
    ) {
        ToggleRow(
            title = "Allow other apps to send messages",
            subtitle = "They need the key below. Off, and nothing gets in.",
            checked = s.externalEnabled,
            onChange = { on ->
                update(s.copy(externalEnabled = on, externalToken = if (on && s.externalToken.isEmpty()) AlertConfig.newToken() else s.externalToken))
            },
        )
        if (s.externalEnabled && s.externalToken.isNotEmpty()) {
            var show by remember { mutableStateOf(false) }
            Surface(
                color = Color(Palette.BG),
                shape = RoundedCornerShape(14.dp),
                border = BorderStroke(1.dp, Color(Palette.TRACK)),
                modifier = Modifier.fillMaxWidth().semantics { contentDescription = if (show) "Key: ${s.externalToken.toList().joinToString(" ")}" else "Key, hidden" },
            ) {
                Text(
                    if (show) s.externalToken.chunked(4).joinToString("-") else "\u2022\u2022\u2022\u2022-\u2022\u2022\u2022\u2022-\u2022\u2022\u2022\u2022-\u2022\u2022\u2022\u2022-\u2022\u2022\u2022\u2022",
                    fontFamily = FontFamily.Monospace,
                    fontWeight = FontWeight.Bold,
                    fontSize = 16.sp,
                    modifier = Modifier.padding(14.dp),
                )
            }
            Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                OutlinedButton(onClick = { show = !show }, modifier = Modifier.weight(1f)) { Text(if (show) "Hide" else "Show") }
                OutlinedButton(
                    onClick = { copySecret(ctx, "PeekESP key", s.externalToken); notify("Key copied.") },
                    modifier = Modifier.weight(1f),
                ) { Text("Copy") }
                OutlinedButton(
                    onClick = { update(s.copy(externalToken = AlertConfig.newToken())); notify("New key made. The old one no longer works.") },
                    modifier = Modifier.weight(1f),
                ) { Text("New key", maxLines = 1) }
            }
            Surface(
                color = Color(Palette.BG),
                shape = RoundedCornerShape(14.dp),
                border = BorderStroke(1.dp, Color(Palette.TRACK)),
                modifier = Modifier.fillMaxWidth(),
            ) {
                Text(
                    "adb shell am broadcast \\\n" +
                        "  -a com.rhshourav.peekesp.action.SEND_MESSAGE \\\n" +
                        "  -p com.rhshourav.peekesp \\\n" +
                        "  --es token <KEY> \\\n" +
                        "  --es severity critical \\\n" +
                        "  --es title \"Intrusion\" \\\n" +
                        "  --es message \"New login\"",
                    fontFamily = FontFamily.Monospace,
                    fontSize = 11.sp,
                    modifier = Modifier.padding(12.dp),
                )
            }
            Text(
                "Extras: token (required), title, message, severity (info, warning, critical or red), source, " +
                    "machine, id. Send the same id twice and it counts once. At most 20 a minute. The sender " +
                    "must name this app (-p) or Android will not deliver it.",
                style = MaterialTheme.typography.bodySmall,
                color = Color(Palette.DIM),
            )
        }
        Text(
            "Machines can send messages and extra sensors themselves, inside each reading. PROTOCOL.md describes how.",
            style = MaterialTheme.typography.bodySmall,
            color = Color(Palette.DIM),
        )
    }
}

private fun copySecret(ctx: Context, label: String, text: String) {
    val cm = ctx.getSystemService(ClipboardManager::class.java) ?: return
    val clip = ClipData.newPlainText(label, text)
    if (Build.VERSION.SDK_INT >= 33) {
        // Keeps it out of the keyboard's clipboard preview.
        clip.description.extras = PersistableBundle().apply { putBoolean(ClipDescription.EXTRA_IS_SENSITIVE, true) }
    }
    cm.setPrimaryClip(clip)
}

/** A button that opens a menu: the stable way to pick one of many without an experimental API. */
@Composable
private fun <T> Picker(label: String, current: String, options: List<Pair<T, String>>, onPick: (T) -> Unit) {
    var open by remember { mutableStateOf(false) }
    Column {
        SubHeading(label)
        Box {
            OutlinedButton(onClick = { open = true }, modifier = Modifier.fillMaxWidth()) { Text("$current  \u25BE", maxLines = 1) }
            DropdownMenu(expanded = open, onDismissRequest = { open = false }) {
                options.forEach { (value, text) ->
                    DropdownMenuItem(text = { Text(text) }, onClick = { onPick(value); open = false })
                }
            }
        }
    }
}

@Composable
private fun RuleDialog(
    initial: Rule,
    isNew: Boolean,
    metrics: List<MetricDef>,
    machines: List<String>,
    onSave: (Rule) -> Unit,
    onDelete: (() -> Unit)?,
    onDismiss: () -> Unit,
) {
    var name by remember { mutableStateOf(initial.name) }
    var metric by remember { mutableStateOf(initial.metric) }
    var op by remember { mutableStateOf(initial.op) }
    var threshold by remember { mutableStateOf(trimNum(initial.threshold)) }
    var minutes by remember { mutableStateOf(initial.forMinutes) }
    var severity by remember { mutableStateOf(initial.severity) }
    var machine by remember { mutableStateOf(initial.machine) }
    var title by remember { mutableStateOf(initial.title) }
    var body by remember { mutableStateOf(initial.body) }

    val value = threshold.trim().toDoubleOrNull()?.takeIf { !it.isNaN() && !it.isInfinite() }
    val def = Metrics.def(metric)

    AlertDialog(
        onDismissRequest = onDismiss,
        containerColor = Color(Palette.PANEL),
        titleContentColor = Color(Palette.TEXT),
        textContentColor = Color(Palette.TEXT),
        title = { Text(if (isNew) "New rule" else "Edit rule") },
        text = {
            Column(modifier = Modifier.verticalScroll(rememberScrollState()), verticalArrangement = Arrangement.spacedBy(10.dp)) {
                OutlinedTextField(
                    value = name, onValueChange = { name = it.take(40) }, label = { Text("Name") },
                    singleLine = true, modifier = Modifier.fillMaxWidth(),
                )
                Picker("Watch", def.label, metrics.map { it.key to it.label }) { metric = it }
                SubHeading("When it")
                Segmented(
                    options = listOf(Op.ABOVE to "Is above", Op.BELOW to "Is below", Op.UNUSUAL to "Unusual"),
                    selected = op,
                    onSelect = {
                        // A limit and a sensitivity are different numbers: do not carry 90 over as "90 spreads".
                        if (it == Op.UNUSUAL && op != Op.UNUSUAL) threshold = "6"
                        if (it != Op.UNUSUAL && op == Op.UNUSUAL) threshold = "90"
                        op = it
                    },
                )
                OutlinedTextField(
                    value = threshold,
                    onValueChange = { threshold = it.take(10) },
                    label = { Text(if (op == Op.UNUSUAL) "Sensitivity (higher means fewer alerts)" else "Limit${if (def.unit.isEmpty()) "" else " (${def.unit})"}") },
                    isError = value == null,
                    supportingText = { if (value == null) Text("Enter a number") },
                    singleLine = true,
                    keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Decimal),
                    modifier = Modifier.fillMaxWidth(),
                )
                SubHeading("For at least")
                Segmented(
                    options = listOf(0, 1, 2, 5, 10, 30).map { it to if (it == 0) "Now" else "$it" },
                    selected = minutes,
                    onSelect = { minutes = it },
                )
                Text(
                    if (minutes == 0) "Alert the moment it is true." else "Alert only if it stays true for $minutes minutes.",
                    style = MaterialTheme.typography.bodySmall, color = Color(Palette.DIM),
                )
                SubHeading("Severity")
                Segmented(options = Severity.values().map { it to it.label }, selected = severity, onSelect = { severity = it })
                Picker(
                    "Applies to", machine ?: "All machines",
                    listOf<Pair<String?, String>>(null to "All machines") + machines.map { it to it },
                ) { machine = it }
                SubHeading("Your wording (optional)")
                OutlinedTextField(
                    value = title, onValueChange = { title = it.take(80) }, label = { Text("Title") },
                    singleLine = true, modifier = Modifier.fillMaxWidth(),
                )
                OutlinedTextField(
                    value = body, onValueChange = { body = it.take(200) }, label = { Text("Message") },
                    modifier = Modifier.fillMaxWidth(),
                )
                Text(
                    "You can put {machine} {metric} {value} {threshold} {normal} {duration} {rule} in either; " +
                        "they are filled in when it trips. Leave both empty for the default wording.",
                    style = MaterialTheme.typography.bodySmall, color = Color(Palette.DIM),
                )
            }
        },
        confirmButton = {
            TextButton(
                enabled = value != null && name.isNotBlank(),
                onClick = {
                    onSave(
                        initial.copy(
                            name = name.trim(), metric = metric, op = op, threshold = value ?: 0.0, forMinutes = minutes,
                            severity = severity, machine = machine, title = title.trim(), body = body.trim(),
                        ),
                    )
                },
            ) { Text("Save") }
        },
        dismissButton = {
            Row {
                if (onDelete != null) TextButton(onClick = onDelete) { Text("Delete", color = Color(Palette.RED)) }
                TextButton(onClick = onDismiss) { Text("Cancel") }
            }
        },
    )
}