from __future__ import annotations

import json
from typing import Any


class SnapshotNormalizer:
    """Produz snapshots estáveis para persistência, hashing e comparação."""

    COLLECTION_KEYS = {
        "roles": ("id",),
        "members": ("id",),
        "channels": ("id",),
        "automod": ("id",),
        "bots": ("id",),
        "webhooks": ("id",),
        "integrations": ("id",),
        "audit_log": ("id",),
    }

    def normalize(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        data = json.loads(json.dumps(snapshot, ensure_ascii=False, default=str))
        normalized = self._value(data)
        for key, identity in self.COLLECTION_KEYS.items():
            values = normalized.get(key)
            if isinstance(values, list):
                normalized[key] = sorted(values, key=lambda item: self._identity(item, identity))
        normalized["source_errors"] = sorted(str(item) for item in normalized.get("source_errors", []))
        normalized["areas_analyzed"] = sorted(str(item) for item in normalized.get("areas_analyzed", []))
        normalized["areas_unavailable"] = sorted(str(item) for item in normalized.get("areas_unavailable", []))
        return normalized

    def _value(self, value: Any) -> Any:
        if isinstance(value, dict):
            result = {str(key): self._value(value[key]) for key in sorted(value, key=str)}
            overwrites = result.get("overwrites")
            if isinstance(overwrites, list):
                result["overwrites"] = sorted(
                    overwrites,
                    key=lambda item: (
                        str(item.get("target_type", "")) if isinstance(item, dict) else "",
                        int(item.get("target_id", 0)) if isinstance(item, dict) else 0,
                    ),
                )
            return result
        if isinstance(value, list):
            normalized = [self._value(item) for item in value]
            if all(isinstance(item, (str, int, float, bool, type(None))) for item in normalized):
                return sorted(normalized, key=str)
            if all(isinstance(item, dict) and "id" in item for item in normalized):
                return sorted(normalized, key=lambda item: str(item.get("id", "")))
            return normalized
        return value

    @staticmethod
    def _identity(item: Any, fields: tuple[str, ...]) -> tuple[str, ...]:
        if not isinstance(item, dict):
            return (str(item),)
        return tuple(str(item.get(field, "")) for field in fields)
