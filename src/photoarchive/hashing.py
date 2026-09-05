from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass, field
from pathlib import Path

QUICKXOR_WIDTH_BYTES = 20
QUICKXOR_SHIFT = 11


@dataclass(slots=True)
class QuickXorHash:
    _state: bytearray = field(default_factory=lambda: bytearray(QUICKXOR_WIDTH_BYTES))
    _length: int = 0

    def update(self, data: bytes) -> None:
        for byte in data:
            bit_offset = (self._length * QUICKXOR_SHIFT) % (QUICKXOR_WIDTH_BYTES * 8)
            byte_offset, shift = divmod(bit_offset, 8)
            self._state[byte_offset] ^= (byte << shift) & 0xFF
            if shift:
                self._state[(byte_offset + 1) % QUICKXOR_WIDTH_BYTES] ^= byte >> (8 - shift)
            self._length += 1

    def digest(self) -> bytes:
        result = bytearray(self._state)
        length_bytes = self._length.to_bytes(8, "little", signed=False)
        start = QUICKXOR_WIDTH_BYTES - len(length_bytes)
        for index, value in enumerate(length_bytes):
            result[start + index] ^= value
        return bytes(result)

    def base64digest(self) -> str:
        return base64.b64encode(self.digest()).decode("ascii")


def hash_file(path: Path, chunk_size: int = 1024 * 1024) -> tuple[int, str, str]:
    sha256 = hashlib.sha256()
    quickxor = QuickXorHash()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            size += len(chunk)
            sha256.update(chunk)
            quickxor.update(chunk)
    return size, sha256.hexdigest(), quickxor.base64digest()
