"""PUBG Clan Inviter — collects clanless players from matches and invites them to your clan in-game."""
import collections
import ctypes
import json
import os
import subprocess
import sys
import threading
import time
import webbrowser
import winreg
from datetime import datetime

import webview

import i18n
import winput as w
from collector import DAY, ApiError, Collector, PubgApi, Store, check_clans
from game import GameBot, Stopped
from i18n import t

APP_VERSION = "2.5.1"
APP_TITLE = "PUBG Clan Inviter"
APP_ID = "PUBG_Inviter"
STEAM_URL = "steam://rungameid/578080"
API_KEY_URL = "https://developer.pubg.com/"

EXE_DIR = os.path.dirname(sys.executable if getattr(sys, "frozen", False) else os.path.abspath(__file__))
RES_DIR = getattr(sys, "_MEIPASS", EXE_DIR)
UI_DIR = os.path.join(RES_DIR, "ui")
ICON_PNG = os.path.join(RES_DIR, "assets", "app.png")
DATA_DIR = os.path.join(os.environ.get("APPDATA", EXE_DIR), APP_ID)
os.makedirs(DATA_DIR, exist_ok=True)
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
DB_PATH = os.path.join(DATA_DIR, "data.db")
LOG_PATH = os.path.join(DATA_DIR, "log.txt")

DEFAULT_CONFIG = {
    "api_key": "",
    "shard": "steam",
    "source_players": [],
    "collect_interval_min": 15,
    "stale_days": 30,
    "recheck_clan_days": 7,
    "max_matches_per_cycle": 60,
    "collect_running": False,
    "invites_per_run": 100,
    "precheck_before_invite": True,
    "launch_game": True,
    "close_game_when_done": True,
    "autostart": False,
    "game_window_timeout_sec": 300,
    "lobby_timeout_sec": 300,
    "delay_scale": 1.0,
    "jitter_ms": 40,
    "stop_key": "F12",
    "language": "auto",
}

# only these can be changed from the settings window
SETTINGS_KEYS = ["language", "api_key", "source_players", "collect_interval_min", "stale_days", "recheck_clan_days",
                 "precheck_before_invite", "launch_game", "close_game_when_done", "autostart"]


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, encoding="utf-8-sig") as f:
            cfg.update(json.load(f))
    except (OSError, ValueError):
        pass
    return cfg


def save_config(cfg):
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_PATH)


def set_autostart(on):
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0,
                        winreg.KEY_SET_VALUE) as key:
        if on:
            winreg.SetValueEx(key, APP_ID, 0, winreg.REG_SZ, f'"{sys.executable}" --tray')
        else:
            try:
                winreg.DeleteValue(key, APP_ID)
            except FileNotFoundError:
                pass


# ---------------------------------------------------------------- invite run

class InviteRun:
    def __init__(self, app, target):
        self.app = app
        self.cfg = dict(app.cfg)
        self.target = target
        self.stop_flag = threading.Event()
        self.running = True
        self.detail = t("d_preparing")
        self.counts = {"ok": 0, "not_found": 0}

    def check(self):
        if self.stop_flag.is_set() or w.key_pressed(self.cfg.get("stop_key", "F12")):
            self.stop_flag.set()
            raise Stopped()

    def wait(self, sec):
        end = time.monotonic() + sec
        while time.monotonic() < end:
            self.check()
            time.sleep(0.1)

    def candidates(self):
        store, cutoff = self.app.store, int(time.time() - float(self.cfg["stale_days"]) * DAY)
        cands = store.valid(cutoff, self.target * 2 + 30)
        if not self.cfg.get("precheck_before_invite") or not self.cfg.get("api_key"):
            return cands
        self.detail = t("d_precheck")
        good = []
        for i in range(0, len(cands), 10):
            batch = [a for a, _ in cands[i:i + 10]]
            try:
                res, _ = check_clans(self.app.api, store, batch, self.check)
            except ApiError as e:
                self.app.log(t("l_precheck_failed", err=e))
                return cands
            good += [(a, res[a][0]) for a in batch if a in res and not res[a][1]]
            if len(good) >= self.target + 20:
                break
        return good

    def run(self):
        app, cfg = self.app, self.cfg
        finished = False
        try:
            if cfg.get("launch_game", True) and not w.game_running():
                app.log(t("l_launch"))
                os.startfile(STEAM_URL)
            names = self.candidates()
            if not names:
                app.log(t("l_no_valid"))
                return
            app.log(t("l_ready", n=len(names), target=self.target))
            if not w.find_game_window():
                if not cfg.get("launch_game", True):
                    raise RuntimeError(t("e_game_not_running"))
                self.detail = t("d_wait_game")
                end = time.monotonic() + cfg["game_window_timeout_sec"]
                while not w.find_game_window():
                    if time.monotonic() > end:
                        raise RuntimeError(t("e_game_no_start"))
                    self.wait(1)
            self.detail = t("d_wait_lobby")
            app.hide_for_game()
            bot = GameBot(cfg, app.log, self.check)
            bot.bring_to_front()
            bot.open_invite_page(cfg["lobby_timeout_sec"])
            app.log(t("l_page_open"))

            for account_id, nick in names:
                if self.counts["ok"] >= self.target:
                    break
                self.detail = t("d_inviting", nick=nick)
                status, msg = bot.invite(nick)
                self.counts[status] += 1
                # every nick is used once: invited or not found, it never comes back
                app.store.mark_invited(account_id, nick, status)
                app.log(f"{nick} — {msg}")
            bot.clear_field()
            finished = True
            app.log(t("l_done", n=self.counts["ok"]))
        except Stopped:
            app.log(t("l_stopped", n=self.counts["ok"]))
        except Exception as e:
            app.log(t("l_error", err=e))
        finally:
            if finished and cfg.get("close_game_when_done", True):
                self.close_game()
            self.running = False
            self.detail = t("d_done") if finished else t("d_stopped")
            app.restore_after_game()

    def close_game(self):
        hwnd = w.find_game_window()
        if not hwnd:
            return
        self.app.log(t("l_closing_game"))
        w.user32.PostMessageW(hwnd, w.WM_CLOSE, 0, 0)
        for _ in range(40):
            if not w.game_running():
                return
            time.sleep(1)
        subprocess.run(["taskkill", "/IM", w.GAME_EXE, "/F"], capture_output=True, creationflags=0x08000000)


# ---------------------------------------------------------------- app / JS API

class Api:
    """Methods callable from the web UI as pywebview.api.<name>()."""

    def __init__(self, app):
        self._app = app

    def state(self):
        return self._app.state()

    def toggle_collect(self):
        self._app.toggle_collect()

    def start_invites(self, count):
        self._app.start_invites(int(count))

    def stop_invites(self):
        self._app.stop_invites()

    def open_settings(self):
        self._app.open_settings()

    def get_strings(self):
        return {"strings": i18n.strings(), "languages": i18n.available(), "current": i18n.current}

    def get_settings(self):
        cfg = self._app.cfg
        return {k: cfg[k] for k in SETTINGS_KEYS} | {"version": APP_VERSION}

    def save_settings(self, data):
        return self._app.save_settings(data)

    def close_settings(self):
        self._app.close_settings()

    def hide(self):
        self._app.hide()

    def minimize(self):
        self._app.window.minimize()

    def open_url(self, which):
        webbrowser.open({"api": API_KEY_URL}.get(which, API_KEY_URL))

    def open_data_folder(self):
        os.startfile(DATA_DIR)


class App:
    def __init__(self, start_hidden):
        self.cfg = load_config()
        save_config(self.cfg)
        i18n.set_language(self.cfg.get("language", "auto"))
        self.lines = collections.deque(maxlen=200)
        self.store = Store(DB_PATH)
        self.api = PubgApi(lambda: self.cfg)
        self.collector = Collector(lambda: self.cfg, self.api, self.store, self.log)
        self.run = None
        self.settings_window = None
        self.quitting = False
        self.js = Api(self)
        self.window = webview.create_window(
            APP_TITLE, os.path.join(UI_DIR, "main.html"), js_api=self.js, width=392, height=640,
            resizable=False, frameless=True, easy_drag=False, hidden=start_hidden, background_color="#0E1014")
        self.window.events.closing += self.on_closing
        if self.cfg.get("collect_running") and self.cfg.get("api_key"):
            self.collector.start()
        self.tray = self.make_tray()

    # ---------- log
    def log(self, msg):
        now = datetime.now()
        self.lines.append({"t": f"{now:%H:%M}", "m": msg})
        try:
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(f"{now:%Y-%m-%d %H:%M:%S} {msg}\n")
        except OSError:
            pass

    # ---------- state for the UI
    def state(self):
        cutoff = int(time.time() - float(self.cfg["stale_days"]) * DAY)
        s = self.store.stats(cutoff)
        c = self.collector
        r = self.run
        setup = [] if self.cfg.get("api_key") else ["api_key"]
        if not self.cfg.get("source_players"):
            setup.append("sources")
        return {
            "setup": setup,
            "stats": s,
            "collect": {"running": c.running, "state": c.state, "detail": c.detail},
            "invite": {
                "running": bool(r and r.running),
                "detail": r.detail if r else "",
                "target": r.target if r else self.cfg["invites_per_run"],
                "counts": r.counts if r else None,
            },
            "count": self.cfg["invites_per_run"],
            "log": list(self.lines)[-40:],
            "stop_key": self.cfg.get("stop_key", "F12"),
            "version": APP_VERSION,
        }

    # ---------- actions
    def toggle_collect(self):
        if self.collector.running:
            self.collector.stop()
            self.log(t("l_collect_stopped"))
        else:
            if not self.cfg.get("api_key") or not self.cfg.get("source_players"):
                self.open_settings()
                return
            self.collector.start()
            self.log(t("l_collect_started"))
        self.cfg["collect_running"] = self.collector.running
        save_config(self.cfg)

    def start_invites(self, count):
        if self.run and self.run.running:
            return
        self.cfg["invites_per_run"] = max(1, min(500, count))
        save_config(self.cfg)
        self.run = InviteRun(self, self.cfg["invites_per_run"])
        threading.Thread(target=self.run.run, daemon=True).start()

    def stop_invites(self):
        if self.run:
            self.run.stop_flag.set()

    def save_settings(self, data):
        cfg = dict(self.cfg)
        for k in SETTINGS_KEYS:
            if k in data:
                cfg[k] = data[k]
        cfg["api_key"] = str(cfg["api_key"]).strip()
        cfg["source_players"] = [s.strip() for s in cfg["source_players"] if str(s).strip()]
        for k, lo, hi in (("collect_interval_min", 5, 1440), ("stale_days", 1, 365), ("recheck_clan_days", 1, 90)):
            try:
                cfg[k] = max(lo, min(hi, int(cfg[k])))
            except (TypeError, ValueError):
                return {"ok": False, "error": t("err_numbers")}
        if cfg["autostart"] != self.cfg.get("autostart") and getattr(sys, "frozen", False):
            set_autostart(cfg["autostart"])
        lang_changed = cfg.get("language") != self.cfg.get("language")
        self.cfg = cfg
        save_config(cfg)
        if lang_changed:
            i18n.set_language(cfg.get("language", "auto"))
            try:
                self.window.evaluate_js("window.applyLang && applyLang()")
            except Exception:
                pass
            if self.tray:
                self.tray.update_menu()
        self.log(t("l_settings_saved"))
        return {"ok": True}

    # ---------- windows
    def open_settings(self):
        if self.settings_window:
            try:
                self.settings_window.show()
                self.settings_window.restore()
                return
            except Exception:
                self.settings_window = None
        self.settings_window = webview.create_window(
            f"{APP_TITLE} — {t('settings_title')}", os.path.join(UI_DIR, "settings.html"), js_api=self.js, width=440, height=720,
            resizable=False, frameless=True, easy_drag=False, background_color="#0E1014")
        self.settings_window.events.closed += lambda: setattr(self, "settings_window", None)

    def close_settings(self):
        if self.settings_window:
            self.settings_window.destroy()
            self.settings_window = None

    def show(self):
        self.window.show()
        self.window.restore()

    def hide_for_game(self):
        """The game is found by screenshots, so our windows must not cover it (windowed mode)."""
        self.hidden_for_game = True
        self.window.hide()
        self.close_settings()

    def restore_after_game(self):
        if getattr(self, "hidden_for_game", False):
            self.hidden_for_game = False
            self.show()

    def hide(self):
        self.window.hide()

    def on_closing(self):
        if self.quitting:
            return True
        self.window.hide()
        return False

    def quit(self):
        self.quitting = True
        self.stop_invites()
        self.collector.stop()
        if self.tray:
            self.tray.stop()
        for win in list(webview.windows):
            win.destroy()

    # ---------- tray
    def make_tray(self):
        try:
            import pystray
            from PIL import Image
        except ImportError:
            return None
        menu = pystray.Menu(
            pystray.MenuItem(lambda item: t("tray_open"), lambda: self.show(), default=True),
            pystray.MenuItem(lambda item: t("tray_invite"), lambda: self.start_invites(self.cfg["invites_per_run"])),
            pystray.MenuItem(lambda item: t("tray_collect_stop") if self.collector.running else t("tray_collect_start"),
                             lambda: self.toggle_collect()),
            pystray.MenuItem(lambda item: t("tray_exit"), lambda: self.quit()),
        )
        icon = pystray.Icon(APP_ID, Image.open(ICON_PNG), APP_TITLE, menu)
        icon.run_detached()
        return icon


def single_instance():
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.CreateMutexW(None, False, f"Local\\{APP_ID}")
    return None if ctypes.get_last_error() == 183 else handle


def selftest():
    """--selftest: checks that screen OCR works in this build, writes the result to selftest.txt."""
    from ocr import Ocr
    try:
        from vision import Vision
        eng = Ocr()
        lines = eng.read((0, 0, 400, 200))
        v = Vision()
        msg = f"ok language={eng.language} lines={len(lines)} templates={sorted(v.templates)}"
    except Exception as e:
        msg = f"fail {e!r}"
    with open(os.path.join(DATA_DIR, "selftest.txt"), "w", encoding="utf-8") as f:
        f.write(msg)


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
        sys.exit(0)
    mutex = single_instance()
    if not mutex:
        i18n.set_language(load_config().get("language", "auto"))
        ctypes.windll.user32.MessageBoxW(0, t("already_running"), APP_TITLE, 0x40)
        sys.exit(0)
    app = App("--tray" in sys.argv)
    webview.start(private_mode=False, storage_path=os.path.join(DATA_DIR, "webview"))
