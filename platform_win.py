"""Платформенный слой панели для Windows: WinAPI (ctypes), медиа-центр SMTC (WinRT), VS Code.

Импортируется из panel.pyw только при sys.platform == "win32". При импорте включает DPI awareness
(до создания окна tk — иначе координаты будут логическими, а не физическими).
"""
import asyncio
import ctypes
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path

NAME = "windows"

try:  # живое видео из окна браузера (DWM thumbnail); нет модуля — показываем обложку
    from video_mirror import VideoMirror
except Exception:
    VideoMirror = None

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass

HOTKEY_LABEL = "Ctrl+Alt+P"
HOTKEY_DEFAULT = True  # RegisterHotKey безопасен — включён по умолчанию
COPY_LABEL = "Ctrl+C"
RIGHT_CLICK = ("<Button-3>",)
WITHDRAW_WHEN_HIDDEN = False   # окно без рамки спокойно уезжает за верхний край экрана
EXTRA_PASSWORD_APPS = ()       # к общему списку менеджеров паролей ничего не добавляем

# Шрифты: роль -> (варианты семейства по приоритету, размер, доп. параметры)
FONTS = {
    "base": (("Segoe UI",), 10, {}),
    "small": (("Segoe UI",), 9, {}),
    "tiny": (("Segoe UI",), 8, {}),
    "bold": (("Segoe UI Semibold",), 10, {}),
    "track": (("Segoe UI Semibold",), 13, {}),
    "title": (("Cascadia Mono",), 11, {"weight": "bold"}),
    "mono": (("Cascadia Mono",), 9, {}),
    "num": (("Cascadia Mono",), 13, {"weight": "bold"}),
    "big": (("Segoe UI Symbol",), 18, {}),
}


def log(msg):
    """Технические события в stderr (под pythonw stderr = None). Без токенов и персональных данных."""
    try:
        if sys.stderr:
            print(f"[panel/win] {msg}", file=sys.stderr, flush=True)
    except Exception:
        pass


# ── Один экземпляр ──
_mutex = None


def single_instance():
    """True — мы единственный экземпляр (именованный mutex), False — панель уже запущена."""
    global _mutex
    _mutex = kernel32.CreateMutexW(None, False, "MyPanel-single-instance")
    return kernel32.GetLastError() != 183  # ERROR_ALREADY_EXISTS


def ui_scale(root):
    """Масштаб интерфейса: процесс DPI-aware, tk отдаёт физический DPI."""
    return root.winfo_fpixels("1i") / 96


def prepare_window(root):
    root.overrideredirect(True)
    root.attributes("-topmost", True)


def wheel_units(delta):
    return int(-delta / 120)


# ── Курсор и мониторы ──
class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]


user32.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
user32.MonitorFromPoint.restype = wintypes.HANDLE
user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
user32.GetAncestor.restype = wintypes.HWND


def cursor_pos(root=None):
    pt = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


def monitor_rect(x, y, root=None):
    h = user32.MonitorFromPoint(wintypes.POINT(x, y), 2)
    mi = MONITORINFO()
    mi.cbSize = ctypes.sizeof(MONITORINFO)
    user32.GetMonitorInfoW(h, ctypes.byref(mi))
    r = mi.rcMonitor
    return r.left, r.top, r.right, r.bottom


# ── Окна, процессы, буфер обмена ──
def foreground_title():
    hwnd = user32.GetForegroundWindow()
    buf = ctypes.create_unicode_buffer(512)
    user32.GetWindowTextW(hwnd, buf, 512)
    return buf.value


def pid_alive(pid):
    h = kernel32.OpenProcess(0x1000, False, int(pid))
    if not h:
        return False
    code = wintypes.DWORD()
    kernel32.GetExitCodeProcess(h, ctypes.byref(code))
    kernel32.CloseHandle(h)
    return code.value == 259


def clipboard_seq(root=None):
    """Счётчик изменений буфера обмена: меняется при каждом копировании в любой программе."""
    return user32.GetClipboardSequenceNumber()


# Свой экземпляр user32/kernel32 с argtypes — общий ctypes.windll.user32 не трогаем
_cb_user32 = ctypes.WinDLL("user32", use_last_error=True)
_cb_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_cb_user32.RegisterClipboardFormatW.argtypes = [wintypes.LPCWSTR]
_cb_user32.RegisterClipboardFormatW.restype = wintypes.UINT
_cb_user32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
_cb_user32.IsClipboardFormatAvailable.restype = wintypes.BOOL
_cb_user32.OpenClipboard.argtypes = [wintypes.HWND]
_cb_user32.OpenClipboard.restype = wintypes.BOOL
_cb_user32.CloseClipboard.restype = wintypes.BOOL
_cb_user32.GetClipboardData.argtypes = [wintypes.UINT]
_cb_user32.GetClipboardData.restype = wintypes.HANDLE
_cb_kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
_cb_kernel32.GlobalLock.restype = ctypes.c_void_p
_cb_kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
_cb_kernel32.GlobalUnlock.restype = wintypes.BOOL
_cb_kernel32.GlobalSize.argtypes = [wintypes.HGLOBAL]
_cb_kernel32.GlobalSize.restype = ctypes.c_size_t

# Форматы, которыми менеджеры паролей (в т.ч. расширения браузера) помечают секреты
_CF_EXCLUDE = [_cb_user32.RegisterClipboardFormatW(n) for n in
               ("ExcludeClipboardContentFromMonitorProcessing", "Clipboard Viewer Ignore")]
_CF_HISTORY = _cb_user32.RegisterClipboardFormatW("CanIncludeInClipboardHistory")


def _history_flag():
    """Значение CanIncludeInClipboardHistory (DWORD) или None, если прочитать не удалось."""
    for _ in range(5):  # буфер может быть ненадолго занят другой программой
        if _cb_user32.OpenClipboard(None):
            break
        time.sleep(0.02)
    else:
        return None
    try:
        h = _cb_user32.GetClipboardData(_CF_HISTORY)
        if not h or _cb_kernel32.GlobalSize(h) < 4:
            return None
        p = _cb_kernel32.GlobalLock(h)
        if not p:
            return None
        try:
            return ctypes.c_uint32.from_address(p).value
        finally:
            _cb_kernel32.GlobalUnlock(h)
    finally:
        _cb_user32.CloseClipboard()


def clipboard_excluded(root=None):
    """True — содержимое буфера помечено как секрет (не для истории/мониторов), записывать нельзя."""
    try:
        if any(f and _cb_user32.IsClipboardFormatAvailable(f) for f in _CF_EXCLUDE):
            return True
        if _CF_HISTORY and _cb_user32.IsClipboardFormatAvailable(_CF_HISTORY):
            flag = _history_flag()
            return flag is None or flag == 0  # не смогли прочитать — перестраховываемся
    except Exception as e:
        log(f"clipboard_excluded: {type(e).__name__}")
        return True
    return False


# ── Горячая клавиша Ctrl+Alt+P ──
def hotkey_thread(q):
    MOD_ALT, MOD_CONTROL, MOD_NOREPEAT, VK_P, WM_HOTKEY = 0x1, 0x2, 0x4000, 0x50, 0x0312
    if not user32.RegisterHotKey(None, 1, MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, VK_P):
        q.put("hotkey-failed")
        return
    msg = wintypes.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
        if msg.message == WM_HOTKEY:
            q.put("toggle")


# ── Медиа-клавиши и громкость ──
VK = {"prev": 0xB1, "play": 0xB3, "next": 0xB0, "mute": 0xAD, "vdown": 0xAE, "vup": 0xAF}


def media_key(name):
    vk = VK[name]
    user32.keybd_event(vk, 0, 0, 0)
    user32.keybd_event(vk, 0, 2, 0)


# ── Что сейчас играет: медиа-центр Windows (SMTC) ──
class Media:
    """Что сейчас играет — через медиа-центр Windows (SMTC): Spotify, YouTube в браузере, любой плеер."""

    def __init__(self):
        self.info, self.session = None, None
        self.lock = threading.Lock()
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self._poll())

    async def _thumb(self, props):
        try:
            from winrt.windows.storage.streams import Buffer, InputStreamOptions
            if not props.thumbnail:
                return None
            st = await props.thumbnail.open_read_async()
            buf = Buffer(st.size)
            await st.read_async(buf, st.size, InputStreamOptions.READ_AHEAD)
            return bytes(memoryview(buf))[:buf.length]
        except Exception:
            return None

    async def _poll(self):
        try:
            from winrt.windows.media.control import GlobalSystemMediaTransportControlsSessionManager as M
            mgr = await M.request_async()
        except Exception as e:
            log(f"SMTC недоступен: {type(e).__name__}")
            return
        last_key, thumb = None, None
        while True:
            info = None
            try:
                s = mgr.get_current_session()
                if not s:
                    rest = list(mgr.get_sessions())
                    s = rest[0] if rest else None
                if s:
                    p = await s.try_get_media_properties_async()
                    pb, tl = s.get_playback_info(), s.get_timeline_properties()
                    key = (s.source_app_user_model_id, p.title, p.artist)
                    if key != last_key:
                        thumb, last_key = await self._thumb(p), key
                    info = {
                        "app": s.source_app_user_model_id or "", "title": p.title or "", "artist": p.artist or "",
                        "album": p.album_title or "", "playing": int(pb.playback_status) == 4,
                        "pos": tl.position.total_seconds(), "end": tl.end_time.total_seconds(),
                        "upd": tl.last_updated_time.timestamp() if tl.last_updated_time else time.time(),
                        "thumb": thumb,
                    }
                self.session = s
            except Exception:
                self.session = None
            with self.lock:
                self.info = info
            await asyncio.sleep(1)

    def control(self, action):
        s = self.session
        if not s:
            media_key(action)
            return
        fn = {"prev": s.try_skip_previous_async, "play": s.try_toggle_play_pause_async, "next": s.try_skip_next_async}[action]

        async def run():
            try:
                await fn()
            except Exception:
                media_key(action)
        asyncio.run_coroutine_threadsafe(run(), self.loop)


# ── VS Code ──
CMD_UNSAFE = set('&|<>^%"!`\r\n')  # для .cmd аргументы идут через cmd.exe — такие символы не пропускаем


def find_code():
    """code из PATH, иначе стандартные места установки VS Code (для пользователя, затем для всех)."""
    found = shutil.which("code")
    if found:
        return found
    local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    user_cmd = Path(local) / "Programs" / "Microsoft VS Code" / "bin" / "code.cmd"
    candidates = [user_cmd]
    for var in ("ProgramFiles", "ProgramFiles(x86)"):
        if os.environ.get(var):
            candidates.append(Path(os.environ[var]) / "Microsoft VS Code" / "bin" / "code.cmd")
    for c in candidates:
        if c.exists():
            return str(c)
    return str(user_cmd)  # не нашли — ошибка будет при запуске, с понятным сообщением


CODE = find_code()


def run_code(args):
    """Запустить code(.cmd) без окна консоли. None — успех, иначе текст ошибки для пользователя."""
    shell = CODE.lower().endswith(".cmd")
    if shell and any(ch in CMD_UNSAFE for a in args for ch in a):
        return "Не удалось открыть: недопустимые символы в пути"
    try:
        subprocess.Popen([CODE, *args], creationflags=0x08000000, shell=shell)
        return None
    except OSError as e:
        log(f"VS Code не запустился: {type(e).__name__}")
        return "Не удалось запустить VS Code"


def open_folder_in_code(path):
    return run_code([path])


def open_uri_in_code(url):
    return run_code(["--open-url", url])


# ── Лимиты: токен входа Claude Code ──
def read_oauth():
    """claudeAiOauth из ~/.claude/.credentials.json. Ошибки чтения/разбора пробрасываются вызывающему."""
    path = Path.home() / ".claude" / ".credentials.json"
    return json.loads(path.read_text(encoding="utf-8")).get("claudeAiOauth") or {}


# ── Живое видео: окна-источники и окно панели ──
def panel_hwnd(root):
    """HWND окна панели (GA_ROOT). До первого показа окна он ещё не настоящий — тогда None."""
    if not root.winfo_viewable():
        return None
    return user32.GetAncestor(root.winfo_id(), 2) or None


def window_alive(h):
    return bool(h) and bool(user32.IsWindow(h))


def window_minimized(h):
    return bool(user32.IsIconic(h))


def focus_window(h):
    if not window_alive(h):
        return
    if user32.IsIconic(h):
        user32.ShowWindow(h, 9)  # SW_RESTORE
    user32.SetForegroundWindow(h)


# ── Файлы: открыть / показать в Проводнике ──
def _existing_path(path):
    """Нормализованный абсолютный путь к существующему файлу/папке или (None, текст ошибки)."""
    if not isinstance(path, (str, os.PathLike)):
        return None, "Некорректный путь"
    p = os.fspath(path)
    if not p or "\x00" in p or len(p) > 32767:
        return None, "Некорректный путь"
    p = os.path.normpath(p)  # explorer понимает только обратные слэши
    if not os.path.isabs(p):
        return None, "Нужен абсолютный путь"
    if not os.path.exists(p):
        return None, "Файл не найден"
    return p, None


def open_path(path):
    """Открыть файл/папку программой по умолчанию. None — успех, иначе текст ошибки."""
    p, err = _existing_path(path)
    if err:
        return err
    try:
        os.startfile(p)
        return None
    except OSError as e:
        log(f"open_path: {type(e).__name__} winerror={getattr(e, 'winerror', None)}")
        return "Не удалось открыть: нет программы для этого типа файла" if getattr(e, "winerror", 0) == 1155 \
            else "Не удалось открыть файл"


def reveal_path(path):
    """Показать файл/папку в Проводнике с выделением (explorer /select,<path>). None — успех."""
    p, err = _existing_path(path)
    if err:
        return err
    try:
        # аргументы списком: list2cmdline даёт  explorer /select, "C:\путь с пробелами\файл"
        subprocess.Popen(["explorer", "/select,", p], creationflags=0x08000000)
        return None
    except OSError as e:
        log(f"reveal_path: {type(e).__name__}")
        return "Не удалось открыть Проводник"


# ── Звук уведомления ──
def notify_sound():
    """Короткий системный звук «Уведомление» (асинхронно, не блокирует). Исключений не бросает."""
    try:
        import winsound
        winsound.MessageBeep(winsound.MB_ICONASTERISK)
    except Exception as e:
        log(f"notify_sound: {type(e).__name__}")


# ── Снимок экрана ──
def capture_region_native():
    """На Windows выделение области делает панель своим оверлеем — системного средства нет."""
    return NotImplemented


def grab(bbox):
    """Снимок области (left, top, right, bottom) в физических пикселях, со всех мониторов."""
    from PIL import ImageGrab
    left, top, right, bottom = (int(v) for v in bbox)
    if right <= left or bottom <= top:
        raise ValueError("пустая область снимка")
    return ImageGrab.grab(bbox=(left, top, right, bottom), all_screens=True)


# ── Картинка в буфере обмена ──
_cb_user32.EmptyClipboard.restype = wintypes.BOOL
_cb_user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
_cb_user32.SetClipboardData.restype = wintypes.HANDLE
_cb_user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                       ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                       wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
_cb_user32.CreateWindowExW.restype = wintypes.HWND
_cb_user32.DestroyWindow.argtypes = [wintypes.HWND]
_cb_user32.DestroyWindow.restype = wintypes.BOOL
_cb_kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
_cb_kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
_cb_kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
_cb_kernel32.GlobalFree.restype = wintypes.HGLOBAL
_CF_DIB = 8
_CF_PNG = _cb_user32.RegisterClipboardFormatW("PNG")  # браузеры/Telegram/Office берут PNG с прозрачностью
_GMEM_MOVEABLE = 0x0002
_IMG_LIMIT = 256 * 1024 * 1024  # больше 256 МБ в буфер не кладём


def _global_from_bytes(data):
    h = _cb_kernel32.GlobalAlloc(_GMEM_MOVEABLE, len(data))
    if not h:
        return None
    p = _cb_kernel32.GlobalLock(h)
    if not p:
        _cb_kernel32.GlobalFree(h)
        return None
    try:
        ctypes.memmove(p, data, len(data))
    finally:
        _cb_kernel32.GlobalUnlock(h)
    return h


def set_clipboard_image(img):
    """Положить картинку PIL в буфер обмена (CF_DIB + PNG). True — получилось."""
    import io
    try:
        rgb = img.convert("RGB")
        bmp = io.BytesIO()
        rgb.save(bmp, "BMP")
        dib = bmp.getvalue()[14:]  # CF_DIB — BMP без 14-байтного BITMAPFILEHEADER
        png = io.BytesIO()
        (img if img.mode in ("RGB", "RGBA", "L", "LA", "P") else rgb).save(png, "PNG")
        png = png.getvalue()
    except Exception as e:
        log(f"set_clipboard_image: подготовка {type(e).__name__}")
        return False
    if len(dib) + len(png) > _IMG_LIMIT:
        log("set_clipboard_image: картинка слишком большая")
        return False
    # Окно-владелец (message-only): с OpenClipboard(NULL) SetClipboardData по документации может не сработать
    hwnd = _cb_user32.CreateWindowExW(0, "STATIC", None, 0, 0, 0, 0, 0, wintypes.HWND(-3), None, None, None)
    try:
        for _ in range(10):  # буфер может быть ненадолго занят другой программой
            if _cb_user32.OpenClipboard(hwnd):
                break
            time.sleep(0.03)
        else:
            log("set_clipboard_image: буфер занят")
            return False
        try:
            if not _cb_user32.EmptyClipboard():
                return False
            ok = False
            for fmt, data in ((_CF_DIB, dib), (_CF_PNG, png)):
                if not fmt:
                    continue
                h = _global_from_bytes(data)
                if not h:
                    continue
                if _cb_user32.SetClipboardData(fmt, h):
                    ok = ok or fmt == _CF_DIB  # память теперь принадлежит системе
                else:
                    _cb_kernel32.GlobalFree(h)
            if not ok:
                log(f"set_clipboard_image: SetClipboardData, ошибка {ctypes.get_last_error()}")
            return ok
        finally:
            _cb_user32.CloseClipboard()
    except Exception as e:
        log(f"set_clipboard_image: {type(e).__name__}")
        return False
    finally:
        if hwnd:
            _cb_user32.DestroyWindow(hwnd)


def clipboard_image():
    """Картинка из буфера обмена (PIL.Image) или None (там текст, файлы, пусто, ошибка)."""
    try:
        from PIL import Image, ImageGrab
        im = ImageGrab.grabclipboard()
        return im if isinstance(im, Image.Image) else None
    except Exception as e:
        log(f"clipboard_image: {type(e).__name__}")
        return None


# ── Мониторы ──
DATA_DIR = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "MyPanel"
try:
    from displays_win import list_displays, set_display_positions, EdgeHelper, ease_cursor_setting
except Exception as _e:
    log(f"displays_win: модуль недоступен ({type(_e).__name__})")
    list_displays = set_display_positions = EdgeHelper = ease_cursor_setting = None


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", wintypes.LONG), ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260)]


_th_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_th_kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
_th_kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
_th_kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
_th_kernel32.Process32FirstW.restype = wintypes.BOOL
_th_kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
_th_kernel32.Process32NextW.restype = wintypes.BOOL
_th_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]


def process_running(names):
    """Какие из имён исполняемых файлов (без учёта регистра, «deskflow-core.exe») сейчас запущены. Toolhelp32."""
    want = {}
    for n in names or ():
        if isinstance(n, str) and n:
            want.setdefault(n.lower(), n)
    if not want:
        return []
    snap = _th_kernel32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    if not snap or snap == wintypes.HANDLE(-1).value:
        log(f"process_running: снимок процессов не получен ({ctypes.get_last_error()})")
        return []
    found = set()
    try:
        pe = _PROCESSENTRY32W()
        pe.dwSize = ctypes.sizeof(pe)
        ok = _th_kernel32.Process32FirstW(snap, ctypes.byref(pe))
        while ok:
            exe = pe.szExeFile.lower()
            if exe in want:
                found.add(exe)
            ok = _th_kernel32.Process32NextW(snap, ctypes.byref(pe))
    finally:
        _th_kernel32.CloseHandle(snap)
    return [orig for low, orig in want.items() if low in found]
