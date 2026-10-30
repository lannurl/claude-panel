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
import peer_link
import stream_ctl

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
        self.displays = [dict(d, rect_px=list(d["rect_px"])) for d in DISPLAYS]
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


APOLLO_OK = {"ok": True, "code": None, "err": None, "hint": None, "state": "ok", "path": "C:/fake/sunshine.exe",
             "version": "0.4.6.0", "running": True, "web_ui": "https://localhost:47990"}
APOLLO_MISSING = {"ok": False, "code": "not_found", "err": "Apollo не найден", "hint": "winget install apollo",
                  "state": "missing", "path": None, "version": None, "running": False, "web_ui": "https://localhost:47990"}
MOON_OK = {"ok": True, "code": None, "err": None, "hint": None, "state": "ok", "path": "C:/fake/Moonlight.exe",
           "version": "6.2.0.0", "warning": None, "dialect": {}}
MOON_MISSING = {"ok": False, "code": "not_found", "err": "Moonlight не найден", "hint": "winget install moonlight",
                "state": "missing", "path": None, "version": None, "warning": None, "dialect": None}
VD = {"os_id": "win:SUDOVDA:1", "gdi_name": r"\\.\DISPLAY3", "label": "SudoMaker Virtual Display", "px": [1920, 1080],
      "rect_px": [4480, 0, 1920, 1080], "primary": False, "scale": 100, "size_mm": None, "virtual": True}


class FakeMoon:
    """Подмена stream_ctl.Moonlight: процессы не запускаются."""
    instances = []
    start_result = None
    adopt_result = False

    def __init__(self, path, dialect, data_dir, on_event=None, runner=None, popen=None):
        self.path, self.on_event = path, on_event
        self.calls, self.alive = [], False
        FakeMoon.instances.append(self)

    def start(self, host, app, resolution=None, fps=60):
        self.calls.append(("start", host, app, resolution, fps))
        if FakeMoon.start_result:
            return dict(FakeMoon.start_result)
        self.alive = True
        return {"ok": True, "code": None, "err": None, "pid": 777}

    def stop(self, host):
        self.calls.append(("stop", host))
        self.alive = False
        return {"ok": True, "stopped": True}

    def status(self):
        return {"running": self.alive, "pid": 777 if self.alive else None, "since": "t", "err": "x", "host": None,
                "app": None}

    def adopt(self, info):
        self.calls.append(("adopt", info))
        self.alive = bool(FakeMoon.adopt_result)
        return FakeMoon.adopt_result

    def process_info(self):
        return {"pid": 777, "image": "moonlight.exe", "created": 9, "since": "t", "host": "h", "app": "Monitor"} \
            if self.alive else None


class FakeLink:
    """Подмена peer_link.PeerLink: ничего не слушает, запросы пишет в requests."""
    instances = []
    reply = {"ok": True, "code": None, "err": None}

    def __init__(self, secret, self_id, bind_host, port, peers, handler, **kw):
        self.args = (secret, self_id, bind_host, port, peers)
        self.handler, self.kw, self.requests, self.stopped = handler, kw, [], False
        FakeLink.instances.append(self)

    def start(self):
        return {"ok": True, "code": None, "err": None}

    def stop(self):
        self.stopped = True

    def running(self):
        return not self.stopped

    def status(self):
        return {"running": True, "err": None, "last_seen": {"b": 1_793_280_000}}

    def request(self, peer, cmd, args=None, timeout=3.0):
        self.requests.append((peer, cmd, dict(args or {})))
        if cmd == "ping":
            return {"ok": True, "code": None, "err": None}
        return dict(FakeLink.reply)


class TestMonitorsCtl(unittest.TestCase):
    def setUp(self):
        FakeEdge.instances.clear()
        FakeDF.instances.clear()
        FakeMoon.instances.clear()
        FakeLink.instances.clear()
        FakeDF.start_result, FakeDF.adopt_result = None, False
        FakeMoon.start_result, FakeMoon.adopt_result = None, False
        FakeLink.reply = {"ok": True, "code": None, "err": None}
        for pt in (mock.patch.object(deskflow_ctl, "Deskflow", FakeDF),
                   mock.patch.object(deskflow_ctl, "find_deskflow", return_value=dict(FIND_OK)),
                   mock.patch.object(deskflow_ctl, "ensure_certificate",
                                     side_effect=lambda d: {"ok": True, "code": None, "err": None,
                                                            "path": str(Path(d) / "deskflow" / "tls" / "deskflow.pem"),
                                                            "created": True, "fp": "00" * 32, "fp_human": "00"}),
                   mock.patch.object(deskflow_ctl, "TRUSTED_DIR_OVERRIDE", None),
                   mock.patch.object(deskflow_ctl, "_restrict_key_file"),
                   mock.patch.object(stream_ctl, "find_host_app", return_value=dict(APOLLO_OK)),
                   mock.patch.object(stream_ctl, "find_moonlight", return_value=dict(MOON_OK)),
                   mock.patch.object(stream_ctl, "Moonlight", FakeMoon),
                   mock.patch.object(stream_ctl, "moonlight_pids", return_value=[]),
                   mock.patch.object(peer_link, "PeerLink", FakeLink)):
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
        self.raw = raw
        self.ctl = monitors_ctl.MonitorsCtl(raw, self.cfg, Path(self.tmp.name) / "data", self.plat,
                                            on_event=lambda k, p: self.events.put((k, p)))
        self.ctl.VD_POLL_S = 0.05
        self.ctl.LINK_PING_S = 0.1

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
        self.assertEqual(self.ctl.set_mode("monitor")["code"], "bad_config")    # то же: машина «я» не выбрана
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

    # ══ Этап 3: «Ноут как монитор» ══
    def wait_until(self, pred, timeout=3.0, what="условие"):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if pred():
                return
            time.sleep(0.02)
        self.fail(f"не дождались: {what}: {self.ctl.snapshot()}")

    def enter_monitor(self, me="a"):
        self.ctl.start()
        self.ctl.set_this_machine(me)
        self.wait_until(lambda: self.ctl.snapshot()["components"]["apollo"]["state"] != "na"
                        or self.ctl.snapshot()["components"]["moonlight"]["state"] != "na", what="find")
        self.wait_until(lambda: not self.ctl.snapshot().get("busy"), what="idle")
        r = self.ctl.set_mode("monitor")
        self.assertTrue(r["ok"], r)
        return self.wait_mode("monitor")

    def link_up(self, me="a"):
        self.ctl.start()
        self.ctl.set_this_machine(me)
        r = self.ctl.link_new_secret()
        self.assertTrue(r["ok"], r)
        self.wait_until(lambda: bool(FakeLink.instances), what="связка поднята")
        return FakeLink.instances[-1]

    def test_monitor_host_stops_deskflow_checks_apollo_and_waits(self):
        self.ctl.set_this_machine("a")
        self.ctl.set_mode("shared", "a")
        self.wait_mode("shared")
        s = self.enter_monitor("a")
        self.assertIn(("stop",), FakeDF.instances[0].calls[1:])           # Deskflow остановлен (инвариант б)
        self.assertEqual(s["prev_mode"], "shared")
        st = s["stream"]
        self.assertEqual((st["role"], st["running"], st["waiting"], st["virtual_display"], st["positioned"]),
                         ("host", False, True, None, False))
        self.assertIn("laptop-b", st["hint"])
        self.assertEqual(s["components"]["apollo"]["state"], "ok")
        self.assertTrue(s["components"]["apollo"]["running"])
        self.assertEqual(s["components"]["moonlight"]["state"], "na")
        self.assertIn("laptop-b", s["components"]["moonlight"]["note"])
        self.assertFalse(s["deskflow"]["running"])
        self.assertEqual(FakeMoon.instances, [])                          # на хосте Moonlight не нужен
        stt = json.loads((Path(self.tmp.name) / "data" / "monitors" / "state.json").read_text(encoding="utf-8"))
        self.assertEqual((stt["mode"], stt["prev_mode"], stt["prev_server"]), ("monitor", "shared", "a"))

    def test_monitor_host_apollo_missing_stays_put(self):
        self.ctl.start()
        self.ctl.set_this_machine("a")
        with mock.patch.object(stream_ctl, "find_host_app", return_value=dict(APOLLO_MISSING)):
            self.assertTrue(self.ctl.set_mode("monitor")["ok"])
            w = self.wait_event("warning")
            self.wait_until(lambda: not self.ctl.snapshot()["busy"], what="busy off")
        self.assertIn("Apollo", w["text"])
        s = self.ctl.snapshot()
        self.assertEqual(s["mode"], "off")
        self.assertEqual(s["components"]["apollo"]["state"], "missing")
        self.assertIn("Apollo", s["stream"]["err"])

    def test_monitor_host_apollo_port_closed_warns_but_waits(self):
        self.ctl.start()
        self.ctl.set_this_machine("a")
        with mock.patch.object(stream_ctl, "find_host_app", return_value={**APOLLO_OK, "running": False}):
            self.ctl.set_mode("monitor")
            s = self.wait_mode("monitor")
        self.assertIn("47990", s["stream"]["err"])

    def test_vd_placed_by_scheme_and_rules_rebuilt(self):
        self.plat.displays[1]["rect_px"] = [1920, -868, 2560, 1440]      # Windows уже по физике (§7.6)
        self.enter_monitor("a")
        self.ctl.set_edge_helper(True)
        edge = FakeEdge.instances[0]
        self.assertNotIn("b.1", edge.rects)
        self.plat.displays.append(dict(VD, rect_px=[4480, 1500, 1920, 1080]))   # появился где попало
        self.wait_until(lambda: self.ctl.snapshot()["stream"]["positioned"], what="VD на месте")
        pos = [c for c in self.plat.calls if not c[1] and VD["os_id"] in c[0]]
        self.assertEqual(pos[-1][0], {VD["os_id"]: (4480, 49)})          # эталон §7.6: VD = (4480, 49)
        self.assertEqual(len(pos[-1][0]), 1)                              # двигали только VD, основной не трогали
        s = self.ctl.snapshot()
        self.assertEqual((s["stream"]["running"], s["stream"]["virtual_display"], s["stream"]["err"]),
                         (True, "SudoMaker Virtual Display", None))
        self.assertIsNone(s["stream"]["hint"])
        self.wait_until(lambda: "b.1" in edge.rects, what="правила пересчитаны")
        self.assertEqual(edge.rects["b.1"], (4480, 49, 1920, 1080))
        self.assertTrue(any(r.src == "a.2" and r.dst == "b.1" and r.side == "right" for r in edge.rules))
        self.assertTrue(any(r.src == "b.1" and r.dst == "a.2" and r.side == "left" for r in edge.rules))

    def test_vd_already_in_place_is_not_moved(self):
        self.plat.displays[1]["rect_px"] = [1920, -868, 2560, 1440]
        self.plat.displays.append(dict(VD, rect_px=[4480, 49, 1920, 1080]))
        self.enter_monitor("a")
        self.wait_until(lambda: self.ctl.snapshot()["stream"]["positioned"], what="VD positioned")
        self.assertEqual([c for c in self.plat.calls if not c[1]], [])

    def test_vd_follows_actual_neighbor_position(self):
        """Монитор M стоит в Windows не по физике (y = 0): VD привязывается к реальному положению M."""
        self.enter_monitor("a")
        self.plat.displays.append(dict(VD, rect_px=[4480, 1500, 1920, 1080]))
        self.wait_until(lambda: self.ctl.snapshot()["stream"]["positioned"], what="VD positioned")
        pos = [c for c in self.plat.calls if not c[1] and VD["os_id"] in c[0]]
        self.assertEqual(pos[-1][0][VD["os_id"]], (4480, 917))            # 49 + 868: смещение от реального y M

    def test_vd_failure_rolls_back_and_does_not_loop(self):
        self.plat.displays[1]["rect_px"] = [1920, -868, 2560, 1440]
        calls = []

        def failing(pos, dry_run=False):
            calls.append(dict(pos))
            return {"ok": False, "code": "os_error", "err": "Windows отклонила раскладку",
                    "previous": {VD["os_id"]: (4480, 1500)}}
        self.plat.set_display_positions = failing
        self.enter_monitor("a")
        self.plat.displays.append(dict(VD, rect_px=[4480, 1500, 1920, 1080]))
        self.wait_until(lambda: self.ctl.snapshot()["stream"]["err"], what="ошибка VD")
        self.assertIn("отклонила", self.ctl.snapshot()["stream"]["err"])
        self.assertFalse(self.ctl.snapshot()["stream"]["positioned"])
        self.assertEqual(calls, [{VD["os_id"]: (4480, 49)}, {VD["os_id"]: (4480, 1500)}])   # попытка + откат
        n = len(calls)
        self.ctl._on_displays_changed(0)
        self.ctl._on_displays_changed(0)
        time.sleep(0.2)
        self.assertEqual(len(calls), n)                                   # той же неудачной позиции не повторяем

    def test_vd_primary_is_not_touched(self):
        self.enter_monitor("a")
        self.plat.displays.append(dict(VD, primary=True, rect_px=[4480, 1500, 1920, 1080]))
        self.wait_until(lambda: self.ctl.snapshot()["stream"]["err"], what="ошибка")
        self.assertIn("основным", self.ctl.snapshot()["stream"]["err"])
        self.assertEqual([c for c in self.plat.calls if not c[1]], [])

    def test_vd_disappears_when_stream_ends(self):
        self.plat.displays[1]["rect_px"] = [1920, -868, 2560, 1440]
        self.plat.displays.append(dict(VD, rect_px=[4480, 49, 1920, 1080]))
        self.enter_monitor("a")
        self.ctl.set_edge_helper(True)
        edge = FakeEdge.instances[0]
        self.wait_until(lambda: "b.1" in edge.rects, what="правила с VD")
        del self.plat.displays[2]
        self.wait_until(lambda: not self.ctl.snapshot()["stream"]["running"], what="VD исчез")
        s = self.ctl.snapshot()
        self.assertEqual((s["mode"], s["stream"]["waiting"], s["stream"]["positioned"]), ("monitor", True, False))
        self.wait_until(lambda: "b.1" not in edge.rects, what="правила без VD")

    def test_vd_recognised_by_sudomaker_even_with_old_config(self):
        """В личном config.json virtual_match без SudoMaker — всё равно распознаём."""
        self.assertIn("SudoMaker", self.ctl._vmatch())
        self.raw["stream"] = {"virtual_match": ["SudoVDA"]}
        ctl = monitors_ctl.MonitorsCtl(self.raw, self.cfg, Path(self.tmp.name) / "d2", self.plat)
        self.assertEqual(ctl._vmatch()[0], "SudoVDA")
        self.assertIn("SudoMaker", ctl._vmatch())

    def test_vd_screen_defaults_to_client_machine_screen(self):
        self.ctl.set_this_machine("a")
        self.assertEqual(self.ctl._virtual_screen_id(), "b.1")

    def test_monitor_client_starts_moonlight_with_layout_resolution(self):
        self.ctl.set_this_machine("b")
        self.ctl.set_mode("shared", "a")
        self.wait_mode("shared")
        s = self.enter_monitor("b")
        self.assertIn(("stop",), FakeDF.instances[0].calls[1:])
        moon = FakeMoon.instances[0]
        self.assertEqual(moon.path, "C:/fake/Moonlight.exe")
        self.assertEqual(moon.calls[0], ("start", "192.0.2.11", "Monitor", (1920, 1080), 60))
        st = s["stream"]
        self.assertEqual((st["role"], st["running"], st["waiting"]), ("client", True, False))
        self.assertEqual(s["components"]["moonlight"]["state"], "ok")
        self.assertEqual(s["components"]["apollo"]["state"], "na")
        self.assertIn("laptop-a", s["components"]["apollo"]["note"])
        self.assertEqual(s["prev_mode"], "shared")
        stt = json.loads((Path(self.tmp.name) / "data" / "monitors" / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(stt["moonlight"]["pid"], 777)

    def test_monitor_client_moonlight_missing(self):
        self.ctl.start()
        self.ctl.set_this_machine("b")
        with mock.patch.object(stream_ctl, "find_moonlight", return_value=dict(MOON_MISSING)):
            self.assertTrue(self.ctl.set_mode("monitor")["ok"])
            w = self.wait_event("warning")
            self.wait_until(lambda: not self.ctl.snapshot()["busy"], what="busy off")
        self.assertIn("Moonlight", w["text"])
        self.assertEqual(self.ctl.snapshot()["mode"], "off")
        self.assertEqual(self.ctl.snapshot()["components"]["moonlight"]["state"], "missing")
        self.assertEqual(FakeMoon.instances, [])

    def test_monitor_client_start_failure(self):
        FakeMoon.start_result = {"ok": False, "code": "exited", "err": "Moonlight сразу завершился (код 1)"}
        self.ctl.start()
        self.ctl.set_this_machine("b")
        self.ctl.set_mode("monitor")
        self.wait_event("warning")
        s = self.wait_mode("off")
        self.assertIn("сразу", s["stream"]["err"])

    def test_conflict_when_deskflow_would_survive(self):
        self.ctl.set_this_machine("b")
        self.ctl.set_mode("shared", "a")
        self.wait_mode("shared")
        df = FakeDF.instances[0]
        df.stop = lambda timeout=3: {"ok": True}          # Deskflow не остановился
        self.ctl.set_mode("monitor")
        self.assertIn("Deskflow", self.wait_event("warning")["text"])
        self.assertEqual(FakeMoon.instances[0].calls, [])
        self.wait_until(lambda: not self.ctl.snapshot()["busy"], what="busy off")

    def test_shared_refused_while_moonlight_runs(self):
        self.ctl.set_this_machine("b")
        self.ctl.start()
        moon = self.ctl._ensure_moon() or None
        self.ctl._do_find_stream(force=True)
        moon = self.ctl._ensure_moon()
        moon.alive = True
        self.ctl.set_mode("shared", "a")
        self.assertIn("Moonlight", self.wait_event("warning")["text"])
        self.assertEqual(self.ctl.snapshot()["mode"], "off")
        self.assertEqual(FakeDF.instances[0].calls if FakeDF.instances else [], [])

    def test_client_leaves_monitor_for_off_and_shared(self):
        self.enter_monitor("b")
        moon = FakeMoon.instances[0]
        self.ctl.set_mode("off")
        self.wait_mode("off")
        self.assertEqual(moon.calls[-1], ("stop", "192.0.2.11"))          # quit хосту + завершение своего процесса
        self.enter_monitor("b")
        self.ctl.set_mode("shared", "a")
        self.wait_mode("shared")
        self.assertEqual(moon.calls[-1], ("stop", "192.0.2.11"))
        self.assertEqual(FakeDF.instances[0].calls[-1][0], "client")
        self.assertEqual(self.ctl.snapshot()["server"], "a")

    def test_off_closes_stray_moonlight_on_client(self):
        self.ctl.set_this_machine("b") if hasattr(self.ctl, "set_this_machine") else None
        with mock.patch.object(stream_ctl, "moonlight_pids", side_effect=[[4242], [4242]]),                 mock.patch.object(deskflow_ctl, "_kill_pid") as kill:
            self.ctl._this = "b"
            self.ctl._stop_stray_stream()
        kill.assert_called_once_with(4242, False)

    def test_leave_monitor_restores_previous_mode(self):
        self.ctl.set_this_machine("b")
        self.ctl.set_mode("shared", "a")
        self.wait_mode("shared")
        self.enter_monitor("b")
        self.assertTrue(self.ctl.leave_monitor()["pending"])
        s = self.wait_mode("shared")
        self.assertEqual(s["server"], "a")
        self.assertTrue(s["deskflow"]["running"])
        self.assertFalse(FakeMoon.instances[0].alive)
        self.assertFalse(self.ctl.leave_monitor()["pending"])             # уже не в monitor

    def test_host_leaves_monitor_for_shared_without_stopping_anything_else(self):
        self.enter_monitor("a")
        self.ctl.set_mode("shared", "a")
        s = self.wait_mode("shared")
        self.assertEqual(s["stream"]["role"], "host")
        self.assertEqual(FakeDF.instances[0].calls[-1][0], "server")
        self.assertFalse(s["stream"]["waiting"])

    def test_moonlight_exit_turns_mode_off(self):
        self.enter_monitor("b")
        moon = FakeMoon.instances[0]
        moon.alive = False
        moon.on_event("moonlight_exited", {"code": 0})
        self.assertIn("Moonlight остановился", self.wait_event("warning")["text"])
        self.assertEqual(self.ctl.snapshot()["mode"], "off")

    def test_role_falls_back_to_apollo_when_machine_not_configured_host(self):
        self.raw["stream"] = {"host_machine": "b"}
        ctl = monitors_ctl.MonitorsCtl(self.raw, self.cfg, Path(self.tmp.name) / "d3", self.plat)
        ctl.set_this_machine("a")
        self.assertEqual(ctl._role(), "client")                           # по config хост — b
        self.assertEqual(ctl._role(), "client")
        ctl._apollo_find, ctl._moon_find = dict(APOLLO_OK), dict(MOON_MISSING)
        self.assertEqual(ctl._role(), "client")                           # config однозначен — Apollo не перебивает
        ctl._host_id = lambda: None                                       # хост в config не определён
        self.assertEqual(ctl._role(), "host")                             # тогда — та, где найден Apollo
        ctl._moon_find = dict(MOON_OK)
        self.assertEqual(ctl._role(), "client")

    def test_restore_monitor_client_adopts_moonlight(self):
        FakeMoon.adopt_result = True
        d = Path(self.tmp.name) / "data" / "monitors"
        d.mkdir(parents=True)
        (d / "state.json").write_text(json.dumps({"mode": "monitor", "this_machine": "b",
                                                  "moonlight": {"pid": 777, "image": "moonlight.exe", "created": 9}}),
                                      encoding="utf-8")
        ctl = monitors_ctl.MonitorsCtl(self.raw, self.cfg, Path(self.tmp.name) / "data", self.plat,
                                       on_event=lambda k, p: self.events.put((k, p)))
        self.addCleanup(ctl.stop)
        ctl.start()
        self.assertEqual(self.wait_event("mode_changed"), {"mode": "monitor"})
        self.assertEqual(FakeMoon.instances[0].calls[0][0], "adopt")
        self.assertEqual(len([c for c in FakeMoon.instances[0].calls if c[0] == "start"]), 0)
        self.assertTrue(ctl.snapshot()["stream"]["running"])

    def test_restore_monitor_host_reenters_without_notifying_peer(self):
        d = Path(self.tmp.name) / "data" / "monitors"
        d.mkdir(parents=True)
        (d / "state.json").write_text(json.dumps({"mode": "monitor", "this_machine": "a"}), encoding="utf-8")
        ctl = monitors_ctl.MonitorsCtl(self.raw, self.cfg, Path(self.tmp.name) / "data", self.plat,
                                       on_event=lambda k, p: self.events.put((k, p)))
        self.addCleanup(ctl.stop)
        ctl.start()
        self.assertEqual(self.wait_event("mode_changed"), {"mode": "monitor"})
        self.assertEqual(ctl.snapshot()["stream"]["role"], "host")
        self.assertEqual([r for lk in FakeLink.instances for r in lk.requests if r[1] == "set_mode"], [])

    def test_snapshot_stream_and_link_blocks_have_contract_keys(self):
        self.ctl.start()
        s = self.ctl.snapshot()
        for k in ("running", "role", "virtual_display", "positioned", "err", "hint", "waiting"):
            self.assertIn(k, s["stream"])
        for k in ("enabled", "paired", "peer_online", "last_seen", "err"):
            self.assertIn(k, s["link"])
        self.assertEqual((s["link"]["enabled"], s["link"]["paired"], s["link"]["peer_online"]), (False, False, None))
        self.assertIsNone(s["stream"]["role"])        # машина не выбрана

    # ══ Этап 4: связка ══
    def test_link_new_secret_saves_key_privately_and_enables(self):
        self.ctl.start()
        self.ctl.set_this_machine("a")
        r = self.ctl.link_new_secret()
        self.assertTrue(r["ok"])
        self.assertRegex(r["code"], r"^([A-Z2-7]{4}-){7}[A-Z2-7]{4}$")
        key = Path(self.tmp.name) / "data" / "monitors" / "link.key"
        self.assertEqual(peer_link.secret_from_code(key.read_text(encoding="utf-8")), peer_link.secret_from_code(r["code"]))
        cfg_text = self.cfg.read_text(encoding="utf-8")
        self.assertNotIn(r["code"], cfg_text)                              # секрет не в config.json
        self.assertTrue(json.loads(cfg_text)["monitors"]["link"]["enabled"])
        self.wait_until(lambda: bool(FakeLink.instances), what="связка поднята")
        lk = FakeLink.instances[-1]
        self.assertEqual(lk.args[1:], (peer_link.secret_from_code(r["code"]) and "a", "192.0.2.11", 24850,
                                       {"b": "192.0.2.12"}))
        self.assertEqual(lk.args[0], peer_link.secret_from_code(r["code"]))
        self.assertFalse(lk.kw["allow_loopback"])
        self.wait_until(lambda: self.ctl.snapshot()["link"]["peer_online"] is True, what="пир на связи")
        l = self.ctl.snapshot()["link"]
        self.assertEqual((l["enabled"], l["paired"], l["err"]), (True, True, None))
        self.assertTrue(l["last_seen"])
        self.ctl.stop()
        self.assertTrue(lk.stopped)

    def test_link_set_secret_validates(self):
        self.ctl.start()
        self.ctl.set_this_machine("b")
        for bad in ("", "ABCD", None, "1111-1111-1111-1111-1111-1111-1111-1111"):
            self.assertEqual(self.ctl.link_set_secret(bad)["code"], "bad_config")
        self.assertEqual(FakeLink.instances, [])
        raw, code = peer_link.new_secret()
        self.assertTrue(self.ctl.link_set_secret(code.lower())["ok"])
        self.wait_until(lambda: bool(FakeLink.instances), what="связка поднята")
        self.assertEqual(FakeLink.instances[-1].args[0], raw)
        self.assertEqual(FakeLink.instances[-1].args[1:3], ("b", "192.0.2.12"))

    def test_link_secret_survives_restart(self):
        self.ctl.start()
        self.ctl.set_this_machine("a")
        self.ctl.link_new_secret()
        self.ctl.stop()
        FakeLink.instances.clear()
        raw = json.loads(self.cfg.read_text(encoding="utf-8"))["monitors"]
        ctl2 = monitors_ctl.MonitorsCtl(raw, self.cfg, Path(self.tmp.name) / "data", self.plat)
        self.addCleanup(ctl2.stop)
        self.assertTrue(ctl2.snapshot()["link"]["paired"])
        ctl2.start()
        self.assertEqual(ctl2.snapshot()["this_machine"], "a")
        end = time.monotonic() + 3
        while not FakeLink.instances and time.monotonic() < end:
            time.sleep(0.02)
        self.assertTrue(FakeLink.instances)

    def test_link_not_started_without_peer_address(self):
        raw = json.loads(json.dumps(self.raw))
        raw["machines"][1]["host"] = None
        ctl = monitors_ctl.MonitorsCtl(raw, self.cfg, Path(self.tmp.name) / "d4", self.plat)
        self.addCleanup(ctl.stop)
        ctl.start()
        ctl.set_this_machine("a")
        self.assertTrue(ctl.link_new_secret()["ok"])
        self.wait_until(lambda: ctl.snapshot()["link"]["err"], what="ошибка связки")
        self.assertIn("адрес", ctl.snapshot()["link"]["err"])
        self.assertEqual(FakeLink.instances, [])

    def test_monitor_on_host_commands_peer_to_follow(self):
        lk = self.link_up("a")
        self.enter_monitor("a")
        self.wait_until(lambda: any(r[1] == "set_mode" for r in lk.requests), what="команда пиру")
        self.assertIn(("b", "set_mode", {"mode": "monitor"}), lk.requests)
        s = self.ctl.snapshot()
        self.assertNotIn("нажмите", s["stream"]["hint"] or "")              # пир ответил ok — «нажмите» не нужно
        self.assertTrue(s["link"]["peer_online"])

    def test_monitor_on_client_commands_host_too(self):
        lk = self.link_up("b")
        self.enter_monitor("b")
        self.wait_until(lambda: any(r[1] == "set_mode" for r in lk.requests), what="команда хосту")
        self.assertIn(("a", "set_mode", {"mode": "monitor"}), lk.requests)

    def test_peer_offline_still_enters_mode_and_shows_hint(self):
        lk = self.link_up("a")
        FakeLink.reply = {"ok": False, "code": "peer_unreachable", "err": "Другая машина не отвечает"}
        self.enter_monitor("a")
        self.wait_until(lambda: self.ctl.snapshot()["stream"]["hint"], what="подсказка")
        s = self.ctl.snapshot()
        self.assertEqual(s["mode"], "monitor")
        self.assertIn("laptop-b", s["stream"]["hint"])
        self.assertIn("Ноут как монитор", s["stream"]["hint"])
        self.assertFalse(s["link"]["peer_online"])
        self.assertIn("не отвечает", self.wait_event("warning")["text"])

    def test_no_link_gives_manual_hint(self):
        s = self.enter_monitor("a")
        self.assertIn("laptop-b", s["stream"]["hint"])
        self.assertFalse(s["link"]["paired"])
        self.assertEqual(FakeLink.instances, [])

    def test_leaving_commands_peer_but_incoming_request_does_not_echo(self):
        lk = self.link_up("a")
        self.enter_monitor("a")
        self.ctl.set_mode("off")
        self.wait_mode("off")
        self.wait_until(lambda: ("b", "set_mode", {"mode": "off"}) in lk.requests, what="off пиру")
        n = len([r for r in lk.requests if r[1] == "set_mode"])
        self.ctl.set_mode("monitor")
        self.wait_mode("monitor")
        # входящая команда выполняется, но обратно не отправляется
        before = len([r for r in lk.requests if r[1] == "set_mode"])
        res = lk.handler("b", "set_mode", {"mode": "off"})
        self.assertTrue(res["ok"] and res["pending"], res)
        self.wait_mode("off")
        time.sleep(0.1)
        self.assertEqual(len([r for r in lk.requests if r[1] == "set_mode"]), before)
        self.assertGreater(before, n - 1)
        self.assertEqual(self.wait_event("peer_mode_request"), {"from": "b", "mode": "off"})

    def test_incoming_monitor_request_runs_local_part(self):
        lk = self.link_up("b")
        res = lk.handler("a", "set_mode", {"mode": "monitor"})
        self.assertTrue(res["ok"], res)
        s = self.wait_mode("monitor")
        self.assertEqual(s["stream"]["role"], "client")
        self.assertEqual(FakeMoon.instances[0].calls[0][0], "start")
        time.sleep(0.1)
        self.assertEqual([r for r in lk.requests if r[1] == "set_mode"], [])

    def test_incoming_commands_validated(self):
        lk = self.link_up("a")
        self.assertEqual(lk.handler("b", "set_mode", {"mode": "format-c"})["code"], "bad_config")
        self.assertEqual(lk.handler("b", "set_mode", {"mode": "shared", "server": 5})["code"], "bad_config")
        self.assertEqual(lk.handler("b", "set_mode", {})["code"], "bad_config")
        self.assertEqual(lk.handler("zzz", "set_mode", {"mode": "off"})["code"], "auth_failed")
        self.assertEqual(lk.handler("b", "run", {"cmd": "calc"})["code"], "bad_config")
        self.assertEqual(lk.handler("b", "set_mode", {"mode": "shared", "server": "nope"})["code"], "bad_config")
        st = lk.handler("b", "status", {})
        self.assertEqual((st["ok"], st["mode"], st["role"]), (True, "off", "host"))

    def test_incoming_while_busy_is_rejected(self):
        lk = self.link_up("a")
        self.assertTrue(self.ctl._begin_busy("занято"))
        self.assertEqual(lk.handler("b", "set_mode", {"mode": "off"})["code"], "busy")

    def test_shared_propagates_to_peer(self):
        lk = self.link_up("a")
        self.ctl.set_mode("shared", "a")
        self.wait_mode("shared")
        self.wait_until(lambda: ("b", "set_mode", {"mode": "shared", "server": "a"}) in lk.requests, what="shared пиру")


if __name__ == "__main__":
    unittest.main()
