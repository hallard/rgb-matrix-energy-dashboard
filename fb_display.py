"""Sortie vers le framebuffer Linux, pour le HyperPixel 2.1 Round.

Le pilote historique bcm2708_fb expose /dev/fb0 en 480x480. Le format se lit
dans `fbset -i` : « rgba 8/16,8/8,8/0,8/24 » signifie bleu en poids faible,
donc un ordre BGRA en memoire. PIL sait produire cet ordre directement avec
le mode brut « BGRX », ce qui evite toute conversion en Python.
"""
import glob
import os
import re


class FramebufferDisplay:
    def __init__(self, device="/dev/fb0"):
        self.device = device
        self.width, self.height, self.bpp = self._probe()
        if self.bpp != 32:
            raise RuntimeError(
                "%s est en %d bits par pixel, ce module attend du 32 bits"
                % (device, self.bpp))
        self._fb = open(device, "wb", buffering=0)

    def _probe(self):
        """Lit la geometrie depuis sysfs plutot que d'appeler fbset, qui n'est
        pas installe partout."""
        base = "/sys/class/graphics/%s" % os.path.basename(self.device)
        size = open(base + "/virtual_size").read().strip()
        bpp = int(open(base + "/bits_per_pixel").read().strip())
        w, h = (int(v) for v in re.split(r"[,x]", size)[:2])
        return w, h, bpp

    def show(self, img):
        """Envoie une image PIL RGB de la taille de l'ecran."""
        if img.size != (self.width, self.height):
            raise ValueError("image %s, ecran %dx%d"
                             % (img.size, self.width, self.height))
        self._fb.seek(0)
        self._fb.write(img.tobytes("raw", "BGRX"))

    def blank(self, on):
        """Reveille la console. Sans cela elle se met en veille au bout de dix
        minutes et efface l'affichage."""
        path = "/sys/class/graphics/%s/blank" % os.path.basename(self.device)
        self._write(path, "0" if on else "4")

    @staticmethod
    def _write(path, value):
        try:
            with open(path, "w") as f:
                f.write(value)
            return True
        except OSError:
            return False  # demande les droits root, sans consequence sinon

    @staticmethod
    def _fbcon():
        """Console virtuelle rattachee au framebuffer, si elle existe."""
        for node in sorted(glob.glob("/sys/class/vtconsole/vtcon*")):
            try:
                if "frame buffer" in open(node + "/name").read():
                    return node
            except OSError:
                pass
        return None

    def takeover(self):
        """Detache la console texte du framebuffer.

        Sans cela le curseur clignotant du prompt de login continue de se
        redessiner par-dessus l'affichage : on voit un tiret parasite
        reapparaitre a intervalle regulier, au meme endroit.
        """
        node = self._fbcon()
        self._detached = bool(node) and self._write(node + "/bind", "0")
        return self._detached

    def release(self):
        """Rend le framebuffer a la console texte."""
        if getattr(self, "_detached", False):
            node = self._fbcon()
            if node:
                self._write(node + "/bind", "1")
            self._detached = False

    def close(self):
        self.release()
        try:
            self._fb.close()
        except OSError:
            pass
