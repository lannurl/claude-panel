"""Моя панель — бесплатная панель у верхнего края экрана.

Сессии Claude Code (изометрический «офис» агентов, клик — открыть VS Code) и лимиты как в /usage,
история буфера обмена, что сейчас играет (музыка и YouTube) с управлением.
Подведи курсор к верхнему краю экрана (по центру) или нажми Ctrl+Alt+P (на macOS — ⌘⌥P).
Закрыть программу: правый клик по заголовку «моя панель».

Работает на Windows и macOS: всё ОС-зависимое — в platform_win.py / platform_mac.py.
Необязательный config.json рядом со скриптом: {"main_agent_name": "Claude", "hotkey": true}
(hotkey по умолчанию: Windows — true, macOS — false).

Лимиты: токен входа Claude Code читается из ~/.claude/.credentials.json (на macOS — из Связки ключей)
и отправляется только на api.anthropic.com (тот же запрос, что делает /usage). История буфера — только в памяти.
"""
import io
import json
import os
import queue
import re
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageTk

if sys.platform == "win32":
    import platform_win as plat
else:
    import platform_mac as plat

VideoMirror = plat.VideoMirror  # живое видео (только Windows); None — показываем обложку

# ── Один экземпляр ──
if not plat.single_instance() and "--test" not in sys.argv:
    sys.exit(0)

HERE = Path(__file__).resolve().parent


def load_config():
    """config.json рядом со скриптом — необязателен. Неверные значения игнорируются (берутся по умолчанию)."""
    cfg = {"main_agent_name": "Claude", "hotkey": plat.HOTKEY_DEFAULT}
    path = HERE / "config.json"
    if not path.exists():
        return cfg
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        plat.log(f"config.json не прочитан ({type(e).__name__}) — настройки по умолчанию")
        return cfg
    if not isinstance(d, dict):
        plat.log("config.json: ожидается объект — настройки по умолчанию")
        return cfg
    name = d.get("main_agent_name")
    if isinstance(name, str) and name.strip():
        cfg["main_agent_name"] = " ".join(name.split())[:24]
    if isinstance(d.get("hotkey"), bool):
        cfg["hotkey"] = d["hotkey"]
    return cfg


CONFIG = load_config()
CLAUDE = Path.home() / ".claude"
HISTORY_MAX = 40
PASSWORD_APPS = ("KeePass", "Bitwarden", "1Password", "Менеджер паролей") + tuple(plat.EXTRA_PASSWORD_APPS)
HINT = (f"{plat.HOTKEY_LABEL} — открыть/закрыть · Esc — спрятать" if CONFIG["hotkey"]
        else f"{plat.HOTKEY_LABEL}: включи hotkey в config.json · Esc — спрятать")
UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
OPEN_URL_DELAY = 3.0  # с: даём VS Code открыть папку, потом просим расширение Claude открыть сессию
RECENT_MAX, RECENT_DAYS = 20, 30
TITLE_MAX = 50

# Палитра «ночной офис»
C = {
    "bg": "#0e0f1a", "head": "#13152a", "item": "#1a1d37", "hover": "#24284d", "line": "#2a2e58",
    "ink": "#eef0ff", "muted": "#8e93c4", "accent": "#ffb547", "accent_ink": "#1a1206",
    "mint": "#5ee6a8", "violet": "#8b7bff", "red": "#ff6b81", "field": "#0a0b14",
    "wall": "#121430", "tile_a": "#1e2148", "tile_b": "#1a1d40", "wood": "#9a6b45", "wood_dark": "#6e4a2f",
    "metal": "#3a3e72", "keys": "#272a52", "screen_on": "#0b3a2e", "screen_off": "#0d1030",
}
AGENT_COLORS = ["#8b7bff", "#5ee6a8", "#4fc3f7", "#ff8fb1", "#ffd166"]
CLAUDE_ORANGE = "#e8885a"
SUB_SCALE, SUB_MAX = 0.62, 8  # субагент в офисе меньше главного; больше SUB_MAX в группе не рисуем
GROUP_LABEL_MAX = 20          # подпись группы в офисе — до ~20 символов

# ── ОС-зависимое — из platform_win / platform_mac ──
media_key, pid_alive, foreground_title = plat.media_key, plat.pid_alive, plat.foreground_title


def open_vscode(cwd):
    """Открыть (или вывести вперёд) окно VS Code с папкой сессии. None — успех, иначе текст ошибки."""
    if cwd and str(cwd).startswith(("\\\\", "//")):  # UNC: даже проверка exists() идёт в сеть
        return "Сетевые папки не открываются"
    if not cwd or not Path(cwd).exists():
        return "Папка сессии не найдена — открыть не получится"
    return plat.open_folder_in_code(os.path.abspath(cwd))


def open_session(cwd, session_id):
    """Открыть папку в VS Code, а через ~3 с — саму сессию в расширении Claude Code (URI-хендлер).

    None — успех, иначе текст ошибки.
    """
    err = open_vscode(cwd)
    if err:
        return err
    sid = session_id or ""
    if UUID_RE.fullmatch(sid):
        url = f"vscode://anthropic.claude-code/open?session={sid.lower()}"
        t = threading.Timer(OPEN_URL_DELAY, plat.open_uri_in_code, args=(url,))
        t.daemon = True
        t.start()
    return None


def plural(n, one, few, many):
    if n % 10 == 1 and n % 100 != 11:
        return one
    return few if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else many


def clip(text, n):
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[:n - 1].rstrip() + "…"


AGENT_SHORT = {"code-reviewer": "review", "qa-tester": "qa", "general-purpose": "general", "explore": "explore",
               "statusline-setup": "statusline", "claude-code-guide": "guide"}
MAIN_AGENT = CONFIG["main_agent_name"]  # так подписан главный агент сессии (config.json, по умолчанию «Claude»)


def agent_label(m):
    """Короткое имя субагента: frontend-dev → «frontend», code-reviewer → «review», qa-tester → «qa»…"""
    t = (m.get("agentType") or m.get("subagent_type") or "").strip()
    if not t:  # типа нет — первое слово описания
        words = (m.get("description") or "").split()
        return clip(words[0].lower(), 12) if words else "агент"
    low = t.lower()
    if low in AGENT_SHORT:
        return AGENT_SHORT[low]
    for suf in ("-dev", "-agent", "-reviewer"):
        if low.endswith(suf) and len(low) > len(suf):
            return low[:-len(suf)]
    return low


def session_title(project, title, name=""):
    """Заголовок сессии: «проект · название вкладки», без названия — «проект · 9b» (суффикс из name)."""
    if title:
        return f"{project} · {title}"
    tail = (name or "").rsplit("-", 1)[-1] if name else ""
    return f"{project} · {tail}" if tail and tail != project else (project or "сессия")


# ── Claude Code: заголовки сессий из jsonl (читаем только начало и хвост файла) ──
HEAD_BYTES, TAIL_BYTES, HEAD_LINES = 256 * 1024, 64 * 1024, 40
CWD_RE = re.compile(rb'"cwd":"((?:[^"\\]|\\.)*)"')
_head_cache, _tail_cache = {}, {}  # path -> (cwd, первое сообщение); (path, mtime) -> заголовок из хвоста
_cache_lock = threading.Lock()


def _jsonl_head(path):
    """cwd и первый текст пользователя — из первых ~40 строк. Они не меняются, кешируем по пути."""
    key = str(path)
    with _cache_lock:
        if key in _head_cache:
            return _head_cache[key]
    with open(path, "rb") as f:
        data = f.read(HEAD_BYTES)
    m = CWD_RE.search(data)
    cwd = ""
    if m:
        try:
            cwd = json.loads(b'"' + m.group(1) + b'"')
        except ValueError:
            pass
    first = ""
    for line in data.split(b"\n")[:HEAD_LINES]:
        if b'"type":"user"' not in line:
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if d.get("type") != "user" or d.get("isMeta") or d.get("isSidechain"):
            continue
        content = (d.get("message") or {}).get("content")
        texts = [content] if isinstance(content, str) else [
            c.get("text") or "" for c in (content or []) if isinstance(c, dict) and c.get("type") == "text"]
        for t in texts:
            t = " ".join(t.split())
            if t and not t.startswith("<"):
                first = t
                break
        if first:
            break
    res = (cwd, first)
    if cwd or first:  # пустой новый файл не кешируем — строки ещё допишутся
        with _cache_lock:
            _head_cache[key] = res
    return res


def _jsonl_tail_title(path, size, mtime):
    """customTitle → aiTitle → последний summary — из хвоста ~64 КБ, кеш по (path, mtime)."""
    key = (str(path), mtime)
    with _cache_lock:
        if key in _tail_cache:
            return _tail_cache[key]
    with open(path, "rb") as f:
        start = max(0, size - TAIL_BYTES)
        f.seek(start)
        lines = f.read().split(b"\n")
    if start:
        lines = lines[1:]  # первая строка обрезана посередине
    custom = ai = summary = ""
    for line in lines:
        if len(line) > 8192 or not (b'Title"' in line or b'"summary"' in line):
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if not isinstance(d, dict):
            continue
        custom = d.get("customTitle") or custom
        ai = d.get("aiTitle") or ai
        if d.get("type") == "summary" and d.get("summary"):
            summary = d["summary"]
    title = custom or ai or summary
    with _cache_lock:
        for k in [k for k in _tail_cache if k[0] == key[0]]:
            del _tail_cache[k]
        _tail_cache[key] = title
    return title


def read_jsonl_info(path, st=None):
    """{'cwd', 'title'} для файла сессии; title — название вкладки Claude (до TITLE_MAX символов) или ''."""
    try:
        st = st or path.stat()
        cwd, first = _jsonl_head(path)
        title = _jsonl_tail_title(path, st.st_size, st.st_mtime) or first
    except OSError:
        return None
    return {"cwd": cwd, "title": clip(title, TITLE_MAX) if title else ""}


# ── Claude Code: сессии и агенты ──
def read_agents(sid):
    """Живые субагенты сессии: не finished и jsonl менялся за последние 15 минут."""
    agents = []
    if not UUID_RE.fullmatch(sid or ""):
        return agents
    for meta in sorted(CLAUDE.glob(f"projects/*/{sid}/subagents/*.meta.json")):
        try:
            m = json.loads(meta.read_text(encoding="utf-8"))
        except Exception:
            continue
        if m.get("finished"):
            continue
        jl = meta.with_name(meta.name.replace(".meta.json", ".jsonl"))
        try:
            age = time.time() - jl.stat().st_mtime
        except OSError:
            age = 1e9
        if age < 900:
            agents.append({"desc": m.get("description") or m.get("agentType") or "агент",
                           "type": agent_label(m), "busy": age < 90})
    return agents


def read_sessions():
    out = []
    for f in (CLAUDE / "sessions").glob("*.json"):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            if not pid_alive(d.get("pid", 0)):
                continue
        except Exception:
            continue
        sid = d.get("sessionId", "") or ""
        cwd = d.get("cwd", "")
        folder = Path(cwd).name if cwd else ""
        title = ""
        if UUID_RE.fullmatch(sid):
            for jl in CLAUDE.glob(f"projects/*/{sid}.jsonl"):
                info = read_jsonl_info(jl)
                title = (info or {}).get("title", "")
                break
        out.append({
            "id": sid, "name": session_title(folder, title, d.get("name") or ""), "title": title,
            "folder": folder, "cwd": cwd, "busy": d.get("status") == "busy",
            "updated": (d.get("statusUpdatedAt") or d.get("updatedAt") or 0) / 1000,
            "agents": read_agents(sid), "vscode": d.get("entrypoint") == "claude-vscode",
        })
    out.sort(key=lambda s: (not s["busy"], -s["updated"]))
    return out


class RecentSessions:
    """Недавние (неактивные) сессии всех проектов: ~/.claude/projects/<проект>/<sessionId>.jsonl.

    Сканирование — в фоновом потоке, не чаще раза в 10 с; файлы читаются частично и кешируются.
    """

    def __init__(self):
        self.items, self.loaded, self.error, self.at = [], False, None, 0
        self.exclude = frozenset()
        self.lock = threading.Lock()
        self.wake = threading.Event()

    def collect(self, exclude):
        cutoff = time.time() - RECENT_DAYS * 86400
        files = []
        for p in (CLAUDE / "projects").glob("*/*.jsonl"):  # только верхний уровень, subagents глубже
            sid = p.stem
            if not UUID_RE.fullmatch(sid) or sid in exclude:
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            if st.st_mtime >= cutoff:
                files.append((st.st_mtime, p, st))
        files.sort(key=lambda t: t[0], reverse=True)
        out = []
        for mtime, p, st in files:
            if len(out) >= RECENT_MAX:
                break
            info = read_jsonl_info(p, st)
            if not info or not (info["title"] or info["cwd"]):
                continue
            folder = Path(info["cwd"]).name if info["cwd"] else p.parent.name
            out.append({"id": p.stem, "cwd": info["cwd"], "folder": folder, "title": info["title"],
                        "name": session_title(folder, info["title"], p.stem[:2]), "mtime": mtime})
        return out

    def loop(self):
        while True:
            self.wake.wait(60)
            self.wake.clear()
            try:
                items, err = self.collect(self.exclude), None
            except Exception as e:
                items, err = None, f"не удалось прочитать ({type(e).__name__})"
            with self.lock:
                if items is not None:
                    self.items, self.loaded = items, True
                self.error, self.at = err, time.time()

    def refresh(self, exclude):
        self.exclude = frozenset(exclude)
        if time.time() - self.at >= 10:
            self.wake.set()

    def snapshot(self):
        with self.lock:
            return [r for r in self.items if r["id"] not in self.exclude], self.loaded, self.error


class Limits:
    """Лимиты как в /usage: GET api.anthropic.com/api/oauth/usage с токеном входа Claude Code."""

    PERIOD, MIN_GAP, BACKOFF = 120, 60, 600  # с: плановый опрос, минимум между попытками, пауза после ошибки

    def __init__(self):
        self.data, self.error, self.plan, self.at = None, None, "", 0
        self.tried = time.time()  # стартовый запрос делает loop(); refresh_if_stale до него не будит
        self.pause_until = 0
        self.lock = threading.Lock()
        self.wake = threading.Event()

    def fetch(self):
        self.tried = time.time()
        try:
            try:
                cred = plat.read_oauth()
            except FileNotFoundError:
                cred = {}
            if not cred.get("accessToken"):  # не вошли в Claude Code — запрос не делаем
                with self.lock:
                    self.pause_until = time.time() + self.MIN_GAP
                    self.error = "войди в Claude Code"
                return
            req = urllib.request.Request("https://api.anthropic.com/api/oauth/usage", headers={
                "Authorization": f"Bearer {cred.get('accessToken', '')}",
                "anthropic-beta": "oauth-2025-04-20", "User-Agent": "my-panel"})
            with urllib.request.urlopen(req, timeout=15) as r:
                d = json.loads(r.read())
            with self.lock:
                self.data, self.error, self.at = d, None, time.time()
                self.plan = (cred.get("subscriptionType") or "").capitalize()
        except urllib.error.HTTPError as e:
            wait = self.BACKOFF
            if e.code == 429:  # сервер просит подождать — уважаем Retry-After
                try:
                    wait = max(self.MIN_GAP, int(float(e.headers.get("Retry-After") or self.BACKOFF)))
                except (TypeError, ValueError):
                    pass
            with self.lock:
                self.pause_until = time.time() + wait
                self.error = ("войди в Claude Code заново" if e.code == 401 else
                              f"слишком часто, повтор через {max(1, round(wait / 60))} мин" if e.code == 429 else
                              f"сервер ответил {e.code}")
        except Exception as e:
            with self.lock:
                self.pause_until = time.time() + self.MIN_GAP
                self.error = f"нет связи ({type(e).__name__})"

    def loop(self):
        while True:
            left = self.pause_until - time.time()
            if left > 0:  # пауза после ошибки: просьбы обновить игнорируем
                time.sleep(min(left, self.BACKOFF))
                self.wake.clear()
                continue
            self.fetch()
            self.wake.wait(self.PERIOD)
            self.wake.clear()

    def refresh_if_stale(self):
        now = time.time()
        if now - self.tried > self.MIN_GAP and now - self.at > self.MIN_GAP and now >= self.pause_until:
            self.wake.set()


Media = plat.Media  # что сейчас играет: SMTC на Windows, osascript на macOS


def app_name(aumid):
    a = aumid.lower()
    for key, name in (("chrome", "Google Chrome"), ("msedge", "Microsoft Edge"), ("firefox", "Firefox"), ("opera", "Opera"),
                      ("yandex", "Яндекс Браузер"), ("spotify", "Spotify"), ("zunemusic", "Медиаплеер Windows"),
                      ("telegram", "Telegram"), ("vlc", "VLC")):
        if key in a:
            return name
    return aumid.split("!")[0] or "плеер"


def mmss(sec):
    sec = max(0, int(sec))
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}" if sec >= 3600 else f"{sec // 60}:{sec % 60:02d}"


def ago(ts):
    s = max(0, time.time() - ts)
    if s < 60:
        return "только что"
    if s < 3600:
        return f"{int(s // 60)} мин назад"
    if s < 48 * 3600:
        return f"{int(s // 3600)} ч назад"
    return f"{int(s // 86400)} дн назад"


# ── Изометрия ──
COS, SIN = 0.8660254, 0.5


def shade(hexc, k):
    r, g, b = (int(hexc[i:i + 2], 16) for i in (1, 3, 5))
    f = lambda v: max(0, min(255, int(v * k)))
    return f"#{f(r):02x}{f(g):02x}{f(b):02x}"


class Iso:
    def __init__(self, cv, ox, oy, S):
        self.cv, self.ox, self.oy, self.S = cv, ox, oy, S

    def P(self, x, y, z):
        return self.ox + (x - y) * COS * self.S, self.oy + (x + y) * SIN * self.S - z * self.S

    def poly(self, pts, fill, outline=""):
        self.cv.create_polygon([c for p in pts for c in self.P(*p)], fill=fill, outline=outline)

    def box(self, x, y, z, w, d, h, color):
        """Кубик: видны верх, грань +y (слева) и грань +x (справа)."""
        self.poly([(x, y + d, z), (x + w, y + d, z), (x + w, y + d, z + h), (x, y + d, z + h)], shade(color, 0.86))
        self.poly([(x + w, y, z), (x + w, y + d, z), (x + w, y + d, z + h), (x + w, y, z + h)], shade(color, 0.64))
        self.poly([(x, y, z + h), (x + w, y, z + h), (x + w, y + d, z + h), (x, y + d, z + h)], shade(color, 1.14))

    def face_y(self, x1, x2, z1, z2, y, fill):
        self.poly([(x1, y, z1), (x2, y, z1), (x2, y, z2), (x1, y, z2)], fill)

    def face_x(self, y1, y2, z1, z2, x, fill):
        self.poly([(x, y1, z1), (x, y2, z1), (x, y2, z2), (x, y1, z2)], fill)


class Panel:
    def __init__(self, test_tab=None):
        self.root = tk.Tk()
        self.root.withdraw()
        plat.prepare_window(self.root)  # окно без рамки, поверх всех
        self.root.configure(bg=C["line"])
        self.s = plat.ui_scale(self.root)
        self.W, self.H = int(600 * self.s), int(660 * self.s)
        self.x = (self.root.winfo_screenwidth() - self.W) // 2
        self.top, self.y = 0, -self.H - 4
        self.shown = self.pinned = self.by_hotkey = False
        self.dwell, self.frame = 0, 0
        self.last_inside = time.time()
        self.anim = None
        self.tab = test_tab or "sessions"
        self.history, self.paused = [], False
        self.clip_seq = plat.clipboard_seq(self.root)
        self.sessions, self.sess_sig, self.hits = [], None, []
        self.scroll_pos, self.ago_labels = {}, []
        self.office = self.limits_box = self.music_ui = None
        self.mirror, self.mirror_on, self.mirror_broken = None, False, VideoMirror is None
        self.mirror_dest = None
        self.q = queue.Queue()
        self.limits = Limits()
        self.recent = RecentSessions()
        self.media = Media()

        for role in ("base", "small", "tiny", "bold", "track", "title", "mono", "num", "big"):
            setattr(self, f"f_{role}", self.make_font(*plat.FONTS[role]))

        self.build()
        self.root.geometry(f"{self.W}x{self.H}+{self.x}+{self.y}")
        self.root.deiconify()
        if plat.WITHDRAW_WHEN_HIDDEN:  # macOS: окно может не уехать за край экрана — прячем целиком
            self.root.withdraw()
        self.root.bind("<Escape>", lambda e: self.hide())
        self.root.bind_all("<MouseWheel>", self.wheel)

        if CONFIG["hotkey"]:
            threading.Thread(target=plat.hotkey_thread, args=(self.q,), daemon=True).start()
        threading.Thread(target=self.limits.loop, daemon=True).start()
        threading.Thread(target=self.recent.loop, daemon=True).start()
        for ms, fn in ((80, self.tick_mouse), (400, self.tick_clipboard), (100, self.tick_queue),
                       (140, self.tick_office), (1500, self.tick_sessions), (500, self.tick_music)):
            self.root.after(ms, fn)

    def make_font(self, families, size, kw):
        """Первый установленный шрифт из списка (один вариант — берём как есть, Tk сам подберёт замену)."""
        fam = families[0]
        if len(families) > 1:
            if not hasattr(Panel, "_families"):
                try:
                    Panel._families = {f.lower() for f in tkfont.families(self.root)}
                except tk.TclError:
                    Panel._families = set()
            fam = next((f for f in families if f.lower() in Panel._families), families[-1])
        return tkfont.Font(family=fam, size=size, **kw)

    # ── Каркас ──
    def build(self):
        outer = tk.Frame(self.root, bg=C["bg"])
        outer.pack(fill="both", expand=True, padx=1, pady=(0, 1))
        head = tk.Frame(outer, bg=C["head"])
        head.pack(fill="x")
        title = tk.Label(head, text="▌моя панель", bg=C["head"], fg=C["accent"], font=self.f_title, padx=14, pady=11)
        title.pack(side="left")
        menu = tk.Menu(self.root, tearoff=0, bg=C["item"], fg=C["ink"], activebackground=C["hover"], activeforeground=C["ink"])
        menu.add_command(label="Закрыть программу", command=self.quit)
        for seq in plat.RIGHT_CLICK:
            title.bind(seq, lambda e: menu.tk_popup(e.x_root, e.y_root))
        self.tabbar = tk.Frame(head, bg=C["head"])
        self.tabbar.pack(side="left", padx=4)
        right = tk.Frame(head, bg=C["head"])
        right.pack(side="right", padx=10)
        self.pin_btn = self.button(right, "◇ закрепить", self.toggle_pin, small=True, fg=C["muted"])
        self.pin_btn.pack(side="left")
        self.tabs = {}
        for key, name in (("sessions", "Сессии"), ("music", "Музыка"), ("clip", "Буфер")):
            cell = tk.Frame(self.tabbar, bg=C["head"])
            cell.pack(side="left")
            lb = tk.Label(cell, text=name, bg=C["head"], fg=C["muted"], font=self.f_base, padx=10, pady=9, cursor="hand2")
            lb.pack()
            bar = tk.Frame(cell, bg=C["head"], height=2)
            bar.pack(fill="x", padx=8)
            lb.bind("<Button-1>", lambda e, k=key: self.set_tab(k))
            self.tabs[key] = (lb, bar)
        foot = tk.Frame(outer, bg=C["bg"])
        foot.pack(fill="x", side="bottom")
        self.status = tk.Label(foot, text=HINT, bg=C["bg"], fg=C["muted"], font=self.f_tiny, pady=5)
        self.status.pack()
        self.body = tk.Frame(outer, bg=C["bg"])
        self.body.pack(fill="both", expand=True)
        self.set_tab(self.tab)

    def button(self, parent, text, cmd, small=False, primary=False, fg=None, font=None):
        bg = C["accent"] if primary else C["item"]
        b = tk.Label(parent, text=text, bg=bg, fg=C["accent_ink"] if primary else (fg or C["ink"]),
                     font=font or (self.f_small if small else self.f_bold if primary else self.f_base),
                     padx=10, pady=4 if small else 6, cursor="hand2")
        b.bind("<Button-1>", lambda e: cmd())
        if not primary:
            b.bind("<Enter>", lambda e: b.configure(bg=C["hover"]))
            b.bind("<Leave>", lambda e: b.configure(bg=C["item"]))
        return b

    def scroll_area(self, parent, height=None, key=None, max_height=None):
        """Прокручиваемый список с тонкой полосой справа.

        key — позиция прокрутки запоминается и восстанавливается при перестройке вкладки;
        max_height — область по высоте содержимого, но не выше max_height (иначе растягивается).
        """
        wrap = tk.Frame(parent, bg=C["bg"])
        if max_height:
            wrap.pack(fill="x", padx=12, pady=(0, 4))
        else:
            wrap.pack(fill="both", expand=True, padx=12, pady=(0, 4))
        bw = max(4, int(5 * self.s))
        bar = tk.Canvas(wrap, width=bw, height=1, bg=C["bg"], highlightthickness=0, bd=0, cursor="hand2")
        bar.pack(side="right", fill="y", padx=(4, 0))
        cv = tk.Canvas(wrap, bg=C["bg"], highlightthickness=0, bd=0, height=height or 100)
        cv.pack(side="left", fill="both", expand=True)
        inner = tk.Frame(cv, bg=C["bg"])
        cv.scroll_inner = inner  # по этому признаку колесо мыши находит область под курсором
        win = cv.create_window(0, 0, window=inner, anchor="nw")
        state = {"restore": self.scroll_pos.get(key) if key else None}

        def on_scroll(lo, hi):
            lo, hi = float(lo), float(hi)
            bar.delete("all")
            if key and state["restore"] is None:
                self.scroll_pos[key] = lo
            if hi - lo < 0.999:
                h = bar.winfo_height()
                bar.create_rectangle(0, lo * h, bw, max(lo * h + 8 * self.s, hi * h), fill=C["line"], width=0)

        def on_inner(e):
            cv.configure(scrollregion=cv.bbox("all"))
            if max_height:
                cv.configure(height=max(1, min(inner.winfo_reqheight(), max_height)))

        def restore():
            """Вернуть прокрутку, когда вкладка достроена и уложена (bbox окончателен).

            Пока идёт восстановление, on_scroll не пишет в scroll_pos — иначе промежуточные значения
            при укладке строк затёрли бы сохранённую позицию.
            """
            if not cv.winfo_exists():
                return
            self.root.update_idletasks()
            on_inner(None)
            cv.yview_moveto(state["restore"])
            state["restore"] = None
            if key:
                self.scroll_pos[key] = cv.yview()[0]
        if state["restore"] is not None:
            self.root.after_idle(restore)  # строки добавляются уже после возврата из scroll_area

        def drag(e):
            lo, hi = cv.yview()
            cv.yview_moveto(e.y / max(1, bar.winfo_height()) - (hi - lo) / 2)
        cv.configure(yscrollcommand=on_scroll)
        inner.bind("<Configure>", on_inner)
        cv.bind("<Configure>", lambda e: cv.itemconfigure(win, width=e.width))
        bar.bind("<Button-1>", drag)
        bar.bind("<B1-Motion>", drag)
        return inner

    def wheel(self, e):
        """Колесо мыши крутит ту прокручиваемую область, над которой курсор."""
        try:
            w = self.root.winfo_containing(e.x_root, e.y_root)
        except (KeyError, tk.TclError):  # курсор над виджетом, который уже удалён
            return
        while w is not None and not hasattr(w, "scroll_inner"):
            w = w.master
        if w is not None and w.winfo_exists() and w.scroll_inner.winfo_height() > w.winfo_height():
            w.yview_scroll(plat.wheel_units(e.delta), "units")

    def row(self, parent, on_click=None):
        r = tk.Frame(parent, bg=C["item"], cursor="hand2" if on_click else "")
        r.pack(fill="x", pady=2)

        def paint(color):
            r.configure(bg=color)
            for w in r.winfo_children():
                w.configure(bg=color)
                for w2 in w.winfo_children():
                    w2.configure(bg=color)
        r.bind("<Enter>", lambda e: paint(C["hover"]))
        r.bind("<Leave>", lambda e: paint(C["item"]))
        if on_click:  # клавиатура: Tab — к строке, Enter/пробел — открыть
            r.configure(takefocus=1)
            r.bind("<FocusIn>", lambda e: paint(C["hover"]))
            r.bind("<FocusOut>", lambda e: paint(C["item"]))
            r.bind("<Return>", lambda e: on_click())
            r.bind("<space>", lambda e: on_click())
        return r

    def bind_tree(self, w, fn):
        if not w.winfo_exists():
            return
        w.bind("<Button-1>", lambda e: fn())
        for c in w.winfo_children():
            self.bind_tree(c, fn)

    def icon(self, parent, text, cmd, fg=None):
        lb = tk.Label(parent, text=text, bg=C["item"], fg=fg or C["muted"], font=self.f_base, padx=6, cursor="hand2")
        lb.bind("<Button-1>", lambda e: cmd())
        return lb

    def flash(self, text, color=None):
        self.status.configure(text=text, fg=color or C["mint"])
        self.root.after(2200, lambda: self.status.configure(text=HINT, fg=C["muted"]))

    def set_tab(self, key):
        self.tab = key
        for k, (lb, bar) in self.tabs.items():
            on = k == key
            lb.configure(fg=C["ink"] if on else C["muted"], font=self.f_bold if on else self.f_base)
            bar.configure(bg=C["accent"] if on else C["head"])
        self.video_off()
        for w in self.body.winfo_children():
            w.destroy()
        self.office = self.limits_box = self.music_ui = None
        getattr(self, f"tab_{key}")()

    def copy(self, text, note="Скопировано"):
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.root.update()
        self.clip_seq = plat.clipboard_seq(self.root)
        self.push_history(text)
        self.flash(note)

    def open_session(self, s):
        err = open_session(s.get("cwd"), s.get("id"))
        if err:
            self.flash(err, C["red"])
        else:
            self.flash(f"Открываю VS Code: {s.get('folder') or 'сессия'}")

    def quit(self):
        self.video_off()
        if self.mirror:
            try:
                self.mirror.close()
            except Exception:
                pass
            self.mirror = None
        self.root.destroy()

    # ── Вкладка: сессии ──
    @staticmethod
    def live_meta(s):
        where = "VS Code" if s["vscode"] else "терминал"
        state = "работает" if s["busy"] else "ждёт тебя"
        n = len(s["agents"])
        extra = f" · {n} {plural(n, 'агент', 'агента', 'агентов')}" if n else ""
        return f"{where} · {state} {ago(s['updated'])}{extra}"

    def session_row(self, lst, s):
        """Живая сессия: «проект · вкладка», ниже где/когда, у кого есть субагенты — их короткие имена."""
        r = self.row(lst, on_click=lambda: self.open_session(s))
        tk.Label(r, text="●", bg=C["item"], fg=C["mint"] if s["busy"] else C["accent"], font=self.f_base, padx=8).pack(side="left")
        tk.Label(r, text="открыть ↗", bg=C["item"], fg=C["muted"], font=self.f_tiny, padx=10).pack(side="right")
        txt = tk.Frame(r, bg=C["item"])
        txt.pack(side="left", fill="x", expand=True, pady=4)
        tk.Label(txt, text=s["name"], bg=C["item"], fg=C["ink"], font=self.f_bold, anchor="w").pack(fill="x")
        meta = tk.Label(txt, text=self.live_meta(s), bg=C["item"], fg=C["muted"], font=self.f_tiny, anchor="w")
        meta.pack(fill="x")
        self.ago_labels.append((meta, "live", s["id"]))
        if s["agents"]:
            names = ", ".join(a["type"] for a in s["agents"])
            tk.Label(txt, text=clip(f"агенты: {names}", 90), bg=C["item"], fg=C["violet"], font=self.f_tiny,
                     anchor="w").pack(fill="x")
        self.bind_tree(r, lambda: self.open_session(s))

    def recent_row(self, lst, s):
        """Недавняя сессия — компактно, в одну строку: ○ проект · вкладка … 7 дн назад · открыть."""
        r = self.row(lst, on_click=lambda: self.open_session(s))
        r.pack_configure(pady=1)
        tk.Label(r, text="○", bg=C["item"], fg=C["muted"], font=self.f_small, padx=8).pack(side="left")
        tk.Label(r, text="открыть ↗", bg=C["item"], fg=C["muted"], font=self.f_tiny, padx=10).pack(side="right")
        when = tk.Label(r, text=ago(s["mtime"]), bg=C["item"], fg=C["muted"], font=self.f_tiny, padx=4)
        when.pack(side="right")
        self.ago_labels.append((when, "recent", s["id"]))
        tk.Label(r, text=clip(s["name"], 62), bg=C["item"], fg=C["ink"], font=self.f_small, anchor="w",
                 pady=3).pack(side="left", fill="x", expand=True)
        self.bind_tree(r, lambda: self.open_session(s))

    def refresh_ago(self):
        """Обновить «N мин назад» без перестройки вкладки (прокрутка не сбрасывается)."""
        live = {s["id"]: s for s in self.sessions}
        recent = {r["id"]: r for r in self.recent.snapshot()[0]}
        for lb, kind, sid in self.ago_labels:
            if not lb.winfo_exists():
                continue
            if kind == "live" and sid in live:
                lb.configure(text=self.live_meta(live[sid]))
            elif kind == "recent" and sid in recent:
                lb.configure(text=ago(recent[sid]["mtime"]))

    def section(self, parent, text):
        tk.Label(parent, text=text, bg=C["bg"], fg=C["muted"], font=self.f_tiny, anchor="w").pack(fill="x", padx=12, pady=(2, 1))

    def tab_sessions(self):
        self.sessions = read_sessions()
        self.recent.refresh(s["id"] for s in self.sessions)
        self.sess_sig = self.signature()
        self.ago_labels = []
        self.office = tk.Canvas(self.body, height=int(180 * self.s), bg=C["wall"], highlightthickness=0, bd=0, cursor="hand2")
        self.office.pack(fill="x", padx=12, pady=(12, 8))
        self.office.bind("<Button-1>", self.office_click)
        stats = tk.Frame(self.body, bg=C["bg"])
        stats.pack(fill="x", padx=12)
        n, busy = len(self.sessions), sum(s["busy"] for s in self.sessions)
        agents = sum(len(s["agents"]) for s in self.sessions)
        for num, label, col in ((n, "сессий открыто", C["ink"]), (busy, "работают сейчас", C["mint"]), (agents, "агентов в деле", C["violet"])):
            box = tk.Frame(stats, bg=C["item"])
            box.pack(side="left", fill="x", expand=True, padx=(0, 6))
            tk.Label(box, text=str(num), bg=C["item"], fg=col, font=self.f_num, padx=10).pack(side="left", pady=6)
            tk.Label(box, text=label, bg=C["item"], fg=C["muted"], font=self.f_small).pack(side="left")
        self.limits_box = tk.Frame(self.body, bg=C["item"])
        self.limits_box.pack(fill="x", padx=12, pady=(2, 6), side="bottom")
        # живые — по высоте содержимого, но не больше ~2 строк, чтобы «Недавние» были видны
        live = self.scroll_area(self.body, height=int(40 * self.s), key="live", max_height=int(118 * self.s))
        live.master.master.pack_configure(pady=(8, 2))
        if not self.sessions:
            tk.Label(live, text="Сессий Claude Code сейчас нет. Открой Claude в VS Code — сотрудник сядет за стол.",
                     bg=C["bg"], fg=C["muted"], font=self.f_small, pady=8).pack()
        for s in self.sessions:
            self.session_row(live, s)
        recent, loaded, err = self.recent.snapshot()
        self.section(self.body, f"НЕДАВНИЕ · за {RECENT_DAYS} дней" + (f" · {len(recent)}" if recent else ""))
        lst = self.scroll_area(self.body, height=int(60 * self.s), key="recent")
        if err:
            tk.Label(lst, text=f"Недавние сессии: {err}", bg=C["bg"], fg=C["red"], font=self.f_tiny, anchor="w").pack(fill="x")
        elif not loaded:
            tk.Label(lst, text="Ищу прошлые сессии…", bg=C["bg"], fg=C["muted"], font=self.f_small, anchor="w").pack(fill="x")
        elif not recent:
            tk.Label(lst, text=f"Прошлых сессий за {RECENT_DAYS} дней нет.", bg=C["bg"], fg=C["muted"], font=self.f_small,
                     anchor="w").pack(fill="x")
        for s in recent:
            self.recent_row(lst, s)
        self.render_limits()
        self.limits.refresh_if_stale()

    def signature(self):
        """Что требует перестройки вкладки. Время («N мин назад») сюда не входит — его обновляет refresh_ago()."""
        recent, loaded, err = self.recent.snapshot()
        live = tuple((s["id"], s["name"], s["busy"], tuple(a["type"] for a in s["agents"])) for s in self.sessions)
        return live, loaded, err, tuple((r["id"], r["name"]) for r in recent)

    def render_limits(self):
        box = self.limits_box
        if not box or not box.winfo_exists():
            return
        for w in box.winfo_children():
            w.destroy()
        with self.limits.lock:
            d, err, plan = self.limits.data, self.limits.error, self.limits.plan
        head = tk.Frame(box, bg=C["item"])
        head.pack(fill="x", padx=10, pady=(7, 4))
        tk.Label(head, text="ЛИМИТЫ CLAUDE" + (f" · {plan}" if plan else ""), bg=C["item"], fg=C["muted"], font=self.f_tiny).pack(side="left")
        if err:
            tk.Label(head, text=err, bg=C["item"], fg=C["red"], font=self.f_tiny).pack(side="right")
        if not d:
            tk.Label(box, text="Загружаю лимиты…" if not err else "Лимиты недоступны", bg=C["item"], fg=C["muted"],
                     font=self.f_small, anchor="w").pack(fill="x", padx=10, pady=(0, 8))
            return
        rows = [("5 часов", d.get("five_hour")), ("Неделя", d.get("seven_day"))]
        for extra, name in (("seven_day_opus", "Неделя Opus"), ("seven_day_sonnet", "Неделя Sonnet")):
            if d.get(extra):
                rows.append((name, d[extra]))
        for label, lim in rows:
            if not lim:
                continue
            used = float(lim.get("utilization") or 0)
            left = max(0, 100 - used)
            line = tk.Frame(box, bg=C["item"])
            line.pack(fill="x", padx=10, pady=2)
            tk.Label(line, text=label, bg=C["item"], fg=C["ink"], font=self.f_bold, width=11, anchor="w").pack(side="left")
            col = C["red"] if left < 15 else C["accent"] if left < 40 else C["mint"]
            tk.Label(line, text=f"осталось {left:.0f}%", bg=C["item"], fg=col, font=self.f_mono, width=14, anchor="w").pack(side="left")
            bw, bh = int(170 * self.s), int(8 * self.s)
            bar = tk.Canvas(line, width=bw, height=bh, bg=C["item"], highlightthickness=0)
            bar.pack(side="left", padx=6)
            bar.create_rectangle(0, 0, bw, bh, fill=C["field"], width=0)
            bar.create_rectangle(0, 0, int(bw * min(1, used / 100)), bh, fill=col, width=0)
            reset = ""
            if lim.get("resets_at"):
                t = datetime.fromisoformat(lim["resets_at"]).astimezone()
                days = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
                reset = ("сброс в " + t.strftime("%H:%M")) if t.date() == datetime.now().date() else f"сброс {days[t.weekday()]} {t.strftime('%H:%M')}"
            tk.Label(line, text=reset, bg=C["item"], fg=C["muted"], font=self.f_tiny).pack(side="right")
        tk.Label(box, text="", bg=C["item"], font=self.f_tiny).pack(pady=1)

    # ── 3D-офис ──
    def office_click(self, e):
        for x0, x1, s in self.hits:
            if x0 <= e.x <= x1:
                self.open_session(s)
                return

    def draw_office(self):
        cv = self.office
        if not cv or not cv.winfo_exists():
            return
        cv.delete("all")
        w, h, s = cv.winfo_width(), cv.winfo_height(), self.s
        if w < 50:
            return
        for i in range(18):
            sx, sy = (i * 97 + 31) % w, (i * 53 + 11) % int(h * 0.45)
            if (self.frame // 4 + i) % 5:
                cv.create_rectangle(sx, sy, sx + 2 * s, sy + 2 * s, fill="#2b2f66", width=0)
        cv.create_text(int(14 * s), int(14 * s), text=time.strftime("%H:%M"), fill=C["muted"], font=self.f_mono, anchor="nw")
        self.hits = []
        if not self.sessions:
            cv.create_text(w / 2, h * 0.5, text="Офис пуст: сессий Claude Code не запущено", fill=C["muted"], font=self.f_small)
            return
        groups, hidden, S = self.office_layout(w, h)
        one_project = len({x["folder"] for x in self.sessions}) == 1  # тогда в подписи группы хватит названия вкладки
        unit = 16 * COS  # ширина плитки станции в единицах масштаба
        gap, more_w = 12 * s, 20 * s
        total = sum(self.group_width(len(g["sess"]["agents"]), g["shown"], S) for g in groups)
        total += gap * (len(groups) - 1) + (more_w if hidden else 0)
        x = (w - total) / 2
        oy = 3.9 * S + 6 * s + max(0, (h - 11.9 * S - 44 * s) / 2)  # мелкий масштаб — центрируем по высоте
        floor = oy + 8 * S  # нижний угол плиток — общая линия пола для всех станций
        for gi, g in enumerate(groups):
            sess, shown = g["sess"], g["shown"]
            gw = self.group_width(len(sess["agents"]), shown, S)
            gx0 = x
            # главный агент (с кружкой)
            step = unit * S
            self.draw_station(Iso(cv, x + step / 2, oy, S), CLAUDE_ORANGE, sess["busy"], gi * 13, True)
            cv.create_text(x + step / 2, floor + 9 * s, text=MAIN_AGENT, fill=CLAUDE_ORANGE, font=self.f_tiny)
            x += step
            # субагенты — меньше и своим цветом
            S2 = S * SUB_SCALE
            step2 = unit * S2
            n_ch = max(4, int(step2 / (5.6 * s)))
            for j, a in enumerate(sess["agents"][:shown]):
                color = AGENT_COLORS[j % len(AGENT_COLORS)]
                self.draw_station(Iso(cv, x + step2 / 2, floor - 8 * S2, S2), color, a["busy"], gi * 13 + j * 5 + 3, False)
                cv.create_text(x + step2 / 2, floor + 9 * s, text=clip(a["type"], n_ch), fill=color, font=self.f_tiny)
                x += step2
            extra = len(sess["agents"]) - shown
            if extra > 0:
                cv.create_text(x + 2 * s, floor - 3 * S2, text=f"+{extra}", fill=C["muted"], font=self.f_bold, anchor="w")
            # подпись сессии под всей группой
            cx = gx0 + gw / 2
            if sess["agents"]:
                cv.create_line(gx0 + 4 * s, floor + 18 * s, gx0 + gw - 4 * s, floor + 18 * s, fill=C["line"], width=max(1, int(2 * s)))
            label = sess.get("title") if one_project and sess.get("title") else sess["name"]
            n_ch = max(6, min(GROUP_LABEL_MAX, int(max(gw, 90 * s) / (7 * s))))
            cv.create_text(cx, floor + 29 * s, text=clip(label, n_ch), fill=C["ink"], font=self.f_small)
            self.hits.append((gx0, gx0 + gw, sess))
            x = gx0 + gw + gap
        if hidden:
            cv.create_text(w - 10 * s, floor - 3 * S, text=f"+{hidden}", fill=C["muted"], font=self.f_bold, anchor="e")

    def group_width(self, n_agents, shown, S):
        """Ширина группы: главный + показанные субагенты (+ место под «+N», если кого-то не влезло)."""
        w = 16 * COS * S * (1 + SUB_SCALE * shown)
        return w + (22 * self.s if n_agents > shown else 0)

    def office_layout(self, w, h):
        """Подобрать масштаб: если агентов много — уменьшаем, ниже минимума — прячем лишних в «+N»."""
        s = self.s
        groups = [{"sess": x, "shown": min(len(x["agents"]), SUB_MAX)} for x in self.sessions]
        hidden = 0

        def fit():
            fixed = 12 * s * (len(groups) - 1) + (20 * s if hidden else 0) + 22 * s * sum(
                len(g["sess"]["agents"]) > g["shown"] for g in groups)
            units = sum(1 + SUB_SCALE * g["shown"] for g in groups)
            return min(13.5 * s, (w - 24 * s - fixed) / (units * 16 * COS), (h - 44 * s) / 11.9)
        S = fit()
        while S < 6.5 * s:
            big = max(groups, key=lambda g: g["shown"])
            if big["shown"] > 2:
                big["shown"] -= 1
            elif len(groups) > 1:
                groups.pop()
                hidden += 1
            else:
                break
            S = fit()
        return groups, hidden, max(S, 3 * s)

    def draw_station(self, iso, color, busy, seed, main):
        f = self.frame
        tile = C["tile_a"] if seed % 2 == 0 else C["tile_b"]
        iso.poly([(0, 0, 0), (8, 0, 0), (8, 8, 0), (0, 8, 0)], tile, outline=C["line"])
        for lx, ly in ((1.2, 1.0), (6.4, 1.0), (1.2, 3.0), (6.4, 3.0)):
            iso.box(lx, ly, 0, 0.4, 0.4, 3, C["wood_dark"])
        iso.box(1, 0.8, 3, 6, 2.8, 0.4, C["wood"])
        iso.box(3.8, 1.2, 3.4, 0.4, 0.4, 0.6, C["metal"])
        iso.box(2.2, 1.0, 4.0, 3.6, 0.5, 2.3, C["metal"])
        scr_y = 1.5
        iso.face_y(2.45, 5.55, 4.2, 6.05, scr_y + 0.01, C["screen_on"] if busy else C["screen_off"])
        if busy:
            for i in range(4):
                ln = 0.6 + ((seed * 7 + f + i * 3) % 6) * 0.4
                z = 5.7 - i * 0.38
                iso.face_y(2.65, min(5.35, 2.65 + ln), z, z + 0.14, scr_y + 0.02, C["mint"] if (i + seed) % 3 else C["violet"])
            cx = 2.65 + ((f // 2 + seed) % 6) * 0.45
            if f % 2:
                iso.face_y(cx, cx + 0.25, 4.45, 4.62, scr_y + 0.02, C["accent"])
        iso.box(2.6, 2.4, 3.4, 2.6, 0.7, 0.12, C["keys"])
        if main:
            iso.box(5.9, 2.6, 3.4, 0.45, 0.45, 0.55, "#d9dbf0")
            if busy and f % 6 < 4:
                iso.box(6.05, 2.75, 4.1 + (f % 3) * 0.25, 0.15, 0.15, 0.15, "#6f74a8")
        iso.box(3.8, 5.3, 0, 0.4, 0.4, 1.8, C["metal"])
        iso.box(2.9, 4.4, 1.8, 2.2, 2.2, 0.35, C["metal"])
        tap = (f + seed) % 2
        bob = 0.12 if busy and (f + seed) % 4 < 2 else 0
        if busy:
            iso.box(3.1, 3.0, 3.55 + (0.15 if tap else 0), 0.4, 1.8, 0.32, shade(color, 0.9))
        iso.box(3.2, 4.7, 2.15, 1.6, 1.4, 1.9, color)
        if busy:
            iso.box(4.5, 3.0, 3.55 + (0 if tap else 0.15), 0.4, 1.8, 0.32, shade(color, 0.95))
        else:
            iso.box(4.5, 4.0, 3.2, 0.4, 0.9, 0.32, shade(color, 0.95))
        hz = 4.05 + bob
        iso.box(3.05, 4.6, hz, 1.9, 1.6, 1.6, shade(color, 1.06))
        if busy:
            iso.face_x(4.75, 5.05, hz + 0.8, hz + 1.15, 4.96, "#0e0f1a")
        else:
            iso.face_x(4.7, 5.1, hz + 0.9, hz + 0.98, 4.96, "#0e0f1a")
        iso.box(3.9, 5.3, hz + 1.6, 0.2, 0.2, 0.55, color)
        glow = C["mint"] if busy and (f + seed) % 4 < 2 else C["muted"]
        iso.box(3.8, 5.2, hz + 2.15, 0.4, 0.4, 0.4, glow)
        iso.box(2.9, 6.4, 1.8, 2.2, 0.3, 1.5, shade(C["metal"], 1.1))
        if not busy:
            zy = (f % 10) * 0.12
            x, y = iso.P(4.6, 5.0, hz + 2.6 + zy)
            iso.cv.create_text(x, y, text="z", fill=C["muted"], font=self.f_tiny)
            if f % 10 > 4:
                x, y = iso.P(5.0, 4.8, hz + 3.2 + zy)
                iso.cv.create_text(x, y, text="Z", fill=C["muted"], font=self.f_small)

    # ── Вкладка: музыка ──
    def tab_music(self):
        ui = {}
        # Живое видео: тёмная заглушка 16:9, поверх неё DWM рисует окно браузера (показывается по необходимости)
        ui["vbox"] = tk.Frame(self.body, bg=C["bg"])
        ui["video"] = tk.Frame(ui["vbox"], bg="#000000", width=int(320 * self.s), height=int(180 * self.s), cursor="hand2")
        ui["video"].pack()
        ui["vcap"] = tk.Label(ui["vbox"], text="", bg=C["bg"], fg=C["muted"], font=self.f_tiny, cursor="hand2")
        ui["vcap"].pack(pady=(3, 0))
        for wdg in (ui["video"], ui["vcap"]):
            wdg.bind("<Button-1>", lambda e: self.focus_video_source())
        card = tk.Frame(self.body, bg=C["item"])
        card.pack(fill="x", padx=12, pady=(14, 8))
        ui["card"] = card
        self.blank = ImageTk.PhotoImage(Image.new("RGB", (int(200 * self.s), int(112 * self.s)), C["field"]))
        ui["img"] = tk.Label(card, image=self.blank, bg=C["field"], bd=0)
        ui["img"].pack(side="left", padx=12, pady=12)
        info = tk.Frame(card, bg=C["item"])
        info.pack(side="left", fill="both", expand=True, pady=12, padx=(0, 12))
        ui["info"] = info
        ui["app"] = tk.Label(info, text="", bg=C["item"], fg=C["muted"], font=self.f_tiny, anchor="w")
        ui["app"].pack(fill="x")
        ui["title"] = tk.Label(info, text="", bg=C["item"], fg=C["ink"], font=self.f_track, anchor="w", justify="left",
                               wraplength=int(300 * self.s))
        ui["title"].pack(fill="x", pady=(2, 2))
        ui["artist"] = tk.Label(info, text="", bg=C["item"], fg=C["accent"], font=self.f_base, anchor="w", justify="left",
                                wraplength=int(300 * self.s))
        ui["artist"].pack(fill="x")
        prog = tk.Frame(self.body, bg=C["bg"])
        prog.pack(fill="x", padx=12, pady=(4, 0))
        ui["t0"] = tk.Label(prog, text="0:00", bg=C["bg"], fg=C["muted"], font=self.f_mono)
        ui["t0"].pack(side="left")
        ui["t1"] = tk.Label(prog, text="0:00", bg=C["bg"], fg=C["muted"], font=self.f_mono)
        ui["t1"].pack(side="right")
        ui["bar"] = tk.Canvas(prog, height=int(6 * self.s), bg=C["bg"], highlightthickness=0)
        ui["bar"].pack(side="left", fill="x", expand=True, padx=8)
        row1 = tk.Frame(self.body, bg=C["bg"])
        row1.pack(pady=(16, 8))
        for sym, key in (("⏮", "prev"), ("⏯", "play"), ("⏭", "next")):
            b = self.button(row1, sym, lambda k=key: self.media.control(k), font=self.f_big)
            b.configure(padx=24, pady=8, fg=C["accent"] if key == "play" else C["ink"])
            b.pack(side="left", padx=6)
            ui[key] = b
        row2 = tk.Frame(self.body, bg=C["bg"])
        row2.pack(pady=4)
        for label, key in (("Тише", "vdown"), ("Без звука", "mute"), ("Громче", "vup")):
            self.button(row2, label, lambda k=key: media_key(k), small=True).pack(side="left", padx=6)
        ui["thumb_key"], ui["photo"] = None, None
        ui["video_mode"], ui["src"], ui["src_key"], ui["src_at"], ui["bad"] = False, None, None, 0, None
        self.music_ui = ui
        self.update_music()

    # ── Живое видео (video_mirror) ──
    def panel_hwnd(self):
        """HWND окна панели (GA_ROOT). До первого показа окна он ещё не настоящий — тогда None."""
        return plat.panel_hwnd(self.root)

    def get_mirror(self):
        """VideoMirror, привязанный к текущему окну панели; если HWND сменился — зеркало пересоздаётся."""
        if self.mirror_broken:
            return None
        dest = self.panel_hwnd()
        if not dest:
            return None
        if self.mirror is not None and self.mirror_dest != dest:
            try:
                self.mirror.close()
            except Exception:
                pass
            self.mirror, self.mirror_on = None, False
        if self.mirror is None:
            try:
                self.mirror, self.mirror_dest = VideoMirror(dest), dest
            except Exception:
                self.mirror, self.mirror_broken = None, True
        return self.mirror

    def video_off(self):
        """Убрать thumbnail безусловно: после неудачного show() он мог остаться зарегистрированным."""
        if self.mirror:
            try:
                self.mirror.hide()
            except Exception:
                pass
        self.mirror_on = False

    def video_source(self, ui, m, browser):
        """HWND окна браузера с этим видео или None.

        Кеш на трек, повторный поиск не чаще раза в 5 с; свёрнутое окно не годится; пара (окно, трек),
        на которой show() уже не сработал, 30 с не пробуется — чтобы не мигать видео↔обложка.
        """
        if not browser or not self.get_mirror():
            return None
        key = (m["app"], m["title"])
        now = time.time()
        if key != ui["src_key"] or (not ui["src"] and now - ui["src_at"] > 5):
            try:
                ui["src"] = VideoMirror.find_source(m["app"], m["title"])
            except Exception:
                ui["src"] = None
            ui["src_key"], ui["src_at"] = key, now
        src = ui["src"]
        if src and not plat.window_alive(src):
            ui["src"] = src = None
        if not src or plat.window_minimized(src):
            return None
        bad = ui["bad"]
        if bad and bad[0] == src and bad[1] == key and now - bad[2] < 30:
            return None
        return src

    def set_video_mode(self, ui, on, app=""):
        if on:
            ui["vcap"].configure(text=f"живое видео из {app} · окно не должно быть свёрнуто")
        if ui["video_mode"] == on:
            return
        ui["video_mode"] = on
        if on:
            ui["img"].pack_forget()
            ui["vbox"].pack(fill="x", padx=12, pady=(12, 0), before=ui["card"])
            ui["card"].pack_configure(pady=(8, 6))
            ui["info"].pack_configure(padx=12)
            wrap = int(540 * self.s)
        else:
            self.video_off()
            ui["vbox"].pack_forget()
            ui["img"].pack(side="left", padx=12, pady=12, before=ui["info"])
            ui["card"].pack_configure(pady=(14, 8))
            ui["info"].pack_configure(padx=(0, 12))
            wrap = int(300 * self.s)
        ui["title"].configure(wraplength=wrap)
        ui["artist"].configure(wraplength=wrap)

    def show_video(self, ui, src):
        """Передать в VideoMirror прямоугольник заглушки (относительно угла окна панели)."""
        fr = ui["video"]
        if fr.winfo_width() <= 1 or not fr.winfo_ismapped():
            self.root.update_idletasks()
        l = fr.winfo_rootx() - self.root.winfo_rootx()
        t = fr.winfo_rooty() - self.root.winfo_rooty()
        rect = (l, t, l + fr.winfo_width(), t + fr.winfo_height())
        try:
            self.mirror_on = bool(self.mirror and self.mirror.show(src, rect))
        except Exception:
            self.mirror_on = False
        if not self.mirror_on:
            self.video_off()
        return self.mirror_on

    def focus_video_source(self):
        ui = self.music_ui
        h = ui and ui.get("src")
        if h:
            plat.focus_window(h)

    def update_music(self):
        ui = self.music_ui
        if not ui or not ui["title"].winfo_exists():
            return
        with self.media.lock:
            m = dict(self.media.info) if self.media.info else None
        if not m or not (m["title"] or m["artist"]):
            self.set_video_mode(ui, False)
            ui["app"].configure(text="")
            ui["title"].configure(text="Сейчас ничего не играет")
            ui["artist"].configure(text="Включи музыку или видео — здесь появятся название и обложка")
            if ui["thumb_key"] is not None:
                ui["img"].configure(image=self.blank)
                ui["thumb_key"], ui["photo"] = None, None
            ui["bar"].delete("all")
            ui["t0"].configure(text="0:00")
            ui["t1"].configure(text="0:00")
            return
        app = app_name(m["app"])
        browser = app in ("Google Chrome", "Microsoft Edge", "Firefox", "Opera", "Яндекс Браузер", "Safari")
        ui["app"].configure(text=("▶ играет" if m["playing"] else "❚❚ пауза") + f" · {app}" + (" · видео в браузере" if browser else ""))
        ui["title"].configure(text=m["title"][:140])
        ui["artist"].configure(text=m["artist"] + (f" · {m['album']}" if m["album"] else ""))
        ui["play"].configure(text="⏸" if m["playing"] else "▶")
        src = self.video_source(ui, m, browser) if self.shown else None
        if src:
            self.set_video_mode(ui, True, app)
            if not self.show_video(ui, src):
                ui["bad"] = (src, ui["src_key"], time.time())  # не вышло — 30 с эту пару не пробуем
                src = None
        if not src:
            self.set_video_mode(ui, False)
        key = (m["title"], m["artist"], len(m["thumb"] or b""))
        if key != ui["thumb_key"]:
            ui["thumb_key"] = key
            bw, bh = int(200 * self.s), int(112 * self.s)
            try:
                canvas = Image.new("RGB", (bw, bh), C["field"])
                if m["thumb"]:
                    im = Image.open(io.BytesIO(m["thumb"])).convert("RGB")
                    k = min(bw / im.width, bh / im.height)
                    im = im.resize((max(1, int(im.width * k)), max(1, int(im.height * k))), Image.LANCZOS)
                    canvas.paste(im, ((bw - im.width) // 2, (bh - im.height) // 2))
                ui["photo"] = ImageTk.PhotoImage(canvas)
                ui["img"].configure(image=ui["photo"])
            except Exception:
                ui["img"].configure(image=self.blank)
        end = m["end"]
        pos = m["pos"] + (time.time() - m["upd"] if m["playing"] else 0)
        pos = min(pos, end) if end else pos
        cv = ui["bar"]
        cv.delete("all")
        w, h = cv.winfo_width(), cv.winfo_height()
        cv.create_rectangle(0, 0, w, h, fill=C["field"], width=0)
        if end > 0:
            cv.create_rectangle(0, 0, int(w * pos / end), h, fill=C["accent"], width=0)
        ui["t0"].configure(text=mmss(pos))
        ui["t1"].configure(text=mmss(end) if end else "—")

    # ── Вкладка: буфер ──
    def tab_clip(self):
        bar = tk.Frame(self.body, bg=C["bg"])
        bar.pack(fill="x", padx=12, pady=10)
        self.button(bar, "Возобновить запись" if self.paused else "Пауза", self.clip_pause, small=True).pack(side="left")
        self.button(bar, "Очистить историю", self.clip_clear, small=True, fg=C["muted"]).pack(side="left", padx=6)
        tk.Label(bar, text="Только в памяти · пароли из менеджеров паролей не пишутся", bg=C["bg"], fg=C["muted"], font=self.f_tiny).pack(side="right")
        lst = self.scroll_area(self.body)
        if self.paused:
            tk.Label(lst, text="Запись буфера на паузе.", bg=C["bg"], fg=C["accent"], font=self.f_base, pady=6).pack()
        if not self.history:
            tk.Label(lst, text=f"История пуста. Скопируй что-нибудь ({plat.COPY_LABEL}).", bg=C["bg"], fg=C["muted"], font=self.f_base, pady=16).pack()
        for i, (text, ts) in enumerate(self.history):
            r = self.row(lst)
            one = " ".join(text.split())
            lb = tk.Label(r, text=one[:78] + ("…" if len(one) > 78 else ""), bg=C["item"], fg=C["ink"], font=self.f_base,
                          anchor="w", padx=10, pady=7, cursor="hand2")
            lb.pack(side="left", fill="x", expand=True)
            lb.bind("<Button-1>", lambda e, t=text: (self.copy(t), self.set_tab("clip")))
            tk.Label(r, text=time.strftime("%H:%M", time.localtime(ts)), bg=C["item"], fg=C["muted"], font=self.f_mono).pack(side="right", padx=6)
            self.icon(r, "✕", lambda i=i: self.clip_del(i), fg=C["red"]).pack(side="right")

    def push_history(self, text):
        self.history = [h for h in self.history if h[0] != text]
        self.history.insert(0, (text, time.time()))
        del self.history[HISTORY_MAX:]

    def clip_pause(self):
        self.paused = not self.paused
        self.set_tab("clip")

    def clip_clear(self):
        self.history.clear()
        self.set_tab("clip")

    def clip_del(self, i):
        self.history.pop(i)
        self.set_tab("clip")

    # ── Показ и скрытие ──
    def slide(self, target, withdraw=False):
        """Плавно сдвинуть окно к y=target; withdraw — по приезде спрятать окно (см. plat.WITHDRAW_WHEN_HIDDEN)."""
        if self.anim:
            self.root.after_cancel(self.anim)
        step = max(8, int(self.H / 6))

        def go():
            if abs(self.y - target) <= step:
                self.y = target
            else:
                self.y += step if target > self.y else -step
            self.root.geometry(f"+{self.x}+{self.y}")
            self.anim = self.root.after(12, go) if self.y != target else None
            if self.anim is None and withdraw and not self.shown:
                try:
                    self.root.withdraw()
                except tk.TclError:
                    pass
        go()

    def place_on_cursor_monitor(self):
        x, y = plat.cursor_pos(self.root)
        l, t, r, b = plat.monitor_rect(x, y, self.root)
        self.x = l + (r - l - self.W) // 2
        self.top = t
        self.y = t - self.H - 4
        self.root.geometry(f"+{self.x}+{self.y}")

    def show(self, by_hotkey=False):
        if self.shown:
            return
        if self.anim:
            self.root.after_cancel(self.anim)
            self.anim = None
        self.place_on_cursor_monitor()
        self.shown, self.by_hotkey = True, by_hotkey
        self.last_inside = time.time()
        if plat.WITHDRAW_WHEN_HIDDEN:
            self.root.deiconify()
        try:
            self.root.attributes("-topmost", True)
        except tk.TclError:
            pass
        self.slide(self.top)
        self.root.after(60, self.root.focus_force)
        if self.tab in ("clip", "sessions"):
            self.set_tab(self.tab)

    def hide(self):
        if not self.shown:
            return
        self.shown = False
        self.video_off()
        if self.music_ui:  # при следующем показе видео заново привяжется к заглушке
            self.set_video_mode(self.music_ui, False)
        self.slide(self.top - self.H - 4, withdraw=plat.WITHDRAW_WHEN_HIDDEN)

    def toggle_pin(self):
        self.pinned = not self.pinned
        self.pin_btn.configure(text="◆ закреплено" if self.pinned else "◇ закрепить", fg=C["accent"] if self.pinned else C["muted"])

    # ── Таймеры ──
    def tick_mouse(self):
        x, y = plat.cursor_pos(self.root)
        if not self.shown:
            l, t, r, b = plat.monitor_rect(x, y, self.root)
            if y <= t + 1 and l + (r - l) // 4 <= x <= r - (r - l) // 4:
                self.dwell += 1
                if self.dwell >= 3:
                    self.show()
            else:
                self.dwell = 0
        elif not self.pinned:
            inside = self.x - 12 <= x <= self.x + self.W + 12 and self.top - 2 <= y <= self.top + self.H + 12
            if inside:
                self.last_inside = time.time()
                self.by_hotkey = False
            elif not self.by_hotkey and time.time() - self.last_inside > 0.7:
                self.hide()
        self.root.after(80, self.tick_mouse)

    def tick_office(self):
        if self.shown and self.tab == "sessions" and self.office:
            self.frame += 1
            self.draw_office()
        self.root.after(160, self.tick_office)

    def tick_sessions(self):
        if self.shown and self.tab == "sessions":
            self.sessions = read_sessions()
            self.recent.refresh(s["id"] for s in self.sessions)
            if self.signature() != self.sess_sig:
                self.set_tab("sessions")
            else:
                self.refresh_ago()
                if self.frame % 15 == 0:
                    self.render_limits()
        self.root.after(2000, self.tick_sessions)

    def tick_music(self):
        if self.shown and self.tab == "music":
            self.update_music()
        self.root.after(500, self.tick_music)

    def tick_clipboard(self):
        seq = plat.clipboard_seq(self.root)
        if seq != self.clip_seq:
            self.clip_seq = seq
            title = foreground_title()
            if (not self.paused and not any(p.lower() in title.lower() for p in PASSWORD_APPS)
                    and not plat.clipboard_excluded(self.root)):
                try:
                    text = self.root.clipboard_get()
                except tk.TclError:
                    text = ""
                if text.strip():
                    self.push_history(text)
                    if self.shown and self.tab == "clip":
                        self.set_tab("clip")
        self.root.after(400, self.tick_clipboard)

    def tick_queue(self):
        try:
            while True:
                ev = self.q.get_nowait()
                if ev == "toggle":
                    self.hide() if self.shown else self.show(by_hotkey=True)
                elif ev == "hotkey-failed":
                    self.flash(f"{plat.HOTKEY_LABEL} занят другой программой", C["accent"])
                elif ev == "hotkey-unavailable":
                    self.flash(f"{plat.HOTKEY_LABEL} недоступна — открывай через верхний край экрана", C["accent"])
        except queue.Empty:
            pass
        self.root.after(100, self.tick_queue)


def fake_limits_fetch(self):
    """Подмена Limits.fetch для --test: фиксированные данные вместо запроса к api.anthropic.com."""
    self.tried = time.time()
    soon = datetime.fromtimestamp(time.time() + 3 * 3600).astimezone().isoformat()
    later = datetime.fromtimestamp(time.time() + 4 * 86400).astimezone().isoformat()
    with self.lock:
        self.data = {"five_hour": {"utilization": 11, "resets_at": soon}, "seven_day": {"utilization": 2, "resets_at": later}}
        self.error, self.plan, self.at = None, "Pro (тест)", time.time()


def self_test(tab, out):
    """Для проверки: показать панель на вкладке и сохранить скриншот."""
    global read_sessions, Media
    Limits.fetch = fake_limits_fetch  # в тестах API лимитов не дёргаем
    if "--demo" in sys.argv:
        now = time.time()
        types = [("frontend-dev", True), ("backend-dev", True), ("controller", False), ("qa-tester", True), ("code-reviewer", True)]
        demo = [
            {"id": "11111111-1111-4111-8111-111111111111", "name": "my-app · Рефакторинг", "folder": "my-app",
             "title": "Рефакторинг", "cwd": "", "busy": True, "updated": now - 20, "vscode": True,
             "agents": [{"desc": t, "type": agent_label({"agentType": t}), "busy": b} for t, b in types]},
            {"id": "22222222-2222-4222-8222-222222222222", "name": "notes · Заметки", "folder": "notes",
             "title": "Заметки", "cwd": "", "busy": False, "updated": now - 900, "vscode": True, "agents": []},
        ]
        demo_recent = [
            {"id": f"3333333{i}-3333-4333-8333-333333333333", "cwd": "", "folder": f, "title": t, "name": f"{f} · {t}",
             "mtime": now - dt}
            for i, (f, t, dt) in enumerate((("website", "Новая страница «О проекте»", 7200),
                                             ("my-app", "Тесты для модуля импорта", 86400 * 2),
                                             ("notes", "Черновик заметок", 86400 * 9)))]
        read_sessions = lambda: demo
        RecentSessions.collect = lambda self, exclude: demo_recent

        class FakeMedia:  # «играет» видео в браузере; DWM-зеркало подменено — проверяем только вёрстку
            def __init__(self):
                self.lock = threading.Lock()
                self.info = {"app": "Chrome", "title": "Lo-fi beats to relax/study to", "artist": "Lofi Girl", "album": "",
                             "playing": True, "pos": 754, "end": 3600, "upd": time.time(), "thumb": None}

            def control(self, action):
                pass
        Media = FakeMedia
        Panel.video_source = lambda self, ui, m, browser: 1
        Panel.show_video = lambda self, ui, src: True
    p = Panel(test_tab=tab)
    if tab == "clip":
        for s in ("ls -la", "git status", "Привет! Посмотри, пожалуйста, черновик."):
            p.push_history(s)
    p.pinned = True
    p.set_tab(tab)
    p.show()

    def shot():
        from PIL import ImageGrab
        if tab == "sessions":
            p.render_limits()
        p.root.update()
        x, y = p.root.winfo_rootx(), p.root.winfo_rooty()
        ImageGrab.grab(bbox=(x, y, x + p.W, y + p.H), all_screens=True).save(out)
        p.quit()
    p.root.after(6000, shot)
    p.root.mainloop()


if __name__ == "__main__":
    if "--test" in sys.argv:
        self_test(sys.argv[sys.argv.index("--test") + 1], sys.argv[sys.argv.index("--test") + 2])
    else:
        p = Panel()

        def greet():
            p.show(by_hotkey=True)
            p.flash("Панель запущена: верхний край экрана" + (f" или {plat.HOTKEY_LABEL}" if CONFIG["hotkey"] else ""))
            p.root.after(3000, lambda: p.by_hotkey and p.hide())
        p.root.after(700, greet)
        p.root.mainloop()
        if p.mirror:  # окно закрыто не через меню — всё равно отпускаем DWM thumbnail
            try:
                p.mirror.close()
            except Exception:
                pass
