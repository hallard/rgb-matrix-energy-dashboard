#!/usr/bin/env python3
"""Tableau de bord energie pour le Pimoroni HyperPixel 2.1 Round (480x480).

Reprend telle quelle la couche donnees de energy_dashboard.py -- MQTT,
historique SQLite, etat partage -- et remplace le rendu 128x64 de la matrice
LED par une mise en page circulaire ecrite directement dans le framebuffer.

Usage: python3 hyperpixel_dashboard.py config_hyperpixel.yaml
"""
import atexit
import logging
import math
import signal
import sys
import threading
import time
from pathlib import Path

import yaml
from PIL import Image, ImageDraw, ImageFont

from energy_dashboard import (State, day_totals, fmt_kwh, init_db, load_history,
                              logger_thread, mqtt_thread, persist_to_disk)
from fb_display import FramebufferDisplay

SCRIPT_DIR = Path(__file__).resolve().parent


# --------------------------------------------------------------------------
# Primitives circulaires
# --------------------------------------------------------------------------

def ring(d, cx, cy, r, width, frac, col, bg):
    """Arc de jauge partant de midi, dans le sens horaire.

    PIL compte les angles depuis 3 heures ; le decalage de 90 degres donne la
    convention attendue sur un cadran.
    """
    box = (cx - r, cy - r, cx + r, cy + r)
    d.arc(box, 0, 360, fill=bg, width=width)
    frac = max(0.0, min(1.0, frac))
    if frac > 0.002:
        d.arc(box, -90, -90 + 360 * frac, fill=col, width=width)


def centered(d, cx, y, txt, font, fill):
    b = d.textbbox((0, 0), txt, font=font)
    d.text((cx - (b[2] - b[0]) / 2 - b[0], y), txt, font=font, fill=fill)


def value_unit(d, cx, y, value, unit, f_val, f_unit, c_val, c_unit):
    """Nombre et unite sur une ligne, l'ensemble centre sur cx.

    L'unite est plus petite et calee sur la base du nombre : les chiffres
    gardent ainsi toute leur taille, ce qui compte sur un cadran ou la valeur
    doit se lire de loin.
    """
    bv = d.textbbox((0, 0), value, font=f_val)
    bu = d.textbbox((0, 0), unit, font=f_unit)
    wv, wu, gap = bv[2] - bv[0], bu[2] - bu[0], 6
    x = cx - (wv + gap + wu) / 2
    d.text((x - bv[0], y), value, font=f_val, fill=c_val)
    d.text((x + wv + gap - bu[0], y + (bv[3] - bv[1]) - (bu[3] - bu[1])),
           unit, font=f_unit, fill=c_unit)


def arrow(d, cx, cy, up, col, w=13, h=15):
    """Montante pour l'export, descendante pour l'import."""
    pts = ([(cx, cy - h / 2), (cx - w / 2, cy + h / 2), (cx + w / 2, cy + h / 2)]
           if up else
           [(cx, cy + h / 2), (cx - w / 2, cy - h / 2), (cx + w / 2, cy - h / 2)])
    d.polygon(pts, fill=col)


def sparkline(d, cx, cy, half_w, base_y, height, layers, clip_r):
    """Historique borne au disque.

    Chaque couche precise son rendu : « fill » pour une aire, « line » pour une
    courbe. Deux aires superposees se masquent l'une l'autre ; une aire plus une
    courbe reste lisible.

    Sur un ecran rond la largeur utilisable depend de la hauteur : on la ramene
    a la corde du cercle a ce niveau plutot que de laisser le trace sortir.
    """
    vals = [abs(v) for serie, _, _ in layers for v in (serie or []) if v]
    if not vals:
        return
    dy = abs(base_y - cy)
    half_w = min(half_w, math.sqrt(max(1.0, clip_r * clip_r - dy * dy)) - 10)
    peak = max(vals) or 1.0
    for serie, col, mode in layers:
        serie = list(serie or [])
        if len(serie) < 2:
            continue
        step = (2 * half_w) / (len(serie) - 1)
        pts = [(cx - half_w + i * step, base_y - (abs(v) / peak) * height)
               for i, v in enumerate(serie)]
        if mode == "fill":
            d.polygon(pts + [(cx + half_w, base_y), (cx - half_w, base_y)],
                      fill=col)
        else:
            d.line(pts, fill=col, width=2, joint="curve")


# --------------------------------------------------------------------------
# Mise en page
# --------------------------------------------------------------------------

def draw_round(img, d, fonts, colors, snap, day, batt, hist, page, cfg):
    f_huge, f_big, f_mid, f_small, f_tiny = fonts
    cx = cy = img.size[0] // 2
    R = img.size[0] // 2

    pv, use, grid = (v or 0 for v in snap)
    day_prod, day_use, day_exp = day
    soc, bpower, _binput = batt

    d.rectangle((0, 0, img.size[0], img.size[1]), fill=colors['bg'])

    peak = max(cfg['scale_w'], use, pv, abs(grid))
    ring(d, cx, cy, R - 16, 22, use / peak, colors['use'], colors['ring_bg'])
    ring(d, cx, cy, R - 46, 14, pv / peak, colors['pv'], colors['ring_bg'])

    # Rythme vertical : la police « huge » fait 58 px, il faut donc au moins
    # 70 px entre le haut d'une valeur et l'etiquette suivante, sinon les
    # etiquettes viennent se poser sur les chiffres.
    centered(d, cx, cy - 158, "MAISON", f_tiny, colors['label'])
    value_unit(d, cx, cy - 140, "%d" % round(use), "W", f_huge, f_mid,
               colors['use'], colors['label'])

    centered(d, cx, cy - 64, "SOLAIRE", f_tiny, colors['label'])
    value_unit(d, cx, cy - 46, "%d" % round(pv), "W", f_big, f_small,
               colors['pv'], colors['label'])

    # Reseau : la fleche donne le sens, la couleur le confirme.
    imported = grid >= 0
    gcol = colors['imp'] if imported else colors['exp']
    centered(d, cx, cy + 4, "RESEAU", f_tiny, colors['label'])
    value_unit(d, cx + 12, cy + 22, "%d" % round(abs(grid)), "W",
               f_big, f_small, gcol, colors['label'])
    arrow(d, cx - 56, cy + 40, not imported, gcol)

    # Historique : production en jaune, consommation en bleu.
    sparkline(d, cx, cy, 150, cy + 104, 34,
              [(hist[0], colors['pv_fill'], 'fill'),
               (hist[1], colors['use'], 'line')],
              R - 24)

    # Bandeau bas alterne, comme sur la version matrice.
    if page == 0 and soc is not None:
        bcol = colors['batt_ok'] if soc > 20 else colors['batt_low']
        bar_w, bar_h = 156, 11
        x0, y0 = cx - bar_w // 2, cy + 122
        d.rounded_rectangle((x0, y0, x0 + bar_w, y0 + bar_h), 5,
                            outline=colors['label'], width=1)
        fw = int(bar_w * max(0.0, min(1.0, soc / 100.0)))
        if fw > 2:
            d.rounded_rectangle((x0, y0, x0 + fw, y0 + bar_h), 5, fill=bcol)
        txt = "%d%%" % round(soc)
        if bpower:
            # Signe : positif = decharge, negatif = charge.
            txt += "   %+d W" % round(bpower)
        centered(d, cx, cy + 142, txt, f_small, bcol)
    else:
        for i, (name, val, col) in enumerate(
                (("Conso", day_use, colors['use']),
                 ("Prod", day_prod, colors['pv']),
                 ("Export", day_exp, colors['exp']))):
            x = cx + (i - 1) * 74
            centered(d, x, cy + 118, name, f_tiny, colors['label'])
            centered(d, x, cy + 136,
                     fmt_kwh(val) if val is not None else "--", f_small, col)

    # La dalle est ronde : ce qui deborde du disque serait dessine dans le vide.
    d.ellipse((-3, -3, img.size[0] + 3, img.size[1] + 3),
              outline=colors['bg'], width=8)


# --------------------------------------------------------------------------

def load_font(spec, size):
    return (ImageFont.load(spec) if spec.endswith('.pil')
            else ImageFont.truetype(spec, size))


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
    cfg_path = (Path(sys.argv[1]) if len(sys.argv) > 1
                else SCRIPT_DIR / "config_hyperpixel.yaml")
    cfg = yaml.safe_load(cfg_path.read_text())
    dcfg = cfg['display']

    state = State()
    db_path = Path(cfg['history']['db_path'])
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = init_db(str(db_path))
    bucket_min = cfg['history']['bucket_minutes']
    sample_sec = cfg['history'].get('sample_minutes', bucket_min) * 60
    backup_sec = cfg['history'].get('backup_minutes', 60) * 60
    hist_cols = dcfg.get('history_cols', 120)
    window_sec = bucket_min * 60 * hist_cols

    def _flush(*_):
        persist_to_disk(db, str(db_path))
        sys.exit(0)
    atexit.register(lambda: persist_to_disk(db, str(db_path)))
    signal.signal(signal.SIGTERM, _flush)
    signal.signal(signal.SIGINT, _flush)

    fb = FramebufferDisplay(dcfg.get('device', '/dev/fb0'))
    fb.blank(True)
    logging.info("framebuffer %dx%d", fb.width, fb.height)

    fcfg = dcfg['fonts']
    fonts = tuple(load_font(fcfg['path'], fcfg[k])
                  for k in ('huge', 'big', 'mid', 'small', 'tiny'))

    img = Image.new('RGB', (fb.width, fb.height))
    d = ImageDraw.Draw(img)

    threading.Thread(target=mqtt_thread, args=(cfg, state), daemon=True).start()
    threading.Thread(target=logger_thread,
                     args=(db, state, str(db_path), sample_sec, backup_sec,
                           window_sec),
                     daemon=True).start()

    colors = {k: tuple(v) for k, v in cfg['colors'].items()}
    refresh = 1.0 / dcfg.get('refresh_hz', 4)
    page_sec = dcfg.get('page_seconds', 5)
    hist = ([], [], [])
    next_load = 0.0
    layout = {'scale_w': dcfg.get('scale_w', 3000)}

    while True:
        t0 = time.time()
        if t0 >= next_load:
            hist = load_history(db, bucket_min, hist_cols)
            next_load = t0 + 30
        draw_round(img, d, fonts, colors, state.snapshot(),
                   day_totals(db, state.snapshot_kwh()),
                   state.snapshot_battery(), hist,
                   int(t0 / page_sec) % 2, layout)
        fb.show(img)
        dt = time.time() - t0
        if dt < refresh:
            time.sleep(refresh - dt)


if __name__ == "__main__":
    main()
