package com.vibeguard.app;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.media.AudioAttributes;
import android.media.RingtoneManager;
import android.os.Build;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;
import android.os.PowerManager;

/**
 * Keeps the sleeve connection and the on-phone processing running when the app is in the background or the
 * screen is off (a foreground service, type connectedDevice: the sleeve is a wearable on this phone's hotspot).
 * Posts the exposure alarms (action level, daily limit) and break reminders as notifications, with sound and
 * vibration, so they reach the wearer wherever the app is.
 */
public class SleeveService extends Service {
    static final String CH_RUN = "vg.running", CH_ALARM = "vg.alarm2", CH_BREAK = "vg.break";
    private static final int ID_RUN = 1;
    private PowerManager.WakeLock wake;
    private final Handler main = new Handler(Looper.getMainLooper());
    private Sleeve sleeve;
    private int alarmId = 100;

    static void start(Context c) {
        Intent i = new Intent(c, SleeveService.class);
        if (Build.VERSION.SDK_INT >= 26) c.startForegroundService(i); else c.startService(i);
    }

    @Override
    public void onCreate() {
        super.onCreate();
        channels(this);
        Notification n = running("Connecting to the sleeve…");
        if (Build.VERSION.SDK_INT >= 29) startForeground(ID_RUN, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_CONNECTED_DEVICE);
        else startForeground(ID_RUN, n);
        // the sleeve streams about 40 records a second: keep the CPU awake to read them with the screen off
        PowerManager pm = (PowerManager) getSystemService(POWER_SERVICE);
        wake = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "vibeguard:sleeve");
        wake.acquire();
        sleeve = Sleeve.get(this);
        sleeve.listener = (kind, title, text) -> main.post(() -> alert(kind, title, text));
        sleeve.start();
        main.post(this::refresh);
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) { return START_STICKY; }

    @Override
    public void onDestroy() {
        main.removeCallbacksAndMessages(null);
        if (sleeve != null) sleeve.listener = null;
        if (wake != null && wake.isHeld()) wake.release();
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) { return null; }

    /** The ongoing notification shows today's points, updated every few seconds. */
    private void refresh() {
        String text = sleeve.summary();
        getSystemService(NotificationManager.class).notify(ID_RUN, running(text));
        main.postDelayed(this::refresh, 5000);
    }

    private PendingIntent openApp() {
        Intent i = new Intent(this, StandaloneActivity.class).addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP);
        return PendingIntent.getActivity(this, 0, i, PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);
    }

    private Notification running(String text) {
        return new Notification.Builder(this, CH_RUN)
                .setSmallIcon(R.mipmap.ic_launcher)
                .setContentTitle("VibeGuard is monitoring")
                .setContentText(text)
                .setOngoing(true)
                .setOnlyAlertOnce(true)
                .setContentIntent(openApp())
                .build();
    }

    private void alert(String kind, String title, String text) {
        boolean level = !kind.equals("break");
        Notification n = new Notification.Builder(this, level ? CH_ALARM : CH_BREAK)
                .setSmallIcon(R.mipmap.ic_launcher)
                .setContentTitle(title)
                .setContentText(text)
                .setStyle(new Notification.BigTextStyle().bigText(text))
                .setCategory(level ? Notification.CATEGORY_ALARM : Notification.CATEGORY_REMINDER)
                .setAutoCancel(true)
                .setContentIntent(openApp())
                .setFullScreenIntent(level ? openApp() : null, level)
                .build();
        getSystemService(NotificationManager.class).notify(alarmId++, n);
    }

    static void channels(Context c) {
        NotificationManager nm = c.getSystemService(NotificationManager.class);
        NotificationChannel run = new NotificationChannel(CH_RUN, "Monitoring", NotificationManager.IMPORTANCE_LOW);
        run.setDescription("Shown while VibeGuard reads the sleeve");
        NotificationChannel alarm = new NotificationChannel(CH_ALARM, "Vibration alarms", NotificationManager.IMPORTANCE_HIGH);
        alarm.setDescription("Action level and daily limit reached");
        alarm.enableVibration(true);
        alarm.setVibrationPattern(new long[]{0, 800, 300, 800, 300, 800});
        alarm.setSound(RingtoneManager.getDefaultUri(RingtoneManager.TYPE_ALARM),
                new AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_ALARM)
                        .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION).build());
        alarm.setBypassDnd(true);
        NotificationChannel brk = new NotificationChannel(CH_BREAK, "Break reminders", NotificationManager.IMPORTANCE_HIGH);
        brk.enableVibration(true);
        nm.createNotificationChannel(run);
        nm.createNotificationChannel(alarm);
        nm.createNotificationChannel(brk);
    }
}
