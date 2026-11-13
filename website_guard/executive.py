from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable
from unicodedata import combining, normalize
from zoneinfo import ZoneInfo

import discord

from .admin import AdminAudit
from .config import Settings
from .identity import OWNER_DISCORD_ID
from .state import StateStore

LOGGER = logging.getLogger(__name__)
EXECUTIVE_OWNER_ID = OWNER_DISCORD_ID
MENTION_PATTERN = re.compile(r"<@!?&?\d+>")
USER_MENTION_PATTERN = re.compile(r"<@!?(\d{15,22})>")
EXPLICIT_USER_ID_PATTERN = re.compile(
    r"\b(?:usu[aá]rio|user|membro)?\s*(?:com\s+)?(?:id\s*)?[:#-]?\s*(\d{15,22})\b",
    flags=re.IGNORECASE,
)
DURATION_PATTERN = re.compile(
    r"\b(\d+)\s*(segundos?|secs?|s|minutos?|mins?|min|m|horas?|hrs?|h|dias?|d)\b"
)
MESSAGE_COUNT_PATTERN = re.compile(
    r"\b(?:ultimas?|últimas?|as)?\s*(\d{1,4})\s+mensagens?\b"
)
EVENT_TIME_PATTERN = re.compile(
    r"\b(?:as|às)\s*(\d{1,2})(?::(\d{2}))?h?\b"
)
NEGATION_PATTERN = re.compile(
    r"\b(cancela|cancelar|desconsidera|desconsiderar)\b|"
    r"^(?:jarvis\s+)?(?:nao|não|nunca|jamais)\b"
)

ACTION_LABELS = {
    "role_add": "Adicionar cargo",
    "role_remove": "Remover cargo",
    "role_swap": "Trocar cargo",
    "role_list": "Listar cargos",
    "ban": "Banir usuário",
    "unban": "Revogar banimento",
    "punishment_revoke": "Revogar punição",
    "kick": "Expulsar usuário",
    "timeout": "Silenciar usuário",
    "untimeout": "Remover silenciamento",
    "voice_disconnect": "Desconectar do canal de voz",
    "message_purge": "Limpar mensagens",
    "channel_lock": "Trancar canal",
    "channel_unlock": "Destrancar canal",
    "channel_rename": "Renomear canal",
    "channel_topic_set": "Alterar tópico do canal",
    "channel_topic_clear": "Remover tópico do canal",
    "channel_slowmode": "Alterar modo lento",
    "channel_create_text": "Criar canal de texto",
    "channel_create_voice": "Criar canal de voz",
    "channel_delete": "Excluir canal",
    "message_send": "Enviar mensagem",
    "message_pin": "Fixar mensagem",
    "message_unpin": "Desafixar mensagem",
    "role_create": "Criar cargo",
    "role_rename": "Renomear cargo",
    "role_delete": "Excluir cargo",
    "nickname_set": "Alterar apelido",
    "nickname_reset": "Restaurar apelido",
    "voice_move": "Mover usuário em canal de voz",
    "scheduled_event_create": "Criar evento",
    "ticket_create": "Criar ticket",
    "ticket_close": "Fechar ticket",
    "ticket_reopen": "Reabrir ticket",
    "smart_ticket_rename": "Renomear ticket",
    "smart_ticket_status": "Alterar status do ticket",
    "smart_ticket_priority": "Alterar prioridade do ticket",
    "smart_ticket_assign": "Atribuir ticket",
    "smart_ticket_escalate": "Encaminhar ticket",
    "smart_ticket_close": "Encerrar ticket",
    "smart_ticket_reopen": "Reabrir ticket",
    "status": "Consultar status",
    "feedback_sync": "Sincronizar feedbacks",
    "knowledge_refresh": "Atualizar conhecimento",
    "changes_check": "Verificar alterações",
    "weekly_report": "Gerar relatório semanal",
    "configure_feedback_channel": "Configurar canal de feedbacks",
    "configure_change_channel": "Configurar canal de alterações",
    "configure_report_channel": "Configurar canal de relatórios",
    "configure_weekly_channel": "Configurar canal semanal",
    "configure_welcome_channel": "Configurar canal de recepção",
    "configure_audit_channel": "Configurar canal de auditoria",
    "memory_set": "Registrar memória administrativa",
    "memory_delete": "Remover memória administrativa",
    "memory_list": "Listar memórias administrativas",
    "operational_rule_set": "Definir regra operacional",
    "statistics_schedule_set": "Agendar estatísticas automáticas",
    "statistics_schedule_remove": "Remover agenda estatística",
    "statistics_schedule_list": "Listar agendas estatísticas",
}


@dataclass(frozen=True)
class ExecutiveRequest:
    action: str
    target_id: int | None = None
    role_ids: tuple[int, ...] = ()
    channel_ids: tuple[int, ...] = ()
    duration_seconds: int | None = None
    normalized_text: str = ""
    memory_key: str = ""
    memory_value: str = ""
    amount: int | None = None
    event_name: str = ""
    scheduled_for: str = ""
    reason: str = ""
    permanent: bool = False
    name: str = ""
    text: str = ""
    topic: str = ""
    status_value: str = ""
    priority_value: str = ""
    statistics_type: str = ""
    frequency: str = ""
    hour: int | None = None
    minute: int | None = None
    weekday: int | None = None
    month_day: int | None = None
    period_days: int | None = None
    # Agente: opções de enquete, valor de pagamento e as etapas de um pedido
    # composto ("cria o canal, dá acesso e manda boas-vindas").
    options: tuple[str, ...] = ()
    price: str = ""
    steps: tuple["ExecutiveRequest", ...] = ()


@dataclass(frozen=True)
class ExecutiveOutcome:
    success: bool
    public_message: str
    audit_reason: str
    target: discord.abc.User | None = None
    roles: tuple[discord.Role, ...] = ()
    notes: str = ""


Handler = Callable[[discord.Message, ExecutiveRequest], Awaitable[ExecutiveOutcome]]
IntentResolver = Callable[..., Awaitable[ExecutiveRequest | None]]
TextGenerator = Callable[..., Awaitable[str]]


class NaturalCommandInterpreter:
    def interpret(
        self,
        content: str,
        *,
        bot_user_id: int,
        mentioned_user_ids: list[int],
        mentioned_role_ids: list[int],
        mentioned_channel_ids: list[int] | None = None,
        member_catalog: list[dict[str, object]] | None = None,
        role_catalog: list[dict[str, object]] | None = None,
        channel_catalog: list[dict[str, object]] | None = None,
        current_channel_id: int | None = None,
        current_channel_name: str = "",
        current_channel_type: str = "",
    ) -> ExecutiveRequest | None:
        del current_channel_name, current_channel_type
        text = _normalize_text(MENTION_PATTERN.sub(" ", content))
        if not text or NEGATION_PATTERN.search(text):
            return None

        targets = [user_id for user_id in mentioned_user_ids if user_id != bot_user_id]
        if not targets:
            targets = _resolve_catalog_ids(text, member_catalog or [])
        target_id = targets[0] if len(targets) == 1 else None
        role_ids = tuple(
            dict.fromkeys(
                mentioned_role_ids
                or _resolve_catalog_ids(text, role_catalog or [])
            )
        )
        channel_ids = tuple(
            dict.fromkeys(
                mentioned_channel_ids
                or _resolve_catalog_ids(text, channel_catalog or [])
            )
        )
        duration = _parse_duration(text)
        amount = _parse_message_count(text)
        event_name = _parse_event_name(content)
        scheduled_for = _parse_event_time(text)

        action = self._detect_action(
            text,
            has_target=target_id is not None,
            has_channel=bool(channel_ids),
        )
        if action is None:
            return None
        if target_id is None and action in {"unban", "punishment_revoke"}:
            target_id = _explicit_user_id(content, excluded_ids={bot_user_id})
        if (
            action
            in {
                "channel_rename",
                "channel_topic_set",
                "channel_topic_clear",
                "channel_slowmode",
                "channel_lock",
                "channel_unlock",
            }
            and not channel_ids
            and current_channel_id
        ):
            channel_ids = (current_channel_id,)
        return ExecutiveRequest(
            action=action,
            target_id=target_id,
            role_ids=role_ids,
            channel_ids=channel_ids,
            duration_seconds=duration,
            normalized_text=text,
            amount=amount,
            event_name=event_name,
            scheduled_for=scheduled_for,
            name=_parse_requested_name(content, text, action),
            topic=_parse_requested_topic(content, text, action),
            text=_parse_requested_message(content, text, action),
        )

    def _detect_action(
        self,
        text: str,
        *,
        has_target: bool = False,
        has_channel: bool = False,
    ) -> str | None:
        if _contains_any(text, "evento", "event") and _has_any(
            text,
            "cria",
            "criar",
            "crie",
            "agenda",
            "agendar",
        ):
            return "scheduled_event_create"
        if _contains_any(text, "mensagem", "mensagens", "chat") and _has_any(
            text,
            "apaga",
            "apagar",
            "apague",
            "limpa",
            "limpar",
            "limpe",
            "remove",
            "remover",
        ):
            return "message_purge"
        if _has_any(
            text,
            "configura",
            "configurar",
            "define",
            "definir",
            "usa",
            "utiliza",
        ):
            if _contains_any(text, "auditoria", "audit"):
                return "configure_audit_channel"
            if _contains_any(text, "feedback", "feedbacks"):
                return "configure_feedback_channel"
            if _contains_any(text, "alteracao", "alteracoes", "mudanca", "mudancas"):
                return "configure_change_channel"
            if _contains_any(text, "recepcao", "boas vindas", "bem vindos"):
                return "configure_welcome_channel"
            if _contains_any(text, "semanal", "semana"):
                return "configure_weekly_channel"
            if _contains_any(text, "relatorio", "relatorios"):
                return "configure_report_channel"

        if _looks_like_message_send_order(text, has_target=has_target, has_channel=has_channel):
            return "message_send"

        if _contains(text, "ticket"):
            if _has_any(text, "reabre", "reabrir", "reativa", "restaura"):
                return "ticket_reopen"
            if _has_any(text, "fecha", "fechar", "encerra", "finaliza"):
                return "ticket_close"
            if _has_any(text, "cria", "criar", "abre", "abrir"):
                return "ticket_create"

        if _has_any(text, "silenciamento", "timeout", "mute"):
            if _has_any(
                text,
                "remove",
                "remover",
                "tira",
                "retira",
                "desfaz",
                "dessilencia",
                "desmuta",
            ):
                return "untimeout"
        if _has_any(text, "dessilencia", "desmuta"):
            return "untimeout"
        if _has_any(text, "silencia", "silenciar", "muta", "aplica timeout"):
            return "timeout"

        if _has_any(text, "desconecta", "desconectar", "remove da voz"):
            if _contains_any(text, "voz", "canal", "call"):
                return "voice_disconnect"

        if _contains_any(text, "canal", "chat"):
            if _has_any(
                text,
                "renomeia",
                "renomeie",
                "muda o nome",
                "mude o nome",
                "altera o nome",
                "alterar o nome",
                "troca o nome",
                "trocar o nome",
                "chama de",
                "chame de",
            ):
                return "channel_rename"
            if _contains_any(text, "topico", "descricao", "descrição"):
                if _has_any(text, "remove", "remover", "limpa", "limpar", "apaga"):
                    return "channel_topic_clear"
                if _has_any(
                    text,
                    "define",
                    "definir",
                    "muda",
                    "mude",
                    "altera",
                    "alterar",
                    "coloca",
                    "coloque",
                ):
                    return "channel_topic_set"
            if _contains_any(text, "modo lento", "slowmode", "delay"):
                return "channel_slowmode"
            if _has_any(
                text,
                "destranca",
                "destranque",
                "destrancar",
                "libera",
                "reabre",
            ):
                return "channel_unlock"
            if _has_any(
                text,
                "tranca",
                "tranque",
                "trancar",
                "bloqueia",
                "fecha",
            ):
                return "channel_lock"

        if _contains_any(text, "ticket") and _has_any(
            text,
            "renomeia",
            "renomeie",
            "muda o nome",
            "mude o nome",
            "altera o nome",
            "alterar o nome",
            "troca o nome",
            "trocar o nome",
            "chama de",
            "chame de",
        ):
            return "channel_rename"

        if _has_any(text, "fixa", "fixar", "fixe", "pina", "pinar"):
            return "message_pin"
        if _has_any(text, "desfixa", "desfixar", "despine", "unpin"):
            return "message_unpin"
        if _looks_like_message_send_order(text, has_target=has_target, has_channel=has_channel):
            return "message_send"

        if _contains_any(text, "mensagem", "aviso", "recado", "comunicado") and _has_any(
            text,
            "manda",
            "mande",
            "envia",
            "envie",
            "enviar",
            "avisa",
            "avise",
            "avisar",
            "fala",
            "fale",
            "diz",
            "diga",
        ):
            return "message_send"

        if _has_any(text, "banimento", "ban", "banido") and _has_any(
            text,
            "remove",
            "remover",
            "tira",
            "tire",
            "tirar",
            "retira",
            "retire",
            "retirar",
            "revoga",
            "revogue",
            "revogar",
            "desfaz",
            "desfazer",
            "desbane",
            "desbanir",
            "unban",
        ):
            return "unban"
        if _has_any(text, "desbane", "desbanir", "unban"):
            return "unban"
        if _has_any(text, "punicao", "punicoes", "penalidade", "penalidades") and _has_any(
            text,
            "remove",
            "remover",
            "tira",
            "tire",
            "tirar",
            "retira",
            "retire",
            "retirar",
            "revoga",
            "revogue",
            "revogar",
            "limpa",
            "limpar",
            "desfaz",
            "desfazer",
        ):
            return "punishment_revoke"
        if _has_any(text, "bane", "banir", "banimento"):
            return "ban"
        if _has_any(text, "expulsa", "expulsar", "kicka", "kick"):
            return "kick"

        if _contains_any(text, "cargo", "cargos", "promove"):
            if _has_any(text, "troca", "trocar", "substitui", "substituir"):
                return "role_swap"
            if _has_any(
                text,
                "lista",
                "listar",
                "mostra",
                "mostrar",
                "quais",
                "consulta",
            ):
                return "role_list"
            if _has_any(
                text,
                "remove",
                "remover",
                "retira",
                "retirar",
                "tira",
                "revoga",
            ):
                return "role_remove"
            if _has_any(
                text,
                "coloca",
                "adiciona",
                "adicionar",
                "atribui",
                "atribuir",
                "concede",
                "dar",
                "da o cargo",
                "de o cargo",
                "promove",
            ):
                return "role_add"

        if (
            _contains_any(text, "sincroniza", "sincronizar", "atualiza", "atualizar")
            and _contains_any(text, "feedback", "feedbacks")
        ):
            return "feedback_sync"
        if (
            _contains_any(
                text,
                "atualiza",
                "atualizar",
                "sincroniza",
                "sincronizar",
                "reindexa",
            )
            and _contains_any(text, "conhecimento", "indice", "conteudo")
        ):
            return "knowledge_refresh"
        if (
            _contains_any(
                text,
                "verifica",
                "verificar",
                "procura",
                "procurar",
                "analisa",
                "analisar",
            )
            and _contains_any(text, "alteracao", "alteracoes", "mudanca", "mudancas")
        ):
            return "changes_check"
        if (
            _contains_any(text, "relatorio", "relatorios")
            and _contains_any(
                text,
                "semanal",
                "gera",
                "gerar",
                "envia",
                "enviar",
            )
        ):
            return "weekly_report"
        if _contains_any(
            text,
            "status",
            "estado da plataforma",
            "situacao do site",
            "como esta o site",
            "como esta a plataforma",
        ):
            return "status"
        return None


class TicketRegistry:
    def __init__(self, state: StateStore) -> None:
        self.state = state

    async def get(self, guild_id: int, user_id: int) -> dict[str, object] | None:
        records = await self.state.get_section("executive_tickets")
        value = records.get(self._key(guild_id, user_id))
        return value if isinstance(value, dict) else None

    async def save(
        self,
        guild_id: int,
        user_id: int,
        value: dict[str, object],
    ) -> None:
        await self.state.update_section(
            "executive_tickets",
            self._key(guild_id, user_id),
            value,
        )

    async def find_by_channel(
        self,
        guild_id: int,
        channel_id: int,
    ) -> tuple[int, dict[str, object]] | None:
        records = await self.state.get_section("executive_tickets")
        prefix = f"{guild_id}:"
        for key, value in records.items():
            if (
                key.startswith(prefix)
                and isinstance(value, dict)
                and int(value.get("channel_id", 0)) == channel_id
            ):
                try:
                    return int(key.split(":", 1)[1]), value
                except ValueError:
                    continue
        return None

    @staticmethod
    def _key(guild_id: int, user_id: int) -> str:
        return f"{guild_id}:{user_id}"


class ExecutiveAssistant:
    def __init__(
        self,
        bot: discord.Client,
        settings: Settings,
        state: StateStore,
        audit: AdminAudit,
        intent_resolver: IntentResolver | None = None,
        text_generator: TextGenerator | None = None,
    ) -> None:
        self.bot = bot
        self.settings = settings
        self.state = state
        self.audit = audit
        self.intent_resolver = intent_resolver
        self.text_generator = text_generator
        self.interpreter = NaturalCommandInterpreter()
        self.tickets = TicketRegistry(state)
        # Ganchos do agente (website_guard/bella_agent.py): before_execute pode
        # assumir o pedido (plano com botões, prévia, confirmação) e
        # after_execute registra o histórico usado por "desfaz" e "faz o mesmo".
        self.before_execute: Callable[
            [discord.Message, ExecutiveRequest], Awaitable[bool]
        ] | None = None
        self.after_execute: Callable[
            [discord.Message, ExecutiveRequest, ExecutiveOutcome], Awaitable[None]
        ] | None = None
        self.handlers: dict[str, Handler] = {
            "role_add": self._role_add,
            "role_remove": self._role_remove,
            "role_swap": self._role_swap,
            "role_list": self._role_list,
            "ban": self._ban,
            "unban": self._unban,
            "punishment_revoke": self._revoke_punishment,
            "kick": self._kick,
            "timeout": self._timeout,
            "untimeout": self._untimeout,
            "voice_disconnect": self._voice_disconnect,
            "message_purge": self._purge_messages,
            "channel_lock": self._lock_channel,
            "channel_unlock": self._unlock_channel,
            "channel_rename": self._rename_channel,
            "channel_topic_set": self._set_channel_topic,
            "channel_topic_clear": self._clear_channel_topic,
            "channel_slowmode": self._set_channel_slowmode,
            "channel_create_text": self._create_text_channel,
            "channel_create_voice": self._create_voice_channel,
            "channel_delete": self._delete_channel,
            "message_send": self._send_message,
            "message_pin": self._pin_message,
            "message_unpin": self._unpin_message,
            "role_create": self._create_role,
            "role_rename": self._rename_role,
            "role_delete": self._delete_role,
            "nickname_set": self._set_nickname,
            "nickname_reset": self._reset_nickname,
            "voice_move": self._move_voice,
            "scheduled_event_create": self._create_scheduled_event,
            "ticket_create": self._ticket_create,
            "ticket_close": self._ticket_close,
            "ticket_reopen": self._ticket_reopen,
            "configure_feedback_channel": self._configure_feedback_channel,
            "configure_change_channel": self._configure_change_channel,
            "configure_report_channel": self._configure_report_channel,
            "configure_weekly_channel": self._configure_weekly_channel,
            "configure_welcome_channel": self._configure_welcome_channel,
            "configure_audit_channel": self._configure_audit_channel,
            "memory_set": self._memory_set,
            "memory_delete": self._memory_delete,
            "memory_list": self._memory_list,
        }
        self._guild_locks: dict[int, asyncio.Lock] = {}

    def register_handler(self, action: str, handler: Handler) -> None:
        self.handlers[action] = handler

    def is_bot_mentioned(self, message: discord.Message) -> bool:
        return bool(
            self.bot.user
            and any(user.id == self.bot.user.id for user in message.mentions)
        )

    def _replies_to_bot(self, message: discord.Message) -> bool:
        reference = getattr(message, "reference", None)
        resolved = getattr(reference, "resolved", None) if reference else None
        author = getattr(resolved, "author", None)
        return bool(self.bot.user and author and author.id == self.bot.user.id)

    @staticmethod
    def is_owner(user_id: int) -> bool:
        return user_id == EXECUTIVE_OWNER_ID

    def recognizes_local(self, message: discord.Message) -> bool:
        if message.guild is None:
            return False
        return self.interpreter.interpret(
            message.content,
            **self._intent_context(message, include_catalogs=True),
        ) is not None

    async def handle(self, message: discord.Message) -> bool:
        if message.guild is None or not isinstance(message.author, discord.Member):
            return False
        if not self.is_owner(message.author.id):
            return False
        pending = await self._pending_action(
            message.guild.id,
            message.author.id,
        )
        if (
            pending is None
            and not self.is_bot_mentioned(message)
            and not self._replies_to_bot(message)
            and not re.match(
                r"^\s*(?:bella|nyelle|jarvis)\b",
                message.content,
                flags=re.IGNORECASE,
            )
        ):
            return False
        if pending is not None:
            return await self._continue_pending_action(message, pending)

        intent_context = self._intent_context(message, include_catalogs=False)
        ai_context = self._intent_context(message, include_catalogs=True)
        request = self.interpreter.interpret(
            message.content,
            **ai_context,
        )
        if request is not None and _looks_compound(message.content):
            # "cria o canal, libera pro @x e manda boas-vindas" tem várias
            # ações; o interpretador local só enxergaria uma delas.
            request = None
        if request is None and self.intent_resolver is not None:
            try:
                request = await self.intent_resolver(
                    message.content,
                    guild_id=message.guild.id,
                    attachments=_attachment_summary(message),
                    **ai_context,
                )
            except Exception as exc:
                await self.state.set(
                    "groq_last_executive_error",
                    f"{type(exc).__name__}: {exc}",
                )
        if request is None:
            return False
        if await self._prepare_pending_action(message, request):
            return True
        if self.before_execute is not None:
            try:
                if await self.before_execute(message, request):
                    return True
            except Exception:
                LOGGER.exception("Falha no gancho before_execute do agente.")
        return await self._execute_request(message, request)

    def _intent_context(
        self,
        message: discord.Message,
        *,
        include_catalogs: bool,
    ) -> dict[str, object]:
        context: dict[str, object] = {
            "bot_user_id": self.bot.user.id if self.bot.user else 0,
            "mentioned_user_ids": [
                member.id for member in message.mentions
            ],
            "mentioned_role_ids": [
                role.id for role in message.role_mentions
            ],
            "mentioned_channel_ids": [
                channel.id for channel in message.channel_mentions
            ],
            "current_channel_id": message.channel.id,
            "current_channel_name": str(
                getattr(message.channel, "name", "mensagem-direta")
            ),
            "current_channel_type": type(message.channel).__name__,
        }
        if not include_catalogs or message.guild is None:
            return context
        context.update(
            {
            "member_catalog": [
                {
                    "id": member.id,
                    "name": member.display_name,
                    "username": member.name,
                }
                for member in message.guild.members[:250]
                if not (self.bot.user and member.id == self.bot.user.id)
            ],
            "role_catalog": [
                {"id": role.id, "name": role.name}
                for role in message.guild.roles
                if not role.is_default()
            ],
            "channel_catalog": [
                {
                    "id": channel.id,
                    "name": channel.name,
                    "type": type(channel).__name__,
                }
                for channel in message.guild.channels
                if isinstance(
                    channel,
                    (
                        discord.TextChannel,
                        discord.VoiceChannel,
                        discord.CategoryChannel,
                    ),
                )
            ]
            + [
                {
                    "id": thread.id,
                    "name": thread.name,
                    "type": type(thread).__name__,
                }
                for thread in message.guild.threads
            ],
            }
        )
        return context

    async def _execute_request(
        self,
        message: discord.Message,
        request: ExecutiveRequest,
    ) -> bool:
        await self.run_request(message, request, reply=True)
        return True

    async def run_request(
        self,
        message: discord.Message,
        request: ExecutiveRequest,
        *,
        reply: bool = True,
    ) -> ExecutiveOutcome:
        """Executa uma ação validada. Com reply=False só audita (usado em planos)."""
        handler = self.handlers.get(request.action)
        started_at = time.perf_counter()
        action_label = ACTION_LABELS.get(request.action, request.action)
        if handler is None:
            outcome = ExecutiveOutcome(
                success=False,
                public_message=self.generic_failure(message.author),
                audit_reason="Ação reconhecida, mas sem executor registrado.",
            )
        else:
            lock = self._guild_locks.setdefault(message.guild.id, asyncio.Lock())
            try:
                async with lock:
                    outcome = await handler(message, request)
            except (discord.Forbidden, discord.HTTPException) as exc:
                outcome = ExecutiveOutcome(
                    success=False,
                    public_message=self.generic_failure(message.author),
                    audit_reason=f"Falha da API do Discord: {type(exc).__name__}.",
                )
            except Exception as exc:
                outcome = ExecutiveOutcome(
                    success=False,
                    public_message=self.generic_failure(message.author),
                    audit_reason=f"Falha de execução: {type(exc).__name__}.",
                )
        if reply:
            await self._record_and_reply(
                message,
                action=action_label,
                outcome=outcome,
                started_at=started_at,
            )
        else:
            try:
                await self.audit.record_executive_action(
                    message,
                    action=action_label,
                    target=outcome.target,
                    roles=outcome.roles,
                    result="Sucesso" if outcome.success else "Falha",
                    reason=outcome.audit_reason,
                    duration_ms=int((time.perf_counter() - started_at) * 1000),
                    notes=outcome.notes,
                )
            except Exception:
                LOGGER.exception("Falha ao auditar a etapa %s.", action_label)
        if self.after_execute is not None and handler is not None:
            try:
                await self.after_execute(message, request, outcome)
            except Exception:
                LOGGER.exception("Falha no gancho after_execute do agente.")
        return outcome

    async def has_pending(self, guild_id: int, user_id: int) -> bool:
        return await self._pending_action(guild_id, user_id) is not None

    async def _prepare_pending_action(
        self,
        message: discord.Message,
        request: ExecutiveRequest,
    ) -> bool:
        if request.action == "ban":
            target, error = self._target(message.guild, request)
            if error or target is None:
                return False
            if not request.reason:
                await self._save_pending_action(
                    message,
                    request,
                    stage="ban_reason",
                )
                await self._reply_pending(
                    message,
                    f"Entendido, Senhor {message.author.mention}. Qual é o motivo "
                    f"do banimento de {_display(target.display_name)}?",
                )
                return True
            # Sem duração informada, o ban é permanente (padrão do Discord e
            # reversível com "tira o ban do ..."), sem pergunta extra.
        if request.action == "timeout" and request.duration_seconds is None:
            target, error = self._target(message.guild, request)
            if error or target is None:
                return False
            await self._save_pending_action(
                message,
                request,
                stage="timeout_duration",
            )
            await self._reply_pending(
                message,
                f"Por quanto tempo {_display(target.display_name)} deve permanecer "
                "silenciado?",
            )
            return True
        if request.action == "message_purge" and request.amount is None:
            await self._save_pending_action(
                message,
                request,
                stage="purge_amount",
            )
            await self._reply_pending(
                message,
                "Quantas mensagens devem ser removidas? Informe um número entre "
                "1 e 1000.",
            )
            return True
        if request.action == "message_send" and not request.text.strip():
            await self._save_pending_action(
                message,
                request,
                stage="message_text",
            )
            await self._reply_pending(
                message,
                "Boa. Qual texto você quer que eu envie?",
            )
            return True
        return False

    async def _continue_pending_action(
        self,
        message: discord.Message,
        pending: dict[str, object],
    ) -> bool:
        text = _normalize_text(message.content)
        if _has_any(text, "cancelar", "cancela", "desistir", "desiste"):
            await self._clear_pending_action(message.guild.id, message.author.id)
            await self._reply_pending(message, "Solicitação cancelada.")
            return True
        request = self._request_from_pending(pending)
        stage = str(pending.get("stage", ""))
        if stage == "ban_reason":
            reason = MENTION_PATTERN.sub("", message.content).strip()
            if len(reason) < 3:
                await self._reply_pending(
                    message,
                    "Informe um motivo objetivo para registrar o banimento.",
                )
                return True
            request = ExecutiveRequest(
                **{
                    **request.__dict__,
                    "reason": reason[:400],
                }
            )
            if request.duration_seconds is not None or request.permanent:
                await self._clear_pending_action(
                    message.guild.id,
                    message.author.id,
                )
                return await self._execute_request(message, request)
            await self._save_pending_action(
                message,
                request,
                stage="ban_duration",
            )
            await self._reply_pending(
                message,
                "Qual será a duração? Informe um período, como 1 hora, 7 dias, "
                "30 dias, ou diga permanente.",
            )
            return True
        if stage in {"ban_duration", "timeout_duration"}:
            permanent = _has_any(text, "permanente", "definitivo", "definitiva")
            duration = None if permanent else _parse_duration(text)
            if duration is None and not permanent:
                await self._reply_pending(
                    message,
                    "Não identifiquei a duração. Use, por exemplo, 1 hora, "
                    "7 dias ou permanente.",
                )
                return True
            request = ExecutiveRequest(
                **{
                    **request.__dict__,
                    "duration_seconds": duration,
                    "permanent": permanent,
                }
            )
        if stage == "purge_amount":
            amount_match = re.search(r"\b(\d{1,4})\b", text)
            amount = int(amount_match.group(1)) if amount_match else 0
            if not 1 <= amount <= 1000:
                await self._reply_pending(
                    message,
                    "Informe uma quantidade válida entre 1 e 1000 mensagens.",
                )
                return True
            request = ExecutiveRequest(
                **{
                    **request.__dict__,
                    "amount": amount,
                }
            )
        if stage == "message_text":
            outgoing_text = _clean_message_content(message.content)
            if len(outgoing_text) < 2:
                await self._reply_pending(
                    message,
                    "Me manda o texto certinho que eu devo enviar.",
                )
                return True
            request = ExecutiveRequest(
                **{
                    **request.__dict__,
                    "text": outgoing_text,
                }
            )
        await self._clear_pending_action(message.guild.id, message.author.id)
        return await self._execute_request(message, request)

    async def _pending_action(
        self,
        guild_id: int,
        user_id: int,
    ) -> dict[str, object] | None:
        values = await self.state.get_section("executive_pending_actions")
        key = f"{guild_id}:{user_id}"
        value = values.get(key)
        if not isinstance(value, dict):
            return None
        try:
            created_at = datetime.fromisoformat(str(value.get("created_at", "")))
        except ValueError:
            created_at = datetime.now(timezone.utc)
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) - created_at > timedelta(minutes=15):
            await self._clear_pending_action(guild_id, user_id)
            return None
        return dict(value)

    async def _save_pending_action(
        self,
        message: discord.Message,
        request: ExecutiveRequest,
        *,
        stage: str,
    ) -> None:
        await self.state.update_section(
            "executive_pending_actions",
            f"{message.guild.id}:{message.author.id}",
            {
                "stage": stage,
                "action": request.action,
                "target_id": request.target_id,
                "role_ids": list(request.role_ids),
                "channel_ids": list(request.channel_ids),
                "duration_seconds": request.duration_seconds,
                "normalized_text": request.normalized_text,
                "amount": request.amount,
                "event_name": request.event_name,
                "scheduled_for": request.scheduled_for,
                "reason": request.reason,
                "permanent": request.permanent,
                "name": request.name,
                "text": request.text,
                "topic": request.topic,
                "status_value": request.status_value,
                "priority_value": request.priority_value,
                "statistics_type": request.statistics_type,
                "frequency": request.frequency,
                "hour": request.hour,
                "minute": request.minute,
                "weekday": request.weekday,
                "month_day": request.month_day,
                "period_days": request.period_days,
                "created_at": datetime.now(timezone.utc).isoformat(),
            },
        )

    async def _clear_pending_action(self, guild_id: int, user_id: int) -> None:
        key = f"{guild_id}:{user_id}"

        def remove(values: dict[str, object]) -> None:
            values.pop(key, None)

        await self.state.mutate_section("executive_pending_actions", remove)

    @staticmethod
    def _request_from_pending(value: dict[str, object]) -> ExecutiveRequest:
        return ExecutiveRequest(
            action=str(value.get("action", "")),
            target_id=_optional_int(value.get("target_id")),
            role_ids=tuple(_int_list(value.get("role_ids"))),
            channel_ids=tuple(_int_list(value.get("channel_ids"))),
            duration_seconds=_optional_int(value.get("duration_seconds")),
            normalized_text=str(value.get("normalized_text", "")),
            amount=_optional_int(value.get("amount")),
            event_name=str(value.get("event_name", "")),
            scheduled_for=str(value.get("scheduled_for", "")),
            reason=str(value.get("reason", "")),
            permanent=bool(value.get("permanent")),
            name=str(value.get("name", "")),
            text=str(value.get("text", "")),
            topic=str(value.get("topic", "")),
            status_value=str(value.get("status_value", "")),
            priority_value=str(value.get("priority_value", "")),
            statistics_type=str(value.get("statistics_type", "")),
            frequency=str(value.get("frequency", "")),
            hour=_optional_int(value.get("hour"), allow_zero=True),
            minute=_optional_int(value.get("minute"), allow_zero=True),
            weekday=_optional_int(value.get("weekday"), allow_zero=True),
            month_day=_optional_int(value.get("month_day"), allow_zero=True),
            period_days=_optional_int(value.get("period_days")),
        )

    @staticmethod
    async def _reply_pending(
        message: discord.Message,
        text: str,
    ) -> None:
        await message.reply(
            text,
            mention_author=False,
            allowed_mentions=discord.AllowedMentions(
                users=[message.author],
                roles=False,
                everyone=False,
                replied_user=False,
            ),
        )

    async def _record_and_reply(
        self,
        message: discord.Message,
        *,
        action: str,
        outcome: ExecutiveOutcome,
        started_at: float,
    ) -> None:
        duration_ms = int((time.perf_counter() - started_at) * 1000)
        replied = False
        try:
            await message.reply(
                outcome.public_message,
                mention_author=False,
                allowed_mentions=discord.AllowedMentions(
                    users=[message.author],
                    roles=False,
                    everyone=False,
                    replied_user=False,
                ),
            )
            replied = True
        except (discord.Forbidden, discord.HTTPException, discord.NotFound):
            try:
                await message.channel.send(
                    outcome.public_message,
                    allowed_mentions=discord.AllowedMentions(
                        users=[message.author],
                        roles=False,
                        everyone=False,
                        replied_user=False,
                    ),
                )
                replied = True
            except (discord.Forbidden, discord.HTTPException, discord.NotFound):
                LOGGER.exception(
                    "Não foi possível entregar o retorno da ação executiva %s.",
                    action,
                )
        try:
            await self.audit.record_executive_action(
                message,
                action=action,
                target=outcome.target,
                roles=outcome.roles,
                result="Sucesso" if outcome.success else "Falha",
                reason=outcome.audit_reason,
                duration_ms=duration_ms,
                notes=outcome.notes,
            )
        except Exception:
            LOGGER.exception(
                "Falha isolada ao registrar auditoria da ação executiva %s.",
                action,
            )
        if not replied:
            await self.state.update_section(
                "executive_delivery_failures",
                f"{message.guild.id}:{message.id}",
                {
                    "action": action,
                    "executor_id": message.author.id,
                    "public_message": outcome.public_message[:1900],
                    "occurred_at": datetime.now(timezone.utc).isoformat(),
                },
            )

    async def _role_add(
        self,
        message: discord.Message,
        request: ExecutiveRequest,
    ) -> ExecutiveOutcome:
        target, error = self._target(message.guild, request)
        role, role_error = self._single_role(message.guild, request)
        reason = error or role_error or self._validate_role_action(
            message.guild,
            target,
            role,
        )
        if reason:
            return self._failure(message, reason, target, (role,) if role else ())
        assert target and role
        if role in target.roles:
            return self._success(
                message,
                f"O cargo {_display(role.name)} já está atribuído a "
                f"{_display(target.display_name)}, Senhor {message.author.mention}.",
                "O usuário já possuía o cargo; nenhuma alteração foi necessária.",
                target,
                (role,),
            )
        await target.add_roles(role, reason=self._discord_reason(message))
        return self._success(
            message,
            f"✨ Tudo pronto, Senhor {message.author.mention}. O cargo "
            f"{_display(role.name)} foi atribuído ao usuário "
            f"{_display(target.display_name)} conforme solicitado.",
            "Cargo atribuído com sucesso.",
            target,
            (role,),
        )

    async def _role_remove(
        self,
        message: discord.Message,
        request: ExecutiveRequest,
    ) -> ExecutiveOutcome:
        target, error = self._target(message.guild, request)
        role, role_error = self._single_role(message.guild, request)
        reason = error or role_error or self._validate_role_action(
            message.guild,
            target,
            role,
        )
        if reason:
            return self._failure(message, reason, target, (role,) if role else ())
        assert target and role
        if role not in target.roles:
            return self._success(
                message,
                f"O usuário {_display(target.display_name)} não possui o cargo "
                f"{_display(role.name)}, Senhor {message.author.mention}.",
                "O usuário não possuía o cargo; nenhuma alteração foi necessária.",
                target,
                (role,),
            )
        await target.remove_roles(role, reason=self._discord_reason(message))
        return self._success(
            message,
            f"✨ Entendido, Senhor {message.author.mention}. O cargo "
            f"{_display(role.name)} foi removido do usuário "
            f"{_display(target.display_name)} conforme solicitado.",
            "Cargo removido com sucesso.",
            target,
            (role,),
        )

    async def _role_swap(
        self,
        message: discord.Message,
        request: ExecutiveRequest,
    ) -> ExecutiveOutcome:
        target, error = self._target(message.guild, request)
        roles = tuple(
            role
            for role_id in request.role_ids[:2]
            if (role := message.guild.get_role(role_id)) is not None
        )
        if error or len(roles) != 2:
            return self._failure(
                message,
                error or "A troca exige exatamente dois cargos válidos.",
                target,
                roles,
            )
        old_role, new_role = roles
        reason = self._validate_role_action(message.guild, target, old_role)
        reason = reason or self._validate_role_action(
            message.guild,
            target,
            new_role,
        )
        if target and old_role not in target.roles:
            reason = reason or "O usuário não possui o cargo que seria substituído."
        if reason:
            return self._failure(message, reason, target, roles)
        assert target
        if new_role not in target.roles:
            await target.add_roles(new_role, reason=self._discord_reason(message))
        await target.remove_roles(old_role, reason=self._discord_reason(message))
        return self._success(
            message,
            f"✨ Concluído, Senhor {message.author.mention}. O cargo "
            f"{_display(old_role.name)} foi substituído por "
            f"{_display(new_role.name)} para {_display(target.display_name)}.",
            "Troca de cargos concluída com sucesso.",
            target,
            roles,
        )

    async def _role_list(
        self,
        message: discord.Message,
        request: ExecutiveRequest,
    ) -> ExecutiveOutcome:
        target = (
            message.guild.get_member(request.target_id)
            if request.target_id
            else message.author
        )
        if target is None:
            return self._failure(message, "Usuário alvo não encontrado.")
        roles = tuple(role for role in reversed(target.roles) if not role.is_default())
        role_text = "\n".join(f"• {role.name}" for role in roles) or "Nenhum cargo."
        try:
            await message.author.send(
                f"Cargos de **{_display(target.display_name)}**:\n{role_text}"
            )
        except (discord.Forbidden, discord.HTTPException):
            return self._failure(
                message,
                "Não foi possível enviar a relação reservadamente.",
                target,
                roles,
            )
        return self._success(
            message,
            f"✨ Certamente, Senhor {message.author.mention}. Enviei a relação "
            f"de cargos de {_display(target.display_name)} em uma mensagem privada.",
            "Relação de cargos enviada por mensagem privada.",
            target,
            roles,
        )

    async def _ban(
        self,
        message: discord.Message,
        request: ExecutiveRequest,
    ) -> ExecutiveOutcome:
        target, error = self._target(message.guild, request)
        if target == message.author:
            error = error or "O executor não pode banir a própria conta."
        reason = error or self._validate_member_action(
            message.guild,
            target,
            "ban_members",
        )
        if reason:
            return self._failure(message, reason, target)
        assert target
        await message.guild.ban(
            target,
            reason=self._discord_reason(message, request.reason),
            delete_message_seconds=0,
        )
        if request.duration_seconds:
            await self.state.update_section(
                "temporary_bans",
                f"{message.guild.id}:{target.id}",
                {
                    "guild_id": message.guild.id,
                    "user_id": target.id,
                    "expires_at": (
                        datetime.now(timezone.utc)
                        + timedelta(seconds=request.duration_seconds)
                    ).isoformat(),
                    "reason": request.reason,
                },
            )
        duration = (
            _duration_text(request.duration_seconds)
            if request.duration_seconds
            else "permanentemente"
        )
        return self._success(
            message,
            f"Entendido, Senhor {message.author.mention}. O usuário "
            f"{_display(target.display_name)} foi banido {duration}.",
            f"Usuário banido {duration}. Motivo: {request.reason}",
            target,
            notes=f"Motivo: {request.reason}; duração: {duration}.",
        )

    async def _unban(
        self,
        message: discord.Message,
        request: ExecutiveRequest,
    ) -> ExecutiveOutcome:
        if request.target_id is None:
            return self._failure(
                message,
                "Informe o ID do usuário cujo banimento deve ser removido.",
            )
        bot_member = message.guild.me
        if bot_member is None or not bot_member.guild_permissions.ban_members:
            return self._failure(
                message,
                "O bot não possui a permissão Banir Membros para revogar o banimento.",
            )
        target_object = discord.Object(id=request.target_id)
        try:
            ban_entry = await message.guild.fetch_ban(target_object)
        except discord.NotFound:
            return self._failure(
                message,
                f"O usuário de ID {request.target_id} não está banido neste servidor.",
            )
        target = ban_entry.user
        await message.guild.unban(
            target,
            reason=self._discord_reason(message, request.reason),
        )
        await self._clear_temporary_ban(message.guild.id, target.id)

        invite_url, invite_error = await self._create_reentry_invite(message)
        dm_delivered = False
        dm_error = ""
        dm_text = (
            f"Seu banimento no servidor **{_display(message.guild.name)}** foi "
            "revogado pelo proprietário."
        )
        if invite_url:
            dm_text += (
                "\n\nVocê pode retornar usando este convite individual, válido "
                f"por 7 dias e para um único uso:\n{invite_url}"
            )
        else:
            dm_text += (
                "\n\nNão consegui gerar automaticamente o convite de retorno. "
                "Entre em contato com a administração para recebê-lo."
            )
        try:
            await target.send(dm_text)
            dm_delivered = True
        except (discord.Forbidden, discord.HTTPException) as exc:
            dm_error = f"{type(exc).__name__}: {exc}"

        target_name = _display(
            str(getattr(target, "display_name", None) or getattr(target, "name", target))
        )
        delivery = (
            "A pessoa foi avisada por DM e recebeu um convite individual."
            if dm_delivered and invite_url
            else (
                "A pessoa foi avisada por DM, mas o convite não pôde ser gerado."
                if dm_delivered
                else "O banimento foi removido, mas a DM não pôde ser entregue."
            )
        )
        notes = (
            f"Usuário: {target_name} ({target.id}); DM: "
            f"{'entregue' if dm_delivered else 'não entregue'}; convite: "
            f"{'criado' if invite_url else 'não criado'}."
        )
        if invite_error:
            notes += f" Falha do convite: {invite_error}."
        if dm_error:
            notes += f" Falha da DM: {dm_error}."
        return self._success(
            message,
            f"Entendido, Senhor {message.author.mention}. O banimento de "
            f"{target_name} (`{target.id}`) foi revogado. {delivery}",
            "Banimento revogado com sucesso.",
            target,
            notes=notes,
        )

    async def _revoke_punishment(
        self,
        message: discord.Message,
        request: ExecutiveRequest,
    ) -> ExecutiveOutcome:
        if request.target_id is None:
            return self._failure(
                message,
                "Informe o usuário ou ID que terá a punição revogada.",
            )
        member = message.guild.get_member(request.target_id)
        if member is None:
            return await self._unban(message, request)
        reason = self._validate_member_action(
            message.guild,
            member,
            "moderate_members",
        )
        if reason:
            return self._failure(message, reason, member)
        await member.timeout(None, reason=self._discord_reason(message, request.reason))
        await self._clear_temporary_ban(message.guild.id, member.id)
        dm_delivered = True
        try:
            await member.send(
                f"As punições reversíveis aplicadas à sua conta no servidor "
                f"**{_display(message.guild.name)}** foram removidas pelo proprietário."
            )
        except (discord.Forbidden, discord.HTTPException):
            dm_delivered = False
        return self._success(
            message,
            f"Entendido, Senhor {message.author.mention}. As punições reversíveis "
            f"de {_display(member.display_name)} foram removidas.",
            "Punições reversíveis removidas com sucesso.",
            member,
            notes=f"DM: {'entregue' if dm_delivered else 'não entregue'}.",
        )

    async def _kick(
        self,
        message: discord.Message,
        request: ExecutiveRequest,
    ) -> ExecutiveOutcome:
        target, error = self._target(message.guild, request)
        if target == message.author:
            error = error or "O executor não pode expulsar a própria conta."
        reason = error or self._validate_member_action(
            message.guild,
            target,
            "kick_members",
        )
        if reason:
            return self._failure(message, reason, target)
        assert target
        await target.kick(reason=self._discord_reason(message))
        return self._success(
            message,
            f"⚠️ Entendido, Senhor {message.author.mention}. "
            f"{_display(target.display_name)} foi expulso do servidor.",
            "Usuário expulso com sucesso.",
            target,
        )

    async def _timeout(
        self,
        message: discord.Message,
        request: ExecutiveRequest,
    ) -> ExecutiveOutcome:
        target, error = self._target(message.guild, request)
        if target == message.author:
            error = error or "O executor não pode silenciar a própria conta."
        if request.duration_seconds is None:
            error = error or "Duração do silenciamento não informada."
        elif request.duration_seconds > 28 * 24 * 3600:
            error = error or "Duração superior ao limite de 28 dias."
        reason = error or self._validate_member_action(
            message.guild,
            target,
            "moderate_members",
        )
        if reason:
            return self._failure(message, reason, target)
        assert target and request.duration_seconds
        until = datetime.now(timezone.utc) + timedelta(
            seconds=request.duration_seconds
        )
        await target.timeout(until, reason=self._discord_reason(message))
        duration_text = _duration_text(request.duration_seconds)
        return self._success(
            message,
            f"🛡️ Entendido, Senhor {message.author.mention}. "
            f"{_display(target.display_name)} foi silenciado por "
            f"{duration_text} conforme solicitado.",
            f"Silenciamento aplicado por {duration_text}.",
            target,
        )

    async def _untimeout(
        self,
        message: discord.Message,
        request: ExecutiveRequest,
    ) -> ExecutiveOutcome:
        target, error = self._target(message.guild, request)
        reason = error or self._validate_member_action(
            message.guild,
            target,
            "moderate_members",
        )
        if reason:
            return self._failure(message, reason, target)
        assert target
        await target.timeout(None, reason=self._discord_reason(message))
        return self._success(
            message,
            f"🛡️ Tudo certo, Senhor {message.author.mention}. O silenciamento "
            f"de {_display(target.display_name)} foi removido.",
            "Silenciamento removido com sucesso.",
            target,
        )

    async def _voice_disconnect(
        self,
        message: discord.Message,
        request: ExecutiveRequest,
    ) -> ExecutiveOutcome:
        target, error = self._target(message.guild, request)
        reason = error or self._validate_member_action(
            message.guild,
            target,
            "move_members",
            hierarchy=False,
        )
        if not reason and target and target.voice is None:
            reason = "O usuário não está conectado a um canal de voz."
        if reason:
            return self._failure(message, reason, target)
        assert target
        await target.move_to(None, reason=self._discord_reason(message))
        return self._success(
            message,
            f"⚡ Certamente, Senhor {message.author.mention}. "
            f"{_display(target.display_name)} foi desconectado do canal de voz.",
            "Usuário desconectado do canal de voz.",
            target,
        )

    async def _purge_messages(
        self,
        message: discord.Message,
        request: ExecutiveRequest,
    ) -> ExecutiveOutcome:
        if not isinstance(message.channel, discord.TextChannel):
            return self._failure(
                message,
                "A limpeza precisa ser executada em um canal de texto.",
            )
        bot_member = message.guild.me
        if bot_member is None:
            return self._failure(message, "Membro do bot indisponível.")
        permissions = message.channel.permissions_for(bot_member)
        if not permissions.manage_messages:
            return self._failure(
                message,
                "O bot não possui a permissão Gerenciar Mensagens neste canal.",
            )
        amount = request.amount
        if amount is None:
            return self._failure(
                message,
                "Informe quantas mensagens devem ser removidas.",
            )
        if not 1 <= amount <= 1000:
            return self._failure(
                message,
                "A quantidade deve ficar entre 1 e 1000 mensagens.",
            )
        deleted = await message.channel.purge(
            limit=amount + 1,
            check=lambda item: item.id != message.id,
            reason=self._discord_reason(message),
        )
        removed = len(deleted)
        return self._success(
            message,
            f"Entendido, Senhor {message.author.mention}. "
            f"As últimas {removed} mensagens foram removidas com sucesso.",
            f"{removed} mensagens removidas do canal.",
            notes=f"Canal: {message.channel.name} ({message.channel.id}).",
        )

    async def _lock_channel(
        self,
        message: discord.Message,
        request: ExecutiveRequest,
    ) -> ExecutiveOutcome:
        del request
        if not isinstance(message.channel, discord.TextChannel):
            return self._failure(
                message,
                "Esta ação precisa ser executada em um canal de texto.",
            )
        bot_member = message.guild.me
        if bot_member is None or not message.channel.permissions_for(
            bot_member
        ).manage_channels:
            return self._failure(
                message,
                "O bot não possui a permissão Gerenciar Canais neste canal.",
            )
        overwrite = message.channel.overwrites_for(message.guild.default_role)
        overwrite.send_messages = False
        await message.channel.set_permissions(
            message.guild.default_role,
            overwrite=overwrite,
            reason=self._discord_reason(message),
        )
        return self._success(
            message,
            f"Entendido, Senhor {message.author.mention}. O canal foi trancado.",
            "Canal trancado com sucesso.",
            notes=f"Canal: {message.channel.name} ({message.channel.id}).",
        )

    async def _unlock_channel(
        self,
        message: discord.Message,
        request: ExecutiveRequest,
    ) -> ExecutiveOutcome:
        del request
        if not isinstance(message.channel, discord.TextChannel):
            return self._failure(
                message,
                "Esta ação precisa ser executada em um canal de texto.",
            )
        bot_member = message.guild.me
        if bot_member is None or not message.channel.permissions_for(
            bot_member
        ).manage_channels:
            return self._failure(
                message,
                "O bot não possui a permissão Gerenciar Canais neste canal.",
            )
        overwrite = message.channel.overwrites_for(message.guild.default_role)
        overwrite.send_messages = None
        await message.channel.set_permissions(
            message.guild.default_role,
            overwrite=overwrite,
            reason=self._discord_reason(message),
        )
        return self._success(
            message,
            f"Entendido, Senhor {message.author.mention}. O canal foi destrancado.",
            "Canal destrancado com sucesso.",
            notes=f"Canal: {message.channel.name} ({message.channel.id}).",
        )

    async def _rename_channel(
        self,
        message: discord.Message,
        request: ExecutiveRequest,
    ) -> ExecutiveOutcome:
        channel, error = self._selected_channel(message, request)
        name = _clean_display_name(request.name, limit=100)
        if error or channel is None:
            return self._failure(message, error or "Canal não encontrado.")
        if not name:
            return self._failure(message, "Informe o novo nome do canal.")
        permission = self._channel_manage_error(message.guild, channel)
        if permission:
            return self._failure(message, permission)
        previous_name = str(getattr(channel, "name", "canal"))
        await channel.edit(
            name=name,
            reason=self._discord_reason(message),
        )
        return self._success(
            message,
            f"Pronto, Senhor {message.author.mention}. O canal agora se chama "
            f"**{_display(name)}**.",
            "Canal renomeado com sucesso.",
            notes=(
                f"Canal: {previous_name} ({channel.id}) -> {name}."
            ),
        )

    async def _set_channel_topic(
        self,
        message: discord.Message,
        request: ExecutiveRequest,
    ) -> ExecutiveOutcome:
        channel, error = self._selected_channel(message, request)
        if error or not isinstance(channel, discord.TextChannel):
            return self._failure(
                message,
                error or "O tópico só pode ser alterado em canal de texto.",
            )
        permission = self._channel_manage_error(message.guild, channel)
        if permission:
            return self._failure(message, permission)
        topic = request.topic.strip()[:1024]
        if not topic:
            return self._failure(message, "Informe o novo tópico do canal.")
        await channel.edit(
            topic=topic,
            reason=self._discord_reason(message),
        )
        return self._success(
            message,
            f"Pronto, Senhor {message.author.mention}. O tópico de "
            f"{channel.mention} foi atualizado.",
            "Tópico do canal atualizado com sucesso.",
            notes=f"Canal: {channel.name} ({channel.id}).",
        )

    async def _clear_channel_topic(
        self,
        message: discord.Message,
        request: ExecutiveRequest,
    ) -> ExecutiveOutcome:
        channel, error = self._selected_channel(message, request)
        if error or not isinstance(channel, discord.TextChannel):
            return self._failure(
                message,
                error or "O tópico só pode ser removido de canal de texto.",
            )
        permission = self._channel_manage_error(message.guild, channel)
        if permission:
            return self._failure(message, permission)
        await channel.edit(topic=None, reason=self._discord_reason(message))
        return self._success(
            message,
            f"Pronto, Senhor {message.author.mention}. O tópico de "
            f"{channel.mention} foi removido.",
            "Tópico do canal removido com sucesso.",
            notes=f"Canal: {channel.name} ({channel.id}).",
        )

    async def _set_channel_slowmode(
        self,
        message: discord.Message,
        request: ExecutiveRequest,
    ) -> ExecutiveOutcome:
        channel, error = self._selected_channel(message, request)
        if error or not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return self._failure(
                message,
                error or "O modo lento exige um canal de texto ou thread.",
            )
        permission = self._channel_manage_error(message.guild, channel)
        if permission:
            return self._failure(message, permission)
        seconds = 0 if request.duration_seconds is None else request.duration_seconds
        if not 0 <= seconds <= 21600:
            return self._failure(
                message,
                "O modo lento deve ficar entre 0 e 21600 segundos.",
            )
        await channel.edit(
            slowmode_delay=seconds,
            reason=self._discord_reason(message),
        )
        state_text = "desativado" if seconds == 0 else f"definido para {seconds}s"
        return self._success(
            message,
            f"Pronto, Senhor {message.author.mention}. O modo lento foi "
            f"{state_text}.",
            "Modo lento atualizado com sucesso.",
            notes=f"Canal: {channel.name} ({channel.id}); segundos: {seconds}.",
        )

    async def _create_text_channel(
        self,
        message: discord.Message,
        request: ExecutiveRequest,
    ) -> ExecutiveOutcome:
        return await self._create_channel(message, request, voice=False)

    async def _create_voice_channel(
        self,
        message: discord.Message,
        request: ExecutiveRequest,
    ) -> ExecutiveOutcome:
        return await self._create_channel(message, request, voice=True)

    async def _create_channel(
        self,
        message: discord.Message,
        request: ExecutiveRequest,
        *,
        voice: bool,
    ) -> ExecutiveOutcome:
        bot_member = message.guild.me
        if bot_member is None or not bot_member.guild_permissions.manage_channels:
            return self._failure(
                message,
                "O bot não possui a permissão Gerenciar Canais.",
            )
        name = _clean_channel_name(request.name)
        if not name:
            return self._failure(message, "Informe o nome do novo canal.")
        category: discord.CategoryChannel | None = None
        for channel_id in request.channel_ids:
            candidate = message.guild.get_channel(channel_id)
            if isinstance(candidate, discord.CategoryChannel):
                category = candidate
                break
        reason = self._discord_reason(message)
        if voice:
            created = await message.guild.create_voice_channel(
                name,
                category=category,
                reason=reason,
            )
        else:
            created = await message.guild.create_text_channel(
                name,
                category=category,
                topic=request.topic.strip()[:1024] or None,
                reason=reason,
            )
        return self._success(
            message,
            f"Pronto, Senhor {message.author.mention}. O canal "
            f"{created.mention} foi criado.",
            "Canal criado com sucesso.",
            notes=f"Canal: {created.name} ({created.id}).",
        )

    async def _delete_channel(
        self,
        message: discord.Message,
        request: ExecutiveRequest,
    ) -> ExecutiveOutcome:
        channel, error = self._selected_channel(message, request)
        if error or channel is None:
            return self._failure(message, error or "Canal não encontrado.")
        permission = self._channel_manage_error(message.guild, channel)
        if permission:
            return self._failure(message, permission)
        name = str(getattr(channel, "name", "canal"))
        channel_id = channel.id
        await channel.delete(reason=self._discord_reason(message))
        return self._success(
            message,
            f"Pronto, Senhor {message.author.mention}. O canal "
            f"**{_display(name)}** foi excluído.",
            "Canal excluído com sucesso.",
            notes=f"Canal excluído: {name} ({channel_id}).",
        )

    async def _send_message(
        self,
        message: discord.Message,
        request: ExecutiveRequest,
    ) -> ExecutiveOutcome:
        if (
            request.target_id is not None
            and not request.channel_ids
            and _wants_direct_message(request.normalized_text)
        ):
            target, error = self._target(message.guild, request)
            if error or target is None:
                return self._failure(message, error or "Usuário alvo não encontrado.")
            text = request.text.strip()
            if not text:
                return self._failure(message, "Informe a mensagem que deve ser enviada.")
            if len(text) > 2000:
                return self._failure(
                    message,
                    "A mensagem ultrapassa o limite de 2000 caracteres do Discord.",
                )
            dm_text = re.sub(
                rf"<@!?{target.id}>[:,\s]*",
                "",
                text,
                count=1,
            ).strip()
            dm_text = await self._compose_relay_notice(
                dm_text or text,
                target=target,
                author_name=message.author.display_name,
                direct=True,
                channel=None,
            )
            try:
                await target.send(
                    dm_text,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except (discord.Forbidden, discord.HTTPException):
                return self._failure(
                    message,
                    "Não consegui enviar DM para esse usuário. Talvez ele esteja com mensagens privadas bloqueadas.",
                    target,
                )
            return self._success(
                message,
                f"Pronto, Senhor {message.author.mention}. Avisei {_display(target.display_name)} por mensagem privada.",
                "Mensagem enviada por DM com sucesso.",
                target,
                notes=f"Usuário: {target.id}.",
            )
        channel, error = self._selected_channel(message, request)
        if error or not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return self._failure(
                message,
                error or "O destino precisa ser um canal de texto ou thread.",
            )
        bot_member = message.guild.me
        if bot_member is None:
            return self._failure(message, "Membro do bot indisponível.")
        permissions = channel.permissions_for(bot_member)
        can_send = (
            permissions.send_messages_in_threads
            if isinstance(channel, discord.Thread)
            else permissions.send_messages
        )
        if not permissions.view_channel or not can_send:
            return self._failure(
                message,
                "O bot não pode enviar mensagens no canal selecionado.",
            )
        text = request.text.strip()
        if request.target_id is not None:
            target = message.guild.get_member(request.target_id)
            if target is not None:
                text = await self._compose_relay_notice(
                    text,
                    target=target,
                    author_name=message.author.display_name,
                    direct=False,
                    channel=channel,
                )
        if not text:
            return self._failure(message, "Informe a mensagem que deve ser enviada.")
        if len(text) > 2000:
            return self._failure(
                message,
                "A mensagem ultrapassa o limite de 2000 caracteres do Discord.",
            )
        sent = await channel.send(
            text,
            allowed_mentions=discord.AllowedMentions(
                users=True,
                roles=True,
                everyone=False,
            ),
        )
        return self._success(
            message,
            f"Pronto, Senhor {message.author.mention}. A mensagem foi enviada em "
            f"{channel.mention}.",
            "Mensagem enviada com sucesso.",
            notes=f"Canal: {channel.name} ({channel.id}); mensagem: {sent.id}.",
        )

    async def _compose_relay_notice(
        self,
        value: str,
        *,
        target: discord.Member,
        author_name: str,
        direct: bool,
        channel: discord.TextChannel | discord.Thread | None,
    ) -> str:
        fallback = (
            _owner_relay_text(value, author_name=author_name, direct=True)
            if direct
            else _channel_relay_text(value, target_id=target.id, author_name=author_name)
        )
        if self.text_generator is None:
            return fallback
        raw_body = re.sub(
            rf"^<@!?{target.id}>[:,\s]*",
            "",
            str(value or ""),
            count=1,
        ).strip()
        body = _strip_relay_connectors(_clean_message_content(raw_body))
        if not body:
            return fallback
        required_mentions = _relay_required_mentions(body, target_id=target.id)
        prompt = _relay_notice_prompt(
            body=body,
            target=target,
            author_name=author_name,
            direct=direct,
            channel=channel,
            required_mentions=required_mentions,
        )
        try:
            generated = await self.text_generator(
                prompt,
                max_output_tokens=180,
                timeout_seconds=5,
                use_cache=False,
            )
        except Exception as exc:
            LOGGER.warning(
                "Falha ao gerar aviso por IA; usando fallback local: %s: %s",
                type(exc).__name__,
                exc,
            )
            return fallback
        notice = _clean_generated_relay_notice(
            generated,
            target_id=target.id,
            direct=direct,
            required_mentions=required_mentions,
            delivery_channel=channel,
        )
        return notice or fallback

    async def _pin_message(
        self,
        message: discord.Message,
        request: ExecutiveRequest,
    ) -> ExecutiveOutcome:
        del request
        target, error = await self._referenced_message(message)
        if error or target is None:
            return self._failure(message, error or "Mensagem não encontrada.")
        bot_member = message.guild.me
        if (
            bot_member is None
            or not target.channel.permissions_for(bot_member).manage_messages
        ):
            return self._failure(
                message,
                "O bot não possui a permissão Gerenciar Mensagens neste canal.",
            )
        await target.pin(reason=self._discord_reason(message))
        return self._success(
            message,
            f"Pronto, Senhor {message.author.mention}. A mensagem foi fixada.",
            "Mensagem fixada com sucesso.",
            notes=f"Mensagem: {target.id}; canal: {target.channel.id}.",
        )

    async def _unpin_message(
        self,
        message: discord.Message,
        request: ExecutiveRequest,
    ) -> ExecutiveOutcome:
        del request
        target, error = await self._referenced_message(message)
        if error or target is None:
            return self._failure(message, error or "Mensagem não encontrada.")
        bot_member = message.guild.me
        if (
            bot_member is None
            or not target.channel.permissions_for(bot_member).manage_messages
        ):
            return self._failure(
                message,
                "O bot não possui a permissão Gerenciar Mensagens neste canal.",
            )
        await target.unpin(reason=self._discord_reason(message))
        return self._success(
            message,
            f"Pronto, Senhor {message.author.mention}. A mensagem foi desafixada.",
            "Mensagem desafixada com sucesso.",
            notes=f"Mensagem: {target.id}; canal: {target.channel.id}.",
        )

    async def _create_role(
        self,
        message: discord.Message,
        request: ExecutiveRequest,
    ) -> ExecutiveOutcome:
        bot_member = message.guild.me
        if bot_member is None or not bot_member.guild_permissions.manage_roles:
            return self._failure(
                message,
                "O bot não possui a permissão Gerenciar Cargos.",
            )
        name = _clean_display_name(request.name, limit=100)
        if not name:
            return self._failure(message, "Informe o nome do novo cargo.")
        role = await message.guild.create_role(
            name=name,
            reason=self._discord_reason(message),
        )
        return self._success(
            message,
            f"Pronto, Senhor {message.author.mention}. O cargo "
            f"**{_display(role.name)}** foi criado.",
            "Cargo criado com sucesso.",
            roles=(role,),
            notes=f"Cargo: {role.name} ({role.id}).",
        )

    async def _rename_role(
        self,
        message: discord.Message,
        request: ExecutiveRequest,
    ) -> ExecutiveOutcome:
        role, error = self._single_role(message.guild, request)
        reason = error or self._validate_role_manage(message.guild, role)
        if reason:
            return self._failure(message, reason, roles=(role,) if role else ())
        name = _clean_display_name(request.name, limit=100)
        if not name:
            return self._failure(message, "Informe o novo nome do cargo.")
        assert role
        previous_name = role.name
        await role.edit(name=name, reason=self._discord_reason(message))
        return self._success(
            message,
            f"Pronto, Senhor {message.author.mention}. O cargo agora se chama "
            f"**{_display(name)}**.",
            "Cargo renomeado com sucesso.",
            roles=(role,),
            notes=f"Cargo: {previous_name} ({role.id}) -> {name}.",
        )

    async def _delete_role(
        self,
        message: discord.Message,
        request: ExecutiveRequest,
    ) -> ExecutiveOutcome:
        role, error = self._single_role(message.guild, request)
        reason = error or self._validate_role_manage(message.guild, role)
        if reason:
            return self._failure(message, reason, roles=(role,) if role else ())
        assert role
        role_id = role.id
        name = role.name
        await role.delete(reason=self._discord_reason(message))
        return self._success(
            message,
            f"Pronto, Senhor {message.author.mention}. O cargo "
            f"**{_display(name)}** foi excluído.",
            "Cargo excluído com sucesso.",
            notes=f"Cargo excluído: {name} ({role_id}).",
        )

    async def _set_nickname(
        self,
        message: discord.Message,
        request: ExecutiveRequest,
    ) -> ExecutiveOutcome:
        target, error = self._target(message.guild, request)
        reason = error or self._validate_member_action(
            message.guild,
            target,
            "manage_nicknames",
        )
        if reason:
            return self._failure(message, reason, target)
        name = _clean_display_name(request.name, limit=32)
        if not name:
            return self._failure(message, "Informe o novo apelido do usuário.")
        assert target
        await target.edit(nick=name, reason=self._discord_reason(message))
        return self._success(
            message,
            f"Pronto, Senhor {message.author.mention}. O apelido de "
            f"{_display(target.name)} foi alterado para **{_display(name)}**.",
            "Apelido alterado com sucesso.",
            target,
        )

    async def _reset_nickname(
        self,
        message: discord.Message,
        request: ExecutiveRequest,
    ) -> ExecutiveOutcome:
        target, error = self._target(message.guild, request)
        reason = error or self._validate_member_action(
            message.guild,
            target,
            "manage_nicknames",
        )
        if reason:
            return self._failure(message, reason, target)
        assert target
        await target.edit(nick=None, reason=self._discord_reason(message))
        return self._success(
            message,
            f"Pronto, Senhor {message.author.mention}. O apelido de "
            f"{_display(target.name)} foi restaurado.",
            "Apelido restaurado com sucesso.",
            target,
        )

    async def _move_voice(
        self,
        message: discord.Message,
        request: ExecutiveRequest,
    ) -> ExecutiveOutcome:
        target, error = self._target(message.guild, request)
        reason = error or self._validate_member_action(
            message.guild,
            target,
            "move_members",
            hierarchy=False,
        )
        voice_channels = [
            message.guild.get_channel(channel_id)
            for channel_id in request.channel_ids
        ]
        destination = next(
            (
                channel
                for channel in voice_channels
                if isinstance(channel, discord.VoiceChannel)
            ),
            None,
        )
        if reason:
            return self._failure(message, reason, target)
        if destination is None:
            return self._failure(
                message,
                "Informe exatamente um canal de voz de destino.",
                target,
            )
        if target is None or target.voice is None:
            return self._failure(
                message,
                "O usuário não está conectado a um canal de voz.",
                target,
            )
        await target.move_to(destination, reason=self._discord_reason(message))
        return self._success(
            message,
            f"Pronto, Senhor {message.author.mention}. "
            f"{_display(target.display_name)} foi movido para "
            f"**{_display(destination.name)}**.",
            "Usuário movido no canal de voz.",
            target,
            notes=f"Destino: {destination.name} ({destination.id}).",
        )

    async def _create_scheduled_event(
        self,
        message: discord.Message,
        request: ExecutiveRequest,
    ) -> ExecutiveOutcome:
        bot_member = message.guild.me
        if bot_member is None:
            return self._failure(message, "Membro do bot indisponível.")
        if not bot_member.guild_permissions.manage_events:
            return self._failure(
                message,
                "O bot não possui a permissão Gerenciar Eventos.",
            )
        if len(request.channel_ids) != 1:
            return self._failure(
                message,
                "Mencione exatamente um canal de voz para o evento.",
            )
        channel = message.guild.get_channel(request.channel_ids[0])
        if not isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
            return self._failure(
                message,
                "O canal mencionado precisa ser de voz ou palco.",
            )
        if not request.event_name:
            return self._failure(
                message,
                "Informe o nome do evento após a palavra chamado.",
            )
        start_time = _scheduled_datetime(request.scheduled_for)
        if start_time is None:
            return self._failure(
                message,
                "Informe quando o evento começa, por exemplo: amanhã às 20h.",
            )
        scheduled = await message.guild.create_scheduled_event(
            name=request.event_name[:100],
            start_time=start_time,
            channel=channel,
            privacy_level=discord.PrivacyLevel.guild_only,
            description=(
                f"Evento criado por solicitação de {message.author.display_name}."
            ),
            reason=self._discord_reason(message),
        )
        return self._success(
            message,
            f"Entendido, Senhor {message.author.mention}. O evento "
            f"{_display(scheduled.name)} foi criado para "
            f"<t:{int(start_time.timestamp())}:F> em {channel.mention}.",
            "Evento agendado com sucesso.",
            notes=f"Evento: {scheduled.id}; canal: {channel.id}.",
        )

    async def _ticket_create(
        self,
        message: discord.Message,
        request: ExecutiveRequest,
    ) -> ExecutiveOutcome:
        return self._failure(
            message,
            "O atendimento agora usa Threads privadas. Configure e publique o "
            "painel com /setup-ticket.",
        )

    async def _ticket_close(
        self,
        message: discord.Message,
        request: ExecutiveRequest,
    ) -> ExecutiveOutcome:
        return await self._set_ticket_state(message, request, reopen=False)

    async def _ticket_reopen(
        self,
        message: discord.Message,
        request: ExecutiveRequest,
    ) -> ExecutiveOutcome:
        return await self._set_ticket_state(message, request, reopen=True)

    async def _configure_feedback_channel(
        self,
        message: discord.Message,
        request: ExecutiveRequest,
    ) -> ExecutiveOutcome:
        return await self._configure_text_channel(
            message,
            request,
            state_key="feedback_channel_id",
            label="feedbacks",
        )

    async def _configure_change_channel(
        self,
        message: discord.Message,
        request: ExecutiveRequest,
    ) -> ExecutiveOutcome:
        return await self._configure_text_channel(
            message,
            request,
            state_key="change_channel_id",
            label="alterações",
        )

    async def _configure_report_channel(
        self,
        message: discord.Message,
        request: ExecutiveRequest,
    ) -> ExecutiveOutcome:
        return await self._configure_text_channel(
            message,
            request,
            state_key="report_channel_id",
            label="relatórios",
        )

    async def _configure_weekly_channel(
        self,
        message: discord.Message,
        request: ExecutiveRequest,
    ) -> ExecutiveOutcome:
        return await self._configure_text_channel(
            message,
            request,
            state_key="weekly_report_channel_id",
            label="relatório semanal",
        )

    async def _configure_welcome_channel(
        self,
        message: discord.Message,
        request: ExecutiveRequest,
    ) -> ExecutiveOutcome:
        outcome = await self._configure_text_channel(
            message,
            request,
            state_key="welcome_channel_id",
            label="recepção",
        )
        if outcome.success:
            await self.state.set("welcome_enabled", True)
        return outcome

    async def _configure_audit_channel(
        self,
        message: discord.Message,
        request: ExecutiveRequest,
    ) -> ExecutiveOutcome:
        channel, error = self._mentioned_text_channel(message.guild, request)
        if error:
            return self._failure(message, error)
        assert channel
        current_configs = await self.state.get_section("guild_audit_config")
        current = current_configs.get(str(message.guild.id), {})
        if not isinstance(current, dict):
            current = {}
        current.update(
            {
                "audit_log_channel_id": channel.id,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        await self.state.update_section(
            "guild_audit_config",
            str(message.guild.id),
            current,
        )
        return self._success(
            message,
            f"✨ Canal de auditoria configurado para {channel.mention}, "
            f"Senhor {message.author.mention}.",
            "Canal de auditoria configurado com sucesso.",
            notes=f"Canal configurado: {channel.name} ({channel.id}).",
        )

    async def _memory_set(
        self,
        message: discord.Message,
        request: ExecutiveRequest,
    ) -> ExecutiveOutcome:
        key = _memory_key(request.memory_key)
        value = " ".join(request.memory_value.split()).strip()
        if not key or not value:
            return self._failure(
                message,
                "A memória exige um nome e um valor não vazio.",
            )
        if len(value) > 500:
            return self._failure(
                message,
                "O valor da memória excede 500 caracteres.",
            )
        if _contains_sensitive_memory(f"{key} {value}"):
            return self._failure(
                message,
                "A memória parece conter uma credencial ou segredo.",
            )
        await self.state.update_section(
            "executive_memory",
            f"{message.guild.id}:{key}",
            {
                "key": key,
                "value": value,
                "updated_by": message.author.id,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        return self._success(
            message,
            f"Entendido, Senhor {message.author.mention}. A preferência "
            f"{_display(key)} foi registrada.",
            "Memória administrativa persistente registrada.",
            notes=f"Chave registrada: {key}.",
        )

    async def _memory_delete(
        self,
        message: discord.Message,
        request: ExecutiveRequest,
    ) -> ExecutiveOutcome:
        key = _memory_key(request.memory_key)
        if not key:
            return self._failure(message, "A memória a remover não foi informada.")
        storage_key = f"{message.guild.id}:{key}"
        records = await self.state.get_section("executive_memory")
        if storage_key not in records:
            return self._failure(message, "A memória informada não existe.")

        def remove(values: dict[str, object]) -> None:
            values.pop(storage_key, None)

        await self.state.mutate_section("executive_memory", remove)
        return self._success(
            message,
            f"Concluído, Senhor {message.author.mention}. A preferência "
            f"{_display(key)} foi removida.",
            "Memória administrativa removida.",
            notes=f"Chave removida: {key}.",
        )

    async def _memory_list(
        self,
        message: discord.Message,
        request: ExecutiveRequest,
    ) -> ExecutiveOutcome:
        records = await self.state.get_section("executive_memory")
        prefix = f"{message.guild.id}:"
        values = [
            item
            for storage_key, item in sorted(records.items())
            if storage_key.startswith(prefix) and isinstance(item, dict)
        ]
        lines = [
            f"• **{_display(str(item.get('key', 'preferência')))}:** "
            f"{_display(str(item.get('value', '')))}"
            for item in values
        ]
        try:
            await message.author.send(
                "Memórias administrativas da Bella:\n"
                + ("\n".join(lines) if lines else "Nenhuma memória registrada.")
            )
        except (discord.Forbidden, discord.HTTPException):
            return self._failure(
                message,
                "Não foi possível enviar as memórias reservadamente.",
            )
        return self._success(
            message,
            f"Certamente, Senhor {message.author.mention}. Enviei as memórias "
            "administrativas em uma mensagem privada.",
            "Memórias administrativas enviadas por mensagem privada.",
        )

    async def _configure_text_channel(
        self,
        message: discord.Message,
        request: ExecutiveRequest,
        *,
        state_key: str,
        label: str,
    ) -> ExecutiveOutcome:
        channel, error = self._mentioned_text_channel(message.guild, request)
        if error:
            return self._failure(message, error)
        assert channel
        await self.state.set(state_key, channel.id)
        return self._success(
            message,
            f"✨ Canal de {label} definido para {channel.mention}, "
            f"Senhor {message.author.mention}.",
            f"Canal de {label} configurado com sucesso.",
            notes=f"Canal configurado: {channel.name} ({channel.id}).",
        )

    @staticmethod
    def _mentioned_text_channel(
        guild: discord.Guild,
        request: ExecutiveRequest,
    ) -> tuple[discord.TextChannel | None, str | None]:
        if len(request.channel_ids) != 1:
            return None, "A configuração exige exatamente um canal mencionado."
        channel = guild.get_channel(request.channel_ids[0])
        if not isinstance(channel, discord.TextChannel):
            return None, "O canal mencionado não é um canal de texto válido."
        bot_member = guild.me
        if bot_member is None:
            return None, "Membro do bot indisponível no servidor."
        permissions = channel.permissions_for(bot_member)
        if not permissions.view_channel or not permissions.send_messages:
            return None, "O bot não pode visualizar ou enviar mensagens no canal."
        return channel, None

    async def _set_ticket_state(
        self,
        message: discord.Message,
        request: ExecutiveRequest,
        *,
        reopen: bool,
    ) -> ExecutiveOutcome:
        target, error = self._target(message.guild, request, required=False)
        record: dict[str, object] | None = None
        if target:
            record = await self.tickets.get(message.guild.id, target.id)
        elif isinstance(message.channel, discord.TextChannel):
            found = await self.tickets.find_by_channel(
                message.guild.id,
                message.channel.id,
            )
            if found:
                target = message.guild.get_member(found[0])
                record = found[1]
        if error or target is None or record is None:
            return self._failure(
                message,
                error or "Ticket ou usuário alvo não encontrado.",
                target,
            )
        channel = message.guild.get_channel(int(record.get("channel_id", 0)))
        if not isinstance(channel, discord.TextChannel):
            return self._failure(
                message,
                "O canal persistido do ticket não existe.",
                target,
            )
        current_status = str(record.get("status", "open"))
        desired_status = "open" if reopen else "closed"
        if current_status == desired_status:
            state_text = "aberto" if reopen else "fechado"
            return self._success(
                message,
                f"O ticket de {_display(target.display_name)} já está "
                f"{state_text}, Senhor {message.author.mention}.",
                f"O ticket já estava {state_text}.",
                target,
            )
        overwrite = channel.overwrites_for(target)
        overwrite.view_channel = True
        overwrite.read_message_history = True
        overwrite.send_messages = reopen
        await channel.set_permissions(
            target,
            overwrite=overwrite,
            reason=self._discord_reason(message),
        )
        base_name = channel.name.removeprefix("fechado-")
        new_name = base_name if reopen else f"fechado-{base_name}"
        await channel.edit(name=new_name[:100], reason=self._discord_reason(message))
        now = datetime.now(timezone.utc)
        record.update(
            {
                "status": desired_status,
                "updated_at": now.isoformat(),
                "updated_by": message.author.id,
            }
        )
        await self.tickets.save(message.guild.id, target.id, record)
        await channel.send(
            embed=discord.Embed(
                title="Ticket reaberto" if reopen else "Ticket encerrado",
                description=(
                    "O atendimento foi reaberto e o envio de mensagens foi restaurado."
                    if reopen
                    else "O atendimento foi encerrado e permanece disponível para consulta."
                ),
                color=(
                    discord.Color.from_rgb(46, 160, 67)
                    if reopen
                    else discord.Color.from_rgb(110, 118, 129)
                ),
                timestamp=now,
            )
        )
        verb = "reaberto" if reopen else "fechado"
        return self._success(
            message,
            f"✨ Tudo pronto, Senhor {message.author.mention}. O ticket de "
            f"{_display(target.display_name)} foi {verb}.",
            f"Ticket {verb} com sucesso.",
            target,
            notes=f"Canal: {channel.name} ({channel.id}).",
        )

    async def _ticket_category(
        self,
        guild: discord.Guild,
    ) -> discord.CategoryChannel:
        configs = await self.state.get_section("executive_ticket_categories")
        stored_id = configs.get(str(guild.id))
        if stored_id:
            channel = guild.get_channel(int(stored_id))
            if isinstance(channel, discord.CategoryChannel):
                return channel
        category = discord.utils.find(
            lambda item: item.name.casefold() in {"tickets", "atendimento", "suporte"},
            guild.categories,
        )
        if category is None:
            category = await guild.create_category(
                "Tickets",
                reason="Categoria automática de atendimento da Bella",
            )
        await self.state.update_section(
            "executive_ticket_categories",
            str(guild.id),
            category.id,
        )
        return category

    async def _create_reentry_invite(
        self,
        message: discord.Message,
    ) -> tuple[str | None, str]:
        configured_invite = str(
            getattr(self.settings, "server_invite_url", "")
        ).strip()
        if configured_invite.startswith(
            ("https://discord.gg/", "https://discord.com/invite/")
        ):
            return configured_invite, ""
        guild = message.guild
        bot_member = guild.me
        if bot_member is None:
            return None, "Membro do bot indisponível."
        candidates: list[object] = []
        for channel in (
            getattr(guild, "system_channel", None),
            message.channel,
            *getattr(guild, "text_channels", ()),
        ):
            if (
                channel is not None
                and channel not in candidates
                and hasattr(channel, "create_invite")
                and hasattr(channel, "permissions_for")
            ):
                candidates.append(channel)
        for channel in candidates:
            try:
                bot_permissions = channel.permissions_for(bot_member)
                default_permissions = channel.permissions_for(guild.default_role)
                if (
                    not getattr(bot_permissions, "create_instant_invite", False)
                    or not getattr(default_permissions, "view_channel", False)
                ):
                    continue
                invite = await channel.create_invite(
                    max_age=7 * 24 * 60 * 60,
                    max_uses=1,
                    unique=True,
                    reason=self._discord_reason(message, "Retorno após revogação de ban"),
                )
                return str(invite.url), ""
            except (discord.Forbidden, discord.HTTPException):
                continue
        return None, "Nenhum canal público permite criar convites."

    async def _clear_temporary_ban(self, guild_id: int, user_id: int) -> None:
        key = f"{guild_id}:{user_id}"

        def mutate(values: dict[str, object]) -> None:
            values.pop(key, None)

        await self.state.mutate_section("temporary_bans", mutate)

    def _target(
        self,
        guild: discord.Guild,
        request: ExecutiveRequest,
        *,
        required: bool = True,
    ) -> tuple[discord.Member | None, str | None]:
        if request.target_id is None:
            return (
                None,
                "A solicitação exige exatamente um usuário mencionado."
                if required
                else None,
            )
        target = guild.get_member(request.target_id)
        if target is None:
            return None, "Usuário alvo não encontrado no servidor."
        if self.bot.user and target.id == self.bot.user.id:
            return target, "A Bella não pode ser o alvo desta ação."
        return target, None

    @staticmethod
    def _single_role(
        guild: discord.Guild,
        request: ExecutiveRequest,
    ) -> tuple[discord.Role | None, str | None]:
        if len(request.role_ids) != 1:
            return None, "A solicitação exige exatamente um cargo mencionado."
        role = guild.get_role(request.role_ids[0])
        return (role, None) if role else (None, "Cargo alvo não encontrado.")

    @staticmethod
    def _validate_role_manage(
        guild: discord.Guild,
        role: discord.Role | None,
    ) -> str | None:
        bot_member = guild.me
        if role is None:
            return "Cargo alvo não encontrado."
        if bot_member is None:
            return "Membro do bot indisponível no servidor."
        if not bot_member.guild_permissions.manage_roles:
            return "O bot não possui a permissão Gerenciar Cargos."
        if role.is_default() or role.managed:
            return "O cargo informado não pode ser gerenciado manualmente."
        if role >= bot_member.top_role:
            return "O cargo está acima ou no mesmo nível do maior cargo do bot."
        return None

    @staticmethod
    def _channel_manage_error(
        guild: discord.Guild,
        channel: discord.abc.GuildChannel | discord.Thread,
    ) -> str | None:
        bot_member = guild.me
        if bot_member is None:
            return "Membro do bot indisponível no servidor."
        permissions = channel.permissions_for(bot_member)
        required = (
            permissions.manage_threads
            if isinstance(channel, discord.Thread)
            else permissions.manage_channels
        )
        if not required:
            permission_name = (
                "Gerenciar Threads"
                if isinstance(channel, discord.Thread)
                else "Gerenciar Canais"
            )
            return f"O bot não possui a permissão {permission_name}."
        return None

    @staticmethod
    def _selected_channel(
        message: discord.Message,
        request: ExecutiveRequest,
    ) -> tuple[
        discord.abc.GuildChannel | discord.Thread | None,
        str | None,
    ]:
        if len(request.channel_ids) > 1:
            return None, "Informe somente um canal para esta ação."
        if request.channel_ids:
            channel_id = request.channel_ids[0]
            getter = getattr(message.guild, "get_channel_or_thread", None)
            channel = (
                getter(channel_id)
                if callable(getter)
                else message.guild.get_channel(channel_id)
            )
            if channel is None:
                return None, "O canal selecionado não foi encontrado."
            return channel, None
        if isinstance(message.channel, (discord.abc.GuildChannel, discord.Thread)):
            return message.channel, None
        return None, "Esta ação precisa ser executada em um canal do servidor."

    @staticmethod
    async def _referenced_message(
        message: discord.Message,
    ) -> tuple[discord.Message | None, str | None]:
        reference = message.reference
        if reference is None or reference.message_id is None:
            return None, "Responda à mensagem que deseja fixar ou desafixar."
        if isinstance(reference.resolved, discord.Message):
            return reference.resolved, None
        try:
            return await message.channel.fetch_message(reference.message_id), None
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return None, "A mensagem respondida não pôde ser acessada."

    @staticmethod
    def _validate_role_action(
        guild: discord.Guild,
        target: discord.Member | None,
        role: discord.Role | None,
    ) -> str | None:
        bot_member = guild.me
        if target is None or role is None:
            return "Usuário ou cargo inválido."
        if bot_member is None:
            return "Membro do bot indisponível no servidor."
        if not bot_member.guild_permissions.manage_roles:
            return "O bot não possui a permissão Gerenciar Cargos."
        if role.is_default() or role.managed:
            return "O cargo informado não pode ser gerenciado manualmente."
        if role >= bot_member.top_role:
            return "O cargo está acima ou no mesmo nível do maior cargo do bot."
        if target == guild.owner or target.top_role >= bot_member.top_role:
            return "A hierarquia do bot não permite gerenciar o usuário alvo."
        return None

    @staticmethod
    def _validate_member_action(
        guild: discord.Guild,
        target: discord.Member | None,
        permission: str,
        *,
        hierarchy: bool = True,
    ) -> str | None:
        bot_member = guild.me
        if target is None:
            return "Usuário alvo inválido."
        if bot_member is None:
            return "Membro do bot indisponível no servidor."
        if not getattr(bot_member.guild_permissions, permission, False):
            return f"O bot não possui a permissão necessária: {permission}."
        if target == guild.owner:
            return "O proprietário do servidor não pode ser alvo desta ação."
        if hierarchy and target.top_role >= bot_member.top_role:
            return "A hierarquia do bot não permite agir sobre o usuário alvo."
        return None

    @staticmethod
    def _discord_reason(
        message: discord.Message,
        detail: str = "",
    ) -> str:
        reason = (
            "Solicitação executiva em linguagem natural por "
            f"{message.author} ({message.author.id})"
        )
        if detail:
            reason += f" | Motivo: {detail}"
        return reason[:512]

    @staticmethod
    def generic_failure(author: discord.abc.User) -> str:
        return (
            f"A ação não foi aplicada, Senhor {author.mention}, porque a validação "
            "de segurança não foi concluída. O diagnóstico ficou registrado."
        )

    def _failure(
        self,
        message: discord.Message,
        reason: str,
        target: discord.abc.User | None = None,
        roles: tuple[discord.Role, ...] = (),
    ) -> ExecutiveOutcome:
        return ExecutiveOutcome(
            success=False,
            public_message=(
                f"A ação foi interrompida, Senhor {message.author.mention}. {reason}"
            ),
            audit_reason=reason,
            target=target,
            roles=roles,
        )

    @staticmethod
    def _success(
        message: discord.Message,
        public_message: str,
        reason: str,
        target: discord.abc.User | None = None,
        roles: tuple[discord.Role, ...] = (),
        notes: str = "",
    ) -> ExecutiveOutcome:
        return ExecutiveOutcome(
            success=True,
            public_message=public_message,
            audit_reason=reason,
            target=target,
            roles=roles,
            notes=notes,
        )


def _attachment_summary(message: discord.Message) -> list[str]:
    """Nome, tipo e tamanho dos anexos da mensagem e da mensagem respondida."""
    attachments = list(getattr(message, "attachments", []) or [])
    reference = getattr(message, "reference", None)
    resolved = getattr(reference, "resolved", None) if reference else None
    if isinstance(resolved, discord.Message):
        attachments += list(resolved.attachments)
    return [
        f"{item.filename} ({item.content_type or 'tipo desconhecido'}, "
        f"{item.size / (1024 * 1024):.1f} MB)"
        for item in attachments[:5]
    ]


_COMPOUND_VERBS = re.compile(
    r"\b(?:cri[ae]r?|libera[r]?|libere|manda[r]?|mande|envi[ae]r?|"
    r"adiciona[r]?|adicione|d[aá]|coloca[r]?|coloque|tira[r]?|tire|remove[r]?|"
    r"bane?|banir|expulsa[r]?|silencia[r]?|tranca[r]?|tranque|renomei[ae]r?|"
    r"fixa[r]?|fixe|apaga[r]?|apague|avisa[r]?|avise|deixa[r]?|deixe|"
    r"move[r]?|mova|anota[r]?|faz|fa[cç]a)\b"
)


def _looks_compound(content: str) -> bool:
    """Pedido com duas ou mais ações ("cria X e manda Y")."""
    text = _normalize_text(re.sub(r"<[@#&!]*\d+>", " ", content))
    return len(_COMPOUND_VERBS.findall(text)) >= 2


def _normalize_text(value: str) -> str:
    decomposed = normalize("NFKD", value.casefold())
    ascii_text = "".join(char for char in decomposed if not combining(char))
    return " ".join(ascii_text.split())


def _explicit_user_id(
    content: str,
    *,
    excluded_ids: set[int] | None = None,
) -> int | None:
    excluded = set(excluded_ids or ())
    for pattern in (USER_MENTION_PATTERN, EXPLICIT_USER_ID_PATTERN):
        for match in pattern.finditer(content):
            try:
                user_id = int(match.group(1))
            except (TypeError, ValueError):
                continue
            if user_id not in excluded:
                return user_id
    return None


def _parse_requested_name(content: str, normalized_text: str, action: str) -> str:
    if action not in {
        "channel_rename",
        "channel_create_text",
        "channel_create_voice",
        "role_create",
        "role_rename",
        "nickname_set",
    }:
        return ""
    patterns = (
        r"(?:para|pra|como|chamar de|chame de|nome de)\s+(.+)$",
        r"(?:renomeie|renomeia|mude o nome|muda o nome|altere o nome|troque o nome)\s+(?:desse|deste|do|da|o|a)?\s*(?:canal|ticket|cargo|apelido|nick)?\s*(?:para|pra|como)?\s+(.+)$",
        r"(?:crie|criar|cria)\s+(?:um|uma)?\s*(?:canal|cargo)?\s*(?:chamado|chamada|com nome)?\s+(.+)$",
    )
    return _extract_free_text(content, patterns) or _extract_free_text(
        normalized_text,
        patterns,
    )


def _parse_requested_topic(content: str, normalized_text: str, action: str) -> str:
    if action != "channel_topic_set":
        return ""
    patterns = (
        r"(?:topico|tópico|descricao|descrição)\s+(?:para|pra|como|de)\s+(.+)$",
        r"(?:define|defina|mude|altere|coloque)\s+(?:o|a)?\s*(?:topico|tópico|descricao|descrição)\s+(.+)$",
    )
    return _extract_free_text(content, patterns) or _extract_free_text(
        normalized_text,
        patterns,
    )


def _parse_requested_message(
    content: str,
    normalized_text: str,
    action: str,
) -> str:
    if action != "message_send":
        return ""
    preserved = _strip_jarvis_reference(content)
    message = _extract_message_send_text(preserved)
    if message:
        return message
    patterns = (
        r"(?:mande|manda|envie|envia|enviar|avise|avisa|avisar|fala|fale|diz|diga)\s+(.+?)(?:\s+no canal|\s+em <#|\s+em #|$)",
    )
    return _extract_free_text(content, patterns) or _extract_free_text(
        normalized_text,
        patterns,
    )


def _extract_free_text(value: str, patterns: tuple[str, ...]) -> str:
    cleaned_source = MENTION_PATTERN.sub(" ", value)
    cleaned_source = re.sub(
        r"\bjarvis\b",
        " ",
        cleaned_source,
        flags=re.IGNORECASE,
    )
    for pattern in patterns:
        match = re.search(pattern, cleaned_source, flags=re.IGNORECASE | re.DOTALL)
        if not match:
            continue
        return _clean_display_name(match.group(1), limit=1024)
    return ""


def _strip_jarvis_reference(value: str) -> str:
    cleaned = re.sub(r"^\s*<@!?\d{15,22}>[:,\s-]*", " ", value, count=1)
    cleaned = re.sub(
        r"^\s*(?:senhor\s+)?jarvis\b[:,\s-]*",
        " ",
        cleaned,
        flags=re.IGNORECASE,
    )
    return " ".join(cleaned.split())


def _extract_message_send_text(value: str) -> str:
    source = " ".join(str(value or "").split())
    if not source:
        return ""
    destination_text = re.search(
        r"\b(?:mande|manda|envie|envia|enviar|publique|publica|avise|avisa|avisar|"
        r"diga|diz|fale|fala)\b.+?(?:<#\d{15,22}>|#[^\s:]+)"
        r"\s*(?::|\s[-–—]\s)(?P<body>.+)$",
        source,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if destination_text:
        return _clean_message_content(destination_text.group("body"))
    source = re.sub(
        r"^(?:quero|preciso)\s+que\s+(?:voce|você|vc)\s+",
        "",
        source,
        flags=re.IGNORECASE,
    )
    source = re.sub(
        r"\b(?:va|vá|vai|ir|entre|entra)\s+(?:nesse|neste|no|na|para|pra|pro)?\s*"
        r"(?:canal|ticket|thread)?\s*(?:aqui)?\s*<#\d{15,22}>\s*(?:e|,)?\s*",
        "",
        source,
        flags=re.IGNORECASE,
    )
    source = re.sub(
        r"\b(?:nesse|neste|no|na|em|para|pra|pro)\s+(?:canal|ticket|thread)\s+"
        r"(?:aqui\s+)?<#\d{15,22}>\s*(?:e|,)?\s*",
        "",
        source,
        flags=re.IGNORECASE,
    )
    source = re.sub(
        r"\b(?:no|em|para|pra|pro)\s+(?:canal|ticket|thread)\s+[#\w\-]+.*$",
        "",
        source,
        flags=re.IGNORECASE,
    ).strip()
    match = re.search(
        r"\b(?:avise|avisa|avisar|diga|diz|fale|fala|mande|manda|envie|envia|enviar)"
        r"\s+(?:(?:o|a|ao|aos|para|pra|pro)\s+)?"
        r"(?P<target><@!?\d{15,22}>|@[^\s,.:;]+)?\s*"
        r"(?:que|q|:|-)\s*(?P<body>.+)$",
        source,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if match:
        target = (match.group("target") or "").strip()
        body = _clean_message_content(match.group("body"))
        return _join_target_and_body(target, body)
    match = re.search(
        r"\b(?:avise|avisa|avisar|diga|diz|fale|fala)\s+"
        r"(?:(?:o|a|ao|aos|para|pra|pro)\s+)?"
        r"(?P<target><@!?\d{15,22}>|@[^\s,.:;]+)\s+(?P<body>.+)$",
        source,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if match:
        target = (match.group("target") or "").strip()
        body = _clean_message_content(match.group("body"))
        body = re.sub(r"^(?:que|q)\s+", "", body, flags=re.IGNORECASE)
        return _join_target_and_body(target, body)
    match = re.search(
        r"\b(?:mande|manda|envie|envia|enviar)\s+"
        r"(?P<body>.+?)(?:\s+(?:no|em|para|pra|pro)\s+(?:canal|ticket|thread)\b|$)",
        source,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if match:
        return _clean_message_content(match.group("body"))
    return ""


def _join_target_and_body(target: str, body: str) -> str:
    if not body:
        return ""
    if target and target not in body:
        return _clean_message_content(f"{target}, {body}")
    return body


def _clean_message_content(value: str) -> str:
    cleaned = str(value or "").strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    cleaned = re.sub(
        r"@\s*(everyone|here)\b",
        lambda match: "@\u200b" + match.group(1),
        cleaned,
        flags=re.IGNORECASE,
    )
    return cleaned.strip(" .")[:2000]


def _channel_relay_text(
    value: str,
    *,
    target_id: int,
    author_name: str,
) -> str:
    body = re.sub(
        rf"^<@!?{target_id}>[:,\s]*",
        "",
        str(value or ""),
        count=1,
    ).strip()
    notice = _owner_relay_text(body, author_name=author_name, direct=True)
    return f"<@{target_id}>, {notice[0].lower()}{notice[1:]}" if notice else ""


def _relay_notice_prompt(
    *,
    body: str,
    target: discord.Member,
    author_name: str,
    direct: bool,
    channel: discord.TextChannel | discord.Thread | None,
    required_mentions: tuple[str, ...],
) -> str:
    target_mention = f"<@{target.id}>"
    destination = (
        "mensagem privada para o destinatário"
        if direct
        else f"mensagem pública no canal/thread {getattr(channel, 'name', 'atual')}"
    )
    required_start = (
        "Não comece com menção; fale diretamente com a pessoa."
        if direct
        else f"Comece obrigatoriamente com exatamente: {target_mention},"
    )
    mention_rule = (
        "Menções obrigatórias que devem aparecer exatamente na mensagem final: "
        + ", ".join(required_mentions)
        if required_mentions
        else "Não há menções extras obrigatórias além do destinatário."
    )
    return (
        "Você é a Bella escrevendo um aviso curto no Discord em nome do Talisson.\n"
        "Transforme a ordem do Talisson em uma mensagem natural para o destinatário.\n"
        "Não copie literalmente a ordem. Não use trechos como 'falando que', "
        "'manda ele', 'avisa pro', 'no canal' ou instruções internas. "
        "Não copie o final do pedido palavra por palavra; reescreva com outras palavras.\n"
        "Se o pedido tiver várias intenções dentro do aviso, preserve todas. "
        "Exemplo: se Talisson quer falar e mandou abrir um ticket, diga as duas coisas.\n"
        "Converta a primeira pessoa do Talisson para uma fala natural: "
        "'eu quero falar com ele' pode virar 'o Talisson quer falar contigo'.\n"
        "Quando 'ele/ela' se referir ao destinatário, escreva como 'você/contigo'.\n"
        "Não troque palavras genéricas como 'empresa' pelo nome do destinatário.\n"
        "Preserve menções de canais, cargos ou usuários que façam parte do aviso. "
        "Nunca transforme menções como <#123> ou <@&123> em texto comum.\n"
        "O destino onde a mensagem será enviada não deve ser citado no texto final, "
        "a menos que ele esteja dentro do pedido bruto como parte do recado.\n"
        "Nunca use @everyone ou @here. Não use emojis. Não invente informações.\n"
        "Escreva em português brasileiro correto, com tom humano, direto e natural. "
        "Revise concordância, preposições e repetições antes de responder. "
        "Evite construções ruins como 'quer saber se você ainda quer'; prefira "
        "'quer saber se você ainda pretende'. Prefira 'bot para Discord' em vez "
        "de 'bot de Discord'.\n"
        "Exemplo de reescrita: 'perguntando se ele ainda quer dar continuidade... "
        "se ele for querer pede pra entrar em contato' deve virar algo como "
        "'quer saber se você ainda pretende continuar com o projeto. Se ainda fizer "
        "sentido pra você, chama ele pra alinhar os próximos passos'.\n"
        "Use no máximo duas frases, salvo se o aviso realmente precisar de mais.\n"
        f"Destino: {destination}.\n"
        f"Autor do pedido: {author_name or 'Talisson'}.\n"
        f"Destinatário: {target.display_name} ({target_mention}).\n"
        f"{mention_rule}\n"
        f"Regra de início: {required_start}\n"
        f"Pedido bruto do Talisson: {body}\n\n"
        "Responda somente com a mensagem final que será enviada."
    )


def _relay_required_mentions(value: str, *, target_id: int) -> tuple[str, ...]:
    mentions: list[str] = []
    for mention in re.findall(r"<#\d{15,22}>|<@&\d{15,22}>|<@!?\d{15,22}>", value):
        if re.fullmatch(rf"<@!?{target_id}>", mention):
            continue
        normalized = re.sub(r"^<@!", "<@", mention)
        if normalized not in mentions:
            mentions.append(normalized)
    return tuple(mentions)


def _clean_generated_relay_notice(
    value: str,
    *,
    target_id: int,
    direct: bool,
    required_mentions: tuple[str, ...] = (),
    delivery_channel: discord.TextChannel | discord.Thread | None = None,
) -> str:
    text = str(value or "").strip()
    text = re.sub(r"^```(?:\w+)?|```$", "", text, flags=re.MULTILINE).strip()
    text = re.sub(
        r"^(?:mensagem\s+final|aviso|resposta|mensagem)\s*[:\-]\s*",
        "",
        text,
        flags=re.IGNORECASE,
    ).strip()
    text = re.sub(r"<[A-Za-z][A-Za-z0-9 _-]{0,40}>", "", text).strip()
    text = text.strip("\"'“”‘’` ")
    text = _clean_message_content(text)
    if not text:
        return ""
    target_mention = f"<@{target_id}>"
    if direct:
        text = re.sub(
            rf"^<@!?{target_id}>[:,\s]*",
            "",
            text,
            count=1,
        ).strip()
    elif not re.match(rf"^<@!?{target_id}>[:,\s]*", text):
        text = re.sub(
            rf"<@!?{target_id}>[:,\s]*",
            "",
            text,
            flags=re.IGNORECASE,
        ).strip()
        text = _strip_relay_leading_punctuation(text)
        text = f"{target_mention}, {_relay_after_mention_sentence(text)}"
    else:
        head_match = re.match(rf"^<@!?{target_id}>[:,\s]*", text)
        if head_match:
            rest = text[head_match.end():]
            rest = re.sub(
                rf"<@!?{target_id}>[:,\s]*",
                "",
                rest,
                flags=re.IGNORECASE,
            ).strip()
            rest = _strip_relay_leading_punctuation(rest)
            text = (
                f"{target_mention}, {_relay_after_mention_sentence(rest)}"
                if rest
                else target_mention
            )
    text = _remove_delivery_channel_from_notice(
        text,
        delivery_channel=delivery_channel,
        required_mentions=required_mentions,
    )
    text = _polish_relay_portuguese(text)
    text = _ensure_required_relay_mentions(text, required_mentions)
    text = _normalize_relay_punctuation(text, target_id=target_id)
    return text[:2000].strip()


def _strip_relay_leading_punctuation(value: str) -> str:
    return re.sub(r"^[\s,.;:!?。！？…-]+", "", str(value or "")).strip()


def _relay_after_mention_sentence(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if re.match(r"^(?:O|A|Os|As|Ele|Ela|Eles|Elas|Eu|Você|Voce)\b", text):
        return text[0].lower() + text[1:]
    return text


def _normalize_relay_punctuation(text: str, *, target_id: int) -> str:
    cleaned = str(text or "").strip()
    cleaned = re.sub(
        rf"^(<@!?{target_id}>),\s*[,.;:!?]+\s*",
        r"\1, ",
        cleaned,
    )
    cleaned = re.sub(r"\s+([,.!?;:])", r"\1", cleaned)
    cleaned = re.sub(r"([,.!?;:]){2,}", r"\1", cleaned)
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    return cleaned.strip()


def _polish_relay_portuguese(text: str) -> str:
    cleaned = str(text or "").strip()
    cleaned = re.sub(
        r"\bquer saber se voc\S*\s+ainda\s+quer\s+dar\s+continuidade\s+"
        r"(?:ao|no|com o)?\s*projeto\s+do\s+bot\s+(?:de|do|para)\s+Discord\s+dele\s+"
        r"e,\s+caso\s+queira,\s+(?:pede|pediu)\s+(?:que|pra)\s+voc\S*\s+"
        r"entrar\s+em\s+contato\s+com\s+ele\b",
        (
            "quer saber se você ainda pretende continuar com o projeto do seu bot "
            "para Discord. Se ainda fizer sentido pra você, chama ele pra alinhar "
            "os próximos passos"
        ),
        cleaned,
        flags=re.IGNORECASE,
    )
    replacements = (
        (
            r"\bquer falar com el[ea]\b",
            "quer falar contigo",
        ),
        (
            r"\bprecisa falar com el[ea]\b",
            "precisa falar contigo",
        ),
        (
            r"\bmandou (?:vc|você|voce) falar com el[ea]\b",
            "pediu pra você falar com ele",
        ),
        (
            r"\bmandou (?:vc|você|voce) procurar el[ea]\b",
            "pediu pra você procurar ele",
        ),
        (
            r"\bse el[ea]\s+(?:for querer|quiser),?\s+"
            r"(?:pede|peça|pedir)\s+(?:pra|para|que)\s+"
            r"(?:el[ea]\s+)?(?:entrar\s+em\s+contato|falar)\s+"
            r"(?:comigo|com\s+o\s+Talisson|com\s+ele)\b",
            "se ainda fizer sentido pra você, chama o Talisson pra alinhar os próximos passos",
        ),
        (
            r"\bquer saber se voc\S*\s+ainda quer dar continuidade\b",
            "quer saber se você ainda pretende dar continuidade",
        ),
        (
            r"\bquer saber se voce ainda quer dar continuidade\b",
            "quer saber se você ainda pretende dar continuidade",
        ),
        (
            r"\bquer saber se voc\S*\s+quer dar continuidade\b",
            "quer saber se você pretende dar continuidade",
        ),
        (
            r"\bquer saber se voce quer dar continuidade\b",
            "quer saber se você pretende dar continuidade",
        ),
        (r"\bbot de Discord\b", "bot para Discord"),
        (r"\bbot do Discord\b", "bot para Discord"),
        (
            r"\bpede que entre em contato com ele\b",
            "pediu pra você entrar em contato com ele",
        ),
        (
            r"\bpede pra entrar em contato com ele\b",
            "pediu pra você entrar em contato com ele",
        ),
    )
    for pattern, replacement in replacements:
        cleaned = re.sub(pattern, replacement, cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(
        r"\bquer saber se voc\S*\s+ainda\s+pretende\s+dar\s+continuidade\s+"
        r"referente\s+ao\s+projeto\s+do\s+bot\s+(?:de|do|para)\s+Discord\s+del[ea]\b",
        "quer saber se você ainda pretende continuar com o projeto do seu bot para Discord",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(
        r"\bquer saber se voc\S*\s+ainda\s+pretende\s+dar\s+continuidade\s+"
        r"(?:ao|no|com o)?\s*projeto\s+do\s+bot\s+(?:de|do|para)\s+Discord\s+del[ea]\b",
        "quer saber se você ainda pretende continuar com o projeto do seu bot para Discord",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(
        r"\s*e,?\s+caso\s+queira,?\s+"
        r"(?:pede|pediu|peça)\s+(?:que|pra|para)\s+"
        r"(?:voc\S+\s+)?entrar\s+em\s+contato\s+com\s+el[ea]\b",
        ". Se ainda fizer sentido pra você, chama o Talisson pra alinhar os próximos passos",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(r"\s+([,.!?;:])", r"\1", cleaned)
    cleaned = re.sub(r"\.{2,}", ".", cleaned)
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    return cleaned.strip()


def _remove_delivery_channel_from_notice(
    text: str,
    *,
    delivery_channel: discord.TextChannel | discord.Thread | None,
    required_mentions: tuple[str, ...],
) -> str:
    if delivery_channel is None:
        return text
    channel_mention = f"<#{delivery_channel.id}>"
    if channel_mention in required_mentions:
        return text
    cleaned = text
    channel_name_value = str(getattr(delivery_channel, "name", "") or "")
    refs = [rf"<#{delivery_channel.id}>"]
    if channel_name_value:
        channel_name = re.escape(channel_name_value)
        refs.extend((rf"#{channel_name}", channel_name))
    for ref in refs:
        cleaned = re.sub(
            rf"\s*(?:,?\s*e\s+)?(?:voc[eê]|voce)\s+pode\s+"
            rf"(?:mandar|enviar|chamar|falar)\s+(?:mensagem\s+)?"
            rf"(?:em|no|na|pelo|pela|dentro\s+do|dentro\s+da)\s+{ref}",
            "",
            cleaned,
            flags=re.IGNORECASE,
        )
        cleaned = re.sub(
            rf"\s*(?:,?\s*e\s+)?(?:manda|mande|envia|envie)\s+"
            rf"(?:mensagem\s+)?(?:em|no|na|pelo|pela|dentro\s+do|dentro\s+da)\s+{ref}",
            "",
            cleaned,
            flags=re.IGNORECASE,
        )
        cleaned = re.sub(
            rf"\s+(?:em|no|na|pelo|pela|dentro\s+do|dentro\s+da)\s+{ref}",
            "",
            cleaned,
            flags=re.IGNORECASE,
        )
    cleaned = re.sub(r"\s+([,.!?])", r"\1", cleaned)
    cleaned = re.sub(
        r"\s*(?:,?\s*e\s+)?(?:voc\S+|voce)\s+pode\s+"
        r"(?:mandar|enviar|chamar|falar)\s+(?:mensagem|msg)\s*$",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(r"(?:,\s*|\s+)(?:e|mas|porém)$", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
    return cleaned or text


def _ensure_required_relay_mentions(text: str, required_mentions: tuple[str, ...]) -> str:
    if not required_mentions:
        return text
    missing = [mention for mention in required_mentions if mention not in text]
    if not missing:
        return text
    addition = " ".join(missing)
    if len(text) + len(addition) + 1 <= 2000:
        return f"{text.rstrip()} {addition}".strip()
    return text


def _owner_relay_text(
    value: str,
    *,
    author_name: str,
    direct: bool,
) -> str:
    body = _strip_relay_connectors(_clean_message_content(value))
    body = _rewrite_owner_first_person(body, direct=direct)
    if not body:
        return ""
    normalized = _normalize_text(body)
    if normalized.startswith(("talisson ", "o talisson ", "recado ")):
        return body
    owner_name = "Talisson"
    if author_name and _normalize_text(author_name) not in {"tali", "talisson"}:
        owner_name = "Talisson"
    return f"O {owner_name} pediu pra avisar que {body[0].lower()}{body[1:]}."


def _wants_direct_message(value: str) -> bool:
    text = _normalize_text(value)
    return any(
        marker in text
        for marker in (
            "dm",
            "pv",
            "privado",
            "privada",
            "mensagem privada",
            "direct",
        )
    )


def _strip_relay_connectors(value: str) -> str:
    cleaned = str(value or "").strip(" .")
    previous = None
    while previous != cleaned:
        previous = cleaned
        cleaned = re.sub(
            r"^(?:no|na|em|para|pra|pro)\s+"
            r"(?:(?:canal|chat|ticket|thread)\s+)?"
            r"(?:<#\d{15,22}>|#[^\s,.:;]+)\s*(?:,|e)?\s*",
            "",
            cleaned,
            flags=re.IGNORECASE,
        ).strip()
        cleaned = re.sub(
            r"^(?:por\s+)?(?:dm|pv|privad[ao]|mensagem\s+privad[ao])\s+",
            "",
            cleaned,
            flags=re.IGNORECASE,
        ).strip()
        cleaned = re.sub(
            r"^(?:falando|dizendo|avisando|informando|comunicando|perguntando)\s+"
            r"(?:que|q)\s+",
            "",
            cleaned,
            flags=re.IGNORECASE,
        ).strip()
        cleaned = re.sub(
            r"^(?:para|pra|pro|ao)\s+"
            r"(?:avisar|falar|dizer|informar|comunicar)\s+"
            r"(?:que|q)\s+",
            "",
            cleaned,
            flags=re.IGNORECASE,
        ).strip()
        cleaned = re.sub(r"^(?:que|q)\s+", "", cleaned, flags=re.IGNORECASE).strip()
    return cleaned.strip(" .")


def _rewrite_owner_first_person(value: str, *, direct: bool) -> str:
    text = str(value or "").strip()
    replacements = (
        (r"^(?:eu\s+)?preciso\b", "precisa"),
        (r"^(?:eu\s+)?quero\b", "quer"),
        (r"^(?:eu\s+)?vou\b", "vai"),
        (r"^(?:eu\s+)?posso\b", "pode"),
        (r"^(?:eu\s+)?consigo\b", "consegue"),
        (r"^(?:eu\s+)?estou\b", "está"),
        (r"^(?:eu\s+)?to\b", "está"),
        (r"^(?:eu\s+)?tô\b", "está"),
        (r"^(?:eu\s+)?nao\s+estou\b", "não está"),
        (r"^(?:eu\s+)?não\s+estou\b", "não está"),
        (r"^(?:eu\s+)?nao\s+vou\b", "não vai"),
        (r"^(?:eu\s+)?não\s+vou\b", "não vai"),
    )
    for pattern, replacement in replacements:
        text = re.sub(pattern, replacement, text, count=1, flags=re.IGNORECASE)
    if direct:
        text = re.sub(r"\bcom\s+el[ea]\b", "com você", text, flags=re.IGNORECASE)
        text = re.sub(r"\b(?:pra|para|pro)\s+el[ea]\b", "pra você", text, flags=re.IGNORECASE)
        text = re.sub(r"\bel[ea]\b", "você", text, count=1, flags=re.IGNORECASE)
    return text.strip(" .")


def _clean_display_name(value: str, *, limit: int) -> str:
    cleaned = MENTION_PATTERN.sub("", str(value))
    cleaned = re.sub(r"[\r\n\t]+", " ", cleaned)
    cleaned = " ".join(cleaned.split()).strip(" .")
    return cleaned[:limit]


def _clean_channel_name(value: str) -> str:
    cleaned = _normalize_text(_clean_display_name(value, limit=100))
    cleaned = re.sub(r"[^a-z0-9_-]+", "-", cleaned)
    return re.sub(r"-{2,}", "-", cleaned).strip("-_")[:100]


def _contains(text: str, value: str) -> bool:
    return re.search(rf"\b{re.escape(value)}\b", text) is not None


def _looks_like_message_send_order(
    text: str,
    *,
    has_target: bool,
    has_channel: bool,
) -> bool:
    if not _has_any(
        text,
        "avisa",
        "avise",
        "avisar",
        "manda",
        "mande",
        "envia",
        "envie",
        "enviar",
        "fala",
        "fale",
        "diz",
        "diga",
        "comunica",
        "comunique",
        "comunicar",
    ):
        return False
    return (
        _contains_any(text, "mensagem", "aviso", "recado", "comunicado")
        or has_target
        or has_channel
    )


def _contains_any(text: str, *values: str) -> bool:
    return any(value in text for value in values)


def _has_any(text: str, *values: str) -> bool:
    return any(
        value in text if " " in value else _contains(text, value)
        for value in values
    )


def _resolve_catalog_ids(
    text: str,
    catalog: list[dict[str, object]],
) -> list[int]:
    matches: list[tuple[int, int]] = []
    for item in catalog:
        try:
            item_id = int(item.get("id", 0))
        except (TypeError, ValueError):
            continue
        names = {
            _normalize_text(str(item.get("name", ""))),
            _normalize_text(str(item.get("username", ""))),
        }
        best_length = max(
            (
                len(name)
                for name in names
                if len(name) >= 2
                and re.search(rf"\b{re.escape(name)}\b", text)
            ),
            default=0,
        )
        if best_length:
            matches.append((best_length, item_id))
    matches.sort(reverse=True)
    return [item_id for _, item_id in matches]


def _parse_duration(text: str) -> int | None:
    match = DURATION_PATTERN.search(text)
    if not match:
        return None
    amount = int(match.group(1))
    unit = match.group(2)
    if unit.startswith(("seg", "sec")) or unit == "s":
        multiplier = 1
    elif unit.startswith(("hora", "hr")) or unit == "h":
        multiplier = 3600
    elif unit.startswith("dia") or unit == "d":
        multiplier = 86400
    else:
        multiplier = 60
    return amount * multiplier


def _optional_int(
    value: object,
    *,
    allow_zero: bool = False,
) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 or (allow_zero and parsed == 0) else None


def _int_list(value: object) -> list[int]:
    if not isinstance(value, (list, tuple)):
        return []
    result: list[int] = []
    for item in value:
        parsed = _optional_int(item)
        if parsed is not None:
            result.append(parsed)
    return result


def _parse_message_count(text: str) -> int | None:
    match = MESSAGE_COUNT_PATTERN.search(text)
    return int(match.group(1)) if match else None


def _parse_event_name(content: str) -> str:
    clean = " ".join(MENTION_PATTERN.sub(" ", content).split()).strip()
    match = re.search(
        r"\b(?:chamado|chamada|nome(?:ado)?|intitulado)\s+"
        r"(.+?)(?=\s+(?:hoje|amanh[ãa]|às|as)\b|$)",
        clean,
        flags=re.IGNORECASE,
    )
    if match:
        return match.group(1).strip(" .,-")[:100]
    match = re.search(
        r"\b(?:cria|criar|crie|agenda|agendar)\s+"
        r"(?:um\s+)?evento\s+(.+?)(?=\s+(?:hoje|amanh[ãa]|às|as)\b|$)",
        clean,
        flags=re.IGNORECASE,
    )
    return match.group(1).strip(" .,-")[:100] if match else ""


def _parse_event_time(text: str) -> str:
    match = EVENT_TIME_PATTERN.search(text)
    if not match:
        return ""
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    if hour > 23 or minute > 59:
        return ""
    zone = ZoneInfo("America/Sao_Paulo")
    now = datetime.now(zone)
    day_offset = 1 if "amanha" in text else 0
    scheduled = (now + timedelta(days=day_offset)).replace(
        hour=hour,
        minute=minute,
        second=0,
        microsecond=0,
    )
    if day_offset == 0 and scheduled <= now:
        scheduled += timedelta(days=1)
    return scheduled.isoformat()


def _scheduled_datetime(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _duration_text(seconds: int) -> str:
    if seconds % 86400 == 0:
        value = seconds // 86400
        return f"{value} dia" if value == 1 else f"{value} dias"
    if seconds % 3600 == 0:
        value = seconds // 3600
        return f"{value} hora" if value == 1 else f"{value} horas"
    if seconds % 60 == 0:
        value = seconds // 60
        return f"{value} minuto" if value == 1 else f"{value} minutos"
    return f"{seconds} segundos"


def _display(value: str) -> str:
    readable = " ".join(MENTION_PATTERN.sub(" ", value).split())
    if not readable:
        readable = "Nome indisponível"
    return discord.utils.escape_markdown(discord.utils.escape_mentions(readable))


def _channel_slug(value: str) -> str:
    normalized = _normalize_text(value)
    slug = re.sub(r"[^a-z0-9]+", "-", normalized).strip("-")
    return (slug or "atendimento")[:80]


def _memory_key(value: str) -> str:
    normalized = _normalize_text(value)
    return re.sub(r"[^a-z0-9]+", "_", normalized).strip("_")[:60]


def _contains_sensitive_memory(value: str) -> bool:
    normalized = _normalize_text(value)
    return bool(
        re.search(
            r"\b(token|senha|password|secret|segredo|api[_ ]?key|chave privada)\b",
            normalized,
        )
        or re.search(r"\b(?:sk|AIza|AQ)\.[A-Za-z0-9_-]{12,}\b", value)
    )
