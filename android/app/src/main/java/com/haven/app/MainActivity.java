package com.haven.app;

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
 * The connect screen, for people who may be new to phones: it starts on its own (the laptop used last time,
 * otherwise a search of this Wi-Fi), one card says what is happening, and each laptop found is one big button.
 * Help and the address typed by hand wait behind "Can't connect?". The demo runs with no laptop.
 */
public class MainActivity extends Activity {
    private static final String PREFS = "connect";
    private static final String KEY_LAST = "last_base_url";
    private static final String STATE_STARTED = "started";
    private static final int FIND_WAIT_MS = 1800;
    private static final int PROBE_TIMEOUT_MS = 3500;

    private final ExecutorService io = Executors.newSingleThreadExecutor();
    private final Handler main = new Handler(Looper.getMainLooper());

    private View spinner;
    private TextView statusTitle;
    private TextView statusDetail;
    private LinearLayout found;
    private Button findButton;
    private Button helpToggle;
    private View help;
    private EditText address;
    private Button connectButton;
    private boolean busy;
    private boolean started;
    private String justFailed;               // the laptop that did not answer just now: not offered again

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);
        EdgeToEdge.apply(this, findViewById(R.id.root));

        spinner = findViewById(R.id.spinner);
        statusTitle = findViewById(R.id.status_title);
        statusDetail = findViewById(R.id.status_detail);
        found = findViewById(R.id.found);
        findButton = findViewById(R.id.find);
        helpToggle = findViewById(R.id.help_toggle);
        help = findViewById(R.id.help);
        address = findViewById(R.id.address);
        connectButton = findViewById(R.id.connect);

        findButton.setOnClickListener(v -> find());
        helpToggle.setOnClickListener(v -> setHelpOpen(help.getVisibility() != View.VISIBLE));
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

        started = savedInstanceState != null && savedInstanceState.getBoolean(STATE_STARTED);
        if (!started) {          // first open: get going without a tap
            started = true;
            String last = prefs().getString(KEY_LAST, null);
            if (last != null) check(last, true);
            else find();
        } else {
            showNone();
        }
    }

    @Override
    protected void onSaveInstanceState(Bundle out) {
        super.onSaveInstanceState(out);
        out.putBoolean(STATE_STARTED, started);
    }

    @Override
    protected void onDestroy() {
        io.shutdownNow();
        super.onDestroy();
    }

    private SharedPreferences prefs() {
        return getSharedPreferences(PREFS, MODE_PRIVATE);
    }

    // ---- the status card ----
    private void status(boolean working, String title, String detail) {
        spinner.setVisibility(working ? View.VISIBLE : View.GONE);
        statusTitle.setText(title);
        statusDetail.setText(detail);
        statusDetail.setVisibility(detail == null || detail.isEmpty() ? View.GONE : View.VISIBLE);
        findButton.setVisibility(working ? View.GONE : View.VISIBLE);
    }

    private void setBusy(boolean on) {
        busy = on;
        connectButton.setEnabled(!on);
        for (int i = 0; i < found.getChildCount(); i++) found.getChildAt(i).setEnabled(!on);
    }

    private void showNone() {
        found.removeAllViews();
        status(false, getString(R.string.found_none), getString(R.string.found_none_detail));
        addLastUsedRow();
    }

    private void setHelpOpen(boolean open) {
        help.setVisibility(open ? View.VISIBLE : View.GONE);
        helpToggle.setText(open ? R.string.help_hide : R.string.help_show);
    }

    private void hideKeyboard() {
        InputMethodManager imm = getSystemService(InputMethodManager.class);
        if (imm != null) imm.hideSoftInputFromWindow(address.getWindowToken(), 0);
    }

    private void addRow(String name, String detail, String base) {
        View row = getLayoutInflater().inflate(R.layout.item_backend, found, false);
        ((TextView) row.findViewById(R.id.name)).setText(name);
        ((TextView) row.findViewById(R.id.detail)).setText(detail);
        row.setContentDescription(getString(R.string.open) + " " + name + ", " + detail);
        row.setOnClickListener(v -> check(base, false));
        found.addView(row);
    }

    /** The laptop used last time, as one more button, when it is not already in the list. */
    private void addLastUsedRow() {
        String last = prefs().getString(KEY_LAST, null);
        if (last == null || last.equals(justFailed)) return;
        for (int i = 0; i < found.getChildCount(); i++) {
            if (String.valueOf(found.getChildAt(i).getTag()).equals(last)) return;
        }
        addRow(getString(R.string.last_used), Address.label(last), last);
        found.getChildAt(found.getChildCount() - 1).setTag(last);
    }

    // ---- search this Wi-Fi ----
    private void find() {
        if (busy) return;
        hideKeyboard();
        found.removeAllViews();
        setBusy(true);
        status(true, getString(R.string.searching), getString(R.string.searching_detail));
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
                setBusy(false);
                if (result.isEmpty()) {
                    if (err != null) status(false, getString(R.string.found_none), getString(R.string.find_failed, err));
                    else showNone();
                    return;
                }
                status(false, getResources().getQuantityString(R.plurals.found_some, result.size(), result.size()),
                        getString(R.string.found_detail));
                for (Discovery.Backend b : result) {
                    String detail = Address.label(b.baseUrl())
                            + (b.password ? " · " + getString(R.string.asks_password) : "");
                    addRow(b.name, detail, b.baseUrl());
                    found.getChildAt(found.getChildCount() - 1).setTag(b.baseUrl());
                }
                addLastUsedRow();
            });
        });
    }

    // ---- an address typed by hand ----
    private void connectTyped() {
        if (busy) return;
        String base = Address.normalize(address.getText().toString());
        if (base == null) {
            address.setError(getString(R.string.bad_address));
            address.requestFocus();
            return;
        }
        hideKeyboard();
        check(base, false);
    }

    /**
     * Make sure HAVen answers at {@code base}, then open its dashboard. {@code auto}: the try on opening the
     * app; if the laptop from last time does not answer, search this Wi-Fi instead of stopping on an error.
     */
    private void check(String base, boolean auto) {
        if (busy) return;
        setBusy(true);
        found.removeAllViews();
        String label = Address.label(base);
        status(true, getString(auto ? R.string.trying_last : R.string.connecting, label),
                auto ? getString(R.string.trying_last_detail) : null);
        io.execute(() -> {
            int code = Address.probe(base, PROBE_TIMEOUT_MS);
            main.post(() -> {
                if (isFinishing()) return;
                setBusy(false);
                justFailed = code == 200 || code == 401 ? null : base;
                if (code == 200 || code == 401) {          // 401: it asks for a password, the dashboard prompts
                    prefs().edit().putString(KEY_LAST, base).apply();
                    status(false, getString(R.string.opened, label), getString(R.string.opened_detail));
                    addRow(label, getString(R.string.last_used), base);
                    found.getChildAt(found.getChildCount() - 1).setTag(base);
                    startActivity(DashboardActivity.intentFor(this, base + "/dashboard/", false));
                } else if (auto) {
                    find();
                } else {
                    String why = code == Address.UNREACHABLE ? getString(R.string.unreachable)
                            : code == Address.NOT_HAVEN ? getString(R.string.not_haven)
                            : code == 403 ? getString(R.string.refused)      // its host check: not started with --host 0.0.0.0
                            : getString(R.string.http_error, code);
                    status(false, getString(R.string.unreachable_title, label), why);
                    addLastUsedRow();
                }
            });
        });
    }
}
