"""Interface languages: one JSON file per language in ui/lang/. The default follows the Windows UI language."""
import ctypes
import json
import locale
import os
import sys

_BASE = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
LANG_DIR = os.path.join(_BASE, "ui", "lang")
FALLBACK = "en"

_strings = {}
_fallback = {}
current = FALLBACK


def _load(code):
    try:
        with open(os.path.join(LANG_DIR, f"{code}.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def available():
    """{code: native name} of every language file shipped with the program."""
    langs = {}
    for f in sorted(os.listdir(LANG_DIR)):
        if f.endswith(".json"):
            langs[f[:-5]] = _load(f[:-5]).get("_name", f[:-5])
    return langs


def windows_language():
    try:
        lcid = ctypes.windll.kernel32.GetUserDefaultUILanguage()
        name = locale.windows_locale.get(lcid, "")
    except Exception:
        name = ""
    return name.split("_")[0].lower()


def resolve(setting):
    """Language code for a settings value ("auto" or a code) among the available files."""
    langs = available()
    code = windows_language() if setting in (None, "", "auto") else setting
    return code if code in langs else FALLBACK


def set_language(setting):
    global _strings, _fallback, current
    current = resolve(setting)
    _fallback = _load(FALLBACK)
    _strings = _load(current)


def strings():
    return {**_fallback, **_strings}


def t(key, **kw):
    text = _strings.get(key) or _fallback.get(key) or key
    return text.format(**kw) if kw else text


set_language("auto")
