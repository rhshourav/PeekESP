package com.rhshourav.peekesp.data

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import java.security.MessageDigest
import java.util.UUID
import kotlin.concurrent.thread

/** The "Silence" button on a critical notification. Not exported: only our own notification can fire it. */
class NotificationActionReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != Notifier.ACTION_SILENCE) return
        val id = intent.getLongExtra(Notifier.EXTRA_EVENT, -1L)
        if (id < 0) return
        Notifier.cancel(context, id)
        AlarmPlayer.stop()
        val pending = goAsync()
        thread(name = "peek-silence") {
            try {
                AlertStore.markRead(context, id)
                AlertBus.bump()
            } finally {
                pending.finish()
            }
        }
    }
}

/**
 * The door other apps use to put a message in front of the user through PeekESP: a security tool,
 * Tasker, a script run over adb. It needs the key the user generated under Configure, so only apps
 * they have chosen to trust can use it, and it is rate limited so a misbehaving one cannot flood the phone.
 *
 * It says nothing back on any failure. A caller that is refused learns nothing about why, which
 * keeps guessing the key as hard as it should be.
 *
 *   adb shell am broadcast -a com.rhshourav.peekesp.action.SEND_MESSAGE -p com.rhshourav.peekesp \
 *       --es token KEY --es severity critical --es title "Intrusion" --es message "New login"
 *
 * The sender must name our package (-p / setPackage): Android does not deliver an unaddressed
 * broadcast to another app's manifest receiver.
 */
class ExternalMessageReceiver : BroadcastReceiver() {
    companion object {
        const val ACTION = "com.rhshourav.peekesp.action.SEND_MESSAGE"
    }

    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != ACTION) return
        val s = AlertConfig.load(context)
        if (!s.enabled || !s.externalEnabled || s.externalToken.isEmpty()) return

        val given = intent.getStringExtra("token").orEmpty()
        // Constant time: how long the comparison takes must not hint at how much of the key was right.
        if (!MessageDigest.isEqual(given.toByteArray(), s.externalToken.toByteArray())) return
        if (!RateLimit.allow()) return

        val title = clean(intent.getStringExtra("title"), 80)
        val body = clean(intent.getStringExtra("message") ?: intent.getStringExtra("body"), 400)
        if (title.isEmpty() && body.isEmpty()) return
        val source = clean(intent.getStringExtra("source"), 40).ifEmpty { "External app" }
        val machine = clean(intent.getStringExtra("machine"), 32).ifEmpty { null }
        // A sender that supplies an id can safely retry: the same id is the same message.
        val id = clean(intent.getStringExtra("id"), 64).ifEmpty { UUID.randomUUID().toString() }

        val event = NewEvent(
            key = "ext:$source:$id",
            t = System.currentTimeMillis(),
            severity = Severity.parse(intent.getStringExtra("severity")),
            title = title.ifEmpty { body.take(60) },
            body = if (title.isEmpty()) "" else body,
            source = source,
            machine = machine,
            kind = EventKind.MESSAGE,
            silent = !s.notifyMessages || (machine != null && machine in s.mutedMachines),
        )
        val pending = goAsync()
        val app = context.applicationContext
        thread(name = "peek-external") {
            try {
                AlertRunner.send(app, event)
            } finally {
                pending.finish()
            }
        }
    }

    private fun clean(s: String?, max: Int): String = s?.trim()?.take(max).orEmpty()
}

/** At most [LIMIT] messages a minute from all other apps together. */
internal object RateLimit {
    private const val LIMIT = 20
    private const val WINDOW_MS = 60_000L
    private val stamps = ArrayDeque<Long>()

    @Synchronized
    fun allow(now: Long = System.currentTimeMillis()): Boolean {
        while (stamps.isNotEmpty() && now - stamps.first() > WINDOW_MS) stamps.removeFirst()
        if (stamps.size >= LIMIT) return false
        stamps.addLast(now)
        return true
    }
}