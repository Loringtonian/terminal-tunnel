---
depth: 3
model: Fable 5.1
---
# TERMINAL TUNNEL VISION - TODOs
*Full-screen art with a hole around the window you are working in*
*Last Updated: 2026-09-11 (both items implemented same day, awaiting Lorin's live check)*

**Project Folder:** `Second_Brain/Projects/Terminal_Tunnel_Vision/` (own git repo, gitignored from the brain)
**Code:** `tunnel.py` · hotkey dispatch at `_tap_callback` (~line 1032) · focus/leave logic in `tick_` (~line 689)

---

## Change list (Lorin, dictated 2026-09-11)

- [ ] **Cmd+Tab back into the tunnel should land in the tunnel, not in the bare terminal** — *"When I alt tab away from the terminal tunnel and then I try to alt tab back it actually takes me to the terminals not to the terminal tunnels so there's something about the way that attention is being held and If we can reorder that so that it's an intuitive Step back in terms of alt tabbing that would be great"* (Source: chat dictation 2026-09-11)
  - **Root cause (agent read of `tunnel.py`, 2026-09-11):** while the art is up, the framed window's app (Terminal) is the frontmost app, not Tunnel Vision — the panel is deliberately non-activating. So macOS's Cmd+Tab most-recent order records *Terminal* as the app he left, and Cmd+Tab-back returns to Terminal directly. `tick_` only re-shows the art when Tunnel Vision *itself* becomes frontmost (`pid == self.own_pid` → `reenter()`); landing back on Terminal takes the "you clicked into the framed window" path only if `last_entry` is still set, but `show_scrim(False)` on leaving clears `last_entry`, so the return is treated as a plain Terminal focus and the art stays down.
  - **Candidate fixes (his call):** (a) when the frontmost app returns to a pool app and `remembered_entry` is still alive, re-enter automatically — Cmd+Tab back to Terminal brings the art with it (no MRU games; Tunnel Vision never needs to be in the switcher for the round-trip); (b) on leaving, briefly activate Tunnel Vision before the other app takes over, so Tunnel Vision sits in the MRU slot and Cmd+Tab-back hits it (fragile: fights the switcher's own activation, and the README already notes that race); (c) both. Recommendation: (a). **Implemented 2026-09-11 (Fable 5.1):** `tick_` now re-enters when the art is down and the remembered window's app comes back in front; `reenter(prefer_front=True)` frames a different pool window if that is the one in front. Static-verified only (compiles; tap dispatch unit-checked) — the Cmd+Tab round trip itself is his to feel out.

- [x] **Right ⌥ + cycles the wallpaper of the framed window** — DONE, Lorin tested 2026-09-11: *"it works nicely!"*; pushed to GitHub same day. — *"We need a mechanic for switching the wallpaper of a certain Tab like if I press option and then the plus key That should cycle through Backgrounds and change the background of a given terminal Sometimes certain backgrounds are More appropriate to the activities inside a terminal than others"* (Source: chat dictation 2026-09-11)
  - **Already exists, on a different key:** right ⌥ \ does exactly this per *window* (`next_wallpaper`, `tunnel.py` ~line 995: re-assigns the framed window's wallpaper and it sticks for the session). The change is a binding: add the `=`/`+` key (keycode 24, `kVK_ANSI_Equal`) as an alias for next wallpaper; consider `-` (keycode 27) for previous. **Implemented 2026-09-11 (Fable 5.1):** right ⌥ + = next, right ⌥ - = previous, ⌥ \ still steps forward; verified by driving `_tap_callback` with synthetic right-Option events (left Option still passes through).
  - **Open question for Lorin:** "a certain Tab" — the app frames *windows* via the Accessibility API, and Terminal.app tabs share one window, so a wallpaper is per Terminal window, not per tab. Per-tab would need reading the selected tab's title and keying the assignment on it. Which did he mean?

---

*Filed by Claude Fable 5.1.*
