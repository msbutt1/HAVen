# Publishing the dashboard on livops.uz (data stays on this computer)

The backend serves the dashboard at `/dashboard/` and its API on the same origin. A **Cloudflare Tunnel**
publishes it under a hostname on `livops.uz`: requests are forwarded to `127.0.0.1` on this computer, the data
directory never leaves it, and nothing is uploaded anywhere.

What is served: **simulated/demo data only**. The backend replays `backup/demo-session`, labelled *replay*
(the dashboard shows "Recorded replay ... This is not live."), and opens no serial device. The API has
no accounts, so the page is protected with HTTP Basic auth (`VG_BASIC_AUTH`): without the password nothing is
served, including the WebSocket.

## One-time setup (needs your Cloudflare login)

Use a **subdomain** such as `vibeguard.livops.uz`: `livops.uz` itself already points at something else, and
a tunnel route would replace that record.

```bash
cloudflared tunnel login                       # opens a browser: pick the livops.uz zone
cloudflared tunnel create vibeguard
cloudflared tunnel route dns vibeguard vibeguard.livops.uz
```

## Run

```bash
cd backend
VG_HOSTNAME=vibeguard.livops.uz VG_BASIC_AUTH='demo:choose-a-long-password' deploy/run-livops.sh
```

Open `https://vibeguard.livops.uz/dashboard/`. Stop with Ctrl-C (it stops both the backend and the tunnel). The site
is only up while this computer is on and the script is running.

## Notes

- Data directory: `~/.vibeguard/livops-data` (`VG_DATA_DIR` to change). Recordings made in the dashboard are
  stored there, labelled replay.
- Do not point this at a real device: live EMG/IMU recordings of a person should not sit behind a public name.
- The perception-test (station) screens are not connected to the backend yet and show that plainly.
