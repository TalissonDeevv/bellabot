from __future__ import annotations

import sys
import zipfile
from pathlib import Path


EXCLUDE_DIRS = {
    ".venv",
    ".git",
    "__pycache__",
    "node_modules",
    ".mypy_cache",
    ".pytest_cache",
}
DEFAULT_EXCLUDE_NAMES = {".env", ".env.local"}
EXCLUDE_SUFFIXES = {".pyc", ".pyo", ".log", ".sqlite", ".db", ".zip"}


SAFE_ENV = """# Bella Nyelle Bot - ambiente seguro
# Preencha os valores reais antes de iniciar.
# Nunca envie este arquivo preenchido para ZIP publico.

DISCORD_TOKEN=

GEMINI_ENABLED=true
# Cole a API key do Gemini depois do "=".
# Exemplo: GEMINI_API_KEY=sua_chave_aqui
GEMINI_API_KEY=
GEMINI_MODEL=gemini-flash-lite-latest
GEMINI_TIMEOUT_SECONDS=3.0
GEMINI_TEMPERATURE=0.92
GEMINI_TOP_P=0.94
GEMINI_THINKING_LEVEL=low
BELLA_AI_PROVIDER=gemini
BELLA_FALLBACK_TO_GROQ=false
BELLA_PUBLIC_TYPING_ENABLED=true
BELLA_SOCIAL_TIMEOUT_SECONDS=2.2
BELLA_REGENERATE_INVALID=false

GROQ_ENABLED=false
GROQ_API_KEY=
GROQ_MODEL=
GROQ_VISION_MODEL=

AI_PUBLIC_ENABLED=true
AI_PUBLIC_REQUIRE_MENTION=true
AI_ALLOWED_GUILD_IDS=1467976297689911377
AI_CONTEXT_MESSAGES=8
AI_CONTEXT_TTL_MINUTES=60
AI_MAX_RESPONSE_CHARS=1800

MESSAGE_CONTENT_INTENT=true
MEMBERS_INTENT=true
BELLA_ECONOMY_MODE=true
"""


def should_include(path: Path, root: Path, *, preserve_env: bool = False) -> bool:
    relative = path.relative_to(root)
    if set(relative.parts) & EXCLUDE_DIRS:
        return False
    exclude_names = set() if preserve_env else DEFAULT_EXCLUDE_NAMES
    if path.name in exclude_names:
        return False
    return path.suffix.casefold() not in EXCLUDE_SUFFIXES


def package(root: Path, output: Path, *, preserve_env: bool = False) -> None:
    if output.exists():
        output.unlink()
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in root.rglob("*"):
            if path.is_dir() or not should_include(path, root, preserve_env=preserve_env):
                continue
            archive.write(path, path.relative_to(root).as_posix())
        if not preserve_env:
            archive.writestr(".env", SAFE_ENV)


def main() -> int:
    preserve_env = "--preserve-env" in sys.argv
    args = [arg for arg in sys.argv[1:] if arg != "--preserve-env"]
    root = Path(args[0]).resolve() if len(args) > 0 else Path.cwd()
    output = (
        Path(args[1]).resolve()
        if len(args) > 1
        else Path.home() / "Downloads" / "Bella Nyelle Bot News - Gemini.zip"
    )
    package(root, output, preserve_env=preserve_env)
    print(output)
    print(output.stat().st_size)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
