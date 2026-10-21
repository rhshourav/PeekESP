package com.rhshourav.peekesp.widget

import android.content.Context
import androidx.work.Constraints
import androidx.work.CoroutineWorker
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.ExistingWorkPolicy
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import com.rhshourav.peekesp.data.FetchResult
import com.rhshourav.peekesp.data.History
import com.rhshourav.peekesp.data.LinkKind
import com.rhshourav.peekesp.data.Net
import com.rhshourav.peekesp.data.Pairing
import com.rhshourav.peekesp.data.RelayClient
import com.rhshourav.peekesp.data.SetupStore
import com.rhshourav.peekesp.data.WidgetCache
import com.rhshourav.peekesp.data.RelayConfig
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import java.util.concurrent.TimeUnit

/** One run: note whether the phone is online, fetch, cache, log, redraw. 96 a day on the 15-minute schedule. */
class RefreshWorker(ctx: Context, params: WorkerParameters) : CoroutineWorker(ctx, params) {

    override suspend fun doWork(): Result {
        val app = applicationContext
        val setup = SetupStore(app).load().firstOrNull() ?: return Result.success()
        val keys = try {
            Pairing.derive(setup.code)
        } catch (e: IllegalArgumentException) {
            return Result.success()
        }

        val cache = WidgetCache(app)

        // No internet on the phone. Say so in the log before anything else: a chart has to be able
        // to tell "the phone had no signal" from "the machine went quiet", and this run is the only
        // witness while the app is closed. The widget shows NO LINK, which is the truth.
        if (!Net.isOnline(app)) {
            runCatching { History.recordLink(app, LinkKind.PHONE_OFFLINE, System.currentTimeMillis()) }
            cache.saveFailed()
            redrawWidgets(app)
            return Result.success()
        }

        when (val r = withContext(Dispatchers.IO) { RelayClient.fetch(RelayConfig.get(app), keys) }) {
            is FetchResult.Ok -> {
                val now = System.currentTimeMillis()
                cache.saveOk(r.raw, r.machines.size, r.latencyMs, now)
                // The background half of the history: one reading per machine every 15 minutes.
                // A full disk or a locked database must never stop the widget from redrawing.
                runCatching {
                    History.record(app, r.machines, now)
                    History.recordLink(app, LinkKind.OK, now)
                }
            }
            FetchResult.Empty -> {
                val now = System.currentTimeMillis()
                cache.saveOk("{}", 0, 0, now)
                runCatching { History.recordLink(app, LinkKind.OK, now) }
            }
            FetchResult.AuthRejected -> {
                cache.saveAuthRejected()
                // The relay answered, so the path is fine; the code is the problem, not the connection.
                runCatching { History.recordLink(app, LinkKind.OK, System.currentTimeMillis()) }
            }
            is FetchResult.Failed -> {
                cache.saveFailed()
                // Online by the system's reckoning, yet the request failed: the relay, not the phone.
                runCatching { History.recordLink(app, Net.failureKind(app), System.currentTimeMillis()) }
            }
        }
        redrawWidgets(app)
        return Result.success()
    }

    companion object {
        private const val PERIODIC = "peek-widget-periodic"
        private const val NOW = "peek-widget-now"

        /** For a refresh you asked for: no point running it until there is a network to run it on. */
        private val online = Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build()

        /**
         * 15 minutes is the platform's floor for background work. The periodic run has no network
         * constraint on purpose: it must run with no internet too, to write that down (see doWork).
         * UPDATE, not KEEP, so an install that already has the old constrained schedule gets this one.
         */
        fun schedule(ctx: Context) {
            WorkManager.getInstance(ctx).enqueueUniquePeriodicWork(
                PERIODIC, ExistingPeriodicWorkPolicy.UPDATE,
                PeriodicWorkRequestBuilder<RefreshWorker>(15, TimeUnit.MINUTES).build(),
            )
        }

        fun refreshNow(ctx: Context) {
            WorkManager.getInstance(ctx).enqueueUniqueWork(
                NOW, ExistingWorkPolicy.REPLACE,
                OneTimeWorkRequestBuilder<RefreshWorker>().setConstraints(online).build(),
            )
        }

        fun cancel(ctx: Context) {
            WorkManager.getInstance(ctx).cancelUniqueWork(PERIODIC)
            WorkManager.getInstance(ctx).cancelUniqueWork(NOW)
        }
    }
}