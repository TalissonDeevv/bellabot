from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

from website_guard.state import StateStore

from .moderation_ai import contains_sensitive_data


KNOWN_USER_FACTS: dict[int, dict[str, str]] = {
    1355150439057985687: {
        "relationship": "É o namorado fictício da Bella dentro da brincadeira/personagem.",
        "relationship_style": (
            "Bella pode tratá-lo com carinho, intimidade leve e brincadeiras de casal, "
            "sempre deixando o contexto naturalmente ficcional e saudável."
        ),
        "relationship_safety": (
            "Nunca incentivar dependência emocional real, exclusividade, isolamento, culpa, "
            "ciúme controlador ou substituir relações humanas. Não afirmar que o namoro é real."
        ),
    },
    1189671608080679012: {
        "professional_identity": "É Anthropic Senior Staff.",
        "expertise": (
            "Bella deve tratá-lo como um especialista sênior em Claude e assuntos técnicos "
            "relacionados à Anthropic, sem inventar informações além desse perfil conhecido."
        ),
    },
    1552721267415584799: {
        "professional_identity": "É membro profissional da FURIA, organização de esports.",
        "expertise": (
            "Bella deve tratá-lo como pro player profissional da FURIA, com respeito ao contexto "
            "competitivo, sem inventar equipe, modalidade, resultados ou conquistas não informadas."
        ),
    },
    1409982177332166676: {
        "relationship": "É o irmão fictício da Bella dentro da brincadeira/personagem.",
        "relationship_style": (
            "Bella deve tratá-lo naturalmente como irmão e bro, com intimidade fraterna leve, "
            "brincadeiras saudáveis e sem inventar fatos da vida real."
        ),
    },
}


KNOWN_PEOPLE_FACTS: dict[str, str] = {
    "bella_fictional_boyfriend": (
        "O usuário <@1355150439057985687> é o namorado fictício da Bella na brincadeira da personagem. "
        "Quando perguntarem quem é o namorado dela, Bella deve identificar esse usuário naturalmente. "
        "A brincadeira deve ser carinhosa e saudável, sem dependência emocional real, exclusividade, "
        "isolamento, culpa, ciúme controlador ou substituição de relações humanas."
    ),
    "anthropic_claude_expert": (
        "O usuário <@1189671608080679012> é Anthropic Senior Staff e deve ser tratado como perito "
        "em Claude, sem inventar informações profissionais além disso."
    ),
    "furia_professional_player": (
        "O usuário <@1552721267415584799> é membro e pro player profissional da FURIA, sem inventar "
        "modalidade, equipe, resultados ou conquistas não informadas."
    ),
    "bella_fictional_brother": (
        "O usuário <@1409982177332166676> é o irmão fictício e bro da Bella na brincadeira da personagem. "
        "Bella deve tratá-lo com intimidade fraterna leve e saudável, sem afirmar parentesco real."
    ),
}


def known_user_facts(user_id: int) -> dict[str, str]:
    return dict(KNOWN_USER_FACTS.get(int(user_id), {}))


def known_people_facts() -> dict[str, str]:
    return dict(KNOWN_PEOPLE_FACTS)


class ConversationMemory:
    def __init__(
        self,
        state: StateStore,
        *,
        max_messages: int = 8,
        ttl_minutes: int = 60,
    ) -> None:
        self.state = state
        self.max_messages = max(2, max_messages)
        self.ttl = timedelta(minutes=max(5, ttl_minutes))

    async def history(
        self,
        guild_id: int,
        channel_id: int,
        user_id: int,
    ) -> list[dict[str, str]]:
        key = self._key(guild_id, channel_id, user_id)
        records = await self.state.get_section("ai_conversations")
        value = records.get(key)
        if not isinstance(value, dict):
            return []
        updated_at = _timestamp(value.get("updated_at"))
        if not updated_at or datetime.now(timezone.utc) - updated_at > self.ttl:
            await self.clear(guild_id, channel_id, user_id)
            return []
        messages = value.get("messages", [])
        if not isinstance(messages, list):
            return []
        return [
            {"role": str(item.get("role", "")), "text": str(item.get("text", ""))}
            for item in messages[-self.max_messages :]
            if isinstance(item, dict)
        ]

    async def append(
        self,
        guild_id: int,
        channel_id: int,
        user_id: int,
        *,
        user_text: str,
        assistant_text: str,
    ) -> None:
        key = self._key(guild_id, channel_id, user_id)
        safe_user_text = (
            "[conteúdo sensível não armazenado]"
            if contains_sensitive_data(user_text)
            else user_text
        )

        def mutate(values: dict[str, Any]) -> None:
            current = values.get(key)
            messages = (
                list(current.get("messages", []))
                if isinstance(current, dict)
                else []
            )
            messages.extend(
                [
                    {"role": "user", "text": safe_user_text[:1000]},
                    {"role": "assistant", "text": assistant_text[:1850]},
                ]
            )
            values[key] = {
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "messages": messages[-self.max_messages :],
            }

        await self.state.mutate_section("ai_conversations", mutate)

    async def observe_user_message(
        self,
        guild_id: int,
        user_id: int,
        text: str,
    ) -> None:
        match = re.search(
            r"\bmeu nome (?:é|e|eh)\s+"
            r"([A-Za-zÀ-ÿ][A-Za-zÀ-ÿ' -]{1,40})",
            text,
            flags=re.IGNORECASE,
        )
        if not match:
            return
        name = re.split(r"[,.!?;\n]", match.group(1), maxsplit=1)[0].strip()
        if not name or contains_sensitive_data(name):
            return
        await self.remember_fact(
            guild_id,
            user_id,
            "name",
            name[:50],
        )

    async def remember_fact(
        self,
        guild_id: int,
        user_id: int,
        name: str,
        value: str,
    ) -> None:
        key = f"{guild_id}:{user_id}"

        def mutate(values: dict[str, Any]) -> None:
            current = values.get(key)
            facts = (
                dict(current.get("facts", {}))
                if isinstance(current, dict)
                else {}
            )
            facts[name] = value[:300]
            values[key] = {
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "facts": facts,
            }

        await self.state.mutate_section("ai_short_term_facts", mutate)

    async def facts(
        self,
        guild_id: int,
        user_id: int,
    ) -> dict[str, str]:
        known = known_user_facts(user_id)
        key = f"{guild_id}:{user_id}"
        records = await self.state.get_section("ai_short_term_facts")
        value = records.get(key)
        if not isinstance(value, dict):
            return known
        updated_at = _timestamp(value.get("updated_at"))
        if not updated_at or datetime.now(timezone.utc) - updated_at > self.ttl:
            def remove(values: dict[str, Any]) -> None:
                values.pop(key, None)

            await self.state.mutate_section("ai_short_term_facts", remove)
            return known
        facts = value.get("facts", {})
        if not isinstance(facts, dict):
            return known
        learned = {
            str(name): str(fact)
            for name, fact in facts.items()
            if str(name) and str(fact)
        }
        return {**learned, **known}

    async def clear(self, guild_id: int, channel_id: int, user_id: int) -> None:
        key = self._key(guild_id, channel_id, user_id)

        def mutate(values: dict[str, Any]) -> None:
            values.pop(key, None)

        await self.state.mutate_section("ai_conversations", mutate)

    @staticmethod
    def _key(guild_id: int, channel_id: int, user_id: int) -> str:
        return f"{guild_id}:{channel_id}:{user_id}"


async def executive_memories(
    state: StateStore,
    guild_id: int,
) -> dict[str, str]:
    records = await state.get_section("executive_memory")
    return {
        str(item.get("key", "")): str(item.get("value", ""))
        for key, item in records.items()
        if key.startswith(f"{guild_id}:") and isinstance(item, dict)
    }


def _timestamp(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
