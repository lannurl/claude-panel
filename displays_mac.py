"""Экраны macOS для вкладки «Мониторы» (docs/monitors-spec.md, §9.1, §11.2) — этап 5, частично.

Сделано (ctypes → CoreGraphics, без pyobjc): list_displays() и set_display_positions() (dry_run по умолчанию
в тестах). Координаты — пункты (points) глобальной системы CGDisplayBounds, y вниз; px — физические пиксели
текущего режима; size_mm — CGDisplayScreenSize (EDID).
Не сделано: EdgeHelper (нужен listen-only CGEventTap через pyobjc-framework-Quartz — этап 5, после проверки
на реальном Mac). Здесь EdgeHelper = None, и панель показывает «помощник недоступен».

ВНИМАНИЕ: модуль не проверялся на реальном Mac. Проверено только на синтетических образцах
(функция display_from_raw) и что импорт не падает вне macOS.
"""
import ctypes
import ctypes.util
import sys

import layout

DEFAULT_VIRTUAL_MATCH = ("SudoVDA", "Virtual Display", "BetterDisplay")
EdgeHelper = None  # этап 5


def log(msg):
    try:
        if sys.stderr:
            print(f"[panel/monitors] mac: {msg}", file=sys.stderr, flush=True)
    except Exception:
        pass


class CGPoint(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]


class CGSize(ctypes.Structure):
    _fields_ = [("width", ctypes.c_double), ("height", ctypes.c_double)]


class CGRect(ctypes.Structure):
    _fields_ = [("origin", CGPoint), ("size", CGSize)]


def _load_cg():
    if sys.platform != "darwin":
        return None
    path = ctypes.util.find_library("CoreGraphics") or \
        "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics"
    try:
        cg = ctypes.CDLL(path)
    except OSError as e:
        log(f"CoreGraphics не загружен: {type(e).__name__}")
        return None
    u32, ptr = ctypes.c_uint32, ctypes.c_void_p
    cg.CGGetActiveDisplayList.argtypes = [u32, ctypes.POINTER(u32), ctypes.POINTER(u32)]
    cg.CGGetActiveDisplayList.restype = ctypes.c_int32
    cg.CGDisplayBounds.argtypes = [u32]
    cg.CGDisplayBounds.restype = CGRect
    cg.CGDisplayScreenSize.argtypes = [u32]
    cg.CGDisplayScreenSize.restype = CGSize
    for fn in ("CGDisplayIsMain", "CGDisplayIsBuiltin", "CGDisplayVendorNumber", "CGDisplayModelNumber",
               "CGDisplaySerialNumber"):
        getattr(cg, fn).argtypes = [u32]
        getattr(cg, fn).restype = u32
    cg.CGDisplayRotation.argtypes = [u32]
    cg.CGDisplayRotation.restype = ctypes.c_double
    cg.CGDisplayCopyDisplayMode.argtypes = [u32]
    cg.CGDisplayCopyDisplayMode.restype = ptr
    cg.CGDisplayModeGetPixelWidth.argtypes = [ptr]
    cg.CGDisplayModeGetPixelWidth.restype = ctypes.c_size_t
    cg.CGDisplayModeGetPixelHeight.argtypes = [ptr]
    cg.CGDisplayModeGetPixelHeight.restype = ctypes.c_size_t
    cg.CGDisplayModeRelease.argtypes = [ptr]
    cg.CGDisplayModeRelease.restype = None
    cg.CGBeginDisplayConfiguration.argtypes = [ctypes.POINTER(ptr)]
    cg.CGBeginDisplayConfiguration.restype = ctypes.c_int32
    cg.CGConfigureDisplayOrigin.argtypes = [ptr, u32, ctypes.c_int32, ctypes.c_int32]
    cg.CGConfigureDisplayOrigin.restype = ctypes.c_int32
    cg.CGCompleteDisplayConfiguration.argtypes = [ptr, u32]
    cg.CGCompleteDisplayConfiguration.restype = ctypes.c_int32
    cg.CGCancelDisplayConfiguration.argtypes = [ptr]
    cg.CGCancelDisplayConfiguration.restype = ctypes.c_int32
    return cg


_cg = _load_cg()
kCGConfigurePermanently = 2


def display_from_raw(raw, vmatch=DEFAULT_VIRTUAL_MATCH):
    """Сырые значения CoreGraphics → запись контракта list_displays(). Чистая функция (тестируется вне Mac).

    raw: {"id", "bounds": (x, y, w, h) в пунктах, "pixels": (w, h) | None, "size_mm": (w, h), "main", "builtin",
          "vendor", "model", "serial", "rotation", "name" (необязательно)}.
    """
    x, y, w, h = (int(round(v)) for v in raw["bounds"])
    pw, ph = raw.get("pixels") or (w, h)
    pw, ph = int(pw), int(ph)
    if int(round(raw.get("rotation") or 0)) % 180 == 90:
        pw, ph = ph, pw
    mm = raw.get("size_mm")
    size = None
    if mm and mm[0] and mm[1]:
        mw, mh = float(mm[0]), float(mm[1])
        if 50 <= mw <= 3000 and 30 <= mh <= 2000 and layout._aspect_ok((mw, mh), (pw, ph)):
            size = [round(mw, 1), round(mh, 1)]
    name = raw.get("name") or ("Встроенный дисплей" if raw.get("builtin") else f"Экран {raw['id']}")
    virtual = any(v.lower() in name.lower() for v in vmatch if v)
    return {
        "os_id": f"mac:{raw.get('vendor', 0):04X}:{raw.get('model', 0):04X}:{raw.get('serial', 0):08X}",
        "gdi_name": str(raw["id"]),   # CGDirectDisplayID — только в рантайме, не как ключ
        "label": name,
        "px": [pw, ph], "rect_px": [x, y, w, h],
        "primary": bool(raw.get("main")),
        "scale": int(round(pw / w * 100)) if w else 100,
        "size_mm": size, "virtual": virtual,
    }


def _raw_list():
    ids = (ctypes.c_uint32 * 16)()
    n = ctypes.c_uint32()
    if _cg.CGGetActiveDisplayList(16, ids, ctypes.byref(n)) != 0:
        return []
    out = []
    for i in range(n.value):
        did = ids[i]
        b = _cg.CGDisplayBounds(did)
        mode = _cg.CGDisplayCopyDisplayMode(did)
        pixels = None
        if mode:
            pixels = (_cg.CGDisplayModeGetPixelWidth(mode), _cg.CGDisplayModeGetPixelHeight(mode))
            _cg.CGDisplayModeRelease(mode)
        sz = _cg.CGDisplayScreenSize(did)
        out.append({"id": did, "bounds": (b.origin.x, b.origin.y, b.size.width, b.size.height),
                    "pixels": pixels, "size_mm": (sz.width, sz.height), "main": _cg.CGDisplayIsMain(did),
                    "builtin": _cg.CGDisplayIsBuiltin(did), "vendor": _cg.CGDisplayVendorNumber(did),
                    "model": _cg.CGDisplayModelNumber(did), "serial": _cg.CGDisplaySerialNumber(did),
                    "rotation": _cg.CGDisplayRotation(did)})
    return out


def list_displays(virtual_match=None):
    if _cg is None:
        return []
    try:
        vm = tuple(virtual_match or DEFAULT_VIRTUAL_MATCH)
        return [display_from_raw(r, vm) for r in _raw_list()]
    except Exception as e:
        log(f"list_displays: {type(e).__name__}")
        return []


def set_display_positions(pos, dry_run=False, displays=None):
    """Как в displays_win: {os_id: (x, y)} в пунктах. dry_run=True — только проверка и план."""
    try:
        return _set_positions(pos, dry_run, displays)
    except Exception as e:
        log(f"set_display_positions: {type(e).__name__}")
        return {"ok": False, "code": "os_error", "err": "Не удалось изменить раскладку экранов", "previous": {}}


def _set_positions(pos, dry_run, displays):
    if not dry_run and _cg is None:
        return {"ok": False, "code": "unsupported_os", "err": "CoreGraphics недоступен", "previous": {}}
    if not isinstance(pos, dict) or not pos:
        return {"ok": False, "code": "bad_layout", "err": "Нет позиций для применения", "previous": {}}
    ds = displays if displays is not None else list_displays()
    by_id = {d["os_id"]: d for d in ds}
    if any(k not in by_id for k in pos):
        return {"ok": False, "code": "bad_layout", "err": "Экран из раскладки не подключён", "previous": {}}
    prim = next((d for d in ds if d["primary"]), None)
    if prim is None:
        return {"ok": False, "code": "os_error", "err": "Основной экран не найден", "previous": {}}
    newpos = {k: (int(v[0]), int(v[1])) for k, v in pos.items()}
    sx, sy = newpos.get(prim["os_id"], (0, 0))
    rects = {}
    for d in ds:
        x, y = newpos.get(d["os_id"], tuple(d["rect_px"][:2]))
        if d["os_id"] in newpos:
            x, y = x - sx, y - sy
        rects[d["os_id"]] = (x, y, d["rect_px"][2], d["rect_px"][3])
    previous = {d["os_id"]: tuple(d["rect_px"][:2]) for d in ds}
    err = layout.check_os_positions(rects, prim["os_id"])
    if err:
        return {"ok": False, "code": "bad_layout", "err": f"Раскладка не применена: {err}", "previous": previous}
    plan = [{"os_id": d["os_id"], "gdi_name": d["gdi_name"], "x": rects[d["os_id"]][0], "y": rects[d["os_id"]][1]}
            for d in ds if not d["primary"] and tuple(rects[d["os_id"]][:2]) != tuple(d["rect_px"][:2])]
    if dry_run or not plan:
        return {"ok": True, "code": None, "err": None, "previous": previous, "plan": plan, "dry_run": bool(dry_run)}
    cfg = ctypes.c_void_p()
    if _cg.CGBeginDisplayConfiguration(ctypes.byref(cfg)) != 0:
        return {"ok": False, "code": "os_error", "err": "macOS не дала изменить раскладку", "previous": previous}
    for step in plan:
        if _cg.CGConfigureDisplayOrigin(cfg, int(step["gdi_name"]), step["x"], step["y"]) != 0:
            _cg.CGCancelDisplayConfiguration(cfg)
            return {"ok": False, "code": "os_error", "err": "macOS отклонила раскладку", "previous": previous}
    if _cg.CGCompleteDisplayConfiguration(cfg, kCGConfigurePermanently) != 0:
        return {"ok": False, "code": "os_error", "err": "macOS не применила раскладку", "previous": previous}
    return {"ok": True, "code": None, "err": None, "previous": previous, "plan": plan, "dry_run": False}


def ease_cursor_setting():
    return None  # на macOS такой настройки нет
