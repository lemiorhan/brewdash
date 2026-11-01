"""Takes the README screenshots from the demo (scripts/demo.py), in light and dark.

    .venv/bin/python scripts/screenshots.py

Needs Playwright for Python (see CONTRIBUTING.md). Writes docs/screenshots/*.png.
"""
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
import demo  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "docs" / "screenshots"

# (file name, tab, row to open in the detail panel or None)
SHOTS = [
    ("overview", "overview", None),
    ("packages", "requested", "f:node"),
    ("security", "security", None),
    ("services", "services", None),
    ("apps", "apps", None),
    ("shell", "shell", None),
]


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    server, url, _ = demo.start()
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        for scheme in ("light", "dark"):
            page = browser.new_page(viewport={"width": 1440, "height": 900}, device_scale_factor=2, color_scheme=scheme)
            page.goto(url)
            page.wait_for_selector('.tile:has-text("on disk") .tile-value:not(:text-is("…"))')
            page.wait_for_selector('.att:has-text("apps without a real signature")')
            for name, tab, row in SHOTS:
                page.click(f'nav button[data-tab="{tab}"]')
                if row:
                    page.click(f'tr.row[data-id="{row}"] td:nth-child(2)')
                    page.wait_for_selector("#graph canvas")
                    page.wait_for_timeout(600)
                page.screenshot(path=str(OUT / f"{name}-{scheme}.png"))
                if row:
                    page.click("#detail [data-close]")
            page.close()
        browser.close()
    server.shutdown()
    print("\n".join(sorted(str(p.relative_to(OUT.parent.parent)) for p in OUT.glob("*.png"))))


if __name__ == "__main__":
    main()
