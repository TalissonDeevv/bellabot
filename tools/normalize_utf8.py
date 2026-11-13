from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from website_guard.encoding import repair_mojibake

TEXT_SUFFIXES = {
    ".bat",
    ".env",
    ".json",
    ".md",
    ".py",
    ".ps1",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}
EXCLUDED_DIRECTORIES = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".venv",
    "__pycache__",
    "data",
}
EXCLUDED_SUFFIXES = {
    ".log",
    ".ndjson",
}


def main() -> int:
    changed = 0
    failed: list[str] = []
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file():
            continue
        if any(part in EXCLUDED_DIRECTORIES for part in path.parts):
            continue
        if path.suffix.casefold() in EXCLUDED_SUFFIXES:
            continue
        if path.name != ".env" and path.suffix.casefold() not in TEXT_SUFFIXES:
            continue
        try:
            raw = path.read_bytes()
            text = _decode_text(raw)
            repaired = repair_mojibake(text)
            normalized = repaired.replace("\r\n", "\n").replace("\r", "\n")
            encoding = "utf-8-sig" if path.suffix.casefold() == ".ps1" else "utf-8"
            encoded = normalized.encode(encoding)
            if raw != encoded:
                path.write_bytes(encoded)
                changed += 1
        except (OSError, UnicodeError) as exc:
            failed.append(f"{path.relative_to(ROOT)}: {type(exc).__name__}")
    print(f"Normalização UTF-8 concluída: {changed} arquivo(s) atualizado(s).")
    if failed:
        print("Arquivos ignorados: " + "; ".join(failed), file=sys.stderr)
        return 1
    return 0


def _decode_text(raw: bytes) -> str:
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("cp1252")


if __name__ == "__main__":
    raise SystemExit(main())
