/**
 * The laptop's address, as typed or scanned. The QR code the laptop prints (backend phoneqr.py) holds
 * http://<laptop>:<port>/dashboard/; a typed address may be "192.168.1.23", "192.168.1.23:8010" or a full URL.
 * No scheme means http and no port means 8000 (the backend's default). Same rules as the Android and iOS apps.
 */
export function baseURL(raw: string | null | undefined): string | null {
  if (raw == null) return null;
  let s = raw.trim();
  if (!s || /\s/.test(s)) return null;
  const lower = s.toLowerCase();
  if (!lower.startsWith('http://') && !lower.startsWith('https://')) {
    if (lower.includes('://')) return null; // another scheme (ftp://, file://): not a laptop
    s = 'http://' + s;
  }
  let u: URL;
  try {
    u = new URL(s);
  } catch {
    return null;
  }
  if (!u.hostname) return null;
  const scheme = u.protocol.replace(':', '').toLowerCase();
  let port = u.port ? Number(u.port) : null;
  if (port == null && scheme === 'http') port = 8000;
  if (port != null && (port <= 0 || port > 65535)) return null;
  return `${scheme}://${u.hostname.toLowerCase()}${port != null ? `:${port}` : ''}`;
}

/** "192.168.1.23:8000", without the scheme, for labels. */
export function label(base: string): string {
  return base.replace(/^https?:\/\//, '');
}

/** "Basic …" for a laptop started with VG_BASIC_AUTH, or null without a user name and password. */
export function basicAuth(user: string, password: string): string | null {
  if (!user && !password) return null;
  return 'Basic ' + base64(new TextEncoder().encode(`${user}:${password}`));
}

const B64 = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/';

/** Standard base64 of bytes (no dependency on btoa, which only takes Latin-1). */
export function base64(bytes: Uint8Array): string {
  let out = '';
  for (let i = 0; i < bytes.length; i += 3) {
    const n = (bytes[i] << 16) | ((bytes[i + 1] ?? 0) << 8) | (bytes[i + 2] ?? 0);
    out += B64[(n >> 18) & 63] + B64[(n >> 12) & 63];
    out += i + 1 < bytes.length ? B64[(n >> 6) & 63] : '=';
    out += i + 2 < bytes.length ? B64[n & 63] : '=';
  }
  return out;
}
