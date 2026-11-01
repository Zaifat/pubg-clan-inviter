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
from collector import DAY, ApiError, Collector, PubgApi, Store, check_clans, member_key
import statuses
from game import GameBot, Stopped
from statuses import LONG_AGO
from i18n import t

APP_VERSION = "2.6.0"
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
    "kick_min_weekly_xp": 0,
    "kick_days_no_matches": 0,
    "kick_offline_status": True,
    "kick_learn_status": True,
    "kick_mode": "any",
    "kick_grace_days": 7,
    "kick_max_per_run": 20,
    "kick_dry_run": True,
    "kick_exclusions": [],
}

# only these can be changed from the settings window
SETTINGS_KEYS = ["language", "api_key", "source_players", "collect_interval_min", "stale_days", "recheck_clan_days",
                 "precheck_before_invite", "launch_game", "close_game_when_done", "autostart",
                 "kick_min_weekly_xp", "kick_days_no_matches", "kick_offline_status", "kick_learn_status", "kick_mode", "kick_grace_days", "kick_max_per_run",
                 "kick_dry_run", "kick_exclusions"]


def unit_name(days):
    """Name of a learned unit / phrase value (for texts)."""
    return min(("now", 0.0), ("minute", 1 / 1440), ("hour", 1 / 24), ("day", 1.0), ("week", 7.0), ("month", 30.0),
               key=lambda u: abs(u[1] - days))[0]


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

class GameRun:
    """A job that runs inside the game: invites or clan cleanup."""

    def __init__(self, app):
        self.app = app
        self.cfg = dict(app.cfg)
        self.stop_flag = threading.Event()
        self.running = True
        self.detail = t("d_preparing")

    def check(self):
        if self.stop_flag.is_set() or w.key_pressed(self.cfg.get("stop_key", "F12")):
            self.stop_flag.set()
            raise Stopped()

    def wait(self, sec):
        end = time.monotonic() + sec
        while time.monotonic() < end:
            self.check()
            time.sleep(0.1)

    def launch_game(self):
        if self.cfg.get("launch_game", True) and not w.game_running():
            self.app.log(t("l_launch"))
            os.startfile(STEAM_URL)

    def wait_game_window(self):
        if w.find_game_window():
            return
        if not self.cfg.get("launch_game", True):
            raise RuntimeError(t("e_game_not_running"))
        self.detail = t("d_wait_game")
        end = time.monotonic() + self.cfg["game_window_timeout_sec"]
        while not w.find_game_window():
            if time.monotonic() > end:
                raise RuntimeError(t("e_game_no_start"))
            self.wait(1)

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


class InviteRun(GameRun):
    def __init__(self, app, target):
        super().__init__(app)
        self.target = target
        self.counts = {"ok": 0, "not_found": 0}

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
            self.launch_game()
            names = self.candidates()
            if not names:
                app.log(t("l_no_valid"))
                return
            app.log(t("l_ready", n=len(names), target=self.target))
            self.wait_game_window()
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


class KickRun(GameRun):
    """Reads the in-game clan activity table and kicks members that match the criteria."""

    def __init__(self, app):
        super().__init__(app)
        self.counts = {"ok": 0, "failed": 0, "candidates": 0}
        self.dry_run = bool(self.cfg.get("kick_dry_run", True))

    def choose(self, members):
        """[(member, reason)] to kick, lowest weekly XP first."""
        cfg, app, now = self.cfg, self.app, time.time()
        min_xp = int(cfg.get("kick_min_weekly_xp", 0))
        max_days = int(cfg.get("kick_days_no_matches", 0))
        by_status = bool(cfg.get("kick_offline_status", True))
        grace = float(cfg.get("kick_grace_days", 7)) * DAY
        need_all = cfg.get("kick_mode", "any") == "all"
        excluded = {member_key(n) for n in cfg.get("kick_exclusions", [])}
        if not min_xp and not max_days and not by_status:
            app.log(t("l_kick_no_criteria"))
            return []
        # days since last online from the status column: Russian built in, other languages learned
        model = app.learn_statuses(members, now)
        for m in members:
            m.days = model.days(m.status, m.online)
        unknown = [m for m in members if not m.online and m.days is None]
        if max_days and len(unknown) > len(members) // 3:
            app.log(t("l_kick_status_unknown", n=len(unknown)))
        # "Offline" from the status text; its short width is the fallback when the text was not understood
        long_offline = {m.name for m in members if m.days is not None and m.days >= LONG_AGO} | \
            {m.name for m in members if m.days is None and m.name in self.long_offline(members)}
        # officers are protected by the exclusion list (the leader cannot be kicked by the game anyway)
        first_seen = app.store.scan_members([(m.name, m.weekly_xp) for m in members])
        app.store.record_scan([(m.name, m.weekly_xp, m.contribution, m.online, m.name in long_offline, m.status)
                               for m in members], now)

        invited = app.store.invited_map()
        joined_at = lambda m: max(first_seen.get(member_key(m.name), 0), invited.get(member_key(m.name), 0))

        # two members whose nicknames differ only in letters OCR confuses ("Twin_A" / "Twin-A") cannot be told
        # apart on screen or in the confirmation: neither is ever kicked
        key_count = collections.Counter(member_key(m.name) for m in members)
        twins = {k for k, c in key_count.items() if c > 1}

        chosen = []
        skipped = collections.defaultdict(list)
        for m in members:
            if member_key(m.name) in excluded:
                skipped["k_skip_excluded"].append(m.name)
                continue
            if member_key(m.name) in twins:
                skipped["k_skip_twins"].append(m.name)
                continue
            if now - joined_at(m) < grace:
                skipped["k_skip_new"].append(m.name)
                continue
            if min_xp and m.weekly_xp is None:
                skipped["k_skip_no_xp"].append(m.name)
            hits, reasons = [], []
            if min_xp:
                hit = m.weekly_xp is not None and m.weekly_xp < min_xp
                hits.append(hit)
                if hit:
                    reasons.append(t("k_xp", xp=m.weekly_xp, min=min_xp))
            gone = m.name in long_offline
            if max_days:
                # days since last online, from the status column; "Не в сети" counts as long ago
                hit = gone or (m.days is not None and m.days >= max_days)
                if hit and not gone:
                    reasons.append(t("k_days", d=round(m.days)))
                hits.append(hit)
            if by_status:
                hits.append(gone)
            if gone and (by_status or max_days):
                reasons.append(t("k_offline"))
            if (all(hits) if need_all else any(hits)):
                chosen.append((m, "; ".join(reasons)))
        for key, names in skipped.items():
            app.log(t(key, n=len(names), names=", ".join(names)))
        chosen.sort(key=lambda c: c[0].weekly_xp if c[0].weekly_xp is not None else 0)
        return chosen

    @staticmethod
    def long_offline(members):
        """Names with the "Offline" status (not logged in for long). It is told apart from "Last online 5 h ago" /
        "a day ago" without reading it: it is about half as long as those."""
        ref = sorted(m.status_width for m in members if not m.online and m.status_width)
        if len(ref) < 3:
            return set()
        typical = ref[len(ref) * 3 // 4]   # a "... ago" width even if many members are Offline
        return {m.name for m in members if not m.online and 0 < m.status_width < typical * 0.7}

    def run(self):
        app, cfg = self.app, self.cfg
        finished = False
        try:
            self.launch_game()
            self.wait_game_window()
            self.detail = t("d_wait_lobby")
            app.hide_for_game()
            bot = GameBot(dict(cfg, _debug_dir=os.path.join(DATA_DIR, "debug")), app.log, self.check)
            bot.bring_to_front()
            bot.open_clan_page(cfg["lobby_timeout_sec"])
            self.detail = t("d_kick_scan")
            members = bot.read_activity()
            app.log(t("l_kick_scanned", n=len(members)))
            if not members:
                raise RuntimeError(t("e_no_activity"))

            chosen = self.choose(members)
            self.counts["candidates"] = len(chosen)
            limit = int(cfg.get("kick_max_per_run", 20))
            app.log(t("l_kick_candidates", n=len(chosen), limit=limit))
            for m, reason in chosen:
                app.log(f"  {m.name} — {reason}")
            if self.dry_run:
                app.log(t("l_kick_dry"))
            else:
                # one pass down the members list; a member that cannot be kicked (the game may refuse, e.g.
                # during a match) does not use up the limit, up to 5 failures in a row
                reasons = {m.name: reason for m, reason in chosen}
                self.detail = t("d_kicking_all", n=min(limit, len(chosen)))

                def report(nick, status, msg):
                    if status == "ok":
                        self.counts["ok"] += 1
                        app.store.mark_kicked(nick, reasons[nick])
                    else:
                        self.counts["failed"] += 1
                    app.log(f"{nick} — {msg}")

                bot.kick_all([m.name for m, _ in chosen], limit, report)
            finished = True
            app.log(t("l_kick_done", n=self.counts["ok"]))
        except Stopped:
            app.log(t("l_kick_stopped", n=self.counts["ok"]))
        except Exception as e:
            app.log(t("l_error", err=e))
        finally:
            if finished and cfg.get("close_game_when_done", True):
                self.close_game()
            self.running = False
            self.detail = t("d_done") if finished else t("d_stopped")
            app.restore_after_game()


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

    def start_kick(self):
        self._app.start_kick()

    def stop_kick(self):
        self._app.stop_kick()

    def open_settings(self):
        self._app.open_settings()

    def open_members(self):
        self._app.open_members()

    def get_status_learning(self):
        return self._app.status_learning()

    def reset_status_learning(self):
        return self._app.reset_status_learning()

    def get_members(self):
        return self._app.store.members_overview()

    def close_members(self):
        self._app.close_members()

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
        self.kick_run = None
        self.settings_window = None
        self.members_window = None
        self.quitting = False
        self.js = Api(self)
        self.window = webview.create_window(
            APP_TITLE, os.path.join(UI_DIR, "main.html"), js_api=self.js, width=392, height=780,
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
        k = self.kick_run
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
            "kick": {
                "running": bool(k and k.running),
                "detail": k.detail if k else "",
                "counts": k.counts if k else None,
                "dry_run": bool(self.cfg.get("kick_dry_run", True)),
                "configured": self.kick_configured(),
                "total": s["kicked_total"],
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

    # ---------- learning the words of the online status (other game languages)
    def learn_statuses(self, members=(), now=None):
        """Model of the status words from every check since the last reset, plus the current reading.
        New words are written to the log; the result is kept for the settings window."""
        if not self.cfg.get("kick_learn_status", True):
            return statuses.Model()
        since = int(self.store.get_meta("status_learn_since", "0") or 0)
        history = list(self.store.status_history(since))
        now = int(now or time.time())
        history += [(now, member_key(m.name), m.status, m.online) for m in members if m.status]
        model = statuses.learn(history)
        before = json.loads(self.store.get_meta("status_model", "{}") or "{}")
        for word, size in model.units.items():
            if before.get("units", {}).get(word) != size:
                self.log(t("l_status_learned_unit", word=word, unit=t("unit_" + unit_name(size))))
        for phrase, days in model.phrases.items():
            if before.get("phrases", {}).get(phrase) != days:
                self.log(t("l_status_learned_phrase", phrase=phrase, value=t("phrase_" + unit_name(days))))
        self.store.set_meta("status_model", json.dumps({"units": model.units, "phrases": model.phrases},
                                                        ensure_ascii=False))
        return model

    def status_learning(self):
        data = json.loads(self.store.get_meta("status_model", "{}") or "{}")
        since = int(self.store.get_meta("status_learn_since", "0") or 0)
        checks = len({r[0] for r in self.store.status_history(since)})
        return {
            "units": [{"word": w, "unit": t("unit_" + unit_name(s))} for w, s in sorted(data.get("units", {}).items())],
            "phrases": [{"word": p, "unit": t("phrase_" + unit_name(d))} for p, d in sorted(data.get("phrases", {}).items())],
            "checks": checks,
        }

    def reset_status_learning(self):
        self.store.set_meta("status_learn_since", str(int(time.time()) + 1))
        self.store.set_meta("status_model", "{}")
        self.log(t("l_status_reset"))
        return self.status_learning()

    def kick_configured(self):
        cfg = self.cfg
        return bool(int(cfg.get("kick_min_weekly_xp", 0)) or int(cfg.get("kick_days_no_matches", 0))
                    or cfg.get("kick_offline_status", True))

    def game_busy(self):
        return any(r and r.running for r in (self.run, self.kick_run))

    def start_invites(self, count):
        if self.game_busy():
            return
        self.cfg["invites_per_run"] = max(1, min(500, count))
        save_config(self.cfg)
        self.run = InviteRun(self, self.cfg["invites_per_run"])
        threading.Thread(target=self.run.run, daemon=True).start()

    def stop_invites(self):
        if self.run:
            self.run.stop_flag.set()

    def start_kick(self):
        if self.game_busy():
            return
        if not self.kick_configured():
            self.open_settings()
            return
        self.kick_run = KickRun(self)
        threading.Thread(target=self.kick_run.run, daemon=True).start()

    def stop_kick(self):
        if self.kick_run:
            self.kick_run.stop_flag.set()

    def save_settings(self, data):
        cfg = dict(self.cfg)
        for k in SETTINGS_KEYS:
            if k in data:
                cfg[k] = data[k]
        cfg["api_key"] = str(cfg["api_key"]).strip()
        cfg["source_players"] = [s.strip() for s in cfg["source_players"] if str(s).strip()]
        cfg["kick_exclusions"] = [s.strip() for s in cfg["kick_exclusions"] if str(s).strip()]
        cfg["kick_mode"] = "all" if cfg.get("kick_mode") == "all" else "any"
        for k, lo, hi in (("collect_interval_min", 5, 1440), ("stale_days", 1, 365), ("recheck_clan_days", 1, 90),
                          ("kick_min_weekly_xp", 0, 10 ** 7), ("kick_days_no_matches", 0, 365),
                          ("kick_grace_days", 0, 90), ("kick_max_per_run", 1, 100)):
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

    def open_members(self):
        if self.members_window:
            try:
                self.members_window.show()
                self.members_window.restore()
                return
            except Exception:
                self.members_window = None
        self.members_window = webview.create_window(
            f"{APP_TITLE} — {t('members_title')}", os.path.join(UI_DIR, "members.html"), js_api=self.js, width=900,
            height=680, min_size=(640, 400), frameless=True, easy_drag=False, background_color="#0E1014")
        self.members_window.events.closed += lambda: setattr(self, "members_window", None)

    def close_members(self):
        if self.members_window:
            self.members_window.destroy()
            self.members_window = None

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
        self.close_members()

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
        self.stop_kick()
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
            pystray.MenuItem(lambda item: t("tray_kick"), lambda: self.start_kick()),
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
