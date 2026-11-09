package com.haven.app;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertTrue;

import org.junit.Test;

/** The Today ring and the sleeve's light share one colour rule (Sleeve.ringHue, index.html ringHue). */
public class RingColourTest {
    @Test
    public void hueRunsGreenToYellowAtTheActionLevelToRedAtTheLimit() {
        assertEquals(140, Sleeve.ringHue(0, 400));
        assertEquals(91, Sleeve.ringHue(50, 400));
        assertEquals(42, Sleeve.ringHue(100, 400));
        assertEquals(21, Sleeve.ringHue(250, 400));
        assertEquals(0, Sleeve.ringHue(400, 400));
        assertEquals(0, Sleeve.ringHue(900, 400));
    }

    @Test
    public void hueNeverGoesBackUpAsPointsRise() {
        int last = 999;
        for (double p = 0; p <= 450; p += 0.5) {
            int h = Sleeve.ringHue(p, 400);
            assertTrue("hue rose at " + p, h <= last);
            last = h;
        }
    }

    @Test
    public void aLowerPersonalLimitStillEndsRed() {
        assertEquals(0, Sleeve.ringHue(80, 80));
        assertTrue(Sleeve.ringHue(40, 80) > 42);
    }

    @Test
    public void ledColourIsTheSameHueGammaCorrected() {
        assertEquals("00ff00", Sleeve.ledHex(120));
        assertEquals("ff0000", Sleeve.ledHex(0));
        // amber-yellow at the action level: the LED's strong green is pulled down so it reads yellow, not lime
        assertEquals("ff7400", Sleeve.ledHex(42));
    }
}
