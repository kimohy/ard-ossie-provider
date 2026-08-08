from __future__ import annotations

import secrets
import time
import uuid

ID_PREFIXES = frozenset({"prd", "tbl", "col", "met", "rel", "lnk", "cst"})
_MAX_TIMESTAMP_MS = (1 << 48) - 1
_MAX_RANDOM_BITS = (1 << 74) - 1


def new_id(
    prefix: str,
    *,
    timestamp_ms: int | None = None,
    random_bits: int | None = None,
) -> str:
    """Return a prefixed RFC 9562 UUIDv7 identifier.

    Explicit timestamp and random values make the allocator testable while the
    production path uses the current Unix time and cryptographic randomness.
    """

    if prefix not in ID_PREFIXES:
        raise ValueError(f"unsupported ID prefix: {prefix}")

    timestamp = time.time_ns() // 1_000_000 if timestamp_ms is None else timestamp_ms
    if not 0 <= timestamp <= _MAX_TIMESTAMP_MS:
        raise ValueError("timestamp_ms must fit in 48 bits")

    randomness = secrets.randbits(74) if random_bits is None else random_bits
    if not 0 <= randomness <= _MAX_RANDOM_BITS:
        raise ValueError("random_bits must be a 74-bit unsigned integer")

    rand_a = randomness >> 62
    rand_b = randomness & ((1 << 62) - 1)
    value = (timestamp << 80) | (0x7 << 76) | (rand_a << 64) | (0b10 << 62) | rand_b
    return f"{prefix}_{uuid.UUID(int=value)}"
