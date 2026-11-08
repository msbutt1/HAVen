#!/usr/bin/env bash
# Assemble the public static site: the landing page at the root and the dashboard at /dashboard/.
# No server is needed: the "Open the demo" buttons open dashboard/?source=mock, which runs on a simulated feed in
# the browser and is labelled simulated. Upload the resulting site/ folder to any static host
# (livops.uz static hosting, GitHub Pages, Cloudflare Pages).
#
#   landing/build-site.sh            # writes site/ next to landing/
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
out="$root/site"
rm -rf "$out"
mkdir -p "$out/dashboard"
cp "$root/landing/index.html" "$root/landing/style.css" "$out/"
cp -R "$root/landing/assets" "$out/assets"
cp "$root/dashboard/index.html" "$out/dashboard/"
cp -R "$root/dashboard/css" "$root/dashboard/js" "$root/dashboard/assets" "$out/dashboard/"
echo "site assembled in $out ($(find "$out" -type f | wc -l | tr -d ' ') files)"
