package com.haven.app;

import java.io.IOException;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URI;
import java.net.URISyntaxException;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.Locale;

/** The backend's address as the user types it, and a quick check that a HAVen backend answers there. */
final class Address {
    /** The backend's default web port (vg-backend serve --http-port). */
    static final int DEFAULT_PORT = 8000;

    static final int UNREACHABLE = -1;
    static final int NOT_HAVEN = -2;

    private Address() {}

    /**
     * "192.168.1.23", "192.168.1.23:8010", "http://laptop.local:8000/dashboard/" -> "http://host:port".
     * No scheme means http and no port means 8000. Returns null when there is no usable host.
     */
    static String normalize(String raw) {
        if (raw == null) return null;
        String s = raw.trim();
        if (s.isEmpty() || s.contains(" ")) return null;
        String lower = s.toLowerCase(Locale.ROOT);
        if (!lower.startsWith("http://") && !lower.startsWith("https://")) {
            if (lower.contains("://")) return null;          // another scheme (ftp://, file://): not a backend
            s = "http://" + s;
        }
        URI u;
        try {
            u = new URI(s);
        } catch (URISyntaxException e) {
            return null;
        }
        String host = u.getHost();
        if (host == null || host.isEmpty()) return null;
        String scheme = u.getScheme().toLowerCase(Locale.ROOT);
        int port = u.getPort();
        if (port == -1 && scheme.equals("http")) port = DEFAULT_PORT;
        if (port == 0 || port > 65535) return null;
        String h = host.contains(":") && !host.startsWith("[") ? "[" + host + "]" : host;   // IPv6 literal
        return scheme + "://" + h.toLowerCase(Locale.ROOT) + (port == -1 ? "" : ":" + port);
    }

    /** What the connect screen shows for a base URL: the host and port, without "http://". */
    static String label(String base) {
        return base.replaceFirst("^https?://", "");
    }

    /**
     * GET base/api/health. Returns the HTTP status (200, or 401 when the backend asks for a password),
     * NOT_HAVEN when something else answered 200, or UNREACHABLE.
     */
    static int probe(String base, int timeoutMs) {
        HttpURLConnection c = null;
        try {
            c = (HttpURLConnection) new URL(base + "/api/health").openConnection();
            c.setConnectTimeout(timeoutMs);
            c.setReadTimeout(timeoutMs);
            c.setInstanceFollowRedirects(false);
            c.setUseCaches(false);
            int code = c.getResponseCode();
            if (code != 200) return code;
            byte[] buf = new byte[2048];
            int n;
            try (InputStream in = c.getInputStream()) {
                n = in.read(buf);
            }
            String body = n > 0 ? new String(buf, 0, n, StandardCharsets.UTF_8) : "";
            return looksLikeHealth(body) ? 200 : NOT_HAVEN;
        } catch (IOException | ClassCastException e) {
            return UNREACHABLE;
        } finally {
            if (c != null) c.disconnect();
        }
    }

    /** The backend's /api/health answer starts {"ok":true,"version":...,"runtimes":...}. */
    static boolean looksLikeHealth(String body) {
        return body.contains("\"ok\"") && body.contains("\"runtimes\"");
    }
}
