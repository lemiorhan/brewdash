"""Renders every icon from web/logo.svg: the favicons, the macOS app icon and the landing page images.

    .venv/bin/python scripts/icons.py

Needs Playwright for Python (see CONTRIBUTING.md) and macOS for iconutil. Run it again after changing the logo.
"""
import shutil
import subprocess
import tempfile
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
LOGO = ROOT / "web" / "logo.svg"
ASSETS = ROOT / "docs" / "assets"

# Apple's macOS icon grid: on a 1024 canvas the tile is 824 wide, with room left for its shadow.
MAC_TILE = 824 / 1024

FULL = """<body style="margin:0;background:transparent"><div style="width:{s}px;height:{s}px">{svg}</div></body>"""
MAC = """<body style="margin:0;background:transparent;display:flex;align-items:center;justify-content:center;
  width:{s}px;height:{s}px"><div style="width:{t}px;height:{t}px;filter:drop-shadow(0 {d}px {b}px rgba(0,0,0,.35))">{svg}</div></body>"""
SOCIAL = """<body style="margin:0;width:1200px;height:630px;background:radial-gradient(circle at 20% 0%,#232a36,#0f1115 70%);
  color:#e6e8ec;font-family:-apple-system,system-ui,sans-serif;display:flex;align-items:center;gap:64px;padding:0 96px;box-sizing:border-box">
  <div style="width:260px;height:260px;flex:none;filter:drop-shadow(0 18px 40px rgba(0,0,0,.5))">{svg}</div>
  <div><div style="font-size:96px;font-weight:800;letter-spacing:-3px">brew<span style="color:#f2b33d">dash</span></div>
  <div style="font-size:38px;line-height:1.3;color:#aab2c0;margin-top:12px">A local dashboard for Homebrew.<br>See what is installed, and why.</div></div></body>"""


def render(page, html, size, out):
    page.set_viewport_size({"width": size[0], "height": size[1]})
    page.set_content(html)
    page.screenshot(path=str(out), omit_background=True)


def main():
    svg = LOGO.read_text()
    ASSETS.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw, tempfile.TemporaryDirectory() as tmp:
        page = pw.chromium.launch().new_page()
        full = lambda s, out: render(page, FULL.format(s=s, svg=svg), (s, s), out)
        full(32, ROOT / "web" / "favicon-32.png")
        full(32, ASSETS / "favicon-32.png")
        full(180, ASSETS / "apple-touch-icon.png")
        render(page, SOCIAL.format(svg=svg), (1200, 630), ASSETS / "social.png")
        iconset = Path(tmp) / "brewdash.iconset"
        iconset.mkdir()
        for base in (16, 32, 128, 256, 512):
            for scale in (1, 2):
                s = base * scale
                name = f"icon_{base}x{base}{'@2x' if scale == 2 else ''}.png"
                render(page, MAC.format(s=s, t=round(s * MAC_TILE), d=max(1, s // 64), b=max(1, s // 40), svg=svg), (s, s), iconset / name)
        (ROOT / "macos").mkdir(exist_ok=True)
        subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(ROOT / "macos" / "brewdash.icns")], check=True)
    shutil.copyfile(LOGO, ASSETS / "logo.svg")
    for p in sorted([ROOT / "web" / "favicon-32.png", ROOT / "macos" / "brewdash.icns", *ASSETS.iterdir()]):
        print(p.relative_to(ROOT))


if __name__ == "__main__":
    main()
