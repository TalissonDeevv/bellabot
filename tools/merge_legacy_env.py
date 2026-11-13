from __future__ import annotations

import sys
from pathlib import Path


REQUIRED_OVERRIDES = {
    "GEMINI_ENABLED": "true",
    "GEMINI_MODEL": "gemini-flash-lite-latest",
    "GEMINI_TIMEOUT_SECONDS": "3.0",
    "GEMINI_TEMPERATURE": "0.92",
    "GEMINI_TOP_P": "0.94",
    "GEMINI_THINKING_LEVEL": "low",
    "BELLA_AI_PROVIDER": "gemini",
    "BELLA_FALLBACK_TO_GROQ": "false",
    "BELLA_PUBLIC_TYPING_ENABLED": "true",
    "BELLA_SOCIAL_TIMEOUT_SECONDS": "2.2",
    "BELLA_REGENERATE_INVALID": "false",
    "AI_ALLOWED_GUILD_IDS": "1467976297689911377",
}


def _read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value
    return values


def merge_legacy_env(legacy_path: Path, current_path: Path) -> None:
    legacy_lines = legacy_path.read_text(encoding="utf-8", errors="replace").splitlines()
    current_values = _read_env(current_path)
    token_value = current_values.get("DISCORD_TOKEN")
    gemini_key = current_values.get("GEMINI_API_KEY")
    overrides = dict(REQUIRED_OVERRIDES)
    if gemini_key is not None:
        overrides["GEMINI_API_KEY"] = gemini_key
    else:
        overrides["GEMINI_API_KEY"] = ""
    if token_value is not None:
        overrides["DISCORD_TOKEN"] = token_value

    seen: set[str] = set()
    output: list[str] = []
    for line in legacy_lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            output.append(line)
            continue
        key = line.split("=", 1)[0].strip()
        if key in seen:
            continue
        seen.add(key)
        if key in overrides:
            output.append(f"{key}={overrides[key]}")
        else:
            output.append(line)

    missing = [f"{key}={value}" for key, value in overrides.items() if key not in seen]
    if missing:
        if output and output[-1].strip():
            output.append("")
        output.append("# Gemini / Bella Nyelle")
        output.extend(missing)

    current_path.write_text("\n".join(output).rstrip() + "\n", encoding="utf-8")


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: merge_legacy_env.py <legacy.env> <current.env>")
        return 2
    merge_legacy_env(Path(sys.argv[1]), Path(sys.argv[2]))
    print("ENV_MERGED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
