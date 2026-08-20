"""Lecture du tactile et reconnaissance de gestes, sans dependance externe.

Le controleur ft5x06 est expose comme un peripherique d'entree Linux standard,
on lit donc directement les evenements evdev plutot que d'ajouter python-evdev
pour quelques lignes de struct.
"""
import collections
import glob
import os
import struct
import threading
import time

EV_KEY, EV_ABS = 0x01, 0x03
BTN_TOUCH = 0x14a
ABS_X, ABS_Y = 0x00, 0x01

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
    """Traduit les contacts en gestes : « tap », « left », « right », « up »,
    « down ».

    Le geste est decide au relachement, en comparant le point de depart au
    point d'arrivee : en dessous du seuil c'est un tap, au-dela c'est un
    glissement, dont l'axe dominant donne le sens.

    Les gestes sont empiles dans une file que la boucle d'affichage vide a son
    rythme : elle n'a pas a guetter le tactile, et aucun geste n'est perdu
    entre deux images.
    """

    def __init__(self, device=None, swipe_min=60, debounce=0.25, maxlen=8):
        super().__init__(daemon=True)
        self.device = device or find_device()
        self.swipe_min = swipe_min
        self.debounce = debounce
        self.gestures = collections.deque(maxlen=maxlen)
        self._last_emit = 0.0

    def _emit(self, gesture):
        now = time.time()
        if now - self._last_emit < self.debounce:
            return
        self._last_emit = now
        self.gestures.append(gesture)

    def pop(self):
        """Geste le plus ancien, ou None. Sur, la deque etant atomique."""
        try:
            return self.gestures.popleft()
        except IndexError:
            return None

    def run(self):
        if not self.device:
            return
        while True:
            try:
                with open(self.device, "rb", buffering=0) as f:
                    x = y = x0 = y0 = None
                    down = False
                    while True:
                        data = f.read(_SIZE)
                        if len(data) < _SIZE:
                            break
                        _, _, etype, code, value = struct.unpack(_FMT, data)
                        if etype == EV_ABS:
                            if code == ABS_X:
                                x = value
                            elif code == ABS_Y:
                                y = value
                        elif etype == EV_KEY and code == BTN_TOUCH:
                            if value == 1:
                                down, x0, y0 = True, x, y
                            elif value == 0 and down:
                                down = False
                                if None in (x0, y0, x, y):
                                    self._emit("tap")
                                    continue
                                dx, dy = x - x0, y - y0
                                if max(abs(dx), abs(dy)) < self.swipe_min:
                                    self._emit("tap")
                                elif abs(dx) >= abs(dy):
                                    self._emit("right" if dx > 0 else "left")
                                else:
                                    self._emit("down" if dy > 0 else "up")
            except OSError:
                time.sleep(2)   # peripherique absent ou retire : on reessaie
