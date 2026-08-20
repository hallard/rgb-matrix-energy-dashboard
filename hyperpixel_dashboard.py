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

from energy_dashboard import (State, day_totals, fmt_axis, fmt_kwh, init_db,
                              load_history, logger_thread, mqtt_thread,
                              network_up, nice_step, persist_to_disk)
import night_clock
from fb_display import FramebufferDisplay
from touch import TouchWatcher

SCRIPT_DIR = Path(__file__).resolve().parent


# --------------------------------------------------------------------------
# Primitives
# --------------------------------------------------------------------------

def chord(clip_r, dy):
    """Demi-largeur disponible dans le disque a une distance verticale dy."""
    return math.sqrt(max(1.0, clip_r * clip_r - dy * dy))


def centered(d, cx, y, txt, font, fill):
    b = d.textbbox((0, 0), txt, font=font)
    d.text((cx - (b[2] - b[0]) / 2 - b[0], y), txt, font=font, fill=fill)


def value_unit(d, cx, y, value, unit, f_val, f_unit, c_val, c_unit,
               arrow_up=None):
    """Nombre et unite sur une ligne, l'ensemble centre sur cx.

    L'unite est plus petite et calee sur la base du nombre : les chiffres
    gardent ainsi toute leur taille, ce qui compte sur un cadran ou la valeur
    doit se lire de loin.

    Si arrow_up est fourni, la fleche fait partie du bloc et entre dans le
    centrage. A position fixe, elle se retrouverait collee a la valeur ou
    eloignee selon le nombre de chiffres.
    """
    bv = d.textbbox((0, 0), value, font=f_val)
    bu = d.textbbox((0, 0), unit, font=f_unit)
    wv, wu, gap = bv[2] - bv[0], bu[2] - bu[0], 6
    aw, a_gap = (13, 10) if arrow_up is not None else (0, 0)
    x = cx - (aw + a_gap + wv + gap + wu) / 2
    if arrow_up is not None:
        arrow(d, x + aw / 2, y + (bv[1] + bv[3]) / 2, arrow_up, c_val,
              w=aw, h=16)
        x += aw + a_gap
    d.text((x - bv[0], y), value, font=f_val, fill=c_val)
    d.text((x + wv + gap - bu[0], y + (bv[3] - bv[1]) - (bu[3] - bu[1])),
           unit, font=f_unit, fill=c_unit)


def arrow(d, cx, cy, up, col, w=11, h=13):
    """Montante pour l'export, descendante pour l'import."""
    pts = ([(cx, cy - h / 2), (cx - w / 2, cy + h / 2), (cx + w / 2, cy + h / 2)]
           if up else
           [(cx, cy + h / 2), (cx - w / 2, cy - h / 2), (cx + w / 2, cy - h / 2)])
    d.polygon(pts, fill=col)


def battery_color(soc, colors):
    """Trois paliers plutot qu'un seuil unique : vert au-dessus de 60 %,
    ambre entre 20 et 60, rouge en dessous. Reprend la palette du reste du
    tableau de bord."""
    if soc is None:
        return colors['label']
    if soc > 60:
        return colors['exp']
    if soc > 20:
        return colors['batt']
    return colors['imp']


def draw_flow(d, cx, top, h, charging, col, phase, n=3, w=15, ch=9, gap=5):
    """Chevrons animes indiquant le sens du courant batterie.

    Ils defilent vers le haut en charge, vers le bas en decharge : un seul est
    allume a la fois, les autres restent en teinte sourde. A 4 images par
    seconde le defilement se lit bien sans clignoter.
    """
    total = n * ch + (n - 1) * gap
    y0 = top + (h - total) / 2
    lit = (n - 1 - phase) if charging else phase
    for i in range(n):
        y = y0 + i * (ch + gap)
        c = col if i == lit else tuple(v // 4 for v in col)
        pts = ([(cx, y), (cx - w / 2, y + ch), (cx + w / 2, y + ch)] if charging
               else [(cx, y + ch), (cx - w / 2, y), (cx + w / 2, y)])
        d.polygon(pts, fill=c)


def battery_color(soc, colors):
    """Trois paliers plutot qu'un seuil unique : vert au-dessus de 60 %,
    ambre entre 20 et 60, rouge en dessous. Reprend la palette du reste du
    tableau de bord."""
    if soc is None:
        return colors['label']
    if soc > 60:
        return colors['exp']
    if soc > 20:
        return colors['batt']
    return colors['imp']


def draw_battery(d, cx, top, soc, power, colors, font, f_unit, phase=0,
                 w=30, h=52):
    """Icone batterie verticale : chevrons de sens a gauche, corps au centre,
    pourcentage et puissance empiles a droite. L'ensemble est centre sur cx.

    Verticale parce que le remplissage se lit alors comme un niveau, ce qui
    est plus immediat qu'une barre horizontale.

    Deux couleurs distinctes, car elles disent deux choses differentes : le
    niveau colore l'icone et le pourcentage, le sens du courant colore les
    chevrons et la puissance. Une batterie pleine en decharge affichait
    autrement une puissance verte, ce qui pretait a confusion.
    """
    lvl_col = battery_color(soc, colors)
    charging = power is not None and power < 0
    flow_col = colors['exp'] if charging else colors['batt']

    l1 = "--" if soc is None else "%d%%" % round(soc)
    # Le signe de `power` (positif = decharge) sert a orienter les chevrons,
    # il n'est pas affiche : la fleche dit deja le sens.
    num = "%d" % round(abs(power)) if power else ""

    b1 = d.textbbox((0, 0), l1, font=font)
    bn = d.textbbox((0, 0), num, font=font) if num else (0, 0, 0, 0)
    bu = d.textbbox((0, 0), "W", font=f_unit) if num else (0, 0, 0, 0)
    u_gap = 5
    w_line2 = (bn[2] - bn[0]) + u_gap + (bu[2] - bu[0]) if num else 0
    tw = max(b1[2] - b1[0], w_line2)
    fw, f_gap = (15, 10) if power else (0, 0)   # largeur et marge des chevrons
    gap = 14
    x = cx - (fw + f_gap + w + gap + tw) / 2

    if power:
        draw_flow(d, x + fw / 2, top, h, charging, flow_col, phase)
        x += fw + f_gap

    nub_h = 5
    d.rounded_rectangle((x + w * 0.32, top, x + w * 0.68, top + nub_h), 2,
                        fill=colors['label'])
    d.rounded_rectangle((x, top + nub_h, x + w, top + h), 4,
                        outline=colors['label'], width=2)
    if soc is not None:
        inner_h = (h - nub_h - 8) * max(0.0, min(1.0, soc / 100.0))
        if inner_h > 2:
            d.rounded_rectangle((x + 4, top + h - 4 - inner_h, x + w - 4,
                                 top + h - 4), 2, fill=lvl_col)

    tx = x + w + gap
    if num:
        d.text((tx - b1[0], top + 2), l1, font=font, fill=lvl_col)
        y2 = top + h / 2 + 1
        d.text((tx - bn[0], y2), num, font=font, fill=flow_col)
        # Unite plus petite et grise, calee sur la base du nombre, comme pour
        # les valeurs maison, solaire et reseau.
        d.text((tx + (bn[2] - bn[0]) + u_gap - bu[0],
                y2 + (bn[3] - bn[1]) - (bu[3] - bu[1])),
               "W", font=f_unit, fill=colors['label'])
    else:
        d.text((tx - b1[0], top + h / 2 - (b1[3] - b1[1]) / 2 - b1[1]), l1,
               font=font, fill=lvl_col)


def draw_chart(img, d, cx, cy, base_y, height, half_w, pv, use, colors, font,
               clip_r, axis_w=38):
    """Historique : production et consommation, toutes deux en aires pleines.

    Une bande de `axis_w` est reservee a gauche pour les graduations : dessinees
    par-dessus les aires elles etaient illisibles, le trace commence donc apres.

    Les aires sont peintes sur un calque RGBA puis composees en transparence.
    Dessinees directement l'une sur l'autre, la seconde masquerait la premiere
    partout ou elle est plus haute. Le calque se limite a la bande du graphique :
    composer 480x480 a chaque image serait inutilement couteux sur un ARMv6.

    Chaque trait est borne a la corde du cercle a sa propre hauteur : sur un
    ecran rond la largeur utile diminue des qu'on s'ecarte du centre, et un
    trace a largeur constante deborderait de la dalle.
    """
    vals = [abs(v) for serie in (pv, use) for v in (serie or []) if v]
    if not vals:
        return
    peak = max(vals)
    step = nice_step(peak)
    top = max(peak, step)

    def half_at(y):
        return min(half_w, chord(clip_r, abs(y - cy)) - 8)

    hw_base = half_at(base_y)
    plot_l = cx - hw_base + axis_w
    plot_r = cx + hw_base

    level = step
    while level <= top + 1e-6:
        y = base_y - (level / top) * height
        hw = half_at(y)
        x_end = min(plot_r, cx + hw)
        if x_end > plot_l:
            d.line((plot_l, y, x_end, y), fill=colors['grid'], width=1)
        txt = fmt_axis(level)
        b = d.textbbox((0, 0), txt, font=font)
        d.text((plot_l - 6 - (b[2] - b[0]) - b[0], y - (b[3] - b[1]) / 2 - b[1]),
               txt, font=font, fill=colors['label'])
        level += step

    d.line((plot_l, base_y, plot_r, base_y), fill=colors['label'], width=1)

    x0, y0 = int(plot_l) - 2, int(base_y - height) - 2
    x1, y1 = int(plot_r) + 2, int(base_y) + 2
    layer = Image.new('RGBA', (x1 - x0, y1 - y0), (0, 0, 0, 0))
    ld = ImageDraw.Draw(layer)

    def area(serie, rgba):
        serie = list(serie or [])
        if len(serie) < 2:
            return
        stepx = (plot_r - plot_l) / (len(serie) - 1)
        pts = []
        for i, v in enumerate(serie):
            y = base_y - (abs(v) / top) * height
            hw = half_at(y)
            x = min(max(plot_l + i * stepx, plot_l), min(plot_r, cx + hw))
            pts.append((x - x0, y - y0))
        ld.polygon(pts + [(pts[-1][0], base_y - y0), (pts[0][0], base_y - y0)],
                   fill=rgba)

    area(pv, colors['pv_area'])
    area(use, colors['use_area'])
    img.paste(Image.alpha_composite(
        img.crop((x0, y0, x1, y1)).convert('RGBA'), layer).convert('RGB'),
        (x0, y0))


def draw_night_tick(d, cx, cy, r, col, second):
    """Point unique tournant sur le pourtour, comme une trotteuse.

    Un tour par minute, un pas par seconde : la position indique donc aussi
    l'heure a la seconde pres. Remplace le clignotement fixe de la version
    matrice, qui n'a pas la resolution pour ce genre de mouvement.

    Depart a midi et sens horaire, d'ou le decalage de 90 degres : les angles
    sont comptes depuis 3 heures.
    """
    a = math.radians(second * 6 - 90)   # 6 degres par seconde
    x, y = cx + r * math.cos(a), cy + r * math.sin(a)
    d.ellipse((x - 5, y - 5, x + 5, y + 5), fill=col)


# --------------------------------------------------------------------------
# Mise en page# --------------------------------------------------------------------------
# Mise en page
# --------------------------------------------------------------------------

def draw_round(img, d, fonts, colors, snap, day, batt, hist, cfg,
               phase=0):
    """Sans anneaux de jauge : les valeurs sont deja lisibles en chiffres, et
    la bordure recuperee profite au graphique, qui est l'element principal."""
    f_huge, f_big, f_mid, f_small, f_tiny, f_bottom = fonts
    cx = cy = img.size[0] // 2
    R = img.size[0] // 2
    usable = R - 8

    pv, use, grid = (v or 0 for v in snap)
    day_prod, day_use, day_exp = day
    soc, bpower, _binput = batt

    d.rectangle((0, 0, img.size[0], img.size[1]), fill=colors['bg'])

    value_unit(d, cx, cy - 208, "%d" % round(use), "W", f_huge, f_mid,
               colors['use'], colors['label'])

    imported = grid >= 0
    gcol = colors['imp'] if imported else colors['exp']
    centered(d, cx - 100, cy - 144, "SOLAIRE", f_tiny, colors['label'])
    value_unit(d, cx - 100, cy - 126, "%d" % round(pv), "W", f_big, f_small,
               colors['pv'], colors['label'])
    centered(d, cx + 100, cy - 144, "RESEAU", f_tiny, colors['label'])
    value_unit(d, cx + 100, cy - 126, "%d" % round(abs(grid)), "W",
               f_big, f_small, gcol, colors['label'], arrow_up=not imported)

    draw_chart(img, d, cx, cy, cy + 92, 166, 205, hist[0], hist[1],
               colors, f_tiny, usable)

    for i, (name, val, col) in enumerate(
            (("Conso", day_use, colors['use']),
             ("Prod", day_prod, colors['pv']),
             ("Export", day_exp, colors['exp']))):
        # Ecartement calcule sur la corde a cette hauteur : environ 203 px de
        # demi-largeur, pour trois blocs de ~96 px avec leur unite.
        x = cx + (i - 1) * 130
        centered(d, x, cy + 94, name, f_tiny, colors['label'])
        # Meme traitement que les valeurs du haut : unite plus petite et grise,
        # calee sur la base du nombre.
        value_unit(d, x, cy + 112,
                   fmt_kwh(val) if val is not None else "--", "kWh",
                   f_bottom, f_tiny, col, colors['label'])

    # Batterie au plus bas : le bloc fait 144 px de large, or a cette hauteur
    # la corde se referme vite. Mesure faite, y + 166 laisse 1 px de marge sous
    # le texte avant le bord utile du disque.
    draw_battery(d, cx, cy + 166, soc, bpower, colors, f_bottom,
                 f_small, phase)

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
    hist_cols = dcfg.get('history_cols', 288)
    # Retention et fenetre affichee sont independantes : l'ecran montre 24 h,
    # mais la base en garde plusieurs jours pour pouvoir naviguer au tactile.
    # load_history() ne lit que la fenetre demandee, le reste est ignore.
    retain_sec = int(cfg['history'].get('retain_days', 7) * 86400)

    def _flush(*_):
        persist_to_disk(db, str(db_path))
        sys.exit(0)   # atexit rend le framebuffer a la console
    atexit.register(lambda: persist_to_disk(db, str(db_path)))
    signal.signal(signal.SIGTERM, _flush)
    signal.signal(signal.SIGINT, _flush)

    fb = FramebufferDisplay(dcfg.get('device', '/dev/fb0'))
    fb.blank(True)
    if dcfg.get('take_over_console', True):
        logging.info("console texte detachee : %s", fb.takeover())
    atexit.register(fb.release)
    atexit.register(lambda: fb.backlight(True))
    logging.info("framebuffer %dx%d", fb.width, fb.height)

    fcfg = dcfg['fonts']
    fonts = tuple(load_font(fcfg['path'], fcfg[k])
                  for k in ('huge', 'big', 'mid', 'small', 'tiny', 'bottom'))

    img = Image.new('RGB', (fb.width, fb.height))
    d = ImageDraw.Draw(img)

    threading.Thread(target=mqtt_thread, args=(cfg, state), daemon=True).start()
    threading.Thread(target=logger_thread,
                     args=(db, state, str(db_path), sample_sec, backup_sec,
                           retain_sec),
                     daemon=True).start()

    colors = {k: tuple(v) for k, v in cfg['colors'].items()}
    refresh = 1.0 / dcfg.get('refresh_hz', 4)
    hist = ([], [], [])
    next_load = 0.0
    layout = {'scale_w': dcfg.get('scale_w', 3000)}

    # Mode nuit : deux presentations, l'appui sur la dalle bascule de l'une a
    # l'autre. Le rouge signale que le reseau est tombe, comme sur la matrice.
    night_key = None
    night_style = dcfg.get('night_style', 'clock')
    touch = TouchWatcher()
    touch.start()
    last_taps = touch.taps
    logging.info("tactile : %s", touch.device or "absent")
    bl_off = dcfg.get('night_backlight_off', False)
    night_r = fb.width / 2 - 16

    while True:
        t0 = time.time()

        if not state.display_on:
            # Ecran coupe par le topic MQTT. Les fils MQTT et journalisation
            # continuent : l'historique reste enregistre pendant la nuit.
            if touch.taps != last_taps:
                last_taps = touch.taps
                night_style = 'dot' if night_style == 'clock' else 'clock'
                night_key = None   # force le redessin immediat
                logging.info("mode nuit -> %s", night_style)

            net = network_up()
            tm = time.localtime(t0)
            key = (tm.tm_sec, net, bl_off, night_style)
            if key != night_key:
                if bl_off:
                    # Dalle vraiment eteinte : rien ne serait visible.
                    fb.backlight(False)
                else:
                    d.rectangle((0, 0, fb.width, fb.height), fill=colors['bg'])
                    if night_style == 'clock':
                        # Toujours rouge : c'est la couleur de la pendule
                        # d'origine. L'etat du reseau reste signale par l'autre
                        # presentation, accessible d'un appui.
                        night_clock.draw_clock(d, fb.width / 2, fb.height / 2,
                                               night_r, colors['clock'], tm)
                    else:
                        draw_night_tick(d, fb.width / 2, fb.height / 2, night_r,
                                        colors['exp'] if net else colors['imp'],
                                        tm.tm_sec)
                    fb.show(img)
                night_key = key
            # Une image par seconde : on echantillonne plus finement pour ne
            # pas decaler le pas de la seconde.
            time.sleep(0.1)
            continue

        if night_key is not None:
            night_key = None
            if bl_off:
                fb.backlight(True)

        if t0 >= next_load:
            hist = load_history(db, bucket_min, hist_cols)
            next_load = t0 + 30
        draw_round(img, d, fonts, colors, state.snapshot(),
                   day_totals(db, state.snapshot_kwh()),
                   state.snapshot_battery(), hist, layout,
                   int(t0 * 3) % 3)
        fb.show(img)
        dt = time.time() - t0
        if dt < refresh:
            time.sleep(refresh - dt)


if __name__ == "__main__":
    main()
