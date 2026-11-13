from __future__ import annotations

import re
from copy import deepcopy
from typing import Any

LEGACY_JARVIS_GUILD_ID = 1467976297689911377
DEFAULT_LEGACY_JARVIS_CHANNEL_IDS: tuple[int, ...] = (
    1467976299233673238,
    1467978642377146480,
    1539798308497793044,
    1473808794990416047,
    1524342251033989201,
    1467981456239497466,
    1467981519732867255,
    1467979975566033160,
    1489777422688850082,
)
LEGACY_JARVIS_MIGRATION_SECTION = "legacy_jarvis_embed_migrations"
LEGACY_JARVIS_MAX_ATTACHMENT_BYTES = 8 * 1024 * 1024

_VISIBLE_EMBED_KEYS = {"title", "description"}
_VISIBLE_NESTED_KEYS = {
    "author": {"name"},
    "footer": {"text"},
}
_VISIBLE_COMPONENT_KEYS = {"content", "label", "description", "placeholder"}
_COMPONENTS_V2_TYPES = {9, 10, 11, 12, 13, 14, 17}

_GRAMMATICAL_REPLACEMENTS: tuple[tuple[str, str], ...] = (
    (r"\bcom\s+o\s+Jarvis\b", "com a Bella"),
    (r"\bpara\s+o\s+Jarvis\b", "para a Bella"),
    (r"\bpro\s+Jarvis\b", "pra Bella"),
    (r"\bpelo\s+Jarvis\b", "pela Bella"),
    (r"\bdo\s+Jarvis\b", "da Bella"),
    (r"\bao\s+Jarvis\b", "à Bella"),
    (r"\bo\s+Jarvis\b", "a Bella"),
    (r"\bAssistente\s+Jarvis\b", "Bella Nyelle"),
    (r"\bSenhor\s+Jarvis\b", "Bella Nyelle"),
)

_PLAIN_REPLACEMENTS: tuple[tuple[str, str], ...] = (
    ("JARVIS", "BELLA"),
    ("Jarvis", "Bella"),
    ("jarvis", "bella"),
)


def contains_jarvis_identity(value: Any) -> bool:
    if isinstance(value, str):
        return "jarvis" in value.casefold() or "senhor jarvis" in value.casefold()
    if isinstance(value, dict):
        return any(contains_jarvis_identity(item) for item in value.values())
    if isinstance(value, (list, tuple, set)):
        return any(contains_jarvis_identity(item) for item in value)
    return False


def adapt_jarvis_identity_text(value: str) -> str:
    text = str(value or "")
    if not text:
        return text
    for pattern, replacement in _GRAMMATICAL_REPLACEMENTS:
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
    for old, new in _PLAIN_REPLACEMENTS:
        text = text.replace(old, new)
    return text


def adapt_jarvis_embed_payload(payload: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(payload)
    for key in _VISIBLE_EMBED_KEYS:
        if isinstance(result.get(key), str):
            result[key] = adapt_jarvis_identity_text(result[key])
    for parent, child_keys in _VISIBLE_NESTED_KEYS.items():
        nested = result.get(parent)
        if isinstance(nested, dict):
            for key in child_keys:
                if isinstance(nested.get(key), str):
                    nested[key] = adapt_jarvis_identity_text(nested[key])
    fields = result.get("fields")
    if isinstance(fields, list):
        for field in fields:
            if not isinstance(field, dict):
                continue
            for key in ("name", "value"):
                if isinstance(field.get(key), str):
                    field[key] = adapt_jarvis_identity_text(field[key])
    return result


def adapt_jarvis_component_payload(payload: Any) -> Any:
    if isinstance(payload, str):
        return adapt_jarvis_identity_text(payload)
    if isinstance(payload, list):
        return [adapt_jarvis_component_payload(item) for item in payload]
    if not isinstance(payload, dict):
        return deepcopy(payload)
    result: dict[str, Any] = {}
    for key, value in payload.items():
        if key in {"url", "custom_id"}:
            result[key] = value
        elif key in _VISIBLE_COMPONENT_KEYS and isinstance(value, str):
            result[key] = adapt_jarvis_identity_text(value)
        else:
            result[key] = adapt_jarvis_component_payload(value)
    return result


def component_payloads_have_v2(payloads: Any) -> bool:
    if isinstance(payloads, dict):
        try:
            component_type = int(payloads.get("type") or 0)
        except (TypeError, ValueError):
            component_type = 0
        if component_type in _COMPONENTS_V2_TYPES:
            return True
        return any(component_payloads_have_v2(value) for value in payloads.values())
    if isinstance(payloads, (list, tuple, set)):
        return any(component_payloads_have_v2(item) for item in payloads)
    return False


def attachment_filenames_from_payload(payload: Any) -> set[str]:
    filenames: set[str] = set()
    _collect_attachment_filenames(payload, filenames)
    return filenames


def _collect_attachment_filenames(value: Any, filenames: set[str]) -> None:
    if isinstance(value, str):
        match = re.match(r"attachment://([^?#]+)", value)
        if match:
            filenames.add(match.group(1))
        return
    if isinstance(value, dict):
        for item in value.values():
            _collect_attachment_filenames(item, filenames)
        return
    if isinstance(value, (list, tuple, set)):
        for item in value:
            _collect_attachment_filenames(item, filenames)
