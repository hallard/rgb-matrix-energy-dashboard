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
    """Fed from the single JSON topic `energy/home`."""
    def __init__(self):
        self.lock = threading.Lock()
        # instantaneous (W)
        self.prod_watt = None   # PV production -> yellow
        self.use_nobat = None   # house load excl. battery -> blue
        self.grid_watt = None   # grid power, signed: >=0 import, <0 export
        # cumulative meters (kWh) -- used for the daily bottom row
        self.prod_kwh = None
        self.use_kwh_nobat = None
        self.grid_import_kwh = None
        self.grid_export_kwh = None
        self.solar_direct_kwh = None
        # battery aggregate (energy/ecoflow/batteries/aggregate)
        self.batt = None                  # SOC %
        self.batt_input_watt = None       # charging power (W)
        self.batt_output_watt = None      # discharging power (W)
        self.batt_power_watt = None       # net (W), informational
        self.batt_input_kwh = None        # cumulative charged (kWh, lifetime)
        self.batt_output_kwh = None       # cumulative discharged (kWh)
        # Display power switch (Tasmota-style ON/OFF, retained). True until told
        # otherwise so the panel lights up even before the first message.
        self.display_on = True

    def update_many(self, d):
        """Set only the keys whose value is not None (full JSON each msg)."""
        with self.lock:
            for k, v in d.items():
                if v is not None:
                    setattr(self, k, v)

    def snapshot(self):
        """(prod_watt, use_nobat, grid_watt) -> stored as (pv, imp, exp)."""
        with self.lock:
            return self.prod_watt, self.use_nobat, self.grid_watt

    def snapshot_kwh(self):
        """Cumulative meters: (prod, use_nobat, grid_import, grid_export, solar_direct)."""
        with self.lock:
            return (self.prod_kwh, self.use_kwh_nobat, self.grid_import_kwh,
                    self.grid_export_kwh, self.solar_direct_kwh)

    def snapshot_battery(self):
        """(SOC %, signed power W (>0 discharge, <0 charge), input kWh cumulative)."""
        with self.lock:
            return self.batt, self.batt_power_watt, self.batt_input_kwh


# --- SQLite history -------------------------------------------------------
DB_SCHEMA = """
    CREATE TABLE IF NOT EXISTS history (
        ts  INTEGER PRIMARY KEY,
        pv  INTEGER,
        imp INTEGER,
        exp INTEGER
    );
    CREATE TABLE IF NOT EXISTS day_base (
        id   INTEGER PRIMARY KEY CHECK (id = 1),
        day  TEXT,
        prod REAL,
        use  REAL,
        exp  REAL
    );
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

    ram.executescript(DB_SCHEMA)
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


def update_day_baseline(db, prod, use, exp):
    """Capture/refresh the midnight reference for the lifetime kWh counters.
    Re-baselines on a new local day, on first run, or if a counter went
    backwards (meter reset). No-op until all three values are available."""
    if prod is None or use is None or exp is None:
        return
    today = time.strftime("%Y-%m-%d")
    row = db.execute("SELECT day, prod, use, exp FROM day_base WHERE id=1").fetchone()
    if row is None:
        db.execute("INSERT INTO day_base (id, day, prod, use, exp) VALUES (1,?,?,?,?)",
                   (today, prod, use, exp))
        return
    bday, bprod, buse, bexp = row
    if bday != today or prod < bprod or use < buse or exp < bexp:
        db.execute("UPDATE day_base SET day=?, prod=?, use=?, exp=? WHERE id=1",
                   (today, prod, use, exp))


def day_totals(db, snap_kwh):
    """Today's totals = current cumulative - midnight baseline.
    Returns (day_prod, day_use, day_export) in kWh, None until ready."""
    prod, use_nb, _gimp, gexp, _sdir = snap_kwh
    row = db.execute("SELECT prod, use, exp FROM day_base WHERE id=1").fetchone()
    if row is None:
        return None, None, None
    bprod, buse, bexp = row

    def delta(cur, base):
        if cur is None or base is None:
            return None
        return max(0.0, cur - base)

    return delta(prod, bprod), delta(use_nb, buse), delta(gexp, bexp)


# --- Threads --------------------------------------------------------------
def mqtt_thread(cfg, state):
    home_topic = cfg['mqtt']['topics']['home']
    batt_topic = cfg['mqtt']['topics'].get('battery')
    disp_topic = cfg['mqtt']['topics'].get('display_power')

    def on_connect(client, userdata, flags, rc):
        logging.info("MQTT connected rc=%s", rc)
        client.subscribe(home_topic)
        if batt_topic:
            client.subscribe(batt_topic)
        if disp_topic:
            client.subscribe(disp_topic)

    def on_message(client, userdata, msg):
        # Display power switch: plain-text ON/OFF (Tasmota stat/... topic).
        if disp_topic and msg.topic == disp_topic:
            try:
                payload = msg.payload.decode().strip().upper()
            except UnicodeDecodeError:
                return
            state.display_on = (payload == "ON")
            logging.info("Display power -> %s", payload)
            return

        try:
            j = json.loads(msg.payload.decode())
        except (json.JSONDecodeError, UnicodeDecodeError):
            return

        def num(key):
            v = j.get(key)
            try:
                return float(v)
            except (TypeError, ValueError):
                return None

        if msg.topic == home_topic:
            state.update_many({
                'prod_watt':        num('prod_watt'),
                'use_nobat':        num('use_watt_no_bat'),
                'grid_watt':        num('grid_watt'),
                'prod_kwh':         num('prod_kwh'),
                'use_kwh_nobat':    num('use_kwh_no_bat'),
                'grid_import_kwh':  num('grid_import_kwh'),
                'grid_export_kwh':  num('grid_export_kwh'),
                'solar_direct_kwh': num('solar_direct_kwh'),
            })
        elif msg.topic == batt_topic:
            state.update_many({
                'batt':             num('batt'),
                'batt_input_watt':  num('input_watt'),
                'batt_output_watt': num('output_watt'),
                'batt_power_watt':  num('power_watt'),
                'batt_input_kwh':   num('input_kwh'),
                'batt_output_kwh':  num('output_kwh'),
            })

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


def brightness_thread(matrix, cfg):
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
        if ema <= lmin:
            b = bmin
        elif ema >= lmax:
            b = bmax
        else:
            b = bmin + (bmax - bmin) * (ema - lmin) / span
        try:
            matrix.brightness = int(round(b))
        except Exception as e:
            logging.error("brightness set: %s", e)
        time.sleep(poll)


def logger_thread(ram_db, state, disk_path, sample_sec, backup_sec, window_seconds):
    """Sample into RAM every `sample_sec`; dump to disk every `backup_sec`."""
    next_prune  = time.time() + 600
    next_backup = time.time() + backup_sec
    while True:
        time.sleep(sample_sec)
        # Refresh the midnight reference for the daily kWh totals.
        kprod, kuse, _gimp, kexp, _sdir = state.snapshot_kwh()
        try:
            update_day_baseline(ram_db, kprod, kuse, kexp)
        except sqlite3.Error as e:
            logging.error("DB baseline: %s", e)
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
    """Top values stay in watts (no kW conversion); the 'W' unit is drawn in a
    smaller font so the full number keeps its size and still fits."""
    if v is None:
        return "0"
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


def draw_arrow(d, x, y0, up, color, w=6, h=8, head=3, shaft_w=2):
    """Arrow whose top pixel is `y0`: a triangular head (`head` px tall, `w` px
    base tapering to a `shaft_w` apex) plus a `shaft_w`-wide shaft on the rest.
    Apex and shaft share the same centred columns so the head stays centred on
    the shaft. up=export, down=import."""
    sx0 = x + (w - shaft_w) // 2     # shaft / apex left column (centred on base)
    sx1 = sx0 + shaft_w - 1
    shaft = h - head
    if up:
        # head points up at the top, shaft below
        d.polygon([(sx0, y0), (sx1, y0),
                   (x + w - 1, y0 + head - 1), (x, y0 + head - 1)], fill=color)
        d.rectangle([sx0, y0 + head, sx1, y0 + h - 1], fill=color)
    else:
        # shaft on top, head points down at the bottom
        d.rectangle([sx0, y0, sx1, y0 + shaft - 1], fill=color)
        d.polygon([(x, y0 + shaft), (x + w - 1, y0 + shaft),
                   (sx1, y0 + h - 1), (sx0, y0 + h - 1)], fill=color)
    return w


def draw_battery_icon(d, x, top, level, frame_col, fill_col,
                      w=7, h=10, term_w=3, term_h=2):
    """Vertical battery icon: terminal nub on top, body below, filled from the
    bottom proportional to `level` (0..100). `top` is the topmost pixel
    (the nub). Total height = h + term_h. Returns horizontal extent (w)."""
    # terminal nub centred on top
    nx = x + (w - term_w) // 2
    d.rectangle([nx, top, nx + term_w - 1, top + term_h - 1], fill=frame_col)
    # body outline below the nub
    body_top = top + term_h
    body_bot = body_top + h - 1
    d.rectangle([x, body_top, x + w - 1, body_bot], outline=frame_col)
    # fill bar from the bottom of the inner area
    if level is not None and level > 0:
        inner_h = h - 2
        fill_h = max(1, min(inner_h, round(inner_h * level / 100)))
        d.rectangle([x + 1, body_bot - fill_h, x + w - 2, body_bot - 1], fill=fill_col)
    return w


# Top values: a big number followed by a smaller 'W' unit. The 8x13B digits'
# ink bottom sits at top_y+10; the small 'W' glyph is 5px tall, so drawing it at
# top_y+UNIT_DY lands its bottom on that same baseline.
UNIT_DY = 6
UNIT_GAP = 1   # px between the number and the small unit


def w_value_width(d, num, font_big, font_unit, unit="W"):
    return (d.textbbox((0, 0), num, font=font_big)[2] + UNIT_GAP
            + d.textbbox((0, 0), unit, font=font_unit)[2])


def draw_w_value(d, x, top_y, num, font_big, font_unit, fill, unit="W"):
    """Big number in font_big + a smaller unit suffix (defaults to 'W')
    baseline-aligned just after. UNIT_DY works for any tom-thumb suffix since
    every glyph is 5 px tall."""
    wv = d.textbbox((0, 0), num, font=font_big)[2]
    d.text((x, top_y), num, font=font_big, fill=fill)
    d.text((x + wv + UNIT_GAP, top_y + UNIT_DY), unit, font=font_unit, fill=fill)


_NET_CACHE = {'ts': 0.0, 'up': True}


def network_up(ttl=5.0):
    """True if any non-loopback interface is up (wifi or ethernet).
    Cached for `ttl` seconds so it's cheap to call from the render loop."""
    now = time.time()
    if now - _NET_CACHE['ts'] < ttl:
        return _NET_CACHE['up']
    up = False
    try:
        for p in Path('/sys/class/net').iterdir():
            if p.name == 'lo':
                continue
            try:
                if p.joinpath('operstate').read_text().strip() == 'up':
                    up = True
                    break
            except OSError:
                continue
    except OSError:
        up = True   # can't tell -> don't false-alarm
    _NET_CACHE['ts'] = now
    _NET_CACHE['up'] = up
    return up


def draw_dashboard(img, d, fonts, colors, layout, snap, day_snap, batt_snap,
                   hist, bucket_minutes):
    W, H = img.size
    d.rectangle([0, 0, W, H], fill=(0, 0, 0))
    f_big, f_small, f_axis = fonts

    # snap = (prod_watt, use_nobat, grid_watt), stored in the DB as (pv, imp, exp)
    prod, use_nobat, grid = snap

    # --- Top: USE left, GRID center, PV right (number big, 'W' unit smaller) ---
    # Left-anchored items shifted by 1 px so they don't hug the panel edge.
    top_y = layout['top_y']
    # Left: house load excl. battery (cyan)
    draw_w_value(d, 1, top_y, fmt_w(use_nobat), f_big, f_small, tuple(colors['use']))
    # Center: grid power with a direction arrow -- colour + arrow show flow
    # (>=0 import: red, arrow down ; <0 export: green, arrow up)
    g = grid or 0
    grid_col = tuple(colors['imp']) if g >= 0 else tuple(colors['exp'])
    gtxt = fmt_w(abs(g))
    aw, gap, ah = 6, 2, 8
    total = aw + gap + w_value_width(d, gtxt, f_big, f_small)
    ax = 64 - total // 2
    # centre the arrow on the number's vertical extent
    bb = d.textbbox((0, 0), gtxt, font=f_big)
    ay0 = top_y + round((bb[1] + bb[3]) / 2 - ah / 2)
    draw_arrow(d, ax, ay0, up=g < 0, color=grid_col, w=aw, h=ah)
    draw_w_value(d, ax + aw + gap, top_y, gtxt, f_big, f_small, grid_col)
    # Right: PV production (yellow)
    rtxt = fmt_w(prod)
    draw_w_value(d, 128 - w_value_width(d, rtxt, f_big, f_small), top_y, rtxt,
                 f_big, f_small, tuple(colors['pv']))

    chart_top = layout['chart_top']
    chart_bot = layout['chart_bot']
    chart_x0  = layout['chart_x0']
    h = chart_bot - chart_top
    gw = W - chart_x0
    pv_h, imp_h, exp_h = hist   # (prod_watt, use_nobat, grid_watt) history
    cdim = tuple(colors['dim'])
    cL   = tuple(colors['label'])

    if len(pv_h) == gw and gw > 0:
        # Blue = house load excl. battery (use_nobat), stacked over yellow PV.
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

    # --- Bottom: daily totals (delta since midnight on cumulative kWh) ---
    # day_snap = (PV produced, house consumption, grid exported) in kWh.
    day_pv, day_use, day_exp = day_snap
    self_pv = None  # PV not exported (self-consumed / stored)
    if day_pv is not None and day_exp is not None:
        self_pv = max(0.0, day_pv - day_exp)
    # GRID % = share of daily consumption pulled from the grid (blue)
    grid_p = None
    if day_use and day_use > 0 and self_pv is not None:
        grid_p = max(0.0, min(100.0, (day_use - self_pv) / day_use * 100))
    # EXPORT % = share of PV exported to the grid (green)
    export_p = None
    if day_pv and day_pv > 0 and day_exp is not None:
        export_p = max(0.0, min(100.0, day_exp / day_pv * 100))
    # DIRECT % = share of PV self-consumed (yellow)
    direct_p = None
    if day_pv and day_pv > 0 and self_pv is not None:
        direct_p = max(0.0, min(100.0, self_pv / day_pv * 100))

    # Bottom: 5s cycle between [% ratios] and [daily kWh USE/EXP/PV].
    # Everything in f_small (tom-thumb 3x5) -> value + suffix on the same
    # line, grey label right above.
    by = layout['bottom_y']
    by_lbl = by - 7  # 5 px glyph + 2 px gap
    cL = tuple(colors['label'])

    if int(time.time() / 5) % 2 == 0:
        # Page 1: battery overview -- no labels, f_big across both bottom rows
        # Left: SOC % | Center: charge/discharge power (arrow + colour)
        # Right: cumulative input_kwh (charged lifetime, rounded int)
        batt, b_pow, b_in_kwh = batt_snap
        bat_y = 52   # 8x13B drawn at y=52 -> ink rows 53..62, vertically
                     # centred in the bottom area (rows 52..63)
        aw, gap_a, ah = 6, 2, 8
        c_batt = tuple(colors['batt'])

        # Left: vertical battery icon (white frame, fill green > 20% / red <=)
        # + SOC %. Icon spans the full bottom area (rows 52..63).
        fill_c = tuple(colors['exp']) if (batt or 0) > 20 else tuple(colors['imp'])
        ic_w = draw_battery_icon(d, 1, bat_y, batt, (255, 255, 255), fill_c)
        btxt = "--" if batt is None else str(int(round(batt)))
        draw_w_value(d, 1 + ic_w + 2, bat_y, btxt, f_big, f_small, c_batt, unit="%")

        # Center: signed battery power -- battery-centric colours
        # >0 discharge (battery drains)  -> up,   red
        # <0 charge   (battery fills up) -> down, green
        # =0 idle                        -> no arrow, plain grey value
        bp = b_pow or 0
        ftxt = str(int(round(abs(bp))))
        if bp == 0:
            col = tuple(colors['label'])
            tw = w_value_width(d, ftxt, f_big, f_small)
            draw_w_value(d, 64 - tw // 2, bat_y, ftxt, f_big, f_small, col)
        else:
            col, up = ((tuple(colors['imp']), True) if bp > 0
                       else (tuple(colors['exp']), False))
            total = aw + gap_a + w_value_width(d, ftxt, f_big, f_small)
            ax = 64 - total // 2
            fbb = d.textbbox((0, 0), ftxt, font=f_big)
            ay0 = bat_y + round((fbb[1] + fbb[3]) / 2 - ah / 2)
            draw_arrow(d, ax, ay0, up=up, color=col, w=aw, h=ah)
            draw_w_value(d, ax + aw + gap_a, bat_y, ftxt, f_big, f_small, col)

        # Right: cumulative input_kwh
        ktxt = "--" if b_in_kwh is None else str(int(round(b_in_kwh)))
        kw = w_value_width(d, ktxt, f_big, f_small, unit="kWh")
        draw_w_value(d, 128 - kw, bat_y, ktxt, f_big, f_small, c_batt, unit="kWh")
    else:
        # Page 2: daily kWh (rounded int) + matching ratio %, grey label above
        cu = tuple(colors['use']); ce = tuple(colors['exp']); cp = tuple(colors['pv'])
        draw_aligned(d,   1, by_lbl, "Conso",   f_small, cL, 'left')
        draw_aligned(d,  64, by_lbl, "Export",  f_small, cL, 'center')
        draw_aligned(d, 128, by_lbl, "Prod PV", f_small, cL, 'right')

        def _kwh(v):  return "--" if v is None else str(int(round(v)))
        def _pct(p):  return "0" if p is None else str(int(round(p)))
        s_use = f"{_kwh(day_use)}kWh {_pct(grid_p)}%"
        s_exp = f"{_kwh(day_exp)}kWh {_pct(export_p)}%"
        s_pv  = f"{_kwh(day_pv)}kWh {_pct(direct_p)}%"
        draw_aligned(d,   1, by, s_use, f_small, cu, 'left')
        draw_aligned(d,  64, by, s_exp, f_small, ce, 'center')
        draw_aligned(d, 128, by, s_pv,  f_small, cp, 'right')


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
        threading.Thread(target=brightness_thread, args=(matrix, br_cfg),
                         daemon=True).start()

    refresh = 1.0 / cfg['display'].get('refresh_hz', 4)
    hist = ([], [], [])
    next_load = 0.0
    layout = cfg['display']['layout']

    W_img, H_img = img.size
    NIGHT_ON_SEC       = 0.25   # blink pulse width
    NIGHT_OFF_OK_SEC   = 2.0    # gap when the network is up  -> green
    NIGHT_OFF_DOWN_SEC = 1.0    # gap when the network is down -> red (faster)
    night_last_key = None       # (on, net_up) actually pushed
    while True:
        t0 = time.time()
        if not state.display_on:
            # Display switched OFF -- blank frame with a short heartbeat on the
            # four corners so the panel visibly stays alive at night. Green if
            # the network is up, red + faster cadence if it's down.
            # MQTT / logger threads keep running so history is still recorded.
            net_up = network_up()
            period = NIGHT_ON_SEC + (NIGHT_OFF_OK_SEC if net_up else NIGHT_OFF_DOWN_SEC)
            on = (t0 % period) < NIGHT_ON_SEC
            key = (on, net_up)
            if key != night_last_key:
                d.rectangle([0, 0, W_img, H_img], fill=(0, 0, 0))
                if on:
                    col = (0, 255, 0) if net_up else (255, 0, 0)
                    for x, y in ((0, 0), (W_img - 1, 0),
                                 (0, H_img - 1), (W_img - 1, H_img - 1)):
                        d.point((x, y), fill=col)
                canvas.SetImage(img)
                canvas = matrix.SwapOnVSync(canvas)
                night_last_key = key
            time.sleep(0.05)  # < NIGHT_ON_SEC so the 250 ms pulse isn't missed
            continue
        night_last_key = None
        if t0 >= next_load:
            hist = load_history(db, bucket_min, graph_cols)
            next_load = t0 + 30
        day_snap = day_totals(db, state.snapshot_kwh())
        draw_dashboard(img, d, (f_big, f_small, f_axis), cfg['colors'],
                       layout, state.snapshot(), day_snap,
                       state.snapshot_battery(), hist, bucket_min)
        canvas.SetImage(img)
        canvas = matrix.SwapOnVSync(canvas)
        dt = time.time() - t0
        if dt < refresh:
            time.sleep(refresh - dt)


if __name__ == "__main__":
    main()
