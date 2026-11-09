package com.vibeguard.app;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

import org.json.JSONArray;
import org.json.JSONObject;
import org.junit.Test;

import java.time.LocalDate;
import java.util.Arrays;
import java.util.List;

/** The day log behind the Overview's Week, Month and Year views (History.java). */
public class HistoryTest {
    static final String TODAY = "2026-11-08";

    static JSONObject real(double points) throws Exception {
        return new JSONObject().put("points", points).put("a8", History.a8FromPoints(points)).put("tool", 3600).put("limit", 400);
    }

    @Test
    public void everyDayIsKeptAndTheScreenGetsTheNewest400() throws Exception {
        JSONObject days = new JSONObject();
        LocalDate t = LocalDate.parse(TODAY);
        for (int i = 1; i <= 1000; i++) days.put(t.minusDays(i).toString(), real(50));
        List<String> shown = History.newest(days, History.SCREEN_DAYS);
        assertEquals(1000, days.length());                     // nothing is dropped from the log
        assertEquals(400, shown.size());
        assertEquals(t.minusDays(1).toString(), shown.get(0));  // newest first
        assertEquals(t.minusDays(400).toString(), shown.get(399));
        // sample days added and removed around old real days leave the real ones alone
        History.addSample(days, TODAY, History.SAMPLE_DAYS);
        History.clearSample(days);
        assertEquals(1000, days.length());
    }

    @Test
    public void sampleYearIsDeterministicAndMarked() throws Exception {
        JSONObject a = new JSONObject(), b = new JSONObject();
        int na = History.addSample(a, TODAY, History.SAMPLE_DAYS), nb = History.addSample(b, TODAY, History.SAMPLE_DAYS);
        assertEquals(na, nb);
        assertEquals(a.length(), b.length());
        for (String k : History.sortedKeys(a)) {
            assertEquals(a.getJSONObject(k).toString(), b.getJSONObject(k).toString());
            assertTrue(a.getJSONObject(k).getBoolean("sample"));
            assertEquals(400, a.getJSONObject(k).getInt("limit"));
        }
        assertFalse("never today", a.has(TODAY));
        assertTrue(History.sortedKeys(a).get(0).compareTo(LocalDate.parse(TODAY).minusDays(365).toString()) >= 0);
    }

    @Test
    public void sampleNeverOverwritesARealDay() throws Exception {
        JSONObject days = new JSONObject();
        String yesterday = LocalDate.parse(TODAY).minusDays(1).toString(), longAgo = LocalDate.parse(TODAY).minusDays(200).toString();
        days.put(yesterday, real(557).put("demoSpeed", 480));
        days.put(longAgo, real(12));
        History.addSample(days, TODAY, History.SAMPLE_DAYS);
        assertEquals(557, days.getJSONObject(yesterday).getDouble("points"), 0);
        assertFalse(days.getJSONObject(yesterday).optBoolean("sample"));
        assertEquals(12, days.getJSONObject(longAgo).getDouble("points"), 0);
        // clearing removes the sample days only
        int removed = History.clearSample(days);
        assertTrue(removed > 200);
        assertEquals(2, days.length());
        assertTrue(days.has(yesterday) && days.has(longAgo));
    }

    @Test
    public void sampleYearLooksLikeWorkAndImproves() throws Exception {
        JSONObject days = new JSONObject();
        History.addSample(days, TODAY, History.SAMPLE_DAYS);
        int n = days.length(), weekend = 0, over = 0, overLimit = 0;
        double oldSum = 0, newSum = 0; int oldN = 0, newN = 0;
        LocalDate t = LocalDate.parse(TODAY);
        for (String k : History.sortedKeys(days)) {
            JSONObject d = days.getJSONObject(k);
            LocalDate date = LocalDate.parse(k);
            double p = d.getDouble("points");
            if (date.getDayOfWeek().getValue() >= 6) weekend++;
            if (p >= 100) over++;
            if (p >= 400) overLimit++;
            // tool time agrees with the points (a 4 to 12 m/s^2 tool, at most 8 h)
            double hours = d.getDouble("tool") / 3600;
            assertTrue(k, hours > 0 && hours <= 8.01);
            double a = Math.sqrt(p / (2 * hours));
            assertTrue(k + " a=" + a, a >= 3.99 && a <= 12.01);
            assertEquals(History.a8FromPoints(p), d.getDouble("a8"), 0.002);
            long age = t.toEpochDay() - date.toEpochDay();
            if (age > 195) { oldSum += p; oldN++; } else if (age < 165) { newSum += p; newN++; }
        }
        assertTrue("about 240 working days: " + n, n >= 220 && n <= 260);
        assertTrue("most weekends off: " + weekend, weekend < 30);
        assertTrue("some days over the action level: " + over, over > n * 0.2 && over < n * 0.6);
        assertTrue("a handful over the limit: " + overLimit, overLimit >= 1 && overLimit <= 12);
        double oldAvg = oldSum / oldN, newAvg = newSum / newN;
        System.out.printf("sample year: %d work days, %d weekend, %d over 100, %d over 400, older avg %.0f, recent avg %.0f%n",
                n, weekend, over, overLimit, oldAvg, newAvg);
        assertTrue("older half higher: " + oldAvg, oldAvg > 120 && oldAvg < 190);
        assertTrue("recent half lower: " + newAvg, newAvg > 55 && newAvg < 100);
    }

    @Test
    public void monthsAreTheEnergyMeanOfDaysNeverASum() throws Exception {
        JSONObject days = new JSONObject();
        // October: three working days of 100, 400 and 25 points (A(8) 2.5, 5.0, 1.25), one day off
        days.put("2026-10-05", real(100));
        days.put("2026-10-06", real(400));
        days.put("2026-10-07", real(25));
        days.put("2026-10-08", real(0));
        // an older entry without a8 (from before it was stored) still counts, from its points
        days.put("2026-09-10", new JSONObject().put("points", 64).put("tool", 1800));
        JSONObject today = real(50).put("demoSpeed", 480);
        JSONArray m = History.months(days, TODAY, today, 12);
        assertEquals(12, m.length());
        assertEquals("2025-12", m.getJSONObject(0).getString("month"));
        JSONObject oct = m.getJSONObject(10), sep = m.getJSONObject(9), nov = m.getJSONObject(11);
        assertEquals("2026-10", oct.getString("month"));
        assertEquals(3, oct.getInt("days"));
        // energy mean: sqrt((2.5^2 + 5^2 + 1.25^2) / 3) = 3.307 m/s^2 -> 175 points per working day (the mean of
        // the points), not the 525-point sum
        assertEquals(Math.sqrt((6.25 + 25 + 1.5625) / 3), oct.getDouble("a8"), 0.001);
        assertEquals(175, oct.getDouble("points"), 0.2);
        assertEquals(2, oct.getInt("overAction"));
        assertEquals(1, oct.getInt("overLimit"));
        assertEquals(3600, oct.getLong("tool"));
        assertEquals(64, sep.getDouble("points"), 0.2);
        assertEquals("2026-11", nov.getString("month"));
        assertEquals(1, nov.getInt("days"));
        assertTrue(nov.getBoolean("demo"));
        assertFalse(oct.getBoolean("sample"));
        assertEquals(0, m.getJSONObject(0).getInt("days"));
        assertEquals(0, m.getJSONObject(0).getDouble("points"), 0);
    }

    @Test
    public void energyMeanMatchesThePointsMean() {
        List<Double> a8 = Arrays.asList(History.a8FromPoints(30), History.a8FromPoints(90), History.a8FromPoints(180));
        assertEquals(100, History.pointsFromA8(History.energyMeanA8(a8)), 1e-9);
        assertEquals(0, History.energyMeanA8(Arrays.<Double>asList()), 0);
    }
}
