# The `fleetmux demo` recording

The README shows a recording of `fleetmux demo`. This file says how it is made, so it can be made again.

## Files

| File | What it is |
|---|---|
| `fleetmux-demo.rec` | The raw recording in the fleetmux `--record` format (rust/PROTOCOL.md section 7), after redaction. |
| `fleetmux-demo.cast` | The same recording as asciicast v2. |
| `fleetmux-demo.svg` | The image the README shows: the `.cast` as one looping animated SVG (see below). |

## What is recorded

`scripts/record_demo.sh` starts a private `fleetmux server --record` in a new `/tmp/fmxd-rec-XXXXXX` directory
and runs one pane in it: `fleetmux demo` from this checkout, in the attach view. The pane fills the screen
(120x33). `SSH_CONNECTION` is set in that pane only, so the demo attaches in the pane and does not open a window.

The recording holds what a person sees in a terminal:

1. The banner `scripted agents, no model calls - the acceptance is real fleetmux code` (the demo prints it first
   and last; every agent pane shows it on its top line).
2. The attached fleet: the supervisor and worker panes, all scripted.
3. The first COMPLETED claim and its rejection by the daemon: `artifact not written: worker-1/test_slug.py`.
4. The fix, the second claim, and its acceptance.
5. The summary, read from the run's audit stream (`acceptance.verdict` events), and the clean-up line.

## Make it again

```bash
PYTHON=<python with requirements.txt> FLEETMUX=<fleetmux binary> scripts/record_demo.sh   # writes docs/images
```

The script:

1. records the demo as above (about 25 seconds);
2. redacts the recording with `scripts/redact_recording.py`, which replaces e-mail addresses, home directory
   paths and token shapes, plus `--literal` the user name and the host name;
3. converts it with `scripts/rec_to_cast.py --idle-limit 2` (pauses longer than 2 seconds are shortened);
4. renders the `.cast` with `scripts/cast_to_svg.py` (see below);
5. fails, and keeps nothing, if the user name, the short host name or the home directory is still in the `.rec`,
   the `.cast` or the `.svg`;
6. shuts down its recording server through the server's own control socket and removes its directory, also on
   Ctrl-C.

Redaction runs on the `.rec`, before the conversion: `redact_recording.py` reads only the `.rec` format. Do not
record with another tool, because that skips this step. The redaction tool does not replace a manual check: play
the `.cast` once before committing it.

## The SVG

`scripts/cast_to_svg.py` replays the `.cast` into a terminal screen with pyte (requirements-dev.txt) and keeps a
frame each time the screen changes. It writes one SVG file: the frames are stacked, and one CSS animation shows them
in order and loops; the last frame stays for 3 seconds (`--hold`). The file has no script, no link and no external
font, because GitHub and PyPI load it as an image. The text uses the viewer's monospace font. The display width is
at most 960 pixels (`--width`).

```bash
python scripts/cast_to_svg.py docs/images/fleetmux-demo.cast docs/images/fleetmux-demo.svg
```

The file must stay at or below 5 MB. The README links it by an absolute URL
(`https://raw.githubusercontent.com/justindbca/fleetmux/main/docs/images/fleetmux-demo.svg`), because PyPI renders
the same README and a relative path does not resolve there. The URL works once the file is on the `main` branch
on GitHub.

For a GIF, install [agg](https://github.com/asciinema/agg) and make it from the same `.cast`:

```bash
agg --idle-time-limit 2 docs/images/fleetmux-demo.cast docs/images/fleetmux-demo.gif
```
