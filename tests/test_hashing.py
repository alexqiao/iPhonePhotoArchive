from __future__ import annotations

import base64

from hypothesis import given
from hypothesis import strategies as st

from photoarchive.hashing import QuickXorHash


def reference_quickxor(data: bytes) -> str:
    bits = [0] * 160
    for index, byte in enumerate(data):
        offset = (index * 11) % 160
        for bit_index in range(8):
            bits[(offset + bit_index) % 160] ^= (byte >> bit_index) & 1
    result = bytearray(20)
    for bit_index, value in enumerate(bits):
        result[bit_index // 8] |= value << (bit_index % 8)
    for index, value in enumerate(len(data).to_bytes(8, "little")):
        result[12 + index] ^= value
    return base64.b64encode(result).decode("ascii")


@given(st.binary(max_size=512), st.lists(st.integers(min_value=0, max_value=64), max_size=20))
def test_quickxor_matches_bit_reference(data: bytes, chunk_sizes: list[int]) -> None:
    hasher = QuickXorHash()
    offset = 0
    for size in chunk_sizes:
        hasher.update(data[offset : offset + size])
        offset += size
    hasher.update(data[offset:])
    assert hasher.base64digest() == reference_quickxor(data)


def test_quickxor_empty_vector() -> None:
    assert QuickXorHash().base64digest() == "AAAAAAAAAAAAAAAAAAAAAAAAAAA="
