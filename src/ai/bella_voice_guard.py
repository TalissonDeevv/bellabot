from __future__ import annotations

import re

from .intent_router import normalize_text


BELLA_AUTHOR_MARKERS = ("bella", "nyelle", "jarvis")

GENERIC_BELLA_PATTERNS = (
    "curtindo a vibe do discord",
    "curtindo a vibe aqui no discord",
    "curtindo a galera",
    "batendo papo com a galera",
    "trocando ideia com a galera",
    "e voce como ta",
    "e vc como ta",
    "como posso ajudar",
    "como posso te ajudar",
    "como posso ajuda-lo",
    "em que posso ajudar",
    "em que posso te ajudar",
    "o que voce precisa",
    "que voce precisa",
    "estou aqui para ajudar",
    "estou aqui para auxiliar",
    "estou a disposicao",
    "fico a disposicao",
    "posso auxilia-lo",
    "posso ajudar em algo",
    "travei um pouquinho",
    "me manda o ponto principal",
    "respondo certinho",
    "tudo sob controle",
    "meu bom",
    "tudo otimo por aqui",
    "tudo tranquilo por aqui",
    "tudo bem por aqui",
    "to por aqui",
    "tô por aqui",
    "to aqui, o que manda",
    "tô aqui, o que manda",
    "to aqui o que manda",
    "tô aqui o que manda",
    "o que manda",
    "manda ai o que precisar",
    "manda aí o que precisar",
    "tudo bem contigo",
    "tudo certo contigo",
    "estou bem e voce",
    "to bem e voce",
    "to bem e vc",
    "oi estou aqui",
    "oie estou aqui",
    "oi to aqui",
    "oie to aqui",
    "to aqui e voce",
    "to aqui e vc",
    "pronto para auxilia-lo",
    "pronta para auxilia-lo",
    "pronto para ajudar",
    "pronta para ajudar",
    "vou responder como a bella",
    "vou responder aqui em json",
    "responder aqui em json",
    "responder em json",
    "como a bella costuma fazer",
    "qualquer coisa e so chamar",
    "qualquer coisa e só chamar",
    "se precisar de alguma coisa",
    "se precisar de algo",
    "gosto de trocar ideia",
    "gosto de trocar ideias",
    "adoro conversar quando",
    "me sinto a vontade",
    "me sinto à vontade",
    "trocar ideia aqui",
    "trocar ideias aqui",
    "aqui no discord",
    "pode contar comigo para conversar",
    "trocar ideias e ajudar no que precisar",
    "hoje to aproveitando o dia",
    "revisada nos projetos",
    "revisando projetos",
    "bater um papo no voice",
    "ouvindo umas musicas novas",
    "ouvindo umas músicas novas",
)

BELLA_TEXTURE_MARKERS = (
    "kkkk",
    "hihi",
    "hehe",
    "ksks",
    "rsrs",
    "hmm",
    "pera",
    "vish",
    "poxa",
    "mds",
    "oiee",
    "quietinha",
    "sem graca",
    "sem graça",
    "a-ah",
    "ah ",
    "boa",
    "bora",
    "eita",
    "tô",
    "to ",
)


def is_bella_author(author: object) -> bool:
    text = normalize_text(str(author or ""))
    return any(marker in text for marker in BELLA_AUTHOR_MARKERS)


def is_generic_bella_text(value: object) -> bool:
    text = normalize_text(str(value or ""))
    if not text:
        return False
    if any(phrase in text for phrase in GENERIC_BELLA_PATTERNS):
        return True
    if re.search(r"\boi[!,.\s]+tudo\s+(?:bem|bom|otimo|ótimo)\b", text):
        return True
    if re.search(r"\boie?[!,.\s]+tudo\s+(?:bem|bom|otimo|ótimo)\b", text):
        return True
    if re.search(r"\bol[aá][!,.\s]+tudo\s+(?:bem|bom|otimo|ótimo)\b", text):
        return True
    if re.search(r"\b(?:bom dia|boa tarde|boa noite)[!,.\s]+(?:estou|to|t[oô])\s+aqui\b", text):
        return True
    return False


def is_status_acknowledgement(value: object) -> bool:
    raw = str(value or "")
    text = normalize_text(raw)
    if "?" in raw or any(
        marker in text
        for marker in (
            "contigo",
            "com voce",
            "com vc",
            "como voce",
            "como vc",
            "e voce",
            "e vc",
        )
    ):
        return False
    return any(
        marker in text
        for marker in (
            "to bem tambem",
            "tô bem tambem",
            "tambem to bem",
            "tambem tô bem",
            "eu to bem tambem",
            "eu tô bem tambem",
            "to bem tbm",
            "tô bem tbm",
            "tbm to bem",
            "tbm tô bem",
            "tudo bem tambem",
            "tudo bom tambem",
            "to bem",
            "tô bem",
            "de boa",
            "suave",
        )
    )


def looks_like_unfounded_bella_activity(user_message: object, answer: object) -> bool:
    user_text = normalize_text(str(user_message or ""))
    response = normalize_text(str(answer or ""))
    asks_current_activity = any(
        marker in user_text
        for marker in (
            "oque anda fazendo",
            "o que anda fazendo",
            "o que ta fazendo",
            "oque ta fazendo",
            "vc ta fazendo",
            "voce ta fazendo",
            "ta fazendo de bom",
            "esta fazendo de bom",
            "vc ta desenhando",
            "voce ta desenhando",
            "qual personagem vc",
            "qual personagem voce",
            "que personagem vc",
            "que personagem voce",
            "o que vc ta desenhando",
            "o que voce ta desenhando",
        )
    )
    invented_activity_markers = (
        "to desenhando",
        "tô desenhando",
        "estou desenhando",
        "to criando",
        "tô criando",
        "estou criando",
        "trabalhando numa personagem",
        "trabalhando em uma personagem",
        "heroina de rpg",
        "heroína de rpg",
        "ladra de sombras",
        "revisada nos projetos",
        "revisando projetos",
        "bater um papo no voice",
        "ouvindo umas musicas novas",
        "ouvindo umas músicas novas",
        "aproveitando o dia pra",
    )
    return asks_current_activity and any(
        marker in response for marker in invented_activity_markers
    )


def is_generic_bella_response(user_message: object, answer: object) -> bool:
    response = normalize_text(str(answer or "")).strip()
    user_text = normalize_text(str(user_message or "")).strip()
    if not response:
        return True
    if is_generic_bella_text(response):
        return True
    if looks_like_unfounded_bella_activity(user_text, response):
        return True
    if _looks_too_polished_for_social(user_text, response):
        return True
    if is_status_acknowledgement(user_text) and any(
        marker in response
        for marker in (
            "e voce como ta",
            "e vc como ta",
            "como ta por ai",
            "como vc ta",
            "como voce ta",
            "voce como ta",
            "vc como ta",
        )
    ):
        return True
    if any(
        marker in user_text
        for marker in ("namora comigo", "trava", "travou", "ai sim", "aí sim")
    ) and is_status_acknowledgement(response):
        return True
    # Resposta curta é válida na persona ("sim", "boa", "vish"). Só barra a
    # resposta seca demais a um cumprimento, tipo "oi" -> "fala".
    words = response.split()
    if len(words) <= 3 and _is_casual_or_self(user_text) and not _has_bella_texture(response):
        return True
    return False


# Risadas de qualquer tamanho (kkk, KKKKKK, hihihi, hehe, ksksks, rsrs) e
# letras esticadas (nadaa, boaaa, aaaa) contam como voz da Bella.
_LAUGH_RE = re.compile(r"k{3,}|(?:hi){2,}|(?:he){2,}|(?:ks){2,}|(?:rs){2,}|([a-z])\1{2,}")
_SHORT_BELLA_WORDS = {
    "imagina", "nadaa", "nada", "sim", "nao", "obrigada", "obrigadinha", "ufa",
    "tranquilo", "relaxa", "fechou", "entendi", "talvez", "acho", "ue", "oxe",
    "nossa", "aff", "socorro", "para", "paraa", "calma", "sei", "pior",
}


def _has_bella_texture(response: str) -> bool:
    if any(marker in response for marker in BELLA_TEXTURE_MARKERS):
        return True
    if _LAUGH_RE.search(response):
        return True
    words = set(re.findall(r"\w+", response))
    return bool(words & _SHORT_BELLA_WORDS)


def _looks_too_polished_for_social(user_text: str, response: str) -> bool:
    if not _is_casual_or_self(user_text):
        return False
    return _is_polished_social_response(response)


def _is_casual_or_self(user_text: str) -> bool:
    # Palavra inteira: "oi" não pode casar dentro de "noiado" ou "coisa".
    return any(
        re.search(rf"(?<!\w){re.escape(marker)}(?!\w)", user_text)
        for marker in (
            "oi",
            "ola",
            "olá",
            "oie",
            "tudo bem",
            "tudo bom",
            "como vc",
            "como voce",
            "timida",
            "tímida",
            "fofa",
            "namora",
            "ta dificil",
            "tá difícil",
            "sabadou",
        )
    )


def _is_polished_social_response(response: str) -> bool:
    polished_markers = (
        "sou meio",
        "um pouquinho, mas",
        "adoro conversar",
        "me sinto a vontade",
        "me sinto à vontade",
        "estou bem",
        "esta tudo bem",
        "está tudo bem",
        "tudo tranquilo",
        "tudo otimo",
        "tudo ótimo",
        "tudo bem contigo",
        "tudo certo contigo",
        "o que manda",
        "manda ai o que precisar",
        "manda aí o que precisar",
        "to aqui",
        "tô aqui",
        "to por aqui",
        "tô por aqui",
        "fico feliz em",
        "obrigada por perguntar",
        "gosto de trocar",
        "trocar ideia aqui",
        "trocar ideias aqui",
    )
    if any(marker in response for marker in polished_markers):
        return True
    if len(response.split()) >= 8 and not _has_bella_texture(response):
        return True
    return False


def filter_history_for_bella(
    values: list[dict[str, str]] | tuple[dict[str, str], ...],
) -> list[dict[str, str]]:
    filtered: list[dict[str, str]] = []
    for item in values:
        author = str(item.get("author", item.get("role", "")))
        text = str(item.get("text", item.get("content", "")))
        if is_bella_author(author) and is_generic_bella_text(text):
            continue
        if str(item.get("role", "")).casefold() == "assistant" and is_generic_bella_text(text):
            continue
        filtered.append(dict(item))
    return filtered
