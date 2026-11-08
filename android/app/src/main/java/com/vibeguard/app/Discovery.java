package com.vibeguard.app;

import android.content.Context;
import android.net.ConnectivityManager;
import android.net.LinkAddress;
import android.net.LinkProperties;
import android.net.Network;
import android.net.RouteInfo;

import java.io.IOException;
import java.net.DatagramPacket;
import java.net.DatagramSocket;
import java.net.Inet4Address;
import java.net.InetAddress;
import java.net.SocketTimeoutException;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

/**
 * Finds VibeGuard backends on this Wi-Fi. Sends "VGFIND 1" (padded to 64 bytes) to UDP 8767 and collects the
 * "VGAPI 1 <http port> <password 0|1> <name>" replies; the backend's address is the reply's source address.
 * The backend side is backend/src/vibeguard_backend/appdiscovery.py.
 */
final class Discovery {
    static final int PORT = 8767;
    static final int QUERY_LENGTH = 64;
    static final byte[] QUERY = query();

    private Discovery() {}

    /** One backend that answered. */
    static final class Backend {
        final String host;
        final int port;
        final boolean password;
        final String name;

        Backend(String host, int port, boolean password, String name) {
            this.host = host;
            this.port = port;
            this.password = password;
            this.name = name;
        }

        String baseUrl() {
            String h = host.contains(":") ? "[" + host + "]" : host;
            return "http://" + h + ":" + port;
        }
    }

    private static byte[] query() {
        byte[] q = new byte[QUERY_LENGTH];
        Arrays.fill(q, (byte) ' ');
        byte[] head = "VGFIND 1".getBytes(StandardCharsets.US_ASCII);
        System.arraycopy(head, 0, q, 0, head.length);
        return q;
    }

    /** A reply from {@code fromHost}, or null when it is not a VibeGuard answer. */
    static Backend parseReply(String text, String fromHost) {
        if (text == null || fromHost == null) return null;
        String[] p = text.trim().split(" ");
        if (p.length < 4 || !p[0].equals("VGAPI") || !p[1].equals("1")) return null;
        int port;
        try {
            port = Integer.parseInt(p[2]);
        } catch (NumberFormatException e) {
            return null;
        }
        if (port < 1 || port > 65535 || !(p[3].equals("0") || p[3].equals("1"))) return null;
        String name = p.length > 4 && p[4].matches("[A-Za-z0-9-]{1,24}") ? p[4] : fromHost;
        String host = fromHost.startsWith("/") ? fromHost.substring(1) : fromHost;
        int zone = host.indexOf('%');
        if (zone >= 0) host = host.substring(0, zone);
        return new Backend(host, port, p[3].equals("1"), name);
    }

    /**
     * Ask, wait {@code waitMs}, and return every backend that answered (one entry per address and port).
     * Runs on a background thread: it blocks for {@code waitMs}.
     */
    static List<Backend> find(Context context, int waitMs) throws IOException {
        Set<InetAddress> targets = targets(context);
        Map<String, Backend> found = new LinkedHashMap<>();
        try (DatagramSocket sock = new DatagramSocket()) {
            sock.setBroadcast(true);
            sock.setSoTimeout(150);
            send(sock, targets);
            long start = System.nanoTime();
            boolean resent = false;
            byte[] buf = new byte[256];
            while (true) {
                long elapsedMs = (System.nanoTime() - start) / 1_000_000;
                if (elapsedMs >= waitMs) break;
                if (!resent && elapsedMs >= waitMs / 3) {     // UDP can drop a packet: ask a second time
                    send(sock, targets);
                    resent = true;
                }
                DatagramPacket packet = new DatagramPacket(buf, buf.length);
                try {
                    sock.receive(packet);
                } catch (SocketTimeoutException e) {
                    continue;
                }
                String text = new String(packet.getData(), 0, packet.getLength(), StandardCharsets.US_ASCII);
                Backend b = parseReply(text, packet.getAddress().getHostAddress());
                if (b != null) found.put(b.host + ":" + b.port, b);
            }
        }
        return new ArrayList<>(found.values());
    }

    private static void send(DatagramSocket sock, Set<InetAddress> targets) {
        for (InetAddress t : targets) {
            try {
                sock.send(new DatagramPacket(QUERY, QUERY.length, t, PORT));
            } catch (IOException ignored) {
                // a network that refuses broadcast: the other targets may still work
            }
        }
    }

    /**
     * Where to ask: the network's own broadcast address (e.g. 192.168.1.255), the general broadcast address,
     * and the default gateway. On a real Wi-Fi the gateway is the router, which ignores the packet; on the
     * Android emulator the gateway (10.0.2.2) is the computer the emulator runs on.
     */
    static Set<InetAddress> targets(Context context) throws IOException {
        Set<InetAddress> out = new LinkedHashSet<>();
        ConnectivityManager cm = context.getSystemService(ConnectivityManager.class);
        Network n = cm == null ? null : cm.getActiveNetwork();
        LinkProperties lp = n == null ? null : cm.getLinkProperties(n);
        if (lp != null) {
            for (LinkAddress la : lp.getLinkAddresses()) {
                InetAddress a = la.getAddress();
                if (a instanceof Inet4Address && !a.isLoopbackAddress()) {
                    out.add(broadcast((Inet4Address) a, la.getPrefixLength()));
                }
            }
        }
        out.add(InetAddress.getByName("255.255.255.255"));
        if (lp != null) {
            for (RouteInfo r : lp.getRoutes()) {
                InetAddress g = r.getGateway();
                if (r.isDefaultRoute() && g instanceof Inet4Address && !g.isAnyLocalAddress()) out.add(g);
            }
        }
        return out;
    }

    /** 192.168.1.40/24 -> 192.168.1.255. */
    static InetAddress broadcast(Inet4Address address, int prefixLength) throws IOException {
        byte[] b = address.getAddress();
        int ip = ((b[0] & 0xff) << 24) | ((b[1] & 0xff) << 16) | ((b[2] & 0xff) << 8) | (b[3] & 0xff);
        int mask = prefixLength <= 0 ? 0 : prefixLength >= 32 ? -1 : -1 << (32 - prefixLength);
        int bc = ip | ~mask;
        return InetAddress.getByAddress(new byte[] {(byte) (bc >>> 24), (byte) (bc >>> 16), (byte) (bc >>> 8), (byte) bc});
    }
}
