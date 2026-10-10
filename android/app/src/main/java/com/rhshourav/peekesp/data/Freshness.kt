package com.rhshourav.peekesp.data

object Freshness {
    const val DEFAULT_POLL_S = 5

    /** Two missed 15-minute widget updates, plus slack. Past this the link reads STALE. */
    const val WIDGET_STALE_S = 35 * 60L

    /**
     * How old a reading really is: the relay's own age at fetch time, plus the time
     * since we fetched it. Without the second term a phone that lost its connection
     * shows every machine as permanently fresh.
     *
     * The caller passes whichever clock it can trust. The widget's cache outlives
     * the process, so it uses the wall clock; a live screen should use
     * SystemClock.elapsedRealtime(), which a clock change cannot move.
     */
    fun ageSeconds(relayAgeS: Int, fetchedAtMs: Long, nowMs: Long): Long =
        relayAgeS + (nowMs - fetchedAtMs).coerceAtLeast(0) / 1000

    /** A machine is offline after max(60 s, 5 x poll interval) - the Pi host's rule. */
    fun offlineAfterSeconds(pollS: Int): Int = maxOf(60, 5 * pollS)
}
