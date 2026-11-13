from __future__ import annotations

import re
import time
import uuid
import hashlib
from dataclasses import dataclass
from typing import Any

from website_guard.identity import OWNER_DISCORD_ID
from website_guard.state import StateStore

from .intent_router import normalize_text


@dataclass(frozen=True)
class PermanentMemoryReply:
    answer: str
    intent: str = "specialized"


class PermanentKnowledgeCenter:
    """Persistent owner-taught knowledge for Bella.

    This layer is intentionally local-first: it stores and retrieves facts before
    asking Gemini, reducing API usage while still allowing Gemini to turn the
    facts into natural responses when available.
    """

    KNOWLEDGE_SECTION = "jarvis_universal_knowledge"
    ORGANIZATION_SECTION = "jarvis_organizational_memory"

    def __init__(self, state: StateStore) -> None:
        self.state = state

    async def define_manual_knowledge(
        self,
        content: str,
        *,
        user_id: int,
        item_id: str | None = None,
    ) -> dict[str, str]:
        if user_id != OWNER_DISCORD_ID:
            raise PermissionError("Somente o proprietário pode definir conhecimentos.")
        text = _strip_memory_prefix(content.strip())
        if not text:
            raise ValueError("Informe o conhecimento que deseja salvar.")

        channel_knowledge = _parse_channel_knowledge(text)
        if channel_knowledge:
            if item_id:
                channel_knowledge["key"] = _safe_key(item_id)
            await self._upsert_knowledge(channel_knowledge)
            return {
                "id": channel_knowledge["key"],
                "type": "channel_rule",
                "title": channel_knowledge["category"],
            }

        organization = _parse_organization_instruction(text)
        if organization:
            if item_id:
                organization["key"] = _safe_key(item_id)
            await self._upsert_organization(organization)
            return {
                "id": organization["key"],
                "type": "organization",
                "title": organization["name"],
            }

        knowledge = _parse_universal_knowledge(text) or _generic_knowledge(text)
        if item_id:
            knowledge["key"] = _safe_key(item_id)
        await self._upsert_knowledge(knowledge)
        return {
            "id": knowledge["key"],
            "type": "knowledge",
            "title": knowledge["category"],
        }

    async def save_server_summary(
        self,
        *,
        guild_id: int,
        guild_name: str,
        summary: str,
        channel_count: int,
        message_count: int,
    ) -> str:
        """Persist the latest server-wide summary as searchable Bella memory."""
        text = " ".join(str(summary or "").split()).strip()
        if not text:
            raise ValueError("O resumo do servidor não pode ficar vazio.")
        safe_name = " ".join(str(guild_name or "servidor").split())[:80]
        key = f"server-summary:{int(guild_id)}"
        category = f"Resumo do servidor {safe_name}".strip()
        record = {
            "key": key,
            "category": category,
            "description": text[:6000],
            "links": [],
            "triggers": [
                "servidor",
                "comunidade",
                "canais",
                "resumo",
                "sobre o servidor",
                safe_name.casefold(),
            ],
            "source": "server_summary",
            "guild_id": int(guild_id),
            "channel_count": max(0, int(channel_count)),
            "message_count": max(0, int(message_count)),
            "raw": text[:6000],
        }
        await self._upsert_knowledge(record)
        return key

    async def list_manual_knowledge(self, limit: int = 25) -> list[dict[str, str]]:
        values: list[dict[str, str]] = []
        knowledge = await self.state.get_section(self.KNOWLEDGE_SECTION)
        organization = await self.state.get_section(self.ORGANIZATION_SECTION)
        for key, item in knowledge.items():
            if not isinstance(item, dict):
                continue
            values.append(
                {
                    "id": str(key),
                    "type": "knowledge",
                    "title": str(item.get("category", key)),
                    "summary": str(item.get("description", item.get("raw", "")))[:240],
                }
            )
        for key, item in organization.items():
            if not isinstance(item, dict):
                continue
            values.append(
                {
                    "id": str(key),
                    "type": "organization",
                    "title": str(item.get("name", key)),
                    "summary": str(item.get("role", item.get("raw", "")))[:240],
                }
            )
        return values[:limit]

    async def search_manual_knowledge(
        self,
        query: str,
        limit: int = 8,
    ) -> list[dict[str, str]]:
        payload = await self.context_payload(query)
        return [
            {
                "id": item.get("entry_id", ""),
                "type": item.get("type", ""),
                "title": item.get("title", ""),
                "summary": item.get("content", "")[:600],
                "url": item.get("url", ""),
            }
            for item in payload[:limit]
        ]

    async def remove_manual_knowledge(self, item_id: str, *, user_id: int) -> bool:
        if user_id != OWNER_DISCORD_ID:
            raise PermissionError("Somente o proprietário pode remover conhecimentos.")
        target = _safe_key(item_id)
        removed = False

        def remove_item(section: dict[str, Any]) -> None:
            nonlocal removed
            if target in section:
                section.pop(target, None)
                removed = True

        await self.state.mutate_section(self.KNOWLEDGE_SECTION, remove_item)
        await self.state.mutate_section(self.ORGANIZATION_SECTION, remove_item)
        return removed

    async def handle_owner_instruction(
        self,
        message: str,
        *,
        user_id: int,
    ) -> PermanentMemoryReply | None:
        if user_id != OWNER_DISCORD_ID:
            return None
        text = _strip_memory_prefix(message.strip())
        if not text:
            return None
        normalized = normalize_text(text)

        if _is_list_request(normalized):
            return PermanentMemoryReply(await self._list_memories(), "memory_list")

        removed = await self._maybe_remove(text, normalized)
        if removed:
            return PermanentMemoryReply(removed, "memory_delete")

        if _is_owner_query(text, normalized):
            return None

        channel_knowledge = _parse_channel_knowledge(text)
        if channel_knowledge:
            await self._upsert_knowledge(channel_knowledge)
            return PermanentMemoryReply(
                f"Fechado. Vou lembrar a finalidade e a regra do {channel_knowledge['category']}.",
                "channel_memory",
            )

        organization = _parse_organization_instruction(text)
        if organization:
            await self._upsert_organization(organization)
            return PermanentMemoryReply(
                f"Fechado. Vou lembrar que {organization['name']} atua como "
                f"{organization['role']}.",
                "organization_memory",
            )

        knowledge = _parse_universal_knowledge(text)
        if knowledge:
            await self._upsert_knowledge(knowledge)
            category = knowledge["category"]
            return PermanentMemoryReply(
                f"Perfeito. Vou usar essa informação quando alguém falar sobre "
                f"{category}.",
                "universal_knowledge",
            )

        return None

    async def context_payload(self, message: str) -> list[dict[str, str]]:
        entries: list[dict[str, str]] = []
        entries.extend(await self._universal_context(message))
        entries.extend(await self._organization_context(message))
        entries.extend(_cheetah_context(message))
        return entries[:8]

    async def local_reply(self, message: str) -> PermanentMemoryReply | None:
        payload = await self.context_payload(message)
        if not payload:
            return None
        primary = payload[0]
        source_type = primary.get("type", "")
        if source_type == "organization_memory":
            return PermanentMemoryReply(_organization_local_answer(primary))
        if source_type == "owner_memory":
            return PermanentMemoryReply(_knowledge_local_answer(primary))
        if source_type == "cheetah_institutional":
            return PermanentMemoryReply(_cheetah_local_answer(primary))
        return None

    async def direct_reply(self, message: str) -> PermanentMemoryReply | None:
        """Return a deterministic answer for strong taught-memory matches.

        Gemini still receives memory context for broad conversations, but direct
        person/knowledge questions should never miss facts explicitly taught by
        the owner.
        """

        organization = await self._organization_context(message)
        if organization:
            primary = organization[0]
            relevance = _payload_relevance(primary)
            if relevance >= 0.52:
                return PermanentMemoryReply(
                    _organization_local_answer(primary),
                    "organization_memory",
                )
        universal = await self._universal_context(message)
        if universal:
            primary = universal[0]
            relevance = _payload_relevance(primary)
            if relevance >= 0.58:
                if primary.get("source") == "server_summary":
                    return PermanentMemoryReply(
                        _server_summary_local_answer(primary),
                        "server_summary",
                    )
                return PermanentMemoryReply(
                    _knowledge_local_answer(primary),
                    "universal_knowledge",
                )
        return None

    async def _upsert_knowledge(self, knowledge: dict[str, Any]) -> None:
        key = knowledge["key"]
        now = int(time.time())

        def mutate(section: dict[str, Any]) -> None:
            existing = section.get(key, {})
            if not isinstance(existing, dict):
                existing = {}
            section[key] = {
                **existing,
                **knowledge,
                "created_at": existing.get("created_at", now),
                "updated_at": now,
            }

        await self.state.mutate_section(self.KNOWLEDGE_SECTION, mutate)

    async def _upsert_organization(self, record: dict[str, Any]) -> None:
        key = record["key"]
        now = int(time.time())

        def mutate(section: dict[str, Any]) -> None:
            existing = section.get(key, {})
            if not isinstance(existing, dict):
                existing = {}
            section[key] = {
                **existing,
                **record,
                "created_at": existing.get("created_at", now),
                "updated_at": now,
            }

        await self.state.mutate_section(self.ORGANIZATION_SECTION, mutate)

    async def _maybe_remove(self, text: str, normalized: str) -> str | None:
        if not any(term in normalized for term in ("remova", "remove", "esqueca", "esqueça", "apague")):
            return None
        if not any(term in normalized for term in ("conhecimento", "informacao", "informação", "registro", "cargo", "funcao", "função")):
            return None
        target = _remove_target(normalized)
        if not target:
            return None

        removed: list[str] = []

        def remove_from(section: dict[str, Any]) -> None:
            for key, value in list(section.items()):
                blob = normalize_text(" ".join(str(part) for part in _flatten(value)))
                if target in key or target in blob:
                    removed.append(str(value.get("category") or value.get("name") or key))
                    section.pop(key, None)

        await self.state.mutate_section(self.KNOWLEDGE_SECTION, remove_from)
        await self.state.mutate_section(self.ORGANIZATION_SECTION, remove_from)
        if not removed:
            return "Não encontrei uma memória permanente clara com esse nome."
        return f"Pronto. Removi a informação sobre {', '.join(removed[:3])}."

    async def _list_memories(self) -> str:
        knowledge = await self.state.get_section(self.KNOWLEDGE_SECTION)
        organization = await self.state.get_section(self.ORGANIZATION_SECTION)
        parts: list[str] = []
        if knowledge:
            items = [
                f"- {value.get('category', key)}"
                for key, value in knowledge.items()
                if isinstance(value, dict)
            ]
            if items:
                parts.append("Conhecimentos que estou guardando:\n" + "\n".join(items[:12]))
        if organization:
            items = [
                f"- {value.get('name', key)}: {value.get('role', 'função não definida')}"
                for key, value in organization.items()
                if isinstance(value, dict)
            ]
            if items:
                parts.append("Memória organizacional:\n" + "\n".join(items[:12]))
        return "\n\n".join(parts) if parts else "Ainda não tenho memórias permanentes registradas."

    async def _universal_context(self, message: str) -> list[dict[str, str]]:
        section = await self.state.get_section(self.KNOWLEDGE_SECTION)
        if not section:
            return []
        text = normalize_text(message)
        scored: list[tuple[float, dict[str, Any]]] = []
        for value in section.values():
            if not isinstance(value, dict):
                continue
            score = _knowledge_score(text, value)
            if score >= 0.36:
                scored.append((score, value))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [_knowledge_payload(value, score) for score, value in scored[:4]]

    async def _organization_context(self, message: str) -> list[dict[str, str]]:
        section = await self.state.get_section(self.ORGANIZATION_SECTION)
        if not section:
            return []
        text = normalize_text(message)
        looks_like_query = _looks_organization_query(text)
        scored: list[tuple[float, dict[str, Any]]] = []
        for value in section.values():
            if not isinstance(value, dict):
                continue
            if _looks_like_bogus_question_memory(value):
                continue
            score = _organization_score(text, value)
            threshold = 0.28 if looks_like_query else 0.52
            if score >= threshold:
                scored.append((score, value))
        if not scored and any(term in text for term in ("equipe", "time", "trabalha", "quem faz parte")):
            scored = [(0.5, value) for value in section.values() if isinstance(value, dict)]
        scored.sort(key=lambda item: item[0], reverse=True)
        return [_organization_payload(value, score) for score, value in scored[:6]]


def _parse_universal_knowledge(text: str) -> dict[str, Any] | None:
    normalized = normalize_text(text)
    if not _looks_like_knowledge_instruction(normalized):
        return None
    urls = _extract_urls(text)
    category = _category_from_text(normalized)
    description = _clean_description(text)
    if not category and not urls:
        return None
    if not category:
        category = _title_from_description(description)
    if not description:
        description = f"Informação oficial relacionada a {category}."
    key_base = normalize_text(category or description)[:80] or str(uuid.uuid4())
    first_url = urls[0] if urls else ""
    key = f"{key_base}:{normalize_text(first_url)}" if first_url else key_base
    return {
        "key": key,
        "category": category.title(),
        "description": description,
        "links": urls,
        "triggers": sorted(_triggers_for(category, description)),
        "raw": text,
    }


def _generic_knowledge(text: str) -> dict[str, Any]:
    urls = _extract_urls(text)
    category = _category_from_text(normalize_text(text)) or _title_from_description(text)
    key = f"manual:{hashlib_sha(text)}"
    return {
        "key": key,
        "category": category.title()[:80] or "Conhecimento",
        "description": text[:1200],
        "links": urls,
        "triggers": sorted(_triggers_for(category, text)),
        "raw": text,
    }


def _parse_channel_knowledge(text: str) -> dict[str, Any] | None:
    channel_match = re.search(r"<#(\d+)>", text)
    if not channel_match:
        return None
    normalized = normalize_text(text)
    if "canal" not in normalized:
        return None
    channel_id = channel_match.group(1)
    channel_mention = f"<#{channel_id}>"
    description = text.strip()
    purpose_match = re.search(
        r"(?:no\s+)?canal\s+<#[0-9]+>\s+(?:é|e|eh)\s+(.+)$",
        description,
        flags=re.I,
    )
    if purpose_match:
        description = f"O canal {channel_mention} {purpose_match.group(1).strip(' .')}"
    triggers = _triggers_for("Canal", description)
    triggers.update(
        {
            "canal",
            "funcao",
            "função",
            "finalidade",
            "duvida",
            "duvidas",
            "dúvida",
            "dúvidas",
            channel_id,
            channel_mention,
        }
    )
    return {
        "key": f"channel:{channel_id}",
        "category": f"Canal {channel_mention}",
        "description": description[:1200],
        "links": [],
        "triggers": sorted(triggers),
        "channel_id": channel_id,
        "raw": text,
    }


def _strip_memory_prefix(text: str) -> str:
    cleaned = re.sub(r"^\s*jarvis\s*[,:\-]?\s*", "", text.strip(), flags=re.I)
    return re.sub(
        r"^\s*<@!?\d+>\s+(?=(?:a|o|esse|essa|este|esta|isso|isto|quando|quem|qual|quais|onde|como|o que|oq|sabe|lembra|no canal)\b)",
        "",
        cleaned,
        flags=re.I,
    ).strip()


def _parse_organization_instruction(text: str) -> dict[str, Any] | None:
    cleaned = re.sub(r"^\s*(?:jarvis[,\s:]+)?", "", text.strip(), flags=re.I)
    normalized = normalize_text(cleaned)
    if normalized.startswith(("esse ", "essa ", "este ", "esta ", "isso ", "isto ", "quando alguem")):
        return None
    if _is_owner_query(cleaned, normalized):
        return None
    if not re.search(r"\b(?:e|eh|agora e)\b", normalized):
        return None

    mention_match = re.search(
        r"^(?:a|o)?\s*<@!?(\d+)>\s+(?:agora\s+)?(?:e|eh|\S{1,3})\s+(.+)$",
        cleaned,
        flags=re.I,
    )
    if mention_match:
        discord_id = mention_match.group(1)
        name = f"<@{discord_id}>"
        role = mention_match.group(2).strip(" .")
    else:
        match = re.match(
            r"^(?:a|o)?\s*([^\s,.:;!?][^,.:;!?]{0,80}?)\s+"
            r"(?:agora\s+)?(?:e|eh|\S{1,3})\s+(.+)$",
            cleaned,
            flags=re.I,
        )
        if not match:
            return None
        name = match.group(1).strip()
        role = match.group(2).strip(" .")
        discord_id = ""
    if not name or not role or len(role) < 3:
        return None
    if normalize_text(name) in {"jarvis", "talisson", "esse link", "essa pagina", "esse formulario", "quem"}:
        return None
    company = _company_from_text(role)
    project = _project_from_text(role)
    responsibilities = _responsibilities_from_text(role)
    aliases = _aliases_for_person(name, discord_id, cleaned)
    return {
        "key": normalize_text(name),
        "name": name,
        "discord_id": discord_id,
        "aliases": aliases,
        "role": role,
        "function": role,
        "responsibilities": responsibilities,
        "company": company,
        "project": project,
        "raw": cleaned,
    }


def _looks_like_knowledge_instruction(normalized: str) -> bool:
    markers = (
        "esse link e para",
        "esse link ? para",
        "este link e para",
        "este link ? para",
        "esse formulario e para",
        "esse formulario ? para",
        "este formulario e para",
        "este formulario ? para",
        "essa pagina e para",
        "essa pagina ? para",
        "esta pagina e para",
        "esta pagina ? para",
        "esse site e para",
        "esse site ? para",
        "quando alguem",
        "quando alguém",
        "envie esse link",
        "mande esse link",
        "use esse link",
        "direcione para",
    )
    return any(marker in normalized for marker in markers)


def _extract_urls(text: str) -> list[str]:
    return [url.rstrip(").,;>") for url in re.findall(r"https?://[^\s<>()]+", text)]


def _category_from_text(normalized: str) -> str:
    mapping = {
        "Orçamento": ("orcamento", "orçamento", "or?amento", "contratar", "proposta", "cotacao", "cotação", "cota?ao"),
        "Feedback": ("feedback", "avaliar", "avaliacao", "avaliação", "avalia?ao", "sugestao", "sugestão", "sugest?o"),
        "Suporte": ("suporte", "ajuda", "problema", "bug"),
        "Parceria": ("parceria", "parceiro", "colaboracao", "colaboração"),
        "Trabalho": ("trabalhar", "vaga", "emprego", "empresa", "curriculo", "currículo"),
        "Contato": ("contato", "falar", "chamar", "atendimento"),
        "Reunião": ("reuniao", "reunião", "agenda", "agendamento"),
    }
    for category, terms in mapping.items():
        if any(term in normalized for term in terms):
            return category
    para = re.search(r"\bpara\s+(.+?)(?:https?://|$)", normalized)
    if para:
        words = para.group(1).strip(" .:;")[:60]
        if words:
            return words
    return ""


def _clean_description(text: str) -> str:
    cleaned = re.sub(r"https?://[^\s<>()]+", "", text).strip()
    cleaned = re.sub(
        r"^(?:esse|este|essa|esta)\s+(?:link|formulário|formulario|página|pagina|site)\s+(?:é|e|eh)\s+",
        "",
        cleaned,
        flags=re.I,
    ).strip(" :.-")
    return cleaned[:420]


def _title_from_description(description: str) -> str:
    words = normalize_text(description).split()
    useful = [word for word in words if word not in {"para", "pessoas", "que", "desejam", "querem", "alguem", "alguém"}]
    return " ".join(useful[:4]).title() or "Conhecimento"


def _safe_key(value: str) -> str:
    key = normalize_text(value).replace(" ", "-")
    key = re.sub(r"[^a-z0-9:_-]", "", key)
    return key[:120] or f"manual:{hashlib_sha(value)}"


def hashlib_sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _triggers_for(category: str, description: str) -> set[str]:
    normalized = normalize_text(f"{category} {description}")
    triggers = {word for word in normalized.split() if len(word) >= 4}
    if "orcamento" in normalized or "orçamento" in normalized:
        triggers.update({"orcamento", "orçamento", "contratar", "proposta", "cotacao", "cotação"})
    if "feedback" in normalized:
        triggers.update({"feedback", "avaliar", "sugestao", "sugestão"})
    if "parceria" in normalized:
        triggers.update({"parceria", "proposta", "comercial"})
    if "trabalh" in normalized or "vaga" in normalized:
        triggers.update({"trabalhar", "vaga", "emprego", "curriculo", "currículo"})
    return triggers


def _knowledge_score(text: str, value: dict[str, Any]) -> float:
    blob = normalize_text(
        " ".join(
            [
                str(value.get("category", "")),
                str(value.get("description", "")),
                " ".join(str(item) for item in value.get("triggers", [])),
            ]
        )
    )
    score = 0.0
    for token in set(text.split()):
        if len(token) >= 4 and token in blob:
            score += 0.18
    category = normalize_text(str(value.get("category", "")))
    if category and category in text:
        score += 0.5
    channel_id = str(value.get("channel_id", "")).strip()
    if channel_id and (channel_id in text or f"<#{channel_id}>" in text):
        score += 0.9
    if "canal" in text and "canal" in blob:
        score += 0.22
    if any(term in text for term in ("funcao", "fun??o", "finalidade", "serve")) and any(
        term in blob for term in ("canal", "funcao", "finalidade", "somente", "apagar")
    ):
        score += 0.28
    if any(term in text for term in ("orcamento", "orçamento", "or?amento", "contratar", "proposta")) and "orcamento" in blob:
        score += 0.55
    if any(term in text for term in ("feedback", "sugestao", "sugestão", "avaliar")) and "feedback" in blob:
        score += 0.55
    if value.get("source") == "server_summary" and any(
        term in text
        for term in (
            "servidor",
            "comunidade",
            "canais",
            "sobre esse servidor",
            "sobre o servidor",
            "o que tem aqui",
        )
    ):
        score += 0.62
    return min(score, 1.0)


def _knowledge_payload(value: dict[str, Any], score: float) -> dict[str, str]:
    links = [str(item) for item in value.get("links", []) if str(item)]
    return {
        "entry_id": f"owner-memory-{value.get('key', 'item')}",
        "question": f"Conhecimento permanente: {value.get('category', 'Informação')}",
        "title": str(value.get("category", "Memória permanente")),
        "type": "owner_memory",
        "source": str(value.get("source", "owner_memory")),
        "url": links[0] if links else "",
        "content": (
            f"Categoria: {value.get('category', '')}\n"
            f"Descrição: {value.get('description', '')}\n"
            f"Links oficiais: {', '.join(links) if links else 'nenhum'}\n"
            f"Relevância estimada: {score:.2f}"
        ),
    }


def _looks_organization_query(text: str) -> bool:
    return any(
        term in text
        for term in (
            "quem e",
            "quem é",
            "quem ?",
            "qual o cargo",
            "qual cargo",
            "qual ? cargo",
            "funcao",
            "função",
            "o que faz",
            "responsavel",
            "responsável",
            "equipe",
            "time",
            "trabalha",
            "cuida",
            "fala de",
            "fale de",
            "me fala da",
            "me fala do",
        )
    )


def _organization_score(text: str, value: dict[str, Any]) -> float:
    blob = normalize_text(" ".join(str(part) for part in _flatten(value)))
    score = 0.0
    name = normalize_text(str(value.get("name", "")))
    discord_id = str(value.get("discord_id", "")).strip()
    aliases = [
        normalize_text(str(alias))
        for alias in value.get("aliases", [])
        if str(alias).strip()
    ]
    if name and name in text:
        score += 0.72
    if name:
        first_name = name.split()[0]
        if len(first_name) >= 3 and re.search(rf"\b{re.escape(first_name)}\b", text):
            score += 0.62
    for alias in aliases:
        if alias and alias in text:
            score += 0.68
        elif alias and len(alias) >= 3 and re.search(rf"\b{re.escape(alias)}\b", text):
            score += 0.54
    if discord_id and (
        discord_id in text
        or f"<@{discord_id}>" in text
        or f"<@!{discord_id}>" in text
    ):
        score += 0.85
    for token in set(text.split()):
        if len(token) >= 4 and token in blob:
            score += 0.13
    return min(score, 1.0)


def _looks_like_bogus_question_memory(value: dict[str, Any]) -> bool:
    name = normalize_text(str(value.get("name", ""))).strip(" ?!.,")
    role = normalize_text(str(value.get("role", ""))).strip(" ?!.,")
    raw = str(value.get("raw", ""))
    if "?" in raw:
        return True
    question_prefixes = (
        "quem",
        "qual",
        "quais",
        "sabe",
        "lembra",
        "voce sabe",
        "você sabe",
        "onde",
        "como",
    )
    return name.startswith(question_prefixes) or role.startswith(question_prefixes)


def _organization_payload(value: dict[str, Any], score: float) -> dict[str, str]:
    return {
        "entry_id": f"organization-{value.get('key', 'person')}",
        "question": f"Quem é {value.get('name', 'essa pessoa')}?",
        "title": str(value.get("name", "Memória organizacional")),
        "type": "organization_memory",
        "url": "",
        "content": (
            f"Nome: {value.get('name', '')}\n"
            f"Discord ID: {value.get('discord_id', '')}\n"
            f"Apelidos/Aliases: {', '.join(str(item) for item in value.get('aliases', []) if str(item))}\n"
            f"Cargo/Função: {value.get('role', '')}\n"
            f"Responsabilidades: {value.get('responsibilities', '')}\n"
            f"Empresa: {value.get('company', '')}\n"
            f"Projeto: {value.get('project', '')}\n"
            f"Relevância estimada: {score:.2f}"
        ),
    }


def _payload_relevance(payload: dict[str, str]) -> float:
    content = payload.get("content", "")
    match = re.search(r"Relevância estimada:\s*([0-9.]+)", content)
    if not match:
        return 0.0
    try:
        return float(match.group(1))
    except ValueError:
        return 0.0


def _cheetah_context(message: str) -> list[dict[str, str]]:
    text = normalize_text(message)
    cheetah_related = any(
        term in text
        for term in (
            "cheetah",
            "cheeta",
            "chita",
            "ops framework",
            "framework ops",
            "operational performance",
            "solutions network",
        )
    )
    budget_related = any(term in text for term in ("orcamento", "orçamento", "contratar", "proposta"))
    if not cheetah_related and not budget_related:
        return []
    entries = [
        {
            "entry_id": "cheetah-company",
            "question": "Quem é a Cheetah Solutions Network?",
            "title": "Cheetah Solutions Network",
            "type": "cheetah_institutional",
            "url": "https://www.cheetahsolutions.com.br/",
            "content": (
                "A Cheetah Solutions Network é uma empresa de engenharia de software "
                "orientada à performance operacional. Ela estrutura operações empresariais "
                "por meio de arquitetura tecnológica sob medida, automação, sistemas internos, "
                "dashboards estratégicos, integrações e infraestrutura escalável. Tese central: "
                "muitos negócios não travam por falta de mercado, mas por falta de estrutura."
            ),
        }
    ]
    if any(term in text for term in ("ops", "framework", "metodologia")):
        entries.insert(
            0,
            {
                "entry_id": "cheetah-ops-framework",
                "question": "O que é a Cheetah OPS Framework?",
                "title": "Cheetah OPS Framework",
                "type": "cheetah_institutional",
                "url": "https://www.cheetahsolutions.com.br/cheetah-ops",
                "content": (
                    "A Cheetah OPS Framework é a metodologia proprietária da Cheetah "
                    "para diagnosticar gargalos, projetar arquitetura organizacional, "
                    "estruturar processos, implementar sistemas alinhados à operação e "
                    "sustentar crescimento. Fases: Diagnóstico Operacional, Arquitetura "
                    "Estrutural, Implementação Controlada, Estabilização e Evolução."
                ),
            },
        )
    if any(term in text for term in ("servico", "serviço", "oferece", "faz")):
        entries.append(
            {
                "entry_id": "cheetah-services",
                "question": "Quais serviços a Cheetah oferece?",
                "title": "Serviços Cheetah",
                "type": "cheetah_institutional",
                "url": "https://www.cheetahsolutions.com.br/servicos",
                "content": "Serviços: engenharia de software, arquitetura operacional, automação, integrações, sistemas internos personalizados, dashboards e infraestrutura escalável.",
            }
        )
    if budget_related:
        entries.insert(
            0,
            {
                "entry_id": "cheetah-budget",
                "question": "Como pedir orçamento na Cheetah?",
                "title": "Orçamento Cheetah",
                "type": "cheetah_institutional",
                "url": "https://www.cheetahsolutions.com.br/orcamento",
                "content": "Para solicitar orçamento ou proposta, utilize a página oficial de orçamento da Cheetah Solutions Network.",
            },
        )
    if any(term in text for term in ("contato", "atendimento", "falar")):
        entries.append(
            {
                "entry_id": "cheetah-contact",
                "question": "Como entrar em contato com a Cheetah?",
                "title": "Atendimento Cheetah",
                "type": "cheetah_institutional",
                "url": "https://www.cheetahsolutions.com.br/atendimento",
                "content": "Canal oficial para atendimento e contato com a Cheetah Solutions Network.",
            }
        )
    if any(term in text for term in ("contrata", "vaga", "trabalhar", "emprego")):
        entries.append(
            {
                "entry_id": "cheetah-careers",
                "question": "A Cheetah contrata?",
                "title": "Inscrições Cheetah",
                "type": "cheetah_institutional",
                "url": "https://www.cheetahsolutions.com.br/inscricoes",
                "content": "Para oportunidades e banco de talentos: https://www.cheetahsolutions.com.br/inscricoes e https://forms.gle/Kaiw5TGKfPwgK4C69.",
            }
        )
    return entries[:4]


def _knowledge_local_answer(entry: dict[str, str]) -> str:
    content = entry.get("content", "")
    url = entry.get("url", "")
    category = entry.get("title", "esse assunto")
    description = _extract_content_line(content, "Descrição") or f"informações sobre {category}"
    if url:
        return (
            f"Para {category.lower()}, use este recurso oficial. "
            f"Ele ajuda a direcionar a solicitação com mais clareza:\n\n{url}"
        )
    return f"Sobre {category.lower()}: {description}"


def _server_summary_local_answer(entry: dict[str, str]) -> str:
    content = entry.get("content", "")
    description = _extract_content_line(content, "Descrição")
    return description or "Ainda não consegui montar um resumo útil desse servidor."


def _organization_local_answer(entry: dict[str, str]) -> str:
    content = entry.get("content", "")
    name = _extract_content_line(content, "Nome") or entry.get("title", "Essa pessoa")
    role = _extract_content_line(content, "Cargo/Função") or "faz parte da equipe"
    company = _extract_content_line(content, "Empresa")
    company_part = f" na {company}" if company else ""
    return f"{name} atua como {role}{company_part}."


def _cheetah_local_answer(entry: dict[str, str]) -> str:
    title = entry.get("title", "Cheetah Solutions Network")
    content = entry.get("content", "")
    url = entry.get("url", "")
    return f"{title}: {content}\n\nSaiba mais: {url}" if url else f"{title}: {content}"


def _extract_content_line(content: str, label: str) -> str:
    for line in content.splitlines():
        if line.startswith(f"{label}:"):
            return line.split(":", 1)[1].strip()
    return ""


def _company_from_text(text: str) -> str:
    normalized = normalize_text(text)
    if "cheetah" in normalized:
        return "Cheetah Solutions Network"
    if "werneck" in normalized:
        return "Werneck Group"
    return ""


def _project_from_text(text: str) -> str:
    match = re.search(r"\bprojeto\s+([A-Za-zÀ-ÿ0-9 _.-]{2,60})", text, flags=re.I)
    return match.group(1).strip(" .") if match else ""


def _responsibilities_from_text(text: str) -> str:
    match = re.search(r"(?:respons[aá]vel por|cuida de|coordena)\s+(.+)", text, flags=re.I)
    return match.group(1).strip(" .") if match else text.strip(" .")


def _aliases_for_person(name: str, discord_id: str, raw_text: str) -> list[str]:
    aliases: list[str] = []
    clean_name = name.strip()
    if clean_name and not clean_name.startswith("<@"):
        aliases.append(clean_name)
        first = clean_name.split()[0]
        if first and first.casefold() != clean_name.casefold():
            aliases.append(first)
    if discord_id:
        aliases.extend([discord_id, f"<@{discord_id}>", f"<@!{discord_id}>"])
    quoted = re.findall(r"[\"'“”‘’]([^\"'“”‘’]{2,40})[\"'“”‘’]", raw_text)
    aliases.extend(item.strip() for item in quoted if item.strip())
    seen: set[str] = set()
    result: list[str] = []
    for alias in aliases:
        key = normalize_text(alias)
        if key and key not in seen:
            seen.add(key)
            result.append(alias)
    return result


def _remove_target(normalized: str) -> str:
    match = re.search(r"(?:sobre|da|do|de)\s+(.+)$", normalized)
    if match:
        return match.group(1).strip(" .")
    words = normalized.split()
    return words[-1] if words else ""


def _is_owner_query(text: str, normalized: str) -> bool:
    compact = normalized.strip(" ?!.,")
    if "?" in text:
        return True
    return compact.startswith(
        (
            "quem ",
            "quem e ",
            "quem eh ",
            "qual ",
            "quais ",
            "onde ",
            "como ",
            "o que ",
            "oq ",
            "quando ",
            "por que ",
            "porque ",
            "me diga quem ",
            "sabe quem ",
            "sabe qual ",
            "voce sabe ",
            "você sabe ",
            "lembra quem ",
            "lembra qual ",
        )
    )


def _is_list_request(normalized: str) -> bool:
    return any(
        phrase in normalized
        for phrase in (
            "liste todas as regras",
            "liste as memorias",
            "listar memorias",
            "quais diretrizes",
            "mostre suas regras",
            "mostre seus conhecimentos",
            "quais conhecimentos",
        )
    )


def _flatten(value: Any) -> list[str]:
    if isinstance(value, dict):
        result: list[str] = []
        for item in value.values():
            result.extend(_flatten(item))
        return result
    if isinstance(value, (list, tuple, set)):
        result = []
        for item in value:
            result.extend(_flatten(item))
        return result
    return [str(value)]
