package com.rhshourav.peekesp.widget

import android.content.Context
import android.graphics.Typeface

/** Montserrat (SIL OFL, licence in assets/licenses) - the face on the device. */
object Fonts {
    private val cache = HashMap<Int, Typeface>()

    /** One variable font file, instanced at the weight asked for (API 26+). */
    @Synchronized
    fun get(ctx: Context, weight: Int): Typeface = cache.getOrPut(weight) {
        Typeface.Builder(ctx.applicationContext.assets, "fonts/Montserrat.ttf")
            .setFontVariationSettings("'wght' $weight")
            .build() ?: Typeface.DEFAULT
    }
}
