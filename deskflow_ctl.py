"""Управление Deskflow 1.27 (docs/monitors-spec.md §5.3, §6, §10; факты — docs/deskflow-1.27-notes.md).

Ядро запускается как `deskflow-core <server|client> -s settings.conf`; всё (имя, адрес, TLS, внешний конфиг связей)
задаётся INI-файлом настроек. Здесь: поиск и проверка версии, генерация настроек и конфига связей, проверка конфига
на loopback, свой сертификат TLS, отпечатки и trusted-файлы, запуск/остановка своего процесса и разбор его лога.

Публичные функции не бросают исключений (результат — словарь {"ok","code","err","hint"}), кроме build_settings /
build_server_config: они возвращают текст и бросают ValueError на заведомо неверных входных данных (вызывающий
код оборачивает их). Секреты (закрытый ключ) никуда не пишутся и не логируются. Чужие процессы Deskflow (GUI, служба)
не трогаем — только свой Popen / перенятый по pid с проверкой образа и времени создания.
"""
import base64
import collections
import hashlib
import ipaddress
import os
import re
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"
OS_KEY = "windows" if IS_WIN else "macos" if IS_MAC else "linux"
CREATE_NO_WINDOW = 0x08000000 if IS_WIN else 0
CREATE_NEW_PROCESS_GROUP = 0x00000200 if IS_WIN else 0

MIN_VERSION = (1, 27)
DEFAULT_PORT = 24800
RUN_TIMEOUT_S = 5
VALIDATE_TIMEOUT_S = 3.0
LOG_TAIL_LINES = 20
LOG_ROTATE_BYTES = 1024 * 1024
HELP_LIMIT = 2048
CORE_NAMES = ("deskflow-core", "deskflow-core.exe")

INSTALL = {
    "windows": {"url": "https://github.com/deskflow/deskflow/releases", "cmd": "winget install --id Deskflow.Deskflow -e",
                "admin": True, "note": "Установщик MSI требует прав администратора"},
    "macos": {"url": "https://github.com/deskflow/deskflow/releases", "cmd": "brew install --cask deskflow",
              "admin": False, "note": "Не проверено: после установки выдайте Deskflow доступ в «Универсальный доступ»"},
    "linux": {"url": "https://github.com/deskflow/deskflow/releases",
              "cmd": "flatpak install flathub org.deskflow.deskflow", "admin": False,
              "note": "Не проверено на этой версии; Linux-узел настраивается через Deskflow GUI"},
}

# ── Регулярки лога 1.27. Подтверждены вживую — см. notes; НП помечены ──
LOG_LINE_RE = re.compile(r"^\[(?P<ts>[^\]]+)\]\s+(?P<lvl>[A-Z]+[0-9]?):\s?(?P<msg>.*)$")
PEER_FP_RE = re.compile(r"peer fingerprint:\s*([0-9A-Fa-f:]{64,95})")
UNTRUSTED_RE = re.compile(r"does not match trusted|failed to verify server certificate fingerprint", re.I)
TRUSTED_OK_RE = re.compile(r"fingerprint matches trusted", re.I)
CLIENT_CONNECTED_RE = re.compile(r'client "(.+?)" has connected')
CLIENT_DISCONNECTED_RE = re.compile(r'client "(.+?)" has disconnected|disconnecting client "(.+?)"')  # НП
SERVER_CONNECTED_RE = re.compile(r"\bconnected to server\b")
SERVER_LOST_RE = re.compile(r"disconnected from server|lost connection to server|failed to connect to server", re.I)  # НП
SERVER_READY_RE = re.compile(r"started server, waiting for clients")
SWITCH_RE = re.compile(r'switch from "(.+)" to "(.+)" at (\d+),(\d+)')  # НП: не подтверждена на 1.27
READ_ERROR_RE = re.compile(r"read error|unknown computer name|cannot read configuration", re.I)

NAME_RE = re.compile(r"[A-Za-z0-9._][A-Za-z0-9._-]{0,62}")
HOST_RE = re.compile(r"[A-Za-z0-9._:\[\]-]{1,253}")
HOTKEY_RE = re.compile(r"^(Control|Alt|Shift|Super)(\+(Control|Alt|Shift|Super))*\+[A-Za-z0-9]{1,3}$")
LOG_LEVELS = ("FATAL", "ERROR", "WARNING", "NOTE", "INFO", "DEBUG", "DEBUG1", "DEBUG2")
SIDE_WORD = {"left": "left", "right": "right", "top": "up", "bottom": "down"}
SIDE_ORDER = {"left": 0, "right": 1, "top": 2, "bottom": 3}
DEFAULT_HOTKEYS = {"work_on": "Control+Alt+Shift+A", "back": "Control+Alt+Shift+B",
                   "lock": "Control+Alt+Shift+F11", "unlock": "Control+Alt+Shift+F12"}

# Папка trusted-* переопределяется для тестов (чтобы не писать в C:/ProgramData). None — стандартная по ОС.
TRUSTED_DIR_OVERRIDE = None


def log(msg):
    try:
        if sys.stderr:
            print(f"[panel/deskflow] {msg}", file=sys.stderr, flush=True)
    except Exception:
        pass


def _err(code, text, hint=None, **extra):
    d = {"ok": False, "code": code, "err": _scrub(text), "hint": hint}
    d.update(extra)
    return d


def _ok(**extra):
    d = {"ok": True, "code": None, "err": None}
    d.update(extra)
    return d


def _scrub(text, limit=200):
    """Без путей профиля пользователя, до 200 символов."""
    s = str(text or "")
    try:
        home = str(Path.home())
        for h in {home, home.replace("\\", "/")}:
            if h:
                s = s.replace(h, "~")
    except Exception:
        pass
    s = s.replace("\r", " ").replace("\n", " ").strip()
    return s if len(s) <= limit else s[:limit - 1] + "…"


def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _clean_env():
    return {k: v for k, v in os.environ.items() if not k.upper().startswith("PYTHON")}


def _fwd(p):
    return str(p).replace("\\", "/")


# ── Запуск коротких команд ──
def _run(argv, timeout=RUN_TIMEOUT_S):
    """(returncode, text). Не бросает; по таймауту дочерний процесс убивается subprocess.run."""
    try:
        r = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           timeout=timeout, creationflags=CREATE_NO_WINDOW, env=_clean_env(), shell=False)
        return r.returncode, r.stdout.decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        return None, "timeout"
    except (OSError, ValueError) as e:
        return None, type(e).__name__


# ── Поиск и диалект ──
def _is_core_name(path):
    return os.path.basename(str(path)).lower() in CORE_NAMES


def _search_dirs():
    """Стандартные места (§10.3 п. 2) и затем PATH (только абсолютные записи — без текущей папки)."""
    dirs = []
    if IS_WIN:
        for var in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"):
            base = os.environ.get(var)
            if base:
                dirs.append(os.path.join(base, "Deskflow"))
    elif IS_MAC:
        dirs += ["/Applications/Deskflow.app/Contents/MacOS", "/opt/homebrew/bin", "/usr/local/bin"]
    else:
        dirs += ["/usr/bin", "/usr/local/bin"]
    for d in os.environ.get("PATH", "").split(os.pathsep):
        d = d.strip().strip('"')
        if d and os.path.isabs(d):
            dirs.append(d)
    seen, out = set(), []
    for d in dirs:
        k = os.path.normcase(os.path.normpath(d))
        if k not in seen:
            seen.add(k)
            out.append(d)
    return out


def _system_dir_warning(path):
    roots = [os.environ.get("ProgramFiles", ""), os.environ.get("ProgramW6432", ""),
             os.environ.get("ProgramFiles(x86)", ""), "/Applications", "/opt/homebrew", "/usr/local", "/usr/bin"]
    p = os.path.normcase(os.path.realpath(path))
    for r in roots:
        if r and p.startswith(os.path.normcase(os.path.realpath(r)) + os.sep):
            return None
    return "Путь к deskflow-core вне системных папок"


def parse_dialect(version_text, help_text):
    """Чистый разбор вывода --version и --help (тесты идут по фикстурам)."""
    vt, ht = version_text or "", help_text or ""
    m = re.search(r"v?(\d+)\.(\d+)\.(\d+)", vt) or re.search(r"v?(\d+)\.(\d+)\.(\d+)", ht)
    res = {"ok": False, "code": None, "err": None, "hint": None, "state": "unknown_dialect", "version": None,
           "form": None, "server_argv": None, "client_argv": None, "settings_flag": None,
           "new_instance_flag": None, "decimals": None, "help": ht[:HELP_LIMIT]}
    if not m:
        res.update(code="dialect_unknown", err="Не удалось определить версию Deskflow")
        return res
    ver = tuple(int(x) for x in m.groups())
    res["version"] = ".".join(str(x) for x in ver)
    if ver[:2] < MIN_VERSION:
        res.update(state="old", code="version_unsupported",
                   err=f"Нужен Deskflow {MIN_VERSION[0]}.{MIN_VERSION[1]} или новее, найден {res['version']}",
                   hint=INSTALL[OS_KEY]["cmd"])
        return res
    sub = bool(re.search(r"\bcoremode\b", ht)) or bool(re.search(r"either:\s*server or client", ht))
    sflag = "-s" if re.search(r"(^|\s)-s[,\s]", ht) else ("--settings" if "--settings" in ht else None)
    if not sub or not sflag:
        res.update(code="dialect_unknown", err="Незнакомый формат командной строки Deskflow")
        return res
    res.update(ok=True, state="ok", form="subcommand", settings_flag=sflag,
               new_instance_flag="--new-instance" if "--new-instance" in ht else None,
               server_argv=["{core}", "server", sflag, "{settings}"],
               client_argv=["{core}", "client", sflag, "{settings}"],
               decimals=2)   # notes: дробные проценты с двумя знаками прочитаны успешно
    return res


def probe_dialect(path):
    """Запускает --version и --help (по 5 с, без окна) и разбирает вывод."""
    rc, vt = _run([str(path), "--version"])
    if rc is None:
        return _err("spawn_failed" if vt != "timeout" else "timeout", "deskflow-core не запустился",
                    state="unknown_dialect", version=None, form=None, help="")
    rc2, ht = _run([str(path), "--help"])
    if rc2 is None:
        ht = ""
    return parse_dialect(vt, ht)


def find_deskflow(cfg_path=None):
    """{"ok","code","err","hint","state","path","version","dialect","warning"}; state: ok/missing/old/unknown_dialect."""
    inst = INSTALL[OS_KEY]
    miss = {"ok": False, "code": "not_found", "err": "Deskflow не найден", "hint": inst["cmd"], "state": "missing",
            "path": None, "version": None, "dialect": None, "warning": None}
    cands, warn = [], None
    if cfg_path:
        p = str(cfg_path)
        if not os.path.isabs(p) or not os.path.isfile(p) or not _is_core_name(p):
            return {**miss, "code": "bad_config", "err": "deskflow.path: нужен абсолютный путь к deskflow-core"}
        cands.append(p)
        warn = _system_dir_warning(p)
    for d in _search_dirs():
        for n in CORE_NAMES:
            f = os.path.join(d, n)
            if os.path.isfile(f):
                cands.append(f)
    seen, last = set(), None
    for p in cands:
        k = os.path.normcase(os.path.realpath(p))
        if k in seen:
            continue
        seen.add(k)
        d = probe_dialect(p)
        if d.get("version") is None and not d.get("ok"):
            last = {**miss, "code": d.get("code") or "spawn_failed", "err": d.get("err") or "Не запустился",
                    "state": "unknown_dialect", "path": p}
            continue
        return {"ok": bool(d.get("ok")), "code": d.get("code"), "err": d.get("err"), "hint": d.get("hint"),
                "state": d["state"], "path": p, "version": d.get("version"), "dialect": d, "warning": warn}
    return last or miss


def build_argv(role, path, dialect, *, settings_file, new_instance=False):
    """[core, role, -s, settings] для диалекта subcommand; без shell."""
    if role not in ("server", "client"):
        raise ValueError("role")
    if not dialect or dialect.get("form") != "subcommand":
        raise ValueError("диалект не поддерживается")
    argv = [str(path), role, dialect.get("settings_flag") or "-s", str(settings_file)]
    if new_instance:
        argv.append(dialect.get("new_instance_flag") or "--new-instance")
    return argv


# ── Генерация текстов ──
def _check_name(v):
    if not isinstance(v, str) or not NAME_RE.fullmatch(v):
        raise ValueError(f"недопустимое имя экрана: {v!r}")
    return v


def _check_host(v, what="host"):
    if not isinstance(v, str) or not HOST_RE.fullmatch(v) or v.startswith("-"):
        raise ValueError(f"недопустимый {what}: {v!r}")
    return v


def _q(v):
    """Значение INI: Qt трактует запятую как список, поэтому такие значения берём в кавычки."""
    s = str(v)
    if "\n" in s or "\r" in s or '"' in s:
        raise ValueError("недопустимые символы в значении настроек")
    if "," in s or s != s.strip():
        return f'"{s}"'
    return s


def _b(v):
    return "true" if v else "false"


def build_settings(role, *, name, machines, bind_host=None, server_host=None, port=DEFAULT_PORT, tls=True,
                   cert_path=None, links_path=None, clipboard=True, log_level="DEBUG"):
    """Файл настроек Qt-INI для `-s` (notes). Детерминированный. machines — имена всех экранов."""
    if role not in ("server", "client"):
        raise ValueError("role")
    _check_name(name)
    names = [_check_name(n) for n in machines]
    if name not in names:
        names.append(name)
    if not isinstance(port, int) or isinstance(port, bool) or not 1024 <= port <= 65535:
        raise ValueError("port")
    if log_level not in LOG_LEVELS:
        raise ValueError("log_level")
    lines = ["[core]", f"coreMode={2 if role == 'server' else 1}", f"computerName={name}"]
    if role == "server":
        _check_host(bind_host, "bind_host")
        if bind_host in ("0.0.0.0", "::", "[::]"):
            raise ValueError("сервер не должен слушать все интерфейсы")
        lines.append(f"interface={bind_host}")
    lines.append(f"port={port}")
    if role == "client":
        _check_host(server_host, "server_host")
        lines += ["[client]", f"remoteHost={server_host}"]
    lines += ["[security]", f"tlsEnabled={_b(tls)}"]
    if tls:
        if not cert_path:
            raise ValueError("cert_path")
        lines.append(f"certificate={_q(_fwd(cert_path))}")
    lines.append(f"checkPeerFingerprints={_b(tls)}")
    if role == "server":
        if not links_path:
            raise ValueError("links_path")
        lines += ["[server]", "externalConfig=true", f"externalConfigFile={_q(_fwd(links_path))}",
                  f"enableClipboard={_b(clipboard)}"]
    lines += ["[log]", f"level={log_level}"]
    for n in names:
        lines += [f"[computer_{n}]", f"name={n}"]
    return "\n".join(lines) + "\n"


def _pct(v):
    s = f"{float(v):.2f}".rstrip("0").rstrip(".")
    return s if s not in ("", "-0") else "0"


def _hotkey(combo):
    """'Control+Alt+Shift+F11' → 'control+alt+shift+F11' (модификаторы строчными, одиночная буква — строчная)."""
    if not isinstance(combo, str) or not HOTKEY_RE.match(combo):
        return None
    parts = combo.split("+")
    key = parts[-1]
    if len(key) == 1 and key.isalpha():
        key = key.lower()
    return "+".join([p.lower() for p in parts[:-1]] + [key])


def build_server_config(layout, links, options=None):
    """Внешний конфиг сервера: только section: links (+ section: options с горячими клавишами). Без section: screens."""
    options = options or {}
    names = {m.id: _check_name(m.name) for m in layout.machines}
    out = ["section: links"]
    for m in layout.machines:
        mine = [lk for lk in links if lk.src_machine == m.id and lk.dst_machine in names]
        if not mine:
            continue
        mine.sort(key=lambda lk: (SIDE_ORDER[lk.side], lk.src_from_pct))
        out.append(f"\t{names[m.id]}:")
        for lk in mine:
            out.append(f"\t\t{SIDE_WORD[lk.side]}({_pct(lk.src_from_pct)},{_pct(lk.src_to_pct)}) = "
                       f"{names[lk.dst_machine]}({_pct(lk.dst_from_pct)},{_pct(lk.dst_to_pct)})")
    out.append("end")
    extra = getattr(layout, "extra", None) or {}
    dfx = extra.get("deskflow") or {}
    hk = dict(DEFAULT_HOTKEYS)
    hk.update(dfx.get("work_on_hotkeys") or {})
    sid = options.get("server") or dfx.get("server")
    server = next((m for m in layout.machines if m.id == sid), None) or (layout.machines[0] if layout.machines else None)
    others = [m for m in layout.machines if server is not None and m.id != server.id]
    opts, used = [], set()

    def add(key, action):
        k = _hotkey(hk.get(key))
        if k and action and k.lower() not in used:
            used.add(k.lower())
            opts.append(f"\tkeystroke({k}) = {action}")
    if options.get("relative_moves"):
        opts.append("\trelativeMouseMoves = true")
    if others:
        add("work_on", f"switchToScreen({names[others[0].id]})")
    if server is not None and others:
        add("back", f"switchToScreen({names[server.id]})")
    if others:
        add("lock", "lockCursorToScreen(on)")
        add("unlock", "lockCursorToScreen(off)")
    if opts:
        out += ["section: options"] + opts + ["end"]
    return "\n".join(out) + "\n"


# ── Отпечатки и trusted-файлы ──
def normalize_fp(fp):
    """Любой вид (с двоеточиями, пробелами, 'SHA256:', 'v2:sha256:', регистр) → 64 hex строчными или None."""
    if not isinstance(fp, str) or len(fp) > 200:
        return None
    s = fp.strip()
    s = re.sub(r"^(v\d+:)?sha-?256[:=\s]+", "", s, flags=re.I)
    s = re.sub(r"[\s:\-]", "", s)
    return s.lower() if re.fullmatch(r"[0-9A-Fa-f]{64}", s) else None


def format_fp(fp):
    h = normalize_fp(fp)
    return ":".join(h[i:i + 2] for i in range(0, 64, 2)).upper() if h else None


def _pem_cert_der(pem_text):
    m = re.search(r"-----BEGIN CERTIFICATE-----.+?-----END CERTIFICATE-----", pem_text, re.S)
    if not m:
        return None
    return ssl.PEM_cert_to_DER_cert(m.group(0))


def local_fingerprint(cert_path):
    """SHA-256 от DER сертификата: {"ok","fp" (hex строчными),"fp_human" ("AB:CD:…"),"cert_path"}."""
    try:
        text = Path(cert_path).read_text(encoding="ascii", errors="replace")
    except FileNotFoundError:
        return _err("not_found", "Сертификат ещё не создан")
    except OSError as e:
        return _err("os_error", f"Сертификат не прочитан: {type(e).__name__}")
    try:
        der = _pem_cert_der(text)
    except Exception:
        der = None
    if not der:
        return _err("bad_config", "В файле нет сертификата")
    h = hashlib.sha256(der).hexdigest()
    return _ok(fp=h, fp_human=format_fp(h), cert_path=str(cert_path))


def trusted_dir():
    if TRUSTED_DIR_OVERRIDE:
        return Path(TRUSTED_DIR_OVERRIDE)
    if IS_WIN:
        return Path(os.environ.get("ProgramData", "C:/ProgramData")) / "Deskflow" / "tls"
    if IS_MAC:   # НП: не проверено на macOS
        return Path.home() / "Library" / "Deskflow" / "tls"
    return Path.home() / ".config" / "Deskflow" / "tls"   # НП


def trusted_path(role, trusted_dir_=None):
    """server читает trusted-clients, client читает trusted-servers."""
    if role not in ("server", "client"):
        raise ValueError("role")
    base = Path(trusted_dir_) if trusted_dir_ else trusted_dir()
    return base / ("trusted-clients" if role == "server" else "trusted-servers")


def trusted_fingerprints(role, trusted_dir_=None):
    try:
        text = trusted_path(role, trusted_dir_).read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        return []
    out = []
    for line in text.splitlines():
        h = normalize_fp(line)
        if h and h not in out:
            out.append(h)
    return out


def _atomic_write_text(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


def add_trusted(role, fp, trusted_dir_=None):
    """Дописать отпечаток в trusted-clients (role=server) / trusted-servers (role=client). Без дублей."""
    if role not in ("server", "client"):
        return _err("bad_config", "Неизвестная роль")
    h = normalize_fp(fp)
    if not h:
        return _err("bad_config", "Отпечаток должен быть 64 шестнадцатеричных символа (SHA-256)")
    path = trusted_path(role, trusted_dir_)
    line = f"v2:sha256:{h}"
    try:
        try:
            old = path.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            old = ""
        if h in [normalize_fp(x) for x in old.splitlines()]:
            return _ok(path=str(path), added=False, line=line)
        text = old if (not old or old.endswith("\n")) else old + "\n"
        text += line + "\n"
        try:
            _atomic_write_text(path, text)
        except PermissionError:
            with open(path, "a", encoding="utf-8", newline="\n") as f:   # папка не даёт заменить файл — пробуем дописать
                if old and not old.endswith("\n"):
                    f.write("\n")
                f.write(line + "\n")
        log(f"отпечаток {h[:8]}… добавлен в {path.name}")
        return _ok(path=str(path), added=True, line=line)
    except PermissionError:
        return _err("admin_required", "Нет прав на запись в папку доверенных отпечатков Deskflow",
                    f"Добавьте строку вручную в {path.name} (от администратора): {line}", line=line)
    except OSError as e:
        return _err("os_error", f"Отпечаток не записан: {type(e).__name__}")


# ── Сертификат TLS ──
def _restrict_key_file(path):
    try:
        if IS_WIN:
            user = os.environ.get("USERNAME")
            if user:
                _run(["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:(F)"], timeout=5)
        else:
            os.chmod(path, 0o600)
    except Exception:
        pass


def _find_openssl():
    names = ("openssl.exe", "openssl") if IS_WIN else ("openssl",)
    dirs = [d for d in _search_dirs_path()]
    if IS_WIN:
        pf = os.environ.get("ProgramFiles", "C:\\Program Files")
        dirs += [os.path.join(pf, "Git", "usr", "bin"), os.path.join(pf, "Git", "mingw64", "bin")]
    for d in dirs:
        for n in names:
            f = os.path.join(d, n)
            if os.path.isfile(f):
                return f
    return None


def _search_dirs_path():
    return [d.strip().strip('"') for d in os.environ.get("PATH", "").split(os.pathsep)
            if d.strip() and os.path.isabs(d.strip().strip('"'))]


def _gen_cert_cryptography():
    from datetime import timedelta, timezone
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    nm = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Deskflow")])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(nm).issuer_name(nm).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(days=1))
            .not_valid_after(now + timedelta(days=3650)).sign(key, hashes.SHA256()))
    pem_key = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption())
    return pem_key + cert.public_bytes(serialization.Encoding.PEM)


def _gen_cert_openssl(openssl):
    with tempfile.TemporaryDirectory(prefix="mp-tls-") as td:
        k, c = os.path.join(td, "k.pem"), os.path.join(td, "c.pem")
        rc, out = _run([openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-subj", "/CN=Deskflow",
                        "-days", "3650", "-keyout", k, "-out", c], timeout=30)
        if rc != 0 or not os.path.isfile(k) or not os.path.isfile(c):
            raise OSError("openssl не создал сертификат")
        return Path(k).read_bytes() + b"\n" + Path(c).read_bytes()


def ensure_certificate(data_dir):
    """data_dir/deskflow/tls/deskflow.pem (ключ + сертификат). Есть — не трогаем. {"ok","path","created",...}."""
    path = Path(data_dir) / "deskflow" / "tls" / "deskflow.pem"
    try:
        if path.is_file() and path.stat().st_size > 0:
            fp = local_fingerprint(path)
            if fp.get("ok"):
                return _ok(path=str(path), created=False, fp=fp["fp"], fp_human=fp["fp_human"])
        pem = None
        try:
            import cryptography  # noqa: F401
            pem = _gen_cert_cryptography()
        except ImportError:
            pass
        except Exception as e:
            log(f"cryptography: {type(e).__name__}")
        if pem is None:
            ossl = _find_openssl()
            if ossl:
                try:
                    pem = _gen_cert_openssl(ossl)
                except Exception as e:
                    log(f"openssl: {type(e).__name__}")
        if pem is None:
            return _err("tls_missing", "Не удалось создать сертификат TLS",
                        "запустите Deskflow GUI один раз или pip install cryptography")
        path.parent.mkdir(parents=True, exist_ok=True)
        if IS_MAC:
            try:
                os.chmod(path.parent, 0o700)
            except OSError:
                pass
        fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=str(path.parent))
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(pem)
            _restrict_key_file(tmp)
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
        fp = local_fingerprint(path)
        if not fp.get("ok"):
            return _err("tls_missing", "Сертификат создан, но не читается", "pip install cryptography")
        log(f"создан сертификат TLS, отпечаток {fp['fp'][:8]}…")
        return _ok(path=str(path), created=True, fp=fp["fp"], fp_human=fp["fp_human"])
    except OSError as e:
        return _err("os_error", f"Сертификат не создан: {type(e).__name__}")
    except Exception as e:
        log(f"ensure_certificate: {type(e).__name__}: {e}")
        return _err("tls_missing", "Не удалось создать сертификат TLS",
                    "запустите Deskflow GUI один раз или pip install cryptography")


# ── Сеть ──
def local_ip_for(peer_host):
    """LAN-IPv4 этой машины по маршруту к peer_host (UDP connect, пакеты не отправляются) или None."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(1.0)
            s.connect((peer_host, 9))
            ip = s.getsockname()[0]
    except (OSError, ValueError, UnicodeError):
        return None
    return ip if ip and not ip.startswith("127.") and ip != "0.0.0.0" else None


def resolve_bind_host(host, peer_host=None):
    """(ip, err). Сервер никогда не слушает 0.0.0.0."""
    h = (host or "").strip()
    if not h:
        ip = local_ip_for(peer_host) if peer_host else None
        return (ip, None) if ip else (None, "Не удалось определить LAN-адрес этой машины — укажите его в мастере")
    try:
        ip = ipaddress.ip_address(h.strip("[]"))
        if ip.is_unspecified:
            return None, "Адрес сервера не может быть 0.0.0.0"
        return str(ip), None
    except ValueError:
        pass
    try:
        ip = socket.gethostbyname(h)
    except (OSError, UnicodeError):
        return None, "Не удалось разрешить адрес этой машины"
    if ip == "0.0.0.0":
        return None, "Адрес сервера не может быть 0.0.0.0"
    return ip, None


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _port_free(host, port):
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind((host, port))
        return True
    except OSError:
        return False


# ── Процессы ──
def proc_identity(pid):
    """{"image": имя файла строчными, "created": метка времени создания} или None, если процесса нет."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    try:
        if IS_WIN:
            import ctypes
            from ctypes import wintypes
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.OpenProcess.restype = wintypes.HANDLE
            k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            k32.CloseHandle.argtypes = [wintypes.HANDLE]
            k32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
            k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            k32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                       ctypes.POINTER(wintypes.DWORD)]
            h = k32.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
            if not h:
                return None
            try:
                code = wintypes.DWORD()
                if not k32.GetExitCodeProcess(h, ctypes.byref(code)) or code.value != 259:   # STILL_ACTIVE
                    return None
                ft = [wintypes.FILETIME() for _ in range(4)]
                if not k32.GetProcessTimes(h, *[ctypes.byref(x) for x in ft]):
                    return None
                created = (ft[0].dwHighDateTime << 32) | ft[0].dwLowDateTime
                buf = ctypes.create_unicode_buffer(1024)
                n = wintypes.DWORD(1024)
                if not k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)):
                    return None
                return {"image": os.path.basename(buf.value).lower(), "created": created}
            finally:
                k32.CloseHandle(h)
        rc, out = _run(["ps", "-o", "comm=", "-o", "lstart=", "-p", str(pid)], timeout=3)   # НП на macOS
        out = out.strip()
        if rc != 0 or not out:
            return None
        m = re.match(r"(.+?)\s+([A-Z][a-z]{2}\s.+)$", out)
        comm = m.group(1) if m else out
        return {"image": os.path.basename(comm).lower(), "created": m.group(2).strip() if m else ""}
    except Exception as e:
        log(f"proc_identity: {type(e).__name__}")
        return None


def foreign_core_pids(own_pid=None):
    """Чужие запущенные deskflow-core (GUI запускает его сам). Только чтение списка процессов, ничего не убиваем."""
    pids = []
    try:
        if IS_WIN:
            rc, out = _run(["tasklist", "/FI", "IMAGENAME eq deskflow-core.exe", "/FO", "CSV", "/NH"], timeout=8)
            for ln in (out or "").splitlines():
                m = re.match(r'^"deskflow-core\.exe","(\d+)"', ln.strip(), re.I)
                if m:
                    pids.append(int(m.group(1)))
        else:
            rc, out = _run(["ps", "-A", "-o", "pid=", "-o", "comm="], timeout=5)
            for ln in (out or "").splitlines():
                m = re.match(r"\s*(\d+)\s+(.+)$", ln)
                if m and os.path.basename(m.group(2).strip()).lower().startswith("deskflow-core"):
                    pids.append(int(m.group(1)))
    except Exception as e:
        log(f"foreign_core_pids: {type(e).__name__}")
    return [p for p in pids if p != own_pid]


def _kill_pid(pid, force):
    import signal
    try:
        os.kill(pid, signal.SIGTERM if not force or IS_WIN else signal.SIGKILL)   # на Windows это TerminateProcess
    except (OSError, ValueError):
        pass


# ── Проверка конфига ──
def _override_ini(text, overrides):
    """Подменить значения в Qt-INI: overrides = {("core", "port"): "1"}. Нет ключа — добавить."""
    sections, order, cur = {}, [], None
    for raw in (text or "").splitlines():
        s = raw.strip()
        m = re.match(r"^\[(.+)\]$", s)
        if m:
            cur = m.group(1)
            if cur not in sections:
                sections[cur] = []
                order.append(cur)
            continue
        if cur is None or not s:
            continue
        sections[cur].append(raw)
    for (sec, key), val in overrides.items():
        if sec not in sections:
            sections[sec] = []
            order.append(sec)
        rows = [r for r in sections[sec] if not r.split("=", 1)[0].strip() == key]
        rows.append(f"{key}={val}")
        sections[sec] = rows
    out = []
    for sec in order:
        out.append(f"[{sec}]")
        out += sections[sec]
    return "\n".join(out) + "\n"


def validate_config(path, settings_text, links_text, dialect=None):
    """Запустить сервер на 127.0.0.1:<свободный порт> (--new-instance, TLS выключен) на ≤ 3 с и разобрать лог.
    Возврат {"ok","code","err","hint","log":[...],"warnings":[...]}. Свой процесс всегда останавливается."""
    if not settings_text or not links_text:
        return _err("bad_config", "Нет текста настроек или конфига для проверки", log=[], warnings=[])
    if not isinstance(path, str) or not path:
        return _err("bad_config", "Нужен путь к deskflow-core", log=[], warnings=[])
    d = dialect or probe_dialect(path)
    if not d.get("ok"):
        return _err(d.get("code") or "dialect_unknown", d.get("err") or "Диалект Deskflow неизвестен",
                    log=[], warnings=[])
    proc = None
    lines, warnings = [], []
    rc, t = None, None
    with tempfile.TemporaryDirectory(prefix="mp-dfval-", ignore_cleanup_errors=True) as td:
        try:
            links_file = os.path.join(td, "links.conf")
            settings_file = os.path.join(td, "settings.conf")
            Path(links_file).write_text(links_text, encoding="utf-8", newline="\n")
            Path(settings_file).write_text(_override_ini(settings_text, {
                ("core", "interface"): "127.0.0.1", ("core", "port"): str(_free_port()),
                ("security", "tlsEnabled"): "false", ("security", "checkPeerFingerprints"): "false",
                ("server", "externalConfig"): "true", ("server", "externalConfigFile"): _fwd(links_file),
                ("core", "coreMode"): "2"}), encoding="utf-8", newline="\n")
            argv = build_argv("server", path, d, settings_file=settings_file, new_instance=True)
            try:
                proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT, cwd=td, env=_clean_env(), shell=False,
                                        creationflags=CREATE_NO_WINDOW)
            except OSError as e:
                return _err("spawn_failed", f"deskflow-core не запустился: {type(e).__name__}", log=[], warnings=[])
            ready = threading.Event()

            def pump():
                try:
                    for raw in iter(proc.stdout.readline, b""):
                        ln = raw.decode("utf-8", "replace").rstrip()
                        if ln:
                            lines.append(ln)
                            if SERVER_READY_RE.search(ln):
                                ready.set()
                except (OSError, ValueError):
                    pass
            t = threading.Thread(target=pump, daemon=True)
            t.start()
            end = time.monotonic() + VALIDATE_TIMEOUT_S
            while time.monotonic() < end and proc.poll() is None and not ready.is_set():
                time.sleep(0.05)
            if ready.is_set() and proc.poll() is None:
                time.sleep(0.25)   # ошибки разбора обычно приходят до «started server», но подождём хвост
            rc = proc.poll()
        except OSError as e:
            return _err("os_error", f"Проверка не выполнена: {type(e).__name__}", log=[], warnings=[])
        finally:
            if proc is not None:
                _stop_popen(proc, 3)
    if t is not None:
        t.join(1.0)
    errs = []
    for ln in lines:
        m = LOG_LINE_RE.match(ln)
        lvl, msg = (m.group("lvl"), m.group("msg")) if m else ("", ln)
        if lvl in ("ERROR", "FATAL") or READ_ERROR_RE.search(msg):
            errs.append(msg)
        elif lvl == "WARNING":
            warnings.append(msg)
    tail = [_scrub(x, 300) for x in lines[-LOG_TAIL_LINES:]]
    if errs:
        return _err("bad_config", errs[0], log=tail, warnings=[_scrub(w) for w in warnings])
    if rc is not None and rc != 0:
        return _err("bad_config", f"Deskflow завершился с кодом {rc}", "Проверьте конфиг связей", log=tail,
                    warnings=[_scrub(w) for w in warnings])
    return _ok(log=tail, warnings=[_scrub(w) for w in warnings], hint=None)


def _stop_popen(proc, timeout):
    """Остановить СВОЙ процесс: terminate, через timeout — kill."""
    try:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(2)
    except OSError:
        pass
    for s in (proc.stdout, proc.stdin):
        try:
            if s:
                s.close()
        except Exception:
            pass


# ── Рабочий процесс ──
class Deskflow:
    """Один свой процесс deskflow-core. Лог — в файл core.log (процесс переживает панель, §8.1), читается хвостом."""

    def __init__(self, path, dialect, data_dir, on_event=None):
        self._path = str(path)
        self._dialect = dialect or {}
        self._dir = Path(data_dir) / "deskflow"
        self._on_event = on_event
        self._lock = threading.RLock()
        self._proc = None
        self._pid = None
        self._ident = None
        self._stop_evt = None
        self._reader = None
        self._reset()

    def _reset(self):
        self._role = None
        self._since = None
        self._name = None
        self._peer = None
        self._tail = collections.deque(maxlen=LOG_TAIL_LINES)
        self._connected = []
        self._err = None
        self._last_switch = None
        self._pending = None
        self._cand_fp = None
        self._wait_trust = False   # клиент доверяет серверу, но сервер его ещё не подтвердил
        self._listening = False
        self._exit_reported = True
        self._alive_cache = (0.0, False)

    # ── события ──
    def _emit(self, kind, payload=None):
        cb = self._on_event
        if cb:
            try:
                cb(kind, payload)
            except Exception as e:
                log(f"on_event({kind}): {type(e).__name__}")

    # ── разбор строк лога ──
    def _feed(self, line, emit=True):
        line = line.rstrip("\r\n")
        if not line.strip():
            return
        ev = []
        with self._lock:
            self._tail.append(_scrub(line, 300))
            m = LOG_LINE_RE.match(line)
            lvl, msg = (m.group("lvl"), m.group("msg")) if m else ("", line)
            role = self._role
            fp = PEER_FP_RE.search(msg)
            if fp:
                h = normalize_fp(fp.group(1))
                self._cand_fp = h
            if UNTRUSTED_RE.search(msg) and self._cand_fp:
                p = {"fp": self._cand_fp, "at": now_iso()}
                if not self._pending or self._pending["fp"] != p["fp"]:
                    self._pending = p
                    ev.append(("deskflow_untrusted_peer", {"fp": p["fp"], "fp_human": format_fp(p["fp"]),
                                                           "role": role, "at": p["at"]}))
            if TRUSTED_OK_RE.search(msg):
                self._pending, self._err = None, None   # сверка прошла — прошлая ошибка устарела
                if role == "client":
                    self._wait_trust = True
            if lvl in ("ERROR", "FATAL"):
                self._err = _scrub(msg)
            if SERVER_READY_RE.search(msg):
                self._listening = True
            cm = CLIENT_CONNECTED_RE.search(msg)
            if cm:
                self._pending, self._err = None, None
                if cm.group(1) not in self._connected:
                    self._connected.append(cm.group(1))
                    ev.append(("deskflow_connected", {"name": cm.group(1)}))
            dm = CLIENT_DISCONNECTED_RE.search(msg)
            if dm:
                n = dm.group(1) or dm.group(2)
                if n in self._connected:
                    self._connected.remove(n)
                    ev.append(("deskflow_disconnected", {"name": n}))
            if role == "client":
                if SERVER_CONNECTED_RE.search(msg):
                    self._pending, self._err, self._wait_trust = None, None, False
                    n = self._peer or "server"
                    if n not in self._connected:
                        self._connected.append(n)
                        ev.append(("deskflow_connected", {"name": n}))
                elif SERVER_LOST_RE.search(msg) and self._connected:
                    for n in self._connected:
                        ev.append(("deskflow_disconnected", {"name": n}))
                    self._connected = []
            sw = SWITCH_RE.search(msg)
            if sw:
                self._last_switch = {"from": sw.group(1), "to": sw.group(2), "x": int(sw.group(3)),
                                     "y": int(sw.group(4)), "at": now_iso()}
                ev.append(("deskflow_switch", dict(self._last_switch)))
        if emit:
            for k, p in ev:
                self._emit(k, p)

    # ── чтение лога ──
    def _alive(self, fresh=False):
        proc, pid, ident = self._proc, self._pid, self._ident
        if proc is not None:
            return proc.poll() is None
        if pid:
            now = time.monotonic()
            t, val = self._alive_cache
            if not fresh and now - t < 0.5:   # status() зовут из потока Tk; проверка перенятого pid — не чаще 2 раз в секунду
                return val
            cur = proc_identity(pid)
            val = bool(cur and ident and cur.get("image") == ident.get("image")
                       and cur.get("created") == ident.get("created"))
            self._alive_cache = (now, val)
            return val
        return False

    def _read_loop(self, stop_evt, offset, log_path):
        f, buf = None, b""
        try:
            while not stop_evt.is_set():
                if f is None:
                    try:
                        f = open(log_path, "rb")
                        f.seek(offset)
                    except OSError:
                        f = None
                data = b""
                if f is not None:
                    try:
                        if os.fstat(f.fileno()).st_size < f.tell():
                            f.seek(0)   # файл обрезан/ротирован
                        data = f.read(65536)
                    except OSError:
                        data = b""
                if data:
                    buf += data
                    *done, buf = buf.split(b"\n")
                    for raw in done:
                        self._feed(raw.decode("utf-8", "replace"))
                    continue
                if not self._alive():
                    if buf.strip():
                        self._feed(buf.decode("utf-8", "replace"))
                    self._on_exit()
                    return
                stop_evt.wait(0.15)
        finally:
            if f is not None:
                try:
                    f.close()
                except OSError:
                    pass

    def _on_exit(self):
        with self._lock:
            if self._exit_reported:
                return
            self._exit_reported = True
            names, self._connected = list(self._connected), []
            code = self._proc.poll() if self._proc is not None else None
            if not self._err:
                self._err = "Deskflow завершился" + (f" (код {code})" if code is not None else "")
        for n in names:
            self._emit("deskflow_disconnected", {"name": n})
        self._emit("deskflow_exited", {"code": code})

    def _start_reader(self, offset):
        evt = threading.Event()
        self._stop_evt = evt
        t = threading.Thread(target=self._read_loop, args=(evt, offset, self._dir / "core.log"),
                             name="deskflow-log", daemon=True)
        self._reader = t
        t.start()

    # ── запуск ──
    def _rotate_log(self):
        lp = self._dir / "core.log"
        try:
            if lp.is_file() and lp.stat().st_size > LOG_ROTATE_BYTES:
                os.replace(lp, self._dir / "core.log.1")
        except OSError as e:
            log(f"ротация лога: {type(e).__name__}")

    def _launch(self, role, settings_text, links_text, name, peer, bind_host, port, new_instance=False):
        if self.running():
            self.stop()
        if not new_instance and foreign_core_pids(None):
            return _err("conflict", "Уже запущен другой Deskflow (GUI или служба)",
                        "закройте Deskflow GUI или остановите в нём сервер/клиент")
        if role == "server" and not _port_free(bind_host, port):
            return _err("conflict", f"Порт {port} на {bind_host} уже занят", "закройте программу, занявшую порт")
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            if IS_MAC:
                os.chmod(self._dir, 0o700)
            settings_file = self._dir / "settings.conf"
            _atomic_write_text(settings_file, settings_text)
            if links_text is not None:
                _atomic_write_text(self._dir / "server.conf", links_text)
            self._rotate_log()
            lp = self._dir / "core.log"
            offset = lp.stat().st_size if lp.exists() else 0
            argv = build_argv(role, self._path, self._dialect, settings_file=settings_file, new_instance=new_instance)
            with open(lp, "ab") as logf:
                proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=logf, stderr=subprocess.STDOUT,
                                        cwd=str(self._dir), env=_clean_env(), shell=False,
                                        creationflags=CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP)
        except ValueError as e:
            return _err("bad_config", str(e))
        except OSError as e:
            return _err("spawn_failed", f"Deskflow не запустился: {type(e).__name__}")
        with self._lock:
            self._reset()
            self._proc, self._pid = proc, proc.pid
            self._ident = proc_identity(proc.pid)
            self._role, self._name, self._peer = role, name, peer
            self._since = now_iso()
            self._exit_reported = False
        self._start_reader(offset)
        log(f"запущен deskflow-core {role}, pid {proc.pid}")
        end = time.monotonic() + 1.0   # быстрый выход = ошибка конфига/окружения
        while time.monotonic() < end:
            if proc.poll() is not None:
                time.sleep(0.3)   # дать читателю собрать хвост
                st = self.status()
                return _err("exited", st.get("err") or f"Deskflow сразу завершился (код {proc.returncode})",
                            log_tail=st.get("log_tail"))
            time.sleep(0.05)
        return _ok(pid=proc.pid, role=role, since=self._since)

    def start_server(self, conf_text, name, bind_host, port, tls=True, *, machines=None, cert_path=None,
                     clipboard=True, log_level="DEBUG", new_instance=False):
        """conf_text — конфиг связей (build_server_config); настройки строятся здесь."""
        try:
            settings = build_settings("server", name=name, machines=machines or [name], bind_host=bind_host,
                                      port=port, tls=tls, cert_path=cert_path,
                                      links_path=self._dir / "server.conf", clipboard=clipboard,
                                      log_level=log_level)
        except ValueError as e:
            return _err("bad_config", str(e))
        return self._launch("server", settings, conf_text, name, None, bind_host, port, new_instance)

    def start_client(self, name, server_host, port, tls=True, *, machines=None, cert_path=None, clipboard=True,
                     log_level="DEBUG", server_name=None, new_instance=False):
        try:
            settings = build_settings("client", name=name, machines=machines or [name], server_host=server_host,
                                      port=port, tls=tls, cert_path=cert_path, clipboard=clipboard,
                                      log_level=log_level)
        except ValueError as e:
            return _err("bad_config", str(e))
        return self._launch("client", settings, None, name, server_name or server_host, None, port, new_instance)

    def running(self):
        return bool(self._pid) and self._alive()

    def stop(self, timeout=3):
        """Остановить только свой процесс. Чужие deskflow не трогаем."""
        with self._lock:
            proc, pid, ident, evt = self._proc, self._pid, self._ident, self._stop_evt
            self._exit_reported = True   # преднамеренная остановка — не «аварийный» выход
        if not pid:
            return _ok(stopped=False)
        try:
            if proc is not None:
                _stop_popen(proc, timeout)
            elif self._alive(fresh=True):   # перенятый по pid: образ и время создания проверены прямо сейчас
                _kill_pid(pid, False)
                end = time.monotonic() + timeout
                while time.monotonic() < end and self._alive(fresh=True):
                    time.sleep(0.05)
                if self._alive(fresh=True):
                    _kill_pid(pid, True)
        except Exception as e:
            log(f"stop: {type(e).__name__}")
            return _err("os_error", "Не удалось остановить Deskflow")
        finally:
            if evt is not None:
                evt.set()
        with self._lock:
            names, self._connected = list(self._connected), []
            self._proc, self._pid, self._ident = None, None, None
            self._role, self._since, self._pending = None, None, None
        for n in names:
            self._emit("deskflow_disconnected", {"name": n})
        log("deskflow-core остановлен")
        return _ok(stopped=True)

    # ── состояние ──
    def status(self):
        alive = self.running()
        with self._lock:
            return {"running": alive, "role": self._role if alive else None, "pid": self._pid if alive else None,
                    "since": self._since if alive else None, "connected": list(self._connected) if alive else [],
                    "last_switch": dict(self._last_switch) if self._last_switch else None, "err": self._err,
                    "log_tail": list(self._tail), "pending_peer_fp": dict(self._pending) if self._pending else None,
                    "waiting_peer_trust": self._wait_trust and not self._connected,
                    "listening": self._listening}

    def clear_pending(self, fp=None):
        with self._lock:
            if self._pending and (fp is None or normalize_fp(fp) == self._pending["fp"]):
                self._pending = None
            self._cand_fp = None

    def process_info(self):
        """Что сохранить в state.json для перенятия после перезапуска панели."""
        with self._lock:
            if not self._pid or not self._ident:
                return None
            return {"pid": self._pid, "image": self._ident.get("image"), "created": self._ident.get("created"),
                    "role": self._role, "since": self._since, "name": self._name, "peer": self._peer}

    def adopt(self, info):
        """Перенять живой процесс из state.json (pid + образ + время создания должны совпасть)."""
        try:
            pid = info.get("pid")
            cur = proc_identity(pid)
            if (not cur or cur.get("image") != info.get("image") or cur.get("created") != info.get("created")
                    or cur["image"] not in CORE_NAMES):
                return False
            if info.get("role") not in ("server", "client"):
                return False
            if self.running():
                return False
            with self._lock:
                self._reset()
                self._proc, self._pid, self._ident = None, pid, cur
                self._role, self._since = info["role"], info.get("since")
                self._name, self._peer = info.get("name"), info.get("peer")
                self._exit_reported = False
            lp = self._dir / "core.log"
            try:
                size = lp.stat().st_size
                with open(lp, "rb") as f:   # восстановить состояние по хвосту (без событий)
                    f.seek(max(0, size - 64 * 1024))
                    for raw in f.read().decode("utf-8", "replace").splitlines()[-300:]:
                        self._feed(raw, emit=False)
            except OSError:
                size = 0
            self._start_reader(size)
            log(f"перенят deskflow-core pid {pid}")
            return True
        except Exception as e:
            log(f"adopt: {type(e).__name__}")
            return False
