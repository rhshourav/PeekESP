package com.rhshourav.peekesp.widget

import android.content.Context
import androidx.glance.appwidget.updateAll
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
import com.rhshourav.peekesp.data.Pairing
import com.rhshourav.peekesp.data.RelayClient
import com.rhshourav.peekesp.data.SetupStore
import com.rhshourav.peekesp.data.WidgetCache
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import java.util.concurrent.TimeUnit

/** One request per run: fetch, cache, redraw. 96 a day on the 15-minute schedule. */
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
        when (val r = withContext(Dispatchers.IO) { RelayClient.fetch(setup.relay, keys) }) {
            is FetchResult.Ok -> cache.saveOk(r.raw, r.machines.size, r.latencyMs, System.currentTimeMillis())
            FetchResult.Empty -> cache.saveOk("{}", 0, 0, System.currentTimeMillis())
            FetchResult.AuthRejected -> cache.saveAuthRejected()
            is FetchResult.Failed -> cache.saveFailed()
        }
        PeekWidget().updateAll(app)
        return Result.success()
    }

    companion object {
        private const val PERIODIC = "peek-widget-periodic"
        private const val NOW = "peek-widget-now"

        private val online = Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build()

        /** 15 minutes is the platform's floor for background work. */
        fun schedule(ctx: Context) {
            WorkManager.getInstance(ctx).enqueueUniquePeriodicWork(
                PERIODIC, ExistingPeriodicWorkPolicy.KEEP,
                PeriodicWorkRequestBuilder<RefreshWorker>(15, TimeUnit.MINUTES).setConstraints(online).build(),
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
