package com.rhshourav.peekesp.data

import kotlin.math.abs
import kotlin.math.max

/** How a metric usually behaves on one machine, in terms that one odd afternoon cannot move. */
data class Baseline(val median: Double, val mad: Double, val n: Int)

/**
 * "Unusual" means unusual for *this* machine: judged against its own recent behaviour, not a fixed
 * number. A build server at 80% CPU all day is normal; a quiet NAS doing 40 Mbps at 3 a.m. is not.
 *
 * Uses the median and the median absolute deviation, not the mean and standard deviation: the
 * odd spike that is exactly what we are hunting would otherwise drag its own yardstick upward.
 */
object Anomaly {
    /** Six hours of ten-minute buckets. Less than this and "normal" is a guess, so nothing is called unusual. */
    const val MIN_POINTS = 36

    fun baseline(values: List<Double>): Baseline? {
        if (values.size < MIN_POINTS) return null
        val med = median(values)
        return Baseline(med, median(values.map { abs(it - med) }), values.size)
    }

    fun median(v: List<Double>): Double {
        val s = v.sorted()
        val n = s.size
        return if (n % 2 == 1) s[n / 2] else (s[n / 2 - 1] + s[n / 2]) / 2.0
    }

    /**
     * True if [v] is more than [k] spreads above normal and at least [floorAbs]. The spread has a
     * floor of its own: a machine that is rock steady has a spread near zero, which would make any
     * wobble look like an emergency.
     */
    fun isUnusual(v: Double, b: Baseline, k: Double, floorAbs: Double): Boolean {
        val spread = max(b.mad * 1.4826, max(b.median * 0.1, floorAbs * 0.5))
        return v >= floorAbs && v > b.median + k * spread
    }
}

/** Where a rule or a machine's status stands between readings: since when it has been true, and whether it has fired. */
data class RuleState(val since: Long?, val fired: Boolean)

interface AlertState {
    fun get(key: String): RuleState?
    fun put(key: String, s: RuleState)
}

/**
 * Turns a reading into the things worth telling you.
 *
 * It only ever sees readings the relay actually answered with: when the phone has no internet
 * nothing is evaluated, so a dead phone signal can never be reported as a machine going offline.
 * That is the whole point of keeping the two apart.
 *
 * Pure: no Android, no clock. State lives behind [AlertState] and the baseline behind a function,
 * so every rule here is tested without a phone.
 */
class AlertEngine(
    private val state: AlertState,
    private val baseline: (machine: String, metric: String) -> Baseline?,
) {
    companion object {
        /** A message older than this is filed away silently: a backlog should not ring the phone. */
        const val STALE_MESSAGE_MS = 6 * 3_600_000L
    }

    fun evaluate(s: AlertSettings, machines: List<Machine>, fetchedAtMs: Long): List<NewEvent> {
        val out = ArrayList<NewEvent>()
        for (m in machines) {
            val muted = m.host in s.mutedMachines
            val push = fetchedAtMs - m.ageS * 1000L
            val offline = m.ageS * 1000L >= s.offlineAfterMin * 60_000L

            status(s, m, push, offline, muted, fetchedAtMs, out)
            messages(s, m, muted, fetchedAtMs, out)
            // A machine that is offline is showing its last reading, not its state. Judging that
            // against thresholds would raise the same alarm over and over about a moment long past.
            if (!offline) rules(s, m, push, muted, fetchedAtMs, out)
        }
        return out
    }

    private fun status(
        s: AlertSettings, m: Machine, push: Long, offline: Boolean, muted: Boolean, now: Long,
        out: MutableList<NewEvent>,
    ) {
        val key = "status:${m.host}"
        val prev = state.get(key)
        when {
            // First sight of this machine: remember how it is, say nothing.
            prev == null -> state.put(key, RuleState(if (offline) push else null, offline))
            offline && !prev.fired -> {
                state.put(key, RuleState(push, true))
                out += NewEvent(
                    key = "offline:${m.host}:$push", t = now, severity = Severity.WARNING,
                    title = "${m.host} went offline",
                    body = "Nothing heard for ${Fmt.ago(m.ageS.toLong())}.",
                    source = "PeekESP", machine = m.host, kind = EventKind.STATUS,
                    silent = !s.notifyOffline || muted,
                )
            }
            !offline && prev.fired -> {
                val silentFor = prev.since?.let { Fmt.ago(((now - it) / 1000).coerceAtLeast(0)) }
                state.put(key, RuleState(null, false))
                out += NewEvent(
                    key = "online:${m.host}:$now", t = now, severity = Severity.INFO,
                    title = "${m.host} is back online",
                    body = if (silentFor != null) "It had been silent for $silentFor." else "",
                    source = "PeekESP", machine = m.host, kind = EventKind.STATUS,
                    silent = !s.notifyOnline || muted,
                )
            }
        }
    }

    private fun messages(s: AlertSettings, m: Machine, muted: Boolean, now: Long, out: MutableList<NewEvent>) {
        for (msg in m.messages) {
            val t = msg.tsMs ?: now
            out += NewEvent(
                key = "msg:${m.host}:${msg.id}", t = t, severity = msg.severity,
                title = msg.title, body = msg.body, source = msg.source, machine = m.host, kind = EventKind.MESSAGE,
                silent = !s.notifyMessages || muted || t < now - STALE_MESSAGE_MS,
            )
        }
    }

    private fun rules(
        s: AlertSettings, m: Machine, push: Long, muted: Boolean, now: Long, out: MutableList<NewEvent>,
    ) {
        for (rule in s.rules) {
            if (!rule.enabled) continue
            if (rule.machine != null && rule.machine != m.host) continue

            val def = Metrics.def(rule.metric)
            val v = Metrics.valueOf(m, rule.metric)
            var normal: Double? = null
            val tripped = v != null && when (rule.op) {
                Op.ABOVE -> v >= rule.threshold
                Op.BELOW -> v <= rule.threshold
                Op.UNUSUAL -> baseline(m.host, rule.metric)?.let { b ->
                    normal = b.median
                    Anomaly.isUnusual(v, b, rule.threshold, Metrics.floorFor(rule.metric))
                } == true
            }

            val key = "rule:${rule.id}:${m.host}"
            val st = state.get(key) ?: RuleState(null, false)
            if (tripped) {
                val since = st.since ?: push
                var fired = st.fired
                // "Sustained" is measured in the machine's own clock (when it pushed), not ours.
                if (!fired && push - since >= rule.forMinutes * 60_000L) {
                    fired = true
                    out += alert(rule, def, m, v ?: 0.0, normal, since, muted, now)
                }
                state.put(key, RuleState(since, fired))
            } else {
                if (st.fired) out += resolved(rule, def, m, s.notifyResolved, muted, now)
                if (st.since != null || st.fired) state.put(key, RuleState(null, false))
            }
        }
    }

    private fun vars(rule: Rule, def: MetricDef, m: Machine, value: Double, normal: Double?): Map<String, String> =
        mapOf(
            "machine" to m.host,
            "metric" to def.label,
            "value" to Metrics.format(value, def.unit),
            "threshold" to if (rule.op == Op.UNUSUAL) trim(rule.threshold) else Metrics.format(rule.threshold, def.unit),
            "unit" to def.unit,
            "normal" to (normal?.let { Metrics.format(it, def.unit) } ?: ""),
            "duration" to if (rule.forMinutes > 0) "${rule.forMinutes} min" else "",
            "severity" to rule.severity.label,
            "rule" to rule.name,
        )

    private fun alert(
        rule: Rule, def: MetricDef, m: Machine, value: Double, normal: Double?, since: Long,
        muted: Boolean, now: Long,
    ): NewEvent {
        val v = vars(rule, def, m, value, normal)
        val defaultTitle = when (rule.op) {
            Op.UNUSUAL -> "{machine}: unusual {metric}"
            else -> "{machine}: {metric} {opword} {threshold}"
        }.replace("{opword}", rule.op.label)
        val defaultBody = when (rule.op) {
            Op.UNUSUAL -> "{metric} is {value}; this machine is normally around {normal}."
            else -> "{metric} is {value}" + if (rule.forMinutes > 0) " and has been for {duration}." else "."
        }
        return NewEvent(
            key = "rule:${rule.id}:${m.host}:$since", t = now, severity = rule.severity,
            title = Template.render(rule.title.ifBlank { defaultTitle }, v),
            body = Template.render(rule.body.ifBlank { defaultBody }, v),
            source = rule.name, machine = m.host, kind = EventKind.ALERT, silent = muted,
        )
    }

    private fun resolved(
        rule: Rule, def: MetricDef, m: Machine, notify: Boolean, muted: Boolean, now: Long,
    ) = NewEvent(
        key = "resolved:${rule.id}:${m.host}:$now", t = now, severity = Severity.INFO,
        title = "${m.host}: ${def.label} back to normal", body = "",
        source = rule.name, machine = m.host, kind = EventKind.ALERT, silent = !notify || muted,
    )

    private fun trim(v: Double): String = if (v == Math.rint(v)) v.toLong().toString() else v.toString()
}