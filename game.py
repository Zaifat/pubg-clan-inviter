"""Drives the PUBG lobby: opens the clan invite page and sends invites.

UI elements are found by their icons (template matching), so any game language and any resolution work:
  lobby clan button icon -> clan page -> "Invitations" tab (A/D keys switch tabs) -> search field (magnifier),
  clear button (x), invite button (person+) of the found player.
Text (OCR) is used only to speed things up. The game's answer after an invite is not read.
"""
import ctypes
import os
import random
import re
import statistics
import time
from ctypes import wintypes
from dataclasses import dataclass

import numpy as np
from PIL import ImageGrab

import winput as w
from i18n import t
from ocr import Ocr, find, find_word, fuzzy_contains, skeleton, similarity
from collector import member_key
from vision import REF_H, Vision

# Texts used only as shortcuts (Russian/English verified; the icon path works for any language)
T_INVITES_TAB = ["приглашения", "invites", "invitations", "einladungen", "invitaciones", "convites",
                 "zaproszenia", "davetler", "inviti"]
T_NOT_FOUND = ["не найден", "not found", "no matching user", "no results", "nicht gefunden", "introuvable", "no encontrado",
               "não encontrado", "nie znaleziono", "bulunamadı"]
T_RECONNECT = ["reconnect", "переподключ", "повторное подключение", "подключиться снова", "reconectar",
               "reconnecter", "wiederverbinden", "erneut verbinden", "yeniden bağlan", "riconnetti", "połącz ponownie"]
T_RESTART_LOBBY = ["restart lobby", "перезапуск лобби", "перезапустить лобби", "перезагрузить лобби"]

# Clan activity table: columns by their share of the table width (number, name, role (never read), contribution,
# weekly clan XP, online status). The headers are never read, so the game language does not matter.
ACT_COLS = (0.0, 0.115, 0.284, 0.469, 0.66, 0.838, 1.0)
ACT_INDEX_X = 0.038   # center of the number column
OCR_SCALE = 2         # small table and card text reads better enlarged
ROSTER = (0.29, 0.445, 0.955, 0.885)   # members grid of the clan "Members" tab (not "Best players of the week")
NICK_LOOKALIKES = str.maketrans("аврсеёкмнорстухАВЕЁКМНОРСТУХ•·–—", "abpceekmhopctyxABEEKMHOPCTYX----")
DIGIT_LOOKALIKES = str.maketrans("lI|Oo", "11100")


class Stopped(Exception):
    pass


@dataclass
class Member:
    index: int
    name: str
    contribution: float | None
    weekly_xp: int | None
    online: bool
    status_width: int = 0     # ink width of an offline status ("Offline" is much shorter than "... ago")
    status: str = ""          # online status text as read (Russian client)
    days: float | None = None  # days since last online from the status (statuses.Model); LONG_AGO for "Offline"


def as_number(text):
    """Row number as OCR reads it (l, I, O for 1, 0); "" if it is not a number."""
    s = text.translate(DIGIT_LOOKALIKES)
    return s if s.isdigit() and len(s) <= 3 else ""


def clean_nick(text):
    """PUBG nicknames are Latin letters, digits, - and _: OCR'd Cyrillic look-alikes become Latin, junk is dropped."""
    text = text.translate(NICK_LOOKALIKES)
    return re.sub(r"[^A-Za-z0-9_-]", "", text)


def parse_int(text):
    digits = re.sub(r"\D", "", text)
    return int(digits) if digits else None


def parse_float(text):
    m = re.search(r"\d+(?:[.,]\d+)?", text)
    return float(m.group().replace(",", ".")) if m else None


def same_nick(word, nick):
    """Strict: the same nickname up to the letters OCR confuses (I/l/1, O/0, _/-)."""
    return member_key(clean_nick(word)) == member_key(nick)


def nick_matches(word, nick):
    """OCR word vs nickname: exact up to look-alike letters, or very close (also a cut "Nickna…")."""
    a, b = skeleton(word.rstrip(".…")), skeleton(nick)
    if not a:
        return False
    if a == b:
        return True
    if word.endswith(("…", "...")) and len(a) >= 5 and b.startswith(a):
        return True
    return abs(len(a) - len(b)) <= 1 and similarity(word, nick) >= 0.88


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
        self.ocr_latin = Ocr("en")  # nicknames and numbers
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

    def hover_click(self, x, y, right=False):
        """Click that the clan page reliably takes: the cursor arrives, rests, then a longer press."""
        self.wait_focus()
        w.mouse_move(x - int(20 * self.s()), y)
        self.sleep(200)
        w.mouse_move(x, y)
        self.sleep(380)
        w.mouse_button(True, right)
        try:
            self.sleep(150)
        finally:
            w.mouse_button(False, right)

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

    # ---------- clan members page (auto-kick)
    def wheel(self, x, y, notches):
        self.wait_focus()
        w.mouse_move(x, y)
        self.sleep(60)
        w.mouse_wheel(notches)
        self.sleep(350)

    def clan_close_button(self):
        """[Esc] Close button at the bottom left: shown on every clan page tab."""
        return self.icon("esc", self.region(0, 0.93, 0.2, 1))

    def members_tab_active(self):
        """The first clan tab ("Members") is active when its title is bright white (inactive tabs are grey).
        Tabs start right after the [A] key icon, so the first title is at the same place in any language."""
        img = Vision.grab(self.region(0.068, 0.11, 0.085, 0.124))
        return bool(img.size) and int(img.max()) > 205

    def find_activity_button(self):
        """"Clan member activity" is the button right after [Esc] Close in the bottom bar. Its text is not
        read, only where it is: the first text right of the Close button."""
        esc = self.clan_close_button()
        if not esc:
            return None
        s = self.s()
        x, y, cw, ch = self.rect()
        box = (esc.cx + int(175 * s), esc.cy - int(25 * s), min(x + cw, esc.cx + int(900 * s)), esc.cy + int(25 * s))
        lines = self.ocr.read(box)
        if lines:
            first = min(lines, key=lambda ln: ln.x)
            return first.words[0].cx, first.cy
        return esc.cx + int(300 * s), esc.cy

    def open_clan_page(self, timeout):
        """From anywhere in the lobby to the clan "Members" tab (members list + activity button)."""
        end = time.monotonic() + timeout
        tab_presses = 0
        idle_since = None
        polls = 0
        while time.monotonic() < end:
            self.check()
            polls += 1
            if not w.game_is_foreground():
                self.bring_to_front()
                self.sleep(500)
            if self.clan_close_button():
                idle_since = None
                if self.members_tab_active():
                    return
                if tab_presses < 14:
                    # the members tab is the first one: walk left with A (then right with D if A wraps around)
                    self.letter("a" if tab_presses < 10 else "d")
                    tab_presses += 1
                    self.sleep(900)
                    continue
                self.log(t("l_no_members_tab"))
                self.key("esc")
                tab_presses = 0
                self.sleep(2000)
                continue
            if polls % 2 == 0 and self.handle_reconnect():
                idle_since = None
                continue
            if self.gear():
                idle_since = None
                clan = self.icon("clan", self.region(0.7, 0.9, 1.0, 1.0))
                if clan:
                    self.log(t("l_open_clan"))
                    self.click(clan.cx, clan.cy)
                    self.park_mouse()
                    self.sleep(1800)
                    continue
                self.sleep(1000)
                continue
            idle_since = idle_since or time.monotonic()
            if time.monotonic() - idle_since > 25:
                self.key("esc")
                idle_since = None
            self.sleep(1000)
        raise RuntimeError(t("e_no_clan_page"))

    def _table_rows(self, words):
        """Rows of the activity table from OCR words: the number column (1, 2, 3...) gives the rows and the
        table width (the table is centered in the window), the other cells are taken by their x position."""
        x, y, cw, ch = self.rect()
        nums = [wd for wd in words if as_number(wd.text) and wd.cx < x + cw * 0.35]
        if len(nums) < 3:
            return None, []
        # the number column: the x position most numbers share
        col = max(nums, key=lambda a: sum(abs(b.cx - a.cx) < cw * 0.012 for b in nums))
        nums = sorted((n for n in nums if abs(n.cx - col.cx) < cw * 0.012), key=lambda n: n.cy)
        if len(nums) < 3:
            return None, []
        anchors = [(n.cy, int(as_number(n.text))) for n in nums]
        steps = [(b[0] - a[0]) / (b[1] - a[1]) for a, b in zip(anchors, anchors[1:]) if b[1] > a[1]]
        if not steps:
            return None, []
        row_h = statistics.median(steps)
        center = x + cw / 2
        width = (center - col.cx) / (0.5 - ACT_INDEX_X)
        left = center - width / 2

        def column(wd):
            f = (wd.cx - left) / width
            return next((i for i in range(len(ACT_COLS) - 1) if ACT_COLS[i] <= f < ACT_COLS[i + 1]), None)

        # OCR sometimes drops a lone digit, so rows come from the name column and get their number from
        # the nearest number that was read
        row_ys = []
        for wd in sorted((wd for wd in words if column(wd) == 1), key=lambda wd: wd.cy):
            if not row_ys or wd.cy - row_ys[-1] > row_h * 0.45:
                row_ys.append(wd.cy)
        rows = []
        for cy in row_ys:
            ay, ai = min(anchors, key=lambda a: abs(a[0] - cy))
            index = ai + round((cy - ay) / row_h)
            if index < 1 or not anchors[0][0] - row_h * 0.5 <= cy <= anchors[-1][0] + row_h * 0.5:
                continue
            cells = [[] for _ in range(len(ACT_COLS) - 1)]
            for wd in words:
                col_i = column(wd)
                if col_i and abs(wd.cy - cy) <= row_h * 0.45:
                    cells[col_i].append(wd)
            text = lambda i, sep=" ": sep.join(c.text for c in sorted(cells[i], key=lambda c: c.x))
            name = clean_nick(text(1, ""))
            if not name:
                continue
            status_box = (int(left + ACT_COLS[5] * width), int(cy - row_h * 0.35),
                          int(left + width), int(cy + row_h * 0.35))
            xp = parse_int(text(4))
            if xp is None:
                xp_box = (int(left + ACT_COLS[4] * width), int(cy - row_h * 0.35),
                          int(left + ACT_COLS[5] * width), int(cy + row_h * 0.35))
                xp = self.lone_digit(xp_box, row_h)
            online = self.is_colored(status_box)
            contribution = parse_float(text(3))
            if contribution is None:
                c_box = (int(left + ACT_COLS[3] * width), int(cy - row_h * 0.35),
                         int(left + ACT_COLS[4] * width), int(cy + row_h * 0.35))
                contribution = self.lone_digit(c_box, row_h)
            rows.append(Member(index, name, contribution, xp, online,
                               0 if online else self.ink_width(status_box)))
            rows[-1].cy = cy
        return (left, width, row_h), rows

    @staticmethod
    def ink(box):
        """(width, height) of the light text inside a screen box, (0, 0) if it is empty."""
        img = Vision.grab(box)
        cols = np.where((img > 150).any(axis=0))[0]
        rows = np.where((img > 150).any(axis=1))[0]
        return (int(cols[-1] - cols[0] + 1), int(rows[-1] - rows[0] + 1)) if len(cols) else (0, 0)

    def ink_width(self, box):
        return self.ink(box)[0]

    def lone_digit(self, box, row_h):
        """OCR skips a lone "0". Weekly XP goes in steps of 5 and a contribution under 1 has decimals, so one narrow
        glyph in the cell means 0: returns 0 for that, None for an empty or unreadable cell."""
        w_, h_ = self.ink(box)
        if not w_ or h_ < row_h * 0.2:
            return None
        return 0 if w_ < row_h * 0.45 else None

    def is_colored(self, box):
        """Online statuses are colored ("Online" / "You" green, "In a match" blue), offline ones are grey:
        a color check works in any language."""
        img = np.asarray(ImageGrab.grab(bbox=box, all_screens=True).convert("RGB")).astype(int)
        hi, lo = img.max(axis=2), img.min(axis=2)
        return int(((hi - lo > 70) & (hi > 140)).sum()) > 12 * self.s()

    def read_activity_table(self, page=None):
        """Visible rows of the open activity window. page: number of the snapshot saved to the debug folder."""
        box = self.region(0.1, 0.18, 0.9, 0.86)
        debug = self.cfg.get("_debug_dir")
        if debug and page is not None:
            try:
                ImageGrab.grab(bbox=box, all_screens=True).save(os.path.join(debug, f"activity_{page:02}.png"))
            except OSError:
                pass
        lines = self.ocr_latin.read(box, OCR_SCALE)
        geo, rows = self._table_rows([wd for ln in lines for wd in ln.words])
        if geo and rows:
            # the status column again, with the Russian OCR: nicknames are Latin, statuses are words
            left, width, row_h = geo
            x0 = int(left + ACT_COLS[5] * width)
            col = (x0, int(min(r.cy for r in rows) - row_h), int(left + width), int(max(r.cy for r in rows) + row_h))
            st = self.ocr.read(col, OCR_SCALE)
            for r in rows:
                near = [ln for ln in st if abs(ln.cy - r.cy) <= row_h * 0.45]
                r.status = " ".join(ln.text for ln in sorted(near, key=lambda ln: ln.x))
        return geo, rows

    def read_activity(self):
        """Opens the activity window, scrolls it to the end and returns all members (sorted by number)."""
        self.park_mouse()
        pos = self.find_activity_button()
        if not pos:
            raise RuntimeError(t("e_no_activity"))
        self.click(*pos)
        self.park_mouse()
        debug = self.cfg.get("_debug_dir")
        if debug:
            os.makedirs(debug, exist_ok=True)
            for f in os.listdir(debug):
                if f.startswith(("activity_", "kick_")):
                    os.remove(os.path.join(debug, f))
        geo, rows = None, []
        for _ in range(10):
            self.sleep(700)
            geo, rows = self.read_activity_table(0)
            if len(rows) >= 3:
                break
        if not geo:
            raise RuntimeError(t("e_no_activity"))
        left, width, row_h = geo
        # pages are joined by nickname: a row number computed near the table edge can be one off, and joining
        # by number then mixes two players up
        members = {}

        def add(r):
            key = member_key(r.name)
            if key not in members:
                key = next((k for k, m in members.items()
                            if abs(m.index - r.index) <= 1 and similarity(m.name, r.name) >= 0.9), key)
            old = members.get(key)
            if old is None or (old.weekly_xp is None and r.weekly_xp is not None):
                members[key] = r   # a row cut by the table edge is read again on the next page
            return old is None

        for r in rows:
            add(r)
        _, y, _, ch = self.rect()
        still = 0
        for page in range(1, 61):
            if still >= 2:
                break
            self.wheel(int(left + width / 2), int(y + ch / 2), -5)
            new = [add(r) for r in self.read_activity_table(page)[1]]
            still = 0 if any(new) else still + 1
        self.key("esc")
        self.sleep(800)
        return sorted(members.values(), key=lambda m: m.index)

    def roster_hit(self, nick, strict=False):
        """The nickname on the visible part of the members list: (OCR word or None, page text)."""
        lines = self.ocr_latin.read(self.region(*ROSTER), OCR_SCALE)
        # a card's first line is "Nickname 3" (name + level); chat pop-ups over the list are longer
        firsts = [ln.words[0] for ln in lines if len(ln.words) <= 3]
        hit = next((wd for wd in firsts if same_nick(wd.text, nick)), None)
        if not hit and not strict:
            hit = next((wd for wd in firsts if nick_matches(wd.text, nick)), None)
        return hit, " ".join(ln.text for ln in lines)

    def find_member(self, nick, strict=False):
        """Finds the nickname in the members list: on the visible part first, then from the top down.
        Returns the OCR word or None. strict: only the same nickname up to OCR look-alikes."""
        hit, _ = self.roster_hit(nick, strict)
        if hit:
            return hit
        x, y, cw, ch = self.rect()
        list_center = (x + int(cw * 0.62), y + int(ch * 0.66))
        self.wheel(*list_center, 30)
        self.park_mouse()
        prev, same = None, 0
        for _ in range(40):
            self.sleep(450)
            hit, page = self.roster_hit(nick, strict)
            if hit:
                return hit
            # the end of the list: the page did not change twice in a row (one unchanged page may be a slow scroll)
            same = same + 1 if page == prev else 0
            if same >= 2:
                return None
            prev = page
            self.wheel(*list_center, -4)
            self.park_mouse()
        return None

    CHAT_BUTTON = (0.854, 0.975)

    def chat_open(self):
        img = np.asarray(ImageGrab.grab(bbox=self.region(0.84, 0.955, 0.868, 0.995), all_screens=True)
                         .convert("RGB")).astype(int)
        r, g, b = img[..., 0], img[..., 1], img[..., 2]
        return float(((g - r > 15) & (g - b > 5)).mean()) > 0.4

    def close_chat(self):
        """The chat panel covers the bottom right of the members list (and its pop-ups have buttons)."""
        if self.chat_open():
            x, y, cw, ch = self.rect()
            self.hover_click(x + int(self.CHAT_BUTTON[0] * cw), y + int(self.CHAT_BUTTON[1] * ch))
            self.park_mouse()
            self.sleep(600)

    def ensure_members_page(self):
        """The clan "Members" tab must be open before a kick (a stray Esc or pop-up may have closed it)."""
        if not (self.clan_close_button() and self.members_tab_active()):
            self.log(t("l_page_lost"))
            self.open_clan_page(90)
        self.close_chat()

    def _menu_lines(self, around):
        x, y, cw, ch = self.rect()
        box = (max(x, around.x - int(cw * 0.35)), max(y, around.cy - int(ch * 0.45)),
               min(x + cw, around.x + int(cw * 0.4)), min(y + ch, around.cy + int(ch * 0.45)))
        return self.ocr.read(box)

    def pick_menu(self, lines):
        """Items of the member menu among new text lines: the longest column with one left edge and an even
        step. Chat pop-ups ("Player in game. [Invite]") may share the left edge but not the step."""
        best = []
        for a in lines:
            col = sorted((b for b in lines if abs(b.x - a.x) < 12 * self.s()), key=lambda b: b.cy)
            if len(col) < 3:
                continue
            gaps = [b.cy - c.cy for c, b in zip(col, col[1:])]
            step = statistics.median(gaps)
            runs, run = [], [col[0]]
            for c, gap in zip(col[1:], gaps):
                if abs(gap - step) <= step * 0.3:
                    run.append(c)
                else:
                    runs.append(run)
                    run = [c]
            runs.append(run)
            longest = max(runs, key=len)
            if len(longest) > len(best):
                best = longest
        return best

    def snapshot(self, name):
        """Whole game window into the debug folder (kick steps of the last run, to see what happened)."""
        debug = self.cfg.get("_debug_dir")
        if not debug:
            return
        x, y, cw, ch = self.rect()
        try:
            ImageGrab.grab(bbox=(x, y, x + cw, y + ch), all_screens=True).save(os.path.join(debug, f"{name}.png"))
        except OSError:
            pass

    def confirm_cancel_button(self):
        """[Esc] icon of the Cancel button in a centered confirmation dialog."""
        return self.icon("esc", self.region(0.35, 0.5, 0.7, 0.8))

    def dialog_names(self, nick):
        """The confirmation text contains the nickname (the rest of the text is not read)."""
        lines = self.ocr_latin.read(self.region(0.15, 0.38, 0.85, 0.58), OCR_SCALE)
        for ln in lines:
            words = [wd.text for wd in ln.words]
            for i in range(len(words)):
                for j in range(i + 1, min(i + 4, len(words) + 1)):   # OCR may split a nickname in up to 3 words
                    if same_nick("".join(words[i:j]), nick):
                        return True
        return False

    def roster_targets(self, nicks):
        """All of the given nicknames on the visible part of the members list.
        Returns ([(nick, OCR word)] top to bottom, page text)."""
        lines = self.ocr_latin.read(self.region(*ROSTER), OCR_SCALE)
        # a card's first line is "Nickname 3" (name + level); chat pop-ups over the list are longer
        firsts = [ln.words[0] for ln in lines if len(ln.words) <= 3]
        found = []
        for nick in nicks:
            wd = next((wd for wd in firsts if same_nick(wd.text, nick)), None) or \
                next((wd for wd in firsts if nick_matches(wd.text, nick)), None)
            if wd:
                found.append((nick, wd))
        found.sort(key=lambda f: (f[1].cy, f[1].cx))
        return found, " ".join(ln.text for ln in lines)

    def kick_all(self, nicks, limit, report, rehearse=False):
        """Goes down the members list once: every target visible on a page is kicked before the list is scrolled.
        report(nick, status, message) is called for each one; stops after `limit` kicks or 5 failures in a row.
        Targets that were never seen are reported as not found."""
        self.ensure_members_page()
        x, y, cw, ch = self.rect()
        list_center = (x + int(cw * 0.62), y + int(ch * 0.66))
        self.wheel(*list_center, 30)
        self.park_mouse()
        pending, done, fails = list(nicks), 0, 0
        prev, same = None, 0
        while pending and done < limit and fails < 5:
            self.sleep(450)
            self.close_chat()
            found, page = self.roster_targets(pending)
            if found:
                nick, wd = found[0]
                pending.remove(nick)
                status, msg = self.kick_card(wd, nick, rehearse)
                report(nick, status, msg)
                done, fails = (done + 1, 0) if status in ("ok", "rehearsed") else (done, fails + 1)
                if status not in ("ok", "rehearsed"):
                    self.ensure_members_page()
                prev, same = None, 0
                continue   # the same page again: cards moved up after a kick
            # the end of the list: the page did not change twice in a row (one unchanged page may be a slow scroll)
            same = same + 1 if page == prev else 0
            if same >= 2:
                break
            prev = page
            self.wheel(*list_center, -4)
            self.park_mouse()
        if done < limit and fails < 5:
            for nick in pending:
                report(nick, "not_found", t("r_kick_not_found"))

    def kick(self, nick, rehearse=False):
        """One member: finds the card, then kick_card()."""
        self.ensure_members_page()
        wd = self.find_member(nick)
        if not wd:
            return "not_found", t("r_kick_not_found")
        return self.kick_card(wd, nick, rehearse)

    def kick_card(self, wd, nick, rehearse=False):
        """Member menu -> its last item ("Kick", x icon) -> check the dialog names the player -> Space.
        rehearse: the whole way up to the confirmation, then Cancel (nobody is kicked). Returns (status, message)."""
        self.kicks = getattr(self, "kicks", 0) + 1
        shot = f"kick_{self.kicks:02}_{clean_nick(nick)}"
        before = {(ln.text, ln.cy // 8) for ln in self._menu_lines(wd)}

        def menu_now():
            return self.pick_menu([ln for ln in self._menu_lines(wd) if (ln.text, ln.cy // 8) not in before])

        menu = []
        for right in (False, True):  # the menu opens on a click; try the right button if the left one did nothing
            self.hover_click(wd.cx, wd.cy, right)
            self.sleep(600)
            menu = menu_now()
            if len(menu) >= 4:
                break
            if menu:
                self.key("esc")   # only when something did open: a bare Esc would close the clan page
                self.sleep(500)
        if len(menu) < 4:
            self.snapshot(shot + "_no_menu")
            return "no_menu", t("r_kick_no_menu")
        last = menu[-1]  # "Kick" is always the last item
        self.snapshot(shot + "_1_menu")
        self.hover_click(last.cx, last.cy)
        self.park_mouse()

        # confirmation: [Space] Accept / [Esc] Cancel, the text names the player
        cancel = None
        for _ in range(8):
            self.sleep(400)
            cancel = self.confirm_cancel_button()
            if cancel:
                break
        self.snapshot(shot + "_2_dialog")
        if not cancel:
            if len(menu_now()) >= 4:
                self.key("esc")   # the menu is still open
                self.sleep(500)
            return "no_dialog", t("r_kick_no_dialog")
        if not self.dialog_names(nick):
            self.key("esc")
            self.sleep(600)
            return "wrong_dialog", t("r_kick_wrong_dialog")
        if rehearse:
            self.key("esc")
            self.sleep(600)
            return "rehearsed", t("r_kick_rehearsed")
        self.key("space")
        self.sleep(1200)
        cancel = self.confirm_cancel_button()
        if cancel:  # Space did not work: click Accept, the button left of Cancel
            self.click(cancel.cx - int(203 * self.s()), cancel.cy)
            self.park_mouse()
            self.sleep(1200)
        if self.confirm_cancel_button():
            self.snapshot(shot + "_3_stuck")
            self.key("esc")
            self.sleep(500)
            return "not_confirmed", t("r_kick_not_confirmed")
        # the card disappears where it was; the whole list is not scrolled through again
        self.sleep(600)
        if self.roster_hit(nick, strict=True)[0]:
            self.snapshot(shot + "_3_still_there")
            return "not_confirmed", t("r_kick_not_confirmed")
        return "ok", t("r_kicked")
