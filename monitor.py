"""Мониторинг серверов: ping каждые 30 с и сбор состояния по ssh каждые 120 с (фоновые потоки).

Конфиг (config.json, ключ "servers"):
    [{"name": "server1", "host": "192.0.2.10", "ssh": "admin@192.0.2.10", "ssh_port": 22}]
"ssh" и "ssh_port" необязательны: без "ssh" сервер только пингуется.

Безопасность: host/ssh проверяются строгой регуляркой (имя хоста, IPv4, IPv6, user@host), значения,
начинающиеся с «-», отклоняются; всё передаётся списком аргументов без shell. ssh — только по ключу
(BatchMode=yes): пароль не спрашивается никогда.
"""
import ipaddress
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

PING_PERIOD = 30
SSH_PERIOD = 120
REFRESH_MIN = 10
SSH_TIMEOUT = 20      # общий предел на один ssh-сбор (ConnectTimeout=5 — только на подключение)
PING_TIMEOUT = 4
MAX_SERVERS = 64
OUT_LIMIT = 256 * 1024

REMOTE_CMD = ("LC_ALL=C df -P -x tmpfs -x devtmpfs; echo ---; free -m; echo ---; cat /proc/loadavg; "
              "echo ---; cat /proc/mdstat 2>/dev/null; echo ---; uptime -p")
# BusyBox (NAS и т.п.): нет df -x и free — простой df -P, память из /proc/meminfo, uptime без -p
REMOTE_CMD_BUSYBOX = ("df -P; echo ---; cat /proc/meminfo; echo ---; cat /proc/loadavg; "
                      "echo ---; cat /proc/mdstat 2>/dev/null; echo ---; uptime -p 2>/dev/null || uptime")
_SKIP_FS = {"tmpfs", "devtmpfs", "none", "udev", "shm", "overlay", "squashfs"}

_WIN = sys.platform == "win32"
_NO_WINDOW = 0x08000000 if _WIN else 0

_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
_HOSTNAME_RE = re.compile(rf"{_LABEL}(?:\.{_LABEL})*\.?")
_USER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9._-]{0,31}")
# служебные точки монтирования, которые неинтересны в панели
_SKIP_MOUNTS = ("/snap/", "/run", "/dev", "/sys", "/proc", "/var/lib/docker/", "/boot/efi")


def log(msg):
    try:
        if sys.stderr:
            print(f"[panel/monitor] {msg}", file=sys.stderr, flush=True)
    except Exception:
        pass


# ── Проверка конфига ──
def valid_host(h):
    """Имя хоста / IPv4 / IPv6 (без скобок). Начинающиеся с «-» и прочий мусор — False."""
    if not isinstance(h, str) or not h or len(h) > 253 or h[0] == "-":
        return False
    try:
        ip = ipaddress.ip_address(h)
        return not getattr(ip, "scope_id", None) or re.fullmatch(r"[A-Za-z0-9]{1,16}", ip.scope_id) is not None
    except ValueError:
        pass
    return _HOSTNAME_RE.fullmatch(h) is not None and not h.replace(".", "").isdigit()


def valid_ssh_target(t):
    """[user@]host с такими же правилами для host; user — обычное имя пользователя Unix."""
    if not isinstance(t, str) or not t or t[0] == "-" or len(t) > 300:
        return False
    if "@" in t:
        user, _, host = t.rpartition("@")
        return _USER_RE.fullmatch(user) is not None and valid_host(host)
    return valid_host(t)


def is_ipv6(h):
    try:
        return ipaddress.ip_address(h).version == 6
    except ValueError:
        return False


_is_ipv6 = is_ipv6  # старое имя — алиас


def _clean_name(n, fallback):
    s = "".join(ch for ch in str(n if n is not None else fallback) if ch.isprintable())
    return s[:64] or str(fallback)[:64]


# ── ping ──
WIN_TTL_RE = re.compile(r"[=<]\s*(\d+(?:[.,]\d+)?)[^=<\d]*TTL=", re.I)
MS_RE = re.compile(r"[=<]\s*(\d+(?:[.,]\d+)?)\s*(?:ms|мс)\b", re.I)


def decode(b):
    for enc in (("oem",) if _WIN else ()) + ("utf-8",):
        try:
            return b.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return b.decode("latin-1")


_WIN_TTL_RE, _MS_RE, _decode = WIN_TTL_RE, MS_RE, decode  # старые имена — алиасы


def parse_ping(rc, out):
    """(up, ms) из вывода ping. Не зависит от языка Windows: успешный ответ IPv4 — строка с «TTL=»
    (ping возвращает 0 и при «Заданный узел недоступен» от маршрутизатора), время — число перед ним."""
    if rc != 0:
        return False, None
    for line in out.splitlines():
        if _WIN and "TTL=" in line.upper():
            m = _WIN_TTL_RE.search(line)
            return True, float(m.group(1).replace(",", ".")) if m else None
        if not _WIN and "time=" in line:
            m = re.search(r"time[=<]\s*(\d+(?:\.\d+)?)", line)
            return True, float(m.group(1)) if m else None
    for line in out.splitlines():  # IPv6 на Windows: TTL не печатается
        m = _MS_RE.search(line)
        if m:
            return True, float(m.group(1).replace(",", "."))
    return (not _WIN), None  # mac/linux: код 0 и так означает ответ


def ping(host):
    """(up, ms). up=None — ping не запустился вовсе."""
    if _WIN:
        args = ["ping", "-n", "1", "-w", "1000", host]
    else:
        args = ["ping6" if _is_ipv6(host) else "ping", "-c", "1"] + ([] if _is_ipv6(host) else ["-t", "1"]) + [host]
    try:
        r = subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True, timeout=PING_TIMEOUT,
                           creationflags=_NO_WINDOW)
    except subprocess.TimeoutExpired:
        return False, None
    except OSError as e:
        log(f"ping не запустился: {type(e).__name__}")
        return None, None
    return parse_ping(r.returncode, _decode(r.stdout[:OUT_LIMIT]))


def tcp_probe(host, port, timeout=1.5):
    """Запасная проверка, если ICMP закрыт: TCP-подключение. Отказ (RST) — тоже признак живого узла."""
    t0 = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            pass
    except ConnectionRefusedError:
        pass
    except OSError:
        return False, None
    return True, round((time.perf_counter() - t0) * 1000, 1)


# ── ssh ──
def _parse_df(text):
    disks = []
    for line in text.splitlines()[1:]:
        f = line.split()
        if len(f) < 6 or not f[4].endswith("%") or f[0] in _SKIP_FS:
            continue
        mount = " ".join(f[5:])
        if mount != "/" and any(mount == s.rstrip("/") or mount.startswith(s) for s in _SKIP_MOUNTS):
            continue
        try:
            disks.append({"mount": mount, "pct": int(f[4][:-1]), "free": int(f[3]) * 1024})
        except ValueError:
            continue
    return disks


def _parse_meminfo(text):
    """/proc/meminfo (kB): занято = MemTotal − MemAvailable (старые ядра: MemFree + Buffers + Cached)."""
    kv = {}
    for line in text.splitlines():
        m = re.match(r"^(\w+):\s+(\d+)", line)
        if m:
            kv[m.group(1)] = int(m.group(2))
    total = kv.get("MemTotal")
    if not total:
        return None
    avail = kv.get("MemAvailable")
    if avail is None:
        avail = kv.get("MemFree", 0) + kv.get("Buffers", 0) + kv.get("Cached", 0)
    return round((total - min(avail, total)) * 100 / total)


def _parse_free(text):
    if "MemTotal:" in text:
        return _parse_meminfo(text)
    for line in text.splitlines():
        f = line.split()
        if f and f[0] == "Mem:" and len(f) >= 3:
            try:
                total = int(f[1])
                avail = int(f[6]) if len(f) >= 7 else total - int(f[2])
                return round((total - avail) * 100 / total) if total > 0 else None
            except (ValueError, ZeroDivisionError):
                return None
    return None


def _parse_load(text):
    f = text.split()
    try:
        return tuple(float(x) for x in f[:3]) if len(f) >= 3 else None
    except ValueError:
        return None


def _parse_mdstat(text):
    """"ok" / "degraded: md0 [U_]" / None (массивов md нет)."""
    arrays, bad, cur = 0, [], None
    for line in text.splitlines():
        m = re.match(r"^(md\w+)\s*:", line)
        if m:
            cur = m.group(1)
            arrays += 1
            continue
        s = re.search(r"\[([U_]+)\]", line)
        if cur and s:
            if "_" in s.group(1):
                bad.append(f"{cur} [{s.group(1)}]")
            cur = None
    if not arrays:
        return None
    return "degraded: " + ", ".join(bad) if bad else "ok"


def parse_ssh_output(out):
    parts = re.split(r"(?m)^---\s*$", out)
    parts += [""] * (5 - len(parts))
    up = parts[4].strip().splitlines()
    uptime = up[0].strip() if up else ""
    if uptime.startswith("up "):
        uptime = uptime[3:]
    else:  # классический uptime (BusyBox): « 12:01:02 up 3 days,  4:05,  1 user,  load average: …»
        m = re.search(r"\bup\s+(.*?),\s+(?:\d+\s+users?|load average)", uptime)
        if m:
            uptime = re.sub(r"\s+", " ", m.group(1)).strip()
    return {"load": _parse_load(parts[2]), "mem_pct": _parse_free(parts[1]), "disks": _parse_df(parts[0]),
            "raid": _parse_mdstat(parts[3]), "uptime": uptime}


def _ssh_error(rc, err):
    low = err.lower()
    if "permission denied" in low:
        return "нет доступа по ключу (Permission denied)"
    if "host key verification failed" in low or "remote host identification has changed" in low:
        return "ключ хоста изменился — проверьте known_hosts"
    if "connection refused" in low:
        return "ssh: соединение отклонено"
    if "timed out" in low:
        return "ssh: таймаут подключения"
    if "could not resolve" in low:
        return "ssh: имя хоста не найдено"
    lines = [ln.strip() for ln in err.splitlines() if ln.strip()]
    return (lines[-1][:200] if lines else f"ssh: код выхода {rc}")


def _first_cmd_failed(out):
    """Первая команда (df -P -x …) не дала таблицы — похоже на BusyBox без -x."""
    df = re.split(r"(?m)^---\s*$", out)[0]
    return not any(len(ln.split()) >= 6 and ln.split()[4].endswith("%") for ln in df.splitlines()[1:])


def ssh_collect(target, port=None, ssh_bin=None):
    """Собрать состояние сервера по ssh. Всегда возвращает dict формата snapshot()["ssh"]."""
    res = {"ok": False, "err": None, "load": None, "mem_pct": None, "disks": [], "raid": None, "uptime": "",
           "checked": time.time()}
    exe = ssh_bin or shutil.which("ssh")
    if not exe:
        res["err"] = "ssh не найден (нужен OpenSSH-клиент)"
        return res
    args = [exe, "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=accept-new",
            "-o", "PasswordAuthentication=no", "-o", "KbdInteractiveAuthentication=no",
            "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=2", "-o", "LogLevel=ERROR"]
    if port:
        args += ["-p", str(port)]
    args += ["--", target]
    for cmd in (REMOTE_CMD, REMOTE_CMD_BUSYBOX):
        try:
            r = subprocess.run(args + [cmd], stdin=subprocess.DEVNULL, capture_output=True, timeout=SSH_TIMEOUT,
                               creationflags=_NO_WINDOW)
        except subprocess.TimeoutExpired:
            res["err"] = "ssh: превышено время ожидания"
            return res
        except OSError as e:
            res["err"] = f"ssh не запустился: {type(e).__name__}"
            return res
        out = r.stdout[:OUT_LIMIT].decode("utf-8", "replace")
        if r.returncode == 255 or (r.returncode != 0 and "---" not in out):
            res["err"] = _ssh_error(r.returncode, r.stderr[:OUT_LIMIT].decode("utf-8", "replace"))
            return res
        if not _first_cmd_failed(out):
            break
        if cmd is REMOTE_CMD:
            log("ssh: df -x/free не сработали — повтор упрощённой командой (BusyBox)")
    try:
        res.update(parse_ssh_output(out))
        res["ok"] = True
    except Exception as e:  # разбор не должен ронять мониторинг
        res["err"] = f"не удалось разобрать ответ: {type(e).__name__}"
    return res


# ── Monitor ──
class Monitor:
    def __init__(self, servers):
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = None
        self._pool = None
        self._busy = set()
        self._next_ping = self._next_ssh = 0.0
        self._last_refresh = 0.0
        self._servers, self._state = [], []
        if not isinstance(servers, list):
            servers = []
        if len(servers) > MAX_SERVERS:
            log(f"серверов больше {MAX_SERVERS} — лишние пропущены")
        for i, s in enumerate(servers[:MAX_SERVERS]):
            s = s if isinstance(s, dict) else {}
            host = s.get("host")
            name = _clean_name(s.get("name"), host if isinstance(host, str) else f"server{i + 1}")
            err = None
            ssh = s.get("ssh")
            port = s.get("ssh_port")
            if not valid_host(host):
                err, host = "недопустимый host в config.json", None
            elif ssh is not None and not valid_ssh_target(ssh):
                err = "недопустимое значение ssh в config.json"
            elif port is not None and (isinstance(port, bool) or not isinstance(port, int) or not 0 < port < 65536):
                err = "недопустимый ssh_port в config.json"
            if err:
                log(f"сервер «{name}» отклонён: {err}")
            self._servers.append({"name": name, "host": host, "ssh": ssh if not err else None,
                                  "port": port if not err else None, "valid": not err})
            st = {"name": name, "host": host if host else "", "up": None, "ms": None, "checked": None, "ssh": None}
            if err:
                st["err"] = err
            self._state.append(st)

    # управление
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="monitor")
        self._thread = threading.Thread(target=self._loop, name="monitor", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()
        if self._pool:
            self._pool.shutdown(wait=False, cancel_futures=True)

    def refresh(self):
        now = time.monotonic()
        with self._lock:
            if now - self._last_refresh < REFRESH_MIN:
                return
            self._last_refresh = now
            self._next_ping = self._next_ssh = 0.0
        self._wake.set()

    def snapshot(self):
        with self._lock:
            out = []
            for st in self._state:
                d = dict(st)
                if st["ssh"] is not None:
                    d["ssh"] = dict(st["ssh"], disks=[dict(x) for x in st["ssh"]["disks"]])
                out.append(d)
            return out

    # работа
    def _loop(self):
        while not self._stop.is_set():
            now = time.monotonic()
            with self._lock:
                do_ping, do_ssh = now >= self._next_ping, now >= self._next_ssh
                if do_ping:
                    self._next_ping = now + PING_PERIOD
                if do_ssh:
                    self._next_ssh = now + SSH_PERIOD
            for i, srv in enumerate(self._servers):
                if not srv["valid"]:
                    continue
                if do_ping:
                    self._submit(("ping", i), self._do_ping, i)
                if do_ssh and srv["ssh"]:
                    self._submit(("ssh", i), self._do_ssh, i)
            self._wake.wait(1.0)
            self._wake.clear()

    def _submit(self, key, fn, i):
        with self._lock:
            if key in self._busy:  # прошлая проверка ещё идёт — не накапливаем
                return
            self._busy.add(key)

        def run():
            try:
                fn(i)
            except Exception as e:
                log(f"{key[0]} «{self._servers[i]['name']}»: {type(e).__name__}")
            finally:
                with self._lock:
                    self._busy.discard(key)
        try:
            self._pool.submit(run)
        except RuntimeError:  # пул уже остановлен
            with self._lock:
                self._busy.discard(key)

    def _do_ping(self, i):
        srv = self._servers[i]
        up, ms = ping(srv["host"])
        if not up and not self._stop.is_set():
            t_up, t_ms = tcp_probe(srv["host"], srv["port"] or 22)
            if t_up:
                up, ms = True, t_ms
            elif up is None:
                up = False
        with self._lock:
            self._state[i].update(up=up, ms=ms, checked=time.time())

    def _do_ssh(self, i):
        srv = self._servers[i]
        res = ssh_collect(srv["ssh"], srv["port"])
        if res["err"]:
            log(f"ssh «{srv['name']}»: {res['err']}")
        with self._lock:
            self._state[i]["ssh"] = res
