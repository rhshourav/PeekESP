"""Pairing-code derivation.

Must stay byte-identical to pair_apply() in PeekESP.ino, windows/peek_pair.py
and derivePairing() in cloudflare/src/index.js:

    stream = SHA-256("peek-stream:" + CODE)  first 16 hex
    read   = SHA-256("peek-read:"   + CODE)  first 48 hex

Pinned vector (from the firmware header):
    K7M2P4QX9R -> stream 4b907ba136d0a7f2
                  read   ec3cb3699bd1284efb2fcfe056609e87edf4813b84e9ce84
"""
from __future__ import annotations

import hashlib

# I, O, 0 and 1 are removed: the pairs people mistype off a 1.14" screen.
ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_LEN = 10


def normalize_code(raw: str) -> str:
    """'k7m2-p4qx-9r' -> 'K7M2P4QX9R' (the screen shows it grouped)."""
    return "".join(ch for ch in raw.upper() if ch not in " -_\t\n")


def is_valid_code(code: str) -> bool:
    return len(code) == CODE_LEN and all(ch in ALPHABET for ch in code)


def _prefixed_hex(prefix: str, code: str, n_hex: int) -> str:
    return hashlib.sha256((prefix + code).encode()).hexdigest()[:n_hex]


def derive(code: str) -> tuple[str, str]:
    """Return (stream_id, read_token) for a normalised code."""
    return _prefixed_hex("peek-stream:", code, 16), _prefixed_hex("peek-read:", code, 48)
