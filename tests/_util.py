"""Общие помощники тестов: путь к модулям проекта и синтетические EDID (без реальных серийных номеров)."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def make_edid(pnp="GSM", product=0x5B7F, serial=0, mm=(597, 336), cm=(60, 34), name="TEST MONITOR",
              pixel_clock=True):
    b = bytearray(128)
    b[0:8] = b"\x00\xff\xff\xff\xff\xff\xff\x00"
    v = 0
    for ch in pnp:
        v = (v << 5) | (ord(ch) - 64)
    b[8], b[9] = (v >> 8) & 0xFF, v & 0xFF
    b[10], b[11] = product & 0xFF, product >> 8
    b[12:16] = int(serial).to_bytes(4, "little")
    b[0x15], b[0x16] = cm
    d = 0x36
    if pixel_clock:
        b[d], b[d + 1] = 0x01, 0x1D
    w, h = mm
    b[d + 12], b[d + 13] = w & 0xFF, h & 0xFF
    b[d + 14] = ((w >> 8) << 4) | (h >> 8)
    n = 0x48
    b[n:n + 5] = bytes([0, 0, 0, 0xFC, 0])
    txt = name.encode("ascii")[:13]
    b[n + 5:n + 5 + len(txt)] = txt
    if len(txt) < 13:
        b[n + 5 + len(txt)] = 0x0A
    b[127] = (-sum(b[:127])) & 0xFF
    return bytes(b)
