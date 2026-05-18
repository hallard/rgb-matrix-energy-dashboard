#!/home/pi/rgbenv/bin/python3
"""
128x64 energy dashboard inspired by OpenEnergyMonitor.
MQTT sources: PV, grid import, grid export (W).
House consumption = PV + import - export.

Rendering through PIL (same approach as cryptoticker):
- top values:  bitmap bold (X11 8x13B) for the big numbers
- bottom text: tom-thumb.pil (3x5) for values + labels + suffixes

Usage: sudo ./energy_dashboard.py [config.yaml]
"""
import atexit
import json
import logging
import signal
import sqlite3
import sys
import threading
import time
from pathlib import Path

import paho.mqtt.client as mqtt
import yaml
from PIL import Image, ImageDraw, ImageFont
from rgbmatrix import RGBMatrix, RGBMatrixOptions, graphics

SCRIPT_DIR = Path(__file__).resolve().parent


# --- Shared MQTT state ----------------------------------------------------
class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.pv = None
        self.imp = None
        self.exp = None
        self.day_use = None    # kWh consumed today (Linky TDAY)
        self.day_exp = None    # kWh exported today (Linky PTDAY)
        self.day_pv  = None    # kWh produced by PV today (MQTT topic)
        self.lux = None        # ambient lux from LTR-559 (debug overlay)
        self.brightness = None # computed matrix brightness 0..100 (debug)

    def update(self, key, value):
        with self.lock:
            setattr(self, key, value)

    def snapshot(self):
        with self.lock:
            return self.pv, self.imp, self.exp

    def snapshot_day(self):
        with self.lock:
            return self.day_use, self.day_exp, self.day_pv


# --- SQLite history -------------------------------------------------------
DB_SCHEMA = """
    CREATE TABLE IF NOT EXISTS history (
        ts  INTEGER PRIMARY KEY,
        pv  INTEGER,
        imp INTEGER,
        exp INTEGER
    )
"""


def init_db(disk_path):
    """In-RAM DB. If a file exists on disk, we restore it into RAM.
    All runtime writes happen in RAM; we dump to SD periodically
    (see persist_to_disk)."""
    ram = sqlite3.connect(":memory:", check_same_thread=False,
                          isolation_level=None)
    ram.execute("PRAGMA temp_store=MEMORY")

    disk_path = Path(disk_path)
    if disk_path.exists():
        try:
            disk = sqlite3.connect(str(disk_path))
            disk.backup(ram)
            disk.close()
            logging.info("DB restored from %s", disk_path)
        except sqlite3.Error as e:
            logging.warning("Restore failed (%s) - starting fresh", e)

    ram.execute(DB_SCHEMA)
    return ram


def persist_to_disk(ram_db, disk_path):
    """Full dump RAM -> disk file (atomic via backup API)."""
    try:
        disk = sqlite3.connect(str(disk_path))
        ram_db.backup(disk)
        disk.close()
        logging.info("DB persisted -> %s", disk_path)
    except sqlite3.Error as e:
        logging.error("Backup failed: %s", e)


def load_history(db, bucket_minutes, buckets):
    """1 column = `bucket_minutes` minutes, total window = buckets*bucket_minutes."""
    now = int(time.time())
    bucket_sec = bucket_minutes * 60
    since = now - buckets * bucket_sec
    pv = [0.0] * buckets
    imp = [0.0] * buckets
    exp = [0.0] * buckets
    n = [0] * buckets
    for ts, p, i, e in db.execute(
            "SELECT ts, pv, imp, exp FROM history WHERE ts >= ?", (since,)):
        idx = int((ts - since) / bucket_sec)
        if 0 <= idx < buckets:
            pv[idx] += p or 0
            imp[idx] += i or 0
            exp[idx] += e or 0
            n[idx] += 1
    for k in range(buckets):
        if n[k]:
            pv[k] /= n[k]; imp[k] /= n[k]; exp[k] /= n[k]
    return pv, imp, exp


def prune_history(db, window_seconds):
    cutoff = int(time.time()) - window_seconds - 60
    db.execute("DELETE FROM history WHERE ts < ?", (cutoff,))


# --- Threads --------------------------------------------------------------
def mqtt_thread(cfg, state):
    topics = cfg['mqtt']['topics']
    t2k = {
        topics['pv']: 'pv',
        topics['import_grid']: 'imp',
        topics['export_grid']: 'exp',
    }
    linky_topic = topics.get('linky_meter') or None
    use_field   = cfg['mqtt'].get('linky_use_field', 'TDAY')
    exp_field   = cfg['mqtt'].get('linky_exp_field', 'PTDAY')
    linky_scale = 0.001 if cfg['mqtt'].get('linky_unit', 'Wh') == 'Wh' else 1.0

    solar_topic = topics.get('solar_meter') or None
    solar_field = cfg['mqtt'].get('solar_field', '') or None
    solar_scale = 0.001 if cfg['mqtt'].get('solar_unit', 'Wh') == 'Wh' else 1.0

    def on_connect(client, userdata, flags, rc):
        logging.info("MQTT connected rc=%s", rc)
        for t in t2k:
            client.subscribe(t)
        if linky_topic:
            client.subscribe(linky_topic)
        if solar_topic:
            client.subscribe(solar_topic)

    def on_message(client, userdata, msg):
        topic = msg.topic
        # Linky: multi-field JSON (daily conso + export)
        if topic == linky_topic:
            try:
                j = json.loads(msg.payload.decode())
            except (json.JSONDecodeError, UnicodeDecodeError):
                return
            if use_field in j:
                try:
                    state.update('day_use', float(j[use_field]) * linky_scale)
                except (ValueError, TypeError):
                    pass
            if exp_field in j:
                try:
                    state.update('day_exp', float(j[exp_field]) * linky_scale)
                except (ValueError, TypeError):
                    pass
            return
        # Daily PV: JSON single field or raw number
        if topic == solar_topic:
            payload = msg.payload.decode(errors='ignore').strip()
            try:
                if solar_field:
                    j = json.loads(payload)
                    val = float(j[solar_field])
                else:
                    val = float(payload)
                state.update('day_pv', val * solar_scale)
            except (json.JSONDecodeError, ValueError, KeyError, TypeError):
                pass
            return
        # Simple topics: raw number in W
        try:
            v = float(msg.payload.decode().strip())
        except (ValueError, UnicodeDecodeError):
            return
        key = t2k.get(topic)
        if key:
            state.update(key, v)

    client_id = f"{cfg['mqtt']['client_id_prefix']}_{int(time.time())}"
    try:
        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, client_id)
    except (AttributeError, TypeError):
        c = mqtt.Client(client_id)
    c.username_pw_set(cfg['mqtt']['username'], cfg['mqtt']['password'])
    c.on_connect = on_connect
    c.on_message = on_message
    c.reconnect_delay_set(min_delay=1, max_delay=30)

    while True:
        try:
            c.connect(cfg['mqtt']['host'], cfg['mqtt']['port'])
            c.loop_forever()
        except Exception as e:
            logging.error("MQTT loop: %s", e)
            time.sleep(5)


def brightness_thread(matrix, state, cfg):
    """Adjust matrix.brightness from ambient light (LTR-559 over I2C)."""
    try:
        from ltr559 import LTR559
    except ImportError:
        logging.warning("ltr559 lib missing - static brightness")
        return
    try:
        sensor = LTR559()
    except Exception as e:
        logging.warning("LTR-559 init failed (%s) - static brightness", e)
        return

    gain = cfg.get('sensor_gain')
    integ = cfg.get('sensor_integration_ms')
    try:
        if gain is not None:
            sensor.set_light_options(gain=gain)
        if integ is not None:
            sensor.set_light_integration_time_ms(integ)
    except Exception as e:
        logging.warning("LTR-559 sensor tuning failed (%s)", e)

    bmin  = cfg.get('min', 15)
    bmax  = cfg.get('max', 80)
    lmin  = cfg.get('lux_min', 2)
    lmax  = cfg.get('lux_max', 300)
    poll  = cfg.get('poll_seconds', 2)
    alpha = cfg.get('ema_alpha', 0.2)
    span  = max(0.001, lmax - lmin)

    ema = None
    while True:
        try:
            sensor.update_sensor()
            lux = sensor.get_lux()
        except Exception as e:
            logging.error("LTR-559 read: %s", e)
            time.sleep(poll)
            continue
        ema = lux if ema is None else alpha * lux + (1 - alpha) * ema
        state.update('lux', ema)
        if ema <= lmin:
            b = bmin
        elif ema >= lmax:
            b = bmax
        else:
            b = bmin + (bmax - bmin) * (ema - lmin) / span
        b_int = int(round(b))
        try:
            matrix.brightness = b_int
        except Exception as e:
            logging.error("brightness set: %s", e)
        state.update('brightness', b_int)
        time.sleep(poll)


def logger_thread(ram_db, state, disk_path, sample_sec, backup_sec, window_seconds):
    """Sample into RAM every `sample_sec`; dump to disk every `backup_sec`."""
    next_prune  = time.time() + 600
    next_backup = time.time() + backup_sec
    while True:
        time.sleep(sample_sec)
        pv, imp, exp = state.snapshot()
        if pv is None and imp is None and exp is None:
            continue
        try:
            ram_db.execute(
                "INSERT OR REPLACE INTO history (ts, pv, imp, exp) VALUES (?,?,?,?)",
                (int(time.time()), int(pv or 0), int(imp or 0), int(exp or 0)))
            if time.time() > next_prune:
                prune_history(ram_db, window_seconds)
                next_prune = time.time() + 600
            if time.time() > next_backup:
                persist_to_disk(ram_db, disk_path)
                next_backup = time.time() + backup_sec
        except sqlite3.Error as e:
            logging.error("DB: %s", e)


# --- Display --------------------------------------------------------------
def setup_matrix(cfg):
    o = RGBMatrixOptions()
    o.rows = cfg.get('rows', 64)
    o.cols = cfg.get('cols', 128)
    o.chain_length = 1
    o.parallel = 1
    o.hardware_mapping = cfg.get('hardware_mapping', 'adafruit-hat-pwm')
    o.gpio_slowdown = cfg.get('gpio_slowdown', 4)
    o.brightness = cfg.get('brightness', 70)
    o.pwm_bits = cfg.get('pwm_bits', 11)
    o.drop_privileges = False
    return RGBMatrix(options=o)


def fmt_w(v):
    if v is None:
        return "0"
    av = abs(v)
    if av >= 10000:
        return f"{v/1000:.1f}k"
    return str(int(round(v)))


def fmt_axis(v):
    if v % 1000 == 0:
        return f"{int(v)//1000}k"
    return f"{v/1000:.1f}k"


def nice_step(peak):
    # 1k floor, auto-scale above
    for s in (1000, 2000, 5000, 10000, 20000):
        if peak / s <= 5:
            return s
    return 50000


def pct(v):
    return "--" if v is None else str(int(round(v)))


def fmt_kwh(v):
    """kWh with 1 decimal place (2.8, 0.4, 12.3). Tom-thumb leaves room."""
    if v is None:
        return "--"
    return f"{v:.1f}"


def draw_aligned(d, ref_x, y, txt, font, fill, mode):
    """mode: 'left' (start at ref_x), 'center' (centered on ref_x),
    'right' (rightmost pixel is ref_x-1)."""
    bb = d.textbbox((0, 0), txt, font=font)
    w = bb[2] - bb[0]
    if mode == 'left':
        x = ref_x
    elif mode == 'center':
        x = ref_x - w // 2
    else:  # right
        x = ref_x - w
    d.text((x, y), txt, font=font, fill=fill)
    return x, w


def draw_pvu(d, ref_x, y_value, y_small, prefix, value, unit,
             fill_value, fill_small, font_value, font_small, mode):
    """[prefix small] [value big] [unit small] aligned as a single block."""
    w_p = d.textbbox((0, 0), prefix, font=font_small)[2] if prefix else 0
    w_v = d.textbbox((0, 0), value,  font=font_value)[2]
    w_u = d.textbbox((0, 0), unit,   font=font_small)[2] if unit   else 0
    total = w_p + w_v + w_u
    if mode == 'left':
        x = ref_x
    elif mode == 'center':
        x = ref_x - total // 2
    else:
        x = ref_x - total
    if prefix:
        d.text((x,            y_small), prefix, font=font_small, fill=fill_small)
    d.text((x + w_p,          y_value), value,  font=font_value, fill=fill_value)
    if unit:
        d.text((x + w_p + w_v, y_small), unit,  font=font_small, fill=fill_small)


def draw_dashboard(img, d, fonts, colors, layout, snap, day_snap, hist,
                   bucket_minutes, lux=None, brightness=None):
    W, H = img.size
    d.rectangle([0, 0, W, H], fill=(0, 0, 0))
    f_big, f_small, f_axis = fonts

    pv, imp, exp = snap
    # Compute conso as soon as at least one topic has a value: others count
    # as 0 (useful at night when PV stops publishing, etc.)
    if pv is None and imp is None and exp is None:
        conso = None
    else:
        conso = max(0, (pv or 0) + (imp or 0) - (exp or 0))

    # --- Top: USE left, EXP center, PV right ---
    # Left-anchored items shifted by 1 px so they don't hug the panel edge.
    top_y = layout['top_y']
    draw_aligned(d,   1, top_y, fmt_w(conso) + "W", f_big, tuple(colors['use']), 'left')
    draw_aligned(d,  64, top_y, fmt_w(exp)   + "W", f_big, tuple(colors['exp']), 'center')
    draw_aligned(d, 128, top_y, fmt_w(pv)    + "W", f_big, tuple(colors['pv']),  'right')

    chart_top = layout['chart_top']
    chart_bot = layout['chart_bot']
    chart_x0  = layout['chart_x0']
    h = chart_bot - chart_top
    gw = W - chart_x0
    pv_h, imp_h, exp_h = hist
    cdim = tuple(colors['dim'])
    cL   = tuple(colors['label'])

    if len(pv_h) == gw and gw > 0:
        # Blue = grid import only (what we actually pull from the grid).
        # Sunny day with no import -> no blue.
        use_h = [max(0.0, imp_h[x]) for x in range(gw)]
        # 1k floor: under 1kW the scale stays at 1k, above it auto-adapts
        peak = max(max(pv_h), max(use_h), 1000.0)

        # Auto-adaptive Y scale (multiples of 1k)
        step = nice_step(peak)
        v = step
        while v <= peak:
            y = chart_bot - int(v / peak * h)
            if y >= chart_top:
                for x in range(chart_x0, W, 3):
                    d.point((x, y), fill=cdim)
                # 4x6 glyph (5 visible rows) centered on line -> top = y - 2
                d.text((1, y - 2), fmt_axis(v), font=f_axis, fill=cL)
            v += step

        # Vertical grid: 1 tick per hour (drawn before the bars so that
        # the bars overwrite the ticks where data is present)
        cols_per_hour = int(round(60 / max(1, bucket_minutes)))
        for x in range(W - 1, chart_x0 - 1, -cols_per_hour):
            for y in range(chart_top, chart_bot, 3):
                d.point((x, y), fill=cdim)

        # Stack: PV (yellow) underneath, GRID import (blue) on top (priority)
        cU = tuple(colors['use']); cP = tuple(colors['pv'])
        for x in range(gw):
            uh = int(use_h[x] / peak * h)
            ph = int(pv_h[x] / peak * h)
            xp = chart_x0 + x
            if ph > 0:
                d.line([(xp, chart_bot - ph), (xp, chart_bot - 1)], fill=cP)
            if uh > 0:
                d.line([(xp, chart_bot - uh), (xp, chart_bot - 1)], fill=cU)

        # Debug overlay: current lux + matrix brightness %, centered, white
        if lux is not None:
            parts = [f"{lux:.0f}lx"]
            if brightness is not None:
                parts.append(f"{brightness}%")
            txt = " ".join(parts)
            cx = (chart_x0 + W) // 2
            cy = (chart_top + chart_bot) // 2 - 3   # f_axis ~5..6 px tall
            draw_aligned(d, cx, cy, txt, f_axis, (255, 255, 255), 'center')

    # --- Bottom: ratios computed from DAILY totals (Linky kWh + PV) ---
    day_use, day_exp, day_pv = day_snap
    # Daily total house consumption = grid + (PV produced - PV exported)
    day_conso = None
    if day_use is not None and day_pv is not None and day_exp is not None:
        day_conso = day_use + max(0.0, day_pv - day_exp)
    # GRID % = share of daily consumption pulled from the grid (blue)
    grid_p = None
    if day_conso and day_conso > 0 and day_use is not None:
        grid_p = max(0.0, min(100.0, day_use / day_conso * 100))
    # EXPORT % = share of PV exported to the grid (green)
    export_p = None
    if day_pv and day_pv > 0 and day_exp is not None:
        export_p = max(0.0, min(100.0, day_exp / day_pv * 100))
    # DIRECT % = share of PV self-consumed (yellow)
    direct_p = None
    if day_pv and day_pv > 0 and day_exp is not None:
        direct_p = max(0.0, min(100.0, (day_pv - day_exp) / day_pv * 100))

    # Bottom: 5s cycle between [% ratios] and [daily kWh USE/EXP/PV].
    # Everything in f_small (tom-thumb 3x5) -> value + suffix on the same
    # line, grey label right above.
    by = layout['bottom_y']
    by_lbl = by - 7  # 5 px glyph + 2 px gap
    cL = tuple(colors['label'])

    if int(time.time() / 5) % 2 == 0:
        # Page 1: 3 ratios -- Grid (blue) / Export (green) / Direct (yellow)
        ci = tuple(colors['use']); cp = tuple(colors['pv']); ce = tuple(colors['exp'])
        draw_aligned(d,   1, by_lbl, "Reseau", f_small, cL, 'left')
        draw_aligned(d,  64, by_lbl, "Export", f_small, cL, 'center')
        draw_aligned(d, 128, by_lbl, "Direct", f_small, cL, 'right')
        draw_pvu(d,   1, by, by, "", pct(grid_p),   "%",
                 ci, ci, f_small, f_small, 'left')
        draw_pvu(d,  64, by, by, "", pct(export_p), "%",
                 ce, ce, f_small, f_small, 'center')
        draw_pvu(d, 128, by, by, "", pct(direct_p), "%",
                 cp, cp, f_small, f_small, 'right')
    else:
        # Page 2: daily kWh, value + "kWh" suffix + grey label above
        cu = tuple(colors['use']); ce = tuple(colors['exp']); cp = tuple(colors['pv'])
        draw_aligned(d,   1, by_lbl, "Conso",   f_small, cL, 'left')
        draw_aligned(d,  64, by_lbl, "Export",  f_small, cL, 'center')
        draw_aligned(d, 128, by_lbl, "Prod PV", f_small, cL, 'right')
        draw_pvu(d,   1, by, by, "", fmt_kwh(day_use), "kWh",
                 cu, cu, f_small, f_small, 'left')
        draw_pvu(d,  64, by, by, "", fmt_kwh(day_exp), "kWh",
                 ce, ce, f_small, f_small, 'center')
        draw_pvu(d, 128, by, by, "", fmt_kwh(day_pv),  "kWh",
                 cp, cp, f_small, f_small, 'right')


# --- Main -----------------------------------------------------------------
def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
    cfg_path = Path(sys.argv[1]) if len(sys.argv) > 1 else SCRIPT_DIR / "config.yaml"
    cfg = yaml.safe_load(cfg_path.read_text())

    state = State()

    db_path = Path(cfg['history']['db_path'])
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = init_db(str(db_path))
    bucket_min = cfg['history']['bucket_minutes']
    sample_sec = cfg['history'].get('sample_minutes', bucket_min) * 60
    backup_sec = cfg['history'].get('backup_minutes', 60) * 60
    cols = cfg['display']['cols']
    chart_x0 = cfg['display']['layout']['chart_x0']
    graph_cols = cols - chart_x0
    window_sec = bucket_min * 60 * graph_cols  # visible time window

    # Clean shutdown: flush RAM -> disk
    def _flush_on_exit(*_):
        persist_to_disk(db, str(db_path))
        sys.exit(0)
    atexit.register(lambda: persist_to_disk(db, str(db_path)))
    signal.signal(signal.SIGTERM, _flush_on_exit)
    signal.signal(signal.SIGINT,  _flush_on_exit)

    matrix = setup_matrix(cfg['display'])
    canvas = matrix.CreateFrameCanvas()

    fcfg = cfg['display']['fonts']

    def _load(path, size):
        return (ImageFont.load(path) if path.endswith('.pil')
                else ImageFont.truetype(path, size))

    f_big   = _load(fcfg['big_path'],   fcfg['big_size'])
    f_small = _load(fcfg['small_path'], fcfg['small_size'])
    f_axis  = _load(fcfg['axis_path'],  fcfg['axis_size'])

    img = Image.new('RGB', (cfg['display']['cols'], cfg['display']['rows']))
    d = ImageDraw.Draw(img)

    threading.Thread(target=mqtt_thread, args=(cfg, state), daemon=True).start()
    threading.Thread(target=logger_thread,
                     args=(db, state, str(db_path), sample_sec, backup_sec, window_sec),
                     daemon=True).start()
    br_cfg = cfg.get('brightness') or {}
    if br_cfg.get('enabled', False):
        threading.Thread(target=brightness_thread, args=(matrix, state, br_cfg),
                         daemon=True).start()

    refresh = 1.0 / cfg['display'].get('refresh_hz', 4)
    hist = ([], [], [])
    next_load = 0.0
    layout = cfg['display']['layout']

    while True:
        t0 = time.time()
        if t0 >= next_load:
            hist = load_history(db, bucket_min, graph_cols)
            next_load = t0 + 30
        draw_dashboard(img, d, (f_big, f_small, f_axis), cfg['colors'],
                       layout, state.snapshot(), state.snapshot_day(),
                       hist, bucket_min, lux=state.lux,
                       brightness=state.brightness)
        canvas.SetImage(img)
        canvas = matrix.SwapOnVSync(canvas)
        dt = time.time() - t0
        if dt < refresh:
            time.sleep(refresh - dt)


if __name__ == "__main__":
    main()
