"""Tramas del enlace Bluetooth SPP (espejo de StreamFrameCodec del firmware).

    0xA7 | type | lenLo | lenHi | hdrXor | payload | crcLo | crcHi

hdrXor = 0xA7 ^ type ^ lenLo ^ lenHi.  CRC-16/CCITT-FALSE (poly 0x1021, init 0xFFFF, sin reflejo)
calculado sobre el payload, little-endian.
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)

SOF = 0xA7
TYPE_TEXT = 1  # JSON
TYPE_AUDIO = 2  # PCM
TYPE_PING = 3
TYPE_PONG = 4
MAX_PAYLOAD = 2048
MAX_PONG_ECHO = 64


def _make_table() -> list[int]:
    table = []
    for i in range(256):
        crc = i << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
        table.append(crc)
    return table


_TABLE = _make_table()


def crc16(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc = ((crc << 8) & 0xFFFF) ^ _TABLE[((crc >> 8) ^ b) & 0xFF]
    return crc


def encode(frame_type: int, payload: bytes = b"") -> bytes:
    n = len(payload)
    if n > MAX_PAYLOAD:
        raise ValueError(f"payload de {n} bytes excede {MAX_PAYLOAD}")
    lo, hi = n & 0xFF, n >> 8
    crc = crc16(payload)
    return bytes([SOF, frame_type, lo, hi, SOF ^ frame_type ^ lo ^ hi]) + payload + bytes([crc & 0xFF, crc >> 8])


class FrameDecoder:
    """Decodificador de flujo con resincronización (descarta basura y tramas corruptas)."""

    def __init__(self) -> None:
        self._buf = bytearray()
        self.bad_crc = 0
        self.resyncs = 0

    def feed(self, data: bytes) -> list[tuple[int, bytes]]:
        buf = self._buf
        buf += data
        out: list[tuple[int, bytes]] = []
        i = 0
        while True:
            j = buf.find(SOF, i)
            if j < 0:
                i = len(buf)
                break
            i = j
            if len(buf) - i < 5:
                break
            t, lo, hi, hx = buf[i + 1], buf[i + 2], buf[i + 3], buf[i + 4]
            if (SOF ^ t ^ lo ^ hi) != hx:
                self.resyncs += 1
                i += 1
                continue
            n = lo | (hi << 8)
            if n > MAX_PAYLOAD:
                self.resyncs += 1
                i += 1
                continue
            end = i + 5 + n + 2
            if len(buf) < end:
                break
            payload = bytes(buf[i + 5 : i + 5 + n])
            crc = buf[i + 5 + n] | (buf[i + 6 + n] << 8)
            if crc != crc16(payload):
                self.bad_crc += 1
                i += 1
                continue
            out.append((t, payload))
            i = end
        del buf[:i]
        return out
