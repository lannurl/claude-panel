"""displays_win (только dry-run, реальная раскладка НЕ меняется) и displays_mac (синтетические образцы)."""
import sys
import unittest

import _util  # добавляет корень проекта в sys.path
assert _util.ROOT.is_dir()
import displays_mac


FAKE = [
    {"os_id": "win:SDC4161:00000000:UID1", "gdi_name": r"\\.\DISPLAY1", "label": "Ноут", "px": [1920, 1080],
     "rect_px": [0, 0, 1920, 1080], "primary": True, "scale": 125, "size_mm": [345.0, 194.0], "virtual": False},
    {"os_id": "win:GSM5B7F:00000000:UID2", "gdi_name": r"\\.\DISPLAY2", "label": "Монитор", "px": [2560, 1440],
     "rect_px": [1920, 0, 2560, 1440], "primary": False, "scale": 100, "size_mm": [597.0, 336.0], "virtual": False},
]


@unittest.skipUnless(sys.platform == "win32", "только Windows")
class TestDisplaysWinDryRun(unittest.TestCase):
    def setUp(self):
        import displays_win
        self.dw = displays_win
        self._orig_write, self._orig_u32 = displays_win._write_position, displays_win._u32

        def forbidden(*a, **k):
            raise AssertionError("ChangeDisplaySettingsEx в тестах запрещён")

        class Guard:
            def __getattr__(self, name):
                if name == "ChangeDisplaySettingsExW":
                    return forbidden
                return getattr(self_orig, name)
        self_orig = self._orig_u32
        displays_win._write_position = forbidden
        displays_win._u32 = Guard()

    def tearDown(self):
        self.dw._write_position, self.dw._u32 = self._orig_write, self._orig_u32

    def test_dry_run_plan(self):
        res = self.dw.set_display_positions({FAKE[1]["os_id"]: (1920, -868)}, dry_run=True, displays=FAKE)
        self.assertTrue(res["ok"], res)
        self.assertTrue(res["dry_run"])
        self.assertEqual(res["plan"], [{"os_id": FAKE[1]["os_id"], "gdi_name": r"\\.\DISPLAY2",
                                        "x": 1920, "y": -868}])
        self.assertEqual(res["previous"][FAKE[1]["os_id"]], (1920, 0))

    def test_primary_shift(self):
        # основной передан не в (0, 0) — всё сдвигается, основной остаётся на месте
        res = self.dw.set_display_positions({FAKE[0]["os_id"]: (100, 100), FAKE[1]["os_id"]: (2020, -768)},
                                            dry_run=True, displays=FAKE)
        self.assertTrue(res["ok"], res)
        self.assertEqual([(p["x"], p["y"]) for p in res["plan"]], [(1920, -868)])

    def test_not_touching(self):
        res = self.dw.set_display_positions({FAKE[1]["os_id"]: (1925, 0)}, dry_run=True, displays=FAKE)
        self.assertFalse(res["ok"])
        self.assertEqual(res["code"], "bad_layout")
        res = self.dw.set_display_positions({FAKE[1]["os_id"]: (1925, 0)}, dry_run=False, displays=FAKE)
        self.assertEqual(res["code"], "bad_layout")   # проверка до любых вызовов ОС

    def test_unknown_and_garbage(self):
        self.assertEqual(self.dw.set_display_positions({"win:X:1": (0, 0)}, True, FAKE)["code"], "bad_layout")
        self.assertEqual(self.dw.set_display_positions({}, True, FAKE)["code"], "bad_layout")
        self.assertEqual(self.dw.set_display_positions({FAKE[1]["os_id"]: ("a", None)}, True, FAKE)["code"],
                         "bad_layout")

    def test_noop_does_not_call_os(self):
        res = self.dw.set_display_positions({FAKE[1]["os_id"]: (1920, 0)}, dry_run=False, displays=FAKE)
        self.assertTrue(res["ok"])
        self.assertEqual(res["plan"], [])

    def test_list_displays_shape(self):
        ds = self.dw.list_displays()
        self.assertIsInstance(ds, list)
        for d in ds:
            for k in ("os_id", "gdi_name", "label", "px", "rect_px", "primary", "scale", "size_mm", "virtual"):
                self.assertIn(k, d)
            self.assertTrue(d["os_id"].startswith("win:"))
            self.assertLessEqual(len(d["os_id"]), 128)
        if ds:
            self.assertEqual(sum(1 for d in ds if d["primary"]), 1)

    def test_edge_helper_status_offline(self):
        h = self.dw.EdgeHelper([], {}, {"poll_ms": 4})
        st = h.status()
        self.assertFalse(st["running"])
        self.assertEqual(st["client_state"], "local")
        self.assertTrue(h.set_client_state("client_absolute"))
        self.assertFalse(h.set_client_state("bogus"))
        self.assertEqual(h.status()["client_state"], "client_absolute")


class TestDisplaysMacSamples(unittest.TestCase):
    def test_import_outside_mac(self):
        if sys.platform != "darwin":
            self.assertEqual(displays_mac.list_displays(), [])
            self.assertIsNone(displays_mac.EdgeHelper)
            r = displays_mac.set_display_positions({"x": (0, 0)})
            self.assertEqual(r["code"], "unsupported_os")

    def test_retina_builtin(self):
        d = displays_mac.display_from_raw({"id": 1, "bounds": (0, 0, 1512, 982), "pixels": (3024, 1964),
                                           "size_mm": (302.2, 196.3), "main": 1, "builtin": 1, "vendor": 0x610,
                                           "model": 0xA050, "serial": 0, "rotation": 0})
        self.assertEqual(d["px"], [3024, 1964])
        self.assertEqual(d["rect_px"], [0, 0, 1512, 982])
        self.assertEqual(d["scale"], 200)
        self.assertEqual(d["size_mm"], [302.2, 196.3])
        self.assertTrue(d["primary"])
        self.assertEqual(d["os_id"], "mac:0610:A050:00000000")
        self.assertEqual(d["label"], "Встроенный дисплей")

    def test_external_bad_size(self):
        d = displays_mac.display_from_raw({"id": 5, "bounds": (1512, -400, 2560, 1440), "pixels": (2560, 1440),
                                           "size_mm": (0, 0), "main": 0, "builtin": 0, "vendor": 0x1E6D,
                                           "model": 0x5B7F, "serial": 7, "rotation": 0})
        self.assertIsNone(d["size_mm"])
        self.assertEqual(d["scale"], 100)
        self.assertFalse(d["primary"])
        d = displays_mac.display_from_raw({"id": 6, "bounds": (0, 0, 1920, 1080), "size_mm": (400, 400),
                                           "name": "BetterDisplay Virtual"})
        self.assertIsNone(d["size_mm"])        # аспект не сходится
        self.assertTrue(d["virtual"])

    def test_rotated(self):
        d = displays_mac.display_from_raw({"id": 2, "bounds": (0, 0, 1080, 1920), "pixels": (1920, 1080),
                                           "size_mm": (336, 597), "rotation": 90.0})
        self.assertEqual(d["px"], [1080, 1920])
        self.assertEqual(d["size_mm"], [336.0, 597.0])

    def test_mac_dry_run(self):
        ds = [displays_mac.display_from_raw({"id": 1, "bounds": (0, 0, 1512, 982), "pixels": (3024, 1964),
                                             "size_mm": (302, 196), "main": 1, "builtin": 1}),
              displays_mac.display_from_raw({"id": 2, "bounds": (1512, 0, 2560, 1440), "pixels": (2560, 1440),
                                             "size_mm": (597, 336), "vendor": 1, "model": 2, "serial": 3})]
        res = displays_mac.set_display_positions({ds[1]["os_id"]: (1512, -458)}, dry_run=True, displays=ds)
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["plan"][0]["y"], -458)


class TestPlatformImports(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "win32", "только Windows")
    def test_platform_win_block(self):
        import platform_win
        self.assertTrue(str(platform_win.DATA_DIR).endswith("MyPanel"))
        self.assertTrue(callable(platform_win.list_displays))
        self.assertTrue(callable(platform_win.process_running))
        self.assertEqual(platform_win.process_running(()), [])
        import os
        me = os.path.basename(sys.executable)
        self.assertEqual(platform_win.process_running((me.upper(), "nope-zz.exe")), [me.upper()])

    def test_displays_mac_imports_anywhere(self):
        import importlib
        importlib.reload(displays_mac)


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(sys.platform == "win32", "только Windows")
class TestEdgeHelperTickFake(unittest.TestCase):
    """_tick с подменёнными GetCursorPos/SetCursorPos: реальная мышь не трогается, поток не запускается."""

    def setUp(self):
        import json
        import displays_win
        import layout as L
        self.dw = displays_win
        self.real = displays_win._u32
        self.pos = [(0, 0)]
        self.warps = []
        test = self

        class Guard:
            def __getattr__(self, name):
                if name == "GetCursorPos":
                    def gcp(ref):
                        p = ref._obj
                        p.x, p.y = test.pos[0]
                        return 1
                    return gcp
                if name == "SetCursorPos":
                    return lambda x, y: test.warps.append((x, y)) or 1
                if name == "ChangeDisplaySettingsExW":
                    raise AssertionError("запрещено")
                return getattr(test.real, name)
        displays_win._u32 = Guard()
        lay, _ = L.parse_config(json.loads(json.dumps(L.EXAMPLE)))
        rules, _ = L.internal_rules(lay, "a")
        self.events = []
        self.h = displays_win.EdgeHelper(rules, {"a.1": (0, 0, 1920, 1080), "a.2": (1920, -868, 2560, 1440)},
                                         {}, on_event=lambda k, p: self.events.append((k, p)))

    def tearDown(self):
        self.dw._u32 = self.real

    def step(self, pt, t):
        from ctypes import wintypes
        self.pos[0] = pt
        self.h._tick(wintypes.POINT(), t)

    def test_warp_and_status(self):
        self.step((1915, 300), 100.0)
        self.step((1925, 300), 100.004)
        self.assertEqual(len(self.warps), 1)
        self.assertEqual(self.warps[0][0], 1922)
        st = self.h.status()
        self.assertEqual(st["warps"], 1)
        self.assertEqual((st["last_warp"]["src"], st["last_warp"]["dst"]), ("a.1", "a.2"))
        self.assertEqual(self.h._prev, self.warps[0])   # свой перенос — не переход

    def test_emergency_trip(self):
        t = 200.0
        for i in range(30):
            self.h._prev = (1915, 300)
            self.step((1925, 300), t)
            t += 0.01
        self.assertEqual(len(self.warps), self.dw.MAX_WARPS_PER_S)
        st = self.h.status()
        self.assertTrue(st["tripped"])
        self.assertTrue(st["paused"])
        self.assertTrue(any(k == "warning" for k, _ in self.events))

    def test_client_absolute_snapback(self):
        self.h.set_client_state("client_relative")
        self.step((1915, 300), 300.0)
        self.step((1925, 300), 300.004)          # перенос на (1922, 305)
        self.step((1926, 300), 300.008)          # сервер вернул курсор «в свою модель»
        self.assertEqual(self.h.status()["client_state"], "client_absolute")
