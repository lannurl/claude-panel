"""stream_ctl: обнаружение Apollo/Moonlight, argv, процесс Moonlight на фейках. Реальные программы не запускаются."""
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import _util  # noqa: F401  (путь к модулям проекта)
import deskflow_ctl as D
import stream_ctl as S


class FakeProc:
    _pid = 5000

    def __init__(self, code=None):
        FakeProc._pid += 1
        self.pid, self.code = FakeProc._pid, code
        self.stdout = self.stdin = None
        self.terminated = self.killed = False

    def poll(self):
        return self.code

    @property
    def returncode(self):
        return self.code

    def terminate(self):
        self.terminated = True
        self.code = 1

    def kill(self):
        self.killed = True
        self.code = 1

    def wait(self, timeout=None):
        return self.code


class TestFind(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def test_apollo_missing(self):
        with mock.patch.object(S, "IS_WIN", True):
            r = S.find_host_app("apollo", dirs=[str(self.dir)], connect=lambda h, p: True)
        self.assertEqual((r["ok"], r["code"], r["state"], r["running"]), (False, "not_found", "missing", False))
        self.assertIn("winget", r["hint"])

    def test_apollo_found_running_by_port_only(self):
        (self.dir / "sunshine.exe").write_bytes(b"MZ")
        calls = []

        def connect(h, p):
            calls.append((h, p))
            return True
        with mock.patch.object(S, "IS_WIN", True):
            r = S.find_host_app("apollo", dirs=[str(self.dir)], connect=connect)
            r2 = S.find_host_app("apollo", dirs=[str(self.dir)], connect=lambda h, p: False)
        self.assertEqual((r["ok"], r["state"], r["running"], r["web_ui"]), (True, "ok", True, "https://localhost:47990"))
        self.assertEqual(calls, [("127.0.0.1", 47990)])     # только TCP на loopback, без HTTP
        self.assertTrue(r["path"].endswith("sunshine.exe"))
        self.assertEqual((r2["ok"], r2["running"]), (True, False))

    def test_apollo_not_on_other_os_and_linux_sunshine(self):
        with mock.patch.object(S, "IS_WIN", False):
            self.assertEqual(S.find_host_app("apollo")["code"], "unsupported_os")
        with mock.patch.object(S, "OS_KEY", "linux"), mock.patch.object(S, "IS_WIN", False):
            r = S.find_host_app("sunshine")
        self.assertEqual((r["code"], r["state"]), ("unsupported_os", "unsupported"))
        self.assertEqual(S.find_host_app("bogus")["code"], "bad_config")

    def test_moonlight_found_and_templates(self):
        (self.dir / "Moonlight.exe").write_bytes(b"MZ")
        r = S.find_moonlight(dirs=[str(self.dir)])
        self.assertTrue(r["ok"])
        self.assertEqual(r["state"], "ok")
        self.assertFalse(r["dialect"]["verified"])           # НП: шаблоны CLI вживую не проверены
        for k in ("stream", "quit", "list", "pair"):
            self.assertIn(k, r["dialect"])
        self.assertEqual(S.find_moonlight(dirs=[str(self.dir / "no")])["state"], "missing")

    def test_moonlight_config_path_checked(self):
        bad = self.dir / "evil.exe"
        bad.write_bytes(b"MZ")
        good = self.dir / "Moonlight.exe"
        good.write_bytes(b"MZ")
        self.assertEqual(S.find_moonlight(str(bad), dirs=[])["code"], "bad_config")       # имя не из белого списка
        self.assertEqual(S.find_moonlight("Moonlight.exe", dirs=[])["code"], "bad_config")  # не абсолютный
        r = S.find_moonlight(str(good), dirs=[])
        self.assertTrue(r["ok"])
        self.assertEqual(r["path"], str(good))

    def test_virtual_display_present(self):
        ds = [{"label": "Ноут", "virtual": False}, {"label": "SudoMaker Virtual Display", "virtual": True}]
        self.assertEqual(S.virtual_display_present(ds)["label"], "SudoMaker Virtual Display")
        self.assertIsNone(S.virtual_display_present(ds[:1]))
        self.assertIsNone(S.virtual_display_present([]))
        ds2 = [{"label": "SudoMaker Virtual Display", "virtual": False}]    # флага нет — по подстроке
        self.assertIsNone(S.virtual_display_present(ds2))
        self.assertIsNotNone(S.virtual_display_present(ds2, ["SudoMaker"]))


class TestArgv(unittest.TestCase):
    EXE = "C:/Program Files/Moonlight Game Streaming/Moonlight.exe"

    def test_stream(self):
        a = S.build_argv("stream", self.EXE, "192.0.2.11", app="Monitor", resolution=(1920, 1080), fps=60)
        self.assertEqual(a, [self.EXE, "stream", "192.0.2.11", "Monitor", "--resolution", "1920x1080",
                             "--fps", "60", "--display-mode", "fullscreen"])

    def test_stream_without_resolution_drops_flag(self):
        a = S.build_argv("stream", self.EXE, "192.0.2.11", app="Monitor", fps=60)
        self.assertNotIn("--resolution", a)
        self.assertNotIn("{resolution}", a)

    def test_quit_list_pair(self):
        self.assertEqual(S.build_argv("quit", self.EXE, "host-a"), [self.EXE, "quit", "host-a"])
        self.assertEqual(S.build_argv("list", self.EXE, "host-a"), [self.EXE, "list", "host-a"])
        self.assertEqual(S.build_argv("pair", self.EXE, "host-a", pin="1234"),
                         [self.EXE, "pair", "host-a", "--pin", "1234"])

    def test_rejects_bad_values(self):
        bad = [dict(host="-x", app="Monitor"), dict(host="a;calc", app="Monitor"), dict(host="a b", app="Monitor"),
               dict(host="", app="Monitor"), dict(host="h", app="--help"), dict(host="h", app=""),
               dict(host="h", app="a" * 65), dict(host="h", app="x&y"),
               dict(host="h", app="Monitor", resolution="axb"), dict(host="h", app="Monitor", resolution=(10, 10)),
               dict(host="h", app="Monitor", fps=0), dict(host="h", app="Monitor", fps=True),
               dict(host="h", app="Monitor", fps="60")]
        for kw in bad:
            host = kw.pop("host")
            with self.assertRaises(ValueError, msg=str((host, kw))):
                S.build_argv("stream", self.EXE, host, **kw)
        with self.assertRaises(ValueError):
            S.build_argv("stream", "C:/x/evil.exe", "h", app="Monitor")      # имя exe не из белого списка
        with self.assertRaises(ValueError):
            S.build_argv("pair", self.EXE, "h", pin="12ab")
        with self.assertRaises(ValueError):
            S.build_argv("nope", self.EXE, "h")

    def test_ipv6_and_hostname_hosts(self):
        self.assertTrue(S.valid_host("192.0.2.11"))
        self.assertTrue(S.valid_host("laptop-a.local"))
        self.assertFalse(S.valid_host("-evil"))
        self.assertFalse(S.valid_host("a/b"))


class TestMoonlight(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.procs, self.argvs, self.runs = [], [], []
        self.exe = "C:/fake/Moonlight.exe"
        for p in (mock.patch.object(S, "START_CHECK_S", 0.25), mock.patch.object(S.Moonlight, "POLL_S", 0.05),
                  mock.patch.object(D, "proc_identity",
                                    side_effect=lambda pid: {"image": "moonlight.exe", "created": 42})):
            p.start()
            self.addCleanup(p.stop)
        self.events = []

    def popen(self, argv, **kw):
        self.argvs.append((argv, kw))
        p = FakeProc(code=getattr(self, "exit_code", None))
        self.procs.append(p)
        return p

    def runner(self, argv, timeout=None):
        self.runs.append(argv)
        return 0, "Desktop\nMonitor\n"

    def make(self):
        return S.Moonlight(self.exe, {}, self.tmp.name, on_event=lambda k, p: self.events.append((k, p)),
                           runner=self.runner, popen=self.popen)

    def test_start_builds_argv_no_shell(self):
        m = self.make()
        r = m.start("192.0.2.11", "Monitor", (1920, 1080), 60)
        self.assertTrue(r["ok"], r)
        argv, kw = self.argvs[0]
        self.assertEqual(argv[:4], [self.exe, "stream", "192.0.2.11", "Monitor"])
        self.assertIn("1920x1080", argv)
        self.assertFalse(kw["shell"])
        self.assertEqual(kw["creationflags"], S.CREATE_NO_WINDOW | S.CREATE_NEW_PROCESS_GROUP)
        self.assertTrue(m.status()["running"])
        self.assertTrue(m.process_info()["pid"] == self.procs[0].pid)
        self.assertTrue((Path(self.tmp.name) / "moonlight" / "moonlight.log").exists())
        m.stop(None)

    def test_start_rejects_bad_input_without_spawning(self):
        m = self.make()
        self.assertEqual(m.start("-x", "Monitor")["code"], "bad_config")
        self.assertEqual(m.start("h", "--evil")["code"], "bad_config")
        self.assertEqual(self.argvs, [])

    def test_quick_exit_is_error(self):
        self.exit_code = 3
        m = self.make()
        r = m.start("192.0.2.11", "Monitor")
        self.assertEqual((r["ok"], r["code"]), (False, "exited"))
        self.assertIn("3", r["err"])
        self.assertFalse(m.status()["running"])

    def test_stop_quits_host_and_terminates_own_process(self):
        m = self.make()
        m.start("192.0.2.11", "Monitor")
        r = m.stop("192.0.2.11")
        self.assertTrue(r["ok"] and r["stopped"])
        self.assertEqual(self.runs, [[self.exe, "quit", "192.0.2.11"]])
        self.assertTrue(self.procs[0].terminated)
        self.assertFalse(m.status()["running"])
        self.assertTrue(m.stop("192.0.2.11")["ok"])       # повторная остановка безопасна

    def test_exit_event_once(self):
        m = self.make()
        m.start("192.0.2.11", "Monitor")
        self.procs[0].code = 0
        end = time.monotonic() + 2
        while not self.events and time.monotonic() < end:
            time.sleep(0.02)
        self.assertEqual([k for k, _ in self.events], ["moonlight_exited"])
        time.sleep(0.2)
        self.assertEqual(len(self.events), 1)
        self.assertIn("Moonlight", m.status()["err"])

    def test_deliberate_stop_no_exit_event(self):
        m = self.make()
        m.start("192.0.2.11", "Monitor")
        m.stop(None)
        time.sleep(0.2)
        self.assertEqual(self.events, [])

    def test_adopt_checks_identity_and_image(self):
        m = self.make()
        info = {"pid": 77, "image": "moonlight.exe", "created": 42, "host": "h", "app": "Monitor", "since": "t"}
        self.assertTrue(m.adopt(info))
        self.assertTrue(m.status()["running"])
        m.stop(None)       # перенятый pid: убивается только он (через _kill_pid — подменяем)
        m2 = self.make()
        for bad in ({**info, "created": 41}, {**info, "image": "deskflow-core.exe"}):
            with mock.patch.object(D, "proc_identity",
                                   side_effect=lambda pid: {"image": "deskflow-core.exe", "created": 42}
                                   if bad["image"] == "deskflow-core.exe" else {"image": "moonlight.exe", "created": 42}):
                self.assertFalse(m2.adopt(bad), bad)

    def test_adopted_stop_kills_only_that_pid(self):
        m = self.make()
        info = {"pid": 77, "image": "moonlight.exe", "created": 42}
        self.assertTrue(m.adopt(info))
        killed = []
        alive = {"v": True}

        def ident(pid):
            return {"image": "moonlight.exe", "created": 42} if alive["v"] else None

        def kill(pid, force):
            killed.append((pid, force))
            alive["v"] = False
        with mock.patch.object(D, "proc_identity", side_effect=ident), mock.patch.object(D, "_kill_pid", side_effect=kill):
            m.stop(None)
        self.assertEqual(killed, [(77, False)])

    def test_list_and_pair(self):
        m = self.make()
        self.assertEqual(m.list_apps("h")["apps"], ["Desktop", "Monitor"])
        self.assertEqual(m.pair("h")["code"], "bad_config")
        self.assertTrue(m.pair("h", "1234")["ok"])
        self.assertEqual(self.runs[-1], [self.exe, "pair", "h", "--pin", "1234"])
        self.assertEqual(m.pair("h", "abc")["code"], "bad_config")


if __name__ == "__main__":
    unittest.main()
