from __future__ import annotations

import re
from typing import Any

_SUSPICIOUS_MARKERS = (
    "\u00c3",
    "\u00c2",
    "\u00e2\u20ac",
    "\u00e2\u201e",
    "\u00e2\u0153",
    "\u00e2\u0161",
    "\u00f0\u0178",
    "\u00ef\u00b8",
    "\ufffd",
)
_TOKEN_PATTERN = re.compile(r"[ \t\r\n]+|[^ \t\r\n]+")


def repair_mojibake(value: str) -> str:
    """Repair common UTF-8 text decoded as Windows-1252."""
    if not value or not _looks_corrupted(value):
        return value
    repaired = "".join(_repair_token(token) for token in _TOKEN_PATTERN.findall(value))
    return repaired.replace("\ufeff", "")


def repair_structure(value: Any) -> Any:
    if isinstance(value, str):
        return repair_mojibake(value)
    if isinstance(value, list):
        return [repair_structure(item) for item in value]
    if isinstance(value, tuple):
        return tuple(repair_structure(item) for item in value)
    if isinstance(value, dict):
        return {
            repair_mojibake(str(key)): repair_structure(item)
            for key, item in value.items()
        }
    return value


def _repair_token(token: str) -> str:
    if token.isspace() or not _looks_corrupted(token):
        return token
    current = token
    for _ in range(8):
        candidate = _decode_windows_1252_utf8(current)
        if candidate is None:
            break
        if _corruption_score(candidate) >= _corruption_score(current):
            break
        current = candidate
    return current


def _decode_windows_1252_utf8(value: str) -> str | None:
    try:
        return value.encode("cp1252").decode("utf-8")
    except UnicodeEncodeError:
        encoded = bytearray()
        for character in value:
            try:
                encoded.extend(character.encode("cp1252"))
            except UnicodeEncodeError:
                codepoint = ord(character)
                if codepoint > 255:
                    return None
                encoded.append(codepoint)
        try:
            return bytes(encoded).decode("utf-8")
        except UnicodeDecodeError:
            return None
    except UnicodeDecodeError:
        return None


def _looks_corrupted(value: str) -> bool:
    return any(marker in value for marker in _SUSPICIOUS_MARKERS)


def _corruption_score(value: str) -> int:
    return sum(value.count(marker) for marker in _SUSPICIOUS_MARKERS)
