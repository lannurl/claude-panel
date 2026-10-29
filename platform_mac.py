"""Платформенный слой панели для macOS.

Всё mac-специфичное (fcntl, AppKit, pynput, osascript, security) импортируется/вызывается внутри
функций и обёрнуто в try — модуль импортируется на любой ОС (так его можно проверить хотя бы
на синтаксис и импорт на Windows).

Что требует разрешений macOS (Системные настройки → Конфиденциальность и безопасность):
  • Универсальный доступ (Accessibility) — глобальная горячая клавиша ⌘⌥P через pynput;
  • Автоматизация (Automation) — чтение Spotify / Music / Google Chrome / Safari через AppleScript;
  • Chrome: View → Developer → Allow JavaScript from Apple Events;
    Safari: Develop → Allow JavaScript from Apple Events — для YouTube в браузере.
Живое видео (как DWM thumbnail на Windows) на macOS не поддерживается — показывается обложка.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

NAME = "macos"

VideoMirror = None  # живое видео на macOS не поддерживается (нужен ScreenCaptureKit)

HOTKEY_LABEL = "⌘⌥P"
HOTKEY_DEFAULT = False  # pynput может уронить процесс на новых macOS — только явно: "hotkey": true в config.json
COPY_LABEL = "⌘C"
RIGHT_CLICK = ("<Button-2>", "<Button-3>", "<Control-Button-1>")  # в Tk на Aqua правая кнопка — Button-2
WITHDRAW_WHEN_HIDDEN = True  # macOS может не пустить окно за верхний край — прячем окно целиком
EXTRA_PASSWORD_APPS = ("KeePassXC", "Keychain Access", "Связка ключей", "Passwords", "Пароли")

_SANS = ("SF Pro Text", "SF Pro", "Helvetica Neue", "Helvetica")
# Tk на macOS считает размер шрифта в пунктах при 72 dpi — чуть крупнее, чем на Windows (96 dpi)
FONTS = {
    "base": (_SANS, 13, {}),
    "small": (_SANS, 12, {}),
    "tiny": (_SANS, 11, {}),
    "bold": (_SANS, 13, {"weight": "bold"}),
    "track": (_SANS, 17, {"weight": "bold"}),
    "title": (("Menlo", "SF Mono", "Monaco"), 14, {"weight": "bold"}),
    "mono": (("Menlo", "SF Mono", "Monaco"), 12, {}),
    "num": (("Menlo", "SF Mono", "Monaco"), 17, {"weight": "bold"}),
    "big": (("SF Pro Text", "Apple Symbols", "Helvetica Neue"), 24, {}),
}

OSA_TIMEOUT = 2.0
SUPPORT_DIR = Path.home() / "Library" / "Application Support" / "MyPanel"


def log(msg):
    """Технические события в stderr. Названия треков, токены и т.п. сюда не пишем."""
    try:
        if sys.stderr:
            print(f"[panel/mac] {msg}", file=sys.stderr, flush=True)
    except Exception:
        pass


# ── osascript ──
def _as_str(s):
    """Строковый литерал AppleScript."""
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'


def osa(script, timeout=OSA_TIMEOUT):
    """Выполнить AppleScript (через stdin, без кавычек в argv). Результат — stdout или None при ошибке."""
    try:
        r = subprocess.run(["osascript", "-"], input=script, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        log("osascript: таймаут")
        return None
    except (OSError, ValueError) as e:
        log(f"osascript недоступен: {type(e).__name__}")
        return None
    if r.returncode != 0:
        err = (r.stderr or "").strip().splitlines()
        # -1743 — нет разрешения Automation, -600 — приложение не запущено; текст ошибки без данных пользователя
        code = re.search(r"\((-?\d+)\)\s*$", err[-1]) if err else None
        log(f"osascript: ошибка {code.group(1) if code else r.returncode}")
        return None
    return r.stdout.rstrip("\n")


def app_running(name):
    """Запущено ли приложение (по имени процесса). Не запускает его и не вызывает диалог «Где …?»."""
    try:
        r = subprocess.run(["pgrep", "-x", name], capture_output=True, timeout=OSA_TIMEOUT)
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _bg(fn, *args):
    t = threading.Thread(target=fn, args=args, daemon=True)
    t.start()
    return t


# ── Один экземпляр ──
_lock_file = None


def single_instance():
    """True — мы единственный экземпляр (fcntl.flock на файле в Application Support)."""
    global _lock_file
    try:
        import fcntl
        SUPPORT_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(SUPPORT_DIR / "panel.lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        _lock_file = fd  # держим открытым всё время работы — блокировка снимается при выходе
        return True
    except Exception as e:  # не смогли проверить — лучше запуститься, чем не запуститься вовсе
        log(f"single instance: {type(e).__name__}")
        return True


def ui_scale(root):
    """На macOS tk работает в пунктах (Retina масштабирует система) — масштаб 1."""
    return 1.0


def prepare_window(root):
    try:
        root.overrideredirect(True)
    except Exception as e:
        log(f"overrideredirect: {type(e).__name__}")
    try:
        root.wm_attributes("-topmost", True)
    except Exception as e:
        log(f"topmost: {type(e).__name__}")


def wheel_units(delta):
    """На Aqua delta колеса маленькая (±1…±10 на щелчок), в Tk 9 — кратна 120."""
    if abs(delta) >= 120:
        return int(-delta / 120)
    return (delta < 0) - (delta > 0)


# ── Курсор и мониторы ──
_appkit = None  # None — ещё не пробовали, False — pyobjc нет


def _AppKit():
    global _appkit
    if _appkit is None:
        try:
            import AppKit
            _appkit = AppKit
        except Exception:
            _appkit = False
    return _appkit or None


def cursor_pos(root=None):
    if root is None:
        return 0, 0
    try:
        return root.winfo_pointerxy()
    except Exception:
        return 0, 0


def monitor_rect(x, y, root=None):
    """(left, top, right, bottom) экрана под точкой в координатах tk (y вниз от верха главного экрана)."""
    ak = _AppKit()
    if ak:
        try:
            screens = list(ak.NSScreen.screens())
            if screens:
                main_h = screens[0].frame().size.height  # screens[0] — экран со строкой меню, начало координат
                rects = []
                for s in screens:
                    f = s.frame()
                    left, top = int(f.origin.x), int(main_h - f.origin.y - f.size.height)
                    rects.append((left, top, left + int(f.size.width), top + int(f.size.height)))
                for r in rects:
                    if r[0] <= x < r[2] and r[1] <= y < r[3]:
                        return r
                return rects[0]
        except Exception as e:
            log(f"NSScreen: {type(e).__name__}")
    if root is not None:
        try:
            return 0, 0, root.winfo_screenwidth(), root.winfo_screenheight()
        except Exception:
            pass
    return 0, 0, 1440, 900


# ── Окна, процессы, буфер обмена ──
def foreground_title():
    """Имя активного приложения (для игнора менеджеров паролей). Пусто — не удалось узнать."""
    ak = _AppKit()
    if ak:
        try:
            app = ak.NSWorkspace.sharedWorkspace().frontmostApplication()
            if app is not None:
                return str(app.localizedName() or "")
        except Exception:
            pass
    out = osa('tell application "System Events" to get name of first application process whose frontmost is true',
              timeout=1.5)
    return out or ""


def pid_alive(pid):
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # процесс есть, но чужой
        return True
    except OSError:
        return False
    return True


_CONCEALED = {"org.nspasteboard.ConcealedType", "org.nspasteboard.TransientType",
              "org.nspasteboard.AutoGeneratedType"}


def clipboard_excluded(root=None):
    """True — содержимое помечено менеджером паролей как секретное/временное (nspasteboard.org). Нужен pyobjc."""
    ak = _AppKit()
    if not ak:
        return False
    try:
        types = ak.NSPasteboard.generalPasteboard().types() or []
        return any(str(t) in _CONCEALED for t in types)
    except Exception as e:
        log(f"clipboard_excluded: {type(e).__name__}")
        return True


def clipboard_seq(root=None):
    """Признак изменения буфера: changeCount NSPasteboard (pyobjc), иначе — хеш текста из tk."""
    ak = _AppKit()
    if ak:
        try:
            return int(ak.NSPasteboard.generalPasteboard().changeCount())
        except Exception:
            pass
    if root is None:
        return 0
    try:
        return hash(root.clipboard_get())
    except Exception:
        return 0


# ── Горячая клавиша ⌘⌥P (pynput, опционально) ──
def hotkey_thread(q):
    """Глобальная ⌘⌥P. Нет pynput или нет доступа Accessibility — шлём 'hotkey-unavailable' и выходим."""
    try:
        from pynput import keyboard
    except Exception:
        log("pynput не установлен — горячая клавиша выключена, работает верхний край экрана")
        q.put("hotkey-unavailable")
        return
    try:
        listener = keyboard.GlobalHotKeys({"<cmd>+<alt>+p": lambda: q.put("toggle")})
        listener.start()
        time.sleep(0.5)
        if getattr(listener, "IS_TRUSTED", True) is False or not listener.is_alive():
            log("нет доступа Accessibility — горячая клавиша не работает")
            q.put("hotkey-unavailable")
            try:
                listener.stop()
            except Exception:
                pass
            return
        listener.join()
    except Exception as e:
        log(f"горячая клавиша: {type(e).__name__}")
        q.put("hotkey-unavailable")


# ── Громкость ──
_VOLUME = {
    "vup": "set volume output volume ((output volume of (get volume settings)) + 6)",
    "vdown": "set volume output volume ((output volume of (get volume settings)) - 6)",
    "mute": "set volume output muted (not (output muted of (get volume settings)))",
}


def media_key(name):
    """Громкость — системная через osascript. play/next/prev без активного плеера — некуда отправить."""
    if name in _VOLUME:
        _bg(osa, _VOLUME[name])


# ── Что сейчас играет: Spotify, Music, YouTube в Chrome / Safari ──
SEP = "\x1e"  # разделитель полей в ответе AppleScript (character id 30)

_SPOTIFY = """
tell application "Spotify"
    set st to player state as string
    if st is "stopped" then return st
    set t to current track
    set sep to character id 30
    return st & sep & (name of t) & sep & (artist of t) & sep & (album of t) & sep & (player position as string) & sep & ((duration of t) as string) & sep & (artwork url of t)
end tell
"""

_MUSIC = """
tell application "Music"
    set st to player state as string
    if st is "stopped" then return st
    set t to current track
    set sep to character id 30
    return st & sep & (name of t) & sep & (artist of t) & sep & (album of t) & sep & (player position as string) & sep & ((duration of t) as string)
end tell
"""

_MUSIC_ART = """
tell application "Music"
    try
        return raw data of artwork 1 of current track
    on error
        return ""
    end try
end tell
"""

_YT_INFO_JS = (
    "(function(){var v=document.querySelector('video');if(!v)return '';"
    "var h=document.querySelector('h1.ytd-watch-metadata yt-formatted-string')||document.querySelector('h1.title');"
    "var t=(h&&h.textContent)||document.title.replace(/ - YouTube$/,'');"
    "var c=document.querySelector('#owner #channel-name a')||document.querySelector('ytd-channel-name a');"
    "var id=new URLSearchParams(location.search).get('v')||'';"
    "return JSON.stringify({title:t.trim(),channel:c?c.textContent.trim():'',pos:v.currentTime||0,"
    "end:isFinite(v.duration)?v.duration:0,paused:v.paused,id:id});})()"
)
_YT_CONTROL_JS = {
    "play": "(function(){var v=document.querySelector('video');if(v){v.paused?v.play():v.pause();}return '';})()",
    "next": "(function(){var b=document.querySelector('.ytp-next-button');if(b)b.click();return '';})()",
    "prev": "(function(){var v=document.querySelector('video');if(v)v.currentTime=0;return '';})()",
}

_BROWSERS = {
    "Google Chrome": ("active tab of front window", "execute ({tab}) javascript {js}"),
    "Safari": ("current tab of front window", "do JavaScript {js} in ({tab})"),
}


def _browser_script(app, js):
    """AppleScript: если в активной вкладке YouTube-видео — выполнить в ней JS, иначе вернуть ''."""
    tab, call = _BROWSERS[app]
    return (f'tell application {_as_str(app)}\n'
            f'    if (count of windows) is 0 then return ""\n'
            f'    set u to URL of ({tab})\n'
            f'    if u does not contain "youtube.com/watch" then return ""\n'
            f'    return {call.format(tab=tab, js=_as_str(js))}\n'
            f'end tell\n')


def _num(s):
    try:
        return float(str(s).strip().replace(",", "."))  # AppleScript может вернуть десятичную запятую
    except (TypeError, ValueError):
        return 0.0


ART_HOSTS = ("i.scdn.co", "i.ytimg.com")
ART_HOST_SUFFIXES = (".spotifycdn.com",)


def art_url_allowed(url):
    """Обложки — только https и только с CDN Spotify / YouTube."""
    try:
        u = urllib.parse.urlsplit(url or "")
        host = (u.hostname or "").lower()
        port = u.port
    except ValueError:
        return False
    if u.scheme != "https" or port not in (None, 443) or u.username or u.password:
        return False
    return host in ART_HOSTS or any(host.endswith(s) for s in ART_HOST_SUFFIXES)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # редирект -> HTTPError: на другой хост/схему не уходим


_opener = urllib.request.build_opener(_NoRedirect)


def _download(url, limit=4 * 1024 * 1024):
    if not art_url_allowed(url):
        log("обложка: адрес не из списка разрешённых")
        return None
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "my-panel"})
        with _opener.open(req, timeout=5) as r:
            data = r.read(limit + 1)
        return data if len(data) <= limit else None
    except Exception as e:
        log(f"обложка не скачалась: {type(e).__name__}")
        return None


def _hex_data(out):
    """raw data из AppleScript приходит как «data tdtaFFD8…» — достать байты."""
    m = re.search(r"«data \w{4}([0-9A-Fa-f]+)»", out or "")
    if not m:
        return None
    try:
        return bytes.fromhex(m.group(1))
    except ValueError:
        return None


class Media:
    """Что сейчас играет — опрос через osascript раз в ~1.5 с в фоновом потоке."""

    PERIOD = 1.5

    def __init__(self):
        self.info, self.source = None, None
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self._thumb_key, self._thumb = None, None
        _bg(self._loop)

    # опрос источников: каждый возвращает dict (как info) или None
    def _spotify(self):
        out = osa(_SPOTIFY)
        if not out or out == "stopped":
            return None
        f = out.split(SEP)
        if len(f) < 7:
            return None
        return {"app": "Spotify", "title": f[1], "artist": f[2], "album": f[3], "playing": f[0] == "playing",
                "pos": _num(f[4]), "end": _num(f[5]) / 1000, "art": ("url", f[6].strip())}

    def _music(self):
        out = osa(_MUSIC)
        if not out or out == "stopped":
            return None
        f = out.split(SEP)
        if len(f) < 6:
            return None
        return {"app": "Music", "title": f[1], "artist": f[2], "album": f[3], "playing": f[0] == "playing",
                "pos": _num(f[4]), "end": _num(f[5]), "art": ("music", None)}

    def _youtube(self, app):
        out = osa(_browser_script(app, _YT_INFO_JS))
        if not out:
            return None
        try:
            d = json.loads(out)
        except ValueError:
            return None
        if not isinstance(d, dict) or not d.get("title"):
            return None
        vid = str(d.get("id") or "")
        art = ("url", f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg") if re.fullmatch(r"[\w-]{6,20}", vid) else (None, None)
        return {"app": app, "title": str(d["title"]), "artist": str(d.get("channel") or ""), "album": "",
                "playing": not d.get("paused", True), "pos": _num(d.get("pos")), "end": _num(d.get("end")), "art": art}

    def _collect(self):
        found = []
        for name, fn in (("Spotify", self._spotify), ("Music", self._music),
                         ("Google Chrome", lambda: self._youtube("Google Chrome")),
                         ("Safari", lambda: self._youtube("Safari"))):
            if not app_running(name):
                continue
            try:
                d = fn()
            except Exception as e:
                log(f"{name}: {type(e).__name__}")
                d = None
            if d:
                if d["playing"]:
                    return d
                found.append(d)
        return found[0] if found else None

    def _art(self, d):
        key = (d["app"], d["title"], d["artist"])
        if key == self._thumb_key:
            return self._thumb
        kind, url = d.get("art") or (None, None)
        thumb = None
        if kind == "url":
            thumb = _download(url)
        elif kind == "music":
            thumb = _hex_data(osa(_MUSIC_ART, timeout=3))
        self._thumb_key, self._thumb = key, thumb
        return thumb

    def _loop(self):
        while True:
            info = None
            try:
                d = self._collect()
                if d:
                    info = {k: d[k] for k in ("app", "title", "artist", "album", "playing", "pos", "end")}
                    info["upd"], info["thumb"] = time.time(), self._art(d)
                self.source = d["app"] if d else None
            except Exception as e:
                log(f"media: {type(e).__name__}")
                self.source = None
            with self.lock:
                self.info = info
            self.wake.wait(self.PERIOD)
            self.wake.clear()

    def control(self, action):
        src = self.source
        if not src:
            return
        _bg(self._control, src, action)

    def _control(self, src, action):
        if src == "Spotify":
            cmd = {"play": "playpause", "next": "next track", "prev": "previous track"}[action]
            osa(f'tell application "Spotify" to {cmd}')
        elif src == "Music":
            cmd = {"play": "playpause", "next": "next track", "prev": "back track"}[action]
            osa(f'tell application "Music" to {cmd}')
        elif src in _BROWSERS:
            osa(_browser_script(src, _YT_CONTROL_JS[action]))
        self.wake.set()  # обновить «что играет» сразу, не дожидаясь периода


# ── VS Code ──
def find_code():
    found = shutil.which("code")
    if found:
        return found
    for c in ("/usr/local/bin/code", "/opt/homebrew/bin/code",
              "/Applications/Visual Studio Code.app/Contents/Resources/app/bin/code",
              str(Path.home() / "Applications/Visual Studio Code.app/Contents/Resources/app/bin/code")):
        if os.path.exists(c):
            return c
    return None


CODE = find_code()


def _popen(args):
    try:
        subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return None
    except OSError as e:
        log(f"запуск {args[0]}: {type(e).__name__}")
        return "Не удалось запустить VS Code"


def open_folder_in_code(path):
    if CODE:
        return _popen([CODE, path])
    return _popen(["open", "-a", "Visual Studio Code", path])


def open_uri_in_code(url):
    if not url.startswith("vscode://"):
        return "Недопустимая ссылка"
    return _popen(["open", url])


# ── Лимиты: токен входа Claude Code ──
def read_oauth():
    """claudeAiOauth: на macOS Claude Code хранит его в Связке ключей; запасной вариант — ~/.claude/.credentials.json."""
    try:
        r = subprocess.run(["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
                           capture_output=True, text=True, timeout=5)
        if r.returncode == 0 and r.stdout.strip():
            d = json.loads(r.stdout.strip())
            if isinstance(d, dict) and d.get("claudeAiOauth"):
                return d["claudeAiOauth"]
    except (OSError, subprocess.TimeoutExpired, ValueError) as e:
        log(f"Связка ключей: {type(e).__name__}")
    path = Path.home() / ".claude" / ".credentials.json"
    return json.loads(path.read_text(encoding="utf-8")).get("claudeAiOauth") or {}


# ── Живое видео — не поддерживается ──
def panel_hwnd(root):
    return None


def window_alive(h):
    return False


def window_minimized(h):
    return True


def focus_window(h):
    return None
