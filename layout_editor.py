"""Редактор раскладки экранов для вкладки «Мониторы» — только интерфейс (tk.Canvas и поля ввода).

Вся геометрия (привязка, сегменты краёв, размер по диагонали) берётся из layout.py — здесь только рисование,
перетаскивание мышью, клавиатура и формы. Контракт виджета — docs/monitors-spec.md §5.7.

LayoutEditor — схема «как на столе»: экраны всех машин прямоугольниками в реальном масштабе (мм), сетка 10 см,
края с переходами подсвечены (зелёный — напротив, жёлтый — в ближайший угол, красный пунктир — стена),
при наведении на край видно, куда выйдет курсор.
SizeForm — размер выбранного экрана: диагональ (дюймы или см) либо ширина × высота в мм.
"""
import math
import tkinter as tk
from dataclasses import replace

try:  # без layout.py редактор не работает — панель покажет «модуль не найден»
    import layout as L
except Exception:  # noqa: BLE001 — любой сбой импорта означает «модуля нет»
    L = None

SIDES = ("left", "right", "top", "bottom")
GRID_MM = 100.0            # клетка сетки — 10 см
EDGE_HIT = 7               # логические px: насколько близко к краю считается «наведение на край»
KEY_STEP_MM, KEY_FINE_MM = 10.0, 1.0   # сдвиг стрелками / со Shift
SNAP_MIN_PX = 10           # привязка не слабее 10 экранных px при мелком масштабе
DIAG_RANGE = {"in": (5.0, 120.0), "cm": (12.0, 305.0)}
SIZE_RANGE = ((50.0, 3000.0), (30.0, 2000.0))   # §4: size_mm
ASPECT_TOL = 0.08          # §5.1 validate: аспект size_mm и px расходится больше 8 %
SHIFT_MASK = 0x0001


def available():
    return L is not None


def fmt_num(v, digits=1):
    """15.6 -> «15,6», 27.0 -> «27» (десятичная запятая, без лишних нулей)."""
    s = f"{v:.{digits}f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s.replace(".", ",")


def parse_num(text):
    """Число из поля ввода: допускает запятую и пробелы. Не число — None."""
    try:
        v = float(str(text).strip().replace(" ", "").replace(",", "."))
    except ValueError:
        return None
    return v if math.isfinite(v) else None


def blend(fg, bg, k):
    """Смешать два цвета #rrggbb: k = доля fg."""
    a = [int(fg[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(bg[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x * k + y * (1 - k)):02x}" for x, y in zip(a, b))


def diag_label(size_mm):
    try:
        return f"{fmt_num(L.diagonal_in(size_mm))}″"
    except Exception:  # noqa: BLE001 — подпись не должна ронять отрисовку
        return ""


def screen_caption(s):
    """«Ноут A · 15,6″» и вторая строка «1920×1080 · 125 %»."""
    d = diag_label(s.size_mm)
    top = f"{s.label} · {d}" if d and d not in s.label else s.label  # «Монитор 27″» уже с диагональю
    low = f"{s.px[0]}×{s.px[1]}" + (f" · {s.scale} %" if s.scale else "")
    return top, low


def aspect_mismatch(size_mm, px):
    try:
        a, b = size_mm[0] / size_mm[1], px[0] / px[1]
    except (ZeroDivisionError, TypeError, IndexError):
        return False
    return abs(a - b) / b > ASPECT_TOL


class LayoutEditor(tk.Frame):
    """Схема экранов. on_change(layout) — после перетаскивания/сдвига, on_select(screen_id | None) — выбор,
    on_drag(True/False) — начало и конец перетаскивания (панель на это время не прячется)."""

    def __init__(self, parent, *, colors, fonts, scale, on_change, on_select, on_drag=None, height=250):
        if L is None:
            raise RuntimeError("layout.py не найден")
        super().__init__(parent, bg=colors["bg"])
        self.C, self.F, self.s = colors, fonts, scale
        self.on_change, self.on_select, self.on_drag = on_change, on_select, on_drag
        self.machine_color = {}
        self._layout, self._segs, self._preset = None, {}, None
        self._sel = self._current = None
        self._drag = self._hover = None
        self._fit = None   # (k, ox, oy, x0, y0) — мм -> px; при перетаскивании заморожен
        self.cv = tk.Canvas(self, height=int(height * scale), bg=colors["field"], highlightthickness=1,
                            highlightbackground=colors["line"], highlightcolor=colors["accent"], bd=0,
                            takefocus=1, cursor="arrow")
        self.cv.pack(fill="both", expand=True)
        self.cv.bind("<Configure>", lambda e: self._refit_redraw())
        self.cv.bind("<ButtonPress-1>", self._press)
        self.cv.bind("<B1-Motion>", self._motion)
        self.cv.bind("<ButtonRelease-1>", self._release)
        self.cv.bind("<Motion>", self._hover_at)
        self.cv.bind("<Leave>", lambda e: self._set_hover(None))
        for key, dx, dy in (("Left", -1, 0), ("Right", 1, 0), ("Up", 0, -1), ("Down", 0, 1)):
            self.cv.bind(f"<{key}>", lambda e, dx=dx, dy=dy: self._key_move(e, dx, dy))
        for key in ("<space>", "<Return>"):
            self.cv.bind(key, lambda e: self._key_cycle())

    # ── Публичное (§5.7) ──
    def set_layout(self, layout, preview=None):
        """preview — {"segments": [Segment, …]} для подсветки краёв; None — сегменты считает layout.edge_segments."""
        self._layout = layout
        self._preset = preview.get("segments") if isinstance(preview, dict) else None
        ids = [s.id for s in layout.screens] if layout else []
        if self._sel not in ids:
            self._sel = None
        if self._current not in ids:
            self._current = None
        palette = (self.C["accent"], self.C["violet"], self.C["mint"])
        self.machine_color = {m.id: palette[i % len(palette)] for i, m in enumerate(layout.machines if layout else ())}
        self._hover = None
        self._recalc()
        self._refit_redraw()

    def get_layout(self):
        return self._layout

    def select(self, screen_id):
        self._sel = screen_id if self._screen(screen_id) else None
        self._redraw()

    def set_current(self, screen_id):
        """Экран, на котором сейчас курсор (метка «● курсор здесь»)."""
        if screen_id != self._current:
            self._current = screen_id
            self._redraw()

    def show_exit(self, screen_id, side, along_mm):
        """Показать, куда выйдет курсор с края side экрана screen_id в точке along_mm (как при наведении)."""
        self._set_hover((screen_id, side, along_mm) if self._screen(screen_id) else None)

    # ── Модель ──
    def _screen(self, sid):
        if not self._layout or sid is None:
            return None
        return next((s for s in self._layout.screens if s.id == sid), None)

    def _machine_of(self, sid):
        s = self._screen(sid)
        return s.machine if s else None

    def _recalc(self):
        """Сегменты краёв: из preview или layout.edge_segments. Ошибка геометрии не ломает схему."""
        self._segs = {}
        if not self._layout:
            return
        if self._preset is not None:
            for seg in self._preset:
                self._segs.setdefault((seg.src, seg.side), []).append(seg)
            return
        for s in self._layout.screens:
            for side in SIDES:
                try:
                    self._segs[(s.id, side)] = list(L.edge_segments(self._layout, s.id, side))
                except Exception:  # noqa: BLE001 — недоделанная раскладка: просто без подсветки
                    self._segs[(s.id, side)] = []

    def _move_screen(self, sid, pos):
        screens = tuple(replace(s, pos_mm=(round(pos[0], 1), round(pos[1], 1))) if s.id == sid else s
                        for s in self._layout.screens)
        self._layout = replace(self._layout, screens=screens)
        self._preset = None  # подсветка из preview устарела — дальше считаем сами
        self._recalc()

    # ── Масштаб «вписать всё» ──
    def _refit_redraw(self):
        if self._drag is None:
            self._fit = self._compute_fit()
        self._redraw()

    def _compute_fit(self):
        w, h = max(self.cv.winfo_width(), 50), max(self.cv.winfo_height(), 50)
        if not self._layout or not self._layout.screens:
            return None
        xs = [v for s in self._layout.screens for v in (s.pos_mm[0], s.pos_mm[0] + s.size_mm[0])]
        ys = [v for s in self._layout.screens for v in (s.pos_mm[1], s.pos_mm[1] + s.size_mm[1])]
        x0, y0, bw, bh = min(xs), min(ys), max(max(xs) - min(xs), 1), max(max(ys) - min(ys), 1)
        pad, foot = 22 * self.s, 16 * self.s
        k = min((w - 2 * pad) / bw, (h - 2 * pad - foot) / bh)
        k = max(k, 0.02)
        ox = (w - bw * k) / 2
        oy = (h - foot - bh * k) / 2
        return k, ox, oy, x0, y0

    def _to_px(self, x, y):
        k, ox, oy, x0, y0 = self._fit
        return ox + (x - x0) * k, oy + (y - y0) * k

    def _rect_px(self, s):
        x1, y1 = self._to_px(*s.pos_mm)
        return x1, y1, x1 + s.size_mm[0] * self._fit[0], y1 + s.size_mm[1] * self._fit[0]

    # ── Отрисовка ──
    def _redraw(self):
        cv, C = self.cv, self.C
        cv.delete("all")
        w, h = cv.winfo_width(), cv.winfo_height()
        if not self._layout or not self._layout.screens or self._fit is None:
            cv.create_text(w / 2, h / 2, text="Экранов нет — нажми «Определить экраны»", fill=C["muted"],
                           font=self.F["small"])
            return
        self._draw_grid(w, h)
        order = sorted(self._layout.screens, key=lambda s: s.id == self._sel)  # выбранный — сверху
        for s in order:
            self._draw_screen(s)
        for s in order:
            self._draw_edges(s)
        self._draw_hover()

    def _draw_grid(self, w, h):
        cv, C = self.cv, self.C
        k, ox, oy, x0, y0 = self._fit
        step = GRID_MM * k
        if step >= 6:
            start = ox - math.ceil(ox / step) * step
            x = start
            while x <= w:
                cv.create_line(x, 0, x, h, fill=C["tile_b"])
                x += step
            y = oy - math.ceil(oy / step) * step
            while y <= h:
                cv.create_line(0, y, w, y, fill=C["tile_b"])
                y += step
        cv.create_text(8 * self.s, h - 6 * self.s, anchor="sw", text="клетка = 10 см", fill=C["muted"],
                       font=self.F["tiny"])

    def _draw_screen(self, s):
        cv, C = self.cv, self.C
        x1, y1, x2, y2 = self._rect_px(s)
        col = self.machine_color.get(s.machine, C["accent"])
        sel = s.id == self._sel
        cv.create_rectangle(x1, y1, x2, y2, fill=blend(col, C["field"], 0.16), outline=C["ink"] if sel else col,
                            width=3 if sel else 2, tags=("screen", s.id))
        top, low = screen_caption(s)
        pw = x2 - x1
        cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
        lines = [(top, self.F["small"], C["ink"])]
        if pw > 90 * self.s and y2 - y1 > 40 * self.s:
            lines.append((low, self.F["tiny"], C["muted"]))
        if s.size_source == "default":
            lines.append(("размер? — проверьте", self.F["tiny"], C["accent"]))
        if s.id == self._current:
            lines.append(("● курсор здесь", self.F["tiny"], C["mint"]))
        lh = 14 * self.s
        y = cy - lh * (len(lines) - 1) / 2
        for text, font, fg in lines:
            cv.create_text(cx, y, text=text, fill=fg, font=font, width=max(pw - 8, 20), tags=("screen", s.id))
            y += lh

    def _edge_line(self, s, side, a, b, inset):
        """Отрезок края side экрана s от a до b (мм вдоль края), сдвинутый внутрь на inset px."""
        x1, y1, x2, y2 = self._rect_px(s)
        k = self._fit[0]
        if side in ("left", "right"):
            x = x2 - inset if side == "right" else x1 + inset
            return x, y1 + (a - s.pos_mm[1]) * k, x, y1 + (b - s.pos_mm[1]) * k
        y = y2 - inset if side == "bottom" else y1 + inset
        return x1 + (a - s.pos_mm[0]) * k, y, x1 + (b - s.pos_mm[0]) * k, y

    def _draw_edges(self, s):
        cv, C = self.cv, self.C
        for side in SIDES:
            segs = self._segs.get((s.id, side)) or []
            if not segs:  # стена: курсор здесь не выйдет
                a, b = (s.pos_mm[1], s.pos_mm[1] + s.size_mm[1]) if side in ("left", "right") else \
                       (s.pos_mm[0], s.pos_mm[0] + s.size_mm[0])
                cv.create_line(*self._edge_line(s, side, a, b, 4 * self.s), fill=blend(C["red"], C["field"], 0.55),
                               width=1, dash=(3, 4))
                continue
            for seg in segs:
                col = C["mint"] if seg.kind == "linear" else C["accent"]
                cv.create_line(*self._edge_line(s, side, seg.src_from_mm, seg.src_to_mm, 4 * self.s), fill=col,
                               width=max(2, int(3 * self.s)), capstyle="butt")

    # ── Куда выйдет курсор ──
    def _exit_target(self, sid, side, along):
        """(сегмент, точка выхода на dst в мм) или (None, None) — стена."""
        for seg in self._segs.get((sid, side)) or []:
            if seg.src_from_mm - 0.05 <= along <= seg.src_to_mm + 0.05:
                d = self._screen(seg.dst)
                if not d:
                    return None, None
                t = along if seg.kind == "linear" else seg.dst_from_mm
                l, tp = d.pos_mm
                r, b = l + d.size_mm[0], tp + d.size_mm[1]
                pt = {"right": (l, t), "left": (r, t), "bottom": (t, tp), "top": (t, b)}[side]
                return seg, pt
        return None, None

    def _draw_hover(self):
        if not self._hover:
            return
        sid, side, along = self._hover
        s = self._screen(sid)
        if not s:
            return
        cv, C = self.cv, self.C
        src = ((s.pos_mm[0] + s.size_mm[0], along) if side == "right" else (s.pos_mm[0], along) if side == "left"
               else (along, s.pos_mm[1] + s.size_mm[1]) if side == "bottom" else (along, s.pos_mm[1]))
        sx, sy = self._to_px(*src)
        seg, dst = self._exit_target(sid, side, along)
        r = 4 * self.s
        if seg is None:
            col, text = C["red"], "стена — курсор здесь не выйдет"
            cv.create_oval(sx - r, sy - r, sx + r, sy + r, outline=col, width=2)
        else:
            d = self._screen(seg.dst)
            col = C["mint"] if seg.kind == "linear" else C["accent"]
            how = "напротив" if seg.kind == "linear" else "в ближайший угол"
            via = " (через Deskflow)" if d.machine != s.machine else ""
            text = f"→ {d.label}: {how}{via}"
            dx, dy = self._to_px(*dst)
            cv.create_line(sx, sy, dx, dy, fill=col, width=2, arrow="last", arrowshape=(8, 10, 4))
            cv.create_oval(sx - r, sy - r, sx + r, sy + r, fill=col, outline="")
            cv.create_oval(dx - r, dy - r, dx + r, dy + r, outline=col, width=2)
        self._tip(sx, sy, text, col)

    def _tip(self, x, y, text, col):
        cv = self.cv
        w = cv.winfo_width()
        t = cv.create_text(0, 0, text=text, fill=col, font=self.F["small"], anchor="nw")
        bx1, by1, bx2, by2 = cv.bbox(t)
        tw, th = bx2 - bx1, by2 - by1
        px = min(max(4, x + 10 * self.s), w - tw - 8)
        py = y - th - 12 * self.s if y - th - 12 * self.s > 2 else y + 12 * self.s
        cv.coords(t, px, py)
        bg = cv.create_rectangle(px - 4, py - 2, px + tw + 4, py + th + 2, fill=self.C["head"], outline=col)
        cv.tag_raise(t, bg)

    def _set_hover(self, hv):
        if hv != self._hover:
            self._hover = hv
            self._redraw()

    def _hover_at(self, e):
        if self._drag is not None or self._fit is None or not self._layout:
            return
        self._set_hover(self._edge_under(e.x, e.y))

    def _edge_under(self, x, y):
        hit = EDGE_HIT * self.s
        k = self._fit[0]
        order = sorted(self._layout.screens, key=lambda s: s.id != self._sel)  # выбранный проверяем первым
        for s in order:
            x1, y1, x2, y2 = self._rect_px(s)
            if not (x1 - hit <= x <= x2 + hit and y1 - hit <= y <= y2 + hit):
                continue
            cands = []
            if y1 <= y <= y2:
                cands += [(abs(x - x2), "right"), (abs(x - x1), "left")]
            if x1 <= x <= x2:
                cands += [(abs(y - y2), "bottom"), (abs(y - y1), "top")]
            cands = [c for c in cands if c[0] <= hit]
            if not cands:
                continue
            side = min(cands)[1]
            if side in ("left", "right"):
                along = s.pos_mm[1] + (y - y1) / k
            else:
                along = s.pos_mm[0] + (x - x1) / k
            return s.id, side, round(along, 1)
        return None

    # ── Мышь: выбор и перетаскивание ──
    def _hit(self, x, y):
        order = sorted(self._layout.screens, key=lambda s: s.id != self._sel)
        for s in order:
            x1, y1, x2, y2 = self._rect_px(s)
            if x1 <= x <= x2 and y1 <= y <= y2:
                return s
        return None

    def _press(self, e):
        self.cv.focus_set()
        if not self._layout or self._fit is None:
            return
        s = self._hit(e.x, e.y)
        sid = s.id if s else None
        if sid != self._sel:
            self._sel = sid
            self._emit_select(sid)
        if s:
            self._drag = {"id": s.id, "x": e.x, "y": e.y, "pos": s.pos_mm, "moved": False}
            self.cv.configure(cursor="fleur")
            if self.on_drag:
                self.on_drag(True)
        self._hover = None
        self._redraw()

    def _motion(self, e):
        d = self._drag
        if not d:
            return
        k = self._fit[0]
        pos = (d["pos"][0] + (e.x - d["x"]) / k, d["pos"][1] + (e.y - d["y"]) / k)
        if not e.state & SHIFT_MASK:  # Shift — без привязки
            try:
                pos = L.snap(self._layout, d["id"], pos, tol_mm=max(15.0, SNAP_MIN_PX / k))
            except Exception:  # noqa: BLE001 — привязка не сработала: двигаем как есть
                pass
        d["moved"] = True
        self._move_screen(d["id"], pos)
        self._redraw()

    def _release(self, e):
        d, self._drag = self._drag, None
        self.cv.configure(cursor="arrow")
        if d is None:
            return
        if self.on_drag:
            self.on_drag(False)
        self._refit_redraw()
        if d["moved"]:
            self.on_change(self._layout)

    def _emit_select(self, sid):
        if self.on_select:
            self.on_select(sid)

    # ── Клавиатура: пробел/Enter — следующий экран, стрелки — сдвиг на 1 см (Shift — на 1 мм) ──
    def _key_cycle(self):
        if not self._layout or not self._layout.screens:
            return "break"
        ids = [s.id for s in self._layout.screens]
        nxt = ids[(ids.index(self._sel) + 1) % len(ids)] if self._sel in ids else ids[0]
        self._sel = nxt
        self._emit_select(nxt)
        self._redraw()
        return "break"

    def _key_move(self, e, dx, dy):
        s = self._screen(self._sel)
        if not s:
            return self._key_cycle()
        step = KEY_FINE_MM if e.state & SHIFT_MASK else KEY_STEP_MM
        self._move_screen(s.id, (s.pos_mm[0] + dx * step, s.pos_mm[1] + dy * step))
        self._refit_redraw()
        self.on_change(self._layout)
        return "break"


class SizeForm(tk.Frame):
    """Размер выбранного экрана: диагональ [__] [дюймы|см] или ширина × высота [мм], «из EDID».

    make_button(parent, text, cmd) и make_entry(parent, var, width) — фабрики в стиле панели.
    on_apply(screen_id, size_mm, source) — source: "user" или "edid".
    """

    def __init__(self, parent, *, colors, fonts, make_button, make_entry, on_apply):
        if L is None:
            raise RuntimeError("layout.py не найден")
        super().__init__(parent, bg=parent["bg"])
        self.C, self.F, self.on_apply = colors, fonts, on_apply
        self.screen, self.edid_mm, self.unit, self.mode = None, None, "in", "diag"
        self.v_diag, self.v_w, self.v_h = tk.StringVar(self), tk.StringVar(self), tk.StringVar(self)
        bg = self["bg"]
        row = tk.Frame(self, bg=bg)
        row.pack(fill="x")
        tk.Label(row, text="Диагональ", bg=bg, fg=colors["muted"], font=fonts["small"]).pack(side="left")
        self.e_diag = make_entry(row, self.v_diag, 6)
        self.e_diag.pack(side="left", padx=(6, 4))
        self.unit_btns = {}
        for unit, text in (("in", "дюймы"), ("cm", "см")):
            b = make_button(row, text, lambda u=unit: self.set_unit(u))
            b.pack(side="left", padx=(0, 2))
            self.unit_btns[unit] = b
        tk.Label(row, text="  или", bg=bg, fg=colors["muted"], font=fonts["small"]).pack(side="left")
        self.e_w = make_entry(row, self.v_w, 6)
        self.e_w.pack(side="left", padx=(6, 2))
        tk.Label(row, text="×", bg=bg, fg=colors["muted"], font=fonts["small"]).pack(side="left")
        self.e_h = make_entry(row, self.v_h, 6)
        self.e_h.pack(side="left", padx=(2, 2))
        tk.Label(row, text="мм", bg=bg, fg=colors["muted"], font=fonts["small"]).pack(side="left")
        self.b_apply = make_button(row, "Применить", self.apply)
        self.b_apply.pack(side="right")
        self.b_edid = make_button(row, "из EDID", self.use_edid)
        info = tk.Frame(self, bg=bg)
        info.pack(fill="x", pady=(3, 0))
        self.l_src = tk.Label(info, text="", bg=bg, fg=colors["muted"], font=fonts["tiny"], anchor="w")
        self.l_src.pack(side="left")
        self.l_err = tk.Label(info, text="", bg=bg, fg=colors["red"], font=fonts["tiny"], anchor="e")
        self.l_err.pack(side="right")
        self.e_diag.bind("<Key>", lambda e: self._edited("diag"))
        for ent in (self.e_w, self.e_h):
            ent.bind("<Key>", lambda e: self._edited("mm"))
        for ent in (self.e_diag, self.e_w, self.e_h):
            ent.bind("<Return>", lambda e: self.apply())
        self._paint_unit()

    def _edited(self, mode):
        self.mode = mode
        self.l_err.configure(text="")

    def set_unit(self, unit):
        """Переключить единицы диагонали — число пересчитывается, размер экрана не меняется."""
        if unit == self.unit:
            return
        d = parse_num(self.v_diag.get())
        self.unit = unit
        if d is not None:
            self.v_diag.set(fmt_num(d * 2.54 if unit == "cm" else d / 2.54))
        self._paint_unit()

    def _paint_unit(self):
        for unit, b in self.unit_btns.items():
            on = unit == self.unit
            b.configure(fg=self.C["accent"] if on else self.C["muted"])

    def set_screen(self, screen, edid_mm=None):
        """screen — layout.Screen или None; edid_mm — размер из EDID этой ОС (если экран локальный и EDID есть)."""
        self.screen, self.edid_mm, self.mode = screen, edid_mm, "diag"
        self.l_err.configure(text="")
        state = "normal" if screen else "disabled"
        for ent in (self.e_diag, self.e_w, self.e_h):
            ent.configure(state=state)
        if not screen:
            self.v_diag.set("")
            self.v_w.set("")
            self.v_h.set("")
            self.l_src.configure(text="Выбери экран на схеме (клик или Tab → пробел)", fg=self.C["muted"])
            self.b_edid.pack_forget()
            return
        d = L.diagonal_in(screen.size_mm)
        self.v_diag.set(fmt_num(d if self.unit == "in" else d * 2.54))
        self.v_w.set(fmt_num(screen.size_mm[0]))
        self.v_h.set(fmt_num(screen.size_mm[1]))
        src = {"edid": ("размер из EDID монитора", self.C["mint"]),
               "user": ("размер введён вручную", self.C["muted"]),
               "default": ("размер по умолчанию — проверьте и введите диагональ", self.C["accent"])}
        text, fg = src.get(screen.size_source, ("источник размера неизвестен", self.C["muted"]))
        if aspect_mismatch(screen.size_mm, screen.px):
            text, fg = f"{text} · соотношение сторон не как у {screen.px[0]}×{screen.px[1]}", self.C["accent"]
        self.l_src.configure(text=text, fg=fg)
        if edid_mm and screen.size_source != "edid":
            self.b_edid.pack(side="right", padx=(0, 4), before=self.b_apply)
        else:
            self.b_edid.pack_forget()

    def use_edid(self):
        if self.screen and self.edid_mm:
            self.on_apply(self.screen.id, (float(self.edid_mm[0]), float(self.edid_mm[1])), "edid")

    def apply(self):
        """Проверить ввод и отдать размер наружу. Ошибка — красный текст рядом, размер не меняется."""
        s = self.screen
        if not s:
            return
        if self.mode == "diag":
            d = parse_num(self.v_diag.get())
            lo, hi = DIAG_RANGE[self.unit]
            unit_ru = "″" if self.unit == "in" else " см"
            if d is None or not lo <= d <= hi:
                return self.l_err.configure(text=f"Диагональ — число от {fmt_num(lo)} до {fmt_num(hi)}{unit_ru}")
            try:
                size = L.size_from_diagonal(d, self.unit, tuple(s.px))
            except Exception:  # noqa: BLE001
                return self.l_err.configure(text="Не удалось посчитать размер по диагонали")
        else:
            w, h = parse_num(self.v_w.get()), parse_num(self.v_h.get())
            (wl, wh), (hl, hh) = SIZE_RANGE
            if w is None or h is None or not (wl <= w <= wh and hl <= h <= hh):
                return self.l_err.configure(text=f"Ширина {wl:.0f}–{wh:.0f} мм, высота {hl:.0f}–{hh:.0f} мм")
            size = (round(w, 1), round(h, 1))
        (wl, wh), (hl, hh) = SIZE_RANGE
        if not (wl <= size[0] <= wh and hl <= size[1] <= hh):
            return self.l_err.configure(text="Получается слишком маленький или большой экран — проверьте число")
        self.on_apply(s.id, size, "user")
