package com.rhshourav.peekesp.data

import android.content.ContentValues
import android.content.Context
import android.database.Cursor
import android.database.sqlite.SQLiteDatabase
import android.database.sqlite.SQLiteOpenHelper
import androidx.compose.runtime.mutableStateOf

/**
 * The inbox, in its own database. It is separate from the history on purpose: clearing the
 * history, or switching it off, must not make alerts vanish, and an alert should be kept even
 * when no one wanted charts.
 */
private class AlertDb(context: Context) : SQLiteOpenHelper(context.applicationContext, "alerts.db", null, 1) {
    override fun onCreate(db: SQLiteDatabase) {
        db.execSQL(
            "CREATE TABLE events (" +
                "id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT NOT NULL UNIQUE, t INTEGER NOT NULL, " +
                "severity INTEGER NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL, source TEXT NOT NULL, " +
                "machine TEXT, kind INTEGER NOT NULL, is_read INTEGER NOT NULL DEFAULT 0, " +
                "is_notified INTEGER NOT NULL DEFAULT 0)",
        )
        db.execSQL("CREATE INDEX events_t ON events (t)")
        // Where each rule and each machine's status stands between readings (see AlertEngine).
        db.execSQL("CREATE TABLE alert_state (k TEXT PRIMARY KEY, since INTEGER, fired INTEGER NOT NULL)")
    }

    override fun onUpgrade(db: SQLiteDatabase, oldVersion: Int, newVersion: Int) = Unit
}

/** Bumped whenever the inbox changes, so a screen that read it knows to read it again. */
object AlertBus {
    val version = mutableStateOf(0)
    fun bump() {
        version.value = version.value + 1
    }
}

object AlertStore {
    private const val KEEP_MS = 30L * 86_400_000L
    private const val KEEP_ROWS = 500

    @Volatile private var helper: AlertDb? = null
    private var lastPrune = 0L

    private fun db(ctx: Context): AlertDb =
        helper ?: synchronized(this) { helper ?: AlertDb(ctx).also { helper = it } }

    /**
     * Adds an event unless one with the same key is already there. Returns its id, or -1 if it was a
     * repeat. A silent event is stored as already notified, so it appears in the inbox and never rings.
     */
    @Synchronized
    fun insert(ctx: Context, e: NewEvent): Long {
        val v = ContentValues()
        v.put("key", e.key)
        v.put("t", e.t)
        v.put("severity", e.severity.ordinal)
        v.put("title", e.title)
        v.put("body", e.body)
        v.put("source", e.source)
        if (e.machine == null) v.putNull("machine") else v.put("machine", e.machine)
        v.put("kind", e.kind.code)
        v.put("is_notified", if (e.silent) 1 else 0)
        val id = db(ctx).writableDatabase.insertWithOnConflict("events", null, v, SQLiteDatabase.CONFLICT_IGNORE)
        pruneIfDue(ctx)
        return id
    }

    private fun Cursor.event() = Event(
        id = getLong(0), key = getString(1), t = getLong(2),
        severity = Severity.values().getOrElse(getInt(3)) { Severity.INFO },
        title = getString(4), body = getString(5), source = getString(6),
        machine = if (isNull(7)) null else getString(7),
        kind = EventKind.of(getInt(8)), read = getInt(9) != 0, notified = getInt(10) != 0,
    )

    private const val COLS = "id, key, t, severity, title, body, source, machine, kind, is_read, is_notified"

    fun recent(ctx: Context, limit: Int = 200): List<Event> =
        db(ctx).readableDatabase.rawQuery("SELECT $COLS FROM events ORDER BY t DESC, id DESC LIMIT $limit", null)
            .use { c -> buildList { while (c.moveToNext()) add(c.event()) } }

    /** Waiting to be shown to the user: not silent, not yet delivered. Oldest first. */
    fun pending(ctx: Context): List<Event> =
        db(ctx).readableDatabase.rawQuery("SELECT $COLS FROM events WHERE is_notified = 0 ORDER BY t, id", null)
            .use { c -> buildList { while (c.moveToNext()) add(c.event()) } }

    fun unreadCount(ctx: Context): Int =
        db(ctx).readableDatabase.rawQuery("SELECT COUNT(*) FROM events WHERE is_read = 0", null)
            .use { c -> if (c.moveToFirst()) c.getInt(0) else 0 }

    fun unreadCritical(ctx: Context): List<Event> =
        db(ctx).readableDatabase.rawQuery(
            "SELECT $COLS FROM events WHERE is_read = 0 AND severity = ${Severity.CRITICAL.ordinal} ORDER BY t DESC", null,
        ).use { c -> buildList { while (c.moveToNext()) add(c.event()) } }

    @Synchronized
    fun markNotified(ctx: Context, ids: List<Long>) {
        if (ids.isEmpty()) return
        db(ctx).writableDatabase.execSQL("UPDATE events SET is_notified = 1 WHERE id IN (${ids.joinToString(",")})")
    }

    @Synchronized
    fun markRead(ctx: Context, id: Long) {
        db(ctx).writableDatabase.execSQL("UPDATE events SET is_read = 1 WHERE id = ?", arrayOf<Any>(id))
    }

    @Synchronized
    fun markAllRead(ctx: Context) {
        db(ctx).writableDatabase.execSQL("UPDATE events SET is_read = 1 WHERE is_read = 0")
    }

    @Synchronized
    fun clear(ctx: Context) {
        db(ctx).writableDatabase.delete("events", null, null)
    }

    private fun pruneIfDue(ctx: Context) {
        val now = System.currentTimeMillis()
        if (now - lastPrune < 10 * 60_000L) return
        lastPrune = now
        val d = db(ctx).writableDatabase
        d.delete("events", "t < ?", arrayOf((now - KEEP_MS).toString()))
        d.execSQL("DELETE FROM events WHERE id NOT IN (SELECT id FROM events ORDER BY t DESC, id DESC LIMIT $KEEP_ROWS)")
    }

    /** Rule and status state, remembered between readings (and between the app and the widget's worker). */
    fun state(ctx: Context): AlertState = object : AlertState {
        private val cache = HashMap<String, RuleState?>()

        override fun get(key: String): RuleState? = synchronized(AlertStore) {
            if (cache.containsKey(key)) return cache[key]
            val st = db(ctx).readableDatabase.rawQuery("SELECT since, fired FROM alert_state WHERE k = ?", arrayOf(key))
                .use { c ->
                    if (c.moveToFirst()) RuleState(if (c.isNull(0)) null else c.getLong(0), c.getInt(1) != 0) else null
                }
            cache[key] = st
            st
        }

        override fun put(key: String, s: RuleState) {
            synchronized(AlertStore) {
                cache[key] = s
                val v = ContentValues()
                v.put("k", key)
                if (s.since == null) v.putNull("since") else v.put("since", s.since)
                v.put("fired", if (s.fired) 1 else 0)
                db(ctx).writableDatabase.insertWithOnConflict("alert_state", null, v, SQLiteDatabase.CONFLICT_REPLACE)
            }
        }
    }
}