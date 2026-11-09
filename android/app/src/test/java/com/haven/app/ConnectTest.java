package com.haven.app;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertNull;
import static org.junit.Assert.assertTrue;

import org.junit.Test;

import java.net.Inet4Address;
import java.net.InetAddress;
import java.nio.charset.StandardCharsets;

public class ConnectTest {
    @Test
    public void typedAddressesBecomeBaseUrls() {
        assertEquals("http://192.168.1.23:8000", Address.normalize("192.168.1.23"));
        assertEquals("http://192.168.1.23:8010", Address.normalize(" 192.168.1.23:8010 "));
        assertEquals("http://laptop.local:8000", Address.normalize("http://Laptop.local:8000/dashboard/#live"));
        assertEquals("https://vg.example.org", Address.normalize("https://vg.example.org/dashboard/"));
        assertEquals("http://[fe80::1]:8000", Address.normalize("[fe80::1]"));
        assertNull(Address.normalize(""));
        assertNull(Address.normalize("   "));
        assertNull(Address.normalize("192.168.1.23 8000"));
        assertNull(Address.normalize("ftp://192.168.1.23"));
        assertNull(Address.normalize("http://"));
        assertNull(Address.normalize("192.168.1.23:99999"));
        assertEquals("192.168.1.23:8000", Address.label("http://192.168.1.23:8000"));
    }

    @Test
    public void onlyTheBackendsHealthAnswerCounts() {
        assertTrue(Address.looksLikeHealth("{\"ok\":true,\"version\":\"0.1.0\",\"data_dir\":\"/x\",\"runtimes\":{}}"));
        assertFalse(Address.looksLikeHealth("<html>router login</html>"));
    }

    @Test
    public void discoveryRepliesAreParsed() {
        Discovery.Backend b = Discovery.parseReply("VGAPI 1 8000 0 lab-laptop", "192.168.1.40");
        assertEquals("http://192.168.1.40:8000", b.baseUrl());
        assertEquals("lab-laptop", b.name);
        assertFalse(b.password);
        Discovery.Backend p = Discovery.parseReply("VGAPI 1 8010 1 x", "/10.0.2.2");
        assertTrue(p.password);
        assertEquals("http://10.0.2.2:8010", p.baseUrl());
        assertEquals("http://[fe80::1]:8000", Discovery.parseReply("VGAPI 1 8000 0 a", "fe80::1%wlan0").baseUrl());
        assertEquals("192.168.1.40", Discovery.parseReply("VGAPI 1 8000 0 bad<name>", "192.168.1.40").name);
        assertNull(Discovery.parseReply("VGBRIDGE 1 8765", "192.168.1.40"));       // the wearable's answer
        assertNull(Discovery.parseReply("VGAPI 2 8000 0 x", "192.168.1.40"));
        assertNull(Discovery.parseReply("VGAPI 1 0 0 x", "192.168.1.40"));
        assertNull(Discovery.parseReply("VGAPI 1 8000 yes x", "192.168.1.40"));
        assertNull(Discovery.parseReply("VGAPI 1 port 0 x", "192.168.1.40"));
    }

    @Test
    public void theQueryIsPaddedSoTheBackendAnswers() {
        assertEquals(64, Discovery.QUERY.length);
        assertTrue(new String(Discovery.QUERY, StandardCharsets.US_ASCII).startsWith("VGFIND 1 "));
    }

    @Test
    public void broadcastAddressOfTheNetwork() throws Exception {
        Inet4Address a = (Inet4Address) InetAddress.getByName("192.168.1.40");
        assertEquals("192.168.1.255", Discovery.broadcast(a, 24).getHostAddress());
        assertEquals("192.168.1.63", Discovery.broadcast(a, 26).getHostAddress());
        assertEquals("10.255.255.255", Discovery.broadcast((Inet4Address) InetAddress.getByName("10.3.4.5"), 8).getHostAddress());
    }
}
