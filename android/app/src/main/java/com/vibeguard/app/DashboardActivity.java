package com.vibeguard.app;

import android.app.Activity;
import android.app.AlertDialog;
import android.graphics.Typeface;
import android.content.ActivityNotFoundException;
import android.content.Context;
import android.content.Intent;
import android.content.pm.ApplicationInfo;
import android.graphics.Bitmap;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.text.InputType;
import android.view.View;
import android.view.WindowManager;
import android.webkit.HttpAuthHandler;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ProgressBar;
import android.widget.TextView;
import android.window.OnBackInvokedDispatcher;

import java.util.Objects;

/**
 * Shows the VibeGuard dashboard: the backend's own copy over Wi-Fi (http://laptop:8000/dashboard/), or the copy
 * bundled in the APK for the demo, which runs on the simulated feed. Stays on that one origin: any other link
 * opens in the browser.
 */
public class DashboardActivity extends Activity {
    static final String DEMO_URL = "file:///android_asset/index.html?source=mock";
    private static final String EXTRA_URL = "url";
    private static final String EXTRA_DEMO = "demo";

    private WebView web;
    private View errorBox;
    private TextView errorText;
    private ProgressBar progress;
    private String startUrl;
    private Uri start;
    private boolean failed;
    private AlertDialog passwordDialog;
    private HttpAuthHandler pendingAuth;     // the newest request waiting for the password

    static Intent intentFor(Context context, String url, boolean demo) {
        return new Intent(context, DashboardActivity.class).putExtra(EXTRA_URL, url).putExtra(EXTRA_DEMO, demo);
    }

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        // a live monitor: keep the screen on while it is open (the recording itself runs on the laptop)
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        setContentView(R.layout.activity_dashboard);
        EdgeToEdge.apply(this, findViewById(R.id.root));

        startUrl = Objects.requireNonNull(getIntent().getStringExtra(EXTRA_URL));
        start = Uri.parse(startUrl);
        web = findViewById(R.id.web);
        errorBox = findViewById(R.id.error);
        errorText = findViewById(R.id.error_text);
        progress = findViewById(R.id.progress);
        findViewById(R.id.retry).setOnClickListener(v -> {
            hideError();
            web.loadUrl(startUrl);
        });
        findViewById(R.id.back).setOnClickListener(v -> finish());

        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);           // the dashboard is a JavaScript app
        s.setDomStorageEnabled(true);
        s.setAllowFileAccess(false);            // the bundled demo is in assets, which stay readable without it
        s.setAllowContentAccess(false);
        s.setMixedContentMode(WebSettings.MIXED_CONTENT_NEVER_ALLOW);
        s.setSupportMultipleWindows(false);
        s.setJavaScriptCanOpenWindowsAutomatically(false);
        if ((getApplicationInfo().flags & ApplicationInfo.FLAG_DEBUGGABLE) != 0) {
            WebView.setWebContentsDebuggingEnabled(true);   // debug builds only: chrome://inspect
        }
        web.setWebChromeClient(new WebChromeClient() {     // also shows the page's confirm() dialogs (New shift)
            @Override
            public void onProgressChanged(WebView view, int newProgress) {
                progress.setProgress(newProgress);
                progress.setVisibility(newProgress >= 100 ? View.GONE : View.VISIBLE);
            }
        });
        web.setWebViewClient(new Client());

        if (Build.VERSION.SDK_INT >= 33) {
            getOnBackInvokedDispatcher().registerOnBackInvokedCallback(
                    OnBackInvokedDispatcher.PRIORITY_DEFAULT, this::goBack);
        }
        if (savedInstanceState == null || web.restoreState(savedInstanceState) == null) {
            web.loadUrl(startUrl);
        }
    }

    private void goBack() {
        if (web.canGoBack() && errorBox.getVisibility() != View.VISIBLE) web.goBack();
        else finish();
    }

    @SuppressWarnings("deprecation")
    @Override
    public void onBackPressed() {            // Android 12 and older
        goBack();
    }

    @Override
    protected void onSaveInstanceState(Bundle outState) {
        super.onSaveInstanceState(outState);
        web.saveState(outState);
    }

    @Override
    protected void onPause() {
        web.onPause();
        super.onPause();
    }

    @Override
    protected void onResume() {
        super.onResume();
        web.onResume();
    }

    @Override
    protected void onDestroy() {
        web.destroy();
        super.onDestroy();
    }

    private boolean sameOrigin(Uri u) {
        if (start.getScheme() == null || !start.getScheme().equals(u.getScheme())) return false;
        if ("file".equals(u.getScheme())) {
            String p = u.getPath();
            return p != null && p.startsWith("/android_asset/");
        }
        return Objects.equals(start.getHost(), u.getHost()) && start.getPort() == u.getPort();
    }

    private void showError(String message) {
        failed = true;
        errorText.setText(message);
        errorBox.setVisibility(View.VISIBLE);
        web.setVisibility(View.INVISIBLE);
    }

    private void hideError() {
        failed = false;
        errorBox.setVisibility(View.GONE);
        web.setVisibility(View.VISIBLE);
    }

    private final class Client extends WebViewClient {
        @Override
        public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
            Uri u = request.getUrl();
            if (sameOrigin(u)) return false;
            try {
                startActivity(new Intent(Intent.ACTION_VIEW, u));
            } catch (ActivityNotFoundException ignored) {
                // nothing can open it: stay on the dashboard
            }
            return true;
        }

        @Override
        public void onPageStarted(WebView view, String url, Bitmap favicon) {
            failed = false;
        }

        @Override
        public void onPageFinished(WebView view, String url) {
            if (!failed) hideError();
        }

        @Override
        public void onReceivedError(WebView view, WebResourceRequest request, WebResourceError error) {
            if (request.isForMainFrame()) {
                showError(getString(R.string.page_error, Address.label(origin()), String.valueOf(error.getDescription())));
            }
        }

        @Override
        public void onReceivedHttpError(WebView view, WebResourceRequest request, WebResourceResponse response) {
            if (!request.isForMainFrame() || response.getStatusCode() == 401) return;
            int code = response.getStatusCode();
            showError(code == 404 ? getString(R.string.no_dashboard, Address.label(origin()))
                    : getString(R.string.page_http_error, Address.label(origin()), code));
        }

        @Override
        public void onReceivedHttpAuthRequest(WebView view, HttpAuthHandler handler, String host, String realm) {
            askPassword(handler, host);
        }
    }

    private String origin() {
        return start.getScheme() + "://" + start.getAuthority();
    }

    /**
     * The backend was started with VG_BASIC_AUTH=user:password: ask for them (kept only while the app runs).
     * One dialog at a time; it answers the newest request (an older one is cancelled when a newer arrives).
     */
    private void askPassword(HttpAuthHandler handler, String host) {
        if (pendingAuth != null && pendingAuth != handler) pendingAuth.cancel();
        pendingAuth = handler;
        if (passwordDialog != null && passwordDialog.isShowing()) return;
        int pad = Math.round(20 * getResources().getDisplayMetrics().density);
        LinearLayout box = new LinearLayout(this);
        box.setOrientation(LinearLayout.VERTICAL);
        box.setPadding(pad, pad / 2, pad, 0);
        EditText user = new EditText(this);
        user.setHint(R.string.user_name);
        user.setSingleLine(true);
        user.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_FLAG_NO_SUGGESTIONS);
        EditText pass = new EditText(this);
        pass.setHint(R.string.password);
        pass.setSingleLine(true);
        pass.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_PASSWORD);
        pass.setTypeface(Typeface.DEFAULT);      // the password type would switch it to monospace
        box.addView(user);
        box.addView(pass);
        passwordDialog = new AlertDialog.Builder(this)
                .setTitle(getString(R.string.password_title, host))
                .setMessage(R.string.password_message)
                .setView(box)
                .setPositiveButton(R.string.sign_in, (d, w) -> {
                    HttpAuthHandler h = pendingAuth;
                    pendingAuth = null;
                    if (h != null) h.proceed(user.getText().toString(), pass.getText().toString());
                    else web.reload();
                })
                .setNegativeButton(android.R.string.cancel, (d, w) -> cancelPassword())
                .setOnCancelListener(d -> cancelPassword())
                .create();
        passwordDialog.setOnDismissListener(d -> passwordDialog = null);
        passwordDialog.show();
    }

    private void cancelPassword() {
        if (pendingAuth != null) pendingAuth.cancel();
        pendingAuth = null;
        showError(getString(R.string.password_cancelled));
    }
}
