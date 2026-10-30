"""Раскладка экранов «Мониторы»: модель, проверка config, геометрия переходов (docs/monitors-spec.md, §4, §5.1, §7).

Чистый модуль: без ОС, без Tk, без сети. Публичные функции исключений наружу не выпускают — неверные данные
превращаются в предупреждения (list[str]) или в значения по умолчанию.

Система координат стола: миллиметры, x вправо, y вниз, позиция экрана — левый верхний угол.
Пиксели — пиксели ОС своей машины (Windows: физические пиксели виртуального рабочего стола).
"""
import json
import math
import os
import re
import sys
import tempfile
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

SIDES = ("left", "right", "top", "bottom")
OS_NAMES = ("windows", "macos", "linux")
SIZE_SOURCES = ("edid", "user", "default")
NEIGHBOR_TOL_MM = 30.0     # допуск на неточное перетаскивание (§7.1)
NEIGHBOR_MAX_MM = 500.0    # дальше — уже не сосед
OVERLAP_WARN_MM = 20.0
ASPECT_TOL = 0.08
DEFAULT_DIAG_IN = {"laptop": 15.6, "monitor": 24.0}
CORNER_PUSH_WINDOW_S = 0.15  # окно накопления «дожима» (§7.4) — им пользуется платформенный код

MACHINE_ID_RE = re.compile(r"[a-z0-9-]{1,16}")
DESKFLOW_NAME_RE = re.compile(r"[A-Za-z0-9._][A-Za-z0-9._-]{0,62}")
APP_NAME_RE = re.compile(r"[\w .:-]{1,64}")

DEFAULT_EXTRA = {
    "deskflow": {"port": 24800, "server": None, "path": None, "tls": True, "clipboard": True,
                 "corner_band_pct": 1.0, "percent_decimals": None,
                 "work_on_hotkeys": {"work_on": "Control+Alt+Shift+F9", "back": "Control+Alt+Shift+F10",
                                     "lock": "Control+Alt+Shift+F11", "unlock": "Control+Alt+Shift+F12"}},
    "stream": {"host_machine": None, "client_machine": None, "host_app": None, "app_name": "Monitor",
               "virtual_screen": None, "virtual_match": ["SudoVDA", "Virtual Display", "SudoMaker"], "moonlight_path": None},
    # помощник краёв по умолчанию ВЫКЛЮЧЕН: включается только пользователем из UI
    "edge_helper": {"enabled": False, "poll_ms": 4, "push_mickeys": 4, "inset_px": 2,
                    "max_step_px": 600, "skip_while_button_down": False},
    "link": {"enabled": False, "port": 24850},
    "startup_mode": "last",
    "stop_on_exit": False,   # версия 1.1: закрытие панели не останавливает Deskflow и Moonlight (§8.1)
}
CLIENT_STATES = ("local", "client_relative", "client_absolute")  # §7.4.1
HOTKEY_RE = re.compile(r"^(Control|Alt|Shift|Super)(\+(Control|Alt|Shift|Super))*\+[A-Za-z0-9]{1,3}$")


def log(msg):
    try:
        if sys.stderr:
            print(f"[panel/layout] {msg}", file=sys.stderr, flush=True)
    except Exception:
        pass


def _r1(v):
    """Округление до 0,1 мм (половина — вверх по модулю, без банковского округления)."""
    return math.floor(v * 10 + 0.5) / 10


def _rd(v, d):
    k = 10 ** d
    return math.floor(v * k + 0.5) / k


def _clean(v):
    """Убрать шум float (336.20000000000005 → 336.2) без потери точности геометрии."""
    return round(v, 6)


def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


# ── Модель ──
@dataclass(frozen=True)
class Machine:
    id: str
    name: str
    hostname: str
    host: "str | None"
    os: str


@dataclass(frozen=True)
class Screen:
    id: str
    machine: str
    label: str
    px: "tuple[int, int]"
    scale: "int | None"
    size_mm: "tuple[float, float]"
    size_source: str
    pos_mm: "tuple[float, float]"
    os_id: "str | None"
    os_rect_px: "tuple[int, int, int, int] | None"


@dataclass(frozen=True)
class Layout:
    machines: "tuple[Machine, ...]"
    screens: "tuple[Screen, ...]"
    revision: int
    extra: dict  # deskflow/stream/edge_helper/link (dict) + startup_mode, stop_on_exit — после проверки

    def screen(self, sid):
        return next((s for s in self.screens if s.id == sid), None)

    def machine(self, mid):
        return next((m for m in self.machines if m.id == mid), None)


@dataclass(frozen=True)
class Segment:
    src: str
    side: str
    src_from_mm: float
    src_to_mm: float
    dst: str
    dst_from_mm: float
    dst_to_mm: float
    kind: str  # "linear" | "corner"


@dataclass(frozen=True)
class EdgeRule:
    src: str
    side: str
    line_px: int
    from_px: int
    to_px: int
    dst: str
    a: float
    b: float
    clamp_lo: int
    clamp_hi: int
    enter_px: int
    kind: str


@dataclass(frozen=True)
class DeskflowLink:
    src_machine: str
    side: str
    src_from_pct: float
    src_to_pct: float
    dst_machine: str
    dst_from_pct: float
    dst_to_pct: float


# ── Размеры ──
def size_from_diagonal(diag, unit, px):
    """Диагональ (дюймы «in» или сантиметры «cm») + разрешение → (ширина, высота) видимой области в мм."""
    d = float(diag) * (25.4 if unit == "in" else 10.0 if unit == "cm" else float("nan"))
    pw, ph = int(px[0]), int(px[1])
    if not (d > 0) or pw <= 0 or ph <= 0:
        raise ValueError("bad diagonal")
    k = math.hypot(pw, ph)
    return _r1(d * pw / k), _r1(d * ph / k)


def diagonal_in(size_mm):
    return round(math.hypot(float(size_mm[0]), float(size_mm[1])) / 25.4, 1)


def _aspect_ok(size_mm, px, tol=ASPECT_TOL):
    try:
        a_mm = size_mm[0] / size_mm[1]
        a_px = px[0] / px[1]
        return abs(a_mm / a_px - 1) <= tol
    except (ZeroDivisionError, TypeError, IndexError):
        return False


def default_size(px, kind="laptop"):
    return size_from_diagonal(DEFAULT_DIAG_IN.get(kind, 15.6), "in", px)


def _pnp_from(b):
    v = (b[8] << 8) | b[9]
    letters = "".join(chr(((v >> s) & 0x1F) + 64) for s in (10, 5, 0))
    if not all("A" <= c <= "Z" for c in letters):
        return None
    return f"{letters}{(b[10] | (b[11] << 8)):04X}"


def parse_edid(data, px=None):
    """EDID → {"mm": (w, h) | None, "name", "serial", "pnp"}. Чистая функция (§9.1).

    Размер: первый Detailed Timing Descriptor (0x36), запасной — байты 0x15/0x16 (см). Отбрасывается, если вне
    50…3000 мм или (при известном px) аспект расходится с пикселями больше 8 %.
    """
    out = {"mm": None, "name": None, "serial": None, "pnp": None}
    try:
        b = bytes(data or b"")
        if len(b) < 128 or b[:8] != b"\x00\xff\xff\xff\xff\xff\xff\x00":
            return out
        out["pnp"] = _pnp_from(b)
        serial = int.from_bytes(b[12:16], "little")
        out["serial"] = serial or None
        for off in (0x36, 0x48, 0x5A, 0x6C):
            d = b[off:off + 18]
            if d[0] == 0 and d[1] == 0 and d[3] in (0xFC, 0xFF):
                text = d[5:18].split(b"\x0a")[0].decode("ascii", "replace").strip()
                if d[3] == 0xFC and text:
                    out["name"] = text
                elif d[3] == 0xFF and text and not out["serial"]:
                    out["serial"] = text
        cands = []
        d = b[0x36:0x36 + 18]
        if d[0] or d[1]:  # pixel clock ≠ 0 — это действительно тайминг
            w = d[12] | ((d[14] & 0xF0) << 4)
            h = d[13] | ((d[14] & 0x0F) << 8)
            cands.append((float(w), float(h)))
        if b[0x15] and b[0x16]:
            cands.append((b[0x15] * 10.0, b[0x16] * 10.0))
        for w, h in cands:
            if not (50 <= w <= 3000 and 30 <= h <= 2000):
                continue
            if px and not _aspect_ok((w, h), px):
                # повёрнутый экран: EDID всегда в «родной» ориентации
                if _aspect_ok((h, w), px):
                    w, h = h, w
                else:
                    continue
            out["mm"] = (w, h)
            break
    except Exception as e:
        log(f"parse_edid: {type(e).__name__}")
    return out


# ── Проверка config (§4) ──
def _valid_host(h):
    try:
        import monitor
        return monitor.valid_host(h)
    except Exception:
        return isinstance(h, str) and 0 < len(h) <= 253 and h[0] != "-" and \
            re.fullmatch(r"[A-Za-z0-9.:%-]+", h) is not None


def _num(v, lo, hi, integer=False):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or (integer and not isinstance(v, int)):
        return None
    if isinstance(v, float) and not math.isfinite(v):
        return None
    return v if lo <= v <= hi else None


def _pair(v, lo, hi, integer=False):
    if not isinstance(v, (list, tuple)) or len(v) != 2:
        return None
    a, b = _num(v[0], lo[0], hi[0], integer), _num(v[1], lo[1], hi[1], integer)
    return None if a is None or b is None else (a, b)


def _str(v, limit, allow_empty=False):
    if not isinstance(v, str) or len(v) > limit or (not v and not allow_empty):
        return None
    if any(ord(c) < 32 for c in v):
        return None
    return v


def _abs_path(v):
    if v is None:
        return None
    if not isinstance(v, str) or not v or len(v) > 1024 or "\x00" in v or not os.path.isabs(v):
        raise ValueError
    return v


def _section(raw, key, warn, checks):
    """Проверить секцию-словарь: checks = {поле: функция(значение) -> значение | raise ValueError}."""
    defaults = DEFAULT_EXTRA[key]
    src = raw.get(key) if isinstance(raw, dict) else None
    if src is not None and not isinstance(src, dict):
        warn.append(f"monitors.{key}: не объект — взяты значения по умолчанию")
        src = None
    out = json.loads(json.dumps(defaults))
    for field, fn in checks.items():
        if src is None or field not in src:
            continue
        try:
            out[field] = fn(src[field])
        except Exception:
            warn.append(f"monitors.{key}.{field}: неверное значение — взято по умолчанию")
    return out


def _chk_range(lo, hi, integer=False):
    def f(v):
        r = _num(v, lo, hi, integer)
        if r is None:
            raise ValueError
        return float(r) if not integer else r
    return f


def _chk_hotkeys(v):
    base = DEFAULT_EXTRA["deskflow"]["work_on_hotkeys"]
    if not isinstance(v, dict) or any(k not in base for k in v) or \
            not all(isinstance(x, str) and HOTKEY_RE.fullmatch(x) for x in v.values()):
        raise ValueError
    return {**base, **v}


def _chk_bool(v):
    if not isinstance(v, bool):
        raise ValueError
    return v


def parse_config(raw):
    """Разобрать секцию monitors. Возврат (Layout | None, предупреждения). None — секции нет или она пустая/битая."""
    warn = []
    try:
        return _parse_config(raw, warn), warn
    except Exception as e:  # защита от неожиданных форм данных
        log(f"parse_config: {type(e).__name__}")
        warn.append("monitors: секция не разобрана")
        return None, warn


def _parse_config(raw, warn):
    if raw is None:
        return None
    if not isinstance(raw, dict):
        warn.append("monitors: не объект")
        return None
    if raw.get("version", 1) != 1:
        warn.append("monitors.version: неизвестная версия схемы")
        return None
    rev = raw.get("revision", 0)
    if isinstance(rev, bool) or not isinstance(rev, int) or rev < 0:
        warn.append("monitors.revision: неверное значение — 0")
        rev = 0

    machines = []
    for i, m in enumerate(raw.get("machines") or [] if isinstance(raw.get("machines"), list) else []):
        if len(machines) >= 4:
            warn.append("monitors.machines: больше 4 машин — лишние пропущены")
            break
        if not isinstance(m, dict):
            warn.append(f"machines[{i}]: не объект — пропущена")
            continue
        mid = m.get("id")
        if not isinstance(mid, str) or not MACHINE_ID_RE.fullmatch(mid) or any(x.id == mid for x in machines):
            warn.append(f"machines[{i}].id: неверный или повторный — машина пропущена")
            continue
        name = m.get("name")
        if not isinstance(name, str) or not DESKFLOW_NAME_RE.fullmatch(name):
            warn.append(f"machines[{i}].name: неверное имя — взято «{mid}»")
            name = mid
        hostname = _str(m.get("hostname"), 253, allow_empty=True)
        if hostname is None:
            if m.get("hostname") is not None:
                warn.append(f"machines[{i}].hostname: неверное значение")
            hostname = ""
        host = m.get("host")
        if host is not None and not _valid_host(host):
            warn.append(f"machines[{i}].host: неверный адрес — не задан")
            host = None
        osn = m.get("os")
        if osn not in OS_NAMES:
            warn.append(f"machines[{i}].os: неизвестная ОС — windows")
            osn = "windows"
        machines.append(Machine(mid, name, hostname, host, osn))
    if not machines:
        warn.append("monitors.machines: нет ни одной машины")
        return None
    mids = {m.id for m in machines}

    screens = []
    raw_screens = raw.get("screens") if isinstance(raw.get("screens"), list) else []
    for i, s in enumerate(raw_screens[:32]):
        if not isinstance(s, dict):
            warn.append(f"screens[{i}]: не объект — пропущен")
            continue
        mid = s.get("machine")
        sid = s.get("id")
        if mid not in mids or not isinstance(sid, str) or not re.fullmatch(rf"{re.escape(mid)}\.[1-9][0-9]{{0,2}}", sid) \
                or any(x.id == sid for x in screens):
            warn.append(f"screens[{i}]: неверный id/machine — экран пропущен")
            continue
        px = _pair(s.get("px"), (320, 200), (16384, 16384), integer=True)
        if px is None:
            warn.append(f"screens[{i}].px: неверное разрешение — экран пропущен")
            continue
        label = _str(s.get("label"), 64) or sid
        scale = s.get("scale")
        if scale is not None and _num(scale, 50, 500, integer=True) is None:
            warn.append(f"screens[{i}].scale: неверное значение")
            scale = None
        size = _pair(s.get("size_mm"), (50, 30), (3000, 2000))
        src = s.get("size_source")
        if size is None:
            warn.append(f"screens[{i}].size_mm: неверный размер — по умолчанию 15,6″")
            size, src = default_size(px), "default"
        elif src not in SIZE_SOURCES:
            src = "user"
        pos = _pair(s.get("pos_mm"), (-10000, -10000), (10000, 10000))
        if pos is None:
            warn.append(f"screens[{i}].pos_mm: неверная позиция — (0, 0)")
            pos = (0.0, 0.0)
        os_id = s.get("os_id")
        if os_id is not None and _str(os_id, 128) is None:
            warn.append(f"screens[{i}].os_id: неверное значение")
            os_id = None
        rect = s.get("os_rect_px")
        if rect is not None:
            ok = isinstance(rect, (list, tuple)) and len(rect) == 4 and \
                all(_num(v, -100000, 100000, integer=True) is not None for v in rect) and rect[2] > 0 and rect[3] > 0
            if not ok:
                warn.append(f"screens[{i}].os_rect_px: неверное значение")
                rect = None
            else:
                rect = tuple(int(v) for v in rect)
        screens.append(Screen(sid, mid, label, (int(px[0]), int(px[1])), scale,
                              (float(size[0]), float(size[1])), src, (float(pos[0]), float(pos[1])), os_id, rect))

    first = machines[0].id
    second = machines[1].id if len(machines) > 1 else None
    extra = {}
    extra["deskflow"] = _section(raw, "deskflow", warn, {
        "port": _chk_range(1024, 65535, integer=True),
        "server": lambda v: v if v in mids else (_ for _ in ()).throw(ValueError()),
        "path": _abs_path,
        "tls": _chk_bool, "clipboard": _chk_bool,
        "corner_band_pct": _chk_range(0.2, 5),
        "percent_decimals": lambda v: v if v is None or (v in (0, 2) and not isinstance(v, bool)) else
        (_ for _ in ()).throw(ValueError()),
        "work_on_hotkeys": _chk_hotkeys,
    })
    if extra["deskflow"]["server"] is None:
        extra["deskflow"]["server"] = first

    def _vm(v):
        if not isinstance(v, list) or not v or len(v) > 16 or not all(_str(x, 64) for x in v):
            raise ValueError
        return list(v)

    def _mid(v):
        if v not in mids:
            raise ValueError
        return v

    def _sid(v):
        if v is not None and not (isinstance(v, str) and re.fullmatch(r"[a-z0-9-]{1,16}\.[1-9][0-9]{0,2}", v)):
            raise ValueError
        return v

    extra["stream"] = _section(raw, "stream", warn, {
        "host_machine": _mid, "client_machine": _mid,
        "host_app": lambda v: v if v in ("apollo", "sunshine") else (_ for _ in ()).throw(ValueError()),
        "app_name": lambda v: v if isinstance(v, str) and APP_NAME_RE.fullmatch(v) and v[0] != "-" else
        (_ for _ in ()).throw(ValueError()),
        "virtual_screen": _sid, "virtual_match": _vm, "moonlight_path": _abs_path,
    })
    st = extra["stream"]
    st["host_machine"] = st["host_machine"] or first
    st["client_machine"] = st["client_machine"] or second
    if st["host_app"] is None:
        host_os = next(m.os for m in machines if m.id == st["host_machine"])
        st["host_app"] = "apollo" if host_os == "windows" else "sunshine"
    extra["edge_helper"] = _section(raw, "edge_helper", warn, {
        "enabled": _chk_bool,
        "poll_ms": _chk_range(2, 20, integer=True),
        "push_mickeys": _chk_range(0, 50, integer=True),
        "inset_px": _chk_range(1, 10, integer=True),
        "max_step_px": _chk_range(100, 5000, integer=True),
        "skip_while_button_down": _chk_bool,
    })
    extra["link"] = _section(raw, "link", warn, {"enabled": _chk_bool, "port": _chk_range(1024, 65535, integer=True)})
    sm = raw.get("startup_mode", "last")
    if sm not in ("last", "off"):
        warn.append("monitors.startup_mode: неверное значение — last")
        sm = "last"
    extra["startup_mode"] = sm
    soe = raw.get("stop_on_exit", False)
    if not isinstance(soe, bool):
        warn.append("monitors.stop_on_exit: неверное значение — false")
        soe = False
    extra["stop_on_exit"] = soe
    return Layout(tuple(machines), tuple(screens), rev, extra)


def to_config(layout):
    """Layout → JSON-вид секции monitors. revision +1, updated — сейчас."""
    ex = layout.extra or {}
    out = {
        "version": 1,
        "revision": int(layout.revision) + 1,
        "updated": now_iso(),
        "machines": [{"id": m.id, "name": m.name, "hostname": m.hostname, "host": m.host, "os": m.os}
                     for m in layout.machines],
        "screens": [{"id": s.id, "machine": s.machine, "label": s.label, "os_id": s.os_id,
                     "px": list(s.px), "scale": s.scale, "size_mm": [_r1(s.size_mm[0]), _r1(s.size_mm[1])],
                     "size_source": s.size_source, "pos_mm": [_r1(s.pos_mm[0]), _r1(s.pos_mm[1])],
                     "os_rect_px": list(s.os_rect_px) if s.os_rect_px else None}
                    for s in layout.screens],
    }
    for k in ("deskflow", "stream", "edge_helper", "link"):
        out[k] = dict(ex.get(k) or DEFAULT_EXTRA[k])
    out["startup_mode"] = ex.get("startup_mode", "last")
    out["stop_on_exit"] = ex.get("stop_on_exit", False)
    return out


def default_extra(machines=()):
    """Секции по умолчанию для новой раскладки (мастер первого запуска)."""
    ex = json.loads(json.dumps(DEFAULT_EXTRA))
    ids = [m.id for m in machines]
    if ids:
        ex["deskflow"]["server"] = ids[0]
        ex["stream"]["host_machine"] = ids[0]
        ex["stream"]["client_machine"] = ids[1] if len(ids) > 1 else None
        ex["stream"]["host_app"] = "apollo" if machines[0].os == "windows" else "sunshine"
    return ex


# ── Геометрия: прямоугольники ──
def _mm(s):
    x, y = s.pos_mm
    w, h = s.size_mm
    return x, y, x + w, y + h  # left, top, right, bottom


def _along_mm(s, side):
    l, t, r, b = _mm(s)
    return (t, b) if side in ("left", "right") else (l, r)


def _gap(x, s, side):
    """Зазор от края side экрана x до экрана s (положительный — между ними пусто)."""
    xl, xt, xr, xb = _mm(x)
    sl, st, sr, sb = _mm(s)
    return {"right": sl - xr, "left": xl - sr, "bottom": st - xb, "top": xt - sb}[side]


def _axis(x, s):
    """Ось пары: "h" — соседи слева/справа, "v" — сверху/снизу (§7.1)."""
    xl, xt, xr, xb = _mm(x)
    sl, st, sr, sb = _mm(s)
    ox = min(xr, sr) - max(xl, sl)
    oy = min(xb, sb) - max(xt, st)
    if ox > 0 and oy > 0:          # перекрываются (неточное перетаскивание) — разделяет меньшее перекрытие
        return "h" if ox <= oy else "v"
    if oy > 0:
        return "h"
    if ox > 0:
        return "v"
    return "h" if -ox <= -oy else "v"   # по диагонали — ось с меньшим зазором


def neighbors(layout, screen_id, side):
    """Экраны-кандидаты на стороне side (§7.1), по возрастанию зазора."""
    try:
        x = layout.screen(screen_id)
        if x is None or side not in SIDES:
            return []
        want = "h" if side in ("left", "right") else "v"
        out = []
        for s in layout.screens:
            if s.id == x.id:
                continue
            g = _gap(x, s, side)
            if g < -NEIGHBOR_TOL_MM or g > NEIGHBOR_MAX_MM or _axis(x, s) != want:
                continue
            out.append((g, s.id))
        return [sid for _, sid in sorted(out)]
    except Exception as e:
        log(f"neighbors: {type(e).__name__}")
        return []


def edge_segments(layout, screen_id, side):
    """Сегменты края (§7.2): покрывают весь край без дыр и перекрытий; [] — стена."""
    try:
        return _edge_segments(layout, screen_id, side)
    except Exception as e:
        log(f"edge_segments: {type(e).__name__}")
        return []


def _edge_segments(layout, screen_id, side):
    x = layout.screen(screen_id)
    ids = neighbors(layout, screen_id, side)
    if x is None or not ids:
        return []
    cands = [layout.screen(i) for i in ids]
    order = {s.id: n for n, s in enumerate(cands)}
    lo, hi = _along_mm(x, side)
    ends = []
    for s in cands:
        ends.extend(_along_mm(s, side))
    pts = {lo, hi}
    pts.update(v for v in ends if lo < v < hi)
    for i in range(len(ends)):
        for j in range(i + 1, len(ends)):
            m = (ends[i] + ends[j]) / 2
            if lo < m < hi:
                pts.add(m)
    pts = sorted(pts)
    raw = []
    for p, q in zip(pts, pts[1:]):
        if q - p <= 1e-9:
            continue
        mid = (p + q) / 2
        cover = [s for s in cands if _along_mm(s, side)[0] <= mid <= _along_mm(s, side)[1]]
        if cover:
            s = min(cover, key=lambda c: (_gap(x, c, side), order[c.id]))
            raw.append([p, q, s.id, "linear", None])
        else:
            def dist(c):
                a, b = _along_mm(c, side)
                return a - mid if mid < a else mid - b
            s = min(cands, key=lambda c: (dist(c), _gap(x, c, side), order[c.id]))
            a, b = _along_mm(s, side)
            raw.append([p, q, s.id, "corner", a if mid < a else b])
    merged = []
    for seg in raw:
        if merged and merged[-1][2:] == seg[2:]:
            merged[-1][1] = seg[1]
        else:
            merged.append(seg)
    out = []
    for p, q, dst, kind, corner in merged:
        if kind == "linear":
            out.append(Segment(x.id, side, _clean(p), _clean(q), dst, _clean(p), _clean(q), kind))
        else:
            out.append(Segment(x.id, side, _clean(p), _clean(q), dst, _clean(corner), _clean(corner), kind))
    return out


def normalize(layout):
    """Сдвинуть раскладку так, чтобы охватывающий прямоугольник начинался в (0, 0)."""
    if not layout.screens:
        return layout
    mx = min(s.pos_mm[0] for s in layout.screens)
    my = min(s.pos_mm[1] for s in layout.screens)
    return replace(layout, screens=tuple(replace(s, pos_mm=(_r1(s.pos_mm[0] - mx), _r1(s.pos_mm[1] - my)))
                                         for s in layout.screens))


def snap(layout, screen_id, pos_mm, tol_mm=15.0):
    """Привязка позиции перетаскиваемого экрана к краям соседей (и выравнивание по верху/низу/центру)."""
    try:
        me = layout.screen(screen_id)
        x, y = float(pos_mm[0]), float(pos_mm[1])
        if me is None:
            return _r1(x), _r1(y)
        w, h = me.size_mm
        best_x, best_y = None, None
        for s in layout.screens:
            if s.id == screen_id:
                continue
            sl, st, sr, sb = _mm(s)
            for cand in (sr, sl - w, sl, sr - w, (sl + sr) / 2 - w / 2):
                d = abs(cand - x)
                if d <= tol_mm and (best_x is None or d < best_x[0]):
                    best_x = (d, cand)
            for cand in (sb, st - h, st, sb - h, (st + sb) / 2 - h / 2):
                d = abs(cand - y)
                if d <= tol_mm and (best_y is None or d < best_y[0]):
                    best_y = (d, cand)
        if best_x:
            x = best_x[1]
        if best_y:
            y = best_y[1]
        return _r1(x), _r1(y)
    except Exception as e:
        log(f"snap: {type(e).__name__}")
        try:
            return _r1(float(pos_mm[0])), _r1(float(pos_mm[1]))
        except Exception:
            return 0.0, 0.0


# ── Пиксели ОС ──
def _rect_px(s, layout=None):
    """Прямоугольник экрана в пикселях ОС; None — неизвестен. Единственный экран машины без rect — (0,0,px)."""
    if s.os_rect_px:
        return tuple(s.os_rect_px)
    if layout is not None and sum(1 for o in layout.screens if o.machine == s.machine) == 1:
        return 0, 0, s.px[0], s.px[1]
    return None


def _px_axis(s, rect, side):
    """(начало вдоль края в px, длина в px, начало в мм, длина в мм)."""
    x, y, w, h = rect
    if side in ("left", "right"):
        return y, h, s.pos_mm[1], s.size_mm[1]
    return x, w, s.pos_mm[0], s.size_mm[0]


def _mm_to_px(s, rect, side, v_mm):
    o, n, o_mm, n_mm = _px_axis(s, rect, side)
    return o + (v_mm - o_mm) * n / n_mm


def _ceil(v):
    return math.ceil(v - 1e-6)


def internal_rules(layout, machine_id, inset_px=2):
    """Правила помощника краёв для переходов между экранами одной машины (§7.4)."""
    warn = []
    try:
        return _internal_rules(layout, machine_id, int(inset_px), warn), warn
    except Exception as e:
        log(f"internal_rules: {type(e).__name__}")
        warn.append("правила помощника не построены")
        return [], warn


def _internal_rules(layout, machine_id, inset, warn):
    rules = []
    mine = [s for s in layout.screens if s.machine == machine_id]
    for x in mine:
        xr = _rect_px(x, layout)
        if xr is None:
            warn.append(f"Экран «{x.label}»: неизвестно его положение в ОС")
            continue
        for side in SIDES:
            for seg in edge_segments(layout, x.id, side):
                y = layout.screen(seg.dst)
                if y is None or y.machine != machine_id:
                    continue
                yr = _rect_px(y, layout)
                if yr is None:
                    continue
                xo, xn, xo_mm, xn_mm = _px_axis(x, xr, side)
                yo, yn, yo_mm, yn_mm = _px_axis(y, yr, side)
                lo_mm, hi_mm = _along_mm(x, side)
                f = xo if abs(seg.src_from_mm - lo_mm) < 1e-6 else _ceil(_mm_to_px(x, xr, side, seg.src_from_mm))
                t = xo + xn if abs(seg.src_to_mm - hi_mm) < 1e-6 else _ceil(_mm_to_px(x, xr, side, seg.src_to_mm))
                if t <= f:
                    continue
                ky, kx = yn / yn_mm, xn / xn_mm
                if seg.kind == "linear":
                    a = ky / kx
                    b = yo + (xo_mm - yo_mm) * ky - a * xo
                else:
                    a = 0.0
                    b = float(yo + inset if abs(seg.dst_from_mm - yo_mm) < 1e-6 else yo + yn - 1 - inset)
                X, Y, W, H = xr
                YX, YY, YW, YH = yr
                line = {"right": X + W, "left": X, "bottom": Y + H, "top": Y}[side]
                enter = {"right": YX + inset, "left": YX + YW - 1 - inset,
                         "bottom": YY + inset, "top": YY + YH - 1 - inset}[side]
                consistent = {"right": YX >= X + W, "left": YX + YW <= X,
                              "bottom": YY >= Y + H, "top": YY + YH <= Y}[side]
                if not consistent:
                    warn.append(f"Раскладка ОС не совпадает с физической («{x.label}» → «{y.label}») — "
                                "нажмите «Применить к Windows»")
                rules.append(EdgeRule(x.id, side, int(line), int(f), int(t), y.id, round(a, 6), round(b, 4),
                                      int(yo + inset), int(yo + yn - 1 - inset), int(enter), seg.kind))
    return rules


def rule_target(rule, along):
    v = int(math.floor(rule.a * along + rule.b + 0.5))
    v = max(rule.clamp_lo, min(rule.clamp_hi, v))
    return (rule.enter_px, v) if rule.side in ("left", "right") else (v, rule.enter_px)


def _which(rects, pt):
    px, py = pt
    for sid, (x, y, w, h) in rects.items():
        if x <= px < x + w and y <= py < y + h:
            return sid
    return None


def edge_decide(rules, rects, prev, now, push, cfg):
    """Куда перенести курсор (x, y) или None (§7.4). Чистая функция.

    rules — EdgeRule этой машины; rects — {screen_id: (x, y, w, h)} в пикселях ОС; prev/now — (x, y);
    push — сумма сырых смещений (dx, dy) за последние 150 мс или None (Raw Input недоступен);
    cfg — {"max_step_px", "push_mickeys", "paused", "button_down", "skip_while_button_down"}.
    """
    res = edge_decide_ex(rules, rects, prev, now, push, cfg)
    return res[0] if res else None


def edge_decide_ex(rules, rects, prev, now, push, cfg):
    """То же, что edge_decide, но возвращает (цель, правило) — для статуса помощника."""
    try:
        if prev is None or now is None or cfg.get("paused"):
            return None
        if math.hypot(now[0] - prev[0], now[1] - prev[1]) > cfg.get("max_step_px", 600):
            return None  # телепорт: Deskflow, программа, «Облегчить перемещение» не настолько далеко
        if cfg.get("skip_while_button_down") and cfg.get("button_down"):
            return None
        if prev != now:
            r = _os_crossing(rules, rects, prev, now)
            if r:
                return r
        if push is not None:
            return _push_through(rules, rects, now, push, cfg.get("push_mickeys", 4))
        return None
    except Exception as e:
        log(f"edge_decide: {type(e).__name__}")
        return None


def _os_crossing(rules, rects, prev, now):
    xs, zs = _which(rects, prev), _which(rects, now)
    if xs is None or zs is None or xs == zs:
        return None
    X, Y, W, H = rects[xs]
    best = None
    for side, line, p0, p1 in (("right", X + W, prev[0], now[0]), ("left", X, prev[0], now[0]),
                               ("bottom", Y + H, prev[1], now[1]), ("top", Y, prev[1], now[1])):
        if side in ("right", "bottom"):
            if not (p0 < line <= p1):
                continue
            t = (line - p0) / (p1 - p0)
        else:
            if not (p1 < line <= p0):
                continue
            t = (p0 - line) / (p0 - p1) if p0 != p1 else 0.0
        if best is None or t < best[0]:
            best = (t, side)
    if best is None:
        return None
    t, side = best
    if side in ("left", "right"):
        along = prev[1] + t * (now[1] - prev[1])
        along = max(Y, min(Y + H - 1, int(math.floor(along + 0.5))))
    else:
        along = prev[0] + t * (now[0] - prev[0])
        along = max(X, min(X + W - 1, int(math.floor(along + 0.5))))
    for r in rules:
        if r.src == xs and r.side == side and r.from_px <= along < r.to_px:
            tgt = rule_target(r, along)
            return None if tuple(tgt) == tuple(now) else (tgt, r)
    return None


def _push_through(rules, rects, now, push, need):
    xs = _which(rects, now)
    if xs is None:
        return None
    X, Y, W, H = rects[xs]
    dx, dy = push
    for side, on_edge, out, along in (("right", now[0] == X + W - 1, dx, now[1]),
                                      ("left", now[0] == X, -dx, now[1]),
                                      ("bottom", now[1] == Y + H - 1, dy, now[0]),
                                      ("top", now[1] == Y, -dy, now[0])):
        if not on_edge or out <= 0 or out < need:
            continue
        for r in rules:
            if r.src == xs and r.side == side and r.from_px <= along < r.to_px:
                return rule_target(r, along), r
    return None


def snapback(warp_from, warp_to, now, tol_px=8):
    """Эвристика отката (§7.4.1): после переноса warp_from→warp_to курсор вернулся (now) ближе tol_px к точке,
    где он был бы без переноса. True — признак абсолютного ввода чужой модели (client_absolute)."""
    try:
        if warp_from is None or warp_to is None or now is None:
            return False
        if tuple(warp_from) == tuple(warp_to):
            return False
        d_back = math.hypot(now[0] - warp_from[0], now[1] - warp_from[1])
        d_tgt = math.hypot(now[0] - warp_to[0], now[1] - warp_to[1])
        return d_back <= tol_px and d_back < d_tgt
    except (TypeError, IndexError):
        return False


# ── Раскладка ОС по физике (§7.5) ──
def suggest_os_positions(layout, machine_id):
    """{screen_id: (x, y)} — позиции экранов машины в пикселях ОС, ближайшие к физической раскладке."""
    try:
        return _suggest(layout, machine_id)
    except Exception as e:
        log(f"suggest_os_positions: {type(e).__name__}")
        return {}


def _px_size(s):
    return (s.os_rect_px[2], s.os_rect_px[3]) if s.os_rect_px else s.px


def _suggest(layout, machine_id):
    mine = [s for s in layout.screens if s.machine == machine_id]
    if not mine:
        return {}
    primary = next((s for s in mine if s.os_rect_px and s.os_rect_px[:2] == (0, 0)), mine[0])
    pos = {primary.id: (0, 0)}
    queue = [primary]
    while queue:
        x = queue.pop(0)
        xw, xh = _px_size(x)
        xx, xy = pos[x.id]
        for side in SIDES:
            for sid in neighbors(layout, x.id, side):
                s = layout.screen(sid)
                if s.machine != machine_id or sid in pos:
                    continue
                sw, sh = _px_size(s)
                if side in ("left", "right"):
                    nx = xx + xw if side == "right" else xx - sw
                    (xo_mm, xn_mm), (so_mm, sn_mm) = (x.pos_mm[1], x.size_mm[1]), (s.pos_mm[1], s.size_mm[1])
                    xo, xn, sn = xy, xh, sh
                else:
                    ny = xy + xh if side == "bottom" else xy - sh
                    (xo_mm, xn_mm), (so_mm, sn_mm) = (x.pos_mm[0], x.size_mm[0]), (s.pos_mm[0], s.size_mm[0])
                    xo, xn, sn = xx, xw, sw
                mid = (max(xo_mm, so_mm) + min(xo_mm + xn_mm, so_mm + sn_mm)) / 2
                x_px = xo + (mid - xo_mm) * xn / xn_mm
                s_px = (mid - so_mm) * sn / sn_mm
                o = int(math.floor(x_px - s_px + 0.5))
                # касание хотя бы на 1 px (Windows требует, чтобы экраны соприкасались)
                o = max(xo - sn + 1, min(xo + xn - 1, o))
                pos[sid] = (nx, o) if side in ("left", "right") else (o, ny)
                queue.append(s)
    return pos


def _px_touch(a, b):
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    oy = min(ay + ah, by + bh) - max(ay, by)
    ox = min(ax + aw, bx + bw) - max(ax, bx)
    if ox > 0 and oy > 0:
        return "overlap"
    if (ax + aw == bx or bx + bw == ax) and oy >= 1:
        return True
    if (ay + ah == by or by + bh == ay) and ox >= 1:
        return True
    return False


def check_os_positions(rects, primary):
    """None — все экраны касаются и не перекрываются, иначе текст ошибки."""
    ids = list(rects)
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            if _px_touch(rects[a], rects[b]) == "overlap":
                return "экраны перекрываются"
    seen, stack = {primary}, [primary]
    while stack:
        a = stack.pop()
        for b in ids:
            if b not in seen and _px_touch(rects[a], rects[b]) is True:
                seen.add(b)
                stack.append(b)
    return None if len(seen) == len(ids) else "не все экраны касаются друг друга"


# ── Связи Deskflow (§7.3) ──
def _bbox(rects):
    x0 = min(r[0] for r in rects)
    y0 = min(r[1] for r in rects)
    x1 = max(r[0] + r[2] for r in rects)
    y1 = max(r[1] + r[3] for r in rects)
    return x0, y0, x1 - x0, y1 - y0


def deskflow_links(layout, decimals=2, band_pct=1.0):
    warn = []
    try:
        return _deskflow_links(layout, decimals, float(band_pct), warn), warn
    except Exception as e:
        log(f"deskflow_links: {type(e).__name__}")
        warn.append("связи Deskflow не построены")
        return [], warn


def _machine_rects(layout, warn):
    out = {}
    for m in layout.machines:
        rects = {}
        for s in layout.screens:
            if s.machine != m.id:
                continue
            r = _rect_px(s, layout)
            if r is None:
                warn.append(f"Экран «{s.label}»: неизвестно его положение в ОС — переходы Deskflow не считаются")
                rects = None
                break
            rects[s.id] = r
        if rects:
            out[m.id] = (rects, _bbox(list(rects.values())))
    return out


def _deskflow_links(layout, d, band, warn):
    mr = _machine_rects(layout, warn)
    links = []
    for m in layout.machines:
        if m.id not in mr:
            continue
        rects, bb = mr[m.id]
        bx, by, bw, bh = bb
        for x in layout.screens:
            if x.machine != m.id:
                continue
            xr = rects[x.id]
            for side in SIDES:
                segs = [g for g in edge_segments(layout, x.id, side)
                        if layout.screen(g.dst).machine != m.id and layout.screen(g.dst).machine in mr]
                if not segs:
                    continue
                X, Y, W, H = xr
                on_edge = {"right": X + W == bx + bw, "left": X == bx,
                           "bottom": Y + H == by + bh, "top": Y == by}[side]
                if not on_edge:
                    names = ", ".join(sorted({layout.screen(g.dst).label for g in segs}))
                    warn.append(f"Переход с «{x.label}» на «{names}» невозможен: «{x.label}» не на краю рабочего "
                                "стола своей машины — переставьте экраны в ОС")
                    continue
                s_o, s_n = (by, bh) if side in ("left", "right") else (bx, bw)
                for g in segs:
                    y = layout.screen(g.dst)
                    t_rects, t_bb = mr[y.machine]
                    yr = t_rects[y.id]
                    t_o, t_n = (t_bb[1], t_bb[3]) if side in ("left", "right") else (t_bb[0], t_bb[2])

                    def spct(v_mm):
                        return (_mm_to_px(x, xr, side, v_mm) - s_o) / s_n * 100

                    def tpct(v_mm):
                        return (_mm_to_px(y, yr, side, v_mm) - t_o) / t_n * 100
                    y_lo_mm, y_hi_mm = _along_mm(y, side)
                    y_lo, y_hi = tpct(y_lo_mm), tpct(y_hi_mm)
                    sf, stt = spct(g.src_from_mm), spct(g.src_to_mm)
                    if g.kind == "linear":
                        df, dt = tpct(g.dst_from_mm), tpct(g.dst_to_mm)
                    elif abs(g.dst_from_mm - y_lo_mm) < 1e-6:
                        df, dt = y_lo, y_lo + band
                    else:
                        df, dt = y_hi - band, y_hi
                    sf, stt = _rd(sf, d), _rd(stt, d)
                    if stt <= sf:
                        continue
                    k = 10 ** d
                    lo_b = math.ceil(y_lo * k - 1e-6) / k   # внутрь покрытого монитором диапазона
                    hi_b = math.floor(y_hi * k + 1e-6) / k
                    df = min(max(_rd(df, d), lo_b), hi_b)
                    dt = min(max(_rd(dt, d), lo_b), hi_b)
                    if dt <= df:  # пусто после округления — расширить на 1 % внутрь
                        if df + 1 <= hi_b:
                            dt = df + 1
                        else:
                            df = max(lo_b, dt - 1)
                    links.append(DeskflowLink(m.id, side, sf, stt, y.machine, _rd(df, d), _rd(dt, d)))
    order = {m.id: i for i, m in enumerate(layout.machines)}
    links.sort(key=lambda lk: (order[lk.src_machine], SIDES.index(lk.side), lk.src_from_pct))
    return links


# ── Проверка раскладки ──
def validate(layout):
    warn = []
    try:
        ss = list(layout.screens)
        for i, a in enumerate(ss):
            for b in ss[i + 1:]:
                al, at, ar, ab = _mm(a)
                bl, bt, br, bb = _mm(b)
                ox, oy = min(ar, br) - max(al, bl), min(ab, bb) - max(at, bt)
                if ox > OVERLAP_WARN_MM and oy > OVERLAP_WARN_MM:
                    warn.append(f"Экраны «{a.label}» и «{b.label}» перекрываются")
        for s in ss:
            if s.size_source == "default":
                warn.append(f"Экран «{s.label}» без EDID-размера — проверьте диагональ")
            if not _aspect_ok(s.size_mm, s.px):
                warn.append(f"Экран «{s.label}»: пропорции размера в мм и разрешения не совпадают")
        _, w = deskflow_links(layout)
        warn.extend(w)
        for m in layout.machines:
            _, w = internal_rules(layout, m.id)
            warn.extend(w)
    except Exception as e:
        log(f"validate: {type(e).__name__}")
        warn.append("раскладка не проверена")
    seen, out = set(), []
    for w in warn:
        if w not in seen:
            seen.add(w)
            out.append(w)
    return out


# ── Запись config.json (§4) ──
def write_config_section(path, key, value):
    """Атомарно заменить один ключ верхнего уровня в config.json. Нечитаемый JSON не перезаписывается."""
    try:
        p = Path(path)
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8-sig"))
            except (ValueError, UnicodeDecodeError):
                return {"ok": False, "code": "bad_config", "err": "config.json повреждён — не перезаписываю",
                        "hint": "исправьте JSON вручную"}
            if not isinstance(data, dict):
                return {"ok": False, "code": "bad_config", "err": "config.json: ожидался объект"}
        else:
            data = {}
        data[key] = value
        text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
        fd, tmp = tempfile.mkstemp(prefix=".config-", suffix=".tmp", dir=str(p.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
            last = None
            for _ in range(5):  # OneDrive/антивирус могут ненадолго держать файл
                try:
                    os.replace(tmp, p)
                    last = None
                    break
                except PermissionError as e:
                    last = e
                    import time
                    time.sleep(0.1)
            if last:
                raise last
        finally:
            if os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
        return {"ok": True, "code": None, "err": None}
    except OSError as e:
        log(f"write_config_section: {type(e).__name__}")
        return {"ok": False, "code": "os_error", "err": "Не удалось записать config.json"}
    except (TypeError, ValueError) as e:
        log(f"write_config_section: {type(e).__name__}")
        return {"ok": False, "code": "bad_config", "err": "Значение не сериализуется в JSON"}


# ── Пример §7.6 ──
EXAMPLE = {
    "version": 1, "revision": 7,
    "machines": [
        {"id": "a", "name": "laptop-a", "hostname": "LAPTOP-A", "host": "192.0.2.11", "os": "windows"},
        {"id": "b", "name": "laptop-b", "hostname": "LAPTOP-B", "host": "192.0.2.12", "os": "windows"},
    ],
    "screens": [
        {"id": "a.1", "machine": "a", "label": "Ноут A", "os_id": "win:SDC4161:00000000:UID4352",
         "px": [1920, 1080], "scale": 125, "size_mm": [345.4, 194.3], "size_source": "edid",
         "pos_mm": [0.0, 220.0], "os_rect_px": [0, 0, 1920, 1080]},
        {"id": "a.2", "machine": "a", "label": "Монитор 27\"", "os_id": "win:GSM5B7F:0001E240",
         "px": [2560, 1440], "scale": 100, "size_mm": [597.7, 336.2], "size_source": "user",
         "pos_mm": [345.4, 0.0], "os_rect_px": [1920, -868, 2560, 1440]},
        {"id": "b.1", "machine": "b", "label": "Ноут B", "os_id": "win:BOE0A1C:00000000:UID256",
         "px": [1920, 1080], "scale": 125, "size_mm": [345.4, 194.3], "size_source": "edid",
         "pos_mm": [943.1, 230.0], "os_rect_px": [0, 0, 1920, 1080]},
    ],
}


def _print_example():
    lay, warn = parse_config(EXAMPLE)
    print("Предупреждения разбора:", warn or "нет")
    for sid in ("a.1", "a.2", "b.1"):
        for side in SIDES:
            for g in edge_segments(lay, sid, side):
                print(f"  {g.src} {g.side}: {g.src_from_mm:.1f}…{g.src_to_mm:.1f} мм → {g.dst} "
                      f"{g.dst_from_mm:.1f}…{g.dst_to_mm:.1f} ({g.kind})")
    print("Раскладка Windows на A:", suggest_os_positions(lay, "a"))
    rules, w = internal_rules(lay, "a")
    print("Правила помощника на A:", w or "")
    for r in rules:
        print(f"  {r.src}.{r.side} line={r.line_px} [{r.from_px},{r.to_px}) → {r.dst} "
              f"y={r.a:.4f}·s{r.b:+.1f} clamp=[{r.clamp_lo},{r.clamp_hi}] enter={r.enter_px} {r.kind}")
    for d in (2, 0):
        links, w = deskflow_links(lay, decimals=d)
        print(f"Deskflow (decimals={d}):", w or "")
        for lk in links:
            print(f"  {lk.src_machine}: {lk.side}({lk.src_from_pct:g},{lk.src_to_pct:g}) = "
                  f"{lk.dst_machine}({lk.dst_from_pct:g},{lk.dst_to_pct:g})")
    vd = replace(lay, screens=tuple(replace(s, machine="a") if s.id == "b.1" else s for s in lay.screens))
    print("«B как монитор» — VD на A:", suggest_os_positions(vd, "a").get("b.1"))
    print("validate:", validate(lay) or "ок")


if __name__ == "__main__":
    if "--example" in sys.argv:
        _print_example()
    else:
        print("python layout.py --example — разобранный пример §7.6")
