package com.rhshourav.peekesp

import com.rhshourav.peekesp.widget.Palette
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import kotlin.math.max
import kotlin.math.min
import kotlin.math.pow

/** Keeps both themes legible: change a colour and this says whether the text is still readable on it. */
class PaletteTest {
    private fun channel(v: Int): Double {
        val x = v / 255.0
        return if (x <= 0.03928) x / 12.92 else ((x + 0.055) / 1.055).pow(2.4)
    }

    private fun luminance(c: Int): Double =
        0.2126 * channel((c shr 16) and 255) + 0.7152 * channel((c shr 8) and 255) + 0.0722 * channel(c and 255)

    private fun contrast(a: Int, b: Int): Double {
        val la = luminance(a)
        val lb = luminance(b)
        return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)
    }

    @Test fun everyTextColourIsReadableOnThePageAndOnTheCardInBothThemes() {
        for (dark in listOf(true, false)) {
            Palette.forMode(dark) {
                val named = mapOf(
                    "TEXT" to Palette.TEXT, "DIM" to Palette.DIM, "CYAN" to Palette.CYAN,
                    "MAGENTA" to Palette.MAGENTA, "AMBER" to Palette.AMBER, "GREEN" to Palette.GREEN,
                    "RED" to Palette.RED, "VIOLET" to Palette.VIOLET,
                )
                for ((name, colour) in named) {
                    for ((onName, surface) in mapOf("BG" to Palette.BG, "PANEL" to Palette.PANEL)) {
                        val ratio = contrast(colour, surface)
                        assertTrue("$name on $onName in ${if (dark) "dark" else "light"} is $ratio", ratio >= 4.5)
                    }
                }
            }
        }
    }

    @Test fun textOnAnAccentFillIsReadableInBothThemes() {
        for (dark in listOf(true, false)) {
            Palette.forMode(dark) {
                assertTrue(contrast(Palette.ON_ACCENT, Palette.CYAN) >= 4.5)
            }
        }
    }

    @Test fun aModeForcedForADrawIsScopedToIt() {
        val before = Palette.isDark
        Palette.forMode(!before) {
            assertEquals(!before, Palette.isDark)
            // And it nests: the widget can draw inside a screen that is in the other mode.
            Palette.forMode(before) { assertEquals(before, Palette.isDark) }
            assertEquals(!before, Palette.isDark)
        }
        assertEquals(before, Palette.isDark)
    }

    @Test fun theTwoModesReallyDiffer() {
        val dark = Palette.forMode(true) { Palette.BG }
        val light = Palette.forMode(false) { Palette.BG }
        assertTrue(dark != light)
    }
}