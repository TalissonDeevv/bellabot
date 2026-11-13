from __future__ import annotations

import asyncio
import copy
import json
import os
import shutil
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from .encoding import repair_structure


class StateStore:
    def __init__(self, path: str | Path = "data/state.json") -> None:
        self.path = Path(path)
        self.db_path = self.path.with_suffix(".db")
        self.sections_path = self.path.with_name(f"{self.path.stem}_sections")
        configured_sections = os.getenv(
            "STATE_EXTERNAL_SECTIONS",
            "operational_events,operational_analysis_cache,server_backup_resources",
        )
        self._external_sections = {
            section.strip()
            for section in configured_sections.split(",")
            if section.strip()
        }
        self._lock = asyncio.Lock()
        self._data: dict[str, Any] = {}
        self._last_database_save_monotonic = 0.0
        self._sqlite_mirror_interval_seconds = max(
            0.0,
            float(os.getenv("STATE_SQLITE_MIRROR_INTERVAL_SECONDS", "45") or "45"),
        )
        self._sqlite_large_payload_bytes = max(
            256_000,
            int(os.getenv("STATE_SQLITE_LARGE_PAYLOAD_BYTES", "2000000") or "2000000"),
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists() and (
            not self.db_path.exists()
            or self.path.stat().st_mtime >= self.db_path.stat().st_mtime
        ):
            self._data = self._load_json_file()
            if not self._data and self.db_path.exists():
                try:
                    self._data = self._load_database()
                except sqlite3.DatabaseError:
                    _quarantine_file(self.db_path)
                    self._data = {}
        elif self.db_path.exists():
            try:
                self._data = self._load_database()
            except sqlite3.DatabaseError:
                _quarantine_file(self.db_path)
                self._data = self._load_json_file()
        elif self.path.exists():
            self._data = self._load_json_file()
        repaired = repair_structure(self._data)
        if isinstance(repaired, dict):
            self._data = repaired
        self._migrate_external_sections()
        self._load_external_sections()
        self._save()

    async def get(self, key: str, default: Any = None) -> Any:
        async with self._lock:
            return self._data.get(key, default)

    async def set(self, key: str, value: Any) -> None:
        async with self._lock:
            self._data[key] = value
            await asyncio.to_thread(self._save_key, key)

    async def set_many(self, values: dict[str, Any]) -> None:
        async with self._lock:
            self._data.update(values)
            await asyncio.to_thread(self._save_keys, set(values))

    async def remove(self, key: str) -> None:
        async with self._lock:
            if key in self._data:
                del self._data[key]
                await asyncio.to_thread(self._save_key, key)

    async def update_section(self, section: str, key: str, value: Any) -> None:
        async with self._lock:
            current = self._data.setdefault(section, {})
            current[key] = value
            await asyncio.to_thread(self._save_key, section)

    async def get_section(self, section: str) -> dict[str, Any]:
        async with self._lock:
            value = self._data.get(section, {})
            return copy.deepcopy(value) if isinstance(value, dict) else {}

    async def mutate_section(
        self,
        section: str,
        mutator: Callable[[dict[str, Any]], None],
    ) -> dict[str, Any]:
        async with self._lock:
            current = self._data.setdefault(section, {})
            if not isinstance(current, dict):
                current = {}
                self._data[section] = current
            mutator(current)
            await asyncio.to_thread(self._save_key, section)
            return copy.deepcopy(current)

    async def snapshot(self) -> dict[str, Any]:
        async with self._lock:
            return copy.deepcopy(self._data)

    def _save(self) -> None:
        payload = json.dumps(
            {
                key: value
                for key, value in self._data.items()
                if key not in self._external_sections
            },
            ensure_ascii=False,
            indent=2,
        )
        self._write_json_atomically(payload)
        if self._should_save_database(self._estimated_payload_bytes(payload)):
            self._save_database()

    def _save_key(self, key: str) -> None:
        self._save_keys({key})

    def _save_keys(self, keys: set[str]) -> None:
        external = keys & self._external_sections
        for section in external:
            self._write_external_section(section)
        if keys - self._external_sections:
            self._save()
            return
        if self._should_save_database(self._estimated_payload_bytes()):
            self._save_database()

    def _external_path(self, section: str) -> Path:
        return self.sections_path / f"{section}.json"

    def _migrate_external_sections(self) -> None:
        pending = [
            section
            for section in self._external_sections
            if section in self._data and not self._external_path(section).exists()
        ]
        if not pending:
            return
        if self.path.exists():
            backup = self.path.with_name(
                f"{self.path.stem}.pre-externalization.backup{self.path.suffix}"
            )
            if not backup.exists():
                shutil.copy2(self.path, backup)
        for section in pending:
            self._write_external_section(section)

    def _load_external_sections(self) -> None:
        for section in self._external_sections:
            path = self._external_path(section)
            if not path.exists():
                continue
            try:
                value = json.loads(_decode_json_file(path))
            except (UnicodeDecodeError, json.JSONDecodeError):
                _quarantine_file(path)
                continue
            repaired = repair_structure(value)
            if isinstance(repaired, dict):
                self._data[section] = repaired

    def _write_external_section(self, section: str) -> None:
        path = self._external_path(section)
        value = self._data.get(section)
        if value is None:
            path.unlink(missing_ok=True)
            return
        self.sections_path.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        _write_text_atomically(path, payload)

    def _estimated_payload_bytes(self, main_payload: str | None = None) -> int:
        total = len((main_payload or "").encode("utf-8"))
        if main_payload is None and self.path.exists():
            total += self.path.stat().st_size
        for section in self._external_sections:
            path = self._external_path(section)
            if path.exists():
                total += path.stat().st_size
        return total

    def _write_json_atomically(self, payload: str) -> None:
        temp_path = self.path.with_name(
            f"{self.path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        )
        try:
            temp_path.write_text(payload, encoding="utf-8")
            _replace_with_retry(temp_path, self.path)
        finally:
            temp_path.unlink(missing_ok=True)

    def _should_save_database(self, payload_bytes: int) -> bool:
        if not self.db_path.exists():
            return True
        if payload_bytes <= self._sqlite_large_payload_bytes:
            return True
        now = time.monotonic()
        if now - self._last_database_save_monotonic < self._sqlite_mirror_interval_seconds:
            return False
        self._last_database_save_monotonic = now
        return True

    def _load_json_file(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            value = json.loads(_decode_json_file(self.path))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    def _load_database(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        connection = sqlite3.connect(self.db_path)
        try:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS state "
                "(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            for key, value in connection.execute(
                "SELECT key, value FROM state"
            ):
                try:
                    result[str(key)] = repair_structure(json.loads(str(value)))
                except json.JSONDecodeError:
                    continue
        finally:
            connection.close()
        return result

    def _save_database(self) -> None:
        try:
            self._write_database()
        except sqlite3.DatabaseError:
            _quarantine_file(self.db_path)
            self._write_database()

    def _write_database(self) -> None:
        connection = sqlite3.connect(self.db_path)
        try:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS state "
                "(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            connection.execute("BEGIN IMMEDIATE")
            connection.executemany(
                "INSERT INTO state(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                [
                    (
                        key,
                        json.dumps(value, ensure_ascii=False, separators=(",", ":")),
                    )
                    for key, value in self._data.items()
                ],
            )
            keys = list(self._data)
            if keys:
                placeholders = ",".join("?" for _ in keys)
                connection.execute(
                    f"DELETE FROM state WHERE key NOT IN ({placeholders})",
                    keys,
                )
            else:
                connection.execute("DELETE FROM state")
            connection.commit()
        finally:
            connection.close()


def _decode_json_file(path: Path) -> str:
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _quarantine_file(path: Path) -> None:
    if not path.exists():
        return
    for suffix in range(1, 100):
        backup = path.with_name(f"{path.name}.corrupt-{suffix}")
        if not backup.exists():
            path.replace(backup)
            return
    path.unlink(missing_ok=True)


def _replace_with_retry(source: Path, target: Path) -> None:
    last_error: PermissionError | None = None
    for attempt in range(6):
        try:
            source.replace(target)
            return
        except PermissionError as exc:
            last_error = exc
            time.sleep(0.05 * (attempt + 1))
    if last_error is not None:
        raise last_error


def _write_text_atomically(path: Path, payload: str) -> None:
    temp_path = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        temp_path.write_text(payload, encoding="utf-8")
        _replace_with_retry(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)
