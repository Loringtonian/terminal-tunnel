#!/usr/bin/env python3
"""Build a double-clickable macOS .app that launches focus_mode.py.

    make_app.py "Focus Mode"                              -> ~/Desktop/Focus Mode.app
    make_app.py "Dyson Swarm" --start ~/wp/dyson.png      start on that wallpaper; icon from it
    make_app.py "Focus Mode" --icon ~/wp/x.png --out ~/Applications

The bundle is a shell launcher around the Python interpreter that runs this script,
so it needs its own Accessibility grant the first time it is opened. Launch output
goes to ~/.config/focus-mode/launch.log.
"""
import argparse
import os
import plistlib
import re
import shlex
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
FOCUS_MODE = os.path.join(HERE, "focus_mode.py")
ICON_SIZES = (16, 32, 128, 256, 512)

LAUNCHER = """#!/bin/bash
LOG="$HOME/.config/focus-mode/launch.log"
mkdir -p "$(dirname "$LOG")"
echo "=== launch $(date '+%Y-%m-%d %H:%M:%S') from $0" >> "$LOG"
exec {python} {script}{args} >> "$LOG" 2>&1
"""


def image_size(path):
    out = subprocess.run(
        ["sips", "-g", "pixelWidth", "-g", "pixelHeight", path],
        check=True, capture_output=True, text=True,
    ).stdout
    dims = dict(line.strip().split(": ") for line in out.splitlines()[1:])
    return int(dims["pixelWidth"]), int(dims["pixelHeight"])


def build_icon(image, icns_path):
    """Centre-crop the image to a square and write an .icns with sips + iconutil,
    both of which ship with macOS."""
    with tempfile.TemporaryDirectory() as tmp:
        side = min(image_size(image))
        square = os.path.join(tmp, "square.png")
        subprocess.run(
            ["sips", "-s", "format", "png", "--cropToHeightWidth", str(side), str(side),
             image, "--out", square],
            check=True, capture_output=True,
        )
        iconset = os.path.join(tmp, "AppIcon.iconset")
        os.mkdir(iconset)
        for size in ICON_SIZES:
            for suffix, px in (("", size), ("@2x", size * 2)):
                subprocess.run(
                    ["sips", "-z", str(px), str(px), square,
                     "--out", os.path.join(iconset, f"icon_{size}x{size}{suffix}.png")],
                    check=True, capture_output=True,
                )
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
            python=shlex.quote(sys.executable), script=shlex.quote(FOCUS_MODE), args=args,
        ))
    os.chmod(launcher, 0o755)

    plist = {
        "CFBundleExecutable": name,
        "CFBundleIdentifier": "focus-mode." + re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-"),
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
    parser.add_argument("name", help='app name, e.g. "Focus Mode"')
    parser.add_argument("--start", help="wallpaper to show first (passed as --start to focus_mode.py)")
    parser.add_argument("--icon", help="image for the app icon (default: the --start wallpaper)")
    parser.add_argument("--out", default=os.path.expanduser("~/Desktop"), help="folder for the .app (default: ~/Desktop)")
    opts = parser.parse_args()
    app = build_app(opts.name, os.path.expanduser(opts.out), opts.start, opts.icon)
    print(f"built {app}")


if __name__ == "__main__":
    main()
