"""Моя панель — бесплатная панель у верхнего края экрана.

Сессии Claude Code (изометрический «офис» агентов, клик — открыть VS Code) и лимиты как в /usage,
история буфера обмена (текст и картинки, скриншот области), что сейчас играет (музыка и YouTube) с управлением,
папка «Загрузки» (перетаскивание файлов мышью в другие программы), мониторинг серверов (ping/ssh).
Уведомления: «Claude закончил» и «ждёт разрешения» — панель ненадолго выезжает, не забирая фокус.
Подведи курсор к верхнему краю экрана (по центру) или нажми Ctrl+Alt+P (на macOS — ⌘⌥P).
Закрыть программу: правый клик по заголовку «моя панель».

Работает на Windows и macOS: всё ОС-зависимое — в platform_win.py / platform_mac.py.
Необязательный config.json рядом со скриптом (пример — config.example.json):
{"main_agent_name": "Claude", "hotkey": true, "notify": {"done": true, "permission": true, "sound": true},
 "servers": [{"name": "файл-сервер", "host": "192.0.2.10", "ssh": "admin@192.0.2.10"}]}
(hotkey по умолчанию: Windows — true, macOS — false).

Лимиты: токен входа Claude Code читается из ~/.claude/.credentials.json (на macOS — из Связки ключей)
и отправляется только на api.anthropic.com (тот же запрос, что делает /usage). История буфера — только в памяти.
"""
import dataclasses
import hashlib
import io
import json
import os
import queue
import re
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageTk

if sys.platform == "win32":
    import platform_win as plat
else:
    import platform_mac as plat

VideoMirror = plat.VideoMirror  # живое видео (только Windows); None — показываем обложку


def _optional(name):
    """Необязательный модуль рядом со скриптом: нет или упал при импорте — его функция просто скрыта."""
    try:
        return __import__(name)
    except Exception as e:
        plat.log(f"{name}: модуль недоступен ({type(e).__name__}) — функция скрыта")
        return None


downloads = _optional("downloads")  # вкладка «Загрузки»
dragout = _optional("dragout")      # перетаскивание файлов из панели (tkinterdnd2)
monitor = _optional("monitor")      # вкладка «Сеть»: карточки серверов
netinfo = _optional("netinfo")      # вкладка «Сеть»: шпаргалка сисадмина («Этот компьютер», «Проверка»)
layout = _optional("layout")              # вкладка «Мониторы»: модель раскладки и геометрия (бэкенд)
monitors_ctl = _optional("monitors_ctl")  # вкладка «Мониторы»: оркестратор (бэкенд)
layout_editor = _optional("layout_editor")  # вкладка «Мониторы»: схема экранов на canvas


def plat_fn(name):
    """Функция платформенного слоя, если она уже есть (новые функции появляются не сразу на обеих ОС)."""
    fn = getattr(plat, name, None)
    return fn if callable(fn) else None

# ── Один экземпляр ──
if not plat.single_instance() and "--test" not in sys.argv:
    sys.exit(0)

HERE = Path(__file__).resolve().parent


CONFIG_PATH = HERE / "config.json"  # self_test подменяет на временную копию


def load_config(errors=None):
    """config.json рядом со скриптом — необязателен. Неверные значения игнорируются (берутся по умолчанию).

    errors — список: если файл не читается как JSON-объект, туда попадает причина для пользователя
    (горячая перезагрузка в этом случае оставляет прежние настройки).
    """
    cfg = {"main_agent_name": "Claude", "hotkey": plat.HOTKEY_DEFAULT,
           "notify": {"done": True, "sound": True, "permission": True}, "servers": [], "monitors": None}
    path = CONFIG_PATH
    if not path.exists():
        return cfg
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        plat.log(f"config.json: ошибка JSON в строке {e.lineno} — настройки по умолчанию")
        if errors is not None:
            errors.append(f"ошибка в строке {e.lineno}")
        return cfg
    except (OSError, ValueError) as e:
        plat.log(f"config.json не прочитан ({type(e).__name__}) — настройки по умолчанию")
        if errors is not None:
            errors.append("не прочитан (кодировка или доступ)" if isinstance(e, ValueError) else "не прочитан")
        return cfg
    if not isinstance(d, dict):
        plat.log("config.json: ожидается объект — настройки по умолчанию")
        if errors is not None:
            errors.append("ожидается объект { … }")
        return cfg
    name = d.get("main_agent_name")
    if isinstance(name, str) and name.strip():
        cfg["main_agent_name"] = " ".join(name.split())[:24]
    if isinstance(d.get("hotkey"), bool):
        cfg["hotkey"] = d["hotkey"]
    notify = d.get("notify")
    if isinstance(notify, dict):
        for k in cfg["notify"]:
            if isinstance(notify.get(k), bool):
                cfg["notify"][k] = notify[k]
    servers = d.get("servers")
    if isinstance(servers, list):
        for srv in servers[:SERVERS_MAX]:
            host = srv.get("host") if isinstance(srv, dict) else None
            if not isinstance(host, str) or not host.strip():
                plat.log("config.json: сервер без host пропущен")
                continue
            entry = dict(srv)  # остальные ключи (ssh и т.п.) — как есть, их разбирает monitor.py
            entry["host"] = host.strip()
            name = srv.get("name")
            entry["name"] = " ".join(name.split())[:32] if isinstance(name, str) and name.strip() else entry["host"]
            cfg["servers"].append(entry)
    if isinstance(d.get("monitors"), dict):  # проверяет layout.parse_config (через MonitorsCtl), здесь — как есть
        cfg["monitors"] = d["monitors"]
    return cfg


SERVERS_MAX = 12


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
NOTE_MS = 3200          # мс: сколько висит плашка уведомления
DONE_MIN_BUSY = 10      # с: короче — «Claude закончил» не показываем (мелкие ответы не дёргают)
PERM_STALE = 5          # с: столько jsonl не менялся с незакрытым tool_use — считаем, что ждёт разрешения
IMG_MAX, IMG_BUDGET = 10, 200 * 1024 * 1024  # картинок в истории буфера и их суммарный объём в памяти (RGB)
MULTI_LABEL = "⌘" if sys.platform == "darwin" else "Ctrl"  # множественный выбор в «Загрузках»
DL_TICK = 3000          # мс: проверка папки «Загрузки»
NET_REFRESH_MIN = 5     # с: local_info — не чаще
PUBLIC_IP_TTL = 600     # с: внешний IP кешируется (запрос к api.ipify.org — только по кнопке)
NET_HIST_MAX, NET_LOG_MAX = 5, 300  # недавних адресов «Проверки» / строк в окне вывода
NET_ACTIONS = (("ping", "Ping"), ("trace", "Trace"), ("port", "Порт"), ("dns", "DNS"))
NET_KIND = {"ethernet": "кабель", "wifi": "Wi-Fi", "vpn": "VPN", "other": "другое"}
NET_OUT_HINT = "Введи адрес и нажми Ping (или Enter), Trace, Порт или DNS."
CONFIG_TICK = 3000      # мс: проверка mtime config.json (горячая перезагрузка)
MON_TICK = 1000         # мс: статус вкладки «Мониторы» (только когда она открыта)
MON_ROLLBACK_S = 15     # с: «Применить к Windows» без «Оставить» откатывается (§7.5; откат делает MonitorsCtl)
MON_LABEL_MAX = 32      # символов в подписи экрана
MON_NAME_RE = re.compile(r"[A-Za-z0-9._][A-Za-z0-9._-]{0,62}")  # имя машины для Deskflow (§2.1)
MON_OS = (("windows", "Windows"), ("macos", "macOS"), ("linux", "Linux"))
MON_STATE = {"ok": ("установлен", "mint"), "missing": ("не установлен", "muted"), "old": ("старая версия", "accent"),
             "unknown_dialect": ("версия не распознана", "accent"), "na": ("не нужен здесь", "muted"),
             "unsupported": ("не поддерживается на этой ОС", "muted"), "conflict": ("конфликт", "red")}

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


def editor_fallback_cmd(path):
    """Системный текстовый редактор, если VS Code нет: Windows — notepad.exe из System32 (абсолютный путь:
    у .json часто нет связанной программы, и os.startfile падает), macOS — open -t, Linux — xdg-open."""
    if sys.platform == "win32":
        root = os.environ.get("SystemRoot") or os.environ.get("windir") or r"C:\Windows"
        return [str(Path(root) / "System32" / "notepad.exe"), path]
    if sys.platform == "darwin":
        return ["open", "-t", path]
    return ["xdg-open", path]


def open_text_file(path):
    """Открыть текстовый файл: VS Code (тот же поиск code, что для сессий), иначе системный редактор.

    Возврат: (None, чем открыли) или (текст ошибки, None).
    """
    path = os.path.abspath(path)
    code = getattr(plat, "CODE", None)
    if code and os.path.isfile(code):
        err = plat.open_folder_in_code(path)  # run_code([path]) / code path — с файлом работает так же
        if err is None:
            return None, "VS Code"
        plat.log("open_text_file: VS Code не запустился — пробую системный редактор")
    cmd = editor_fallback_cmd(path)
    if sys.platform == "win32" and not os.path.isfile(cmd[0]):
        return "Не найден ни VS Code, ни Блокнот", None
    try:
        subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         **({"creationflags": 0x08000000} if sys.platform == "win32" else {}))
    except OSError as e:
        plat.log(f"open_text_file: {Path(cmd[0]).name} — {type(e).__name__}")
        return f"Не удалось открыть: нет VS Code, {Path(cmd[0]).name} не запустился", None
    return None, {"win32": "Блокнот", "darwin": "текстовый редактор"}.get(sys.platform, "редактор по умолчанию")


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


_state_cache = {}  # path -> (mtime, {"pending": bool, "end_turn": bool})


def jsonl_turn_state(path, st=None):
    """Чем закончился хвост jsonl: pending — есть tool_use без tool_result; end_turn — ход завершён.

    Читаем только хвост ~64 КБ и только записи assistant/user; кеш по mtime. Ошибки чтения — «ничего не знаем».
    """
    key = str(path)
    try:
        st = st or path.stat()
    except OSError:
        return {"pending": False, "end_turn": False}
    with _cache_lock:
        hit = _state_cache.get(key)
    if hit and hit[0] == st.st_mtime:
        return hit[1]
    pending, stop = set(), None
    try:
        with open(path, "rb") as f:
            start = max(0, st.st_size - TAIL_BYTES)
            f.seek(start)
            lines = f.read().split(b"\n")
    except OSError:
        return {"pending": False, "end_turn": False}
    if start:
        lines = lines[1:]
    for line in lines:
        if len(line) > 256 * 1024 or not (b'"assistant"' in line or b'"tool_result"' in line):
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if not isinstance(d, dict) or d.get("type") not in ("assistant", "user"):
            continue
        msg = d.get("message") or {}
        content = msg.get("content")
        blocks = [c for c in content if isinstance(c, dict)] if isinstance(content, list) else []
        if d["type"] == "assistant":
            pending.update(c.get("id") for c in blocks if c.get("type") == "tool_use" and c.get("id"))
            stop = msg.get("stop_reason") or stop
        else:
            results = [c for c in blocks if c.get("type") == "tool_result"]
            for c in results:
                pending.discard(c.get("tool_use_id"))
            if not results and not d.get("isMeta"):  # новый запрос пользователя — ход начался заново
                stop = None
    res = {"pending": bool(pending), "end_turn": stop == "end_turn" and not pending}
    with _cache_lock:
        _state_cache[key] = (st.st_mtime, res)
    return res


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
def read_agents(sid, done=None):
    """Живые субагенты сессии: не finished, ход не завершён (end_turn) и jsonl менялся за последние 15 минут.

    done — множество, куда складываются id субагентов, которые только что завершили работу (для уведомлений).
    """
    agents = []
    if not UUID_RE.fullmatch(sid or ""):
        return agents
    for meta in sorted(CLAUDE.glob(f"projects/*/{sid}/subagents/*.meta.json")):
        try:
            m = json.loads(meta.read_text(encoding="utf-8"))
        except Exception:
            continue
        aid = meta.name[:-len(".meta.json")]
        jl = meta.with_name(aid + ".jsonl")
        try:
            st = jl.stat()
            age = time.time() - st.st_mtime
        except OSError:
            st, age = None, 1e9
        if age >= 900:
            continue
        if m.get("finished") or (st and jsonl_turn_state(jl, st)["end_turn"]):
            if done is not None:
                done.add(aid)
            continue
        agents.append({"id": aid, "desc": m.get("description") or m.get("agentType") or "агент",
                       "type": agent_label(m), "busy": age < 90})
    return agents


# Белый список: эти расширения открываются двойным кликом сразу. Всё остальное (программы, скрипты, ярлыки,
# образы, неизвестные типы) open_path мог бы запустить — только после «Запустить …?».
SAFE_OPEN_EXT = frozenset((
    "pdf", "txt", "md", "log", "doc", "docx", "xls", "xlsx", "csv", "ppt", "pptx", "odt", "ods", "odp", "rtf",
    "png", "jpg", "jpeg", "gif", "webp", "bmp", "svg", "heic", "tif", "tiff", "ico",
    "mp3", "wav", "flac", "m4a", "ogg", "aac", "mp4", "mkv", "mov", "avi", "webm", "wmv", "m4v",
    "zip", "7z", "rar"))
MAC_BUNDLE_EXT = ("app", "pkg", "mpkg")  # на macOS это папки, но открытие = запуск/установка
PERMISSION_WAIT = ("sandbox request", "worker request", "permission prompt", "goal proposal")  # тоже «разреши»
IGNORED_WAIT = ("dialog open",)  # диалог открыл сам пользователь — это не «ждёт тебя»
PERM_FALLBACK_MAX_AGE = 600      # с: запасной признак — только для jsonl моложе 10 минут (старые не залипают)


def session_attention(d, jl):
    """Ждёт ли сессия человека: (вид, подробность). Вид — "permission", "input" или None.

    Основной признак — ~/.claude/sessions/<pid>.json: Claude Code пишет status "waiting" и waitingFor
    («approve Bash: git push», «input needed», «sandbox request»…), пока на экране запрос разрешения или вопрос.
    Запасной — хвост jsonl: статус не busy, а последний tool_use без tool_result и файл не менялся PERM_STALE с.
    """
    status, wf = d.get("status"), d.get("waitingFor")
    wf = " ".join(wf.split()) if isinstance(wf, str) else ""
    if status == "waiting":
        low = wf.lower()
        if low in IGNORED_WAIT:
            return None, ""
        if low.startswith("approve") or low in PERMISSION_WAIT or not low:
            detail = wf[len("approve"):].strip() if low.startswith("approve") else wf
            return "permission", clip(detail, 60)
        return "input", clip(wf, 60)
    if status != "busy" and jl is not None:
        try:
            st = jl.stat()
        except OSError:
            return None, ""
        age = time.time() - st.st_mtime
        if PERM_STALE < age < PERM_FALLBACK_MAX_AGE and jsonl_turn_state(jl, st)["pending"]:
            return "permission", ""
    return None, ""


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
        title, jl_path = "", None
        if UUID_RE.fullmatch(sid):
            for jl in CLAUDE.glob(f"projects/*/{sid}.jsonl"):
                info = read_jsonl_info(jl)
                title, jl_path = (info or {}).get("title", ""), jl
                break
        attn, detail = session_attention(d, jl_path)
        done = set()
        out.append({
            "id": sid, "name": session_title(folder, title, d.get("name") or ""), "title": title,
            "folder": folder, "cwd": cwd, "busy": d.get("status") == "busy" and not attn,
            "attn": attn, "attn_detail": detail,
            "updated": (d.get("statusUpdatedAt") or d.get("updatedAt") or 0) / 1000,
            "agents": read_agents(sid, done), "agents_done": done, "vscode": d.get("entrypoint") == "claude-vscode",
        })
    out.sort(key=lambda s: (not s["attn"], not s["busy"], -s["updated"]))
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


def secs_ago(ts):
    """«проверено 5 с назад» — для мониторинга нужна точность до секунд."""
    if not isinstance(ts, (int, float)) or ts <= 0:
        return "ещё не проверено"
    s = max(0, time.time() - ts)
    return f"проверено {int(s)} с назад" if s < 120 else f"проверено {ago(ts)}"


def fmt_bytes(n):
    """Размер для людей: из downloads.human_size, если модуль есть, иначе своя простая версия."""
    if not isinstance(n, (int, float)):
        return str(n) if n not in (None, "") else ""
    if downloads and hasattr(downloads, "human_size"):
        try:
            return downloads.human_size(n)
        except Exception:
            pass
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if abs(n) < 1024 or unit == "ТБ":
            return f"{n:.0f} {unit}" if unit == "Б" else f"{n:.1f} {unit}"
        n /= 1024


UPTIME_WORDS = (("weeks", "нед"), ("week", "нед"), ("days", "дн"), ("day", "дн"), ("hours", "ч"), ("hour", "ч"),
                ("minutes", "мин"), ("minute", "мин"), ("years", "г"), ("year", "г"))


def fmt_uptime(v):
    """uptime: секунды → «12 дн 4 ч»; строку из uptime -p («12 days, 4 hours») — по-русски."""
    if isinstance(v, (int, float)) and v >= 0:
        d, h, m = int(v // 86400), int(v % 86400 // 3600), int(v % 3600 // 60)
        return f"{d} дн {h} ч" if d else f"{h} ч {m} мин" if h else f"{m} мин"
    if not v:
        return ""
    text = str(v)
    for en, ru in UPTIME_WORDS:
        text = re.sub(r"\b" + en + r"\b", ru, text)
    return clip(text.replace(",", ""), 40)


# Тип файла в «Загрузках»: метка и её цвет (папка — отдельно)
FILE_KINDS = (
    ("#ffb547", ("zip", "rar", "7z", "tar", "gz", "tgz", "bz2", "xz", "zst")),           # архив
    ("#ff6b81", ("pdf",)),
    ("#5ee6a8", ("png", "jpg", "jpeg", "gif", "webp", "bmp", "svg", "heic", "ico", "tif", "tiff")),
    ("#4fc3f7", ("doc", "docx", "odt", "rtf", "txt", "md", "pages")),                    # документ
    ("#7bd88f", ("xls", "xlsx", "ods", "csv", "numbers")),                               # таблица
    ("#ff8fb1", ("mp4", "mkv", "avi", "mov", "webm", "wmv", "m4v")),                     # видео
    ("#8b7bff", ("exe", "msi", "bat", "cmd", "ps1", "dmg", "pkg", "appimage", "deb", "apk")),  # программа
)
FOLDER_COLOR, OTHER_COLOR = "#d9a35a", "#5a5f96"


def file_badge(name, is_dir):
    """(текст метки, цвет) для строки загрузок: «папка», «PDF», «ZIP»… неизвестное — серым."""
    if is_dir:
        return "папка", FOLDER_COLOR
    ext = name.rsplit(".", 1)[-1].lower() if "." in name.strip(".") else ""
    for color, exts in FILE_KINDS:
        if ext in exts:
            return ext.upper()[:4], color
    return (ext.upper()[:4] or "файл"), OTHER_COLOR


class ClipImage:
    """Картинка в истории буфера: оригинал (RGB, только в памяти) и миниатюра для списка."""

    def __init__(self, img, label):
        self.img = img.convert("RGB") if img.mode != "RGB" else img
        self.w, self.h = self.img.size
        self.label = f"{label} {self.w}×{self.h}"
        # дубль — только побайтно та же картинка (хеш всех пикселей, а не миниатюры: похожие не склеиваются)
        self.key = (self.w, self.h, hashlib.blake2b(self.img.tobytes(), digest_size=16).digest())
        self.photo = None  # ImageTk.PhotoImage создаётся при первом показе (только в потоке Tk)

    @property
    def nbytes(self):
        return self.w * self.h * 3

    def thumb(self, height, max_w):
        if self.photo is None:
            k = min(height / self.h, max_w / self.w)
            im = self.img.resize((max(1, int(self.w * k)), max(1, int(self.h * k))), Image.LANCZOS)
            self.photo = ImageTk.PhotoImage(im)
        return self.photo


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
        self.shown = self.pinned = self.by_hotkey = self.by_notify = False
        self.dwell, self.frame = 0, 0
        self.last_inside = time.time()
        self.anim = None
        self.tab = {"server": "net"}.get(test_tab, test_tab) or "sessions"  # «server» — старое имя вкладки
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
        # уведомления «Claude закончил» / «ждёт разрешения»
        self.watch, self.watch_ready = {}, False  # sid -> {"st", "since", "agents": {aid: начало работы}}
        self.sess_reading = self.sess_loaded = False  # read_sessions идёт в фоне / первый результат уже есть
        self.notes, self.note_cur, self.note_job, self.banner = [], None, None, None
        # загрузки
        self.dl_items, self.dl_mtime, self.dl_err, self.dl_loading = [], None, None, False
        self.dl_ui, self.dl_sig, self.dl_sel, self.dl_anchor, self.dl_pending = None, None, set(), None, None
        self.dl_query = tk.StringVar(self.root)
        self.dl_query.trace_add("write", lambda *a: self.dl_render())
        self.dnd_ok = False
        if dragout:
            try:
                self.dnd_ok = bool(dragout.init_dnd(self.root))
            except Exception as e:
                plat.log(f"dragout.init_dnd: {type(e).__name__}")
        # мониторинг серверов
        self.monitor, self.monitor_err, self.srv_ui, self.srv_sig = None, None, None, None
        if monitor and CONFIG["servers"]:
            try:
                self.monitor = monitor.Monitor(CONFIG["servers"])
                self.monitor.start()
            except Exception as e:
                self.monitor, self.monitor_err = None, f"мониторинг не запустился ({type(e).__name__})"
                plat.log(self.monitor_err)
        # шпаргалка сисадмина (netinfo): сведения о сети и проверки; состояние переживает перестройку вкладки
        self.net_ui, self.net_info, self.net_info_err = None, None, None
        self.net_loading, self.net_info_req = False, 0.0  # local_info идёт в фоне / когда запрошен последний раз
        self.net_pub, self.net_pub_loading, self.net_pub_err = (None, 0.0), False, False  # (ip, когда получен)
        self.net_busy, self.net_cancel, self.net_log, self.net_hist = None, None, [], []
        self.net_addr, self.net_port = tk.StringVar(self.root), tk.StringVar(self.root)
        self.snapping = False  # идёт скриншот области
        self.flash_job, self.srv_rebuild = None, False
        self.cfg_mtime = self.config_mtime()  # горячая перезагрузка config.json (tick_config)
        # «Мониторы»: черновик раскладки и мастер переживают перестройку вкладки
        self.mon_ctl, self.mon_err, self.mon_ui, self.mon_editor, self.mon_snap = None, None, None, None, {}
        self.mon_draft, self.mon_dirty, self.mon_sel, self.mon_displays = None, False, None, None
        self.mon_pending, self.mon_wiz, self.mon_hold, self.mon_pin_prev = None, None, set(), False
        self.mon_detecting = False
        self.mon_init(autostart=test_tab is None)  # в --test помощник краёв и режимы не запускаем

        for role in ("base", "small", "tiny", "bold", "track", "title", "mono", "num", "big"):
            setattr(self, f"f_{role}", self.make_font(*plat.FONTS[role]))
        bold_fam, _, bold_kw = plat.FONTS["bold"]
        self.f_tab_bold = self.make_font(bold_fam, plat.FONTS["small"][1], bold_kw)  # активная вкладка при 6 вкладках

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
                       (140, self.tick_office), (50, self.tick_sessions), (500, self.tick_music),
                       (DL_TICK, self.tick_downloads), (1000, self.tick_server), (MON_TICK, self.tick_monitors),
                       (CONFIG_TICK, self.tick_config)):
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
        tabs = [("sessions", "Сессии"), ("music", "Музыка"), ("clip", "Буфер")]
        if downloads:  # модуля нет — вкладку не показываем
            tabs.append(("downloads", "Загрузки"))
        if monitor or netinfo:
            tabs.append(("net", "Сеть"))
        if layout_editor:  # нет бэкенда (layout.py, monitors_ctl.py) — вкладка скажет «модуль не найден»
            tabs.append(("monitors", "Мониторы"))
        pad = 10 if len(tabs) <= 3 else 7 if len(tabs) <= 5 else 4
        # 6 вкладок в 600 px: подписи мельче (§13 спецификации «Мониторов»)
        self.f_tab, self.f_tab_on = (self.f_base, self.f_bold) if len(tabs) <= 5 else (self.f_small, self.f_tab_bold)
        for key, name in tabs:
            cell = tk.Frame(self.tabbar, bg=C["head"])
            cell.pack(side="left")
            lb = tk.Label(cell, text=name, bg=C["head"], fg=C["muted"], font=self.f_tab, padx=pad, pady=9, cursor="hand2",
                          takefocus=1)
            lb.pack()
            bar = tk.Frame(cell, bg=C["head"], height=2)
            bar.pack(fill="x", padx=pad - 2)
            lb.bind("<Button-1>", lambda e, k=key: self.set_tab(k))
            lb.bind("<Return>", lambda e, k=key: self.set_tab(k))
            self.tabs[key] = (lb, bar)
        if self.tab not in self.tabs:
            self.tab = "sessions"
        self.head = head
        foot = tk.Frame(outer, bg=C["bg"])
        foot.pack(fill="x", side="bottom")
        line = tk.Frame(foot, bg=C["bg"])  # строка статуса по центру; справа от текста — кнопка действия flash
        line.pack()
        self.status = tk.Label(line, text=HINT, bg=C["bg"], fg=C["muted"], font=self.f_tiny, pady=5)
        self.status.pack(side="left")
        self.status_btn = tk.Label(line, text="", bg=C["item"], fg=C["ink"], font=self.f_tiny, padx=8, cursor="hand2",
                                   takefocus=1, highlightthickness=1, highlightbackground=C["bg"],
                                   highlightcolor=C["accent"])
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

    def flash(self, text, color=None, action=None):
        """Сообщение в строке статуса на 2,2 с. action — (подпись, функция): кнопка рядом, сообщение висит 6 с."""
        if self.flash_job:
            self.root.after_cancel(self.flash_job)
        self.status.configure(text=text, fg=color or C["mint"])
        btn = self.status_btn
        if action:
            label, fn = action

            def run(e=None):
                self.flash_reset()
                fn()
            btn.configure(text=label)
            for seq in ("<Button-1>", "<Return>", "<space>"):
                btn.bind(seq, run)
            btn.pack(side="left", padx=(6, 0))
        else:
            btn.pack_forget()
        self.flash_job = self.root.after(6000 if action else 2200, self.flash_reset)

    def flash_reset(self):
        self.flash_job = None
        self.status.configure(text=HINT, fg=C["muted"])
        self.status_btn.pack_forget()

    def set_tab(self, key):
        self.tab = key
        for k, (lb, bar) in self.tabs.items():
            on = k == key
            lb.configure(fg=C["ink"] if on else C["muted"], font=self.f_tab_on if on else self.f_tab)
            bar.configure(bg=C["accent"] if on else C["head"])
        self.video_off()
        for w in self.body.winfo_children():
            w.destroy()
        self.office = self.limits_box = self.music_ui = self.dl_ui = self.srv_ui = self.net_ui = None
        self.mon_ui = self.mon_editor = None
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

    def run_bg(self, fn, done):
        """fn() в фоновом потоке, done(результат, ошибка) — потом в потоке Tk (через очередь tick_queue)."""
        def work():
            try:
                res, err = fn(), None
            except Exception as e:
                res, err = None, e
            self.q.put(("call", done, res, err))
        threading.Thread(target=work, daemon=True).start()

    def quit(self):
        if self.monitor:
            try:
                self.monitor.stop()
            except Exception:
                pass
        if self.net_cancel is not None:  # идёт traceroute — пусть процесс завершится
            self.net_cancel.set()
        if self.mon_ctl:  # при stop_on_exit контроллер останавливает свои процессы
            try:
                self.mon_ctl.stop()
            except Exception as e:
                plat.log(f"monitors: stop — {type(e).__name__}")
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
        n = len(s["agents"])
        extra = f" · {n} {plural(n, 'агент', 'агента', 'агентов')}" if n else ""
        if s.get("attn"):
            state = "ждёт разрешения" if s["attn"] == "permission" else "ждёт ответа"
            detail = f": {s['attn_detail']}" if s.get("attn_detail") else ""
            return f"{where} · {state}{detail} · {ago(s['updated'])}{extra}"
        state = "работает" if s["busy"] else "ждёт тебя"
        return f"{where} · {state} {ago(s['updated'])}{extra}"

    def session_row(self, lst, s):
        """Живая сессия: «проект · вкладка», ниже где/когда, у кого есть субагенты — их короткие имена."""
        r = self.row(lst, on_click=lambda: self.open_session(s))
        dot = C["red"] if s.get("attn") else C["mint"] if s["busy"] else C["accent"]
        tk.Label(r, text="●", bg=C["item"], fg=dot, font=self.f_base, padx=8).pack(side="left")
        tk.Label(r, text="открыть ↗", bg=C["item"], fg=C["muted"], font=self.f_tiny, padx=10).pack(side="right")
        txt = tk.Frame(r, bg=C["item"])
        txt.pack(side="left", fill="x", expand=True, pady=4)
        tk.Label(txt, text=s["name"], bg=C["item"], fg=C["ink"], font=self.f_bold, anchor="w").pack(fill="x")
        meta = tk.Label(txt, text=self.live_meta(s), bg=C["item"], fg=C["red"] if s.get("attn") else C["muted"],
                        font=self.f_tiny, anchor="w")
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
        # self.sessions — последний результат фонового read_sessions (tick_sessions); здесь диск не читаем
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
        if not self.sess_loaded:
            tk.Label(live, text="Ищу сессии Claude Code…", bg=C["bg"], fg=C["muted"], font=self.f_small, pady=8).pack()
        elif not self.sessions:
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
        live = tuple((s["id"], s["name"], s["busy"], s.get("attn"), tuple(a["type"] for a in s["agents"]))
                     for s in self.sessions)
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
            cv.create_text(w / 2, h * 0.5, text="Офис пуст: сессий Claude Code не запущено" if self.sess_loaded
                           else "Ищу сессии…", fill=C["muted"], font=self.f_small)
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
            self.draw_station(Iso(cv, x + step / 2, oy, S), CLAUDE_ORANGE, sess["busy"], gi * 13, True,
                              alert=bool(sess.get("attn")))
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

    def draw_station(self, iso, color, busy, seed, main, alert=False):
        """Стол с агентом; alert — над головой мигает красный «!» (ждёт разрешения), вместо «zZ»."""
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
        if alert:
            x, y = iso.P(4.0, 5.0, hz + 4.2)
            r = max(7, iso.S * 1.2)
            on = f % 6 < 4  # мигает: 4 кадра горит, 2 — тусклый
            iso.cv.create_oval(x - r, y - r, x + r, y + r, fill=C["red"] if on else shade(C["red"], 0.45),
                               outline=shade(C["red"], 1.2) if on else "", width=1)
            iso.cv.create_text(x, y, text="!", fill=C["ink"] if on else C["muted"], font=self.f_bold)
        elif not busy:
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
        self.button(bar, "Очистить", self.clip_clear, small=True, fg=C["muted"]).pack(side="left", padx=6)
        if self.can_screenshot():
            self.button(bar, "✂ Скриншот", self.screenshot, small=True, fg=C["accent"]).pack(side="left")
        tk.Label(bar, text="Только в памяти · пароли из менеджеров паролей не пишутся", bg=C["bg"], fg=C["muted"], font=self.f_tiny).pack(side="right")
        lst = self.scroll_area(self.body, key="clip")
        if self.paused:
            tk.Label(lst, text="Запись буфера на паузе.", bg=C["bg"], fg=C["accent"], font=self.f_base, pady=6).pack()
        if not self.history:
            tk.Label(lst, text=f"История пуста. Скопируй что-нибудь ({plat.COPY_LABEL}).", bg=C["bg"], fg=C["muted"], font=self.f_base, pady=16).pack()
        for i, (item, ts) in enumerate(self.history):
            if isinstance(item, ClipImage):
                self.clip_image_row(lst, i, item, ts)
                continue
            r = self.row(lst, on_click=lambda t=item: (self.copy(t), self.set_tab("clip")))
            one = " ".join(item.split())
            lb = tk.Label(r, text=one[:78] + ("…" if len(one) > 78 else ""), bg=C["item"], fg=C["ink"], font=self.f_base,
                          anchor="w", padx=10, pady=7, cursor="hand2")
            lb.pack(side="left", fill="x", expand=True)
            lb.bind("<Button-1>", lambda e, t=item: (self.copy(t), self.set_tab("clip")))
            tk.Label(r, text=time.strftime("%H:%M", time.localtime(ts)), bg=C["item"], fg=C["muted"], font=self.f_mono).pack(side="right", padx=6)
            self.icon(r, "✕", lambda i=i: self.clip_del(i), fg=C["red"]).pack(side="right")

    def clip_image_row(self, lst, i, item, ts):
        """Картинка в истории: миниатюра высотой ~60, подпись «скриншот 812×430»; клик — снова в буфер."""
        r = self.row(lst, on_click=lambda: self.copy_image(item))
        try:
            photo = item.thumb(int(60 * self.s), int(220 * self.s))
            pic = tk.Label(r, image=photo, bg=C["item"], bd=0, cursor="hand2")
        except Exception:  # миниатюра не собралась — хотя бы подпись
            pic = tk.Label(r, text="▣", bg=C["item"], fg=C["muted"], font=self.f_big, cursor="hand2")
        pic.pack(side="left", padx=10, pady=6)
        txt = tk.Frame(r, bg=C["item"])
        txt.pack(side="left", fill="x", expand=True)
        tk.Label(txt, text=item.label, bg=C["item"], fg=C["ink"], font=self.f_base, anchor="w", cursor="hand2").pack(fill="x")
        tk.Label(txt, text="клик — снова в буфер", bg=C["item"], fg=C["muted"], font=self.f_tiny, anchor="w",
                 cursor="hand2").pack(fill="x")
        for w in (pic, txt, *txt.winfo_children()):
            w.bind("<Button-1>", lambda e: self.copy_image(item))
        tk.Label(r, text=time.strftime("%H:%M", time.localtime(ts)), bg=C["item"], fg=C["muted"], font=self.f_mono).pack(side="right", padx=6)
        self.icon(r, "✕", lambda: self.clip_del(i), fg=C["red"]).pack(side="right")

    def push_history(self, text):
        self.history = [h for h in self.history if h[0] != text]
        self.history.insert(0, (text, time.time()))
        del self.history[HISTORY_MAX:]

    def push_image(self, img, label):
        """Картинка в историю (дубль поднимается наверх). Не больше IMG_MAX картинок и IMG_BUDGET байт."""
        try:
            item = ClipImage(img, label)
        except Exception as e:
            plat.log(f"картинка не добавлена: {type(e).__name__}")
            return None
        self.history = [h for h in self.history if not (isinstance(h[0], ClipImage) and h[0].key == item.key)]
        self.history.insert(0, (item, time.time()))
        n = total = 0
        keep = []
        for h in self.history:
            if isinstance(h[0], ClipImage):
                n, total = n + 1, total + h[0].nbytes
                if n > IMG_MAX or (n > 1 and total > IMG_BUDGET):
                    continue  # старые картинки уходят первыми
            keep.append(h)
        self.history = keep[:HISTORY_MAX]
        return item

    def copy_image(self, item):
        """Положить картинку из истории обратно в буфер обмена."""
        setter = plat_fn("set_clipboard_image")
        ok = False
        if setter:
            try:
                ok = bool(setter(item.img))
            except Exception as e:
                plat.log(f"set_clipboard_image: {type(e).__name__}")
        if not ok:
            self.flash("Не удалось положить картинку в буфер", C["red"])
            return
        self.clip_seq = plat.clipboard_seq(self.root)  # своё копирование в историю второй раз не пишем
        self.push_image(item.img, item.label.rsplit(" ", 1)[0])
        if self.shown and self.tab == "clip":
            self.set_tab("clip")
        self.flash(f"В буфере: {item.label}")

    # ── Скриншот области ──
    def can_screenshot(self):
        if not plat_fn("set_clipboard_image"):
            return False
        return bool(plat_fn("capture_region_native") or plat_fn("grab"))

    def hide_now(self):
        """Спрятать панель мгновенно, без анимации (перед скриншотом — чтобы не попала в кадр)."""
        if self.anim:
            self.root.after_cancel(self.anim)
            self.anim = None
        self.end_note(silent=True)
        self.shown = self.by_notify = False
        self.video_off()
        if self.music_ui:
            self.set_video_mode(self.music_ui, False)
        self.y = self.top - self.H - 4
        self.root.geometry(f"+{self.x}+{self.y}")
        if plat.WITHDRAW_WHEN_HIDDEN:
            self.root.withdraw()

    def screenshot(self):
        if self.snapping:
            return
        self.snapping = True
        self.hide_now()
        native = plat_fn("capture_region_native")
        if native:  # macOS: системное выделение области (screencapture -i) — блокирующее, в фоне
            self.root.after(250, lambda: self.run_bg(native, self.native_shot_done))
        else:
            self.root.after(250, self.region_overlay)

    def native_shot_done(self, img, err):
        if err is not None:
            plat.log(f"capture_region_native: {type(err).__name__}")
            return self.shot_finish(None, "Скриншот не получился")
        if img is NotImplemented:  # на этой ОС системного выделения нет — рисуем своё
            return self.region_overlay()
        self.shot_finish(img, None if img is not None else "")

    def region_overlay(self):
        """Свой полноэкранный оверлей на мониторе курсора: тянешь прямоугольник, Esc или правый клик — отмена.

        Процесс DPI-aware, поэтому геометрия окна, координаты мыши и plat.grab — в физических пикселях.
        """
        if not plat_fn("grab"):
            return self.shot_finish(None, "Скриншот недоступен на этой системе")
        cx, cy = plat.cursor_pos(self.root)
        l, t, r, b = plat.monitor_rect(cx, cy, self.root)
        ov = tk.Toplevel(self.root)
        ov.overrideredirect(True)
        ov.geometry(f"{r - l}x{b - t}+{l}+{t}")
        try:
            ov.attributes("-alpha", 0.3)
            ov.attributes("-topmost", True)
        except tk.TclError:
            pass
        cv = tk.Canvas(ov, bg="#000000", highlightthickness=0, bd=0, cursor="crosshair")
        cv.pack(fill="both", expand=True)
        cv.create_text((r - l) // 2, int(40 * self.s), text="Выдели область мышью · Esc — отмена",
                       fill="#ffffff", font=self.f_track)
        st = {"x0": None, "y0": None, "rect": None, "size": None}

        def cancel(e=None):
            if ov.winfo_exists():
                ov.destroy()
            self.shot_finish(None, "")

        def press(e):
            st["x0"], st["y0"] = e.x, e.y
            # только рамка, без заливки: область не засвечивается (оверлей и так затемняет весь экран)
            st["rect"] = cv.create_rectangle(e.x, e.y, e.x, e.y, outline="#ffffff", width=max(2, int(2 * self.s)))
            st["size"] = cv.create_text(e.x, e.y - 6, text="", fill="#ffffff", font=self.f_bold, anchor="sw")

        def motion(e):
            if st["rect"] is None:
                return
            cv.coords(st["rect"], st["x0"], st["y0"], e.x, e.y)
            cv.coords(st["size"], min(st["x0"], e.x), min(st["y0"], e.y) - 6)
            cv.itemconfigure(st["size"], text=f"{abs(e.x - st['x0'])}×{abs(e.y - st['y0'])}")

        def release(e):
            if st["rect"] is None:
                return
            x0, x1 = sorted((st["x0"], e.x))
            y0, y1 = sorted((st["y0"], e.y))
            ov.destroy()
            if x1 - x0 < 4 or y1 - y0 < 4:  # просто клик — считаем отменой
                return self.shot_finish(None, "")
            bbox = (l + x0, t + y0, l + x1, t + y1)
            self.root.after(150, lambda: self.grab_region(bbox))  # оверлей успевает исчезнуть с экрана

        cv.bind("<ButtonPress-1>", press)
        cv.bind("<B1-Motion>", motion)
        cv.bind("<ButtonRelease-1>", release)
        for seq in ("<Escape>", *plat.RIGHT_CLICK):
            ov.bind(seq, cancel)
        ov.after(30, ov.focus_force)  # сами нажали «Скриншот» — фокус оверлею нужен, иначе Esc не дойдёт

    def grab_region(self, bbox):
        try:
            img = plat.grab(bbox)
        except Exception as e:
            plat.log(f"grab: {type(e).__name__}")
            img = None
        self.shot_finish(img, None if img is not None else "Скриншот не получился")

    def shot_finish(self, img, err):
        """Итог скриншота: картинку — в буфер и в историю; err="" — отмена (молча), текст — ошибка."""
        self.snapping = False
        if img is not None:
            item = self.push_image(img, "скриншот")
            if item:
                setter = plat_fn("set_clipboard_image")
                ok = False
                try:
                    ok = bool(setter and setter(item.img))
                except Exception as e:
                    plat.log(f"set_clipboard_image: {type(e).__name__}")
                self.clip_seq = plat.clipboard_seq(self.root)
                err = None if ok else "Скриншот в истории, но в буфер не попал"
        self.tab = "clip"
        self.show(by_hotkey=True)
        if err:
            self.flash(err, C["red"])
        elif img is not None:
            self.flash(f"Скриншот {img.width}×{img.height} — в буфере обмена")

    def clip_pause(self):
        self.paused = not self.paused
        self.set_tab("clip")

    def clip_clear(self):
        self.history.clear()
        self.set_tab("clip")

    def clip_del(self, i):
        self.history.pop(i)
        self.set_tab("clip")

    # ── Уведомления: «Claude закончил» и «ждёт разрешения» ──
    def watch_sessions(self, sessions):
        """Сравнить состояния сессий с прошлым опросом и поставить уведомления в очередь.

        Первый опрос после запуска панели — только запоминаем (иначе при старте посыпались бы уведомления).
        «Закончил» — переход busy → idle после работы не меньше DONE_MIN_BUSY с; субагент — его ход
        завершился (end_turn / finished), пока сессия жива. «Ждёт разрешения» — переход в attn.
        """
        now, ready = time.time(), self.watch_ready
        new = {}
        for s in sessions:
            st = "attn" if s.get("attn") else "busy" if s["busy"] else "idle"
            prev = self.watch.get(s["id"])
            since = prev["since"] if prev else None
            if st != "idle" and since is None:
                # впервые видим работающей: при старте — с момента смены статуса, дальше — с этого опроса
                since = min(now, s["updated"] or now) if not prev else now
            if ready and prev:
                if st == "idle" and prev["st"] == "busy" and since and now - since >= DONE_MIN_BUSY:
                    self.notify("done", s, "Claude закончил")
                elif st == "attn" and prev["st"] != "attn":
                    self.notify("permission", s, "ждёт твоего разрешения" if s["attn"] == "permission"
                                else "ждёт твоего ответа")
            agents = {}
            for a in s["agents"]:
                agents[a["id"]] = (prev or {}).get("agents", {}).get(a["id"], now)
            if ready and prev:
                for aid, started in prev.get("agents", {}).items():
                    if aid not in agents and aid in s.get("agents_done", ()) and now - started >= DONE_MIN_BUSY:
                        name = prev.get("names", {}).get(aid, "агент")
                        self.notify("done", s, f"агент {name} закончил")
            new[s["id"]] = {"st": st, "since": None if st == "idle" else since, "agents": agents,
                            "names": {a["id"]: a["type"] for a in s["agents"]}}
        self.watch, self.watch_ready = new, True

    def notify(self, kind, s, text):
        """В очередь уведомлений (выключенные в config.json — пропускаем; одинаковые подряд — не дублируем)."""
        if not CONFIG["notify"].get(kind, True):
            return
        note = {"kind": kind, "sess": s, "text": f"{s['name']} — {text}"}
        if any(n["kind"] == kind and n["text"] == note["text"] for n in self.notes):
            return
        self.notes.append(note)
        if self.note_cur is None:
            self.next_note()

    def next_note(self):
        if not self.notes:
            self.note_cur = None
            if self.by_notify and self.shown:  # выезжали только ради уведомлений — уезжаем обратно
                self.by_notify = False
                self.hide()
            return
        note = self.note_cur = self.notes.pop(0)
        self.show_banner(note)
        if CONFIG["notify"].get("sound", True):
            snd = plat_fn("notify_sound")
            if snd:
                threading.Thread(target=snd, daemon=True).start()
        if not self.shown:
            self.show_quiet()
        self.note_job = self.root.after(NOTE_MS, self.end_note)

    def end_note(self, silent=False):
        """Убрать текущую плашку; silent — без перехода к следующей (панель прячется насовсем)."""
        if self.note_job:
            self.root.after_cancel(self.note_job)
            self.note_job = None
        if self.banner is not None and self.banner.winfo_exists():
            self.banner.destroy()
        self.banner = None
        if silent:
            self.note_cur = None
            self.notes.clear()
            return
        self.next_note()

    def show_banner(self, note):
        """Плашка под заголовком: ✓ зелёная (закончил) или ⚠ красная (ждёт разрешения). Клик — открыть сессию."""
        if self.banner is not None and self.banner.winfo_exists():
            self.banner.destroy()
        perm = note["kind"] == "permission"
        bg = shade(C["red"], 0.42) if perm else shade(C["mint"], 0.3)
        fg = C["red"] if perm else C["mint"]
        b = self.banner = tk.Frame(self.head.master, bg=bg, cursor="hand2", takefocus=1)
        b.pack(fill="x", after=self.head)
        tk.Frame(b, bg=fg, width=max(3, int(4 * self.s))).pack(side="left", fill="y")
        tk.Label(b, text="⚠" if perm else "✓", bg=bg, fg=fg, font=self.f_track, padx=10).pack(side="left")
        tk.Label(b, text=clip(note["text"], 78), bg=bg, fg=C["ink"], font=self.f_bold, anchor="w",
                 pady=8).pack(side="left", fill="x", expand=True)
        close = tk.Label(b, text="✕", bg=bg, fg=C["muted"], font=self.f_base, padx=10, cursor="hand2")
        close.pack(side="right")
        tk.Label(b, text="открыть ↗", bg=bg, fg=C["muted"], font=self.f_tiny).pack(side="right")

        def open_it(e=None):
            self.by_notify = False  # пользователь откликнулся — панель дальше ведёт себя как обычно
            self.open_session(note["sess"])
            self.end_note()
        for w in (b, *b.winfo_children()):
            if w is not close:
                w.bind("<Button-1>", open_it)
        close.bind("<Button-1>", lambda e: self.end_note())
        b.bind("<Return>", open_it)
        b.bind("<space>", open_it)

    def show_quiet(self):
        """Выехать ради уведомления, не забирая фокус клавиатуры (пользователь может печатать в другом окне)."""
        if self.anim:
            self.root.after_cancel(self.anim)
            self.anim = None
        self.place_on_cursor_monitor()
        self.shown, self.by_notify, self.by_hotkey = True, True, False
        self.last_inside = time.time()
        if plat.WITHDRAW_WHEN_HIDDEN:
            self.root.deiconify()
        try:
            self.root.attributes("-topmost", True)
        except tk.TclError:
            pass
        self.slide(self.top)
        if self.tab == "sessions":
            self.set_tab("sessions")

    # ── Вкладка: загрузки ──
    def tab_downloads(self):
        ui = self.dl_ui = {"ago": []}
        bar = tk.Frame(self.body, bg=C["bg"])
        bar.pack(fill="x", padx=12, pady=(10, 6))
        tk.Label(bar, text="⌕", bg=C["bg"], fg=C["muted"], font=self.f_track).pack(side="left", padx=(0, 4))
        ent = tk.Entry(bar, textvariable=self.dl_query, bg=C["field"], fg=C["ink"], insertbackground=C["ink"],
                       relief="flat", font=self.f_base, highlightthickness=1, highlightbackground=C["line"],
                       highlightcolor=C["accent"])
        ent.pack(side="left", fill="x", expand=True, ipady=4)
        ent.bind("<Escape>", lambda e: (self.dl_query.set(""), "break")[1] if self.dl_query.get() else None)
        ent.bind("<Down>", lambda e: self.dl_focus_first())
        ui["entry"] = ent
        self.button(bar, "📂 Открыть папку", self.dl_open_folder, small=True).pack(side="right", padx=(8, 0))
        ui["head"] = tk.Label(self.body, text="", bg=C["bg"], fg=C["muted"], font=self.f_tiny, anchor="w")
        ui["head"].pack(fill="x", padx=12, pady=(2, 1))
        foot = tk.Label(self.body, bg=C["bg"], fg=C["muted"], font=self.f_tiny, anchor="w",
                        text=f"Двойной клик — открыть · {MULTI_LABEL}+клик — несколько · правый клик — меню"
                        + (" · файлы можно тащить мышью" if self.dnd_ok else ""))
        if not self.dnd_ok:
            foot.configure(text="перетаскивание недоступно: pip install tkinterdnd2", fg=C["accent"])
        foot.pack(fill="x", padx=12, pady=(0, 2), side="bottom")
        ui["confirm"] = tk.Frame(self.body, bg=C["bg"])  # «Запустить программу …?» — появляется над списком
        ui["confirm"].pack(fill="x", padx=12)
        ui["list"] = self.scroll_area(self.body, key="downloads")
        self.dl_sig = None
        self.dl_render()
        self.dl_refresh(force=True)

    def dl_dir_mtime(self):
        try:
            return downloads.downloads_dir().stat().st_mtime
        except Exception:
            return None

    def dl_refresh(self, force=False):
        """Перечитать папку в фоне — при показе вкладки или если сменился mtime папки."""
        if not downloads or self.dl_loading:
            return
        mt = self.dl_dir_mtime()
        if not force and mt == self.dl_mtime and self.dl_err is None:
            return
        self.dl_loading = True

        def done(items, err):
            self.dl_loading = False
            self.dl_mtime = mt
            if err is not None:
                plat.log(f"list_downloads: {type(err).__name__}")
                self.dl_err = "не удалось прочитать папку"
            else:
                self.dl_items, self.dl_err = list(items or []), None
                alive = {d["path"] for d in self.dl_items}
                self.dl_sel &= alive
            self.dl_render()
        self.run_bg(lambda: downloads.list_downloads(limit=300), done)

    def dl_visible(self):
        q = " ".join(self.dl_query.get().split()).casefold()
        return [d for d in self.dl_items if q in d.get("name", "").casefold()] if q else list(self.dl_items)

    def dl_render(self):
        """Перестроить строки, только если список или фильтр изменились; иначе — обновить «N мин назад»."""
        ui = self.dl_ui
        if not ui or not ui["list"].winfo_exists():
            return
        items = self.dl_visible()
        sig = (tuple((d["path"], d.get("size"), d.get("mtime")) for d in items), self.dl_err,
               self.dl_loading and not self.dl_items)
        if sig == self.dl_sig:  # выделение сюда не входит — его перекрашивает dl_apply_sel()
            for lb, ts in ui["ago"]:
                if lb.winfo_exists():
                    lb.configure(text=ago(ts))
            self.dl_apply_sel()
            return
        self.dl_sig = sig
        lst = ui["list"]
        for w in lst.winfo_children():
            w.destroy()
        ui["ago"], ui["rows"] = [], []
        q = self.dl_query.get().strip()
        self.dl_head()
        if self.dl_err:
            tk.Label(lst, text=f"Загрузки: {self.dl_err}", bg=C["bg"], fg=C["red"], font=self.f_small,
                     pady=10).pack()
            return
        if not items:
            msg = ("Читаю папку…" if self.dl_loading and not self.dl_items else
                   f"Ничего не найдено по «{clip(q, 30)}»." if q else "В папке «Загрузки» пусто.")
            tk.Label(lst, text=msg, bg=C["bg"], fg=C["muted"], font=self.f_base, pady=16).pack()
            return
        for d in items:
            self.dl_row(lst, d)

    def dl_row(self, lst, d):
        path = d["path"]
        r = tk.Frame(lst, bg=C["item"], cursor="hand2", takefocus=1)
        r.pack(fill="x", pady=1)
        text, color = file_badge(d.get("name", ""), d.get("is_dir"))
        badge = tk.Label(r, text=text, bg=color, fg=C["bg"], font=self.f_tiny, width=5, pady=1)
        badge.pack(side="left", padx=(8, 8), pady=5)
        when = tk.Label(r, text=ago(d.get("mtime") or 0), bg=C["item"], fg=C["muted"], font=self.f_tiny, width=11,
                        anchor="e")
        when.pack(side="right", padx=(4, 10))
        size = "" if d.get("is_dir") else fmt_bytes(d.get("size"))
        tk.Label(r, text=size, bg=C["item"], fg=C["muted"], font=self.f_mono, width=9, anchor="e").pack(side="right")
        tk.Label(r, text=clip(d.get("name", ""), 52), bg=C["item"], fg=C["ink"], font=self.f_base, anchor="w").pack(
            side="left", fill="x", expand=True)
        widgets = [w for w in (r, *r.winfo_children()) if w is not badge]
        row = {"frame": r, "d": d, "widgets": widgets, "hover": False}
        self.dl_ui["ago"].append((when, d.get("mtime") or 0))
        self.dl_ui["rows"].append(row)
        self.dl_paint(row)

        def hover(on):
            row["hover"] = on
            self.dl_paint(row)
        multi = "<Command-Button-1>" if sys.platform == "darwin" else "<Control-Button-1>"
        for w in (r, *r.winfo_children()):
            # не возвращаем "break": иначе tkdnd (bindtag после виджета) не увидит начало перетаскивания
            w.bind("<Button-1>", lambda e: self.dl_click(path, "single"))
            w.bind("<ButtonRelease-1>", lambda e: self.dl_release(path))
            w.bind(multi, lambda e: self.dl_click(path, "toggle"))
            w.bind("<Shift-Button-1>", lambda e: self.dl_click(path, "range"))
            w.bind("<Double-Button-1>", lambda e: self.dl_open(d))
            w.bind("<Enter>", lambda e: hover(True))
            w.bind("<Leave>", lambda e: hover(False))
            for seq in plat.RIGHT_CLICK:
                w.bind(seq, lambda e: self.dl_menu(d, e.x_root, e.y_root))
            if self.dnd_ok:
                try:
                    dragout.enable_drag(w, lambda: self.dl_drag_paths(path))
                except Exception as ex:
                    plat.log(f"enable_drag: {type(ex).__name__}")
        # клавиатура: Tab/стрелки — по строкам, Enter — открыть, пробел — выделить, Menu/Shift+F10 — меню
        r.bind("<FocusIn>", lambda e: hover(True))
        r.bind("<FocusOut>", lambda e: hover(False))
        r.bind("<Return>", lambda e: self.dl_open(d))
        r.bind("<space>", lambda e: self.dl_click(path, "toggle"))
        r.bind("<Down>", lambda e: self.dl_focus_step(r, 1))
        r.bind("<Up>", lambda e: self.dl_focus_step(r, -1))
        for seq in ("<App>", "<Shift-F10>"):
            try:
                r.bind(seq, lambda e: self.dl_menu(d, r.winfo_rootx() + 40, r.winfo_rooty() + r.winfo_height()))
            except tk.TclError:  # на macOS нет клавиши App
                pass

    def dl_paint(self, row):
        """Цвет строки: выделена — фиолетовая, под курсором/в фокусе — светлее, иначе обычная."""
        if not row["frame"].winfo_exists():
            return
        sel = row["d"]["path"] in self.dl_sel
        color = shade(C["violet"], 0.5 if row["hover"] else 0.42) if sel else C["hover"] if row["hover"] else C["item"]
        for w in row["widgets"]:
            w.configure(bg=color)

    def dl_apply_sel(self):
        """Перекрасить строки и заголовок без перестройки — виджет под мышью должен жить, чтобы начался drag."""
        ui = self.dl_ui
        if not ui:
            return
        for row in ui.get("rows") or []:
            self.dl_paint(row)
        self.dl_head()

    def dl_head(self):
        ui = self.dl_ui
        total, q = len(self.dl_items), self.dl_query.get().strip()
        head = f"ЗАГРУЗКИ · {total}" if not q else f"ЗАГРУЗКИ · найдено {len(self.dl_visible())} из {total}"
        if self.dl_sel:
            head += f" · выделено {len(self.dl_sel)}"
        ui["head"].configure(text=head)

    def dl_focus_first(self):
        rows = (self.dl_ui or {}).get("rows") or []
        if rows:
            rows[0]["frame"].focus_set()
        return "break"

    def dl_focus_step(self, r, step):
        rows = [x["frame"] for x in (self.dl_ui or {}).get("rows") or []]
        if r in rows:
            i = rows.index(r) + step
            if 0 <= i < len(rows):
                rows[i].focus_set()
            elif i < 0 and self.dl_ui.get("entry"):
                self.dl_ui["entry"].focus_set()
        return "break"

    def dl_click(self, path, mode):
        """Выделение: клик — одна строка (по уже выделенной группе — группа остаётся, чтобы её можно было утащить),
        Ctrl/⌘+клик — добавить/убрать, Shift+клик — диапазон от прошлого клика."""
        if mode == "toggle":
            self.dl_sel ^= {path}
            self.dl_anchor = path
        elif mode == "range" and self.dl_anchor:
            paths = [d["path"] for d in self.dl_visible()]
            if self.dl_anchor in paths and path in paths:
                a, b = sorted((paths.index(self.dl_anchor), paths.index(path)))
                self.dl_sel = set(paths[a:b + 1])
        elif path in self.dl_sel and len(self.dl_sel) > 1:
            self.dl_pending = path  # группу сбросим на отпускании, если это был клик, а не перетаскивание
            return None
        else:
            self.dl_sel, self.dl_anchor = {path}, path
        self.dl_pending = None
        self.dl_apply_sel()
        return None  # без "break" — дальше по bindtags идёт tkdnd

    def dl_release(self, path):
        """Отпустили кнопку на строке группы без перетаскивания — выделенной остаётся только она.
        После настоящего drag ButtonRelease может не прийти — тогда группа просто остаётся выделенной."""
        if self.dl_pending == path:
            self.dl_sel, self.dl_anchor = {path}, path
            self.dl_apply_sel()
        self.dl_pending = None

    def dl_drag_paths(self, path):
        """Что тащим: всю выделенную группу, если строка в ней, иначе — только эту строку."""
        if path in self.dl_sel and len(self.dl_sel) > 1:
            return [d["path"] for d in self.dl_items if d["path"] in self.dl_sel]
        return [path]

    def dl_targets(self, d):
        return self.dl_drag_paths(d["path"]) if d["path"] in self.dl_sel else [d["path"]]

    def dl_menu(self, d, x, y):
        if d["path"] not in self.dl_sel:
            self.dl_sel, self.dl_anchor = {d["path"]}, d["path"]
            self.dl_render()
        paths = self.dl_targets(d)
        menu = tk.Menu(self.root, tearoff=0, bg=C["item"], fg=C["ink"], activebackground=C["hover"], activeforeground=C["ink"])
        menu.add_command(label="Открыть", command=lambda: self.dl_open(d))
        menu.add_command(label="Показать в папке", command=lambda: self.dl_reveal(d["path"]))
        menu.add_separator()
        menu.add_command(label="Копировать путь" if len(paths) == 1 else f"Копировать пути ({len(paths)})",
                         command=lambda: self.copy("\n".join(paths), "Путь скопирован"))
        menu.tk_popup(x, y)

    def dl_open(self, d):
        """Открыть программой по умолчанию. Без вопроса — только папки и белый список SAFE_OPEN_EXT;
        всё остальное open_path мог бы запустить — сначала «Запустить …?»."""
        name = d.get("name", "")
        ext = name.rsplit(".", 1)[-1].lower() if "." in name.strip(".") else ""
        if d.get("is_dir"):
            safe = not (sys.platform == "darwin" and ext in MAC_BUNDLE_EXT)
        else:
            safe = ext in SAFE_OPEN_EXT
        if not safe:
            return self.dl_confirm(d)
        self.dl_do_open(d["path"])

    def dl_do_open(self, path):
        fn = plat_fn("open_path")
        err = fn(path) if fn else "Открытие файлов недоступно"
        if err:
            self.flash(err, C["red"])
        else:
            self.flash(f"Открываю: {clip(Path(path).name, 50)}")

    def dl_reveal(self, path):
        fn = plat_fn("reveal_path")
        err = fn(path) if fn else "«Показать в папке» недоступно"
        if err:
            self.flash(err, C["red"])

    def dl_confirm(self, d):
        """Плашка над списком: «Запустить программу setup.exe?» — Запустить / Показать в папке / Отмена."""
        box = (self.dl_ui or {}).get("confirm")
        if not box or not box.winfo_exists():
            return
        for w in box.winfo_children():
            w.destroy()
        card = tk.Frame(box, bg=shade(C["accent"], 0.3))
        card.pack(fill="x", pady=(0, 6))
        bg = card["bg"]
        tk.Label(card, text=f"Запустить «{clip(d['name'], 34)}»?", bg=bg, fg=C["ink"], font=self.f_bold,
                 padx=10, pady=6).pack(side="left")

        def close():
            for w in box.winfo_children():
                w.destroy()
        cancel = self.button(card, "Отмена", close, small=True)
        cancel.pack(side="right", padx=(4, 8), pady=4)
        self.button(card, "Показать в папке", lambda: (close(), self.dl_reveal(d["path"])), small=True).pack(side="right", padx=4)
        self.button(card, "Запустить", lambda: (close(), self.dl_do_open(d["path"])), primary=True,
                    small=True).pack(side="right", padx=4)
        cancel.configure(takefocus=1)
        cancel.bind("<Return>", lambda e: close())
        cancel.focus_set()

    def dl_open_folder(self):
        try:
            folder = str(downloads.downloads_dir())
        except Exception:
            return self.flash("Папка «Загрузки» не найдена", C["red"])
        self.dl_do_open(folder)

    # ── Вкладка: сервер ──
    # ── Вкладка: сеть (шпаргалка сисадмина + серверы) ──
    def tab_net(self):
        """Сверху вниз: «Этот компьютер», «Проверка» (netinfo.py), карточки серверов (monitor.py); всё — в одной прокрутке."""
        area = self.scroll_area(self.body, key="net")
        tk.Frame(area, bg=C["bg"], height=int(8 * self.s)).pack(fill="x")
        if netinfo:
            self.net_build(area)
        if monitor:
            self.server_block(area)

    def net_head(self, parent, text, pady=(0, 4)):
        bar = tk.Frame(parent, bg=C["bg"])
        bar.pack(fill="x", pady=pady)
        tk.Label(bar, text=text, bg=C["bg"], fg=C["muted"], font=self.f_tiny).pack(side="left")
        return bar

    def net_button(self, parent, text, cmd, fg=None):
        """Маленькая кнопка с клавиатурой: Tab — фокус (рамка), Enter/пробел — нажать."""
        b = self.button(parent, text, cmd, small=True, fg=fg)
        b.configure(takefocus=1, highlightthickness=1, highlightbackground=parent["bg"], highlightcolor=C["accent"])
        if parent["bg"] == C["item"]:  # на карточке — своя подложка, иначе кнопка сливается с фоном
            b.configure(bg=C["line"])
            b.bind("<Leave>", lambda e: b.configure(bg=C["line"]))
        b.bind("<Return>", lambda e: cmd())
        b.bind("<space>", lambda e: cmd())
        return b

    def net_copy_label(self, parent, text, font, fg=None, value=None):
        """Значение, которое копируется кликом (или Enter/пробелом с клавиатуры)."""
        bg, fg, value = parent["bg"], fg or C["ink"], value or text
        lb = tk.Label(parent, text=text, bg=bg, fg=fg, font=font, cursor="hand2", takefocus=1, padx=2,
                      highlightthickness=1, highlightbackground=bg, highlightcolor=C["accent"])
        for seq in ("<Button-1>", "<Return>", "<space>"):
            lb.bind(seq, lambda e: self.copy(value, f"Скопировано: {clip(value, 48)}"))
        lb.bind("<Enter>", lambda e: lb.configure(fg=C["mint"]))
        lb.bind("<Leave>", lambda e: lb.configure(fg=fg))
        return lb

    def net_entry(self, parent, var, width=None):
        ent = tk.Entry(parent, textvariable=var, bg=C["field"], fg=C["ink"], insertbackground=C["ink"], relief="flat",
                       font=self.f_mono, highlightthickness=1, highlightbackground=C["line"], highlightcolor=C["accent"])
        if width:
            ent.configure(width=width)
        return ent

    def net_build(self, area):
        ui = self.net_ui = {}
        head = self.net_head(area, "ЭТОТ КОМПЬЮТЕР")
        self.net_button(head, "↻", lambda: self.net_refresh(manual=True)).pack(side="right")
        ui["state"] = tk.Label(head, text="", bg=C["bg"], fg=C["muted"], font=self.f_tiny, padx=8)
        ui["state"].pack(side="right")
        ui["local"] = tk.Frame(area, bg=C["item"])
        ui["local"].pack(fill="x")
        self.net_check_block(area)
        self.net_render_local()
        self.net_refresh()

    # «Этот компьютер»
    def net_refresh(self, manual=False):
        """local_info в фоне — при показе вкладки и по ↻, не чаще раза в NET_REFRESH_MIN секунд."""
        if not netinfo:
            return
        if self.net_loading:
            if manual:
                self.flash("Уже обновляю…", C["muted"])
            return
        if time.time() - self.net_info_req < NET_REFRESH_MIN:
            if manual:
                self.flash(f"Только что обновлено — не чаще раза в {NET_REFRESH_MIN} с", C["accent"])
            return
        self.net_loading, self.net_info_req = True, time.time()
        self.net_paint_state()

        def done(info, err):
            self.net_loading = False
            if err is not None:
                plat.log(f"netinfo.local_info: {type(err).__name__}")
                self.net_info_err = f"сведения о сети не получены ({type(err).__name__})"
            elif not isinstance(info, dict):
                self.net_info_err = "сведения о сети не получены"
            else:
                self.net_info, self.net_info_err = info, None
            self.net_render_local()
        self.run_bg(netinfo.local_info, done)

    def net_paint_state(self):
        ui = self.net_ui
        if ui and ui["state"].winfo_exists():
            ui["state"].configure(text="обновляю…" if self.net_loading else "")

    def net_render_local(self):
        ui = self.net_ui
        if not ui or not ui["local"].winfo_exists():
            return
        self.net_paint_state()
        box = ui["local"]
        for w in box.winfo_children():
            w.destroy()
        info = self.net_info
        if info is None:
            err = self.net_info_err
            tk.Label(box, text=err or "Собираю сведения о сети…", bg=C["item"], fg=C["red"] if err else C["muted"],
                     font=self.f_small, anchor="w", padx=10, pady=10).pack(fill="x")
            return
        top = tk.Frame(box, bg=C["item"])
        top.pack(fill="x", padx=10, pady=(8, 2))
        left = tk.Frame(top, bg=C["item"])
        left.pack(side="left", anchor="nw")
        tk.Label(left, text=clip(info.get("hostname") or "имя компьютера неизвестно", 40), bg=C["item"], fg=C["ink"],
                 font=self.f_bold, padx=2).pack(anchor="w")
        ip = info.get("primary_ip")
        if ip:
            self.net_copy_label(left, ip, self.f_num, C["accent"]).pack(anchor="w")
            hint = "основной IP · клик — скопировать"
        else:
            tk.Label(left, text="нет подключения", bg=C["item"], fg=C["red"], font=self.f_num, padx=2).pack(anchor="w")
            hint = "исходящего интерфейса нет"
        tk.Label(left, text=hint, bg=C["item"], fg=C["muted"], font=self.f_tiny, padx=2).pack(anchor="w")
        ui["pub"] = tk.Frame(top, bg=C["item"])
        ui["pub"].pack(side="right", anchor="ne")
        self.net_render_pub()

        active = [i for i in info.get("interfaces") or [] if isinstance(i, dict) and i.get("up")]
        dns = [d for d in info.get("dns") or [] if isinstance(d, str)]
        rows = [("Шлюз", info.get("gateway"), info.get("gateway")),
                ("DNS", ", ".join(dns[:3]) + (f" +{len(dns) - 3}" if len(dns) > 3 else ""), ", ".join(dns))]
        wifi = next((i for i in active if i.get("kind") == "wifi"), None)
        if wifi:
            rows.append(("Wi-Fi", wifi.get("ssid") or "", wifi.get("ssid")))
        grid = tk.Frame(box, bg=C["item"])
        grid.pack(fill="x", padx=10, pady=(6, 2))
        for r, (name, text, value) in enumerate(rows):
            tk.Label(grid, text=name, bg=C["item"], fg=C["muted"], font=self.f_small, anchor="w").grid(
                row=r, column=0, sticky="w", padx=(2, 12))
            if value:
                self.net_copy_label(grid, text, self.f_mono, value=value).grid(row=r, column=1, sticky="w")
            else:
                empty = "подключено, имя сети не определено" if name == "Wi-Fi" else "—"
                tk.Label(grid, text=empty, bg=C["item"], fg=C["muted"], font=self.f_small).grid(row=r, column=1, sticky="w")
        self.net_ifaces(box, active)
        err = info.get("err") or self.net_info_err
        if err:
            tk.Label(box, text=clip(str(err), 90), bg=C["item"], fg=C["red"], font=self.f_tiny, anchor="w",
                     padx=12).pack(fill="x", pady=(0, 6))

    def net_ifaces(self, box, active):
        """Активные интерфейсы в одну строку каждый: имя, тип, IPv4 (клик — скопировать)."""
        tk.Label(box, text="АКТИВНЫЕ ИНТЕРФЕЙСЫ", bg=C["item"], fg=C["muted"], font=self.f_tiny, anchor="w",
                 padx=12).pack(fill="x", pady=(6, 0))
        lst = tk.Frame(box, bg=C["item"])
        lst.pack(fill="x", padx=10, pady=(0, 8))
        if not active:
            tk.Label(lst, text="активных интерфейсов нет", bg=C["item"], fg=C["muted"], font=self.f_small,
                     padx=2).grid(row=0, column=0, sticky="w")
            return
        shown = active[:8]
        for r, it in enumerate(shown):
            tk.Label(lst, text=clip(str(it.get("name") or "?"), 30), bg=C["item"], fg=C["ink"], font=self.f_small,
                     anchor="w", padx=2).grid(row=r, column=0, sticky="w")
            tk.Label(lst, text=NET_KIND.get(it.get("kind"), "другое"), bg=C["item"], fg=C["violet"], font=self.f_tiny,
                     anchor="w").grid(row=r, column=1, sticky="w", padx=10)
            v4 = [a for a in it.get("ipv4") or [] if isinstance(a, str)]
            cell = tk.Frame(lst, bg=C["item"])
            cell.grid(row=r, column=2, sticky="w")
            if v4:
                self.net_copy_label(cell, v4[0], self.f_mono).pack(side="left")
                if len(v4) > 1:
                    tk.Label(cell, text=f"+{len(v4) - 1}", bg=C["item"], fg=C["muted"], font=self.f_tiny).pack(side="left")
            else:
                tk.Label(cell, text="нет IPv4", bg=C["item"], fg=C["muted"], font=self.f_tiny).pack(side="left")
        if len(active) > len(shown):
            tk.Label(lst, text=f"и ещё {len(active) - len(shown)}", bg=C["item"], fg=C["muted"], font=self.f_tiny,
                     padx=2).grid(row=len(shown), column=0, sticky="w")

    def net_render_pub(self):
        """Внешний IP: только по кнопке (запрос к api.ipify.org), удачный ответ кешируется на PUBLIC_IP_TTL."""
        ui = self.net_ui
        if not ui or "pub" not in ui or not ui["pub"].winfo_exists():
            return
        box = ui["pub"]
        for w in box.winfo_children():
            w.destroy()
        tk.Label(box, text="внешний IP", bg=C["item"], fg=C["muted"], font=self.f_tiny).pack(anchor="e")
        ip, at = self.net_pub
        if self.net_pub_loading:
            tk.Label(box, text="запрашиваю…", bg=C["item"], fg=C["muted"], font=self.f_mono).pack(anchor="e")
        elif ip and time.time() - at < PUBLIC_IP_TTL:
            self.net_copy_label(box, ip, self.f_mono).pack(anchor="e")
            tk.Label(box, text=f"получен в {datetime.fromtimestamp(at):%H:%M}", bg=C["item"], fg=C["muted"],
                     font=self.f_tiny).pack(anchor="e")
        else:
            self.net_button(box, "показать" if not self.net_pub_err else "повторить", self.net_public).pack(anchor="e")
            note, col = ("не удалось получить", C["red"]) if self.net_pub_err else ("запрос к api.ipify.org", C["muted"])
            tk.Label(box, text=note, bg=C["item"], fg=col, font=self.f_tiny).pack(anchor="e")

    def net_public(self):
        if not netinfo or self.net_pub_loading:
            return
        ip, at = self.net_pub
        if ip and time.time() - at < PUBLIC_IP_TTL:
            return self.net_render_pub()
        self.net_pub_loading, self.net_pub_err = True, False
        self.net_render_pub()

        def done(res, err):
            self.net_pub_loading = False
            if err is None and isinstance(res, str) and res:
                self.net_pub = (res, time.time())
            else:
                self.net_pub_err = True
                if err is not None:
                    plat.log(f"netinfo.public_ip: {type(err).__name__}")
            self.net_render_pub()
        self.run_bg(lambda: netinfo.public_ip(timeout=4), done)

    # «Проверка»
    def net_check_block(self, area):
        ui = self.net_ui
        self.net_head(area, "ПРОВЕРКА", pady=(12, 4))
        box = tk.Frame(area, bg=C["item"])
        box.pack(fill="x")
        row = tk.Frame(box, bg=C["item"])
        row.pack(fill="x", padx=10, pady=(10, 4))
        tk.Label(row, text="адрес", bg=C["item"], fg=C["muted"], font=self.f_small).pack(side="left")
        ent = self.net_entry(row, self.net_addr)
        ent.pack(side="left", fill="x", expand=True, ipady=3, padx=(6, 12))
        tk.Label(row, text="порт", bg=C["item"], fg=C["muted"], font=self.f_small).pack(side="left")
        port = self.net_entry(row, self.net_port, width=6)
        port.pack(side="left", ipady=3, padx=(6, 0))
        ent.bind("<Return>", lambda e: self.net_run("ping"))
        port.bind("<Return>", lambda e: self.net_run("port"))
        ui["entry"], ui["port"] = ent, port

        btns = tk.Frame(box, bg=C["item"])
        btns.pack(fill="x", padx=10, pady=4)
        ui["btns"] = {}
        for kind, text in NET_ACTIONS:
            b = self.net_button(btns, text, lambda k=kind: self.net_run(k))
            b.pack(side="left", padx=(0, 6))
            ui["btns"][kind] = b
        self.net_button(btns, "⧉ копировать", self.net_copy_out, fg=C["muted"]).pack(side="right")
        ui["hist"] = tk.Frame(box, bg=C["item"])
        ui["hist"].pack(fill="x", padx=10, pady=(2, 4))
        self.net_render_hist()

        out = tk.Text(box, height=8, bg=C["field"], fg=C["ink"], font=self.f_mono, relief="flat", wrap="word",
                      padx=8, pady=6, highlightthickness=1, highlightbackground=C["line"], highlightcolor=C["accent"],
                      insertbackground=C["ink"], selectbackground=C["hover"], takefocus=1, cursor="xterm")
        for tag, col in (("ok", C["mint"]), ("warn", C["accent"]), ("bad", C["red"]), ("dim", C["muted"]),
                         ("cmd", C["violet"])):
            out.tag_configure(tag, foreground=col)
        out.pack(fill="x", padx=10, pady=(4, 10))

        def wheel(e):  # длинный вывод крутим сам, короткий — колесо уходит общей прокрутке вкладки
            lo, hi = out.yview()
            if hi - lo < 0.999:
                out.yview_scroll(plat.wheel_units(e.delta), "units")
                return "break"
        out.bind("<MouseWheel>", wheel)
        ui["out"] = out
        self.net_out_redraw()
        self.net_paint_buttons()

    def net_render_hist(self):
        """Последние адреса — «чипы»: клик (Enter/пробел) подставляет адрес в поле."""
        ui = self.net_ui
        if not ui or "hist" not in ui or not ui["hist"].winfo_exists():
            return
        box = ui["hist"]
        for w in box.winfo_children():
            w.destroy()
        if not self.net_hist:
            tk.Label(box, text="здесь появятся последние адреса", bg=C["item"], fg=C["muted"], font=self.f_tiny).pack(side="left")
            return
        tk.Label(box, text="недавние:", bg=C["item"], fg=C["muted"], font=self.f_tiny).pack(side="left", padx=(0, 4))
        for h in self.net_hist:
            chip = tk.Label(box, text=clip(h, 22), bg=C["field"], fg=C["ink"], font=self.f_tiny, padx=8, pady=1,
                            cursor="hand2", takefocus=1, highlightthickness=1, highlightbackground=C["item"],
                            highlightcolor=C["accent"])
            for seq in ("<Button-1>", "<Return>", "<space>"):
                chip.bind(seq, lambda e, a=h: self.net_pick(a))
            chip.pack(side="left", padx=(0, 4))

    def net_pick(self, addr):
        self.net_addr.set(addr)
        ui = self.net_ui
        if ui and ui["entry"].winfo_exists():
            ui["entry"].focus_set()
            ui["entry"].icursor("end")

    def net_push_hist(self, host):
        self.net_hist = [host] + [h for h in self.net_hist if h.lower() != host.lower()][:NET_HIST_MAX - 1]
        self.net_render_hist()

    def net_focus(self, key):
        ui = self.net_ui
        if ui and ui[key].winfo_exists():
            ui[key].focus_set()

    def net_host(self):
        """Адрес из поля или None (с подсказкой). Вставленный URL сводится к имени хоста.
        Полную проверку (допустимые символы и т.п.) делает netinfo — его ошибку покажем в выводе."""
        raw = self.net_addr.get().strip()
        if "://" in raw:
            try:
                raw = urllib.parse.urlsplit(raw).hostname or ""
            except ValueError:
                raw = ""
        if not raw:
            self.flash("Введи адрес: имя хоста или IP", C["red"])
            self.net_focus("entry")
            return None
        if len(raw) > 253 or any(c.isspace() for c in raw):
            self.flash("Адрес — без пробелов, не длиннее 253 символов", C["red"])
            self.net_focus("entry")
            return None
        if raw != self.net_addr.get():
            self.net_addr.set(raw)
        return raw

    def net_port_value(self):
        s = self.net_port.get().strip()
        if not (s.isascii() and s.isdigit() and 1 <= int(s) <= 65535):
            self.flash("Порт — число от 1 до 65535", C["red"])
            self.net_focus("port")
            return None
        return int(s)

    def net_run(self, kind):
        """Ping / Trace / Порт / DNS — в фоне; одновременно идёт одна проверка. Trace во время работы — «Стоп»."""
        if not netinfo:
            return
        if self.net_busy:
            if kind == "trace" and self.net_busy == "trace":
                return self.net_stop()
            name = dict(NET_ACTIONS).get(self.net_busy, self.net_busy)
            return self.flash(f"Подожди: идёт {name}" + (" — его можно остановить" if self.net_busy == "trace" else ""),
                              C["accent"])
        host = self.net_host()
        if host is None:
            return
        port = self.net_port_value() if kind == "port" else None
        if kind == "port" and port is None:
            return
        self.net_push_hist(host)
        self.net_busy = kind
        self.net_log = []
        self.net_paint_buttons()
        if kind == "ping":
            self.net_out(f"$ ping {host}", "cmd")
            self.run_bg(lambda: netinfo.ping(host, count=4), self.net_ping_done)
        elif kind == "trace":
            self.net_out(f"$ traceroute {host}", "cmd")
            self.net_cancel = cancel = threading.Event()
            # on_hop зовётся из фонового потока — в Tk только через очередь (её разбирает tick_queue)
            on_hop = lambda d: self.q.put(("call", self.net_hop, d, None))
            self.run_bg(lambda: netinfo.traceroute(host, max_hops=20, on_hop=on_hop, cancel=cancel), self.net_trace_done)
        elif kind == "port":
            self.net_out(f"$ порт {f'[{host}]' if ':' in host else host}:{port}", "cmd")
            self.run_bg(lambda: netinfo.check_port(host, port, timeout=3), self.net_port_done)
        else:
            self.net_out(f"$ dns {host}", "cmd")
            self.run_bg(lambda: netinfo.dns_lookup(host), self.net_dns_done)

    def net_stop(self):
        if self.net_cancel is not None:
            self.net_cancel.set()
            self.net_paint_buttons()

    def net_finish(self):
        self.net_busy = None
        self.net_paint_buttons()

    def net_paint_buttons(self):
        ui = self.net_ui
        if not ui or "btns" not in ui:
            return
        for kind, b in ui["btns"].items():
            if not b.winfo_exists():
                continue
            text, fg = dict(NET_ACTIONS)[kind], C["ink"]
            if kind == "trace" and self.net_busy == "trace":
                stopping = self.net_cancel is not None and self.net_cancel.is_set()
                text, fg = ("останавливаю…", C["muted"]) if stopping else ("■ Стоп", C["red"])
            elif kind == self.net_busy:
                text, fg = text + " …", C["accent"]
            elif self.net_busy:
                fg = C["muted"]
            b.configure(text=text, fg=fg)

    def net_result_ok(self, res, err, what):
        """Сбой вызова (исключение / не тот формат) — строка в вывод; True — результат можно разбирать."""
        if err is not None:
            plat.log(f"netinfo.{what}: {type(err).__name__}")
            self.net_out(f"сбой проверки: {type(err).__name__}", "bad")
            return False
        if not isinstance(res, dict):
            self.net_out("нет результата", "bad")
            return False
        return True

    @staticmethod
    def net_ms(v):
        if not isinstance(v, (int, float)):
            return "?"
        return f"{v:.0f}" if v >= 10 or float(v).is_integer() else f"{v:.1f}"

    def net_ping_done(self, res, err):
        self.net_finish()
        if not self.net_result_ok(res, err, "ping"):
            return
        sent, got, e = res.get("sent") or 0, res.get("received") or 0, res.get("err")
        if e and not got and e != "нет ответа":  # адрес не принят / имя не найдено / нет утилиты
            return self.net_out(f"ошибка: {e}", "bad")
        loss = res.get("loss_pct")
        if not isinstance(loss, (int, float)):
            loss = (sent - got) * 100 / sent if sent else 100
        line = f"{got}/{sent}, {self.net_ms(loss)}% потерь"
        if got:
            line += f", мин/ср/макс {self.net_ms(res.get('min'))}/{self.net_ms(res.get('avg'))}/{self.net_ms(res.get('max'))} мс"
        elif e:
            line += f" — {e}"
        self.net_out(line, "ok" if got and not loss else "warn" if got else "bad")

    def net_hop(self, d, _err):
        """Хоп traceroute — по мере поступления (из очереди, уже в потоке Tk)."""
        if not isinstance(d, dict):
            return
        if d.get("err"):
            return self.net_out(f"ошибка: {d['err']}", "bad")
        ms = d.get("ms")
        host = d.get("host") or d.get("ip") or "*"
        t = f"{self.net_ms(ms)} мс" if isinstance(ms, (int, float)) else "*"
        self.net_out(f"{d.get('hop', '?'):>2}  {host:<15}  {t}", None if d.get("ip") else "dim")

    def net_trace_done(self, hops, err):
        stopped = self.net_cancel is not None and self.net_cancel.is_set()
        self.net_cancel = None
        self.net_finish()
        if err is not None:
            plat.log(f"netinfo.traceroute: {type(err).__name__}")
            return self.net_out(f"сбой проверки: {type(err).__name__}", "bad")
        hops = [h for h in hops or [] if isinstance(h, dict)]
        n = sum(1 for h in hops if not h.get("err"))
        count = f"{n} {plural(n, 'хоп', 'хопа', 'хопов')}"
        if stopped:
            self.net_out(f"— остановлено · {count}", "warn")
        elif any(h.get("err") for h in hops):
            pass  # ошибка уже выведена через on_hop
        elif n:
            self.net_out(f"— готово · {count}", "dim")
        else:
            self.net_out("— ни одного хопа", "warn")

    def net_port_done(self, res, err):
        self.net_finish()
        if not self.net_result_ok(res, err, "check_port"):
            return
        if res.get("open"):
            ms = res.get("ms")
            return self.net_out("открыт" + (f" · {self.net_ms(ms)} мс" if isinstance(ms, (int, float)) else ""), "ok")
        e = str(res.get("err") or "нет ответа")
        self.net_out(e if e.startswith("закрыт") else f"закрыт ({e})", "bad")

    def net_dns_done(self, res, err):
        self.net_finish()
        if not self.net_result_ok(res, err, "dns_lookup"):
            return
        v4 = [a for a in res.get("ipv4") or [] if isinstance(a, str)]
        v6 = [a for a in res.get("ipv6") or [] if isinstance(a, str)]
        for a in v4:
            self.net_out(f"A     {a}", "ok")
        for a in v6:
            self.net_out(f"AAAA  {a}", "ok")
        if not v4 and not v6:
            self.net_out(f"ошибка: {res.get('err') or 'адресов нет'}", "bad")

    # окно вывода: строки хранятся в self.net_log и переживают перестройку вкладки
    def net_out_widget(self):
        out = self.net_ui.get("out") if self.net_ui else None
        return out if out is not None and out.winfo_exists() else None

    def net_out(self, text, tag=None):
        self.net_log.append((text, tag))
        out = self.net_out_widget()
        if len(self.net_log) > NET_LOG_MAX:
            del self.net_log[:-NET_LOG_MAX]
            return self.net_out_redraw()
        if out is None:
            return
        if len(self.net_log) == 1:  # в окне ещё подсказка
            return self.net_out_redraw()
        out.configure(state="normal")
        out.insert("end", "\n" + text, (tag,) if tag else ())
        out.configure(state="disabled")
        out.see("end")

    def net_out_redraw(self):
        out = self.net_out_widget()
        if out is None:
            return
        out.configure(state="normal")
        out.delete("1.0", "end")
        if not self.net_log:
            out.insert("end", NET_OUT_HINT, ("dim",))
        for i, (text, tag) in enumerate(self.net_log):
            out.insert("end", ("\n" if i else "") + text, (tag,) if tag else ())
        out.configure(state="disabled")
        out.see("end")

    def net_copy_out(self):
        if not self.net_log:
            return self.flash("Вывод пуст — сначала запусти проверку", C["accent"])
        self.copy("\n".join(t for t, _ in self.net_log), "Результат скопирован")

    def server_block(self, parent):
        """Карточки серверов (monitor.py) — нижняя часть вкладки «Сеть»."""
        ui = self.srv_ui = {"ago": []}
        servers = CONFIG["servers"]
        bar = tk.Frame(parent, bg=C["bg"])
        bar.pack(fill="x", pady=(14 if netinfo else 0, 4))
        if not servers:
            tk.Label(bar, text="СЕРВЕРЫ", bg=C["bg"], fg=C["muted"], font=self.f_tiny).pack(side="left")
            return self.server_empty(parent)
        tk.Label(bar, text=f"СЕРВЕРЫ · {len(servers)} · ping раз в 30 с, ssh раз в 2 мин", bg=C["bg"], fg=C["muted"],
                 font=self.f_tiny).pack(side="left")
        self.button(bar, "↻ обновить", self.server_refresh, small=True).pack(side="right")
        ui["list"] = tk.Frame(parent, bg=C["bg"])
        ui["list"].pack(fill="x")
        self.srv_sig = None
        self.server_render()

    def server_empty(self, parent):
        box = tk.Frame(parent, bg=C["bg"])
        box.pack(fill="x", pady=(4, 10))
        tk.Label(box, text="Серверов для мониторинга нет", bg=C["bg"], fg=C["ink"], font=self.f_track).pack(anchor="w")
        tk.Label(box, text="Добавь в config.json рядом с панелью список \"servers\" — панель подхватит его сама. "
                 "\"ssh\" необязателен: без него сервер только пингуется; с ним (вход по ключу) видны "
                 "загрузка, память, диски и RAID.", bg=C["bg"], fg=C["muted"], font=self.f_small, justify="left",
                 wraplength=int(520 * self.s)).pack(anchor="w", pady=(6, 10))
        example = ('"servers": [\n'
                   '  {"name": "файл-сервер", "host": "192.0.2.10", "ssh": "admin@192.0.2.10"},\n'
                   '  {"name": "роутер", "host": "192.0.2.1"}\n'
                   ']')
        tk.Label(box, text=example, bg=C["field"], fg=C["mint"], font=self.f_mono, justify="left", anchor="w",
                 padx=12, pady=10).pack(fill="x")
        row = tk.Frame(box, bg=C["bg"])
        row.pack(fill="x", pady=12)
        self.button(row, "Открыть config.json", self.open_config, primary=True).pack(side="left")
        self.button(row, "Скопировать пример", lambda: self.copy(example, "Пример скопирован"), small=True).pack(side="left", padx=8)
        if not monitor:
            tk.Label(box, text="модуль monitor.py недоступен", bg=C["bg"], fg=C["red"], font=self.f_tiny).pack(anchor="w")

    def open_config(self):
        """Открыть config.json в VS Code или системном редакторе; если его нет — создать из config.example.json.

        Не через dl_do_open/os.startfile: у .json в Windows часто нет связанной программы.
        """
        path = CONFIG_PATH
        if not path.exists():
            try:
                example = HERE / "config.example.json"
                text = example.read_text(encoding="utf-8") if example.exists() else '{\n  "servers": []\n}\n'
                path.write_text(text, encoding="utf-8")
            except OSError as e:
                return self.flash(f"Не удалось создать config.json ({type(e).__name__})", C["red"])
        err, how = open_text_file(str(path))
        if err:
            return self.flash(err, C["red"], action=("Показать в папке", lambda: self.dl_reveal(str(path))))
        self.flash(f"Открываю config.json: {how} · изменения подхватятся сами")

    # ── Горячая перезагрузка config.json ──
    @staticmethod
    def config_mtime():
        try:
            return CONFIG_PATH.stat().st_mtime_ns
        except OSError:
            return None

    def tick_config(self):
        m = self.config_mtime()
        if m != self.cfg_mtime:
            self.cfg_mtime = m
            try:
                self.reload_config()
            except Exception as e:
                plat.log(f"config.json: перезагрузка — {type(e).__name__}: {e}")
        self.root.after(CONFIG_TICK, self.tick_config)

    def reload_config(self):
        """Перечитать config.json. Битый JSON — сообщение, прежние настройки остаются.

        servers — Monitor пересоздаётся; notify — сразу (читается при каждом уведомлении); main_agent_name —
        при следующей перерисовке офиса; hotkey — только после перезапуска; monitors — секцию пишет сам
        MonitorsCtl (save_layout), здесь только обновляется копия.
        """
        global MAIN_AGENT
        errors = []
        new = load_config(errors)
        if errors:
            return self.flash(f"config.json: {errors[0]} — оставлены прежние настройки", C["red"])
        old = dict(CONFIG)
        CONFIG.clear()
        CONFIG.update(new)
        changed = [k for k in ("main_agent_name", "notify", "servers") if old.get(k) != new.get(k)]
        if old.get("servers") != new.get("servers"):
            self.monitor_restart()
        MAIN_AGENT = CONFIG["main_agent_name"]
        if old.get("hotkey") != new.get("hotkey"):
            CONFIG["hotkey"] = old.get("hotkey")  # поток горячей клавиши уже запущен (или нет) — до перезапуска как было
            self.flash("config.json: hotkey применится после перезапуска панели", C["accent"])
        elif changed:
            names = {"main_agent_name": "имя агента", "notify": "уведомления", "servers": "серверы"}
            self.flash("config.json перечитан: " + ", ".join(names[k] for k in changed))

    def monitor_restart(self):
        """Серверы в config изменились: старый Monitor — stop, новый — start; вкладка «Сеть» перестроится."""
        if self.monitor:
            try:
                self.monitor.stop()
            except Exception as e:
                plat.log(f"monitor.stop: {type(e).__name__}")
        self.monitor, self.monitor_err = None, None
        if monitor and CONFIG["servers"]:
            try:
                self.monitor = monitor.Monitor(CONFIG["servers"])
                self.monitor.start()
            except Exception as e:
                self.monitor, self.monitor_err = None, f"мониторинг не запустился ({type(e).__name__})"
                plat.log(self.monitor_err)
        self.srv_rebuild = True  # перестроить «Сеть», когда там не идёт проверка (tick_server)

    def server_refresh(self):
        if self.monitor:
            try:
                self.monitor.refresh()
            except Exception as e:
                plat.log(f"monitor.refresh: {type(e).__name__}")
        self.flash("Проверяю серверы…")

    def server_snapshot(self):
        if not self.monitor:
            return None
        try:
            return self.monitor.snapshot()
        except Exception as e:
            plat.log(f"monitor.snapshot: {type(e).__name__}")
            return None

    def server_render(self):
        """Перестроить карточки, только если что-то кроме времени проверки поменялось."""
        ui = self.srv_ui
        if not ui or "list" not in ui or not ui["list"].winfo_exists():
            return
        snap = self.server_snapshot()
        sig = repr([{k: v for k, v in x.items() if k != "checked"} | (
            {"ssh": {k: v for k, v in x["ssh"].items() if k != "checked"}} if isinstance(x.get("ssh"), dict) else {})
            for x in snap]) if snap is not None else ("err", self.monitor_err)
        if sig == self.srv_sig:
            for lb, ts, prefix in ui["ago"]:
                if lb.winfo_exists():
                    lb.configure(text=prefix + secs_ago(ts))
            return
        self.srv_sig = sig
        lst = ui["list"]
        for w in lst.winfo_children():
            w.destroy()
        ui["ago"] = []
        if snap is None:
            tk.Label(lst, text=self.monitor_err or "мониторинг недоступен", bg=C["bg"], fg=C["red"], font=self.f_small,
                     pady=12).pack()
            return
        if not snap:
            tk.Label(lst, text="Ни один сервер из config.json не принят.", bg=C["bg"], fg=C["muted"], font=self.f_small,
                     pady=12).pack()
        for x in snap:
            self.server_card(lst, x)

    def server_card(self, lst, x):
        card = tk.Frame(lst, bg=C["item"])
        card.pack(fill="x", pady=3)
        top = tk.Frame(card, bg=C["item"])
        top.pack(fill="x", padx=10, pady=(8, 2))
        up = x.get("up")
        dot = C["mint"] if up is True else C["red"] if up is False else C["muted"]
        state = "в сети" if up is True else "не отвечает" if up is False else "неизвестно"
        tk.Label(top, text="●", bg=C["item"], fg=dot, font=self.f_base).pack(side="left")
        tk.Label(top, text=x.get("name") or "сервер", bg=C["item"], fg=C["ink"], font=self.f_bold, padx=6).pack(side="left")
        tk.Label(top, text=x.get("host") or "", bg=C["item"], fg=C["muted"], font=self.f_mono).pack(side="left")
        when = tk.Label(top, text=secs_ago(x.get("checked")), bg=C["item"], fg=C["muted"], font=self.f_tiny)
        when.pack(side="right")
        self.srv_ui["ago"].append((when, x.get("checked"), ""))
        ms = x.get("ms")
        ping = f"{ms:.0f} мс" if isinstance(ms, (int, float)) and up else state
        tk.Label(top, text=ping, bg=C["item"], fg=dot if up is not None else C["muted"], font=self.f_mono,
                 padx=10).pack(side="right")
        body = tk.Frame(card, bg=C["item"])
        body.pack(fill="x", padx=(28, 10), pady=(0, 8))
        if x.get("err"):
            tk.Label(body, text=x["err"], bg=C["item"], fg=C["red"], font=self.f_tiny, anchor="w").pack(fill="x")
        ssh = x.get("ssh")
        if not isinstance(ssh, dict):
            if ssh is None and not x.get("err"):
                tk.Label(body, text="только ping (ssh не настроен или ещё не опрошен)", bg=C["item"], fg=C["muted"],
                         font=self.f_tiny, anchor="w").pack(fill="x")
            return
        if not ssh.get("ok"):
            tk.Label(body, text=f"ssh: {clip(str(ssh.get('err') or 'нет данных'), 90)}", bg=C["item"], fg=C["red"],
                     font=self.f_tiny, anchor="w").pack(fill="x")
            return
        parts = []
        load = ssh.get("load")
        if isinstance(load, (list, tuple)) and len(load) >= 3:
            parts.append("загрузка " + " ".join(f"{v:.2f}" if isinstance(v, (int, float)) else str(v) for v in load[:3]))
        mem = ssh.get("mem_pct")
        if isinstance(mem, (int, float)):
            parts.append(f"память {mem:.0f}%")
        if ssh.get("uptime"):
            parts.append(f"работает {fmt_uptime(ssh['uptime'])}")
        if parts:
            tk.Label(body, text=" · ".join(parts), bg=C["item"], fg=C["ink"], font=self.f_small, anchor="w").pack(fill="x")
        for disk in ssh.get("disks") or []:
            self.disk_bar(body, disk)
        raid = ssh.get("raid")
        if raid:
            bad = str(raid).lower() != "ok"
            tk.Label(body, text=f"RAID: {clip(str(raid), 80)}", bg=C["item"], fg=C["red"] if bad else C["mint"],
                     font=self.f_bold if bad else self.f_small, anchor="w").pack(fill="x", pady=(2, 0))
        if ssh.get("err"):
            tk.Label(body, text=f"ssh: {clip(str(ssh['err']), 90)}", bg=C["item"], fg=C["muted"], font=self.f_tiny,
                     anchor="w").pack(fill="x")

    def disk_bar(self, parent, disk):
        """Диск: точка монтирования, полоса заполнения (жёлтая > 75 %, красная > 90 %), процент и свободное место."""
        if not isinstance(disk, dict):
            return
        line = tk.Frame(parent, bg=C["item"])
        line.pack(fill="x", pady=1)
        pct = disk.get("pct")
        pct = float(pct) if isinstance(pct, (int, float)) else None
        col = C["muted"] if pct is None else C["red"] if pct > 90 else C["accent"] if pct > 75 else C["mint"]
        tk.Label(line, text=clip(str(disk.get("mount") or "?"), 14), bg=C["item"], fg=C["muted"], font=self.f_mono,
                 width=14, anchor="w").pack(side="left")
        bw, bh = int(220 * self.s), int(7 * self.s)
        bar = tk.Canvas(line, width=bw, height=bh, bg=C["item"], highlightthickness=0)
        bar.pack(side="left", padx=6)
        bar.create_rectangle(0, 0, bw, bh, fill=C["field"], width=0)
        if pct is not None:
            bar.create_rectangle(0, 0, int(bw * min(1, max(0, pct) / 100)), bh, fill=col, width=0)
        free = fmt_bytes(disk.get("free"))
        txt = (f"{pct:.0f}%" if pct is not None else "—") + (f" · свободно {free}" if free else "")
        tk.Label(line, text=txt, bg=C["item"], fg=col if pct is not None and pct > 75 else C["ink"], font=self.f_tiny).pack(side="left")

    # ── Показ и скрытие ──
    # ── Вкладка: мониторы ──
    # Схема — layout_editor.py; модель и геометрия — layout.py; всё остальное — только через MonitorsCtl
    # (monitors_ctl.py). Контракты — docs/monitors-spec.md §5, вёрстка — §15.1.
    def mon_init(self, autostart):
        """Создать MonitorsCtl, если бэкенд на месте. Нет модулей или конструктор упал — вкладка покажет причину."""
        self.mon_save_warn, self.mon_paste_armed, self.mon_answered = [], 0.0, set()  # answered — токены отката
        self.mon_df_rejected = None  # отпечаток, которому сказали «не совпадает» (плашку больше не показываем)
        if not (layout and monitors_ctl and layout_editor):
            return
        data_dir = getattr(plat, "DATA_DIR", None)
        if data_dir is None:
            self.mon_err = "в platform-слое нет DATA_DIR (блок «Мониторы» ещё не добавлен)"
            plat.log(f"monitors: {self.mon_err}")
            return
        try:
            self.mon_ctl = monitors_ctl.MonitorsCtl(
                CONFIG["monitors"], CONFIG_PATH, Path(data_dir) / "monitors", plat,
                on_event=lambda kind, payload: self.q.put(("monitors", kind, payload)))
        except Exception as e:
            self.mon_err = f"не запустились ({type(e).__name__})"
            plat.log(f"monitors: MonitorsCtl — {type(e).__name__}: {e}")
            return
        if autostart:
            try:
                self.mon_ctl.start()
            except Exception as e:
                plat.log(f"monitors: start — {type(e).__name__}: {e}")

    def mon_call(self, name, *args):
        """Метод MonitorsCtl без исключений наружу: сбой — словарь ошибки, как в §2.3."""
        fn = getattr(self.mon_ctl, name, None) if self.mon_ctl else None
        if not callable(fn):
            return {"ok": False, "code": "not_found", "err": "функция недоступна в этой версии"}
        try:
            return fn(*args)
        except Exception as e:
            plat.log(f"monitors: {name} — {type(e).__name__}: {e}")
            return {"ok": False, "code": "os_error", "err": f"внутренняя ошибка ({type(e).__name__})"}

    @staticmethod
    def mon_ok(res):
        return isinstance(res, dict) and res.get("ok") is True

    def mon_fail(self, res, what):
        err = res.get("err") if isinstance(res, dict) else None
        hint = res.get("hint") if isinstance(res, dict) else None
        self.flash(clip(f"{what}: {err or 'не получилось'}" + (f" — {hint}" if hint else ""), 110), C["red"])

    def mon_load_layout(self):
        lay = self.mon_call("layout")
        return lay if layout and isinstance(lay, layout.Layout) else None

    def mon_refresh_snap(self):
        snap = self.mon_call("snapshot")
        if isinstance(snap, dict) and snap.get("ok") is not False:
            self.mon_snap = snap

    def mon_this(self):
        """id этой машины (из snapshot), если она есть в черновике раскладки."""
        me = self.mon_snap.get("this_machine")
        lay = self.mon_draft
        return me if lay and any(m.id == me for m in lay.machines) else None

    def mon_screen(self, sid):
        lay = self.mon_draft
        return next((s for s in lay.screens if s.id == sid), None) if lay and sid else None

    def mon_machine_name(self, mid):
        lay = self.mon_draft
        m = next((m for m in lay.machines if m.id == mid), None) if lay else None
        return m.name if m else mid

    @staticmethod
    def mon_os_name():
        return "Windows" if sys.platform == "win32" else "macOS"

    # мелкие элементы в стиле вкладки «Сеть», но свои — блок «Сеть» не трогаем
    def mon_head(self, parent, text, pady=(0, 4)):
        bar = tk.Frame(parent, bg=C["bg"])
        bar.pack(fill="x", pady=pady)
        tk.Label(bar, text=text, bg=C["bg"], fg=C["muted"], font=self.f_tiny).pack(side="left")
        return bar

    def mon_button(self, parent, text, cmd, fg=None, primary=False):
        """Маленькая кнопка с клавиатурой: Tab — фокус (рамка), Enter/пробел — нажать."""
        b = self.button(parent, text, cmd, small=True, fg=fg, primary=primary, font=self.f_small if primary else None)
        b.configure(takefocus=1, highlightthickness=1, highlightbackground=parent["bg"], highlightcolor=C["ink"])
        if parent["bg"] == C["item"] and not primary:  # на карточке — своя подложка
            b.configure(bg=C["line"])
            b.bind("<Leave>", lambda e: b.configure(bg=C["line"]))
        b.bind("<Return>", lambda e: cmd())
        b.bind("<space>", lambda e: cmd())
        return b

    def mon_entry(self, parent, var, width=None):
        ent = tk.Entry(parent, textvariable=var, bg=C["field"], fg=C["ink"], insertbackground=C["ink"], relief="flat",
                       font=self.f_mono, highlightthickness=1, highlightbackground=C["line"], highlightcolor=C["accent"],
                       disabledbackground=C["item"], disabledforeground=C["muted"])
        if width:
            ent.configure(width=width)
        return ent

    def mon_note(self, parent, text, fg=None, font=None):
        lb = tk.Label(parent, text=text, bg=parent["bg"], fg=fg or C["muted"], font=font or self.f_tiny, anchor="w",
                      justify="left", wraplength=int(540 * self.s))
        lb.pack(fill="x")
        return lb

    def tab_monitors(self):
        area = self.scroll_area(self.body, key="monitors")
        tk.Frame(area, bg=C["bg"], height=int(8 * self.s)).pack(fill="x")
        self.mon_ui = {"area": area}
        miss = [f for f, m in (("layout.py", layout), ("monitors_ctl.py", monitors_ctl)) if m is None]
        if miss or not self.mon_ctl:
            return self.mon_unavailable(area, miss)
        self.mon_refresh_snap()
        if self.mon_draft is None and self.mon_wiz is None:
            self.mon_draft, self.mon_dirty = self.mon_load_layout(), False
            if self.mon_draft is None:  # секции monitors нет — мастер первого запуска
                self.mon_wiz = self.mon_wiz_new()
        if self.mon_wiz is not None:
            return self.mon_wizard(area)
        self.mon_build(area)

    def mon_unavailable(self, area, miss):
        box = tk.Frame(area, bg=C["item"])
        box.pack(fill="x")
        inner = tk.Frame(box, bg=C["item"])
        inner.pack(fill="x", padx=12, pady=10)
        if miss:
            self.mon_note(inner, "Модуль не найден", C["accent"], self.f_bold)
            self.mon_note(inner, f"Рядом с панелью нет: {', '.join(miss)}. Схема экранов и помощник курсора "
                                 "заработают, когда эти файлы появятся (обнови панель).", C["ink"], self.f_small)
        else:
            self.mon_note(inner, "Мониторы недоступны", C["accent"], self.f_bold)
            self.mon_note(inner, f"Модули на месте, но {self.mon_err or 'контроллер не создан'}. "
                                 "Подробности — в журнале панели (stderr).", C["ink"], self.f_small)

    # главный вид: режимы, схема, свойства экрана, кнопки, помощник
    def mon_build(self, area):
        ui = self.mon_ui
        ui["banner"] = tk.Frame(area, bg=C["bg"])
        ui["banner"].pack(fill="x")
        self.mon_paint_banner()
        self.mon_modes(area)
        if not self.mon_this():
            self.mon_pick_machine(area)
        self.mon_shared(area)
        head = self.mon_head(area, "РАСКЛАДКА — КАК НА СТОЛЕ", pady=(10, 4))
        ui["detect"] = self.mon_button(head, "Определить экраны", self.mon_detect)
        ui["detect"].pack(side="right")
        ui["dirty"] = tk.Label(head, text="", bg=C["bg"], fg=C["accent"], font=self.f_tiny, padx=8)
        ui["dirty"].pack(side="right")
        try:
            ed = layout_editor.LayoutEditor(area, colors=C, fonts=self.mon_fonts(), scale=self.s,
                                            on_change=self.mon_changed, on_select=self.mon_selected,
                                            on_drag=lambda on: self.mon_set_hold("drag", on), height=230)
        except Exception as e:
            plat.log(f"monitors: LayoutEditor — {type(e).__name__}: {e}")
            return self.mon_note(area, f"Схема не построилась ({type(e).__name__})", C["red"], self.f_small)
        ed.pack(fill="x")
        self.mon_editor = ed
        ed.set_layout(self.mon_draft)
        ed.select(self.mon_sel)
        self.mon_legend(area)
        self.mon_props(area)
        ui["warn"] = tk.Label(area, text="", bg=C["bg"], fg=C["accent"], font=self.f_tiny, anchor="w", justify="left",
                              wraplength=int(560 * self.s))
        ui["warn"].pack(fill="x", pady=(4, 0))
        row = tk.Frame(area, bg=C["bg"])
        row.pack(fill="x", pady=(6, 0))
        self.mon_button(row, "Сохранить", self.mon_save, primary=True).pack(side="left")
        self.mon_button(row, f"Применить к {self.mon_os_name()}", self.mon_apply_os).pack(side="left", padx=(6, 0))
        self.mon_button(row, "Вставить раскладку", self.mon_paste_layout).pack(side="right")
        self.mon_button(row, "Копировать раскладку", self.mon_copy_layout).pack(side="right", padx=(0, 6))
        self.mon_helper(area)
        tk.Frame(area, bg=C["bg"], height=int(8 * self.s)).pack(fill="x")
        self.mon_paint_dirty()
        self.mon_paint_warn()
        self.mon_paint_props()
        self.mon_paint_status()
        if self.mon_displays is None:  # EDID для кнопки «из EDID» и метки «курсор здесь»
            self.mon_fetch_displays(merge=False)

    def mon_fonts(self):
        return {"small": self.f_small, "tiny": self.f_tiny, "bold": self.f_bold}

    def mon_modes(self, area):
        """Полоса режимов: «Выкл» и «Общая мышь» (этап 2) нажимаются; «Ноут как монитор» — этап 3."""
        ui = self.mon_ui
        self.mon_head(area, "РЕЖИМЫ")
        row = tk.Frame(area, bg=C["bg"])
        row.pack(fill="x")
        ui["modes"] = {}
        for key, title in (("off", "Выкл"), ("shared", "Общая мышь (Deskflow)"), ("monitor", "Ноут как монитор")):
            cell = tk.Frame(row, bg=C["item"], highlightthickness=1, highlightbackground=C["item"])
            cell.pack(side="left", fill="both", expand=True, padx=(0 if key == "off" else 4, 0))
            soon = key == "monitor"
            t = tk.Label(cell, text=title + (" · скоро" if soon else ""), bg=C["item"], fg=C["ink"], font=self.f_small,
                         anchor="w", padx=8, pady=2)
            t.pack(fill="x", pady=(3, 0))
            sub = tk.Label(cell, text="", bg=C["item"], fg=C["muted"], font=self.f_tiny, anchor="w", justify="left",
                           padx=8, wraplength=int(170 * self.s))
            sub.pack(fill="x", pady=(0, 4))
            if not soon:
                cell.configure(cursor="hand2", takefocus=1, highlightcolor=C["ink"])
                for w in (cell, t, sub):
                    w.bind("<Button-1>", lambda e, k=key: self.mon_pick_mode(k))
                for seq in ("<Return>", "<space>"):
                    cell.bind(seq, lambda e, k=key: self.mon_pick_mode(k))
            ui["modes"][key] = (cell, t, sub)
        ui["mode_line"] = tk.Label(area, text="", bg=C["bg"], fg=C["muted"], font=self.f_tiny, anchor="w",
                                   justify="left", wraplength=int(540 * self.s))
        ui["mode_line"].pack(fill="x", pady=(2, 0))

    def mon_pick_mode(self, key, server=None):
        snap = self.mon_snap
        if snap.get("busy"):
            return self.flash("Подожди — предыдущая команда ещё выполняется", C["accent"])
        if key == "shared":
            comp = (snap.get("components") or {}).get("deskflow") or {}
            if comp.get("state") not in ("ok", None):
                cmd = comp.get("cmd")
                return self.flash(f"Deskflow {self.mon_comp('deskflow')[0]}" + (f" — поставь: {cmd}" if cmd else ""),
                                  C["accent"])
            if self.mon_dirty:
                return self.flash("Сначала «Сохранить» раскладку — Deskflow берёт сохранённую", C["accent"])
            if not self.mon_this():
                return self.flash("Сначала выбери, какой компьютер на схеме — этот", C["accent"])
        elif snap.get("mode") == key:
            return
        res = self.mon_call("set_mode", key, server) if key == "shared" else self.mon_call("set_mode", key)
        if not self.mon_ok(res):
            return self.mon_fail(res, "Режим не включён" if key == "shared" else "Режим не выключен")
        self.mon_refresh_snap()
        self.mon_paint_status()
        self.flash("Включаю общую мышь…" if key == "shared" else "Общая мышь выключена")

    def mon_shared(self, area):
        """Блок «Общая мышь» (§8.2): статус Deskflow, главный компьютер, отпечаток и доверие (TLS)."""
        ui = self.mon_ui
        self.mon_head(area, "ОБЩАЯ МЫШЬ И КЛАВИАТУРА", pady=(10, 4))
        box = tk.Frame(area, bg=C["item"])
        box.pack(fill="x")
        inner = tk.Frame(box, bg=C["item"])
        inner.pack(fill="x", padx=10, pady=7)
        row = tk.Frame(inner, bg=C["item"])
        row.pack(fill="x")
        ui["df_status"] = tk.Label(row, text="", bg=C["item"], fg=C["ink"], font=self.f_bold, anchor="w")
        ui["df_status"].pack(side="left")
        srv = tk.Frame(row, bg=C["item"])
        srv.pack(side="right")
        tk.Label(srv, text="Главный:", bg=C["item"], fg=C["muted"], font=self.f_tiny, padx=4).pack(side="left")
        ui["df_srv"] = {}
        for m in (self.mon_draft.machines if self.mon_draft else []):
            b = self.mon_button(srv, m.name, lambda mid=m.id: self.mon_pick_server(mid))
            b.pack(side="left", padx=(4, 0))
            ui["df_srv"][m.id] = b
        me_row = tk.Frame(inner, bg=C["item"])
        me_row.pack(fill="x", pady=(4, 0))
        tk.Label(me_row, text="Этот компьютер на схеме:", bg=C["item"], fg=C["muted"], font=self.f_tiny).pack(side="left")
        ui["df_me"] = {}
        for m in (self.mon_draft.machines if self.mon_draft else []):
            b = self.mon_button(me_row, m.name, lambda mid=m.id: self.mon_switch_this(mid))
            b.pack(side="left", padx=(6, 0))
            ui["df_me"][m.id] = b
        ui["df_detail"] = tk.Label(inner, text="", bg=C["item"], fg=C["muted"], font=self.f_tiny, anchor="w",
                                   justify="left", wraplength=int(540 * self.s))
        ui["df_detail"].pack(fill="x", pady=(2, 0))
        # плашка доверия: появляется, когда второй компьютер стучится с незнакомым отпечатком
        ui["df_trust"] = tk.Frame(inner, bg=C["item"], highlightthickness=1, highlightbackground=C["accent"])
        ti = tk.Frame(ui["df_trust"], bg=C["item"])
        ti.pack(fill="x", padx=8, pady=6)
        ui["df_trust_text"] = tk.Label(ti, text="", bg=C["item"], fg=C["ink"], font=self.f_small, anchor="w",
                                       justify="left", wraplength=int(520 * self.s))
        ui["df_trust_text"].pack(fill="x")
        ui["df_trust_fp"] = tk.Label(ti, text="", bg=C["item"], fg=C["accent"], font=self.f_mono, anchor="w",
                                     justify="left", wraplength=int(520 * self.s))
        ui["df_trust_fp"].pack(fill="x", pady=(3, 0))
        tr = tk.Frame(ti, bg=C["item"])
        tr.pack(fill="x", pady=(5, 0))
        self.mon_button(tr, "Совпадает — доверять", self.mon_trust_peer, primary=True).pack(side="left")
        self.mon_button(tr, "Не совпадает", self.mon_distrust_peer).pack(side="left", padx=(6, 0))
        # отпечаток этого компьютера — его сверяют на другом
        fp_row = tk.Frame(inner, bg=C["item"])
        fp_row.pack(fill="x", pady=(5, 0))
        tk.Label(fp_row, text="Отпечаток этого компьютера:", bg=C["item"], fg=C["muted"], font=self.f_tiny).pack(side="left")
        ui["df_fp_copy"] = self.mon_button(fp_row, "Копировать", self.mon_copy_fp)
        ui["df_fp_copy"].pack(side="right")
        ui["df_fp"] = tk.Label(inner, text="", bg=C["item"], fg=C["ink"], font=self.f_mono, anchor="w", justify="left",
                               wraplength=int(540 * self.s))
        ui["df_fp"].pack(fill="x")
        ui["df_install"] = tk.Frame(inner, bg=C["item"])
        ui["df_install_text"] = tk.Label(ui["df_install"], text="", bg=C["item"], fg=C["accent"], font=self.f_tiny,
                                         anchor="w", justify="left", wraplength=int(430 * self.s))
        ui["df_install_text"].pack(side="left", fill="x", expand=True)
        self.mon_button(ui["df_install"], "Копировать команду", self.mon_copy_install).pack(side="right")
        self.mon_note(inner, "На втором компьютере тоже нужна панель с той же раскладкой («Копировать раскладку» → "
                             "«Вставить раскладку») и режим «Общая мышь» с тем же главным. Ctrl+Alt+Shift+F11 — "
                             "запереть курсор на текущем компьютере, F12 — отпустить. Input Director на время "
                             "общей мыши закрой: две такие программы мешают друг другу.")

    @staticmethod
    def mon_fp_human(fp):
        """«ab12…» (64 hex) → «AB:12:…»; уже человекочитаемый — как есть."""
        s = re.sub(r"[^0-9A-Fa-f]", "", str(fp or ""))
        if len(s) != 64:
            return str(fp or "")
        s = s.upper()
        return ":".join(s[i:i + 2] for i in range(0, 64, 2))

    def mon_peer_id(self):
        """Второй компьютер схемы (UI рассчитан на две машины, §1)."""
        me = self.mon_this()
        lay = self.mon_draft
        return next((m.id for m in lay.machines if m.id != me), None) if lay else None

    def mon_paint_shared(self):
        ui = self.mon_ui
        lb = ui.get("df_status") if ui else None
        if not lb or not lb.winfo_exists():
            return
        snap = self.mon_snap
        df = snap.get("deskflow") or {}
        comp = (snap.get("components") or {}).get("deskflow") or {}
        mode, server = snap.get("mode") or "off", snap.get("server")
        me, peer = self.mon_this(), self.mon_peer_id()
        peer_name = self.mon_machine_name(peer) if peer else "второй компьютер"
        connected = [str(c) for c in (df.get("connected") or [])]
        role = df.get("role")
        if comp.get("state") not in ("ok", None):
            text, fg = f"Deskflow {self.mon_comp('deskflow')[0]}", C["accent"]
        elif mode != "shared":
            text, fg = "○ Выключено", C["ink"]
        elif df.get("err"):
            text, fg = "● Ошибка", C["red"]
        elif not df.get("running"):
            text, fg = "● Запускаю…", C["accent"]
        elif role == "server":
            text, fg = (f"● Главный · подключён {', '.join(connected)}", C["mint"]) if connected else \
                       (f"● Главный · жду {peer_name}", C["accent"])
        elif df.get("connected"):
            text, fg = f"● Подключён к {self.mon_machine_name(server)}", C["mint"]
        elif df.get("waiting_peer_trust"):
            text, fg = f"● Жду подтверждения на {self.mon_machine_name(server)}", C["accent"]
        else:
            text, fg = f"● Подключаюсь к {self.mon_machine_name(server)}…", C["accent"]
        lb.configure(text=text, fg=fg)
        for mid, b in ui.get("df_srv", {}).items():
            on = mid == server
            b.configure(fg=C["accent"] if on else C["ink"], text=("● " if on else "") + self.mon_machine_name(mid))
        for mid, b in ui.get("df_me", {}).items():
            on = mid == me
            b.configure(fg=C["mint"] if on else C["ink"], text=("● " if on else "") + self.mon_machine_name(mid))
        detail = []
        if df.get("err"):
            detail.append(clip(str(df["err"]), 160))
        elif mode == "shared" and role == "client" and df.get("waiting_peer_trust") and not df.get("connected"):
            detail.append(f"Этот компьютер уже доверяет {self.mon_machine_name(server)}. Теперь на "
                          f"{self.mon_machine_name(server)} в панели нажми «Совпадает — доверять» "
                          f"(сверь отпечаток с тем, что ниже)")
        elif mode == "shared" and df.get("running") and df.get("since"):
            detail.append(f"работает с {str(df['since'])[11:16]}")
        if mode != "shared" and comp.get("state") == "ok":
            detail.append("Нажми «Общая мышь» вверху — или выбери главный компьютер справа.")
        if not me:
            detail.append("Не выбрано, какой компьютер на схеме — этот.")
        ui["df_detail"].configure(text=" · ".join(detail))
        pend = df.get("pending_peer_fp")
        fp = pend.get("fp") if isinstance(pend, dict) else (pend if isinstance(pend, str) else None)
        if fp and fp != self.mon_df_rejected:
            ui["df_trust_text"].configure(text=f"{peer_name} хочет подключиться, но его отпечаток ещё незнакомый. "
                                               f"Открой панель на {peer_name} и сравни с её строкой «Отпечаток этого "
                                               f"компьютера» — хотя бы первые и последние 4 пары:")
            ui["df_trust_fp"].configure(text=self.mon_fp_human(fp))
            if not ui["df_trust"].winfo_ismapped():
                ui["df_trust"].pack(fill="x", pady=(6, 0), after=ui["df_detail"])
        elif ui["df_trust"].winfo_ismapped():
            ui["df_trust"].pack_forget()
        ui["df_fp"].configure(text=self.mon_fp_human(df.get("fp_local")) if df.get("fp_local")
                              else "появится после первого включения общей мыши")
        if comp.get("state") in ("missing", "old") and comp.get("cmd"):
            ui["df_install_text"].configure(text=f"Установка: {comp['cmd']}" +
                                            (" (нужны права администратора)" if comp.get("admin_to_install") else ""))
            if not ui["df_install"].winfo_ismapped():
                ui["df_install"].pack(fill="x", pady=(6, 0))
        elif ui["df_install"].winfo_ismapped():
            ui["df_install"].pack_forget()

    def mon_switch_this(self, mid):
        if mid == self.mon_this():
            return
        snap = self.mon_snap
        was_shared, server = snap.get("mode") == "shared", snap.get("server")
        res = self.mon_call("set_this_machine", mid)
        if not self.mon_ok(res):
            return self.mon_fail(res, "Не запомнил")
        self.mon_refresh_snap()
        self.set_tab("monitors")
        self.flash(f"Этот компьютер — {self.mon_machine_name(mid)}")
        if was_shared:  # роль (главный/второй) зависит от того, кто «я», — перезапустить общую мышь
            self.mon_pick_mode("shared", server)

    def mon_pick_server(self, mid):
        if self.mon_snap.get("mode") == "shared" and self.mon_snap.get("server") == mid:
            return
        self.mon_pick_mode("shared", mid)

    def mon_trust_peer(self):
        df = self.mon_snap.get("deskflow") or {}
        pend = df.get("pending_peer_fp")
        fp = pend.get("fp") if isinstance(pend, dict) else pend
        peer = self.mon_peer_id()
        if not fp or not peer:
            return
        res = self.mon_call("trust_peer", peer, fp)
        if not self.mon_ok(res):
            return self.mon_fail(res, "Не добавил в доверенные")
        self.mon_refresh_snap()
        self.mon_paint_shared()
        self.flash(f"{self.mon_machine_name(peer)} в доверенных — подключение пойдёт само через пару секунд")

    def mon_distrust_peer(self):
        df = self.mon_snap.get("deskflow") or {}
        pend = df.get("pending_peer_fp")
        self.mon_df_rejected = pend.get("fp") if isinstance(pend, dict) else pend
        self.mon_paint_shared()
        self.flash("Не доверяю. Если это точно твой компьютер — переустанови сертификат на нём и сверь снова", C["red"])

    def mon_copy_fp(self):
        fp = (self.mon_snap.get("deskflow") or {}).get("fp_local")
        if not fp:
            return self.flash("Отпечатка ещё нет — включи общую мышь один раз", C["accent"])
        self.copy(self.mon_fp_human(fp), "Отпечаток скопирован")

    def mon_copy_install(self):
        cmd = ((self.mon_snap.get("components") or {}).get("deskflow") or {}).get("cmd")
        if cmd:
            self.copy(cmd, "Команда скопирована — вставь в PowerShell" if sys.platform == "win32"
                      else "Команда скопирована — вставь в Терминал")

    def mon_comp(self, name):
        """(текст, цвет) состояния компонента из snapshot()["components"] (§5.5.1)."""
        comp = (self.mon_snap.get("components") or {}).get(name)
        if not isinstance(comp, dict):
            return "статус неизвестен", C["muted"]
        st = comp.get("state")
        text, col = MON_STATE.get(st, ("статус неизвестен", "muted"))
        if st == "ok" and comp.get("version"):
            text = f"{text} {comp['version']}"
        elif st == "na" and comp.get("note"):
            text = str(comp["note"])
        elif st == "missing" and comp.get("admin_to_install"):
            text += " (нужен администратор)"
        return text, C[col]

    def mon_pick_machine(self, area):
        """this_machine не определился по hostname — спросить, какой компьютер схемы этот (§4)."""
        box = tk.Frame(area, bg=C["item"], highlightthickness=1, highlightbackground=C["accent"])
        box.pack(fill="x", pady=(8, 0))
        inner = tk.Frame(box, bg=C["item"])
        inner.pack(fill="x", padx=10, pady=8)
        self.mon_note(inner, "Какой компьютер на схеме — этот? От этого зависят «Определить экраны» и помощник курсора.",
                      C["ink"], self.f_small)
        row = tk.Frame(inner, bg=C["item"])
        row.pack(fill="x", pady=(6, 0))
        for m in self.mon_draft.machines:
            self.mon_button(row, f"{m.name} ({m.hostname or 'имя неизвестно'})",
                            lambda mid=m.id: self.mon_set_this(mid)).pack(side="left", padx=(0, 6))

    def mon_set_this(self, mid):
        res = self.mon_call("set_this_machine", mid)
        if not self.mon_ok(res):
            return self.mon_fail(res, "Не запомнил")
        self.mon_refresh_snap()
        self.set_tab("monitors")

    def mon_legend(self, area):
        row = tk.Frame(area, bg=area["bg"])
        row.pack(fill="x", pady=(3, 0))
        for text, fg in (("━ напротив", C["mint"]), ("━ в ближайший угол", C["accent"]), ("┄ стена", C["red"])):
            tk.Label(row, text=text, bg=area["bg"], fg=fg, font=self.f_tiny, padx=0).pack(side="left", padx=(0, 10))
        tk.Label(row, text="· наведи на край — увидишь, куда выйдет курсор", bg=area["bg"], fg=C["muted"],
                 font=self.f_tiny).pack(side="left")
        tk.Label(area, text="Перетаскивай мышью — края притягиваются (Shift — без привязки). Клавиатура: Tab к схеме, "
                            "пробел — следующий экран, стрелки — сдвиг на 1 см (Shift — на 1 мм).",
                 bg=area["bg"], fg=C["muted"], font=self.f_tiny, anchor="w", justify="left",
                 wraplength=int(540 * self.s)).pack(fill="x")

    def mon_props(self, area):
        """Свойства выбранного экрана: подпись и размер (SizeForm из layout_editor)."""
        ui = self.mon_ui
        box = tk.Frame(area, bg=C["item"])
        box.pack(fill="x", pady=(6, 0))
        inner = tk.Frame(box, bg=C["item"])
        inner.pack(fill="x", padx=10, pady=7)
        top = tk.Frame(inner, bg=C["item"])
        top.pack(fill="x", pady=(0, 5))
        ui["sel_title"] = tk.Label(top, text="", bg=C["item"], fg=C["ink"], font=self.f_bold, anchor="w")
        ui["sel_title"].pack(side="left")
        ui["label_var"] = tk.StringVar(self.root)
        ent = self.mon_entry(top, ui["label_var"], 18)
        ent.pack(side="right")
        ent.bind("<Return>", lambda e: self.mon_rename())
        ent.bind("<FocusOut>", lambda e: self.mon_rename())
        ui["label_ent"] = ent
        tk.Label(top, text="Подпись", bg=C["item"], fg=C["muted"], font=self.f_small, padx=6).pack(side="right")
        ui["size"] = layout_editor.SizeForm(inner, colors=C, fonts=self.mon_fonts(), make_button=self.mon_button,
                                            make_entry=self.mon_entry, on_apply=self.mon_apply_size)
        ui["size"].pack(fill="x")

    def mon_helper(self, area):
        """Переключатель помощника краёв (§7.4): выкл по умолчанию, при включении — статус и пауза."""
        ui = self.mon_ui
        self.mon_head(area, "ПОМОЩНИК КУРСОРА (СВОИ МОНИТОРЫ)", pady=(12, 4))
        box = tk.Frame(area, bg=C["item"])
        box.pack(fill="x")
        inner = tk.Frame(box, bg=C["item"])
        inner.pack(fill="x", padx=10, pady=7)
        row = tk.Frame(inner, bg=C["item"])
        row.pack(fill="x")
        tg = tk.Label(row, text="", bg=C["item"], fg=C["ink"], font=self.f_bold, cursor="hand2", takefocus=1, padx=2,
                      highlightthickness=1, highlightbackground=C["item"], highlightcolor=C["accent"])
        for seq in ("<Button-1>", "<Return>", "<space>"):
            tg.bind(seq, lambda e: self.mon_toggle_edge())
        tg.pack(side="left")
        ui["edge_toggle"] = tg
        ui["edge_pause"] = self.mon_button(row, "Пауза", self.mon_pause_edge)  # пауза без выключения (config не меняется)
        ui["edge_status"] = tk.Label(row, text="", bg=C["item"], fg=C["muted"], font=self.f_tiny, padx=10)
        ui["edge_status"].pack(side="left")
        self.mon_note(inner, "Курсор переходит между экранами этого компьютера по реальным размерам: напротив, "
                             "а из пустой зоны — в ближайший угол (линии на схеме). Сторонние программы не нужны; "
                             "работает по сохранённой раскладке.")

    # отрисовка состояния (без перестройки вкладки)
    def mon_paint_status(self):
        ui = self.mon_ui
        if not ui or "modes" not in ui or not ui["mode_line"].winfo_exists():
            return
        self.mon_refresh_snap()
        snap = self.mon_snap
        mode = snap.get("mode") or "off"
        df_text, df_col = self.mon_comp("deskflow")
        subs = {"off": ("только свои экраны", C["muted"]),
                "shared": (f"Deskflow: {df_text}", df_col),
                "monitor": (f"Apollo: {self.mon_comp('apollo')[0]} · Moonlight: {self.mon_comp('moonlight')[0]}",
                            C["muted"])}
        for key, (cell, t, sub) in ui["modes"].items():
            on = key == mode
            cell.configure(highlightbackground=C["accent"] if on else C["item"])
            t.configure(fg=C["accent"] if on else (C["muted"] if key == "monitor" else C["ink"]))
            sub.configure(text=subs[key][0], fg=subs[key][1])
        pend = snap.get("os_positions_pending")  # плашка отката переживает перестройку вкладки
        if isinstance(pend, dict) and pend.get("token") and not self.mon_pending:
            self.mon_start_pending(pend["token"], pend.get("deadline_s", MON_ROLLBACK_S))
        if snap.get("busy"):
            ui["mode_line"].configure(text=snap.get("busy_text") or "Выполняю…", fg=C["accent"])
        elif mode == "shared":
            ui["mode_line"].configure(text="Курсор уходит на другой компьютер через край по схеме ниже — по реальным "
                                           "размерам экранов.", fg=C["muted"])
        else:
            ui["mode_line"].configure(text="«Общая мышь» — одна мышь и клавиатура на два компьютера. «Ноут как монитор» "
                                           "— в следующей версии.", fg=C["muted"])
        self.mon_paint_shared()
        self.mon_paint_edge()

    def mon_paint_edge(self):
        ui = self.mon_ui
        tg = ui.get("edge_toggle") if ui else None
        if not tg or not tg.winfo_exists():
            return
        edge = self.mon_snap.get("edge") or {}
        comp = (self.mon_snap.get("components") or {}).get("edge") or {}
        running = bool(edge.get("running"))
        enabled = bool(edge.get("enabled", running))  # enabled — в config; running — поток помощника жив
        tg.configure(text="● Помощник включён" if enabled else "○ Помощник выключен",
                     fg=C["mint"] if enabled else C["ink"])
        ui["edge_pause"].configure(text="Продолжить" if edge.get("user_paused") else "Пауза")
        if enabled:
            ui["edge_pause"].pack(side="right")
        else:
            ui["edge_pause"].pack_forget()
        if comp.get("state") in ("unsupported", "missing"):
            text, fg = "недоступен на этом компьютере", C["muted"]
        elif edge.get("err"):
            text, fg = clip(f"ошибка: {edge['err']}", 70), C["red"]
        elif running and edge.get("client_state") == "client_absolute":  # §7.4.1: курсором ведёт главный (Deskflow)
            text, fg = "пауза: курсором управляет главный компьютер — для переходов по размерам нажми там Ctrl+Alt+Shift+F9", C["accent"]
        elif running and edge.get("user_paused"):
            text, fg = "на паузе — нажми «Продолжить»", C["accent"]
        elif running and edge.get("paused"):
            text, fg = "на паузе", C["accent"]
        elif running:
            n = int(edge.get("warps") or 0)
            text = f"работает · {n} {plural(n, 'переход', 'перехода', 'переходов')} поправлено"
            if comp.get("raw_input") is False:
                text += " · «дожим» в край недоступен"
            fg = C["mint"]
        elif enabled:
            text, fg = "включён, но не запущен — проверь раскладку (предупреждения выше)", C["accent"]
        else:
            text = ("переходы делает Windows — «пиксель в пиксель»" if sys.platform == "win32"
                    else "переходы делает система — «пиксель в пиксель»")
            fg = C["muted"]
        me = self.mon_this()
        if me and self.mon_draft and sum(s.machine == me for s in self.mon_draft.screens) < 2:
            text = "у этого компьютера один экран — помощнику нечего делать"
        ui["edge_status"].configure(text=text, fg=fg)

    def mon_toggle_edge(self):
        edge = self.mon_snap.get("edge") or {}
        want = not bool(edge.get("enabled", edge.get("running")))
        if want and self.mon_dirty:
            self.flash("Помощник работает по сохранённой раскладке — сначала «Сохранить»", C["accent"])
        res = self.mon_call("set_edge_helper", want)
        if not self.mon_ok(res):
            return self.mon_fail(res, "Помощник не " + ("включён" if want else "выключен"))
        self.mon_refresh_snap()
        self.mon_paint_edge()
        if not self.mon_dirty or not want:
            self.flash("Помощник курсора включён" if want else "Помощник курсора выключен")

    def mon_pause_edge(self):
        want = not bool((self.mon_snap.get("edge") or {}).get("user_paused"))
        res = self.mon_call("pause_edge_helper", want)
        if not self.mon_ok(res):
            return self.mon_fail(res, "Пауза не " + ("включена" if want else "снята"))
        self.mon_refresh_snap()
        self.mon_paint_edge()
        self.flash("Помощник курсора на паузе" if want else "Помощник курсора снова работает")

    def mon_list_displays(self):
        """Экраны ОС: refresh_displays() перечитывает их синхронно (~2 мс, start() не нужен); старый бэкенд — кэш."""
        res = self.mon_call("refresh_displays")
        return self.mon_call("local_displays") if isinstance(res, dict) and res.get("code") == "not_found" else res

    def mon_paint_dirty(self):
        lb = self.mon_ui.get("dirty") if self.mon_ui else None
        if lb and lb.winfo_exists():
            lb.configure(text="● не сохранено" if self.mon_dirty else "")

    def mon_paint_warn(self):
        lb = self.mon_ui.get("warn") if self.mon_ui else None
        if not lb or not lb.winfo_exists():
            return
        warns = []
        try:
            warns += list(layout.validate(self.mon_draft)) if self.mon_draft else []
        except Exception as e:
            plat.log(f"monitors: validate — {type(e).__name__}")
        if not self.mon_dirty:
            warns += list(self.mon_snap.get("warnings") or []) + list(self.mon_save_warn)
        warns = list(dict.fromkeys(str(w) for w in warns))
        more = f"\n…и ещё {len(warns) - 3}" if len(warns) > 3 else ""
        lb.configure(text="\n".join(f"⚠ {clip(w, 110)}" for w in warns[:3]) + more if warns else "")

    def mon_paint_props(self):
        ui = self.mon_ui
        if not ui or "size" not in ui or not ui["size"].winfo_exists():
            return
        s = self.mon_screen(self.mon_sel)
        if s:
            where = "этот компьютер" if s.machine == self.mon_this() else self.mon_machine_name(s.machine)
            ui["sel_title"].configure(text=clip(f"{s.label} · {where}", 34), fg=C["ink"])
            ui["label_ent"].configure(state="normal")
            ui["label_var"].set(s.label)
            ui["label_for"] = s.id  # поле подписи заполнено для этого экрана
        else:
            ui["label_for"] = None
            ui["sel_title"].configure(text="Экран не выбран", fg=C["muted"])
            ui["label_var"].set("")
            ui["label_ent"].configure(state="disabled")
        ui["size"].set_screen(s, self.mon_edid_for(s))

    def mon_edid_for(self, s):
        """Размер из EDID для экрана этой машины (по os_id из local_displays), иначе None."""
        if not s or s.machine != self.mon_this() or not s.os_id:
            return None
        d = next((d for d in self.mon_displays or [] if d["os_id"] == s.os_id), None)
        return d["size_mm"] if d else None

    def mon_track_cursor(self):
        """Метка «курсор здесь» на экране этой машины, где сейчас курсор (os_rect_px — в координатах ОС)."""
        ed, me, lay = self.mon_editor, self.mon_this(), self.mon_draft
        if not ed or not ed.winfo_exists() or not me or not lay:
            return
        try:
            x, y = plat.cursor_pos(self.root)
        except Exception:
            return
        cur = None
        for s in lay.screens:
            r = s.os_rect_px
            if s.machine == me and r and r[0] <= x < r[0] + r[2] and r[1] <= y < r[1] + r[3]:
                cur = s.id
                break
        ed.set_current(cur)

    # правки черновика
    def mon_set_draft(self, lay, dirty=True):
        self.mon_draft = lay
        self.mon_dirty = self.mon_dirty or dirty
        if self.mon_editor and self.mon_editor.winfo_exists():
            self.mon_editor.set_layout(lay)
            self.mon_editor.select(self.mon_sel)
        self.mon_paint_dirty()
        self.mon_paint_warn()
        self.mon_paint_props()

    def mon_replace_screen(self, sid, **kw):
        lay = self.mon_draft
        screens = tuple(dataclasses.replace(s, **kw) if s.id == sid else s for s in lay.screens)
        self.mon_set_draft(dataclasses.replace(lay, screens=screens))

    def mon_changed(self, lay):
        self.mon_draft, self.mon_dirty = lay, True
        self.mon_paint_dirty()
        self.mon_paint_warn()

    def mon_selected(self, sid):
        self.mon_rename()  # подпись прошлого экрана, набранная без Enter
        self.mon_sel = sid
        self.mon_paint_props()

    def mon_set_hold(self, reason, on):
        """Пока идёт перетаскивание или висит плашка отката, панель не прячется (§15)."""
        was = bool(self.mon_hold)
        (self.mon_hold.add if on else self.mon_hold.discard)(reason)
        if self.mon_hold and not was:
            self.mon_pin_prev, self.pinned = self.pinned, True
        elif was and not self.mon_hold:
            self.pinned = self.mon_pin_prev

    def mon_rename(self):
        ui = self.mon_ui
        s = self.mon_screen(ui.get("label_for")) if ui else None
        if not s or not ui or "label_ent" not in ui or not ui["label_ent"].winfo_exists():
            return
        new = " ".join(ui["label_var"].get().split())[:MON_LABEL_MAX]
        if not new:
            ui["label_var"].set(s.label)
            return self.flash("Подпись экрана не может быть пустой", C["accent"])
        if new != s.label:
            self.mon_replace_screen(s.id, label=new)

    def mon_apply_size(self, sid, size, source):
        self.mon_replace_screen(sid, size_mm=(float(size[0]), float(size[1])), size_source=source)
        s = self.mon_screen(sid)
        if s:
            self.flash(f"{s.label}: {layout_editor.diag_label(s.size_mm)} "
                       f"({layout_editor.fmt_num(s.size_mm[0])} × {layout_editor.fmt_num(s.size_mm[1])} мм)")

    # «Определить экраны» — список экранов этой ОС (list_displays через MonitorsCtl.local_displays)
    @staticmethod
    def mon_clean_displays(res):
        """Отбросить записи не по контракту §5.2 — UI не должен падать на чужих данных."""
        out = []
        for d in res if isinstance(res, list) else []:
            try:
                px = tuple(int(v) for v in d["px"])[:2]
                rect = tuple(int(v) for v in d["rect_px"])[:4]
                if len(px) != 2 or len(rect) != 4:
                    raise ValueError
                mm = d.get("size_mm")
                mm = (float(mm[0]), float(mm[1])) if mm else None
                scale = d.get("scale")
                out.append({"os_id": d.get("os_id") if isinstance(d.get("os_id"), str) else None,
                            "label": str(d.get("label") or "").strip()[:MON_LABEL_MAX], "px": px, "rect_px": rect,
                            "primary": bool(d.get("primary")), "scale": int(scale) if scale else None,
                            "size_mm": mm, "virtual": bool(d.get("virtual"))})
            except (KeyError, TypeError, ValueError, IndexError):
                plat.log("monitors: local_displays — запись пропущена (не по контракту)")
        return out

    def mon_fetch_displays(self, merge):
        if self.mon_detecting:
            return
        self.mon_detecting = True
        btn = self.mon_ui.get("detect") if self.mon_ui else None
        if merge and btn and btn.winfo_exists():
            btn.configure(text="Ищу экраны…")

        def done(res, err):
            self.mon_detecting = False
            if btn and btn.winfo_exists():
                btn.configure(text="Определить экраны")
            if err is not None or not isinstance(res, list):
                if merge:
                    self.mon_fail(res, "Экраны не определены")
                return
            self.mon_displays = self.mon_clean_displays(res)
            if merge:
                self.mon_merge_displays(self.mon_displays)
            elif self.tab == "monitors":
                self.mon_paint_props()
                self.mon_track_cursor()
        self.run_bg(self.mon_list_displays, done)

    def mon_detect(self):
        if not self.mon_this():
            return self.flash("Сначала выбери, какой компьютер на схеме — этот", C["accent"])
        self.mon_fetch_displays(merge=True)

    @staticmethod
    def mon_default_size(d):
        """EDID нет — типовая диагональ (15,6″ для основного, 24″ для остальных), size_source="default" (§9.1)."""
        return layout.size_from_diagonal(15.6 if d["primary"] else 24.0, "in", d["px"])

    def mon_merge_displays(self, displays):
        """Обновить экраны этой машины в черновике: пиксели, масштаб, os_rect_px, EDID; новые — добавить справа."""
        lay, me = self.mon_draft, self.mon_this()
        if not lay or not me:
            return
        screens = list(lay.screens)
        index = {s.os_id: i for i, s in enumerate(screens) if s.machine == me and s.os_id}
        used = {s.id for s in screens}
        seen, added, updated = set(), [], 0
        for d in displays:
            if d["virtual"]:  # виртуальный дисплей стрима — этап 3 (§8.3)
                continue
            seen.add(d["os_id"])
            if d["os_id"] in index:
                i = index[d["os_id"]]
                kw = {"px": d["px"], "os_rect_px": d["rect_px"], "scale": d["scale"]}
                if d["size_mm"] and screens[i].size_source != "user":
                    kw.update(size_mm=d["size_mm"], size_source="edid")
                screens[i] = dataclasses.replace(screens[i], **kw)
                updated += 1
                continue
            n = 1
            while f"{me}.{n}" in used:
                n += 1
            sid = f"{me}.{n}"
            used.add(sid)
            right = max((s.pos_mm[0] + s.size_mm[0] for s in screens), default=0.0)
            size, src = (d["size_mm"], "edid") if d["size_mm"] else (self.mon_default_size(d), "default")
            screens.append(layout.Screen(id=sid, machine=me, label=d["label"] or f"Экран {n}", px=d["px"],
                                         scale=d["scale"], size_mm=tuple(size), size_source=src,
                                         pos_mm=(round(right + 20.0, 1), 0.0), os_id=d["os_id"], os_rect_px=d["rect_px"]))
            added.append(screens[-1].label)
        gone = [s.label for s in screens if s.machine == me and s.os_id and s.os_id not in seen]
        self.mon_set_draft(dataclasses.replace(lay, screens=tuple(screens)), dirty=bool(updated or added))
        parts = [f"обновлено: {updated}"] + ([f"новые: {', '.join(added)}"] if added else []) + \
                ([f"сейчас не подключены: {', '.join(gone)}"] if gone else [])
        self.flash(clip("Экраны: " + " · ".join(parts), 110), C["accent"] if gone or added else C["mint"])

    # сохранение, раскладка ОС, копирование
    def mon_save(self):
        if not self.mon_draft:
            return
        self.mon_rename()  # подпись, набранная без Enter
        res = self.mon_call("save_layout", self.mon_draft)
        if not self.mon_ok(res):
            return self.mon_fail(res, "Не сохранено")
        self.mon_save_warn = [str(w) for w in res.get("warnings") or []]
        self.mon_draft, self.mon_dirty = self.mon_load_layout() or self.mon_draft, False
        self.mon_refresh_snap()
        self.mon_set_draft(self.mon_draft, dirty=False)
        n = len(self.mon_save_warn)
        self.flash("Раскладка сохранена" + (f" · предупреждений: {n}" if n else ""), C["accent"] if n else C["mint"])

    def mon_apply_os(self):
        if self.mon_dirty:
            return self.flash(f"Сначала «Сохранить» — к {self.mon_os_name()} применяется сохранённая раскладка",
                              C["accent"])
        if self.mon_pending:
            return self.flash("Сначала ответь на плашку сверху: оставить или вернуть", C["accent"])
        res = self.mon_call("apply_os_positions")
        if not self.mon_ok(res):
            return self.mon_fail(res, f"Раскладка {self.mon_os_name()} не изменена")
        if not res.get("token"):
            return self.flash(f"Раскладка {self.mon_os_name()} уже совпадает со схемой")
        self.mon_start_pending(res["token"], MON_ROLLBACK_S)

    def mon_start_pending(self, token, deadline_s):
        if (self.mon_pending and self.mon_pending["token"] == str(token)) or str(token) in self.mon_answered:
            return
        try:
            left = max(1.0, float(deadline_s))
        except (TypeError, ValueError):
            left = float(MON_ROLLBACK_S)
        self.mon_pending = {"token": str(token), "until": time.monotonic() + left}
        self.mon_set_hold("rollback", True)
        self.mon_paint_banner()

    def mon_paint_banner(self):
        """Плашка «Раскладка Windows изменена. Оставить?» с отсчётом; без ответа откат делает MonitorsCtl (§7.5)."""
        ui = self.mon_ui
        box = ui.get("banner") if ui else None
        pend = self.mon_pending
        if pend and time.monotonic() >= pend["until"]:
            self.mon_answered.add(pend["token"])
            self.mon_pending = pend = None
            self.mon_set_hold("rollback", False)
            self.flash(f"Раскладка {self.mon_os_name()} возвращена — не подтверждена за {MON_ROLLBACK_S} с", C["accent"])
        if not box or not box.winfo_exists():
            return
        if not pend:
            for w in box.winfo_children():
                w.destroy()
            ui.pop("banner_left", None)
            return
        left = max(0, int(pend["until"] - time.monotonic() + 0.99))
        if "banner_left" not in ui or not ui["banner_left"].winfo_exists():
            card = tk.Frame(box, bg=C["item"], highlightthickness=1, highlightbackground=C["accent"])
            card.pack(fill="x", pady=(0, 8))
            inner = tk.Frame(card, bg=C["item"])
            inner.pack(fill="x", padx=10, pady=7)
            tk.Label(inner, text=f"Раскладка {self.mon_os_name()} изменена. Оставить?", bg=C["item"], fg=C["ink"],
                     font=self.f_bold).pack(side="left")
            self.mon_button(inner, "Вернуть", lambda: self.mon_confirm(False)).pack(side="right")
            keep = self.mon_button(inner, "Оставить", lambda: self.mon_confirm(True), primary=True)
            keep.pack(side="right", padx=(0, 6))
            ui["banner_left"] = tk.Label(inner, text="", bg=C["item"], fg=C["accent"], font=self.f_tiny, padx=8)
            ui["banner_left"].pack(side="right")
            keep.focus_set()
        ui["banner_left"].configure(text=f"откат через {left} с")

    def mon_confirm(self, keep):
        pend, self.mon_pending = self.mon_pending, None
        if pend:
            self.mon_answered.add(pend["token"])
        self.mon_set_hold("rollback", False)
        self.mon_paint_banner()
        if not pend:
            return
        res = self.mon_call("confirm_os_positions" if keep else "rollback_os_positions", pend["token"])
        if not self.mon_ok(res):
            return self.mon_fail(res, "Раскладка не подтверждена" if keep else "Не вернул")
        self.flash(f"Раскладка {self.mon_os_name()} " + ("оставлена" if keep else "возвращена"))

    def mon_copy_layout(self):
        if self.mon_dirty:
            return self.flash("Сначала «Сохранить» — копируется сохранённая раскладка", C["accent"])
        text = self.mon_call("export_layout")
        if not isinstance(text, str) or not text.strip():
            return self.mon_fail(text, "Раскладка не скопирована")
        self.copy(text, "Раскладка скопирована — нажми «Вставить раскладку» на другом компьютере")

    def mon_paste_layout(self):
        try:
            text = self.root.clipboard_get()
        except tk.TclError:
            text = ""
        if not text.strip():
            return self.flash("В буфере пусто — сначала «Копировать раскладку» на другом компьютере", C["accent"])
        if self.mon_dirty and time.monotonic() - self.mon_paste_armed > 5:
            self.mon_paste_armed = time.monotonic()
            return self.flash("Есть несохранённые правки — нажми «Вставить» ещё раз, чтобы заменить", C["accent"])
        res = self.mon_call("import_layout", text)
        if not self.mon_ok(res):
            return self.mon_fail(res, "Раскладка не вставлена")
        self.mon_draft, self.mon_dirty, self.mon_wiz, self.mon_sel = self.mon_load_layout(), False, None, None
        self.mon_refresh_snap()
        self.set_tab("monitors")
        self.flash("Раскладка вставлена")

    def mon_event(self, kind, payload):
        """События MonitorsCtl из очереди (поток Tk)."""
        payload = payload if isinstance(payload, dict) else {}
        open_tab = self.shown and self.tab == "monitors" and self.mon_ui
        if kind == "os_positions_pending" and payload.get("token"):
            self.mon_start_pending(payload["token"], payload.get("deadline_s", MON_ROLLBACK_S))
        elif kind == "displays_changed":
            self.mon_displays = None
            if open_tab:
                self.flash("Экраны изменились — нажми «Определить экраны»", C["accent"])
                self.mon_fetch_displays(merge=False)
        elif kind == "warning" and payload.get("text"):
            plat.log(f"monitors: предупреждение — {clip(str(payload['text']), 120)}")
            if open_tab:
                self.flash(clip(str(payload["text"]), 110), C["accent"])
        elif open_tab:  # snapshot_changed, mode_changed, deskflow_* — перерисовать статус
            self.mon_paint_status()

    # мастер первого запуска (§15.1: шаги 1, 2 и 4; второй компьютер — коротко, только для схемы)
    def mon_wiz_new(self):
        host = socket.gethostname() or "this-pc"
        name = re.sub(r"[^A-Za-z0-9._-]+", "-", host.lower()).strip("-")[:63] or "laptop-a"

        def var(val=""):
            return tk.StringVar(self.root, value=val)
        return {"step": 0, "count": 2, "hostname": host, "v_name": var(name), "displays": None, "rows": [],
                "loading": False, "b_os": "windows", "v_b_name": var("laptop-b"), "v_b_host": var(""),
                "v_b_label": var("Ноут B"), "v_b_diag": var("15,6"), "v_b_px": var("1920×1080"), "draft": None,
                "sig": None}

    def mon_wiz_steps(self):
        return ["me", "screens", "peer", "arrange"] if self.mon_wiz["count"] == 2 else ["me", "screens", "arrange"]

    def mon_wizard(self, area):
        wz = self.mon_wiz
        steps = self.mon_wiz_steps()
        wz["step"] = min(wz["step"], len(steps) - 1)
        key = steps[wz["step"]]
        titles = {"me": "Этот компьютер", "screens": "Экраны этого компьютера", "peer": "Второй компьютер",
                  "arrange": "Расставь экраны как на столе"}
        self.mon_head(area, f"ПЕРВАЯ НАСТРОЙКА МОНИТОРОВ · ШАГ {wz['step'] + 1} ИЗ {len(steps)}")
        box = tk.Frame(area, bg=C["item"])
        box.pack(fill="x")
        inner = tk.Frame(box, bg=C["item"])
        inner.pack(fill="x", padx=12, pady=10)
        self.mon_note(inner, titles[key], C["ink"], self.f_bold)
        tk.Frame(inner, bg=C["item"], height=int(4 * self.s)).pack(fill="x")
        getattr(self, f"mon_wiz_{key}")(inner)
        self.mon_ui["wiz_err"] = tk.Label(area, text="", bg=C["bg"], fg=C["red"], font=self.f_tiny, anchor="w")
        self.mon_ui["wiz_err"].pack(fill="x", pady=(4, 0))
        nav = tk.Frame(area, bg=C["bg"])
        nav.pack(fill="x", pady=(4, 8))
        if wz["step"] > 0:
            self.mon_button(nav, "← Назад", lambda: self.mon_wiz_go(-1)).pack(side="left")
        last = wz["step"] == len(steps) - 1
        self.mon_button(nav, "Готово — сохранить" if last else "Далее →",
                        self.mon_wiz_finish if last else (lambda: self.mon_wiz_go(1)), primary=True).pack(side="right")

    def mon_wiz_go(self, d):
        if d > 0:
            err = self.mon_wiz_check(self.mon_wiz_steps()[self.mon_wiz["step"]])
            if err:
                return self.mon_ui["wiz_err"].configure(text=err)
        self.mon_wiz["step"] = max(0, self.mon_wiz["step"] + d)
        self.set_tab("monitors")

    def mon_wiz_chips(self, parent, options, current, on_pick):
        row = tk.Frame(parent, bg=parent["bg"])
        row.pack(fill="x", pady=(2, 6))
        for val, text in options:
            on = val == current
            self.mon_button(row, ("● " if on else "○ ") + text, lambda v=val: on_pick(v),
                            fg=C["accent"] if on else None).pack(side="left", padx=(0, 6))
        return row

    def mon_wiz_field(self, parent, label, var, width, hint=""):
        row = tk.Frame(parent, bg=parent["bg"])
        row.pack(fill="x", pady=2)
        tk.Label(row, text=label, bg=parent["bg"], fg=C["muted"], font=self.f_small, width=16, anchor="w").pack(side="left")
        ent = self.mon_entry(row, var, width)
        ent.pack(side="left")
        if hint:
            tk.Label(row, text=hint, bg=parent["bg"], fg=C["muted"], font=self.f_tiny, padx=8).pack(side="left")
        return ent

    def mon_wiz_pick(self, key, val):
        self.mon_wiz[key] = val
        self.set_tab("monitors")

    def mon_wiz_me(self, inner):
        wz = self.mon_wiz
        self.mon_note(inner, f"Имя в системе: {wz['hostname']}", C["ink"], self.f_small)
        self.mon_wiz_field(inner, "Имя для схемы", wz["v_name"], 22, "латиница, цифры, точка, дефис")
        self.mon_note(inner, "Сколько компьютеров стоит на столе?", C["ink"], self.f_small).pack_configure(pady=(8, 0))
        self.mon_wiz_chips(inner, ((1, "1 — только этот"), (2, "2 — этот и ещё один")), wz["count"],
                           lambda v: self.mon_wiz_pick("count", v))
        self.mon_note(inner, "Экраны этого компьютера найдём на следующем шаге. Второй компьютер пока участвует только "
                             "в схеме — общая мышь появится в следующей версии.")

    def mon_wiz_screens(self, inner):
        wz = self.mon_wiz
        if wz["displays"] is None:
            self.mon_note(inner, "Ищу экраны…", C["muted"], self.f_small)
            if not wz["loading"]:
                wz["loading"] = True

                def done(res, err):
                    wz["loading"] = False
                    ds = [d for d in self.mon_clean_displays(res) if not d["virtual"]] if err is None else []
                    if not ds:  # список не получен — хотя бы основной экран по данным окна
                        w, h = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
                        ds = [{"os_id": None, "label": "", "px": (w, h), "rect_px": (0, 0, w, h), "primary": True,
                               "scale": None, "size_mm": None, "virtual": False, "guess": True}]
                    wz["displays"] = ds
                    wz["rows"] = [{"v_label": tk.StringVar(self.root, value=self.mon_default_label(d, n)),
                                   "v_diag": tk.StringVar(self.root, value=layout_editor.fmt_num(
                                       layout.diagonal_in(d["size_mm"])) if d["size_mm"] else "")}
                                  for n, d in enumerate(ds, 1)]
                    if self.mon_wiz is wz and self.tab == "monitors":
                        self.set_tab("monitors")
                self.run_bg(self.mon_list_displays, done)
            return
        if any(d.get("guess") for d in wz["displays"]):
            self.mon_note(inner, "Список экранов получить не удалось — показан основной экран. Остальные можно "
                                 "добавить потом кнопкой «Определить экраны».", C["accent"])
        for d, row in zip(wz["displays"], wz["rows"]):
            r = tk.Frame(inner, bg=C["item"])
            r.pack(fill="x", pady=3)
            self.mon_entry(r, row["v_label"], 16).pack(side="left")
            info = (f"{d['px'][0]}×{d['px'][1]}" + (f" · {d['scale']} %" if d["scale"] else "")
                    + (" · основной" if d["primary"] else "") + (f" · {d['label']}" if d["label"] else ""))
            tk.Label(r, text=info, bg=C["item"], fg=C["muted"], font=self.f_small, padx=8).pack(side="left")
            tk.Label(r, text="″", bg=C["item"], fg=C["muted"], font=self.f_small).pack(side="right")
            self.mon_entry(r, row["v_diag"], 6).pack(side="right")
            src, fg = ("из EDID", C["mint"]) if d["size_mm"] else ("введи диагональ", C["accent"])
            tk.Label(r, text=src, bg=C["item"], fg=fg, font=self.f_tiny, padx=6).pack(side="right")
        self.mon_note(inner, "Диагональ — в дюймах, как в характеристиках (15,6; 24; 27). Точнее — ширину и высоту "
                             "в мм можно ввести потом в свойствах экрана.").pack_configure(pady=(6, 0))

    @staticmethod
    def mon_default_label(d, n):
        """Подпись по умолчанию: «Ноут A» для основного экрана до 17,5″, иначе «Монитор 24″» (модель EDID — в сведениях)."""
        diag = layout.diagonal_in(d["size_mm"]) if d["size_mm"] else None
        if d["primary"] and (diag is None or diag <= 17.5):
            return "Ноут A"
        return f"Монитор {round(diag)}″" if diag else f"Монитор {n}"

    def mon_wiz_peer(self, inner):
        wz = self.mon_wiz
        self.mon_wiz_field(inner, "Имя для схемы", wz["v_b_name"], 22, "латиница, цифры, точка, дефис")
        self.mon_wiz_field(inner, "IP в сети", wz["v_b_host"], 22, "можно пусто — понадобится позже")
        tk.Label(inner, text="Система", bg=C["item"], fg=C["muted"], font=self.f_small, anchor="w").pack(fill="x", pady=(4, 0))
        self.mon_wiz_chips(inner, MON_OS, wz["b_os"], lambda v: self.mon_wiz_pick("b_os", v))
        self.mon_wiz_field(inner, "Его экран", wz["v_b_label"], 22)
        self.mon_wiz_field(inner, "Диагональ, ″", wz["v_b_diag"], 8)
        self.mon_wiz_field(inner, "Разрешение", wz["v_b_px"], 12, "например 1920×1080")
        row = tk.Frame(inner, bg=C["item"])
        row.pack(fill="x", pady=(8, 0))
        self.mon_button(row, "Вставить раскладку со второго компьютера", self.mon_paste_layout).pack(side="left")
        tk.Label(row, text="если там уже настроено", bg=C["item"], fg=C["muted"], font=self.f_tiny,
                 padx=8).pack(side="left")

    @staticmethod
    def mon_parse_px(text):
        m = re.fullmatch(r"\s*(\d{3,5})\s*[xXхХ×*]\s*(\d{3,5})\s*", text or "")
        if not m:
            return None
        w, h = int(m.group(1)), int(m.group(2))
        return (w, h) if 320 <= w <= 16384 and 200 <= h <= 16384 else None

    @staticmethod
    def mon_wiz_diag(text):
        d = layout_editor.parse_num(text)
        lo, hi = layout_editor.DIAG_RANGE["in"]
        return d if d is not None and lo <= d <= hi else None

    def mon_wiz_check(self, key):
        """Текст ошибки шага или None."""
        wz = self.mon_wiz
        if key == "me":
            if not MON_NAME_RE.fullmatch(wz["v_name"].get().strip()):
                return "Имя для схемы: 1–63 символа — латиница, цифры, точка, дефис; не начинается с дефиса"
        elif key == "screens":
            if wz["displays"] is None:
                return "Подожди — ищу экраны"
            for row in wz["rows"]:
                if not " ".join(row["v_label"].get().split()):
                    return "У каждого экрана нужна подпись"
                if self.mon_wiz_diag(row["v_diag"].get()) is None:
                    return f"«{row['v_label'].get().strip()}»: диагональ — число от 5 до 120 дюймов"
        elif key == "peer":
            name = wz["v_b_name"].get().strip()
            if not MON_NAME_RE.fullmatch(name):
                return "Имя второго компьютера: латиница, цифры, точка, дефис"
            if name.lower() == wz["v_name"].get().strip().lower():
                return "У компьютеров должны быть разные имена"
            host = wz["v_b_host"].get().strip()
            valid_host = getattr(monitor, "valid_host", None) if monitor else None
            if host and callable(valid_host) and not valid_host(host):
                return "IP или имя второго компьютера записаны с ошибкой"
            if not " ".join(wz["v_b_label"].get().split()):
                return "Подпиши экран второго компьютера"
            if self.mon_wiz_diag(wz["v_b_diag"].get()) is None:
                return "Диагональ экрана второго компьютера — число от 5 до 120 дюймов"
            if self.mon_parse_px(wz["v_b_px"].get()) is None:
                return "Разрешение — как 1920×1080"
        return None

    def mon_wiz_build(self):
        """Черновик раскладки из ответов мастера: экраны этой машины слева направо по раскладке ОС,
        низом на одной линии; второй компьютер — справа. Дальше пользователь двигает их сам."""
        wz = self.mon_wiz
        me_os = "windows" if sys.platform == "win32" else "macos"
        machines = [layout.Machine(id="a", name=wz["v_name"].get().strip(), hostname=wz["hostname"], host=None, os=me_os)]
        items = []
        for n, (d, row) in enumerate(zip(wz["displays"], wz["rows"]), 1):
            diag = self.mon_wiz_diag(row["v_diag"].get())
            if d["size_mm"] and abs(layout.diagonal_in(d["size_mm"]) - diag) < 0.06:
                size, src = d["size_mm"], "edid"
            else:
                size, src = layout.size_from_diagonal(diag, "in", d["px"]), "user"
            items.append(dict(id=f"a.{n}", machine="a", label=" ".join(row["v_label"].get().split())[:MON_LABEL_MAX],
                              px=d["px"], scale=d["scale"], size_mm=tuple(size), size_source=src, os_id=d["os_id"],
                              os_rect_px=None if d.get("guess") else d["rect_px"], order=d["rect_px"][0]))
        if wz["count"] == 2:
            px = self.mon_parse_px(wz["v_b_px"].get())
            machines.append(layout.Machine(id="b", name=wz["v_b_name"].get().strip(), hostname="",
                                           host=wz["v_b_host"].get().strip() or None, os=wz["b_os"]))
            items.append(dict(id="b.1", machine="b", label=" ".join(wz["v_b_label"].get().split())[:MON_LABEL_MAX],
                              px=px, scale=None, size_mm=tuple(layout.size_from_diagonal(
                                  self.mon_wiz_diag(wz["v_b_diag"].get()), "in", px)),
                              size_source="user", os_id=None, os_rect_px=None, order=10 ** 9))
        items.sort(key=lambda it: (it["machine"], it["order"]))
        floor = max(it["size_mm"][1] for it in items)
        x, screens = 0.0, []
        for it in items:
            it.pop("order")
            screens.append(layout.Screen(pos_mm=(round(x, 1), round(floor - it["size_mm"][1], 1)), **it))
            x += it["size_mm"][0] + 20.0
        return layout.normalize(layout.Layout(machines=tuple(machines), screens=tuple(screens), revision=0, extra={}))

    def mon_wiz_sig(self):
        wz = self.mon_wiz
        vals = [wz["count"], wz["b_os"], wz["v_name"].get()] + [r[k].get() for r in wz["rows"] for k in ("v_label", "v_diag")]
        if wz["count"] == 2:
            vals += [wz[k].get() for k in ("v_b_name", "v_b_host", "v_b_label", "v_b_diag", "v_b_px")]
        return tuple(vals)

    def mon_wiz_arrange(self, inner):
        wz = self.mon_wiz
        sig = self.mon_wiz_sig()
        if wz["draft"] is None or wz["sig"] != sig:  # ответы на прошлых шагах изменились — собрать заново
            try:
                wz["draft"], wz["sig"] = self.mon_wiz_build(), sig
            except Exception as e:
                plat.log(f"monitors: мастер — {type(e).__name__}: {e}")
                return self.mon_note(inner, f"Схема не собралась ({type(e).__name__}) — вернись на шаг назад",
                                     C["red"], self.f_small)
        self.mon_note(inner, "Курсор переходит по прямой — поставь экраны так же, как они стоят на столе: "
                             "какой выше, какой правее. Края притягиваются друг к другу.", C["ink"], self.f_small)
        tk.Frame(inner, bg=C["item"], height=int(6 * self.s)).pack(fill="x")
        try:
            ed = layout_editor.LayoutEditor(inner, colors=C, fonts=self.mon_fonts(), scale=self.s,
                                            on_change=lambda lay: wz.update(draft=lay), on_select=lambda sid: None,
                                            on_drag=lambda on: self.mon_set_hold("drag", on), height=260)
        except Exception as e:
            plat.log(f"monitors: LayoutEditor — {type(e).__name__}: {e}")
            return self.mon_note(inner, f"Схема не построилась ({type(e).__name__})", C["red"], self.f_small)
        ed.pack(fill="x")
        ed.set_layout(wz["draft"])
        self.mon_editor = ed
        self.mon_legend(inner)

    def mon_wiz_finish(self):
        wz = self.mon_wiz
        if not wz or wz["draft"] is None:
            return
        res = self.mon_call("save_layout", wz["draft"])
        if not self.mon_ok(res):
            return self.mon_fail(res, "Не сохранено")
        res2 = self.mon_call("set_this_machine", "a")
        if not self.mon_ok(res2):
            self.mon_fail(res2, "Этот компьютер не запомнен")
        self.mon_save_warn = [str(w) for w in res.get("warnings") or []]
        self.mon_wiz, self.mon_sel = None, None
        self.mon_draft, self.mon_dirty = self.mon_load_layout() or wz["draft"], False
        self.mon_refresh_snap()
        self.set_tab("monitors")
        self.flash("Готово — раскладка сохранена. Помощник курсора включается внизу вкладки")

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
        self.shown, self.by_hotkey, self.by_notify = True, by_hotkey, False
        self.last_inside = time.time()
        if plat.WITHDRAW_WHEN_HIDDEN:
            self.root.deiconify()
        try:
            self.root.attributes("-topmost", True)
        except tk.TclError:
            pass
        self.slide(self.top)
        self.root.after(60, self.root.focus_force)
        if self.tab in ("clip", "sessions", "downloads"):
            self.set_tab(self.tab)
        elif self.tab == "net" and self.net_ui:  # без перестройки: идущая проверка и вывод остаются
            self.net_refresh()
        elif self.tab == "monitors" and self.mon_ui:  # без перестройки: черновик и выбор остаются
            self.mon_paint_status()

    def hide(self):
        if not self.shown:
            return
        self.shown = self.by_notify = False
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
                self.by_hotkey = self.by_notify = False  # мышь зашла в панель — дальше обычное поведение
            elif not self.by_hotkey and not self.by_notify and time.time() - self.last_inside > 0.7:
                self.hide()
        self.root.after(80, self.tick_mouse)

    def tick_office(self):
        if self.shown and self.tab == "sessions" and self.office:
            self.frame += 1
            self.draw_office()
        self.root.after(160, self.tick_office)

    def tick_sessions(self):
        """Сессии читаем всегда (уведомления нужны и при спрятанной панели) — в фоновом потоке:
        glob и чтение jsonl на OneDrive/медленном диске не должны подвешивать интерфейс."""
        if not self.sess_reading:
            self.sess_reading = True
            self.run_bg(lambda: read_sessions(), self.sessions_read)
        self.root.after(2000, self.tick_sessions)

    def sessions_read(self, sessions, err):
        """Результат фонового read_sessions — в потоке Tk: уведомления и обновление вкладки."""
        self.sess_reading = False
        if err is not None:
            plat.log(f"read_sessions: {type(err).__name__}")
            return
        first = not self.sess_loaded
        self.sessions, self.sess_loaded = sessions, True
        self.watch_sessions(sessions)
        if self.shown and self.tab == "sessions":
            if first:  # до первого чтения вкладка показывала «Ищу сессии…»
                return self.set_tab("sessions")
            self.recent.refresh(s["id"] for s in self.sessions)
            if self.signature() != self.sess_sig:
                self.set_tab("sessions")
            else:
                self.refresh_ago()
                if self.frame % 15 == 0:
                    self.render_limits()

    def tick_downloads(self):
        if self.shown and self.tab == "downloads" and self.dl_ui:
            self.dl_refresh()
            self.dl_render()  # без изменений — только «N мин назад»
        self.root.after(DL_TICK, self.tick_downloads)

    def tick_server(self):
        if self.srv_rebuild and not (self.shown and self.tab == "net" and self.net_busy):
            self.srv_rebuild = False
            if self.shown and self.tab == "net":
                self.set_tab("net")
        elif self.shown and self.tab == "net" and self.srv_ui:
            self.server_render()
        self.root.after(1000, self.tick_server)

    def tick_monitors(self):
        if self.shown and self.tab == "monitors" and self.mon_ui:
            self.mon_paint_status()
            self.mon_track_cursor()
        if self.mon_pending:
            self.mon_paint_banner()
        self.root.after(MON_TICK, self.tick_monitors)

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
                added = False
                if text.strip():
                    self.push_history(text)
                    added = True
                else:  # текста нет — может, скопировали картинку (Win+Shift+S, «Копировать изображение»…)
                    grab = plat_fn("clipboard_image")
                    img = None
                    if grab:
                        try:
                            img = grab()
                        except Exception as e:
                            plat.log(f"clipboard_image: {type(e).__name__}")
                    if img is not None:
                        added = self.push_image(img, "картинка") is not None
                if added and self.shown and self.tab == "clip":
                    self.set_tab("clip")
        self.root.after(400, self.tick_clipboard)

    def tick_queue(self):
        try:
            while True:
                ev = self.q.get_nowait()
                if isinstance(ev, tuple) and ev[0] == "call":  # результат фоновой задачи (run_bg)
                    try:
                        ev[1](ev[2], ev[3])
                    except Exception as e:
                        plat.log(f"run_bg: {type(e).__name__}: {e}")
                elif ev == "toggle":
                    self.hide() if self.shown else self.show(by_hotkey=True)
                elif ev == "hotkey-failed":
                    self.flash(f"{plat.HOTKEY_LABEL} занят другой программой", C["accent"])
                elif ev == "hotkey-unavailable":
                    self.flash(f"{plat.HOTKEY_LABEL} недоступна — открывай через верхний край экрана", C["accent"])
                elif isinstance(ev, tuple) and len(ev) == 3 and ev[0] == "monitors":  # события MonitorsCtl (§2.4)
                    try:
                        self.mon_event(ev[1], ev[2])
                    except Exception as e:
                        plat.log(f"monitors: событие {ev[1]!r} — {type(e).__name__}: {e}")
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


def demo_image(w=812, h=430):
    """Картинка для демо буфера: «окно» с полосками, как будто скриншот."""
    from PIL import ImageDraw
    im = Image.new("RGB", (w, h), C["wall"])
    dr = ImageDraw.Draw(im)
    dr.rectangle((0, 0, w, 36), fill=C["head"])
    for i, col in enumerate((C["red"], C["accent"], C["mint"])):
        dr.ellipse((14 + i * 22, 12, 26 + i * 22, 24), fill=col)
    for i in range(8):
        dr.rectangle((30, 64 + i * 40, 30 + (i * 97 % 500) + 160, 84 + i * 40), fill=AGENT_COLORS[i % len(AGENT_COLORS)])
    return im


def self_test(tab, out):
    """Для проверки: показать панель на вкладке и сохранить скриншот.

    tab: sessions | music | clip | downloads | net (server — старое имя) | notify (уведомления поверх спрятанной панели).
    net: в «Проверке» запускается ping; с --demo — ещё и traceroute (второй снимок *_trace.png, пока он идёт).
    Внешний IP (api.ipify.org) в тесте запрашивается только с --demo, и то подменённый.
    """
    global read_sessions, Media, monitor, downloads, netinfo, monitors_ctl
    if tab == "server":
        tab = "net"
    Limits.fetch = fake_limits_fetch  # в тестах API лимитов не дёргаем
    demo_mode = "--demo" in sys.argv
    fake_items = []
    if demo_mode:
        now = time.time()
        types = [("frontend-dev", True), ("backend-dev", True), ("controller", False), ("qa-tester", True), ("code-reviewer", True)]
        demo = [
            {"id": "11111111-1111-4111-8111-111111111111", "name": "my-app · Рефакторинг", "folder": "my-app",
             "title": "Рефакторинг", "cwd": "", "busy": True, "updated": now - 20, "vscode": True,
             "attn": None, "attn_detail": "", "agents_done": set(),
             "agents": [{"id": f"agent-{i}", "desc": t, "type": agent_label({"agentType": t}), "busy": b}
                        for i, (t, b) in enumerate(types)]},
            {"id": "44444444-4444-4444-8444-444444444444", "name": "website · Деплой", "folder": "website",
             "title": "Деплой", "cwd": "", "busy": False, "updated": now - 40, "vscode": False,
             "attn": "permission", "attn_detail": "Bash: git push origin main", "agents_done": set(), "agents": []},
            {"id": "22222222-2222-4222-8222-222222222222", "name": "notes · Заметки", "folder": "notes",
             "title": "Заметки", "cwd": "", "busy": False, "updated": now - 900, "vscode": True,
             "attn": None, "attn_detail": "", "agents_done": set(), "agents": []},
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

        # «Загрузки»: 10 файлов разных типов (пути выдуманные — открыть/перетащить их нельзя)
        fake_dir = Path.home() / "Downloads-demo"
        files = [("Отчёт за квартал.pdf", 2_400_000, 120), ("photos-2026", None, 600), ("backup.zip", 812_000_000, 2400),
                 ("скриншот экрана.png", 384_000, 3600), ("Договор поставки.docx", 54_000, 7200),
                 ("бюджет 2027.xlsx", 31_000, 10800), ("демо-ролик.mp4", 145_000_000, 86400),
                 ("setup-tool.exe", 22_000_000, 90000), ("notes.txt", 1_200, 172800), ("data.bin", 7_000_000, 400000)]
        fake_items = [{"path": str(fake_dir / n), "name": n, "is_dir": sz is None, "size": sz, "mtime": now - dt}
                      for n, sz, dt in files]
        real_human = downloads.human_size if downloads else None

        class FakeDownloads:
            human_size = staticmethod(real_human) if real_human else None
            downloads_dir = staticmethod(lambda: fake_dir)
            list_downloads = staticmethod(lambda limit=300, folder=None: fake_items[:limit])
        downloads = FakeDownloads
        Panel.dl_dir_mtime = lambda self: 1.0

        # «Сервер»: один в сети с ssh (RAID деградировал), второй не отвечает
        CONFIG["servers"] = [{"name": "файл-сервер", "host": "192.0.2.10", "ssh": "admin@192.0.2.10"},
                             {"name": "резерв", "host": "192.0.2.20"}]

        class FakeMonitor:
            def __init__(self, servers):
                self.snap = [
                    {"name": "файл-сервер", "host": "192.0.2.10", "up": True, "ms": 3.4, "checked": now - 7,
                     "ssh": {"ok": True, "err": None, "load": (0.42, 0.38, 0.31), "mem_pct": 63.2,
                             "disks": [{"mount": "/", "pct": 41, "free": 61 * 1024 ** 3},
                                       {"mount": "/srv/backup", "pct": 82, "free": 330 * 1024 ** 3},
                                       {"mount": "/var/log", "pct": 94, "free": 1.1 * 1024 ** 3}],
                             "raid": "degraded: md0 [U_]", "uptime": "12 days, 4 hours", "checked": now - 40}},
                    {"name": "резерв", "host": "192.0.2.20", "up": False, "ms": None, "checked": now - 12, "ssh": None},
                ]

            def start(self):
                pass

            def stop(self):
                pass

            def refresh(self):
                pass

            def snapshot(self):
                return [dict(x) for x in self.snap]

        class FakeMonitorModule:
            Monitor = FakeMonitor
        monitor = FakeMonitorModule

        # «Сеть»: выдуманный компьютер и результаты проверок — без внешних запросов
        class FakeNetinfo:
            @staticmethod
            def local_info():
                time.sleep(0.2)
                return {"hostname": "OFFICE-PC-07", "gateway": "192.168.1.1", "dns": ["192.168.1.1", "1.1.1.1"],
                        "primary_ip": "192.168.1.42", "err": None, "interfaces": [
                            {"name": "Ethernet", "ipv4": ["192.168.1.42"], "ipv6": [], "mac": "00:00:5E:00:53:01",
                             "up": True, "kind": "ethernet", "ssid": None},
                            {"name": "Wi-Fi", "ipv4": ["192.168.1.57"], "ipv6": ["fe80::1"], "mac": "00:00:5E:00:53:02",
                             "up": True, "kind": "wifi", "ssid": "Office-Demo"},
                            {"name": "WireGuard (офис)", "ipv4": ["10.8.0.3"], "ipv6": [], "mac": None,
                             "up": True, "kind": "vpn", "ssid": None},
                            {"name": "Bluetooth", "ipv4": [], "ipv6": [], "mac": None, "up": False, "kind": "other",
                             "ssid": None}]}

            @staticmethod
            def public_ip(timeout=4):
                time.sleep(0.3)
                return "203.0.113.45"

            @staticmethod
            def ping(host, count=4):
                time.sleep(0.5)
                return {"ok": True, "sent": count, "received": count, "loss_pct": 0.0, "min": 1.0, "avg": 1.4,
                        "max": 2.1, "err": None}

            @staticmethod
            def traceroute(host, max_hops=20, on_hop=None, cancel=None):
                route = [("192.168.1.1", 1.0), ("10.20.0.1", 4.2), (None, None), ("198.51.100.17", 11.6),
                         ("198.51.100.94", 12.3), ("203.0.113.9", 18.8), ("192.0.2.10", 19.4)]
                hops = []
                for n, (ip, ms) in enumerate(route[:max_hops], 1):
                    if cancel is not None and cancel.wait(0.4):
                        break
                    d = {"hop": n, "host": ip or "*", "ip": ip, "ms": ms}
                    hops.append(d)
                    if on_hop:
                        on_hop(dict(d))
                return hops

            @staticmethod
            def check_port(host, port, timeout=3):
                return {"open": True, "ms": 12.0, "err": None}

            @staticmethod
            def dns_lookup(name):
                return {"ipv4": ["192.0.2.10"], "ipv6": ["2001:db8::10"], "err": None}
        netinfo = FakeNetinfo

        # «Мониторы»: раскладка пользователя (§7.6) — ноут A 15,6″ 1920×1080 125 %, монитор 27″ выше и правее,
        # ноут B 15,6″ справа. Deskflow и Apollo «не установлены», помощник выключен; ничего не запускается.
        if layout is not None:
            demo_monitors = {
                "version": 1, "revision": 7,
                "machines": [{"id": "a", "name": "laptop-a", "hostname": "LAPTOP-A", "host": "192.0.2.11", "os": "windows"},
                             {"id": "b", "name": "laptop-b", "hostname": "LAPTOP-B", "host": "192.0.2.12", "os": "windows"}],
                "screens": [
                    {"id": "a.1", "machine": "a", "label": "Ноут A", "os_id": "win:SDC4161:00000000:UID4352",
                     "px": [1920, 1080], "scale": 125, "size_mm": [345.4, 194.3], "size_source": "edid",
                     "pos_mm": [0.0, 220.0], "os_rect_px": [0, 0, 1920, 1080]},
                    {"id": "a.2", "machine": "a", "label": "Монитор 27″", "os_id": "win:GSM5B7F:0001E240",
                     "px": [2560, 1440], "scale": 100, "size_mm": [597.7, 336.2], "size_source": "user",
                     "pos_mm": [345.4, 0.0], "os_rect_px": [1920, -868, 2560, 1440]},
                    {"id": "b.1", "machine": "b", "label": "Ноут B", "os_id": "win:BOE0A1C:00000000:UID256",
                     "px": [1920, 1080], "scale": 125, "size_mm": [345.4, 194.3], "size_source": "edid",
                     "pos_mm": [943.1, 230.0], "os_rect_px": [0, 0, 1920, 1080]}],
                "edge_helper": {"enabled": False}}

            class FakeMonitorsCtl:  # контракт §5.5; snapshot — как §5.5.1
                def __init__(self, raw_cfg, config_path, data_dir, plat_mod, on_event=None):
                    self.on_event = on_event
                    self.lay = layout.parse_config(demo_monitors)[0]
                    self.edge_on = self.edge_paused = False

                def start(self):
                    pass

                def stop(self):
                    pass

                def layout(self):
                    return self.lay

                def snapshot(self):
                    return {"this_machine": "a", "mode": "off", "server": "a", "busy": False, "busy_text": "",
                            "layout_ok": self.lay is not None, "warnings": [],
                            "components": {
                                "deskflow": {"state": "missing", "admin_to_install": True,
                                             "url": "https://github.com/deskflow/deskflow/releases",
                                             "cmd": "winget install --id Deskflow.Deskflow -e", "err": None},
                                "apollo": {"state": "missing", "admin_to_install": True},
                                "moonlight": {"state": "na", "note": "нужен на ноуте B"},
                                "edge": {"state": "ok", "raw_input": True, "ease_cursor": False}},
                            "deskflow": {"running": False, "role": None, "connected": [], "err": None},
                            "stream": {"running": False, "virtual_display": None, "err": None},
                            "edge": {"enabled": self.edge_on, "running": self.edge_on, "paused": self.edge_paused,
                                     "user_paused": self.edge_paused, "warps": 132 if self.edge_on else 0,
                                     "last_warp": None},
                            "link": {"enabled": False, "peer_online": None, "err": None}}

                def local_displays(self):
                    return [{"os_id": "win:SDC4161:00000000:UID4352", "gdi_name": "\\\\.\\DISPLAY1", "label": "Ноут A",
                             "px": [1920, 1080], "rect_px": [0, 0, 1920, 1080], "primary": True, "scale": 125,
                             "size_mm": [345.4, 194.3], "virtual": False},
                            {"os_id": "win:GSM5B7F:0001E240", "gdi_name": "\\\\.\\DISPLAY2", "label": "LG 27UL500",
                             "px": [2560, 1440], "rect_px": [1920, -868, 2560, 1440], "primary": False, "scale": 100,
                             "size_mm": [600.0, 340.0], "virtual": False}]

                refresh_displays = local_displays

                def pause_edge_helper(self, paused):
                    self.edge_paused = bool(paused)
                    return {"ok": True, "code": None, "err": None, "paused": self.edge_paused, "running": self.edge_on}

                def save_layout(self, lay):
                    self.lay = lay
                    return {"ok": True, "err": None, "warnings": []}

                def set_this_machine(self, machine_id):
                    return {"ok": True, "err": None}

                def set_edge_helper(self, enabled):
                    self.edge_on = bool(enabled)
                    return {"ok": True, "err": None}

                def apply_os_positions(self):
                    return {"ok": True, "err": None, "token": "demo", "positions": {}}

                def confirm_os_positions(self, token):
                    return {"ok": True, "err": None}

                rollback_os_positions = confirm_os_positions

                def export_layout(self):
                    return json.dumps(layout.to_config(self.lay), ensure_ascii=False)

                def import_layout(self, text):
                    return {"ok": False, "code": "bad_layout", "err": "демо: вставка отключена"}

            class FakeMonitorsModule:
                MonitorsCtl = FakeMonitorsCtl
            monitors_ctl = FakeMonitorsModule
            if getattr(plat, "DATA_DIR", None) is None:  # фейку папка данных не нужна — ничего не пишет
                plat.DATA_DIR = Path.home() / "MyPanel-demo"
    p = Panel(test_tab="sessions" if tab == "notify" else tab)
    if tab == "clip":
        for s in ("ls -la", "git status", "Привет! Посмотри, пожалуйста, черновик."):
            p.push_history(s)
        if demo_mode:
            p.push_image(demo_image(), "скриншот")
            p.push_history("ssh admin@192.0.2.10")
    if tab == "downloads" and demo_mode:
        p.dl_sel = {fake_items[0]["path"], fake_items[2]["path"]}  # множественный выбор
    if tab == "notify":
        def fire():
            s0 = p.sessions[0] if p.sessions else {"name": "проект · сессия", "id": "", "cwd": ""}
            p.notify("done", s0, "Claude закончил")
            perm = next((x for x in p.sessions if x.get("attn")), s0)
            p.notify("permission", perm, "ждёт твоего разрешения")
        def fg():
            """(заголовок активного окна, принадлежит ли оно панели) — проверка, что выезд не крадёт фокус."""
            if sys.platform != "win32":
                return plat.foreground_title(), None
            import ctypes
            from ctypes import wintypes
            h = ctypes.windll.user32.GetForegroundWindow()
            pid = wintypes.DWORD()
            ctypes.windll.user32.GetWindowThreadProcessId(h, ctypes.byref(pid))
            return plat.foreground_title(), pid.value == os.getpid()
        st = {}
        p.root.after(3400, lambda: st.update(before=fg()))
        p.root.after(3500, fire)
        p.root.after(5900, lambda: print("активное окно до:", st.get("before"), "после выезда:", fg(), "shown:", p.shown, "y:", p.y, p.top, "note:", p.note_cur and p.note_cur["kind"], flush=True))
    else:
        p.pinned = True
        p.set_tab(p.tab)
        p.show()
    net_trace_shot = tab == "net" and demo_mode and netinfo is not None
    if tab == "net" and netinfo:
        p.net_hist = ["192.0.2.10", "example.com", "192.168.1.1"] if demo_mode else []
        p.net_addr.set("192.0.2.10" if demo_mode else "127.0.0.1")  # реальный режим — только локальный ping
        p.net_port.set("443")
        p.root.after(700, lambda: p.net_run("ping"))
        if demo_mode:
            p.root.after(900, p.net_public)
            p.root.after(6300, lambda: p.net_run("trace"))

    mon_extra = []  # (задержка мс, суффикс снимка, действие перед снимком)
    if tab == "monitors" and p.mon_editor is not None:
        def mon_demo():
            p.mon_sel = "a.2"
            p.mon_editor.select("a.2")
            p.mon_selected("a.2")
            p.mon_editor.set_current("a.1")
            p.mon_editor.show_exit("a.1", "right", 380.0)  # низ ноута A — пустая зона: «в угол» монитора
            if demo_mode:
                p.mon_start_pending("demo", MON_ROLLBACK_S)  # плашка отката «Применить к Windows»
        p.root.after(500, mon_demo)
        if demo_mode:
            def mon_helper_on():
                p.mon_confirm(True)
                p.mon_toggle_edge()  # только фейк: в демо реальный помощник не запускается
                p.mon_editor.show_exit("a.2", "right", 300.0)  # монитор → ноут B, напротив
                p.root.after(400, lambda: p.mon_ui and p.mon_ui["area"].master.yview_moveto(1.0))
            def mon_wizard():
                p.mon_ctl.lay, p.mon_draft, p.mon_wiz = None, None, None
                p.set_tab("monitors")
                p.root.after(300, lambda: p.mon_wiz_go(1))
            mon_extra = [(7000, mon_helper_on, "_on"), (8500, mon_wizard, "_wizard")]

    def grab_to(path):
        from PIL import ImageGrab
        p.root.update()
        x, y = p.root.winfo_rootx(), p.root.winfo_rooty()
        ImageGrab.grab(bbox=(x, y, x + p.W, y + p.H), all_screens=True).save(path)

    def shot():
        if tab == "sessions":
            p.render_limits()
        grab_to(out)
        if tab != "notify" and not net_trace_shot and not mon_extra:
            p.quit()
    p.root.after(6000, shot)
    for i, (ms, act, suffix) in enumerate(mon_extra):
        last = i == len(mon_extra) - 1
        p.root.after(ms, act)
        p.root.after(ms + 1200, lambda s=suffix, last=last: (grab_to(out.replace(".png", f"{s}.png")),
                                                             last and p.quit()))
    if net_trace_shot:  # traceroute ещё идёт — хопы приходят по одному, на кнопке «Стоп»
        p.root.after(8400, lambda: (grab_to(out.replace(".png", "_trace.png")), p.quit()))
    if tab == "notify":  # вторая плашка из очереди — красная «ждёт разрешения»
        p.root.after(7600, lambda: (grab_to(out.replace(".png", "_perm.png")), p.quit()))
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
