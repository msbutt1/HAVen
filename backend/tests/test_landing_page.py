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
        assert (LANDING / ref.split("#")[0]).exists() or ref.endswith("/"), f"missing: {ref}"
    css = (LANDING / "style.css").read_text()
    assert "http" not in re.sub(r"/\*.*?\*/", "", css, flags=re.S)     # no @import / url() to other sites


def test_landing_keeps_the_scientific_framing():
    text = re.sub(r"<[^>]+>", " ", (LANDING / "index.html").read_text()).lower()
    for must in ("not a medical device", "experimental", "not a dose", "replay", "simulated", "not iso 5349"):
        assert must in text, must
    for banned in ("clinically proven", "guarantee", "prevents injury", "diagnoses", "early detection of", "certified"):
        assert banned not in text, banned
