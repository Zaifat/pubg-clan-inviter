"""Finds game UI icons on screen by template matching (language independent).

Templates are cut from a 2560x1440 client; they are scaled by client height / 1440 before matching.
"""
import os
import sys

import cv2
import numpy as np
from PIL import ImageGrab

REF_H = 1440
_BASE = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
TPL_DIR = os.path.join(_BASE, "assets", "tpl")

THRESHOLD = {"search": 0.78, "clear": 0.78, "invite": 0.75, "clan": 0.72, "toast": 0.72, "gear": 0.75}


class Match:
    __slots__ = ("name", "x", "y", "w", "h", "score")

    def __init__(self, name, x, y, w, h, score):
        self.name, self.x, self.y, self.w, self.h, self.score = name, x, y, w, h, score

    @property
    def cx(self):
        return self.x + self.w // 2

    @property
    def cy(self):
        return self.y + self.h // 2

    def __repr__(self):
        return f"{self.name}@({self.cx},{self.cy}) {self.score:.2f}"


class Vision:
    def __init__(self):
        self.templates = {}
        for f in os.listdir(TPL_DIR):
            if f.endswith(".png"):
                img = cv2.imdecode(np.fromfile(os.path.join(TPL_DIR, f), np.uint8), cv2.IMREAD_GRAYSCALE)
                self.templates[f[:-4]] = img
        self._cache = {}

    @staticmethod
    def grab(box):
        img = ImageGrab.grab(bbox=box, all_screens=True).convert("L")
        return np.asarray(img)

    def _scaled(self, name, scale):
        key = (name, round(scale, 3))
        if key not in self._cache:
            t = self.templates[name]
            w, h = max(6, round(t.shape[1] * scale)), max(6, round(t.shape[0] * scale))
            self._cache[key] = cv2.resize(t, (w, h), interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_CUBIC)
        return self._cache[key]

    def find_all(self, name, box, client_h, threshold=None, gray=None):
        """All matches of a template inside a screen box, best first (screen coordinates)."""
        threshold = threshold or THRESHOLD.get(name, 0.78)
        img = gray if gray is not None else self.grab(box)
        base = client_h / REF_H
        found = []
        for k in (1.0, 0.92, 1.08, 0.85, 1.15):
            t = self._scaled(name, base * k)
            if t.shape[0] > img.shape[0] or t.shape[1] > img.shape[1]:
                continue
            res = cv2.matchTemplate(img, t, cv2.TM_CCOEFF_NORMED)
            ys, xs = np.where(res >= threshold)
            for x, y in zip(xs, ys):
                found.append(Match(name, box[0] + int(x), box[1] + int(y), t.shape[1], t.shape[0], float(res[y, x])))
            if found and k == 1.0:
                break
        found.sort(key=lambda m: -m.score)
        result = []
        for m in found:  # drop overlapping duplicates
            if all(abs(m.cx - r.cx) > r.w // 2 or abs(m.cy - r.cy) > r.h // 2 for r in result):
                result.append(m)
        return result

    def find(self, name, box, client_h, threshold=None, gray=None):
        ms = self.find_all(name, box, client_h, threshold, gray)
        return ms[0] if ms else None
