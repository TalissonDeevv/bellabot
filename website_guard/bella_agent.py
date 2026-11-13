"""Camada de agente da Bella sobre o executor administrativo.

O interpretador (src/ai/intent_parser.py) transforma o pedido em linguagem
natural em um ExecutiveRequest, que pode ter várias etapas. Este módulo:

- confirma com botões planos de várias etapas, ações irreversíveis, posts em
  outros canais (prévia com Publicar/Editar) e links de pagamento;
- guarda o histórico de ações para "desfaz isso" e "faz o mesmo com o @x";
- registra menções ao proprietário e monta o briefing por DM (sob pedido e
  diário);
- sugere moderação com botões quando a moderação autônoma vê risco;
- executa as ações novas: tarefas, lembrete por reply, busca no histórico,
  enquetes, acesso a canais e link de pagamento no Stripe.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Awaitable, Callable
from unicodedata import combining, normalize
from zoneinfo import ZoneInfo

import aiohttp
import discord

from .executive import (
    ACTION_LABELS,
    ExecutiveAssistant,
    ExecutiveOutcome,
    ExecutiveRequest,
)
from .state import StateStore

LOGGER = logging.getLogger(__name__)

HISTORY_SECTION = "agent_action_history"
TODO_SECTION = "agent_todos"
INBOX_SECTION = "agent_inbox"
META_SECTION = "agent_meta"
MOD_ALERT_SECTION = "agent_moderation_alerts"
PAYMENT_SECTION = "agent_payment_links"
TICKET_SECTION = "smart_tickets"
REMINDER_SECTION = "talisson_personal_reminders"

HISTORY_LIMIT = 15
INBOX_LIMIT = 300
INBOX_RETENTION_DAYS = 14
VIEW_TIMEOUT_SECONDS = 15 * 60
MOD_ALERT_COOLDOWN_SECONDS = 30 * 60

# Ações que nunca entram no histórico de "desfaz"/"faz o mesmo".
NOT_RECORDED = {
    "undo_last",
    "briefing",
    "todo_add",
    "todo_list",
    "todo_done",
    "history_search",
    "role_list",
    "status",
    "statistics_schedule_list",
    "memory_list",
}
# Precisam de confirmação mesmo sozinhas: não têm volta.
IRREVERSIBLE = {"channel_delete", "role_delete", "message_purge"}
CHANNEL_SCOPED = {
    "message_send",
    "channel_lock",
    "channel_unlock",
    "channel_rename",
    "channel_topic_set",
    "channel_topic_clear",
    "channel_slowmode",
    "channel_access_grant",
    "channel_make_private",
    "poll_create",
}
URGENT_TERMS = (
    "urgente",
    "urgencia",
    "socorro",
    "caiu",
    "fora do ar",
    "nao funciona",
    "parou",
    "erro",
    "bug",
    "pagamento",
    "paguei",
    "pagar",
    "orcamento",
    "contrato",
    "prazo",
    "hoje",
    "agora",
    "rapido",
    "emergencia",
    "problema",
)

AGENT_LABELS = {
    "undo_last": "Desfazer última ação",
    "briefing": "Briefing",
    "todo_add": "Anotar tarefa",
    "todo_list": "Listar tarefas",
    "todo_done": "Concluir tarefa",
    "reminder_set": "Criar lembrete",
    "history_search": "Buscar no histórico",
    "poll_create": "Criar enquete",
    "payment_link_create": "Criar link de pagamento",
    "channel_access_grant": "Liberar acesso ao canal",
    "channel_make_private": "Tornar canal privado",
    "stripe_setup": "Configurar Stripe",
}


def _norm(value: str) -> str:
    decomposed = normalize("NFKD", str(value or "").casefold())
    return " ".join(
        "".join(char for char in decomposed if not combining(char)).split()
    )


def _short(value: str, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _channel_slug(name: str) -> str:
    return re.sub(r"[\s_]+", "-", _norm(name)).strip("-")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def agent_zone() -> ZoneInfo:
    try:
        return ZoneInfo(os.getenv("AGENT_TIMEZONE", "America/Sao_Paulo"))
    except Exception:
        return ZoneInfo("UTC")


def _env_flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().casefold() in {"1", "true", "yes", "on", "sim"}


def parse_price(value: str) -> Decimal | None:
    """Aceita '1500.00', '1.500,00', '1500,5', 'R$ 89,90'. Devolve reais."""
    text = re.sub(r"[^\d,.]", "", str(value or ""))
    if not text:
        return None
    if "," in text and "." in text:
        text = text.replace(".", "").replace(",", ".")
    elif "," in text:
        text = text.replace(",", ".")
    elif text.count(".") == 1 and re.fullmatch(r"\d{1,3}\.\d{3}", text):
        text = text.replace(".", "")  # "1.500" = mil e quinhentos
    elif text.count(".") > 1:
        text = text.replace(".", "")
    try:
        amount = Decimal(text).quantize(Decimal("0.01"))
    except InvalidOperation:
        return None
    return amount if amount > 0 else None


def format_brl(amount: Decimal) -> str:
    whole, cents = f"{amount:.2f}".split(".")
    groups = []
    while whole:
        groups.insert(0, whole[-3:])
        whole = whole[:-3]
    return f"R$ {'.'.join(groups)},{cents}"


def classify_urgency(text: str) -> tuple[bool, tuple[str, ...]]:
    normalized = _norm(text)
    hits = tuple(term for term in URGENT_TERMS if re.search(rf"\b{term}\b", normalized))
    shouting = sum(c.isupper() for c in text) > 12 and text.upper() == text
    return bool(hits or shouting or text.count("!") >= 3), hits


ENV_PATH = Path(__file__).resolve().parents[1] / ".env"
STRIPE_SETUP_BUTTON_ID = "bella_agent:stripe_setup"
STRIPE_GUIDE = (
    "oii, pra eu criar links de pagamento no Stripe preciso de umas coisinhas:\n\n"
    "**1. conta no Stripe**\n"
    "https://dashboard.stripe.com/register\n"
    "pra testar não precisa de mais nada. pra cobrar de verdade a conta tem que "
    "estar ativada (seus dados ou da empresa e uma conta bancária pra receber).\n\n"
    "**2. chave de API**\n"
    "o mais seguro é uma **chave restrita**: Developers > API keys > "
    "Create restricted key, com permissão **Write** só em **Products**, "
    "**Prices** e **Payment Links**. o resto pode ficar em None.\n"
    "• teste: https://dashboard.stripe.com/test/apikeys (começa com `rk_test_` ou `sk_test_`)\n"
    "• produção: https://dashboard.stripe.com/apikeys (começa com `rk_live_` ou `sk_live_`)\n"
    "recomendo começar pela de teste.\n\n"
    "**3. moeda**\n"
    "por padrão eu cobro em BRL. dá pra trocar no formulário.\n\n"
    "**4. Pix e boleto (opcional)**\n"
    "Settings > Payment methods. os links aceitam os métodos que estiverem ativos lá.\n\n"
    "**não me manda a chave aqui no chat**, porque mensagem fica salva no Discord. "
    "clica em **Configurar Stripe**: abre um formulário, eu testo a chave no Stripe "
    "e salvo no .env sem precisar me reiniciar."
)


class StripeError(RuntimeError):
    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.status = status


def write_env_value(path: Path, key: str, value: str) -> None:
    """Troca (ou acrescenta) KEY=valor no .env sem mexer nas outras linhas."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*=")
    replaced = False
    for index, line in enumerate(lines):
        if pattern.match(line):
            lines[index] = f"{key}={value}"
            replaced = True
    if not replaced:
        lines.append(f"{key}={value}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


class StripeClient:
    API = "https://api.stripe.com/v1"

    def __init__(self, secret_key: str, *, currency: str = "brl") -> None:
        self.secret_key = secret_key.strip()
        self.currency = currency.strip().casefold() or "brl"
        self._session: aiohttp.ClientSession | None = None

    @property
    def configured(self) -> bool:
        return self.secret_key.startswith(("sk_", "rk_"))

    @property
    def test_mode(self) -> bool:
        return "_test_" in self.secret_key

    async def _request(
        self,
        method: str,
        path: str,
        *,
        data: dict[str, str] | None = None,
        params: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=20)
            )
        async with self._session.request(
            method,
            f"{self.API}{path}",
            data=data,
            params=params,
            headers={"Authorization": f"Bearer {self.secret_key}"},
        ) as response:
            payload = await response.json(content_type=None)
            if response.status >= 400:
                error = payload.get("error", {}) if isinstance(payload, dict) else {}
                raise StripeError(
                    str(error.get("message") or f"Stripe HTTP {response.status}"),
                    response.status,
                )
            return payload

    async def _post(self, path: str, data: dict[str, str]) -> dict[str, Any]:
        return await self._request("POST", path, data=data)

    async def verify(self) -> None:
        """Confere se a chave existe e pode mexer em Payment Links."""
        await self._request("GET", "/payment_links", params={"limit": "1"})

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()

    async def create_payment_link(
        self,
        *,
        amount: Decimal,
        service: str,
        customer: str,
        metadata: dict[str, str],
    ) -> dict[str, Any]:
        product_name = _short(
            f"{service} - {customer}" if customer else service,
            240,
        )
        price = await self._post(
            "/prices",
            {
                "currency": self.currency,
                "unit_amount": str(int(amount * 100)),
                "product_data[name]": product_name,
            },
        )
        data = {
            "line_items[0][price]": str(price["id"]),
            "line_items[0][quantity]": "1",
            # Link de cobrança de um serviço: paga uma vez só.
            "restrictions[completed_sessions][limit]": "1",
        }
        for key, value in metadata.items():
            data[f"metadata[{key}]"] = _short(value, 450)
        return await self._post("/payment_links", data)


class BellaAgent:
    def __init__(
        self,
        bot: discord.Client,
        state: StateStore,
        executive: ExecutiveAssistant,
        *,
        owner_id: int,
        availability: Any | None = None,
    ) -> None:
        self.bot = bot
        self.state = state
        self.executive = executive
        self.owner_id = int(owner_id)
        self.availability = availability
        self.stripe = StripeClient(
            os.getenv("STRIPE_SECRET_KEY", ""),
            currency=os.getenv("STRIPE_CURRENCY", "brl"),
        )
        self._suppress_record = False
        self._last_artifact: dict[int, dict[str, Any]] = {}
        self._mod_alert_sent: dict[tuple[int, int], float] = {}

    # ------------------------------------------------------------------ setup
    def install(self, parser: Any | None = None, moderation: Any | None = None) -> None:
        ACTION_LABELS.update(AGENT_LABELS)
        self.executive.before_execute = self.before_execute
        self.executive.after_execute = self.after_execute
        for action, handler in {
            "undo_last": self._undo_last,
            "briefing": self._briefing,
            "todo_add": self._todo_add,
            "todo_list": self._todo_list,
            "todo_done": self._todo_done,
            "reminder_set": self._reminder_set,
            "history_search": self._history_search,
            "poll_create": self._poll_create,
            "payment_link_create": self._payment_unconfirmed,
            "stripe_setup": self._payment_unconfirmed,
            "channel_access_grant": self._channel_access_grant,
            "channel_make_private": self._channel_make_private,
        }.items():
            self.executive.register_handler(action, handler)
        if parser is not None:
            parser.recent_actions_provider = self.recent_actions
        if moderation is not None:
            moderation.suggestion_callback = self.moderation_suggestion

    # ------------------------------------------------------- history / memory
    async def recent_actions(self, guild_id: int) -> list[dict[str, Any]]:
        section = await self.state.get_section(HISTORY_SECTION)
        entries = section.get(str(guild_id), [])
        return [
            {
                key: entry.get(key)
                for key in (
                    "action",
                    "target_id",
                    "target_name",
                    "role_ids",
                    "channel_ids",
                    "name",
                    "reason",
                    "duration_seconds",
                )
                if entry.get(key) not in (None, "", [])
            }
            for entry in reversed(entries[-3:])
        ]

    async def after_execute(
        self,
        message: discord.Message,
        request: ExecutiveRequest,
        outcome: ExecutiveOutcome,
    ) -> None:
        if (
            self._suppress_record
            or not outcome.success
            or request.action in NOT_RECORDED
            or message.guild is None
        ):
            return
        artifact = self._last_artifact.pop(message.guild.id, {})
        channel_ids = list(request.channel_ids)
        if request.action == "message_send":
            # O executor anota "Canal: nome (id); mensagem: id". Sem isso foi
            # uma DM, que não dá para apagar depois.
            sent = re.search(r"\((\d+)\); mensagem: (\d+)", outcome.notes or "")
            if sent is None:
                return
            channel_ids = [int(sent.group(1))]
            artifact = {"message_id": int(sent.group(2))}
        if request.action in CHANNEL_SCOPED and not channel_ids:
            channel_ids = [message.channel.id]
        target = outcome.target
        entry = {
            "action": request.action,
            "target_id": request.target_id,
            "target_name": getattr(target, "display_name", "") if target else "",
            "role_ids": list(request.role_ids),
            "channel_ids": channel_ids,
            "name": request.name,
            "reason": request.reason,
            "duration_seconds": request.duration_seconds,
            "at": _now().isoformat(),
            **artifact,
        }
        await self._push_history(message.guild.id, entry)

    async def _push_history(self, guild_id: int, entry: dict[str, Any]) -> None:
        def mutate(section: dict[str, Any]) -> None:
            values = list(section.get(str(guild_id), []))
            values.append(entry)
            section[str(guild_id)] = values[-HISTORY_LIMIT:]

        await self.state.mutate_section(HISTORY_SECTION, mutate)

    async def _pop_history(self, guild_id: int) -> dict[str, Any] | None:
        popped: list[dict[str, Any]] = []

        def mutate(section: dict[str, Any]) -> None:
            values = list(section.get(str(guild_id), []))
            if values:
                popped.append(values.pop())
            section[str(guild_id)] = values

        await self.state.mutate_section(HISTORY_SECTION, mutate)
        return popped[0] if popped else None

    # ------------------------------------------------------ confirmation gate
    async def before_execute(
        self,
        message: discord.Message,
        request: ExecutiveRequest,
    ) -> bool:
        if request.action == "payment_link_create":
            await self._offer_payment(message, request)
            return True
        if request.action == "stripe_setup":
            status = (
                f"o Stripe já tá configurado ({'teste' if self.stripe.test_mode else 'produção'}). "
                "se quiser trocar a chave, é só clicar no botão.\n\n"
                if self.stripe.configured
                else ""
            )
            await message.reply(
                status + STRIPE_GUIDE,
                view=StripeSetupView(self),
                mention_author=False,
                suppress_embeds=True,
            )
            return True
        if len(request.steps) > 1:
            await self._offer_plan(message, request.steps)
            return True
        if request.action in IRREVERSIBLE:
            await self._offer_plan(message, (request,))
            return True
        if request.action == "message_send" and self._is_cross_channel_post(
            message, request
        ):
            await self._offer_post(message, request)
            return True
        return False

    @staticmethod
    def _is_cross_channel_post(
        message: discord.Message,
        request: ExecutiveRequest,
    ) -> bool:
        return bool(
            request.channel_ids
            and request.channel_ids[0] != message.channel.id
            and request.text.strip()
        )

    def _describe(self, guild: discord.Guild, request: ExecutiveRequest) -> str:
        label = ACTION_LABELS.get(request.action, request.action)
        parts = [label]
        if request.target_id:
            member = guild.get_member(request.target_id)
            parts.append(f"em {member.display_name if member else request.target_id}")
        roles = [guild.get_role(rid) for rid in request.role_ids]
        if any(roles):
            parts.append("cargo " + ", ".join(r.name for r in roles if r))
        if request.channel_ids:
            channel = guild.get_channel(request.channel_ids[0])
            parts.append(f"canal #{channel.name if channel else request.channel_ids[0]}")
        elif request.name and request.action not in {"todo_add"}:
            parts.append(f'"{_short(request.name, 60)}"')
        if request.text and request.action in {"message_send", "poll_create"}:
            parts.append(f'— "{_short(request.text, 90)}"')
        if request.reason:
            parts.append(f"(motivo: {_short(request.reason, 60)})")
        if request.amount and request.action == "message_purge":
            parts.append(f"({request.amount} mensagens)")
        return " ".join(parts)

    async def _offer_plan(
        self,
        message: discord.Message,
        steps: tuple[ExecutiveRequest, ...],
    ) -> None:
        lines = [
            f"{index}. {self._describe(message.guild, step)}"
            for index, step in enumerate(steps, start=1)
        ]
        irreversible = any(step.action in IRREVERSIBLE for step in steps)
        header = (
            "pera, isso não tem volta. confirma pra mim?"
            if irreversible and len(steps) == 1
            else "entendi assim, posso fazer?"
        )
        view = ConfirmView(
            self.owner_id,
            confirm_label="Executar",
            on_confirm=lambda interaction: self._run_plan(interaction, message, steps),
        )
        view.message = await message.reply(
            f"{header}\n" + "\n".join(lines),
            view=view,
            mention_author=False,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _run_plan(
        self,
        interaction: discord.Interaction,
        message: discord.Message,
        steps: tuple[ExecutiveRequest, ...],
    ) -> str:
        created_channel: discord.abc.GuildChannel | None = None
        results: list[str] = []
        for index, step in enumerate(steps, start=1):
            if (
                created_channel is not None
                and step.action in CHANNEL_SCOPED
                and not step.channel_ids
            ):
                step = replace(step, channel_ids=(created_channel.id,))
            outcome = await self.executive.run_request(message, step, reply=False)
            mark = "feito" if outcome.success else "falhou"
            detail = "" if outcome.success else f" — {_short(outcome.audit_reason, 120)}"
            results.append(f"{index}. {mark}: {self._describe(message.guild, step)}{detail}")
            if outcome.success and step.action in {
                "channel_create_text",
                "channel_create_voice",
            }:
                created_channel = self._find_new_channel(message.guild, step.name)
        ok = all(line.split(": ", 1)[0].endswith("feito") for line in results)
        head = "pronto, fiz tudo" if ok else "fiz o que deu, olha:"
        return f"{head}\n" + "\n".join(results)

    @staticmethod
    def _find_new_channel(
        guild: discord.Guild,
        name: str,
    ) -> discord.abc.GuildChannel | None:
        slug = _channel_slug(name)
        candidates = [
            channel
            for channel in guild.channels
            if _channel_slug(channel.name) == slug
            or _norm(channel.name) == _norm(name)
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda channel: channel.created_at)

    # ---------------------------------------------------------- post approval
    async def _offer_post(
        self,
        message: discord.Message,
        request: ExecutiveRequest,
    ) -> None:
        channel = message.guild.get_channel(request.channel_ids[0])
        channel_name = f"#{channel.name}" if channel else "o canal"
        view = PostApprovalView(self, message, request, channel_name)
        view.message = await message.reply(
            view.preview_text(),
            view=view,
            mention_author=False,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def publish_post(
        self,
        message: discord.Message,
        request: ExecutiveRequest,
    ) -> ExecutiveOutcome:
        return await self.executive.run_request(message, request, reply=False)

    # ----------------------------------------------------------------- undo
    async def _undo_last(
        self,
        message: discord.Message,
        request: ExecutiveRequest,
    ) -> ExecutiveOutcome:
        entry = await self._pop_history(message.guild.id)
        if entry is None:
            return self._ok("hmm, não achei nenhuma ação recente minha pra desfazer.")
        action = str(entry.get("action"))
        label = ACTION_LABELS.get(action, action)
        inverse = self._inverse_request(entry)
        try:
            if inverse is not None:
                self._suppress_record = True
                try:
                    outcome = await self.executive.run_request(
                        message, inverse, reply=False
                    )
                finally:
                    self._suppress_record = False
                if not outcome.success:
                    await self._push_history(message.guild.id, entry)
                    return self._fail(
                        f"tentei desfazer ({label}), mas não rolou: "
                        f"{_short(outcome.audit_reason, 160)}"
                    )
                return self._ok(f"desfeito: {label}.", target=outcome.target)
            done = await self._undo_artifact(message.guild, entry)
            if done:
                return self._ok(f"desfeito: {label}.")
        except (discord.Forbidden, discord.HTTPException) as exc:
            await self._push_history(message.guild.id, entry)
            return self._fail(f"o Discord não deixou desfazer ({type(exc).__name__}).")
        await self._push_history(message.guild.id, entry)
        return self._fail(
            f"essa última ({label}) eu não consigo desfazer. "
            "expulsão, exclusão e limpeza de mensagens não têm volta."
        )

    @staticmethod
    def _inverse_request(entry: dict[str, Any]) -> ExecutiveRequest | None:
        action = entry.get("action")
        target_id = entry.get("target_id")
        roles = tuple(int(r) for r in entry.get("role_ids") or ())
        channels = tuple(int(c) for c in entry.get("channel_ids") or ())
        inverse = {
            "ban": "unban",
            "timeout": "untimeout",
            "role_add": "role_remove",
            "role_remove": "role_add",
            "channel_lock": "channel_unlock",
            "channel_unlock": "channel_lock",
            "nickname_set": "nickname_reset",
        }.get(str(action))
        if inverse is not None:
            return ExecutiveRequest(
                action=inverse,
                target_id=int(target_id) if target_id else None,
                role_ids=roles,
                channel_ids=channels,
                normalized_text="desfazer",
                reason="Desfeito a pedido do proprietário",
            )
        if action == "channel_slowmode":
            return ExecutiveRequest(
                action="channel_slowmode",
                channel_ids=channels,
                duration_seconds=0,
                normalized_text="desfazer",
            )
        return None

    async def _undo_artifact(self, guild: discord.Guild, entry: dict[str, Any]) -> bool:
        action = entry.get("action")
        channels = [int(c) for c in entry.get("channel_ids") or ()]
        if action in {"message_send", "poll_create"} and channels:
            channel = guild.get_channel(channels[0]) or guild.get_thread(channels[0])
            if channel is None:
                return False
            message_id = entry.get("message_id")
            if not message_id:
                return False
            try:
                sent = await channel.fetch_message(int(message_id))
            except discord.NotFound:
                return True  # já foi apagada
            await sent.delete()
            return True
        if action in {"channel_create_text", "channel_create_voice"}:
            created = self._find_new_channel(guild, str(entry.get("name") or ""))
            if created is not None:
                await created.delete(reason="Desfeito a pedido do proprietário")
                return True
            return False
        if action == "channel_access_grant" and channels:
            channel = guild.get_channel(channels[0])
            if channel is None:
                return False
            for role_id in entry.get("role_ids") or ():
                role = guild.get_role(int(role_id))
                if role:
                    await channel.set_permissions(role, overwrite=None)
            if entry.get("target_id"):
                member = guild.get_member(int(entry["target_id"]))
                if member:
                    await channel.set_permissions(member, overwrite=None)
            return True
        if action == "channel_make_private" and channels:
            channel = guild.get_channel(channels[0])
            if channel is None:
                return False
            await channel.set_permissions(guild.default_role, view_channel=None)
            return True
        return False

    # ----------------------------------------------------------------- todos
    async def _todos(self) -> list[dict[str, Any]]:
        section = await self.state.get_section(TODO_SECTION)
        return list(section.get(str(self.owner_id), []))

    async def _save_todos(self, todos: list[dict[str, Any]]) -> None:
        await self.state.update_section(TODO_SECTION, str(self.owner_id), todos[-200:])

    async def _todo_add(self, message, request) -> ExecutiveOutcome:
        text = _short(request.text or request.name, 300)
        if not text:
            return self._fail("anotar o quê? me fala a tarefa.")
        todos = await self._todos()
        todos.append({"text": text, "done": False, "created_at": _now().isoformat()})
        await self._save_todos(todos)
        pending = sum(1 for item in todos if not item.get("done"))
        return self._ok(f"anotado: {text}\nvc tem {pending} pendente(s).")

    async def _todo_list(self, message, request) -> ExecutiveOutcome:
        pending = [item for item in await self._todos() if not item.get("done")]
        if not pending:
            return self._ok("nada pendente, tá tudo em dia.")
        lines = [f"{i}. {item['text']}" for i, item in enumerate(pending, start=1)]
        return self._ok("suas pendências:\n" + "\n".join(lines))

    async def _todo_done(self, message, request) -> ExecutiveOutcome:
        todos = await self._todos()
        pending = [item for item in todos if not item.get("done")]
        chosen: dict[str, Any] | None = None
        if request.amount and 1 <= request.amount <= len(pending):
            chosen = pending[request.amount - 1]
        elif request.text:
            wanted = _norm(request.text)
            chosen = next(
                (item for item in pending if wanted in _norm(item["text"])),
                None,
            )
        if chosen is None:
            return self._fail("não achei essa tarefa na lista. pede pra eu listar.")
        chosen["done"] = True
        chosen["done_at"] = _now().isoformat()
        await self._save_todos(todos)
        return self._ok(f"riscado: {chosen['text']}")

    # -------------------------------------------------------------- reminder
    async def _reminder_set(self, message, request) -> ExecutiveOutcome:
        if self.availability is None:
            return self._fail("o módulo de lembretes não está disponível.")
        zone = agent_zone()
        try:
            when = datetime.strptime(request.scheduled_for.strip()[:16], "%Y-%m-%d %H:%M")
            when = when.replace(tzinfo=zone)
        except ValueError:
            return self._fail("não entendi quando é pra lembrar. fala dia e hora?")
        if when <= datetime.now(zone):
            return self._fail("esse horário já passou.")
        text = request.text.strip()
        link = ""
        reference = message.reference
        if reference is not None and reference.message_id:
            link = (
                f"https://discord.com/channels/{message.guild.id}/"
                f"{reference.channel_id or message.channel.id}/{reference.message_id}"
            )
            if not text and isinstance(reference.resolved, discord.Message):
                text = f'"{_short(reference.resolved.content, 300)}"'
        text = text or "você pediu pra eu te lembrar disso"
        await self.availability.create_deterministic_reminder(
            guild_id=message.guild.id,
            channel_id=message.channel.id,
            creator_id=self.owner_id,
            message=f"{text}\n{link}".strip(),
            scheduled_at=when.isoformat(),
            antecedence_minutes=0,
        )
        return self._ok(
            f"fechado, te lembro {when.strftime('%d/%m às %H:%M')}: {_short(text, 120)}"
        )

    # ---------------------------------------------------------- history search
    async def _history_search(self, message, request) -> ExecutiveOutcome:
        guild = message.guild
        channels: list[Any] = []
        for channel_id in request.channel_ids[:3]:
            channel = guild.get_channel(channel_id) or guild.get_thread(channel_id)
            if channel is not None and hasattr(channel, "history"):
                channels.append(channel)
        if not channels:
            channels = [message.channel]
        days = min(30, request.period_days or 7)
        after = _now() - timedelta(days=days)
        keywords = [
            word
            for word in re.findall(r"[a-z0-9]+", _norm(request.text))
            if len(word) >= 3
        ]
        found: list[discord.Message] = []
        for channel in channels:
            try:
                async for item in channel.history(limit=1500, after=after, oldest_first=False):
                    if item.id == message.id or item.author.bot:
                        continue
                    if request.target_id and item.author.id != request.target_id:
                        continue
                    content = _norm(item.content)
                    if keywords and not any(
                        re.search(rf"\b{re.escape(word)}", content) for word in keywords
                    ):
                        continue
                    if not item.content.strip():
                        continue
                    found.append(item)
            except (discord.Forbidden, discord.HTTPException):
                continue
        found.sort(key=lambda item: item.created_at, reverse=True)
        if not found:
            return self._ok(f"não achei nada disso nos últimos {days} dias.")
        zone = agent_zone()
        lines = [
            f"• {item.created_at.astimezone(zone).strftime('%d/%m %H:%M')} "
            f"**{item.author.display_name}**: {_short(item.content, 160)} — {item.jump_url}"
            for item in found[:8]
        ]
        extra = f"\n(+{len(found) - 8} mensagens)" if len(found) > 8 else ""
        return self._ok(f"achei {len(found)}:\n" + "\n".join(lines) + extra)

    # ------------------------------------------------------------------ poll
    async def _poll_create(self, message, request) -> ExecutiveOutcome:
        channel, error = self.executive._selected_channel(message, request)
        if error or channel is None or not hasattr(channel, "send"):
            return self._fail(error or "não achei o canal da enquete.")
        options = [option for option in request.options if option][:10]
        question = _short(request.text or request.name, 300)
        if not question or len(options) < 2:
            return self._fail("pra enquete preciso da pergunta e de pelo menos 2 opções.")
        hours = round((request.duration_seconds or 86400) / 3600)
        poll = discord.Poll(
            question=question,
            duration=timedelta(hours=max(1, min(768, hours))),
        )
        for option in options:
            poll.add_answer(text=option)
        sent = await channel.send(poll=poll)
        self._last_artifact[message.guild.id] = {"message_id": sent.id}
        return self._ok(f"enquete no ar em #{channel.name}: {sent.jump_url}")

    # -------------------------------------------------------- channel access
    async def _channel_access_grant(self, message, request) -> ExecutiveOutcome:
        channel, error = self.executive._selected_channel(message, request)
        if error or channel is None or isinstance(channel, discord.Thread):
            return self._fail(error or "acesso só dá pra liberar em canal, não em thread.")
        targets: list[Any] = [
            role for role in (message.guild.get_role(r) for r in request.role_ids) if role
        ]
        if request.target_id:
            member = message.guild.get_member(request.target_id)
            if member:
                targets.append(member)
        if not targets:
            return self._fail("liberar acesso pra quem? menciona o cargo ou a pessoa.")
        for target in targets:
            await channel.set_permissions(
                target,
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                connect=True,
                speak=True,
                reason=f"Pedido de {message.author} via Bella",
            )
        names = ", ".join(getattr(t, "display_name", None) or t.name for t in targets)
        return self._ok(f"liberei #{channel.name} pra {names}.")

    async def _channel_make_private(self, message, request) -> ExecutiveOutcome:
        channel, error = self.executive._selected_channel(message, request)
        if error or channel is None or isinstance(channel, discord.Thread):
            return self._fail(error or "só dá pra fechar canal, não thread.")
        reason = f"Pedido de {message.author} via Bella"
        if message.guild.me is not None:
            await channel.set_permissions(
                message.guild.me, view_channel=True, send_messages=True, reason=reason
            )
        await channel.set_permissions(
            message.guild.default_role, view_channel=False, reason=reason
        )
        return self._ok(f"#{channel.name} agora é privado.")

    # --------------------------------------------------------------- payments
    async def _payment_unconfirmed(self, message, request) -> ExecutiveOutcome:
        return self._fail("link de pagamento só sai depois de confirmar no botão.")

    async def _offer_payment(
        self,
        message: discord.Message,
        request: ExecutiveRequest,
    ) -> None:
        if not self.stripe.configured:
            await message.reply(
                "o Stripe ainda não tá configurado.\n\n" + STRIPE_GUIDE,
                view=StripeSetupView(self),
                mention_author=False,
                suppress_embeds=True,
            )
            return
        amount = parse_price(request.price)
        service = _short(request.name, 120)
        customer_member = (
            message.guild.get_member(request.target_id) if request.target_id else None
        )
        customer = _short(
            request.text or (customer_member.display_name if customer_member else ""),
            120,
        )
        if amount is None or amount < Decimal("0.50") or amount > Decimal("999999.99"):
            await message.reply(
                "não entendi o valor. me fala de novo quanto é?", mention_author=False
            )
            return
        if not service:
            await message.reply("é pagamento de qual serviço?", mention_author=False)
            return
        mode = " (modo teste)" if self.stripe.test_mode else ""
        preview = (
            f"vou criar esse link de pagamento no Stripe{mode}:\n"
            f"valor: **{format_brl(amount)}**\n"
            f"serviço: {service}\n"
            f"cliente: {customer or 'não informado'}\n"
            "confere e confirma?"
        )

        async def create(interaction: discord.Interaction) -> str:
            link = await self.stripe.create_payment_link(
                amount=amount,
                service=service,
                customer=customer,
                metadata={
                    "servico": service,
                    "cliente": customer,
                    "cliente_discord_id": str(request.target_id or ""),
                    "criado_por": str(message.author.id),
                    "origem": "bella-discord",
                },
            )
            url = str(link.get("url", ""))
            await self.state.update_section(
                PAYMENT_SECTION,
                str(link.get("id")),
                {
                    "url": url,
                    "amount": str(amount),
                    "service": service,
                    "customer": customer,
                    "customer_id": request.target_id,
                    "guild_id": message.guild.id,
                    "created_at": _now().isoformat(),
                    "test_mode": self.stripe.test_mode,
                },
            )
            if customer_member is not None:
                await interaction.followup.send(
                    f"quer que eu mande pro {customer_member.display_name} no privado?",
                    view=SendLinkView(self.owner_id, customer_member, url, service, amount),
                    ephemeral=False,
                )
            return f"link criado ({format_brl(amount)} — {service}):\n{url}"

        view = ConfirmView(self.owner_id, confirm_label="Criar link", on_confirm=create)
        view.message = await message.reply(
            preview,
            view=view,
            mention_author=False,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    # ------------------------------------------------------- inbox / briefing
    async def observe_message(self, message: discord.Message) -> None:
        if (
            message.guild is None
            or message.author.bot
            or message.author.id == self.owner_id
        ):
            return
        mentions_owner = any(user.id == self.owner_id for user in message.mentions)
        reference = message.reference
        resolved = getattr(reference, "resolved", None) if reference else None
        replies_owner = isinstance(resolved, discord.Message) and (
            resolved.author.id == self.owner_id
        )
        names_owner = bool(re.search(r"\b(talisson|tali|talinho)\b", _norm(message.content)))
        if not (mentions_owner or replies_owner or names_owner):
            return
        urgent, reasons = classify_urgency(message.content)
        entry = {
            "at": _now().isoformat(),
            "author_id": message.author.id,
            "author_name": message.author.display_name,
            "channel_id": message.channel.id,
            "channel_name": getattr(message.channel, "name", ""),
            "jump_url": message.jump_url,
            "snippet": _short(message.clean_content, 220),
            "urgent": urgent,
            "reasons": list(reasons),
        }
        cutoff = _now() - timedelta(days=INBOX_RETENTION_DAYS)

        def mutate(section: dict[str, Any]) -> None:
            values = [
                item
                for item in section.get(str(message.guild.id), [])
                if datetime.fromisoformat(item["at"]) >= cutoff
            ]
            values.append(entry)
            section[str(message.guild.id)] = values[-INBOX_LIMIT:]

        await self.state.mutate_section(INBOX_SECTION, mutate)

    async def build_briefing(
        self,
        guild: discord.Guild,
        since: datetime,
    ) -> str:
        zone = agent_zone()
        lines = [
            f"**resumo de {guild.name}** desde "
            f"{since.astimezone(zone).strftime('%d/%m %H:%M')}"
        ]
        inbox = [
            item
            for item in (await self.state.get_section(INBOX_SECTION)).get(str(guild.id), [])
            if datetime.fromisoformat(item["at"]) >= since
        ]
        urgent = [item for item in inbox if item.get("urgent")]
        normal = [item for item in inbox if not item.get("urgent")]
        if inbox:
            lines.append(f"\n**te chamaram {len(inbox)}x**")
            for item in urgent[-6:]:
                lines.append(
                    f"• [urgente] {item['author_name']} em #{item['channel_name']}: "
                    f"{_short(item['snippet'], 140)} — {item['jump_url']}"
                )
            for item in normal[-5:]:
                lines.append(
                    f"• {item['author_name']} em #{item['channel_name']}: "
                    f"{_short(item['snippet'], 120)} — {item['jump_url']}"
                )
            if len(normal) > 5:
                lines.append(f"  (+{len(normal) - 5} menções menos importantes)")
        else:
            lines.append("\nninguém te chamou.")

        tickets = (await self.state.get_section(TICKET_SECTION)).values()
        new_tickets = []
        open_tickets = 0
        for ticket in tickets:
            if not isinstance(ticket, dict) or int(ticket.get("guild_id") or 0) != guild.id:
                continue
            if not ticket.get("closed_at"):
                open_tickets += 1
            created = _parse_dt(ticket.get("created_at"))
            if created and created >= since:
                new_tickets.append(ticket)
        if new_tickets or open_tickets:
            lines.append(f"\n**tickets**: {len(new_tickets)} novo(s), {open_tickets} aberto(s)")
            for ticket in new_tickets[-5:]:
                lines.append(
                    f"• {ticket.get('creator_name', 'alguém')} — "
                    f"{ticket.get('category', 'geral')}, prioridade "
                    f"{ticket.get('priority', 'normal')}: "
                    f"{_short(str(ticket.get('summary') or ''), 100)}"
                )

        alerts = [
            item
            for item in (await self.state.get_section(MOD_ALERT_SECTION)).get(str(guild.id), [])
            if datetime.fromisoformat(item["at"]) >= since
        ]
        if alerts:
            lines.append(f"\n**moderação**: {len(alerts)} alerta(s)")
            for item in alerts[-4:]:
                lines.append(
                    f"• {item['author_name']}: {', '.join(item.get('reasons', [])[:3])}"
                )

        joined = [
            member
            for member in guild.members
            if member.joined_at and member.joined_at >= since and not member.bot
        ]
        if joined:
            names = ", ".join(member.display_name for member in joined[:6])
            more = f" e mais {len(joined) - 6}" if len(joined) > 6 else ""
            lines.append(f"\n**entraram {len(joined)}**: {names}{more}")

        pending = [item for item in await self._todos() if not item.get("done")]
        if pending:
            lines.append(f"\n**suas pendências ({len(pending)})**")
            lines.extend(f"• {item['text']}" for item in pending[:6])

        today = datetime.now(zone).date()
        reminders = []
        for record in (await self.state.get_section(REMINDER_SECTION)).values():
            if not isinstance(record, dict) or record.get("status") != "pendente":
                continue
            when = _parse_dt(record.get("scheduled_at"))
            if when and when.astimezone(zone).date() == today:
                reminders.append((when, record))
        if reminders:
            lines.append("\n**hoje**")
            for when, record in sorted(reminders, key=lambda pair: pair[0])[:6]:
                lines.append(
                    f"• {when.astimezone(zone).strftime('%H:%M')} "
                    f"{_short(str(record.get('message_original') or record.get('title')), 100)}"
                )
        return "\n".join(lines)

    async def _briefing(self, message, request) -> ExecutiveOutcome:
        guild = message.guild
        meta = await self.state.get_section(META_SECTION)
        last = _parse_dt((meta.get("last_briefing_at") or {}).get(str(guild.id)))
        if request.period_days:
            since = _now() - timedelta(days=request.period_days)
        else:
            since = last or (_now() - timedelta(hours=24))
        text = await self.build_briefing(guild, since)
        delivered = await self._dm_owner(text)
        await self._mark_briefing(guild.id)
        if delivered:
            return self._ok("te mandei o resumo no privado.")
        return self._ok(text[:1900])

    async def _mark_briefing(self, guild_id: int) -> None:
        def mutate(section: dict[str, Any]) -> None:
            values = dict(section.get("last_briefing_at") or {})
            values[str(guild_id)] = _now().isoformat()
            section["last_briefing_at"] = values

        await self.state.mutate_section(META_SECTION, mutate)

    async def _dm_owner(self, text: str, view: discord.ui.View | None = None) -> bool:
        try:
            owner = self.bot.get_user(self.owner_id) or await self.bot.fetch_user(
                self.owner_id
            )
            chunks = _split_message(text)
            for index, chunk in enumerate(chunks):
                await owner.send(
                    chunk,
                    view=view if view is not None and index == len(chunks) - 1 else None,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            return True
        except (discord.Forbidden, discord.HTTPException, discord.NotFound):
            LOGGER.warning("Não consegui mandar DM para o proprietário.")
            return False

    # ------------------------------------------------------------ stripe setup
    async def startup(self) -> None:
        """Registra o botão persistente e, na primeira vez, manda o guia do Stripe."""
        await self.bot.wait_until_ready()
        self.bot.add_view(StripeSetupView(self))
        if self.stripe.configured:
            return
        meta = await self.state.get_section(META_SECTION)
        if meta.get("stripe_guide_sent_at"):
            return
        sent = await self._dm_owner(STRIPE_GUIDE, view=StripeSetupView(self))
        if sent:
            await self.state.update_section(
                META_SECTION, "stripe_guide_sent_at", _now().isoformat()
            )

    async def configure_stripe(self, secret_key: str, currency: str) -> str:
        key = secret_key.strip()
        currency = (currency.strip().casefold() or "brl")[:3]
        if not re.fullmatch(r"(?:sk|rk)_(?:test|live)_[A-Za-z0-9]{10,}", key):
            return (
                "essa chave não parece do Stripe. ela começa com sk_test_, sk_live_, "
                "rk_test_ ou rk_live_."
            )
        if not re.fullmatch(r"[a-z]{3}", currency):
            return "a moeda tem que ser um código de 3 letras, tipo brl ou usd."
        candidate = StripeClient(key, currency=currency)
        try:
            await candidate.verify()
        except StripeError as exc:
            await candidate.close()
            if exc.status == 401:
                return "o Stripe recusou essa chave. confere se copiou ela inteira."
            if exc.status == 403:
                return (
                    "a chave funciona, mas não tem permissão em Payment Links. "
                    "dá Write em Products, Prices e Payment Links e tenta de novo."
                )
            return f"o Stripe respondeu com erro: {_short(str(exc), 200)}"
        except (aiohttp.ClientError, asyncio.TimeoutError):
            await candidate.close()
            return "não consegui falar com o Stripe agora. tenta de novo daqui a pouco."
        try:
            write_env_value(ENV_PATH, "STRIPE_SECRET_KEY", key)
            write_env_value(ENV_PATH, "STRIPE_CURRENCY", currency)
        except OSError:
            LOGGER.exception("Não consegui salvar a chave do Stripe no .env.")
            saved = " (não consegui gravar no .env, então ela vale só até eu reiniciar)"
        else:
            saved = ""
        os.environ["STRIPE_SECRET_KEY"] = key
        os.environ["STRIPE_CURRENCY"] = currency
        old = self.stripe
        self.stripe = candidate
        await old.close()
        await self.state.update_section(META_SECTION, "stripe_configured_at", _now().isoformat())
        mode = "modo teste" if candidate.test_mode else "modo produção, cobrança de verdade"
        return (
            f"pronto, Stripe configurado ({mode}, moeda {currency.upper()}){saved}. "
            "já pode me pedir links de pagamento."
        )

    async def daily_briefing_worker(self) -> None:
        await self.bot.wait_until_ready()
        while not self.bot.is_closed():
            try:
                await self._maybe_send_daily_briefing()
            except Exception:
                LOGGER.exception("Falha no briefing diário do agente.")
            await asyncio.sleep(60)

    async def _maybe_send_daily_briefing(self) -> None:
        if not _env_flag("AGENT_DAILY_BRIEFING", True):
            return
        zone = agent_zone()
        now = datetime.now(zone)
        hour = int(os.getenv("AGENT_BRIEFING_HOUR", "8") or 8)
        minute = int(os.getenv("AGENT_BRIEFING_MINUTE", "0") or 0)
        if (now.hour, now.minute) < (hour, minute):
            return
        meta = await self.state.get_section(META_SECTION)
        today = now.date().isoformat()
        if meta.get("last_daily_date") == today:
            return
        await self.state.update_section(META_SECTION, "last_daily_date", today)
        scheduled = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if now - scheduled > timedelta(hours=3):
            # Ligou bem depois do horário (ex.: 22h): sem "bom dia" fora de hora,
            # o briefing fica para amanhã.
            return
        for guild in self.bot.guilds:
            if guild.get_member(self.owner_id) is None:
                continue
            last = _parse_dt((meta.get("last_briefing_at") or {}).get(str(guild.id)))
            since = last or (_now() - timedelta(hours=24))
            text = await self.build_briefing(guild, since)
            if await self._dm_owner("bom dia. " + text):
                await self._mark_briefing(guild.id)

    # ------------------------------------------------- moderation suggestion
    async def moderation_suggestion(
        self,
        message: discord.Message,
        *,
        reasons: tuple[str, ...],
        risk: int,
        deleted: bool,
        timed_out: bool,
    ) -> None:
        if message.guild is None or not isinstance(message.author, discord.Member):
            return
        key = (message.guild.id, message.author.id)
        now = time.monotonic()
        if now - self._mod_alert_sent.get(key, 0) < MOD_ALERT_COOLDOWN_SECONDS:
            return
        self._mod_alert_sent[key] = now
        entry = {
            "at": _now().isoformat(),
            "author_id": message.author.id,
            "author_name": message.author.display_name,
            "channel_id": message.channel.id,
            "reasons": list(reasons)[:5],
            "risk": risk,
        }

        def mutate(section: dict[str, Any]) -> None:
            values = list(section.get(str(message.guild.id), []))
            values.append(entry)
            section[str(message.guild.id)] = values[-100:]

        await self.state.mutate_section(MOD_ALERT_SECTION, mutate)
        done = []
        if deleted:
            done.append("apaguei a mensagem")
        if timed_out:
            done.append("já silenciei por um tempo")
        text = (
            f"detectei algo estranho do **{message.author.display_name}** em "
            f"#{getattr(message.channel, 'name', '?')} (risco {risk}): "
            f"{', '.join(reasons[:4]) or 'comportamento suspeito'}.\n"
            f"\"{_short(message.clean_content, 200)}\"\n"
            + (f"{' e '.join(done)}. " if done else "")
            + "o que eu faço?"
        )
        await self._dm_owner(
            text,
            view=ModerationSuggestionView(self, message.guild.id, message.author.id),
        )

    async def apply_moderation_choice(
        self,
        guild_id: int,
        user_id: int,
        choice: str,
    ) -> str:
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return "não achei o servidor."
        reason = "Sugestão de moderação aprovada pelo proprietário via Bella"
        if choice == "ban":
            await guild.ban(discord.Object(id=user_id), reason=reason, delete_message_seconds=3600)
            await self._push_history(
                guild_id,
                {"action": "ban", "target_id": user_id, "reason": reason, "at": _now().isoformat()},
            )
            return "banido. se mudar de ideia, é só pedir pra desfazer."
        member = guild.get_member(user_id)
        if member is None:
            return "a pessoa já saiu do servidor."
        await member.timeout(timedelta(hours=1), reason=reason)
        await self._push_history(
            guild_id,
            {
                "action": "timeout",
                "target_id": user_id,
                "target_name": member.display_name,
                "duration_seconds": 3600,
                "at": _now().isoformat(),
            },
        )
        return f"silenciei {member.display_name} por 1 hora."

    # ----------------------------------------------------------------- utils
    @staticmethod
    def _ok(text: str, target: discord.abc.User | None = None) -> ExecutiveOutcome:
        return ExecutiveOutcome(
            success=True,
            public_message=text,
            audit_reason=_short(text, 300),
            target=target,
        )

    @staticmethod
    def _fail(text: str) -> ExecutiveOutcome:
        return ExecutiveOutcome(success=False, public_message=text, audit_reason=text)


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _split_message(text: str, limit: int = 1900) -> list[str]:
    chunks: list[str] = []
    current = ""
    for line in text.splitlines():
        if len(current) + len(line) + 1 > limit and current:
            chunks.append(current)
            current = ""
        current = f"{current}\n{line}" if current else line[:limit]
    if current:
        chunks.append(current)
    return chunks or [""]


# --------------------------------------------------------------------- views
class OwnerOnlyView(discord.ui.View):
    def __init__(self, owner_id: int) -> None:
        super().__init__(timeout=VIEW_TIMEOUT_SECONDS)
        self.owner_id = owner_id
        self.message: discord.Message | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message(
            "só o Talisson pode usar esses botões.", ephemeral=True
        )
        return False

    def _disable(self) -> None:
        for item in self.children:
            item.disabled = True

    async def on_timeout(self) -> None:
        self._disable()
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except (discord.HTTPException, discord.NotFound):
                pass


class ConfirmView(OwnerOnlyView):
    def __init__(
        self,
        owner_id: int,
        *,
        confirm_label: str,
        on_confirm: Callable[[discord.Interaction], Awaitable[str]],
    ) -> None:
        super().__init__(owner_id)
        self.on_confirm = on_confirm
        self.confirm_button.label = confirm_label

    @discord.ui.button(label="Executar", style=discord.ButtonStyle.success)
    async def confirm_button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self._disable()
        await interaction.response.edit_message(view=self)
        try:
            result = await self.on_confirm(interaction)
        except Exception as exc:
            LOGGER.exception("Falha ao executar ação confirmada.")
            result = f"deu ruim: {_short(str(exc), 200)}"
        await interaction.followup.send(
            result[:1900], allowed_mentions=discord.AllowedMentions.none()
        )
        self.stop()

    @discord.ui.button(label="Cancelar", style=discord.ButtonStyle.secondary)
    async def cancel_button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self._disable()
        await interaction.response.edit_message(content="cancelado.", view=self)
        self.stop()


class PostEditModal(discord.ui.Modal, title="Editar post"):
    def __init__(self, view: "PostApprovalView") -> None:
        super().__init__()
        self.view_ref = view
        self.body = discord.ui.TextInput(
            label="Texto",
            style=discord.TextStyle.paragraph,
            default=view.request.text[:2000],
            max_length=2000,
        )
        self.add_item(self.body)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        self.view_ref.request = replace(self.view_ref.request, text=str(self.body.value))
        await interaction.response.edit_message(
            content=self.view_ref.preview_text(), view=self.view_ref
        )


class PostApprovalView(OwnerOnlyView):
    def __init__(
        self,
        agent: BellaAgent,
        message: discord.Message,
        request: ExecutiveRequest,
        channel_name: str,
    ) -> None:
        super().__init__(agent.owner_id)
        self.agent = agent
        self.origin = message
        self.request = request
        self.channel_name = channel_name

    def preview_text(self) -> str:
        return (
            f"prévia do post em {self.channel_name}:\n"
            f">>> {self.request.text[:1700]}"
        )

    @discord.ui.button(label="Publicar", style=discord.ButtonStyle.success)
    async def publish(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self._disable()
        await interaction.response.edit_message(view=self)
        outcome = await self.agent.publish_post(self.origin, self.request)
        result = (
            f"publicado em {self.channel_name}."
            if outcome.success
            else f"não consegui publicar: {_short(outcome.audit_reason, 200)}"
        )
        await interaction.followup.send(result)
        self.stop()

    @discord.ui.button(label="Editar", style=discord.ButtonStyle.primary)
    async def edit(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(PostEditModal(self))

    @discord.ui.button(label="Cancelar", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self._disable()
        await interaction.response.edit_message(content="post cancelado.", view=self)
        self.stop()


class ModerationSuggestionView(OwnerOnlyView):
    def __init__(self, agent: BellaAgent, guild_id: int, user_id: int) -> None:
        super().__init__(agent.owner_id)
        self.timeout = 6 * 60 * 60
        self.agent = agent
        self.guild_id = guild_id
        self.user_id = user_id

    async def _apply(self, interaction: discord.Interaction, choice: str) -> None:
        self._disable()
        await interaction.response.edit_message(view=self)
        try:
            result = await self.agent.apply_moderation_choice(
                self.guild_id, self.user_id, choice
            )
        except (discord.Forbidden, discord.HTTPException) as exc:
            result = f"o Discord não deixou ({type(exc).__name__})."
        await interaction.followup.send(result)
        self.stop()

    @discord.ui.button(label="Banir", style=discord.ButtonStyle.danger)
    async def ban(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._apply(interaction, "ban")

    @discord.ui.button(label="Silenciar 1h", style=discord.ButtonStyle.primary)
    async def mute(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._apply(interaction, "timeout")

    @discord.ui.button(label="Ignorar", style=discord.ButtonStyle.secondary)
    async def ignore(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self._disable()
        await interaction.response.edit_message(view=self)
        await interaction.followup.send("ok, deixei quieto.")
        self.stop()


class StripeKeyModal(discord.ui.Modal, title="Configurar Stripe"):
    def __init__(self, agent: BellaAgent) -> None:
        super().__init__()
        self.agent = agent
        self.key = discord.ui.TextInput(
            label="Chave secreta ou restrita",
            placeholder="rk_test_... ou sk_test_...",
            min_length=20,
            max_length=255,
        )
        self.currency = discord.ui.TextInput(
            label="Moeda",
            default=os.getenv("STRIPE_CURRENCY", "brl") or "brl",
            min_length=3,
            max_length=3,
        )
        self.add_item(self.key)
        self.add_item(self.currency)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        result = await self.agent.configure_stripe(str(self.key.value), str(self.currency.value))
        await interaction.followup.send(result, ephemeral=True)


class StripeSetupView(discord.ui.View):
    """Botão persistente: funciona mesmo depois de a Bella reiniciar."""

    def __init__(self, agent: BellaAgent) -> None:
        super().__init__(timeout=None)
        self.agent = agent

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.agent.owner_id:
            return True
        await interaction.response.send_message(
            "só o Talisson pode configurar o Stripe.", ephemeral=True
        )
        return False

    @discord.ui.button(
        label="Configurar Stripe",
        style=discord.ButtonStyle.primary,
        custom_id=STRIPE_SETUP_BUTTON_ID,
    )
    async def configure(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(StripeKeyModal(self.agent))


class SendLinkView(OwnerOnlyView):
    def __init__(
        self,
        owner_id: int,
        member: discord.Member,
        url: str,
        service: str,
        amount: Decimal,
    ) -> None:
        super().__init__(owner_id)
        self.member = member
        self.url = url
        self.service = service
        self.amount = amount

    @discord.ui.button(label="Mandar por DM", style=discord.ButtonStyle.success)
    async def send(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self._disable()
        await interaction.response.edit_message(view=self)
        try:
            await self.member.send(
                f"oi! segue o link de pagamento de **{self.service}** "
                f"({format_brl(self.amount)}), enviado a pedido do Talisson:\n{self.url}"
            )
            result = f"mandei pro {self.member.display_name}."
        except (discord.Forbidden, discord.HTTPException):
            result = f"o {self.member.display_name} tá com a DM fechada. manda o link direto pra ele."
        await interaction.followup.send(result)
        self.stop()

    @discord.ui.button(label="Não precisa", style=discord.ButtonStyle.secondary)
    async def skip(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self._disable()
        await interaction.response.edit_message(view=self)
        self.stop()
