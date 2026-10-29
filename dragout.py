"""Перетаскивание файлов ИЗ панели в другие программы (Проводник/Finder, Telegram, браузер, VS Code).

Использует расширение tkdnd через пакет tkinterdnd2 (необязательная зависимость). Окно создаётся
обычным tk.Tk(), а tkdnd подгружается в него позже — init_dnd(root). Нет пакета или библиотека не
загрузилась — функции возвращают False, панель работает без перетаскивания.

Drag начинается только при движении мыши с зажатой левой кнопкой (порог в несколько пикселей),
поэтому обычный клик и двойной клик по виджету работают как раньше.
"""
import os
import sys

_dnd = None  # модуль tkinterdnd2 после успешного init_dnd


def log(msg):
    try:
        if sys.stderr:
            print(f"[panel/dragout] {msg}", file=sys.stderr, flush=True)
    except Exception:
        pass


def init_dnd(root) -> bool:
    """Подгрузить tkdnd в уже созданный Tk. False — пакета нет или библиотека не загрузилась."""
    global _dnd
    if _dnd is not None:
        return True
    try:
        import tkinterdnd2
        from tkinterdnd2 import TkinterDnD  # импорт добавляет методы drag_source_register/dnd_bind виджетам
    except Exception as e:
        log(f"tkinterdnd2 недоступен: {type(e).__name__}")
        return False
    try:
        TkinterDnD._require(root)
    except Exception as e:
        log(f"tkdnd не загрузился: {type(e).__name__}")
        return False
    try:  # порог начала drag: чуть больше стандартных 3 px, чтобы дрожание при клике не начинало перенос
        px = max(4, round(root.winfo_fpixels("1i") / 96 * 5))
        root.tk.call("set", "::tkdnd::_dx", px)
        root.tk.call("set", "::tkdnd::_dy", px)
    except Exception:
        pass
    _dnd = tkinterdnd2
    return True


def _clean_paths(paths):
    out = []
    for p in paths or ():
        if not isinstance(p, (str, os.PathLike)):
            continue
        p = os.fspath(p)
        if not p or "\x00" in p:
            continue
        p = os.path.normpath(p)  # CF_HDROP на Windows — только обратные слэши
        if os.path.isabs(p) and os.path.exists(p):
            out.append(p)
    return out


def enable_drag(widget, get_paths) -> bool:
    """Сделать widget источником перетаскивания файлов. get_paths() -> list[str] абсолютных путей
    (вызывается в момент начала drag). Действие — копирование. False — tkdnd не инициализирован/ошибка."""
    if _dnd is None or not callable(get_paths):
        return False
    dnd = _dnd

    def on_drag_init(event):
        try:
            paths = _clean_paths(get_paths())
        except Exception as e:
            log(f"get_paths: {type(e).__name__}")
            paths = []
        if not paths:
            return dnd.REFUSE_DROP
        # кортежи tkinter передаёт в Tcl как списки — пробелы и кириллица в путях не ломают разбор
        return ((dnd.COPY,), (dnd.DND_FILES,), tuple(paths))

    try:
        widget.drag_source_register(1, dnd.DND_FILES)
        widget.dnd_bind("<<DragInitCmd>>", on_drag_init)
        return True
    except Exception as e:
        log(f"enable_drag: {type(e).__name__}")
        return False
