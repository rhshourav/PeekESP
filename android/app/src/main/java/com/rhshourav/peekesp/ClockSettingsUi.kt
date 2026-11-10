package com.rhshourav.peekesp

import android.text.format.DateFormat
import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.selection.selectable
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import com.rhshourav.peekesp.data.ClockFmt
import com.rhshourav.peekesp.data.ClockZones
import com.rhshourav.peekesp.data.HourFormat
import com.rhshourav.peekesp.data.ZoneCatalog
import com.rhshourav.peekesp.data.ZoneChoice
import com.rhshourav.peekesp.data.ZoneSearch
import com.rhshourav.peekesp.widget.Palette
import kotlinx.coroutines.delay
import java.time.Instant
import java.time.ZoneId
import java.time.format.DateTimeFormatter
import java.time.format.TextStyle
import java.util.Locale

/**
 * What the clock page shows and how it writes the hour. Emits into the caller's column.
 *
 * "My location" is the default and means the phone's own time zone, which Android keeps right from the
 * network or GPS when the user travels. The app asks for no location permission to do this: the system has
 * already worked it out, and reading it costs nothing and reveals nothing.
 */
@Composable
internal fun ClockSettings(
    zoneId: String?,
    hours: HourFormat,
    onZone: (String?) -> Unit,
    onHours: (HourFormat) -> Unit,
) {
    val ctx = LocalContext.current
    var picking by remember { mutableStateOf(false) }

    // The row shows the time there as you look at it, so you can see that you chose the right place.
    var now by remember { mutableStateOf(System.currentTimeMillis()) }
    LaunchedEffect(Unit) {
        while (true) {
            delay(15_000)
            now = System.currentTimeMillis()
        }
    }

    val chosen = ClockZones.resolve(zoneId)
    val shown = chosen ?: ZoneId.systemDefault()
    val is24 = hours.is24(DateFormat.is24HourFormat(ctx))
    val p = ClockFmt.parts(now, shown, Locale.getDefault(), is24)
    val country = chosen?.let { ZoneCatalog.find(it.id)?.country }.orEmpty()
    val delta = ClockZones.deltaLabel(
        ClockZones.deltaMinutes(shown, ZoneId.systemDefault(), Instant.ofEpochMilli(now)),
    )

    SubHeading("Clock region")
    Surface(
        color = Color(Palette.BG),
        shape = RoundedCornerShape(14.dp),
        border = BorderStroke(1.dp, Color(Palette.TRACK)),
        modifier = Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(14.dp))
            .clickable(onClickLabel = "Choose clock region") { picking = true },
    ) {
        Row(
            modifier = Modifier.padding(14.dp),
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.spacedBy(12.dp),
        ) {
            Column(modifier = Modifier.weight(1f), verticalArrangement = Arrangement.spacedBy(2.dp)) {
                Text(
                    if (chosen == null) "My location" else p.place.ifEmpty { chosen.id },
                    style = MaterialTheme.typography.bodyLarge,
                    fontWeight = FontWeight.SemiBold,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis,
                )
                Text(
                    if (chosen == null) "Follows your phone (${p.label})"
                    else listOf(country, p.zone).filter { it.isNotEmpty() }.joinToString(" \u00B7 "),
                    style = MaterialTheme.typography.bodySmall,
                    color = Color(Palette.DIM),
                )
                Text(
                    timeLine(p) + if (chosen == null) "" else " \u00B7 $delta",
                    style = MaterialTheme.typography.bodySmall,
                    fontFamily = FontFamily.Monospace,
                    color = Color(Palette.CYAN),
                )
            }
            Text("Change", style = MaterialTheme.typography.labelLarge, color = Color(Palette.CYAN), fontWeight = FontWeight.Bold)
        }
    }
    if (chosen != null) {
        Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.End) {
            TextButton(onClick = { onZone(null) }) { Text("Use my location") }
        }
    }

    SubHeading("Hours")
    Segmented(
        options = HourFormat.values().map { it to it.label },
        selected = hours,
        onSelect = onHours,
    )
    Text(
        "Phone follows the 12 or 24-hour setting of your phone.",
        style = MaterialTheme.typography.bodySmall,
        color = Color(Palette.DIM),
    )

    if (picking) {
        ZonePickerDialog(
            current = zoneId,
            is24 = is24,
            onPick = { picked ->
                picking = false
                onZone(picked)
            },
            onDismiss = { picking = false },
        )
    }
}

private fun timeLine(p: ClockFmt.Parts): String =
    p.time + (if (p.ampm.isEmpty()) "" else " ${p.ampm}") + " \u00B7 " + p.weekday

private fun offsetLabel(id: String, at: Instant): String =
    runCatching { ClockFmt.utcLabel(ZoneId.of(id).rules.getOffset(at).totalSeconds) }.getOrDefault("")

/**
 * Pick a region by typing a city, a country or an offset. Empty, it offers "My location", the last few
 * regions used, then everywhere A to Z; typing narrows it to the best matches. Each row shows the time
 * there now and how far it is from you, which is what a person is actually trying to decide.
 */
@Composable
private fun ZonePickerDialog(
    current: String?,
    is24: Boolean,
    onPick: (String?) -> Unit,
    onDismiss: () -> Unit,
) {
    val ctx = LocalContext.current
    val at = remember { Instant.now() }
    val here = remember { ZoneId.systemDefault() }
    var query by rememberSaveable { mutableStateOf("") }
    val all = remember { ZoneCatalog.all }
    val recent = remember { ZoneCatalog.recents(ctx) }
    val results = remember(query) {
        if (query.isBlank()) emptyList() else ZoneSearch.search(query, all, { offsetLabel(it.id, at) })
    }
    fun pick(z: ZoneChoice) {
        ZoneCatalog.noteUsed(ctx, z.id)
        onPick(z.id)
    }

    AlertDialog(
        onDismissRequest = onDismiss,
        containerColor = Color(Palette.PANEL),
        titleContentColor = Color(Palette.TEXT),
        textContentColor = Color(Palette.TEXT),
        title = { Text("Clock region") },
        text = {
            Column(verticalArrangement = Arrangement.spacedBy(10.dp)) {
                OutlinedTextField(
                    value = query,
                    onValueChange = { query = it.take(40) },
                    label = { Text("City, country or UTC offset") },
                    singleLine = true,
                    keyboardOptions = KeyboardOptions(imeAction = ImeAction.Search),
                    keyboardActions = KeyboardActions(onSearch = { results.firstOrNull()?.let { pick(it) } }),
                    modifier = Modifier.fillMaxWidth(),
                )
                LazyColumn(modifier = Modifier.fillMaxWidth().heightIn(max = 340.dp)) {
                    if (query.isBlank()) {
                        item(key = "mine") {
                            PickRow(
                                title = "My location",
                                detail = "Follows your phone, including when you travel",
                                time = null,
                                selected = current == null,
                                onClick = { onPick(null) },
                            )
                        }
                        if (recent.isNotEmpty()) {
                            item(key = "h-recent") { ListHeading("Recent") }
                            items(recent, key = { "r:" + it.id }) { z -> ZoneItem(z, at, here, is24, z.id == current) { pick(z) } }
                        }
                        item(key = "h-all") { ListHeading("Everywhere, A to Z") }
                        items(all, key = { "a:" + it.id }) { z -> ZoneItem(z, at, here, is24, z.id == current) { pick(z) } }
                    } else {
                        if (results.isEmpty()) {
                            item(key = "none") {
                                Text(
                                    "Nothing matches \u201C${query.trim()}\u201D. Try a city, a country, or an offset like UTC+6.",
                                    style = MaterialTheme.typography.bodyMedium,
                                    color = Color(Palette.DIM),
                                    modifier = Modifier.padding(vertical = 8.dp),
                                )
                            }
                        }
                        items(results, key = { "s:" + it.id }) { z -> ZoneItem(z, at, here, is24, z.id == current) { pick(z) } }
                    }
                }
            }
        },
        confirmButton = {},
        dismissButton = { TextButton(onClick = onDismiss) { Text("Cancel") } },
    )
}

@Composable
private fun ListHeading(text: String) {
    Text(
        text,
        style = MaterialTheme.typography.labelLarge,
        color = Color(Palette.CYAN),
        modifier = Modifier.padding(top = 10.dp, bottom = 2.dp),
    )
}

@Composable
private fun ZoneItem(z: ZoneChoice, at: Instant, here: ZoneId, is24: Boolean, selected: Boolean, onClick: () -> Unit) {
    val zone = remember(z.id) { ClockZones.resolve(z.id) } ?: return
    val local = at.atZone(zone)
    val clock = DateTimeFormatter.ofPattern(if (is24) "HH:mm" else "h:mm a", Locale.getDefault()).format(local)
    // A different day over there is worth saying: "11:40 PM Tue" is not the same as "11:40 PM" today.
    val day = if (local.toLocalDate() != at.atZone(here).toLocalDate()) {
        " " + local.dayOfWeek.getDisplayName(TextStyle.SHORT, Locale.getDefault())
    } else {
        ""
    }
    val delta = ClockZones.deltaLabel(ClockZones.deltaMinutes(zone, here, at))
    PickRow(
        title = z.city,
        detail = listOf(z.country, offsetLabel(z.id, at)).filter { it.isNotEmpty() }.joinToString(" \u00B7 "),
        time = "$clock$day \u00B7 $delta",
        selected = selected,
        onClick = onClick,
    )
}

/** One choice. Selected is shown with a tick as well as the colour, and read out as selected. */
@Composable
private fun PickRow(title: String, detail: String, time: String?, selected: Boolean, onClick: () -> Unit) {
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .heightIn(min = 56.dp)
            .clip(RoundedCornerShape(10.dp))
            .selectable(selected = selected, role = Role.RadioButton, onClick = onClick)
            .padding(horizontal = 6.dp, vertical = 6.dp),
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(10.dp),
    ) {
        Column(modifier = Modifier.weight(1f)) {
            Text(
                title,
                style = MaterialTheme.typography.bodyLarge,
                fontWeight = if (selected) FontWeight.Bold else FontWeight.Normal,
                maxLines = 1,
                overflow = TextOverflow.Ellipsis,
            )
            if (detail.isNotEmpty()) {
                Text(detail, style = MaterialTheme.typography.bodySmall, color = Color(Palette.DIM), maxLines = 1, overflow = TextOverflow.Ellipsis)
            }
            if (time != null) {
                Text(time, style = MaterialTheme.typography.bodySmall, color = Color(Palette.CYAN), maxLines = 1, overflow = TextOverflow.Ellipsis)
            }
        }
        if (selected) Text("\u2713", style = MaterialTheme.typography.titleMedium, color = Color(Palette.CYAN), fontWeight = FontWeight.Bold)
    }
}
