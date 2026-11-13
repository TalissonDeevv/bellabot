from __future__ import annotations

import os
import re
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

from website_guard.executive import ExecutiveRequest
from website_guard.state import StateStore

from .groq_service import GroqService
from .intent_router import normalize_text
from .memory import executive_memories
from .prompts import executive_intent_prompt

EXECUTIVE_ACTIONS = (
    "role_add",
    "role_remove",
    "role_swap",
    "role_list",
    "ban",
    "unban",
    "punishment_revoke",
    "kick",
    "timeout",
    "untimeout",
    "voice_disconnect",
    "message_purge",
    "channel_lock",
    "channel_unlock",
    "channel_rename",
    "channel_topic_set",
    "channel_topic_clear",
    "channel_slowmode",
    "channel_create_text",
    "channel_create_voice",
    "channel_delete",
    "message_send",
    "message_pin",
    "message_unpin",
    "role_create",
    "role_rename",
    "role_delete",
    "nickname_set",
    "nickname_reset",
    "voice_move",
    "scheduled_event_create",
    "ticket_create",
    "ticket_close",
    "ticket_reopen",
    "smart_ticket_rename",
    "smart_ticket_status",
    "smart_ticket_priority",
    "smart_ticket_assign",
    "smart_ticket_escalate",
    "smart_ticket_close",
    "smart_ticket_reopen",
    "status",
    "feedback_sync",
    "knowledge_refresh",
    "changes_check",
    "weekly_report",
    "configure_feedback_channel",
    "configure_change_channel",
    "configure_report_channel",
    "configure_weekly_channel",
    "configure_welcome_channel",
    "configure_audit_channel",
    "statistics_schedule_set",
    "statistics_schedule_remove",
    "statistics_schedule_list",
    # Agente
    "undo_last",
    "briefing",
    "todo_add",
    "todo_list",
    "todo_done",
    "reminder_set",
    "history_search",
    "poll_create",
    "payment_link_create",
    "channel_access_grant",
    "channel_make_private",
    "stripe_setup",
    "audio_process",
    "knowledge_save",
    "knowledge_list",
    "knowledge_forget",
    "knowledge_update",
    "none",
)

AGENT_ACTIONS = (
    "undo_last",
    "briefing",
    "todo_add",
    "todo_list",
    "todo_done",
    "reminder_set",
    "history_search",
    "poll_create",
    "payment_link_create",
    "channel_access_grant",
    "channel_make_private",
    "stripe_setup",
    "audio_process",
    "knowledge_save",
    "knowledge_list",
    "knowledge_forget",
    "knowledge_update",
)
# Ações que não entram como etapa de um plano encadeado.
NON_CHAINABLE_ACTIONS = ("undo_last", "briefing", "payment_link_create", "stripe_setup", "audio_process")
MAX_PLAN_STEPS = 5

# Instruções (~8 mil caracteres com as ações do agente) + catálogo filtrado
# cabem aqui sem corte. O teste test_executive_natural garante isso.
EXECUTIVE_PROMPT_CHAR_LIMIT = 12000
_STOPWORDS = {
    "bella", "nyelle", "jarvis", "canal", "cargo", "para", "pra", "pro", "esse",
    "essa", "este", "esta", "isso", "aqui", "nesse", "nessa", "falando", "manda",
    "mande", "envia", "envie", "fala", "fale", "que", "com", "por", "porque",
    "motivo", "ele", "ela", "dele", "dela", "meu", "minha", "meus", "minhas",
    "deus", "amor", "favor", "mensagem", "todo", "todos", "agora", "ai",
}


class GeminiIntentParser:
    def __init__(self, service: GroqService, state: StateStore) -> None:
        self.service = service
        self.state = state
        # Preenchido pelo agente: últimas ações executadas no servidor, para
        # resolver "faz o mesmo com o @Pedro" e "desfaz isso".
        self.recent_actions_provider: Callable[
            [int], Awaitable[list[dict[str, Any]]]
        ] | None = None

    async def interpret(
        self,
        content: str,
        *,
        bot_user_id: int,
        mentioned_user_ids: list[int],
        mentioned_role_ids: list[int],
        mentioned_channel_ids: list[int] | None = None,
        guild_id: int | None = None,
        member_catalog: list[dict[str, Any]] | None = None,
        role_catalog: list[dict[str, Any]] | None = None,
        channel_catalog: list[dict[str, Any]] | None = None,
        current_channel_id: int | None = None,
        current_channel_name: str = "",
        current_channel_type: str = "",
        attachments: list[str] | None = None,
    ) -> ExecutiveRequest | None:
        if not self.service.available:
            return None
        attachments = list(attachments or [])
        # Com alvo mencionado ou áudio anexado, a IA sempre decide.
        has_targets = bool(
            [uid for uid in mentioned_user_ids if uid != bot_user_id]
            or mentioned_role_ids
            or mentioned_channel_ids
            or attachments
        )
        # Com alvo mencionado, a IA sempre decide: "sabe aquele @fulano? tira
        # ele daqui" começa como pergunta, mas é uma ordem.
        if not has_targets and (
            _looks_like_information_query(content)
            or _looks_like_clear_smalltalk(content)
        ):
            return None
        recent_actions: list[dict[str, Any]] = []
        if self.recent_actions_provider is not None and guild_id is not None:
            try:
                recent_actions = list(await self.recent_actions_provider(guild_id))[:3]
            except Exception:
                recent_actions = []
        recent_users = {int(a["target_id"]) for a in recent_actions if a.get("target_id")}
        recent_roles = {int(r) for a in recent_actions for r in a.get("role_ids", [])}
        recent_channels = {int(c) for a in recent_actions for c in a.get("channel_ids", [])}
        # O catálogo inteiro do servidor (centenas de membros e canais) passava
        # de 29 mil caracteres e o corte do prompt removia a própria ordem.
        # Mandamos só o que tem relação com o pedido.
        users = _relevant_entries(
            member_catalog,
            content,
            always=set(mentioned_user_ids) | recent_users,
            limit=20,
            fallback=0,
        )
        roles = _relevant_entries(
            role_catalog,
            content,
            always=set(mentioned_role_ids) | recent_roles,
            limit=20,
            fallback=0,
        )
        channels = _relevant_entries(
            channel_catalog,
            content,
            always=set(mentioned_channel_ids or [])
            | {current_channel_id or 0}
            | recent_channels,
            limit=25,
            fallback=15,
        )
        memories = (
            await executive_memories(self.state, guild_id)
            if guild_id is not None
            else {}
        )
        data = await self.service.generate_json(
            executive_intent_prompt(
                content=content,
                users=users,
                roles=roles,
                channels=channels,
                memories=memories,
                current_channel={
                    "id": current_channel_id,
                    "name": current_channel_name,
                    "type": current_channel_type,
                },
                recent_actions=recent_actions,
                now_text=_now_text(),
                attachments=attachments,
            ),
            _intent_schema(),
            max_output_tokens=1500,
            timeout_seconds=15,
            use_cache=False,
            prompt_char_limit=EXECUTIVE_PROMPT_CHAR_LIMIT,
        )
        action = str(data.get("action", "none"))
        confidence = _score(data.get("confidence"))
        if action not in EXECUTIVE_ACTIONS or action == "none":
            return None
        if confidence < _required_confidence(action):
            return None

        allowed_users = _catalog_ids(users) | set(mentioned_user_ids) | recent_users
        allowed_users.discard(bot_user_id)
        allowed_roles = _catalog_ids(roles) | set(mentioned_role_ids) | recent_roles
        allowed_channels = (
            _catalog_ids(channels) | set(mentioned_channel_ids or []) | recent_channels
        )
        if current_channel_id:
            allowed_channels.add(current_channel_id)

        def build(step: dict[str, Any], step_action: str) -> ExecutiveRequest:
            target_id = _allowed_id(step.get("target_id"), allowed_users)
            if step_action == "payment_link_create":
                # Cobrança só vai para um membro que foi @mencionado; "João da
                # padaria" escrito não pode virar o João do servidor.
                target_id = _allowed_id(step.get("target_id"), set(mentioned_user_ids))
            if step_action in {"unban", "punishment_revoke"}:
                target_id = _explicit_user_id(content, bot_user_id) or target_id
            return _request_from(
                step,
                step_action,
                content,
                target_id,
                allowed_roles,
                allowed_channels,
            )

        first = build(data, action)
        extra: list[ExecutiveRequest] = []
        raw_steps = data.get("next_steps")
        for step in raw_steps if isinstance(raw_steps, list) else []:
            if not isinstance(step, dict) or len(extra) >= MAX_PLAN_STEPS - 1:
                continue
            step_action = str(step.get("action", "none"))
            if step_action not in EXECUTIVE_ACTIONS or step_action in {
                "none",
                *NON_CHAINABLE_ACTIONS,
            }:
                continue
            extra.append(build(step, step_action))
        if not extra or action in NON_CHAINABLE_ACTIONS:
            return first
        return replace(first, steps=(first, *extra))


def _request_from(
    data: dict[str, Any],
    action: str,
    content: str,
    target_id: int | None,
    allowed_roles: set[int],
    allowed_channels: set[int],
) -> ExecutiveRequest:
    raw_options = data.get("options")
    options = tuple(
        str(item).strip()[:55]
        for item in (raw_options if isinstance(raw_options, list) else [])
        if str(item).strip()
    )[:10]
    return ExecutiveRequest(
        action=action,
        target_id=target_id,
        role_ids=_allowed_ids(data.get("role_ids"), allowed_roles),
        channel_ids=_allowed_ids(data.get("channel_ids"), allowed_channels),
        duration_seconds=_positive_int(data.get("duration_seconds")),
        normalized_text=content,
        memory_key=str(data.get("memory_key", ""))[:80],
        memory_value=str(data.get("memory_value", ""))[:600],
        amount=_positive_int(data.get("amount")),
        event_name=str(data.get("event_name", ""))[:100],
        scheduled_for=str(data.get("scheduled_for", ""))[:100],
        reason=str(data.get("reason", ""))[:400],
        permanent=bool(data.get("permanent")),
        name=str(data.get("name", ""))[:100],
        text=str(data.get("text", ""))[:2000],
        topic=str(data.get("topic", ""))[:1024],
        status_value=str(data.get("status_value", ""))[:100],
        priority_value=str(data.get("priority_value", ""))[:50],
        statistics_type=str(data.get("statistics_type", ""))[:30],
        frequency=str(data.get("frequency", ""))[:20],
        hour=_bounded_int(data.get("hour"), 0, 23),
        minute=_bounded_int(data.get("minute"), 0, 59),
        weekday=_bounded_int(data.get("weekday"), 0, 6),
        month_day=_bounded_int(data.get("month_day"), 1, 28),
        period_days=_bounded_int(data.get("period_days"), 1, 90),
        options=options,
        price=str(data.get("price", "") or "")[:30],
    )


def _required_confidence(action: str) -> float:
    if action in {"ban", "unban", "punishment_revoke", "kick", "channel_delete", "role_delete"}:
        return 0.88
    if action in {
        "message_send",
        "channel_rename",
        "channel_topic_set",
        "channel_slowmode",
        "smart_ticket_rename",
        "smart_ticket_status",
        "smart_ticket_priority",
        "smart_ticket_assign",
        "smart_ticket_escalate",
        *AGENT_ACTIONS,
    }:
        return 0.78
    return 0.86


def _now_text() -> str:
    try:
        zone = ZoneInfo(os.getenv("AGENT_TIMEZONE", "America/Sao_Paulo"))
    except Exception:
        zone = timezone.utc
    now = datetime.now(zone)
    weekdays = ("segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo")
    return f"{weekdays[now.weekday()]}, {now.strftime('%Y-%m-%d %H:%M')} ({zone})"


def _words(value: str) -> set[str]:
    text = normalize_text(re.sub(r"<[@#&!]*\d+>", " ", str(value or "")))
    return {
        word
        for word in re.findall(r"[a-z0-9]+", text)
        if len(word) >= 3 and word not in _STOPWORDS
    }


def _word_match(asked: set[str], name_words: set[str]) -> bool:
    # "anuncio" casa com "anuncios", "parceiro" com "parceiros".
    for word in asked:
        for name_word in name_words:
            if word == name_word:
                return True
            if min(len(word), len(name_word)) >= 4 and (
                word.startswith(name_word) or name_word.startswith(word)
            ):
                return True
    return False


def _relevant_entries(
    catalog: list[dict[str, Any]] | None,
    content: str,
    *,
    always: set[int],
    limit: int,
    fallback: int,
) -> list[dict[str, Any]]:
    """Filtra o catálogo pelas palavras do pedido, mantendo os alvos mencionados."""
    entries = list(catalog or [])
    asked = _words(content)
    chosen: list[dict[str, Any]] = []
    seen: set[int] = set()
    for entry in entries:
        entry_id = _positive_int(entry.get("id"))
        if entry_id and entry_id in always and entry_id not in seen:
            chosen.append(entry)
            seen.add(entry_id)
    for entry in entries:
        if len(chosen) >= limit:
            break
        entry_id = _positive_int(entry.get("id"))
        if not entry_id or entry_id in seen:
            continue
        names = " ".join(
            str(entry.get(key, "")) for key in ("name", "username")
        )
        if _word_match(asked, _words(names)):
            chosen.append(entry)
            seen.add(entry_id)
    if len(chosen) <= len(always & seen) and fallback:
        for entry in entries:
            if len(chosen) >= fallback:
                break
            entry_id = _positive_int(entry.get("id"))
            if entry_id and entry_id not in seen:
                chosen.append(entry)
                seen.add(entry_id)
    return chosen


def _looks_like_information_query(content: str) -> bool:
    cleaned = re.sub(r"<@!?\d+>", " ", content.casefold())
    cleaned = " ".join(cleaned.split()).strip(" ?!.,")
    if not cleaned:
        return False
    explicit_memory = (
        "lembre que",
        "salve",
        "registre",
        "a partir de agora",
        "guarde",
        "memorize",
    )
    if any(term in cleaned for term in explicit_memory):
        return False
    administrative = (
        "apague",
        "limpe",
        "ban",
        "desban",
        "unban",
        "revoga",
        "punição",
        "punicao",
        "expulse",
        "cargo",
        "timeout",
        "ticket",
        "tranque",
        "destranque",
        "mude",
        "mudar",
        "altere",
        "alterar",
        "renomeie",
        "renomear",
        "envie",
        "avise",
        "avisar",
        "avisa",
        "mande",
        "mandar",
        "manda",
        "diga",
        "fale",
        "enviar",
        "publique",
        "publicar",
        "crie",
        "criar",
        "exclua",
        "excluir",
        "delete",
        "deletar",
        "fixe",
        "fixar",
        "desafixe",
        "desafixar",
        "mova",
        "mover",
        "coloque",
        "colocar",
        "defina",
        "definir",
        "atribua",
        "atribuir",
        "assuma",
        "assumir",
        # Agente: briefing, tarefas, lembretes, busca, enquete, pagamento.
        "rolou",
        "aconteceu",
        "resumo",
        "briefing",
        "pendente",
        "tarefa",
        "anota",
        "lembra",
        "lembre",
        "desfaz",
        "desfazer",
        "busca",
        "procura",
        "falou",
        "enquete",
        "votacao",
        "votação",
        "pagamento",
        "cobranca",
        "cobrança",
        "link de pag",
        "fatura",
        "stripe",
        "esquece",
        "esqueca",
        "esqueça",
        "memoria",
        "memória",
        "guardado",
        "atualiza",
        "regra mudou",
        "guardou",
    )
    if any(term in cleaned for term in administrative):
        return False
    return cleaned.startswith(
        (
            "bella quem ",
            "bella qual ",
            "bella quais ",
            "bella sabe ",
            "bella lembra ",
            "bella o que ",
            "bella como funciona ",
            "bella por que ",
            "bella quando ",
            "bella onde ",
            "nyelle quem ",
            "nyelle qual ",
            "jarvis quem ",
            "jarvis qual ",
            "jarvis quais ",
            "jarvis sabe ",
            "jarvis lembra ",
            "jarvis o que ",
            "jarvis como funciona ",
            "jarvis por que ",
            "jarvis quando ",
            "jarvis onde ",
            "quem ",
            "qual ",
            "quais ",
            "o que ",
            "como funciona ",
            "por que ",
            "porque ",
            "quando ",
            "onde ",
            "sabe ",
            "lembra ",
            "voce sabe ",
            "você sabe ",
            "me fala quem ",
            "me diga quem ",
            "me explique ",
        )
    )


def _looks_like_clear_smalltalk(content: str) -> bool:
    cleaned = re.sub(r"<@!?\d+>", " ", content.casefold())
    cleaned = re.sub(r"^\s*(?:senhor\s+)?jarvis\b", " ", cleaned)
    cleaned = " ".join(cleaned.split()).strip(" ?!.,")
    if not cleaned:
        return True
    if re.fullmatch(r"(?:k{3,}|ha){2,}", cleaned):
        return True
    return cleaned in {
        "oi",
        "ola",
        "olá",
        "opa",
        "e ai",
        "e aí",
        "bom dia",
        "boa tarde",
        "boa noite",
        "tudo bem",
        "tudo bom",
        "ta ai",
        "tá aí",
        "acorda",
        "six seven",
    }


def _intent_schema() -> dict[str, object]:
    return {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": list(EXECUTIVE_ACTIONS)},
            "target_id": {"type": "integer"},
            "role_ids": {"type": "array", "items": {"type": "integer"}},
            "channel_ids": {"type": "array", "items": {"type": "integer"}},
            "duration_seconds": {"type": "integer"},
            "amount": {"type": "integer"},
            "event_name": {"type": "string"},
            "scheduled_for": {"type": "string"},
            "reason": {"type": "string"},
            "permanent": {"type": "boolean"},
            "name": {"type": "string"},
            "text": {"type": "string"},
            "topic": {"type": "string"},
            "status_value": {"type": "string"},
            "priority_value": {"type": "string"},
            "statistics_type": {
                "type": "string",
                "enum": [
                    "",
                    "site",
                    "servidor",
                    "bella",
                    "nyelle",
                    "jarvis",
                    "perfil",
                    "ranking",
                    "boosts",
                    "conhecimentos",
                ],
            },
            "frequency": {
                "type": "string",
                "enum": ["", "daily", "weekly", "monthly"],
            },
            "hour": {"type": "integer"},
            "minute": {"type": "integer"},
            "weekday": {"type": "integer"},
            "month_day": {"type": "integer"},
            "period_days": {"type": "integer"},
            "memory_key": {"type": "string"},
            "memory_value": {"type": "string"},
            "options": {"type": "array", "items": {"type": "string"}},
            "price": {"type": "string"},
            "next_steps": {
                "type": "array",
                "description": "Etapas seguintes de um pedido composto, mesmos campos de action.",
                "items": {"type": "object"},
            },
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        },
        "required": [
            "action",
            "target_id",
            "role_ids",
            "channel_ids",
            "duration_seconds",
            "amount",
            "event_name",
            "scheduled_for",
            "reason",
            "permanent",
            "name",
            "text",
            "topic",
            "status_value",
            "priority_value",
            "statistics_type",
            "frequency",
            "hour",
            "minute",
            "weekday",
            "month_day",
            "period_days",
            "memory_key",
            "memory_value",
            "confidence",
        ],
    }


def _explicit_user_id(content: str, bot_user_id: int) -> int | None:
    patterns = (
        re.compile(r"<@!?(\d{15,22})>"),
        re.compile(
            r"\b(?:usu[aá]rio|user|membro)?\s*(?:com\s+)?"
            r"(?:id\s*)?[:#-]?\s*(\d{15,22})\b",
            flags=re.IGNORECASE,
        ),
    )
    for pattern in patterns:
        for match in pattern.finditer(content):
            try:
                user_id = int(match.group(1))
            except (TypeError, ValueError):
                continue
            if user_id != bot_user_id:
                return user_id
    return None


def _catalog_ids(values: list[dict[str, Any]]) -> set[int]:
    result: set[int] = set()
    for value in values:
        try:
            result.add(int(value.get("id", 0)))
        except (TypeError, ValueError):
            continue
    return result


def _allowed_id(value: object, allowed: set[int]) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed in allowed else None


def _allowed_ids(value: object, allowed: set[int]) -> tuple[int, ...]:
    if not isinstance(value, list):
        return ()
    result: list[int] = []
    for item in value:
        selected = _allowed_id(item, allowed)
        if selected is not None and selected not in result:
            result.append(selected)
    return tuple(result)


def _positive_int(value: object) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _bounded_int(
    value: object,
    minimum: int,
    maximum: int,
) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if minimum <= parsed <= maximum else None


def _score(value: object) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0
