# peer-hold — the machine holds the process, the front end draws the screen

sm and Fine draw coding agents' terminals. Wherever an agent runs, the life of its process is separate from the screen that shows it, so the machine needs no tmux: it holds the process and the current VT screen state, and the front end renders.

One `peer-hold` per agent does that:
- It starts the harness (claude, codex, or a wrapper) on a new PTY with no parent: its parent becomes the system (ppid 1). The agent lives on when whatever started it quits or the Mac's lid closes.
- It keeps reading PTY output while nobody watches, because a TUI that cannot write stalls. Recent output (`--ring`, 1 MB by default) is kept as a diagnostic log; the current screen is kept separately in a `vt100` state model.
- Once attached, live bytes pass through unchanged. A reconnect gets only the current screen, colours, cursor and input modes, never past output, terminal queries, bells or clipboard actions. The screen is restored in one synchronized-output batch (`CSI ?2026 h/l`); on the alternate screen, the primary screen it returns to is restored too.
- No layout, windows or key bindings: those are sm's.

## Commands
```
peer-hold start --sock PATH [--cwd DIR] [--env K=V]... [--cols C --rows R] [--ring BYTES] -- CMD...
peer-hold attach --sock PATH [--detach-key] [--passive]
peer-hold status --sock PATH
peer-hold tail --sock PATH [--lines N] [--raw]
peer-hold screen --file PATH [--cols C --rows R]
```
- `start` returns once the socket is up and prints `{"holder":PID,"child":PID,"sock":PATH}`. `PATH.json` holds cmd, cwd and start time; when CMD ends, its exit code is left in `PATH.exit` (the socket and .json are removed).
- CMD gets `PEER_HOLD_SOCK=PATH`. It does not inherit the starter's `FINE_TAB_ID`, `SM_PANE_ID` or `TMUX_PANE`: with them, peer would take the session for the starter's tab or pane. To bind it to an sm pane, pass `--env SM_PANE_ID=<UUID>` explicitly.
- `attach` attaches the current terminal. Closing the terminal detaches it; the agent keeps running. A reconnect at the same size leaves the PTY size and the agent alone (the old redraw nudge, one row smaller and back 80 ms later, is gone).
- `tail` is a diagnostic output log, not a screen restore. `--raw` includes past queries, so never replay it into a terminal.
- On exit, `.screen` is `PHSC` + version byte `1` + cols/rows (u16 BE) + a frozen ANSI snapshot. It includes the last PTY output and drops an unfinished escape or UTF-8 prefix. Do not print the file directly; read it with `peer-hold screen --file …`.
- `screen` extracts a snapshot in the new format and converts an old raw ring file into a screen as it reads it. Old files carry no size, so they are read at 120×40 unless the options say otherwise. The original file is kept. `sm screen` and the last screen of a plain `sm attach` use this path too.

## Socket messages (the same in stage 1 and stage 2)
A message is `[type u8][len u32 big-endian][payload]`.

| type | direction | payload |
|---|---|---|
| 1 HELLO | front→holder | cols u16, rows u16 (BE). Answered with one current-screen snapshot as OUTPUT. 0×0 is a passive viewer. The PTY is resized only when the real size differs |
| 2 INPUT | front→holder | bytes for the PTY (keys, pastes) |
| 3 RESIZE | front→holder | cols u16, rows u16 |
| 4 STATUS | front→holder | none |
| 5 TAIL | front→holder | none; asks for the diagnostic raw ring |
| 10 OUTPUT | holder→front | the first frame is the screen snapshot, then live PTY bytes as they come |
| 11 EXIT | holder→front | i32 exit code; the connection closes after it |
| 12 STATUS | holder→front | JSON: holder, child, cols, rows, viewers, active, seen, ring, started, cwd |
| 13 TAIL | holder→front | the diagnostic raw ring |

Several front ends can attach at once. The window size follows the active viewer that typed last (before any input, the active viewer that attached last). Passive viewers never change it. A front end more than 8 MB behind is dropped; a slow front end never makes the agent wait.

## How a front end attaches
- **Stage 1 (now):** run `peer-hold attach --sock …` in the pane's PTY; for another machine, `ssh -t HOST peer-hold attach --sock …`.
- **Stage 2 (sm directly):** one ssh connection per machine (`ssh HOST peer-hold mux`, not built yet) carrying a channel per pane with the messages above. Channel messages get a `chan u32` prefix, plus open and close control messages. sm opens only visible panes; the rest are seen through peer's state JSON. The format is settled here when mux is built.

## Tests

```sh
cargo test --manifest-path hold/Cargo.toml
cargo build --release --manifest-path hold/Cargo.toml
python3 -I hold/tests/test_hold.py hold/target/release/peer-hold
python3 -I hold/tests/measure_reconnect.py hold/target/release/peer-hold
sh tests/run.sh
```

`tests/run.sh` runs the session layer's tests with the peer-hold it finds in `hold/target/release` (or `$PEER_HOLD`). `measure_reconnect.py` uses `SM_TEST_LAUNCHER` and isolated sm and peer state; it starts no real model. Terminal replies come from a deterministic test responder. A separate app test with the real libghostty confirmed that replayed DA, DSR and OSC 11 queries used to come back as input; the new snapshot shows the current screen and puts 0 input bytes in 1.5 seconds.

Five reconnects at 120×40 on a fixture with about 1.4 MB of past redraws: the old holder sent about 1,048,500 bytes each time, took 81 ms to the last output and caused 10 SIGWINCH in all. The new one sends only the current screen, with no past queries and no extra SIGWINCH. Times are measured at the local socket, excluding the 150 ms quiet window and the renderer. Other work loaded the CPU, so bytes, signals and input pollution count for more than the times.

## Scope and limits

- A reconnect restores only the visible screen (and the primary screen under an alternate one), not the full scrollback. An attached viewer's scrollback and the `tail` log stay. Live output is not turned into diffs.
- The state model covers what `vt100` supports: text, SGR, cursor, alternate screen. Image protocols, OSC 8 links and other extended rendering are not restored in the snapshot (live output still carries them). If they need restoring, add them to the model or switch to a fuller VT engine.
- A width change cuts or pads rows in the model and does not reflow long lines. After a size change, the program's own redraw on the real SIGWINCH may follow. A same-size reconnect forces no redraw.
- When a PTY read ends inside an escape or UTF-8 sequence, a live reconnect continues the unfinished prefix. An unfinished control string over 4 KB is ignored by a new viewer up to its terminator. The frozen screen never includes the prefix.
- Several live viewers each answering a new query behaves as before; this change removes the cause that re-ran past queries on every reconnect.
- An old `.screen` has to be interpreted from the raw log once, so reading it costs in proportion to the file. Screens saved by a new holder cost in proportion to the screen, to read and to send.
- After a reboot no process is left. sm brings the same conversation back from the session id in peer's registry (`sm restore`, `sm resume NAME`).
