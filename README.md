# Focus Mode

A full-screen piece of art with a hole cut around the one window you are working in.

Your real desktop is untouched. Focus Mode is a floating panel that sits above every other window, ignores the mouse entirely, and follows whatever window is frontmost. Summon it, work inside the hole, and everything else on the screen is art. Cycle between your terminal windows and your editor windows with one hand, and every window you cycle to is centred for you and put back where it was when you leave.

## Hotkeys

Hold the **right Option key** (ctrl+option works too). They are global, so they work while you are typing in the focused window. The left Option key is left alone, so word-jump in your shell and editor keeps working.

| keys            | action                                                  |
|-----------------|---------------------------------------------------------|
| ⌥ ← / ⌥ →       | cycle terminal windows                                  |
| ⌥ ↑ / ⌥ ↓       | cycle text-editor windows                               |
| ⌥ Space         | window picker: click a row, or press its number         |
| ⌥ \             | next wallpaper                                          |
| ⌥ C             | centre the front window                                 |
| ⌥ Esc           | exit, putting every window it moved back where it was   |

Cycling centres the chosen window and cuts the hole around it. Launching Focus Mode, or Cmd+Tabbing to it, hands keyboard focus straight to the framed window, so you can type without clicking. Clicking into the framed window keeps the art up. Switching to any other app (a browser, Finder) drops the art and restores the windows; coming back brings it up again on the window you were last on. New windows are picked up on your next cycle and appear at the end of the order.

## Install

macOS only: it is built on AppKit, the Accessibility API and a CGEvent tap.

```
pip install -r requirements.txt
python3 focus_mode.py ~/Pictures/some_wallpaper.png
```

Grant Accessibility access when asked (System Settings > Privacy & Security > Accessibility). Without it Focus Mode cannot see which window is in front or move windows. Run from a terminal, it inherits the terminal app's grant; a `.app` bundle needs its own.

## Wallpapers and configuration

`~/.config/focus-mode/config.json`:

```json
{
  "wallpapers": [
    "~/Pictures/wallpapers/orbital.png",
    "~/Pictures/wallpapers/solarpunk.png"
  ],
  "terminals": ["Terminal"],
  "editors": ["TextEdit"]
}
```

- `wallpapers` are cycled with ⌥ \ in this order. Any size works; each is decoded once and scaled to the screen. If the key is missing, every image in a `wallpapers/` folder next to `focus_mode.py` is used.
- `terminals` and `editors` are app names as macOS reports them. Defaults: Terminal, iTerm2, Warp, Ghostty, Alacritty, kitty and TextEdit, CotEditor, BBEdit, Sublime Text, Code, Cursor, Zed. `python3 focus_mode.py --list` prints the windows currently in each pool, which is the quickest way to check a name.

Image paths on the command line replace the config list. `--start PATH` keeps the config list but begins on that image.

## A double-clickable app

```
python3 make_app.py "Focus Mode"                          # -> ~/Desktop/Focus Mode.app
python3 make_app.py "Orbital" --start ~/wp/orbital.png    # starts on that wallpaper, icon from it
```

The bundle wraps the Python interpreter you ran `make_app.py` with, takes its icon from the start wallpaper, and logs to `~/.config/focus-mode/launch.log`. It needs its own Accessibility grant the first time you open it.

## Other commands

```
python3 focus_mode.py --quit      # ask the running instance to restore its windows and exit
python3 focus_mode.py --restore   # after a crash: put windows back from the sidecar
python3 focus_mode.py --list      # show the terminal and editor window pools
```

Every window Focus Mode moves is written to `~/.config/focus-mode/restore.json` before the move, so a crash or a `kill` never strands your windows off-centre.

## How it works

- The scrim is a borderless, non-activating `NSPanel` at floating level with `ignoresMouseEvents` on, so it can never take a click, steal focus, or hide a window from you.
- A 15 Hz timer reads the frontmost window through the Accessibility API and redraws the hole.
- Hotkeys come from a session-level `CGEventTap`. macOS reports the two Option keys with different device bits; only right-Option chords are consumed.
- Cycling is `AXRaise` plus, when the target belongs to a different app, a System Events `set frontmost` (the one activation route that works from a process that is not itself active). A cycle measures around 12 ms.
- Transient collection behaviour keeps the panel on every Space but out of Mission Control and Show Desktop.

Main display only; other monitors are not covered.

## License

MIT. Built with Claude Code.
