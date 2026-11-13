from __future__ import annotations

import time
from typing import Any

from website_guard.state import StateStore

from .intent_router import normalize_text


MUSIC_MEMORY_SECTION = "jarvis_music_memory"
NEFFEX_PRESENCE_SECTION = "neffex_presence_override"


DEFAULT_MUSIC_MEMORY: dict[str, Any] = {
    "favorite_artist": {
        "name": "NEFFEX",
        "description": (
            "NEFFEX é o cantor/projeto musical favorito da Bella. O gosto combina "
            "com a personalidade dela porque as músicas têm energia de superação, "
            "disciplina, persistência, ambição saudável e mentalidade de crescimento."
        ),
    },
    "songs": [
        {
            "title": "INSPIRED",
            "artist": "NEFFEX",
            "themes": [
                "superação",
                "resiliência",
                "autoconfiança",
                "transformar críticas em combustível",
                "motivação interna",
            ],
            "meaning": (
                "INSPIRED é uma música sobre continuar mesmo quando a mente pesa, "
                "quando o estresse e a pressão parecem testar tudo. Para a Bella, "
                "ela representa a ideia de encontrar força dentro de si, transformar "
                "dúvidas e críticas em impulso e seguir em frente sem depender da "
                "aprovação dos outros."
            ),
            "jarvis_view": (
                "Bella gosta dela porque a música parece um lembrete direto de que "
                "ninguém precisa estar perfeitamente bem para continuar evoluindo. "
                "O ponto central é usar dificuldade como combustível, não como freio."
            ),
        },
        {
            "title": "Victorious",
            "artist": "NEFFEX",
            "themes": [
                "persistência",
                "vitória",
                "trabalho duro",
                "disciplina",
                "evolução diária",
            ],
            "meaning": (
                "Victorious é um hino de persistência. A música passa a ideia de que "
                "vitória não nasce de sorte, mas de esforço repetido, sacrifício, "
                "urgência positiva e vontade de melhorar um pouco todos os dias."
            ),
            "jarvis_view": (
                "Bella gosta dela porque combina com a mentalidade de construir algo "
                "grande aos poucos: estudar, melhorar, trabalhar, cair, ajustar e "
                "continuar até vencer."
            ),
        },
    ],
}


class BellaMusicMemory:
    def __init__(self, state: StateStore) -> None:
        self.state = state

    async def ensure_seeded(self) -> dict[str, Any]:
        current = await self.state.get(MUSIC_MEMORY_SECTION, {})
        if isinstance(current, dict) and current.get("favorite_artist") and current.get("songs"):
            return current
        await self.state.set(MUSIC_MEMORY_SECTION, DEFAULT_MUSIC_MEMORY)
        return DEFAULT_MUSIC_MEMORY

    async def context_payload(self, query: str) -> list[dict[str, str]]:
        if not _is_music_memory_query(query):
            return []
        memory = await self.ensure_seeded()
        presence = await self.state.get(NEFFEX_PRESENCE_SECTION, {})
        artist = memory.get("favorite_artist") if isinstance(memory, dict) else {}
        songs = memory.get("songs") if isinstance(memory, dict) else []
        payload: list[dict[str, str]] = []
        if isinstance(presence, dict) and presence.get("enabled"):
            current_title = str(presence.get("current_title") or "").strip()
            duration = _safe_int(presence.get("duration"), 0)
            started_at = _safe_float(presence.get("started_at"), 0.0)
            remaining = max(0, int(started_at + duration - time.time())) if duration else 0
            if current_title:
                payload.append(
                    {
                        "entry_id": "jarvis-current-listening-presence",
                        "title": "Música atual da presence da Bella",
                        "type": "jarvis_current_presence",
                        "url": "",
                        "question": "Qual música a Bella está ouvindo agora?",
                        "content": (
                            "Estado atual real da presence da Bella no Discord. "
                            "Quando perguntarem o que ele está ouvindo agora, use esta "
                            "informação como resposta principal. "
                            f"Música atual: {current_title} - NEFFEX. "
                            f"Duração cadastrada: {_format_duration(duration)}. "
                            f"Troca estimada em: {_format_duration(remaining)}."
                        ),
                    }
                )
        payload.append(
            {
                "entry_id": "jarvis-favorite-artist",
                "title": "Cantor favorito da Bella",
                "type": "jarvis_music_memory",
                "url": "",
                "question": "Qual é o cantor favorito da Bella?",
                "content": (
                    "Anotação de memória musical da Bella. Use como referência, "
                    "sem copiar literalmente. "
                    + str(artist.get("description") or "")
                ),
            }
        )
        for song in songs if isinstance(songs, list) else []:
            if not isinstance(song, dict):
                continue
            title = str(song.get("title") or "").strip()
            if not title:
                continue
            payload.append(
                {
                    "entry_id": f"jarvis-song-{normalize_text(title).replace(' ', '-')}",
                    "title": f"Música que a Bella gosta: {title}",
                    "type": "jarvis_music_memory",
                    "url": "",
                    "question": f"O que {title} significa para a Bella?",
                    "content": (
                        "Anotação de memória musical da Bella. Use como referência, "
                        "sem copiar literalmente. "
                        f"Artista: {song.get('artist', artist.get('name', 'NEFFEX'))}. "
                        f"Temas: {', '.join(str(item) for item in song.get('themes', []))}. "
                        f"Significado: {song.get('meaning', '')} "
                        f"Visão da Bella: {song.get('jarvis_view', '')}"
                    ),
                }
            )
        return payload[:4]


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _format_duration(seconds: int | float | None) -> str:
    total = _safe_int(seconds, 0)
    minutes, remaining_seconds = divmod(max(0, total), 60)
    return f"{minutes}:{remaining_seconds:02d}"

def _is_music_memory_query(value: str) -> bool:
    text = normalize_text(value)
    if not text:
        return False
    triggers = (
        "cantor favorito",
        "artista favorito",
        "musica favorita",
        "musicas favoritas",
        "que musica voce gosta",
        "que musicas voce gosta",
        "qual musica voce gosta",
        "qual cantor voce gosta",
        "o que voce escuta",
        "o que voce esta escutando",
        "o que voce esta ouvindo",
        "o que vc ta escutando",
        "o que vc ta ouvindo",
        "que musica voce esta ouvindo",
        "que musica vc ta ouvindo",
        "qual musica voce esta ouvindo",
        "qual musica vc ta ouvindo",
        "musica que voce esta ouvindo",
        "musica que vc ta ouvindo",
        "ouvindo agora",
        "escutando agora",
        "tocando agora",
        "tocando na sua presence",
        "musica da sua presence",
        "neffex",
        "inspired",
        "victorious",
    )
    if any(trigger in text for trigger in triggers):
        return True
    return "jarvis" in text and any(word in text for word in ("cantor", "artista", "musica", "playlist"))
