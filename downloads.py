"""Папка «Загрузки»: где она и что в ней лежит (верхний уровень, новые сверху).

Windows: реальное расположение через SHGetKnownFolderPath(FOLDERID_Downloads) — папка может быть
перенесена (например, в OneDrive или на другой диск). macOS и прочие: ~/Downloads.
"""
import heapq
import os
import stat
import sys
from pathlib import Path

# Незавершённые загрузки и служебные файлы — в список не попадают
_SKIP_SUFFIXES = (".crdownload", ".part", ".tmp", ".partial", ".download", ".opdownload")
_SKIP_NAMES = {"desktop.ini", "thumbs.db", ".ds_store", ".localized"}

_FILE_ATTRIBUTE_HIDDEN = 0x2
_FILE_ATTRIBUTE_SYSTEM = 0x4
_UF_HIDDEN = getattr(stat, "UF_HIDDEN", 0x8000)  # macOS: флаг «скрытый» (chflags hidden)


def log(msg):
    try:
        if sys.stderr:
            print(f"[panel/downloads] {msg}", file=sys.stderr, flush=True)
    except Exception:
        pass


def _known_folder_downloads():
    """Путь FOLDERID_Downloads через shell32 или None, если не получилось."""
    import ctypes
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                    ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

    # {374DE290-123F-4565-9164-39C4925E467B}
    fid = GUID(0x374DE290, 0x123F, 0x4565, (ctypes.c_ubyte * 8)(0x91, 0x64, 0x39, 0xC4, 0x92, 0x5E, 0x46, 0x7B))
    shell32 = ctypes.WinDLL("shell32")
    ole32 = ctypes.WinDLL("ole32")
    shell32.SHGetKnownFolderPath.argtypes = [ctypes.POINTER(GUID), wintypes.DWORD, wintypes.HANDLE,
                                             ctypes.POINTER(ctypes.c_wchar_p)]
    shell32.SHGetKnownFolderPath.restype = ctypes.c_long
    ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    out = ctypes.c_wchar_p()
    hr = shell32.SHGetKnownFolderPath(ctypes.byref(fid), 0, None, ctypes.byref(out))
    try:
        if hr != 0 or not out.value:
            log(f"SHGetKnownFolderPath: HRESULT 0x{hr & 0xFFFFFFFF:08X}")
            return None
        return out.value
    finally:
        if out:
            ole32.CoTaskMemFree(ctypes.cast(out, ctypes.c_void_p))


def downloads_dir() -> Path:
    """Папка «Загрузки» текущего пользователя."""
    if sys.platform == "win32":
        try:
            p = _known_folder_downloads()
            if p:
                return Path(p)
        except Exception as e:
            log(f"папка загрузок через shell32: {type(e).__name__}")
    return Path.home() / "Downloads"


def _hidden(entry, st):
    name = entry.name
    low = name.lower()
    if name.startswith(".") or low in _SKIP_NAMES or low.startswith("~$"):
        return True
    if low.endswith(_SKIP_SUFFIXES):
        return True
    attrs = getattr(st, "st_file_attributes", 0)  # Windows
    if attrs & (_FILE_ATTRIBUTE_HIDDEN | _FILE_ATTRIBUTE_SYSTEM):
        return True
    if getattr(st, "st_flags", 0) & _UF_HIDDEN:  # macOS
        return True
    return False


def list_downloads(limit=300, folder=None) -> list:
    """Файлы и папки верхнего уровня «Загрузок», новые сверху.

    Элемент: {"path": str, "name": str, "is_dir": bool, "size": int|None (для папок None), "mtime": float}.
    Без рекурсии; скрытые/системные и недокачанные файлы пропускаются. Ошибка чтения папки — пустой список.
    """
    try:
        limit = max(0, int(limit))
    except (TypeError, ValueError):
        limit = 300
    root = Path(folder) if folder else downloads_dir()
    items = []
    try:
        with os.scandir(root) as it:
            for entry in it:
                try:
                    try:
                        st = entry.stat()  # с переходом по ссылке
                    except OSError:
                        st = entry.stat(follow_symlinks=False)  # битая ссылка
                    if _hidden(entry, st):
                        continue
                    is_dir = stat.S_ISDIR(st.st_mode)
                    items.append({"path": os.path.join(root, entry.name), "name": entry.name, "is_dir": is_dir,
                                  "size": None if is_dir else int(st.st_size), "mtime": float(st.st_mtime)})
                except OSError:
                    continue  # файл исчез или нет доступа — просто пропускаем
    except FileNotFoundError:
        log("папка загрузок не найдена")
        return []
    except OSError as e:
        log(f"папка загрузок не читается: {type(e).__name__}")
        return []
    return heapq.nlargest(limit, items, key=lambda d: d["mtime"])


_UNITS = ("КБ", "МБ", "ГБ", "ТБ", "ПБ")


def human_size(n) -> str:
    """Размер по-русски: «532 Б», «1,2 МБ», «12,5 ГБ», «340 МБ». None/мусор — пустая строка."""
    try:
        n = float(n)
    except (TypeError, ValueError):
        return ""
    if n < 0:
        return ""
    if n < 1024:
        return f"{int(n)} Б"
    for unit in _UNITS:
        n /= 1024
        if n < 1024 or unit == _UNITS[-1]:
            if n >= 100:
                s = f"{n:.0f}"
            else:
                s = f"{n:.1f}"
                if s.endswith(".0"):
                    s = s[:-2]
            if s == "1024" and unit != _UNITS[-1]:  # 1023,9… округлилось вверх — следующая единица
                continue
            return s.replace(".", ",") + " " + unit
    return ""
