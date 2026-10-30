# Спецификация: вкладка «Мониторы»

Версия 1.1 · 2026-10-29 · статус: к реализации (MVP — этапы 0–2). Изменения 1.1 (ответы пользователя): §7.4 (помощник на Deskflow-клиенте), §8.1, §8.5, §8.6 (новый), §14, §15.

Документ — единственный источник правды по функции «Мониторы». Код по нему пишут backend-dev и frontend-dev.
Пометка **НП** — «не проверено»: факт требует проверки на этапе 0 или должен быть вынесен в настройку, а не зашит в код.

---

## 1. Задача и границы

Стол пользователя: слева ноут **A** (Windows, главный), посередине HDMI-монитор **M**, подключённый к A (он больше и стоит выше), справа ноут **B** (Windows). Сейчас для общей мыши стоит Input Director, от него хотим отказаться. У коллеги есть Windows, macOS и Linux.

| # | Возможность | Чем делается |
|---|---|---|
| 1 | Схема экранов в реальных размерах (мм), курсор выходит ровно напротив, «пустая зона» → ближайший угол | `layout.py` (чистая геометрия) + редактор на canvas |
| 2 | Общая мышь и клавиатура между машинами, переключение роли сервера | Deskflow (`deskflow_ctl.py`) |
| 3 | B как дополнительный монитор A | Apollo (SudoVDA) на A + Moonlight на B (`stream_ctl.py`) |
| 4 | Физически правильный переход между экранами **одной** машины (A ↔ M) | Свой помощник краёв `EdgeHelper` (`displays_win.py`), без сторонних программ |

Вне рамок: управление звуком, общий буфер вне Deskflow, сценарии с тремя и более машинами (модель их допускает, но UI и тесты рассчитаны на 2 машины), Wayland-помощник.

**Важно про Linux.** Сейчас `panel.pyw` на любой ОС, кроме win32, импортирует `platform_mac`, поэтому на Linux панель официально не работает. Linux-машина участвует как узел Deskflow: настраивается через Deskflow GUI по данным, которые показывает панель (кнопка «Экспорт для Deskflow GUI», §8.4). Помощник краёв для X11 — этап 6, «по возможности».

---

## 2. Соглашения

### 2.1 Именование

| Что | Правило | Пример |
|---|---|---|
| Модули | `snake_case.py`. Управление внешней программой — суффикс `_ctl`. ОС-зависимое — `displays_win.py` / `displays_mac.py`, подключаются через `platform_*` (как `video_mirror.py`) | `deskflow_ctl.py` |
| Функции, переменные | `snake_case`. Классы — `PascalCase` | `edge_segments()`, `EdgeHelper` |
| Ключи config.json и JSON-протоколов | `snake_case`, английские | `size_mm`, `os_rect_px` |
| id машины | `[a-z0-9-]{1,16}` | `a`, `b`, `mac-1` |
| id экрана | `<id машины>.<n>`, n с 1 | `a.1`, `a.2`, `b.1` |
| Имя экрана Deskflow (`machines[].name`) | `[A-Za-z0-9._-]{1,63}`, не начинается с `-` | `laptop-a` |
| Стороны | `left` `right` `top` `bottom`. В конфиге Deskflow превращаются в `left right up down` | |
| Ключ вкладки | `monitors`, метод `tab_monitors`, тик `tick_monitors`, UI-словарь `self.mon_ui` | |
| Лог-префиксы | `[panel/layout]`, `[panel/monitors]`, `[panel/deskflow]`, `[panel/stream]`, `[panel/link]`, `[panel/edge]` | |
| Суффиксы единиц в именах | `_mm` (float, 0,1 мм), `_px` (int, физические пиксели ОС), `_pct` (float 0–100), `_ms`, `_s` | `pos_mm`, `inset_px` |

### 2.2 Единицы, координаты, время

- **Физическая плоскость стола («desk»)**: миллиметры, ось x вправо, ось y **вниз**. Начало координат — левый верхний угол охватывающего прямоугольника всех экранов; `layout.normalize()` сдвигает раскладку при сохранении. Позиция экрана задаётся его левым верхним углом. Округление — до 0,1 мм.
- **Пиксели ОС**:
  - Windows — физические пиксели виртуального рабочего стола. Процесс DPI-aware (`SetProcessDpiAwareness(2)` уже стоит в `platform_win.py`), поэтому `GetCursorPos`, `SetCursorPos`, `EnumDisplayMonitors` и `DM_POSITION` работают в одной системе координат. **Масштаб Windows (125 %) в геометрии переходов не участвует**, он хранится только для подписи.
  - macOS — пункты (points) глобальной системы `CGDisplayBounds` с y вниз.
- **Время**: в файлах состояния и логах — ISO 8601 с часовым поясом (`datetime.now().astimezone().isoformat(timespec="seconds")`, например `2026-10-29T14:05:00+05:00`). Таймеры внутри процесса — `time.monotonic()`. В LAN-протоколе `ts` — целые Unix-секунды (UTC).
- Дюймы → мм: ×25,4. Размер по диагонали считается по соотношению сторон в пикселях: `w = d·px_w/√(px_w²+px_h²)`.

### 2.3 Формат ошибок и результатов

Как в `monitor.py` и `netinfo.py`: **исключения из публичных функций наружу не выходят**. Ошибка возвращается в словаре:

```json
{"ok": false, "code": "not_found", "err": "Deskflow не найден", "hint": "winget install --id Deskflow.Deskflow -e"}
```

- `ok` — bool;
- `code` — машинный код из списка ниже;
- `err` — текст по-русски для показа пользователю, до 200 символов, без путей к профилю пользователя и без секретов;
- `hint` — необязательная подсказка (команда или действие).

При успехе — `{"ok": true, ...полезные поля}`, `err` = `None`.

Коды: `not_found`, `version_unsupported`, `dialect_unknown`, `admin_required`, `permission_required` (разрешения macOS), `bad_config`, `bad_layout`, `timeout`, `spawn_failed`, `exited`, `busy`, `conflict` (например, Moonlight и Deskflow-клиент одновременно), `peer_unreachable`, `auth_failed`, `unsupported_os`, `os_error`.

### 2.4 Потоки

Публичные методы `MonitorsCtl` не блокируют поток Tk дольше 50 мс: всё долгое уходит в рабочий поток контроллера. События идут из фоновых потоков через колбэк `on_event(kind, payload)`. Панель кладёт их в существующую очередь `self.q` кортежем `("monitors", kind, payload)` и обрабатывает в `tick_queue`. `snapshot()` возвращает потокобезопасную копию (как `Monitor.snapshot()`).

---

## 3. Структура файлов и владельцы

```
MyPanel/
  panel.pyw            [frontend] вкладка «Мониторы», load_config (+1 ключ), tick_monitors, ветка в tick_queue, self_test
  layout_editor.py     [frontend] НОВЫЙ: виджет LayoutEditor (tk.Canvas) — только UI, вся математика из layout.py
  layout.py            [backend]  НОВЫЙ: модель раскладки и геометрия, без зависимостей от ОС и Tk
  monitors_ctl.py      [backend]  НОВЫЙ: оркестратор — режимы, состояние, связка всех частей
  deskflow_ctl.py      [backend]  НОВЫЙ: поиск/версия/диалект CLI, генерация конфига, запуск/остановка, статус, TLS
  stream_ctl.py        [backend]  НОВЫЙ: Apollo/Sunshine/Moonlight — обнаружение, pair, start, stop, статус
  peer_link.py         [backend]  НОВЫЙ (этап 4): LAN-координация двух панелей, HMAC
  displays_win.py      [backend]  НОВЫЙ: список экранов + EDID, DM_POSITION, EdgeHelper (Windows)
  displays_mac.py      [backend]  НОВЫЙ (этап 5): то же для macOS
  platform_win.py      [backend]  только блок «# ── Мониторы ──»: DATA_DIR, реэкспорт из displays_win, process_running()
  platform_mac.py      [backend]  только блок «# ── Мониторы ──»: аналогично (этап 5)
  config.example.json  [backend]  пример секции "monitors"
  README.md            [frontend] раздел о вкладке (текст про config и безопасность даёт backend)
  tests/               [backend]  unittest (stdlib), запуск: python -m unittest discover -s tests
    test_layout.py, test_edge_decide.py, test_deskflow_conf.py, test_deskflow_probe.py,
    test_stream_probe.py, test_peer_link.py, fixtures/*.txt (снятые выводы --help/--version)
  docs/monitors-spec.md
  monitor.py, downloads.py, dragout.py, video_mirror.py, netinfo.py — НЕ ТРОГАЕМ
```

Папка данных (не в репозитории): `plat.DATA_DIR / "monitors"`, где `DATA_DIR`:
- Windows — `%LOCALAPPDATA%\MyPanel`;
- macOS — существующий `SUPPORT_DIR`.

Права на каталог: 0o700 на macOS, на Windows — унаследованные от профиля.

| Файл в `DATA_DIR/monitors/` | Что |
|---|---|
| `state.json` | текущий режим, кто сервер, `this_machine`, pid запущенных процессов с временем старта, отпечатки TLS доверенных машин |
| `deskflow/server.conf` | сгенерированный конфиг сервера |
| `deskflow/settings.conf` | файл настроек для `-s` (если этого требует диалект, §6.2) |
| `deskflow/core.log` | вывод deskflow-core, ограничен 1 МБ (при переполнении обрезается старая половина) |
| `link.key` | общий секрет LAN-связки (этап 4) |

`config.json` лежит в папке проекта, и папка может синхронизироваться через OneDrive. Поэтому **секреты и отпечатки в config.json не пишутся**.

Новых обязательных зависимостей нет. На macOS, на этапе 5, добавляется необязательная `pyobjc-framework-Quartz`.

---

## 4. Схема config.json — секция `monitors`

Раскладка общая для обеих машин: секция `monitors` одинакова на A и B. Синхронизация — через LAN-связку (этап 4) или кнопками «Копировать раскладку / Вставить раскладку» (буфер обмена). Поэтому **какая машина «я», в config не пишется**. Это определяется сравнением `machines[].hostname` с `socket.gethostname()` без учёта регистра, а при неудаче — выбором в мастере, который сохраняется в `state.json` → `this_machine`.

```json
{
  "main_agent_name": "Claude",
  "monitors": {
    "version": 1,
    "revision": 7,
    "updated": "2026-10-29T14:05:00+05:00",
    "machines": [
      {"id": "a", "name": "laptop-a", "hostname": "LAPTOP-A", "host": "192.0.2.11", "os": "windows"},
      {"id": "b", "name": "laptop-b", "hostname": "LAPTOP-B", "host": "192.0.2.12", "os": "windows"}
    ],
    "screens": [
      {"id": "a.1", "machine": "a", "label": "Ноут A", "os_id": "win:SDC4161:00000000:UID4352",
       "px": [1920, 1080], "scale": 125, "size_mm": [345.4, 194.3], "size_source": "edid",
       "pos_mm": [0.0, 220.0], "os_rect_px": [0, 0, 1920, 1080]},
      {"id": "a.2", "machine": "a", "label": "Монитор 27\"", "os_id": "win:GSM5B7F:0001E240",
       "px": [2560, 1440], "scale": 100, "size_mm": [597.7, 336.2], "size_source": "user",
       "pos_mm": [345.4, 0.0], "os_rect_px": [1920, -868, 2560, 1440]},
      {"id": "b.1", "machine": "b", "label": "Ноут B", "os_id": "win:BOE0A1C:00000000:UID256",
       "px": [1920, 1080], "scale": 125, "size_mm": [345.4, 194.3], "size_source": "edid",
       "pos_mm": [943.1, 230.0], "os_rect_px": [0, 0, 1920, 1080]}
    ],
    "deskflow": {"port": 24800, "server": "a", "path": null, "tls": true, "clipboard": true,
                 "corner_band_pct": 1.0, "percent_decimals": null},
    "stream": {"host_machine": "a", "client_machine": "b", "host_app": "apollo",
               "app_name": "Desktop", "virtual_screen": "b.1",
               "virtual_match": ["SudoVDA", "Virtual Display"], "moonlight_path": null},
    "edge_helper": {"enabled": true, "poll_ms": 4, "push_mickeys": 4, "inset_px": 2,
                    "max_step_px": 600, "skip_while_button_down": false},
    "link": {"enabled": false, "port": 24850},
    "startup_mode": "last",
    "stop_on_exit": false
  }
}
```

Поля и проверки (проверяет `layout.parse_config`; неверное значение заменяется значением по умолчанию с записью в лог, как у остального config):

| Поле | Тип / диапазон | По умолчанию | Смысл |
|---|---|---|---|
| `version` | int = 1 | 1 | версия схемы |
| `revision` | int ≥ 0 | 0 | растёт при каждом сохранении. При синхронизации побеждает больший |
| `machines[]` | 1–4 записи | — | `host` — IPv4/IPv6/имя (проверка `monitor.valid_host`); `os` ∈ `windows`, `macos`, `linux` |
| `screens[].px` | [int 320–16384, int 200–16384] | из ОС | физическое разрешение |
| `screens[].size_mm` | [float 50–3000, float 30–2000] | EDID или ввод | физический размер **видимой области** |
| `screens[].size_source` | `edid`, `user`, `default` | — | откуда взят размер (UI помечает `default` жёлтым) |
| `screens[].pos_mm` | [float, float], модуль ≤ 10000 | — | левый верхний угол на столе |
| `screens[].os_id` | строка ≤ 128 или null | — | стабильный ключ локального экрана (§9.1) |
| `screens[].os_rect_px` | [x, y, w, h] или null | null | прямоугольник в пикселях ОС своей машины. Заполняет панель **этой** машины. Для удалённой машины с одним экраном можно null: тогда считается `[0, 0, px_w, px_h]` |
| `deskflow.server` | id машины | первая машина | кто сервер в режиме «Общая мышь» |
| `deskflow.path` | абсолютный путь или null | null | ручной путь к deskflow-core (§10.3) |
| `deskflow.tls` | bool | true | `false` разрешён только явно; UI показывает красное предупреждение |
| `deskflow.clipboard` | bool | true | опция `clipboardSharing` Deskflow (риск — §13) |
| `deskflow.corner_band_pct` | 0,2–5 | 1,0 | ширина «угловой» полосы для пустых зон |
| `deskflow.percent_decimals` | null, 0 или 2 | null | null — взять из результата проверки диалекта (§6.2) |
| `stream.host_app` | `apollo`, `sunshine` | по ОС хоста | |
| `stream.virtual_match` | список подстрок | как в примере | **НП**: как распознать виртуальный дисплей SudoVDA среди экранов |
| `edge_helper.poll_ms` | 2–20 | 4 | период опроса курсора |
| `edge_helper.push_mickeys` | 0–50 | 4 | «дожим»: сколько единиц сырого смещения наружу нужно в заблокированной зоне. 0 — сразу |
| `edge_helper.inset_px` | 1–10 | 2 | насколько от края ставить курсор после переноса |
| `edge_helper.max_step_px` | 100–5000 | 600 | скачок курсора больше этого — телепорт (Deskflow, программа), не переход |
| `link.port` | 1024–65535 | 24850 | порт LAN-связки панелей |
| `startup_mode` | `last`, `off` | `last` | что включать при старте панели |
| `stop_on_exit` | bool | false | останавливать ли Deskflow и Moonlight при закрытии панели. По умолчанию нет: процессы переживают панель и перенимаются при следующем запуске (§8.1, §14 п. 2) |

`panel.pyw/load_config` только передаёт сырой `d.get("monitors")` дальше, как сейчас делает с `servers`. Проверку выполняет `layout.parse_config`.

**Запись config.json** делает только `layout.write_config_section(path, "monitors", value)`. Порядок: прочитать файл, заменить один ключ (остальные ключи сохраняются), записать во временный файл в той же папке, затем `os.replace`. Отступ 2, `ensure_ascii=False`. Если файл не читается как JSON — **не перезаписывать**, вернуть `bad_config`.

---

## 5. Модули и контракты

### 5.1 `layout.py` — модель и геометрия (без ОС, без Tk)

```python
@dataclass(frozen=True)
class Machine:   id: str; name: str; hostname: str; host: str | None; os: str
@dataclass(frozen=True)
class Screen:    id: str; machine: str; label: str; px: tuple[int, int]; scale: int | None
                 size_mm: tuple[float, float]; size_source: str; pos_mm: tuple[float, float]
                 os_id: str | None; os_rect_px: tuple[int, int, int, int] | None
@dataclass(frozen=True)
class Layout:    machines: tuple[Machine, ...]; screens: tuple[Screen, ...]; revision: int; extra: dict
                 # extra — секции deskflow/stream/edge_helper/link как dict после проверки

@dataclass(frozen=True)
class Segment:   # участок края src в мм, вдоль края (для left/right — y, для top/bottom — x)
    src: str; side: str; src_from_mm: float; src_to_mm: float
    dst: str; dst_from_mm: float; dst_to_mm: float; kind: str   # "linear" | "corner"
@dataclass(frozen=True)
class EdgeRule:  # то же в пикселях ОС одной машины — для EdgeHelper
    src: str; side: str; line_px: int      # координата границы (для right — src.x+src.w, для left — src.x)
    from_px: int; to_px: int               # диапазон вдоль края, полуинтервал [from, to)
    dst: str; a: float; b: float           # dst_along = a*src_along + b
    clamp_lo: int; clamp_hi: int           # допустимый диапазон вдоль края dst (с учётом inset)
    enter_px: int                          # перпендикулярная координата после переноса (dst.x + inset и т.п.)
    kind: str                              # "linear" | "corner"
@dataclass(frozen=True)
class DeskflowLink:
    src_machine: str; side: str; src_from_pct: float; src_to_pct: float
    dst_machine: str; dst_from_pct: float; dst_to_pct: float
```

| Функция | Возврат | Назначение |
|---|---|---|
| `parse_config(raw: dict \| None) -> tuple[Layout \| None, list[str]]` | раскладка + предупреждения | проверка и разбор секции `monitors` |
| `to_config(layout: Layout) -> dict` | dict | обратно в JSON-вид. `revision` +1, `updated` — сейчас |
| `normalize(layout) -> Layout` | | сдвиг начала координат в (0, 0) охватывающего прямоугольника |
| `size_from_diagonal(diag: float, unit: str, px: tuple[int,int]) -> tuple[float,float]` | (w_mm, h_mm) | unit ∈ `in`, `cm` |
| `diagonal_in(size_mm) -> float` | дюймы | для подписи «15,6″» |
| `snap(layout, screen_id, pos_mm, tol_mm=15.0) -> tuple[float,float]` | новая позиция | привязка краёв к краям соседей и выравнивание по верху, низу и центру |
| `neighbors(layout, screen_id, side) -> list[str]` | id экранов-кандидатов | §7.1 |
| `edge_segments(layout, screen_id, side) -> list[Segment]` | | §7.2. Покрывает весь край, если кандидаты есть, иначе `[]` (стена) |
| `internal_rules(layout, machine_id, inset_px=2) -> tuple[list[EdgeRule], list[str]]` | правила + предупреждения | только переходы внутри машины. Нужны `os_rect_px` её экранов |
| `edge_decide(rules, rects, prev, now, push, cfg) -> tuple[int,int] \| None` | куда перенести курсор или None | решение помощника (§7.4). **Чистая функция** — тестируется без ОС |
| `suggest_os_positions(layout, machine_id) -> dict[str, tuple[int,int]]` | {screen_id: (x, y)} | раскладка ОС, ближайшая к физической (§7.5) |
| `deskflow_links(layout, decimals=2, band_pct=1.0) -> tuple[list[DeskflowLink], list[str]]` | связи + предупреждения | §7.3 |
| `validate(layout) -> list[str]` | предупреждения | перекрытия больше 20 мм, экран не на краю рабочего стола своей машины там, где у него сосед с другой машины, аспект `size_mm` и `px` расходится больше 8 % и т.п. |
| `write_config_section(path, key, value) -> dict` | `{"ok","code","err"}` | атомарная запись (§4) |

Кроме того, `python layout.py --example` печатает разобранный пример из §7.6 (для ручной сверки).

### 5.2 `displays_win.py` (подключается через `platform_win`)

В `platform_win.py` добавляется только блок:

```python
# ── Мониторы ──
DATA_DIR = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "MyPanel"
try:
    from displays_win import list_displays, set_display_positions, EdgeHelper, ease_cursor_setting
except Exception:
    list_displays = set_display_positions = EdgeHelper = ease_cursor_setting = None
def process_running(names: tuple[str, ...]) -> list[str]: ...   # Toolhelp32, без tasklist
```

Панель проверяет наличие функций через существующий `plat_fn(...)`.

| Функция / класс | Контракт |
|---|---|
| `list_displays() -> list[dict]` | `[{"os_id", "gdi_name" ("\\.\DISPLAY2"), "label", "px": [w,h], "rect_px": [x,y,w,h], "primary": bool, "scale": int, "size_mm": [w,h] \| None, "virtual": bool}]`. `size_mm` берётся из EDID (§9.1). `virtual` — `DeviceString` или имя из EDID содержит подстроку из `stream.virtual_match`. Время ≤ 100 мс, без PowerShell и WMI |
| `set_display_positions(pos: dict[str, tuple[int,int]]) -> dict` | ключ — `os_id`. Для каждого неосновного экрана: `ChangeDisplaySettingsExW(gdi, devmode с DM_POSITION, None, CDS_UPDATEREGISTRY \| CDS_NORESET, None)`, затем один `ChangeDisplaySettingsExW(None, None, None, 0, None)`. Основной экран остаётся в (0, 0). Перед применением — проверка «все касаются», иначе `bad_layout`. Возврат `{"ok", "code", "err", "previous": {os_id: (x, y)}}` для отката |
| `ease_cursor_setting() -> bool \| None` | Включено ли «Облегчить перемещение курсора между дисплеями». **НП**: ключ реестра (кандидат `HKCU\Control Panel\Cursors\CursorDeadzoneJumpingSetting`). Только для подсказки, логика от него не зависит (§7.4) |
| `class EdgeHelper(rules: list[EdgeRule], rects: dict[str, tuple], cfg: dict, on_event=None)` | `start()`, `stop()`, `set_rules(rules, rects)`, `pause(flag: bool)`, `status() -> {"running", "paused", "err", "warps", "last_warp": {"src","dst","at"} \| None, "raw_input": bool}` |

Устройство `EdgeHelper` на Windows (выбор и обоснование — §12.1):
- отдельный поток `edge-helper`, в нём **скрытое окно верхнего уровня** (`WS_EX_TOOLWINDOW`, не показывается). Message-only окно не подходит: оно не получает широковещательный `WM_DISPLAYCHANGE`;
- `RegisterRawInputDevices` (usage page 1, usage 2, `RIDEV_INPUTSINK`) — сырые относительные смещения мыши и тачпада: по ним виден «дожим» в край, где курсор уже упёрся;
- цикл `MsgWaitForMultipleObjects(timeout=poll_ms)`, затем разбор сообщений: `WM_INPUT` копит `push=(dx,dy)`, `WM_DISPLAYCHANGE` вызывает `on_event("displays_changed", None)`. Дальше `GetCursorPos`, `layout.edge_decide(...)` и при необходимости `SetCursorPos`;
- после собственного `SetCursorPos` `prev` = цель (свой перенос не считается переходом);
- **никакого `WH_MOUSE_LL`**;
- если `RegisterRawInputDevices` не удался, работает без «дожима»: исправляются только переходы, которые ОС сделала сама, а `status().raw_input = False` (UI пишет «дожим недоступен»).

### 5.3 `deskflow_ctl.py`

| Функция / класс | Контракт |
|---|---|
| `INSTALL: dict[str, dict]` | по ОС: `{"url", "cmd", "admin": bool, "note"}` (§10.2) |
| `find_deskflow(cfg_path: str \| None = None) -> dict` | `{"ok", "code", "err", "path", "version": "1.27.0", "dialect": {...}}`. Порядок поиска — §10.3. Таймаут каждого запуска — 5 с |
| `probe_dialect(path) -> dict` | запускает `--version` и `--help` и разбирает вывод. Возврат `{"form": "subcommand" \| "legacy", "server_argv": [...шаблон], "client_argv": [...], "config_flag", "settings_flag", "name_flag", "address_flag", "tls_flag", "decimals": 0 \| 2 \| None}`. Неизвестный вывод → `dialect_unknown` (UI показывает вывод `--help`, обрезанный до 2 КБ) |
| `build_server_config(layout, links: list[DeskflowLink], options: dict) -> str` | текст конфига (§8.2), детерминированный: одинаковый вход даёт одинаковый текст, что нужно для golden-тестов |
| `build_argv(role, path, dialect, *, name, bind_host, server_host, port, config_file, settings_file, tls) -> list[str]` | только список аргументов, без shell |
| `validate_config(path, dialect, conf_text) -> dict` | **НП-проверка**: запустить сервер на `127.0.0.1:<свободный порт>` на ≤ 3 с, найти в логе ошибки разбора. Возврат `{"ok", "err", "log": [...]}` |
| `class Deskflow(path, dialect, data_dir, on_event=None)` | `start_server(conf_text, name, bind_host, port, tls) -> dict`, `start_client(name, server_host, port, tls) -> dict`, `stop(timeout=3) -> dict`, `status() -> {"running", "role", "pid", "since", "connected": [names], "last_switch": {"from","to","x","y","at"} \| None, "err", "log_tail": [≤ 20 строк]}` |
| `local_fingerprint() -> dict` | `{"ok", "fp": "SHA256:AB:CD:…", "cert_path"}`: SHA-256 от DER (`ssl.PEM_cert_to_DER_cert` + `hashlib`), без openssl. **НП**: путь к сертификату Deskflow 1.27 |
| `trusted_fingerprints(role) -> list[str]`, `add_trusted(role, fp) -> dict` | role: `server` — список доверенных клиентов, `client` — доверенных серверов. **НП**: пути и формат файлов trusted-* |

Разбор лога процесса (поток-читатель stdout/stderr → `core.log` + события). Регулярки — константы в начале модуля, **НП** по формату 1.27:
- `switch from "(.+)" to "(.+)" at (\d+),(\d+)` → `last_switch`, событие `deskflow_switch` (нужно для паузы помощника, §7.4);
- `client "(.+)" has connected` / `disconnected` → `connected`;
- строки с `ERROR` / `FATAL` → поле `err`.

Процессы: `subprocess.Popen(argv, stdin=DEVNULL, stdout=PIPE, stderr=STDOUT, cwd=data_dir, creationflags=CREATE_NO_WINDOW)`. Окружение — копия без `PYTHON*`. Pid и время старта пишутся в `state.json`. При старте панели: если pid из `state.json` жив и это процесс с тем же именем и временем создания — перенять или остановить, в зависимости от `startup_mode`. **Чужой** deskflow (GUI или служба) не трогаем: показываем `conflict` («закройте Deskflow GUI или остановите в нём сервер/клиент»). Остановка: `terminate()`, через 3 с — `kill()`.

### 5.4 `stream_ctl.py`

| Функция / класс | Контракт |
|---|---|
| `INSTALL` | как в deskflow_ctl (§10.2) |
| `find_host_app(kind: "apollo" \| "sunshine") -> dict` | `{"ok", "code", "err", "path", "running": bool, "web_ui": "https://localhost:47990"}`. `running` — TCP-подключение к `127.0.0.1:47990`, **без HTTP-запросов к API** (API Apollo менялся). На Linux-хосте — `unsupported_os` с текстом §11.3 |
| `find_moonlight(cfg_path=None) -> dict` | `{"ok", "path", "version", "dialect": {"stream", "pair", "list", "quit": шаблоны argv}}` — из разбора `--help` (**НП**: точный синтаксис). Если разобрать не удалось — `dialect_unknown` |
| `class Moonlight(path, dialect, on_event=None)` | `pair(host) -> dict` (запускает pair и возвращает PIN из вывода — **НП**; иначе подсказка «введите PIN, показанный Moonlight, в веб-интерфейсе Apollo»), `list_apps(host) -> {"ok", "apps": [str]}`, `start(host, app_name) -> dict`, `stop(host) -> dict` (`quit host` + terminate своего процесса), `status() -> {"running", "pid", "since", "err"}` |
| `virtual_display_present(displays: list[dict]) -> dict \| None` | первый экран с `virtual=True` |

Pairing и настройка Apollo — через **его веб-интерфейс** (кнопка «Открыть веб-интерфейс Apollo»). Панель не пишет в конфиг Apollo и не вызывает его HTTP API.

### 5.5 `monitors_ctl.py` — оркестратор (единственное, что вызывает UI)

```python
class MonitorsCtl:
    def __init__(self, raw_cfg: dict | None, config_path: Path, data_dir: Path, plat, on_event=None): ...
    def start(self) -> None          # восстановить режим по startup_mode, запустить EdgeHelper, LAN-связку
    def stop(self) -> None           # при stop_on_exit — остановить свои процессы
    def snapshot(self) -> dict       # §5.5.1
    def layout(self) -> Layout | None
    def local_displays(self) -> list[dict]
    def save_layout(self, layout: Layout) -> dict            # {"ok","err","warnings"}; пишет config, пересчитывает правила
    def set_this_machine(self, machine_id: str) -> dict
    def set_mode(self, mode: str, server: str | None = None) -> dict   # асинхронно: {"ok": True, "pending": True}
    def set_edge_helper(self, enabled: bool) -> dict
    def preview(self) -> dict        # {"ok","deskflow_conf","argv","links","rules","warnings"} — dry-run, ничего не запускает
    def apply_os_positions(self) -> dict      # {"ok","err","token","positions"} — откат через 15 с без confirm
    def confirm_os_positions(self, token: str) -> dict
    def rollback_os_positions(self, token: str) -> dict
    def refresh_components(self) -> None
    def trust_peer(self, machine_id: str, fp: str) -> dict
    def export_layout(self) -> str            # JSON секции monitors — для «Копировать раскладку»
    def import_layout(self, text: str) -> dict
    def export_for_deskflow_gui(self) -> dict # {"server_conf","server_host","port","screen_name"} для Linux/ручной настройки
    # этап 4:
    def link_new_secret(self) -> dict         # {"ok","code": "ABCD-EFGH-…"}
    def link_set_secret(self, code: str) -> dict
```

События `on_event(kind, payload)`: `snapshot_changed`, `mode_changed`, `displays_changed`, `deskflow_switch`, `deskflow_connected`, `deskflow_disconnected`, `os_positions_pending` (`{"token", "deadline_s"}`), `warning` (`{"text"}`), `peer_mode_request` (`{"from", "mode"}` — справочно, применяется автоматически, §8.5).

#### 5.5.1 Формат `snapshot()`

```json
{
  "this_machine": "a", "mode": "shared", "server": "a", "busy": false, "busy_text": "",
  "layout_ok": true, "warnings": ["Экран «Монитор» без EDID-размера — проверьте диагональ"],
  "components": {
    "deskflow":  {"state": "ok", "version": "1.27.0", "path": "C:\\Program Files\\Deskflow\\deskflow-core.exe",
                  "admin_to_install": true, "url": "https://github.com/deskflow/deskflow/releases",
                  "cmd": "winget install --id Deskflow.Deskflow -e", "err": null},
    "apollo":    {"state": "missing", "...": "..."},
    "moonlight": {"state": "na", "note": "нужен на ноуте B"},
    "edge":      {"state": "ok", "raw_input": true, "ease_cursor": false}
  },
  "deskflow": {"running": true, "role": "server", "connected": ["laptop-b"], "since": "2026-10-29T14:05:00+05:00",
               "fp_local": "SHA256:AB:…", "peer_trusted": true, "err": null},
  "stream":   {"running": false, "virtual_display": null, "err": null},
  "edge":     {"running": true, "paused": false, "warps": 132, "last_warp": {"src": "a.1", "dst": "a.2", "at": "…"}},
  "link":     {"enabled": false, "peer_online": null, "err": null}
}
```

`components.*.state` ∈ `ok`, `missing`, `old` (версия ниже минимальной), `unknown_dialect`, `na` (не нужен на этой машине), `unsupported` (ОС), `conflict`.

### 5.6 `peer_link.py` (этап 4) — §8.5

```python
class PeerLink:
    def __init__(self, secret: bytes, self_id: str, bind_host: str, port: int, peers: dict[str, str], handler): ...
    def start(self) -> dict ; def stop(self) -> None
    def request(self, peer_id: str, cmd: str, args: dict, timeout=3.0) -> dict
def new_secret() -> tuple[bytes, str]          # (20 байт, код "ABCD-EFGH-…" base32 8×4)
def secret_from_code(code: str) -> bytes | None
def sign(msg: dict, secret: bytes) -> dict ; def verify(msg: dict, secret: bytes, now: int, seen: set) -> tuple[bool, str]
```

### 5.7 `layout_editor.py` (frontend)

```python
class LayoutEditor(tk.Frame):
    def __init__(self, parent, *, colors: dict, fonts: dict, scale: float, on_change, on_select): ...
    def set_layout(self, layout: Layout, preview: dict | None = None) -> None  # preview — сегменты для подсветки краёв
    def get_layout(self) -> Layout
    def select(self, screen_id: str | None) -> None
```

Геометрию (привязку, сегменты, размеры по диагонали) редактор берёт **только** из `layout.py`.

---

## 6. Deskflow: диалекты CLI и конфиг

### 6.1 Проверка версии и диалекта (этап 0 и `probe_dialect`)

1. `deskflow-core --version` → версия по `(\d+)\.(\d+)\.(\d+)`. Минимальная поддерживаемая — 1.17 (**НП**: подтвердить, с какой версии есть `deskflow-core`). Ниже — `old`.
2. `deskflow-core --help`:
   - есть подкоманды `server` / `client` → `form="subcommand"`; ищем флаги `-s`/`--settings`, `--config`/`-c`, `--name`/`-n`, `--address`/`-a`, флаг TLS;
   - нет подкоманд → проверяем рядом `deskflow-server(.exe)` и `deskflow-client(.exe)` → `form="legacy"` (флаги в стиле synergy: `-f --no-tray --debug INFO --name N --address H:P -c FILE`, клиент — `... N H:P`).
3. Вывод `--help` и `--version` из реальной установки 1.27 сохраняется в `tests/fixtures/deskflow_help_1.27.txt` и `tests/fixtures/deskflow_version_1.27.txt`. На них пишется `test_deskflow_probe.py`.
4. `decimals`: проверяется через `validate_config` с тестовым конфигом `right(0,50.5)=...`. Есть ошибка разбора — значит 0 (только целые).

Шаблоны argv (итог зависит от проверки, **НП**):

| Роль | subcommand (1.27, man) | legacy |
|---|---|---|
| сервер | `[core, "server", "-s", settings.conf]` (в settings — имя, адрес, путь к server.conf, TLS) или `[core, "server", "--config", server.conf, "--name", N, "--address", "192.0.2.11:24800"]` | `[deskflow-server, "-f", "--no-tray", "--debug", "INFO", "--name", N, "--address", "192.0.2.11:24800", "-c", server.conf]` |
| клиент | `[core, "client", "-s", settings.conf]` или `[core, "client", "--name", N, "192.0.2.11:24800"]` | `[deskflow-client, "-f", "--no-tray", "--debug", "INFO", "--name", N, "192.0.2.11:24800"]` |

Сервер **всегда** слушает конкретный LAN-адрес своей машины (`machines[].host`), а не `0.0.0.0` (§10.1).

### 6.2 Формат конфига сервера

Генерирует `build_server_config`. Отступ — табуляция. Порядок: машины в порядке `machines`, стороны `left`, `right`, `up`, `down`, внутри стороны — по возрастанию `src_from_pct`.

```
section: screens
	laptop-a:
	laptop-b:
end
section: links
	laptop-a:
		right(0,50.57) = laptop-b(0,1)
		right(50.57,73.92) = laptop-b(0,54.66)
	laptop-b:
		left(0,54.66) = laptop-a(50.57,73.92)
		left(54.66,100) = laptop-a(72.92,73.92)
end
section: options
	clipboardSharing = true
	switchDelay = 0
end
```

Опции — только те, что подтверждены на этапе 0 (**НП** для `clipboardSharing` в 1.27). Участок края без связи блокирует курсор (**НП**). Генератор на это опирается: «мёртвые» участки (там, где на краю рабочего стола машины нет экрана) не связываются.

---

## 7. Геометрия

### 7.1 Соседи

Для экрана X и стороны `right` (остальные стороны — симметрично):
- кандидаты — экраны S ≠ X, у которых `S.left ≥ X.right − 30 мм` (допуск на неточное перетаскивание) и `S.left − X.right ≤ 500 мм`;
- пара, которая не перекрывается ни по x, ни по y (по диагонали), относится к той оси, где зазор меньше. Так один и тот же сосед не может оказаться одновременно справа и сверху;
- физический зазор между экранами (рамки, расстояние на столе) **на перенос не влияет**: важна только координата вдоль края.

### 7.2 Сегменты края (`edge_segments`)

Край X `right` — отрезок `[X.top, X.bottom]` в мм. Для каждой точки y:
1. если среди кандидатов есть экраны, у которых `y ∈ [S.top, S.bottom]`, берётся ближайший по зазору. Получается сегмент `kind="linear"`, `dst_y = y` (тождественно в мм);
2. иначе берётся кандидат, ближайший к y по вертикали, и сегмент `kind="corner"`: `dst_y = S.top`, если y < S.top, иначе `S.bottom` — **ближайший угол**;
3. если кандидатов нет — сегментов нет (стена).

Соседние точки с одинаковым dst и kind сливаются в сегменты. Края считаются по границам интервалов всех кандидатов, поэтому сегментов мало. Сегменты покрывают весь край без дыр и перекрытий (это проверяется в тестах).

### 7.3 Связи Deskflow (`deskflow_links`)

Deskflow видит машину как один screen. Его «край» — край **охватывающего прямоугольника рабочего стола машины в пикселях ОС** (bbox), проценты 0–100 считаются по длине стороны bbox (**НП**: что проценты считаются именно по bbox, а не по отдельному монитору, — первый пункт калибровки, §12.3).

Для каждой машины Ms и каждого её экрана X, у которого сосед на стороне `side` — на **другой** машине:
1. Если край X не лежит на краю bbox(Ms) (например, X не самый правый), добавить предупреждение «переход с X на Y невозможен: X не на краю рабочего стола своей машины — переставьте экраны в ОС» и пропустить.
2. Для каждого `Segment` перевести мм в пиксели X: `y_px = X.os_y + (y_mm − X.top_mm) · X.px_h / X.h_mm`. Затем пиксели в проценты: `pct = (y_px − bbox.y) / bbox.h · 100`. То же для dst-экрана Y на машине Mt.
3. `linear` → `side(src_from, src_to) = Mt(dst_from, dst_to)`. Внутри одной связи отображение пропорциональное (**НП**), а мм ↔ пиксели внутри одного монитора линейны, поэтому одной связи на сегмент достаточно.
4. `corner` → цель — узкая полоса у угла внутри Y: `[Ytop_pct, Ytop_pct + band]` или `[Ybot_pct − band, Ybot_pct]`, где `band = corner_band_pct`.
5. Участки края bbox, где нет экрана, не связываются.
6. Округление при `decimals=0`: границы источника — до ближайшего целого (у соседних связей граница общая, поэтому дыр нет); границы цели — **внутрь** покрытого монитором диапазона (`ceil` нижней, `floor` верхней). Так курсор не попадёт в пиксели, где нет монитора. Если после округления диапазон пуст — расширить до 1 % внутрь.

**Проблема мониторов разной высоты на одной машине** решается в пунктах 1, 2 и 5: проценты считаются от bbox, в котором есть участки без мониторов. Эти участки не связываются, а реальные участки сторон отображаются кусочно-линейно.

### 7.4 Помощник краёв (`internal_rules` + `edge_decide`)

`internal_rules` — те же `Segment`, но только между экранами одной машины, переведённые в пиксели ОС:
- для `linear`: `a = (dst_px_h / dst_h_mm) / (src_px_h / src_h_mm)`, `b` — из совпадения точек;
- для `corner`: `a = 0`, `b` = угол dst с учётом `inset_px`;
- `clamp_lo` и `clamp_hi` — пиксели dst вдоль края с отступом `inset_px` от углов;
- `enter_px` — первая колонка (строка) dst + `inset_px`.

`edge_decide(rules, rects, prev, now, push, cfg)`:
1. **Телепорт**: если `|now − prev| > max_step_px` или помощник на паузе — `None`. Пауза включается, пока Deskflow сообщает, что курсор на другой машине: событие `deskflow_switch` с `to ≠ эта машина`, снимается обратным switch. Деталь важна: когда курсор уходит на B, Deskflow-сервер держит курсор A в центре экрана, и этот скачок нельзя принять за переход.
2. **Переход, который сделала ОС**: prev в экране X, now в экране Z ≠ X, отрезок prev→now пересекает `line_px` правила (X, side). Точка пересечения вдоль края → правило с `from ≤ along < to` → цель `(enter_px, clamp(a·along + b))`.
3. **«Дожим» в заблокированный участок**: now на крайнем пикселе X со стороны side (для right — `x == X.right − 1`), сумма `push` наружу за последние 150 мс ≥ `push_mickeys`, и для `along` есть правило → цель по правилу. Накопитель `push` после переноса сбрасывается.
4. Если `skip_while_button_down` и зажата кнопка мыши (флаг передаёт платформенный код) — `None`.
5. Иначе `None`.

Почему это работает при любом «Облегчить перемещение курсора между дисплеями» в Windows. Если Windows сама перебросит курсор через неперекрывающийся участок, сработает п. 2 и поправит точку по физике. Если не перебросит — сработает п. 3.

Помощник работает **во всех режимах** (и в «Выкл»): он отвечает только за внутренние края. Внешние края (к другой машине) обслуживает Deskflow, а в режиме «B как монитор» — правила для виртуального дисплея (§8.3).

#### 7.4.1 Помощник на машине — Deskflow-клиенте (главный B) — версия 1.1

Пункт 1 выше («пауза, пока курсор на другой машине») относится к машине-**серверу**. Если A — клиент (главный — B), курсор A двигает Deskflow-клиент, который выполняет команды сервера B. Помощник получает подсостояние `EdgeHelper.status()["client_state"]`. Это аддитивное поле, контракт §5.2 не ломается.

| `client_state` | Когда | Что делает помощник |
|---|---|---|
| `local` | Deskflow на этой машине не клиент, или клиент, но курсор сейчас не на этой машине | обычная работа по п. 1–5 |
| `client_relative` | сервер B «зафиксировал» курсор на A и шлёт **относительные** смещения (`relativeMouseMoves`, §8.6). Клиент вводит их через `SendInput` без `MOUSEEVENTF_ABSOLUTE`, то есть как обычную мышь | **обычная работа по п. 2–3**. Переход, который сделала ОС, поправляется так же. «Дожим» — по Raw Input, если инжектированный ввод туда попадает (**НП**). Если нет, вместо `push` используется число шагов курсора, упёршегося в край: курсор стоит на крайнем пикселе, а клиент продолжает присылать смещения наружу (признак — из лога клиента, §8.6, **НП**) |
| `client_absolute` | сервер ставит курсор **абсолютными** координатами своей модели (обычный переход B→A через край, без фиксации) | **пауза**. Перенос бесполезен: следующее движение сервера вернёт курсор в точку своей модели, а края сервер считает по ней же. Внутри A переходы временно «пиксель в пиксель». Это промежуточное состояние: из него в `client_relative` переводит фиксация (§8.6) |

Как определяется подсостояние (по убыванию надёжности):
1. **Этап 4b**: сигнал панели B по LAN-связке (`lock_state`, §8.6), который та берёт из лога сервера B. Строки «cursor locked to current screen» / «cursor unlocked from current screen» — **НП**.
2. **Лог клиента A**: «entering screen» / «leaving screen» (**НП**). Без «entering» — `local`. После «entering» начальное значение — `client_relative`, если в конфиге сервера включён `relativeMouseMoves` (передаётся связкой), иначе `client_absolute`.
3. **Эвристика отката** (запасной вариант без связки): после переноса курсор за ≤ 2 тика вернулся ближе чем на 8 px к точке, где он был бы без переноса. Это признак абсолютного режима → `client_absolute` до следующего «entering screen». Чистая функция `layout.snapback(warp_from, warp_to, now, tol_px=8) -> bool` — **аддитивное дополнение к §5.1**, с тестами в `test_edge_decide.py`. Цена: на каждый вход в абсолютном режиме возможен один видимый «дёрг».

Чего помощник **не делает**: не блокирует и не переписывает инжектированный ввод Deskflow (без `WH_MOUSE_LL`). Почему — §8.6, «отвергнутые варианты».

### 7.5 Раскладка ОС по физике (`suggest_os_positions`)

Основной экран остаётся в (0, 0). Остальные цепочкой от основного: сосед справа — `x = X.x + X.w`, слева — `x = X.x − S.w`; сверху и снизу — аналогично по y. Сдвиг вдоль общего края выбирается так, чтобы **середина физического перекрытия** двух экранов совпала в пикселях. Затем проверяется, что пиксельные отрезки перекрываются хотя бы на 1 px (Windows требует касания). Если нет — сдвиг минимально подгоняется. Применение: `apply_os_positions` → `set_display_positions` → в UI плашка «Раскладка Windows изменена. Оставить? [Оставить] (откат через 15 с)», без `confirm` выполняется откат по `previous`.

### 7.6 Разобранный пример (раскладка пользователя)

Исходные данные:
- `a.1` — ноут A 15,6″, 1920×1080, 125 %;
- `a.2` — монитор M 27″, 2560×1440, 100 %, стоит правее и выше;
- `b.1` — ноут B 15,6″, 1920×1080, правее M.

Размеры по диагонали при 16:9 (`k_w = 16/√337 = 0,87158`, `k_h = 9/√337 = 0,49026`):
- 15,6″ = 396,24 мм → **345,4 × 194,3 мм**, плотность по вертикали 1080/194,3 = **5,5584 px/мм**;
- 27″ = 685,8 мм → **597,7 × 336,2 мм**, плотность 1440/336,2 = **4,2832 px/мм**.

Физическая раскладка (мм, y вниз, начало — верх монитора):

| Экран | x | y (верх … низ) |
|---|---|---|
| a.1 | 0 … 345,4 | 220,0 … 414,3 |
| a.2 (M) | 345,4 … 943,1 | 0 … 336,2 |
| b.1 | 943,1 … 1288,5 | 230,0 … 424,3 |

Физическое перекрытие a.1/M по вертикали: 220,0 … 336,2 мм. Под M у ноута A остаётся 58,1 мм пустой зоны. Над ноутом A у M — 220 мм пустой зоны.

**Раскладка Windows на A (`suggest_os_positions`).** Середина перекрытия — 278,1 мм. На a.1 это (278,1 − 220)·5,5584 = 322,9 px от верха, на M — 278,1·4,2832 = 1191,2 px. Сдвиг M = 322,9 − 1191,2 = −868. Итого: a.1 = (0, 0, 1920, 1080), **M = (1920, −868, 2560, 1440)**. Пиксельное перекрытие — y 0…571, то есть экраны касаются.

**Правила помощника A1 → M** (край a.1 `right`, line_px = 1920):

| Участок a.1 (y, px) | kind | Формула (y на M, виртуальные px) |
|---|---|---|
| 0 … 645 | linear | `y_M = 0,7706·y + 74,3` |
| 646 … 1079 | corner | (1922, 569) — левый нижний угол M (inset 2) |

| y на a.1 | Что делает Windows | Куда ставит помощник |
|---|---|---|
| 0 | M, y = 0 | y = 74 |
| 300 | M, y = 300 | y = 305 |
| 540 (середина) | M, y = 540 | y = 490 |
| 571 | M, y = 571 | y = 514 |
| 620 | блок (M там нет) | «дожим» → y = 552 |
| 800 | блок | «дожим» → угол (1922, 569) |

**Правила M → A1** (край M `left`, line_px = 1920, курсор ставится на x = 1917):

| Участок M (y, px) | kind | Формула (y на a.1) |
|---|---|---|
| −868 … 74 | corner | (1917, 2) — правый верхний угол a.1 |
| 75 … 571 | linear | `y_A1 = 1,2977·y − 96,4` (y = 200 → 163; y = 571 → 645) |

**Deskflow, A сервер — B клиент.**
- bbox A: x 0…4480, y −868…1080, высота **1948 px**. Правый край bbox = правый край M, он покрывает y −868…571, то есть 0 … 73,92 % (1440/1948). Ниже 73,92 % на правом краю экрана нет — участок не связывается.
- bbox B: 1920×1080, 1 px = 0,0926 %.

Сегменты правого края M:
- 0 … 230 мм → у B нет экрана → **corner**, верхний левый угол B. В процентах A: 0 … 985,1/1948 = 0 … **50,57 %**.
- 230 … 336,2 мм → **linear**. В процентах A: 50,57 … 73,92 %. На B: (336,2 − 230)·5,5584 = 590,3 px, то есть 0 … **54,66 %**.

Сегменты левого края B (230 … 424,3 мм):
- 0 … 54,66 % → linear → A 50,57 … 73,92 %;
- 54,66 … 100 % (ниже M) → corner, нижний правый угол M: A 72,92 … 73,92 %.

```
laptop-a:  right(0,50.57)      = laptop-b(0,1)
           right(50.57,73.92)  = laptop-b(0,54.66)
laptop-b:  left(0,54.66)       = laptop-a(50.57,73.92)
           left(54.66,100)     = laptop-a(72.92,73.92)
```

Если Deskflow принимает только целые проценты:

```
right(0,51)=laptop-b(0,1)   right(51,74)=laptop-b(0,55)
left(0,55)=laptop-a(51,73)  left(55,100)=laptop-a(72,73)
```

Проверка точки: курсор на M, y = 1200 px от верха M (280,16 мм) → 1200/1948 = 61,60 % → (61,60 − 50,57)/23,35·54,66 = 25,82 % B → 278,9 px → 230 + 278,9/5,5584 = **280,17 мм**. Совпало с точностью 0,01 мм.

**Режим «B как монитор»:** виртуальный дисплей `b.1` на A (1920×1080, физический размер B). Середина перекрытия M/B — 283,1 мм → M: −868 + 283,1·4,2832 = 344,6; VD: (283,1 − 230)·5,5584 = 295,2 → **VD = (4480, 49)**. Правила M ↔ VD считаются так же, как A1 ↔ M.

Все числа этого раздела — эталон для `tests/test_layout.py`. Допуск: ±1 px, ±0,05 %, ±0,1 мм.

---

## 8. Режимы

### 8.1 Состояния и взаимоисключение

Режим пары машин — один из трёх:

| Режим | A | B |
|---|---|---|
| `off` | Deskflow остановлен, стрима нет | то же |
| `shared` (server = X) | X — `deskflow server`, другой — `deskflow client` → X | |
| `monitor` | Apollo ждёт (служба), Deskflow остановлен, VD ставится по раскладке | Deskflow **остановлен**, Moonlight `stream` → A |

Инварианты, которые проверяет `MonitorsCtl` перед любым запуском:
- (а) на машине не запущены одновременно Moonlight и Deskflow-клиент, иначе `conflict`;
- (б) в `monitor` Deskflow остановлен на обеих машинах (так проще и предсказуемее, чем оставлять сервер без клиента);
- (в) на машине работает не больше одного Deskflow-процесса, запущенного панелью.

Переходы (выполняет рабочий поток; `busy=true`, `busy_text` для UI):
- `off → shared(X)`: X запускает server; другая машина — client.
- `shared(A) → shared(B)` («главный — этот компьютер» на B): обе останавливают Deskflow → B запускает server (с конфигом, сгенерированным **на B** из той же раскладки, включая опции §8.6) → A запускает client. Клиент сам повторяет подключение, поэтому строгий порядок не обязателен.
- `* → monitor`: обе останавливают Deskflow → B запускает Moonlight. A по `displays_changed` видит VD → `set_display_positions` по §7.6 → пересчитывает правила помощника.
- `monitor → предыдущий` («Отдельный ноут»): B делает `moonlight quit` и завершает свой процесс; Apollo удаляет VD → A пересчитывает правила; затем восстанавливается режим, бывший до `monitor` (`shared` или `off`, хранится в `state.json`).
- `* → off`: всё остановить.

**Жизненный цикл при закрытии панели (решение пользователя, версия 1.1).** Закрытие панели **не останавливает** Deskflow и Moonlight. Эффективное значение `stop_on_exit` по умолчанию — `false`: этот ответ заменяет значение `true` в таблице §4, backend-dev исправляет значение по умолчанию в `parse_config`, `config.example.json` и строку таблицы.

Следствия:
- **Уточнение к §5.3.** Внешние процессы запускаются так, чтобы пережить панель: `stdout`/`stderr` — **открытый файл** `core.log`, а не `PIPE`. Пайп умер бы вместе с панелью, и процесс получил бы ошибку записи. Панель читает лог «хвостом» (tail) из файла. Ротация — только при запуске нового процесса: если `core.log` больше 1 МБ, он переименовывается в `core.log.1`. Флаги Windows — `CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP`, процесс не входит в job-объект панели. У Moonlight — аналогично, `moonlight.log`.
- **При запуске панели** режим берётся из `state.json` (DATA_DIR). Живые процессы из `state.json` (pid + имя образа + время создания совпадают) **перенимаются**: панель продолжает читать их лог с конца файла. Если процесса нет, а `startup_mode="last"`, он запускается заново.
- **`MonitorsCtl.stop()`** при `stop_on_exit=false` только останавливает помощник краёв и LAN-связку и сохраняет `state.json`.
- **Помощник краёв живёт только в панели.** Пока панель закрыта, переходы между экранами одной машины делает ОС «пиксель в пиксель». UI в статусе пишет «переходы по размерам работают, пока открыта панель».

### 8.2 Без LAN-связки (MVP, этапы 1–3)

Каждая панель выполняет **свою часть** перехода и показывает, что сделать на другой машине. Например, на B нажали «главный — этот компьютер» → B стал сервером, а плашка пишет: «На ноуте A нажмите „Общая мышь → клиент“ (его тачпадом)».

Раскладка переносится кнопками «Копировать раскладку» → «Вставить раскладку». Буфер удобно передать через уже работающий Deskflow.

### 8.3 «B как монитор» на A

Пока VD есть, он считается внутренним экраном A со `screen_id = stream.virtual_screen` (`b.1`) и физическим размером B. Помощник обрабатывает переход M ↔ VD. Мышь и клавиатура B через Moonlight управляют курсором A. В абсолютном режиме мыши Moonlight курсор с тачпада B остаётся в пределах VD (**НП**, проверка вживую).

### 8.4 Linux и ручная настройка

`export_for_deskflow_gui()` выдаёт для Linux-машины адрес сервера, порт, имя экрана и, если сервер — Linux, текст `server.conf` для режима «использовать существующий конфиг» в Deskflow GUI. Отпечатки сверяются глазами в Deskflow GUI.

### 8.5 LAN-связка панелей (этап 4): нужна ли и как защищена

**Зачем.** «Одна кнопка с любой машины» по определению требует, чтобы вторая машина узнала о нажатии. Deskflow такого канала не даёт. Без связки — два нажатия на двух машинах (§8.2). Это рабочий MVP, но не то, что просили. Плюсом связка даёт синхронизацию раскладки и **аутентичный** обмен TLS-отпечатками Deskflow.

**Минимально безопасный протокол:**
- **Транспорт**: TCP, один запрос — одно соединение, одна строка JSON ≤ 16 КБ, таймаут 3 с.
- **Слушаем** только на `machines[self].host`, и только если адрес частный (`10/8`, `172.16/12`, `192.168/16`, `169.254/16`, `fc00::/7`, `fe80::/10`). Иначе связка не запускается: `bad_config`, «связка работает только в локальной сети». Соединения не с IP пиров из config закрываются сразу.
- **Секрет**: 20 случайных байт (`secrets.token_bytes`), показывается как код base32 «ABCD-EFGH-IJKL-MNOP-QRST-UVWX-YZ23-4567». Его один раз вводят на второй машине. Хранится в `DATA_DIR/monitors/link.key`; в config, лог и UI после ввода не выводится.
- **Сообщение**: `{"v":1,"from":"b","to":"a","ts":1793280000,"nonce":"<16 байт hex>","cmd":"set_mode","args":{...},"mac":"<hex>"}`. `mac = HMAC-SHA256(secret, canonical_json(сообщение без mac))`, где `canonical_json` — `sort_keys=True, separators=(",",":"), ensure_ascii=False`, UTF-8. Проверка: `hmac.compare_digest`, `|ts − now| ≤ 30 с`, nonce не встречался за 120 с, `to == self`. Ответ подписывается так же и содержит `re` = nonce запроса.
- **Ограничение перебора**: после 5 неверных `mac` за минуту с одного IP — 60 с игнора и запись в лог.
- **Команды** — закрытый список; аргументы проверяются как config:
  - `status`;
  - `set_mode {mode, server, prev_mode}`;
  - `push_layout {monitors}` — принимается, только если `revision` больше локального и `parse_config` без ошибок;
  - `fingerprint {fp}` — сохранить отпечаток пира как доверенный в Deskflow;
  - (этап 4b, §8.6) `lock_state {locked: bool, relative: bool}` — от сервера к клиенту;
  - (этап 4b, §8.6) `release {side, along_mm}` — от клиента к серверу.
  Ни одна команда не запускает переданный путь или строку.
- **Без шифрования**: передаются режим, раскладка и отпечатки. Это не секреты, нужна только подлинность. Так обходимся одной stdlib, без сертификатов.
- **Брандмауэр Windows** при первом `bind` спросит про `pythonw.exe`. UI заранее подсказывает: «разрешите только для частных сетей». Панель правила брандмауэра не создаёт.

Альтернативы, которые рассмотрены и отвергнуты: mDNS-обнаружение — лишняя поверхность атаки, адреса и так в config; TLS с самоподписанными сертификатами — stdlib не умеет генерировать сертификаты, а pinning вручную сложнее ввода кода; ssh между ноутами — на Windows нужен sshd.

### 8.6 Главный — B: физические переходы A1 ↔ HDMI (версия 1.1)

**Требование.** Когда сервер Deskflow — B, а A — клиент, переход ноут A ↔ HDMI тоже должен идти по физическим размерам.

**Суть проблемы.** В обычном режиме сервер Deskflow ведёт **свою модель** курсора клиента в пикселях bbox клиента: прибавляет к модели смещения мыши B и шлёт клиенту абсолютные координаты. Внутри A сервер поэтому двигает курсор «пиксель в пиксель», включая мёртвые зоны bbox. Клиент о своём курсоре серверу не сообщает (**НП**, проверка вживую). Любой наш перенос на A сервер «отменит» следующим движением, а края A→B он будет считать по своей, уже неверной модели.

**Выбранный путь: сервер B фиксирует курсор на A и шлёт относительные смещения (`relativeMouseMoves` + `lockCursorToScreen`).** Когда курсор зафиксирован на клиенте, Deskflow (наследие synergy) с опцией `relativeMouseMoves = true` шлёт клиенту относительные смещения (**НП** для 1.27). Клиент A вводит их как обычное движение мыши. Дальше ОС и наш помощник работают так же, как с родной мышью A: подсостояние `client_relative`, §7.4.1. Модель сервера в это время не используется, расхождению неоткуда взяться.

Плата за это: пока курсор зафиксирован, край A→B не переключает экраны. Выход сделан по горячей клавише (этап 2b) или автоматически через связку (этап 4b).

Что панель B добавляет в `server.conf` в роли сервера (опции — только после проверки на этапе 0; синтаксис нескольких действий через запятую — **НП**):

```
section: options
	clipboardSharing = true
	relativeMouseMoves = true
	keystroke(Control+Alt+Shift+F9) = switchToScreen(laptop-a), lockCursorToScreen(on)
	keystroke(Control+Alt+Shift+F10) = lockCursorToScreen(off), switchToScreen(laptop-b)
	keystroke(Control+Alt+Shift+F11) = lockCursorToScreen(on)
	keystroke(Control+Alt+Shift+F12) = lockCursorToScreen(off)
end
```

Сочетания вынесены в аддитивный ключ `monitors.deskflow.work_on_hotkeys` (дополнение к §4, необязательный; по умолчанию — как выше). Ключ `{"work_on": "Control+Alt+Shift+F9", "back": "Control+Alt+Shift+F10", "lock": "Control+Alt+Shift+F11", "unlock": "Control+Alt+Shift+F12"}` проверяется регуляркой `^(Control|Alt|Shift|Super)(\+(Control|Alt|Shift|Super))*\+[A-Za-z0-9]{1,3}$`. F11/F12 нужны только автоматике этапа 4b. Ctrl+Alt+Shift+Q не занимаем: это выход из Moonlight.

**Этап 2b — вручную, без связки:**
1. Пользователь за B нажимает Ctrl+Alt+Shift+A: курсор переходит на A (в точку, которую выбирает `switchToScreen`, обычно центр — **НП**) и фиксируется.
2. Мышь и клавиатура B работают на A. A1 ↔ HDMI — по физике (помощник в `client_relative`).
3. Ctrl+Alt+Shift+B: фиксация снимается, курсор на B.
4. Обычный переход B→A **через край** без фиксации остаётся доступен. В нём A работает «пиксель в пиксель» (`client_absolute`, помощник на паузе), а UI на A показывает подсказку «для переходов по размерам — Ctrl+Alt+Shift+A на B».

**Этап 4b — автоматически, через LAN-связку (бесшовно):**
1. Панель B видит в логе сервера `switch from "laptop-b" to "laptop-a"`. Курсор уже вошёл в A в физически верную точку (её дают связи §7.3).
2. Панель B сразу вводит на B сочетание `lock` (`SendInput`, только сочетание из config, только когда B — сервер). Затем шлёт A `lock_state {locked: true, relative: true}`, и A переходит в `client_relative`.
3. Пользователь работает на A, A1 ↔ HDMI — по физике.
4. Помощник A ловит «дожим» на внешнем крае A, за которым B (правый край M в участке, связанном с B). Он шлёт B `release {side: "right", along_mm: 280.2}`.
5. Панель B вводит `unlock`, затем `back` (`switchToScreen(laptop-b)`) и сразу ставит курсор B (`SetCursorPos` на B, где он — локальный курсор сервера) в точку, рассчитанную по `edge_segments` из `along_mm`. Так курсор выходит «ровно напротив».
6. Если связки нет, она не отвечает 1 с или ввод сочетания не прошёл, панель остаётся в режиме этапа 2b и показывает подсказку.

**Отвергнутые варианты:**

| Вариант | Почему нет |
|---|---|
| Помощник на A перехватывает инжектированные Deskflow-клиентом движения (`WH_MOUSE_LL`), блокирует их и вводит заново по физике | модель сервера B расходится с реальным курсором по y. Сервер переключает экраны и считает проценты связей A→B по модели, поэтому выход на B будет в неверной точке или не случится вовсе. Плюс системная задержка от хука на Python (§12.1) |
| Своя геометрия в `server.conf` на B, где мониторы A описаны отдельными зонами | в Deskflow screen = машина, один прямоугольник; зон внутри screen нет, `aliases` дают только другие имена той же машины |
| Два Deskflow одновременно (сервер на обеих машинах, каждая — клиент другой) | хук сервера A видит ввод клиента A → риск петли; внутри A всё равно абсолютные координаты чужой модели |
| `relativeMouseMoves` без фиксации | в synergy-ветке относительные смещения шлются только при зафиксированном курсоре (**НП** для 1.27). Если на этапе 0 окажется, что 1.27 шлёт их всегда, этап 2b упрощается: фиксация не нужна. Но тогда остаётся проверить, как сервер решает, когда переключать экран на A→B (проверка 4 ниже) |

**Проверки вживую** (этап 0 — пункты 1–4 в коротком виде, этап 2b — полностью):
1. Deskflow 1.27 принимает `relativeMouseMoves`, `keystroke(...)` с несколькими действиями через запятую и `lockCursorToScreen(on|off)`. `validate_config` без ошибок.
2. С фиксацией на A лог клиента A показывает относительные смещения (уровень лога и формат строки — **НП**). Курсор A пересекает A1↔HDMI «как родной», помощник поправляет точку.
3. Попадает ли инжектированный `SendInput` (relative) в Raw Input на A. Если нет — проверить «дожим» по признаку из лога клиента (§7.4.1).
4. Без фиксации: шлёт ли сервер абсолютные координаты (подтверждает `client_absolute`) и срабатывает ли эвристика отката без ложных пауз при обычных переходах.
5. Ускорение указателя Windows («Повышенная точность установки указателя») на A при относительных смещениях: скорость курсора на A не должна заметно отличаться. При необходимости дать подсказку отключить.
6. Куда ставит курсор `switchToScreen(laptop-a)`.
7. Строки лога сервера B «cursor locked/unlocked» и `switch from … at x,y` — для этапа 4b.
8. Этап 4b: ловит ли хук Deskflow-сервера на B сочетание, введённое `SendInput` панели B. Работает ли это, когда на B активно окно администратора (UIPI, §13). Задержка `release` → курсор на B — не больше 30 мс в LAN.
9. Переход A→B после `release` — точка входа на B совпадает с расчётом §7.6 (±2 мм).

---

## 9. Экраны ОС и физический размер

### 9.1 Источник EDID и стабильный `os_id`

| ОС | Список и пиксели | Физический размер | `os_id` |
|---|---|---|---|
| Windows | `EnumDisplayDevicesW` (адаптеры → мониторы, `EDD_GET_DEVICE_INTERFACE_NAME`), `EnumDisplaySettingsExW(ENUM_CURRENT_SETTINGS)` → `dmPosition`, `dmPelsWidth`; масштаб — `GetDpiForMonitor(MDT_EFFECTIVE_DPI)` | `winreg`: `HKLM\SYSTEM\CurrentControlSet\Enum\DISPLAY\<PnP-ID>\<instance>\Device Parameters\EDID` (чтение без админа; путь берётся из `DeviceID` вида `\\?\DISPLAY#GSM5B7F#<instance>#{…}`) | `win:<PnP-ID>:<серийник EDID hex>` или, если серийник 0, `…:<instance>` |
| macOS | `CGGetActiveDisplayList`, `CGDisplayBounds` (пункты), `CGDisplayPixelsWide` | `CGDisplayScreenSize(id)` → мм | `mac:<vendor>:<model>:<serial>` (`CGDisplayVendorNumber` и т.д.) |
| Linux (этап 6) | `xrandr --query` | `/sys/class/drm/card*-*/edid` или строка `… 344mm x 194mm` в `xrandr` | `linux:<connector>:<EDID-хэш>` |

Разбор EDID (`layout.py` не трогает ОС, поэтому функция `parse_edid(data: bytes) -> {"mm": (w,h) | None, "name", "serial", "pnp"}` лежит **в `layout.py`** как чистая):
- приоритет — первый Detailed Timing Descriptor: байты 0x36+12 и +13, старшие тетрады в +14 — размер в мм;
- запасной вариант — байты 0x15 и 0x16 (см ×10);
- значение отбрасывается, если выходит за 50…3000 мм или аспект расходится с пикселями больше 8 %. Так бывает у телевизоров, проекторов и виртуальных дисплеев.

Тогда `size_source="default"` (берётся размер по типовой диагонали 15,6″ или 24″), и UI просит ввести диагональ.

GDI-имя `\\.\DISPLAYn` и CGDirectDisplayID **не используются как ключ** (меняются после перезагрузки и переподключения), только в рантайме.

---

## 10. Безопасность

### 10.1 Сеть
- Наружу ничего не открывается. Deskflow-сервер — `--address <LAN-IP>:24800`, связка — `<LAN-IP>:24850`. Панель не настраивает переброс портов и UPnP, не создаёт правил брандмауэра.
- **Deskflow TLS обязателен.** При первом подключении отпечаток сверяется: UI показывает «отпечаток этого компьютера» и «отпечаток подключившегося». Доверенным он становится только после нажатия «Совпадает» на обеих машинах или после обмена через аутентифицированную связку (§8.5). При смене отпечатка — красная плашка, Deskflow останавливается, автоматического принятия нет.
- **Apollo/Sunshine**: веб-интерфейс по умолчанию слушает LAN. UI советует оставить в настройках Apollo доступ «только локальная сеть/этот ПК» (**НП**: имя параметра в Apollo). Pairing — только PIN-кодом Moonlight.
- Если активная сеть Windows помечена как «Общедоступная» (**НП**: способ определения без PowerShell), режимы не запускаются, пока пользователь не подтвердит.

### 10.2 Установка (панель только подсказывает, сама ничего не ставит)

| Компонент | Windows | macOS | Linux | Админ |
|---|---|---|---|---|
| Deskflow | GitHub Releases (MSI); `winget install --id Deskflow.Deskflow -e` (**НП**) | `brew install --cask deskflow` (**НП**) | `flatpak install flathub org.deskflow.deskflow` (**НП**) | Win: да (MSI); mac/Linux: нет |
| Apollo (+ SudoVDA) | GitHub Releases `ClassicOldSong/Apollo` | — | — | да |
| Sunshine | — (на Windows вместо него Apollo) | GitHub Releases `LizardByte/Sunshine` / brew (**НП**) | flatpak `dev.lizardbyte.app.Sunshine` (**НП**) | mac: нет; Linux: для uinput — права на устройство |
| BetterDisplay | — | `brew install --cask betterdisplay` | — | нет |
| Moonlight-qt | GitHub Releases `moonlight-stream/moonlight-qt`; winget (**НП** id) | `brew install --cask moonlight` | flatpak `com.moonlight_stream.Moonlight` | Win: да (установщик) |
| Virtual Display Driver | `winget install --id VirtualDrivers.Virtual-Display-Driver -e` — **только как запасной вариант без Apollo**, в MVP не используется | — | — | да |

UI для отсутствующего компонента: «не найден», кнопки «Открыть страницу загрузки» (`plat.open_path(url)` или браузер) и «Скопировать команду». Если `admin=True` — бейдж «нужен администратор: запустите команду в терминале от имени администратора».

### 10.3 Запуск внешних программ
- Только `subprocess` со списком аргументов, `shell=False`, `stdin=DEVNULL`; на Windows — `CREATE_NO_WINDOW`.
- Порядок поиска бинарника, без текущей папки и без папки проекта:
  1. `deskflow.path` / `stream.moonlight_path` из config: абсолютный путь, существующий файл, имя из белого списка (`deskflow-core(.exe)`, `deskflow-server(.exe)`, `deskflow-client(.exe)`, `Moonlight(.exe)`, `moonlight`, `moonlight-qt`). Если путь вне `%ProgramFiles%`, `/Applications`, `/opt/homebrew`, `/usr/local`, `/usr/bin` — жёлтое предупреждение «путь вне системных папок»;
  2. фиксированные пути: Windows — `%ProgramFiles%\Deskflow\`, `%ProgramFiles%\Moonlight Game Streaming\`, `%ProgramFiles%\Apollo\`; macOS — `/Applications/Deskflow.app/Contents/MacOS/`, `/Applications/Moonlight.app/Contents/MacOS/`, `/opt/homebrew/bin`, `/usr/local/bin`; Linux — `/usr/bin`, `/usr/local/bin`, flatpak как `["flatpak", "run", "--command=deskflow-core", "org.deskflow.deskflow"]` (**НП**);
  3. `shutil.which` — только на macOS и Linux, результат через `os.path.realpath` и проверку имени. На Windows PATH не используется, потому что `which` смотрит и текущую папку.
- Значения, которые подставляются в argv (имена экранов, хосты, имя приложения Moonlight), проходят регулярки из §2.1 и `monitor.valid_host`. Начинающиеся с `-` отклоняются. Имя приложения Moonlight: `[\w .:-]{1,64}`, и оно должно быть в списке из `list_apps`.
- Панель **никогда не повышает права**: никакого `runas`, `ShellExecute "runas"`, `sudo`, `osascript … with administrator privileges`.

### 10.4 Что требует прав

| Действие | Права | Что показывает панель |
|---|---|---|
| Установка Deskflow (MSI), Apollo, SudoVDA, Moonlight (Win), VDD | администратор | бейдж + команда/ссылка |
| Запуск deskflow-core, Moonlight, помощника краёв | обычный пользователь | — |
| `DM_POSITION` (раскладка Windows) | обычный пользователь | — |
| Чтение EDID из реестра | обычный пользователь | — |
| Управление окнами, запущенными от администратора, экран UAC, экран блокировки через Deskflow | **служба Deskflow** (ставится его установщиком) — панель её не использует | предупреждение «в окнах администратора и на экране UAC общая мышь не работает» (§13) |
| macOS: Deskflow — Accessibility; помощник — Input Monitoring (event tap); Sunshine — Screen Recording и Accessibility | разрешения в System Settings | строки в таблице разрешений README и статус `permission_required` |

---

## 11. Платформы

### 11.1 Windows — полная поддержка (этапы 1–4).

### 11.2 macOS (этап 5)
- `displays_mac.py`: список экранов (`CGGetActiveDisplayList`, `CGDisplayBounds`, `CGDisplayScreenSize`), раскладка — `CGBeginDisplayConfiguration` / `CGConfigureDisplayOrigin` / `CGCompleteDisplayConfiguration(kCGConfigurePermanently)`, без админа.
- `EdgeHelper`: перенос — `CGWarpMouseCursorPosition`, сразу после него `CGAssociateMouseAndMouseCursorPosition(True)`, иначе курсор «замирает» примерно на 0,25 с. Позиция — `CGEventGetLocation(CGEventCreate(None))`. «Дожим» — listen-only `CGEventTap` (`kCGEventMouseMoved`, поля `kCGMouseEventDeltaX/Y`) в отдельном потоке с `CFRunLoop`. Нужно разрешение Input Monitoring; без него работает без дожима, как на Windows без Raw Input. Реализация — через необязательный `pyobjc-framework-Quartz`: ctypes для event tap ненадёжен.
- Стрим с Mac-хоста: Sunshine + BetterDisplay. Панель только обнаруживает их и показывает инструкцию: создать в BetterDisplay виртуальный экран с разрешением B и выбрать его в Sunshine. Автоматизации BetterDisplay в v1 нет (**НП**: его CLI/URL-схема).

### 11.3 Linux
- Панель на Linux сейчас не поддерживается (нет `platform_linux.py`) — см. §1 и §8.4.
- Linux как хост «монитора»: в UI честно пишем «не поддерживается: на Linux нет штатного способа создать виртуальный дисплей для Sunshine — используйте режим „Общая мышь“».
- Этап 6 (по возможности, X11): `XQueryPointer` + `XWarpPointer` через ctypes `libX11`, «дожим» — XInput2 `XI_RawMotion`. Wayland: помощник краёв **не поддерживается** — глобально перемещать курсор там нельзя. Deskflow под Wayland работает частично (libei/portal).

---

## 12. Решения, альтернативы, проверка

### 12.1 Ключевые решения и альтернативы

| Решение | Почему | Альтернатива и её риск |
|---|---|---|
| Помощник: опрос `GetCursorPos` 4 мс + Raw Input + `SetCursorPos` | ничего не вставляется в системную цепочку ввода: если поток Python тормозит (GIL, Tk рисует офис), страдает только наша коррекция, а не мышь всей системы | `WH_MOUSE_LL`: можно отменить ход ОС без видимого скачка, но задержка колбэка Python тормозит мышь во всей системе, а при таймауте Windows молча снимает хук. Оставлено как идея для этапа «дальше» на C-расширении — не делаем |
| Видимый скачок до одного кадра опроса | при 4 мс почти незаметен | на Python 3.10 под Windows `time.sleep` ≈ 15,6 мс, поэтому используем `MsgWaitForMultipleObjects` с таймаутом. При необходимости `timeBeginPeriod(1)` только пока помощник включён (**НП**: влияние на батарею) |
| Логика решений — чистая `edge_decide` в `layout.py` | один алгоритм на всех ОС, 100 % тестируемо без железа | писать в каждом `displays_*` — три копии и расхождения |
| Deskflow-конфиг генерирует панель | только так получаются частичные связи с пустыми зонами | Deskflow GUI: связи только «экран к экрану» целиком |
| Apollo без API, только обнаружение и ссылка на веб-интерфейс | API менялся, CLI не задокументирован | вызов API — ломается при обновлении Apollo |
| LAN-связка HMAC вместо TLS | stdlib, ноль сертификатов, данные не секретны | без связки — два нажатия (это MVP) |
| Раскладка в config.json, состояние и секреты в DATA_DIR | config редактируют руками и синхронизируют; секрету место не там | отдельный `monitors.json` — второй файл настроек для пользователя |
| `stop_on_exit=false` (решение пользователя, §14 п. 2) | общая мышь и стрим не пропадают при закрытии или падении панели; процессы перенимаются при следующем запуске (§8.1) | `true` — нет «осиротевших» процессов, но мышь пропадает вместе с панелью. Риск `false` — процессы без присмотра панели; закрывается перениманием по pid + имени + времени создания и логом в файл |

### 12.2 План проверки на одной Windows-машине

Автоматически (`python -m unittest discover -s tests`, без сети и без внешних программ):
1. `test_layout.py`: все числа §7.6, `size_from_diagonal` (15,6″ и 27″, дюймы и см), `parse_edid` на трёх сохранённых EDID (ноут, монитор, «битый» с нулями), `snap`, `normalize`, `suggest_os_positions` (= (1920, −868) и (4480, 49) для VD), `validate` (экран не на краю bbox, перекрытия), покрытие края сегментами без дыр. Дополнительные раскладки: монитор над ноутом (top/bottom), три экрана на одной машине, мониторы одного размера «середина в середину».
2. `test_edge_decide.py`: таблицы §7.6 как кейсы: переход ОС → поправка, «дожим» → угол, телепорт > `max_step_px` → None, пауза, `push` ниже порога → None.
3. `test_deskflow_conf.py`: golden-текст §6.2 (decimals 2 и 0), округление внутрь, детерминированность.
4. `test_deskflow_probe.py`, `test_stream_probe.py`: разбор `--help`/`--version` из `fixtures` (1.27 и легаси-вывод), неизвестный вывод → `dialect_unknown`.
5. `test_peer_link.py`: подпись и проверка, чужой секрет, `ts` вне окна, повтор nonce, размер > 16 КБ, неизвестная команда. Два `PeerLink` на `127.0.0.1` с разными портами — loopback разрешён только флагом `allow_loopback=True` в конструкторе для тестов.
6. Внешние процессы в тестах не запускаются: в `Deskflow`, `Moonlight`, `find_*` внедряется `runner` (по умолчанию `subprocess`), в тестах — фейк, который отдаёт строки из `fixtures`.

Вручную на A (ноут + HDMI, второго ноута нет):
7. `python layout.py --example`; `python deskflow_ctl.py --dry-run` — печать конфига и argv для текущей раскладки, ничего не запускает.
8. `python panel.pyw --test monitors out.png --demo` — вкладка с фейковым `MonitorsCtl` (раскладка §7.6, Deskflow «подключён», Apollo «не найден»).
9. **Живой** помощник A1 ↔ M: включить, пройти точки таблицы §7.6 (видно глазами по краю окна и линейке). Проверить мышью и тачпадом (**НП**: дают ли precision-тачпады Raw Input), при «Облегчить перемещение курсора» вкл и выкл, при перетаскивании окна.
10. «Применить к Windows» с откатом через 15 с.
11. Deskflow-сервер на A без клиента: процесс стартует, конфиг принят (`validate_config`), лог разбирается.

Только вживую со вторым ноутом:
- переходы A ↔ B и пустые зоны;
- **калибровка Deskflow (§12.3)**;
- TLS-доверие;
- смена ролей;
- Moonlight/Apollo/SudoVDA (появление VD, его имя для `virtual_match`, позиция);
- конфликт Moonlight + Deskflow;
- окна администратора и UAC;
- брандмауэр;
- macOS и Linux.

### 12.3 Калибровка Deskflow (этап 2, вживую)
Кнопка «Проверить переходы» (видна при `link`-статусе или в режиме `shared`):
1. Генерируется тестовый конфиг: `right(0,50)=B(50,100)`, `right(50,100)=B(0,50)`.
2. Пользователь проводит курсор через край в двух местах.
3. Из лога берётся `switch from … at x,y` и сравнивается с прогнозом.

Итог записывается в `state.json` → `deskflow_model`:
- `"bbox"` — проценты от bbox, как в §7.3;
- `"monitor"` — от монитора на краю, тогда генератор считает проценты от него;
- `"unknown"` — показать предупреждение.

Тем же способом проверяется, принимаются ли дробные проценты и блокирует ли участок без связи.

---

## 13. Риски

| Риск | Влияние | Что делаем |
|---|---|---|
| Семантика процентов Deskflow при мониторах разной высоты (НП) | неверные точки входа | калибровка §12.3, модель — параметр генератора |
| Главный — B, A — Deskflow-**клиент**. Без фиксации сервер B ставит курсор A абсолютными координатами своей модели bbox, перенос помощника с ней расходится. С фиксацией нужны `relativeMouseMoves` и `lockCursorToScreen`, которые в 1.27 ещё не проверены (НП) | без фиксации A1 ↔ HDMI идут «пиксель в пиксель»; если 1.27 не поддерживает эти опции, путь §8.6 не работает | путь §8.6: фиксация + `relativeMouseMoves`. Этап 2b — по горячей клавише, этап 4b — автоматически через связку. В `client_absolute` помощник на паузе (§7.4.1), UI подсказывает горячую клавишу. Опции проверяются первыми на этапе 0; если не подтвердятся — возврат к архитектору за другим путём |
| Deskflow как обычный процесс не управляет окнами администратора, UAC и экраном блокировки (UIPI) | «мышь не работает» в отдельных окнах | предупреждение в UI; служба Deskflow — этап «дальше» (только подсказка, без повышения прав) |
| Общий буфер Deskflow включён по умолчанию (решение пользователя, §14 п. 1). Метки «не сохранять» менеджеров паролей с B до A не доходят | пароль, скопированный на B, может попасть в историю буфера панели на A (только в память) | входящий текст проходит те же фильтры, что и локальный (по заголовку активного окна на A); ограничение описано в README; `deskflow.clipboard=false` выключает общий буфер |
| Apollo меняет поведение (API, имя VD) | сломается обнаружение | без API; `virtual_match` в config; обнаружение только по файлу и порту |
| На вкладках уже 5–6 пунктов (возможна «Сеть» от netinfo) | не влезает в 600 px | frontend: при > 6 вкладках `pad=5` и шрифт `f_small` для подписей, вкладка называется «Мониторы» (или «Экраны», если не влезет) |
| Python 3.10: таймеры Windows грубее | заметный скачок курсора | `MsgWaitForMultipleObjects`; рекомендовать 3.11+ в README |
| Mac-часть панели не тестировалась на реальном Mac | неизвестно | этап 5 строго после проверки на Mac коллеги |

---

## 14. Ответы пользователя (были открытыми вопросами) — версия 1.1

1. **Общий буфер через Deskflow — включён** (`deskflow.clipboard = true`, как в §4). Входящий через Deskflow текст проходит через **те же фильтры паролей**, что и локальный: существующий `tick_clipboard` в `panel.pyw` ничего не знает об источнике, поэтому отдельного кода не нужно. Ограничение, которое честно указываем в README: Deskflow переносит только содержимое. Метки «не сохранять» менеджеров паролей с B (`ExcludeClipboardContentFromMonitorProcessing` и др.) до A не доходят. На A срабатывает только фильтр по заголовку активного окна **на A**, а пароль, скопированный на B, в историю A попасть может (только в память). Строка риска в §13 остаётся в силе.
2. **Deskflow и стрим при закрытии панели не останавливаются.** При следующем запуске панель берёт режим из DATA_DIR и перенимает живые процессы. Помощник краёв живёт только в панели. Детали и уточнения к §4 (значение по умолчанию `stop_on_exit = false`) и §5.3 (лог в файл вместо PIPE) — в §8.1.
3. **LAN-связка — этап 4**, как в §8.5.
4. **Физические переходы A1 ↔ HDMI нужны и при главном B.** Выбран путь `relativeMouseMoves` + фиксация курсора на A: этап 2b — по горячей клавише, этап 4b — автоматически через связку. Описание — §8.6, поведение помощника — §7.4.1. Строка риска в §13 про «помощник на клиенте — пауза» относится только к подсостоянию `client_absolute`.
5. **Linux коллеги — пока неизвестен.** Оставляем как в §11.3: Deskflow через GUI, помощник X11 — этап 6, Wayland — не поддерживается.

---

## 15. Этапы и разделение работы

| Этап | Содержание | backend-dev | frontend-dev | Готово, когда |
|---|---|---|---|---|
| 0. Проверки НП (≈0,5–1 день, на A + по возможности B) | `deskflow-core --help/--version` → fixtures; пути TLS и trusted-файлов; `moonlight --help`; имя VD SudoVDA в `EnumDisplayDevices`; ключ «Облегчить перемещение»; **1.1:** `relativeMouseMoves`, `lockCursorToScreen`, `keystroke` с несколькими действиями, строки лога клиента и сервера о фиксации и входе на экран (§8.6, проверки 1–4, 6–7) | собрать выводы, обновить §6 и §9 этого файла (через архитектора) | — | fixtures в `tests/fixtures`, НП в §6 и §8.6 закрыты или вынесены в настройки |
| 1. MVP-геометрия (Windows, одна машина) | `layout.py` + тесты; `displays_win.py` (список, EDID, DM_POSITION, EdgeHelper); `monitors_ctl.py` (раскладка, помощник, apply/rollback, preview) | всё перечисленное + блок в `platform_win.py` + `config.example.json` | вкладка, `LayoutEditor`, поля размеров (дюймы/см/мм), подсветка сегментов, плашка отката, мастер шагов 1–2 и 4, self_test `monitors` | A1 ↔ M ходит по физике, тесты §12.2 п. 1–2 зелёные |
| 2. Deskflow | `deskflow_ctl.py`, режимы `off`/`shared`, смена ролей без связки, TLS-отпечатки, калибровка. **1.1:** процессы переживают панель и перенимаются при запуске (§8.1); `client_state` в EdgeHelper (`local` / `client_absolute`) + эвристика отката `layout.snapback` | модуль + тесты 3–4 + интеграция в ctl; `stop_on_exit=false` по умолчанию | кнопки режимов, «главный — этот компьютер», блок компонентов, сверка отпечатков, мастер шаги 3 и 5; подпись «переходы по размерам работают, пока открыта панель» | A ↔ B вживую, пустые зоны → углы; панель закрыли и открыли — мышь не пропала, режим подхвачен |
| **2b. Главный B — работа на A (1.1)** | опции §8.6 в `server.conf` на B; `work_on_hotkeys`; подсостояние `client_relative` у помощника A (п. 2–3 §7.4 для инжектированного ввода, запасной признак «дожима» из лога клиента) | генерация опций, разбор лога клиента и сервера, `client_state` в EdgeHelper и `snapshot()["edge"]`, тесты `snapback` | подсказка на A «для переходов по размерам — Ctrl+Alt+Shift+A на B», отображение `client_state` в статусе, сочетания в мастере (шаг 5) | проверки §8.6 п. 1–6 пройдены: при главном B и фиксации A1 ↔ HDMI идут по таблице §7.6 |
| 3. «B как монитор» | `stream_ctl.py`, режим `monitor`, позиционирование VD | модуль + тест 4 + ctl | кнопка «Монитор / Отдельный ноут», статусы Apollo/Moonlight, «Открыть веб-интерфейс Apollo» | VD появляется справа от M на своём месте |
| 4. LAN-связка | `peer_link.py`, синхронизация раскладки, обмен отпечатками | модуль + тест 5 | экран сопряжения (код), индикатор «B на связи» | одна кнопка с любой машины |
| **4b. Автофиксация при главном B (1.1)** | команды `lock_state` и `release`, ввод сочетаний `lock/unlock/back` на сервере, точная точка выхода на B (§8.6, этап 4b) | логика в `monitors_ctl.py` + `peer_link.py`, `SendInput` сочетаний в блоке «Мониторы» `platform_win.py` (`send_hotkey(combo)`), тесты команд | индикатор «курсор зафиксирован на A», настройка «автофиксация: вкл/выкл» | проверки §8.6 п. 7–9: B→A→B через края без горячих клавиш, выход «напротив» ±2 мм |
| 5. macOS | `displays_mac.py`, EdgeHelper на Quartz, Sunshine + BetterDisplay (подсказки) | модуль + блок в `platform_mac.py` + `requirements-mac.txt` | правки вёрстки под Aqua | проверено на Mac коллеги |
| 6. Linux X11 (по возможности) | помощник X11; без панели Linux не обойтись — отдельное решение о `platform_linux.py` | — | — | когда станет известно, какой Linux у коллеги (§14, п. 5) |

**Правила, чтобы не мешать друг другу:**
- backend-dev не правит `panel.pyw`, `layout_editor.py`, README. frontend-dev не правит модули из списка backend и `platform_*.py`. `netinfo.py` не трогает никто.
- Контракты §5 заморожены. Изменения — только через правку этого файла (архитектор), с пометкой версии в шапке. Дополнения версии 1.1 к контрактам **только аддитивные**: `status()["client_state"]` у EdgeHelper, `layout.snapback`, ключ `deskflow.work_on_hotkeys`, команды связки `lock_state` / `release`, `platform_win.send_hotkey` (этап 4b). Значение по умолчанию `stop_on_exit` меняется на `false`.
- frontend-dev начинает параллельно с backend: в `self_test` есть `FakeMonitorsCtl` с `snapshot()` из §5.5.1, а `layout.py` нужен ему с первого дня. Поэтому backend отдаёт `layout.py` (датаклассы, `parse_config`, `size_from_diagonal`, `snap`, `edge_segments`) **первым** коммитом этапа 1.
- Подключение в `panel.pyw`: `layout = _optional("layout")`, `monitors_ctl = _optional("monitors_ctl")`. Вкладка `("monitors", "Мониторы")` добавляется, только если оба модуля импортировались. Контроллер создаётся в `__init__` после мониторинга серверов, `start()` вызывается в `__init__`, `stop()` — в `quit()`. При `stop_on_exit=false` `stop()` не трогает Deskflow и Moonlight (§8.1). `tick_monitors` — каждые 1000 мс, только при открытой вкладке. События — ветка `("monitors", kind, payload)` в `tick_queue`.
- Пока идёт перетаскивание в редакторе или открыта плашка отката, панель не прячется (выставлять `self.pinned` и возвращать прежнее значение).

### 15.1 UI вкладки «Мониторы» (для frontend-dev)

Сверху вниз, ширина 600 логических px:
1. **Полоса режимов**: сегменты `Выкл · Общая мышь · B как монитор` (текущий — `accent`); справа переключатель «главный: этот компьютер / <имя>». Строка статуса мелким шрифтом: «Deskflow: сервер, laptop-b подключён · переходы по размерам: вкл (132)». При `busy` — текст `busy_text`, кнопки неактивны.
2. **Редактор** (`LayoutEditor`, ~560×250):
   - масштаб «вписать всё», сетка 10 см (подпись «клетка = 10 см»);
   - прямоугольники экранов в цветах машин: A — `accent`, B — `violet`, третья — `mint`; виртуальный — пунктир;
   - подпись «Ноут A · 15,6″ · 1920×1080 · 125 %»;
   - перетаскивание с привязкой (`layout.snap`), Shift — без привязки;
   - края с переходами подсвечиваются: зелёный — `linear`, жёлтый — `corner`, красный — стена или ошибка `validate`.
3. **Свойства выбранного экрана**: диагональ [__] [дюймы|см] или ширина × высота [мм]; кнопка «из EDID», если `size_source` ≠ `edid` и EDID есть; источник размера (жёлтое «по умолчанию — проверьте»).
4. **Кнопки**: «Сохранить», «Применить к Windows» (только для экранов этой машины), «Предпросмотр Deskflow» (моноширинный текст из `preview()` + «Скопировать»), «Копировать / Вставить раскладку».
5. **Компоненты** (сворачиваемый список): строка на компонент — состояние ✓/✗, версия, «Скачать», «Скопировать команду», бейдж «нужен администратор».
6. **Безопасность**: отпечаток этого компьютера, статус отпечатка второй машины, «Сверить» → два отпечатка рядом и кнопки «Совпадает / Не совпадает»; (этап 4) «Связать панели» → код.

**Мастер первого запуска** (если нет секции `monitors`), внутри вкладки, шаги с «Далее/Назад»:
1. «Это компьютер …» (hostname, LAN-IP из `local_displays`/`socket`), имя для Deskflow.
2. Найденные экраны с размерами из EDID; для пустых — ввод диагонали.
3. Второй компьютер: имя/IP, ОС, его экраны (диагональ, разрешение) или «Вставить раскладку со второго компьютера».
4. Раскладка: перетащить как на столе, подсказка «курсор переходит по прямой — поставьте экраны так же, как стоят на столе».
5. Компоненты для выбранных режимов.
6. Готово → «Применить к Windows» и включение помощника.
