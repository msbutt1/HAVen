package com.vibeguard.app;

import android.app.Activity;
import android.content.SharedPreferences;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.view.KeyEvent;
import android.view.LayoutInflater;
import android.view.View;
import android.view.inputmethod.EditorInfo;
import android.view.inputmethod.InputMethodManager;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.TextView;

import java.io.IOException;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * The connect screen: find the laptop running the VibeGuard backend on this Wi-Fi (or type its address), then
 * open its dashboard. Also opens the bundled demo, which runs on a simulated feed with no laptop.
 */
public class MainActivity extends Activity {
    private static final String PREFS = "connect";
    private static final String KEY_LAST = "last_base_url";
    private static final int FIND_WAIT_MS = 1800;
    private static final int PROBE_TIMEOUT_MS = 3500;

    private final ExecutorService io = Executors.newSingleThreadExecutor();
    private final Handler main = new Handler(Looper.getMainLooper());

    private EditText address;
    private Button findButton;
    private Button connectButton;
    private TextView status;
    private LinearLayout found;
    private TextView recent;
    private boolean busy;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);
        EdgeToEdge.apply(this, findViewById(R.id.root));

        address = findViewById(R.id.address);
        findButton = findViewById(R.id.find);
        connectButton = findViewById(R.id.connect);
        status = findViewById(R.id.status);
        found = findViewById(R.id.found);
        recent = findViewById(R.id.recent);

        findButton.setOnClickListener(v -> find());
        connectButton.setOnClickListener(v -> connectTyped());
        address.setOnEditorActionListener((v, actionId, event) -> {
            boolean enter = event != null && event.getKeyCode() == KeyEvent.KEYCODE_ENTER
                    && event.getAction() == KeyEvent.ACTION_DOWN;
            if (actionId == EditorInfo.IME_ACTION_GO || enter) {
                connectTyped();
                return true;
            }
            return false;
        });
        findViewById(R.id.demo).setOnClickListener(v ->
                startActivity(DashboardActivity.intentFor(this, DashboardActivity.DEMO_URL, true)));
    }

    @Override
    protected void onResume() {
        super.onResume();
        String last = prefs().getString(KEY_LAST, null);
        if (last == null) {
            recent.setVisibility(View.GONE);
        } else {
            recent.setVisibility(View.VISIBLE);
            recent.setText(getString(R.string.recent, Address.label(last)));
            recent.setOnClickListener(v -> check(last));
        }
    }

    @Override
    protected void onDestroy() {
        io.shutdownNow();
        super.onDestroy();
    }

    private SharedPreferences prefs() {
        return getSharedPreferences(PREFS, MODE_PRIVATE);
    }

    private void setBusy(boolean on, String message) {
        busy = on;
        findButton.setEnabled(!on);
        connectButton.setEnabled(!on);
        recent.setEnabled(!on);
        for (int i = 0; i < found.getChildCount(); i++) found.getChildAt(i).setEnabled(!on);
        if (message != null) say(message);
    }

    private void say(String message) {
        status.setText(message);
        status.setVisibility(message.isEmpty() ? View.GONE : View.VISIBLE);
    }

    private void hideKeyboard() {
        InputMethodManager imm = getSystemService(InputMethodManager.class);
        if (imm != null) imm.hideSoftInputFromWindow(address.getWindowToken(), 0);
    }

    // ---- find on this Wi-Fi ----
    private void find() {
        if (busy) return;
        hideKeyboard();
        found.removeAllViews();
        setBusy(true, getString(R.string.finding));
        io.execute(() -> {
            List<Discovery.Backend> list;
            String error = null;
            try {
                list = Discovery.find(this, FIND_WAIT_MS);
            } catch (IOException e) {
                list = new ArrayList<>();
                error = e.getMessage();
            }
            final List<Discovery.Backend> result = list;
            final String err = error;
            main.post(() -> {
                if (isFinishing()) return;
                setBusy(false, null);
                if (result.isEmpty()) {
                    say(err != null ? getString(R.string.find_failed, err) : getString(R.string.found_none));
                    return;
                }
                say(getResources().getQuantityString(R.plurals.found_some, result.size(), result.size()));
                LayoutInflater inf = getLayoutInflater();
                for (Discovery.Backend b : result) {
                    View row = inf.inflate(R.layout.item_backend, found, false);
                    ((TextView) row.findViewById(R.id.name)).setText(b.name);
                    String detail = Address.label(b.baseUrl())
                            + (b.password ? " · " + getString(R.string.asks_password) : "");
                    ((TextView) row.findViewById(R.id.detail)).setText(detail);
                    row.setContentDescription(b.name + ", " + detail);
                    row.setOnClickListener(v -> check(b.baseUrl()));
                    found.addView(row);
                }
            });
        });
    }

    // ---- typed address ----
    private void connectTyped() {
        if (busy) return;
        String base = Address.normalize(address.getText().toString());
        if (base == null) {
            say(getString(R.string.bad_address));
            address.requestFocus();
            return;
        }
        hideKeyboard();
        check(base);
    }

    /** Make sure a VibeGuard backend answers at {@code base}, then open its dashboard. */
    private void check(String base) {
        if (busy) return;
        setBusy(true, getString(R.string.connecting, Address.label(base)));
        io.execute(() -> {
            int code = Address.probe(base, PROBE_TIMEOUT_MS);
            main.post(() -> {
                if (isFinishing()) return;
                setBusy(false, null);
                if (code == 200 || code == 401) {          // 401: it asks for a password, the dashboard prompts
                    say("");
                    prefs().edit().putString(KEY_LAST, base).apply();
                    startActivity(DashboardActivity.intentFor(this, base + "/dashboard/", false));
                } else if (code == Address.UNREACHABLE) {
                    say(getString(R.string.unreachable, Address.label(base)));
                } else if (code == Address.NOT_VIBEGUARD) {
                    say(getString(R.string.not_vibeguard, Address.label(base)));
                } else {
                    say(getString(R.string.http_error, Address.label(base), code));
                }
            });
        });
    }
}
