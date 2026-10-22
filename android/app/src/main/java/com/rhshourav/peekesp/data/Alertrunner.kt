package com.rhshourav.peekesp.data

import android.content.Context

/**
 * The one place readings and messages become alerts. The app calls it while it is open and the
 * widget's worker calls it in the background, so both behave the same way.
 *
 * Everything here blocks (it touches a database): call it off the main thread.
 */
object AlertRunner {
    /** A reading arrived from the relay: decide what is worth saying, store it, show what is allowed. */
    fun process(ctx: Context, machines: List<Machine>, fetchedAtMs: Long) {
        val s = AlertConfig.load(ctx)
        if (!s.enabled) return
        val engine = AlertEngine(AlertStore.state(ctx)) { host, metric -> History.baseline(ctx, host, metric, fetchedAtMs) }
        val events = engine.evaluate(s, machines, fetchedAtMs)
        var added = false
        for (e in events) if (AlertStore.insert(ctx, e) > 0) added = true
        if (added) AlertBus.bump()
        Notifier.flush(ctx, s)
    }

    /** One event from somewhere else: another app, a test button, a message you wrote. */
    fun send(ctx: Context, e: NewEvent) {
        val s = AlertConfig.load(ctx)
        if (AlertStore.insert(ctx, e) > 0) AlertBus.bump()
        Notifier.flush(ctx, s)
    }
}