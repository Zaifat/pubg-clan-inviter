"""Mouse/keyboard input through SendInput and screen pixel reading. Never touches game memory."""
import ctypes
import os
import subprocess
from ctypes import wintypes

GAME_EXE = "TslGame.exe"


user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32")
kernel32 = ctypes.WinDLL("kernel32")
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    user32.SetProcessDPIAware()

ULONG_PTR = ctypes.c_size_t


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
MOUSEEVENTF_MOVE, MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x0001, 0x0002, 0x0004
MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP = 0x0008, 0x0010
MOUSEEVENTF_VIRTUALDESK, MOUSEEVENTF_ABSOLUTE = 0x4000, 0x8000
KEYEVENTF_EXTENDEDKEY, KEYEVENTF_KEYUP, KEYEVENTF_UNICODE, KEYEVENTF_SCANCODE = 0x1, 0x2, 0x4, 0x8
SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN, SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 76, 77, 78, 79
WM_CLOSE, WM_INPUTLANGCHANGEREQUEST = 0x0010, 0x0050
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.GetDC.restype = wintypes.HDC
gdi32.GetPixel.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
gdi32.GetPixel.restype = wintypes.DWORD
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
user32.GetForegroundWindow.restype = wintypes.HWND
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.LoadKeyboardLayoutW.restype = wintypes.HKL
user32.VkKeyScanExW.argtypes = [wintypes.WCHAR, wintypes.HKL]
user32.VkKeyScanExW.restype = ctypes.c_short
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                ctypes.POINTER(wintypes.DWORD)]

US_LAYOUT = user32.LoadKeyboardLayoutW("00000409", 0x80)  # KLF_NOTELLSHELL: load without switching

VK = {
    "enter": 0x0D, "esc": 0x1B, "tab": 0x09, "backspace": 0x08, "delete": 0x2E, "space": 0x20,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27, "home": 0x24, "end": 0x23,
    "ctrl": 0x11, "shift": 0x10, "alt": 0x12,
    **{f"f{i}": 0x6F + i for i in range(1, 13)},
}
EXTENDED_VK = {0x2E, 0x26, 0x28, 0x25, 0x27, 0x24, 0x23}


def _send(*inputs):
    arr = (INPUT * len(inputs))(*inputs)
    user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT))


def _mouse(flags, x=0, y=0):
    inp = INPUT(type=INPUT_MOUSE)
    inp.u.mi = MOUSEINPUT(x, y, 0, flags, 0, 0)
    return inp


def _abs(x, y):
    vx, vy = user32.GetSystemMetrics(SM_XVIRTUALSCREEN), user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
    vw, vh = user32.GetSystemMetrics(SM_CXVIRTUALSCREEN), user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)
    return round((x - vx) * 65535 / (vw - 1)), round((y - vy) * 65535 / (vh - 1))


def mouse_move(x, y):
    ax, ay = _abs(x, y)
    _send(_mouse(MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK, ax, ay))


def mouse_button(down, right=False):
    if right:
        flag = MOUSEEVENTF_RIGHTDOWN if down else MOUSEEVENTF_RIGHTUP
    else:
        flag = MOUSEEVENTF_LEFTDOWN if down else MOUSEEVENTF_LEFTUP
    _send(_mouse(flag))


def _key_input(vk, up, scancode=True):
    inp = INPUT(type=INPUT_KEYBOARD)
    flags = KEYEVENTF_KEYUP if up else 0
    if vk in EXTENDED_VK:
        flags |= KEYEVENTF_EXTENDEDKEY
    if scancode:
        scan = user32.MapVirtualKeyW(vk, 0)
        inp.u.ki = KEYBDINPUT(0, scan, flags | KEYEVENTF_SCANCODE, 0, 0)
    else:
        inp.u.ki = KEYBDINPUT(vk, 0, flags, 0, 0)
    return inp


def key_event(vk, up):
    _send(_key_input(vk, up))


def type_char(ch, mode):
    if mode == "unicode":
        down, upk = INPUT(type=INPUT_KEYBOARD), INPUT(type=INPUT_KEYBOARD)
        down.u.ki = KEYBDINPUT(0, ord(ch), KEYEVENTF_UNICODE, 0, 0)
        upk.u.ki = KEYBDINPUT(0, ord(ch), KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, 0, 0)
        _send(down, upk)
        return
    res = user32.VkKeyScanExW(ch, US_LAYOUT)
    if res == -1:
        type_char(ch, "unicode")
        return
    vk, shift = res & 0xFF, bool(res & 0x100)
    seq = []
    if shift:
        seq.append(_key_input(VK["shift"], False))
    seq += [_key_input(vk, False), _key_input(vk, True)]
    if shift:
        seq.append(_key_input(VK["shift"], True))
    _send(*seq)


def key_pressed(name):
    vk = VK.get(name.lower())
    return bool(vk and user32.GetAsyncKeyState(vk) & 0x8000)


def get_pixel(x, y):
    dc = user32.GetDC(0)
    c = gdi32.GetPixel(dc, x, y)
    user32.ReleaseDC(0, dc)
    if c == 0xFFFFFFFF:
        return None
    return c & 0xFF, (c >> 8) & 0xFF, (c >> 16) & 0xFF


def cursor_pos():
    p = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(p))
    return p.x, p.y


def window_exe(hwnd):
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not h:
        return ""
    buf = ctypes.create_unicode_buffer(1024)
    size = wintypes.DWORD(1024)
    ok = kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size))
    kernel32.CloseHandle(h)
    return os.path.basename(buf.value) if ok else ""


def find_game_window():
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _):
        if user32.IsWindowVisible(hwnd) and window_exe(hwnd).lower() == GAME_EXE.lower():
            found.append(hwnd)
            return False
        return True

    user32.EnumWindows(cb, 0)
    return found[0] if found else None


def game_is_foreground():
    hwnd = user32.GetForegroundWindow()
    return bool(hwnd) and window_exe(hwnd).lower() == GAME_EXE.lower()


def game_running():
    out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {GAME_EXE}", "/NH"],
                         capture_output=True, text=True, creationflags=0x08000000).stdout
    return GAME_EXE.lower() in out.lower()


