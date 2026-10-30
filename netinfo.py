"""Сетевая шпаргалка сисадмина: сведения о локальной сети и простые сетевые проверки.

Все функции блокирующие — фронтенд вызывает их в фоновом потоке. Исключения наружу не выходят:
ошибки возвращаются в поле "err" (у traceroute — элементом списка с "err", см. traceroute()).

    local_info()                    -> {"hostname", "interfaces": [...], "gateway", "dns", "primary_ip", "err"}
    public_ip(timeout=4)            -> str | None        (внешний запрос — только по кнопке)
    ping(host, count=4)             -> {"ok", "sent", "received", "loss_pct", "min", "avg", "max", "err"}
    traceroute(host, max_hops=20, on_hop=None, cancel=None) -> [{"hop", "host", "ip", "ms"}, ...]
    check_port(host, port, timeout=3) -> {"open", "ms", "err"}
    dns_lookup(name)                -> {"ipv4": [...], "ipv6": [...], "err"}

Безопасность: host проверяется monitor.valid_host (имя хоста / IPv4 / IPv6, «-» в начале и
спецсимволы отклоняются); внешние программы запускаются без shell, аргументы — списком, системные
утилиты — по абсолютному пути (Windows — из System32). MAC-адреса и SSID в лог не пишутся.

Платформы: win32 (WinAPI GetAdaptersAddresses через ctypes — не зависит от языка системы,
без psutil и без медленного PowerShell; SSID — netsh wlan), darwin (ifconfig, route, scutil,
networksetup / ipconfig getsummary), linux (ip -j, /run/systemd/resolve/resolv.conf или
/etc/resolv.conf, resolvectl, iwgetid / nmcli).
"""
import ipaddress
import json
import os
import queue
import re
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

from monitor import MS_RE, OUT_LIMIT, WIN_TTL_RE, decode, is_ipv6, valid_host

_WIN = sys.platform == "win32"
_MAC = sys.platform == "darwin"
_LINUX = sys.platform.startswith("linux")
_NO_WINDOW = 0x08000000 if _WIN else 0

CMD_TIMEOUT = 5          # предел на вспомогательную команду local_info
MAX_PING_COUNT = 20
MAX_HOPS = 30
MAX_INPUT = 300          # длина пользовательского ввода до нормализации
PUBLIC_IP_URL = "https://api.ipify.org"
_IFNAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:@-]{0,31}")
_GUID_RE = re.compile(r"\{?([0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12})\}?")
_VPN_WORDS = ("vpn", "tap-windows", "tap-win", "wireguard", "wintun", "openvpn", "tailscale", "zerotier",
              "fortinet", "forticlient", "anyconnect", "globalprotect", "pangp", "nordlynx", "softether",
              "amneziawg", "outline", "radmin", "hamachi")
_VIRT_WORDS = ("hyper-v", "vmware", "virtualbox", "bluetooth", "loopback", "npcap", "docker")
# старые site-local DNS, которые Windows подставляет адаптерам без настроенного DNS
_FAKE_DNS = {"fec0:0:0:ffff::1", "fec0:0:0:ffff::2", "fec0:0:0:ffff::3"}


def log(msg):
    try:
        if sys.stderr:
            print(f"[panel/netinfo] {msg}", file=sys.stderr, flush=True)
    except Exception:
        pass


# ── общие помощники ──
def norm_host(h):
    """Пользовательский ввод -> проверенный host или None. Пробелы по краям и [скобки] IPv6 снимаются,
    кириллические домены переводятся в punycode; дальше — строгая проверка monitor.valid_host."""
    if not isinstance(h, str) or not h or len(h) > MAX_INPUT:
        return None
    h = h.strip()
    if h.startswith("[") and h.endswith("]"):
        h = h[1:-1]
    if h and not h.isascii():
        try:
            h = h.encode("idna").decode("ascii")
        except (UnicodeError, ValueError):
            return None
    return h if valid_host(h) else None


def _int_in(v, lo, hi):
    return isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi


def _ip_or_none(s):
    """Строка -> нормализованный IP (без %зоны) или None."""
    if not isinstance(s, str):
        return None
    s = s.strip().strip("[]")
    try:
        return str(ipaddress.ip_address(s.split("%", 1)[0]))
    except ValueError:
        return None


def _uniq(seq):
    out = []
    for x in seq:
        if x and x not in out:
            out.append(x)
    return out


def _exe(name):
    """Абсолютный путь к системной утилите или None (не ищем в текущей папке)."""
    if _WIN:
        p = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", name + ".exe")
        return p if os.path.isfile(p) else None
    path = os.environ.get("PATH", "") + os.pathsep + "/usr/sbin:/sbin:/usr/bin:/bin"
    return shutil.which(name, path=path)


def _env():
    return None if _WIN else dict(os.environ, LC_ALL="C", LANG="C")


def _run(args, timeout=CMD_TIMEOUT):
    """(код, текст) или (None, описание ошибки). args[0] — имя утилиты, ищется через _exe."""
    exe = _exe(args[0])
    if not exe:
        return None, f"{args[0]} не найден"
    try:
        r = subprocess.run([exe] + list(args[1:]), stdin=subprocess.DEVNULL, capture_output=True,
                           timeout=timeout, creationflags=_NO_WINDOW, env=_env())
    except subprocess.TimeoutExpired:
        return None, f"{args[0]}: превышено время ожидания"
    except OSError as e:
        return None, f"{args[0]} не запустился: {type(e).__name__}"
    return r.returncode, decode(r.stdout[:OUT_LIMIT])


def _primary_ip():
    """IPv4 исходящего интерфейса: UDP connect без отправки пакетов + getsockname."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))
            ip = s.getsockname()[0]
        return ip if ip and ip != "0.0.0.0" else None
    except OSError:
        return None


def _iface(name, ipv4=(), ipv6=(), mac=None, up=False, kind="other", ssid=None):
    return {"name": name, "ipv4": list(ipv4), "ipv6": list(ipv6), "mac": mac, "up": bool(up),
            "kind": kind, "ssid": ssid}


def _fmt_mac(raw):
    if not raw or not any(raw):
        return None
    return ":".join(f"{b:02X}" for b in raw)


def _norm_mac(s):
    if not isinstance(s, str) or not re.fullmatch(r"[0-9A-Fa-f]{2}(?:[:-][0-9A-Fa-f]{2}){5}", s.strip()):
        return None
    s = s.strip().replace("-", ":").upper()
    return None if s == "00:00:00:00:00:00" else s


def _pick_primary(ifaces, primary_ip):
    for it in ifaces:
        if primary_ip and primary_ip in it["ipv4"]:
            return it
    return None


# ── local_info: Windows ──
def _win_adapters():
    """Список (iface, gateways, dns, guid) через GetAdaptersAddresses."""
    import ctypes
    from ctypes import POINTER, c_char_p, c_int, c_ubyte, c_ulong, c_ulonglong, c_void_p, c_wchar_p, cast

    class SOCKET_ADDRESS(ctypes.Structure):
        _fields_ = [("lpSockaddr", c_void_p), ("iSockaddrLength", c_int)]

    class ADDR(ctypes.Structure):  # общий заголовок unicast / dns / gateway
        _fields_ = [("Alignment", c_ulonglong), ("Next", c_void_p), ("Address", SOCKET_ADDRESS)]

    class ADAPTER(ctypes.Structure):  # IP_ADAPTER_ADDRESSES_LH до FirstGatewayAddress включительно
        _fields_ = [("Alignment", c_ulonglong), ("Next", c_void_p), ("AdapterName", c_char_p),
                    ("FirstUnicastAddress", c_void_p), ("FirstAnycastAddress", c_void_p),
                    ("FirstMulticastAddress", c_void_p), ("FirstDnsServerAddress", c_void_p),
                    ("DnsSuffix", c_wchar_p), ("Description", c_wchar_p), ("FriendlyName", c_wchar_p),
                    ("PhysicalAddress", c_ubyte * 8), ("PhysicalAddressLength", c_ulong), ("Flags", c_ulong),
                    ("Mtu", c_ulong), ("IfType", c_ulong), ("OperStatus", c_int), ("Ipv6IfIndex", c_ulong),
                    ("ZoneIndices", c_ulong * 16), ("FirstPrefix", c_void_p), ("TransmitLinkSpeed", c_ulonglong),
                    ("ReceiveLinkSpeed", c_ulonglong), ("FirstWinsServerAddress", c_void_p),
                    ("FirstGatewayAddress", c_void_p)]

    fn = ctypes.windll.iphlpapi.GetAdaptersAddresses
    fn.argtypes = [c_ulong, c_ulong, c_void_p, c_void_p, POINTER(c_ulong)]
    fn.restype = c_ulong
    flags = 0x0002 | 0x0004 | 0x0080   # SKIP_ANYCAST | SKIP_MULTICAST | INCLUDE_GATEWAYS
    size = c_ulong(32 * 1024)
    for _ in range(4):
        buf = ctypes.create_string_buffer(size.value)
        rc = fn(0, flags, None, buf, ctypes.byref(size))
        if rc == 0:
            break
        if rc != 111:  # ERROR_BUFFER_OVERFLOW — повторить с новым размером
            raise OSError(f"GetAdaptersAddresses: код {rc}")
    else:
        raise OSError("GetAdaptersAddresses: буфер не подошёл")

    def sockaddr(sa):
        if not sa.lpSockaddr or sa.iSockaddrLength < 8:
            return None
        raw = ctypes.string_at(sa.lpSockaddr, sa.iSockaddrLength)
        fam = int.from_bytes(raw[0:2], "little")
        if fam == 2:
            return str(ipaddress.IPv4Address(raw[4:8]))
        if fam == 23 and len(raw) >= 24:
            return str(ipaddress.IPv6Address(raw[8:24]))
        return None

    def addrs(p):
        out = []
        while p and len(out) < 64:
            a = cast(p, POINTER(ADDR)).contents
            ip = sockaddr(a.Address)
            if ip:
                out.append(ip)
            p = a.Next
        return out

    res = []
    p = ctypes.addressof(buf)
    while p and len(res) < 128:
        a = cast(p, POINTER(ADAPTER)).contents
        p = a.Next
        desc = a.Description or ""
        name = a.FriendlyName or desc or "?"
        kind = _win_kind(a.IfType, desc)
        if kind is None:
            continue
        ips = addrs(a.FirstUnicastAddress)
        v4 = [x for x in ips if ":" not in x]
        v6 = [x for x in ips if ":" in x]
        n = min(a.PhysicalAddressLength, 8)
        mac = _fmt_mac(bytes(a.PhysicalAddress[:n])) if n == 6 else None
        guid = (a.AdapterName or b"").decode("ascii", "replace")
        it = _iface(name, v4, v6, mac, a.OperStatus == 1, kind)
        res.append((it, addrs(a.FirstGatewayAddress), [d for d in addrs(a.FirstDnsServerAddress)
                                                         if d not in _FAKE_DNS], guid))
    return res


def _win_kind(if_type, desc):
    """IfType + описание -> kind; None — служебный адаптер, который не показываем."""
    d = desc.lower()
    if if_type == 24 or any(w in d for w in ("teredo", "isatap", "6to4", "ip-https", "kernel debug",
                                             "wi-fi direct virtual", "wan miniport")):
        return None
    if if_type in (23, 53, 131) or any(w in d for w in _VPN_WORDS):
        return "vpn"
    if if_type == 71:
        return "wifi"
    if if_type == 6:
        return "other" if any(w in d for w in _VIRT_WORDS) else "ethernet"
    return "other"


def parse_netsh_wlan(text):
    """{guid_lower: ssid} из `netsh wlan show interfaces`. Без привязки к языку: GUID узнаём по формату
    значения, SSID — по ключу «SSID» (не переводится; «AP BSSID»/«BSSID» не путаются)."""
    out, guid = {}, None
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key, val = key.strip(), val.strip()
        m = _GUID_RE.fullmatch(val)
        if m:
            guid = m.group(1).lower()
        elif key.upper() == "SSID" and guid and val:
            out[guid] = val
    return out


def _local_win():
    ad = _win_adapters()
    if any(it["kind"] == "wifi" and it["up"] for it, *_ in ad):
        rc, text = _run(["netsh", "wlan", "show", "interfaces"])
        ssids = parse_netsh_wlan(text) if rc is not None else {}
        for it, _, _, guid in ad:
            m = _GUID_RE.search(guid)
            if it["kind"] == "wifi" and m:
                it["ssid"] = ssids.get(m.group(1).lower())
    return ad


def _info_win(primary_ip):
    ad = _local_win()
    ifaces = [x[0] for x in ad]
    prim = _pick_primary(ifaces, primary_ip)
    order = sorted(ad, key=lambda x: (x[0] is not prim, not x[0]["up"]))
    gw4 = [g for x in order if x[0]["up"] for g in x[1] if ":" not in g and g != "0.0.0.0"]
    gw6 = [g for x in order if x[0]["up"] for g in x[1] if ":" in g]
    dns = (order[0][2] if order and order[0][0] is prim else []) or \
        [d for x in order if x[0]["up"] and x[1] for d in x[2]]
    return ifaces, (gw4 or gw6 or [None])[0], _uniq(dns)


# ── local_info: Linux ──
def parse_ip_addr_json(text, sysfs=True):
    """`ip -j addr` -> интерфейсы (без lo)."""
    try:
        data = json.loads(text)
    except ValueError:
        return []
    out = []
    for d in data if isinstance(data, list) else []:
        if not isinstance(d, dict):
            continue
        name = str(d.get("ifname") or "")
        lt = str(d.get("link_type") or "")
        if not name or lt == "loopback":
            continue
        flags = d.get("flags") or []
        v4, v6 = [], []
        for a in d.get("addr_info") or []:
            ip = _ip_or_none(a.get("local")) if isinstance(a, dict) else None
            if ip:
                (v4 if a.get("family") == "inet" else v6).append(ip)
        up = "UP" in flags and ("LOWER_UP" in flags or d.get("operstate") == "UP")
        mac = _norm_mac(d.get("address")) if lt == "ether" else None
        out.append(_iface(name, v4, v6, mac, up, _linux_kind(name, lt, sysfs)))
    return out


def _linux_kind(name, link_type, sysfs=True):
    n = name.lower()
    if link_type in ("none", "ppp") or n.startswith(("tun", "tap", "wg", "ppp", "tailscale", "zt", "nordlynx",
                                                       "proton", "ipsec", "vti", "awg")):
        return "vpn"
    base = f"/sys/class/net/{name}"
    if sysfs and _IFNAME_RE.fullmatch(name) and os.path.isdir(base):
        if os.path.isdir(base + "/wireless") or os.path.exists(base + "/phy80211"):
            return "wifi"
        if link_type == "ether":
            return "ethernet" if os.path.exists(base + "/device") else "other"
        return "other"
    if n.startswith("wl"):
        return "wifi"
    if link_type == "ether" and n.startswith(("en", "eth")):
        return "ethernet"
    return "other"


def parse_ip_route_json(text):
    """`ip -j route show default` -> [(gateway, dev)] по возрастанию metric."""
    try:
        data = json.loads(text)
    except ValueError:
        return []
    rows = []
    for r in data if isinstance(data, list) else []:
        if isinstance(r, dict) and r.get("dst") == "default":
            gw = _ip_or_none(r.get("gateway"))
            if gw:
                m = r.get("metric")
                rows.append((m if isinstance(m, int) else 0, gw, str(r.get("dev") or "")))
    return [(gw, dev) for _, gw, dev in sorted(rows, key=lambda x: x[0])]


def parse_resolv_conf(text):
    return _uniq(_ip_or_none(ln.split()[1]) for ln in text.splitlines()
                 if ln.split()[:1] == ["nameserver"] and len(ln.split()) > 1)


def parse_resolvectl_dns(text):
    """`resolvectl dns`: «Global: 1.1.1.1» / «Link 2 (eth0): 192.168.0.1 fe80::1»."""
    ips = []
    for line in text.splitlines():
        _, sep, rest = line.partition("):") if "):" in line else line.partition(":")
        if sep:
            ips += [_ip_or_none(t) for t in rest.split()]
    return _uniq(ips)


def _is_stub(dns):
    return not dns or all(ipaddress.ip_address(d).is_loopback for d in dns)


def _read(path, limit=64 * 1024):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read(limit)
    except OSError:
        return ""


def _linux_dns():
    dns = parse_resolv_conf(_read("/run/systemd/resolve/resolv.conf"))
    if _is_stub(dns):
        dns = parse_resolv_conf(_read("/etc/resolv.conf"))
    if _is_stub(dns):
        rc, text = _run(["resolvectl", "dns"])
        if rc == 0:
            dns = parse_resolvectl_dns(text) or dns
    return dns


def parse_nmcli_wifi(text):
    """`nmcli -t -f ACTIVE,SSID dev wifi list ...` (LC_ALL=C): строка «yes:<ssid>», «\\:» — экранированное «:»."""
    for line in text.splitlines():
        if line.startswith("yes:"):
            return line[4:].replace("\\:", ":").replace("\\\\", "\\") or None
    return None


def _linux_ssid(name):
    if not _IFNAME_RE.fullmatch(name):
        return None
    rc, text = _run(["iwgetid", name, "-r"], timeout=3)
    if rc == 0 and text.strip():
        return text.strip().splitlines()[0]
    rc, text = _run(["nmcli", "-t", "-f", "ACTIVE,SSID", "dev", "wifi", "list", "ifname", name,
                     "--rescan", "no"], timeout=4)
    return parse_nmcli_wifi(text) if rc == 0 else None


def _info_linux(primary_ip):
    rc, text = _run(["ip", "-j", "addr"])
    if rc != 0:
        raise OSError(text if rc is None else f"ip addr: код {rc}")
    ifaces = parse_ip_addr_json(text)
    for it in ifaces:
        if it["kind"] == "wifi" and it["up"]:
            it["ssid"] = _linux_ssid(it["name"])
    rc, text = _run(["ip", "-j", "route", "show", "default"])
    routes = parse_ip_route_json(text) if rc == 0 else []
    rc, text = _run(["ip", "-j", "-6", "route", "show", "default"])
    routes += parse_ip_route_json(text) if rc == 0 else []
    prim = _pick_primary(ifaces, primary_ip)
    routes.sort(key=lambda r: (not prim or r[1] != prim["name"], ":" in r[0]))
    return ifaces, routes[0][0] if routes else None, _linux_dns()


# ── local_info: macOS ──
_MAC_SKIP = ("lo", "gif", "stf", "awdl", "llw", "anpi", "ap", "bridge", "XHC", "pktap", "iptap")


def parse_ifconfig(text, ports=None):
    """`ifconfig` (macOS/BSD) -> интерфейсы. ports — {device: "Hardware Port"} из networksetup."""
    ports = ports or {}
    blocks, cur = [], None
    for line in text.splitlines():
        m = re.match(r"^([A-Za-z0-9_.-]+):\s+flags=\d+<([^>]*)>", line)
        if m:
            cur = {"name": m.group(1), "flags": m.group(2).split(","), "v4": [], "v6": [], "mac": None,
                   "status": None}
            blocks.append(cur)
            continue
        if cur is None:
            continue
        f = line.split()
        if not f:
            continue
        if f[0] == "inet" and len(f) > 1:
            ip = _ip_or_none(f[1])
            if ip:
                cur["v4"].append(ip)
        elif f[0] == "inet6" and len(f) > 1:
            ip = _ip_or_none(f[1])
            if ip:
                cur["v6"].append(ip)
        elif f[0] == "ether" and len(f) > 1:
            cur["mac"] = _norm_mac(f[1])
        elif f[0] == "status:" and len(f) > 1:
            cur["status"] = f[1]
    out = []
    for b in blocks:
        name = b["name"]
        if re.match(r"^(?:%s)\d*$" % "|".join(_MAC_SKIP), name):
            continue
        port = ports.get(name, "")
        if re.match(r"^(?:utun|ipsec|ppp|tun|tap|wg)\d*$", name):
            if not b["v4"] and not any(not ipaddress.ip_address(x).is_link_local for x in b["v6"]):
                continue  # системные utun (iCloud, Continuity) — только link-local IPv6
            kind = "vpn"
        elif re.search(r"wi-?fi|airport", port, re.I):
            kind = "wifi"
        elif re.search(r"ethernet|lan\b", port, re.I) and "bridge" not in port.lower():
            kind = "ethernet"
        else:
            kind = "other"
        up = "UP" in b["flags"] and (b["status"] == "active" if b["status"] else "RUNNING" in b["flags"])
        out.append(_iface(name, b["v4"], b["v6"], b["mac"], up, kind))
    return out


def parse_hardware_ports(text):
    """`networksetup -listallhardwareports` -> {device: port}."""
    out, port = {}, None
    for line in text.splitlines():
        if line.startswith("Hardware Port:"):
            port = line.split(":", 1)[1].strip()
        elif line.startswith("Device:") and port is not None:
            out[line.split(":", 1)[1].strip()] = port
            port = None
    return out


def parse_route_get(text):
    for line in text.splitlines():
        k, _, v = line.strip().partition(":")
        if k == "gateway":
            return _ip_or_none(v)
    return None


def parse_scutil_dns(text):
    """`scutil --dns`: nameserver[N] из основной части (до «for scoped queries»)."""
    main = text.split("DNS configuration (for scoped queries)", 1)[0]
    return _uniq(_ip_or_none(m.group(1)) for m in re.finditer(r"nameserver\[\d+\]\s*:\s*(\S+)", main))


def parse_mac_ssid(text):
    """networksetup -getairportnetwork / ipconfig getsummary -> SSID или None."""
    for line in text.splitlines():
        m = re.match(r"^\s*Current (?:Wi-Fi|AirPort) Network:\s*(.+?)\s*$", line)
        if m:
            return m.group(1)
        m = re.match(r"^\s*SSID\s*:\s*(.+?)\s*$", line)
        if m and m.group(1) != "<redacted>":
            return m.group(1)
    return None


def _info_mac(primary_ip):
    rc, text = _run(["ifconfig"])
    if rc != 0:
        raise OSError(text if rc is None else f"ifconfig: код {rc}")
    rc2, ports_text = _run(["networksetup", "-listallhardwareports"])
    ifaces = parse_ifconfig(text, parse_hardware_ports(ports_text) if rc2 == 0 else {})
    for it in ifaces:
        if it["kind"] == "wifi" and it["up"] and _IFNAME_RE.fullmatch(it["name"]):
            rc, t = _run(["networksetup", "-getairportnetwork", it["name"]], timeout=3)
            ssid = parse_mac_ssid(t) if rc == 0 else None
            if not ssid:
                rc, t = _run(["ipconfig", "getsummary", it["name"]], timeout=3)
                ssid = parse_mac_ssid(t) if rc == 0 else None
            it["ssid"] = ssid
    rc, text = _run(["route", "-n", "get", "default"])
    gw = parse_route_get(text) if rc == 0 else None
    rc, text = _run(["scutil", "--dns"])
    return ifaces, gw, parse_scutil_dns(text) if rc == 0 else []


def local_info():
    """Сведения о локальной сети. Не бросает исключений; при сбое — частичные данные и "err"."""
    res = {"hostname": None, "interfaces": [], "gateway": None, "dns": [], "primary_ip": None, "err": None}
    try:
        res["hostname"] = socket.gethostname() or None
    except OSError:
        pass
    res["primary_ip"] = _primary_ip()
    try:
        if _WIN:
            fn = _info_win
        elif _MAC:
            fn = _info_mac
        elif _LINUX:
            fn = _info_linux
        else:
            res["err"] = f"платформа {sys.platform} не поддерживается"
            return res
        res["interfaces"], res["gateway"], res["dns"] = fn(res["primary_ip"])
        # активные и с адресами — выше
        res["interfaces"].sort(key=lambda i: (not i["up"], not (i["ipv4"] or i["ipv6"])))
    except Exception as e:
        res["err"] = f"не удалось получить сведения о сети: {type(e).__name__}"
        log(f"local_info: {type(e).__name__}")
    return res


# ── public_ip ──
def parse_public_ip(body):
    """Ответ сервиса -> IP-строка или None. Принимается только «голый» глобальный IPv4/IPv6."""
    if isinstance(body, bytes):
        try:
            body = body.decode("ascii")
        except UnicodeDecodeError:
            return None
    if not isinstance(body, str):
        return None
    s = body.strip()
    if not s or len(s) > 45 or not re.fullmatch(r"[0-9A-Fa-f:.]+", s):
        return None
    try:
        ip = ipaddress.ip_address(s)
    except ValueError:
        return None
    return str(ip) if ip.is_global else None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):  # редиректы не нужны — и не дадут уйти на http
        return None


def public_ip(timeout=4):
    """Внешний IP через https://api.ipify.org. Вызывать только по действию пользователя."""
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not 0 < timeout <= 30:
        timeout = 4
    opener = urllib.request.build_opener(_NoRedirect, urllib.request.HTTPSHandler(context=ssl.create_default_context()))
    req = urllib.request.Request(PUBLIC_IP_URL, headers={"User-Agent": "claude-panel", "Accept": "text/plain"})
    try:
        with opener.open(req, timeout=timeout) as r:
            if r.status != 200 or not r.geturl().startswith("https://"):
                return None
            return parse_public_ip(r.read(64))
    except (urllib.error.URLError, OSError, ValueError) as e:
        log(f"public_ip: {type(e).__name__}")
        return None


# ── ping ──
def _ping_result(sent):
    return {"ok": False, "sent": sent, "received": 0, "loss_pct": 100.0, "min": None, "avg": None, "max": None,
            "err": None}


def parse_ping_times(out, win=_WIN):
    """Времена ответов (мс) из вывода ping. Windows — без привязки к языку: IPv4-ответ — строка с «TTL=»
    (строки «узел недоступен» от маршрутизатора не считаются), IPv6 — строка ровно с одним «N ms/мс»
    (в итоговой строке их три). mac/linux (LC_ALL=C) — «time=», дубликаты (DUP!) пропускаются."""
    times = []
    for line in out.splitlines():
        if win:
            if "TTL=" in line.upper():
                m = WIN_TTL_RE.search(line)
            else:
                ms = MS_RE.findall(line)
                m = MS_RE.search(line) if len(ms) == 1 else None
            if m:
                times.append(float(m.group(1).replace(",", ".")))
        elif "time=" in line and "DUP!" not in line:
            m = re.search(r"time[=<]\s*(\d+(?:\.\d+)?)", line)
            if m:
                times.append(float(m.group(1)))
    return times


def _resolve_err(host):
    """None, если host — IP или имя разрешается; иначе текст ошибки."""
    if _ip_or_none(host):
        return None
    try:
        socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        return None
    except (socket.gaierror, UnicodeError):
        return "имя не найдено"
    except OSError as e:
        return f"ошибка DNS: {type(e).__name__}"


def ping(host, count=4):
    sent = count if _int_in(count, 1, MAX_PING_COUNT) else 4
    res = _ping_result(sent)
    if not _int_in(count, 1, MAX_PING_COUNT):
        res["err"] = f"count должен быть от 1 до {MAX_PING_COUNT}"
        return res
    h = norm_host(host)
    if not h:
        res["err"] = "недопустимый адрес"
        return res
    err = _resolve_err(h)
    if err:
        res["err"] = err
        return res
    v6 = is_ipv6(h)
    if _WIN:
        args = ["ping", "-n", str(count), "-w", "1000", h]
    elif _MAC:
        args = ["ping6", "-c", str(count), h] if v6 else ["ping", "-n", "-c", str(count), "-t", str(count + 2), h]
    else:  # старый iputils не знает -6 — для IPv6 берём ping6, если он есть
        tool, fam = ("ping6", []) if v6 and _exe("ping6") else ("ping", ["-6"] if v6 else [])
        args = [tool, "-n", "-c", str(count), "-W", "1", "-w", str(count + 2)] + fam + [h]
    rc, out = _run(args, timeout=count * 1.5 + 6)
    if rc is None:
        res["err"] = out
        return res
    times = parse_ping_times(out)[:count]
    n = len(times)
    res.update(received=n, loss_pct=round((count - n) * 100 / count, 1), ok=n > 0)
    if times:
        res.update(min=round(min(times), 1), avg=round(sum(times) / n, 1), max=round(max(times), 1))
    else:
        res["err"] = "нет ответа"
    return res


# ── traceroute ──
_TIME_TOK = re.compile(r"^(<)?(\d+(?:[.,]\d+)?)(?:ms|мс|msec)?$", re.I)


def parse_trace_line(line):
    """Строка tracert (Windows, любой язык) / traceroute -n -> {"hop","host","ip","ms"} или None.
    Время — среднее по ответившим пробам; «<1 мс» считается как 1."""
    f = line.split()
    if len(f) < 2 or not f[0].isdigit():
        return None
    hop, ip, times, stars = int(f[0]), None, [], 0
    for i, t in enumerate(f[1:], 1):
        if t == "*":
            stars += 1
            continue
        cand = _ip_or_none(t)
        if cand and ("." in t or ":" in t):
            ip = ip or cand
            continue
        m = _TIME_TOK.match(t)
        if m:
            nxt = f[i + 1].lower() if i + 1 < len(f) else ""
            glued = t[len(m.group(1) or "") + len(m.group(2)):]
            if glued or nxt in ("ms", "мс", "msec"):
                times.append(float(m.group(2).replace(",", ".")))
    if not times and not stars and not ip:
        return None
    ms = round(sum(times) / len(times), 1) if times else None
    return {"hop": hop, "host": ip or "*", "ip": ip, "ms": ms}


def parse_tracepath_line(line):
    """Строка `tracepath -n`: « 1:  192.168.0.1   0.512ms» / « 2:  no reply»; LOCALHOST и pmtu — пропуск."""
    m = re.match(r"^\s*(\d+)\??:\s+(\S+)(.*)$", line)
    if not m or m.group(2) == "[LOCALHOST]":
        return None
    ip = _ip_or_none(m.group(2))
    t = re.search(r"(\d+(?:\.\d+)?)ms", m.group(3))
    if not ip and m.group(2) != "no":
        return None
    return {"hop": int(m.group(1)), "host": ip or "*", "ip": ip, "ms": float(t.group(1)) if t and ip else None}


def _trace_err(msg):
    return {"hop": 0, "host": None, "ip": None, "ms": None, "err": msg}


def traceroute(host, max_hops=20, on_hop=None, cancel=None):
    """Маршрут до host. Каждый хоп — сразу в on_hop(dict) (вызывается из фонового потока: во фронте
    переложить в очередь/after). cancel — threading.Event: при установке процесс убивается, возвращается
    собранное. Ошибка (недопустимый host, нет утилиты, таймаут) — последним элементом с ключом "err"
    и hop=0; у обычных хопов ключа "err" нет."""
    hops = []

    def emit(d):
        hops.append(d)
        if on_hop:
            try:
                on_hop(dict(d))
            except Exception as e:
                log(f"traceroute on_hop: {type(e).__name__}")

    if not _int_in(max_hops, 1, MAX_HOPS):
        emit(_trace_err(f"max_hops должен быть от 1 до {MAX_HOPS}"))
        return hops
    h = norm_host(host)
    if not h:
        emit(_trace_err("недопустимый адрес"))
        return hops
    err = _resolve_err(h)
    if err:
        emit(_trace_err(err))
        return hops
    parser = parse_trace_line
    v6 = is_ipv6(h)
    if _WIN:
        args = ["tracert", "-d", "-h", str(max_hops), "-w", "1000", h]
    elif _MAC:
        args = ["traceroute6" if v6 else "traceroute", "-n", "-m", str(max_hops), "-w", "1", h]
    elif _exe("traceroute"):
        args = ["traceroute", "-n", "-m", str(max_hops), "-w", "1"] + (["-6"] if v6 else []) + [h]
    else:
        args, parser = ["tracepath", "-n", "-m", str(max_hops), h], parse_tracepath_line
    exe = _exe(args[0])
    if not exe:
        emit(_trace_err(f"{args[0]} не найден"))
        return hops
    try:
        proc = subprocess.Popen([exe] + args[1:], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, creationflags=_NO_WINDOW, env=_env())
    except OSError as e:
        emit(_trace_err(f"{args[0]} не запустился: {type(e).__name__}"))
        return hops

    lines = queue.Queue()

    def reader():
        total = 0
        try:
            for raw in iter(proc.stdout.readline, b""):
                total += len(raw)
                if total > OUT_LIMIT:
                    break
                lines.put(raw)
        except (OSError, ValueError):
            pass
        finally:
            lines.put(None)

    threading.Thread(target=reader, name="traceroute-read", daemon=True).start()
    deadline = time.monotonic() + max_hops * 5 + 10
    seen = set()
    try:
        while True:
            if cancel is not None and cancel.is_set():
                break
            if time.monotonic() > deadline:
                emit(_trace_err("превышено время ожидания"))
                break
            try:
                raw = lines.get(timeout=0.2)
            except queue.Empty:
                continue
            if raw is None:
                break
            d = parser(decode(raw))
            if d and d["hop"] not in seen and d["hop"] <= max_hops:
                seen.add(d["hop"])
                emit(d)
    finally:
        if proc.poll() is None:
            proc.kill()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
        if proc.stdout:
            proc.stdout.close()
    return hops


# ── порт и DNS ──
def check_port(host, port, timeout=3):
    res = {"open": False, "ms": None, "err": None}
    if not _int_in(port, 1, 65535):
        res["err"] = "порт должен быть от 1 до 65535"
        return res
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not 0 < timeout <= 30:
        res["err"] = "timeout должен быть от 0 до 30 с"
        return res
    h = norm_host(host)
    if not h:
        res["err"] = "недопустимый адрес"
        return res
    t0 = time.perf_counter()
    try:
        with socket.create_connection((h, port), timeout=timeout):
            pass
        res.update(open=True, ms=round((time.perf_counter() - t0) * 1000, 1))
    except ConnectionRefusedError:
        res["err"] = "соединение отклонено"
    except socket.timeout:
        # Windows на отказ (RST) повторяет SYN ~2 с, поэтому закрытый порт на loopback выглядит как таймаут
        loop = _ip_or_none(h) and ipaddress.ip_address(_ip_or_none(h)).is_loopback or h.lower().rstrip(".") == "localhost"
        res["err"] = ("закрыт или не отвечает (Windows повторяет попытки)" if _WIN and loop
                      else f"нет ответа за {timeout:g} с")
    except socket.gaierror:
        res["err"] = "имя не найдено"
    except OSError as e:
        res["err"] = f"ошибка соединения: {e.strerror or type(e).__name__}"
    return res


def dns_lookup(name):
    res = {"ipv4": [], "ipv6": [], "err": None}
    h = norm_host(name)
    if not h:
        res["err"] = "недопустимое имя"
        return res
    try:
        infos = socket.getaddrinfo(h, None, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError):
        res["err"] = "имя не найдено"
        return res
    except OSError as e:
        res["err"] = f"ошибка DNS: {type(e).__name__}"
        return res
    for fam, _, _, _, sa in infos:
        ip = _ip_or_none(sa[0])
        if not ip:
            continue
        key = "ipv4" if fam == socket.AF_INET else "ipv6" if fam == socket.AF_INET6 else None
        if key and ip not in res[key]:
            res[key].append(ip)
    if not res["ipv4"] and not res["ipv6"]:
        res["err"] = "адресов нет"
    return res
