#!/usr/bin/env python3
"""Build a double-clickable macOS .app that launches tunnel.py.

    make_app.py "Tunnel Vision"                              -> ~/Desktop/Tunnel Vision.app
    make_app.py "Dyson Swarm" --start ~/wp/dyson.png      start on that wallpaper; icon from it
    make_app.py "Tunnel Vision" --icon ~/wp/x.png --out ~/Applications

The bundle is a shell launcher around the Python interpreter that runs this script,
so it needs its own Accessibility grant the first time it is opened. Launch output
goes to ~/.config/terminal-tunnel-vision/launch.log.
"""
import argparse
import os
import plistlib
import re
import shlex
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tunnel import write_icon_png  # noqa: E402  (needs HERE on sys.path first)

HERE = os.path.dirname(os.path.abspath(__file__))
TUNNEL_VISION = os.path.join(HERE, "tunnel.py")
ICON_SIZES = (16, 32, 128, 256, 512)

LAUNCHER = """#!/bin/bash
LOG="$HOME/.config/terminal-tunnel-vision/launch.log"
mkdir -p "$(dirname "$LOG")"
echo "=== launch $(date '+%Y-%m-%d %H:%M:%S') from $0" >> "$LOG"
exec {python} {script}{args} >> "$LOG" 2>&1
"""


def _sips(*args):
    """Every sips call is checked and kept quiet."""
    return subprocess.run(["sips", *args], check=True, capture_output=True)


def build_icon(image, icns_path):
    """Write an .icns showing the same thing the running app puts in the Dock: the
    wallpaper with a terminal window on it. sips and iconutil ship with macOS."""
    with tempfile.TemporaryDirectory() as tmp:
        square = os.path.join(tmp, "square.png")
        write_icon_png(image, square)
        iconset = os.path.join(tmp, "AppIcon.iconset")
        os.mkdir(iconset)
        for size in ICON_SIZES:
            for suffix, px in (("", size), ("@2x", size * 2)):
                _sips("-z", str(px), str(px), square,
                      "--out", os.path.join(iconset, f"icon_{size}x{size}{suffix}.png"))
        subprocess.run(["iconutil", "-c", "icns", iconset, "-o", icns_path], check=True)


def build_app(name, out_dir, start, icon):
    app = os.path.join(out_dir, f"{name}.app")
    contents = os.path.join(app, "Contents")
    macos = os.path.join(contents, "MacOS")
    resources = os.path.join(contents, "Resources")
    os.makedirs(macos, exist_ok=True)
    os.makedirs(resources, exist_ok=True)

    args = f" --start {shlex.quote(os.path.abspath(start))}" if start else ""
    launcher = os.path.join(macos, name)
    with open(launcher, "w") as fh:
        fh.write(LAUNCHER.format(
            python=shlex.quote(sys.executable), script=shlex.quote(TUNNEL_VISION), args=args,
        ))
    os.chmod(launcher, 0o755)

    plist = {
        "CFBundleExecutable": name,
        "CFBundleIdentifier": "terminal-tunnel-vision." + re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-"),
        "CFBundleName": name,
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": "1.0",
        "LSMinimumSystemVersion": "10.13",
        "NSHighResolutionCapable": True,
    }
    icon = icon or start
    if icon:
        build_icon(icon, os.path.join(resources, "AppIcon.icns"))
        plist["CFBundleIconFile"] = "AppIcon"
    with open(os.path.join(contents, "Info.plist"), "wb") as fh:
        plistlib.dump(plist, fh)
    # Finder caches icons by bundle; touching the bundle makes it re-read.
    os.utime(app, None)
    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("name", help='app name, e.g. "Tunnel Vision"')
    parser.add_argument("--start", help="wallpaper to show first (passed as --start to tunnel.py)")
    parser.add_argument("--icon", help="image for the app icon (default: the --start wallpaper)")
    parser.add_argument("--out", default=os.path.expanduser("~/Desktop"), help="folder for the .app (default: ~/Desktop)")
    opts = parser.parse_args()
    app = build_app(opts.name, os.path.expanduser(opts.out), opts.start, opts.icon)
    print(f"built {app}")


if __name__ == "__main__":
    main()
