package com.rhshourav.peekesp.data

import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import org.json.JSONArray
import org.json.JSONObject
import java.security.KeyStore
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

data class Setup(val name: String, val relay: String, val code: String)

/**
 * The pairing code is the credential, so it is stored encrypted with an AES-GCM
 * key that lives in the Android Keystore and never leaves it. Never logged, never
 * sent (only the derived stream and token travel).
 */
class SetupStore(context: Context) {
    private val prefs = context.getSharedPreferences("peek_setups", Context.MODE_PRIVATE)

    fun has(): Boolean = prefs.contains(KEY)

    fun load(): List<Setup> {
        val blob = prefs.getString(KEY, null) ?: return emptyList()
        return try {
            val json = String(decrypt(Base64.decode(blob, Base64.NO_WRAP)), Charsets.UTF_8)
            val arr = JSONArray(json)
            List(arr.length()) { i ->
                val o = arr.getJSONObject(i)
                Setup(o.getString("name"), o.getString("relay"), o.getString("code"))
            }
        } catch (e: Exception) {
            // Key lost or blob damaged. Forget it and let the app ask to pair again.
            prefs.edit().remove(KEY).apply()
            emptyList()
        }
    }

    fun save(setups: List<Setup>) {
        val arr = JSONArray()
        setups.forEach {
            arr.put(JSONObject().put("name", it.name).put("relay", it.relay).put("code", it.code))
        }
        val sealed = encrypt(arr.toString().toByteArray(Charsets.UTF_8))
        prefs.edit().putString(KEY, Base64.encodeToString(sealed, Base64.NO_WRAP)).apply()
    }

    fun clear() = prefs.edit().remove(KEY).apply()

    private fun key(): SecretKey {
        val ks = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
        (ks.getKey(ALIAS, null) as? SecretKey)?.let { return it }
        val gen = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore")
        gen.init(
            KeyGenParameterSpec.Builder(
                ALIAS, KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT,
            )
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .setKeySize(256)
                .build(),
        )
        return gen.generateKey()
    }

    /** iv (12 bytes) + ciphertext and tag. */
    private fun encrypt(plain: ByteArray): ByteArray {
        val cipher = Cipher.getInstance("AES/GCM/NoPadding")
        cipher.init(Cipher.ENCRYPT_MODE, key())
        return cipher.iv + cipher.doFinal(plain)
    }

    private fun decrypt(blob: ByteArray): ByteArray {
        val cipher = Cipher.getInstance("AES/GCM/NoPadding")
        cipher.init(Cipher.DECRYPT_MODE, key(), GCMParameterSpec(128, blob.copyOfRange(0, IV_LEN)))
        return cipher.doFinal(blob, IV_LEN, blob.size - IV_LEN)
    }

    private companion object {
        const val KEY = "setups"
        const val ALIAS = "peekesp.setups"
        const val IV_LEN = 12
    }
}
