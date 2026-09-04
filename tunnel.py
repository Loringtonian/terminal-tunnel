#!/usr/bin/env python3
"""
Terminal Tunnel Vision — a full-screen art scrim with a hole cut around the one window
you are working in. Your real desktop background is untouched; this is a mode
you summon and dismiss.

The scrim is a non-activating panel above all normal windows that IGNORES ALL
MOUSE EVENTS, so it can never take a click, steal focus, or hide a window from
you: the hole continuously follows whatever window is frontmost, including
after a plain Cmd+Tab.

Hotkeys — hold the RIGHT Option key (ctrl-opt also works as a fallback). They are
global, so they work while you are typing in the focused window. The right Option
key is used because macOS reports the two Option keys as distinct device bits, and
these events are consumed, so LEFT Option keeps doing word-jump as normal.
    rightopt-Left / Right   cycle terminal windows
    rightopt-Up   / Down    cycle text-editor windows
    rightopt-Space          window picker; then a bare number key or a click
    rightopt-\\              next wallpaper
    rightopt-C              center the current front window
    rightopt-Escape         exit, restoring every window this app moved

Usage:
    tunnel.py --setup           choose which apps count as terminals and editors
    tunnel.py [image ...]       start tunnel vision; images override the config list
    tunnel.py --start PATH      start tunnel vision on PATH, then the config list
    tunnel.py --list            print the window pools and exit
    tunnel.py --quit            ask the running instance to restore and exit
    tunnel.py --restore         re-apply the crash sidecar and exit

Configuration (wallpapers, which apps count as terminals and editors) lives in
~/.config/terminal-tunnel-vision/config.json — see README.md.
"""
import json
import os
import signal
import sys
import time

import objc
from AppKit import (
    NSAlert, NSApplication, NSApplicationActivationPolicyRegular, NSBezierPath,
    NSColor, NSCompositingOperationSourceOver, NSEvenOddWindingRule,
    NSFont, NSGraphicsContext, NSImage,
    NSMenu, NSMenuItem, NSPanel, NSScreen, NSTimer, NSView,
    NSWindowCollectionBehaviorCanJoinAllSpaces,
    NSWindowCollectionBehaviorFullScreenNone,
    NSWindowCollectionBehaviorIgnoresCycle,
    NSWindowCollectionBehaviorTransient, NSWindowStyleMaskBorderless,
    NSWindowStyleMaskNonactivatingPanel, NSBackingStoreBuffered,
    NSFloatingWindowLevel, NSWorkspace,
)
from ApplicationServices import (
    AXUIElementCopyAttributeValue, AXUIElementCreateApplication,
    AXIsProcessTrustedWithOptions, AXUIElementPerformAction,
    AXUIElementSetAttributeValue, AXValueCreate, AXValueGetValue,
    kAXValueCGPointType, kAXValueCGSizeType,
)
from Foundation import (NSAppleScript, NSMakePoint, NSMakeRect, NSObject,
                        NSPointInRect, NSRunLoop, NSString)
from Quartz import (
    CFMachPortCreateRunLoopSource, CFRunLoopAddSource, CFRunLoopGetCurrent,
    CGEventGetFlags, CGEventGetIntegerValueField, CGEventMaskBit,
    CGEventTapCreate, CGEventTapEnable, kCGEventKeyDown,
    kCGEventTapDisabledByTimeout, kCGEventTapDisabledByUserInput,
    kCGEventTapOptionDefault, kCGHeadInsertEventTap, kCGKeyboardEventKeycode,
    kCGSessionEventTap, kCFRunLoopCommonModes,
)

CONFIG_DIR = os.path.expanduser(os.environ.get("TUNNEL_VISION_HOME", "~/.config/terminal-tunnel-vision"))
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
SIDECAR = os.path.join(CONFIG_DIR, "restore.json")
QUIT_FLAG = os.path.join(CONFIG_DIR, "quit")
_TERMINATE = [False]

DEFAULT_WALLPAPER_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wallpapers")
DEFAULT_TERMINALS = ("Terminal", "iTerm2", "Warp", "Ghostty", "Alacritty", "kitty")
DEFAULT_EDITORS = ("TextEdit", "CotEditor", "BBEdit", "Sublime Text", "Code", "Cursor", "Zed")
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".heic", ".tiff")


def load_config():
    """config.json over the built-in defaults. Keys: wallpapers (image paths, cycled
    in order), terminals and editors (app names as macOS reports them). With no
    wallpapers listed, every image in a wallpapers/ folder beside this file is used."""
    data = {}
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH) as fh:
            data = json.load(fh)
    wallpapers = [os.path.expanduser(p) for p in data.get("wallpapers", [])]
    if not wallpapers and os.path.isdir(DEFAULT_WALLPAPER_DIR):
        wallpapers = sorted(
            os.path.join(DEFAULT_WALLPAPER_DIR, name)
            for name in os.listdir(DEFAULT_WALLPAPER_DIR)
            if name.lower().endswith(IMAGE_EXTENSIONS)
        )
    return {
        "wallpapers": wallpapers,
        "terminals": tuple(data.get("terminals", DEFAULT_TERMINALS)),
        "editors": tuple(data.get("editors", DEFAULT_EDITORS)),
    }


CONFIG = load_config()
TERMINAL_APPS = CONFIG["terminals"]
EDITOR_APPS = CONFIG["editors"]

KEY_LEFT, KEY_RIGHT, KEY_DOWN, KEY_UP = 123, 124, 125, 126
KEY_ESC, KEY_BACKSLASH, KEY_C, KEY_SPACE = 53, 42, 8, 49
# Top-row 1..9 then 0, mapped to picker rows 0..9.
NUMBER_KEYS = {18: 0, 19: 1, 20: 2, 21: 3, 23: 4, 22: 5, 26: 6, 28: 7, 25: 8, 29: 9}
FLAG_CTRL, FLAG_ALT, FLAG_CMD, FLAG_SHIFT = 0x40000, 0x80000, 0x100000, 0x20000
# Device-dependent bits that tell the two Option keys apart (left = 0x20).
DEV_RIGHT_ALT = 0x40

DEBUG = bool(os.environ.get("TUNNEL_VISION_DEBUG"))

# The hole hugs the window exactly — no pad. Any pad at all exposes a ring of
# whatever is behind the window (i.e. the real desktop) around its edge.
HOLE_RADIUS = 10.0
MIN_WIN_W, MIN_WIN_H = 200, 120


# ---------------------------------------------------------------- accessibility

def _ax_copy(element, attribute):
    err, value = AXUIElementCopyAttributeValue(element, attribute, None)
    return value if err == 0 else None


def ax_app(pid):
    return AXUIElementCreateApplication(pid)


def ax_point(element, attribute):
    value = _ax_copy(element, attribute)
    if value is None:
        return None
    ok, point = AXValueGetValue(value, kAXValueCGPointType, None)
    return (point.x, point.y) if ok else None


def ax_size(element):
    value = _ax_copy(element, "AXSize")
    if value is None:
        return None
    ok, size = AXValueGetValue(value, kAXValueCGSizeType, None)
    return (size.width, size.height) if ok else None


def win_frame(element):
    """(x, y, w, h) in Accessibility coordinates: origin top-left, y down."""
    position = ax_point(element, "AXPosition")
    size = ax_size(element)
    if position is None or size is None:
        return None
    return (position[0], position[1], size[0], size[1])


def set_win_pos(element, x, y):
    value = AXValueCreate(kAXValueCGPointType, NSMakePoint(x, y))
    return AXUIElementSetAttributeValue(element, "AXPosition", value) == 0


def win_title(element):
    return _ax_copy(element, "AXTitle") or ""


def is_real_window(element):
    if _ax_copy(element, "AXSubrole") not in ("AXStandardWindow", "AXDialog"):
        return False
    if _ax_copy(element, "AXMinimized"):
        return False
    size = ax_size(element)
    return bool(size and size[0] >= MIN_WIN_W and size[1] >= MIN_WIN_H)


def short_title(title):
    """Claude Code terminal titles read
        <folder> — <spinner> <task> — <proc> ◂ <full command> — <cols>x<rows>
    which is mostly noise in a list. Keep the folder and the task, drop the rest.
    Titles that do not match that shape (an editor filename) are left alone."""
    parts = [p.strip() for p in title.split(" — ")]
    if len(parts) < 2:
        return title
    task = parts[1].lstrip("✳✻✽✢✶✷◐◑◒◓·•* ").strip()
    if not task:
        return title
    folder = parts[0].strip()
    return f"{folder} · {task}" if folder else task


_SCRIPTS: dict[str, NSAppleScript] = {}


def _script_error(error):
    """The message out of an NSAppleScript error dictionary."""
    return error.get("NSAppleScriptErrorMessage", error)


def _sysevents_activate(app_name):
    """Only System Events can bring an app forward for us: macOS ignores activation
    requests from a process that is not itself active, and tunnel vision is backgrounded
    whenever another app has focus. Measured — activateWithOptions_ and a direct
    AXFrontmost write both report success and do nothing.

    Compiled scripts are cached per app: compiling costs ~26ms, running one ~4ms, and
    the cost is per distinct source text, so a generic warmup does not cover it."""
    script = _SCRIPTS.get(app_name)
    if script is None:
        script = NSAppleScript.alloc().initWithSource_(
            f'tell application "System Events" to set frontmost of process '
            f'"{app_name}" to true')
        compiled, error = script.compileAndReturnError_(None)
        if not compiled:
            print(f"could not compile the activation script for {app_name!r}: "
                  f"{_script_error(error)}", flush=True)
            return
        _SCRIPTS[app_name] = script
    _, error = script.executeAndReturnError_(None)
    if error is not None:
        # Cross-app cycling needs the System Events automation grant, which is a
        # SEPARATE permission from Accessibility. Without a message here the cycle
        # silently does nothing and there is no way to tell why.
        print(f"could not activate {app_name!r}: {_script_error(error)}", flush=True)


def warm_sysevents():
    """First AppleScript call costs ~100ms to compile and connect; every one after is
    ~4ms. Pay it at startup so the first cycle is not the slow one."""
    NSAppleScript.alloc().initWithSource_(
        'tell application "System Events" to return 1').executeAndReturnError_(None)


def running_apps(names):
    out = []
    for app in NSWorkspace.sharedWorkspace().runningApplications():
        name = app.localizedName()
        if name in names:
            out.append((name, app.processIdentifier()))
    return out


def pool_for(names):
    """Windows of the named apps, in a deterministic order that does NOT depend
    on z-order: sorted by app name, then by the window's position on screen."""
    entries = []
    for app_name, pid in running_apps(names):
        windows = _ax_copy(ax_app(pid), "AXWindows") or []
        for element in windows:
            if not is_real_window(element):
                continue
            frame = win_frame(element)
            if frame is None:
                continue
            entries.append({
                "app": app_name, "pid": pid, "el": element,
                "title": win_title(element), "frame": frame,
            })
    entries.sort(key=lambda e: (e["app"], round(e["frame"][0]), round(e["frame"][1])))
    return entries


# ------------------------------------------------------------------- scrim view

class ScrimView(NSView):
    def initWithFrame_(self, frame):
        self = objc.super(ScrimView, self).initWithFrame_(frame)
        if self is None:
            return None
        self._image = None
        self._hole = None
        self._rows = []          # [(rect, label)] drawn picker rows
        self._card = None        # opaque backing card behind the rows
        self._on_pick = None     # callback(index) when a row is clicked
        return self

    @objc.python_method
    def set_rows(self, rows, card, on_pick):
        self._rows = rows
        self._card = card
        self._on_pick = on_pick
        self.setNeedsDisplay_(True)

    def mouseDown_(self, event):
        """Only reachable while the picker is open — the panel ignores mouse
        events otherwise, so outside the picker the scrim stays purely optical."""
        point = self.convertPoint_fromView_(event.locationInWindow(), None)
        for index, (rect, _label) in enumerate(self._rows):
            if NSPointInRect(point, rect):
                self._on_pick(index)
                return

    @objc.python_method
    def set_image(self, image):
        self._image = image
        self.setNeedsDisplay_(True)

    @objc.python_method
    def set_hole(self, rect):
        if rect != self._hole:
            self._hole = rect
            self.setNeedsDisplay_(True)

    def isOpaque(self):
        return False

    def drawRect_(self, dirty):
        bounds = self.bounds()
        if DEBUG:
            print(f"[draw] bounds={bounds} image={self._image} hole={self._hole}", flush=True)
        if self._image is None:
            return

        path = NSBezierPath.bezierPath()
        path.appendBezierPathWithRect_(bounds)
        if self._hole is not None:
            path.appendBezierPathWithRoundedRect_xRadius_yRadius_(
                self._hole, HOLE_RADIUS, HOLE_RADIUS
            )
            path.setWindingRule_(NSEvenOddWindingRule)

        gc = NSGraphicsContext.currentContext()
        gc.saveGraphicsState()
        path.addClip()
        self._image.drawInRect_fromRect_operation_fraction_(
            bounds, ((0, 0), (0, 0)), NSCompositingOperationSourceOver, 1.0
        )
        gc.restoreGraphicsState()

        if self._hole is not None:
            edge = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                self._hole, HOLE_RADIUS, HOLE_RADIUS
            )
            edge.setLineWidth_(1.5)
            NSColor.colorWithCalibratedWhite_alpha_(1.0, 0.35).set()
            edge.stroke()

        if self._card is not None:
            # Fully opaque, and drawn AFTER the hole is punched — otherwise the
            # window showing through the hole bleeds into the list text.
            card = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                self._card, 14.0, 14.0
            )
            NSColor.colorWithCalibratedWhite_alpha_(0.11, 1.0).setFill()
            card.fill()
            NSColor.colorWithCalibratedWhite_alpha_(1.0, 0.22).setStroke()
            card.setLineWidth_(1.0)
            card.stroke()

        for index, (rect, label) in enumerate(self._rows):
            if index % 2:
                NSColor.colorWithCalibratedWhite_alpha_(1.0, 0.05).setFill()
                NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                    rect, 6.0, 6.0
                ).fill()
            NSString.stringWithString_(label).drawAtPoint_withAttributes_(
                (rect.origin.x + 14, rect.origin.y + 8),
                {
                    "NSFont": NSFont.monospacedSystemFontOfSize_weight_(14.0, 0.0),
                    "NSColor": NSColor.colorWithCalibratedWhite_alpha_(1.0, 0.95),
                },
            )


# -------------------------------------------------------------------- controller

class TunnelVision(NSObject):
    def initWithWallpapers_(self, paths):
        self = objc.super(TunnelVision, self).init()
        if self is None:
            return None

        self.screen = NSScreen.mainScreen()
        self.screen_frame = self.screen.frame()
        self.visible = self.screen.visibleFrame()
        self.screen_h = self.screen_frame.size.height

        self.images = [self._load(path) for path in paths]
        self.wp_index = 0

        self.moved = []          # [{el, pid, title, x, y}] original AX positions
        self.pools = {"term": [], "edit": []}
        self.index = {"term": -1, "edit": -1}
        self.own_pid = os.getpid()
        self.scrim_on = True
        self.picker = []         # [entry] rows offered while the picker is open
        self.sidecar_dirty = False
        self.busy_until = 0.0    # suppresses the leave-tunnel-vision check during a handover
        self.last_entry = None       # window framed right now; cleared when you leave
        self.remembered_entry = None # what to re-frame on your next entry; survives leaving
        self.timer = None        # _start_timer sets it; the delegate is live before then

        self._build_panel()
        self._build_menu()
        warm_sysevents()
        self._install_tap()
        self._start_timer()

        # Test/safety valve: a hard deadline after which the scrim tears itself
        # down and restores anything it moved, even if nothing else works.
        limit = float(os.environ.get("TUNNEL_VISION_SECONDS", "0") or 0)
        if limit > 0:
            NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                limit, self, "deadline:", None, False
            )
        return self

    def deadline_(self, _timer):
        print("deadline reached — exiting", flush=True)
        self.quit()


    # ---------------------------------------------------------------- setup

    @objc.python_method
    def _load(self, path):
        """Decode once, downsampled to the screen, so drawRect_ never rescales a
        multi-megapixel source on every frame."""
        source = NSImage.alloc().initWithContentsOfFile_(path)
        if source is None:
            sys.exit(f"could not decode wallpaper: {path}")
        w, h = self.screen_frame.size.width, self.screen_frame.size.height
        scaled = NSImage.alloc().initWithSize_((w, h))
        src_size = source.size()
        scale = max(w / src_size.width, h / src_size.height)
        draw_w, draw_h = src_size.width * scale, src_size.height * scale
        scaled.lockFocus()
        source.drawInRect_fromRect_operation_fraction_(
            NSMakeRect((w - draw_w) / 2, (h - draw_h) / 2, draw_w, draw_h),
            ((0, 0), (0, 0)), NSCompositingOperationSourceOver, 1.0,
        )
        scaled.unlockFocus()
        return scaled

    @objc.python_method
    def _build_panel(self):
        style = NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel
        panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            self.screen_frame, style, NSBackingStoreBuffered, False
        )
        panel.setLevel_(NSFloatingWindowLevel)
        panel.setOpaque_(False)
        panel.setBackgroundColor_(NSColor.clearColor())
        panel.setHasShadow_(False)
        panel.setIgnoresMouseEvents_(True)
        panel.setBecomesKeyOnlyIfNeeded_(True)
        # Visibility is managed in tick_, not by hidesOnDeactivate: the art must
        # survive clicking INTO the window it is framing, and disappear only when
        # you switch to an app outside the cycle.
        panel.setHidesOnDeactivate_(False)
        # Transient, NOT Stationary: Stationary means "unaffected by Expose", which
        # left the scrim covering the desktop during a Show Desktop hot corner.
        # Transient floats across Spaces but gets out of the way for Expose.
        panel.setCollectionBehavior_(
            NSWindowCollectionBehaviorCanJoinAllSpaces
            | NSWindowCollectionBehaviorTransient
            | NSWindowCollectionBehaviorIgnoresCycle
            | NSWindowCollectionBehaviorFullScreenNone
        )
        view = ScrimView.alloc().initWithFrame_(
            NSMakeRect(0, 0, self.screen_frame.size.width, self.screen_frame.size.height)
        )
        view.set_image(self.images[self.wp_index])
        panel.setContentView_(view)
        panel.orderFrontRegardless()
        if DEBUG:
            print(f"[panel] images={self.images}", flush=True)
            print(f"[panel] visible={panel.isVisible()} level={panel.level()} "
                  f"frame={panel.frame()} view={view.frame()}", flush=True)
        self.panel = panel
        self.view = view

    @objc.python_method
    def _build_menu(self):
        app = NSApplication.sharedApplication()
        bar = NSMenu.alloc().init()
        holder = NSMenuItem.alloc().init()
        bar.addItem_(holder)
        menu = NSMenu.alloc().init()
        menu.addItem_(NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Quit Terminal Tunnel Vision", "exitTunnelVision:", "q"))
        menu.itemAtIndex_(0).setTarget_(self)
        holder.setSubmenu_(menu)
        app.setMainMenu_(bar)
        # Restore on ANY route out — Cmd-Q, the Dock, terminate: from anywhere.
        app.setDelegate_(self)

    def applicationWillTerminate_(self, _notification):
        self._stop_timer()
        self.restore_all()


    @objc.python_method
    def _install_tap(self):
        mask = CGEventMaskBit(kCGEventKeyDown)
        tap = CGEventTapCreate(
            kCGSessionEventTap, kCGHeadInsertEventTap, kCGEventTapOptionDefault,
            mask, _tap_callback, None,
        )
        if tap is None:
            print("FATAL: could not create the event tap (Accessibility permission?)")
            sys.exit(1)
        source = CFMachPortCreateRunLoopSource(None, tap, 0)
        CFRunLoopAddSource(CFRunLoopGetCurrent(), source, kCFRunLoopCommonModes)
        CGEventTapEnable(tap, True)
        self.tap = tap

    @objc.python_method
    def _start_timer(self):
        self.timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            1.0 / 15.0, self, "tick:", None, True
        )
        NSRunLoop.currentRunLoop().addTimer_forMode_(self.timer, kCFRunLoopCommonModes)

    @objc.python_method
    def _stop_timer(self):
        """NSTimer retains its target, so the repeating tick and this controller hold
        each other alive for the life of the run loop. Invalidating on the way out
        breaks that cycle and, more visibly, stops tick_ firing back into quit()
        while terminate: is still unwinding."""
        if self.timer is not None:
            self.timer.invalidate()
            self.timer = None

    # ------------------------------------------------------------ hole tracking

    @objc.python_method
    def front_window(self):
        app = NSWorkspace.sharedWorkspace().frontmostApplication()
        if app is None or app.processIdentifier() == self.own_pid:
            return None
        element = ax_app(app.processIdentifier())
        for attribute in ("AXFocusedWindow", "AXMainWindow"):
            window = _ax_copy(element, attribute)
            if window is not None:
                return window
        windows = _ax_copy(element, "AXWindows") or []
        return windows[0] if windows else None

    def tick_(self, _timer):
        # A signal handler cannot run Python while the Cocoa run loop is idle, so
        # SIGTERM and the quit sentinel are picked up here instead. This is what
        # makes an external stop restore your windows rather than strand them.
        quit_flag = os.path.exists(QUIT_FLAG)
        if _TERMINATE[0] or quit_flag:
            if quit_flag:
                os.remove(QUIT_FLAG)
            self.quit()
            return
        if self.sidecar_dirty:
            self.sidecar_dirty = False
            self.write_sidecar()
        app = NSWorkspace.sharedWorkspace().frontmostApplication()
        pid = app.processIdentifier() if app is not None else None

        # Ignore the frontmost app entirely while we are mid-handover: cycling has
        # to activate the target app for a moment to win the z-order, and treating
        # that as "you left" would restore the window out from under the
        # hole we just placed.
        if time.time() < self.busy_until:
            return

        # Tunnel vision itself is frontmost (launch, Cmd+Tab, a Dock click): show the
        # art and hand off to the window that belongs in the hole, so it is ready
        # to type into without a click. Unconditional, because the app switcher's
        # own activation can land after our hand-off and put tunnel vision back in
        # front; the next tick then simply hands off again.
        if pid == self.own_pid:
            self.show_scrim(True)
            self.reenter()
            return

        # The app owning the window we are framing is frontmost — you clicked into
        # the framed window to work in it. The art MUST stay: hiding here is what
        # made every click knock tunnel vision into the background. No reenter() on
        # this path either, since that would steal focus straight back off you.
        if self.last_entry is not None and pid == self.last_entry["pid"]:
            self.show_scrim(True)
            window = self.front_window()
            frame = win_frame(window) if window is not None else None
            if frame is not None:
                self.set_hole_from_ax(frame)
            return

        # Anything else (Chrome, Finder, an app with nothing framed): you have left.
        if self.scrim_on:
            self.restore_all()
            self.show_scrim(False)

    @objc.python_method
    def show_scrim(self, on):
        if on == self.scrim_on:
            return
        self.scrim_on = on
        if on:
            self.panel.orderFrontRegardless()
        else:
            self.close_picker()
            self.last_entry = None
            self.view.set_hole(None)
            self.panel.orderOut_(None)

    @objc.python_method
    def reenter(self):
        """Put a window back in the hole on entering tunnel vision: the one you were
        last on, or the first terminal the very first time."""
        entry = self.remembered_entry
        if entry is not None and win_frame(entry["el"]) is None:
            entry = None                       # that window has since closed
        if entry is None:
            pool = self.refresh_pool("term") or self.refresh_pool("edit")
            if not pool:
                return
            entry = pool[0]
            self.index["term"] = 0
        self.focus(entry)

    @objc.python_method
    def center_and_frame(self, element, pid, title, frame):
        """Remember where the window was, centre it, and move the hole onto the
        centred frame. The title is a parameter rather than a live win_title read:
        focus() must record the title its pool was built with, since a Claude Code
        terminal animates a spinner in its title and the sidecar matches on it.
        Returns (centred_frame, set_pos_ok)."""
        self.remember(element, pid, title, frame)
        x, y = self.centered_origin(frame[2], frame[3])
        ok = set_win_pos(element, x, y)
        centred = (x, y, frame[2], frame[3])
        self.set_hole_from_ax(centred)
        return centred, ok

    @objc.python_method
    def set_hole_from_ax(self, frame):
        x, y, w, h = frame
        self.view.set_hole(NSMakeRect(x, self.screen_h - y - h, w, h))

    # ---------------------------------------------------------------- actions

    @objc.python_method
    def refresh_pool(self, kind):
        names = TERMINAL_APPS if kind == "term" else EDITOR_APPS
        fresh = pool_for(names)
        old = self.pools[kind]

        # Order must be FROZEN once established, or cycling back does not retrace
        # your steps. Two things would otherwise reshuffle it every keypress:
        # pool_for sorts by current position and we move windows to the centre, and
        # a title-based identity breaks the moment a title changes (Claude Code
        # animates a spinner in it). So: match on the AX element, which is stable
        # for the window's lifetime, keep known windows in their existing order,
        # and append only genuinely new ones at the end.
        by_el = {e["el"]: e for e in fresh}
        merged, taken = [], set()
        for entry in old:
            match = by_el.get(entry["el"])
            if match is not None:
                merged.append(match)
                taken.add(entry["el"])
        merged.extend(e for e in fresh if e["el"] not in taken)
        self.pools[kind] = merged
        return merged

    @objc.python_method
    def position_in(self, pool, entry):
        if entry is None:
            return None
        return next((i for i, e in enumerate(pool) if e["el"] == entry["el"]), None)

    @objc.python_method
    def cycle(self, kind, direction):
        t0 = time.perf_counter()
        pool = self.refresh_pool(kind)
        if not pool:
            return
        here = self.position_in(pool, self.last_entry)
        if here is None:
            # You are in the other pool, so this press is a switch, not a step:
            # go back to the window you were last on here. Only a repeat press
            # moves. Otherwise alternating terminal/editor would walk both pools
            # and you would have to hunt for the window you just left.
            index = self.index[kind] if 0 <= self.index[kind] < len(pool) else 0
        else:
            index = (here + direction) % len(pool)
        self.focus(pool[index])
        if DEBUG:
            print(f"[perf] cycle {kind} took {(time.perf_counter()-t0)*1000:.2f} ms", flush=True)

    @objc.python_method
    def focus(self, entry):
        element, pid = entry["el"], entry["pid"]
        frame = win_frame(element)
        if frame is None:
            return
        # BOTH steps are needed: AXRaise only reorders the window within its own
        # app's layer, and the active app's windows always sit on top, so the
        # owning app has to be brought forward too. Activation fires only when the
        # target is in a DIFFERENT app; cycling among terminals is pure AXRaise.
        # Both run synchronously on the main thread from the key tap, so tick_
        # cannot interleave. Activation is deliberately never handed back — the
        # target stays active so you can type into it straight away.
        self.last_entry = entry
        self.remembered_entry = entry
        # Remember where this window sits in its pool no matter how it was chosen,
        # so switching pools and coming back lands on it rather than on whatever
        # the last arrow press happened to leave behind.
        for pool_kind, pool in self.pools.items():
            position = self.position_in(pool, entry)
            if position is not None:
                self.index[pool_kind] = position
        front = NSWorkspace.sharedWorkspace().frontmostApplication()
        if front is None or front.processIdentifier() != pid:
            _sysevents_activate(entry["app"])
            # The OS takes a few ms to actually change frontmost. Hold off the
            # leave-tunnel-vision check until it lands, or tick_ sees the OLD app still
            # in front, mismatches the new selection and restores the window.
            self.busy_until = time.time() + 0.5
        AXUIElementPerformAction(element, "AXRaise")
        # AXRaise orders the window front; these make it the app's main and
        # keyboard-focused window too, so typing lands in the framed window and
        # not in whichever of the app's windows was key before.
        AXUIElementSetAttributeValue(element, "AXMain", True)
        AXUIElementSetAttributeValue(element, "AXFocused", True)

        frame, ok = self.center_and_frame(element, pid, entry["title"], frame)
        if DEBUG:
            print(f"[focus] {short_title(entry['title'])[:40]!r} set_pos_ok={ok} "
                  f"want={frame} actual={win_frame(element)}", flush=True)

    @objc.python_method
    def centered_origin(self, w, h):
        vx = self.visible.origin.x
        vw = self.visible.size.width
        vh = self.visible.size.height
        ax_top = self.screen_h - (self.visible.origin.y + vh)
        x = vx + max(0, (vw - w) / 2)
        y = ax_top + max(0, (vh - h) / 2)
        return int(x), int(y)

    @objc.python_method
    def remember(self, element, pid, title, frame):
        for record in self.moved:
            if record["el"] == element:
                return
        self.moved.append(
            {"el": element, "pid": pid, "title": title, "x": frame[0], "y": frame[1]}
        )
        self.sidecar_dirty = True

    @objc.python_method
    def write_sidecar(self):
        payload = [
            {"pid": r["pid"], "title": r["title"], "x": r["x"], "y": r["y"]}
            for r in self.moved
        ]
        try:
            os.makedirs(os.path.dirname(SIDECAR), exist_ok=True)
            tmp = SIDECAR + ".tmp"
            with open(tmp, "w") as fh:
                json.dump({"written": time.time(), "windows": payload}, fh, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, SIDECAR)
        except OSError as exc:
            print(f"sidecar write failed: {exc}")

    @objc.python_method
    def restore_all(self):
        for record in self.moved:
            set_win_pos(record["el"], record["x"], record["y"])
        self.moved = []
        if os.path.exists(SIDECAR):
            os.remove(SIDECAR)

    # ----------------------------------------------------------------- picker

    @objc.python_method
    def toggle_picker(self):
        if self.picker:
            self.close_picker()
            return
        entries = self.refresh_pool("term") + self.refresh_pool("edit")
        if not entries:
            return
        row_h, gap, pad = 32.0, 2.0, 18.0
        width = 660.0
        total = len(entries) * (row_h + gap) - gap
        top = (self.screen_frame.size.height + total) / 2
        left = (self.screen_frame.size.width - width) / 2
        rows = []
        for i, entry in enumerate(entries):
            rect = NSMakeRect(left, top - (i + 1) * row_h - i * gap, width, row_h)
            key = str(i + 1) if i < 9 else ("0" if i == 9 else " ")
            rows.append((rect, f" {key}   {short_title(entry['title'])[:58]}"))
        card = NSMakeRect(left - pad, top - total - pad, width + 2 * pad, total + 2 * pad)
        self.picker = entries
        self.panel.setIgnoresMouseEvents_(False)
        self.view.set_rows(rows, card, self.pick_index)

    @objc.python_method
    def close_picker(self):
        self.picker = []
        self.panel.setIgnoresMouseEvents_(True)
        self.view.set_rows([], None, None)

    @objc.python_method
    def pick_index(self, index):
        entries = self.picker
        self.close_picker()
        if 0 <= index < len(entries):
            self.focus(entries[index])

    @objc.python_method
    def next_wallpaper(self):
        self.wp_index = (self.wp_index + 1) % len(self.images)
        self.view.set_image(self.images[self.wp_index])

    @objc.python_method
    def center_front(self):
        window = self.front_window()
        if window is None:
            return
        app = NSWorkspace.sharedWorkspace().frontmostApplication()
        frame = win_frame(window)
        if frame is None:
            return
        self.center_and_frame(window, app.processIdentifier(), win_title(window), frame)

    def exitTunnelVision_(self, _sender):
        self.quit()

    @objc.python_method
    def quit(self):
        self._stop_timer()
        self.restore_all()
        self.panel.orderOut_(None)
        NSApplication.sharedApplication().terminate_(None)


# --------------------------------------------------------------------- event tap

CONTROLLER = None


def _tap_callback(proxy, event_type, event, refcon):
    if event_type in (kCGEventTapDisabledByTimeout, kCGEventTapDisabledByUserInput):
        if CONTROLLER is not None:
            CGEventTapEnable(CONTROLLER.tap, True)
        return event
    if event_type != kCGEventKeyDown or CONTROLLER is None:
        return event

    flags = CGEventGetFlags(event)
    code = CGEventGetIntegerValueField(event, kCGKeyboardEventKeycode)
    if DEBUG:
        print(f"[key] code={code} flags=0x{flags:x} ralt={bool(flags & DEV_RIGHT_ALT)} "
              f"ctrl={bool(flags & FLAG_CTRL)} alt={bool(flags & FLAG_ALT)}", flush=True)

    if flags & FLAG_CMD or flags & FLAG_SHIFT:
        return event

    # An open picker is modal to the keyboard: a bare number picks that row and
    # bare Escape closes it, with or without Option still held. The panel is
    # non-activating so your window keeps keyboard focus, which means consuming
    # these here is the only way they can reach the picker at all.
    if CONTROLLER.picker:
        if code in NUMBER_KEYS:
            CONTROLLER.pick_index(NUMBER_KEYS[code])
            return None
        if code == KEY_ESC:
            CONTROLLER.close_picker()
            return None
    # Right Option only (the two Option keys carry distinct device bits), so
    # consuming the event leaves left Option free for word-jump; ctrl-opt too.
    right_alt = bool(flags & FLAG_ALT and flags & DEV_RIGHT_ALT)
    chord = bool(flags & FLAG_CTRL and flags & FLAG_ALT)
    if not (right_alt or chord):
        return event

    if code == KEY_SPACE:
        CONTROLLER.toggle_picker()
    elif code == KEY_LEFT:
        CONTROLLER.cycle("term", -1)
    elif code == KEY_RIGHT:
        CONTROLLER.cycle("term", 1)
    elif code == KEY_UP:
        CONTROLLER.cycle("edit", -1)
    elif code == KEY_DOWN:
        CONTROLLER.cycle("edit", 1)
    elif code == KEY_BACKSLASH:
        CONTROLLER.next_wallpaper()
    elif code == KEY_C:
        CONTROLLER.center_front()
    elif code == KEY_ESC:
        CONTROLLER.quit()
    else:
        return event
    return None


# --------------------------------------------------------------------- entry

def _alert(title, body):
    alert = NSAlert.alloc().init()
    alert.setMessageText_(title)
    alert.setInformativeText_(body)
    alert.runModal()


def cmd_list():
    for kind, names in (("terminals", TERMINAL_APPS), ("editors", EDITOR_APPS)):
        print(f"== {kind}")
        for i, entry in enumerate(pool_for(names)):
            x, y, w, h = entry["frame"]
            print(f"  [{i}] {entry['app']:<10} {int(x):>5},{int(y):<5} "
                  f"{int(w)}x{int(h)}  {entry['title'][:60]}")


def cmd_setup():
    """Write config.json by asking which of the running apps are your terminals and
    your editors. Everyone's editor is different, so the built-in defaults are only
    a guess; this is how you replace them without hand-editing JSON."""
    names = sorted({
        app.localizedName()
        for app in NSWorkspace.sharedWorkspace().runningApplications()
        if app.activationPolicy() == NSApplicationActivationPolicyRegular
        and app.localizedName()
    })
    if not names:
        sys.exit("no running apps to choose from — open your terminal and editor first")

    print("Open the apps you want in the cycle before running this. Running now:\n")
    for i, name in enumerate(names, 1):
        print(f"  {i:>2}  {name}")
    print("\nAnswer with numbers or names, separated by spaces or commas.")
    print("Press Return to accept the suggestion in brackets.")

    def ask(label, suggested):
        raw = input(f"\n{label} [{', '.join(suggested)}]: ").strip()
        if not raw:
            return list(suggested)
        chosen = []
        for token in raw.replace(",", " ").split():
            if token.isdigit() and 1 <= int(token) <= len(names):
                chosen.append(names[int(token) - 1])
            else:
                chosen.append(token)
        return chosen

    def suggest(defaults):
        running = [n for n in names if n in defaults]
        return running or list(defaults)

    terminals = ask("Terminal apps", suggest(DEFAULT_TERMINALS))
    editors = ask("Editor apps", suggest(DEFAULT_EDITORS))

    existing = {}
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH) as fh:
            existing = json.load(fh)
    config = {
        "wallpapers": existing.get("wallpapers", []),
        "terminals": terminals,
        "editors": editors,
    }
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(CONFIG_PATH, "w") as fh:
        json.dump(config, fh, indent=2)
        fh.write("\n")
    print(f"\nwrote {CONFIG_PATH}")
    print(f"  terminals: {', '.join(terminals)}")
    print(f"  editors:   {', '.join(editors)}")
    if not config["wallpapers"]:
        print(f"\nNo wallpapers listed yet: add paths to that file, drop images in "
              f"{DEFAULT_WALLPAPER_DIR}, or pass one on the command line.")
    print("Check it with: tunnel.py --list")


def cmd_restore():
    if not os.path.exists(SIDECAR):
        print("no sidecar to restore")
        return
    with open(SIDECAR) as fh:
        data = json.load(fh)
    restored = 0
    for record in data.get("windows", []):
        windows = _ax_copy(ax_app(record["pid"]), "AXWindows") or []
        for element in windows:
            if win_title(element) == record["title"]:
                if set_win_pos(element, record["x"], record["y"]):
                    restored += 1
                break
    print(f"restored {restored} of {len(data.get('windows', []))} windows")
    os.remove(SIDECAR)


def wallpaper_list(args):
    """Images on the command line replace the config list; --start PATH keeps the
    config list but rotates it so PATH is shown first."""
    if args[:1] == ["--start"]:
        if len(args) != 2:
            sys.exit("usage: tunnel.py --start PATH")
        start = os.path.abspath(args[1])
        rest = [p for p in CONFIG["wallpapers"] if os.path.abspath(p) != start]
        paths = [start] + rest
    else:
        paths = [os.path.abspath(a) for a in args] or CONFIG["wallpapers"]
    if not paths:
        sys.exit(f"no wallpapers: pass image paths, or list them in {CONFIG_PATH}")
    missing = [p for p in paths if not os.path.exists(p)]
    if missing:
        sys.exit("wallpaper not found: " + ", ".join(missing))
    return paths


def main():
    global CONTROLLER
    args = sys.argv[1:]
    if args[:1] == ["--setup"]:
        return cmd_setup()
    if args[:1] == ["--list"]:
        return cmd_list()
    if args[:1] == ["--restore"]:
        return cmd_restore()
    if args[:1] == ["--quit"]:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        open(QUIT_FLAG, "w").close()
        print("quit requested — the running instance will restore and exit")
        return
    wallpapers = wallpaper_list(args)

    signal.signal(signal.SIGTERM, lambda *_: _TERMINATE.__setitem__(0, True))
    signal.signal(signal.SIGINT, lambda *_: _TERMINATE.__setitem__(0, True))

    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyRegular)

    # Launched from a .app bundle, this process is its own responsible process:
    # it needs its own Accessibility grant, which Terminal-launched runs inherit.
    if not AXIsProcessTrustedWithOptions({"AXTrustedCheckOptionPrompt": True}):
        _alert(
            "Terminal Tunnel Vision needs Accessibility access",
            "Open System Settings > Privacy & Security > Accessibility and switch "
            "on Terminal Tunnel Vision (or the wallpaper app you double-clicked), then launch "
            "it again.\n\nWithout it the scrim cannot see which window is in front "
            "or move windows to the centre.",
        )
        return

    CONTROLLER = TunnelVision.alloc().initWithWallpapers_(wallpapers)
    app.run()


if __name__ == "__main__":
    main()
