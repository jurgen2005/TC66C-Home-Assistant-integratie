"""Decrypting and unpacking the 192-byte response of the TC66C."""

from __future__ import annotations

import struct

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .const import AES_KEY, RESPONSE_LEN


def decrypt(raw: bytes) -> bytes:
    """AES-256-ECB, fixed key."""
    if len(raw) < RESPONSE_LEN:
        raise ValueError(f"response too short: {len(raw)} bytes")
    dec = Cipher(algorithms.AES(AES_KEY), modes.ECB()).decryptor()
    return dec.update(bytes(raw[:RESPONSE_LEN])) + dec.finalize()


def _u32(data: bytes, offset: int) -> int:
    return struct.unpack_from("<I", data, offset)[0]


def _crc16(data: bytes) -> int:
    """CRC-16/Modbus, checked per 64-byte block like sigrok (rdtech-tc) does."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def _check(d: bytes) -> None:
    """Check the header ('pac1'..'pac3') and checksum of the three blocks."""
    for i in range(3):
        block = d[i * 64 : (i + 1) * 64]
        if block[0:4] != f"pac{i + 1}".encode():
            raise ValueError("unexpected header in response, packet probably garbled")
        if _crc16(block[:60]) != _u32(block, 60):
            raise ValueError(f"checksum mismatch in block pac{i + 1}")


def decode(raw: bytes) -> dict[str, float | int]:
    """Extract the readings from the (encrypted) response."""
    d = decrypt(raw)
    _check(d)
    temp_sign = -1 if _u32(d, 88) == 1 else 1
    return {
        "voltage": _u32(d, 48) / 10000,
        "current": _u32(d, 52) / 100000,
        "power": _u32(d, 56) / 10000,
        "resistance": _u32(d, 68) / 10,
        "charge_0": _u32(d, 72),   # mAh data group 0
        "energy_0": _u32(d, 76),   # mWh data group 0
        "charge_1": _u32(d, 80),   # mAh data group 1
        "energy_1": _u32(d, 84),   # mWh data group 1
        "temperature": _u32(d, 92) * temp_sign,
        "data_plus": _u32(d, 96) / 100,
        "data_minus": _u32(d, 100) / 100,
    }
