package com.rhshourav.peekesp

import android.appwidget.AppWidgetManager
import android.content.ComponentName
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.safeDrawingPadding
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material3.Button
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.input.KeyboardCapitalization
import androidx.compose.ui.unit.dp
import androidx.glance.appwidget.updateAll
import com.rhshourav.peekesp.data.FetchResult
import com.rhshourav.peekesp.data.Pairing
import com.rhshourav.peekesp.data.RelayClient
import com.rhshourav.peekesp.data.Setup
import com.rhshourav.peekesp.data.SetupStore
import com.rhshourav.peekesp.data.WidgetCache
import com.rhshourav.peekesp.widget.Palette
import com.rhshourav.peekesp.widget.PeekWidget
import com.rhshourav.peekesp.widget.PeekWidgetReceiver
import com.rhshourav.peekesp.widget.RefreshWorker
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContent {
            MaterialTheme(
                colorScheme = darkColorScheme(
                    primary = Color(Palette.CYAN), onPrimary = Color(Palette.BG),
                    background = Color(Palette.BG), onBackground = Color(Palette.TEXT),
                    surface = Color(Palette.PANEL), onSurface = Color(Palette.TEXT),
                    error = Color(Palette.RED),
                ),
            ) { Screen() }
        }
    }
}

/** Pair with a code, then add the widget. The dashboard screens come after this slice. */
@Composable
private fun Screen() {
    val ctx = LocalContext.current
    val store = remember { SetupStore(ctx) }
    val cache = remember { WidgetCache(ctx) }
    val scope = rememberCoroutineScope()

    var setup by remember { mutableStateOf(store.load().firstOrNull()) }
    var input by remember { mutableStateOf("") }
    var status by remember { mutableStateOf<String?>(null) }
    var busy by remember { mutableStateOf(false) }

    fun pair() {
        if (!Pairing.isValid(input)) {
            status = "That is not a valid code: ten characters, with no I, O, 0 or 1."
            return
        }
        busy = true
        status = "Checking the relay..."
        scope.launch {
            val keys = Pairing.derive(input)
            val base = RelayClient.DEFAULT_BASE
            val result = withContext(Dispatchers.IO) { RelayClient.fetch(base, keys) }
            val now = System.currentTimeMillis()
            val save = {
                val s = Setup("My setup", base, keys.code)
                store.save(listOf(s))
                setup = s
                input = ""
                RefreshWorker.schedule(ctx)
            }
            when (result) {
                is FetchResult.Ok -> {
                    save()
                    cache.saveOk(result.raw, result.machines.size, result.latencyMs, now)
                    status = "Found ${result.machines.size} machine(s). Add the widget below."
                }
                FetchResult.Empty -> {
                    save()
                    cache.saveOk("{}", 0, 0, now)
                    // The relay claims a token on first use, so it cannot tell these apart.
                    status = "Nothing is pushing to this code yet. That is expected if no agent " +
                        "is installed, and it is also exactly what a typo looks like."
                }
                FetchResult.AuthRejected -> status = "The relay rejected this code's token."
                is FetchResult.Failed -> status = "Could not reach the relay: ${result.reason}"
            }
            PeekWidget().updateAll(ctx)
            busy = false
        }
    }

    Column(
        modifier = Modifier
            .fillMaxSize()
            .background(Color(Palette.BG))
            .safeDrawingPadding()
            .padding(24.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        Text("PeekESP", style = MaterialTheme.typography.headlineMedium, color = Color(Palette.CYAN))

        val current = setup
        if (current == null) {
            Text("Enter the code your PeekESP device shows on its screen.")
            OutlinedTextField(
                value = input,
                onValueChange = { input = Pairing.normalise(it).take(Pairing.CODE_LEN) },
                label = { Text("Pairing code") },
                supportingText = { if (input.isNotEmpty()) Text(Pairing.format(input)) },
                singleLine = true,
                keyboardOptions = KeyboardOptions(capitalization = KeyboardCapitalization.Characters),
                modifier = Modifier.fillMaxWidth(),
            )
            Button(onClick = { pair() }, enabled = !busy && input.isNotEmpty()) { Text("Pair") }
        } else {
            // The code is the credential: shown masked, never in full after entry.
            Text("Paired  ${Pairing.format(current.code).take(4)}-\u2022\u2022\u2022\u2022-\u2022\u2022")
            val mgr = AppWidgetManager.getInstance(ctx)
            if (mgr.isRequestPinAppWidgetSupported) {
                Button(onClick = {
                    mgr.requestPinAppWidget(ComponentName(ctx, PeekWidgetReceiver::class.java), null, null)
                }) { Text("Add widget to home screen") }
            } else {
                Text("Long-press the home screen, choose Widgets, then PeekESP.")
            }
            OutlinedButton(onClick = {
                RefreshWorker.refreshNow(ctx)
                status = "Refreshing the widget..."
            }) { Text("Refresh widget now") }
            OutlinedButton(onClick = {
                store.clear(); cache.clear(); RefreshWorker.cancel(ctx)
                setup = null
                status = "Forgotten."
                scope.launch { PeekWidget().updateAll(ctx) }
            }) { Text("Forget this setup") }
        }

        status?.let {
            Spacer(Modifier.height(4.dp))
            Text(it, color = Color(Palette.DIM))
        }
    }
}
