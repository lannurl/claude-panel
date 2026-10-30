"""LAN-связка двух панелей (docs/monitors-spec.md §5.6, §8.5, §10): «одна кнопка с любой машины».

Транспорт — TCP, а не UDP: нужен запрос-ответ с подтверждением доставки, а у UDP адрес отправителя подделывается
одним пакетом (плюс отражённые ответы); при TCP IP-источник подтверждён рукопожатием, и «чужой IP» отсекается
надёжнее. Один запрос — одно соединение, одна строка JSON ≤ 16 КБ, таймаут 3 с.

Безопасность:
  * слушаем ТОЛЬКО на адресе этой машины из раскладки (0.0.0.0 / :: отвергаются) и только на частном адресе
    (10/8, 172.16/12, 192.168/16, 169.254/16, fc00::/7, fe80::/10); loopback — лишь с allow_loopback=True (тесты);
  * соединения не с IP пиров закрываются сразу, без чтения;
  * сообщения подписаны HMAC-SHA256 общим секретом (20 байт), метка времени ±30 с, nonce не повторяется 120 с,
    адресат (to) обязан совпасть; проверка hmac.compare_digest;
  * 5 неверных подписей за минуту с одного IP — игнор этого IP на 60 с;
  * команды — закрытый список, ни одна не запускает переданный путь или строку;
  * шифрования нет: режим/состояние не секретны, нужна лишь подлинность. Секрет в логи и ответы не попадает.
"""
import base64
import hashlib
import hmac
import ipaddress
import json
import re
import secrets
import socket
import sys
import threading
import time

PROTO = 1
MAX_MSG = 16 * 1024
IO_TIMEOUT_S = 3.0
TS_WINDOW_S = 30
NONCE_TTL_S = 120
BAD_LIMIT = 5
BAD_WINDOW_S = 60
BAN_S = 60
MAX_CONNS = 8
SEEN_CAP = 4096
SECRET_LEN = 20
COMMANDS = ("ping", "status", "set_mode")   # закрытый список (расширять только через спецификацию)

NONCE_RE = re.compile(r"[0-9a-f]{32}")
ID_RE = re.compile(r"[a-z0-9-]{1,16}")
_PRIVATE_NETS = [ipaddress.ip_network(n) for n in
                 ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16", "fc00::/7", "fe80::/10")]


def log(msg):
    try:
        if sys.stderr:
            print(f"[panel/link] {msg}", file=sys.stderr, flush=True)
    except Exception:
        pass


def _err(code, text, **extra):
    d = {"ok": False, "code": code, "err": text}
    d.update(extra)
    return d


# ── секрет ──
def new_secret():
    """(20 байт, код «ABCD-EFGH-…» из 8 групп по 4 символа base32)."""
    raw = secrets.token_bytes(SECRET_LEN)
    return raw, format_code(raw)


def format_code(raw):
    b = base64.b32encode(bytes(raw)).decode("ascii")
    return "-".join(b[i:i + 4] for i in range(0, 32, 4))


def secret_from_code(code):
    """Код → 20 байт или None (любой мусор, неверная длина, регистр и дефисы не важны)."""
    if not isinstance(code, str) or len(code) > 64:
        return None
    s = re.sub(r"[\s-]", "", code).upper()
    if not re.fullmatch(r"[A-Z2-7]{32}", s):
        return None
    try:
        raw = base64.b32decode(s)
    except (ValueError, TypeError):
        return None
    return raw if len(raw) == SECRET_LEN else None


# ── подпись ──
def canonical(msg):
    """Каноничный JSON сообщения без поля mac (sort_keys, без пробелов, UTF-8)."""
    body = {k: v for k, v in msg.items() if k != "mac"}
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _mac(msg, secret):
    return hmac.new(secret, canonical(msg), hashlib.sha256).hexdigest()


def sign(msg, secret):
    """Копия сообщения с полем mac."""
    out = {k: v for k, v in msg.items() if k != "mac"}
    out["mac"] = _mac(out, secret)
    return out


def verify(msg, secret, now, seen, self_id=None):
    """(ok, причина). Причины: format, version, mac, time, replay, to.

    seen — set nonce'ов (или dict nonce→ts: тогда устаревшие записи удаляются). Успешная проверка запоминает nonce.
    """
    try:
        if not isinstance(msg, dict) or not isinstance(secret, (bytes, bytearray)) or not secret:
            return False, "format"
        mac = msg.get("mac")
        if not isinstance(mac, str) or not re.fullmatch(r"[0-9a-f]{64}", mac):
            return False, "format"
        if msg.get("v") != PROTO:
            return False, "version"
        ts, nonce, frm, to = msg.get("ts"), msg.get("nonce"), msg.get("from"), msg.get("to")
        if (isinstance(ts, bool) or not isinstance(ts, int) or not isinstance(nonce, str)
                or not NONCE_RE.fullmatch(nonce) or not isinstance(frm, str) or not ID_RE.fullmatch(frm)
                or not isinstance(to, str) or not ID_RE.fullmatch(to)):
            return False, "format"
        if not hmac.compare_digest(mac, _mac(msg, bytes(secret))):
            return False, "mac"
        if abs(int(now) - ts) > TS_WINDOW_S:
            return False, "time"
        if self_id is not None and to != self_id:
            return False, "to"
        if isinstance(seen, dict):
            for k in [k for k, t in seen.items() if int(now) - t > NONCE_TTL_S]:
                del seen[k]
            if nonce in seen:
                return False, "replay"
            if len(seen) >= SEEN_CAP:
                return False, "replay"   # переполнение: лучше отказ, чем пропуск повтора
            seen[nonce] = int(now)
        else:
            if nonce in seen:
                return False, "replay"
            seen.add(nonce)
        return True, "ok"
    except Exception as e:
        log(f"verify: {type(e).__name__}")
        return False, "format"


def is_private_ip(ip, allow_loopback=False):
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if a.is_unspecified or a.is_multicast:
        return False
    if a.is_loopback:
        return bool(allow_loopback)
    return any(a in n for n in _PRIVATE_NETS if a.version == n.version)


def _split_hostport(s, default_port):
    """«host» или «host:port» (порт — только когда в строке ровно одно двоеточие, т.е. не IPv6)."""
    s = (s or "").strip()
    if s.count(":") == 1:
        h, p = s.rsplit(":", 1)
        if p.isdigit() and 1 <= int(p) <= 65535:
            return h, int(p)
    return s.strip("[]"), default_port


def _resolve_ip(host):
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        pass
    try:
        return socket.gethostbyname(host)
    except (OSError, UnicodeError):
        return None


def _read_line(conn, deadline):
    """Одна строка (без \\n) не длиннее MAX_MSG или None."""
    buf = b""
    while b"\n" not in buf:
        left = deadline - time.monotonic()
        if left <= 0:
            return None
        conn.settimeout(left)
        try:
            chunk = conn.recv(4096)
        except (OSError, socket.timeout):
            return None
        if not chunk:
            break
        buf += chunk
        if len(buf) > MAX_MSG + 4096:
            return None
    line = buf.split(b"\n", 1)[0]
    return line if 0 < len(line) <= MAX_MSG else None


class PeerLink:
    """Сервер в фоновом потоке + request() к пирам. handler(from_id, cmd, args) -> dict (результат команды)."""

    def __init__(self, secret, self_id, bind_host, port, peers, handler, *, allow_loopback=False,
                 commands=COMMANDS, clock=time.time):
        self._secret = bytes(secret) if secret else b""
        self._id = self_id
        self._bind = bind_host
        self._port = port
        self._peers = dict(peers or {})
        self._handler = handler
        self._allow_loopback = bool(allow_loopback)
        self._commands = tuple(commands)
        self._clock = clock
        self._lock = threading.RLock()
        self._sock = None
        self._thread = None
        self._stop = threading.Event()
        self._seen = {}
        self._bad = {}      # ip -> [время неверных подписей]
        self._ban = {}      # ip -> до какого времени игнорируем
        self._ip_to_peer = {}
        self._targets = {}  # peer -> (ip, port)
        self._last_seen = {}
        self._conns = threading.Semaphore(MAX_CONNS)
        self._err = None

    # ── жизненный цикл ──
    def _check_config(self):
        if len(self._secret) != SECRET_LEN:
            return _err("bad_config", "Нет общего секрета связки")
        if not isinstance(self._id, str) or not ID_RE.fullmatch(self._id):
            return _err("bad_config", "Неверный id этой машины")
        if not isinstance(self._port, int) or isinstance(self._port, bool) or not 1024 <= self._port <= 65535:
            return _err("bad_config", "Порт связки: 1024–65535")
        b = (self._bind or "").strip()
        if not b or b in ("0.0.0.0", "::", "*"):
            return _err("bad_config", "Связка не слушает 0.0.0.0 — нужен LAN-адрес этой машины")
        try:
            ipaddress.ip_address(b)
        except ValueError:
            return _err("bad_config", "Адрес этой машины для связки должен быть IP-адресом")
        if not is_private_ip(b, self._allow_loopback):
            return _err("bad_config", "Связка работает только в локальной сети")
        if not self._peers:
            return _err("bad_config", "Не указан адрес другой машины")
        self._targets, self._ip_to_peer = {}, {}
        for pid, hp in self._peers.items():
            if not isinstance(pid, str) or not ID_RE.fullmatch(pid) or pid == self._id:
                return _err("bad_config", "Неверный id пира")
            host, port = _split_hostport(hp, self._port)
            ip = _resolve_ip(host)
            if not ip or ip in ("0.0.0.0", "::") or not is_private_ip(ip, self._allow_loopback):
                return _err("bad_config", "Адрес другой машины должен быть из локальной сети")
            self._targets[pid] = (ip, port)
            self._ip_to_peer[ip] = pid
        return None

    def start(self):
        """{"ok","code","err"}. Не бросает."""
        try:
            bad = self._check_config()
            if bad:
                self._err = bad["err"]
                return bad
            with self._lock:
                if self._thread and self._thread.is_alive():
                    return {"ok": True, "code": None, "err": None}
            fam = socket.AF_INET6 if ":" in self._bind else socket.AF_INET
            s = socket.socket(fam, socket.SOCK_STREAM)
            try:
                if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                s.bind((self._bind, self._port))
                s.listen(8)
                s.settimeout(0.5)
            except OSError as e:
                s.close()
                self._err = "Порт связки занят или недоступен"
                log(f"bind {self._port}: {type(e).__name__}")
                return _err("os_error", self._err)
            self._stop.clear()
            with self._lock:
                self._sock = s
                self._thread = threading.Thread(target=self._serve, name="peer-link", daemon=True)
                self._thread.start()
            self._err = None
            log(f"связка слушает порт {self._port}")
            return {"ok": True, "code": None, "err": None}
        except Exception as e:
            log(f"start: {type(e).__name__}")
            return _err("os_error", "Связка не запустилась")

    def stop(self):
        self._stop.set()
        with self._lock:
            s, t = self._sock, self._thread
            self._sock = self._thread = None
        if s is not None:
            try:
                s.close()
            except OSError:
                pass
        if t is not None and t is not threading.current_thread():
            t.join(2)

    def running(self):
        t = self._thread
        return bool(t and t.is_alive())

    def status(self):
        with self._lock:
            return {"running": self.running(), "err": self._err, "last_seen": dict(self._last_seen)}

    def last_seen(self, peer_id):
        with self._lock:
            return self._last_seen.get(peer_id)

    # ── сервер ──
    def _serve(self):
        s = self._sock
        while not self._stop.is_set() and s is not None:
            try:
                conn, addr = s.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            ip = addr[0]
            if ip.startswith("::ffff:"):
                ip = ip[7:]
            if ip not in self._ip_to_peer or self._banned(ip) or not self._conns.acquire(blocking=False):
                try:
                    conn.close()
                except OSError:
                    pass
                continue
            threading.Thread(target=self._handle, args=(conn, ip), name="peer-link-conn", daemon=True).start()

    def _banned(self, ip):
        now = time.monotonic()
        with self._lock:
            until = self._ban.get(ip)
            if until is None:
                return False
            if now >= until:
                del self._ban[ip]
                return False
            return True

    def _note_bad(self, ip, reason):
        now = time.monotonic()
        with self._lock:
            lst = [t for t in self._bad.get(ip, []) if now - t < BAD_WINDOW_S] + [now]
            self._bad[ip] = lst
            if len(lst) >= BAD_LIMIT:
                self._ban[ip] = now + BAN_S
                self._bad[ip] = []
                log(f"{ip}: {BAD_LIMIT} неверных подписей за минуту — игнорирую {BAN_S} с")
        log(f"отклонено сообщение от {ip}: {reason}")

    def _verify(self, msg, now):
        with self._lock:
            return verify(msg, self._secret, now, self._seen, self_id=self._id)

    def _send(self, conn, obj):
        data = (json.dumps(obj, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")
        conn.settimeout(IO_TIMEOUT_S)
        conn.sendall(data)

    def _handle(self, conn, ip):
        try:
            line = _read_line(conn, time.monotonic() + IO_TIMEOUT_S)
            if line is None:
                return
            try:
                msg = json.loads(line.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                self._note_bad(ip, "format")
                return
            now = self._clock()
            ok, why = self._verify(msg, now)
            peer = self._ip_to_peer.get(ip)
            if ok and msg.get("from") != peer:
                ok, why = False, "from"
            if not ok:
                if why in ("mac", "format", "version"):
                    self._note_bad(ip, why)
                else:
                    log(f"отклонено сообщение от {ip}: {why}")
                try:
                    self._send(conn, {"v": PROTO, "error": why})   # без подписи: только причина (время/повтор)
                except OSError:
                    pass
                return
            with self._lock:
                self._last_seen[peer] = int(now)
            res = self._dispatch(peer, msg)
            reply = sign({"v": PROTO, "from": self._id, "to": peer, "ts": int(self._clock()),
                          "nonce": secrets.token_hex(16), "re": msg["nonce"], "res": res}, self._secret)
            self._send(conn, reply)
        except Exception as e:
            log(f"обработка соединения: {type(e).__name__}")
        finally:
            try:
                conn.close()
            except OSError:
                pass
            self._conns.release()

    def _dispatch(self, peer, msg):
        cmd, args = msg.get("cmd"), msg.get("args", {})
        if cmd not in self._commands or not isinstance(args, dict):
            return _err("bad_config", "Неизвестная команда")
        if cmd == "ping":
            return {"ok": True, "code": None, "err": None, "id": self._id}
        try:
            res = self._handler(peer, cmd, args)
            if not isinstance(res, dict):
                res = {"ok": True}
            json.dumps(res)   # проверка сериализуемости
            if len(json.dumps(res)) > MAX_MSG - 1024:
                return _err("bad_config", "Ответ слишком большой")
            res.setdefault("ok", True)
            return res
        except Exception as e:
            log(f"handler({cmd}): {type(e).__name__}")
            return _err("os_error", "Ошибка выполнения команды")

    # ── клиент ──
    def request(self, peer_id, cmd, args=None, timeout=IO_TIMEOUT_S):
        """Выполнить команду на пире. Возврат — результат команды ({"ok",...}) или ошибка транспорта
        (peer_unreachable / auth_failed / bad_config). Не бросает."""
        try:
            if peer_id not in self._targets:
                bad = self._check_config() if not self._targets else None
                if bad:
                    return bad
                if peer_id not in self._targets:
                    return _err("bad_config", "Неизвестная машина")
            if cmd not in self._commands:
                return _err("bad_config", "Неизвестная команда")
            args = args if args is not None else {}
            if not isinstance(args, dict):
                return _err("bad_config", "Аргументы команды должны быть объектом")
            ip, port = self._targets[peer_id]
            nonce = secrets.token_hex(16)
            msg = sign({"v": PROTO, "from": self._id, "to": peer_id, "ts": int(self._clock()), "nonce": nonce,
                        "cmd": cmd, "args": args}, self._secret)
            data = (json.dumps(msg, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")
            if len(data) > MAX_MSG:
                return _err("bad_config", "Запрос слишком большой")
            deadline = time.monotonic() + max(0.2, float(timeout))
            try:
                with socket.create_connection((ip, port), timeout=max(0.2, float(timeout))) as c:
                    c.settimeout(max(0.2, deadline - time.monotonic()))
                    c.sendall(data)
                    line = _read_line(c, deadline)
            except (OSError, socket.timeout):
                return _err("peer_unreachable", "Другая машина не отвечает")
            if line is None:
                return _err("peer_unreachable", "Другая машина не ответила вовремя")
            try:
                resp = json.loads(line.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                return _err("auth_failed", "Непонятный ответ другой машины")
            if isinstance(resp, dict) and "mac" not in resp and "error" in resp:
                why = resp.get("error")
                if why == "time":
                    return _err("auth_failed", "Часы на компьютерах расходятся больше чем на 30 с")
                return _err("auth_failed", "Другая машина не приняла подпись — проверьте код связки")
            ok, why = self._verify(resp, self._clock())
            if not ok or resp.get("re") != nonce or resp.get("from") != peer_id:
                return _err("auth_failed", "Ответ другой машины не прошёл проверку")
            with self._lock:
                self._last_seen[peer_id] = int(self._clock())
            res = resp.get("res")
            if not isinstance(res, dict):
                return _err("auth_failed", "Пустой ответ другой машины")
            res.setdefault("ok", True)
            res.setdefault("code", None)
            res.setdefault("err", None)
            return res
        except Exception as e:
            log(f"request: {type(e).__name__}")
            return _err("os_error", "Ошибка связки")
