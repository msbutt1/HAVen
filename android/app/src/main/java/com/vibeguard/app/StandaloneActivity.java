package com.vibeguard.app;

import android.annotation.SuppressLint;
import android.Manifest;
import android.app.Activity;
import android.content.pm.PackageManager;
import android.os.Build;
import android.content.Intent;
import android.os.Bundle;
import android.view.WindowManager;
import android.webkit.JavascriptInterface;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;

/**
 * The standalone app's one screen: Dashboard and Overview (assets/standalone/index.html), drawn in a WebView over
 * the on-phone engine ({@link Sleeve}). The page reads the state with VG.state() and sends commands with
 * VG.command(...). The laptop dashboard is still one tap away (Settings, "Use a laptop instead").
 */
public class StandaloneActivity extends Activity {
    private WebView web;
    private Sleeve sleeve;

    @SuppressLint({"SetJavaScriptEnabled", "AddJavascriptInterface"})
    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        // the phone does the processing: keep the screen on while the app is open
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        sleeve = Sleeve.get(this);
        web = new WebView(this);
        // a WebView ignores its own padding: put it in a frame and pad the frame clear of the system bars
        android.widget.FrameLayout root = new android.widget.FrameLayout(this);
        root.addView(web);
        setContentView(root);
        EdgeToEdge.apply(this, root);
        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setAllowFileAccess(false);
        web.setWebViewClient(new WebViewClient());      // stay in the app
        web.setWebChromeClient(new android.webkit.WebChromeClient());   // shows the page's confirm() dialogs
        web.addJavascriptInterface(new Bridge(), "VG");
        web.loadUrl("file:///android_asset/standalone/index.html");
        // alarms are notifications: ask once (Android 13+); then keep reading the sleeve in the background
        if (Build.VERSION.SDK_INT >= 33 && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS}, 1);
        }
        SleeveService.start(this);
    }

    @Override
    public void onBackPressed() {
        web.evaluateJavascript("window.vgBack && window.vgBack()", v -> {
            if (!"true".equals(v)) StandaloneActivity.super.onBackPressed();
        });
    }

    /** Only these two methods are visible to the page, which is the app's own bundled file. */
    final class Bridge {
        @JavascriptInterface
        public String state() { return sleeve.stateJson(); }

        @JavascriptInterface
        public void command(String c) {
            if ("laptop".equals(c)) {
                runOnUiThread(() -> startActivity(new Intent(StandaloneActivity.this, MainActivity.class)));
                return;
            }
            sleeve.command(c);
        }
    }
}
