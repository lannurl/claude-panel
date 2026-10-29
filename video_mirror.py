"""Живое зеркало видео для панели — DWM live thumbnail (как превью на панели задач).

Окно-источник (браузер/плеер) не дублируется вторым плеером: DWM сам рисует его содержимое
в нашем окне, 60 fps, почти без CPU. Фоновый поток раз в ~4 с снимает источник через PrintWindow,
сравнивает два кадра и находит область, где картинка меняется, — это и есть видео (rcSource).

Ограничения: свёрнутое окно DWM не рендерит (последний кадр/пусто); видео должно быть
на активной вкладке. Координаты физические (процесс DPI-aware).

Самотест: python video_mirror.py --selftest out.png
"""
import ctypes
import os
import sys
import threading
import time
from ctypes import wintypes

from PIL import Image, ImageChops

# Свои экземпляры DLL: argtypes/restype не пересекаются с panel.pyw (там общий ctypes.windll.user32)
_user32 = ctypes.WinDLL("user32", use_last_error=True)
_gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_dwm = ctypes.WinDLL("dwmapi")

# ── Настройки детектора ──
DETECT_PERIOD = 4.0      # с, между замерами
WATCH_PERIOD = 0.5       # с, проверка «источник свёрнут/закрыт»
FRAME_GAP = 0.25         # с, между двумя кадрами одного замера
GRID_W, GRID_H = 48, 27  # сетка клеток
DIFF_THRESHOLD = 24      # порог яркости разницы пикселя
CELL_FILL = 0.04         # доля изменившихся пикселей, чтобы клетка считалась «живой»
MIN_AREA = 0.05          # отбросить области меньше 5% окна
FALLBACK_TOP = 0.11      # фоллбэк: срезать верх (вкладки/адрес браузера)
ANALYZE_W = 640          # кадры для сравнения уменьшаются до этой ширины

# Известные плееры: ключ (подстрока app_id) → имя exe без расширения
KNOWN_APPS = {
    "chrome": "chrome", "msedge": "msedge", "edge": "msedge", "firefox": "firefox",
    "308046b0af4a39cb": "firefox", "opera": "opera", "brave": "brave", "yandex": "browser",
    "vivaldi": "vivaldi", "spotify": "spotify", "vlc": "vlc", "mpc-hc": "mpc-hc64",
    "potplayer": "potplayermini64", "telegram": "telegram", "python": "python",
}

# ── WinAPI ──
HTHUMBNAIL = ctypes.c_void_p
DWM_TNP_RECTDESTINATION, DWM_TNP_RECTSOURCE, DWM_TNP_OPACITY = 0x1, 0x2, 0x4
DWM_TNP_VISIBLE, DWM_TNP_SOURCECLIENTAREAONLY = 0x8, 0x10
PW_CLIENTONLY, PW_RENDERFULLCONTENT = 0x1, 0x2
DWMWA_CLOAKED = 14


class DWM_THUMBNAIL_PROPERTIES(ctypes.Structure):
    _fields_ = [("dwFlags", wintypes.DWORD), ("rcDestination", wintypes.RECT), ("rcSource", wintypes.RECT),
                ("opacity", ctypes.c_ubyte), ("fVisible", wintypes.BOOL), ("fSourceClientAreaOnly", wintypes.BOOL)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]


_dwm.DwmRegisterThumbnail.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.POINTER(HTHUMBNAIL)]
_dwm.DwmRegisterThumbnail.restype = ctypes.c_long
_dwm.DwmUnregisterThumbnail.argtypes = [HTHUMBNAIL]
_dwm.DwmUnregisterThumbnail.restype = ctypes.c_long
_dwm.DwmUpdateThumbnailProperties.argtypes = [HTHUMBNAIL, ctypes.POINTER(DWM_THUMBNAIL_PROPERTIES)]
_dwm.DwmUpdateThumbnailProperties.restype = ctypes.c_long
_dwm.DwmGetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
_dwm.DwmGetWindowAttribute.restype = ctypes.c_long

WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
_user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
_user32.EnumWindows.restype = wintypes.BOOL
for _name in ("IsWindowVisible", "IsIconic", "IsWindow"):
    getattr(_user32, _name).argtypes = [wintypes.HWND]
    getattr(_user32, _name).restype = wintypes.BOOL
_user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
_user32.GetWindowTextW.restype = ctypes.c_int
_user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
_user32.GetWindowThreadProcessId.restype = wintypes.DWORD
_user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
_user32.GetClientRect.restype = wintypes.BOOL
_user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
_user32.GetWindowRect.restype = wintypes.BOOL
_user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
_user32.ClientToScreen.restype = wintypes.BOOL
_user32.GetDC.argtypes = [wintypes.HWND]
_user32.GetDC.restype = wintypes.HDC
_user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
_user32.ReleaseDC.restype = ctypes.c_int
_user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
_user32.PrintWindow.restype = wintypes.BOOL
_gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
_gdi32.CreateCompatibleDC.restype = wintypes.HDC
_gdi32.CreateDIBSection.argtypes = [wintypes.HDC, ctypes.c_void_p, wintypes.UINT,
                                    ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD]
_gdi32.CreateDIBSection.restype = wintypes.HBITMAP
_gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
_gdi32.SelectObject.restype = wintypes.HGDIOBJ
_gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
_gdi32.DeleteObject.restype = wintypes.BOOL
_gdi32.DeleteDC.argtypes = [wintypes.HDC]
_gdi32.DeleteDC.restype = wintypes.BOOL
_gdi32.GdiFlush.restype = wintypes.BOOL
_kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_kernel32.OpenProcess.restype = wintypes.HANDLE
_kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                 ctypes.POINTER(wintypes.DWORD)]
_kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel32.CloseHandle.restype = wintypes.BOOL


def _log(msg):
    # Без заголовков/названий треков — только технические события
    try:
        if sys.stderr:  # под pythonw stderr = None
            print(f"[video_mirror] {msg}", file=sys.stderr, flush=True)
    except Exception:
        pass


def _client_size(hwnd):
    r = wintypes.RECT()
    if not _user32.GetClientRect(hwnd, ctypes.byref(r)):
        return None
    w, h = r.right - r.left, r.bottom - r.top
    return (w, h) if w > 0 and h > 0 else None


def _exe_stem(hwnd, cache):
    pid = wintypes.DWORD()
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if pid.value in cache:
        return cache[pid.value]
    stem = ""
    h = _kernel32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
    if h:
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(1024)
            if _kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                stem = os.path.splitext(os.path.basename(buf.value))[0].lower()
        finally:
            _kernel32.CloseHandle(h)
    cache[pid.value] = stem
    return stem


def _is_cloaked(hwnd):
    # Окна на другом рабочем столе / спрятанные UWP: видимы формально, но не рисуются
    val = wintypes.DWORD()
    try:
        if _dwm.DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, ctypes.byref(val), ctypes.sizeof(val)) == 0:
            return val.value != 0
    except Exception:
        pass
    return False


def _app_key(app_id):
    """app_id из SMTC → ожидаемое имя exe (или None, если это непонятный AUMID — тогда по заголовку)."""
    s = (app_id or "").strip().lower()
    if not s:
        return None
    for key, exe in KNOWN_APPS.items():
        if key in s:
            return exe
    s = s.rsplit("!", 1)[-1].rsplit("\\", 1)[-1]
    if s.endswith(".exe"):
        s = s[:-4]
    # Похоже на hex-хэш AUMID — exe не угадать
    if len(s) >= 12 and all(c in "0123456789abcdef" for c in s):
        return None
    return s or None


def _capture(hwnd):
    """Снимок клиентской области через PrintWindow → PIL RGB (или None)."""
    size = _client_size(hwnd)
    if not size:
        return None
    w, h = size
    screen_dc = _user32.GetDC(None)
    if not screen_dc:
        return None
    mem_dc = bmp = old = None
    try:
        mem_dc = _gdi32.CreateCompatibleDC(screen_dc)
        if not mem_dc:
            return None
        bi = BITMAPINFOHEADER()
        bi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bi.biWidth, bi.biHeight = w, -h  # сверху вниз
        bi.biPlanes, bi.biBitCount, bi.biCompression = 1, 32, 0
        bits = ctypes.c_void_p()
        bmp = _gdi32.CreateDIBSection(mem_dc, ctypes.byref(bi), 0, ctypes.byref(bits), None, 0)
        if not bmp or not bits.value:
            return None
        old = _gdi32.SelectObject(mem_dc, bmp)
        if not _user32.PrintWindow(hwnd, mem_dc, PW_CLIENTONLY | PW_RENDERFULLCONTENT):
            return None
        _gdi32.GdiFlush()
        raw = ctypes.string_at(bits.value, w * h * 4)
        return Image.frombuffer("RGB", (w, h), raw, "raw", "BGRX", 0, 1).copy()
    finally:
        if old and mem_dc:
            _gdi32.SelectObject(mem_dc, old)
        if bmp:
            _gdi32.DeleteObject(bmp)
        if mem_dc:
            _gdi32.DeleteDC(mem_dc)
        _user32.ReleaseDC(None, screen_dc)


def _largest_blob(cells, gw, gh):
    """Наибольшая 8-связная область «живых» клеток → bbox в клетках (x0, y0, x1, y1) или None."""
    seen = bytearray(gw * gh)
    best, best_n = None, 0
    for start in range(gw * gh):
        if not cells[start] or seen[start]:
            continue
        seen[start] = 1
        stack, n = [start], 0
        x0 = x1 = start % gw
        y0 = y1 = start // gw
        while stack:
            i = stack.pop()
            n += 1
            cx, cy = i % gw, i // gw
            x0, x1, y0, y1 = min(x0, cx), max(x1, cx), min(y0, cy), max(y1, cy)
            for dy in (-1, 0, 1):
                ny = cy + dy
                if not 0 <= ny < gh:
                    continue
                for dx in (-1, 0, 1):
                    nx = cx + dx
                    if 0 <= nx < gw:
                        j = ny * gw + nx
                        if cells[j] and not seen[j]:
                            seen[j] = 1
                            stack.append(j)
        if n > best_n:
            best, best_n = (x0, y0, x1 + 1, y1 + 1), n
    return best


def detect_video_rect(img_a, img_b):
    """Два кадра клиентской области → (l, t, r, b) области видео в пикселях клиента или None."""
    w, h = img_a.size
    if img_b.size != (w, h) or w < 16 or h < 16:
        return None
    k = min(1.0, ANALYZE_W / w)
    sw, sh = max(GRID_W, int(w * k)), max(GRID_H, int(h * k))
    a = img_a.resize((sw, sh), Image.BILINEAR)
    b = img_b.resize((sw, sh), Image.BILINEAR)
    mask = ImageChops.difference(a, b).convert("L").point(lambda v: 255 if v > DIFF_THRESHOLD else 0)
    if not mask.getbbox():
        return None
    grid = mask.resize((GRID_W, GRID_H), Image.BOX)
    cells = [v > 255 * CELL_FILL for v in grid.tobytes()]
    blob = _largest_blob(cells, GRID_W, GRID_H)
    if not blob:
        return None
    # Клетки → пиксели уменьшенного кадра, с запасом в одну клетку, затем точный bbox маски внутри
    cw, ch = sw / GRID_W, sh / GRID_H
    x0, y0 = int(max(0, blob[0] - 1) * cw), int(max(0, blob[1] - 1) * ch)
    x1, y1 = int(min(GRID_W, blob[2] + 1) * cw), int(min(GRID_H, blob[3] + 1) * ch)
    inner = mask.crop((x0, y0, x1, y1)).getbbox()
    if inner:
        x0, y0, x1, y1 = x0 + inner[0], y0 + inner[1], x0 + inner[2], y0 + inner[3]
    # Назад в полный размер; привязать к краям, если до них меньше клетки
    l, t, r, btm = int(x0 * w / sw), int(y0 * h / sh), int(x1 * w / sw + 0.999), int(y1 * h / sh + 0.999)
    snap_x, snap_y = w / GRID_W, h / GRID_H
    if l < snap_x:
        l = 0
    if t < snap_y:
        t = 0
    if w - r < snap_x:
        r = w
    if h - btm < snap_y:
        btm = h
    if (r - l) * (btm - t) < MIN_AREA * w * h:
        return None
    return l, t, r, btm


def _fallback_rect(size):
    w, h = size
    return 0, int(h * FALLBACK_TOP), w, h


def _fit(src_w, src_h, dest):
    """Вписать src_w×src_h в dest с сохранением пропорций, по центру."""
    l, t, r, b = dest
    dw, dh = r - l, b - t
    if src_w <= 0 or src_h <= 0 or dw <= 0 or dh <= 0:
        return dest
    k = min(dw / src_w, dh / src_h)
    fw, fh = int(round(src_w * k)), int(round(src_h * k))
    x, y = l + (dw - fw) // 2, t + (dh - fh) // 2
    return x, y, x + fw, y + fh


def _near(a, b, tol):
    return all(abs(p - q) <= tol for p, q in zip(a, b))


class VideoMirror:
    def __init__(self, dest_hwnd: int):
        self._dest = int(dest_hwnd)
        self._lock = threading.Lock()
        self._thumb = None        # HTHUMBNAIL
        self._src = None          # HWND источника
        self._dest_rect = None    # куда вписывать
        self._crop = None         # найденная область видео (клиентские пиксели источника)
        self._crop_src = None     # для какого src найдена _crop
        self._stop = threading.Event()
        self._kick = threading.Event()
        self._thread = threading.Thread(target=self._detector, name="video-mirror", daemon=True)
        self._thread.start()

    # ── Поиск окна-источника ──
    @staticmethod
    def find_source(app_id: str, title: str) -> int | None:
        try:
            exe = _app_key(app_id)
            title = (title or "").strip().lower()
            needles = [t for t in (title, title[:25].strip()) if t]
            found = []  # (по_exe, площадь, hwnd)
            cache = {}
            buf = ctypes.create_unicode_buffer(512)

            def cb(hwnd, _):
                try:
                    if not _user32.IsWindowVisible(hwnd) or _user32.IsIconic(hwnd) or _is_cloaked(hwnd):
                        return True
                    _user32.GetWindowTextW(hwnd, buf, 512)
                    wt = buf.value.lower()
                    if not wt:
                        return True
                    stem = _exe_stem(hwnd, cache)
                    by_exe = bool(exe and stem and (exe in stem or stem in exe))
                    by_title = any(n in wt for n in needles)
                    if needles and not by_title:
                        return True
                    if not needles and not by_exe:
                        return True
                    size = _client_size(hwnd)
                    if size and size[0] >= 64 and size[1] >= 64:
                        found.append((by_exe, size[0] * size[1], int(hwnd)))
                except Exception:
                    pass
                return True

            _user32.EnumWindows(WNDENUMPROC(cb), 0)
            if not found:
                return None
            if exe:
                exact = [f for f in found if f[0]]
                if exact:
                    found = exact
            found.sort(reverse=True)
            return found[0][2]
        except Exception as e:
            _log(f"find_source: {e!r}")
            return None

    # ── Показ ──
    def show(self, src_hwnd: int, dest_rect: tuple[int, int, int, int]) -> bool:
        # dest_rect — от левого-верхнего угла клиентской области dest (у окна без рамки = угол окна)
        try:
            src_hwnd = int(src_hwnd)
            if not src_hwnd or not _user32.IsWindow(src_hwnd) or _user32.IsIconic(src_hwnd):
                self.hide()  # свёрнутое/исчезнувшее окно DWM не рисует — не оставлять старый кадр
                return False
            with self._lock:
                if self._thumb is not None and self._src != src_hwnd:
                    self._unregister_locked()
                if self._thumb is None:
                    th = HTHUMBNAIL()
                    hr = _dwm.DwmRegisterThumbnail(self._dest, src_hwnd, ctypes.byref(th))
                    if hr != 0 or not th.value:
                        _log(f"DwmRegisterThumbnail hr=0x{hr & 0xFFFFFFFF:08X}")
                        return False
                    self._thumb = th.value
                    new_src = self._src != src_hwnd
                    self._src = src_hwnd
                    if new_src:
                        self._kick.set()
                self._dest_rect = tuple(int(v) for v in dest_rect)
                if self._apply_locked():
                    return True
                self._unregister_locked()  # свойства не применились — убрать, чтобы не висела картинка
                return False
        except Exception as e:
            _log(f"show: {e!r}")
            self.hide()
            return False

    def hide(self) -> None:
        try:
            with self._lock:
                self._unregister_locked()
        except Exception as e:
            _log(f"hide: {e!r}")

    def close(self) -> None:
        self._stop.set()
        self._kick.set()
        self.hide()
        try:
            self._thread.join(timeout=2)
        except Exception:
            pass

    @property
    def video_size(self) -> tuple[int, int] | None:
        with self._lock:
            rect = self._source_rect_locked()
        return (rect[2] - rect[0], rect[3] - rect[1]) if rect else None

    # ── Внутреннее (вызывать под self._lock) ──
    def _source_rect_locked(self):
        if not self._src:
            return None
        size = _client_size(self._src)
        if not size:
            return None
        if self._crop and self._crop_src == self._src:
            l, t, r, b = self._crop
            l, t, r, b = max(0, l), max(0, t), min(size[0], r), min(size[1], b)
            if r - l > 0 and b - t > 0:
                return l, t, r, b
        return _fallback_rect(size)

    def _apply_locked(self):
        if self._thumb is None or not self._dest_rect:
            return False
        src = self._source_rect_locked()
        if not src:
            return False
        dst = _fit(src[2] - src[0], src[3] - src[1], self._dest_rect)
        p = DWM_THUMBNAIL_PROPERTIES()
        p.dwFlags = (DWM_TNP_RECTDESTINATION | DWM_TNP_RECTSOURCE | DWM_TNP_VISIBLE
                     | DWM_TNP_OPACITY | DWM_TNP_SOURCECLIENTAREAONLY)
        p.rcDestination = wintypes.RECT(*dst)
        p.rcSource = wintypes.RECT(*src)
        p.opacity, p.fVisible, p.fSourceClientAreaOnly = 255, True, True
        hr = _dwm.DwmUpdateThumbnailProperties(HTHUMBNAIL(self._thumb), ctypes.byref(p))
        if hr != 0:
            _log(f"DwmUpdateThumbnailProperties hr=0x{hr & 0xFFFFFFFF:08X}")
            return False
        return True

    def _unregister_locked(self):
        if self._thumb is not None:
            hr = _dwm.DwmUnregisterThumbnail(HTHUMBNAIL(self._thumb))
            if hr != 0:
                _log(f"DwmUnregisterThumbnail hr=0x{hr & 0xFFFFFFFF:08X}")
            self._thumb = None

    # ── Детектор области видео (daemon-поток) ──
    def _source_gone(self, src):
        """Источник свернули/закрыли — сам снять thumbnail. True, если снят."""
        try:
            gone = not _user32.IsWindow(src) or _user32.IsIconic(src)
        except Exception:
            gone = True
        if not gone:
            return False
        with self._lock:
            if self._src == src and self._thumb is not None:
                self._unregister_locked()
                _log("source minimized/closed - thumbnail removed")
        return True

    def _detector(self):
        next_detect = 0.0
        while not self._stop.is_set():
            kicked = self._kick.wait(WATCH_PERIOD)
            self._kick.clear()
            if self._stop.is_set():
                break
            with self._lock:
                src, active = self._src, self._thumb is not None
            if not src or not active:
                continue
            if self._source_gone(src):
                continue
            now = time.monotonic()
            if kicked:
                time.sleep(0.3)  # дать источнику дорисоваться
            elif now < next_detect:
                continue
            next_detect = now + DETECT_PERIOD
            try:
                a = _capture(src)
                if self._stop.wait(FRAME_GAP):
                    break
                b = _capture(src)
                if a is None or b is None:
                    continue
                rect = detect_video_rect(a, b)
            except Exception as e:
                _log(f"detector: {e!r}")
                continue
            if not rect:
                continue  # пауза/нет изменений — оставить прежнюю область
            with self._lock:
                if self._src != src:
                    continue
                tol = max(4, int(min(a.size) * 0.01))
                if self._crop_src == src and self._crop and _near(self._crop, rect, tol):
                    continue  # не дёргать картинку из-за мелкого шума
                self._crop, self._crop_src = rect, src
                try:
                    if not self._apply_locked():
                        self._unregister_locked()
                except Exception as e:
                    _log(f"apply: {e!r}")


# ── Самотест ──
def _selftest(out_path):
    import tkinter as tk
    from PIL import ImageGrab

    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass
    root = tk.Tk()
    root.withdraw()
    title = f"VM-SELFTEST-SOURCE-{os.getpid()}"

    src = tk.Toplevel(root, bg="#202020")
    src.title(title)
    src.geometry("800x500+40+60")
    sc = tk.Canvas(src, width=800, height=500, bg="#202020", highlightthickness=0)
    sc.pack(fill="both", expand=True)
    # Статичный «интерфейс» вокруг
    sc.create_rectangle(0, 0, 800, 50, fill="#3a3a3a", outline="")
    sc.create_text(20, 25, text="static header / tabs", fill="white", anchor="w", font=("Segoe UI", 14))
    sc.create_rectangle(20, 420, 300, 480, fill="#335", outline="")
    sc.create_text(560, 450, text="static footer", fill="#aaa", font=("Segoe UI", 12))
    x0, y0 = (800 - 320) // 2, (500 - 180) // 2
    box = sc.create_rectangle(x0, y0, x0 + 320, y0 + 180, fill="red", outline="")
    ball = sc.create_oval(x0, y0, x0 + 40, y0 + 40, fill="white", outline="")
    colors = ["#ff3030", "#30c030", "#3060ff", "#ffcc00", "#ff30ff", "#00d0d0"]
    st = {"i": 0}

    def animate():
        st["i"] += 1
        i = st["i"]
        sc.itemconfigure(box, fill=colors[(i // 3) % len(colors)])
        bx = x0 + (i * 7) % 280
        by = y0 + (i * 5) % 140
        sc.coords(ball, bx, by, bx + 40, by + 40)
        root.after(40, animate)

    dst = tk.Toplevel(root, bg="#0e0f1a")
    dst.title("VM-SELFTEST-DEST")
    dst.geometry("640x420+880+60")
    dst.attributes("-topmost", True)
    dc = tk.Frame(dst, bg="#0e0f1a")
    dc.pack(fill="both", expand=True)

    res = {}

    def dest_hwnd():
        return int(dst.wm_frame(), 16)

    def dest_rect():
        # DWM считает rcDestination от клиентской области окна (у панели без рамки это и есть угол окна)
        pt = wintypes.POINT(0, 0)
        _user32.ClientToScreen(dest_hwnd(), ctypes.byref(pt))
        ox, oy = dc.winfo_rootx() - pt.x, dc.winfo_rooty() - pt.y
        return ox + 10, oy + 10, ox + dc.winfo_width() - 10, oy + dc.winfo_height() - 10

    def start():
        hwnd = VideoMirror.find_source("python", title)
        res["src"] = hwnd
        print(f"find_source -> {hwnd} (frame tk = {int(src.wm_frame(), 16)})")
        if not hwnd:
            root.destroy()
            return
        res["mirror"] = VideoMirror(dest_hwnd())
        print("show ->", res["mirror"].show(hwnd, dest_rect()), "fallback size:", res["mirror"].video_size)
        root.after(5000, finish)

    def grab(path):
        dst.update()
        x, y = dc.winfo_rootx(), dc.winfo_rooty()
        img = ImageGrab.grab(bbox=(x, y, x + dc.winfo_width(), y + dc.winfo_height()), all_screens=True)
        img.save(path)
        print("saved:", path)
        return img

    def finish():
        m = res["mirror"]
        with m._lock:
            crop = m._crop
        print("video area (client px):", crop, "video_size:", m.video_size)
        grab(out_path)
        # Сценарий: свернуть источник — детектор сам снимает thumbnail
        src.iconify()
        root.after(1500, after_minimize)

    def after_minimize():
        m = res["mirror"]
        with m._lock:
            res["min_thumb_gone"] = m._thumb is None
        res["min_show"] = m.show(res["src"], dest_rect())  # show на свёрнутом → False
        with m._lock:
            res["min_thumb_gone"] = res["min_thumb_gone"] and m._thumb is None
        base, ext = os.path.splitext(out_path)
        img = grab(f"{base}_minimized{ext or '.png'}")
        # Приёмник должен быть пустым (однотонный фон)
        g = img.convert("L")
        bg = max(g.getcolors(256 * 256), key=lambda c: c[0])[1]
        odd = sum(1 for v in g.tobytes() if abs(v - bg) > 12)
        res["min_blank"] = odd < 0.01 * g.width * g.height  # углы окна Win11 скруглены — допуск 1%
        print("minimized: thumb_removed =", res["min_thumb_gone"], "| show() ->", res["min_show"],
              "| dest_blank =", res["min_blank"])
        m.close()
        root.destroy()

    animate()
    root.after(800, start)
    root.mainloop()
    crop_ok = bool(res.get("mirror")) and res["mirror"]._crop is not None
    min_ok = res.get("min_thumb_gone") and res.get("min_show") is False and res.get("min_blank")
    return 0 if crop_ok and min_ok else 1

if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--selftest":
        sys.exit(_selftest(sys.argv[2]))
    print(__doc__)
