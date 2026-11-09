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
import android.media.AudioManager;
import android.media.MediaPlayer;
import android.media.RingtoneManager;
import android.media.ToneGenerator;
import android.net.Uri;
import android.os.VibrationEffect;
import android.os.Vibrator;
import android.os.Build;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;
import android.os.PowerManager;

/**
 * Keeps the sleeve connection and the on-phone processing running when the app is in the background or the
 * screen is off (a foreground service, type connectedDevice: the sleeve is a wearable on this phone's hotspot).
 * Raises the only two alarms, the action level and the daily limit: a notification plus a loud looping alarm
 * sound and vibration, so they reach the wearer wherever the app is.
 */
public class SleeveService extends Service {
    static final String CH_RUN = "vg.running", CH_ALARM = "vg.alarm2";
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
        sleeve.listener = new Sleeve.Listener() {
            @Override public void alert(String kind, String title, String text) { main.post(() -> SleeveService.this.alert(kind, title, text)); }
            @Override public void silence() { main.post(SleeveService.this::stopSiren); }
        };
        sleeve.start();
        main.post(this::refresh);
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) { return START_STICKY; }

    @Override
    public void onDestroy() {
        main.removeCallbacksAndMessages(null);
        if (sleeve != null) sleeve.listener = null;
        stopSiren();
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
        Notification n = new Notification.Builder(this, CH_ALARM)
                .setSmallIcon(R.mipmap.ic_launcher)
                .setContentTitle(title)
                .setContentText(text)
                .setStyle(new Notification.BigTextStyle().bigText(text))
                .setCategory(Notification.CATEGORY_ALARM)
                .setAutoCancel(true)
                .setContentIntent(openApp())
                .setFullScreenIntent(openApp(), true)
                .build();
        getSystemService(NotificationManager.class).notify(alarmId++, n);
        siren(60_000);
    }

    // ---- the audible alarm: loud enough to hear next to a running tool ----

    private MediaPlayer player;
    private ToneGenerator tone;
    private int savedVolume = -1;
    private final Runnable beep = this::beep;

    /**
     * Plays the phone's alarm sound on the ALARM stream at full volume, looping, with a strong repeating vibration,
     * until the wearer taps OK (or for maxMs). Falls back to a built-in siren tone if no alarm sound is set.
     */
    private void siren(long maxMs) {
        stopSiren();
        AudioManager am = getSystemService(AudioManager.class);
        try {
            savedVolume = am.getStreamVolume(AudioManager.STREAM_ALARM);
            am.setStreamVolume(AudioManager.STREAM_ALARM, am.getStreamMaxVolume(AudioManager.STREAM_ALARM), 0);
        } catch (Exception ignored) { savedVolume = -1; }
        Uri uri = RingtoneManager.getDefaultUri(RingtoneManager.TYPE_ALARM);
        if (uri == null) uri = RingtoneManager.getDefaultUri(RingtoneManager.TYPE_RINGTONE);
        try {
            if (uri == null) throw new IllegalStateException("no alarm sound");
            player = new MediaPlayer();
            player.setAudioAttributes(new AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_ALARM)
                    .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION).build());
            player.setDataSource(this, uri);
            player.setLooping(true);
            player.prepare();
            player.start();
        } catch (Exception e) {
            player = null;
            try { tone = new ToneGenerator(AudioManager.STREAM_ALARM, ToneGenerator.MAX_VOLUME); beep(); } catch (Exception ignored) {}
        }
        Vibrator v = getSystemService(Vibrator.class);
        if (v != null && v.hasVibrator()) v.vibrate(VibrationEffect.createWaveform(new long[]{0, 900, 250, 900, 250}, 0));
        main.postDelayed(this::stopSiren, maxMs);
    }

    private void beep() {
        if (tone == null) return;
        tone.startTone(ToneGenerator.TONE_CDMA_EMERGENCY_RINGBACK, 1500);
        main.postDelayed(beep, 1700);
    }

    void stopSiren() {
        main.removeCallbacks(beep);
        if (player != null) { try { player.stop(); } catch (Exception ignored) {} player.release(); player = null; }
        if (tone != null) { tone.release(); tone = null; }
        Vibrator v = getSystemService(Vibrator.class);
        if (v != null) v.cancel();
        if (savedVolume >= 0) {
            try { getSystemService(AudioManager.class).setStreamVolume(AudioManager.STREAM_ALARM, savedVolume, 0); } catch (Exception ignored) {}
            savedVolume = -1;
        }
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
        nm.createNotificationChannel(run);
        nm.createNotificationChannel(alarm);
        nm.deleteNotificationChannel("vg.break");    // older builds had break reminders
    }
}
