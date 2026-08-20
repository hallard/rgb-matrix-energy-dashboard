"""Lecture du tactile, sans dependance externe.

Le controleur ft5x06 est expose comme un peripherique d'entree Linux standard,
on lit donc directement les evenements evdev plutot que d'ajouter python-evdev
pour trois lignes de struct.
"""
import glob
import os
import struct
import threading
import time

EV_KEY = 0x01
BTN_TOUCH = 0x14a

# struct input_event : deux longs (timeval) puis type, code, valeur. La taille
# du long depend de l'architecture, d'ou le calcul plutot qu'une constante.
_FMT = "llHHi"
_SIZE = struct.calcsize(_FMT)


def find_device(name_hint="ft5x06"):
    """Chemin du peripherique d'entree correspondant au tactile."""
    for path in sorted(glob.glob("/sys/class/input/event*")):
        try:
            with open(os.path.join(path, "device", "name")) as f:
                if name_hint in f.read().lower():
                    return "/dev/input/" + os.path.basename(path)
        except OSError:
            continue
    return None


class TouchWatcher(threading.Thread):
    """Compte les appuis sur la dalle.

    On ne s'interesse qu'a l'appui (BTN_TOUCH a 1), pas aux coordonnees : le
    seul geste utilise est le tap. Un delai de garde evite qu'un contact un peu
    tremblant ne compte double.
    """

    def __init__(self, device=None, debounce=0.4):
        super().__init__(daemon=True)
        self.device = device or find_device()
        self.debounce = debounce
        self.taps = 0
        self._last = 0.0

    def run(self):
        if not self.device:
            return
        while True:
            try:
                with open(self.device, "rb", buffering=0) as f:
                    while True:
                        data = f.read(_SIZE)
                        if len(data) < _SIZE:
                            break
                        _, _, etype, code, value = struct.unpack(_FMT, data)
                        if etype == EV_KEY and code == BTN_TOUCH and value == 1:
                            now = time.time()
                            if now - self._last >= self.debounce:
                                self._last = now
                                self.taps += 1
            except OSError:
                time.sleep(2)   # peripherique absent ou retire : on reessaie
