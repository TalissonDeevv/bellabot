"""Transcrição e briefing de áudios (reuniões com cliente, mensagens de voz).

Fluxo:
1. o ffmpeg comprime o áudio (mono, 16 kHz, mp3 32 kbps) e divide em partes de
   10 minutos, porque os serviços de transcrição aceitam no máximo ~25 MB;
2. cada parte é transcrita pela FreeLLMAPI (/v1/audio/transcriptions) ou, se
   ela estiver desligada, pela Groq direta (Whisper);
3. a transcrição vira briefing de orçamento, resumo ou texto puro. Transcrições
   longas são resumidas por partes e depois consolidadas.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from .freellmapi_client import freellmapi_enabled

LOGGER = logging.getLogger(__name__)

AUDIO_EXTENSIONS = {
    ".mp3", ".m4a", ".ogg", ".oga", ".opus", ".wav", ".webm", ".flac",
    ".aac", ".mp4", ".mov", ".mkv", ".wma", ".amr",
}
MAX_DIRECT_UPLOAD_BYTES = 24 * 1024 * 1024
SEGMENT_SECONDS = 600
NOTES_CHUNK_CHARS = 8000

ANALYST_SYSTEM_PROMPT = (
    "Você é um analista que prepara documentos de trabalho para o Talisson Souza, "
    "desenvolvedor de software. Escreva em português do Brasil, de forma objetiva, "
    "usando somente o que aparece na transcrição. Nunca invente valores, prazos, "
    "nomes ou requisitos. Quando algo não foi mencionado, escreva 'não mencionado'. "
    "Não use emojis."
)

BRIEFING_INSTRUCTIONS = """Prepare um BRIEFING PARA ORÇAMENTO a partir da reunião abaixo entre o Talisson e um possível cliente.

Use exatamente estas seções, com tópicos curtos:
**Cliente e contexto**: quem é, empresa/negócio, situação atual.
**Problema ou objetivo**: o que a pessoa quer resolver ou alcançar.
**Escopo e funcionalidades**: tudo que foi pedido, um item por linha.
**Tecnologias, integrações e referências**: sistemas, plataformas, exemplos citados.
**Prazo**: datas e urgência mencionadas.
**Orçamento e valores**: números ou faixas mencionados.
**Quem decide**: decisor e processo de aprovação.
**Dúvidas em aberto**: o que o Talisson ainda precisa perguntar para orçar.
**Próximos passos combinados**
**Pontos de atenção para o orçamento**: riscos, complexidade, coisas que podem aumentar custo ou prazo.
"""

SUMMARY_INSTRUCTIONS = """Faça um RESUMO do áudio abaixo:
**Resumo**: 3 a 6 frases com o essencial.
**Pontos principais**: tópicos curtos.
**Decisões e combinados**: se houver.
**Pendências**: o que ficou para depois, se houver.
"""


class TranscriptionError(RuntimeError):
    pass


@dataclass(frozen=True)
class AudioResult:
    transcript: str
    document: str
    parts: int
    mode: str


def is_audio_attachment(filename: str, content_type: str | None) -> bool:
    kind = (content_type or "").split(";")[0].strip().casefold()
    if kind.startswith(("audio/", "video/")):
        return True
    return Path(filename or "").suffix.casefold() in AUDIO_EXTENSIONS


def normalize_mode(value: str) -> str:
    text = (value or "").casefold()
    if "transcri" in text or "texto" in text:
        return "transcricao"
    if "resum" in text:
        return "resumo"
    return "briefing"


async def split_audio(source: Path, workdir: Path) -> list[Path]:
    """Comprime e divide com ffmpeg; sem ffmpeg, manda o arquivo inteiro se couber."""
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        if source.stat().st_size <= MAX_DIRECT_UPLOAD_BYTES:
            return [source]
        raise TranscriptionError(
            "o áudio passa de 24 MB e o ffmpeg não está instalado para dividir."
        )
    pattern = workdir / "parte_%03d.mp3"
    process = await asyncio.create_subprocess_exec(
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(source),
        "-vn", "-ac", "1", "-ar", "16000", "-c:a", "libmp3lame", "-b:a", "32k",
        "-f", "segment", "-segment_time", str(SEGMENT_SECONDS), "-reset_timestamps", "1",
        str(pattern),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await process.communicate()
    parts = sorted(workdir.glob("parte_*.mp3"))
    if process.returncode != 0 or not parts:
        detail = stderr.decode("utf-8", "replace").strip()[-300:]
        raise TranscriptionError(f"não consegui ler esse áudio ({detail or 'formato inválido'}).")
    return parts


def _transcription_endpoint() -> tuple[str, str, str]:
    if freellmapi_enabled():
        base = os.getenv("FREELLMAPI_BASE_URL", "http://127.0.0.1:3001/v1").rstrip("/")
        if not base.endswith("/v1"):
            base += "/v1"
        return (
            f"{base}/audio/transcriptions",
            os.getenv("FREELLMAPI_API_KEY", ""),
            os.getenv("FREELLMAPI_TRANSCRIBE_MODEL", "whisper-large-v3-turbo"),
        )
    return (
        "https://api.groq.com/openai/v1/audio/transcriptions",
        os.getenv("GROQ_API_KEY", ""),
        os.getenv("GROQ_TRANSCRIBE_MODEL", "whisper-large-v3-turbo"),
    )


async def transcribe_file(path: Path, *, language: str = "pt") -> str:
    url, key, model = _transcription_endpoint()
    if not key:
        raise TranscriptionError("nenhuma chave configurada para transcrever áudio.")
    async with httpx.AsyncClient(timeout=300) as client:
        for attempt in range(3):
            with path.open("rb") as handle:
                response = await client.post(
                    url,
                    headers={"Authorization": f"Bearer {key}"},
                    files={"file": (path.name, handle, "application/octet-stream")},
                    data={"model": model, "language": language, "response_format": "json"},
                )
            if response.status_code == 429 and attempt < 2:
                await asyncio.sleep(min(30.0, 5.0 * (attempt + 1)))
                continue
            if response.status_code >= 400:
                raise TranscriptionError(
                    f"a transcrição falhou (HTTP {response.status_code}): {response.text[:200]}"
                )
            return str(response.json().get("text", "")).strip()
    raise TranscriptionError("a transcrição ficou sem cota; tenta de novo em alguns minutos.")


async def transcribe_audio(source: Path, workdir: Path) -> tuple[str, int]:
    parts = await split_audio(source, workdir)
    texts: list[str] = []
    for index, part in enumerate(parts):
        text = await transcribe_file(part)
        if len(parts) > 1:
            start = index * SEGMENT_SECONDS // 60
            texts.append(f"[{start:02d} min]\n{text}")
        else:
            texts.append(text)
    transcript = "\n\n".join(item for item in texts if item.strip())
    if not transcript.strip():
        raise TranscriptionError("não ouvi fala nenhuma nesse áudio.")
    return transcript, len(parts)


def _chunks(text: str, size: int) -> list[str]:
    chunks: list[str] = []
    current = ""
    for paragraph in text.split("\n"):
        if len(current) + len(paragraph) + 1 > size and current:
            chunks.append(current)
            current = ""
        current += paragraph + "\n"
        while len(current) > size:
            chunks.append(current[:size])
            current = current[size:]
    if current.strip():
        chunks.append(current)
    return chunks


async def build_document(service: Any, transcript: str, mode: str) -> str:
    if mode == "transcricao":
        return ""
    material = transcript
    if len(transcript) > NOTES_CHUNK_CHARS + 1000:
        notes: list[str] = []
        parts = _chunks(transcript, NOTES_CHUNK_CHARS)
        for index, part in enumerate(parts, start=1):
            note = await service.generate_text(
                "Anote de forma factual e completa tudo desta parte da reunião que "
                "importa para entender o pedido: necessidades, funcionalidades, números, "
                "prazos, nomes, decisões e dúvidas. Tópicos curtos.\n\n"
                f"PARTE {index} DE {len(parts)}:\n{part}",
                system_prompt=ANALYST_SYSTEM_PROMPT,
                max_output_tokens=900,
                timeout_seconds=90,
                use_cache=False,
                prompt_char_limit=NOTES_CHUNK_CHARS + 1500,
            )
            notes.append(f"Parte {index}:\n{note.strip()}")
        material = "NOTAS DA REUNIÃO (por partes):\n\n" + "\n\n".join(notes)
    instructions = BRIEFING_INSTRUCTIONS if mode == "briefing" else SUMMARY_INSTRUCTIONS
    document = await service.generate_text(
        f"{instructions}\n\n{material}",
        system_prompt=ANALYST_SYSTEM_PROMPT,
        max_output_tokens=900,
        timeout_seconds=120,
        use_cache=False,
        prompt_char_limit=16000,
    )
    return document.strip()


async def process_audio(
    service: Any,
    source: Path,
    workdir: Path,
    mode: str,
) -> AudioResult:
    transcript, parts = await transcribe_audio(source, workdir)
    document = await build_document(service, transcript, mode)
    return AudioResult(transcript=transcript, document=document, parts=parts, mode=mode)
