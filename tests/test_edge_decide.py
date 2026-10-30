"""edge_decide на таблицах §7.6: переходы ОС, «дожим», телепорт, пауза, порог push, а также snapback (§7.4.1)."""
import json
import unittest

import _util  # добавляет корень проекта в sys.path
assert _util.ROOT.is_dir()
import layout as L

CFG = {"max_step_px": 600, "push_mickeys": 4, "paused": False, "button_down": False,
       "skip_while_button_down": False}
RECTS = {"a.1": (0, 0, 1920, 1080), "a.2": (1920, -868, 2560, 1440)}


def rules():
    lay, _ = L.parse_config(json.loads(json.dumps(L.EXAMPLE)))
    r, warn = L.internal_rules(lay, "a")
    assert not warn
    return r


class TestEdgeDecide(unittest.TestCase):
    def setUp(self):
        self.rules = rules()

    def decide(self, prev, now, push=(0, 0), **cfg):
        return L.edge_decide(self.rules, RECTS, prev, now, push, {**CFG, **cfg})

    def near(self, got, want):
        self.assertIsNotNone(got)
        self.assertEqual(got[0], want[0])
        self.assertLessEqual(abs(got[1] - want[1]), 1, (got, want))

    def test_os_transition_table(self):
        """Windows сама перевела курсор на M «пиксель в пиксель» — помощник ставит по физике."""
        for y, want in ((0, 74), (300, 305), (540, 490), (571, 514)):
            self.near(self.decide((1915, y), (1925, y)), (1922, want))

    def test_os_transition_eased(self):
        """«Облегчить перемещение»: Windows перебросила из мёртвой зоны в угол M — поправляем на угол по правилу."""
        self.assertEqual(self.decide((1918, 800), (1921, 571)), (1922, 569))

    def test_push_blocked_zone(self):
        self.near(self.decide((1919, 620), (1919, 620), push=(5, 0)), (1922, 552))
        self.assertEqual(self.decide((1919, 800), (1919, 800), push=(4, 0)), (1922, 569))

    def test_push_below_threshold(self):
        self.assertIsNone(self.decide((1919, 800), (1919, 800), push=(3, 0)))
        self.assertIsNone(self.decide((1919, 800), (1919, 800), push=(-10, 0)))   # движение внутрь
        self.assertIsNone(self.decide((1918, 800), (1918, 800), push=(50, 0)))    # не на крайнем пикселе

    def test_push_unavailable(self):
        self.assertIsNone(self.decide((1919, 800), (1919, 800), push=None))

    def test_push_zero_threshold_needs_outward(self):
        self.assertEqual(self.decide((1919, 800), (1919, 800), push=(1, 0), push_mickeys=0), (1922, 569))
        self.assertIsNone(self.decide((1919, 800), (1919, 800), push=(0, 0), push_mickeys=0))

    def test_m_to_a1(self):
        self.near(self.decide((1925, 200), (1915, 200)), (1917, 163))
        self.near(self.decide((1925, 571), (1915, 571)), (1917, 645))
        # над ноутом — упор в левый край M и «дожим» → правый верхний угол A1
        self.assertEqual(self.decide((1920, -500), (1920, -500), push=(-6, 0)), (1917, 2))

    def test_teleport(self):
        self.assertIsNone(self.decide((100, 100), (3000, 0)))
        self.assertIsNone(self.decide((960, 540), (3200, -100)))   # Deskflow держит курсор в центре

    def test_paused(self):
        self.assertIsNone(self.decide((1915, 300), (1925, 300), paused=True))
        self.assertIsNone(self.decide((1919, 800), (1919, 800), push=(9, 0), paused=True))

    def test_button_down(self):
        self.assertIsNone(self.decide((1915, 300), (1925, 300), skip_while_button_down=True, button_down=True))
        self.assertIsNotNone(self.decide((1915, 300), (1925, 300), skip_while_button_down=False, button_down=True))

    def test_inside_screen_no_action(self):
        self.assertIsNone(self.decide((500, 500), (520, 510)))
        self.assertIsNone(self.decide(None, (520, 510)))

    def test_already_at_target(self):
        # y ≈ 324: 0,7706·324 + 74,3 = 324 — Windows уже поставила курсор туда же, куда и помощник
        self.assertIsNone(self.decide((1919, 324), (1922, 324)))

    def test_ex_returns_rule(self):
        tgt, rule = L.edge_decide_ex(self.rules, RECTS, (1915, 300), (1925, 300), (0, 0), CFG)
        self.assertEqual((rule.src, rule.dst, rule.kind), ("a.1", "a.2", "linear"))

    def test_garbage_does_not_raise(self):
        self.assertIsNone(L.edge_decide(self.rules, RECTS, ("x", 1), (1, 2), (0, 0), CFG))
        self.assertIsNone(L.edge_decide([], {}, (0, 0), (1, 1), (0, 0), CFG))


class TestSnapback(unittest.TestCase):
    def test_snapback(self):
        self.assertTrue(L.snapback((1925, 800), (1922, 569), (1927, 801)))
        self.assertFalse(L.snapback((1925, 800), (1922, 569), (1924, 575)))
        self.assertFalse(L.snapback((1925, 800), (1922, 569), (1960, 820)))
        self.assertFalse(L.snapback(None, (1, 1), (1, 1)))
        self.assertFalse(L.snapback((5, 5), (5, 5), (5, 5)))


if __name__ == "__main__":
    unittest.main()
