from __future__ import annotations

from pathlib import Path


UPDATES = {
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
ENSURE_BLANK_IF_MISSING = ("GEMINI_API_KEY",)


def update_env(path: Path) -> None:
    if not path.exists():
        path.write_text("", encoding="utf-8")
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    seen: set[str] = set()
    output: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            output.append(line)
            continue
        key = line.split("=", 1)[0].strip()
        if key in seen:
            continue
        seen.add(key)
        if key in UPDATES:
            output.append(f"{key}={UPDATES[key]}")
        elif key in ENSURE_BLANK_IF_MISSING:
            output.append(line if line.split("=", 1)[1].strip() else f"{key}=")
        else:
            output.append(line)
    missing = [f"{key}={value}" for key, value in UPDATES.items() if key not in seen]
    missing.extend(f"{key}=" for key in ENSURE_BLANK_IF_MISSING if key not in seen)
    if missing:
        if output and output[-1].strip():
            output.append("")
        output.append("# Gemini / Bella Nyelle")
        output.extend(missing)
    path.write_text("\n".join(output).rstrip() + "\n", encoding="utf-8")


if __name__ == "__main__":
    update_env(Path(".env"))
    print("ENV_UPDATED")
