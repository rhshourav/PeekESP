package com.rhshourav.peekesp.data

import android.content.Context
import android.net.ConnectivityManager
import android.net.NetworkCapabilities

/**
 * Whether the phone itself has internet, which is a different question from whether a
 * machine is on. A failed request could be either: this tells them apart, so a chart can
 * say "your phone had no connection" instead of blaming a machine that was fine.
 */
object Net {
    /** True only for a network the system has actually validated as reaching the internet. */
    fun isOnline(ctx: Context): Boolean {
        val cm = ctx.getSystemService(ConnectivityManager::class.java) ?: return false
        val net = cm.activeNetwork ?: return false
        val caps = cm.getNetworkCapabilities(net) ?: return false
        return caps.hasCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET) &&
            caps.hasCapability(NetworkCapabilities.NET_CAPABILITY_VALIDATED)
    }

    /** Why a request failed: no internet on the phone, or internet fine and the relay not answering. */
    fun failureKind(ctx: Context): LinkKind = failureKind(isOnline(ctx))

    fun failureKind(phoneOnline: Boolean): LinkKind =
        if (phoneOnline) LinkKind.RELAY_DOWN else LinkKind.PHONE_OFFLINE
}