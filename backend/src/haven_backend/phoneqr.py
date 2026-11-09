"""A QR code a phone scans to open this backend: the HAVen app connects with it, and a plain phone camera opens
the dashboard in the browser. It holds only the address (``http://<laptop>:<port>/dashboard/``), never the password.

Printed by ``serve`` when it listens beyond this computer. Drawn with explicit black and white terminal colours (not
reverse video), so it scans the same in light and dark terminals.
"""
from __future__ import annotations

import ipaddress

_DARK, _LIGHT = 16, 231          # xterm-256 black and white
_RESET = "\x1b[0m"


def phone_url(addresses: list[str], port: int) -> str | None:
    """The dashboard address on this computer's network, preferring a normal LAN address over a link-local one."""
    ranked = sorted(addresses, key=lambda a: ipaddress.ip_address(a).is_link_local)
    return f"http://{ranked[0]}:{int(port)}/dashboard/" if ranked else None


def qr_lines(text: str, border: int = 2) -> list[str]:
    """The QR code as terminal lines: two modules per character cell (upper half block), black on white."""
    import segno

    rows = [list(r) for r in segno.make(text, error="m").matrix_iter(border=border)]
    if len(rows) % 2:
        rows.append([0] * len(rows[0]))
    out = []
    for top, bottom in zip(rows[0::2], rows[1::2]):
        cells = []
        for t, b in zip(top, bottom):
            fg = _DARK if t else _LIGHT
            bg = _DARK if b else _LIGHT
            cells.append(f"\x1b[38;5;{fg}m\x1b[48;5;{bg}m▀")
        out.append("".join(cells) + _RESET)
    return out
