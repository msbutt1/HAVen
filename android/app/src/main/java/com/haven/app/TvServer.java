package com.haven.app;

import android.content.Context;
import android.content.res.AssetManager;

import java.io.BufferedReader;
import java.io.IOException;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.net.Inet4Address;
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.net.NetworkInterface;
import java.net.ServerSocket;
import java.net.Socket;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.regex.Pattern;

/**
 * The TV view, served by this phone: a laptop or TV browser on the same Wi-Fi (usually this phone's hotspot, which
 * the sleeve is on too) opens http://&lt;phone&gt;:8080/ and gets the dashboard's tv.html in phone mode. The page
 * reads {@link Sleeve#tvJson()} as a Server-Sent Events stream (/api/tv/events, 20 updates a second), so the TV
 * shows exactly what this phone computes, with this phone's calibration. The sleeve still streams only to the phone.
 *
 * Read-only: no request changes anything on the phone. Static files come from the APK's assets (the repo's
 * dashboard folder is bundled there), limited to the TV page's own files.
 */
final class TvServer {
    static final int PORT = 8080;
    private static final int MAX_CLIENTS = 8;
    private static final Pattern STATIC = Pattern.compile(
            "tv\\.html|css/tv\\.css|js/tv\\.js|assets/fonts/[a-z0-9-]+\\.woff2");

    private static TvServer instance;

    private final AssetManager assets;
    private final Sleeve sleeve;
    private final AtomicInteger clients = new AtomicInteger();
    private static volatile List<String> urls = Collections.emptyList();
    private static volatile long urlsAt;

    static synchronized void start(Context c, Sleeve sleeve) {
        if (instance != null) return;
        instance = new TvServer(c.getApplicationContext().getAssets(), sleeve);
        Thread t = new Thread(instance::serve, "vg-tv-server");
        t.setDaemon(true);
        t.start();
    }

    private TvServer(AssetManager assets, Sleeve sleeve) {
        this.assets = assets;
        this.sleeve = sleeve;
    }

    /** The addresses a TV can open, one per IPv4 interface (hotspot, Wi-Fi). Refreshed at most every 5 s. */
    static List<String> urls() {
        long now = System.currentTimeMillis();
        if (now - urlsAt < 5000) return urls;
        urlsAt = now;
        List<String> out = new ArrayList<>();
        try {
            for (NetworkInterface ni : Collections.list(NetworkInterface.getNetworkInterfaces())) {
                if (!ni.isUp() || ni.isLoopback()) continue;
                for (InetAddress a : Collections.list(ni.getInetAddresses())) {
                    if (a instanceof Inet4Address && !a.isLoopbackAddress() && !a.isLinkLocalAddress()) {
                        out.add("http://" + a.getHostAddress() + ":" + PORT + "/");
                    }
                }
            }
        } catch (Exception ignored) {
        }
        urls = out;
        return out;
    }

    private void serve() {
        while (true) {
            try (ServerSocket ss = new ServerSocket()) {
                ss.setReuseAddress(true);
                ss.bind(new InetSocketAddress(PORT));
                while (true) {
                    Socket s = ss.accept();
                    if (clients.incrementAndGet() > MAX_CLIENTS) {
                        clients.decrementAndGet();
                        try { s.close(); } catch (IOException ignored) {}
                        continue;
                    }
                    Thread t = new Thread(() -> {
                        try { handle(s); } finally {
                            clients.decrementAndGet();
                            try { s.close(); } catch (IOException ignored) {}
                        }
                    }, "vg-tv-client");
                    t.setDaemon(true);
                    t.start();
                }
            } catch (Exception e) {
                // port busy or the network changed: try again shortly
                try { Thread.sleep(3000); } catch (InterruptedException ie) { return; }
            }
        }
    }

    private void handle(Socket s) {
        try {
            s.setSoTimeout(10000);
            s.setTcpNoDelay(true);
            BufferedReader in = new BufferedReader(new InputStreamReader(s.getInputStream(), StandardCharsets.US_ASCII), 4096);
            String first = in.readLine();
            if (first == null) return;
            for (String h = in.readLine(); h != null && !h.isEmpty(); h = in.readLine()) { /* headers: not needed */ }
            String[] parts = first.split(" ");
            OutputStream out = s.getOutputStream();
            if (parts.length < 2 || !(parts[0].equals("GET") || parts[0].equals("HEAD"))) {
                send(out, "405 Method Not Allowed", "text/plain", "GET only\n".getBytes(StandardCharsets.UTF_8));
                return;
            }
            String path = parts[1];
            int qm = path.indexOf('?');
            if (qm >= 0) path = path.substring(0, qm);
            if (path.equals("/") || path.equals("/tv")) {
                out.write(("HTTP/1.1 302 Found\r\nLocation: /tv.html?source=phone\r\nContent-Length: 0\r\n"
                        + "Connection: close\r\n\r\n").getBytes(StandardCharsets.US_ASCII));
            } else if (path.equals("/api/tv")) {
                send(out, "200 OK", "application/json", sleeve.tvJson().getBytes(StandardCharsets.UTF_8));
            } else if (path.equals("/api/tv/events")) {
                events(s, out);
            } else if (STATIC.matcher(path.substring(1)).matches()) {
                byte[] body;
                try (InputStream a = assets.open(path.substring(1))) { body = readAll(a); }
                catch (IOException e) { send(out, "404 Not Found", "text/plain", "not found\n".getBytes(StandardCharsets.UTF_8)); return; }
                send(out, "200 OK", type(path), body);
            } else {
                send(out, "404 Not Found", "text/plain", "not found\n".getBytes(StandardCharsets.UTF_8));
            }
        } catch (IOException ignored) {
            // the browser went away
        }
    }

    /** Server-Sent Events: one JSON snapshot every 50 ms until the browser disconnects. */
    private void events(Socket s, OutputStream out) throws IOException {
        s.setSoTimeout(0);
        out.write(("HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nCache-Control: no-store\r\n"
                + "Connection: keep-alive\r\nX-Accel-Buffering: no\r\n\r\nretry: 1000\n\n").getBytes(StandardCharsets.US_ASCII));
        out.flush();
        while (true) {
            out.write(("data: " + sleeve.tvJson() + "\n\n").getBytes(StandardCharsets.UTF_8));
            out.flush();
            try { Thread.sleep(50); } catch (InterruptedException e) { return; }
        }
    }

    private static void send(OutputStream out, String status, String type, byte[] body) throws IOException {
        out.write(("HTTP/1.1 " + status + "\r\nContent-Type: " + type + "\r\nContent-Length: " + body.length
                + "\r\nCache-Control: no-store\r\nConnection: close\r\n\r\n").getBytes(StandardCharsets.US_ASCII));
        out.write(body);
        out.flush();
    }

    private static String type(String path) {
        if (path.endsWith(".html")) return "text/html; charset=utf-8";
        if (path.endsWith(".css")) return "text/css; charset=utf-8";
        if (path.endsWith(".js")) return "text/javascript; charset=utf-8";
        if (path.endsWith(".woff2")) return "font/woff2";
        return "application/octet-stream";
    }

    private static byte[] readAll(InputStream in) throws IOException {
        java.io.ByteArrayOutputStream b = new java.io.ByteArrayOutputStream();
        byte[] buf = new byte[8192];
        for (int n; (n = in.read(buf)) > 0; ) b.write(buf, 0, n);
        return b.toByteArray();
    }
}
