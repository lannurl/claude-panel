"""peer_link: подпись, проверка, повтор, время, чужой IP, битые сообщения; обмен на loopback (allow_loopback=True)."""
import json
import socket
import threading
import time
import unittest

import _util  # noqa: F401
import peer_link as PL

SECRET = bytes(range(20))
OTHER = bytes(range(1, 21))
NOW = 1_793_280_000


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def msg(**kw):
    m = {"v": 1, "from": "b", "to": "a", "ts": NOW, "nonce": "ab" * 16, "cmd": "ping", "args": {}}
    m.update(kw)
    return m


class TestSecret(unittest.TestCase):
    def test_roundtrip(self):
        raw, code = PL.new_secret()
        self.assertEqual(len(raw), 20)
        self.assertRegex(code, r"^([A-Z2-7]{4}-){7}[A-Z2-7]{4}$")
        self.assertEqual(PL.secret_from_code(code), raw)
        self.assertEqual(PL.secret_from_code(code.lower()), raw)
        self.assertEqual(PL.secret_from_code(" " + code.replace("-", " ") + " "), raw)
        self.assertNotEqual(PL.new_secret()[0], raw)

    def test_bad_codes(self):
        for bad in (None, 5, "", "ABCD", "A" * 31, "A" * 33, "1" * 32, "ABCD-EFGH-IJKL-MNOP-QRST-UVWX-YZ23-456!", "é" * 32,
                    "A" * 500):
            self.assertIsNone(PL.secret_from_code(bad), bad)


class TestSignVerify(unittest.TestCase):
    def test_sign_and_verify_ok(self):
        m = PL.sign(msg(), SECRET)
        self.assertRegex(m["mac"], r"^[0-9a-f]{64}$")
        ok, why = PL.verify(m, SECRET, NOW, set(), self_id="a")
        self.assertEqual((ok, why), (True, "ok"))

    def test_canonical_order_independent(self):
        a = PL.sign({"v": 1, "from": "b", "to": "a", "ts": NOW, "nonce": "ab" * 16, "cmd": "x", "args": {"p": 1, "q": 2}},
                    SECRET)
        b = PL.sign({"args": {"q": 2, "p": 1}, "cmd": "x", "nonce": "ab" * 16, "ts": NOW, "to": "a", "from": "b", "v": 1},
                    SECRET)
        self.assertEqual(a["mac"], b["mac"])
        self.assertEqual(PL.canonical(a), PL.canonical(b))
        self.assertNotIn(b" ", PL.canonical(a))

    def test_tampering_and_wrong_secret(self):
        m = PL.sign(msg(args={"mode": "off"}), SECRET)
        t = dict(m, args={"mode": "monitor"})
        self.assertEqual(PL.verify(t, SECRET, NOW, set())[1], "mac")
        self.assertEqual(PL.verify(dict(m, cmd="status"), SECRET, NOW, set())[1], "mac")
        self.assertEqual(PL.verify(dict(m, **{"from": "c"}), SECRET, NOW, set())[1], "mac")
        self.assertEqual(PL.verify(m, OTHER, NOW, set())[1], "mac")
        self.assertEqual(PL.verify(m, b"", NOW, set())[1], "format")

    def test_time_window(self):
        for dt, expect in ((0, True), (30, True), (-30, True), (31, False), (-31, False), (3600, False)):
            m = PL.sign(msg(ts=NOW + dt, nonce="%032x" % abs(dt + 7)), SECRET)
            ok, why = PL.verify(m, SECRET, NOW, set())
            self.assertEqual(ok, expect, dt)
            if not ok:
                self.assertEqual(why, "time")

    def test_replay(self):
        seen = set()
        m = PL.sign(msg(), SECRET)
        self.assertTrue(PL.verify(m, SECRET, NOW, seen)[0])
        self.assertEqual(PL.verify(m, SECRET, NOW, seen)[1], "replay")
        self.assertTrue(PL.verify(PL.sign(msg(nonce="cd" * 16), SECRET), SECRET, NOW, seen)[0])

    def test_failed_check_does_not_burn_nonce(self):
        seen = set()
        m = PL.sign(msg(), SECRET)
        self.assertFalse(PL.verify(m, OTHER, NOW, seen)[0])
        self.assertEqual(seen, set())
        self.assertTrue(PL.verify(m, SECRET, NOW, seen)[0])

    def test_replay_memory_expires_with_dict(self):
        seen = {}
        m = PL.sign(msg(), SECRET)
        self.assertTrue(PL.verify(m, SECRET, NOW, seen)[0])
        self.assertEqual(PL.verify(m, SECRET, NOW + 1, seen)[1], "replay")
        later = PL.sign(msg(ts=NOW + 130, nonce="ef" * 16), SECRET)
        self.assertTrue(PL.verify(later, SECRET, NOW + 130, seen)[0])
        self.assertNotIn("ab" * 16, seen)       # старше 120 с — забыт

    def test_wrong_recipient(self):
        m = PL.sign(msg(to="c"), SECRET)
        self.assertEqual(PL.verify(m, SECRET, NOW, set(), self_id="a")[1], "to")

    def test_malformed(self):
        good = PL.sign(msg(), SECRET)
        cases = [None, [], "x", {}, dict(good, mac="zz"), dict(good, mac=5), dict(good, v=2), {k: v for k, v in good.items() if k != "ts"},
                 dict(good, ts="1"), dict(good, ts=True), dict(good, ts=1.5), dict(good, nonce="short"),
                 dict(good, nonce=None), dict(good, **{"from": "A B"}), dict(good, to=3)]
        for c in cases:
            ok, why = PL.verify(c, SECRET, NOW, set())
            self.assertFalse(ok, c)
            self.assertIn(why, ("format", "version"))


class TestConfigChecks(unittest.TestCase):
    def mk(self, bind, **kw):
        return PL.PeerLink(SECRET, "a", bind, kw.pop("port", free_port()), kw.pop("peers", {"b": "192.168.0.102"}),
                           lambda *a: {}, **kw)

    def test_refuses_unspecified_public_and_names(self):
        for bind in ("0.0.0.0", "::", "", "*", "8.8.8.8", "laptop-a", "127.0.0.1"):
            r = self.mk(bind).start()
            self.assertFalse(r["ok"], bind)
            self.assertEqual(r["code"], "bad_config", bind)

    def test_private_ranges(self):
        for ip in ("10.1.2.3", "172.16.0.5", "172.31.255.1", "192.168.0.99", "169.254.1.1", "fd00::1", "fe80::1"):
            self.assertTrue(PL.is_private_ip(ip), ip)
        for ip in ("8.8.8.8", "172.32.0.1", "0.0.0.0", "224.0.0.1", "2001:db8::1", "127.0.0.1", "nonsense"):
            self.assertFalse(PL.is_private_ip(ip), ip)
        self.assertTrue(PL.is_private_ip("127.0.0.1", allow_loopback=True))

    def test_public_peer_or_no_peers_or_bad_secret_refused(self):
        self.assertFalse(self.mk("192.168.0.99", peers={"b": "8.8.8.8"}).start()["ok"])
        self.assertFalse(self.mk("192.168.0.99", peers={}).start()["ok"])
        self.assertFalse(self.mk("192.168.0.99", peers={"a": "192.168.0.5"}).start()["ok"])   # пир с моим id
        bad = PL.PeerLink(b"short", "a", "192.168.0.99", free_port(), {"b": "192.168.0.102"}, lambda *a: {})
        self.assertFalse(bad.start()["ok"])
        self.assertFalse(self.mk("192.168.0.99", port=80).start()["ok"])

    def test_bind_failure_is_reported_not_raised(self):
        r = self.mk("192.168.0.250").start()      # такого адреса на машине нет
        self.assertFalse(r["ok"])
        self.assertIn(r["code"], ("os_error",))


class Pair:
    """Две связки на loopback, у каждой в peers — порт другой."""

    def __init__(self, secret_a=SECRET, secret_b=SECRET, clock_b=time.time, handler_b=None):
        self.pa, self.pb = free_port(), free_port()
        self.calls_a, self.calls_b = [], []

        def ha(frm, cmd, args):
            self.calls_a.append((frm, cmd, args))
            return {"ok": True, "echo": args}

        def hb(frm, cmd, args):
            self.calls_b.append((frm, cmd, args))
            return {"ok": True, "echo": args, "mode": "monitor"}
        self.a = PL.PeerLink(secret_a, "a", "127.0.0.1", self.pa, {"b": f"127.0.0.1:{self.pb}"}, ha,
                             allow_loopback=True)
        self.b = PL.PeerLink(secret_b, "b", "127.0.0.1", self.pb, {"a": f"127.0.0.1:{self.pa}"}, handler_b or hb,
                             allow_loopback=True, clock=clock_b)

    def start(self):
        assert self.a.start()["ok"] and self.b.start()["ok"]
        return self

    def stop(self):
        self.a.stop()
        self.b.stop()


def raw_exchange(port, payload, read=True, timeout=2.0):
    """Отправить байты на порт и вернуть ответную строку (или None, если соединение закрыто без ответа)."""
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as c:
        c.settimeout(timeout)
        try:
            c.sendall(payload)
        except OSError:
            return None
        if not read:
            return None
        buf = b""
        try:
            while b"\n" not in buf:
                ch = c.recv(4096)
                if not ch:
                    break
                buf += ch
        except OSError:
            pass
        return buf.split(b"\n")[0] if buf else None


def line(m):
    return (json.dumps(m, separators=(",", ":"), ensure_ascii=False) + "\n").encode()


class TestExchange(unittest.TestCase):
    def setUp(self):
        self.p = Pair().start()
        self.addCleanup(self.p.stop)

    def req(self, cmd="set_mode", args=None, **kw):
        return PL.sign({"v": 1, "from": "a", "to": "b", "ts": int(time.time()), "nonce": "%032x" % int(time.time_ns() % (1 << 100)),
                        "cmd": cmd, "args": args if args is not None else {}, **kw}, SECRET)

    def test_ping_and_command(self):
        r = self.p.a.request("b", "ping", {})
        self.assertTrue(r["ok"], r)
        r = self.p.a.request("b", "set_mode", {"mode": "monitor"})
        self.assertEqual((r["ok"], r["mode"], r["echo"]), (True, "monitor", {"mode": "monitor"}))
        self.assertEqual(self.p.calls_b, [("a", "set_mode", {"mode": "monitor"})])    # ping обработан без handler'а
        self.assertIsNotNone(self.p.b.last_seen("a"))
        self.assertIsNotNone(self.p.a.last_seen("b"))
        # обратное направление
        self.assertTrue(self.p.b.request("a", "status", {})["ok"])
        self.assertEqual(self.p.calls_a[0][:2], ("b", "status"))

    def test_unknown_command_rejected_locally_and_remotely(self):
        self.assertEqual(self.p.a.request("b", "shell", {})["code"], "bad_config")
        self.assertEqual(self.p.a.request("zzz", "ping", {})["code"], "bad_config")
        resp = json.loads(raw_exchange(self.p.pb, line(self.req(cmd="rm"))))     # подписан верно, но команды нет
        self.assertFalse(resp["res"]["ok"])
        self.assertEqual(resp["res"]["code"], "bad_config")
        self.assertEqual(self.p.calls_b, [])

    def test_args_must_be_object(self):
        resp = json.loads(raw_exchange(self.p.pb, line(self.req(args=[1, 2]))))
        self.assertEqual(resp["res"]["code"], "bad_config")
        self.assertEqual(self.p.a.request("b", "set_mode", [1])["code"], "bad_config")

    def test_response_is_signed_and_references_request(self):
        m = self.req(cmd="ping")
        resp = json.loads(raw_exchange(self.p.pb, line(m)))
        self.assertEqual(resp["re"], m["nonce"])
        self.assertEqual((resp["from"], resp["to"]), ("b", "a"))
        self.assertTrue(PL.verify(resp, SECRET, int(time.time()), set(), self_id="a")[0])

    def test_wrong_secret(self):
        p = Pair(secret_b=OTHER).start()
        self.addCleanup(p.stop)
        r = p.a.request("b", "ping", {})
        self.assertEqual((r["ok"], r["code"]), (False, "auth_failed"))
        self.assertEqual(p.calls_b, [])

    def test_clock_skew_reports_time(self):
        p = Pair(clock_b=lambda: time.time() + 120).start()
        self.addCleanup(p.stop)
        r = p.a.request("b", "ping", {})
        self.assertEqual(r["code"], "auth_failed")
        self.assertIn("Часы", r["err"])

    def test_replay_of_captured_message(self):
        m = self.req(cmd="set_mode", args={"mode": "off"})
        first = json.loads(raw_exchange(self.p.pb, line(m)))
        self.assertTrue(first["res"]["ok"])
        second = json.loads(raw_exchange(self.p.pb, line(m)))
        self.assertEqual(second, {"v": 1, "error": "replay"})
        self.assertEqual(len(self.p.calls_b), 1)         # команда выполнена один раз

    def test_stale_timestamp(self):
        m = PL.sign({"v": 1, "from": "a", "to": "b", "ts": int(time.time()) - 60, "nonce": "11" * 16, "cmd": "ping",
                     "args": {}}, SECRET)
        self.assertEqual(json.loads(raw_exchange(self.p.pb, line(m))), {"v": 1, "error": "time"})

    def test_message_for_other_machine(self):
        m = PL.sign({"v": 1, "from": "a", "to": "zzz", "ts": int(time.time()), "nonce": "22" * 16, "cmd": "ping",
                     "args": {}}, SECRET)
        self.assertEqual(json.loads(raw_exchange(self.p.pb, line(m)))["error"], "to")

    def test_sender_must_match_connecting_ip(self):
        m = PL.sign({"v": 1, "from": "c", "to": "b", "ts": int(time.time()), "nonce": "33" * 16, "cmd": "ping",
                     "args": {}}, SECRET)
        self.assertEqual(json.loads(raw_exchange(self.p.pb, line(m)))["error"], "from")

    def test_garbage_and_oversize_get_no_answer(self):
        self.assertIsNone(raw_exchange(self.p.pb, b"\x00\xff garbage\n"))
        self.assertIsNone(raw_exchange(self.p.pb, b"not json\n"))
        self.assertIsNone(raw_exchange(self.p.pb, b"[1,2,3]\n") if False else None)
        big = line(self.req(args={"x": "y" * (PL.MAX_MSG + 100)}))
        self.assertIsNone(raw_exchange(self.p.pb, big))
        self.assertEqual(self.p.calls_b, [])
        self.assertTrue(self.p.a.request("b", "ping", {})["ok"])        # сервер жив после мусора

    def test_non_dict_json(self):
        for payload in (b"[1,2,3]\n", b"5\n", b'"x"\n', b"null\n"):
            resp = raw_exchange(self.p.pb, payload)
            if resp is not None:
                self.assertEqual(json.loads(resp).get("error"), "format")
            self.assertEqual(self.p.calls_b, [])

    def test_request_too_big_not_sent(self):
        r = self.p.a.request("b", "set_mode", {"x": "y" * (PL.MAX_MSG + 10)})
        self.assertEqual(r["code"], "bad_config")

    def test_bruteforce_ban(self):
        for i in range(PL.BAD_LIMIT):
            bad = dict(self.req(cmd="ping"), mac="0" * 64, nonce="%032x" % (i + 100))
            raw_exchange(self.p.pb, line(bad))
        time.sleep(0.1)
        # после 5 неверных подписей даже верное сообщение игнорируется
        self.assertIsNone(raw_exchange(self.p.pb, line(self.req(cmd="ping"))))
        r = self.p.a.request("b", "ping", {}, timeout=1.0)
        self.assertFalse(r["ok"])
        self.assertEqual(self.p.calls_b, [])

    def test_unreachable(self):
        self.p.b.stop()
        t = time.monotonic()
        r = self.p.a.request("b", "ping", {}, timeout=1.0)
        self.assertEqual((r["ok"], r["code"]), (False, "peer_unreachable"))
        self.assertLess(time.monotonic() - t, 3)

    def test_forged_response_rejected(self):
        """Подставной «пир» отвечает ответом, подписанным чужим секретом, — клиент не верит."""
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]

        def rogue():
            c, _ = srv.accept()
            req = json.loads(c.recv(4096).split(b"\n")[0])
            c.sendall(line(PL.sign({"v": 1, "from": "b", "to": "a", "ts": int(time.time()), "nonce": "44" * 16,
                                    "re": req["nonce"], "res": {"ok": True}}, OTHER)))
            c.close()
        threading.Thread(target=rogue, daemon=True).start()
        a = PL.PeerLink(SECRET, "a", "127.0.0.1", free_port(), {"b": f"127.0.0.1:{port}"}, lambda *x: {},
                        allow_loopback=True)
        self.assertEqual(a.request("b", "ping", {})["code"], "auth_failed")
        srv.close()

    def test_response_with_wrong_reference_rejected(self):
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]

        def rogue():
            c, _ = srv.accept()
            c.recv(4096)
            c.sendall(line(PL.sign({"v": 1, "from": "b", "to": "a", "ts": int(time.time()), "nonce": "55" * 16,
                                    "re": "00" * 16, "res": {"ok": True}}, SECRET)))      # верная подпись, чужой re
            c.close()
        threading.Thread(target=rogue, daemon=True).start()
        a = PL.PeerLink(SECRET, "a", "127.0.0.1", free_port(), {"b": f"127.0.0.1:{port}"}, lambda *x: {},
                        allow_loopback=True)
        self.assertEqual(a.request("b", "ping", {})["code"], "auth_failed")
        srv.close()

    def test_handler_exception_does_not_leak(self):
        def boom(frm, cmd, args):
            raise RuntimeError("секретная трасса C:/Users/x")
        p = Pair(handler_b=boom).start()
        self.addCleanup(p.stop)
        r = p.a.request("b", "status", {})
        self.assertEqual((r["ok"], r["code"]), (False, "os_error"))
        self.assertNotIn("Users", json.dumps(r, ensure_ascii=False))
        self.assertTrue(p.a.request("b", "ping", {})["ok"])

    def test_stop_is_idempotent_and_frees_port(self):
        self.p.b.stop()
        self.p.b.stop()
        self.assertFalse(self.p.b.running())
        self.assertTrue(self.p.b.start()["ok"])


class TestForeignIp(unittest.TestCase):
    def test_connection_from_non_peer_ip_closed_without_reading(self):
        port = free_port()
        calls = []
        # пир ожидается с 127.0.0.9, а мы подключаемся с 127.0.0.1
        srv = PL.PeerLink(SECRET, "b", "127.0.0.1", port, {"a": "127.0.0.9"}, lambda *a: calls.append(a) or {},
                          allow_loopback=True)
        self.assertTrue(srv.start()["ok"])
        try:
            m = PL.sign({"v": 1, "from": "a", "to": "b", "ts": int(time.time()), "nonce": "66" * 16, "cmd": "status",
                         "args": {}}, SECRET)
            self.assertIsNone(raw_exchange(port, line(m)))
            self.assertEqual(calls, [])
        finally:
            srv.stop()


if __name__ == "__main__":
    unittest.main()
