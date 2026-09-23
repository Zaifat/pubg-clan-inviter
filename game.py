"""Drives the PUBG lobby: opens the clan invite page and sends invites.

UI elements are found by their icons (template matching), so any game language and any resolution work:
  lobby clan button icon -> clan page -> "Invitations" tab (A/D keys switch tabs) -> search field (magnifier),
  clear button (x), invite button (person+) of the found player.
Text (OCR) is used only to speed things up. The game's answer after an invite is not read.
"""
import ctypes
import random
import time
from ctypes import wintypes

import winput as w
from i18n import t
from ocr import Ocr, find, find_word, fuzzy_contains
from vision import REF_H, Vision

# Texts used only as shortcuts (Russian/English verified; the icon path works for any language)
T_INVITES_TAB = ["приглашения", "invites", "invitations", "einladungen", "invitaciones", "convites",
                 "zaproszenia", "davetler", "inviti"]
T_NOT_FOUND = ["не найден", "not found", "no matching user", "no results", "nicht gefunden", "introuvable", "no encontrado",
               "não encontrado", "nie znaleziono", "bulunamadı"]
T_RECONNECT = ["reconnect", "переподключ", "повторное подключение", "подключиться снова", "reconectar",
               "reconnecter", "wiederverbinden", "erneut verbinden", "yeniden bağlan", "riconnetti", "połącz ponownie"]
T_RESTART_LOBBY = ["restart lobby", "перезапуск лобби", "перезапустить лобби", "перезагрузить лобби"]


class Stopped(Exception):
    pass


def has_any(text, keys, threshold=0.8):
    return any(fuzzy_contains(text, k, threshold) for k in keys)


class GameBot:
    OK, NOT_FOUND = "ok", "not_found"

    def __init__(self, cfg, log, check):
        """check() is called often and raises Stopped to abort."""
        self.cfg = cfg
        self.log = log
        self.check = check
        self.ocr = Ocr()
        self.vision = Vision()
        self.search = None  # magnifier icon in the search field
        self.clear = None   # x button in the search field

    # ---------- window
    def rect(self):
        hwnd = w.find_game_window()
        if not hwnd:
            raise RuntimeError(t("e_window_closed"))
        r = wintypes.RECT()
        w.user32.GetClientRect(hwnd, ctypes.byref(r))
        p = wintypes.POINT(0, 0)
        w.user32.ClientToScreen(hwnd, ctypes.byref(p))
        return p.x, p.y, r.right, r.bottom

    def s(self):
        return self.rect()[3] / REF_H

    def region(self, fx0, fy0, fx1, fy1):
        x, y, cw, ch = self.rect()
        return x + int(fx0 * cw), y + int(fy0 * ch), x + int(fx1 * cw), y + int(fy1 * ch)

    def icon(self, name, box):
        return self.vision.find(name, box, self.rect()[3])

    def icons(self, name, box):
        return self.vision.find_all(name, box, self.rect()[3])

    def bring_to_front(self):
        hwnd = w.find_game_window()
        if hwnd and not w.game_is_foreground():
            # a synthetic Alt press lets a background process take the foreground
            if w.user32.IsIconic(hwnd):
                w.user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            w.key_event(w.VK["alt"], False)
            w.key_event(w.VK["alt"], True)
            w.user32.BringWindowToTop(hwnd)
            w.user32.SetForegroundWindow(hwnd)

    # ---------- input
    def sleep(self, ms):
        j = int(self.cfg.get("jitter_ms", 40))
        end = time.monotonic() + max(0, ms + random.randint(-j, j)) * float(self.cfg.get("delay_scale", 1.0)) / 1000
        while time.monotonic() < end:
            self.check()
            time.sleep(0.02)

    def wait_focus(self):
        warned = False
        while not w.game_is_foreground():
            if not warned:
                self.log(t("l_focus_wait"))
                warned = True
            time.sleep(0.3)
            self.check()
        if warned:
            self.sleep(800)

    def click(self, x, y):
        self.wait_focus()
        w.mouse_move(x, y)
        self.sleep(70)
        w.mouse_button(True)
        try:
            self.sleep(80)
        finally:
            w.mouse_button(False)

    def key(self, name):
        self.wait_focus()
        w.key_event(w.VK[name], False)
        try:
            self.sleep(90)
        finally:
            w.key_event(w.VK[name], True)

    def letter(self, ch):
        self.wait_focus()
        w.user32.PostMessageW(w.user32.GetForegroundWindow(), w.WM_INPUTLANGCHANGEREQUEST, 0, w.US_LAYOUT or 0)
        w.type_char(ch, "scancode")

    def type_text(self, text):
        self.wait_focus()
        w.user32.PostMessageW(w.user32.GetForegroundWindow(), w.WM_INPUTLANGCHANGEREQUEST, 0, w.US_LAYOUT or 0)
        for ch in text:
            w.type_char(ch, "scancode")
            self.sleep(45)

    def park_mouse(self):
        """Move the cursor away so hover effects do not change the icons."""
        x, y, cw, ch = self.rect()
        w.mouse_move(x + cw // 2, y + int(ch * 0.06))

    # ---------- navigation
    def find_search(self):
        box = self.region(0.1, 0.15, 0.65, 0.5)
        search = self.icon("search", box)
        if not search:
            return False
        clear = [c for c in self.icons("clear", box) if abs(c.cy - search.cy) < search.h and c.cx > search.cx]
        if not clear:
            return False
        clear = min(clear, key=lambda c: c.cx)
        # the clan "Search" tab has a similar but narrower field (392 px vs 488 px at 1440p)
        width = (clear.cx - search.cx) / self.s()
        if not 450 <= width <= 530:
            return False
        self.search, self.clear = search, clear
        return True

    def gear(self):
        """Settings gear of the main lobby screen: visible only when the lobby is loaded and nothing covers it."""
        return self.icon("gear", self.region(0.8, 0, 1, 0.15))

    def handle_reconnect(self):
        """Clicks "Reconnect" in a server error dialog. Returns True if it did."""
        lines = self.ocr.read(self.region(0.15, 0.2, 0.85, 0.85))
        btn = find(lines, T_RECONNECT, 0.8)
        if not btn:
            return False
        wd = find_word(btn, T_RECONNECT)
        self.log(t("l_reconnect"))
        self.click(wd.cx, wd.cy)
        self.park_mouse()
        self.sleep(5000)
        return True

    def restart_lobby(self):
        """System menu (gear) -> "Restart lobby" (4th item), then wait for the lobby to load again."""
        gear = self.gear()
        if not gear:
            return False
        self.log(t("l_restart_lobby"))
        self.click(gear.cx, gear.cy)
        self.park_mouse()
        self.sleep(1500)
        x, y, cw, ch = self.rect()
        lines = [l for l in self.ocr.read(self.region(0.3, 0.33, 0.7, 0.8)) if l.h > ch * 0.012]
        target = find(lines, T_RESTART_LOBBY, 0.8)
        if not target and len(lines) >= 5:
            target = sorted(lines, key=lambda l: l.cy)[3]   # Resume, Settings, Key guide, Restart lobby, Exit
        if target:
            px, py = target.cx, target.cy
        else:
            px, py = x + cw // 2, y + int(ch * 0.629)
        self.click(px, py)
        self.park_mouse()
        end = time.monotonic() + 180
        self.sleep(5000)
        while time.monotonic() < end and not self.gear():
            if not self.handle_reconnect():
                self.sleep(1500)
        self.sleep(2000)
        return True

    def open_invite_page(self, timeout):
        """From anywhere in the lobby to the "invite to clan" page."""
        end = time.monotonic() + timeout
        clan_clicked_at = None
        tab_presses = 0
        no_clan_since = idle_since = None
        idle_actions = 0
        restarts = 0
        polls = 0
        while time.monotonic() < end:
            self.check()
            polls += 1
            if not w.game_is_foreground():
                self.bring_to_front()
                self.sleep(500)
            if self.find_search():
                return
            if polls % 2 == 0 and self.handle_reconnect():
                clan_clicked_at = no_clan_since = idle_since = None
                continue
            if self.gear():
                idle_since = None
                clan = self.icon("clan", self.region(0.7, 0.9, 1.0, 1.0))
                if clan:
                    self.log(t("l_open_clan"))
                    self.click(clan.cx, clan.cy)
                    self.park_mouse()
                    clan_clicked_at, tab_presses, no_clan_since = time.monotonic(), 0, None
                    self.sleep(1800)
                    continue
                # the lobby is up, but the clan button did not load
                no_clan_since = no_clan_since or time.monotonic()
                if time.monotonic() - no_clan_since > 15 and restarts < 3:
                    restarts += 1
                    self.restart_lobby()
                    no_clan_since = None
                    continue
                self.sleep(1000)
                continue
            if clan_clicked_at:
                # on the clan page: click the tab by its name if we can read it, else switch tabs with D
                if tab_presses == 0:
                    lines = self.ocr.read(self.region(0, 0.05, 1, 0.16))
                    tab = find(lines, T_INVITES_TAB, 0.85)
                    if tab:
                        wd = find_word(tab, T_INVITES_TAB)
                        self.click(wd.cx, wd.cy)
                        self.park_mouse()
                        tab_presses = 1
                        self.sleep(1200)
                        continue
                if tab_presses < 14:
                    self.letter("d")
                    tab_presses += 1
                    self.sleep(900)
                    continue
                self.log(t("l_no_tab"))
                self.key("esc")
                clan_clicked_at = None
                self.sleep(2000)
                continue
            # nothing known on screen: loading screen, or a dialog (server error...) covers the lobby
            idle_since = idle_since or time.monotonic()
            idle = time.monotonic() - idle_since
            if idle > 20 and idle_actions == 0:
                self.log(t("l_dialog_enter"))
                self.key("enter")
                idle_actions = 1
            elif idle > 35:
                self.log(t("l_dialog_esc"))
                self.key("esc")
                idle_since, idle_actions = time.monotonic(), 0
            self.sleep(1000)
        raise RuntimeError(t("e_no_invite_page"))

    def ensure_page(self):
        if self.find_search():
            return
        self.log(t("l_page_lost"))
        self.open_invite_page(90)

    # ---------- invites
    def result_box(self):
        s = self.s()
        sr, cl = self.search, self.clear
        return sr.x - int(60 * s), sr.cy + int(45 * s), cl.cx + int(70 * s), sr.cy + int(260 * s)

    def invite(self, nick):
        """Returns (status, message)."""
        self.ensure_page()
        s = self.s()
        self.click(self.clear.cx, self.clear.cy)
        self.sleep(200)
        self.click(self.search.cx + int(140 * s), self.search.cy)
        self.sleep(150)
        self.type_text(nick)
        self.sleep(120)
        self.key("enter")
        self.park_mouse()

        # wait for the invite button of the found player, or for "not found"
        button = None
        start = time.monotonic()
        polls = 0
        while time.monotonic() - start < 3.5:
            self.sleep(200)
            polls += 1
            found = self.icons("invite", self.result_box())
            if found:
                button = min(found, key=lambda m: m.cy)
                break
            if polls % 3 == 0 and time.monotonic() - start > 0.8:
                box = self.result_box()
                text = " ".join(l.text for l in self.ocr.read((box[0], box[1], box[2], box[1] + int(700 * s))))
                if has_any(text, T_NOT_FOUND):
                    return self.NOT_FOUND, t("r_not_found")
        if not button:
            return self.NOT_FOUND, t("r_not_found_btn")

        self.click(button.cx, button.cy)
        self.park_mouse()
        self.sleep(700)
        return self.OK, t("r_invited")

    def clear_field(self):
        if self.clear:
            self.click(self.clear.cx, self.clear.cy)
