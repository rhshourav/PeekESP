package com.rhshourav.peekesp.data

import android.content.Context
import org.json.JSONArray
import org.json.JSONObject
import java.security.SecureRandom

/**
 * Everything the user can set about alerts, kept as one piece of JSON so a new setting is a new
 * field with a default, not a migration. A missing field means "the default", never "off".
 */
object AlertConfig {
    private const val PREFS = "peek_alerts"
    private const val KEY = "settings"

    private fun prefs(ctx: Context) = ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE)

    fun load(ctx: Context): AlertSettings {
        val raw = prefs(ctx).getString(KEY, null) ?: return AlertDefaults.settings()
        return try {
            parse(JSONObject(raw))
        } catch (e: Exception) {
            AlertDefaults.settings()     // damaged: start again rather than refuse to notify at all
        }
    }

    fun save(ctx: Context, s: AlertSettings) {
        prefs(ctx).edit().putString(KEY, toJson(s).toString()).apply()
    }

    /**
     * A key other apps must present to send messages. 100 bits, from the platform's CSPRNG, in the
     * same alphabet as pairing codes (no 0/O/1/I to misread). It only lets an app *post a message
     * on this phone*; it cannot read anything. Still, it is a secret: never logged.
     */
    fun newToken(): String {
        val rng = SecureRandom()
        return buildString(20) { repeat(20) { append(Pairing.ALPHABET[rng.nextInt(Pairing.ALPHABET.length)]) } }
    }

    fun parse(o: JSONObject): AlertSettings {
        val d = AlertDefaults.settings()
        val q = o.optJSONObject("quiet")
        val dq = d.quiet
        val quiet = if (q == null) dq else QuietHours(
            enabled = q.optBoolean("enabled", dq.enabled),
            startMin = q.optInt("start", dq.startMin).coerceIn(0, 1439),
            endMin = q.optInt("end", dq.endMin).coerceIn(0, 1439),
            days = q.optInt("days", dq.days) and QuietHours.ALL_DAYS,
            criticalBreaksThrough = q.optBoolean("critical", dq.criticalBreaksThrough),
        )
        val muted = HashSet<String>()
        o.optJSONArray("muted")?.let { a -> for (i in 0 until a.length()) a.optString(i).takeIf { it.isNotEmpty() }?.let(muted::add) }

        // "rules" absent means the defaults; present (even empty) means exactly what the user left.
        val rules = o.optJSONArray("rules")?.let { a ->
            (0 until a.length()).mapNotNull { a.optJSONObject(it)?.let(::parseRule) }
        } ?: d.rules

        return AlertSettings(
            enabled = o.optBoolean("enabled", d.enabled),
            notifyOffline = o.optBoolean("notifyOffline", d.notifyOffline),
            notifyOnline = o.optBoolean("notifyOnline", d.notifyOnline),
            notifyResolved = o.optBoolean("notifyResolved", d.notifyResolved),
            notifyMessages = o.optBoolean("notifyMessages", d.notifyMessages),
            offlineAfterMin = o.optInt("offlineAfterMin", d.offlineAfterMin).coerceIn(1, 60),
            quiet = quiet,
            mutedMachines = muted,
            rules = rules,
            externalEnabled = o.optBoolean("externalEnabled", d.externalEnabled),
            externalToken = o.optString("externalToken", d.externalToken),
        )
    }

    private fun parseRule(r: JSONObject): Rule? {
        val id = r.optString("id").ifEmpty { return null }
        val metric = r.optString("metric").ifEmpty { return null }
        val op = Op.values().firstOrNull { it.name == r.optString("op") } ?: return null
        return Rule(
            id = id,
            name = r.optString("name").ifEmpty { Metrics.label(metric) },
            enabled = r.optBoolean("enabled", true),
            metric = metric,
            op = op,
            threshold = r.optDouble("threshold", 0.0).let { if (it.isNaN() || it.isInfinite()) 0.0 else it },
            forMinutes = r.optInt("forMinutes", 0).coerceIn(0, 24 * 60),
            severity = Severity.parse(r.optString("severity")),
            machine = r.optString("machine").ifEmpty { null },
            title = r.optString("title"),
            body = r.optString("body"),
        )
    }

    fun toJson(s: AlertSettings): JSONObject = JSONObject()
        .put("enabled", s.enabled)
        .put("notifyOffline", s.notifyOffline)
        .put("notifyOnline", s.notifyOnline)
        .put("notifyResolved", s.notifyResolved)
        .put("notifyMessages", s.notifyMessages)
        .put("offlineAfterMin", s.offlineAfterMin)
        .put(
            "quiet",
            JSONObject()
                .put("enabled", s.quiet.enabled).put("start", s.quiet.startMin).put("end", s.quiet.endMin)
                .put("days", s.quiet.days).put("critical", s.quiet.criticalBreaksThrough),
        )
        .put("muted", JSONArray(s.mutedMachines.toList()))
        .put("rules", JSONArray().also { a -> s.rules.forEach { a.put(ruleJson(it)) } })
        .put("externalEnabled", s.externalEnabled)
        .put("externalToken", s.externalToken)

    private fun ruleJson(r: Rule) = JSONObject()
        .put("id", r.id).put("name", r.name).put("enabled", r.enabled).put("metric", r.metric)
        .put("op", r.op.name).put("threshold", r.threshold).put("forMinutes", r.forMinutes)
        .put("severity", r.severity.name).put("machine", r.machine ?: "")
        .put("title", r.title).put("body", r.body)
}