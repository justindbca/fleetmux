# ft-probe — behaviour spec (test fixture)

`ft-probe` is a small full-screen program that the fleetmux end-to-end tests
(`crates/app/tests/e2e_probe.rs`) run inside a pane. It turns on the terminal
modes real agent CLIs use, asks the terminal the questions Codex asks at
start-up, and writes every byte it receives back to a JSON-lines log, so a test
can drive the pane through the control protocol and check byte for byte what
the inner program saw.

Phase 0 ships this spec and a flag-parsing skeleton (`src/main.rs`, bodies
`todo!()`); the M0 `probe-e2e` package implements it. Dependencies: crossterm
(raw mode, terminal size) and std only.

## Command line

```text
ft-probe [--no-mouse] [--no-paste] [--kitty-query] [--app-cursor] [--exit-after <ms>]
         [--mouse-mode click|drag|motion] [--x10] [--flood <lines_per_s>] [--lines <n>] [--wide]
```

| Flag | Effect |
|---|---|
| `--no-mouse` | do not enable mouse reporting (no 1002 / 1006) |
| `--no-paste` | do not enable bracketed paste (no 2004) |
| `--kitty-query` | at start-up also send the kitty keyboard query `CSI ? u` and log whether it was answered |
| `--app-cursor` | enable application cursor keys (`CSI ? 1 h`, DECCKM) |
| `--exit-after <ms>` | exit by itself after `<ms>` milliseconds |
| `--mouse-mode click\|drag\|motion` | which mouse tracking mode to enable: `CSI ? 1000 h` / `CSI ? 1002 h` (default) / `CSI ? 1003 h`; ignored with `--no-mouse` |
| `--x10` | do not enable SGR mouse encoding (no 1006): reports arrive X10-encoded (`CSI M` + 3 bytes) |
| `--flood <lines_per_s>` | after start-up, print `<lines_per_s>` lines `FLOOD <k>` per second (k = 0, 1, …) until exit; used by `perf.rs` |
| `--lines <n>` | before start-up, print `n` lines `LINE <k>` (k = 0 … n−1) into scrollback and stay on the main screen (below); used by the wheel / page-mode tests |
| `--wide` | also draw row 13: `WIDE 漢字😀│end` (wide-character alignment) |
| `-h`, `--help` | print usage, exit 0 |

Anything else (unknown flag; missing / non-numeric `--exit-after`, `--lines` or
`--flood` value; `--flood 0`; a `--mouse-mode` other than `click` / `drag` /
`motion`) prints usage to stderr and exits 2. `Opts` defaults with no flags:
mouse on, paste on, `mouse_mode` drag, `sgr` true, `flood` none, `lines` 0,
`wide` false (`crates/probe/tests/args.rs`). `FT_PROBE_LOG` must name the log file; if it is
unset or cannot be opened for append, the probe prints why to stderr and exits 2
before touching the terminal.

## Start-up, in this order

1. Put the tty in raw mode (crossterm), read the size (`cols`, `rows`).
   With `--lines <n>`: print `LINE 0` … `LINE <n-1>`, each followed by
   `\r\n`, then `rows − 1` more `\r\n`, so every `LINE` row has scrolled off
   the top and the pane's scrollback ends exactly with `LINE <n-1>`; then
   `CSI H`.
2. Unless `--lines`: enter the alternate screen (`CSI ? 1049 h`) — with
   `--lines` the probe stays on the main screen so its scrollback stays
   reachable. Enable focus events (`CSI ? 1004 h`); unless `--no-mouse`: the
   `--mouse-mode` tracking mode (`CSI ? 1000 h` / `CSI ? 1002 h` default /
   `CSI ? 1003 h`) and, unless `--x10`, SGR encoding (`CSI ? 1006 h`); unless
   `--no-paste`: bracketed paste (`CSI ? 2004 h`); with `--app-cursor`:
   `CSI ? 1 h`.
3. Log `start` (see below) with the size and the modes it turned on.
4. Draw the screen (below).
5. Send `CSI 6 n` (cursor position report) and wait for `CSI <row> ; <col> R`
   at most 2 s → log `dsr`, or `timeout` with `"query": "dsr"`.
6. Send `OSC 11 ; ? BEL` (background colour) and wait for
   `OSC 11 ; <colour> (BEL | ST)` at most 2 s → log `osc11`, or `timeout` with
   `"query": "osc11"`.
7. With `--kitty-query`: send `CSI ? u` immediately followed by `CSI c` (DA1).
   A `CSI ? <flags> u` that arrives before the DA1 reply means kitty keyboard
   support is advertised; the DA1 reply arriving first (or nothing within 2 s)
   means it is not. Log `kitty`.
8. Log `ready`. From here on the probe only reads input, logs it, and redraws
   (and, with `--flood`, prints its lines).

Bytes that arrive while waiting in steps 5–7 and are not the awaited reply are
logged as ordinary input events (below), never dropped.

## Screen

Every redraw is wrapped in synchronized output: `CSI ? 2026 h` … `CSI ? 2026 l`.
Contents, starting at row 1 column 1 of the alternate screen, one item per row:

- rows 1–10: `ROW 0` … `ROW 9` (plain text, default colours);
- row 11: `TRUECOLOR` drawn with SGR `38;2;255;128;0` (foreground) and
  `48;2;0;64;128` (background), then `CSI 0 m`;
- row 12: `LAST <ev>` — the kind of the most recent logged event (blank before
  the first one);
- row 13, only with `--wide`: `WIDE 漢字😀│end` (`漢`, `字`, `😀` take two
  cells each, `│` one).

Without `--lines` the rows are on the alternate screen; with `--lines` they are
on the main screen and redraws never use `CSI 2 J` / `CSI 3 J` (each row is
rewritten in place with `CSI <row> ; 1 H` + text + `CSI K`), so the scrollback
above stays intact.

With `--flood <n>`: after `ready`, the probe sets the scroll region to rows
14–`rows` (`CSI 14 ; <rows> r`) and prints `FLOOD <k>` lines at its bottom, `n`
per second spread over about 10 batches a second, k counting from 0. Rows 1–13
stay put. Flood lines are not logged. A window too small for that region gets no scroll region
(`CSI r`): at exactly 14 rows each flood line overwrites row 14 in place, below 14 rows flood lines are not
printed (k still counts on).

The probe redraws after start-up, after every resize and after every logged
event. Tests read rows 1–11 back through `pane.read` and the viewer stream.

## Event log (`$FT_PROBE_LOG`)

One JSON object per line, appended and flushed per event. Common fields:

| Field | Meaning |
|---|---|
| `t` | integer milliseconds since the probe started |
| `ev` | event kind (table below) |
| `hex` | the raw bytes this event was made of, lowercase hex, no separators (`""` for events with no input bytes) |

| `ev` | When | Extra fields |
|---|---|---|
| `start` | after step 2 | `cols`, `rows`, `mouse` (bool), `paste` (bool), `app_cursor` (bool), `mouse_mode` (`click` / `drag` / `motion`, or `null` with `--no-mouse`), `sgr` (bool), `alt_screen` (bool; false with `--lines`) |
| `dsr` | cursor position report received | `row`, `col` (1-based, as reported) |
| `osc11` | background colour reply received | `color` (the text between `11;` and the terminator, e.g. `rgb:0000/0000/0000`) |
| `kitty` | step 7 decided | `answered` (bool), `flags` (integer, or `null` when not answered) |
| `timeout` | a start-up query got no reply in 2 s | `query`: `dsr` / `osc11` / `kitty` (kitty also logs a `kitty` event with `answered: false`) |
| `ready` | start-up finished | — |
| `key` | any input that is not one of the kinds below; one event per `read()` chunk after the other kinds have been cut out | — |
| `mouse` | an SGR (`CSI < b ; x ; y M/m`) or X10 (`CSI M` + 3 bytes) mouse report | `button` (low 2 bits of `b`, wheel = 64/65 kept as is), `col`, `row` (1-based, in the probe's own screen), `press` (`M` = true, `m` = false; X10 release = false), `motion` (bit 32 of `b`), `encoding` (`sgr` / `x10`) |
| `paste` | `CSI 200 ~` … `CSI 201 ~` | `text_hex` (the bytes between the markers); `hex` holds the whole thing including both markers |
| `resize` | SIGWINCH | `cols`, `rows` (new size from `TIOCGWINSZ`) |
| `focus` | `CSI I` / `CSI O` | `in` (bool) |
| `exit` | just before exiting | `reason`: `q` / `sigterm` / `exit-after` |

A paste is one event even if it spans several reads (the probe buffers until
`CSI 201 ~`). A lone `q` key (a `key` event whose bytes are exactly `71`) logs
the `key` event and then quits; a `q` inside a paste does not quit.

## Exit

On `q`, SIGTERM or `--exit-after`: log `exit`, turn off every mode it turned on
(reverse order), leave the alternate screen, restore the tty, exit 0. Any I/O
error on the tty or the log: message to stderr, exit 1.

## How the tests use it (`crates/app/tests/e2e_probe.rs`, `mouse.rs`, `perf.rs`)

The tests start a real `fleetmux server`, spawn `ft-probe` in a pane through
`pane.spawn` (with `FT_PROBE_LOG` in `env`), wait for the `ready` event, then
drive the pane through `pane.write` / `pane.key` / a view connection and assert
on the log. The test harness finds the binary at `$FT_PROBE_BIN`, else next to
the `fleetmux` test binary (`target/<profile>/ft-probe`), so `cargo build -p
ft-probe` (or `cargo build --workspace`) must have run first.
