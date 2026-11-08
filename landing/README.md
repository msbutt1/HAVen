# VibeGuard landing page

A static page built on livops.uz's design system (its colour tokens for light and dark, rim-lit panels, the hero
grid with a moving light, section layout and footer): `index.html`, `style.css`, two screenshots and self-hosted fonts (Onest and
JetBrains Mono, SIL OFL, licences in `assets/fonts/`). No build step, no scripts (the header and reveal effects
use CSS scroll timelines where the browser has them), and no third-party requests (no font CDN, no analytics). Open `index.html` in a browser, or serve the folder:

```bash
cd landing && python3 -m http.server 8800
```

**The "Open the demo" buttons open `dashboard/?source=mock`**: the dashboard's simulated feed, which runs entirely in
the browser (no server) and is labelled "Simulated data" the whole time. To publish the page with its demo, assemble
the static site and upload the `site/` folder to any static host (livops.uz static hosting, GitHub Pages, Cloudflare Pages):

```bash
landing/build-site.sh        # writes site/: the landing page at /, the dashboard at /dashboard/
```

The source link points at the GitHub repository; change it if the repository is private.

The screenshots are real captures of the dashboard replaying `backend/backup/demo-session` (the banner in them says
"Recorded replay ... This is not live."). Retake them after a dashboard redesign.

Wording follows the project brief's scientific framing: keep the three readings separate, call the score experimental,
no diagnostic or dosimetry claims. Keep it that way when editing.
