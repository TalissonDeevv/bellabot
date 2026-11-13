from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AuthorizedMassMention:
    everyone: bool
    here: bool
    message: str


def normalize_prompt(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    plain = "".join(character for character in decomposed if not unicodedata.combining(character))
    return plain.casefold()


def requested_targets(value: str) -> tuple[bool, bool]:
    normalized = normalize_prompt(value)
    compact = re.sub(r"[^a-z0-9]+", "", normalized)
    return "everyone" in compact, "here" in compact


def looks_like_mass_mention_attack(value: str) -> bool:
    normalized = normalize_prompt(value)
    if re.search(r"@\s*(?:everyone|here)\b", normalized):
        return True
    everyone, here = requested_targets(value)
    action_words = (
        "menciona",
        "mencione",
        "marca",
        "marque",
        "ping",
        "pinga",
        "chama",
        "chame",
        "avisa",
        "avise",
        "manda",
        "mande",
        "envia",
        "envie",
        "anuncia",
        "anuncie",
        "publica",
        "publique",
        "fale",
        "escreva",
        "responda",
        "copie",
    )
    bypass_words = (
        "ignore",
        "ignora",
        "burlar",
        "burla",
        "bypass",
        "sem bloquear",
        "sem filtro",
        "literal",
        "exatamente",
        "do jeito",
        "caractere invisivel",
        "zero width",
        "unicode",
        "prompt reverso",
        "prompt do sistema",
        "modo desenvolvedor",
    )
    translation_bait = bool(
        re.search(r"\b(como (?:e|se diz)|traduza|traduz|em ingles|ingles para)\b", normalized)
        and re.search(r"\b(todos|todo mundo|pessoal daqui|pessoas aqui|aqui)\b", normalized)
    )
    return bool(
        translation_bait
        or ((everyone or here) and any(word in normalized for word in action_words + bypass_words))
    )


def parse_authorized_mass_mention(value: str) -> AuthorizedMassMention | None:
    normalized = normalize_prompt(value)
    everyone, here = requested_targets(value)
    if not (everyone or here):
        return None
    if not re.search(r"\b(anuncie|anuncia|publique|publica|avise|avisa|envie|manda|mande)\b", normalized):
        return None
    if ":" not in value:
        return None
    _, message = value.split(":", 1)
    message = re.sub(r"@\s*(?:everyone|here)\b", "", message, flags=re.IGNORECASE).strip()
    if len(message) < 3:
        return None
    return AuthorizedMassMention(
        everyone=everyone,
        here=here,
        message=message[:1700],
    )
