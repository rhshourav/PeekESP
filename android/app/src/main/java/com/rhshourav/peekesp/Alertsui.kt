package com.rhshourav.peekesp

import android.Manifest
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.provider.Settings
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.IntrinsicSize
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.produceState
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import com.rhshourav.peekesp.data.AlarmPlayer
import com.rhshourav.peekesp.data.AlertBus
import com.rhshourav.peekesp.data.AlertConfig
import com.rhshourav.peekesp.data.AlertRunner
import com.rhshourav.peekesp.data.AlertStore
import com.rhshourav.peekesp.data.Event
import com.rhshourav.peekesp.data.EventKind
import com.rhshourav.peekesp.data.Fmt
import com.rhshourav.peekesp.data.NewEvent
import com.rhshourav.peekesp.data.Notifier
import com.rhshourav.peekesp.data.Severity
import com.rhshourav.peekesp.widget.Palette
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.text.SimpleDateFormat
import java.time.ZonedDateTime
import java.util.Date
import java.util.Locale

/** Info is cyan, warning amber, critical red. Never the only cue: every one also says its name. */
internal fun severityColor(s: Severity): Color = Color(
    when (s) {
        Severity.CRITICAL -> Palette.RED
        Severity.WARNING -> Palette.AMBER
        Severity.INFO -> Palette.CYAN
    },
)

/**
 * Asks for permission to show notifications. Android 13+ shows the system prompt the first time;
 * once the user has said no, asking again does nothing, so from then on this opens the
 * settings page where they can change their mind.
 */
@Composable
internal fun rememberNotificationEnabler(): () -> Unit {
    val ctx = LocalContext.current
    val launcher = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) { }
    return {
        val prefs = ctx.getSharedPreferences("peek_alerts_ui", Context.MODE_PRIVATE)
        val denied = Build.VERSION.SDK_INT >= 33 &&
            ctx.checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED
        if (denied && !prefs.getBoolean("asked", false)) {
            prefs.edit().putBoolean("asked", true).apply()
            launcher.launch(Manifest.permission.POST_NOTIFICATIONS)
        } else {
            ctx.startActivity(
                Intent(Settings.ACTION_APP_NOTIFICATION_SETTINGS)
                    .putExtra(Settings.EXTRA_APP_PACKAGE, ctx.packageName)
                    .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK),
            )
        }
    }
}

private fun relTime(t: Long, now: Long): String {
    val s = ((now - t) / 1000).coerceAtLeast(0)
    return when {
        s < 60 -> "just now"
        s < 3600 -> "${s / 60}m ago"
        s < 86_400 -> "${s / 3600}h ago"
        s < 7 * 86_400 -> "${s / 86_400}d ago"
        else -> SimpleDateFormat("d MMM", Locale.getDefault()).format(Date(t))
    }
}

/** The Alerts tab: what needs you, then everything else, newest first. Emits into the caller's column. */
@Composable
internal fun AlertsPanel(onOpenConfigure: () -> Unit) {
    val ctx = LocalContext.current
    val scope = rememberCoroutineScope()
    val version = AlertBus.version.value
    val settings = remember(version) { AlertConfig.load(ctx) }
    val events by produceState(emptyList<Event>(), version) {
        value = withContext(Dispatchers.IO) { runCatching { AlertStore.recent(ctx) }.getOrDefault(emptyList()) }
    }
    var filter by rememberSaveable { mutableStateOf("ALL") }
    var composing by remember { mutableStateOf(false) }
    var confirmClear by remember { mutableStateOf(false) }
    val enable = rememberNotificationEnabler()
    val now = System.currentTimeMillis()

    val unread = events.count { !it.read }
    val critical = events.filter { it.severity == Severity.CRITICAL && !it.read }

    // ---- things the user should know about the alerting itself ----
    if (!settings.enabled) {
        InfoBanner(
            "Alerts are switched off, so nothing new is being checked.",
            Palette.AMBER, "Open settings", onOpenConfigure,
        )
    } else if (!Notifier.canPost(ctx)) {
        InfoBanner(
            "Notifications are off, so alerts only appear here.",
            Palette.AMBER, "Turn on", enable,
        )
    }
    if (settings.quiet.contains(ZonedDateTime.now())) {
        InfoBanner(
            "Quiet hours are on: notifications are being held back" +
                if (settings.quiet.criticalBreaksThrough) ", except critical ones." else ".",
            Palette.CYAN, null, {},
        )
    }
    if (critical.isNotEmpty()) {
        InfoBanner(
            if (critical.size == 1) "1 critical alert needs you." else "${critical.size} critical alerts need you.",
            Palette.RED, "Silence",
        ) {
            scope.launch(Dispatchers.IO) {
                critical.forEach {
                    AlertStore.markRead(ctx, it.id)
                    Notifier.cancel(ctx, it.id)
                }
                AlarmPlayer.stop()
                AlertBus.bump()
            }
        }
    }

    SectionCard(title = "Alerts", subtitle = if (unread == 0) "Nothing unread." else "$unread unread.") {
        Segmented(
            options = listOf("ALL" to "All", "CRITICAL" to "Critical", "WARNING" to "Warning", "INFO" to "Info"),
            selected = filter,
            onSelect = { filter = it },
        )
        Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            OutlinedButton(
                onClick = { scope.launch(Dispatchers.IO) { AlertStore.markAllRead(ctx); AlarmPlayer.stop(); AlertBus.bump() } },
                enabled = unread > 0,
                modifier = Modifier.weight(1f),
            ) { Text("Mark all read", maxLines = 1) }
            OutlinedButton(
                onClick = { confirmClear = true },
                enabled = events.isNotEmpty(),
                modifier = Modifier.weight(1f),
            ) { Text("Clear", maxLines = 1) }
            Button(onClick = { composing = true }, modifier = Modifier.weight(1f)) { Text("Send", maxLines = 1) }
        }
    }

    val shown = events.filter { filter == "ALL" || it.severity.name == filter }.take(100)
    if (shown.isEmpty()) {
        Text(
            if (events.isEmpty()) {
                "Nothing yet. This is where you will see machines going offline or coming back, rules " +
                    "you set tripping, and messages from your machines or other apps. Use Send to try one."
            } else {
                "Nothing of that kind."
            },
            style = MaterialTheme.typography.bodyMedium,
            color = Color(Palette.DIM),
        )
    }
    shown.forEach { e ->
        EventCard(e, now) {
            if (!e.read) scope.launch(Dispatchers.IO) { AlertStore.markRead(ctx, e.id); Notifier.cancel(ctx, e.id); AlertBus.bump() }
        }
    }

    if (composing) ComposeDialog(onDismiss = { composing = false })

    if (confirmClear) {
        AlertDialog(
            onDismissRequest = { confirmClear = false },
            containerColor = Color(Palette.PANEL),
            titleContentColor = Color(Palette.TEXT),
            textContentColor = Color(Palette.TEXT),
            title = { Text("Clear all alerts?") },
            text = { Text("They are removed from this list. Rules keep running and will raise new ones.") },
            confirmButton = {
                TextButton(onClick = {
                    confirmClear = false
                    scope.launch(Dispatchers.IO) { AlertStore.clear(ctx); AlarmPlayer.stop(); AlertBus.bump() }
                }) { Text("Clear", color = Color(Palette.RED)) }
            },
            dismissButton = { TextButton(onClick = { confirmClear = false }) { Text("Keep") } },
        )
    }
}

@Composable
private fun InfoBanner(text: String, tone: Int, action: String?, onAction: () -> Unit) {
    val c = Color(tone)
    Surface(
        color = c.copy(alpha = 0.10f),
        shape = RoundedCornerShape(14.dp),
        border = BorderStroke(1.dp, c.copy(alpha = 0.45f)),
        modifier = Modifier.fillMaxWidth(),
    ) {
        Row(
            modifier = Modifier.padding(start = 14.dp, end = 6.dp, top = 6.dp, bottom = 6.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Text(text, style = MaterialTheme.typography.bodyMedium, modifier = Modifier.weight(1f).padding(vertical = 6.dp))
            if (action != null) TextButton(onClick = onAction) { Text(action, color = c, fontWeight = FontWeight.Bold) }
        }
    }
}

@Composable
private fun EventCard(e: Event, now: Long, onClick: () -> Unit) {
    val tone = severityColor(e.severity)
    val where = listOfNotNull(e.machine, e.source.takeIf { it.isNotEmpty() && it != e.machine }).joinToString(" \u00B7 ")
    Surface(
        color = Color(Palette.PANEL),
        shape = RoundedCornerShape(16.dp),
        border = BorderStroke(1.dp, if (e.read) Color(Palette.TRACK) else tone.copy(alpha = 0.5f)),
        modifier = Modifier
            .fillMaxWidth()
            .semantics(mergeDescendants = true) {
                contentDescription = "${e.severity.label}${if (e.read) "" else ", unread"}. ${e.title}. ${e.body}"
            }
            .clickable(onClickLabel = if (e.read) null else "Mark as read", onClick = onClick),
    ) {
        Row(modifier = Modifier.height(IntrinsicSize.Min)) {
            Box(Modifier.width(5.dp).fillMaxHeight().background(tone))
            Column(modifier = Modifier.padding(14.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
                Row(modifier = Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                    Text(
                        e.severity.label.uppercase(),
                        style = MaterialTheme.typography.labelSmall,
                        fontWeight = FontWeight.Bold,
                        color = tone,
                    )
                    if (!e.read) {
                        Box(Modifier.padding(start = 8.dp).size(7.dp).background(tone, CircleShape))
                    }
                    Box(Modifier.weight(1f))
                    Text(relTime(e.t, now), style = MaterialTheme.typography.labelSmall, color = Color(Palette.DIM))
                }
                Text(
                    e.title,
                    style = MaterialTheme.typography.titleSmall,
                    fontWeight = if (e.read) FontWeight.Normal else FontWeight.Bold,
                )
                if (e.body.isNotEmpty()) Text(e.body, style = MaterialTheme.typography.bodyMedium)
                if (where.isNotEmpty()) {
                    Text(
                        where,
                        style = MaterialTheme.typography.bodySmall,
                        color = Color(Palette.DIM),
                        maxLines = 1,
                        overflow = TextOverflow.Ellipsis,
                    )
                }
            }
        }
    }
}

/**
 * Write a message to yourself: shows in the list and as a notification at the severity you pick,
 * exactly as one from a machine or another app would. Useful for a reminder, and for hearing what
 * critical sounds like before you need to.
 */
@Composable
private fun ComposeDialog(onDismiss: () -> Unit) {
    val ctx = LocalContext.current
    val scope = rememberCoroutineScope()
    var title by remember { mutableStateOf("") }
    var body by remember { mutableStateOf("") }
    var severity by remember { mutableStateOf(Severity.INFO) }

    AlertDialog(
        onDismissRequest = onDismiss,
        containerColor = Color(Palette.PANEL),
        titleContentColor = Color(Palette.TEXT),
        textContentColor = Color(Palette.TEXT),
        title = { Text("Send a message") },
        text = {
            Column(verticalArrangement = Arrangement.spacedBy(10.dp)) {
                Segmented(
                    options = Severity.values().map { it to it.label },
                    selected = severity,
                    onSelect = { severity = it },
                )
                Text(
                    when (severity) {
                        Severity.INFO -> "A quiet line."
                        Severity.WARNING -> "A sound and a popup."
                        Severity.CRITICAL -> "Rings like an alarm until you silence it."
                    },
                    style = MaterialTheme.typography.bodySmall,
                    color = Color(Palette.DIM),
                )
                OutlinedTextField(
                    value = title, onValueChange = { title = it.take(80) }, label = { Text("Title") },
                    singleLine = true, modifier = Modifier.fillMaxWidth(),
                )
                OutlinedTextField(
                    value = body, onValueChange = { body = it.take(400) }, label = { Text("Message (optional)") },
                    modifier = Modifier.fillMaxWidth(),
                )
            }
        },
        confirmButton = {
            TextButton(
                enabled = title.isNotBlank() || body.isNotBlank(),
                onClick = {
                    val t = title.trim()
                    val b = body.trim()
                    scope.launch(Dispatchers.IO) {
                        AlertRunner.send(
                            ctx,
                            NewEvent(
                                key = "you:${System.nanoTime()}", t = System.currentTimeMillis(), severity = severity,
                                title = t.ifEmpty { b.take(60) }, body = if (t.isEmpty()) "" else b,
                                source = "You", machine = null, kind = EventKind.MESSAGE,
                            ),
                        )
                    }
                    onDismiss()
                },
            ) { Text("Send") }
        },
        dismissButton = { TextButton(onClick = onDismiss) { Text("Cancel") } },
    )
}