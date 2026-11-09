package com.haven.app;

import com.haven.app.engine.Exposure;

import org.json.JSONArray;
import org.json.JSONObject;

import java.time.DayOfWeek;
import java.time.LocalDate;
import java.util.ArrayList;
import java.util.Collections;
import java.util.Iterator;
import java.util.List;
import java.util.Random;

/**
 * The daily log kept on the phone ({@link Sleeve}'s "days": one small object per date, "yyyy-MM-dd" keys) and
 * the sums the Overview needs from it. No Android classes here, so the JVM tests can run it.
 *
 * Limits are DAILY: a month is never a sum of points. A month's value is the energy mean of the daily A(8) over
 * the days with tool use, given as "average points per working day" (100 x (A8/2.5)^2), the same as the
 * backend's exposure.py daily_exposure and the Overview's weekly average.
 */
final class History {
    /**
     * Every day is kept on the phone (about 100 bytes a day, roughly 36 KB a year). The screen gets the newest
     * {@code SCREEN_DAYS} (about 13 months: enough for the Year view), so the state stays small.
     */
    static final int SCREEN_DAYS = 400;
    static final int SAMPLE_DAYS = 365;
    static final long SEED = 20261109L;

    private History() {}

    static JSONObject parse(String s) {
        try { return new JSONObject(s == null ? "{}" : s); } catch (Exception e) { return new JSONObject(); }
    }

    static List<String> sortedKeys(JSONObject days) {
        List<String> keys = new ArrayList<>();
        for (Iterator<String> it = days.keys(); it.hasNext(); ) keys.add(it.next());
        Collections.sort(keys);
        return keys;
    }

    /** The newest {@code n} dates, newest first (what the screen gets); nothing is removed from the log. */
    static List<String> newest(JSONObject days, int n) {
        List<String> keys = sortedKeys(days);
        Collections.reverse(keys);
        return keys.size() > n ? new ArrayList<>(keys.subList(0, n)) : keys;
    }

    /** Removes the sample days only; real days stay. Returns how many were removed. */
    static int clearSample(JSONObject days) {
        int n = 0;
        for (String k : sortedKeys(days)) {
            JSONObject d = days.optJSONObject(k);
            if (d != null && d.optBoolean("sample")) { days.remove(k); n++; }
        }
        return n;
    }

    /**
     * SAMPLE history for demos: the {@code count} days before {@code today} (never today, never over a day that
     * is already there), marked sample. Deterministic: the same date always gets the same made-up day.
     * Returns how many days were added.
     */
    static int addSample(JSONObject days, String today, int count) {
        LocalDate t = LocalDate.parse(today);
        int added = 0;
        for (int age = 1; age <= count; age++) {
            LocalDate d = t.minusDays(age);
            String k = d.toString();
            if (days.has(k)) continue;
            JSONObject s = sampleDay(d, age);
            if (s == null) continue;                    // a day off: nothing logged, like a real day off
            try { days.put(k, s); added++; } catch (Exception ignored) {}
        }
        return added;
    }

    /**
     * One made-up day, or null for a day off. Weekdays are worked, most weekends and a few holidays are off.
     * The story: the older half of the year is higher (about 150 points a working day), then rotation and
     * lower-vibration tools bring it down to about 75, with a one-month change-over.
     */
    static JSONObject sampleDay(LocalDate d, int age) {
        Random r = new Random(SEED * 31 + d.toString().hashCode());
        DayOfWeek w = d.getDayOfWeek();
        double off = w == DayOfWeek.SUNDAY ? 0.97 : w == DayOfWeek.SATURDAY ? 0.85 : 0.04;   // sick days on weekdays
        if (holiday(d) || r.nextDouble() < off) return null;
        // median points a working day: 125 before the change, 65 after it, blended over 30 days
        double mix = Math.max(0, Math.min(1, (age - 165) / 30.0));
        double median = 65 + (125 - 65) * mix;
        double points = Math.max(5, Math.min(520, median * Math.exp(0.6 * r.nextGaussian())));
        // tool time from a per-day tool vibration of 4 to 12 m/s^2 (hours = points / (2 a^2)), at most 8 h
        double a = 4 + 8 * r.nextDouble();
        a = Math.max(a, Math.sqrt(points / (Exposure.POINTS_PER_HOUR * 8)));
        double tool = points / (Exposure.POINTS_PER_HOUR * a * a) * 3600;
        try {
            return new JSONObject().put("points", round(points, 1)).put("a8", round(a8FromPoints(points), 3))
                    .put("tool", Math.round(tool)).put("contact", Math.round(tool * (0.8 + 0.15 * r.nextDouble())))
                    .put("limit", 400).put("sample", true);
        } catch (Exception e) {
            return null;
        }
    }

    /** Canada's fixed-date holidays plus the Christmas week. */
    static boolean holiday(LocalDate d) {
        int m = d.getMonthValue(), day = d.getDayOfMonth();
        return (m == 1 && day == 1) || (m == 7 && day == 1) || (m == 11 && day == 11)
                || (m == 12 && day >= 24) || (m == 1 && day == 2);
    }

    static double a8FromPoints(double points) { return 2.5 * Math.sqrt(Math.max(0, points) / 100); }

    static double pointsFromA8(double a8) { return 100 * (a8 / 2.5) * (a8 / 2.5); }

    /** A day's A(8): the stored value, or from its points for older entries. */
    static double a8Of(JSONObject d) {
        return d.has("a8") && !d.isNull("a8") ? d.optDouble("a8", 0) : a8FromPoints(d.optDouble("points", 0));
    }

    /** Energy mean of the daily A(8) values (sqrt of the mean of A8^2); 0 for none. */
    static double energyMeanA8(List<Double> a8s) {
        if (a8s.isEmpty()) return 0;
        double s = 0;
        for (double a : a8s) s += a * a;
        return Math.sqrt(s / a8s.size());
    }

    /**
     * The last {@code count} calendar months, oldest first, the current month last (with today's live entry,
     * which may be null). Each: month "yyyy-MM", days (with tool use), a8 (energy mean), points (average per
     * working day), tool (average seconds per working day), toolTotal, overAction, overLimit, sample, demo.
     */
    static JSONArray months(JSONObject days, String today, JSONObject todayEntry, int count) throws Exception {
        LocalDate t = LocalDate.parse(today);
        LocalDate first = t.withDayOfMonth(1).minusMonths(count - 1);
        int n = count;
        List<List<Double>> a8 = new ArrayList<>();
        double[] tool = new double[n];
        int[] over = new int[n], overLim = new int[n];
        boolean[] sample = new boolean[n], demo = new boolean[n];
        for (int i = 0; i < n; i++) a8.add(new ArrayList<>());
        List<String> keys = sortedKeys(days);
        List<JSONObject> entries = new ArrayList<>();
        List<String> dates = new ArrayList<>();
        for (String k : keys) {
            if (k.compareTo(today) >= 0) continue;     // today comes from the live entry
            JSONObject d = days.optJSONObject(k);
            if (d != null) { entries.add(d); dates.add(k); }
        }
        if (todayEntry != null) { entries.add(todayEntry); dates.add(today); }
        for (int j = 0; j < entries.size(); j++) {
            LocalDate d;
            try { d = LocalDate.parse(dates.get(j)); } catch (Exception e) { continue; }
            if (d.isBefore(first) || d.isAfter(t)) continue;
            int i = (d.getYear() - first.getYear()) * 12 + d.getMonthValue() - first.getMonthValue();
            if (i < 0 || i >= n) continue;
            JSONObject e = entries.get(j);
            double p = e.optDouble("points", 0);
            if (!(p > 0)) continue;
            a8.get(i).add(a8Of(e));
            tool[i] += e.optDouble("tool", 0);
            double lim = e.optDouble("limit", Exposure.ELV_POINTS);
            if (p >= Exposure.EAV_POINTS) over[i]++;
            if (p >= lim) overLim[i]++;
            if (e.optBoolean("sample")) sample[i] = true;
            if (e.optDouble("demoSpeed", 1) > 1) demo[i] = true;
        }
        JSONArray out = new JSONArray();
        for (int i = 0; i < n; i++) {
            LocalDate m = first.plusMonths(i);
            int k = a8.get(i).size();
            double mean = energyMeanA8(a8.get(i));
            out.put(new JSONObject().put("month", m.toString().substring(0, 7)).put("days", k)
                    .put("a8", round(mean, 3)).put("points", round(pointsFromA8(mean), 1))
                    .put("tool", k == 0 ? 0 : Math.round(tool[i] / k)).put("toolTotal", Math.round(tool[i]))
                    .put("overAction", over[i]).put("overLimit", overLim[i]).put("sample", sample[i]).put("demo", demo[i]));
        }
        return out;
    }

    static double round(double v, int places) {
        double f = Math.pow(10, places);
        return Math.round(v * f) / f;
    }
}
