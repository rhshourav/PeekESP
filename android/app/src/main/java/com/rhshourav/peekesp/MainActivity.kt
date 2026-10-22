package com.rhshourav.peekesp

import android.appwidget.AppWidgetManager
import android.app.Activity
import android.content.ActivityNotFoundException
import android.content.ClipData
import android.content.ClipDescription
import android.content.ClipboardManager
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.graphics.drawable.ColorDrawable
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.PersistableBundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ColumnScope
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.safeDrawingPadding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.selection.selectable
import androidx.compose.foundation.selection.selectableGroup
import androidx.compose.foundation.selection.toggleable
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.Badge
import androidx.compose.material3.BadgedBox
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ColorScheme
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.LocalContentColor
import androidx.compose.material3.NavigationBar
import androidx.compose.material3.NavigationBarItem
import androidx.compose.material3.NavigationBarItemDefaults
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Scaffold
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Slider
import androidx.compose.material3.SliderDefaults
import androidx.compose.material3.Switch
import androidx.compose.material3.SwitchDefaults
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.SideEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.produceState
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.drawBehind
import androidx.compose.ui.geometry.CornerRadius
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalView
import androidx.compose.ui.semantics.LiveRegionMode
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.semantics.clearAndSetSemantics
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.liveRegion
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.semantics.stateDescription
import androidx.compose.ui.text.AnnotatedString
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.input.KeyboardCapitalization
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.OffsetMapping
import androidx.compose.ui.text.input.TransformedText
import androidx.compose.ui.text.input.VisualTransformation
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.core.view.WindowCompat
import com.rhshourav.peekesp.data.AlarmPlayer
import com.rhshourav.peekesp.data.AlertBus
import com.rhshourav.peekesp.data.AlertRunner
import com.rhshourav.peekesp.data.AlertStore
import com.rhshourav.peekesp.data.AppTheme
import com.rhshourav.peekesp.data.Battery
import com.rhshourav.peekesp.data.BatteryLevel
import com.rhshourav.peekesp.data.FetchResult
import com.rhshourav.peekesp.data.Fmt
import com.rhshourav.peekesp.data.History
import com.rhshourav.peekesp.data.LinkKind
import com.rhshourav.peekesp.data.Net
import com.rhshourav.peekesp.data.Notifier
import com.rhshourav.peekesp.data.ThemeConfig
import com.rhshourav.peekesp.data.WidgetTheme
import com.rhshourav.peekesp.data.HistoryRange
import com.rhshourav.peekesp.data.Freshness
import com.rhshourav.peekesp.data.Machine
import com.rhshourav.peekesp.data.Pairing
import com.rhshourav.peekesp.data.Parser
import com.rhshourav.peekesp.data.PowerState
import com.rhshourav.peekesp.data.RelayClient
import com.rhshourav.peekesp.data.RelayConfig
import com.rhshourav.peekesp.data.Setup
import com.rhshourav.peekesp.data.SetupStore
import com.rhshourav.peekesp.data.StyleStore
import com.rhshourav.peekesp.data.WidgetCache
import com.rhshourav.peekesp.data.WidgetStyle
import com.rhshourav.peekesp.widget.Frame
import com.rhshourav.peekesp.widget.Link
import com.rhshourav.peekesp.widget.PageKind
import com.rhshourav.peekesp.widget.Palette
import com.rhshourav.peekesp.widget.PeekWidgetReceiver
import com.rhshourav.peekesp.widget.RefreshWorker
import com.rhshourav.peekesp.widget.WidgetRenderer
import com.rhshourav.peekesp.widget.redrawWidgets
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import kotlin.math.roundToInt

/** Credit and project home. */
private const val AUTHOR = "rhshourav"
private const val REPO_LABEL = "github.com/rhshourav/PeekESP"
private const val REPO_URL = "https://github.com/rhshourav/PeekESP"

class MainActivity : ComponentActivity() {
    /** True between onStart and onStop. The live dashboard only polls while the app can be seen. */
    private val foreground = mutableStateOf(false)

    /** Set when the app is opened from one of its notifications, so it can land on Alerts. */
    private val openAlerts = mutableStateOf(false)

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        if (intent.getBooleanExtra(Notifier.EXTRA_OPEN_ALERTS, false)) openAlerts.value = true
    }

    override fun onStart() {
        super.onStart()
        foreground.value = true
    }

    override fun onStop() {
        foreground.value = false
        super.onStop()
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        // Installs that predate the no-network-constraint schedule pick the new one up here.
        if (SetupStore(this).has()) RefreshWorker.schedule(this)
        openAlerts.value = intent?.getBooleanExtra(Notifier.EXTRA_OPEN_ALERTS, false) == true

        setContent {
            var themeMode by remember { mutableStateOf(ThemeConfig.get(this@MainActivity)) }
            val dark = when (themeMode) {
                AppTheme.SYSTEM -> isSystemInDarkTheme()
                AppTheme.LIGHT -> false
                AppTheme.DARK -> true
            }
            // Before anything below reads a colour, so the first frame is already in the right mode.
            Palette.setAppDark(dark)

            MaterialTheme(colorScheme = peekColorScheme(dark)) {
                SystemBars(dark, window)
                // Without a Surface, Text falls back to LocalContentColor's default (black):
                // unreadable on a dark background.
                Surface(color = MaterialTheme.colorScheme.background) {
                    Screen(
                        foreground = foreground.value,
                        themeMode = themeMode,
                        onTheme = {
                            themeMode = it
                            ThemeConfig.set(this@MainActivity, it)
                        },
                        jumpToAlerts = openAlerts.value,
                        onJumped = { openAlerts.value = false },
                    )
                }
            }
        }
    }
}

/** Material's colours from ours. Every role the app relies on is set, so none falls back to a stock purple. */
private fun peekColorScheme(dark: Boolean): ColorScheme {
    val accent = Color(Palette.CYAN)
    val onAccent = Color(Palette.ON_ACCENT)
    val bg = Color(Palette.BG)
    val text = Color(Palette.TEXT)
    val panel = Color(Palette.PANEL)
    val dim = Color(Palette.DIM)
    val track = Color(Palette.TRACK)
    val error = Color(Palette.RED)
    return if (dark) {
        darkColorScheme(
            primary = accent, onPrimary = onAccent, background = bg, onBackground = text,
            surface = panel, onSurface = text, surfaceVariant = track, onSurfaceVariant = dim,
            outline = dim, outlineVariant = track, error = error,
        )
    } else {
        lightColorScheme(
            primary = accent, onPrimary = onAccent, background = bg, onBackground = text,
            surface = panel, onSurface = text, surfaceVariant = track, onSurfaceVariant = dim,
            outline = dim, outlineVariant = track, error = error,
        )
    }
}

/** Status and navigation bar icons that stay readable in both modes, and a window that never flashes the wrong colour. */
@Composable
private fun SystemBars(dark: Boolean, window: android.view.Window) {
    val view = LocalView.current
    SideEffect {
        window.setBackgroundDrawable(ColorDrawable(Palette.BG))
        val controller = WindowCompat.getInsetsController(window, view)
        controller.isAppearanceLightStatusBars = !dark
        controller.isAppearanceLightNavigationBars = !dark
    }
}

// ----------------------------------------------------------------------------
//  State
// ----------------------------------------------------------------------------

/** Two ways in: type the code a device already shows, or make one for a new setup. */
private enum class Mode { ENTER, CREATE }

private enum class Tone { INFO, OK, WARN, ERROR }

private data class Notice(val text: String, val tone: Tone = Tone.INFO)

private fun Tone.color(): Color = Color(
    when (this) {
        Tone.INFO -> Palette.CYAN
        Tone.OK -> Palette.GREEN
        Tone.WARN -> Palette.AMBER
        Tone.ERROR -> Palette.RED
    },
)

private enum class Tab { DASHBOARD, ALERTS, CONFIGURE }

/** Configure has a lot in it: the everyday settings, and everything about alerts. */
private enum class ConfigPage { GENERAL, ALERTS }

@Composable
private fun Screen(
    foreground: Boolean,
    themeMode: AppTheme,
    onTheme: (AppTheme) -> Unit,
    jumpToAlerts: Boolean,
    onJumped: () -> Unit,
) {
    val ctx = LocalContext.current
    val store = remember { SetupStore(ctx) }
    val cache = remember { WidgetCache(ctx) }
    val styleStore = remember { StyleStore(ctx) }
    val scope = rememberCoroutineScope()

    var setup by remember { mutableStateOf(store.load().firstOrNull()) }
    var mode by rememberSaveable { mutableStateOf(Mode.ENTER) }
    var input by rememberSaveable { mutableStateOf("") }
    var draft by rememberSaveable { mutableStateOf(Pairing.generate()) }
    var notice by remember { mutableStateOf<Notice?>(null) }
    var busy by remember { mutableStateOf(false) }
    var revealed by remember { mutableStateOf(false) }
    var confirmForget by remember { mutableStateOf(false) }
    var showRelay by rememberSaveable { mutableStateOf(false) }
    var relay by remember { mutableStateOf(RelayConfig.get(ctx)) }

    var tab by rememberSaveable { mutableStateOf(Tab.DASHBOARD) }
    var configPage by rememberSaveable { mutableStateOf(ConfigPage.GENERAL) }

    // What the Alerts tab's badge counts, and which critical ones are still unanswered.
    val unreadCount by produceState(0, AlertBus.version.value) {
        value = withContext(Dispatchers.IO) { runCatching { AlertStore.unreadCount(ctx) }.getOrDefault(0) }
    }
    val unreadCritical by produceState(emptyList<Long>(), AlertBus.version.value) {
        value = withContext(Dispatchers.IO) { runCatching { AlertStore.unreadCritical(ctx).map { it.id } }.getOrDefault(emptyList()) }
    }
    var selected by rememberSaveable { mutableStateOf<String?>(null) }
    var range by rememberSaveable { mutableStateOf(HistoryRange.H1) }
    var snapshot by remember { mutableStateOf(readSnapshot(ctx)) }

    /**
     * One path for both modes. The difference is what a bad answer means:
     *  - ENTER: the code came from a person, so an unverifiable one is not saved.
     *  - CREATE: the code came from us, so there is no typo to catch. It is saved
     *    even offline and the worker retries; "nothing pushing yet" is simply the
     *    expected state of a brand-new code.
     */
    fun connect(code: String, created: Boolean) {
        if (busy) return
        busy = true
        notice = Notice("Checking the relay\u2026")
        scope.launch {
            val keys = Pairing.derive(code)
            val base = RelayConfig.get(ctx)
            val result = withContext(Dispatchers.IO) { RelayClient.fetch(base, keys) }
            val now = System.currentTimeMillis()

            fun save() {
                store.save(listOf(Setup("My setup", base, keys.code)))
                setup = store.load().firstOrNull()
                input = ""
                revealed = created
                RefreshWorker.schedule(ctx)
            }

            notice = when (result) {
                is FetchResult.Ok -> {
                    save()
                    cache.saveOk(result.raw, result.machines.size, result.latencyMs, now)
                    val n = result.machines.size
                    Notice(
                        if (created) "Code saved. $n machine(s) are already pushing to it."
                        else "Found $n machine(s). Add the widget below.",
                        Tone.OK,
                    )
                }
                FetchResult.Empty -> {
                    save()
                    cache.saveOk("{}", 0, 0, now)
                    if (created) {
                        Notice(
                            "Code saved. Enter it on the device or agent you want to watch. " +
                                "Readings appear as soon as it pushes.",
                            Tone.OK,
                        )
                    } else {
                        // The relay claims a token on first use, so it cannot tell these apart.
                        Notice(
                            "Nothing is pushing to this code yet. That is expected if no agent is " +
                                "installed, and it is also exactly what a typo looks like.",
                            Tone.WARN,
                        )
                    }
                }
                FetchResult.AuthRejected -> {
                    if (created) draft = Pairing.generate()   // a collision: take another
                    Notice(
                        if (created) "That code was already taken, so a new one was made. Try again."
                        else "The relay rejected this code's token. Check it for typos.",
                        Tone.ERROR,
                    )
                }
                is FetchResult.Failed -> {
                    if (created) {
                        save()
                        RefreshWorker.refreshNow(ctx)
                        Notice("Code saved. The relay is unreachable right now; the widget will retry.", Tone.WARN)
                    } else {
                        Notice("Could not reach the relay: ${result.reason}", Tone.ERROR)
                    }
                }
            }
            redrawWidgets(ctx)
            busy = false
        }
    }

    // The live loop. While the app can be seen and a code is paired, ask the relay every 5 s
    // (the rate the devices push at), keep the widget's cache fresh, and log every reading.
    LaunchedEffect(setup?.code, foreground, relay) {
        val code = setup?.code ?: return@LaunchedEffect
        if (!foreground) return@LaunchedEffect
        val keys = Pairing.derive(code)
        while (true) {
            val base = RelayConfig.get(ctx)
            val result = withContext(Dispatchers.IO) { RelayClient.fetch(base, keys) }
            val now = System.currentTimeMillis()
            // The log needs to know how the phone's own path is doing as well as what the machines said,
            // or a chart cannot tell a dead phone signal from a machine that went quiet.
            snapshot = when (result) {
                is FetchResult.Ok -> {
                    cache.saveOk(result.raw, result.machines.size, result.latencyMs, now)
                    withContext(Dispatchers.IO) {
                        runCatching {
                            History.record(ctx, result.machines, now)
                            History.recordLink(ctx, LinkKind.OK, now)
                        }
                        // Separate, so a history problem never stops an alert.
                        runCatching { AlertRunner.process(ctx, result.machines, now) }
                    }
                    Snapshot(result.machines, now, now, LinkState.LIVE, result.latencyMs)
                }
                FetchResult.Empty -> {
                    cache.saveOk("{}", 0, 0, now)
                    withContext(Dispatchers.IO) { runCatching { History.recordLink(ctx, LinkKind.OK, now) } }
                    Snapshot(emptyList(), now, now, LinkState.LIVE)
                }
                FetchResult.AuthRejected -> {
                    cache.saveAuthRejected()
                    // The relay answered, so the connection is fine; the code is what is wrong.
                    withContext(Dispatchers.IO) { runCatching { History.recordLink(ctx, LinkKind.OK, now) } }
                    snapshot.copy(nowMs = now, link = LinkState.REJECTED)
                }
                is FetchResult.Failed -> {
                    // Same failure, two very different causes. Ask the system which.
                    val kind = Net.failureKind(ctx)
                    withContext(Dispatchers.IO) { runCatching { History.recordLink(ctx, kind, now) } }
                    snapshot.copy(
                        nowMs = now,
                        link = if (kind == LinkKind.PHONE_OFFLINE) LinkState.PHONE_OFFLINE else LinkState.RELAY_DOWN,
                        error = result.reason,
                    )
                }
            }
            delay(5_000)
        }
    }

    // Opened from a notification: land where the news is.
    LaunchedEffect(jumpToAlerts) {
        if (jumpToAlerts) {
            tab = Tab.ALERTS
            onJumped()
        }
    }

    // A critical alert is still unanswered. Normally its notification is what rings. This is for the one
    // case with nothing else to make a noise: the app is open and the phone will not show notifications.
    LaunchedEffect(unreadCritical, foreground) {
        if (unreadCritical.isEmpty() || !foreground) AlarmPlayer.stop()
        else if (!Notifier.canPost(ctx)) AlarmPlayer.ring(ctx)
    }

    // Leaving the app: the cache is now newer than the widget, so redraw it once.
    LaunchedEffect(foreground) { if (!foreground) redrawWidgets(ctx) }

    val current = setup
    // The machine being charted: the one picked, or the first if that one is gone.
    val sel = selected?.takeIf { h -> snapshot.machines.any { it.host == h } } ?: snapshot.machines.firstOrNull()?.host
    val chip = when {
        current == null -> ChipState("Not paired", Palette.AMBER)
        snapshot.link == LinkState.LIVE -> ChipState("Live", Palette.GREEN)
        snapshot.link == LinkState.PHONE_OFFLINE -> ChipState("No internet", Palette.AMBER)
        snapshot.link == LinkState.RELAY_DOWN -> ChipState("Relay unreachable", Palette.RED)
        snapshot.link == LinkState.REJECTED -> ChipState("Code rejected", Palette.RED)
        else -> ChipState("Connecting", Palette.DIM)
    }
    // One scroll position per tab, so switching back finds you where you were.
    val dashScroll = rememberScrollState()
    val configScroll = rememberScrollState()
    val alertsScroll = rememberScrollState()

    Scaffold(
        containerColor = Color(Palette.BG),
        bottomBar = { BottomTabs(tab, unreadCount) { tab = it } },
    ) { inner ->
        Box(modifier = Modifier.fillMaxSize().padding(inner), contentAlignment = Alignment.TopCenter) {
        Column(
            modifier = Modifier
                .widthIn(max = 640.dp)
                .fillMaxWidth()
                .verticalScroll(
                    when (tab) {
                        Tab.DASHBOARD -> dashScroll
                        Tab.ALERTS -> alertsScroll
                        Tab.CONFIGURE -> configScroll
                    },
                )
                .padding(horizontal = 20.dp, vertical = 24.dp),
            verticalArrangement = Arrangement.spacedBy(16.dp),
        ) {
            if (tab == Tab.DASHBOARD) {
                // ================= Dashboard =================
                Header("PeekESP", "Live stats from your machines", chip)
                notice?.let { NoticeBanner(it) }

                if (current == null) {
                    SectionCard(title = "Connect a machine", subtitle = "Pair this phone with a device or agent.") {
                        Segmented(
                            options = listOf(Mode.ENTER to "I have a code", Mode.CREATE to "Create a code"),
                            selected = mode,
                            enabled = !busy,
                            onSelect = { mode = it; notice = null },
                        )
                        when (mode) {
                            Mode.ENTER -> EnterCodeForm(
                                input = input,
                                busy = busy,
                                onInput = { input = Pairing.normalise(it).take(Pairing.CODE_LEN) },
                                onSubmit = { if (Pairing.isValid(input)) connect(input, created = false) },
                            )
                            Mode.CREATE -> CreateCodeForm(
                                code = draft,
                                busy = busy,
                                onRegenerate = { draft = Pairing.generate() },
                                onCopy = { copyCode(ctx, draft); notice = Notice("Code copied.", Tone.OK) },
                                onShare = { shareCode(ctx, draft) },
                                onUse = { connect(draft, created = true) },
                            )
                        }
                    }
                    Text(
                        "Running your own relay? Set its address under Configure before you pair.",
                        style = MaterialTheme.typography.bodySmall,
                        color = Color(Palette.DIM),
                    )
                } else {
                    DashboardPanel(
                        snap = snapshot,
                        selected = sel,
                        onSelect = { selected = it },
                        range = range,
                        onRange = { range = it },
                    )
                }
            } else if (tab == Tab.ALERTS) {
                // ================= Alerts =================
                Header("Alerts", "Machines, rules and messages that need you", null)
                notice?.let { NoticeBanner(it) }
                AlertsPanel(onOpenConfigure = { configPage = ConfigPage.ALERTS; tab = Tab.CONFIGURE })
            } else {
                // ================= Configure =================
                Header("Configure", "Appearance, widget, history, alerts and connection", null)
                notice?.let { NoticeBanner(it) }
                Segmented(
                    options = listOf(ConfigPage.GENERAL to "General", ConfigPage.ALERTS to "Alerts"),
                    selected = configPage,
                    onSelect = { configPage = it },
                )

                if (configPage == ConfigPage.GENERAL) {

                SectionCard(title = "Appearance", subtitle = "How the app looks. The widget has its own setting below.") {
                    Segmented(
                        options = AppTheme.values().map { it to it.label },
                        selected = themeMode,
                        onSelect = onTheme,
                    )
                }

                SectionCard(
                    title = "Widget",
                    subtitle = "Changes reach your home screen automatically.",
                ) {
                    WidgetPanel(store = styleStore, onApply = { scope.launch { redrawWidgets(ctx) } })
                }

                SectionCard(
                    title = "History",
                    subtitle = "What the dashboard charts are built from.",
                ) {
                    HistoryPanel(notify = { notice = Notice(it, Tone.OK) })
                }

                SectionCard(
                    title = if (current == null) "Setup" else "Your setup",
                    subtitle = if (current == null) "Not paired yet." else null,
                ) {
                    if (current == null) {
                        Text(
                            "Pair this phone on the Dashboard to see your machines and fill the widget.",
                            style = MaterialTheme.typography.bodyMedium,
                        )
                        OutlinedButton(onClick = { tab = Tab.DASHBOARD }, modifier = Modifier.fillMaxWidth()) {
                            Text("Go to Dashboard")
                        }
                    } else {
                        PairedPanel(
                            code = current.code,
                            revealed = revealed,
                            onToggleReveal = { revealed = !revealed },
                            onCopy = { copyCode(ctx, current.code); notice = Notice("Code copied.", Tone.OK) },
                            onShare = { shareCode(ctx, current.code) },
                            onRefresh = {
                                RefreshWorker.refreshNow(ctx)
                                notice = Notice("Refreshing the widget\u2026")
                            },
                            onForget = { confirmForget = true },
                        )
                    }
                }

                SectionCard(
                    title = "Relay",
                    subtitle = "The server your phone and your agents meet at.",
                ) {
                    RelayPanel(relay = relay, custom = relay != RelayConfig.DEFAULT_BASE, onEdit = { showRelay = true })
                }

                AboutFooter(ctx)
                } else {
                    AlertSettingsPanel(
                        machines = snapshot.machines,
                        foreground = foreground,
                        notify = { notice = Notice(it, Tone.OK) },
                    )
                }
            }
        }
        }
    }

    if (confirmForget) {
        AlertDialog(
            onDismissRequest = { confirmForget = false },
            containerColor = Color(Palette.PANEL),
            titleContentColor = Color(Palette.TEXT),
            textContentColor = Color(Palette.TEXT),
            title = { Text("Forget this setup?") },
            text = {
                Text(
                    "The code is removed from this phone and the widget goes blank. " +
                        "Your devices keep their copy, so you can enter the same code again later."
                )
            },
            confirmButton = {
                TextButton(onClick = {
                    confirmForget = false
                    store.clear(); cache.clear(); RefreshWorker.cancel(ctx)
                    setup = null
                    revealed = false
                    draft = Pairing.generate()
                    notice = Notice("Setup forgotten.")
                    scope.launch { redrawWidgets(ctx) }
                }) { Text("Forget", color = Color(Palette.RED)) }
            },
            dismissButton = { TextButton(onClick = { confirmForget = false }) { Text("Keep") } },
        )
    }

    if (showRelay) {
        RelayDialog(
            onDismiss = { showRelay = false },
            onChanged = { newBase ->
                relay = newBase
                notice = Notice(
                    if (newBase == RelayConfig.DEFAULT_BASE) "Using the default relay."
                    else "Relay changed. Make sure your agents push to it too.",
                    Tone.OK,
                )
            },
        )
    }
}

// ----------------------------------------------------------------------------
//  Layout building blocks
// ----------------------------------------------------------------------------

private data class ChipState(val label: String, val color: Int)

@Composable
private fun Header(title: String, subtitle: String, chip: ChipState?) {
    Row(
        modifier = Modifier.fillMaxWidth(),
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.SpaceBetween,
    ) {
        Column {
            Text(
                title,
                style = MaterialTheme.typography.headlineMedium,
                fontWeight = FontWeight.Bold,
                color = Color(Palette.CYAN),
            )
            Text(
                subtitle,
                style = MaterialTheme.typography.bodyMedium,
                color = Color(Palette.DIM),
            )
        }
        if (chip != null) StatusChip(chip)
    }
}

@Composable
private fun StatusChip(chip: ChipState) {
    val tone = Color(chip.color)
    Row(
        verticalAlignment = Alignment.CenterVertically,
        modifier = Modifier
            .background(tone.copy(alpha = 0.14f), RoundedCornerShape(50))
            .padding(horizontal = 12.dp, vertical = 6.dp),
    ) {
        Box(Modifier.size(8.dp).background(tone, CircleShape))
        Spacer(Modifier.width(8.dp))
        Text(chip.label, style = MaterialTheme.typography.labelLarge, color = tone)
    }
}

private enum class TabGlyph { DASHBOARD, ALERTS, CONFIGURE }

@Composable
private fun BottomTabs(tab: Tab, unread: Int, onSelect: (Tab) -> Unit) {
    val cyan = Color(Palette.CYAN)
    val colors = NavigationBarItemDefaults.colors(
        selectedIconColor = cyan, selectedTextColor = cyan,
        indicatorColor = cyan.copy(alpha = 0.16f),
        unselectedIconColor = Color(Palette.DIM), unselectedTextColor = Color(Palette.DIM),
    )
    NavigationBar(containerColor = Color(Palette.PANEL)) {
        NavigationBarItem(
            selected = tab == Tab.DASHBOARD, onClick = { onSelect(Tab.DASHBOARD) },
            icon = { TabIcon(TabGlyph.DASHBOARD) }, label = { Text("Dashboard") }, colors = colors,
        )
        NavigationBarItem(
            selected = tab == Tab.ALERTS, onClick = { onSelect(Tab.ALERTS) },
            icon = {
                BadgedBox(
                    badge = {
                        if (unread > 0) {
                            Badge(containerColor = Color(Palette.RED), contentColor = Color.White) {
                                Text(if (unread > 99) "99+" else "$unread")
                            }
                        }
                    },
                    modifier = Modifier.semantics { contentDescription = if (unread > 0) "$unread unread" else "" },
                ) { TabIcon(TabGlyph.ALERTS) }
            },
            label = { Text("Alerts") }, colors = colors,
        )
        NavigationBarItem(
            selected = tab == Tab.CONFIGURE, onClick = { onSelect(Tab.CONFIGURE) },
            icon = { TabIcon(TabGlyph.CONFIGURE) }, label = { Text("Configure") }, colors = colors,
        )
    }
}

/** Drawn, not imported: no icon library for three glyphs. Takes the colour the bar gives its icons. */
@Composable
private fun TabIcon(glyph: TabGlyph) {
    val c = LocalContentColor.current
    Canvas(Modifier.size(24.dp)) {
        val s = size.width
        when (glyph) {
            TabGlyph.DASHBOARD -> {
                val gap = s * 0.12f
                val cell = (s - gap) / 2f
                for (i in 0..1) for (j in 0..1) {
                    drawRoundRect(c, Offset(i * (cell + gap), j * (cell + gap)), Size(cell, cell), CornerRadius(s * 0.1f))
                }
            }
            TabGlyph.ALERTS -> {
                // A bell: a dome, a flared skirt, and the clapper.
                val bell = Path().apply {
                    moveTo(s * 0.50f, s * 0.10f)
                    cubicTo(s * 0.27f, s * 0.10f, s * 0.25f, s * 0.32f, s * 0.25f, s * 0.50f)
                    lineTo(s * 0.13f, s * 0.70f)
                    lineTo(s * 0.87f, s * 0.70f)
                    lineTo(s * 0.75f, s * 0.50f)
                    cubicTo(s * 0.75f, s * 0.32f, s * 0.73f, s * 0.10f, s * 0.50f, s * 0.10f)
                    close()
                }
                drawPath(bell, c)
                drawCircle(c, s * 0.10f, Offset(s * 0.50f, s * 0.84f))
            }
            TabGlyph.CONFIGURE -> {
                val ys = floatArrayOf(0.2f, 0.5f, 0.8f)
                val knobs = floatArrayOf(0.66f, 0.30f, 0.70f)
                for (i in 0..2) {
                    val y = ys[i] * s
                    drawLine(c, Offset(s * 0.08f, y), Offset(s * 0.92f, y), strokeWidth = s * 0.09f, cap = StrokeCap.Round)
                    drawCircle(c, s * 0.13f, Offset(knobs[i] * s, y))
                }
            }
        }
    }
}

/** Status line under the header. TalkBack announces changes without the user hunting for them. */
@Composable
private fun NoticeBanner(n: Notice) {
    val tone = n.tone.color()
    Surface(
        color = tone.copy(alpha = 0.10f),
        shape = RoundedCornerShape(14.dp),
        border = BorderStroke(1.dp, tone.copy(alpha = 0.45f)),
        modifier = Modifier.fillMaxWidth().semantics { liveRegion = LiveRegionMode.Polite },
    ) {
        Text(
            n.text,
            style = MaterialTheme.typography.bodyMedium,
            color = Color(Palette.TEXT),
            modifier = Modifier.padding(horizontal = 14.dp, vertical = 12.dp),
        )
    }
}

/** Every section looks the same: a card, a title, an optional one-line explanation, then content. */
@Composable
internal fun SectionCard(
    title: String,
    subtitle: String? = null,
    content: @Composable ColumnScope.() -> Unit,
) {
    Surface(
        color = Color(Palette.PANEL),
        shape = RoundedCornerShape(20.dp),
        border = BorderStroke(1.dp, Color(Palette.TRACK)),
        modifier = Modifier.fillMaxWidth(),
    ) {
        Column(
            modifier = Modifier.padding(16.dp),
            verticalArrangement = Arrangement.spacedBy(12.dp),
        ) {
            Column(verticalArrangement = Arrangement.spacedBy(2.dp)) {
                Text(
                    title,
                    style = MaterialTheme.typography.titleMedium,
                    fontWeight = FontWeight.SemiBold,
                    color = Color(Palette.TEXT),
                )
                if (subtitle != null) {
                    Text(
                        subtitle,
                        style = MaterialTheme.typography.bodySmall,
                        color = Color(Palette.DIM),
                    )
                }
            }
            content()
        }
    }
}

// ----------------------------------------------------------------------------
//  Widget
// ----------------------------------------------------------------------------

/** Made-up readings, so the preview works before anything is paired. */
private val SAMPLE_MACHINE = Machine(
    host = "preview", ageS = 2, cpu = 42.0, ram = 61.0, storagePct = 58.0,
    storageTotalGb = 512.0, storageFreeGb = 215.0, tempC = 48.0,
    batteryPct = 64, batteryMin = 190, charging = true, ac = true,
    rxKbps = 1331.0, txKbps = 113.0, uptimeS = 3 * 86400L + 4 * 3600,
)

/** A charging laptop, a desktop with no battery, and a nearly flat laptop: the three things the page has to tell apart. */
private val SAMPLE_MACHINES = listOf(
    SAMPLE_MACHINE.copy(host = "dhaka-laptop"),
    SAMPLE_MACHINE.copy(host = "office-pc", batteryPct = 18, batteryMin = 42, charging = false, ac = false),
    SAMPLE_MACHINE.copy(host = "kushtia-server", batteryPct = null, batteryMin = null, charging = false, ac = false),
)

private fun sampleFrame(kind: PageKind, nowMs: Long, count: Int, index: Int, look: WidgetStyle) = Frame(
    machine = if (kind == PageKind.MACHINE) SAMPLE_MACHINE else null,
    machines = if (kind == PageKind.POWER) SAMPLE_MACHINES else emptyList(),
    index = index, count = count, sinceFetchS = 0, offline = false, link = Link.OK, latencyMs = 42,
    kind = kind, nowMs = nowMs, drawTime = true, showBattery = look.battery,
)

private fun PageKind.label() = when (this) {
    PageKind.MACHINE -> "Machine"
    PageKind.POWER -> "Battery"
    PageKind.CLOCK -> "Clock"
}

@Composable
private fun WidgetPanel(store: StyleStore, onApply: () -> Unit) {
    val ctx = LocalContext.current
    var look by remember { mutableStateOf(store.read()) }
    var applied by remember { mutableStateOf(look) }
    var wanted by rememberSaveable { mutableStateOf(PageKind.MACHINE) }
    var now by remember { mutableStateOf(System.currentTimeMillis()) }

    // Only pages that are switched on can be previewed, in the order the widget cycles them.
    val kinds = buildList {
        add(PageKind.MACHINE)
        if (look.battery) add(PageKind.POWER)
        if (look.clock) add(PageKind.CLOCK)
    }
    val kind = if (wanted in kinds) wanted else PageKind.MACHINE

    // The preview clock keeps time while you look at it.
    LaunchedEffect(kind) {
        while (kind == PageKind.CLOCK) {
            now = System.currentTimeMillis()
            delay(15_000)
        }
    }

    // The same renderer the widget uses, so what you see here is what the home screen gets.
    val minute = if (kind == PageKind.CLOCK) now / 60_000 else 0L
    val preview = remember(look, kind, minute) {
        WidgetRenderer.render(
            ctx, 640, 360,
            sampleFrame(kind, now, kinds.size, kinds.indexOf(kind), look),
            look,
        ).asImageBitmap()
    }

    // Auto-sync: a short pause after the last change, then the widget redraws.
    // Restarting on every change is the debounce.
    LaunchedEffect(look) {
        if (look != applied) {
            delay(250)
            applied = look
            onApply()
        }
    }

    // A bright, busy backdrop so transparency is easy to judge.
    Box(
        modifier = Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(16.dp))
            .background(Brush.linearGradient(listOf(Color(0xFF3A1C71), Color(0xFFD76D77), Color(0xFFFFAF7B))))
            .drawBehind {
                drawCircle(Color(0x66FFFFFF), size.minDimension * 0.35f, Offset(size.width * 0.15f, size.height * 0.2f))
                drawCircle(Color(0x5500E5FF), size.minDimension * 0.40f, Offset(size.width * 0.85f, size.height * 0.85f))
            }
            .padding(12.dp),
    ) {
        Image(
            bitmap = preview,
            contentDescription = "Preview of the ${kind.label().lowercase()} page of the widget",
            modifier = Modifier.fillMaxWidth().aspectRatio(16f / 9f),
        )
    }
    if (kinds.size > 1) {
        Segmented(options = kinds.map { it to it.label() }, selected = kind, onSelect = { wanted = it })
    }

    SubHeading("Pages")
    ToggleRow(
        title = "Clock",
        subtitle = "Time and date. Tap the widget to flip to it.",
        checked = look.clock,
        onChange = { look = look.copy(clock = it); store.save(look) },
    )
    ToggleRow(
        title = "Battery and charging",
        subtitle = "One page with every connected machine\u2019s battery and charging state, and BAT on the machine page when it has one.",
        checked = look.battery,
        onChange = { look = look.copy(battery = it); store.save(look) },
    )

    SubHeading("Look")
    Segmented(
        options = WidgetTheme.values().map { it to it.label },
        selected = look.theme,
        onSelect = { look = look.copy(theme = it); store.save(look) },
    )
    Text(
        "Match system follows your phone, but a widget is a picture: it changes the next time it " +
            "redraws (within 15 minutes, on a tap, or when you open the app).",
        style = MaterialTheme.typography.bodySmall,
        color = Color(Palette.DIM),
    )
    StyleSlider(
        label = "Background opacity",
        value = look.opacity,
        onChange = { look = look.copy(opacity = it); store.save(look) },
    )
    StyleSlider(
        label = "Glass effect",
        value = look.glass,
        onChange = { look = look.copy(glass = it); store.save(look) },
    )

    Text(
        "The preview uses sample readings. On the widget the clock shows your phone's time and " +
            "keeps itself right. Lower opacity shows more wallpaper but can make text hard to read " +
            "on bright backgrounds. Android cannot blur what is behind a widget, so the glass " +
            "effect is lighting only.",
        style = MaterialTheme.typography.bodySmall,
        color = Color(Palette.DIM),
    )
    val lookIsDefault = look.opacity == WidgetStyle.DEFAULT_OPACITY && look.glass == WidgetStyle.DEFAULT_GLASS
    Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.End) {
        TextButton(
            onClick = {
                look = look.copy(opacity = WidgetStyle.DEFAULT_OPACITY, glass = WidgetStyle.DEFAULT_GLASS)
                store.save(look)
            },
            enabled = !lookIsDefault,
        ) { Text("Reset look") }
    }
}

@Composable
internal fun SubHeading(text: String) {
    Text(
        text,
        style = MaterialTheme.typography.labelLarge,
        color = Color(Palette.CYAN),
        modifier = Modifier.padding(top = 4.dp),
    )
}

/** The whole row toggles, not just the switch: a bigger target, and one stop for TalkBack. */
@Composable
internal fun ToggleRow(title: String, subtitle: String, checked: Boolean, onChange: (Boolean) -> Unit) {
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .heightIn(min = 56.dp)
            .toggleable(value = checked, role = Role.Switch, onValueChange = onChange),
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        Column(modifier = Modifier.weight(1f)) {
            Text(title, style = MaterialTheme.typography.bodyLarge)
            Text(subtitle, style = MaterialTheme.typography.bodySmall, color = Color(Palette.DIM))
        }
        Switch(
            checked = checked,
            onCheckedChange = null,
            colors = SwitchDefaults.colors(uncheckedTrackColor = Color(Palette.TRACK)),
        )
    }
}

@Composable
private fun StyleSlider(label: String, value: Int, onChange: (Int) -> Unit) {
    Column {
        Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
            Text(label, style = MaterialTheme.typography.bodyLarge)
            Text(
                "$value%",
                style = MaterialTheme.typography.bodyLarge,
                fontFamily = FontFamily.Monospace,
                color = Color(Palette.DIM),
            )
        }
        Slider(
            value = value.toFloat(),
            onValueChange = { onChange(it.roundToInt()) },
            valueRange = 0f..100f,
            colors = SliderDefaults.colors(inactiveTrackColor = Color(Palette.TRACK)),
            modifier = Modifier.semantics {
                contentDescription = label
                stateDescription = "$value percent"
            },
        )
    }
}

// ----------------------------------------------------------------------------
//  Relay
// ----------------------------------------------------------------------------

@Composable
private fun RelayPanel(relay: String, custom: Boolean, onEdit: () -> Unit) {
    val host = remember(relay) { Uri.parse(relay).host ?: relay }
    Row(
        modifier = Modifier.fillMaxWidth(),
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        Column(modifier = Modifier.weight(1f)) {
            Text(
                host,
                style = MaterialTheme.typography.bodyLarge,
                fontFamily = FontFamily.Monospace,
                maxLines = 1,
                overflow = TextOverflow.Ellipsis,
            )
            Text(
                if (custom) "Custom relay" else "Default relay",
                style = MaterialTheme.typography.bodySmall,
                color = Color(if (custom) Palette.AMBER else Palette.DIM),
            )
        }
        OutlinedButton(onClick = onEdit) { Text("Change") }
    }
    Text(
        "Only change this if you run your own PeekESP relay. Your agents and devices must push to the same one.",
        style = MaterialTheme.typography.bodySmall,
        color = Color(Palette.DIM),
    )
}

@Composable
private fun RelayDialog(onDismiss: () -> Unit, onChanged: (String) -> Unit) {
    val ctx = LocalContext.current
    var text by remember { mutableStateOf(RelayConfig.get(ctx)) }
    var error by remember { mutableStateOf(false) }

    AlertDialog(
        onDismissRequest = onDismiss,
        containerColor = Color(Palette.PANEL),
        titleContentColor = Color(Palette.TEXT),
        textContentColor = Color(Palette.TEXT),
        title = { Text("Relay address") },
        text = {
            Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                Text(
                    "Where PeekESP reads your machines from. It must be an https:// address.",
                    style = MaterialTheme.typography.bodyMedium,
                )
                OutlinedTextField(
                    value = text,
                    onValueChange = { text = it; error = false },
                    label = { Text("Relay address") },
                    isError = error,
                    supportingText = { if (error) Text("Enter an address starting with https://") },
                    singleLine = true,
                    keyboardOptions = KeyboardOptions(
                        keyboardType = KeyboardType.Uri,
                        capitalization = KeyboardCapitalization.None,
                        imeAction = ImeAction.Done,
                    ),
                    modifier = Modifier.fillMaxWidth(),
                )
                if (text.trim().trimEnd('/') != RelayConfig.DEFAULT_BASE) {
                    TextButton(onClick = { text = RelayConfig.DEFAULT_BASE; error = false }) {
                        Text("Use the default address")
                    }
                }
            }
        },
        confirmButton = {
            TextButton(onClick = {
                val clean = RelayConfig.clean(text)
                if (clean == null) {
                    error = true
                } else {
                    if (clean == RelayConfig.DEFAULT_BASE) RelayConfig.reset(ctx) else RelayConfig.save(ctx, clean)
                    RefreshWorker.refreshNow(ctx)
                    onChanged(RelayConfig.get(ctx))
                    onDismiss()
                }
            }) { Text("Save") }
        },
        dismissButton = { TextButton(onClick = onDismiss) { Text("Cancel") } },
    )
}

// ----------------------------------------------------------------------------
//  About
// ----------------------------------------------------------------------------

@Composable
private fun AboutFooter(ctx: Context) {
    Column(
        modifier = Modifier.fillMaxWidth().padding(top = 4.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        Text(
            "PeekESP \u00B7 made by $AUTHOR",
            style = MaterialTheme.typography.bodyMedium,
            color = Color(Palette.TEXT),
        )
        TextButton(onClick = { openLink(ctx, REPO_URL) }) { Text(REPO_LABEL) }
        Text(
            "Version ${BuildConfig.VERSION_NAME} \u00B7 Montserrat font, SIL Open Font License",
            style = MaterialTheme.typography.bodySmall,
            color = Color(Palette.DIM),
            textAlign = TextAlign.Center,
        )
    }
}

private fun openLink(ctx: Context, url: String) {
    val view = Intent(Intent.ACTION_VIEW, Uri.parse(url)).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
    try {
        ctx.startActivity(view)
    } catch (e: ActivityNotFoundException) {
        // No browser installed: nothing useful to do, and nothing worth crashing over.
    }
}

// ----------------------------------------------------------------------------
//  Pairing forms
// ----------------------------------------------------------------------------

/** A row of exclusive choices: pairing mode, and which widget page to preview. */
@Composable
internal fun <T> Segmented(
    options: List<Pair<T, String>>,
    selected: T,
    enabled: Boolean = true,
    onSelect: (T) -> Unit,
) {
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .selectableGroup()
            .background(Color(Palette.BG), RoundedCornerShape(14.dp))
            .padding(4.dp),
    ) {
        options.forEach { (value, label) ->
            val on = value == selected
            Box(
                contentAlignment = Alignment.Center,
                modifier = Modifier
                    .weight(1f)
                    .heightIn(min = 48.dp)                       // touch target
                    .background(
                        if (on) Color(Palette.CYAN) else Color.Transparent,
                        RoundedCornerShape(10.dp),
                    )
                    .selectable(
                        selected = on,
                        enabled = enabled,
                        role = Role.RadioButton,
                        onClick = { onSelect(value) },
                    ),
            ) {
                Text(
                    label,
                    maxLines = 1,
                    fontWeight = if (on) FontWeight.Bold else FontWeight.Medium,
                    color = if (on) Color(Palette.ON_ACCENT) else Color(Palette.TEXT),
                )
            }
        }
    }
}

@Composable
private fun EnterCodeForm(
    input: String,
    busy: Boolean,
    onInput: (String) -> Unit,
    onSubmit: () -> Unit,
) {
    val bad = input.filter { it !in Pairing.ALPHABET }.toSet()
    val valid = Pairing.isValid(input)

    Text("Enter the code your PeekESP device or agent shows.", style = MaterialTheme.typography.bodyMedium)
    OutlinedTextField(
        value = input,
        onValueChange = onInput,
        label = { Text("Pairing code") },
        placeholder = { Text("XXXX-XXXX-XX") },
        visualTransformation = CodeTransformation,
        isError = bad.isNotEmpty(),
        supportingText = {
            Text(
                if (bad.isNotEmpty()) "${bad.sorted().joinToString(", ")} never appear in a code. I, O, 0 and 1 are left out."
                else "${input.length} of ${Pairing.CODE_LEN} characters",
            )
        },
        singleLine = true,
        textStyle = MaterialTheme.typography.titleLarge.copy(fontFamily = FontFamily.Monospace, letterSpacing = 2.sp),
        keyboardOptions = KeyboardOptions(
            capitalization = KeyboardCapitalization.Characters,
            imeAction = ImeAction.Done,
        ),
        keyboardActions = KeyboardActions(onDone = { if (valid && !busy) onSubmit() }),
        modifier = Modifier.fillMaxWidth(),
    )
    Button(
        onClick = onSubmit,
        enabled = !busy && valid,
        modifier = Modifier.fillMaxWidth().heightIn(min = 52.dp),
    ) { ButtonLabel("Pair", busy) }
}

@Composable
private fun CreateCodeForm(
    code: String,
    busy: Boolean,
    onRegenerate: () -> Unit,
    onCopy: () -> Unit,
    onShare: () -> Unit,
    onUse: () -> Unit,
) {
    Text(
        "Make a new code, then enter it on the device or agent you want to watch. " +
            "It works in either order.",
        style = MaterialTheme.typography.bodyMedium,
    )
    CodeBlock(code = code, revealed = true)
    Row(horizontalArrangement = Arrangement.spacedBy(8.dp), modifier = Modifier.fillMaxWidth()) {
        OutlinedButton(onClick = onCopy, enabled = !busy, modifier = Modifier.weight(1f)) { Text("Copy") }
        OutlinedButton(onClick = onShare, enabled = !busy, modifier = Modifier.weight(1f)) { Text("Share") }
        OutlinedButton(onClick = onRegenerate, enabled = !busy, modifier = Modifier.weight(1f)) { Text("New code") }
    }
    Button(
        onClick = onUse,
        enabled = !busy,
        modifier = Modifier.fillMaxWidth().heightIn(min = 52.dp),
    ) { ButtonLabel("Use this code", busy) }
    Text(
        "Anyone with this code can read your readings. Share it only with your own devices.",
        style = MaterialTheme.typography.bodySmall,
        color = Color(Palette.DIM),
    )
}

@Composable
private fun PairedPanel(
    code: String,
    revealed: Boolean,
    onToggleReveal: () -> Unit,
    onCopy: () -> Unit,
    onShare: () -> Unit,
    onRefresh: () -> Unit,
    onForget: () -> Unit,
) {
    val ctx = LocalContext.current

    // The code is the credential: masked by default, shown on request.
    CodeBlock(code = code, revealed = revealed)
    Row(horizontalArrangement = Arrangement.spacedBy(8.dp), modifier = Modifier.fillMaxWidth()) {
        OutlinedButton(onClick = onToggleReveal, modifier = Modifier.weight(1f)) {
            Text(if (revealed) "Hide" else "Show")
        }
        OutlinedButton(onClick = onCopy, modifier = Modifier.weight(1f)) { Text("Copy") }
        OutlinedButton(onClick = onShare, modifier = Modifier.weight(1f)) { Text("Share") }
    }

    val mgr = AppWidgetManager.getInstance(ctx)
    if (mgr.isRequestPinAppWidgetSupported) {
        Button(
            onClick = {
                mgr.requestPinAppWidget(ComponentName(ctx, PeekWidgetReceiver::class.java), null, null)
            },
            modifier = Modifier.fillMaxWidth().heightIn(min = 52.dp),
        ) { Text("Add widget to home screen") }
    } else {
        Text(
            "Long-press the home screen, choose Widgets, then PeekESP.",
            style = MaterialTheme.typography.bodyMedium,
        )
    }
    OutlinedButton(onClick = onRefresh, modifier = Modifier.fillMaxWidth()) { Text("Refresh widget now") }
    TextButton(onClick = onForget, modifier = Modifier.fillMaxWidth()) {
        Text("Forget this setup", color = Color(Palette.RED))
    }
}

// ----------------------------------------------------------------------------
//  Pieces
// ----------------------------------------------------------------------------

@Composable
private fun ButtonLabel(text: String, busy: Boolean) {
    if (busy) {
        CircularProgressIndicator(
            modifier = Modifier.size(20.dp),
            strokeWidth = 2.dp,
            color = Color(Palette.DIM),
        )
        Box(Modifier.width(10.dp))
    }
    Text(text)
}

/** The code, large and monospaced so I/1 and O/0 cannot be misread when copying by eye. */
@Composable
private fun CodeBlock(code: String, revealed: Boolean) {
    val shown = if (revealed) Pairing.format(code) else "\u2022\u2022\u2022\u2022-\u2022\u2022\u2022\u2022-\u2022\u2022"
    // TalkBack reads a code one character at a time, or says nothing while it is hidden.
    val spoken = if (revealed) "Pairing code: " + Pairing.normalise(code).toList().joinToString(" ")
    else "Pairing code, hidden"
    Surface(
        color = Color(Palette.BG),                    // darker than the card around it
        shape = RoundedCornerShape(14.dp),
        border = BorderStroke(1.dp, Color(Palette.TRACK)),
        modifier = Modifier.fillMaxWidth().clearAndSetSemantics { contentDescription = spoken },
    ) {
        Text(
            shown,
            fontFamily = FontFamily.Monospace,
            fontWeight = FontWeight.Bold,
            fontSize = 28.sp,
            letterSpacing = 3.sp,
            color = Color(if (revealed) Palette.CYAN else Palette.DIM),
            textAlign = TextAlign.Center,
            maxLines = 1,
            modifier = Modifier.padding(horizontal = 12.dp, vertical = 24.dp),
        )
    }
}

/** Shows K7M2P4QX9R as K7M2-P4QX-9R while the field still holds the bare code. */
private object CodeTransformation : VisualTransformation {
    override fun filter(text: AnnotatedString): TransformedText {
        val raw = text.text
        val out = buildString {
            raw.forEachIndexed { i, ch ->
                if (i == 4 || i == 8) append('-')
                append(ch)
            }
        }
        val map = object : OffsetMapping {
            override fun originalToTransformed(offset: Int) = when {
                offset <= 4 -> offset
                offset <= 8 -> offset + 1
                else -> offset + 2
            }

            override fun transformedToOriginal(offset: Int) = when {
                offset <= 4 -> offset
                offset <= 9 -> offset - 1
                else -> offset - 2
            }.coerceIn(0, raw.length)
        }
        return TransformedText(AnnotatedString(out), map)
    }
}

// ----------------------------------------------------------------------------
//  Clipboard and share
// ----------------------------------------------------------------------------

private fun copyCode(ctx: Context, code: String) {
    val cm = ctx.getSystemService(ClipboardManager::class.java) ?: return
    val clip = ClipData.newPlainText("PeekESP code", Pairing.format(code))
    if (Build.VERSION.SDK_INT >= 33) {
        // Keeps the code out of the keyboard's clipboard preview and suggestions.
        clip.description.extras = PersistableBundle().apply {
            putBoolean(ClipDescription.EXTRA_IS_SENSITIVE, true)
        }
    }
    cm.setPrimaryClip(clip)
}

private fun shareCode(ctx: Context, code: String) {
    val send = Intent(Intent.ACTION_SEND).apply {
        type = "text/plain"
        putExtra(Intent.EXTRA_TEXT, "PeekESP pairing code: ${Pairing.format(code)}")
    }
    ctx.startActivity(Intent.createChooser(send, "Share pairing code").addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
}