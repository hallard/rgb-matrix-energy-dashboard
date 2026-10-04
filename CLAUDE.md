# CLAUDE.md — rgb-matrix-energy-dashboard

Project guide for Claude Code. Read this before editing. Targeted at this
repo (128x64 LED matrix energy dashboard) **and** at future siblings that
reuse the same architecture on a different display.

## What this project is

A single Python 3 process (`energy_dashboard.py`) that:
- subscribes to a few MQTT topics (home energy JSON, battery JSON, optional
  display power ON/OFF, optional lux),
- keeps shared state in a `State` object protected by one `threading.Lock`,
- records history to an **in-RAM SQLite** DB that is dumped to disk every
  `history.backup_minutes` minutes (SD-wear mitigation),
- renders a frame with PIL into an `Image` and pushes it to a hzeller
  `RGBMatrix` canvas via `SwapOnVSync`.

Target hardware here: Raspberry Pi Zero 2W + Adafruit RGB Matrix HAT (PWM)
+ a 128x64 panel. The dashboard is MIT-licensed; the matrix lib is
GPL-2.0-or-later (installed separately, not vendored).

## Repo layout

```
energy_dashboard.py        # single-file app, ~857 lines
config.yaml                # gitignored, real runtime config
config_sample.yaml         # tracked template, mirror of config.yaml
energy-dashboard.service   # systemd unit, runs as root (GPIO)
fonts/, images/            # bitmap PIL fonts + README screenshots
history.db                 # runtime, gitignored
test_fonts.py, test_top_fonts.py   # throwaway render benches, not CI
```

The venv is **hardcoded** in the shebang: `#!/home/pi/rgbenv/bin/python3`.
On other machines you may need a system `python3` instead — change the
shebang **and** the systemd `ExecStart`.

## Architecture

Four threads, all daemonised, all reading/writing `State` under its lock:

| Thread | Purpose |
|---|---|
| `mqtt_thread` | paho-mqtt loop; dispatches on `msg.topic` to the right branch |
| `logger_thread` | samples `state.snapshot()` into SQLite, prunes, backs up to disk, refreshes the daily midnight baseline |
| `brightness_thread` | reads lux (sensor **or** MQTT), applies EMA, writes `matrix.brightness` |
| main loop | 4 Hz PIL render → `SwapOnVSync` |

Communication is `State` + a few `State.set_*/get_*` helpers. There are no
queues or event loops — the shared-state design is intentional because the
render loop snapshots atomically and never blocks on I/O.

### Why the DB lives in `:memory:`

The Pi runs off an SD card. Writing samples at 1-sample-per-minute would
chew it up. So:
- all `INSERT` happen in a `:memory:` connection,
- a full dump to `history.db` happens every `history.backup_minutes`
  minutes and on `SIGTERM`/`SIGINT`/`atexit`,
- on boot, the on-disk file is restored into RAM.

**Do not** add a per-sample disk write "for safety". Prefer bumping the
backup period down if resilience matters.

### Midnight-baseline daily totals

Daily kWh for `Conso`/`Export`/`Prod PV` are **deltas** of the lifetime
cumulative counters (`*_kwh` from `energy/home`) against a baseline
captured in the `day_base` SQLite table at the first sample of each local
day. Re-baselined on day change **or** on counter rollback (meter reset).

See `update_day_baseline()` and `day_totals()`. The baseline survives
reboots because it is in the backed-up DB.

### MQTT topics (configurable under `mqtt.topics.*`)

| Key | Purpose | Payload |
|---|---|---|
| `home` (required) | Live power + lifetime kWh counters | JSON object, see README |
| `battery` (optional) | SOC / signed power / cumulative input kWh | JSON object |
| `display_power` (optional) | Night-mode switch | plain-text `ON`/`OFF` (Tasmota-style, retained) |
| (under `brightness.mqtt_topic`) | Lux feed for auto brightness | bare number, `"12.3 lx"`, or JSON (`mqtt_json_key`) |

`display_power` → when `OFF`, the render loop blanks the matrix and only
pushes a short green/red "heartbeat" on the four corners (red + faster
when `/sys/class/net/*/operstate` reports no non-`lo` interface up). MQTT
and history threads keep running during OFF — this is the whole point.

## Rendering conventions

- **Fonts**: three bitmap PIL fonts — `8x13B.pil` (top digits), `tom-thumb.pil`
  (3x5, bottom lines + small `W`/`%`/`kWh` suffixes), `4x6.pil` (Y-axis
  labels). TTF is supported via `big_size`/`small_size` but all current
  layout constants are tuned for the bitmap fonts.
- **Smaller suffix trick**: top values are a big number followed by a
  tom-thumb `W` baseline-aligned via the `UNIT_DY = 6` offset (because the
  8x13B glyph bottom sits at `top_y + 10` and tom-thumb is 5px tall).
  `w_value_width()` + `draw_w_value()` handle width/draw; `unit=` lets you
  swap `W` for `%`, `kWh`, etc.
- **Ink-based kerning**: the earlier `kW` attempt used actual rendered
  ink (`getbbox` on a glyph mask), not `textbbox` which returns the full
  monospace cell. That approach was stashed in favour of "always watts +
  smaller `W`"; the ink-mask helper is **not** in the file right now.
- **Arrows**: `draw_arrow()` draws a shaft + triangle head; the apex
  columns match the shaft columns so the head stays centred. Head width
  must be ≥ shaft width + at least 1 px on each side for a visible flare.
- **Battery icon**: `draw_battery_icon()`, 7×10 body + 3×2 nub on top,
  level fills from the bottom; width must be **odd** so the nub centres.
- **Chart**: PV (yellow) drawn first, then house load (cyan/blue) stacked
  on top. Y-axis step auto-adapts via `nice_step()`; the baseline is 1 kW.

## Night mode heartbeat (reference cadence)

- Network OK → **green**, `250 ms` on / `2 s` off.
- Network down → **red**, `250 ms` on / `1 s` off.

Only re-swap the canvas when `(on, net_up)` changes — polling runs at
50 ms so we don't miss the 250 ms pulse but we don't needlessly swap.

## Running

- `sudo /home/pi/rgbenv/bin/python3 ./energy_dashboard.py [config.yaml]`
- Via systemd: `energy-dashboard.service` (runs as root, GPIO requirement).

**Do not** start/stop the service autonomously — the owner manages the
lifecycle and reports results. Only edit code and systemd unit files;
let the user restart.

## Porting to a different display

Keep: `State`, `mqtt_thread`, `logger_thread`, `brightness_thread` (both
sources), the SQLite+baseline model, `_parse_lux`, the `display_power` /
heartbeat / network-check logic, `fmt_axis`/`nice_step`/`fmt_w`.

Change for a non-128x64 or non-hzeller output:
1. **Matrix setup** — replace `setup_matrix()` + the main-loop
   `SwapOnVSync()` with the new display's driver. The rest of the loop
   (snapshot → draw into PIL `Image` → push) stays the same.
2. **Layout constants** — `display.layout.*` in YAML (top_y, top_centers,
   chart_x0/top/bot, bottom_y). `top_centers` assumes a 128-wide canvas.
3. **Fonts** — pick bitmap sizes that match the new resolution; if you
   switch away from 8x13B/tom-thumb, re-tune `UNIT_DY` so the small
   suffix baseline matches the big-digit baseline on the new font pair.
4. **Chart width** — `graph_cols = cols - chart_x0`. The visible time
   window = `graph_cols * bucket_minutes`. If the panel is narrower,
   either accept a shorter window or shrink `bucket_minutes`.
5. **Brightness** — on a non-matrix display, `matrix.brightness = int(b)`
   may not apply; wrap the write in whatever the new driver expects, or
   drop the thread entirely.

Hardware-specific files that will differ per Pi:
- `config.yaml` (gitignored by design — ship a `config_sample.yaml` with
  path placeholders).
- `energy-dashboard.service` (ExecStart path depends on venv vs system
  python; one existing deployment uses `/usr/bin/python3 -u`).
- The shebang in the Python file.

## Conventions / gotchas

- Comments: short, only when non-obvious. Don't restate what the code
  does. The existing code is relatively clean of noise — keep it that way.
- Never commit `config.yaml` (contains broker credentials).
- Co-author trailer is **not** used in commits for this repo.
- `git` is HTTPS-authenticated from `/home/pi` on one machine only. If
  you need to push from a sibling Pi, SSH-fetch the branch to the auth'd
  machine and push from there: `git fetch ssh://pi@<other-pi>/path main`.
- When adding new MQTT topics: subscribe conditionally (`if topic:`) and
  dispatch at the **top** of `on_message` so later JSON parsing can't
  swallow the payload.
- Any new background thread **must** be `daemon=True` and must not hold
  `State.lock` across I/O.
