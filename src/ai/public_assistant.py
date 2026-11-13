from __future__ import annotations

import os
import re
from dataclasses import dataclass, replace

from website_guard.config import Settings
from website_guard.knowledge import KnowledgeBase
from website_guard.state import StateStore

from .bella_voice_guard import (
    is_generic_bella_response,
)
from .bella_ai_handler import BellaAIHandler
from website_guard.discord_entities import entities_from_dicts, render_mentions

from .complexity import (
    assess_generation_mode,
    is_playful_tease,
    looks_like_code_request,
    looks_like_task_request,
    task_generation_mode,
)
from .context import AIContextBuilder
from .groq_service import GroqService, MediaInput
from .intent_router import IntentDecision, IntentRouter, normalize_text
from .jarvis_music_memory import BellaMusicMemory
from .local_conversation import LocalConversationEngine, known_people_direct_reply
from .memory import ConversationMemory
from .moderation_ai import sanitize_output
from .personality import BELLA_SYSTEM_PROMPT
from .freellmapi_client import bella_ai_provider
from .personality_engine import CommunityPersonalityEngine, ConversationStyle
from .permanent_memory import PermanentKnowledgeCenter
from .prompts import fast_public_text_prompt, public_response_prompt
from .server_knowledge import DiscordKnowledgeIndex
from .social_intelligence import assess_social_intent


@dataclass(frozen=True)
class PublicReply:
    should_reply: bool
    answer: str
    intent: str
    confidence: float
    site_related: bool
    source: str = "local"
    conversation_context: str = "conversation"
    staggered: bool = False
    # Entidades do Discord vindas de memórias usadas na resposta (menções nativas).
    entities: tuple = ()


class PublicAssistant:
    def __init__(
        self,
        service: GroqService,
        settings: Settings,
        state: StateStore,
        knowledge: KnowledgeBase,
        server_knowledge: DiscordKnowledgeIndex | None = None,
    ) -> None:
        self.service = service
        self.settings = settings
        self.state = state
        self.memory = ConversationMemory(
            state,
            max_messages=settings.ai_context_messages,
            ttl_minutes=settings.ai_context_ttl_minutes,
        )
        self.server_knowledge = server_knowledge or DiscordKnowledgeIndex(
            state,
            enabled=False,
        )
        self.router = IntentRouter()
        self.personality = CommunityPersonalityEngine()
        self.local = LocalConversationEngine(
            knowledge,
            self.server_knowledge,
            self.memory,
            state,
            max_response_chars=settings.ai_max_response_chars,
        )
        self.permanent = PermanentKnowledgeCenter(state)
        self.music_memory = BellaMusicMemory(state)
        self.bella_handler = BellaAIHandler(service, state)
        # Entidades das memórias usadas, por conversa (servidor, canal, usuário):
        # chave por conversa evita misturar respostas simultâneas.
        self._pending_entities: dict[tuple, tuple] = {}
        self.context = AIContextBuilder(
            knowledge,
            self.memory,
            self.server_knowledge,
        )

    def classify(self, message: str) -> IntentDecision:
        return self.router.classify(message)

    def can_generate_ai(self) -> bool:
        groq_ready = self.service.available and getattr(
            self.service,
            "has_ready_model",
            lambda: True,
        )()
        return bool(self.bella_handler.available or (groq_ready and _allow_groq_public_fallback()))

    async def reply(
        self,
        message: str,
        *,
        display_name: str,
        guild_id: int,
        channel_id: int,
        user_id: int,
        recent_context: list[dict[str, str]] | None = None,
        media: list[MediaInput] | None = None,
        attachment_context: list[str] | None = None,
        operational_rules: list[str] | None = None,
        humor_allowed: bool = True,
    ) -> PublicReply:
        if int(user_id or 0) == int(getattr(self.settings, "talisson_user_id", 0) or 0):
            # Quem fala é o próprio Talisson: a Bella fala COM ele, não sobre ele.
            display_name = (
                f"{display_name} (é o próprio Talisson, seu criador; responda direto a ele, "
                "em segunda pessoa, nunca 'o Talisson pode confirmar')"
            )
        decision = self.router.classify(message)
        generation = assess_generation_mode(
            message,
            humor_allowed=humor_allowed,
        )
        recent = list(recent_context or ())[-20:]
        if looks_like_code_request(message) or _follows_code_request(message, recent):
            # A Bella não escreve código no chat: avisa direto, sem gastar IA.
            await self.memory.observe_user_message(guild_id, user_id, message)
            return PublicReply(
                should_reply=True,
                answer=_code_refusal(message),
                intent="conversation",
                confidence=1.0,
                site_related=False,
                source="code_policy",
            )
        if generation.social and await self._matches_owner_memory(
            message, guild_id=guild_id, user_id=user_id
        ):
            # "quero comprar o livro" parece papo curto, mas o Talisson ensinou a
            # resposta: o modo conversa não consulta a memória, este sim.
            generation = replace(
                task_generation_mode(),
                instruction=(
                    "Responda usando a memória que o Talisson ensinou (na base "
                    "especializada), com os valores, links e canais exatamente como "
                    "estão. Curto e direto."
                ),
                max_output_tokens=400,
                timeout_seconds=6.0,
                task=False,
            )
        if not generation.task and _continues_pending_task(message, recent):
            # "python", "quero", "um bot simples de py" respondendo a uma pergunta
            # da Bella ou logo após um pedido: é continuação da tarefa, não papo.
            generation = task_generation_mode()
        style = self.personality.analyze(
            message,
            recent_context=recent,
            humor_allowed=humor_allowed,
        )
        await self.memory.observe_user_message(guild_id, user_id, message)
        if decision.administrative:
            return PublicReply(
                False,
                "",
                decision.name,
                decision.confidence,
                False,
                "router",
            )
        known_person_answer = known_people_direct_reply(message)
        if known_person_answer:
            return await self._finalize(
                message,
                known_person_answer,
                "identity",
                guild_id=guild_id,
                channel_id=channel_id,
                user_id=user_id,
                confidence=1.0,
                source="known_people",
                max_chars=500,
                style=style,
            )
        social_assessment = assess_social_intent(message, recent)
        if not humor_allowed and (
            social_assessment.playful or social_assessment.deescalate
        ):
            return await self._finalize(
                message,
                _neutral_social_reply(
                    message,
                    conflict=social_assessment.deescalate,
                ),
                "conversation",
                guild_id=guild_id,
                channel_id=channel_id,
                user_id=user_id,
                confidence=0.90,
                source="local_channel_policy",
                max_chars=800,
                style=style,
            )
        groq_ready = self.service.available and getattr(
            self.service,
            "has_ready_model",
            lambda: True,
        )()
        allow_groq = _allow_groq_public_fallback()
        if not (groq_ready and allow_groq) and not self.bella_handler.available:
            direct_memory = await self.permanent.direct_reply(message)
            if direct_memory is not None:
                return await self._finalize(
                    message,
                    direct_memory.answer,
                    direct_memory.intent,
                    guild_id=guild_id,
                    channel_id=channel_id,
                    user_id=user_id,
                    confidence=0.95,
                    site_related=True,
                    source="permanent_memory_direct",
                    max_chars=max(2600, self.settings.ai_max_response_chars),
                    style=style,
                )
            if generation.social:
                return await self._fallback(
                    decision,
                    message,
                    recent,
                    guild_id,
                    channel_id,
                    user_id,
                    humor_allowed=humor_allowed,
                    style=style,
                )
            return await self._fallback(
                decision,
                message,
                recent,
                guild_id,
                channel_id,
                user_id,
                humor_allowed=humor_allowed,
                style=style,
            )

        context = await self.context.public(
            message,
            guild_id=guild_id,
            channel_id=channel_id,
            user_id=user_id,
            recent_context=recent,
        )
        if generation.social:
            social_assessment = assess_social_intent(
                message,
                context.channel_history,
            )
            social_answer = await self._social_ai_recovery(
                message,
                display_name=display_name,
                recent_context=context.channel_history,
                attachment_context=list(attachment_context or ()),
                media=media,
                deescalate=social_assessment.deescalate,
                style_instruction=style.instruction,
                user_facts=context.short_term_facts,
            )
            if social_answer and not _looks_like_service_reply(social_answer):
                return await self._finalize(
                    message,
                    social_answer,
                    "meme" if decision.name == "meme" else "casual",
                    guild_id=guild_id,
                    channel_id=channel_id,
                    user_id=user_id,
                    confidence=0.90,
                    source="bella_social",
                    max_chars=generation.max_response_chars,
                    style=style,
                )
            return await self._fallback(
                decision,
                message,
                recent,
                guild_id,
                channel_id,
                user_id,
                humor_allowed=humor_allowed,
                style=style,
            )
        music_context = await self.music_memory.context_payload(message)
        permanent_context = await self.permanent.context_payload(
            message, guild_id=guild_id, user_id=user_id
        )
        self._pending_entities[(guild_id, channel_id, user_id)] = tuple(
            entity
            for item in permanent_context
            for entity in entities_from_dicts(item.get("entities") or [])
        )
        specialized_context = [
            *music_context,
            *permanent_context,
            *context.site_payload(),
        ]
        if _can_use_gemini_text_path(generation.response_size, media):
            gemini_answer = await self.bella_handler.generate_public_text_reply(
                fast_public_text_prompt(
                    message=message,
                    display_name=display_name,
                    channel_history=context.channel_history or context.history[-6:],
                    site_context=specialized_context,
                    server_context=context.server_payload(),
                    short_term_facts=context.short_term_facts,
                    operational_rules=list(operational_rules or ()),
                    generation_instruction=generation.instruction,
                    personality_instruction=(
                        style.instruction
                        + (
                            "\nSe a base especializada trouxer jarvis_music_memory, trate como "
                            "gosto pessoal da Bella. Use a memória apenas como referência, "
                            "reescreva com suas próprias palavras e não copie frases da memória."
                            if music_context
                            else ""
                        )
                    ),
                ),
                user_message=message,
                recent_context=context.channel_history,
                max_output_tokens=min(generation.max_output_tokens, 720),
                max_chars=min(generation.max_response_chars, 2600),
                timeout_seconds=min(generation.timeout_seconds, 4.2),
            )
            if (
                gemini_answer
                and not _looks_like_bad_context_carryover(message, gemini_answer)
                and not _looks_like_generic_bella_reply(message, gemini_answer)
            ):
                return await self._finalize(
                    message,
                    gemini_answer,
                    "conversation",
                    guild_id=guild_id,
                    channel_id=channel_id,
                    user_id=user_id,
                    confidence=0.90,
                    site_related=bool(specialized_context)
                    or bool(context.server_payload()),
                    source="gemini_text",
                    max_chars=min(generation.max_response_chars, 2600),
                    style=style,
                )
            if not groq_ready:
                return await self._fallback(
                    decision,
                    message,
                    recent,
                    guild_id,
                    channel_id,
                    user_id,
                    humor_allowed=humor_allowed,
                    style=style,
                )
        if not (groq_ready and allow_groq):
            return await self._fallback(
                decision,
                message,
                recent,
                guild_id,
                channel_id,
                user_id,
                humor_allowed=humor_allowed,
                style=style,
            )
        if allow_groq and _can_use_fast_text_path(generation.response_size, media):
            try:
                fast_answer = sanitize_output(
                    _format_for_readability(
                        await self.service.generate_text(
                            fast_public_text_prompt(
                                message=message,
                                display_name=display_name,
                                channel_history=context.channel_history or context.history[-6:],
                                site_context=specialized_context,
                                server_context=context.server_payload(),
                                short_term_facts=context.short_term_facts,
                                operational_rules=list(operational_rules or ()),
                                generation_instruction=generation.instruction,
                                personality_instruction=(
                                    style.instruction
                                    + (
                                        "\nSe a base especializada trouxer jarvis_music_memory, trate como "
                                        "gosto pessoal da Bella. Use a memória apenas como referência, "
                                        "reescreva com suas próprias palavras e não copie frases da memória."
                                        if music_context
                                        else ""
                                    )
                                ),
                            ),
                            max_output_tokens=(
                                generation.max_output_tokens
                                if generation.task
                                else min(generation.max_output_tokens, 360)
                            ),
                            timeout_seconds=(
                                generation.timeout_seconds
                                if generation.task
                                else min(generation.timeout_seconds, 3.8)
                            ),
                            model_policy="economy",
                            use_cache=generation.use_cache,
                        ),
                        complex_mode=False,
                    ),
                    generation.max_response_chars
                    if generation.task
                    else min(generation.max_response_chars, 1800),
                )
            except Exception as exc:
                await self.state.set(
                    "groq_last_fast_public_error",
                    f"{type(exc).__name__}: {exc}",
                )
                return await self._fallback(
                    decision,
                    message,
                    recent,
                    guild_id,
                    channel_id,
                    user_id,
                    humor_allowed=humor_allowed,
                    style=style,
                )
            if (
                fast_answer
                and not _looks_like_bad_context_carryover(message, fast_answer)
                and not _looks_like_generic_bella_reply(message, fast_answer)
            ):
                return await self._finalize(
                    message,
                    fast_answer,
                    "conversation",
                    guild_id=guild_id,
                    channel_id=channel_id,
                    user_id=user_id,
                    confidence=0.88,
                    site_related=bool(specialized_context)
                    or bool(context.server_payload()),
                    source="groq_fast_text",
                    max_chars=min(generation.max_response_chars, 1800),
                    style=style,
                )
        if not allow_groq:
            return await self._fallback(
                decision,
                message,
                recent,
                guild_id,
                channel_id,
                user_id,
                humor_allowed=humor_allowed,
                style=style,
            )
        try:
            data = await self.service.generate_json(
                public_response_prompt(
                    message=message,
                    display_name=display_name,
                    history=context.history,
                    channel_history=context.channel_history,
                    site_context=specialized_context,
                    server_context=context.server_payload(),
                    short_term_facts=context.short_term_facts,
                    attachment_context=list(attachment_context or ()),
                    operational_rules=list(operational_rules or ()),
                    generation_instruction=generation.instruction,
                    personality_instruction=(
                        style.instruction
                        + (
                            "\nSe a base especializada trouxer jarvis_music_memory, trate como "
                            "gosto pessoal da Bella. Use a memória apenas como referência, "
                            "reescreva com suas próprias palavras e não copie frases da memória. "
                            "Responda naturalmente em primeira pessoa quando perguntarem sobre "
                            "cantor ou músicas favoritas."
                            if music_context
                            else ""
                        )
                    ),
                ),
                _public_schema(),
                max_output_tokens=generation.max_output_tokens,
                timeout_seconds=min(
                    generation.timeout_seconds,
                    float(
                        getattr(
                            self.service,
                            "timeout_seconds",
                            getattr(self.settings, "groq_timeout_seconds", 15),
                        )
                    ),
                ),
                media=media,
                model_policy=generation.model_policy,
                use_cache=generation.use_cache,
                system_prompt=(
                    f"{BELLA_SYSTEM_PROMPT}\n\n"
                    "O campo answer é a fala da Bella no Discord e precisa seguir "
                    "toda a voz acima. Só os outros campos são metadados."
                ),
            )
        except Exception as exc:
            await self.state.set(
                "groq_last_public_error",
                f"{type(exc).__name__}: {exc}",
            )
            return await self._fallback(
                decision,
                message,
                recent,
                guild_id,
                channel_id,
                user_id,
                humor_allowed=humor_allowed,
                style=style,
            )

        intent = str(data.get("intent", "conversation"))
        confidence = _score(data.get("confidence"))
        answer = sanitize_output(
            _format_for_readability(
                str(data.get("answer", "")),
                complex_mode=generation.complex,
            ),
            generation.max_response_chars,
        )
        if (
            data.get("should_reply")
            and answer
            and not _looks_like_bad_context_carryover(message, answer)
            and not _looks_like_generic_bella_reply(message, answer)
        ):
            return await self._finalize(
                message,
                answer,
                intent,
                guild_id=guild_id,
                channel_id=channel_id,
                user_id=user_id,
                confidence=confidence,
                site_related=bool(data.get("specialized_context_used"))
                or bool(data.get("site_related")),
                source="groq",
                max_chars=generation.max_response_chars,
                style=style,
            )
        return await self._fallback(
            decision,
            message,
            recent,
            guild_id,
            channel_id,
            user_id,
            humor_allowed=humor_allowed,
            style=style,
        )

    async def _fallback(
        self,
        decision: IntentDecision,
        message: str,
        recent_context: list[dict[str, str]],
        guild_id: int,
        channel_id: int,
        user_id: int,
        *,
        humor_allowed: bool = True,
        style: ConversationStyle | None = None,
    ) -> PublicReply:
        fallback = await self.local.fallback(
            decision,
            message,
            recent_context=recent_context,
        )
        if not fallback.should_reply or not fallback.answer.strip():
            return PublicReply(
                False,
                "",
                fallback.intent,
                0.0,
                False,
                "silent_fallback",
            )
        if not humor_allowed and fallback.intent in {"casual", "meme"}:
            return await self._finalize(
                message,
                _neutral_social_reply(message),
                "conversation",
                guild_id=guild_id,
                channel_id=channel_id,
                user_id=user_id,
                confidence=0.75,
                source="local_neutral",
                max_chars=800,
                style=style,
            )
        return await self._finalize(
            message,
            fallback.answer,
            fallback.intent,
            guild_id=guild_id,
            channel_id=channel_id,
            user_id=user_id,
            confidence=0.65,
            source="fallback",
            max_chars=max(2600, self.settings.ai_max_response_chars),
            style=style,
        )

    async def _matches_owner_memory(
        self,
        message: str,
        *,
        guild_id: int | None = None,
        user_id: int | None = None,
    ) -> bool:
        try:
            payload = await self.permanent.context_payload(
                message, guild_id=guild_id, user_id=user_id
            )
        except Exception:
            return False
        return any(
            item.get("type") == "owner_memory" and item.get("source") == "owner_fact"
            for item in payload
        )

    async def _finalize(
        self,
        user_text: str,
        answer: str,
        intent: str,
        *,
        guild_id: int,
        channel_id: int,
        user_id: int,
        confidence: float,
        site_related: bool = False,
        source: str,
        max_chars: int | None = None,
        style: ConversationStyle | None = None,
    ) -> PublicReply:
        selected_style = style or self.personality.analyze(
            user_text,
            recent_context=[],
            humor_allowed=False,
        )
        if "```" in answer:
            # Trava: a Bella não escreve código no chat, venha de onde vier.
            answer = _code_refusal(user_text)
        adapted_answer = self.personality.adapt(
            answer,
            user_message=user_text,
            style=selected_style,
        )
        safe_answer = sanitize_output(
            adapted_answer,
            max_chars or self.settings.ai_max_response_chars,
        )
        if (
            assess_social_intent(user_text).deescalate
            and (
                _looks_like_personal_attack(safe_answer)
                or _looks_like_retaliatory_offense(safe_answer)
            )
        ):
            safe_answer = _neutral_social_reply(user_text, conflict=True)
        if not safe_answer:
            safe_answer = _neutral_social_reply(user_text)
        entities = self._pending_entities.pop((guild_id, channel_id, user_id), ())
        if entities and safe_answer:
            # O modelo recebe as menções prontas, mas pode trocar por nome ou link;
            # a aplicação garante a menção nativa clicável.
            safe_answer = render_mentions(safe_answer, entities)
        await self.memory.append(
            guild_id,
            channel_id,
            user_id,
            user_text=user_text,
            assistant_text=safe_answer,
        )
        return PublicReply(
            should_reply=True,
            answer=safe_answer,
            intent=intent,
            confidence=confidence,
            site_related=site_related,
            source=source,
            conversation_context=selected_style.context,
            staggered=bool(
                selected_style.short_rhythm
                and selected_style.context in {"casual", "meme", "banter"}
            ),
            entities=tuple(entities),
        )

    async def _social_ai_recovery(
        self,
        message: str,
        *,
        display_name: str,
        recent_context: list[dict[str, str]],
        attachment_context: list[str] | None = None,
        media: list[MediaInput] | None = None,
        deescalate: bool = False,
        style_instruction: str = "",
        user_facts: dict[str, str] | None = None,
    ) -> str:
        answer = await self.bella_handler.generate_social_reply(
            message,
            display_name=display_name,
            recent_context=recent_context[-8:],
            attachment_context=list(attachment_context or ()),
            media=media,
            deescalate=deescalate,
            style_instruction=style_instruction,
            user_facts=user_facts,
            extra_validation=lambda candidate: _social_guard_reasons(
                message,
                candidate,
            ),
        )
        if answer and not deescalate:
            answer = _apply_social_style(message, answer)
            if answer:
                return answer
        if _is_greeting_correction(message):
            return _greeting_correction_reply(message)
        if deescalate:
            return _neutral_social_reply(message, conflict=True)
        return ""


_GREETING_ONLY = {
    "oi", "oii", "ola", "bom dia", "boa tarde", "boa noite", "tudo bem", "obrigado",
    "obrigada", "valeu", "vlw", "tchau", "kkkk", "kkkkk", "ok", "blz", "beleza",
}


_CODE_REFUSALS = (
    "não faço código por aqui. se for um projeto, abre um ticket que o Talisson vê com vc.",
    "código eu não escrevo no chat. se for projeto, o caminho é abrir um ticket com o Talisson.",
    "isso eu não faço: código não sai pelo chat. pra projeto, abre um ticket.",
)


def _code_refusal(message: str) -> str:
    return _CODE_REFUSALS[sum(map(ord, message)) % len(_CODE_REFUSALS)]


def _follows_code_request(message: str, recent: list[dict[str, str]]) -> bool:
    """'python', 'quero', 'sim' logo depois de um pedido de código."""
    text = normalize_text(message).strip(" ?!.,")
    if not text or "?" in message or len(text.split()) > 6 or text in _GREETING_ONLY:
        return False
    user_turns = [
        str(item.get("text", ""))
        for item in reversed(recent[-6:])
        if not any(
            name in normalize_text(str(item.get("author", "")))
            for name in ("bella", "nyelle", "jarvis")
        )
    ]
    return any(looks_like_code_request(turn) for turn in user_turns[:2])


def _continues_pending_task(message: str, recent: list[dict[str, str]]) -> bool:
    """Resposta curta a uma pergunta da Bella, ou logo depois de um pedido de tarefa."""
    text = normalize_text(message).strip(" ?!.,")
    if not text or text in _GREETING_ONLY or len(text.split()) > 25:
        return False
    last_bella = ""
    user_turns: list[str] = []
    for item in reversed(recent[-8:]):
        author = normalize_text(str(item.get("author", "")))
        content = str(item.get("text", ""))
        if any(name in author for name in ("bella", "nyelle", "jarvis")):
            if not last_bella:
                last_bella = content.strip()
        else:
            user_turns.append(content)
        if len(user_turns) >= 3 and last_bella:
            break
    asked = last_bella.endswith("?") or "quer que eu" in normalize_text(last_bella)
    recent_task = any(looks_like_task_request(turn) for turn in user_turns[:3])
    return asked or recent_task


def _social_guard_reasons(message: str, answer: str) -> tuple[str, ...]:
    """Erros de conversa que o validador genérico da voz não enxerga."""
    checks = (
        ("social_explanation", _looks_like_social_explanation(answer)),
        ("asks_for_context", _is_referential_social_message(message) and _asks_for_missing_context(answer)),
        ("personal_attack", _looks_like_personal_attack(answer) or _looks_like_retaliatory_offense(answer)),
        ("missed_tease", _misses_playful_tease(message, answer)),
        ("missed_correction", _misses_correction_tone(message, answer)),
        ("missed_greeting_correction", _misses_greeting_correction(message, answer)),
        ("six_seven_literal", _misses_six_seven_tone(message, answer)),
        ("service_reply", _looks_like_service_reply(answer)),
    )
    return tuple(reason for reason, failed in checks if failed)


def _public_schema() -> dict[str, object]:
    return {
        "type": "object",
        "properties": {
            "intent": {
                "type": "string",
                "enum": [
                    "general",
                    "meme",
                    "conversation",
                    "support",
                    "feedback",
                    "specialized",
                    "unsafe",
                ],
            },
            "should_reply": {"type": "boolean"},
            "site_related": {"type": "boolean"},
            "specialized_context_used": {"type": "boolean"},
            "answer_supported": {"type": "boolean"},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "answer": {"type": "string"},
        },
        "required": [
            "intent",
            "should_reply",
            "site_related",
            "specialized_context_used",
            "answer_supported",
            "confidence",
            "answer",
        ],
    }


def _score(value: object) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def _looks_like_service_reply(value: str) -> bool:
    text = " ".join(value.casefold().split())
    generic = (
        "como posso ajudar",
        "em que posso ajudar",
        "estou à disposição",
        "estou a disposição",
        "como posso ser útil",
        "como posso ser util",
        "posso ajudar em algo",
        "estou aqui para ajudar",
        "estou aqui para auxiliar",
        "posso auxilia-lo",
        "posso auxiliá-lo",
        "caso precise",
        "fique a vontade",
        "fique à vontade",
        "compreendo sua",
        "entendo sua solicitacao",
        "entendo sua solicitação",
        "certamente",
        "sua solicitacao",
        "sua solicitação",
        "tudo sob controle",
        "meu bom",
        "vou responder em json",
        "responder aqui em json",
        "objeto json",
        "schema json",
        "como a bella costuma fazer",
    )
    return any(phrase in text for phrase in generic)


def _looks_like_generic_bella_reply(message: str, answer: str) -> bool:
    return is_generic_bella_response(message, answer)


def _is_short_casual_message(text: str) -> bool:
    if len(text.split()) <= 4:
        return True
    casual_markers = (
        "oi",
        "oie",
        "ola",
        "bom dia",
        "boa tarde",
        "boa noite",
        "tudo bem",
        "tudo bom",
        "como vc ta",
        "como voce ta",
        "como vc esta",
        "como voce esta",
        "e vc",
        "e voce",
        "sabadou",
        "ta boa",
        "ta bao",
        "ta difícil",
        "ta dificil",
    )
    return any(marker in text for marker in casual_markers)


def _looks_like_social_explanation(value: str) -> bool:
    text = " ".join(value.casefold().split())
    explanatory = (
        "é uma gíria",
        "e uma giria",
        "significa ",
        "quer dizer ",
        "forma casual",
        "forma descontraída",
        "forma descontraida",
        "você está usando",
        "voce esta usando",
        "parece que você",
        "parece que voce",
        "se refere a",
        "essa expressão",
        "essa expressao",
        "não entendi direito",
        "nao entendi direito",
        "pode explicar melhor",
        "poderia explicar melhor",
        "do que você está falando",
        "do que voce esta falando",
    )
    return any(phrase in text for phrase in explanatory)


def _looks_like_bad_context_carryover(message: str, answer: str) -> bool:
    user_text = normalize_text(message)
    response = normalize_text(answer)
    if not user_text or social_message_requires_context(user_text):
        return False
    bad_markers = (
        "parece que voce estava conversando",
        "parece que vc estava conversando",
        "parece que voce esta conversando",
        "parece que vc esta conversando",
        "parece que voce estava falando",
        "quer continuar a conversa",
        "continuar a conversa",
        "conversando com alguem",
        "falando com alguem",
    )
    return any(marker in response for marker in bad_markers)


def _can_use_fast_text_path(
    response_size: str,
    media: list[MediaInput] | None,
) -> bool:
    if media:
        return False
    return response_size in {"concise", "standard"}


def _can_use_gemini_text_path(
    response_size: str,
    media: list[MediaInput] | None,
) -> bool:
    if media:
        return False
    return response_size in {"concise", "standard", "detailed"}


def _allow_groq_public_fallback() -> bool:
    provider = bella_ai_provider()
    if provider not in {"gemini", "google", "google-gemini"}:
        return True
    raw = os.getenv("BELLA_FALLBACK_TO_GROQ", "true").strip().casefold()
    return raw in {"1", "true", "yes", "on", "sim"}


def social_message_requires_context(text: str) -> bool:
    return any(
        marker in text
        for marker in (
            "isso",
            "disso",
            "nisso",
            "aquilo",
            "essas",
            "esses",
            "continua",
            "continue",
            "que voce mandou",
            "que vc mandou",
            "mensagem anterior",
            "papo anterior",
        )
    )


def _is_referential_social_message(value: str) -> bool:
    text = normalize_text(value).strip(" ?!.,")
    return any(
        phrase in text
        for phrase in (
            "o que acha disso",
            "oque acha disso",
            "que acha disso",
            "e isso",
            "olha isso",
            "que foi isso",
        )
    )


def _asks_for_missing_context(value: str) -> bool:
    text = normalize_text(value)
    return any(
        phrase in text
        for phrase in (
            "nao entendi",
            "pode explicar",
            "poderia explicar",
            "do que voce esta falando",
            "ao que voce se refere",
            "falando do jogo ou",
            "ou de outra coisa",
        )
    )


def _is_user_correction(value: str) -> bool:
    text = normalize_text(value).strip(" ?!.,")
    return bool(
        any(
            phrase in text
            for phrase in (
                "mas e verdade",
                "bom dia nao",
                "bom dia n?o",
                "boa tarde nao",
                "boa tarde n?o",
                "boa noite nao",
                "boa noite n?o",
                "nao moco",
                "to falando",
                "estou falando",
                "eu falei",
                "foi isso",
                "ganharam ontem",
                "ganhou ontem",
                "foi ontem",
            )
        )
        or re.match(r"^(?:mas|nao|na verdade)\b", text)
    )


def _is_greeting_correction(value: str) -> bool:
    text = normalize_text(value)
    return any(
        marker in text
        for marker in (
            "bom dia nao",
            "bom dia n?o",
            "boa tarde nao",
            "boa tarde n?o",
            "boa noite nao",
            "boa noite n?o",
        )
    )


def _misses_greeting_correction(message: str, answer: str) -> bool:
    if not _is_greeting_correction(message):
        return False
    text = normalize_text(answer).strip()
    message_text = normalize_text(message)
    if "bom dia nao" in message_text or "bom dia n?o" in message_text:
        return text.startswith("bom dia")
    if "boa tarde nao" in message_text or "boa tarde n?o" in message_text:
        return text.startswith("boa tarde")
    if "boa noite nao" in message_text or "boa noite n?o" in message_text:
        return text.startswith("boa noite")
    return False


def _greeting_correction_reply(message: str) -> str:
    text = normalize_text(message)
    if "noite" in text:
        return "Foi mal, boa noite então."
    if "tarde" in text:
        return "Foi mal, boa tarde então."
    if "dia" in text:
        return "Foi mal, bom dia então."
    return "Foi mal, corrigi aqui."


def _misses_correction_tone(message: str, answer: str) -> bool:
    if not _is_user_correction(message):
        return False
    text = normalize_text(answer)
    return any(
        phrase in text
        for phrase in (
            "nao acredito",
            "ainda e cedo",
            "tem certeza",
            "voce ta zoando",
            "voce esta zoando",
            "nao e possivel",
            "qual foi o placar",
            "sera que",
            "quem sabe",
        )
    )


def _misses_playful_tease(message: str, answer: str) -> bool:
    if not is_playful_tease(message):
        return False
    text = normalize_text(answer)
    missed_banter = (
        "o que ta rolando",
        "o que esta rolando",
        "o que aconteceu",
        "aconteceu alguma coisa",
        "como assim",
        "pode explicar",
        "nao entendi",
        "to ligado, o que",
        "zoei de volta",
        "foi brincadeira",
        "to brincando",
        "estou brincando",
        "retribuindo a provocacao",
        "cego em tiroteio",
    )
    return any(phrase in text for phrase in missed_banter)


def _looks_like_personal_attack(value: str) -> bool:
    text = normalize_text(value)
    direct_attack = re.search(
        r"\b(?:voce|tu|ce)\s+(?:e|eh|parece|continua|ficou)\s+"
        r"(?:(?:um|uma|muito|muita)\s+)?"
        r"(?:burro|idiota|fracassado|inutil|patetico|carente|feio|vergonhoso)\b",
        text,
    )
    if direct_attack:
        return True
    personal_phrases = (
        "seu burro",
        "seu idiota",
        "seu fracassado",
        "sua anta",
        "ninguem gosta de voce",
        "ninguem te aguenta",
        "voce passa vergonha",
        "voce se humilhou",
        "volta pra escola",
        "aprende a ler",
        "cala a boca",
    )
    return any(phrase in text for phrase in personal_phrases)


def _looks_like_retaliatory_offense(value: str) -> bool:
    text = normalize_text(value)
    return any(
        phrase in text
        for phrase in (
            "vai tomar",
            "vai se foder",
            "teu cu",
            "seu idiota",
            "seu imbecil",
            "cala a boca",
            "otario",
            "otário",
            "retardado",
        )
    )


def _neutral_social_reply(message: str, *, conflict: bool = False) -> str:
    if conflict:
        choices = (
            "calma aí. dá pra manter a resenha sem partir pra ofensa",
            "vamos baixar a temperatura um pouquinho e seguir sem atacar ninguém",
            "tá tudo bem, só vamos manter o respeito por aqui",
        )
    else:
        return ""
    return choices[sum(ord(character) for character in message) % len(choices)]


def _misses_six_seven_tone(message: str, answer: str) -> bool:
    if normalize_text(message).strip(" ?!.,") not in {"six seven", "67"}:
        return False
    text = normalize_text(answer)
    literal_markers = (
        "numero da sorte",
        "numeros da sorte",
        "jogo de numero",
        "campeonato de numero",
        "campeonato dos numero",
        "jogar na loteria",
        "apostar na loteria",
        "matematica",
        "sessenta e sete significa",
        "em ingles",
        "traducao",
    )
    return any(marker in text for marker in literal_markers)


def _apply_social_style(message: str, answer: str) -> str:
    if not answer:
        return ""
    high_energy = _is_high_energy_social(message)
    styled = re.sub(
        r"(?i)(?<!\w)(?:ha){2,}(?!\w)",
        "KKKKKKKK" if high_energy else "kkkk",
        answer,
    )
    styled = sanitize_output(styled, 500)
    return styled.upper() if high_energy else styled


def _is_high_energy_social(message: str) -> bool:
    letters = [character for character in message if character.isalpha()]
    if len(letters) >= 4:
        upper_ratio = sum(character.isupper() for character in letters) / len(letters)
        if upper_ratio >= 0.65:
            return True
    return message.count("!") >= 2


def _format_for_readability(value: str, *, complex_mode: bool) -> str:
    text = value.strip()
    if not text or "```" in text:
        return text  # código: quebrar linhas aqui estragaria a indentação
    text = re.sub(r"\s+(?=\d{1,3}[.)]\s+)", "\n", text)
    text = re.sub(r"\s+(?=[-•]\s+)", "\n", text)
    if "\n" in text or len(text) < 700:
        return text

    sentences = re.split(r"(?<=[.!?])\s+", text)
    if len(sentences) < 4:
        return text
    paragraphs: list[str] = []
    chunk: list[str] = []
    max_sentences = 2 if complex_mode or len(text) > 1200 else 3
    for sentence in sentences:
        if not sentence:
            continue
        chunk.append(sentence)
        if len(chunk) >= max_sentences:
            paragraphs.append(" ".join(chunk).strip())
            chunk = []
    if chunk:
        paragraphs.append(" ".join(chunk).strip())
    return "\n\n".join(part for part in paragraphs if part)
