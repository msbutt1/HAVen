"""The landing page is static and self-contained, and keeps the project's honest framing."""
import re
from html.parser import HTMLParser
from pathlib import Path

LANDING = Path(__file__).resolve().parents[2] / "landing"


class Scan(HTMLParser):
    def __init__(self):
        super().__init__()
        self.refs, self.imgs, self.tags, self.h1 = [], [], [], 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        self.tags.append(tag)
        if tag == "h1":
            self.h1 += 1
        if tag == "img":
            self.imgs.append(a)
        for k in ("src", "srcset", "href"):
            if k in a:
                self.refs.append((tag, k, a[k]))


def test_landing_is_self_contained_and_accessible():
    html = (LANDING / "index.html").read_text()
    s = Scan()
    s.feed(html)
    assert "script" not in s.tags                     # no scripts at all
    assert s.h1 == 1 and '<html lang="en">' in html and "<title>" in html
    assert all(i.get("alt") for i in s.imgs)          # every image is described
    for tag, _k, ref in s.refs:
        if ref.startswith(("#", "data:")):
            continue
        if ref.startswith("http"):
            assert tag == "a", f"third-party resource loaded: {ref}"   # only plain links may leave the site
            continue
        path = ref.split("#")[0].split("?")[0]
        if path.startswith("dashboard/"):   # landing/build-site.sh puts the dashboard at /dashboard/
            target = LANDING.parent / path
            assert (target / "index.html").exists() if path.endswith("/") else target.exists(), f"missing: {ref}"
        else:
            assert (LANDING / path).exists(), f"missing: {ref}"
    css = (LANDING / "style.css").read_text()
    assert "http" not in re.sub(r"/\*.*?\*/", "", css, flags=re.S)     # no @import / url() to other sites


def test_the_demo_links_open_the_simulated_feed_and_need_no_server():
    html = (LANDING / "index.html").read_text()
    links = re.findall(r'href="(dashboard/[^"]*)"', html)
    assert links and all(link == "dashboard/?source=mock" for link in links)


def test_landing_keeps_the_scientific_framing():
    text = re.sub(r"<[^>]+>", " ", (LANDING / "index.html").read_text()).lower()
    for must in ("not a medical device", "experimental", "not a dose", "replay", "simulated", "not iso 5349"):
        assert must in text, must
    for banned in ("clinically proven", "guarantee", "prevents injury", "diagnoses", "early detection of", "certified"):
        assert banned not in text, banned


def test_the_built_site_carries_every_file_the_dashboard_loads():
    # build-site.sh copies the dashboard folder by folder; a new folder (e.g. vendor/ for Chart.js) must be listed there
    dash = LANDING.parent / "dashboard"
    s = Scan()
    s.feed((dash / "index.html").read_text())
    local = [v for tag, k, v in s.refs if tag in ("script", "link") and not v.startswith(("http", "#", "data:"))]
    assert local, "the dashboard page loads no local files?"
    script = (LANDING / "build-site.sh").read_text()
    for ref in local:
        assert (dash / ref).is_file(), f"missing: dashboard/{ref}"
        top = ref.split("/")[0]
        assert f'"$root/dashboard/{top}"' in script or top == "index.html", f"build-site.sh does not copy dashboard/{top}"
