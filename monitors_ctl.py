"""Оркестратор вкладки «Мониторы» (docs/monitors-spec.md, §5.5, §7.5, §8) — единственное, что вызывает UI.

Этап 1: раскладка (сохранение/импорт/экспорт), экраны ОС, помощник краёв, «Применить к Windows» с откатом через
15 с, предпросмотр (dry-run). Этап 2: режим "shared" (Deskflow через deskflow_ctl): сертификат, файлы настроек,
запуск роли сервер/клиент, сверка отпечатков, восстановление при запуске. "monitor" (Apollo/Moonlight) — этап 3:
set_mode для него возвращает ошибку not_found с понятным текстом.

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
from pathlib import Path

import deskflow_ctl as D
import layout as L

ROLLBACK_S = 15
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
        lay = self._layout
        return (lay.extra.get("stream") or {}).get("virtual_match") if lay else None

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
        for s in lay.screens:
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

    def _on_displays_changed(self):
        time.sleep(0.3)  # Windows шлёт несколько WM_DISPLAYCHANGE подряд
        self._refresh_displays()
        self._emit("displays_changed", None)
        self._emit("snapshot_changed", None)

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
                "server": server, "last_switch": st.get("last_switch")}

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
        na = {"state": "na", "note": "будет на следующем этапе", "err": None}
        return {
            "this_machine": self._this, "mode": mode,
            "server": self._server if mode == "shared" and self._server else (ex.get("deskflow") or {}).get("server"),
            "busy": busy, "busy_text": busy_text,
            "layout_ok": lay is not None and bool(lay.screens), "warnings": uniq,
            "components": {
                "deskflow": self._deskflow_component(), "apollo": dict(na), "moonlight": dict(na),
                "edge": {"state": "ok" if edge_cls else "unsupported", "raw_input": bool(st.get("raw_input")),
                         "ease_cursor": ease},
            },
            "deskflow": self._deskflow_block(),
            "stream": {"running": False, "virtual_display": None, "err": None},
            "edge": {"enabled": bool((ex.get("edge_helper") or {}).get("enabled")),
                     "running": bool(st.get("running")), "paused": bool(st.get("paused")) or self._edge_paused,
                     "user_paused": self._edge_paused,
                     "warps": int(st.get("warps") or 0), "last_warp": st.get("last_warp"),
                     "raw_input": bool(st.get("raw_input")), "client_state": st.get("client_state", "local"),
                     "err": st.get("err")},
            "link": {"enabled": bool((ex.get("link") or {}).get("enabled")), "peer_online": None, "err": None},
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
        if mode not in MODES:
            return _err("bad_config", "Неизвестный режим")
        if mode == "monitor":
            return _err("not_found", "Режим «B как монитор» появится на этапе 3 (Apollo + Moonlight)")
        if mode == "shared":
            lay = self._layout
            if lay is None or len(lay.machines) < 2:
                return _err("bad_layout", "Для общей мыши нужна раскладка минимум из двух машин")
            if self._this is None:
                return _err("bad_config", "Не определено, какой это компьютер — выберите в мастере")
            if server is not None and (not isinstance(server, str) or lay.machine(server) is None):
                return _err("bad_config", "Такой машины нет в раскладке")
            sid = self._pick_server(server)
            if not self._begin_busy("Запускаю Deskflow…"):
                return _err("busy", "Предыдущее действие ещё выполняется")
            self._submit(self._job_shared, sid)
            return {"ok": True, "code": None, "err": None, "pending": True}
        if not self._begin_busy("Останавливаю Deskflow…"):
            return _err("busy", "Предыдущее действие ещё выполняется")
        self._submit(self._job_off)
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

    def _job_shared(self, server_id):
        try:
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

    def _job_off(self):
        try:
            self._stop_df()
            with self._lock:
                self._mode, self._df_err = "off", None
                self._state["mode"] = "off"
            self._save_state()
        finally:
            self._end_busy()
        self._apply_edge_client_state()
        self._emit("mode_changed", {"mode": "off"})
        self._emit("snapshot_changed", None)

    def _restore(self):
        """При запуске панели: найти Deskflow, перенять живой процесс из state.json или запустить режим заново."""
        find = self._do_find()
        self._emit("snapshot_changed", None)
        lay = self._layout
        st = self._state
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

    def _job_off_state(self):
        with self._lock:
            self._mode = "off"
            self._state["mode"] = "off"
            self._state.pop("deskflow", None)
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

    def link_new_secret(self):
        return _err("not_found", "Связка панелей появится на этапе 4")

    def link_set_secret(self, code):
        return _err("not_found", "Связка панелей появится на этапе 4")
