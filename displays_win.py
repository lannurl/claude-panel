"""Экраны Windows для вкладки «Мониторы» (docs/monitors-spec.md, §5.2, §9.1, §12.1).

- list_displays()          — экраны, пиксели ОС, масштаб, физический размер из EDID (реестр, без админа);
- set_display_positions()  — DM_POSITION (раскладка Windows); dry_run=True ничего не меняет;
- ease_cursor_setting()    — «Облегчить перемещение курсора между дисплеями» (только подсказка);
- EdgeHelper               — помощник краёв: опрос GetCursorPos + Raw Input («дожим») + SetCursorPos.

Помощник НЕ ставит глобальных хуков (никакого WH_MOUSE_LL): системная мышь от него не зависит. Если поток
завис или процесс упал — курсор ведёт себя как обычно, просто без поправки по физике.
Подключается через platform_win (импорт в try).
"""
import ctypes
import sys
import threading
import time
import winreg
from collections import deque
from ctypes import wintypes

import layout

_u32 = ctypes.WinDLL("user32", use_last_error=True)
_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
try:
    _shcore = ctypes.WinDLL("shcore")
except OSError:
    _shcore = None

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

DEFAULT_VIRTUAL_MATCH = ("SudoVDA", "Virtual Display", "SudoMaker")
MAX_WARPS_PER_S = 10        # аварийная остановка: больше переносов за 1 с — помощник встаёт на паузу
PUSH_WINDOW_S = layout.CORNER_PUSH_WINDOW_S
IDLE_WAIT_MS = 50           # пауза / нет правил — просыпаемся редко


def log(msg):
    try:
        if sys.stderr:
            print(f"[panel/edge] {msg}", file=sys.stderr, flush=True)
    except Exception:
        pass


def _iso_now():
    return layout.now_iso()


# ── Структуры ──
class DISPLAY_DEVICEW(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("DeviceName", wintypes.WCHAR * 32), ("DeviceString", wintypes.WCHAR * 128),
                ("StateFlags", wintypes.DWORD), ("DeviceID", wintypes.WCHAR * 128), ("DeviceKey", wintypes.WCHAR * 128)]


class DEVMODEW(ctypes.Structure):  # вариант для дисплеев (sizeof = 220)
    _fields_ = [("dmDeviceName", wintypes.WCHAR * 32), ("dmSpecVersion", wintypes.WORD),
                ("dmDriverVersion", wintypes.WORD), ("dmSize", wintypes.WORD), ("dmDriverExtra", wintypes.WORD),
                ("dmFields", wintypes.DWORD), ("dmPositionX", wintypes.LONG), ("dmPositionY", wintypes.LONG),
                ("dmDisplayOrientation", wintypes.DWORD), ("dmDisplayFixedOutput", wintypes.DWORD),
                ("dmColor", ctypes.c_short), ("dmDuplex", ctypes.c_short), ("dmYResolution", ctypes.c_short),
                ("dmTTOption", ctypes.c_short), ("dmCollate", ctypes.c_short), ("dmFormName", wintypes.WCHAR * 32),
                ("dmLogPixels", wintypes.WORD), ("dmBitsPerPel", wintypes.DWORD), ("dmPelsWidth", wintypes.DWORD),
                ("dmPelsHeight", wintypes.DWORD), ("dmDisplayFlags", wintypes.DWORD),
                ("dmDisplayFrequency", wintypes.DWORD), ("dmICMMethod", wintypes.DWORD),
                ("dmICMIntent", wintypes.DWORD), ("dmMediaType", wintypes.DWORD), ("dmDitherType", wintypes.DWORD),
                ("dmReserved1", wintypes.DWORD), ("dmReserved2", wintypes.DWORD),
                ("dmPanningWidth", wintypes.DWORD), ("dmPanningHeight", wintypes.DWORD)]


assert ctypes.sizeof(DEVMODEW) == 220


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("style", wintypes.UINT), ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE),
                ("hIcon", wintypes.HICON), ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR), ("hIconSm", wintypes.HICON)]


class RAWINPUTDEVICE(ctypes.Structure):
    _fields_ = [("usUsagePage", wintypes.USHORT), ("usUsage", wintypes.USHORT), ("dwFlags", wintypes.DWORD),
                ("hwndTarget", wintypes.HWND)]


class RAWINPUTHEADER(ctypes.Structure):
    _fields_ = [("dwType", wintypes.DWORD), ("dwSize", wintypes.DWORD), ("hDevice", wintypes.HANDLE),
                ("wParam", wintypes.WPARAM)]


class RAWMOUSE(ctypes.Structure):
    _fields_ = [("usFlags", wintypes.USHORT), ("ulButtons", wintypes.ULONG), ("ulRawButtons", wintypes.ULONG),
                ("lLastX", wintypes.LONG), ("lLastY", wintypes.LONG), ("ulExtraInformation", wintypes.ULONG)]


class RAWINPUT_MOUSE(ctypes.Structure):
    _fields_ = [("header", RAWINPUTHEADER), ("mouse", RAWMOUSE)]


# ── Прототипы ──
_u32.EnumDisplayDevicesW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(DISPLAY_DEVICEW), wintypes.DWORD]
_u32.EnumDisplayDevicesW.restype = wintypes.BOOL
_u32.EnumDisplaySettingsExW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(DEVMODEW), wintypes.DWORD]
_u32.EnumDisplaySettingsExW.restype = wintypes.BOOL
_u32.ChangeDisplaySettingsExW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(DEVMODEW), wintypes.HWND, wintypes.DWORD,
                                          wintypes.LPVOID]
_u32.ChangeDisplaySettingsExW.restype = wintypes.LONG
_u32.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
_u32.MonitorFromPoint.restype = wintypes.HANDLE
_u32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
_u32.GetCursorPos.restype = wintypes.BOOL
_u32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
_u32.SetCursorPos.restype = wintypes.BOOL
_u32.GetAsyncKeyState.argtypes = [ctypes.c_int]
_u32.GetAsyncKeyState.restype = ctypes.c_short
_u32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
_u32.DefWindowProcW.restype = LRESULT
_u32.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEXW)]
_u32.RegisterClassExW.restype = wintypes.ATOM
_u32.UnregisterClassW.argtypes = [wintypes.LPCWSTR, wintypes.HINSTANCE]
_u32.UnregisterClassW.restype = wintypes.BOOL
_u32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_int,
                                 ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND, wintypes.HMENU,
                                 wintypes.HINSTANCE, wintypes.LPVOID]
_u32.CreateWindowExW.restype = wintypes.HWND
_u32.DestroyWindow.argtypes = [wintypes.HWND]
_u32.DestroyWindow.restype = wintypes.BOOL
_u32.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT]
_u32.PeekMessageW.restype = wintypes.BOOL
_u32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
_u32.DispatchMessageW.restype = LRESULT
_u32.MsgWaitForMultipleObjects.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE), wintypes.BOOL,
                                           wintypes.DWORD, wintypes.DWORD]
_u32.MsgWaitForMultipleObjects.restype = wintypes.DWORD
_u32.RegisterRawInputDevices.argtypes = [ctypes.POINTER(RAWINPUTDEVICE), wintypes.UINT, wintypes.UINT]
_u32.RegisterRawInputDevices.restype = wintypes.BOOL
_u32.GetRawInputData.argtypes = [wintypes.HANDLE, wintypes.UINT, wintypes.LPVOID, ctypes.POINTER(wintypes.UINT),
                                 wintypes.UINT]
_u32.GetRawInputData.restype = wintypes.UINT
_k32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
_k32.GetModuleHandleW.restype = wintypes.HMODULE
_k32.CreateEventW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
_k32.CreateEventW.restype = wintypes.HANDLE
_k32.SetEvent.argtypes = [wintypes.HANDLE]
_k32.SetEvent.restype = wintypes.BOOL
_k32.CloseHandle.argtypes = [wintypes.HANDLE]
_k32.CloseHandle.restype = wintypes.BOOL
_k32.SetWaitableTimer.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.LARGE_INTEGER), wintypes.LONG,
                                  wintypes.LPVOID, wintypes.LPVOID, wintypes.BOOL]
_k32.SetWaitableTimer.restype = wintypes.BOOL
try:
    _k32.CreateWaitableTimerExW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    _k32.CreateWaitableTimerExW.restype = wintypes.HANDLE
except AttributeError:
    pass
if _shcore is not None:
    _shcore.GetDpiForMonitor.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.POINTER(wintypes.UINT),
                                         ctypes.POINTER(wintypes.UINT)]
    _shcore.GetDpiForMonitor.restype = ctypes.c_long

ENUM_CURRENT_SETTINGS = 0xFFFFFFFF
DISPLAY_DEVICE_ATTACHED_TO_DESKTOP = 0x1
DISPLAY_DEVICE_PRIMARY_DEVICE = 0x4
DISPLAY_DEVICE_ACTIVE = 0x1
EDD_GET_DEVICE_INTERFACE_NAME = 0x1
DM_POSITION = 0x20
CDS_UPDATEREGISTRY = 0x1
CDS_NORESET = 0x10000000
DISP_CHANGE = {0: "успешно", 1: "нужна перезагрузка", -1: "ошибка драйвера", -2: "режим не поддерживается",
               -3: "ошибка записи в реестр", -4: "неверные флаги", -5: "неверные параметры", -6: "BADDUALVIEW"}
WM_INPUT, WM_DISPLAYCHANGE = 0x00FF, 0x007E
RID_INPUT, RIM_TYPEMOUSE, MOUSE_MOVE_ABSOLUTE = 0x10000003, 0, 0x1
RIDEV_INPUTSINK, RIDEV_REMOVE = 0x100, 0x1
QS_ALLINPUT, PM_REMOVE = 0x04FF, 0x1
WAIT_TIMEOUT, WAIT_FAILED = 0x102, 0xFFFFFFFF
WS_EX_TOOLWINDOW, WS_POPUP = 0x80, 0x80000000
CREATE_WAITABLE_TIMER_HIGH_RESOLUTION, TIMER_ALL_ACCESS = 0x2, 0x1F0003


# ── Список экранов ──
def _edid(pnp, instance):
    path = rf"SYSTEM\CurrentControlSet\Enum\DISPLAY\{pnp}\{instance}\Device Parameters"
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path) as k:
            v, _ = winreg.QueryValueEx(k, "EDID")
            return bytes(v)
    except OSError:
        return None


def _scale_at(x, y):
    if _shcore is None:
        return None
    try:
        h = _u32.MonitorFromPoint(wintypes.POINT(x, y), 2)
        dx, dy = wintypes.UINT(), wintypes.UINT()
        if _shcore.GetDpiForMonitor(h, 0, ctypes.byref(dx), ctypes.byref(dy)) == 0 and dx.value:
            return int(round(dx.value * 100 / 96))
    except Exception:
        pass
    return None


def _current_mode(gdi):
    dm = DEVMODEW()
    dm.dmSize = ctypes.sizeof(DEVMODEW)
    if not _u32.EnumDisplaySettingsExW(gdi, ENUM_CURRENT_SETTINGS, ctypes.byref(dm), 0):
        return None
    return dm


def list_displays(virtual_match=None):
    """Экраны, подключённые к рабочему столу. Ошибка — пустой список (с записью в лог)."""
    try:
        return _list_displays(tuple(virtual_match or DEFAULT_VIRTUAL_MATCH))
    except Exception as e:
        log(f"list_displays: {type(e).__name__}")
        return []


def _list_displays(vmatch):
    out = []
    i = 0
    while i < 64:
        ad = DISPLAY_DEVICEW()
        ad.cb = ctypes.sizeof(ad)
        if not _u32.EnumDisplayDevicesW(None, i, ctypes.byref(ad), 0):
            break
        i += 1
        if not ad.StateFlags & DISPLAY_DEVICE_ATTACHED_TO_DESKTOP:
            continue
        dm = _current_mode(ad.DeviceName)
        if dm is None:
            continue
        rect = [int(dm.dmPositionX), int(dm.dmPositionY), int(dm.dmPelsWidth), int(dm.dmPelsHeight)]
        mon = DISPLAY_DEVICEW()
        mon.cb = ctypes.sizeof(mon)
        found = False
        j = 0
        while _u32.EnumDisplayDevicesW(ad.DeviceName, j, ctypes.byref(mon), EDD_GET_DEVICE_INTERFACE_NAME):
            if mon.StateFlags & DISPLAY_DEVICE_ACTIVE:
                found = True
                break
            j += 1
            mon = DISPLAY_DEVICEW()
            mon.cb = ctypes.sizeof(mon)
        pnp, instance = None, None
        if found:
            parts = mon.DeviceID.split("#")  # \\?\DISPLAY#GSM5B7F#<instance>#{guid}
            if len(parts) >= 3 and parts[0].upper().endswith("DISPLAY"):
                pnp, instance = parts[1], parts[2]
        info = layout.parse_edid(_edid(pnp, instance), px=(rect[2], rect[3])) if pnp else \
            {"mm": None, "name": None, "serial": None, "pnp": None}
        pnp = pnp or info.get("pnp") or "UNKNOWN"
        serial = info.get("serial")
        if isinstance(serial, int) and serial:
            os_id = f"win:{pnp}:{serial:08X}"
        else:
            uid = (instance or ad.DeviceName).split("&")[-1]
            os_id = f"win:{pnp}:00000000:{uid}"
        names = " ".join(filter(None, [ad.DeviceString, mon.DeviceString if found else "", info.get("name") or ""]))
        virtual = any(v.lower() in names.lower() for v in vmatch if v)
        out.append({
            "os_id": os_id[:128], "gdi_name": ad.DeviceName,
            "label": info.get("name") or (mon.DeviceString if found else "") or ad.DeviceString or ad.DeviceName,
            "px": [rect[2], rect[3]], "rect_px": rect,
            "primary": bool(ad.StateFlags & DISPLAY_DEVICE_PRIMARY_DEVICE),
            "scale": _scale_at(rect[0] + rect[2] // 2, rect[1] + rect[3] // 2) or 100,
            "size_mm": list(info["mm"]) if info.get("mm") else None,
            "virtual": virtual,
        })
    return out


# ── Раскладка Windows (DM_POSITION) ──
check_positions = layout.check_os_positions


def set_display_positions(pos, dry_run=False, displays=None):
    """Поставить экраны в позиции {os_id: (x, y)}. Основной остаётся в (0, 0).

    dry_run=True — только проверка и план, ChangeDisplaySettingsEx не вызывается.
    Возврат {"ok","code","err","previous": {os_id: (x, y)}, "plan": [...]}.
    """
    try:
        return _set_positions(pos, dry_run, displays)
    except Exception as e:
        log(f"set_display_positions: {type(e).__name__}")
        return {"ok": False, "code": "os_error", "err": "Не удалось изменить раскладку Windows", "previous": {}}


def _set_positions(pos, dry_run, displays):
    if not isinstance(pos, dict) or not pos:
        return {"ok": False, "code": "bad_layout", "err": "Нет позиций для применения", "previous": {}}
    ds = displays if displays is not None else list_displays()
    by_id = {d["os_id"]: d for d in ds}
    unknown = [k for k in pos if k not in by_id]
    if unknown:
        return {"ok": False, "code": "bad_layout", "err": "Экран из раскладки не подключён", "previous": {}}
    prim = next((d for d in ds if d["primary"]), None)
    if prim is None:
        return {"ok": False, "code": "os_error", "err": "Основной экран не найден", "previous": {}}
    try:
        newpos = {k: (int(v[0]), int(v[1])) for k, v in pos.items()}
    except (TypeError, ValueError, IndexError):
        return {"ok": False, "code": "bad_layout", "err": "Неверные координаты", "previous": {}}
    sx, sy = newpos.get(prim["os_id"], (0, 0))
    newpos = {k: (x - sx, y - sy) for k, (x, y) in newpos.items()}
    rects = {}
    for d in ds:
        x, y = newpos.get(d["os_id"], tuple(d["rect_px"][:2]))
        if abs(x) > 32000 or abs(y) > 32000:
            return {"ok": False, "code": "bad_layout", "err": "Координаты вне допустимого диапазона", "previous": {}}
        rects[d["os_id"]] = (x, y, d["rect_px"][2], d["rect_px"][3])
    err = check_positions(rects, prim["os_id"])
    previous = {d["os_id"]: tuple(d["rect_px"][:2]) for d in ds}
    if err:
        return {"ok": False, "code": "bad_layout", "err": f"Раскладка не применена: {err}", "previous": previous}
    plan = [{"os_id": d["os_id"], "gdi_name": d["gdi_name"], "x": rects[d["os_id"]][0], "y": rects[d["os_id"]][1]}
            for d in ds if not d["primary"] and tuple(rects[d["os_id"]][:2]) != tuple(d["rect_px"][:2])]
    if dry_run or not plan:
        return {"ok": True, "code": None, "err": None, "previous": previous, "plan": plan, "dry_run": bool(dry_run)}
    done = []
    for step in plan:
        r = _write_position(step["gdi_name"], step["x"], step["y"])
        if r != 0:
            log(f"ChangeDisplaySettingsEx: {r} ({DISP_CHANGE.get(r, '?')})")
            for d in done:  # вернуть в реестр прежние значения, ничего не применяя
                x, y = previous[d["os_id"]]
                _write_position(d["gdi_name"], x, y)
            return {"ok": False, "code": "os_error", "err": f"Windows отклонила раскладку ({DISP_CHANGE.get(r, r)})",
                    "previous": previous, "plan": plan}
        done.append(step)
    r = _u32.ChangeDisplaySettingsExW(None, None, None, 0, None)
    if r != 0:
        log(f"ChangeDisplaySettingsEx(apply): {r}")
        return {"ok": False, "code": "os_error", "err": f"Windows не применила раскладку ({DISP_CHANGE.get(r, r)})",
                "previous": previous, "plan": plan}
    log(f"раскладка Windows изменена: {len(plan)} экр.")
    return {"ok": True, "code": None, "err": None, "previous": previous, "plan": plan, "dry_run": False}


def _write_position(gdi, x, y):
    dm = _current_mode(gdi)
    if dm is None:
        return -5
    dm.dmFields = DM_POSITION
    dm.dmPositionX, dm.dmPositionY = int(x), int(y)
    return _u32.ChangeDisplaySettingsExW(gdi, ctypes.byref(dm), None, CDS_UPDATEREGISTRY | CDS_NORESET, None)


def ease_cursor_setting():
    """True/False — «Облегчить перемещение курсора между дисплеями»; None — значение не задано/не прочитано (НП)."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Control Panel\Cursors") as k:
            v, _ = winreg.QueryValueEx(k, "CursorDeadzoneJumpingSetting")
            return bool(int(v))
    except (OSError, ValueError, TypeError):
        return None


# ── Помощник краёв ──
class EdgeHelper:
    """Поток edge-helper: скрытое окно (Raw Input + WM_DISPLAYCHANGE), опрос курсора, перенос по правилам.

    cfg — секция edge_helper (poll_ms, push_mickeys, inset_px, max_step_px, skip_while_button_down).
    on_event(kind, payload): "displays_changed" (None), "warning" ({"text"}), "edge_warp" ({"src","dst"}).
    """

    def __init__(self, rules, rects, cfg, on_event=None):
        self._lock = threading.Lock()
        self._rules = tuple(rules or ())
        self._rects = dict(rects or {})
        self._cfg = dict(layout.DEFAULT_EXTRA["edge_helper"])
        self._cfg.update({k: v for k, v in (cfg or {}).items() if k in self._cfg})
        self._on_event = on_event
        self._thread = None
        self._stop = threading.Event()
        self._hstop = None
        self._paused = False
        self._tripped = False
        self._err = None
        self._warps = 0
        self._last_warp = None
        self._raw_ok = False
        self._push = deque()
        self._warp_times = deque()
        self._prev = None
        self._wndproc = None
        self._ticks = 0
        self._client_state = "local"   # §7.4.1: local | client_relative | client_absolute
        self._last_jump = None         # (warp_from, warp_to, тиков после переноса) — для layout.snapback

    # публичное API
    def start(self):
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._tripped = False
            self._err = None
            self._hstop = _k32.CreateEventW(None, True, False, None)
            self._thread = threading.Thread(target=self._run, name="edge-helper", daemon=True)
            self._thread.start()
        log("запущен")

    def stop(self):
        with self._lock:
            t, h = self._thread, self._hstop
            self._stop.set()
        if h:
            _k32.SetEvent(h)
        if t and t is not threading.current_thread():
            t.join(2.0)
            if t.is_alive():
                log("поток не завершился за 2 с")
        with self._lock:
            if self._hstop and not (t and t.is_alive()):
                _k32.CloseHandle(self._hstop)
                self._hstop = None
            self._thread = None
        log("остановлен")

    def set_rules(self, rules, rects):
        with self._lock:
            self._rules = tuple(rules or ())
            self._rects = dict(rects or {})
            self._prev = None

    def pause(self, flag):
        with self._lock:
            self._paused = bool(flag)
            self._prev = None

    def set_client_state(self, state):
        """Подсостояние курсора (§7.4.1, версия 1.1). client_absolute — пауза. Определяет его ctl (этап 2/2b/4b)."""
        if state not in layout.CLIENT_STATES:
            return False
        with self._lock:
            self._client_state = state
            self._prev = None
            self._last_jump = None
        return True

    def status(self):
        with self._lock:
            return {"running": bool(self._thread and self._thread.is_alive()),
                    "paused": self._paused or self._tripped, "err": self._err, "warps": self._warps,
                    "last_warp": dict(self._last_warp) if self._last_warp else None, "raw_input": self._raw_ok,
                    "tripped": self._tripped, "client_state": self._client_state}

    # внутреннее
    def _emit(self, kind, payload):
        cb = self._on_event
        if cb:
            try:
                cb(kind, payload)
            except Exception as e:
                log(f"on_event: {type(e).__name__}")

    def _proc(self, hwnd, msg, wparam, lparam):
        try:
            if msg == WM_INPUT:
                self._on_raw(lparam)
            elif msg == WM_DISPLAYCHANGE:
                with self._lock:
                    self._rules = ()   # до пересчёта правил панелью ничего не переносим
                    self._prev = None
                self._emit("displays_changed", None)
        except Exception as e:
            log(f"wndproc: {type(e).__name__}")
        return _u32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _on_raw(self, lparam):
        ri = RAWINPUT_MOUSE()
        size = wintypes.UINT(ctypes.sizeof(ri))
        n = _u32.GetRawInputData(lparam, RID_INPUT, ctypes.byref(ri), ctypes.byref(size),
                                 ctypes.sizeof(RAWINPUTHEADER))
        if n == 0xFFFFFFFF or n < ctypes.sizeof(RAWINPUTHEADER) or ri.header.dwType != RIM_TYPEMOUSE:
            return
        if ri.mouse.usFlags & MOUSE_MOVE_ABSOLUTE:  # планшет/RDP — абсолютные координаты, не «дожим»
            return
        dx, dy = ri.mouse.lLastX, ri.mouse.lLastY
        if dx or dy:
            self._push.append((time.perf_counter(), dx, dy))

    def _push_sum(self, now_t):
        q = self._push
        while q and now_t - q[0][0] > PUSH_WINDOW_S:
            q.popleft()
        return sum(p[1] for p in q), sum(p[2] for p in q)

    def _run(self):
        hwnd = timer = None
        cls = "MyPanelEdgeHelper"
        hinst = _k32.GetModuleHandleW(None)
        registered = False
        try:
            try:  # в этом потоке — координаты в физических пикселях независимо от процесса
                _u32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
            except Exception:
                pass
            self._wndproc = WNDPROC(self._proc)
            wc = WNDCLASSEXW()
            wc.cbSize = ctypes.sizeof(wc)
            wc.lpfnWndProc = self._wndproc
            wc.hInstance = hinst
            wc.lpszClassName = cls
            if not _u32.RegisterClassExW(ctypes.byref(wc)):
                if ctypes.get_last_error() != 1410:  # класс уже есть — переиспользуем
                    raise OSError("RegisterClassExW")
                _u32.UnregisterClassW(cls, hinst)
                if not _u32.RegisterClassExW(ctypes.byref(wc)):
                    raise OSError("RegisterClassExW")
            registered = True
            hwnd = _u32.CreateWindowExW(WS_EX_TOOLWINDOW, cls, "", WS_POPUP, 0, 0, 0, 0, None, None, hinst, None)
            if not hwnd:
                raise OSError("CreateWindowExW")
            rid = RAWINPUTDEVICE(1, 2, RIDEV_INPUTSINK, hwnd)
            self._raw_ok = bool(_u32.RegisterRawInputDevices(ctypes.byref(rid), 1, ctypes.sizeof(rid)))
            if not self._raw_ok:
                log(f"Raw Input недоступен ({ctypes.get_last_error()}) — работаю без «дожима»")
            try:
                timer = _k32.CreateWaitableTimerExW(None, None, CREATE_WAITABLE_TIMER_HIGH_RESOLUTION,
                                                    TIMER_ALL_ACCESS)
            except Exception:
                timer = None
            self._loop(timer)
        except Exception as e:
            log(f"поток упал: {type(e).__name__}: {e}")
            with self._lock:
                self._err = "Помощник краёв остановлен из-за ошибки"
            self._emit("warning", {"text": "Помощник краёв остановлен из-за ошибки"})
        finally:
            if self._raw_ok:
                rid = RAWINPUTDEVICE(1, 2, RIDEV_REMOVE, None)
                _u32.RegisterRawInputDevices(ctypes.byref(rid), 1, ctypes.sizeof(rid))
                self._raw_ok = False
            if hwnd:
                _u32.DestroyWindow(hwnd)
            if registered:
                _u32.UnregisterClassW(cls, hinst)
            if timer:
                _k32.CloseHandle(timer)

    def _loop(self, timer):
        msg = wintypes.MSG()
        pt = wintypes.POINT()
        last_tick = 0.0
        while not self._stop.is_set():
            with self._lock:
                active = bool(self._rules) and not self._paused and not self._tripped \
                    and self._client_state != "client_absolute"
                poll = int(self._cfg["poll_ms"])
            if active and timer:
                due = wintypes.LARGE_INTEGER(-poll * 10000)
                _k32.SetWaitableTimer(timer, ctypes.byref(due), 0, None, None, False)
                handles = (wintypes.HANDLE * 2)(self._hstop, timer)
                r = _u32.MsgWaitForMultipleObjects(2, handles, False, poll + 20, QS_ALLINPUT)
            else:
                handles = (wintypes.HANDLE * 1)(self._hstop)
                r = _u32.MsgWaitForMultipleObjects(1, handles, False, poll if active else IDLE_WAIT_MS, QS_ALLINPUT)
            if r == WAIT_FAILED:
                time.sleep(poll / 1000)
            if self._stop.is_set():
                break
            n = 0
            while n < 512 and _u32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
                _u32.DispatchMessageW(ctypes.byref(msg))
                n += 1
            if not active:
                self._push.clear()
                self._prev = None
                continue
            t = time.perf_counter()
            if t - last_tick < 0.001:
                continue
            last_tick = t
            self._tick(pt, t)

    def _buttons_down(self):
        return any(_u32.GetAsyncKeyState(vk) & 0x8000 for vk in (0x01, 0x02, 0x04))

    def _tick(self, pt, t):
        self._ticks += 1
        if not _u32.GetCursorPos(ctypes.byref(pt)):  # экран блокировки / UAC — пропускаем
            self._prev = None
            return
        now = (pt.x, pt.y)
        with self._lock:
            rules, rects, cfg = self._rules, self._rects, self._cfg
            prev = self._prev
        dcfg = {"max_step_px": cfg["max_step_px"], "push_mickeys": cfg["push_mickeys"], "paused": False,
                "skip_while_button_down": cfg["skip_while_button_down"],
                "button_down": self._buttons_down() if cfg["skip_while_button_down"] else False}
        push = self._push_sum(t) if self._raw_ok else None
        lj = self._last_jump
        if lj is not None:  # эвристика отката: только когда мы Deskflow-клиент (client_state ≠ local)
            if layout.snapback(lj[0], lj[1], now):
                with self._lock:
                    self._client_state = "client_absolute"
                log("откат после переноса — курсор ведёт сервер Deskflow (client_absolute)")
                self._emit("edge_client_state", {"client_state": "client_absolute"})
                self._last_jump = None
                self._prev = None
                return
            self._last_jump = None if lj[2] >= 2 else (lj[0], lj[1], lj[2] + 1)
        res = layout.edge_decide_ex(rules, rects, prev, now, push, dcfg)
        if not res:
            self._prev = now
            return
        (tx, ty), rule = res
        wt = self._warp_times
        while wt and t - wt[0] > 1.0:
            wt.popleft()
        if len(wt) >= MAX_WARPS_PER_S:
            with self._lock:
                self._tripped = True
                self._err = "Аварийная пауза: слишком много переносов курсора подряд"
            log(f"аварийная пауза: {len(wt)} переносов за 1 с")
            self._emit("warning", {"text": "Помощник краёв на паузе: слишком много переносов подряд. "
                                           "Выключите и включите его снова."})
            self._prev = None
            return
        if _u32.SetCursorPos(int(tx), int(ty)):
            wt.append(t)
            self._push.clear()
            self._prev = (int(tx), int(ty))  # свой перенос — не переход
            if self._client_state != "local":
                self._last_jump = (now, self._prev, 0)
            with self._lock:
                self._warps += 1
                self._last_warp = {"src": rule.src, "dst": rule.dst, "at": _iso_now()}
        else:
            self._prev = now


def measure_loop_rate(seconds=1.0):
    """Диагностика для ручной проверки: сколько раз в секунду просыпается ожидание с таймером (без курсора)."""
    timer = _k32.CreateWaitableTimerExW(None, None, CREATE_WAITABLE_TIMER_HIGH_RESOLUTION, TIMER_ALL_ACCESS)
    n, t0 = 0, time.perf_counter()
    try:
        while time.perf_counter() - t0 < seconds:
            due = wintypes.LARGE_INTEGER(-4 * 10000)
            _k32.SetWaitableTimer(timer, ctypes.byref(due), 0, None, None, False)
            h = (wintypes.HANDLE * 1)(timer)
            _u32.MsgWaitForMultipleObjects(1, h, False, 30, 0)
            n += 1
    finally:
        if timer:
            _k32.CloseHandle(timer)
    return n / seconds


if __name__ == "__main__":
    for d in list_displays():
        print({k: v for k, v in d.items()})
    print("ease_cursor:", ease_cursor_setting())
    print("loop rate, Гц:", round(measure_loop_rate(), 1))
