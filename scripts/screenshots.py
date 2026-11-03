"""Takes the README and Pages screenshots of the Mac app on the demo (scripts/demo.py), in light and dark.

    python3 scripts/screenshots.py

Builds scripts/screenshots.swift with swiftc, which opens the app's own window on the demo and saves it with
screencapture, so the app you run this from needs Screen Recording (System Settings > Privacy & Security).
Writes docs/screenshots/*.png at the scale of the sharpest screen that holds a 1440x900 window.
"""
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import demo  # noqa: E402

OUT = ROOT / "docs" / "screenshots"


def main():
    with tempfile.TemporaryDirectory() as tmp:
        exe = Path(tmp) / "screenshots"
        subprocess.run(["swiftc", "-swift-version", "5", "-parse-as-library", "-D", "BREWDASH_CHECK", "-o", str(exe),
                        str(ROOT / "scripts" / "screenshots.swift"), str(ROOT / "macos" / "brewdash.swift"),
                        str(ROOT / "macos" / "policy.swift")], check=True)
        shots = Path(tmp) / "shots"
        server, url, _ = demo.start()
        try:
            subprocess.run([str(exe), url, str(shots)], check=True)
        finally:
            server.shutdown()
        # A run that stops halfway leaves docs/screenshots as it was, not half old and half new.
        made = sorted(p.name for p in shots.glob("*.png"))
        missing = sorted({p.name for p in OUT.glob("*.png")} - set(made))
        if not made or missing:
            sys.exit("screenshots: the harness did not write " + (", ".join(missing) or "any image"))
        OUT.mkdir(parents=True, exist_ok=True)
        for name in made:
            shutil.copyfile(shots / name, OUT / name)
    print("\n".join(str((OUT / name).relative_to(ROOT)) for name in made))


if __name__ == "__main__":
    main()
