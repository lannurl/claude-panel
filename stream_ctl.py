"""Стрим «ноут как монитор»: Apollo/Sunshine на хосте, Moonlight на клиенте (docs/monitors-spec.md §5.4, §10.3).

Apollo НЕ управляется: его HTTP API не вызываем, конфиг не пишем, службу не трогаем. Обнаружение = файл на диске
+ TCP-подключение к 127.0.0.1:47990 (веб-интерфейс). Moonlight запускается как свой процесс и переживает панель
(лог в файл, перенимается по pid + образ + время создания, как Deskflow).

Шаблоны CLI Moonlight (moonlight-qt) — по документации, ВЖИВУЮ НЕ ПРОВЕРЕНЫ (НП): вынесены в константу
MOONLIGHT_ARGV и подставляются без shell; значения проходят регулярки (host, имя приложения, WxH, fps, PIN).

Публичные функции не бросают исключений: {"ok","code","err","hint",...}.
"""
import ipaddress
import os
import re
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import deskflow_ctl as D

IS_WIN = D.IS_WIN
OS_KEY = D.OS_KEY
CREATE_NO_WINDOW = D.CREATE_NO_WINDOW
CREATE_NEW_PROCESS_GROUP = D.CREATE_NEW_PROCESS_GROUP

WEB_UI_PORT = 47990
WEB_UI_URL = "https://localhost:47990"
CONNECT_TIMEOUT_S = 0.4
QUIT_TIMEOUT_S = 8
START_CHECK_S = 1.5          # быстрый выход Moonlight = ошибка (хост недоступен, не сопряжён, неверное приложение)
DEFAULT_FPS = 60
LOG_ROTATE_BYTES = 1024 * 1024

MOONLIGHT_NAMES = ("moonlight.exe", "moonlight", "moonlight-qt")
APOLLO_NAMES = ("sunshine.exe", "apollo.exe")
SUNSHINE_NAMES = ("sunshine.exe", "sunshine")
DEFAULT_MATCH = ("SudoMaker", "SudoVDA", "Virtual Display")

# НП: синтаксис moonlight-qt по документации. Пары [флаг, значение] с пустым значением отбрасываются.
MOONLIGHT_ARGV = {
    "stream": ["{exe}", "stream", "{host}", "{app}", "--resolution", "{resolution}", "--fps", "{fps}",
               "--display-mode", "fullscreen"],
    "quit": ["{exe}", "quit", "{host}"],
    "list": ["{exe}", "list", "{host}"],
    "pair": ["{exe}", "pair", "{host}", "--pin", "{pin}"],
}

APP_RE = re.compile(r"[\w .:-]{1,64}")
RES_RE = re.compile(r"([1-9][0-9]{2,4})x([1-9][0-9]{2,4})")
PIN_RE = re.compile(r"[0-9]{4}")

INSTALL = {
    "apollo": {"windows": {"url": "https://github.com/ClassicOldSong/Apollo/releases",
                           "cmd": "winget install --id ClassicOldSong.Apollo -e", "admin": True,
                           "note": "Установщик Apollo (с драйвером виртуального дисплея) требует прав администратора"}},
    "sunshine": {"windows": {"url": "https://github.com/LizardByte/Sunshine/releases",
                             "cmd": "winget install --id LizardByte.Sunshine -e", "admin": True, "note": ""},
                 "macos": {"url": "https://github.com/LizardByte/Sunshine/releases",
                           "cmd": "brew install lizardbyte/homebrew/sunshine", "admin": False,
                           "note": "Не проверено; виртуальный экран на Mac — через BetterDisplay"},
                 "linux": {"url": "https://github.com/LizardByte/Sunshine/releases",
                           "cmd": "flatpak install flathub dev.lizardbyte.app.Sunshine", "admin": False, "note": ""}},
    "moonlight": {"windows": {"url": "https://github.com/moonlight-stream/moonlight-qt/releases",
                              "cmd": "winget install --id MoonlightGameStreamingProject.Moonlight -e",
                              "admin": True, "note": "Установщик Windows требует прав администратора"},
                  "macos": {"url": "https://github.com/moonlight-stream/moonlight-qt/releases",
                            "cmd": "brew install --cask moonlight", "admin": False, "note": ""},
                  "linux": {"url": "https://github.com/moonlight-stream/moonlight-qt/releases",
                            "cmd": "flatpak install flathub com.moonlight_stream.Moonlight", "admin": False,
                            "note": ""}},
}


def install_info(what):
    d = INSTALL.get(what) or {}
    return d.get(OS_KEY) or next(iter(d.values()), {"url": "", "cmd": "", "admin": False, "note": ""})


def log(msg):
    try:
        if sys.stderr:
            print(f"[panel/stream] {msg}", file=sys.stderr, flush=True)
    except Exception:
        pass


def _err(code, text, hint=None, **extra):
    return D._err(code, text, hint, **extra)


def _ok(**extra):
    return D._ok(**extra)


# ── версия файла (без запуска программы) ──
def file_version(path):
    """Версия из ресурса файла (Windows, version.dll) или None. Программа НЕ запускается."""
    if not IS_WIN:
        return None
    try:
        import ctypes
        from ctypes import wintypes
        ver = ctypes.WinDLL("version", use_last_error=True)
        ver.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
        ver.GetFileVersionInfoSizeW.restype = wintypes.DWORD
        ver.GetFileVersionInfoW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
        ver.GetFileVersionInfoW.restype = wintypes.BOOL
        ver.VerQueryValueW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p),
                                       ctypes.POINTER(wintypes.UINT)]
        ver.VerQueryValueW.restype = wintypes.BOOL
        size = ver.GetFileVersionInfoSizeW(str(path), None)
        if not size:
            return None
        buf = ctypes.create_string_buffer(size)
        if not ver.GetFileVersionInfoW(str(path), 0, size, buf):
            return None
        p, n = ctypes.c_void_p(), wintypes.UINT()
        if not ver.VerQueryValueW(buf, "\\", ctypes.byref(p), ctypes.byref(n)) or n.value < 52:
            return None
        v = (wintypes.DWORD * 13).from_address(p.value)   # VS_FIXEDFILEINFO: [4],[5] = FileVersionMS/LS
        ms, ls = v[4], v[5]
        return f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}.{ls & 0xFFFF}"
    except Exception as e:
        log(f"file_version: {type(e).__name__}")
        return None


# ── поиск ──
def _win_dirs(sub):
    out = []
    for var in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"):
        base = os.environ.get(var)
        if base:
            p = os.path.join(base, sub)
            if p not in out:
                out.append(p)
    return out


def _tcp_open(host, port, timeout=CONNECT_TIMEOUT_S):
    """Только TCP-подключение (без HTTP-запросов)."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (OSError, ValueError):
        return False


def find_host_app(kind="apollo", *, connect=None, dirs=None):
    """{"ok","code","err","hint","state","path","version","running","web_ui"}; state: ok/missing/unsupported.

    running — TCP-подключение к 127.0.0.1:47990. HTTP API не вызывается. dirs — подмена папок (тесты).
    """
    if kind not in ("apollo", "sunshine"):
        return _err("bad_config", "Неизвестное приложение-хост", state="missing", path=None, version=None,
                    running=False, web_ui=WEB_UI_URL)
    inst = install_info(kind)
    base = {"state": "missing", "path": None, "version": None, "running": False, "web_ui": WEB_UI_URL}
    if kind == "apollo" and not IS_WIN:
        return _err("unsupported_os", "Apollo есть только для Windows", "На Mac/Linux используйте Sunshine",
                    **{**base, "state": "unsupported"})
    if kind == "sunshine" and OS_KEY == "linux":
        return _err("unsupported_os", "На Linux нет штатного способа создать виртуальный дисплей для Sunshine",
                    "Используйте режим «Общая мышь»", **{**base, "state": "unsupported"})
    if dirs is None:
        if IS_WIN:
            dirs = _win_dirs("Apollo" if kind == "apollo" else "Sunshine")
        else:
            dirs = ["/Applications/Sunshine.app/Contents/MacOS", "/opt/homebrew/bin", "/usr/local/bin"]
    names = APOLLO_NAMES if kind == "apollo" else SUNSHINE_NAMES
    path = None
    for d in dirs:
        for n in names:
            f = os.path.join(d, n)
            if os.path.isfile(f):
                path = f
                break
        if path:
            break
    if path is None:
        return _err("not_found", "Apollo не найден" if kind == "apollo" else "Sunshine не найден", inst["cmd"],
                    **base)
    running = bool((connect or _tcp_open)("127.0.0.1", WEB_UI_PORT))
    return _ok(hint=None, state="ok", path=path, version=file_version(path), running=running, web_ui=WEB_UI_URL)


def _moonlight_dirs():
    if IS_WIN:
        return _win_dirs("Moonlight Game Streaming")
    if D.IS_MAC:
        return ["/Applications/Moonlight.app/Contents/MacOS", "/opt/homebrew/bin", "/usr/local/bin"]
    return ["/usr/bin", "/usr/local/bin"]


def _moonlight_dialect():
    return {"form": "moonlight-qt", **{k: list(v) for k, v in MOONLIGHT_ARGV.items()}, "verified": False}


def find_moonlight(cfg_path=None, *, dirs=None, which=None):
    """{"ok","code","err","hint","state","path","version","warning","dialect"}. Moonlight не запускается.

    Порядок (§10.3): stream.moonlight_path → стандартные папки → на macOS/Linux shutil.which (с проверкой имени).
    Шаблоны argv — константы (НП, вживую не проверены): dialect["verified"] = False.
    """
    inst = install_info("moonlight")
    miss = {"ok": False, "code": "not_found", "err": "Moonlight не найден", "hint": inst["cmd"], "state": "missing",
            "path": None, "version": None, "warning": None, "dialect": None}
    cands, warn = [], None
    if cfg_path:
        p = str(cfg_path)
        if not os.path.isabs(p) or not os.path.isfile(p) or os.path.basename(p).lower() not in MOONLIGHT_NAMES:
            return {**miss, "code": "bad_config", "err": "stream.moonlight_path: нужен абсолютный путь к Moonlight"}
        cands.append(p)
        if D._system_dir_warning(p):
            warn = "Путь к Moonlight вне системных папок"
    for d in (dirs if dirs is not None else _moonlight_dirs()):
        for n in MOONLIGHT_NAMES:
            f = os.path.join(d, n)
            if os.path.isfile(f):
                cands.append(f)
    if not IS_WIN and dirs is None:
        import shutil
        w = (which or shutil.which)("moonlight") or (which or shutil.which)("moonlight-qt")
        if w:
            w = os.path.realpath(w)
            if os.path.basename(w).lower() in MOONLIGHT_NAMES:
                cands.append(w)
    if not cands:
        return miss
    p = cands[0]
    return {"ok": True, "code": None, "err": None, "hint": None, "state": "ok", "path": p,
            "version": file_version(p), "warning": warn, "dialect": _moonlight_dialect()}


def virtual_display_present(displays, match=None):
    """Первый экран с virtual=True; если флага нет — по подстрокам match в label/gdi_name. Иначе None."""
    for d in displays or []:
        if d.get("virtual"):
            return d
    subs = [m.lower() for m in (match or ()) if isinstance(m, str) and m]
    if subs:
        for d in displays or []:
            txt = f"{d.get('label') or ''} {d.get('gdi_name') or ''}".lower()
            if any(s in txt for s in subs):
                return d
    return None


# ── валидация и argv ──
def moonlight_pids(own_pid=None):
    """Запущенные Moonlight (в т.ч. открытые вручную). Только чтение списка процессов."""
    pids = []
    try:
        if os.name == "nt":
            rc, out = D._run(["tasklist", "/FI", "IMAGENAME eq moonlight.exe", "/FO", "CSV", "/NH"], timeout=8)
            for ln in (out or "").splitlines():
                m = re.match(r'^"moonlight\.exe","(\d+)"', ln.strip(), re.I)
                if m:
                    pids.append(int(m.group(1)))
        else:
            rc, out = D._run(["ps", "-A", "-o", "pid=", "-o", "comm="], timeout=5)
            for ln in (out or "").splitlines():
                m = re.match(r"\s*(\d+)\s+(.+)$", ln)
                if m and os.path.basename(m.group(2).strip()).lower() in MOONLIGHT_NAMES:
                    pids.append(int(m.group(1)))
    except Exception as e:
        log(f"moonlight_pids: {type(e).__name__}")
    return [p for p in pids if p != own_pid]


def valid_host(h):
    if not isinstance(h, str) or not h or h[0] == "-" or not D.HOST_RE.fullmatch(h):
        return False
    try:
        ipaddress.ip_address(h.strip("[]"))
        return True
    except ValueError:
        pass
    return re.fullmatch(r"[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?", h) is not None


def valid_app(a):
    return isinstance(a, str) and a[:1] != "-" and a != "" and APP_RE.fullmatch(a) is not None


def build_argv(kind, exe, host, *, app=None, resolution=None, fps=None, pin=None, template=None):
    """Список аргументов без shell. ValueError на неверных значениях."""
    tpl = template or MOONLIGHT_ARGV.get(kind)
    if tpl is None:
        raise ValueError("kind")
    if os.path.basename(str(exe)).lower() not in MOONLIGHT_NAMES:
        raise ValueError("имя программы не в белом списке")
    if not valid_host(host):
        raise ValueError("неверный адрес хоста")
    vals = {"exe": str(exe), "host": host}
    if kind == "stream":
        if not valid_app(app):
            raise ValueError("неверное имя приложения")
        vals["app"] = app
        if resolution is not None:
            r = f"{resolution[0]}x{resolution[1]}" if isinstance(resolution, (tuple, list)) else str(resolution)
            if not RES_RE.fullmatch(r):
                raise ValueError("неверное разрешение")
            vals["resolution"] = r
        if fps is not None:
            if isinstance(fps, bool) or not isinstance(fps, int) or not 10 <= fps <= 240:
                raise ValueError("неверная частота кадров")
            vals["fps"] = str(fps)
    if kind == "pair":
        if not isinstance(pin, str) or not PIN_RE.fullmatch(pin):
            raise ValueError("PIN — 4 цифры")
        vals["pin"] = pin
    out, i = [], 0
    while i < len(tpl):
        tok = tpl[i]
        m = re.fullmatch(r"\{(\w+)\}", tok)
        if m:
            if m.group(1) not in vals:
                raise ValueError(m.group(1))
            out.append(vals[m.group(1)])
        elif i + 1 < len(tpl) and tok.startswith("--") and tpl[i + 1].startswith("{") \
                and tpl[i + 1].strip("{}") not in vals:
            i += 2   # необязательная пара «флаг значение» без значения
            continue
        else:
            out.append(tok)
        i += 1
    return out


# ── Moonlight ──
class Moonlight:
    """Один свой процесс Moonlight stream. Лог — в файл moonlight.log (процесс переживает панель)."""
    POLL_S = 1.0

    def __init__(self, path, dialect, data_dir, on_event=None, runner=None, popen=None):
        self._path = str(path)
        self._dialect = dialect or {}
        self._dir = Path(data_dir) / "moonlight"
        self._on_event = on_event
        self._run = runner or D._run
        self._popen = popen or subprocess.Popen
        self._lock = threading.RLock()
        self._proc = None
        self._pid = None
        self._ident = None
        self._since = None
        self._host = None
        self._app = None
        self._err = None
        self._exit_reported = True
        self._stop_evt = None
        self._tpl = {k: self._dialect.get(k) or MOONLIGHT_ARGV[k] for k in MOONLIGHT_ARGV}

    def _emit(self, kind, payload=None):
        cb = self._on_event
        if cb:
            try:
                cb(kind, payload)
            except Exception as e:
                log(f"on_event({kind}): {type(e).__name__}")

    def _argv(self, kind, host, **kw):
        return build_argv(kind, self._path, host, template=self._tpl[kind], **kw)

    # ── жизнь процесса ──
    def _alive(self):
        proc, pid, ident = self._proc, self._pid, self._ident
        if proc is not None:
            return proc.poll() is None
        if pid:
            cur = D.proc_identity(pid)
            return bool(cur and ident and cur.get("image") == ident.get("image")
                        and cur.get("created") == ident.get("created"))
        return False

    def running(self):
        return bool(self._pid) and self._alive()

    def _watch(self, evt):
        while not evt.wait(self.POLL_S):
            if not self._alive():
                break
        else:
            return
        with self._lock:
            if self._exit_reported:
                return
            self._exit_reported = True
            code = self._proc.poll() if self._proc is not None else None
            if not self._err:
                self._err = "Moonlight завершился" + (f" (код {code})" if code is not None else "")
        self._emit("moonlight_exited", {"code": code})

    def _start_watch(self):
        evt = threading.Event()
        self._stop_evt = evt
        threading.Thread(target=self._watch, args=(evt,), name="moonlight-watch", daemon=True).start()

    def _log_tail(self, n=3):
        try:
            lines = (self._dir / "moonlight.log").read_text(encoding="utf-8", errors="replace").splitlines()
            return D._scrub(" | ".join(x.strip() for x in lines[-n:] if x.strip()), 160)
        except OSError:
            return ""

    def start(self, host, app, resolution=None, fps=DEFAULT_FPS):
        """Запустить `stream host app`. {"ok","code","err","hint","pid"}."""
        try:
            argv = self._argv("stream", host, app=app, resolution=resolution, fps=fps)
        except ValueError as e:
            return _err("bad_config", str(e))
        if self.running():
            self.stop(None)
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            lp = self._dir / "moonlight.log"
            if lp.is_file() and lp.stat().st_size > LOG_ROTATE_BYTES:
                os.replace(lp, self._dir / "moonlight.log.1")
            with open(lp, "ab") as logf:
                proc = self._popen(argv, stdin=subprocess.DEVNULL, stdout=logf, stderr=subprocess.STDOUT,
                                   cwd=str(self._dir), env=D._clean_env(), shell=False,
                                   creationflags=CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP)
        except OSError as e:
            return _err("spawn_failed", f"Moonlight не запустился: {type(e).__name__}")
        with self._lock:
            self._proc, self._pid = proc, proc.pid
            self._ident = D.proc_identity(proc.pid)
            self._host, self._app, self._err = host, app, None
            self._since = D.now_iso()
            self._exit_reported = False
        log(f"запущен Moonlight stream, pid {proc.pid}")
        end = time.monotonic() + START_CHECK_S
        while time.monotonic() < end:
            if proc.poll() is not None:
                with self._lock:
                    self._exit_reported = True
                    tail = self._log_tail()
                    self._err = f"Moonlight сразу завершился (код {proc.returncode})" + (f": {tail}" if tail else "")
                    err = self._err
                    self._proc, self._pid, self._ident = None, None, None
                return _err("exited", err, "Проверьте сопряжение (PIN) и имя приложения в Apollo")
            time.sleep(0.05)
        self._start_watch()
        return _ok(pid=proc.pid, since=self._since)

    def stop(self, host):
        """`quit host` (если host задан) + завершение СВОЕГО процесса. Чужие Moonlight не трогаем."""
        warn = None
        if host:
            try:
                rc, out = self._run(self._argv("quit", host), QUIT_TIMEOUT_S)
                if rc is None:
                    warn = "quit не выполнен: " + D._scrub(out, 60)
            except ValueError as e:
                warn = str(e)
        with self._lock:
            proc, pid, ident, evt = self._proc, self._pid, self._ident, self._stop_evt
            self._exit_reported = True
        try:
            if pid:
                if proc is not None:
                    D._stop_popen(proc, 3)
                elif self._alive():
                    D._kill_pid(pid, False)
                    end = time.monotonic() + 3
                    while time.monotonic() < end and self._alive():
                        time.sleep(0.05)
                    if self._alive():
                        D._kill_pid(pid, True)
        except Exception as e:
            log(f"stop: {type(e).__name__}")
            return _err("os_error", "Не удалось остановить Moonlight")
        finally:
            if evt is not None:
                evt.set()
        with self._lock:
            self._proc, self._pid, self._ident, self._since = None, None, None, None
        if pid:
            log("Moonlight остановлен")
        return _ok(stopped=bool(pid), warning=warn)

    def status(self):
        alive = self.running()
        with self._lock:
            return {"running": alive, "pid": self._pid if alive else None, "since": self._since if alive else None,
                    "err": self._err, "host": self._host, "app": self._app}

    def process_info(self):
        with self._lock:
            if not self._pid or not self._ident:
                return None
            return {"pid": self._pid, "image": self._ident.get("image"), "created": self._ident.get("created"),
                    "since": self._since, "host": self._host, "app": self._app}

    def adopt(self, info):
        """Перенять живой процесс из state.json (pid + образ + время создания совпадают, образ в белом списке)."""
        try:
            pid = info.get("pid")
            cur = D.proc_identity(pid)
            if (not cur or cur.get("image") != info.get("image") or cur.get("created") != info.get("created")
                    or cur["image"] not in MOONLIGHT_NAMES or self.running()):
                return False
            with self._lock:
                self._proc, self._pid, self._ident = None, pid, cur
                self._since, self._host, self._app = info.get("since"), info.get("host"), info.get("app")
                self._err, self._exit_reported = None, False
            self._start_watch()
            log(f"перенят Moonlight pid {pid}")
            return True
        except Exception as e:
            log(f"adopt: {type(e).__name__}")
            return False

    # ── вспомогательное ──
    def list_apps(self, host):
        try:
            rc, out = self._run(self._argv("list", host), 15)
        except ValueError as e:
            return _err("bad_config", str(e), apps=[])
        if rc is None:
            return _err("timeout" if out == "timeout" else "spawn_failed", "Moonlight не ответил", apps=[])
        apps = [ln.strip() for ln in out.splitlines() if ln.strip() and APP_RE.fullmatch(ln.strip())]
        return _ok(apps=apps)

    def pair(self, host, pin=None):
        """НП: `pair host --pin NNNN`. Без PIN — подсказка ввести PIN в веб-интерфейсе Apollo."""
        if pin is None:
            return _err("bad_config", "Нужен PIN, показанный Moonlight",
                        "Введите PIN, показанный Moonlight, в веб-интерфейсе Apollo")
        try:
            rc, out = self._run(self._argv("pair", host, pin=pin), 60)
        except ValueError as e:
            return _err("bad_config", str(e))
        if rc != 0:
            return _err("auth_failed", "Сопряжение не удалось: " + D._scrub(out, 100))
        return _ok()
