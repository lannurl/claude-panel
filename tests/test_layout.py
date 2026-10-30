"""Геометрия раскладки: эталонные числа §7.6 (раскладка пользователя) и дополнительные раскладки §12.2 п. 1."""
import json
import os
import tempfile
import unittest
from dataclasses import replace

import _util  # добавляет корень проекта в sys.path
assert _util.ROOT.is_dir()
import layout as L
from _util import make_edid


def example():
    lay, warn = L.parse_config(json.loads(json.dumps(L.EXAMPLE)))
    assert lay is not None and not warn, warn
    return lay


def scr(sid, pos, size, px=(1920, 1080), rect=None, machine=None, label=None):
    return {"id": sid, "machine": machine or sid.split(".")[0], "label": label or sid, "px": list(px),
            "size_mm": list(size), "size_source": "user", "pos_mm": list(pos), "os_rect_px": rect}


def make(screens, machines=("a",)):
    raw = {"version": 1, "machines": [{"id": m, "name": f"pc-{m}", "hostname": "", "os": "windows"} for m in machines],
           "screens": screens}
    lay, warn = L.parse_config(raw)
    assert lay is not None, warn
    return lay


def assert_cover(tc, lay, sid, side):
    """Сегменты покрывают край целиком, без дыр и перекрытий."""
    segs = L.edge_segments(lay, sid, side)
    if not segs:
        return segs
    s = lay.screen(sid)
    lo, hi = (s.pos_mm[1], s.pos_mm[1] + s.size_mm[1]) if side in ("left", "right") else \
        (s.pos_mm[0], s.pos_mm[0] + s.size_mm[0])
    tc.assertAlmostEqual(segs[0].src_from_mm, lo, places=4)
    tc.assertAlmostEqual(segs[-1].src_to_mm, hi, places=4)
    for a, b in zip(segs, segs[1:]):
        tc.assertAlmostEqual(a.src_to_mm, b.src_from_mm, places=6)
    for g in segs:
        tc.assertGreater(g.src_to_mm, g.src_from_mm)
    return segs


class TestSizes(unittest.TestCase):
    def test_diagonal_inches(self):
        self.assertEqual(L.size_from_diagonal(15.6, "in", (1920, 1080)), (345.4, 194.3))
        self.assertEqual(L.size_from_diagonal(27, "in", (2560, 1440)), (597.7, 336.2))

    def test_diagonal_cm(self):
        self.assertEqual(L.size_from_diagonal(39.624, "cm", (1920, 1080)), (345.4, 194.3))
        self.assertEqual(L.size_from_diagonal(68.58, "cm", (2560, 1440)), (597.7, 336.2))

    def test_diagonal_bad(self):
        with self.assertRaises(ValueError):
            L.size_from_diagonal(15.6, "ft", (1920, 1080))
        with self.assertRaises(ValueError):
            L.size_from_diagonal(-1, "in", (1920, 1080))

    def test_diagonal_in(self):
        self.assertEqual(L.diagonal_in((345.4, 194.3)), 15.6)
        self.assertEqual(L.diagonal_in((597.7, 336.2)), 27.0)

    def test_density(self):
        self.assertAlmostEqual(1080 / 194.3, 5.5584, places=4)
        self.assertAlmostEqual(1440 / 336.2, 4.2832, places=4)


class TestEdid(unittest.TestCase):
    def test_laptop(self):
        e = L.parse_edid(make_edid("SDC", 0x4161, 0, (344, 194), (34, 19), "PANEL"), px=(1920, 1080))
        self.assertEqual(e["mm"], (344.0, 194.0))
        self.assertEqual(e["pnp"], "SDC4161")
        self.assertIsNone(e["serial"])
        self.assertEqual(e["name"], "PANEL")

    def test_monitor(self):
        e = L.parse_edid(make_edid("GSM", 0x5B7F, 123456, (597, 336), (60, 34), "LG ULTRAGEAR"), px=(2560, 1440))
        self.assertEqual(e["mm"], (597.0, 336.0))
        self.assertEqual(e["pnp"], "GSM5B7F")
        self.assertEqual(e["serial"], 123456)
        self.assertEqual(e["name"], "LG ULTRAGEAR")

    def test_broken_zeros(self):
        self.assertIsNone(L.parse_edid(bytes(128))["mm"])
        self.assertIsNone(L.parse_edid(b"")["mm"])
        self.assertIsNone(L.parse_edid(None)["mm"])
        e = L.parse_edid(make_edid(mm=(0, 0), cm=(0, 0), pixel_clock=False))
        self.assertIsNone(e["mm"])

    def test_fallback_cm(self):
        e = L.parse_edid(make_edid(mm=(0, 0), cm=(60, 34), pixel_clock=False), px=(2560, 1440))
        self.assertEqual(e["mm"], (600.0, 340.0))

    def test_tv_aspect_rejected(self):
        # телевизор/проектор: размер «16x9 см» (условный) — не проходит ни диапазон, ни аспект
        e = L.parse_edid(make_edid(mm=(16, 9), cm=(1, 1)), px=(1920, 1080))
        self.assertIsNone(e["mm"])
        e = L.parse_edid(make_edid(mm=(400, 400), cm=(40, 40)), px=(1920, 1080))
        self.assertIsNone(e["mm"])

    def test_rotated(self):
        e = L.parse_edid(make_edid(mm=(597, 336)), px=(1440, 2560))
        self.assertEqual(e["mm"], (336.0, 597.0))


class TestParseConfig(unittest.TestCase):
    def test_example_ok(self):
        lay = example()
        self.assertEqual([s.id for s in lay.screens], ["a.1", "a.2", "b.1"])
        self.assertEqual(lay.revision, 7)
        self.assertEqual(lay.extra["deskflow"]["server"], "a")
        self.assertEqual(lay.extra["stream"]["host_app"], "apollo")

    def test_defaults_safe(self):
        lay = example()
        self.assertFalse(lay.extra["edge_helper"]["enabled"])   # помощник по умолчанию выключен
        self.assertFalse(lay.extra["stop_on_exit"])             # версия 1.1
        self.assertTrue(lay.extra["deskflow"]["tls"])

    def test_bad_values_replaced(self):
        raw = json.loads(json.dumps(L.EXAMPLE))
        raw["edge_helper"] = {"poll_ms": 1000, "enabled": "yes", "inset_px": 3}
        raw["deskflow"] = {"port": 80, "tls": False, "server": "zzz", "path": "relative/deskflow-core.exe"}
        raw["machines"][1]["host"] = "-oProxyCommand=evil"
        raw["screens"][0]["pos_mm"] = [1e9, 0]
        raw["screens"].append({"id": "c.1", "machine": "c", "px": [1, 1]})
        lay, warn = L.parse_config(raw)
        self.assertEqual(lay.extra["edge_helper"]["poll_ms"], 4)
        self.assertFalse(lay.extra["edge_helper"]["enabled"])
        self.assertEqual(lay.extra["edge_helper"]["inset_px"], 3)
        self.assertEqual(lay.extra["deskflow"]["port"], 24800)
        self.assertFalse(lay.extra["deskflow"]["tls"])           # явное false разрешено
        self.assertEqual(lay.extra["deskflow"]["server"], "a")
        self.assertIsNone(lay.extra["deskflow"]["path"])
        self.assertIsNone(lay.machine("b").host)
        self.assertEqual(lay.screen("a.1").pos_mm, (0.0, 0.0))
        self.assertIsNone(lay.screen("c.1"))
        self.assertGreaterEqual(len(warn), 7)

    def test_hotkeys(self):
        raw = json.loads(json.dumps(L.EXAMPLE))
        raw["deskflow"] = {"work_on_hotkeys": {"work_on": "Control+Alt+Q"}}
        lay, warn = L.parse_config(raw)
        self.assertEqual(lay.extra["deskflow"]["work_on_hotkeys"]["work_on"], "Control+Alt+Q")
        self.assertEqual(lay.extra["deskflow"]["work_on_hotkeys"]["back"], "Control+Alt+Shift+B")
        raw["deskflow"] = {"work_on_hotkeys": {"work_on": "Control+Q & calc"}}
        lay, warn = L.parse_config(raw)
        self.assertEqual(lay.extra["deskflow"]["work_on_hotkeys"]["work_on"], "Control+Alt+Shift+A")
        self.assertTrue(warn)

    def test_none_and_garbage(self):
        self.assertEqual(L.parse_config(None), (None, []))
        self.assertIsNone(L.parse_config("x")[0])
        self.assertIsNone(L.parse_config({"machines": "nope"})[0])
        self.assertIsNone(L.parse_config({"version": 2, "machines": []})[0])

    def test_defaults_not_shared(self):
        a, _ = L.parse_config(json.loads(json.dumps(L.EXAMPLE)))
        a.extra["stream"]["virtual_match"].append("X")
        b, _ = L.parse_config(json.loads(json.dumps(L.EXAMPLE)))
        self.assertNotIn("X", b.extra["stream"]["virtual_match"])

    def test_to_config_roundtrip(self):
        lay = example()
        data = L.to_config(lay)
        self.assertEqual(data["revision"], 8)
        self.assertIn("updated", data)
        lay2, warn = L.parse_config(data)
        self.assertEqual(warn, [])
        self.assertEqual(lay2.screens, lay.screens)
        self.assertEqual(lay2.machines, lay.machines)


class TestExampleGeometry(unittest.TestCase):
    """§7.6: ноут A 15,6″ + монитор 27″ правее и выше + ноут B правее монитора."""

    def setUp(self):
        self.lay = example()

    def test_neighbors(self):
        self.assertEqual(L.neighbors(self.lay, "a.1", "right"), ["a.2"])
        self.assertEqual(L.neighbors(self.lay, "a.2", "left"), ["a.1"])
        self.assertEqual(L.neighbors(self.lay, "a.2", "right"), ["b.1"])
        self.assertEqual(L.neighbors(self.lay, "a.1", "top"), [])
        self.assertEqual(L.neighbors(self.lay, "a.1", "left"), [])

    def test_segments_a1_right(self):
        segs = assert_cover(self, self.lay, "a.1", "right")
        self.assertEqual([(g.kind, g.dst) for g in segs], [("linear", "a.2"), ("corner", "a.2")])
        self.assertAlmostEqual(segs[0].src_to_mm, 336.2, places=4)
        self.assertAlmostEqual(segs[1].dst_from_mm, 336.2, places=4)   # ближайший угол — низ M

    def test_segments_m_left_right(self):
        segs = assert_cover(self, self.lay, "a.2", "left")
        self.assertEqual([g.kind for g in segs], ["corner", "linear"])
        self.assertAlmostEqual(segs[0].src_to_mm, 220.0, places=4)
        self.assertAlmostEqual(segs[0].dst_from_mm, 220.0, places=4)
        segs = assert_cover(self, self.lay, "a.2", "right")
        self.assertEqual([g.kind for g in segs], ["corner", "linear"])
        self.assertAlmostEqual(segs[0].src_to_mm, 230.0, places=4)
        self.assertEqual(L.edge_segments(self.lay, "a.1", "left"), [])  # стена

    def test_all_edges_cover(self):
        for s in self.lay.screens:
            for side in L.SIDES:
                assert_cover(self, self.lay, s.id, side)

    def test_suggest_os_positions(self):
        pos = L.suggest_os_positions(self.lay, "a")
        self.assertEqual(pos["a.1"], (0, 0))
        x, y = pos["a.2"]
        self.assertEqual(x, 1920)
        self.assertLessEqual(abs(y - (-868)), 1)
        # касаются: пиксельное перекрытие y 0…571
        self.assertGreater(min(1080, y + 1440) - max(0, y), 500)

    def test_suggest_vd(self):
        vd = replace(self.lay, screens=tuple(replace(s, machine="a") if s.id == "b.1" else s for s in self.lay.screens))
        pos = L.suggest_os_positions(vd, "a")
        self.assertEqual(pos["b.1"][0], 4480)
        self.assertLessEqual(abs(pos["b.1"][1] - 49), 1)

    def test_rules_a1_to_m(self):
        rules, warn = L.internal_rules(self.lay, "a")
        self.assertEqual(warn, [])
        r = [x for x in rules if x.src == "a.1" and x.side == "right"]
        self.assertEqual(len(r), 2)
        lin, cor = r
        self.assertEqual((lin.kind, lin.line_px, lin.from_px), ("linear", 1920, 0))
        self.assertLessEqual(abs(lin.to_px - 646), 1)
        self.assertAlmostEqual(lin.a, 0.7706, places=3)
        self.assertAlmostEqual(lin.b, 74.3, delta=0.1)
        self.assertEqual((cor.kind, cor.to_px, cor.enter_px), ("corner", 1080, 1922))
        self.assertEqual(L.rule_target(cor, 800), (1922, 569))
        for y, want in ((0, 74), (300, 305), (540, 490), (571, 514), (620, 552)):
            got = L.rule_target(lin, y)
            self.assertEqual(got[0], 1922)
            self.assertLessEqual(abs(got[1] - want), 1, (y, got))

    def test_rules_m_to_a1(self):
        rules, _ = L.internal_rules(self.lay, "a")
        cor, lin = [x for x in rules if x.src == "a.2" and x.side == "left"]
        self.assertEqual((cor.kind, cor.from_px, cor.line_px, cor.enter_px), ("corner", -868, 1920, 1917))
        self.assertLessEqual(abs(cor.to_px - 75), 1)
        self.assertEqual(L.rule_target(cor, -500), (1917, 2))
        self.assertAlmostEqual(lin.a, 1.2977, places=3)
        self.assertAlmostEqual(lin.b, -96.4, delta=0.1)
        self.assertEqual(lin.to_px, 572)
        self.assertLessEqual(abs(L.rule_target(lin, 200)[1] - 163), 1)
        self.assertLessEqual(abs(L.rule_target(lin, 571)[1] - 645), 1)

    def test_rules_cover_edge_px(self):
        rules, _ = L.internal_rules(self.lay, "a")
        for src, side, lo, hi in (("a.1", "right", 0, 1080), ("a.2", "left", -868, 572)):
            r = sorted((x for x in rules if x.src == src and x.side == side), key=lambda x: x.from_px)
            self.assertEqual(r[0].from_px, lo)
            self.assertEqual(r[-1].to_px, hi)
            for a, b in zip(r, r[1:]):
                self.assertEqual(a.to_px, b.from_px)

    def test_deskflow_links_2(self):
        links, warn = L.deskflow_links(self.lay, decimals=2)
        self.assertEqual(warn, [])
        got = [(k.src_machine, k.side, k.src_from_pct, k.src_to_pct, k.dst_machine, k.dst_from_pct, k.dst_to_pct)
               for k in links]
        want = [("a", "right", 0, 50.57, "b", 0, 1), ("a", "right", 50.57, 73.92, "b", 0, 54.66),
                ("b", "left", 0, 54.66, "a", 50.57, 73.92), ("b", "left", 54.66, 100, "a", 72.92, 73.92)]
        self.assertEqual(len(got), len(want))
        for g, w in zip(got, want):
            self.assertEqual(g[:2] + g[4:5], w[:2] + w[4:5])
            for a, b in zip(g[2:4] + g[5:], w[2:4] + w[5:]):
                self.assertAlmostEqual(a, b, delta=0.05)

    def test_deskflow_links_0(self):
        links, _ = L.deskflow_links(self.lay, decimals=0)
        got = [(k.side, k.src_from_pct, k.src_to_pct, k.dst_from_pct, k.dst_to_pct) for k in links]
        self.assertEqual(got, [("right", 0, 51, 0, 1), ("right", 51, 74, 0, 55),
                               ("left", 0, 55, 51, 73), ("left", 55, 100, 72, 73)])

    def test_point_check(self):
        """Курсор на M, y = 1200 px от верха → 280,17 мм на B (§7.6)."""
        links, _ = L.deskflow_links(self.lay, decimals=2)
        lk = links[1]
        pct = 1200 / 1948 * 100
        self.assertAlmostEqual(pct, 61.60, delta=0.01)
        b_pct = (pct - lk.src_from_pct) / (lk.src_to_pct - lk.src_from_pct) * (lk.dst_to_pct - lk.dst_from_pct)
        b_px = b_pct / 100 * 1080
        self.assertAlmostEqual(230 + b_px / (1080 / 194.3), 280.17, delta=0.1)

    def test_validate_example(self):
        self.assertEqual(L.validate(self.lay), [])


class TestOtherLayouts(unittest.TestCase):
    def test_monitor_above_laptop(self):
        lay = make([scr("a.1", (100, 400), (345.4, 194.3), rect=(0, 0, 1920, 1080)),
                    scr("a.2", (0, 0), (597.7, 336.2), px=(2560, 1440), rect=(0, -1440, 2560, 1440))])
        self.assertEqual(L.neighbors(lay, "a.1", "top"), ["a.2"])
        self.assertEqual(L.neighbors(lay, "a.2", "bottom"), ["a.1"])
        segs = assert_cover(self, lay, "a.1", "top")
        self.assertEqual([g.kind for g in segs], ["linear"])
        segs = assert_cover(self, lay, "a.2", "bottom")
        self.assertEqual([g.kind for g in segs], ["corner", "linear", "corner"])
        rules, warn = L.internal_rules(lay, "a")
        up = [r for r in rules if r.src == "a.1" and r.side == "top"][0]
        self.assertEqual(up.line_px, 0)
        self.assertEqual(up.enter_px, -1 - 2)
        pos = L.suggest_os_positions(lay, "a")
        self.assertEqual(pos["a.2"][1], -1440)

    def test_three_screens_one_machine(self):
        lay = make([scr("a.1", (0, 100), (345.4, 194.3), rect=(0, 0, 1920, 1080)),
                    scr("a.2", (345.4, 0), (527, 296), rect=(1920, -200, 1920, 1080)),
                    scr("a.3", (872.4, 0), (527, 296), rect=(3840, -200, 1920, 1080))])
        for s in lay.screens:
            for side in L.SIDES:
                assert_cover(self, lay, s.id, side)
        self.assertEqual(L.neighbors(lay, "a.2", "right"), ["a.3"])
        self.assertEqual(L.neighbors(lay, "a.1", "right"), ["a.2"])   # a.3 за a.2 дальше — не ближайший
        pos = L.suggest_os_positions(lay, "a")
        self.assertEqual(pos["a.3"][0], 3840)
        self.assertEqual(pos["a.3"][1], pos["a.2"][1])

    def test_same_size_center(self):
        lay = make([scr("a.1", (0, 0), (527, 296), rect=(0, 0, 1920, 1080)),
                    scr("a.2", (527, 0), (527, 296), rect=(1920, 0, 1920, 1080))])
        self.assertEqual(L.suggest_os_positions(lay, "a"), {"a.1": (0, 0), "a.2": (1920, 0)})
        rules, _ = L.internal_rules(lay, "a")
        r = [x for x in rules if x.src == "a.1"][0]
        self.assertAlmostEqual(r.a, 1.0)
        self.assertAlmostEqual(r.b, 0.0, places=3)

    def test_two_candidates_midpoint(self):
        """Высокий экран справа от двух низких с зазором: пустая зона делится пополам между углами."""
        lay = make([scr("a.1", (0, 0), (300, 600), px=(1080, 1920)),
                    scr("a.2", (300, 0), (300, 168.8), machine="a"),
                    scr("a.3", (300, 400), (300, 168.8), machine="a")])
        segs = assert_cover(self, lay, "a.1", "right")
        kinds = [(g.dst, g.kind) for g in segs]
        self.assertEqual(kinds, [("a.2", "linear"), ("a.2", "corner"), ("a.3", "corner"), ("a.3", "linear"),
                                 ("a.3", "corner")])
        self.assertAlmostEqual(segs[1].src_to_mm, (168.8 + 400) / 2, places=4)

    def test_diagonal_neighbor(self):
        """Монитор правее и целиком выше ноута (зазор по y 20 мм, по x 0) — сосед справа, весь край → угол."""
        lay = make([scr("a.1", (0, 220), (345.4, 194.3)), scr("a.2", (345.4, 0), (597.7, 200), px=(2560, 857))])
        self.assertEqual(L.neighbors(lay, "a.1", "right"), ["a.2"])
        self.assertEqual(L.neighbors(lay, "a.1", "top"), [])
        segs = L.edge_segments(lay, "a.1", "right")
        self.assertEqual([(g.kind, g.dst_from_mm) for g in segs], [("corner", 200.0)])

    def test_far_screen_not_neighbor(self):
        lay = make([scr("a.1", (0, 0), (345.4, 194.3)), scr("a.2", (900, 0), (345.4, 194.3))])
        self.assertEqual(L.neighbors(lay, "a.1", "right"), [])

    def test_validate_not_on_bbox_edge(self):
        lay = example()
        # в ОС ноут A стоит справа от монитора — правый край M не край рабочего стола A
        screens = tuple(replace(s, os_rect_px=(2560, 0, 1920, 1080)) if s.id == "a.1" else
                        replace(s, os_rect_px=(0, -300, 2560, 1440)) if s.id == "a.2" else s for s in lay.screens)
        warn = L.validate(replace(lay, screens=screens))
        self.assertTrue(any("не на краю" in w for w in warn), warn)
        self.assertTrue(any("Раскладка ОС не совпадает" in w for w in warn), warn)

    def test_validate_overlap_and_aspect(self):
        lay = make([scr("a.1", (0, 0), (345.4, 194.3)), scr("a.2", (100, 50), (345.4, 300))])
        warn = L.validate(lay)
        self.assertTrue(any("перекрываются" in w for w in warn))
        self.assertTrue(any("пропорции" in w for w in warn))

    def test_normalize(self):
        lay = make([scr("a.1", (50, 30), (345.4, 194.3)), scr("a.2", (395.4, 10.5), (345.4, 194.3))])
        n = L.normalize(lay)
        self.assertEqual(n.screen("a.1").pos_mm, (0.0, 19.5))
        self.assertEqual(n.screen("a.2").pos_mm, (345.4, 0.0))

    def test_snap(self):
        lay = make([scr("a.1", (0, 0), (345.4, 194.3)), scr("a.2", (400, 300), (597.7, 336.2))])
        self.assertEqual(L.snap(lay, "a.2", (350, 5)), (345.4, 0.0))          # к правому краю, по верху
        self.assertEqual(L.snap(lay, "a.2", (351, -136)), (345.4, -141.9))    # низ по низу: 194,3 − 336,2
        self.assertEqual(L.snap(lay, "a.2", (351, -72)), (345.4, -70.9))      # центр по центру: 97,15 − 168,1
        self.assertEqual(L.snap(lay, "a.2", (500, 100)), (500.0, 100.0))      # далеко — без привязки


class TestCheckOsPositions(unittest.TestCase):
    def test_touch(self):
        ok = {"p": (0, 0, 1920, 1080), "m": (1920, -868, 2560, 1440)}
        self.assertIsNone(L.check_os_positions(ok, "p"))
        gap = {"p": (0, 0, 1920, 1080), "m": (1921, 0, 1920, 1080)}
        self.assertIn("касаются", L.check_os_positions(gap, "p"))
        corner = {"p": (0, 0, 1920, 1080), "m": (1920, 1080, 1920, 1080)}
        self.assertIsNotNone(L.check_os_positions(corner, "p"))
        over = {"p": (0, 0, 1920, 1080), "m": (1000, 0, 1920, 1080)}
        self.assertIn("перекрываются", L.check_os_positions(over, "p"))


class TestWriteConfig(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "config.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_replace_key_keeps_others(self):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"main_agent_name": "Assistant", "servers": [{"name": "файл-сервер", "host": "192.0.2.1"}]}, f,
                      ensure_ascii=False)
        res = L.write_config_section(self.path, "monitors", {"version": 1})
        self.assertTrue(res["ok"], res)
        with open(self.path, encoding="utf-8") as f:
            text = f.read()
        data = json.loads(text)
        self.assertEqual(data["main_agent_name"], "Assistant")
        self.assertIn("файл-сервер", text)         # ensure_ascii=False
        self.assertEqual(data["monitors"], {"version": 1})
        self.assertEqual(data["servers"][0]["host"], "192.0.2.1")
        self.assertEqual([n for n in os.listdir(self.tmp.name) if n != "config.json"], [])  # без хвостов

    def test_broken_json_untouched(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{broken")
        res = L.write_config_section(self.path, "monitors", {})
        self.assertFalse(res["ok"])
        self.assertEqual(res["code"], "bad_config")
        with open(self.path, encoding="utf-8") as f:
            self.assertEqual(f.read(), "{broken")

    def test_missing_file_created(self):
        res = L.write_config_section(self.path, "monitors", {"a": 1})
        self.assertTrue(res["ok"])
        with open(self.path, encoding="utf-8") as f:
            self.assertEqual(json.load(f), {"monitors": {"a": 1}})


if __name__ == "__main__":
    unittest.main()
