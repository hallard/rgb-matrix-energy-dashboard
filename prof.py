import math, time, yaml
from PIL import Image, ImageDraw
import hyperpixel_dashboard as H

cfg = yaml.safe_load(open("config_hyperpixel.yaml").read())
f = cfg["display"]["fonts"]
fonts = tuple(H.load_font(f["path"], f[k])
              for k in ("huge", "big", "mid", "small", "tiny", "bottom"))
colors = {k: tuple(v) for k, v in cfg["colors"].items()}
f_huge, f_big, f_mid, f_small, f_tiny, f_bottom = fonts

n = 288
pv = [max(0, 2600 * math.sin(max(0, (i - 70) / 150.0) * math.pi)) for i in range(n)]
use = [900 + 700 * abs(math.sin(i / 14.0)) for i in range(n)]
img = Image.new("RGB", (480, 480))
d = ImageDraw.Draw(img)


def bench(fn, k=20):
    fn()
    t = time.time()
    for _ in range(k):
        fn()
    return (time.time() - t) / k * 1000


parts = [
    ("fond plein ecran", lambda: d.rectangle((0, 0, 480, 480), fill=(0, 0, 0))),
    ("graphique", lambda: H.draw_chart(img, d, 240, 240, 332, 166, 205, pv, use,
                                       colors, f_tiny, 232)),
    ("masque circulaire", lambda: d.ellipse((-3, -3, 483, 483),
                                            outline=(0, 0, 0), width=8)),
    ("valeur maison", lambda: H.value_unit(d, 240, 52, "1640", "W", f_huge,
                                           f_mid, (0, 180, 255), (130, 130, 130))),
    ("batterie complete", lambda: H.draw_battery(d, 240, 406, 88, 640, colors,
                                                 f_bottom, f_small, 1)),
]
for name, fn in parts:
    print("%-20s %6.1f ms" % (name, bench(fn)))
