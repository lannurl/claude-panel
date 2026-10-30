"""MonitorsCtl на фейковой платформе: ничего в ОС не меняется (ни мышь, ни раскладка экранов)."""
import json
import os
import queue
import tempfile
import threading
import time
import unittest
from pathlib import Path

import _util  # добавляет корень проекта в sys.path
assert _util.ROOT.is_dir()
import layout as L
import monitors_ctl

DISPLAYS = [
    {"os_id": "win:SDC4161:00000000:UID4352", "gdi_name": r"\\.\DISPLAY1", "label": "Ноут", "px": [1920, 1080],
     "rect_px": [0, 0, 1920, 1080], "primary": True, "scale": 125, "size_mm": [345.0, 194.0], "virtual": False},
    {"os_id": "win:GSM5B7F:0001E240", "gdi_name": r"\\.\DISPLAY2", "label": "Монитор", "px": [2560, 1440],
     "rect_px": [1920, 0, 2560, 1440], "primary": False, "scale": 100, "size_mm": [597.0, 336.0], "virtual": False},
]


class FakeEdge:
    instances = []

    def __init__(self, rules, rects, cfg, on_event=None):
        self.rules, self.rects, self.cfg, self.on_event = rules, rects, cfg, on_event
        self.running = False
        FakeEdge.instances.append(self)

    def start(self):
        self.running = True

    def stop(self):
        self.running = False

    def set_rules(self, rules, rects):
        self.rules, self.rects = rules, rects

    def pause(self, flag):
        self.paused = flag

    def status(self):
        return {"running": self.running, "paused": False, "err": None, "warps": 0, "last_warp": None,
                "raw_input": True, "client_state": "local", "tripped": False}


class FakePlat:
    EdgeHelper = FakeEdge

    def __init__(self):
        self.displays = [dict(d) for d in DISPLAYS]
        self.calls = []

    def list_displays(self, vm=None):
        return [dict(d) for d in self.displays]

    def set_display_positions(self, pos, dry_run=False):
        self.calls.append((dict(pos), dry_run))
        prev = {d["os_id"]: tuple(d["rect_px"][:2]) for d in self.displays}
        plan = []
        for d in self.displays:
            if d["os_id"] in pos and tuple(pos[d["os_id"]]) != tuple(d["rect_px"][:2]):
                plan.append({"os_id": d["os_id"]})
                if not dry_run:
                    d["rect_px"][:2] = list(pos[d["os_id"]])
        return {"ok": True, "code": None, "err": None, "previous": prev, "plan": plan, "dry_run": dry_run}

    def ease_cursor_setting(self):
        return True


class TestMonitorsCtl(unittest.TestCase):
    def setUp(self):
        FakeEdge.instances.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Path(self.tmp.name) / "config.json"
        raw = json.loads(json.dumps(L.EXAMPLE))
        raw["screens"][1]["os_rect_px"] = [1920, 0, 2560, 1440]   # в ОС монитор пока «верх по верху»
        self.cfg.write_text(json.dumps({"main_agent_name": "Claude", "monitors": raw}, ensure_ascii=False),
                            encoding="utf-8")
        self.events = queue.Queue()
        self.plat = FakePlat()
        self.ctl = monitors_ctl.MonitorsCtl(raw, self.cfg, Path(self.tmp.name) / "data", self.plat,
                                            on_event=lambda k, p: self.events.put((k, p)))

    def tearDown(self):
        self.ctl.stop()
        time.sleep(0.05)
        self.tmp.cleanup()

    def wait_event(self, kind, timeout=3.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                k, p = self.events.get(timeout=0.05)
            except queue.Empty:
                continue
            if k == kind:
                return p
        self.fail(f"нет события {kind}")

    def test_snapshot_shape(self):
        self.ctl.start()
        s = self.ctl.snapshot()
        for k in ("this_machine", "mode", "server", "busy", "busy_text", "layout_ok", "warnings", "components",
                  "deskflow", "stream", "edge", "link"):
            self.assertIn(k, s)
        for c in ("deskflow", "apollo", "moonlight", "edge"):
            self.assertIn("state", s["components"][c])
        self.assertEqual(s["mode"], "off")
        self.assertTrue(s["layout_ok"])
        self.assertIsNone(s["this_machine"])       # hostname не совпал — нужно выбрать
        self.assertFalse(s["edge"]["running"])     # по умолчанию выключен
        self.assertEqual(FakeEdge.instances, [])
        self.assertEqual(s["components"]["edge"]["ease_cursor"], True)

    def test_this_machine_and_rules(self):
        self.ctl.start()
        self.assertTrue(self.ctl.set_this_machine("a")["ok"])
        self.assertFalse(self.ctl.set_this_machine("zzz")["ok"])
        st = json.loads((Path(self.tmp.name) / "data" / "monitors" / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(st["this_machine"], "a")
        pv = self.ctl.preview()
        self.assertTrue(pv["ok"], pv)
        self.assertEqual(pv["os_positions"]["a.2"][0], 1920)
        self.assertLessEqual(abs(pv["os_positions"]["a.2"][1] + 868), 1)
        self.assertTrue(pv["rules"])
        self.assertEqual(len(pv["links"]), 4)

    def test_edge_toggle_writes_config(self):
        self.ctl.start()
        self.ctl.set_this_machine("a")
        res = self.ctl.set_edge_helper(True)
        self.assertTrue(res["ok"], res)
        data = json.loads(self.cfg.read_text(encoding="utf-8"))
        self.assertTrue(data["monitors"]["edge_helper"]["enabled"])
        self.assertEqual(data["monitors"]["revision"], 8)
        self.assertEqual(data["main_agent_name"], "Claude")
        self.assertEqual(len(FakeEdge.instances), 1)
        self.assertTrue(FakeEdge.instances[0].running)
        self.assertTrue(FakeEdge.instances[0].rules)
        self.assertTrue(self.ctl.snapshot()["edge"]["running"])
        self.assertTrue(self.ctl.set_edge_helper(False)["ok"])
        self.assertFalse(FakeEdge.instances[0].running)
        self.assertFalse(self.ctl.set_edge_helper("yes")["ok"])

    def test_apply_confirm(self):
        self.ctl.start()
        self.ctl.set_this_machine("a")
        dry = self.ctl.apply_os_positions(dry_run=True)
        self.assertTrue(dry["ok"])
        self.assertEqual(self.plat.calls[-1][1], True)
        res = self.ctl.apply_os_positions()
        self.assertTrue(res["pending"])
        p = self.wait_event("os_positions_pending")
        self.assertEqual(p["token"], res["token"])
        self.assertEqual(self.plat.displays[1]["rect_px"][1], -868)
        self.assertEqual(self.ctl.apply_os_positions()["code"], "busy")
        self.assertFalse(self.ctl.confirm_os_positions("wrong")["ok"])
        self.assertTrue(self.ctl.confirm_os_positions(res["token"])["ok"])
        self.assertIsNone(self.ctl.snapshot()["os_positions_pending"])

    def test_apply_auto_rollback(self):
        old = monitors_ctl.ROLLBACK_S
        monitors_ctl.ROLLBACK_S = 0.2
        try:
            self.ctl.start()
            self.ctl.set_this_machine("a")
            res = self.ctl.apply_os_positions()
            self.wait_event("os_positions_pending")
            self.assertEqual(self.plat.displays[1]["rect_px"][1], -868)
            end = time.monotonic() + 3
            while self.plat.displays[1]["rect_px"][1] != 0 and time.monotonic() < end:
                time.sleep(0.02)
            self.assertEqual(self.plat.displays[1]["rect_px"][1], 0)   # без «Оставить» — откат
            self.assertFalse(self.ctl.confirm_os_positions(res["token"])["ok"])
        finally:
            monitors_ctl.ROLLBACK_S = old

    def test_modes_stage1(self):
        self.assertEqual(self.ctl.set_mode("shared", "a")["code"], "not_found")
        self.assertEqual(self.ctl.set_mode("monitor")["code"], "not_found")
        self.assertEqual(self.ctl.set_mode("bogus")["code"], "bad_config")
        self.assertTrue(self.ctl.set_mode("off")["pending"])
        self.assertEqual(self.wait_event("mode_changed"), {"mode": "off"})

    def test_export_import(self):
        text = self.ctl.export_layout()
        data = json.loads(text)
        self.assertEqual(data["revision"], 7)
        data["screens"][2]["pos_mm"] = [943.1, 240.0]
        res = self.ctl.import_layout(json.dumps(data))
        self.assertTrue(res["ok"], res)
        self.assertEqual(self.ctl.layout().screen("b.1").pos_mm, (943.1, 240.0))
        self.assertFalse(self.ctl.import_layout("not json")["ok"])
        self.assertFalse(self.ctl.import_layout("x" * (300 * 1024))["ok"])

    def test_no_layout(self):
        ctl = monitors_ctl.MonitorsCtl(None, self.cfg, Path(self.tmp.name) / "d2", self.plat)
        ctl.start()
        s = ctl.snapshot()
        self.assertFalse(s["layout_ok"])
        self.assertEqual(len(ctl.local_displays()), 2)
        self.assertFalse(ctl.set_edge_helper(True)["ok"])
        self.assertEqual(ctl.export_layout(), "")
        ctl.stop()

    def test_displays_changed_event(self):
        self.ctl.start()
        self.ctl.set_this_machine("a")
        self.ctl.set_edge_helper(True)
        edge = FakeEdge.instances[0]
        self.plat.displays[1]["rect_px"] = [1920, -868, 2560, 1440]
        edge.on_event("displays_changed", None)
        self.wait_event("displays_changed")
        self.assertEqual(edge.rects["a.2"], (1920, -868, 2560, 1440))

    def test_pause_edge_helper(self):
        self.ctl.start()
        self.ctl.set_this_machine("a")
        r = self.ctl.pause_edge_helper(True)          # помощник ещё не включён — пауза запоминается
        self.assertEqual((r["ok"], r["paused"], r["running"]), (True, True, False))
        self.ctl.set_edge_helper(True)
        edge = FakeEdge.instances[0]
        self.assertTrue(edge.paused)
        self.assertTrue(self.ctl.snapshot()["edge"]["paused"])
        self.assertTrue(self.ctl.snapshot()["edge"]["running"])   # пауза — не выключение
        self.assertTrue(self.ctl.pause_edge_helper(False)["ok"])
        self.assertFalse(edge.paused)
        self.assertFalse(self.ctl.snapshot()["edge"]["paused"])
        self.assertFalse(self.ctl.pause_edge_helper("yes")["ok"])
        data = json.loads(self.cfg.read_text(encoding="utf-8"))
        self.assertTrue(data["monitors"]["edge_helper"]["enabled"])

    def test_refresh_displays_without_start(self):
        self.assertEqual(self.ctl.local_displays(), [])
        ds = self.ctl.refresh_displays()
        self.assertEqual(len(ds), 2)
        self.assertEqual(len(self.ctl.local_displays()), 2)

    def test_stop_leaves_no_threads_blocking(self):
        self.ctl.start()
        self.ctl.stop()
        self.assertTrue(all(t.daemon for t in threading.enumerate() if t.name == "monitors-ctl"))
        self.assertTrue(os.path.exists(Path(self.tmp.name) / "data" / "monitors" / "state.json"))


if __name__ == "__main__":
    unittest.main()
