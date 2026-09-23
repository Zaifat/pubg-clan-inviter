"""Screen capture + Windows built-in OCR (Windows.Media.Ocr), used to find game UI elements by their text."""
import asyncio
import difflib
from dataclasses import dataclass

from PIL import ImageGrab

from i18n import t
from winrt.windows.globalization import Language
from winrt.windows.graphics.imaging import BitmapPixelFormat, SoftwareBitmap
from winrt.windows.media.ocr import OcrEngine
from winrt.windows.storage.streams import DataWriter

# Cyrillic letters that look like Latin ones: OCR mixes them up, so both sides are compared as "skeletons".
_HOMOGLYPHS = str.maketrans("аврсеёкмнорстухАВЕЁКМНОРСТУХ", "abpceekmhopctyxabeekmhopctyx")


_LOOKALIKES = str.maketrans("l1|!", "iiii")


def skeleton(s):
    """Lowercase, Cyrillic look-alikes as Latin, and I/l/1 as one letter (OCR confuses them)."""
    return s.lower().translate(_HOMOGLYPHS).translate(_LOOKALIKES).replace(" ", "")


def similarity(a, b):
    return difflib.SequenceMatcher(None, skeleton(a), skeleton(b)).ratio()


def fuzzy_contains(text, key, threshold=0.8):
    t, k = skeleton(text), skeleton(key)
    if k in t:
        return True
    if len(t) < len(k) - 2:
        return difflib.SequenceMatcher(None, t, k).ratio() >= threshold
    n = len(k)
    return any(difflib.SequenceMatcher(None, t[i:i + n], k).ratio() >= threshold
               for i in range(0, max(1, len(t) - n + 1)))


@dataclass
class Word:
    text: str
    x: int
    y: int
    w: int
    h: int

    @property
    def cx(self):
        return self.x + self.w // 2

    @property
    def cy(self):
        return self.y + self.h // 2


@dataclass
class Line(Word):
    words: list


class Ocr:
    def __init__(self):
        langs = [l.language_tag for l in OcrEngine.available_recognizer_languages]
        tag = next((t for t in langs if t.lower().startswith("ru")), None)
        self.engine = OcrEngine.try_create_from_language(Language(tag)) if tag else \
            OcrEngine.try_create_from_user_profile_languages()
        if self.engine is None:
            raise RuntimeError(t("e_no_ocr"))
        self.language = tag or "user"
        self.loop = asyncio.new_event_loop()

    def read(self, box):
        """OCR a screen rectangle (left, top, right, bottom). Returns lines in screen coordinates."""
        img = ImageGrab.grab(bbox=box, all_screens=True).convert("RGBA")
        writer = DataWriter()
        writer.write_bytes(img.tobytes("raw", "BGRA"))
        bmp = SoftwareBitmap.create_copy_from_buffer(writer.detach_buffer(), BitmapPixelFormat.BGRA8,
                                                     img.width, img.height)
        result = self.loop.run_until_complete(self.engine.recognize_async(bmp))
        ox, oy = box[0], box[1]
        lines = []
        for ln in result.lines:
            words = [Word(wd.text, ox + int(wd.bounding_rect.x), oy + int(wd.bounding_rect.y),
                          int(wd.bounding_rect.width), int(wd.bounding_rect.height)) for wd in ln.words]
            if not words:
                continue
            x0, y0 = min(w.x for w in words), min(w.y for w in words)
            x1, y1 = max(w.x + w.w for w in words), max(w.y + w.h for w in words)
            lines.append(Line(ln.text, x0, y0, x1 - x0, y1 - y0, words))
        return lines


def find(lines, keys, threshold=0.8):
    """First line containing any of the keys (fuzzy)."""
    for ln in lines:
        if any(fuzzy_contains(ln.text, k, threshold) for k in keys):
            return ln
    return None


def find_word(line, keys, threshold=0.75):
    """Word inside a line that best matches one of the keys; falls back to the line itself."""
    best, score = None, 0.0
    for wd in line.words:
        for k in keys:
            s = difflib.SequenceMatcher(None, skeleton(wd.text), skeleton(k)).ratio()
            if s > score:
                best, score = wd, s
    return best if best and score >= threshold else line
