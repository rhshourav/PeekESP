package com.rhshourav.peekesp.data

import android.content.ContentValues
import android.content.Context
import android.database.Cursor
import android.database.DatabaseUtils
import android.database.sqlite.SQLiteDatabase
import android.database.sqlite.SQLiteOpenHelper
import kotlin.math.abs

/** One chart point: the average of every reading that fell in a time bucket. Null means no sensor. */
data class Bucket(
    val t: Long,
    val cpu: Double?, val ram: Double?, val temp: Double?, val storage: Double?,
    val rx: Double?, val tx: Double?, val bat: Double?,
    val charging: Boolean,
)

data class Stat(val min: Double, val avg: Double, val max: Double)

data class Stats(
    val samples: Int,
    val cpu: Stat?, val ram: Stat?, val temp: Stat?, val storage: Stat?,
    val rx: Stat?, val tx: Stat?, val bat: Stat?,
)

/** Everything a dashboard needs to draw one machine over one range. */
data class ChartData(
    val range: HistoryRange,
    val fromMs: Long,
    val toMs: Long,
    val bucketMs: Long,
    val buckets: List<Bucket>,
    val stats: Stats,
    /** What was happening across the window: online, machine offline, phone offline, relay unreachable. */
    val spans: List<Span>,
)

/** The two choices the user has: keep a history at all, and for how long. */
object HistoryConfig {
    private fun p(ctx: Context) = ctx.getSharedPreferences("peek_history", Context.MODE_PRIVATE)

    fun enabled(ctx: Context): Boolean = p(ctx).getBoolean("enabled", true)
    fun setEnabled(ctx: Context, on: Boolean) = p(ctx).edit().putBoolean("enabled", on).apply()

    fun retentionDays(ctx: Context): Int = p(ctx).getInt("days", 7).coerceIn(1, 30)
    fun setRetentionDays(ctx: Context, days: Int) = p(ctx).edit().putInt("days", days.coerceIn(1, 30)).apply()
}

private class HistoryDb(context: Context) : SQLiteOpenHelper(context.applicationContext, "history.db", null, 2) {
    override fun onConfigure(db: SQLiteDatabase) {
        // Readers (the dashboard) never block the writer (the widget's worker), or the other way round.
        db.enableWriteAheadLogging()
    }

    override fun onCreate(db: SQLiteDatabase) {
        db.execSQL(
            "CREATE TABLE samples (" +
                "machine TEXT NOT NULL, t INTEGER NOT NULL, " +
                "cpu REAL, ram REAL, temp REAL, storage REAL, rx REAL, tx REAL, " +
                "bat INTEGER, charging INTEGER NOT NULL DEFAULT 0, ac INTEGER NOT NULL DEFAULT 0)",
        )
        db.execSQL("CREATE INDEX samples_machine_t ON samples (machine, t)")
        createV2(db)
    }

    override fun onUpgrade(db: SQLiteDatabase, oldVersion: Int, newVersion: Int) {
        // Readings are kept; only the new tables are added.
        if (oldVersion < 2) createV2(db)
    }

    private fun createV2(db: SQLiteDatabase) {
        // The phone's own view of its path to the relay, at the moments it looked.
        db.execSQL("CREATE TABLE link (t INTEGER NOT NULL, state INTEGER NOT NULL)")
        db.execSQL("CREATE INDEX link_t ON link (t)")
        // Stretches when the relay said a machine had been silent: it last pushed at `push`
        // and was still silent when we looked at `seen`.
        db.execSQL("CREATE TABLE offline (machine TEXT NOT NULL, push INTEGER NOT NULL, seen INTEGER NOT NULL)")
        db.execSQL("CREATE INDEX offline_machine_seen ON offline (machine, seen)")
    }
}

/**
 * The log behind the dashboard. The relay only ever holds the latest reading, so
 * this is the only history there is, and it lives on the phone.
 *
 * Two things write to it: the dashboard while it is open (about every 5 s) and the
 * widget's background refresh (every 15 minutes, the platform's floor). So charts
 * are dense while the app is in use and sparse overnight, and they say so.
 */
object History {
    /** At most one stored reading per machine per this long, however often they arrive. */
    const val MIN_GAP_MS = 15_000L

    private const val DB_NAME = "history.db"
    private const val PRUNE_EVERY_MS = 10 * 60_000L

    /** While nothing changes, say again every minute: the log needs to show "still fine", not just changes. */
    private const val LINK_HEARTBEAT_MS = 60_000L

    @Volatile private var helper: HistoryDb? = null
    private val lastT = HashMap<String, Long>()
    private val openOffline = HashMap<String, Pair<Long, Long>>()   // machine -> (rowid, push)
    private var lastLink: LinkEvent? = null
    private var lastPrune = 0L

    private fun db(ctx: Context): HistoryDb =
        helper ?: synchronized(this) { helper ?: HistoryDb(ctx).also { helper = it } }

    /**
     * Whether a reading belongs in the log. A machine that has gone quiet keeps
     * being reported with the same push time, so "not newer than the last one by
     * at least the gap" also means "this is one we already have".
     */
    fun shouldLog(lastT: Long?, t: Long, minGapMs: Long): Boolean = lastT == null || t - lastT >= minGapMs

    /**
     * Stores one row per machine, stamped with when the machine pushed, not when
     * we asked: that is [fetchedAtMs] minus the relay's age for it.
     */
    @Synchronized
    fun record(ctx: Context, machines: List<Machine>, fetchedAtMs: Long) {
        if (machines.isEmpty() || !HistoryConfig.enabled(ctx)) return
        val d = db(ctx).writableDatabase
        d.beginTransaction()
        try {
            for (m in machines) {
                val t = fetchedAtMs - m.ageS * 1000L
                // The relay answered, and says this machine has been quiet for a while. Only now
                // can that be called the machine's doing, so this is where it is written down.
                if (m.ageS * 1000L >= Availability.OFFLINE_AFTER_MS) markOffline(d, m.host, t, fetchedAtMs)
                val last = lastT[m.host] ?: maxT(d, m.host)
                if (last != null) lastT[m.host] = last
                if (!shouldLog(last, t, MIN_GAP_MS)) continue

                val v = ContentValues()
                v.put("machine", m.host)
                v.put("t", t)
                v.putD("cpu", m.cpu); v.putD("ram", m.ram); v.putD("temp", m.tempC)
                v.putD("storage", m.storagePct); v.putD("rx", m.rxKbps); v.putD("tx", m.txKbps)
                val bat = m.batteryPct
                if (bat == null) v.putNull("bat") else v.put("bat", bat)
                v.put("charging", if (m.charging) 1 else 0)
                v.put("ac", if (m.ac) 1 else 0)
                d.insert("samples", null, v)
                lastT[m.host] = t
            }
            d.setTransactionSuccessful()
        } finally {
            d.endTransaction()
        }
        val now = System.currentTimeMillis()
        if (now - lastPrune > PRUNE_EVERY_MS) {
            lastPrune = now
            pruneLocked(ctx, d)
        }
    }

    /** One row per silence, extended each time we look and it is still going on. */
    private fun markOffline(d: SQLiteDatabase, host: String, push: Long, seen: Long) {
        val open = openOffline[host]
        val row = if (open != null && abs(open.second - push) <= 2_000L) open.first else findOffline(d, host, push)
        if (row != null) {
            d.execSQL("UPDATE offline SET seen = ? WHERE rowid = ?", arrayOf<Any>(seen, row))
            openOffline[host] = row to push
        } else {
            val v = ContentValues()
            v.put("machine", host)
            v.put("push", push)
            v.put("seen", seen)
            openOffline[host] = d.insert("offline", null, v) to push
        }
    }

    /** After a restart the in-memory note is gone; the same silence is still in the table. */
    private fun findOffline(d: SQLiteDatabase, host: String, push: Long): Long? =
        d.rawQuery(
            "SELECT rowid FROM offline WHERE machine = ? AND push BETWEEN ${push - 2_000} AND ${push + 2_000} LIMIT 1",
            arrayOf(host),
        ).use { c -> if (c.moveToFirst()) c.getLong(0) else null }

    /**
     * Notes how the phone's path to the relay is doing. A change is always written; "same as
     * before" is written once a minute so a long outage (or a long fine spell) leaves a trail.
     * Not tied to any machine: it describes the phone.
     */
    @Synchronized
    fun recordLink(ctx: Context, kind: LinkKind, nowMs: Long) {
        if (!HistoryConfig.enabled(ctx)) return
        val d = db(ctx).writableDatabase
        val last = lastLink ?: d.rawQuery("SELECT t, state FROM link ORDER BY t DESC LIMIT 1", null).use { c ->
            if (c.moveToFirst()) LinkEvent(c.getLong(0), LinkKind.of(c.getInt(1))) else null
        }
        if (last != null && last.kind == kind && nowMs - last.t < LINK_HEARTBEAT_MS) {
            lastLink = last
            return
        }
        val v = ContentValues()
        v.put("t", nowMs)
        v.put("state", kind.code)
        d.insert("link", null, v)
        lastLink = LinkEvent(nowMs, kind)
    }

    private fun ContentValues.putD(key: String, v: Double?) {
        if (v == null) putNull(key) else put(key, v)
    }

    private fun maxT(d: SQLiteDatabase, host: String): Long? =
        d.rawQuery("SELECT MAX(t) FROM samples WHERE machine = ?", arrayOf(host)).use { c ->
            if (c.moveToFirst() && !c.isNull(0)) c.getLong(0) else null
        }

    /** Drops what is older than the retention. Cheap: the index is on (machine, t). */
    @Synchronized
    fun prune(ctx: Context) = pruneLocked(ctx, db(ctx).writableDatabase)

    private fun pruneLocked(ctx: Context, d: SQLiteDatabase) {
        val cutoff = System.currentTimeMillis() - HistoryConfig.retentionDays(ctx) * 86_400_000L
        d.delete("samples", "t < ?", arrayOf(cutoff.toString()))
        d.delete("link", "t < ?", arrayOf(cutoff.toString()))
        d.delete("offline", "seen < ?", arrayOf(cutoff.toString()))
    }

    @Synchronized
    fun clear(ctx: Context) {
        val d = db(ctx).writableDatabase
        d.delete("samples", null, null)
        d.delete("link", null, null)
        d.delete("offline", null, null)
        lastT.clear()
        openOffline.clear()
        lastLink = null
        d.execSQL("VACUUM")
    }

    fun count(ctx: Context): Long = DatabaseUtils.queryNumEntries(db(ctx).readableDatabase, "samples")

    fun sizeBytes(ctx: Context): Long = ctx.getDatabasePath(DB_NAME).length()

    /** One machine over one range: bucketed averages for the lines, and exact min/avg/max for the numbers. */
    fun load(ctx: Context, machine: String, range: HistoryRange): ChartData {
        val now = System.currentTimeMillis()
        val from = now - range.ms
        val bucket = ChartMath.bucketMs(range)
        val d = db(ctx).readableDatabase

        // The numbers in the SQL are Longs we computed, not user input; the machine name is bound.
        val buckets = ArrayList<Bucket>()
        d.rawQuery(
            "SELECT AVG(t), AVG(cpu), AVG(ram), AVG(temp), AVG(storage), AVG(rx), AVG(tx), AVG(bat), MAX(charging) " +
                "FROM samples WHERE machine = ? AND t >= $from GROUP BY t / $bucket ORDER BY 1",
            arrayOf(machine),
        ).use { c ->
            while (c.moveToNext()) {
                buckets += Bucket(
                    t = c.getDouble(0).toLong(),
                    cpu = c.dbl(1), ram = c.dbl(2), temp = c.dbl(3), storage = c.dbl(4),
                    rx = c.dbl(5), tx = c.dbl(6), bat = c.dbl(7),
                    charging = !c.isNull(8) && c.getInt(8) != 0,
                )
            }
        }

        val stats = d.rawQuery(
            "SELECT COUNT(*), " +
                "MIN(cpu), AVG(cpu), MAX(cpu), MIN(ram), AVG(ram), MAX(ram), " +
                "MIN(temp), AVG(temp), MAX(temp), MIN(storage), AVG(storage), MAX(storage), " +
                "MIN(rx), AVG(rx), MAX(rx), MIN(tx), AVG(tx), MAX(tx), MIN(bat), AVG(bat), MAX(bat) " +
                "FROM samples WHERE machine = ? AND t >= $from",
            arrayOf(machine),
        ).use { c ->
            if (!c.moveToFirst()) {
                Stats(0, null, null, null, null, null, null, null)
            } else {
                Stats(
                    samples = c.getInt(0),
                    cpu = c.stat(1), ram = c.stat(4), temp = c.stat(7), storage = c.stat(10),
                    rx = c.stat(13), tx = c.stat(16), bat = c.stat(19),
                )
            }
        }

        // Phone-side outages: read from a little before the window, since one holds into it.
        val link = ArrayList<LinkEvent>()
        d.rawQuery(
            "SELECT t, state FROM link WHERE t >= ${from - Availability.LINK_HOLD_MS} ORDER BY t", null,
        ).use { c -> while (c.moveToNext()) link += LinkEvent(c.getLong(0), LinkKind.of(c.getInt(1))) }

        // Machine silences: a machine counts as offline once it has been quiet a minute.
        val silent = ArrayList<Pair<Long, Long>>()
        d.rawQuery("SELECT push, seen FROM offline WHERE machine = ? AND seen >= $from", arrayOf(machine)).use { c ->
            while (c.moveToNext()) {
                val start = c.getLong(0) + Availability.OFFLINE_AFTER_MS
                if (c.getLong(1) > start) silent += start to c.getLong(1)
            }
        }

        val spans = Availability.build(
            from = from, to = now, times = buckets.map { it.t },
            joinGapMs = ChartMath.gapMs(bucket), tailMs = bucket,
            link = link, machineOffline = silent,
        )
        return ChartData(range, from, now, bucket, buckets, stats, spans)
    }

    private fun Cursor.dbl(i: Int): Double? = if (isNull(i)) null else getDouble(i)

    private fun Cursor.stat(i: Int): Stat? =
        if (isNull(i)) null else Stat(getDouble(i), getDouble(i + 1), getDouble(i + 2))
}