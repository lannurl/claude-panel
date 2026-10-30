"""Оркестратор вкладки «Мониторы» (docs/monitors-spec.md, §5.5, §7.5, §8) — единственное, что вызывает UI.

Этап 1: раскладка (сохранение/импорт/экспорт), экраны ОС, помощник краёв, «Применить к Windows» с откатом через
15 с, предпросмотр (dry-run). Этап 2: режим "shared" (Deskflow через deskflow_ctl): сертификат, файлы настроек,
запуск роли сервер/клиент, сверка отпечатков, восстановление при запуске. Этап 3: режим "monitor" (Apollo на хосте,
Moonlight на клиенте через stream_ctl; виртуальный экран на хосте сам встаёт на место по схеме). Этап 4: LAN-связка
(peer_link): одна кнопка — другая машина получает ту же команду; без связки режим включается локально, а
snapshot["stream"]["hint"] говорит, что нажать на другой машине.

Публичные методы не бросают исключений и не блокируют поток Tk дольше 50 мс: долгое — в рабочем потоке,
результаты — событиями on_event(kind, payload) из фоновых потоков.
"""
import json
import os
import queue
import secrets
import socket
import sys
import tempfile
import threading
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import deskflow_ctl as D
import layout as L
import peer_link as PL
import stream_ctl as S

ROLLBACK_S = 15
PEER_TIMEOUT_S = 3.0
IMPORT_LIMIT = 256 * 1024
MODES = ("off", "shared", "monitor")


def log(msg):
    try:
        if sys.stderr:
            print(f"[panel/monitors] {msg}", file=sys.stderr, flush=True)
    except Exception:
        pass


def _optional(name):
    try:
        return __import__(name)
    except Exception:
        return None


def _err(code, text, hint=None, **extra):
    d = {"ok": False, "code": code, "err": text}
    if hint:
        d["hint"] = hint
    d.update(extra)
    return d


def _atomic_write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform == "darwin":
        try:
            os.chmod(path.parent, 0o700)
        except OSError:
            pass
    fd, tmp = tempfile.mkstemp(prefix=".state-", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


class MonitorsCtl:
    VD_POLL_S = 2.0       # опрос экранов в режиме monitor на хосте (события EdgeHelper могут быть выключены)
    LINK_PING_S = 10.0    # проверка «пир на связи»

    def __init__(self, raw_cfg, config_path, data_dir, plat, on_event=None):
        self._lock = threading.RLock()
        self._plat = plat
        self._on_event = on_event
        self._config_path = Path(config_path)
        d = Path(data_dir)
        self._data_dir = d if d.name == "monitors" else d / "monitors"   # принимаем и DATA_DIR, и DATA_DIR/monitors
        self._state_path = self._data_dir / "state.json"
        self._layout, self._parse_warn = L.parse_config(raw_cfg)
        for w in self._parse_warn:
            log(w)
        self._state = self._load_state()
        self._mode = "off"
        self._busy, self._busy_text = False, ""
        self._displays = []
        self._runtime = None
        self._rules, self._rects, self._rule_warn = [], {}, []
        self._edge = None
        self._edge_paused = False   # пауза пользователя (кнопка «Пауза»), не выключение
        self._ease = None
        self._pending = None   # {"token", "previous", "deadline", "timer"} — ожидание «Оставить»
        self._jobs = queue.Queue()
        self._worker = None
        self._started = False
        self._this = self._detect_this_machine()
        self._df = None            # deskflow_ctl.Deskflow — свой процесс (или перенятый)
        self._df_path = None
        self._find = None          # кэш find_deskflow (запуск --version — не в потоке Tk)
        self._df_err = None        # последняя ошибка запуска/остановки Deskflow (для snapshot)
        self._server = None        # id машины-сервера в режиме shared
        self._fp_cache = (None, None)
        # этап 3: стрим (Apollo на хосте, Moonlight на клиенте)
        self._apollo_find = None   # кэш stream_ctl.find_host_app
        self._moon_find = None     # кэш stream_ctl.find_moonlight
        self._moon = None          # stream_ctl.Moonlight — свой процесс (или перенятый)
        self._moon_path = None
        self._stream_err = None
        self._stream_hint = None   # подсказка «нажмите … на другом ноуте» (нет связки / пир офлайн)
        self._vd_positioned = False
        self._vd_fail_key = None   # (os_id, rect, цель): неудачную позицию не повторяем (нет цикла displays_changed)
        self._vd_stop = None
        # этап 4: LAN-связка
        self._allow_loopback = False   # только тесты
        self._link = None
        self._link_secret = None
        self._link_err = None
        self._peer_online = None
        self._link_stop = None
        self._link_secret = self._load_link_secret()

    # ── служебное ──
    def _fn(self, name):
        fn = getattr(self._plat, name, None)
        return fn if callable(fn) else None

    def _emit(self, kind, payload=None):
        cb = self._on_event
        if cb:
            try:
                cb(kind, payload)
            except Exception as e:
                log(f"on_event({kind}): {type(e).__name__}")

    def _load_state(self):
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as e:
            log(f"state.json не прочитан: {type(e).__name__}")
            return {}

    def _save_state(self):
        with self._lock:
            st = dict(self._state)
            st.update({"this_machine": self._this, "updated": L.now_iso()})
        try:
            _atomic_write_json(self._state_path, st)
        except OSError as e:
            log(f"state.json не записан: {type(e).__name__}")

    def _remember_host(self, machine_id, host):
        """Записать в раскладку найденный LAN-адрес машины (host был пустым). Сбой — только в лог."""
        try:
            lay = self._layout
            ms = [replace(m, host=host) if m.id == machine_id else m for m in lay.machines]
            res = self.save_layout(replace(lay, machines=ms))
            log(f"адрес {machine_id} записан в раскладку: {host}" if res.get("ok")
                else f"адрес {machine_id} не записан: {res.get('code')}")
        except Exception as e:
            log(f"_remember_host: {type(e).__name__}")

    def _detect_this_machine(self):
        lay = self._layout
        if lay is None:
            return None
        host = socket.gethostname().lower()
        for m in lay.machines:
            if m.hostname and m.hostname.lower() == host:
                return m.id
        saved = self._state.get("this_machine")
        return saved if lay.machine(saved) else None

    def _submit(self, fn, *args):
        self._ensure_worker()
        self._jobs.put((fn, args))

    def _ensure_worker(self):
        with self._lock:
            if self._worker and self._worker.is_alive():
                return
            self._worker = threading.Thread(target=self._work, name="monitors-ctl", daemon=True)
            self._worker.start()

    def _work(self):
        while True:
            item = self._jobs.get()
            if item is None:
                return
            fn, args = item
            try:
                fn(*args)
            except Exception as e:
                log(f"задача {getattr(fn, '__name__', '?')}: {type(e).__name__}: {e}")
                self._emit("warning", {"text": "Внутренняя ошибка вкладки «Мониторы» — подробности в логе"})

    # ── экраны и правила помощника ──
    def _vmatch(self):
        """virtual_match из config + обязательные подстроки по умолчанию (SudoMaker и т.д.: в старом config их нет)."""
        lay = self._layout
        if lay is None:
            return None
        cfg = list((lay.extra.get("stream") or {}).get("virtual_match") or [])
        low = {c.lower() for c in cfg}
        return cfg + [m for m in S.DEFAULT_MATCH if m.lower() not in low]

    def _refresh_displays(self):
        fn = self._fn("list_displays")
        ds = []
        if fn:
            try:
                ds = fn(self._vmatch()) or []
            except TypeError:
                ds = fn() or []
            except Exception as e:
                log(f"list_displays: {type(e).__name__}")
        ease_fn = self._fn("ease_cursor_setting")
        ease = None
        if ease_fn:
            try:
                ease = ease_fn()
            except Exception:
                ease = None
        with self._lock:
            self._displays = ds
            self._ease = ease
        self._rebuild_rules()

    def _runtime_layout(self):
        """Раскладка, где у экранов этой машины os_rect_px/px взяты из ОС прямо сейчас (по os_id)."""
        lay, me = self._layout, self._this
        warn = []
        if lay is None or me is None:
            return None, {}, warn
        by_os = {d.get("os_id"): d for d in self._displays}
        screens, rects, used = [], {}, set()
        vd, vs_id = self._vd_current(), self._virtual_screen_id()
        for s in lay.screens:
            if vd is not None and s.id == vs_id:   # виртуальный экран = экран другой машины, ставший экраном этой
                r = tuple(int(v) for v in vd["rect_px"])
                screens.append(replace(s, machine=me, os_id=vd.get("os_id"), os_rect_px=r, px=(r[2], r[3])))
                rects[s.id] = r
                used.add(vd.get("os_id"))
                continue
            if s.machine != me:
                screens.append(s)
                continue
            d = by_os.get(s.os_id)
            if d is None:
                warn.append(f"Экран «{s.label}» сейчас не подключён")
                continue
            r = tuple(int(v) for v in d["rect_px"])
            screens.append(replace(s, os_rect_px=r, px=(r[2], r[3])))
            rects[s.id] = r
            used.add(s.os_id)
        for d in self._displays:   # экраны, которых нет в раскладке, — чтобы помощник знал, где курсор
            if d.get("os_id") not in used:
                rects[f"os:{d.get('gdi_name')}"] = tuple(int(v) for v in d["rect_px"])
                warn.append(f"Экран «{d.get('label')}» не добавлен в раскладку")
        return replace(lay, screens=tuple(screens)), rects, warn

    def _rebuild_rules(self):
        with self._lock:
            rt, rects, warn = self._runtime_layout()
            rules = []
            if rt is not None:
                inset = (rt.extra.get("edge_helper") or {}).get("inset_px", 2)
                rules, w = L.internal_rules(rt, self._this, inset)
                warn += w
            self._runtime, self._rules, self._rects, self._rule_warn = rt, rules, rects, warn
            edge = self._edge
        if edge:
            edge.set_rules(rules, rects)

    def _edge_cfg(self):
        lay = self._layout
        return dict((lay.extra.get("edge_helper") if lay else None) or L.DEFAULT_EXTRA["edge_helper"])

    def _sync_edge(self):
        """Запустить/остановить помощник по edge_helper.enabled (по умолчанию выключен)."""
        cls = getattr(self._plat, "EdgeHelper", None)
        enabled = bool(self._edge_cfg().get("enabled")) and self._started
        with self._lock:
            edge = self._edge
        if enabled and cls and edge is None:
            try:
                edge = cls(self._rules, self._rects, self._edge_cfg(), on_event=self._edge_event)
                if self._edge_paused:
                    edge.pause(True)
                edge.start()
                with self._lock:
                    self._edge = edge
                self._apply_edge_client_state()
                log("помощник краёв включён")
            except Exception as e:
                log(f"EdgeHelper: {type(e).__name__}")
                self._emit("warning", {"text": "Помощник краёв не запустился"})
        elif not enabled and edge is not None:
            with self._lock:
                self._edge = None
            try:
                edge.stop()
            except Exception as e:
                log(f"EdgeHelper.stop: {type(e).__name__}")
            log("помощник краёв выключен")

    def _edge_event(self, kind, payload):
        if kind == "displays_changed":
            self._submit(self._on_displays_changed)
        elif kind == "warning":
            self._emit("warning", payload)
        else:
            self._emit(kind, payload)

    def _on_displays_changed(self, delay=0.3):
        if delay:
            time.sleep(delay)  # Windows шлёт несколько WM_DISPLAYCHANGE подряд
        self._refresh_displays()
        self._place_vd()
        self._emit("displays_changed", None)
        self._emit("snapshot_changed", None)

    def _on_displays_polled(self):
        self._on_displays_changed(0)

    # ── публичное API ──
    def start(self):
        try:
            with self._lock:
                if self._started:
                    return
                self._started = True
            self._refresh_displays()   # ~2 мс на Windows
            self._sync_edge()
            self._submit(self._restore)   # поиск Deskflow и восстановление режима — в рабочем потоке
            self._ensure_worker()
        except Exception as e:
            log(f"start: {type(e).__name__}: {e}")

    def stop(self):
        try:
            with self._lock:
                self._started = False
                pend = self._pending
            if pend and pend.get("timer"):
                pend["timer"].cancel()   # закрытие панели во время ожидания — откатить сразу
                self._do_rollback(pend["token"])
            self._sync_edge()
            ex = self._layout.extra if self._layout else L.DEFAULT_EXTRA
            if ex.get("stop_on_exit"):
                self._stop_df()
                self._stop_moon()
            self._vd_watch_stop()
            self._link_shutdown()
            self._save_state()
            self._jobs.put(None)
        except Exception as e:
            log(f"stop: {type(e).__name__}: {e}")

    def layout(self):
        return self._layout

    def local_displays(self):
        with self._lock:
            return [dict(d) for d in self._displays]

    def snapshot(self):
        try:
            return self._snapshot()
        except Exception as e:
            log(f"snapshot: {type(e).__name__}")
            return {"this_machine": None, "mode": "off", "server": None, "busy": False, "busy_text": "",
                    "layout_ok": False, "warnings": ["Внутренняя ошибка — подробности в логе"], "components": {},
                    "deskflow": {}, "stream": {}, "edge": {}, "link": {}}

    def _deskflow_component(self):
        f = self._find
        inst = D.INSTALL[D.OS_KEY]
        if f is None:
            return {"state": "na", "note": "проверяется…", "version": None, "path": None,
                    "admin_to_install": inst["admin"], "url": inst["url"], "cmd": inst["cmd"], "err": None}
        return {"state": f.get("state") or "missing", "version": f.get("version"), "path": f.get("path"),
                "admin_to_install": inst["admin"], "url": inst["url"], "cmd": inst["cmd"],
                "err": f.get("err"), "hint": f.get("hint"), "warning": f.get("warning")}

    def _local_fp(self):
        """Отпечаток нашего сертификата (человекочитаемый) или None, если сертификата ещё нет."""
        path = self._data_dir / "deskflow" / "tls" / "deskflow.pem"
        try:
            mt = path.stat().st_mtime_ns
        except OSError:
            return None
        if self._fp_cache[0] == mt:
            return self._fp_cache[1]
        r = D.local_fingerprint(path)
        fp = r.get("fp_human") if r.get("ok") else None
        self._fp_cache = (mt, fp)
        return fp

    def _deskflow_block(self):
        with self._lock:
            df, err, server = self._df, self._df_err, self._server
        st = df.status() if df else {}
        pending = st.get("pending_peer_fp")
        connected = list(st.get("connected") or [])
        trusted = False if pending else (True if connected else None)
        return {"running": bool(st.get("running")), "role": st.get("role"), "connected": connected,
                "since": st.get("since"), "fp_local": self._local_fp(), "pending_peer_fp": pending,
                "peer_trusted": trusted, "err": err or st.get("err"), "log_tail": list(st.get("log_tail") or []),
                "server": server, "last_switch": st.get("last_switch"),
                "waiting_peer_trust": bool(st.get("waiting_peer_trust"))}

    def _snapshot(self):
        with self._lock:
            lay = self._layout
            warns = []
            if lay is not None:
                warns += L.validate(lay)
                warns += self._rule_warn
                if self._this is None:
                    warns.append("Не определено, какой это компьютер — выберите в мастере")
            edge = self._edge
            ease = self._ease
            ex = lay.extra if lay else L.DEFAULT_EXTRA
            mode, busy, busy_text = self._mode, self._busy, self._busy_text
            pending = {"token": self._pending["token"],
                       "deadline_s": max(0, round(self._pending["deadline"] - time.monotonic()))} \
                if self._pending else None
        seen, uniq = set(), []
        for w in warns:
            if w not in seen:
                seen.add(w)
                uniq.append(w)
        edge_cls = getattr(self._plat, "EdgeHelper", None)
        st = edge.status() if edge else {"running": False, "paused": False, "err": None, "warps": 0,
                                         "last_warp": None, "raw_input": False, "client_state": "local"}
        return {
            "this_machine": self._this, "mode": mode,
            "server": self._server if mode == "shared" and self._server else (ex.get("deskflow") or {}).get("server"),
            "busy": busy, "busy_text": busy_text,
            "layout_ok": lay is not None and bool(lay.screens), "warnings": uniq,
            "components": {
                "deskflow": self._deskflow_component(), "apollo": self._stream_component("apollo"),
                "moonlight": self._stream_component("moonlight"),
                "edge": {"state": "ok" if edge_cls else "unsupported", "raw_input": bool(st.get("raw_input")),
                         "ease_cursor": ease},
            },
            "deskflow": self._deskflow_block(),
            "stream": self._stream_block(mode),
            "prev_mode": self._state.get("prev_mode"),
            "edge": {"enabled": bool((ex.get("edge_helper") or {}).get("enabled")),
                     "running": bool(st.get("running")), "paused": bool(st.get("paused")) or self._edge_paused,
                     "user_paused": self._edge_paused,
                     "warps": int(st.get("warps") or 0), "last_warp": st.get("last_warp"),
                     "raw_input": bool(st.get("raw_input")), "client_state": st.get("client_state", "local"),
                     "err": st.get("err")},
            "link": self._link_block(),
            "os_positions_pending": pending,
        }

    def save_layout(self, layout):
        try:
            if not isinstance(layout, L.Layout) or not layout.machines:
                return _err("bad_layout", "Раскладка пустая", warnings=[])
            with self._lock:
                cur = self._layout
                rev = max(layout.revision, cur.revision if cur else 0)
            lay = L.normalize(replace(layout, revision=rev))
            data = L.to_config(lay)
            check, warn = L.parse_config(data)
            if check is None:
                return _err("bad_layout", "Раскладка не прошла проверку", warnings=warn)
            res = L.write_config_section(self._config_path, "monitors", data)
            if not res.get("ok"):
                return {"ok": False, "code": res.get("code"), "err": res.get("err"), "warnings": warn}
            with self._lock:
                self._layout = check
                if self._this is None:
                    self._this = self._detect_this_machine()
            self._rebuild_rules()
            self._sync_edge()
            log(f"раскладка сохранена (revision {check.revision}, экранов {len(check.screens)})")
            self._emit("snapshot_changed", None)
            return {"ok": True, "code": None, "err": None, "warnings": warn + L.validate(check)}
        except Exception as e:
            log(f"save_layout: {type(e).__name__}: {e}")
            return _err("os_error", "Не удалось сохранить раскладку", warnings=[])

    def set_this_machine(self, machine_id):
        lay = self._layout
        if lay is None or not isinstance(machine_id, str) or lay.machine(machine_id) is None:
            return _err("bad_config", "Такой машины нет в раскладке")
        with self._lock:
            self._this = machine_id
            self._state["this_machine"] = machine_id
        self._save_state()
        self._rebuild_rules()
        self._emit("snapshot_changed", None)
        return {"ok": True, "code": None, "err": None}

    def set_mode(self, mode, server=None):
        """Асинхронно: {"ok": True, "pending": True}. Если связка работает, другая машина получает ту же команду."""
        return self._set_mode(mode, server, False)

    def _set_mode(self, mode, server, from_peer):
        if mode not in MODES:
            return _err("bad_config", "Неизвестный режим")
        if mode == "monitor":
            bad = self._monitor_precheck()
            if bad:
                return bad
            if not self._begin_busy("Включаю режим «Ноут как монитор»…"):
                return _err("busy", "Предыдущее действие ещё выполняется")
            self._submit(self._job_monitor, from_peer)
            return {"ok": True, "code": None, "err": None, "pending": True}
        if mode == "shared":
            lay = self._layout
            if lay is None or len(lay.machines) < 2:
                return _err("bad_layout", "Для общей мыши нужна раскладка минимум из двух машин")
            if self._this is None:
                return _err("bad_config", "Не определено, какой это компьютер — выберите в мастере")
            if server is not None and (not isinstance(server, str) or lay.machine(server) is None):
                return _err("bad_config", "Такой машины нет в раскладке")
            if server is None and self._mode == "monitor":
                server = self._state.get("prev_server")   # возврат из monitor — к прежнему серверу
            sid = self._pick_server(server)
            if not self._begin_busy("Запускаю Deskflow…"):
                return _err("busy", "Предыдущее действие ещё выполняется")
            self._submit(self._job_shared, sid, from_peer)
            return {"ok": True, "code": None, "err": None, "pending": True}
        if not self._begin_busy("Останавливаю…"):
            return _err("busy", "Предыдущее действие ещё выполняется")
        self._submit(self._job_off, from_peer)
        return {"ok": True, "code": None, "err": None, "pending": True}

    # ── Deskflow (этап 2) ──
    def _begin_busy(self, text):
        with self._lock:
            if self._busy or self._pending:
                return False
            self._busy, self._busy_text = True, text
        self._emit("snapshot_changed", None)
        return True

    def _end_busy(self):
        with self._lock:
            self._busy, self._busy_text = False, ""

    def _pick_server(self, server=None):
        """Аргумент → deskflow.server из раскладки → сохранённый в state → первая машина."""
        lay = self._layout
        for c in (server, (lay.extra.get("deskflow") or {}).get("server"), self._state.get("server")):
            if c and lay.machine(c):
                return c
        return lay.machines[0].id

    def _df_cfg(self):
        lay = self._layout
        return dict((lay.extra.get("deskflow") if lay else None) or L.DEFAULT_EXTRA["deskflow"])

    def _do_find(self, force=False):
        with self._lock:
            if self._find is not None and not force:
                return self._find
        f = D.find_deskflow(self._df_cfg().get("path"))
        with self._lock:
            self._find = f
        return f

    def _ensure_df(self, find):
        """Объект Deskflow под найденный путь; при смене пути старый процесс останавливается."""
        with self._lock:
            df, path = self._df, self._df_path
        if df is not None and path == find["path"]:
            return df
        if df is not None:
            df.stop()
        df = D.Deskflow(find["path"], find["dialect"], self._data_dir, on_event=self._df_event)
        with self._lock:
            self._df, self._df_path = df, find["path"]
        return df

    def _stop_df(self):
        with self._lock:
            df = self._df
        if df is None:
            return {"ok": True}
        res = df.stop()
        with self._lock:
            self._state.pop("deskflow", None)
        return res

    def _df_event(self, kind, payload):
        """События из потока-читателя лога Deskflow."""
        if kind == "deskflow_exited":
            with self._lock:
                was = self._mode == "shared"
                if was:
                    self._mode = "off"
                    self._state["mode"] = "off"
                    self._state.pop("deskflow", None)
                err = (self._df.status().get("err") if self._df else None) or "Deskflow завершился"
            if was:
                self._save_state()
                self._apply_edge_client_state()
                self._emit("warning", {"text": f"Deskflow остановился: {err}"})
                self._emit("mode_changed", {"mode": "off"})
            self._emit("snapshot_changed", None)
            return
        if kind in ("deskflow_connected", "deskflow_disconnected", "deskflow_untrusted_peer", "deskflow_switch"):
            self._emit(kind, payload)
        self._emit("snapshot_changed", None)

    def _apply_edge_client_state(self):
        """Помощник краёв на Deskflow-клиенте: client_relative (spec §7.4.1; запасная эвристика отката есть в
        EdgeHelper), иначе local."""
        with self._lock:
            edge = self._edge
            client = self._mode == "shared" and self._this is not None and self._server not in (None, self._this)
        fn = getattr(edge, "set_client_state", None) if edge is not None else None
        if callable(fn):
            try:
                fn("client_relative" if client else "local")
            except Exception as e:
                log(f"set_client_state: {type(e).__name__}")

    def _start_shared(self, server_id):
        """Остановить своё, затем запустить роль. Возвращает словарь результата (в рабочем потоке)."""
        lay = self._layout
        me, srv = lay.machine(self._this), lay.machine(server_id)
        if me is None or srv is None:
            return _err("bad_config", "Не определены машины")
        find = self._do_find()
        if not find.get("ok"):
            return _err(find.get("code") or "not_found", find.get("err") or "Deskflow не найден", find.get("hint"))
        with self._lock:
            moon = self._moon
        if moon is not None and moon.status().get("running"):   # инвариант §8.1 (а)
            return _err("conflict", "Moonlight ещё работает — Moonlight и Deskflow-клиент не запускаются вместе",
                        "остановите стрим («Выкл» или «Отдельный ноут»)")
        df = self._ensure_df(find)
        df.stop()
        cfg = self._df_cfg()
        tls, port, clip = bool(cfg.get("tls", True)), int(cfg.get("port", 24800)), bool(cfg.get("clipboard", True))
        names = [m.name for m in lay.machines]
        cert = None
        if tls:
            c = D.ensure_certificate(self._data_dir)
            if not c.get("ok"):
                return c
            cert = c["path"]
        if me.id == srv.id:
            other = next((m for m in lay.machines if m.id != me.id), None)
            bind, err = D.resolve_bind_host(me.host, other.host if other else None)
            if err:
                return _err("bad_config", err)
            if not (me.host or "").strip():
                self._remember_host(me.id, bind)   # чтобы раскладка для второго компьютера несла адрес сервера
            with self._lock:
                rt = self._runtime
            dec = cfg.get("percent_decimals")
            if dec is None:
                dec = (find.get("dialect") or {}).get("decimals") or 2
            links, _w = L.deskflow_links(rt or lay, decimals=dec, band_pct=cfg.get("corner_band_pct", 1.0))
            conf = D.build_server_config(rt or lay, links, {"server": srv.id})
            return df.start_server(conf, me.name, bind, port, tls, machines=names, cert_path=cert, clipboard=clip)
        if not srv.host:
            return _err("bad_config", "Не указан адрес сервера в раскладке")
        return df.start_client(me.name, srv.host, port, tls, machines=names, cert_path=cert, clipboard=clip,
                               server_name=srv.name)

    def _job_shared(self, server_id, from_peer=True):
        try:
            self._begin_leave_monitor()
            self._stop_stray_stream()
            try:
                res = self._start_shared(server_id)
            except (ValueError, OSError) as e:
                log(f"start_shared: {type(e).__name__}: {e}")
                res = _err("bad_config", "Не удалось подготовить запуск Deskflow")
            with self._lock:
                df = self._df
            if res.get("ok"):
                with self._lock:
                    self._mode, self._server, self._df_err = "shared", server_id, None
                    self._state.update({"mode": "shared", "server": server_id,
                                        "deskflow": df.process_info() if df else None})
                log(f"режим shared, сервер {server_id}")
                if not from_peer:
                    self._notify_peer("shared", server_id)
            else:
                with self._lock:
                    self._mode, self._df_err = "off", res.get("err")
                    self._state.update({"mode": "off"})
                    self._state.pop("deskflow", None)
                log(f"shared не запущен: {res.get('code')}")
                self._emit("warning", {"text": res.get("err") or "Deskflow не запустился"})
            self._save_state()
        finally:
            self._end_busy()
        self._apply_edge_client_state()
        self._emit("mode_changed", {"mode": self._mode})
        self._emit("snapshot_changed", None)

    def _job_off(self, from_peer=True):
        try:
            self._begin_leave_monitor()
            self._stop_stray_stream()
            self._stop_df()
            with self._lock:
                self._mode, self._df_err = "off", None
                self._state["mode"] = "off"
            self._save_state()
            if not from_peer:
                self._notify_peer("off", None)
        finally:
            self._end_busy()
        self._apply_edge_client_state()
        self._emit("mode_changed", {"mode": "off"})
        self._emit("snapshot_changed", None)

    def _restore(self):
        """При запуске панели: найти Deskflow, перенять живой процесс из state.json или запустить режим заново."""
        self._sync_link()
        self._do_find_stream()
        find = self._do_find()
        self._emit("snapshot_changed", None)
        lay = self._layout
        st = self._state
        if lay is not None and self._this is not None and st.get("mode") == "monitor":
            self._restore_monitor()
            return
        if lay is None or self._this is None or st.get("mode") != "shared" or not find.get("ok"):
            return
        server = st.get("server") if lay.machine(st.get("server")) else None
        if server is None:
            return
        startup = lay.extra.get("startup_mode", "last")
        info = st.get("deskflow")
        df = self._ensure_df(find)
        if info and df.adopt(info):
            if startup == "off":
                self._stop_df()
                self._job_off_state()
                return
            with self._lock:
                self._mode, self._server = "shared", server
            self._apply_edge_client_state()
            self._emit("mode_changed", {"mode": "shared"})
            self._emit("snapshot_changed", None)
            return
        if startup == "last" and self._begin_busy("Восстанавливаю Deskflow…"):
            self._job_shared(server)
        else:
            self._job_off_state()

    def _restore_monitor(self):
        """После перезапуска панели: клиент перенимает живой Moonlight, иначе режим запускается заново."""
        lay, st = self._layout, self._state
        if lay.extra.get("startup_mode", "last") == "off":
            self._job_off_state()
            return
        if self._role() == "client":
            moon, info = self._ensure_moon(), st.get("moonlight")
            if moon is not None and info and moon.adopt(info):
                with self._lock:
                    self._mode, self._server = "monitor", None
                self._apply_edge_client_state()
                self._emit("mode_changed", {"mode": "monitor"})
                self._emit("snapshot_changed", None)
                return
        if self._begin_busy("Восстанавливаю режим «Ноут как монитор»…"):
            self._job_monitor(True)
        else:
            self._job_off_state()

    def _job_off_state(self):
        with self._lock:
            self._mode = "off"
            self._state["mode"] = "off"
            self._state.pop("deskflow", None)
            self._state.pop("moonlight", None)
        self._save_state()
        self._emit("snapshot_changed", None)

    def set_edge_helper(self, enabled):
        if not isinstance(enabled, bool):
            return _err("bad_config", "Ожидалось вкл/выкл")
        lay = self._layout
        if lay is None:
            return _err("bad_layout", "Сначала сохраните раскладку")
        ex = json.loads(json.dumps(lay.extra))
        ex.setdefault("edge_helper", dict(L.DEFAULT_EXTRA["edge_helper"]))["enabled"] = enabled
        if enabled:
            with self._lock:
                edge = self._edge
            if edge is not None and edge.status().get("tripped"):   # повторное включение снимает аварийную паузу
                with self._lock:
                    self._edge = None
                edge.stop()
        res = self.save_layout(replace(lay, extra=ex))
        if res.get("ok") and enabled and getattr(self._plat, "EdgeHelper", None) is None:
            res["warnings"] = res.get("warnings", []) + ["Помощник краёв на этой ОС пока недоступен"]
        return res

    def pause_edge_helper(self, paused):
        """Пауза помощника краёв без выключения (config не меняется). {"ok","code","err","paused","running"}."""
        if not isinstance(paused, bool):
            return _err("bad_config", "Ожидалось вкл/выкл паузы")
        with self._lock:
            self._edge_paused = paused
            edge = self._edge
        if edge is not None:
            try:
                edge.pause(paused)
            except Exception as e:
                log(f"EdgeHelper.pause: {type(e).__name__}")
                return _err("os_error", "Не удалось поставить помощник на паузу")
        log("помощник краёв на паузе" if paused else "помощник краёв снят с паузы")
        self._emit("snapshot_changed", None)
        return {"ok": True, "code": None, "err": None, "paused": paused, "running": edge is not None}

    def refresh_displays(self):
        """Синхронно перечитать экраны ОС (≈2 мс на Windows) и вернуть копию списка; start() не нужен."""
        try:
            self._refresh_displays()
        except Exception as e:
            log(f"refresh_displays: {type(e).__name__}")
        return self.local_displays()

    def preview(self):
        try:
            with self._lock:
                lay, rt, rules, me = self._layout, self._runtime, list(self._rules), self._this
                warn = list(self._rule_warn)
            if lay is None:
                return _err("bad_layout", "Раскладки нет")
            df = lay.extra.get("deskflow") or {}
            dec = df.get("percent_decimals")
            if dec is None:
                dec = ((self._find or {}).get("dialect") or {}).get("decimals") or 2
            links, w = L.deskflow_links(rt or lay, decimals=dec,
                                        band_pct=df.get("corner_band_pct", 1.0))
            warn += w
            conf, argv, settings = None, None, None
            sid = self._pick_server()
            try:
                conf = D.build_server_config(rt or lay, links, {"server": sid, "clipboard": df.get("clipboard", True)})
            except (ValueError, KeyError) as e:
                log(f"build_server_config: {type(e).__name__}")
                warn.append("Конфиг Deskflow не построен — проверьте имена машин")
            argv, settings = self._preview_launch(lay, me, sid, df, warn)
            positions = L.suggest_os_positions(rt, me) if rt is not None and me else {}
            return {"ok": True, "code": None, "err": None, "deskflow_conf": conf, "argv": argv,
                    "deskflow_settings": settings,
                    "links": [lk.__dict__ for lk in links], "rules": [r.__dict__ for r in rules],
                    "os_positions": positions, "warnings": warn}
        except Exception as e:
            log(f"preview: {type(e).__name__}: {e}")
            return _err("os_error", "Предпросмотр не построен")

    def _preview_launch(self, lay, me_id, server_id, df, warn):
        """argv и текст настроек для предпросмотра. Без сети и запуска процессов (вызов из потока Tk)."""
        me = lay.machine(me_id) if me_id else None
        if me is None or lay.machine(server_id) is None:
            warn.append("Команда запуска Deskflow не показана: не определено, какой это компьютер")
            return None, None
        find = self._find
        if not find or not find.get("ok"):
            warn.append("Команда запуска Deskflow не показана: Deskflow не найден или ещё проверяется")
            return None, None
        srv = lay.machine(server_id)
        d = self._data_dir / "deskflow"
        names = [m.name for m in lay.machines]
        tls, port = bool(df.get("tls", True)), int(df.get("port", 24800))
        cert = d / "tls" / "deskflow.pem"
        try:
            if me.id == srv.id:
                bind = (me.host or "").strip() or "<LAN-адрес этой машины>"
                if bind in ("0.0.0.0", "::"):
                    warn.append("Адрес сервера 0.0.0.0 недопустим")
                    return None, None
                text = D.build_settings("server", name=me.name, machines=names, bind_host=bind, port=port, tls=tls,
                                        cert_path=cert, links_path=d / "server.conf",
                                        clipboard=bool(df.get("clipboard", True)))
                role = "server"
            else:
                text = D.build_settings("client", name=me.name, machines=names, server_host=srv.host or "<адрес сервера>",
                                        port=port, tls=tls, cert_path=cert, clipboard=bool(df.get("clipboard", True)))
                role = "client"
            argv = D.build_argv(role, find["path"], find["dialect"], settings_file=d / "settings.conf")
            return argv, text
        except ValueError as e:
            warn.append(f"Настройки Deskflow не построены: {e}")
            return None, None

    # ── «Применить к Windows» с откатом ──
    def _positions_by_os_id(self):
        with self._lock:
            rt, me = self._runtime, self._this
        if rt is None or me is None:
            return None, None, "Не определено, какой это компьютер, или раскладки нет"
        pos = L.suggest_os_positions(rt, me)
        by_os = {}
        for sid, xy in pos.items():
            s = rt.screen(sid)
            if s and s.os_id:
                by_os[s.os_id] = xy
        if len(by_os) < 2:
            return None, None, "Для раскладки нужно хотя бы два подключённых экрана этой машины"
        return pos, by_os, None

    def apply_os_positions(self, dry_run=False):
        try:
            fn = self._fn("set_display_positions")
            if fn is None:
                return _err("unsupported_os", "Изменение раскладки экранов на этой ОС недоступно")
            pos, by_os, err = self._positions_by_os_id()
            if err:
                return _err("bad_layout", err)
            if dry_run:
                res = fn(by_os, dry_run=True)
                return {**res, "token": None, "positions": pos}
            with self._lock:
                if self._pending or self._busy:
                    return _err("busy", "Уже ждём подтверждения прошлой раскладки")
                token = secrets.token_hex(8)
                self._busy, self._busy_text = True, "Меняю раскладку Windows…"
            self._submit(self._do_apply, token, by_os)
            return {"ok": True, "code": None, "err": None, "pending": True, "token": token, "positions": pos}
        except Exception as e:
            log(f"apply_os_positions: {type(e).__name__}: {e}")
            return _err("os_error", "Не удалось изменить раскладку")

    def _do_apply(self, token, by_os):
        fn = self._fn("set_display_positions")
        try:
            res = fn(by_os)
        finally:
            with self._lock:
                self._busy, self._busy_text = False, ""
        if not res.get("ok"):
            self._emit("warning", {"text": res.get("err") or "Раскладка не применена"})
            self._emit("snapshot_changed", None)
            return
        if not res.get("plan"):
            self._emit("warning", {"text": "Раскладка Windows уже совпадает с физической"})
            self._emit("snapshot_changed", None)
            return
        timer = threading.Timer(ROLLBACK_S, lambda: self._submit(self._do_rollback, token))
        timer.daemon = True
        with self._lock:
            self._pending = {"token": token, "previous": res.get("previous") or {},
                             "deadline": time.monotonic() + ROLLBACK_S, "timer": timer}
        timer.start()
        self._refresh_displays()
        self._emit("os_positions_pending", {"token": token, "deadline_s": ROLLBACK_S})
        self._emit("snapshot_changed", None)

    def _do_rollback(self, token):
        with self._lock:
            p = self._pending
            if not p or p["token"] != token:
                return
            self._pending = None
        if p.get("timer"):
            p["timer"].cancel()
        fn = self._fn("set_display_positions")
        res = fn(p["previous"]) if fn else {"ok": False, "err": "нет функции"}
        log("раскладка Windows возвращена" if res.get("ok") else f"откат не удался: {res.get('code')}")
        if not res.get("ok"):
            self._emit("warning", {"text": "Не удалось вернуть прежнюю раскладку Windows — "
                                           "проверьте «Параметры → Дисплей»"})
        self._refresh_displays()
        self._emit("snapshot_changed", None)

    def confirm_os_positions(self, token):
        with self._lock:
            p = self._pending
            if not p or not isinstance(token, str) or not secrets.compare_digest(p["token"], token):
                return _err("bad_config", "Нечего подтверждать")
            self._pending = None
        p["timer"].cancel()
        log("новая раскладка Windows оставлена")
        self._emit("snapshot_changed", None)
        return {"ok": True, "code": None, "err": None}

    def rollback_os_positions(self, token):
        with self._lock:
            p = self._pending
            if not p or not isinstance(token, str) or not secrets.compare_digest(p["token"], token):
                return _err("bad_config", "Нечего откатывать")
        p["timer"].cancel()
        self._submit(self._do_rollback, token)
        return {"ok": True, "code": None, "err": None, "pending": True}

    # ── прочее ──
    def refresh_components(self):
        def job():
            self._do_find(force=True)
            self._do_find_stream(force=True)
            self._refresh_displays()
            self._emit("snapshot_changed", None)
        self._submit(job)

    def trust_peer(self, machine_id, fp):
        """Доверять отпечатку собеседника: сервер пишет в trusted-clients, клиент — в trusted-servers."""
        try:
            lay = self._layout
            if lay is None or not isinstance(machine_id, str) or lay.machine(machine_id) is None:
                return _err("bad_config", "Такой машины нет в раскладке")
            if self._this is None:
                return _err("bad_config", "Не определено, какой это компьютер — выберите в мастере")
            if machine_id == self._this:
                return _err("bad_config", "Нужен отпечаток другой машины, а не этой")
            h = D.normalize_fp(fp)
            if not h:
                return _err("bad_config", "Отпечаток должен быть 64 шестнадцатеричных символа (SHA-256)")
            with self._lock:
                df = self._df
            st = df.status() if df else {}
            if st.get("running") and st.get("role"):
                role = st["role"]
            else:
                role = "server" if self._pick_server() == self._this else "client"
            res = D.add_trusted(role, h)
            if not res.get("ok"):
                return res
            if df is not None:
                df.clear_pending(h)
            log(f"доверенный отпечаток {machine_id} принят ({role})")
            self._emit("snapshot_changed", None)
            return {"ok": True, "code": None, "err": None, "role": role, "added": res.get("added"),
                    "path": res.get("path")}
        except Exception as e:
            log(f"trust_peer: {type(e).__name__}: {e}")
            return _err("os_error", "Не удалось сохранить отпечаток")

    def export_layout(self):
        lay = self._layout
        if lay is None:
            return ""
        data = L.to_config(lay)
        data["revision"] = lay.revision
        return json.dumps(data, ensure_ascii=False, indent=2)

    def import_layout(self, text):
        try:
            if not isinstance(text, str) or not text.strip():
                return _err("bad_layout", "Буфер пуст")
            if len(text) > IMPORT_LIMIT:
                return _err("bad_layout", "Слишком большой текст")
            try:
                data = json.loads(text)
            except ValueError:
                return _err("bad_layout", "В буфере не JSON раскладки")
            if isinstance(data, dict) and "monitors" in data and "machines" not in data:
                data = data["monitors"]
            lay, warn = L.parse_config(data)
            if lay is None or not lay.screens:
                return _err("bad_layout", "Раскладка не распознана", warnings=warn)
            res = self.save_layout(lay)
            res["warnings"] = warn + res.get("warnings", [])
            return res
        except Exception as e:
            log(f"import_layout: {type(e).__name__}")
            return _err("bad_layout", "Раскладка не импортирована")

    def export_for_deskflow_gui(self):
        lay = self._layout
        if lay is None:
            return _err("bad_layout", "Раскладки нет")
        df = lay.extra.get("deskflow") or {}
        server = lay.machine(df.get("server")) or lay.machines[0]
        me = lay.machine(self._this) if self._this else None
        conf = None
        links, _ = L.deskflow_links(lay)
        try:
            conf = D.build_server_config(lay, links, {"server": server.id, "clipboard": df.get("clipboard", True)})
        except (ValueError, KeyError) as e:
            log(f"build_server_config: {type(e).__name__}")
        return {"ok": True, "code": None, "err": None, "server_conf": conf, "server_host": server.host,
                "port": df.get("port", 24800), "screen_name": me.name if me else None}

    # ══ Этап 3: режим «Ноут как монитор» ══
    def _peer_id(self):
        lay, me = self._layout, self._this
        if lay is None or me is None:
            return None
        return next((m.id for m in lay.machines if m.id != me), None)

    def _mname(self, mid):
        m = self._layout.machine(mid) if self._layout and mid else None
        return m.name if m else (mid or "другой компьютер")

    def _host_id(self):
        lay = self._layout
        return (lay.extra.get("stream") or {}).get("host_machine") if lay else None

    def _role(self):
        """"host" — у машины Apollo (хост стрима); "client" — Moonlight; None — машина не определена.
        Хост = stream.host_machine из config; только если он не определён — та, где найден Apollo (без Moonlight)."""
        if self._layout is None or self._this is None:
            return None
        host = self._host_id()
        if host is not None and self._layout.machine(host) is not None:
            return "host" if self._this == host else "client"
        a, m = self._apollo_find, self._moon_find
        if a and a.get("state") == "ok" and not (m and m.get("state") == "ok"):
            return "host"
        return "client"

    def _stream_cfg(self):
        lay = self._layout
        return dict((lay.extra.get("stream") if lay else None) or L.DEFAULT_EXTRA["stream"])

    def _client_machine(self):
        return self._peer_id() if self._role() == "host" else self._this

    def _virtual_screen_id(self):
        """Экран раскладки, которым становится виртуальный дисплей: stream.virtual_screen, иначе первый экран
        машины-клиента (по умолчанию b.1)."""
        lay = self._layout
        if lay is None:
            return None
        vs = self._stream_cfg().get("virtual_screen")
        if vs and lay.screen(vs):
            return vs
        cm = self._client_machine()
        return next((s.id for s in lay.screens if s.machine == cm), None)

    def _vd_current(self):
        """Виртуальный дисплей среди экранов — только на хосте в режиме monitor."""
        with self._lock:
            if self._mode != "monitor" or self._role() != "host":
                return None
            return S.virtual_display_present(self._displays, self._vmatch())

    def _do_find_stream(self, force=False):
        with self._lock:
            if self._apollo_find is not None and self._moon_find is not None and not force:
                return
        st = self._stream_cfg()
        a = S.find_host_app(st.get("host_app") or "apollo")
        m = S.find_moonlight(st.get("moonlight_path"))
        with self._lock:
            self._apollo_find, self._moon_find = a, m

    def _ensure_moon(self):
        with self._lock:
            mf, moon, path = self._moon_find, self._moon, self._moon_path
        if not mf or not mf.get("ok"):
            return None
        if moon is not None and path == mf["path"]:
            return moon
        if moon is not None:
            moon.stop(None)
        moon = S.Moonlight(mf["path"], mf.get("dialect"), self._data_dir, on_event=self._moon_event)
        with self._lock:
            self._moon, self._moon_path = moon, mf["path"]
        return moon

    def _stop_moon(self, host=None):
        with self._lock:
            moon = self._moon
        if moon is None:
            return {"ok": True}
        res = moon.stop(host)
        with self._lock:
            self._state.pop("moonlight", None)
        return res

    def _moon_event(self, kind, payload):
        if kind != "moonlight_exited":
            self._emit(kind, payload)
            return
        with self._lock:
            was = self._mode == "monitor" and self._role() == "client"
            err = (self._moon.status().get("err") if self._moon else None) or "Moonlight завершился"
            if was:
                self._mode = "off"
                self._state["mode"] = "off"
                self._state.pop("moonlight", None)
                self._stream_err = err
        if was:
            self._save_state()
            self._emit("warning", {"text": f"Moonlight остановился: {err}"})
            self._emit("mode_changed", {"mode": "off"})
        self._emit("snapshot_changed", None)

    def _stream_component(self, which):
        role = self._role()
        inst = S.install_info("apollo" if which == "apollo" else "moonlight")
        if which == "apollo":
            f, need = self._apollo_find, role in (None, "host")
            where = self._mname(self._host_id())
        else:
            f, need = self._moon_find, role in (None, "client")
            where = self._mname(self._client_machine() or self._peer_id())
        if not need:
            return {"state": "na", "note": f"нужен на {where}", "err": None}
        if f is None:
            return {"state": "na", "note": "проверяется…", "err": None}
        d = {"state": f.get("state") or "missing", "version": f.get("version"), "path": f.get("path"),
             "admin_to_install": inst["admin"], "url": inst["url"], "cmd": inst["cmd"], "err": f.get("err"),
             "hint": f.get("hint"), "warning": f.get("warning"), "note": None}
        if which == "apollo":
            d.update(running=bool(f.get("running")), web_ui=f.get("web_ui"), kind=self._stream_cfg().get("host_app"))
        return d

    def _stream_block(self, mode):
        role = self._role()
        with self._lock:
            moon, err, hint, pos = self._moon, self._stream_err, self._stream_hint, self._vd_positioned
            vd = S.virtual_display_present(self._displays, self._vmatch()) if (mode == "monitor" and role == "host") \
                else None
        if role == "client":
            running = bool(moon and moon.status().get("running")) if mode == "monitor" else False
        else:
            running = vd is not None
        if mode == "monitor" and not running and not hint and not err:
            hint = (f"Жду, когда {self._mname(self._peer_id())} подключится через Moonlight" if role == "host"
                    else "Moonlight не запущен")
        if running:
            hint = None
        stray = None
        if mode != "monitor" and role == "host":
            with self._lock:
                stray = S.virtual_display_present(self._displays, self._vmatch())
            if stray:
                hint = (f"{self._mname(self._peer_id())} ещё показывает экран (Moonlight открыт вне режима). "
                        f"Нажмите там «Выкл» или закройте Moonlight; со связкой это делается само.")
        return {"running": running, "role": role, "waiting": mode == "monitor" and not running,
                "stray_stream": bool(stray),
                "virtual_display": (vd.get("label") or vd.get("gdi_name")) if vd else None,
                "positioned": bool(pos and vd is not None), "virtual_screen": self._virtual_screen_id(),
                "app_name": self._stream_cfg().get("app_name"), "err": err, "hint": hint}

    # ── виртуальный экран: автоматически на место по схеме ──
    def _vd_target(self, vd):
        """(x, y, None) — позиция VD в пикселях ОС по физической раскладке; (None, None, текст) — нельзя."""
        vs_id, me = self._virtual_screen_id(), self._this
        with self._lock:
            rt = self._runtime
        vs = rt.screen(vs_id) if rt is not None and vs_id else None
        if vs is None or vs.machine != me:
            return None, None, "Экран для виртуального дисплея не найден в раскладке"
        w, h = int(vd["rect_px"][2]), int(vd["rect_px"][3])
        for side in L.SIDES:
            for nid in L.neighbors(rt, vs_id, side):
                n = rt.screen(nid)
                if n is None or n.machine != me or not n.os_rect_px:
                    continue
                nx, ny, nw, nh = n.os_rect_px
                tmp = L.Layout(rt.machines, (replace(n, os_rect_px=(0, 0, nw, nh)),
                                             replace(vs, os_rect_px=(nw, 0, w, h))), 0, rt.extra)
                pos = L.suggest_os_positions(tmp, me)
                if vs_id in pos:
                    return nx + pos[vs_id][0], ny + pos[vs_id][1], None
        return None, None, f"Экран «{vs.label}» не соседствует с экранами этого компьютера — поправьте раскладку"

    def _place_vd(self):
        """Поставить появившийся виртуальный экран на место (без подтверждения; при ошибке — откат), пересчитать
        правила помощника краёв. Основной экран не меняется."""
        vd = self._vd_current()
        if vd is None:
            with self._lock:
                self._vd_positioned = False
                self._vd_fail_key = None
            return
        if vd.get("primary"):
            self._set_stream_err("Виртуальный экран стал основным — отключите «Enforce Virtual Display Primary»")
            return
        x, y, why = self._vd_target(vd)
        if why:
            self._set_stream_err(why)
            return
        cur = tuple(int(v) for v in vd["rect_px"][:2])
        if cur == (x, y):
            self._vd_ok()
            return
        key = (vd.get("os_id"), tuple(vd["rect_px"]), (x, y))
        if key == self._vd_fail_key:
            return
        fn = self._fn("set_display_positions")
        if fn is None:
            self._set_stream_err("Изменение раскладки экранов на этой ОС недоступно")
            return
        res = fn({vd["os_id"]: (x, y)})
        if not res.get("ok"):
            prev = (res.get("previous") or {}).get(vd["os_id"])
            if prev is not None and res.get("code") == "os_error":
                fn({vd["os_id"]: prev})   # откат; основной экран не двигаем
            self._vd_fail_key = key
            self._set_stream_err(res.get("err") or "Не удалось поставить виртуальный экран на место")
            self._refresh_displays()
            return
        self._refresh_displays()
        after = self._vd_current()
        if after is None or tuple(int(v) for v in after["rect_px"][:2]) != (x, y):
            self._vd_fail_key = key
            self._set_stream_err("Windows не приняла позицию виртуального экрана")
            return
        self._vd_ok()
        log(f"виртуальный экран поставлен в ({x}, {y})")

    def _vd_ok(self):
        with self._lock:
            self._vd_positioned, self._stream_err, self._stream_hint, self._vd_fail_key = True, None, None, None

    def _set_stream_err(self, text):
        with self._lock:
            changed = self._stream_err != text
            self._stream_err, self._vd_positioned = text, False
        if changed:
            log(f"стрим: {text}")
            self._emit("warning", {"text": text})

    def _vd_watch_start(self):
        with self._lock:
            if self._vd_stop is not None and not self._vd_stop.is_set():
                return
            evt = threading.Event()
            self._vd_stop = evt
        threading.Thread(target=self._vd_loop, args=(evt,), name="monitors-vd", daemon=True).start()

    def _vd_watch_stop(self):
        with self._lock:
            evt, self._vd_stop = self._vd_stop, None
        if evt is not None:
            evt.set()

    def _vd_loop(self, evt):
        """Опрос экранов (события EdgeHelper приходят, только если он включён): изменение → _on_displays_polled."""
        last, wait = None, 0.05
        while not evt.wait(wait):
            wait = self.VD_POLL_S
            fn = self._fn("list_displays")
            try:
                ds = (fn(self._vmatch()) if fn else []) or []
            except Exception:
                continue
            sig = tuple(sorted((str(d.get("os_id")), tuple(d.get("rect_px") or ())) for d in ds))
            if sig != last:
                last = sig
                self._submit(self._on_displays_polled)

    # ── переключение режимов ──
    def _begin_leave_monitor(self):
        """Выход из monitor на этой машине: остановить Moonlight (клиент) и сторож VD; режим → off, правила без VD."""
        with self._lock:
            if self._mode != "monitor":
                return
            role = self._role()
        self._vd_watch_stop()
        if role == "client":
            host = self._layout.machine(self._host_id())
            self._stop_moon(host.host if host and S.valid_host(host.host or "") else None)
        with self._lock:
            self._mode = "off"
            self._state["mode"] = "off"
            self._stream_err = self._stream_hint = None
            self._vd_positioned, self._vd_fail_key = False, None
        self._rebuild_rules()   # без VD

    def _stop_stray_stream(self):
        """Не в режиме monitor картинки быть не должно: клиент закрывает любой Moonlight, даже открытый вручную
        (`quit` у хоста закрывает сеанс Apollo и виртуальный экран)."""
        if self._role() != "client":
            return
        with self._lock:
            moon = self._moon
        own = moon.status().get("pid") if moon is not None else None
        stray = S.moonlight_pids(own)
        if not stray:
            return
        mf = self._moon_find or {}
        host = self._layout.machine(self._host_id()) if self._layout else None
        if mf.get("ok") and host is not None and S.valid_host(host.host or ""):
            try:
                self._ensure_moon().stop(host.host)
            except Exception as e:
                log(f"stray quit: {type(e).__name__}")
        for pid in S.moonlight_pids(own):
            D._kill_pid(pid, False)
        log(f"закрыт Moonlight вне режима monitor: {len(stray)}")

    def _monitor_precheck(self):
        lay = self._layout
        if lay is None or len(lay.machines) < 2:
            return _err("bad_layout", "Для режима «Ноут как монитор» нужна раскладка минимум из двух машин")
        if self._this is None:
            return _err("bad_config", "Не определено, какой это компьютер — выберите в мастере")
        if self._role() == "client":
            host = lay.machine(self._host_id())
            if host is None or not S.valid_host(host.host or ""):
                return _err("bad_config", "Не указан адрес компьютера с Apollo — заполните его в раскладке")
        return None

    def _job_monitor(self, from_peer):
        try:
            self._do_find_stream(force=True)
            res = self._enter_monitor_host() if self._role() == "host" else self._enter_monitor_client()
            if res.get("ok"):
                self._save_state()
                log(f"режим monitor ({self._role()})")
                if not from_peer:
                    self._notify_peer("monitor", None)
            else:
                with self._lock:
                    self._stream_err = res.get("err")
                log(f"monitor не включён: {res.get('code')}")
                self._emit("warning", {"text": res.get("err") or "Режим не включился"})
        finally:
            self._end_busy()
        self._apply_edge_client_state()
        self._emit("mode_changed", {"mode": self._mode})
        self._emit("snapshot_changed", None)
        if self._role() == "host" and self._mode == "monitor":
            self._on_displays_changed(0)   # VD мог появиться раньше

    def _remember_prev(self):
        with self._lock:
            if self._mode in ("off", "shared"):
                self._state["prev_mode"] = self._mode
                self._state["prev_server"] = self._server if self._mode == "shared" else None

    def _enter_monitor_host(self):
        a = self._apollo_find
        if not a or not a.get("ok"):
            return _err((a or {}).get("code") or "not_found", (a or {}).get("err") or "Apollo не найден",
                        (a or {}).get("hint"))
        self._remember_prev()
        self._stop_df()   # инвариант §8.1 (б): в monitor Deskflow остановлен
        with self._lock:
            self._mode, self._server, self._df_err = "monitor", None, None
            self._stream_hint = None
            self._stream_err = None if a.get("running") else \
                "Apollo не отвечает на порту 47990 — проверьте, что служба ApolloService запущена"
            self._state.update({"mode": "monitor", "server": None})
            self._state.pop("deskflow", None)
        self._vd_watch_start()
        return {"ok": True}

    def _enter_monitor_client(self):
        mf = self._moon_find
        if not mf or not mf.get("ok"):
            return _err((mf or {}).get("code") or "not_found", (mf or {}).get("err") or "Moonlight не найден",
                        (mf or {}).get("hint"))
        moon = self._ensure_moon()
        host = self._layout.machine(self._host_id())
        if moon is None or host is None or not S.valid_host(host.host or ""):
            return _err("bad_config", "Не указан адрес компьютера с Apollo")
        self._remember_prev()
        self._stop_df()
        with self._lock:
            df = self._df
        if df is not None and df.status().get("running"):
            return _err("conflict", "Deskflow ещё работает — Moonlight и Deskflow-клиент не запускаются вместе")
        vs = self._layout.screen(self._virtual_screen_id())
        res = tuple(vs.px) if vs is not None and vs.machine == self._this else None   # px экрана B из раскладки
        r = moon.start(host.host, self._stream_cfg().get("app_name") or "Monitor", res, S.DEFAULT_FPS)
        if not r.get("ok"):
            with self._lock:
                self._mode = "off"   # Deskflow уже остановлен
                self._state["mode"] = "off"
            return r
        with self._lock:
            self._mode, self._server, self._df_err = "monitor", None, None
            self._stream_hint = self._stream_err = None
            self._state.update({"mode": "monitor", "server": None, "moonlight": moon.process_info()})
            self._state.pop("deskflow", None)
        return {"ok": True}

    def leave_monitor(self):
        """Вернуться к режиму, бывшему до monitor («Отдельный ноут»): shared (с тем же сервером) или off."""
        with self._lock:
            if self._mode != "monitor":
                return {"ok": True, "code": None, "err": None, "pending": False}
            prev, srv = self._state.get("prev_mode"), self._state.get("prev_server")
        return self._set_mode("shared" if prev == "shared" else "off", srv if prev == "shared" else None, False)

    # ══ Этап 4: LAN-связка ══
    def _link_key_path(self):
        return self._data_dir / "link.key"

    def _load_link_secret(self):
        try:
            return PL.secret_from_code(self._link_key_path().read_text(encoding="utf-8").strip())
        except OSError:
            return None

    def _save_link_secret(self, raw):
        path = self._link_key_path()
        D._atomic_write_text(path, PL.format_code(raw) + "\n")
        self._submit(D._restrict_key_file, path)   # права только пользователю (icacls на Windows) — в фоне

    def _link_cfg(self):
        lay = self._layout
        return dict((lay.extra.get("link") if lay else None) or L.DEFAULT_EXTRA["link"])

    def _sync_link(self):
        """Поднять/остановить связку по config (link.enabled) и наличию секрета (в рабочем потоке)."""
        self._link_shutdown()
        with self._lock:
            self._link_err, self._peer_online = None, None
        lay, me, secret = self._layout, self._this, self._link_secret
        if lay is None or me is None or secret is None or not self._link_cfg().get("enabled") or not self._started:
            self._emit("snapshot_changed", None)
            return
        peer, mine = lay.machine(self._peer_id()), lay.machine(me)
        err = None
        if peer is None or not (peer.host or "").strip():
            err = "Не указан адрес другой машины"
        else:
            bind, err = D.resolve_bind_host(mine.host, peer.host)
        if err:
            with self._lock:
                self._link_err = err
            self._emit("snapshot_changed", None)
            return
        lnk = PL.PeerLink(secret, me, bind, int(self._link_cfg().get("port", 24850)), {peer.id: peer.host},
                          self._peer_handler, allow_loopback=self._allow_loopback)
        r = lnk.start()
        if r.get("ok"):
            evt = threading.Event()
            with self._lock:
                self._link, self._link_stop = lnk, evt
            threading.Thread(target=self._link_loop, args=(evt, lnk, peer.id), name="monitors-link",
                             daemon=True).start()
        else:
            with self._lock:
                self._link_err = r.get("err")
        self._emit("snapshot_changed", None)

    def _link_shutdown(self):
        with self._lock:
            old, self._link = self._link, None
            evt, self._link_stop = self._link_stop, None
        if evt is not None:
            evt.set()
        if old is not None:
            old.stop()

    def _link_loop(self, evt, lnk, peer_id):
        first = True
        while first or not evt.wait(self.LINK_PING_S):
            first = False
            r = lnk.request(peer_id, "ping", {}, timeout=2.0)
            online = bool(r.get("ok"))
            err = r.get("err") if r.get("code") == "auth_failed" else None
            with self._lock:
                changed = self._peer_online != online or self._link_err != err
                self._peer_online, self._link_err = online, err
            if changed:
                self._emit("snapshot_changed", None)

    def _peer_handler(self, from_id, cmd, args):
        """Входящие команды по связке (поток PeerLink): тот же путь, что у команд UI."""
        with self._lock:
            self._peer_online = True
        if from_id != self._peer_id():
            return _err("auth_failed", "Неизвестный отправитель")
        if cmd == "status":
            with self._lock:
                return {"ok": True, "code": None, "err": None, "mode": self._mode, "server": self._server,
                        "busy": self._busy, "role": self._role()}
        if cmd == "set_mode":
            mode, server = args.get("mode"), args.get("server")
            if mode not in MODES or (server is not None and not isinstance(server, str)):
                return _err("bad_config", "Неверный режим")
            self._emit("peer_mode_request", {"from": from_id, "mode": mode})
            res = self._set_mode(mode, server, True)
            return {k: res.get(k) for k in ("ok", "code", "err", "hint", "pending")}
        return _err("bad_config", "Неизвестная команда")

    def _notify_peer(self, mode, server):
        """Велеть другой машине выполнить свою часть. Нет связки / пир офлайн — режим здесь уже включён, а
        подсказка (§8.2) говорит, что нажать на другой машине."""
        names = {"monitor": "«Ноут как монитор»", "off": "«Выкл»", "shared": "«Общая мышь»"}
        peer = self._peer_id()
        with self._lock:
            lnk = self._link
        hint = f"На {self._mname(peer)} нажмите {names.get(mode, mode)}"
        if lnk is None or peer is None:
            with self._lock:
                self._stream_hint = hint if mode == "monitor" else None
            return {"ok": False, "code": "peer_unreachable"}
        args = {"mode": mode}
        if server:
            args["server"] = server
        r = lnk.request(peer, "set_mode", args, timeout=PEER_TIMEOUT_S)
        with self._lock:
            self._peer_online = r.get("code") not in ("peer_unreachable", "auth_failed")
            self._stream_hint = None if r.get("ok") or mode != "monitor" else hint
            self._link_err = None if r.get("ok") else r.get("err")
        if not r.get("ok"):
            log(f"пир не выполнил {mode}: {r.get('code')}")
            self._emit("warning", {"text": f"{self._mname(peer)}: {r.get('err') or 'не выполнил команду'}"})
        return r

    def _link_enable(self, enabled):
        lay = self._layout
        if lay is None:
            return _err("bad_layout", "Сначала сохраните раскладку")
        if bool(self._link_cfg().get("enabled")) != enabled:
            ex = json.loads(json.dumps(lay.extra))
            ex.setdefault("link", dict(L.DEFAULT_EXTRA["link"]))["enabled"] = enabled
            res = self.save_layout(replace(lay, extra=ex))
            if not res.get("ok"):
                return res
        self._submit(self._sync_link)
        return {"ok": True, "code": None, "err": None}

    def set_link_enabled(self, enabled):
        if not isinstance(enabled, bool):
            return _err("bad_config", "Ожидалось вкл/выкл")
        return self._link_enable(enabled)

    def link_new_secret(self):
        """Создать общий секрет и вернуть код (его вводят на второй машине). Прежний секрет заменяется."""
        try:
            if self._layout is None:
                return _err("bad_layout", "Сначала сохраните раскладку")
            raw, code = PL.new_secret()
            self._save_link_secret(raw)
            self._link_secret = raw
            res = self._link_enable(True)
            if not res.get("ok"):
                return res
            log("создан секрет связки")
            return {"ok": True, "code": code, "err": None}
        except (OSError, ValueError) as e:
            log(f"link_new_secret: {type(e).__name__}")
            return _err("os_error", "Не удалось сохранить секрет связки")

    def link_set_secret(self, code):
        """Ввести код, показанный на первой машине."""
        try:
            raw = PL.secret_from_code(code)
            if raw is None:
                return _err("bad_config", "Код связки неверный: нужно 8 групп по 4 символа (A–Z, 2–7)")
            if self._layout is None:
                return _err("bad_layout", "Сначала сохраните раскладку")
            self._save_link_secret(raw)
            self._link_secret = raw
            res = self._link_enable(True)
            if not res.get("ok"):
                return res
            log("секрет связки принят")
            return {"ok": True, "code": None, "err": None}
        except (OSError, ValueError) as e:
            log(f"link_set_secret: {type(e).__name__}")
            return _err("os_error", "Не удалось сохранить секрет связки")

    def _link_block(self):
        with self._lock:
            lnk, online, err = self._link, self._peer_online, self._link_err
            paired = self._link_secret is not None
        seen = None
        if lnk is not None:
            ts = (lnk.status().get("last_seen") or {}).get(self._peer_id())
            if ts:
                seen = datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")
        return {"enabled": bool(self._link_cfg().get("enabled")), "paired": paired, "running": lnk is not None,
                "peer_online": online if lnk is not None else None, "last_seen": seen, "err": err}
