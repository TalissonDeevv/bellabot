from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .normalizer import SnapshotNormalizer


@dataclass(frozen=True)
class SnapshotChange:
    resource_key: str
    change_type: str
    before: Any
    after: Any


class SnapshotDiffEngine:
    COLLECTIONS = ("roles", "members", "channels", "automod", "bots", "webhooks", "integrations")

    def __init__(self) -> None:
        self.normalizer = SnapshotNormalizer()

    def compare(self, before: dict[str, Any], after: dict[str, Any]) -> list[SnapshotChange]:
        if not before:
            return []
        previous = self._projection(self.normalizer.normalize(before))
        current = self._projection(self.normalizer.normalize(after))
        changes: list[SnapshotChange] = []
        for key in sorted(set(previous) | set(current)):
            if key not in previous:
                changes.append(SnapshotChange(key, "ADDED", None, current[key]))
            elif key not in current:
                changes.append(SnapshotChange(key, "REMOVED", previous[key], None))
            elif previous[key] != current[key]:
                changes.append(SnapshotChange(key, "CHANGED", previous[key], current[key]))
        return changes

    def _projection(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        projection: dict[str, Any] = {"guild:settings": snapshot.get("guild", {})}
        for collection in self.COLLECTIONS:
            for item in snapshot.get(collection, []):
                if isinstance(item, dict):
                    projection[f"{collection}:{item.get('id')}"] = item
        return projection
