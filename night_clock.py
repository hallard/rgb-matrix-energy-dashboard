"""Horloge de nuit facon pendule a LED : couronne de secondes et chiffres en
matrice de points.

Les chiffres sont dessines point par point plutot qu'avec une police : c'est ce
qui donne l'aspect afficheur a LED, et sur une dalle ronde cela evite d'avoir a
choisir une taille de police qui tombe juste.
"""
import math

# Matrice 5x7, un caractere par motif de 7 lignes.
GLYPHS = {
    '0': ("01110", "10001", "10011", "10101", "11001", "10001", "01110"),
    '1': ("00100", "01100", "00100", "00100", "00100", "00100", "01110"),
    '2': ("01110", "10001", "00001", "00010", "00100", "01000", "11111"),
    '3': ("11111", "00010", "00100", "00010", "00001", "10001", "01110"),
    '4': ("00010", "00110", "01010", "10010", "11111", "00010", "00010"),
    '5': ("11111", "10000", "11110", "00001", "00001", "10001", "01110"),
    '6': ("00110", "01000", "10000", "11110", "10001", "10001", "01110"),
    '7': ("11111", "00001", "00010", "00100", "01000", "01000", "01000"),
    '8': ("01110", "10001", "10001", "01110", "10001", "10001", "01110"),
    '9': ("01110", "10001", "10001", "01111", "00001", "00010", "01100"),
    ':': ("00", "00", "11", "00", "11", "00", "00"),
}


def dim(col, factor=0.22):
    """Teinte sourde pour les secondes pas encore ecoulees."""
    return tuple(max(0, int(c * factor)) for c in col)


def draw_ring(d, cx, cy, r, second, col, dot_r=5, mark_gap=15):
    """Couronne de 60 points, ceux deja parcourus dans la minute allumes.

    Les reperes des cinq minutes portent une seconde LED vers l'interieur,
    comme sur la pendule d'origine : ils se reperent alors au premier coup
    d'oeil sans avoir a compter les points.
    """
    for i in range(60):
        a = math.radians(i * 6 - 90)   # depart a midi, sens horaire
        ca, sa = math.cos(a), math.sin(a)
        c = col if i <= second else dim(col)
        x, y = cx + r * ca, cy + r * sa
        d.ellipse((x - dot_r, y - dot_r, x + dot_r, y + dot_r), fill=c)
        if i % 5 == 0:
            rr = r - mark_gap
            x2, y2 = cx + rr * ca, cy + rr * sa
            d.ellipse((x2 - dot_r, y2 - dot_r, x2 + dot_r, y2 + dot_r), fill=c)


def draw_glyphs(d, cx, cy, text, col, pitch=11, dot_r=4):
    """Suite de caracteres en matrice de points, centree sur (cx, cy)."""
    widths = [len(GLYPHS[ch][0]) for ch in text]
    total = sum(widths) + (len(text) - 1)          # une colonne d'espace entre
    x0 = cx - total * pitch / 2
    y0 = cy - 7 * pitch / 2
    for ch, w in zip(text, widths):
        rows = GLYPHS[ch]
        for ry, row in enumerate(rows):
            for rx, cell in enumerate(row):
                if cell == '1':
                    x = x0 + (rx + 0.5) * pitch
                    y = y0 + (ry + 0.5) * pitch
                    d.ellipse((x - dot_r, y - dot_r, x + dot_r, y + dot_r),
                              fill=col)
        x0 += (w + 1) * pitch


def draw_clock(d, cx, cy, r, col, tm):
    """Horloge complete : couronne des secondes et heure au centre."""
    draw_ring(d, cx, cy, r, tm.tm_sec, col)
    draw_glyphs(d, cx, cy, "%02d:%02d" % (tm.tm_hour, tm.tm_min), col)
