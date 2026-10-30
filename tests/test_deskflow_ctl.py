"""deskflow_ctl: диалект по фикстурам, golden-тексты, разбор лога, отпечатки, trusted-файлы, проверка на loopback.

В C:/ProgramData ничего не пишется: папка trusted-* всегда переопределяется на временную.
"""
import hashlib
import os
import shutil
import ssl
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

import _util  # добавляет корень проекта в sys.path
assert _util.ROOT.is_dir()
import deskflow_ctl as D
import layout as L

FIX = _util.ROOT / "tests" / "fixtures"
VERSION_TXT = (FIX / "deskflow_version_1.27.txt").read_text(encoding="utf-8")
HELP_TXT = (FIX / "deskflow_help_1.27.txt").read_text(encoding="utf-8")
FP_A = "65b3" + "0" * 56 + "c0c2"
FP_B = "ab" * 32

REAL = D.find_deskflow()   # один раз: нужен интеграционным тестам


class TestDialect(unittest.TestCase):
    def test_fixtures_1_27(self):
        d = D.parse_dialect(VERSION_TXT, HELP_TXT)
        self.assertTrue(d["ok"], d)
        self.assertEqual((d["state"], d["version"], d["form"]), ("ok", "1.27.0", "subcommand"))
        self.assertEqual(d["server_argv"], ["{core}", "server", "-s", "{settings}"])
        self.assertEqual(d["settings_flag"], "-s")
        self.assertEqual(d["new_instance_flag"], "--new-instance")
        self.assertEqual(d["decimals"], 2)
        self.assertLessEqual(len(d["help"]), 2048)

    def test_old_version(self):
        d = D.parse_dialect("deskflow-core v1.26.3, protocol v1.8", HELP_TXT)
        self.assertFalse(d["ok"])
        self.assertEqual((d["state"], d["code"]), ("old", "version_unsupported"))

    def test_unknown_dialect(self):
        self.assertEqual(D.parse_dialect("v1.27.0", "Usage: deskflow-server [options]")["code"], "dialect_unknown")
        self.assertEqual(D.parse_dialect("hello", "")["code"], "dialect_unknown")

    def test_build_argv(self):
        d = D.parse_dialect(VERSION_TXT, HELP_TXT)
        self.assertEqual(D.build_argv("server", "C:/x/deskflow-core.exe", d, settings_file="s.conf"),
                         ["C:/x/deskflow-core.exe", "server", "-s", "s.conf"])
        self.assertEqual(D.build_argv("client", "c", d, settings_file="s", new_instance=True)[-1], "--new-instance")
        with self.assertRaises(ValueError):
            D.build_argv("both", "c", d, settings_file="s")

    def test_find_order_and_bad_cfg_path(self):
        with tempfile.TemporaryDirectory() as td:
            core = Path(td) / ("deskflow-core.exe" if D.IS_WIN else "deskflow-core")
            core.write_bytes(b"x")
            ok = D.parse_dialect(VERSION_TXT, HELP_TXT)
            with mock.patch.object(D, "probe_dialect", return_value=ok), \
                    mock.patch.object(D, "_search_dirs", return_value=[]):
                r = D.find_deskflow(str(core))
                self.assertTrue(r["ok"], r)
                self.assertEqual((r["state"], r["version"], r["path"]), ("ok", "1.27.0", str(core)))
            with mock.patch.object(D, "_search_dirs", return_value=[td]), \
                    mock.patch.object(D, "probe_dialect", return_value=ok):
                self.assertEqual(D.find_deskflow()["path"], str(core))   # поиск по стандартным путям/PATH
            with mock.patch.object(D, "_search_dirs", return_value=[]):
                r = D.find_deskflow()
                self.assertEqual((r["ok"], r["state"], r["code"]), (False, "missing", "not_found"))
            bad = D.find_deskflow(str(Path(td) / "evil.exe"))
            self.assertEqual(bad["code"], "bad_config")
            self.assertEqual(D.find_deskflow("relative/deskflow-core.exe")["code"], "bad_config")


class TestBuild(unittest.TestCase):
    def setUp(self):
        self.lay, _ = L.parse_config(L.EXAMPLE)
        self.links, _ = L.deskflow_links(self.lay)

    def test_server_config_golden(self):
        text = D.build_server_config(self.lay, self.links, {"server": "a"})
        self.assertEqual(text, (
            "section: links\n"
            "\tlaptop-a:\n"
            "\t\tright(0,50.57) = laptop-b(0,1)\n"
            "\t\tright(50.57,73.92) = laptop-b(0,54.66)\n"
            "\tlaptop-b:\n"
            "\t\tleft(0,54.66) = laptop-a(50.57,73.92)\n"
            "\t\tleft(54.66,100) = laptop-a(72.92,73.92)\n"
            "end\n"
            "section: options\n"
            "\tkeystroke(control+alt+shift+F9) = switchToScreen(laptop-b)\n"
            "\tkeystroke(control+alt+shift+F10) = switchToScreen(laptop-a)\n"
            "\tkeystroke(control+alt+shift+F11) = lockCursorToScreen(on)\n"
            "\tkeystroke(control+alt+shift+F12) = lockCursorToScreen(off)\n"
            "end\n"))
        self.assertNotIn("section: screens", text)
        self.assertEqual(text, D.build_server_config(self.lay, list(reversed(self.links)), {"server": "a"}))

    def test_server_b_swaps_work_on(self):
        text = D.build_server_config(self.lay, self.links, {"server": "b", "relative_moves": True})
        self.assertIn("\trelativeMouseMoves = true\n", text)
        self.assertIn("keystroke(control+alt+shift+F9) = switchToScreen(laptop-a)", text)   # «работать на другой машине»
        self.assertIn("keystroke(control+alt+shift+F10) = switchToScreen(laptop-b)", text)

    def test_hotkeys_custom_and_invalid(self):
        ex = dict(self.lay.extra)
        ex["deskflow"] = dict(ex["deskflow"], work_on_hotkeys={"work_on": "Control+Alt+F5", "back": "bad key",
                                                              "lock": "Super+Shift+F11", "unlock": "Control+Alt+F5"})
        lay = L.Layout(self.lay.machines, self.lay.screens, self.lay.revision, ex)
        text = D.build_server_config(lay, self.links, {"server": "a"})
        self.assertIn("keystroke(control+alt+F5) = switchToScreen(laptop-b)", text)
        self.assertIn("keystroke(super+shift+F11) = lockCursorToScreen(on)", text)
        self.assertNotIn("bad key", text)
        self.assertNotIn("lockCursorToScreen(off)", text)   # дубль сочетания пропущен

    def test_settings_golden_server(self):
        text = D.build_settings("server", name="laptop-a", machines=["laptop-a", "laptop-b"],
                                bind_host="192.168.0.101", port=24800, tls=True,
                                cert_path="C:\\Users\\u\\tls\\deskflow.pem", links_path=Path("C:/d/server.conf"))
        self.assertEqual(text, (
            "[core]\ncoreMode=2\ncomputerName=laptop-a\ninterface=192.168.0.101\nport=24800\n"
            "[security]\ntlsEnabled=true\ncertificate=C:/Users/u/tls/deskflow.pem\ncheckPeerFingerprints=true\n"
            "[server]\nexternalConfig=true\nexternalConfigFile=C:/d/server.conf\nenableClipboard=true\n"
            "disableLockToComputer=true\n"
            "[log]\nlevel=DEBUG\n"
            "[computer_laptop-a]\nname=laptop-a\n[computer_laptop-b]\nname=laptop-b\n"))

    def test_settings_golden_client_no_tls(self):
        text = D.build_settings("client", name="laptop-b", machines=["laptop-a"], server_host="192.168.0.101",
                                port=24801, tls=False, clipboard=False, log_level="INFO")
        self.assertEqual(text, (
            "[core]\ncoreMode=1\ncomputerName=laptop-b\nport=24801\n"
            "[client]\nremoteHost=192.168.0.101\n"
            "[security]\ntlsEnabled=false\ncheckPeerFingerprints=false\n"
            "[log]\nlevel=INFO\n"
            "[computer_laptop-a]\nname=laptop-a\n[computer_laptop-b]\nname=laptop-b\n"))

    def test_settings_rejects_bad_input(self):
        base = dict(name="a", machines=["a"], bind_host="10.0.0.1", port=24800, tls=False, links_path="/l")
        D.build_settings("server", **base)
        for bad in ({"bind_host": "0.0.0.0"}, {"bind_host": "-x"}, {"name": "-a"}, {"name": "a b"},
                    {"port": 80}, {"port": "24800"}, {"log_level": "LOUD"}, {"machines": ["a", "b;c"]}):
            with self.assertRaises(ValueError, msg=bad):
                D.build_settings("server", **{**base, **bad})
        with self.assertRaises(ValueError):
            D.build_settings("server", **{**base, "tls": True})   # нет cert_path
        with self.assertRaises(ValueError):
            D.build_settings("server", **{**base, "links_path": 'C:/a"b'})

    def test_settings_quotes_commas(self):
        text = D.build_settings("server", name="a", machines=["a"], bind_host="10.0.0.1", port=24800, tls=True,
                                cert_path="C:/a,b/x.pem", links_path="C:/l.conf")
        self.assertIn('certificate="C:/a,b/x.pem"', text)

    def test_override_ini(self):
        src = "[core]\nport=1\ninterface=1.2.3.4\n[x]\na=b\n"
        out = D._override_ini(src, {("core", "port"): "9", ("security", "tlsEnabled"): "false"})
        self.assertEqual(out, "[core]\ninterface=1.2.3.4\nport=9\n[x]\na=b\n[security]\ntlsEnabled=false\n")


class TestLogParsing(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.events = []
        self.df = D.Deskflow("x", {}, self.tmp.name, on_event=lambda k, p: self.events.append((k, p)))

    def tearDown(self):
        self.tmp.cleanup()

    def feed(self, *lines):
        for ln in lines:
            self.df._feed(f"[2026-10-30T12:49:58.198] {ln}")

    def kinds(self):
        return [k for k, _ in self.events]

    def test_untrusted_then_trusted_resets(self):
        self.df._role = "server"
        self.feed("DEBUG: binding to address: 127.0.0.1:24899", "DEBUG: started server, waiting for clients",
                  f"DEBUG: peer fingerprint: {FP_A.upper()}", "WARNING: fingerprint does not match trusted fingerprint")
        p = self.df.status()["pending_peer_fp"]
        self.assertEqual(p["fp"], FP_A)
        self.assertTrue(p["at"])
        self.assertEqual(self.kinds(), ["deskflow_untrusted_peer"])
        self.assertEqual(self.events[0][1]["fp_human"][:5], "65:B3")
        self.assertTrue(self.df.status()["listening"])
        self.feed(f"DEBUG: peer fingerprint: {FP_A.upper()}", "WARNING: fingerprint does not match trusted fingerprint")
        self.assertEqual(self.kinds(), ["deskflow_untrusted_peer"])   # тот же отпечаток при повторе — без нового события
        self.feed("DEBUG: read 1 fingerprint(s) from file: C:/x", "DEBUG: fingerprint matches trusted fingerprint")
        self.assertIsNone(self.df.status()["pending_peer_fp"])

    def test_no_pending_without_peer_line(self):
        self.df._role = "client"
        self.feed("ERROR: failed to verify server certificate fingerprint")
        self.assertIsNone(self.df.status()["pending_peer_fp"])
        self.assertEqual(self.df._err, "failed to verify server certificate fingerprint")

    def test_client_trusts_server_waits_for_server(self):
        # реальный лог ноута A: сначала отказ, потом отпечаток добавлен — сервер ещё рвёт соединение
        self.df._role = "client"
        self.feed(f"DEBUG: peer fingerprint: {FP_A.upper()}", "WARNING: fingerprint does not match trusted fingerprint",
                  "ERROR: failed to verify server certificate fingerprint")
        self.assertTrue(self.df._err)
        self.feed(f"DEBUG: peer fingerprint: {FP_A.upper()}", "DEBUG: fingerprint matches trusted fingerprint",
                  "INFO: connected to secure socket", "DEBUG: tls connection closed",
                  "DEBUG: disconnected from server", "DEBUG: retry in 1 seconds")
        st = self.df.status()
        self.assertIsNone(st["err"])
        self.assertIsNone(st["pending_peer_fp"])
        self.assertTrue(st["waiting_peer_trust"])
        self.feed("DEBUG: connected to server")
        self.assertFalse(self.df.status()["waiting_peer_trust"])

    def test_server_clients_and_error_clear(self):
        self.df._role = "server"
        self.feed("ERROR: secure socket error: tls certificate doesn't exist: C:/x")
        self.assertIn("tls certificate", self.df._err)
        self.feed('INFO: accepted client connection', 'DEBUG: client "laptop-b" has connected')
        self.assertEqual(self.df._connected, ["laptop-b"])
        self.assertIsNone(self.df._err)
        self.feed('DEBUG: client "laptop-b" has connected')   # повтор — без дубля
        self.assertEqual(self.kinds(), ["deskflow_connected"])
        self.feed('INFO: client "laptop-b" has disconnected')
        self.assertEqual(self.df._connected, [])
        self.assertEqual(self.kinds(), ["deskflow_connected", "deskflow_disconnected"])

    def test_client_connect_and_retry(self):
        self.df._role, self.df._peer = "client", "laptop-a"
        self.feed("WARNING: failed to connect to server: Timed out", "DEBUG: retry in 1 seconds")
        self.assertEqual(self.df._connected, [])
        self.feed("INFO: connected to secure socket", "NOTE: connected to server")
        self.assertEqual(self.df._connected, ["laptop-a"])
        self.assertEqual(self.kinds(), ["deskflow_connected"])
        self.feed("WARNING: failed to connect to server: Timed out")
        self.assertEqual(self.df._connected, [])
        self.assertEqual(self.kinds()[-1], "deskflow_disconnected")

    def test_switch_and_tail(self):
        self.df._role = "server"
        self.feed('INFO: switch from "laptop-a" to "laptop-b" at 1919,300')
        sw = self.df.status()["last_switch"]
        self.assertEqual((sw["from"], sw["to"], sw["x"], sw["y"]), ("laptop-a", "laptop-b", 1919, 300))
        self.assertEqual(self.kinds(), ["deskflow_switch"])
        for i in range(30):
            self.feed(f"DEBUG: line {i}")
        tail = self.df.status()["log_tail"]
        self.assertEqual(len(tail), 20)
        self.assertTrue(tail[-1].endswith("line 29"))

    def test_clear_pending(self):
        self.df._role = "server"
        self.feed(f"DEBUG: peer fingerprint: {FP_A.upper()}", "WARNING: fingerprint does not match trusted fingerprint")
        self.df.clear_pending("aa" * 32)
        self.assertIsNotNone(self.df.status()["pending_peer_fp"])
        self.df.clear_pending(FP_A)
        self.assertIsNone(self.df.status()["pending_peer_fp"])

    def test_adopt_rejects_foreign_identity(self):
        me = D.proc_identity(os.getpid())
        self.assertIsNotNone(me)
        self.assertIn("python", me["image"])
        self.assertFalse(self.df.adopt({"pid": os.getpid(), "image": me["image"], "created": me["created"],
                                        "role": "server"}))   # свой образ — не deskflow-core
        self.assertFalse(self.df.adopt({"pid": os.getpid(), "image": "deskflow-core.exe", "created": 1,
                                        "role": "server"}))
        self.assertFalse(self.df.adopt({"pid": 999999, "image": "deskflow-core.exe", "created": 1, "role": "server"}))
        self.assertIsNone(D.proc_identity(-5))
        self.assertIsNone(D.proc_identity(True))


class TestFingerprints(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name) / "tls"

    def tearDown(self):
        self.tmp.cleanup()

    def test_normalize_variants(self):
        human = ":".join(FP_A[i:i + 2] for i in range(0, 64, 2)).upper()
        for v in (FP_A, FP_A.upper(), human, human.lower(), "SHA256:" + human, "v2:sha256:" + FP_A,
                  "  " + FP_A[:32] + " " + FP_A[32:] + "\n", "sha256 " + FP_A, human.replace(":", "-")):
            self.assertEqual(D.normalize_fp(v), FP_A, v)
        self.assertEqual(D.format_fp(FP_A), human)
        for bad in ("", "abc", FP_A[:-1], FP_A + "0", "g" * 64, None, 5, "x" * 500, "v2:sha1:" + FP_A[:40]):
            self.assertIsNone(D.normalize_fp(bad), bad)

    def test_local_fingerprint_sha256_of_der(self):
        der = b"\x30\x82fake-certificate-bytes"
        path = Path(self.tmp.name) / "c.pem"
        path.write_text("-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----\n" + ssl.DER_cert_to_PEM_cert(der),
                        encoding="ascii")
        r = D.local_fingerprint(path)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["fp"], hashlib.sha256(der).hexdigest())
        self.assertEqual(r["fp_human"], D.format_fp(r["fp"]))
        self.assertEqual(D.local_fingerprint(Path(self.tmp.name) / "none.pem")["code"], "not_found")
        path.write_text("nothing", encoding="ascii")
        self.assertEqual(D.local_fingerprint(path)["code"], "bad_config")

    def test_add_trusted_server_and_client_files(self):
        r = D.add_trusted("server", FP_A.upper(), self.dir)
        self.assertTrue(r["ok"] and r["added"], r)
        self.assertEqual((self.dir / "trusted-clients").read_text(encoding="utf-8"), f"v2:sha256:{FP_A}\n")
        self.assertTrue(D.add_trusted("client", D.format_fp(FP_B), self.dir)["ok"])
        self.assertEqual((self.dir / "trusted-servers").read_text(encoding="utf-8"), f"v2:sha256:{FP_B}\n")
        self.assertEqual(D.trusted_fingerprints("server", self.dir), [FP_A])
        self.assertEqual(D.trusted_fingerprints("client", self.dir), [FP_B])
        self.assertEqual(D.trusted_fingerprints("server", self.dir / "none"), [])

    def test_add_trusted_no_duplicates_and_keeps_existing(self):
        self.dir.mkdir(parents=True)
        (self.dir / "trusted-clients").write_text("v2:sha256:" + FP_B, encoding="utf-8")   # без перевода строки в конце
        self.assertTrue(D.add_trusted("server", FP_A, self.dir)["added"])
        again = D.add_trusted("server", "SHA256:" + D.format_fp(FP_A), self.dir)
        self.assertTrue(again["ok"])
        self.assertFalse(again["added"])
        self.assertFalse(D.add_trusted("server", FP_B, self.dir)["added"])
        self.assertEqual((self.dir / "trusted-clients").read_text(encoding="utf-8"),
                         f"v2:sha256:{FP_B}\nv2:sha256:{FP_A}\n")
        self.assertEqual([p.name for p in self.dir.iterdir()], ["trusted-clients"])   # временные файлы убраны

    def test_add_trusted_validation(self):
        for bad in ("", "zz", FP_A[:-2]):
            r = D.add_trusted("server", bad, self.dir)
            self.assertEqual((r["ok"], r["code"]), (False, "bad_config"))
        self.assertEqual(D.add_trusted("both", FP_A, self.dir)["code"], "bad_config")
        self.assertFalse(self.dir.exists())

    def test_add_trusted_permission_error(self):
        with mock.patch.object(D, "_atomic_write_text", side_effect=PermissionError), \
                mock.patch("builtins.open", side_effect=PermissionError):
            r = D.add_trusted("server", FP_A, self.dir)
        self.assertEqual((r["ok"], r["code"]), (False, "admin_required"))
        self.assertIn(FP_A, r["hint"])

    def test_default_trusted_dir_by_os(self):
        with mock.patch.object(D, "TRUSTED_DIR_OVERRIDE", None):
            self.assertEqual(D.trusted_path("server").name, "trusted-clients")
            self.assertEqual(D.trusted_path("client").name, "trusted-servers")
            if D.IS_WIN:
                self.assertEqual(D.trusted_dir().parts[-2:], ("Deskflow", "tls"))
        with mock.patch.object(D, "TRUSTED_DIR_OVERRIDE", str(self.dir)):
            self.assertEqual(D.trusted_dir(), self.dir)


class TestCertificate(unittest.TestCase):
    def test_missing_tools(self):
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(D, "_gen_cert_cryptography", side_effect=ImportError), \
                mock.patch.object(D, "_find_openssl", return_value=None), \
                mock.patch.dict("sys.modules", {"cryptography": mock.MagicMock()}):
            r = D.ensure_certificate(td)
            self.assertEqual((r["ok"], r["code"]), (False, "tls_missing"))
            self.assertIn("pip install cryptography", r["hint"])
            self.assertFalse((Path(td) / "deskflow" / "tls" / "deskflow.pem").exists())

    def _has_generator(self):
        try:
            import cryptography  # noqa: F401
            return True
        except ImportError:
            return D._find_openssl() is not None

    def test_generate_and_reuse(self):
        if not self._has_generator():
            self.skipTest("нет ни cryptography, ни openssl")
        with tempfile.TemporaryDirectory() as td:
            r = D.ensure_certificate(td)
            self.assertTrue(r["ok"] and r["created"], r)
            path = Path(r["path"])
            self.assertEqual(path, Path(td) / "deskflow" / "tls" / "deskflow.pem")
            text = path.read_text(encoding="ascii")
            self.assertIn("PRIVATE KEY", text)
            self.assertIn("BEGIN CERTIFICATE", text)
            self.assertEqual(len(r["fp"]), 64)
            self.assertEqual(r["fp_human"], D.format_fp(r["fp"]))
            r2 = D.ensure_certificate(td)
            self.assertFalse(r2["created"])
            self.assertEqual(r2["fp"], r["fp"])
            self.assertEqual(D.local_fingerprint(path)["fp"], r["fp"])
            if not D.IS_WIN:
                self.assertEqual(path.stat().st_mode & 0o077, 0)


class TestNetwork(unittest.TestCase):
    def test_resolve_bind_host(self):
        self.assertEqual(D.resolve_bind_host("192.168.0.101"), ("192.168.0.101", None))
        for bad in ("0.0.0.0", "::"):
            ip, err = D.resolve_bind_host(bad)
            self.assertIsNone(ip)
            self.assertTrue(err)
        with mock.patch.object(D, "local_ip_for", return_value="192.168.0.5") as m:
            self.assertEqual(D.resolve_bind_host("", "192.168.0.6"), ("192.168.0.5", None))
            m.assert_called_once_with("192.168.0.6")
        with mock.patch.object(D, "local_ip_for", return_value=None):
            self.assertIsNone(D.resolve_bind_host(None, "192.168.0.6")[0])
            self.assertIsNone(D.resolve_bind_host("", None)[0])

    def test_local_ip_never_loopback_or_any(self):
        ip = D.local_ip_for("127.0.0.1")
        self.assertIsNone(ip)


@unittest.skipUnless(REAL.get("ok"), "Deskflow 1.27 не установлен")
class TestRealCore(unittest.TestCase):
    """Живые запуски только на 127.0.0.1, каждый ≤ нескольких секунд; процессы — только свои."""

    def setUp(self):
        self.d = REAL["dialect"]
        self.path = REAL["path"]
        self.lay, _ = L.parse_config(L.EXAMPLE)
        self.links, _ = L.deskflow_links(self.lay)
        self.conf = D.build_server_config(self.lay, self.links, {"server": "a"})
        self.tmp = types.SimpleNamespace(name=tempfile.mkdtemp(prefix="mp-test-"))

    def tearDown(self):
        shutil.rmtree(self.tmp.name, ignore_errors=True)

    def settings(self):
        return D.build_settings("server", name="laptop-a", machines=["laptop-a", "laptop-b"],
                                bind_host="127.0.0.1", port=24800, tls=False, links_path="C:/none/server.conf")

    def test_validate_config_ok(self):
        r = D.validate_config(self.path, self.settings(), self.conf, self.d)
        self.assertTrue(r["ok"], r)
        self.assertTrue(any("started server" in x for x in r["log"]))
        self.assertTrue(any("active sides" in x for x in r["log"]))
        # хоткеи может держать уже запущенный Deskflow панели — это не ошибка конфига
        self.assertEqual([w for w in r["warnings"] if "register hotkey" not in w], [])

    def test_validate_config_bad_syntax(self):
        bad = self.conf.replace("right(0,50.57)", "sideways(0,50.57)")
        r = D.validate_config(self.path, self.settings(), bad, self.d)
        self.assertFalse(r["ok"])
        self.assertEqual(r["code"], "bad_config")
        self.assertIn("read error", r["err"])
        self.assertLessEqual(len(r["err"]), 200)

    def test_validate_config_unknown_computer(self):
        st = D.build_settings("server", name="laptop-a", machines=["laptop-a"], bind_host="127.0.0.1",
                              port=24800, tls=False, links_path="x")
        r = D.validate_config(self.path, st, self.conf, self.d)
        self.assertFalse(r["ok"])

    def test_validate_config_input_errors(self):
        self.assertEqual(D.validate_config(self.path, "", self.conf)["code"], "bad_config")
        self.assertEqual(D.validate_config("", self.settings(), self.conf)["code"], "bad_config")

    def test_start_adopt_stop_loopback(self):
        events = []
        df = D.Deskflow(self.path, self.d, self.tmp.name, on_event=lambda k, p: events.append(k))
        port = D._free_port()
        r = df.start_server(self.conf, "laptop-a", "127.0.0.1", port, False, machines=["laptop-a", "laptop-b"],
                            new_instance=True)
        self.assertTrue(r["ok"], r)
        df2 = None
        try:
            end = time.monotonic() + 5
            while time.monotonic() < end and not df.status()["listening"]:
                time.sleep(0.05)
            st = df.status()
            self.assertTrue(st["running"])
            self.assertEqual(st["role"], "server")
            self.assertTrue(st["listening"], st["log_tail"])
            self.assertIsNone(st["err"])
            info = df.process_info()
            self.assertEqual(info["image"], os.path.basename(self.path).lower())
            df2 = D.Deskflow(self.path, self.d, self.tmp.name)   # «перезапуск панели»: перенимаем процесс по pid
            self.assertTrue(df2.adopt(info))
            self.assertTrue(df2.status()["running"])
            self.assertTrue(df2.status()["listening"])
            self.assertTrue(df2.stop()["ok"])
            end = time.monotonic() + 5
            while time.monotonic() < end and df.running():
                time.sleep(0.05)
            self.assertFalse(df.running())
            self.assertIsNone(D.proc_identity(info["pid"]))
        finally:
            df.stop()
            if df2:
                df2.stop()
        self.assertTrue((Path(self.tmp.name) / "deskflow" / "core.log").is_file())
        self.assertTrue((Path(self.tmp.name) / "deskflow" / "settings.conf").is_file())

    def test_port_busy_is_conflict(self):
        import socket
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        try:
            df = D.Deskflow(self.path, self.d, self.tmp.name)
            r = df.start_server(self.conf, "laptop-a", "127.0.0.1", s.getsockname()[1], False,
                                machines=["laptop-a", "laptop-b"], new_instance=True)
            self.assertEqual((r["ok"], r["code"]), (False, "conflict"))
            self.assertFalse(df.running())
        finally:
            s.close()


if __name__ == "__main__":
    unittest.main()
