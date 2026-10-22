package com.rhshourav.peekesp.data

import android.Manifest
import android.annotation.SuppressLint
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Color
import android.media.AudioAttributes
import android.media.MediaPlayer
import android.media.RingtoneManager
import android.os.Build
import android.os.Handler
import android.os.Looper
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import com.rhshourav.peekesp.MainActivity
import com.rhshourav.peekesp.R
import java.time.Instant
import java.time.ZoneId

/**
 * Shows events to the user, at the volume they deserve:
 *
 *  - INFO     a quiet line: no sound, no popup.
 *  - WARNING  the phone's normal notification sound, and a popup.
 *  - CRITICAL rings like an alarm (the alarm volume, which also sounds when the phone is on silent)
 *             and keeps ringing until it is silenced or ten minutes pass.
 *
 * Quiet hours hold everything back except critical alerts, if the user lets those through; what
 * was held is delivered when the window ends, with its original time on it.
 */
object Notifier {
    const val CH_CRITICAL = "peek_critical"
    const val CH_WARNING = "peek_warning"
    const val CH_INFO = "peek_info"
    const val ACTION_SILENCE = "com.rhshourav.peekesp.action.SILENCE"
    const val EXTRA_EVENT = "event"
    const val EXTRA_OPEN_ALERTS = "open_alerts"

    private const val GROUP = "peek_alerts"
    private const val MAX_AGE_MS = 24L * 3_600_000L          // older than a day is history, not news
    private const val MAX_INDIVIDUAL = 4                      // more than this many and they become one summary
    private const val MAX_CRITICAL_INDIVIDUAL = 6
    private const val SUMMARY_ID = 9_999
    private const val RING_TIMEOUT_MS = 10L * 60_000L

    /** Whether the system will show our notifications: the permission (Android 13+) and the app-level switch. */
    fun canPost(ctx: Context): Boolean {
        val nm = ctx.getSystemService(NotificationManager::class.java) ?: return false
        if (Build.VERSION.SDK_INT >= 33 &&
            ctx.checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED
        ) return false
        return nm.areNotificationsEnabled()
    }

    /**
     * Channels are where sound and importance live, and Android lets the user override them per
     * channel, so these are only the starting point. They cannot be changed after creation.
     */
    fun ensureChannels(ctx: Context) {
        val nm = ctx.getSystemService(NotificationManager::class.java) ?: return
        val alarm = RingtoneManager.getDefaultUri(RingtoneManager.TYPE_ALARM)
            ?: RingtoneManager.getDefaultUri(RingtoneManager.TYPE_RINGTONE)

        val critical = NotificationChannel(CH_CRITICAL, "Critical alerts", NotificationManager.IMPORTANCE_HIGH).apply {
            description = "Rings like an alarm until you silence it. For things that need you now."
            setSound(
                alarm,
                AudioAttributes.Builder()
                    .setUsage(AudioAttributes.USAGE_ALARM)
                    .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION)
                    .build(),
            )
            enableVibration(true)
            vibrationPattern = longArrayOf(0, 700, 300, 700, 300, 1000)
            enableLights(true)
            lightColor = Color.RED
            lockscreenVisibility = Notification.VISIBILITY_PUBLIC
        }
        val warning = NotificationChannel(CH_WARNING, "Warnings", NotificationManager.IMPORTANCE_HIGH).apply {
            description = "A sound and a popup: a machine went offline, a threshold was crossed."
            enableVibration(true)
        }
        val info = NotificationChannel(CH_INFO, "Information", NotificationManager.IMPORTANCE_LOW).apply {
            description = "Quiet: a machine came back, an alert cleared, a note was left."
        }
        nm.createNotificationChannels(listOf(critical, warning, info))
    }

    /**
     * Delivers whatever is waiting, if it is allowed to be delivered now. Safe to call as often as
     * you like: with nothing pending it is one small query.
     */
    @SuppressLint("MissingPermission")   // canPost() is the permission check
    fun flush(ctx: Context, s: AlertSettings, nowMs: Long = System.currentTimeMillis()) {
        val pending = AlertStore.pending(ctx)
        if (pending.isEmpty()) return

        val quiet = s.quiet.contains(Instant.ofEpochMilli(nowMs).atZone(ZoneId.systemDefault()))
        // Held events are simply left pending; they go out the first time this runs outside the window.
        val ready = pending.filter { !quiet || (it.severity == Severity.CRITICAL && s.quiet.criticalBreaksThrough) }
        if (ready.isEmpty()) return

        if (canPost(ctx)) {
            ensureChannels(ctx)
            val fresh = ready.filter { nowMs - it.t <= MAX_AGE_MS }
            fresh.filter { it.severity == Severity.CRITICAL }.takeLast(MAX_CRITICAL_INDIVIDUAL).forEach { post(ctx, it) }
            val others = fresh.filter { it.severity != Severity.CRITICAL }
            if (others.size <= MAX_INDIVIDUAL) others.forEach { post(ctx, it) } else summary(ctx, others)
        }
        // Delivered or not allowed to be, they are no longer waiting; the inbox still has them.
        AlertStore.markNotified(ctx, ready.map { it.id })
    }

    private fun notificationId(eventId: Long): Int = 10_000 + (eventId % 100_000).toInt()

    private fun open(ctx: Context): PendingIntent = PendingIntent.getActivity(
        ctx, 0,
        Intent(ctx, MainActivity::class.java)
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP)
            .putExtra(EXTRA_OPEN_ALERTS, true),
        PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
    )

    private fun silenceIntent(ctx: Context, eventId: Long): PendingIntent = PendingIntent.getBroadcast(
        ctx, notificationId(eventId),
        Intent(ctx, NotificationActionReceiver::class.java).setAction(ACTION_SILENCE).putExtra(EXTRA_EVENT, eventId),
        PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
    )

    @SuppressLint("MissingPermission")
    private fun post(ctx: Context, e: Event) {
        val channel = when (e.severity) {
            Severity.CRITICAL -> CH_CRITICAL
            Severity.WARNING -> CH_WARNING
            Severity.INFO -> CH_INFO
        }
        val where = listOfNotNull(e.machine, e.source.takeIf { it.isNotEmpty() && it != e.machine }).joinToString(" \u00B7 ")
        val b = NotificationCompat.Builder(ctx, channel)
            .setSmallIcon(R.drawable.ic_notification)
            .setContentTitle(e.title)
            .setContentText(e.body.ifEmpty { where })
            .setStyle(NotificationCompat.BigTextStyle().bigText(e.body.ifEmpty { where }))
            .setSubText(where.ifEmpty { null })
            .setWhen(e.t)
            .setShowWhen(true)
            .setAutoCancel(true)
            .setGroup(GROUP)
            .setContentIntent(open(ctx))
        when (e.severity) {
            Severity.CRITICAL -> b.setColor(0xFFFF4D6D.toInt())
                .setPriority(NotificationCompat.PRIORITY_MAX)
                .setCategory(NotificationCompat.CATEGORY_ALARM)
                .setVisibility(NotificationCompat.VISIBILITY_PUBLIC)
                .setTimeoutAfter(RING_TIMEOUT_MS)
                .addAction(0, "Silence", silenceIntent(ctx, e.id))
            Severity.WARNING -> b.setColor(0xFFFFC145.toInt()).setPriority(NotificationCompat.PRIORITY_HIGH)
            Severity.INFO -> b.setColor(0xFF00E5FF.toInt()).setPriority(NotificationCompat.PRIORITY_LOW)
        }
        val n = b.build()
        // Keep ringing until it is silenced or times out: that is what makes it an alarm and not a ping.
        if (e.severity == Severity.CRITICAL) n.flags = n.flags or Notification.FLAG_INSISTENT
        NotificationManagerCompat.from(ctx).notify(notificationId(e.id), n)
    }

    @SuppressLint("MissingPermission")
    private fun summary(ctx: Context, events: List<Event>) {
        val worst = if (events.any { it.severity == Severity.WARNING }) CH_WARNING else CH_INFO
        val lines = events.takeLast(5).joinToString("\n") { "\u2022 ${it.title}" }
        val n = NotificationCompat.Builder(ctx, worst)
            .setSmallIcon(R.drawable.ic_notification)
            .setContentTitle("${events.size} new alerts")
            .setContentText("Open PeekESP to see them.")
            .setStyle(NotificationCompat.BigTextStyle().bigText(lines))
            .setAutoCancel(true)
            .setGroup(GROUP)
            .setContentIntent(open(ctx))
            .build()
        NotificationManagerCompat.from(ctx).notify(SUMMARY_ID, n)
    }

    /** Stops the ringing and takes the notification down. */
    fun cancel(ctx: Context, eventId: Long) {
        NotificationManagerCompat.from(ctx).cancel(notificationId(eventId))
    }
}

/**
 * The ring for when the app is open and notifications are not allowed: the one case where there is
 * nothing else to make a noise. Otherwise the notification itself rings, and this stays quiet.
 */
object AlarmPlayer {
    private var player: MediaPlayer? = null
    private val handler = Handler(Looper.getMainLooper())

    @Synchronized
    fun ring(ctx: Context, maxMs: Long = 30_000L) {
        if (player != null) return
        val uri = RingtoneManager.getDefaultUri(RingtoneManager.TYPE_ALARM)
            ?: RingtoneManager.getDefaultUri(RingtoneManager.TYPE_RINGTONE) ?: return
        val mp = MediaPlayer()
        try {
            mp.setAudioAttributes(
                AudioAttributes.Builder()
                    .setUsage(AudioAttributes.USAGE_ALARM)
                    .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION)
                    .build(),
            )
            mp.setDataSource(ctx.applicationContext, uri)
            mp.isLooping = true
            mp.prepare()
            mp.start()
            player = mp
            handler.postDelayed({ stop() }, maxMs)       // never an endless noise
        } catch (e: Exception) {
            mp.release()
        }
    }

    @Synchronized
    fun stop() {
        val p = player ?: return
        try {
            p.stop()
        } catch (e: IllegalStateException) {
            // already stopped
        }
        p.release()
        player = null
        handler.removeCallbacksAndMessages(null)
    }
}