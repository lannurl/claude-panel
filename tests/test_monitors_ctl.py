"""MonitorsCtl на фейковой платформе: ничего в ОС не меняется (ни мышь, ни раскладка экранов)."""
import json
import os
import queue
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import _util  # добавляет корень проекта в sys.path
assert _util.ROOT.is_dir()
import deskflow_ctl
import layout as L
import monitors_ctl

FP_PEER = "ab" * 32
FIND_OK = {"ok": True, "code": None, "err": None, "hint": None, "state": "ok", "path": "C:/fake/deskflow-core.exe",
           "version": "1.27.0", "warning": None,
           "dialect": {"ok": True, "form": "subcommand", "settings_flag": "-s", "new_instance_flag": "--new-instance",
                       "decimals": 2}}

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

    def set_client_state(self, state):
        self.client_state = state
        return True

    def status(self):
        return {"running": self.running, "paused": False, "err": None, "warps": 0, "last_warp": None,
                "raw_input": True, "client_state": "local", "tripped": False}


def D_human(fp):
    return ":".join(fp[i:i + 2] for i in range(0, 64, 2)).upper()


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


class FakeDF:
    """Подмена deskflow_ctl.Deskflow: процессы не запускаются."""
    instances = []
    start_result = None      # переопределяется тестом: словарь ошибки
    adopt_result = False

    def __init__(self, path, dialect, data_dir, on_event=None):
        self.path, self.dialect, self.data_dir, self.on_event = path, dialect, data_dir, on_event
        self.calls, self.role, self.alive, self.pending = [], None, False, None
        FakeDF.instances.append(self)

    def start_server(self, conf, name, bind, port, tls, **kw):
        self.calls.append(("server", conf, name, bind, port, tls, kw))
        return self._started("server")

    def start_client(self, name, host, port, tls, **kw):
        self.calls.append(("client", name, host, port, tls, kw))
        return self._started("client")

    def _started(self, role):
        if FakeDF.start_result:
            return dict(FakeDF.start_result)
        self.role, self.alive = role, True
        return {"ok": True, "code": None, "err": None, "pid": 4242, "role": role}

    def stop(self, timeout=3):
        self.calls.append(("stop",))
        self.alive, self.role = False, None
        return {"ok": True}

    def adopt(self, info):
        self.calls.append(("adopt", info))
        if FakeDF.adopt_result:
            self.role, self.alive = info["role"], True
        return FakeDF.adopt_result

    def process_info(self):
        return {"pid": 4242, "image": "deskflow-core.exe", "created": 7, "role": self.role} if self.alive else None

    def status(self):
        return {"running": self.alive, "role": self.role, "pid": 4242 if self.alive else None, "since": "t",
                "connected": [], "last_switch": None, "err": None, "log_tail": ["x"], "pending_peer_fp": self.pending}

    def clear_pending(self, fp=None):
        self.pending = None


class TestMonitorsCtl(unittest.TestCase):
    def setUp(self):
        FakeEdge.instances.clear()
        FakeDF.instances.clear()
        FakeDF.start_result, FakeDF.adopt_result = None, False
        for pt in (mock.patch.object(deskflow_ctl, "Deskflow", FakeDF),
                   mock.patch.object(deskflow_ctl, "find_deskflow", return_value=dict(FIND_OK)),
                   mock.patch.object(deskflow_ctl, "ensure_certificate",
                                     side_effect=lambda d: {"ok": True, "code": None, "err": None,
                                                            "path": str(Path(d) / "deskflow" / "tls" / "deskflow.pem"),
                                                            "created": True, "fp": "00" * 32, "fp_human": "00"}),
                   mock.patch.object(deskflow_ctl, "TRUSTED_DIR_OVERRIDE", None)):
            pt.start()
            self.addCleanup(pt.stop)
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

    def test_modes_validation(self):
        self.assertEqual(self.ctl.set_mode("shared", "a")["code"], "bad_config")    # машина «я» не выбрана
        self.assertEqual(self.ctl.set_mode("monitor")["code"], "not_found")
        self.assertEqual(self.ctl.set_mode("bogus")["code"], "bad_config")
        self.ctl.set_this_machine("a")
        self.assertEqual(self.ctl.set_mode("shared", "zzz")["code"], "bad_config")
        self.assertEqual(self.ctl.set_mode("shared", 5)["code"], "bad_config")
        self.assertTrue(self.ctl.set_mode("off")["pending"])
        self.assertEqual(self.wait_event("mode_changed"), {"mode": "off"})

    def wait_mode(self, mode, timeout=3.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            s = self.ctl.snapshot()
            if s["mode"] == mode and not s["busy"]:
                return s
            time.sleep(0.02)
        self.fail(f"режим не стал {mode}: {self.ctl.snapshot()['mode']}")

    def test_deskflow_component_after_start(self):
        s = self.ctl.snapshot()
        self.assertEqual(s["components"]["deskflow"]["state"], "na")   # ещё не проверяли
        self.ctl.start()
        end = time.monotonic() + 3
        while self.ctl.snapshot()["components"]["deskflow"]["state"] == "na" and time.monotonic() < end:
            time.sleep(0.02)
        c = self.ctl.snapshot()["components"]["deskflow"]
        self.assertEqual((c["state"], c["version"], c["path"]), ("ok", "1.27.0", FIND_OK["path"]))
        for k in ("admin_to_install", "url", "cmd", "err"):
            self.assertIn(k, c)

    def test_shared_server_starts_files_and_state(self):
        self.ctl.set_this_machine("a")
        res = self.ctl.set_mode("shared", "a")
        self.assertTrue(res["pending"])
        self.assertEqual(self.wait_event("mode_changed"), {"mode": "shared"})
        s = self.wait_mode("shared")
        self.assertEqual(s["server"], "a")
        df = FakeDF.instances[0]
        kind, conf, name, bind, port, tls, kw = df.calls[-1]
        self.assertEqual((kind, name, bind, port, tls), ("server", "laptop-a", "192.0.2.11", 24800, True))
        self.assertIn("section: links", conf)
        self.assertNotIn("section: screens", conf)
        self.assertEqual(kw["machines"], ["laptop-a", "laptop-b"])
        self.assertTrue(kw["cert_path"].endswith("deskflow.pem"))
        blk = s["deskflow"]
        for k in ("running", "role", "connected", "since", "fp_local", "pending_peer_fp", "peer_trusted", "err",
                  "log_tail"):
            self.assertIn(k, blk)
        self.assertEqual((blk["running"], blk["role"]), (True, "server"))
        st = json.loads((Path(self.tmp.name) / "data" / "monitors" / "state.json").read_text(encoding="utf-8"))
        self.assertEqual((st["mode"], st["server"], st["deskflow"]["pid"]), ("shared", "a", 4242))

    def test_shared_client_connects_to_server_host(self):
        self.ctl.set_this_machine("b")
        self.ctl.set_mode("shared", "a")
        self.wait_event("mode_changed")
        call = FakeDF.instances[0].calls[-1]
        self.assertEqual(call[:5], ("client", "laptop-b", "192.0.2.11", 24800, True))
        self.assertEqual(call[5]["server_name"], "laptop-a")
        self.assertEqual(self.wait_mode("shared")["deskflow"]["role"], "client")

    def test_server_default_from_layout_and_empty_host_autodetect(self):
        self.ctl.set_this_machine("b")
        self.ctl.set_mode("shared")              # extra.deskflow.server не задан → первая машина (a)
        self.wait_event("mode_changed")
        self.assertEqual(self.wait_mode("shared")["server"], "a")
        raw = json.loads(self.ctl.export_layout())
        raw["machines"][1]["host"] = None
        self.assertTrue(self.ctl.import_layout(json.dumps(raw))["ok"])
        with mock.patch.object(deskflow_ctl, "local_ip_for", return_value="192.0.2.99") as m:
            self.ctl.set_mode("shared", "b")
            self.wait_event("mode_changed")
            self.wait_mode("shared")
        m.assert_called_once_with("192.0.2.11")
        self.assertEqual(FakeDF.instances[0].calls[-1][3], "192.0.2.99")

    def test_edge_helper_client_state(self):
        self.ctl.start()
        self.ctl.set_this_machine("b")
        self.ctl.set_edge_helper(True)
        edge = FakeEdge.instances[0]
        self.ctl.set_mode("shared", "a")
        self.wait_event("mode_changed")
        self.assertEqual(edge.client_state, "client_relative")
        self.ctl.set_mode("off")
        self.wait_event("mode_changed")
        self.assertEqual(edge.client_state, "local")

    def test_off_stops_process_and_clears_state(self):
        self.ctl.set_this_machine("a")
        self.ctl.set_mode("shared", "a")
        self.wait_event("mode_changed")
        self.wait_mode("shared")
        self.ctl.set_mode("off")
        self.assertEqual(self.wait_event("mode_changed"), {"mode": "off"})
        self.assertEqual(FakeDF.instances[0].calls[-1], ("stop",))
        st = json.loads((Path(self.tmp.name) / "data" / "monitors" / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(st["mode"], "off")
        self.assertNotIn("deskflow", st)

    def test_start_failure_reports_and_stays_off(self):
        FakeDF.start_result = {"ok": False, "code": "conflict", "err": "Уже запущен другой Deskflow", "hint": None}
        self.ctl.set_this_machine("a")
        self.ctl.set_mode("shared", "a")
        self.assertEqual(self.wait_event("warning")["text"], "Уже запущен другой Deskflow")
        s = self.wait_mode("off")
        self.assertEqual(s["deskflow"]["err"], "Уже запущен другой Deskflow")
        self.assertFalse(s["deskflow"]["running"])

    def test_deskflow_missing(self):
        miss = {"ok": False, "code": "not_found", "err": "Deskflow не найден", "hint": "winget install", "state": "missing",
                "path": None, "version": None, "dialect": None, "warning": None}
        with mock.patch.object(deskflow_ctl, "find_deskflow", return_value=miss):
            self.ctl.set_this_machine("a")
            self.ctl.set_mode("shared", "a")
            self.assertEqual(self.wait_event("warning")["text"], "Deskflow не найден")
            self.wait_mode("off")
            c = self.ctl.snapshot()["components"]["deskflow"]
            self.assertEqual((c["state"], c["err"]), ("missing", "Deskflow не найден"))
            self.assertEqual(FakeDF.instances, [])

    def test_busy_rejects_second_request(self):
        self.ctl.set_this_machine("a")
        with self.ctl._lock:
            self.ctl._busy = True
        self.assertEqual(self.ctl.set_mode("shared", "a")["code"], "busy")
        self.assertEqual(self.ctl.set_mode("off")["code"], "busy")

    def test_events_from_deskflow_pass_through(self):
        self.ctl.set_this_machine("a")
        self.ctl.set_mode("shared", "a")
        self.wait_event("mode_changed")
        df = FakeDF.instances[0]
        df.pending = {"fp": FP_PEER, "at": "t"}
        df.on_event("deskflow_untrusted_peer", {"fp": FP_PEER, "role": "server"})
        self.assertEqual(self.wait_event("deskflow_untrusted_peer")["fp"], FP_PEER)
        s = self.ctl.snapshot()
        self.assertEqual(s["deskflow"]["pending_peer_fp"]["fp"], FP_PEER)
        self.assertIs(s["deskflow"]["peer_trusted"], False)
        df.on_event("deskflow_connected", {"name": "laptop-b"})
        self.assertEqual(self.wait_event("deskflow_connected"), {"name": "laptop-b"})
        self.wait_event("snapshot_changed")

    def test_unexpected_exit_turns_mode_off(self):
        self.ctl.set_this_machine("a")
        self.ctl.set_mode("shared", "a")
        self.wait_event("mode_changed")
        df = FakeDF.instances[0]
        df.alive = False
        df.on_event("deskflow_exited", {"code": 1})
        self.assertIn("Deskflow остановился", self.wait_event("warning")["text"])
        self.assertEqual(self.ctl.snapshot()["mode"], "off")

    def test_trust_peer_role_and_files(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.object(deskflow_ctl, "TRUSTED_DIR_OVERRIDE", td):
            self.ctl.set_this_machine("a")          # a — сервер по умолчанию → trusted-clients
            r = self.ctl.trust_peer("b", D_human(FP_PEER))
            self.assertTrue(r["ok"], r)
            self.assertEqual((r["role"], r["added"]), ("server", True))
            self.assertEqual((Path(td) / "trusted-clients").read_text(encoding="utf-8"), f"v2:sha256:{FP_PEER}\n")
            self.assertFalse(self.ctl.trust_peer("b", FP_PEER)["added"])
            self.ctl.set_this_machine("b")          # b — клиент → trusted-servers
            r = self.ctl.trust_peer("a", "SHA256:" + FP_PEER.upper())
            self.assertEqual(r["role"], "client")
            self.assertTrue((Path(td) / "trusted-servers").is_file())
            for bad in (("zzz", FP_PEER), ("b", FP_PEER), ("a", "12:34"), ("a", None), (None, FP_PEER)):
                self.assertFalse(self.ctl.trust_peer(*bad)["ok"], bad)

    def test_trust_peer_uses_running_role_and_clears_pending(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.object(deskflow_ctl, "TRUSTED_DIR_OVERRIDE", td):
            self.ctl.set_this_machine("a")
            self.ctl.set_mode("shared", "b")        # a — клиент в этом режиме
            self.wait_event("mode_changed")
            FakeDF.instances[0].pending = {"fp": FP_PEER, "at": "t"}
            r = self.ctl.trust_peer("b", FP_PEER)
            self.assertEqual(r["role"], "client")
            self.assertIsNone(FakeDF.instances[0].pending)

    def test_preview_fills_argv_and_conf(self):
        self.ctl.start()
        self.ctl.set_this_machine("a")
        end = time.monotonic() + 3
        while self.ctl._find is None and time.monotonic() < end:
            time.sleep(0.02)
        pv = self.ctl.preview()
        self.assertTrue(pv["ok"], pv)
        self.assertIn("section: links", pv["deskflow_conf"])
        self.assertEqual(pv["argv"][1:3], ["server", "-s"])
        self.assertTrue(pv["argv"][3].replace("\\", "/").endswith("monitors/deskflow/settings.conf"))
        self.assertIn("interface=192.0.2.11", pv["deskflow_settings"])
        self.ctl.set_this_machine("b")
        self.assertEqual(self.ctl.preview()["argv"][1], "client")

    def test_preview_without_deskflow_has_conf_but_no_argv(self):
        self.ctl.set_this_machine("a")
        pv = self.ctl.preview()
        self.assertIn("section: links", pv["deskflow_conf"])
        self.assertIsNone(pv["argv"])
        self.assertTrue(any("Deskflow" in w for w in pv["warnings"]))

    def test_export_for_gui_has_conf(self):
        r = self.ctl.export_for_deskflow_gui()
        self.assertTrue(r["ok"])
        self.assertIn("section: links", r["server_conf"])
        self.assertEqual((r["server_host"], r["port"]), ("192.0.2.11", 24800))

    def test_restore_adopts_live_process(self):
        self._write_state({"mode": "shared", "server": "a", "this_machine": "a",
                           "deskflow": {"pid": 4242, "image": "deskflow-core.exe", "created": 7, "role": "server"}})
        FakeDF.adopt_result = True
        ctl = self._new_ctl()
        ctl.start()
        self.assertEqual(self.wait_event("mode_changed"), {"mode": "shared"})
        df = FakeDF.instances[0]
        self.assertEqual([c[0] for c in df.calls], ["adopt"])    # заново не запускали
        self.assertEqual(ctl.snapshot()["deskflow"]["running"], True)
        ctl.stop()

    def test_restore_restarts_when_process_gone(self):
        self._write_state({"mode": "shared", "server": "a", "this_machine": "a",
                           "deskflow": {"pid": 4242, "image": "deskflow-core.exe", "created": 7, "role": "server"}})
        ctl = self._new_ctl()
        ctl.start()
        self.assertEqual(self.wait_event("mode_changed"), {"mode": "shared"})
        self.assertEqual([c[0] for c in FakeDF.instances[0].calls][:2], ["adopt", "stop"])
        self.assertEqual(FakeDF.instances[0].calls[-1][0], "server")
        ctl.stop()

    def test_restore_respects_startup_mode_off(self):
        self._write_state({"mode": "shared", "server": "a", "this_machine": "a"})
        raw = json.loads(json.dumps(L.EXAMPLE))
        raw["startup_mode"] = "off"
        ctl = self._new_ctl(raw)
        ctl.start()
        self.wait_event("snapshot_changed")
        time.sleep(0.2)
        self.assertEqual(ctl.snapshot()["mode"], "off")
        self.assertEqual([c for df in FakeDF.instances for c in df.calls if c[0] in ("server", "client")], [])
        ctl.stop()

    def test_stop_on_exit_stops_process(self):
        raw = json.loads(json.dumps(L.EXAMPLE))
        raw["stop_on_exit"] = True
        ctl = self._new_ctl(raw)
        ctl.set_this_machine("a")
        ctl.set_mode("shared", "a")
        self.wait_event("mode_changed")
        ctl.stop()
        self.assertEqual(FakeDF.instances[0].calls[-1], ("stop",))

    def test_stop_default_leaves_process_running(self):
        self.ctl.set_this_machine("a")
        self.ctl.set_mode("shared", "a")
        self.wait_event("mode_changed")
        self.ctl.stop()
        self.assertTrue(FakeDF.instances[0].alive)
        st = json.loads((Path(self.tmp.name) / "data" / "monitors" / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(st["deskflow"]["pid"], 4242)    # для перенятия при следующем запуске

    def _write_state(self, st):
        d = Path(self.tmp.name) / "data2" / "monitors"
        d.mkdir(parents=True, exist_ok=True)
        (d / "state.json").write_text(json.dumps(st), encoding="utf-8")

    def _new_ctl(self, raw=None):
        raw = raw or json.loads(json.dumps(L.EXAMPLE))
        ctl = monitors_ctl.MonitorsCtl(raw, self.cfg, Path(self.tmp.name) / "data2", self.plat,
                                       on_event=lambda k, p: self.events.put((k, p)))
        return ctl

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
