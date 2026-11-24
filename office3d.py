"""3D-офис агентов для вкладки «Сессии».

Картинки — готовые кадры настоящей 3D-сцены (tools/office3d, three.js), лежат в assets/office.
Здесь только «жизнь»: кто за каким столом, куда идёт, что делает. Рисование — create_image на холсте tkinter,
так что процессор почти не тратится.

Офис больше окна: мышью двигают, колесом отдаляют/приближают. Мир в метрах: x вправо, z к зрителю.
Ходят только по дорожкам (meta.room.lanes, все вдоль осей) — через двери кабинетов, не сквозь стекло.

Демо без панели: python office3d.py
"""
from __future__ import annotations

import heapq
import json
import math
import random
import time
from pathlib import Path

from PIL import Image, ImageEnhance

ASSETS = Path(__file__).resolve().parent / "assets" / "office"

# Команда из ~/.claude/agents: у каждого свой образ и свой стол
ROSTER = ["architect", "backend-dev", "frontend-dev", "code-reviewer", "database-agent", "debugger", "devops-agent",
          "docs-agent", "orchestrator", "qa-tester", "researcher", "scaffolder", "security-reviewer", "token-auditor"]
TEAM_DESK = {"frontend-dev": "A1", "backend-dev": "A2", "database-agent": "A3", "devops-agent": "A4",
             "code-reviewer": "B1", "qa-tester": "B2", "security-reviewer": "B3", "debugger": "B4",
             "architect": "O1", "orchestrator": "O2", "researcher": "O3", "docs-agent": "O4", "scaffolder": "O5", "token-auditor": "O6"}
MAIN = "igris"
INTERN = "intern"        # встроенные агенты (Explore, Plan, general-purpose…) и все без своего образа
WALK_SPEED = 1.4         # м/с
STEP = 0.21              # метров на кадр шага
DAY_HOURS = range(7, 18)
ZOOMS = (1.0, 0.82, 0.68, 0.56, 0.46)

IDLE_WEIGHTS = {"relax": 3.0, "coffee": 2.0, "game": 2.0, "window": 1.6, "meeting": 1.8, "chat": 1.5, "phone": 1.2, "bar": 1.0, "stretch": 0.7}
POSE_FPS = {"type": 5, "relax": 0.8, "wave": 4, "drink": 1.2, "game": 3, "chat": 2, "stretch": 1.2, "phone": 1, "machine": 1.5,
            "window": 0.5, "stand": 0.5, "meetS": 1.3, "meetN": 1.3}
SEATED = {"type", "relax", "meetS", "meetN", "game"}


def _k(p):
    return (round(p[0], 3), round(p[1], 3))


class Graph:
    """Сеть дорожек: узлы — концы, пересечения и точки подхода к местам; рёбра — куски дорожек."""

    def __init__(self, lanes, points):
        segs = [(_k(a), _k(b)) for a, b in lanes]
        nodes = set()
        for a, b in segs:
            nodes.update((a, b))
        for i, (a, b) in enumerate(segs):
            for c, d in segs[i + 1:]:
                h1, h2 = a[1] == b[1], c[1] == d[1]
                if h1 == h2:
                    continue
                hz, vx = ((a, b), (c, d)) if h1 else ((c, d), (a, b))
                x, z = vx[0][0], hz[0][1]
                if min(hz[0][0], hz[1][0]) - 1e-6 <= x <= max(hz[0][0], hz[1][0]) + 1e-6 and \
                        min(vx[0][1], vx[1][1]) - 1e-6 <= z <= max(vx[0][1], vx[1][1]) + 1e-6:
                    nodes.add(_k((x, z)))
        self.segs = segs
        for p in points:
            q = self.snap(p)
            if q:
                nodes.add(q)
        self.adj = {n: [] for n in nodes}
        for a, b in segs:
            on = sorted((n for n in nodes if self.on_seg(n, a, b)), key=lambda n: (n[0] - a[0]) ** 2 + (n[1] - a[1]) ** 2)
            for u, v in zip(on, on[1:]):
                d = abs(u[0] - v[0]) + abs(u[1] - v[1])
                self.adj[u].append((v, d))
                self.adj[v].append((u, d))

    @staticmethod
    def on_seg(p, a, b):
        if a[1] == b[1]:
            return abs(p[1] - a[1]) < 1e-3 and min(a[0], b[0]) - 1e-3 <= p[0] <= max(a[0], b[0]) + 1e-3
        return abs(p[0] - a[0]) < 1e-3 and min(a[1], b[1]) - 1e-3 <= p[1] <= max(a[1], b[1]) + 1e-3

    def snap(self, p):
        """Ближайшая точка на дорожках (для точек подхода — они на дорожке и так)."""
        best, bd = None, 1e9
        for a, b in self.segs:
            if a[1] == b[1]:
                q = (min(max(p[0], min(a[0], b[0])), max(a[0], b[0])), a[1])
            else:
                q = (a[0], min(max(p[1], min(a[1], b[1])), max(a[1], b[1])))
            d = abs(q[0] - p[0]) + abs(q[1] - p[1])
            if d < bd:
                best, bd = _k(q), d
        return best

    def path(self, a, b):
        a, b = self.snap(a), self.snap(b)
        if a == b:
            return [a]
        dist, prev, pq = {a: 0}, {}, [(0, a)]
        while pq:
            d, u = heapq.heappop(pq)
            if u == b:
                break
            if d > dist.get(u, 1e9):
                continue
            for v, w in self.adj.get(u, ()):
                nd = d + w
                if nd < dist.get(v, 1e9):
                    dist[v], prev[v] = nd, u
                    heapq.heappush(pq, (nd, v))
        if b not in dist:
            return [a, b]
        out = [b]
        while out[-1] != a:
            out.append(prev[out[-1]])
        return out[::-1]


class Assets:
    """Кадры в исходном размере; уменьшенные копии под масштаб холста и зум — лениво, с кэшем."""

    def __init__(self):
        self.meta = json.loads((ASSETS / "office.json").read_text(encoding="utf-8"))
        self.raw_back = {}
        self.props = self._atlas(ASSETS / "props.png", self.meta["props"])
        self.agents = {}
        self.cache = {}             # (зум, вид, имя, режим) → (PhotoImage, якорь)
        self.backs = {}             # (зум, режим) → (PIL, PhotoImage)

    @staticmethod
    def _atlas(path, frames):
        atlas = Image.open(path).convert("RGBA")
        return {n: (atlas.crop((f["rect"][0], f["rect"][1], f["rect"][0] + f["rect"][2], f["rect"][1] + f["rect"][3])), f["anchor"])
                for n, f in frames.items()}

    def agent(self, outfit):
        src = outfit if outfit in self.meta["agents"] else INTERN
        if src not in self.agents:
            self.agents[src] = self._atlas(ASSETS / f"agent_{src}.png", self.meta["agents"][src]["frames"])
        return self.agents[src]

    def back(self, scale, mode):
        key = (round(scale, 4), mode)
        if key not in self.backs:
            from PIL import ImageTk
            if mode not in self.raw_back:
                self.raw_back[mode] = Image.open(ASSETS / f"backdrop_{mode}.png").convert("RGB")
            v = self.meta["view"]
            k = scale / v["scale"]
            im = self.raw_back[mode].resize((max(1, round(v["w"] * k)), max(1, round(v["h"] * k))), Image.LANCZOS)
            for old in [x for x in self.backs if x[0] != key[0]]:
                del self.backs[old]
            self.backs[key] = (im, ImageTk.PhotoImage(im))
        return self.backs[key]

    def photo(self, scale, kind, name, mode):
        key = (round(scale, 4), kind, name, mode)
        hit = self.cache.get(key)
        if hit:
            return hit
        from PIL import ImageTk
        src = self.props if kind == "prop" else self.agent(kind)
        im, (ax, ay) = src[name]
        ks = scale / self.meta["sprite"]["scale"]
        im = im.resize((max(1, round(im.width * ks)), max(1, round(im.height * ks))), Image.LANCZOS)
        if mode == "night" and not name.startswith("tv_") and not name.startswith("glass"):
            rgb = ImageEnhance.Brightness(im.convert("RGB")).enhance(0.8)
            r, g, b = rgb.split()
            im = Image.merge("RGBA", (r, g, b.point(lambda v: min(255, int(v * 1.08))), im.getchannel("A")))
        if len(self.cache) > 4000:       # старые зумы не держим
            for k in [k for k in self.cache if k[0] != key[0]]:
                del self.cache[k]
        self.cache[key] = (ImageTk.PhotoImage(im), (ax * ks, ay * ks))
        return self.cache[key]


class Person:
    def __init__(self, key, outfit, x, z):
        self.key, self.outfit = key, outfit
        self.x, self.z = x, z
        self.path, self.walked = [], 0.0
        self.exit = None            # точка на дорожке, откуда пришёл к текущему месту
        self.goal_exit = None
        self.pose, self.dir = "stand", "S"
        self.task, self.until = None, 0.0
        self.busy = self.alert = self.leaving = self.gone = False
        self.session, self.label = None, ""
        self.desk = None            # id стола
        self.spot = None            # ключ занятого места
        self.phase = random.random() * 10
        self.box = None


class Office:
    """Живой офис на холсте tkinter. update(people) — кто есть и чем занят; draw() — кадр."""

    def __init__(self, canvas, scale=1.0, font=None, font_bold=None):
        self.cv, self.s = canvas, scale
        self.font, self.font_bold = font or ("Segoe UI", 8), font_bold or ("Segoe UI", 9, "bold")
        self.assets = None
        self.people: dict[str, Person] = {}
        self.spots: dict[str, str] = {}
        self.last = time.time()
        self.hover = self.drag = None
        self.zoom = None
        self.ox = self.oy = 0.0
        self.room = None
        try:
            self.assets = Assets()
            self.room = self.assets.meta["room"]
        except Exception:
            return
        r = self.room
        self.desks = {d["id"]: d for d in r["desks"]}
        pts = [(d["ax"], d["az"]) for d in r["desks"]] + [(s["ax"], s["az"]) for v in r["spots"].values() for s in v]
        self.graph = Graph(r["lanes"], pts)

    # ── вид: масштаб, сдвиг, зум колесом ──
    @property
    def scale(self):
        return self.s * self.zoom

    def zoom_levels(self):
        v = self.assets.meta["view"]
        w, h = self.cv.winfo_width(), self.cv.winfo_height()
        fit = min(w * v["scale"] / (v["w"] * self.s), h * v["scale"] / (v["h"] * self.s))
        return sorted({z for z in ZOOMS if z > fit + 0.02} | {round(min(1.0, fit), 3)}, reverse=True)

    def ready(self):
        if not self.room or self.cv.winfo_width() < 50:
            return False
        if self.zoom is None:            # первый показ: середина офиса, масштаб 1
            self.zoom = 1.0
            cx, cy = self.proj(self.room["center"]["x"], 0.6, self.room["center"]["z"])
            self.ox += self.cv.winfo_width() / 2 - cx
            self.oy += self.cv.winfo_height() / 2 - cy
        self.pan(0, 0)
        return True

    def pan(self, dx, dy):
        v = self.assets.meta["view"]
        k = self.scale / v["scale"]
        bw, bh = v["w"] * k, v["h"] * k
        w, h = self.cv.winfo_width(), self.cv.winfo_height()
        self.ox = min(0, max(w - bw, self.ox + dx)) if bw > w else (w - bw) / 2
        self.oy = min(0, max(h - bh, self.oy + dy)) if bh > h else (h - bh) / 2

    def wheel(self, x, y, delta):
        """Колесо: от себя — ближе, на себя — дальше; точка под курсором остаётся на месте."""
        if not self.room or self.zoom is None:
            return
        levels = self.zoom_levels()
        cur = min(range(len(levels)), key=lambda i: abs(levels[i] - self.zoom))
        nxt = levels[max(0, min(len(levels) - 1, cur + (-1 if delta > 0 else 1)))]
        if nxt == self.zoom:
            return
        f = nxt / self.zoom
        self.ox, self.oy = x - (x - self.ox) * f, y - (y - self.oy) * f
        self.zoom = nxt
        self.pan(0, 0)

    def press(self, x, y):
        self.drag = (x, y, x, y)

    def move(self, x, y):
        if self.drag:
            x0, y0, lx, ly = self.drag
            if abs(x - x0) + abs(y - y0) > 4:
                self.pan(x - lx, y - ly)
                self.cv.configure(cursor="fleur")
            self.drag = (x0, y0, x, y)
        else:
            self.hover = self.at(x, y)

    def release(self, x, y):
        """True — это был клик (не перетаскивание)."""
        d, self.drag = self.drag, None
        self.cv.configure(cursor="hand2")
        return bool(d) and abs(x - d[0]) + abs(y - d[1]) <= 4

    def proj(self, x, y, z):
        v = self.assets.meta["view"]
        k = self.scale / v["scale"]
        px = v["origin"][0] + x * v["ex"][0] + y * v["ey"][0] + z * v["ez"][0]
        py = v["origin"][1] + x * v["ex"][1] + y * v["ey"][1] + z * v["ez"][1]
        return px * k + self.ox, py * k + self.oy

    def depth(self, x, z):
        f = self.assets.meta["view"]["fwd"]
        return x * f[0] + z * f[2]

    # ── места и маршруты ──
    def desk_spot(self, did):
        d = self.desks[did]
        return {"x": d["seat"]["x"], "z": d["seat"]["z"], "ax": d["ax"], "az": d["az"], "dir": "S"}

    def go(self, p, task, spot, key=None, until=0.0):
        if p.spot and p.spot != key:
            self.spots.pop(p.spot, None)
        if key:
            self.spots[key] = p.key
        p.spot, p.task, p.until = key, task, until
        p.target = spot
        if p.path:
            first = p.path[0]
            start = p.goal_exit if len(p.path) == 1 else first
            pts = [first]
        else:
            first = (p.x, p.z)
            start = p.exit or self.graph.snap(first)
            pts = []
        goal = (spot["ax"], spot["az"])
        if _k(start) != _k(first):
            pts.append(start)
        pts += self.graph.path(start, goal)[1:]
        pts.append((spot["x"], spot["z"]))
        clean, cur = [], (p.x, p.z)
        for q in pts:
            if abs(q[0] - cur[0]) > 0.01 or abs(q[1] - cur[1]) > 0.01:
                clean.append(q)
                cur = q
        p.path, p.goal_exit = clean, goal
        if not clean:
            self.arrive(p, time.time())

    def free(self, key, who):
        return self.spots.get(key) in (None, who)

    # ── кто есть в офисе ──
    def update(self, people):
        """people: [{key, outfit, busy, alert, label, session, desk, enter}] — все, кто должен быть в офисе."""
        if not self.room:
            return
        seen = set()
        for d in people:
            key = d["key"]
            seen.add(key)
            p = self.people.get(key)
            if p is None:
                desk = self.free_desk(d.get("desk"))
                if d.get("enter") or desk is None:       # пришёл по ходу работы — входит в дверь
                    door = self.room["spots"]["door"][0]
                    p = Person(key, d["outfit"], door["x"], door["z"])
                    p.exit = (door["ax"], door["az"])
                else:                                     # команда уже в офисе: сидят за столами
                    sp = self.desk_spot(desk)
                    p = Person(key, d["outfit"], sp["x"], sp["z"])
                    p.exit, p.pose, p.dir = (sp["ax"], sp["az"]), "relax", "S"
                    p.task, p.until, p.spot = ("relax",), time.time() + random.uniform(3, 30), f"desk:{desk}"
                    self.spots[p.spot] = key
                p.desk = desk
                self.people[key] = p
            p.busy, p.alert = bool(d.get("busy")), bool(d.get("alert"))
            p.label, p.session, p.leaving = d.get("label", ""), d.get("session"), False
            p.hint = d.get("hint", "")
        for key, p in self.people.items():
            if key not in seen and not p.leaving:
                p.leaving, p.busy, p.alert, p.desk = True, False, False, None
                self.go(p, ("leave",), self.room["spots"]["door"][0], "door:" + key)

    def free_desk(self, prefer=None):
        taken = {p.desk for p in self.people.values() if p.desk}
        if prefer and prefer in self.desks and prefer not in taken:
            return prefer
        for did in self.desks:
            if did not in taken and did != "boss":
                return did
        return None

    # ── поведение ──
    def think(self, p, now):
        if p.leaving:
            return
        t = p.task[0] if p.task else None
        if p.desk and (p.busy or p.alert):
            want = "type" if p.busy else "wave"
            if t != want:
                sp = self.desk_spot(p.desk)
                if want == "type":
                    self.go(p, ("type",), sp, f"desk:{p.desk}")
                else:
                    self.go(p, ("wave",), {**sp, "x": sp["ax"], "z": sp["az"]})
            return
        if t in ("type", "wave"):
            p.task, p.until = None, 0
        if p.path or (p.task and now < p.until):
            return
        if t == "coffee" and p.pose == "machine":
            p.task, p.until = ("coffee2",), now + random.uniform(6, 12)
            return
        self.pick(p, now)

    def pick(self, p, now):
        r = self.room["spots"]
        acts = dict(IDLE_WEIGHTS)
        if not p.desk:
            acts.pop("relax")
        for _ in range(10):
            act = random.choices(list(acts), weights=list(acts.values()))[0]
            dur = random.uniform(10, 28)
            if act == "relax":
                return self.go(p, ("relax",), self.desk_spot(p.desk), f"desk:{p.desk}", now + random.uniform(15, 45))
            if act == "stretch":
                p.task, p.until = ("stretch",), now + 3.5
                return
            kinds = {"coffee": ["coffee"], "game": ["sofa"], "window": ["window"], "meeting": ["meetS", "meetN"],
                     "chat": ["chat"], "bar": ["bar"], "phone": ["phone"]}[act]
            options = [(kind, i, s) for kind in kinds for i, s in enumerate(r.get(kind, ())) if self.free(f"{kind}{i}", p.key)]
            if options:
                kind, i, s = random.choice(options)
                until = now + (3.5 if act == "coffee" else random.uniform(20, 50) if act in ("game", "meeting") else dur)
                return self.go(p, (act, kind), s, f"{kind}{i}", until)
        p.task, p.until = ("stand",), now + 4

    def step(self, dt, now):
        for key in list(self.people):
            p = self.people[key]
            self.think(p, now)
            if p.path:
                left = WALK_SPEED * dt
                while left > 0 and p.path:
                    tx, tz = p.path[0]
                    dx, dz = tx - p.x, tz - p.z
                    dist = math.hypot(dx, dz)
                    if dist > 1e-6:
                        p.dir = ("E" if dx > 0 else "W") if abs(dx) > abs(dz) else ("S" if dz > 0 else "N")
                    if dist <= left:
                        p.x, p.z = tx, tz
                        p.walked += dist
                        left -= dist
                        p.path.pop(0)
                    else:
                        p.x += dx / dist * left
                        p.z += dz / dist * left
                        p.walked += left
                        left = 0
                if not p.path:
                    self.arrive(p, now)
            if p.gone:
                if p.spot:
                    self.spots.pop(p.spot, None)
                del self.people[key]

    def arrive(self, p, now):
        p.exit = p.goal_exit
        t = p.task[0] if p.task else "stand"
        kind = p.task[1] if p.task and len(p.task) > 1 else None
        spot = getattr(p, "target", None) or {}
        if t == "leave":
            p.gone = True
            return
        poses = {"type": "type", "relax": "relax", "wave": "wave", "coffee": "machine", "game": "game", "window": "window",
                 "bar": "drink", "phone": "phone", "chat": "chat"}
        if t == "meeting":
            p.pose = kind
        else:
            p.pose = poses.get(t, "stand")
        p.dir = spot.get("dir", "S") if t not in ("type", "relax", "wave") else "S"
        if t == "coffee":
            p.until = now + 3.5

    def current_pose(self, p):
        if p.path:
            return "walk", p.dir
        t = p.task[0] if p.task else "stand"
        if t == "coffee2":
            return "drink", "S"
        if t == "stretch":
            return "stretch", "S"
        if t == "stand":
            return "stand", p.dir
        return p.pose, p.dir

    # ── кадр ──
    def draw(self):
        cv = self.cv
        if not self.ready():
            return
        now = time.time()
        dt = min(0.25, now - self.last)
        self.last = now
        self.step(dt, now)
        a, r, plan, sc = self.assets, self.room, self.assets.meta["plan"], self.scale
        mode = "day" if time.localtime().tm_hour in DAY_HOURS else "night"
        cv.delete("all")
        cv.create_image(self.ox, self.oy, image=a.back(sc, mode)[1], anchor="nw")
        self.clock(now)
        items = []
        sitting = {p.spot for p in self.people.values() if not p.path and p.task and p.pose in SEATED}
        for d in r["decor"]:
            items.append((self.depth(d["x"], d["z"]), "prop", d["sprite"], d["x"], 0, d["z"], None))
            if d["sprite"] == "media" and any(p.pose == "game" and not p.path for p in self.people.values()):
                items.append((self.depth(d["x"], d["z"]) - 0.01, "prop", f"tv_{int(now * 6) % 6}", r["tv"]["x"], r["tv"]["y"], r["tv"]["z"], None))
        for did, d in self.desks.items():
            typing = any(p.spot == f"desk:{did}" and not p.path and p.pose == "type" for p in self.people.values())
            name = f"desk_boss_{'on' if typing else 'off'}" if did == "boss" else f"desk_{'on' if typing else 'off'}_{sum(map(ord, did)) % 3}"
            items.append((self.depth(d["x"], d["z"]), "prop", name, d["x"], 0, d["z"], None))
            if f"desk:{did}" not in sitting:
                sx, sz = d["seat"]["x"], d["seat"]["z"] - r["chairDz"]
                items.append((self.depth(sx, sz), "prop", "chair", sx, 0, sz, None))
        for kind, chair, dz in (("meetS", "chair", -1), ("meetN", "chairN", 1)):
            for i, s in enumerate(r["spots"][kind]):
                if f"{kind}{i}" not in sitting:
                    z = s["z"] + dz * r["chairDz"]
                    items.append((self.depth(s["x"], z), "prop", chair, s["x"], 0, z, None))
        for p in self.people.values():
            pose, d = self.current_pose(p)
            info = plan.get(pose) or plan["stand"]
            if d not in info["dirs"]:
                d = info["dirs"][0]
            n = info["frames"]
            f = int(p.walked / STEP) % n if pose == "walk" else int((now + p.phase) * POSE_FPS.get(pose, 1)) % n
            items.append((self.depth(p.x, p.z), p.outfit, f"{pose}_{d}_{f}", p.x, 0, p.z, p))
        items.sort(key=lambda it: -it[0])
        w, h = cv.winfo_width(), cv.winfo_height()
        for _, kind, name, x, y, z, p in items:
            box = self.blit(kind, name, mode, x, y, z, w, h)
            if p is not None:
                p.box = box
        for p in self.people.values():
            self.overlay(p, now)

    def blit(self, kind, name, mode, x, y, z, w, h):
        px, py = self.proj(x, y, z)
        if px < -300 or px > w + 300 or py < -300 or py > h + 400:      # далеко за краем — не рисуем
            return None
        try:
            photo, (ax, ay) = self.assets.photo(self.scale, kind, name, mode)
        except KeyError:
            return None
        x0, y0 = px - ax, py - ay
        self.cv.create_image(x0, y0, image=photo, anchor="nw")
        return (x0, y0, x0 + photo.width(), y0 + photo.height())

    def clock(self, now):
        c = self.room["clock"]
        lt = time.localtime(now)
        cx, cy = self.proj(c["x"], c["y"], c["z"])
        for frac, length, w in (((lt.tm_hour % 12 + lt.tm_min / 60) / 12, 0.55, 2), ((lt.tm_min + lt.tm_sec / 60) / 60, 0.85, 1)):
            ang = frac * 2 * math.pi
            hx, hy = self.proj(c["x"] + math.sin(ang) * c["r"] * length, c["y"] + math.cos(ang) * c["r"] * length, c["z"])
            self.cv.create_line(cx, cy, hx, hy, fill="#1b1b1f", width=max(1, round(w * self.scale)), capstyle="round")

    def overlay(self, p, now):
        if not p.box:
            return
        x0, y0, x1, _ = p.box
        cx, s = (x0 + x1) / 2, self.s
        if p.alert and not p.path:
            rr = 7 * s
            on = int(now * 3) % 3 != 0
            self.cv.create_oval(cx - rr, y0 - 2 * rr - 2, cx + rr, y0 - 2, fill="#ff5a5f" if on else "#7a2c30", outline="")
            self.cv.create_text(cx, y0 - rr - 2, text="!", fill="#ffffff", font=self.font_bold)
        elif p.task and p.task[0] == "relax" and not p.path and not p.busy:
            k = (now * 0.8 + p.phase) % 3
            self.cv.create_text(cx + 8 * s + k * 2 * s, y0 + 4 * s - k * 4 * s, text="z", fill="#9aa3d8", font=self.font)
        text = f"{p.label} · {p.hint}" if p is self.hover and getattr(p, "hint", "") else p.label
        if (p.busy or p.alert or p is self.hover or p.outfit == MAIN) and text:
            col = "#e8885a" if p.outfit == MAIN else "#c9c3ff" if p.busy else "#aab0d6"
            ty = y0 - (18 * s if p.alert and not p.path else 2 * s)
            self.cv.create_text(cx + 1, ty + 1, text=text, fill="#0b0c18", font=self.font, anchor="s")
            self.cv.create_text(cx, ty, text=text, fill=col, font=self.font, anchor="s")

    def at(self, x, y):
        """Кто под курсором (ближний к зрителю)."""
        best = None
        for p in self.people.values():
            if p.box and p.box[0] <= x <= p.box[2] and p.box[1] <= y <= p.box[3]:
                if best is None or self.depth(p.x, p.z) < self.depth(best.x, best.z):
                    best = p
        return best


SHORT = {"code-reviewer": "review", "security-reviewer": "security", "qa-tester": "qa", "token-auditor": "auditor",
         "researcher": "research", "database-agent": "database", "devops-agent": "devops", "docs-agent": "docs"}


def short(kind):
    return SHORT.get(kind) or kind.removesuffix("-dev")


def people_from_sessions(sessions, main_name="Игрис", known=None):
    """Список для Office.update: Игрис на каждую сессию + вся команда + запущенные встроенные агенты."""
    known = set(ROSTER if known is None else known)
    out, busy_kind = [], {}
    spare = ["O7", "O8"]
    for i, s in enumerate(sessions):
        out.append({"key": f"main:{i}", "outfit": MAIN, "busy": s.get("busy"), "alert": bool(s.get("attn")),
                    "label": main_name if i == 0 else f"{main_name} {i + 1}", "hint": str(s.get("name", ""))[:40],
                    "session": s, "desk": "boss" if i == 0 else (spare[i - 1] if i - 1 < len(spare) else None)})
        for a in s.get("agents", []):
            kind = a.get("kind") or ""
            if kind in known and kind not in busy_kind:
                busy_kind[kind] = (a.get("busy"), s)
            else:
                out.append({"key": f"sub:{a['id']}", "outfit": kind if kind in known else INTERN, "busy": a.get("busy"),
                            "label": a.get("type") or "агент", "session": s, "enter": True})
    if not sessions:
        out.append({"key": "main:0", "outfit": MAIN, "label": main_name, "desk": "boss"})
    for kind in ROSTER:
        if kind in known:
            b = busy_kind.get(kind)
            out.append({"key": f"team:{kind}", "outfit": kind, "busy": bool(b and b[0]), "label": short(kind),
                        "session": b[1] if b else None, "desk": TEAM_DESK.get(kind)})
    return out


def team_kinds():
    """Агенты из ~/.claude/agents, для которых есть образ."""
    d = Path.home() / ".claude" / "agents"
    names = {f.stem for f in d.glob("*.md")} if d.exists() else set()
    return [k for k in ROSTER if k in names] or ROSTER


if __name__ == "__main__":       # демо: офис в отдельном окне, сессии меняются сами
    import sys
    import tkinter as tk
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass
    root = tk.Tk()
    s = root.winfo_fpixels("1i") / 96
    root.title("Офис агентов — демо")
    cv = tk.Canvas(root, width=int(536 * s), height=int(270 * s), bg="#121430", highlightthickness=0, cursor="hand2")
    cv.pack(padx=int(12 * s), pady=int(12 * s))
    office = Office(cv, s, ("Segoe UI", 8), ("Segoe UI", 9, "bold"))
    t0 = time.time()
    args = sys.argv[1:]

    def feed():
        t = time.time() - t0
        sess = [{"id": "a", "name": "sisadmin", "busy": int(t / 12) % 2 == 0, "attn": 20 < t % 60 < 26,
                 "agents": ([{"id": "x1", "kind": "frontend-dev", "type": "frontend", "busy": True}] if 8 < t % 40 < 30 else [])
                 + ([{"id": "x2", "kind": "Explore", "type": "explore", "busy": True}] if 15 < t % 50 < 40 else [])}]
        office.update(people_from_sessions(sess))
        root.after(1000, feed)

    def tick():
        office.draw()
        root.after(90, tick)

    cv.bind("<Motion>", lambda e: office.move(e.x, e.y))
    cv.bind("<B1-Motion>", lambda e: office.move(e.x, e.y))
    cv.bind("<ButtonPress-1>", lambda e: office.press(e.x, e.y))
    cv.bind("<ButtonRelease-1>", lambda e: office.release(e.x, e.y))
    cv.bind("<MouseWheel>", lambda e: office.wheel(e.x, e.y, e.delta))
    feed()
    tick()
    if args:          # python office3d.py out.png 30 [zoom]  — снимок через N секунд (zoom: out — отдалить до конца)
        from PIL import ImageGrab
        delay = float(args[1]) if len(args) > 1 else 8
        if len(args) > 2 and args[2] == "out":
            root.after(1500, lambda: [office.wheel(cv.winfo_width() // 2, cv.winfo_height() // 2, -120) for _ in range(6)])

        def snap():
            x, y = cv.winfo_rootx(), cv.winfo_rooty()
            ImageGrab.grab((x, y, x + cv.winfo_width(), y + cv.winfo_height()), all_screens=True).save(args[0])
            root.destroy()
        root.after(int(delay * 1000), snap)
    root.mainloop()
