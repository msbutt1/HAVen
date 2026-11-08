# VibeGuard landing page

A static page in the style of livops.uz: `index.html`, `style.css`, two screenshots and self-hosted fonts (Onest and
JetBrains Mono, SIL OFL, licences in `assets/fonts/`). No build step, no scripts, and no third-party requests (no font
CDN, no analytics). Open `index.html` in a browser, or serve the folder:

```bash
cd landing && python3 -m http.server 8800
```

**The "Open the demo" buttons point at `dashboard/`** (relative). Host this folder at the root of the same site that
serves the dashboard (for example the backend's `/dashboard/`), or change those two links to the demo's address.
The source link points at the GitHub repository; change it if the repository is private.

The screenshots are real captures of the dashboard replaying `backend/backup/demo-session` (the banner in them says
"Recorded replay ... This is not live."). Retake them after a dashboard redesign.

Wording follows the project brief's scientific framing: keep the three readings separate, call the score experimental,
no diagnostic or dosimetry claims. Keep it that way when editing.
