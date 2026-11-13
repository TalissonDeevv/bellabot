from __future__ import annotations

import hashlib
import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any
from unicodedata import combining, normalize
from zoneinfo import ZoneInfo

import discord

from src.ai.personality import BELLA_PERSONA_RUNTIME

from .identity import OWNER_DISCORD_ID
from .state import StateStore

SMART_TASKS_SECTION = "jarvis_smart_tasks"
SMART_TASK_HISTORY_SECTION = "jarvis_smart_task_history"
OPERATIONAL_PENDING_SECTION = "operational_rule_pending"
TICKET_RECORD_SECTION = "smart_tickets"
DEFAULT_TZ = ZoneInfo("America/Sao_Paulo")


@dataclass
class SmartTask:
    task_id: str
    guild_id: int
    owner_id: int
    title: str
    objective: str
    trigger_type: str
    trigger_terms: list[str]
    action_type: str
    response_text: str
    scope_channel_ids: list[int]
    scope_category_ids: list[int]
    scope_user_ids: list[int]
    scope_role_ids: list[int]
    target_channel_id: int | None
    starts_at: float
    ends_at: float | None
    end_condition: str
    enabled: bool
    raw_instruction: str
    created_at: float
    updated_at: float
    execution_count: int = 0
    schedule_frequency: str = ""
    schedule_hour: int | None = None
    schedule_minute: int | None = None
    schedule_weekday: int | None = None
    schedule_month_day: int | None = None
    next_run_at: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "SmartTask":
        valid = cls.__dataclass_fields__
        payload = {key: item for key, item in value.items() if key in valid}
        for key in (
            "trigger_terms",
            "scope_channel_ids",
            "scope_category_ids",
            "scope_user_ids",
            "scope_role_ids",
        ):
            payload[key] = list(payload.get(key) or [])
        payload["guild_id"] = int(payload.get("guild_id", 0))
        payload["owner_id"] = int(payload.get("owner_id", OWNER_DISCORD_ID))
        payload["starts_at"] = float(payload.get("starts_at", 0))
        payload["ends_at"] = _optional_float(payload.get("ends_at"))
        payload["created_at"] = float(payload.get("created_at", time.time()))
        payload["updated_at"] = float(payload.get("updated_at", time.time()))
        payload["execution_count"] = int(payload.get("execution_count", 0))
        payload["target_channel_id"] = _optional_int(
            payload.get("target_channel_id")
        )
        payload["schedule_frequency"] = str(payload.get("schedule_frequency", ""))
        payload["schedule_hour"] = _optional_int(payload.get("schedule_hour"))
        payload["schedule_minute"] = _optional_int(payload.get("schedule_minute"))
        payload["schedule_weekday"] = _optional_int(payload.get("schedule_weekday"))
        payload["schedule_month_day"] = _optional_int(
            payload.get("schedule_month_day")
        )
        payload["next_run_at"] = _optional_float(payload.get("next_run_at"))
        return cls(**payload)


class SmartTaskCenter:
    def __init__(
        self,
        bot: discord.Client,
        state: StateStore,
        service: Any,
        *,
        owner_id: int = OWNER_DISCORD_ID,
    ) -> None:
        self.bot = bot
        self.state = state
        self.service = service
        self.owner_id = owner_id

    @staticmethod
    def looks_like_instruction(content: str) -> bool:
        text = _normalize(_strip_bot_reference(content))
        if not text:
            return False
        conditional_patterns = (
            r"\bse\s+(?:alguem|a\s+pessoa|o\s+usuario|a\s+usuaria|um\s+membro|"
            r"o\s+membro|quem)\b",
            r"\bquando\s+(?:alguem|a\s+pessoa|o\s+usuario|a\s+usuaria|um\s+membro|"
            r"o\s+membro|me)\b",
            r"\bcaso\s+(?:alguem|a\s+pessoa|o\s+usuario|a\s+usuaria|um\s+membro)\b",
            r"\bsempre\s+que\b",
            r"\bqualquer\s+pessoa\s+que\b",
        )
        temporary_markers = (
            "enquanto eu",
            "ate eu",
            "durante essa",
            "durante esta",
            "durante o evento",
            "durante a semana",
            "essa semana",
            "esta semana",
            "hoje vou",
            "amanha vou",
            "assim que eu",
            "quando eu voltar",
            "quando eu responder",
            "se eu responder",
            "precisei sair",
            "vou sair",
            "estiver offline",
            "estiver ausente",
        )
        schedule_markers = (
            "todos os dias",
            "todo dia",
            "diariamente",
            "toda semana",
            "semanalmente",
            "todo mes",
            "todo mês",
            "mensalmente",
            "toda segunda",
            "toda terca",
            "toda terça",
            "toda quarta",
            "toda quinta",
            "toda sexta",
            "todo sabado",
            "todo sábado",
            "todo domingo",
        )
        action_patterns = (
            r"\bavis(?:a|e|ar)\b",
            r"\brespon(?:de|da|der)\b",
            r"\bmand(?:a|e|ar)\b",
            r"\benvi(?:a|e|ar)\b",
            r"\bexplic(?:a|e|ar)\b",
            r"\bdig(?:a|o|am|er)\b",
            r"\bfal(?:a|e|ar)\b",
            r"\binform(?:a|e|ar)\b",
            r"\bdirecion(?:a|e|ar)\b",
            r"\blemb(?:ra|re|rar)\b",
        )
        has_condition = any(
            re.search(pattern, text) for pattern in conditional_patterns
        )
        has_schedule = any(marker in text for marker in schedule_markers)
        has_temporary_context = any(marker in text for marker in temporary_markers)
        has_action = any(re.search(pattern, text) for pattern in action_patterns)
        return has_action and (has_condition or has_temporary_context or has_schedule)

    async def handle_instruction(
        self,
        message: discord.Message,
        *,
        invoked: bool,
    ) -> bool:
        if (
            message.guild is None
            or message.author.id != OWNER_DISCORD_ID
            or not invoked
            or not self.looks_like_instruction(message.content)
        ):
            return False
        await self._clear_operational_pending(
            message.guild.id,
            message.author.id,
        )
        if not getattr(self.service, "available", False):
            await message.reply(
                "Não consegui interpretar essa tarefa agora. A conexão com a "
                "Groq precisa estar disponível apenas no momento do cadastro.",
                mention_author=False,
            )
            return True
        try:
            task = await self._interpret_once(message)
        except Exception as exc:
            await self.state.set(
                "smart_task_last_error",
                f"{type(exc).__name__}: {exc}",
            )
            await message.reply(
                "Não consegui estruturar essa tarefa com segurança. Tente informar "
                "o gatilho, o que devo fazer e até quando ela deve permanecer ativa.",
                mention_author=False,
            )
            return True
        if task is None:
            await message.reply(
                "Entendi que isso é uma tarefa temporária, não uma regra "
                "permanente. Porém não consegui identificar o gatilho ou o "
                "encerramento com segurança; reformule esses dois pontos.",
                mention_author=False,
            )
            return True
        await self.state.update_section(
            SMART_TASKS_SECTION,
            task.task_id,
            task.to_dict(),
        )
        await self._clear_operational_pending(
            task.guild_id,
            task.owner_id,
        )
        ending = _describe_ending(task)
        scopes = _describe_scope(task)
        await message.reply(
            (
                "Pode deixar. **Guardei essa tarefa temporária e ela já está "
                "ativa.**\n\n"
                f"**Tarefa:** {task.title}\n"
                f"**Gatilho:** {_describe_trigger(task)}\n"
                f"**Escopo:** {scopes}\n"
                f"**Encerramento:** {ending}\n\n"
                "Ela continuará salva mesmo se o bot reiniciar. A Groq foi usada "
                "somente para interpretar o pedido; as execuções serão locais."
            ),
            mention_author=False,
        )
        return True

    async def apply_message(self, message: discord.Message) -> bool:
        tasks = await self._active_tasks(
            message.guild.id if message.guild else None
        )
        if message.author.id == self.owner_id:
            await self._close_owner_return_tasks(tasks, message)
            return False
        executed = False
        for task in tasks:
            if not self._message_matches(task, message):
                continue
            if await self._recently_executed(task, message.author.id):
                continue
            if await self._execute_message_task(task, message):
                await self._record_execution(
                    task,
                    event="message",
                    actor_id=message.author.id,
                    channel_id=getattr(message.channel, "id", 0),
                )
                executed = True
        return executed

    async def apply_member_join(self, member: discord.Member) -> int:
        tasks = await self._active_tasks(member.guild.id)
        executed = 0
        for task in tasks:
            if task.trigger_type != "member_join":
                continue
            if not self._member_matches(task, member):
                continue
            response = _render_response(task.response_text, member)
            try:
                await member.send(response[:1900])
            except (discord.Forbidden, discord.HTTPException):
                continue
            await self._record_execution(
                task,
                event="member_join",
                actor_id=member.id,
                channel_id=0,
            )
            executed += 1
        return executed

    async def apply_ticket_open(
        self,
        *,
        guild: discord.Guild,
        member: discord.Member,
        thread: discord.Thread,
        category: str,
    ) -> int:
        tasks = await self._active_tasks(guild.id)
        executed = 0
        for task in tasks:
            if task.trigger_type != "ticket_open":
                continue
            if not self._member_matches(task, member):
                continue
            if task.trigger_terms and not _contains_term(
                category,
                task.trigger_terms,
            ):
                continue
            try:
                await thread.send(
                    _render_response(task.response_text, member)[:1900],
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except (discord.Forbidden, discord.HTTPException):
                continue
            await self._record_execution(
                task,
                event="ticket_open",
                actor_id=member.id,
                channel_id=thread.id,
            )
            executed += 1
        return executed

    async def list_active(self, guild_id: int) -> list[SmartTask]:
        return await self._active_tasks(guild_id)

    async def disable(self, guild_id: int, task_id: str) -> bool:
        section = await self.state.get_section(SMART_TASKS_SECTION)
        raw = section.get(task_id)
        if not isinstance(raw, dict) or int(raw.get("guild_id", 0)) != guild_id:
            return False
        raw["enabled"] = False
        raw["updated_at"] = time.time()
        await self.state.update_section(SMART_TASKS_SECTION, task_id, raw)
        return True

    async def run_due_scheduled(self, *, limit: int = 5) -> int:
        due = [
            task
            for task in await self._active_tasks(None)
            if task.trigger_type == "schedule"
            and task.next_run_at is not None
            and task.next_run_at <= time.time()
        ][:limit]
        executed = 0
        for task in due:
            if await self._execute_scheduled_task(task):
                executed += 1
        return executed

    async def _execute_scheduled_task(self, task: SmartTask) -> bool:
        if not task.target_channel_id:
            await self._reschedule_task(task, failed=True)
            return False
        channel = self.bot.get_channel(task.target_channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(task.target_channel_id)
            except (discord.Forbidden, discord.HTTPException, discord.NotFound):
                await self._reschedule_task(task, failed=True)
                return False
        if not hasattr(channel, "send"):
            await self._reschedule_task(task, failed=True)
            return False
        try:
            if task.action_type == "generate_send_message":
                response = await self._generate_scheduled_response(task)
            else:
                response = task.response_text
            response = _sanitize_scheduled_response(
                _ensure_role_mentions(response, task.scope_role_ids)
            )[:1900]
            await channel.send(
                response,
                allowed_mentions=discord.AllowedMentions(
                    users=False,
                    roles=True,
                    everyone=False,
                ),
            )
            await self._record_scheduled_execution(task, response)
            await self._reschedule_task(task, failed=False)
            return True
        except Exception as exc:
            await self.state.set(
                "smart_task_schedule_last_error",
                f"{type(exc).__name__}: {exc}",
            )
            await self._reschedule_task(task, failed=True)
            return False

    async def _generate_scheduled_response(self, task: SmartTask) -> str:
        role_prefix = " ".join(f"<@&{role_id}>" for role_id in task.scope_role_ids)
        recent_outputs = await self._recent_scheduled_outputs(task.task_id)
        if not getattr(self.service, "available", False):
            raise RuntimeError(
                "IA indisponível para gerar conteúdo recorrente não repetitivo."
            )
        prompt = (
            "Gere a mensagem final que Bella publicará no Discord para uma tarefa "
            "recorrente. Responda somente com o texto final, sem explicar. Não use "
            "@everyone nem @here. Se existir menção de cargo obrigatória, mantenha "
            "no começo. O texto precisa ser novo e não pode repetir as ideias, "
            "frases ou estrutura dos envios recentes.\n\n"
            f"Persona oficial da Bella:\n{BELLA_PERSONA_RUNTIME}\n\n"
            f"Menção de cargo obrigatória: {role_prefix or 'nenhuma'}\n"
            f"Tarefa: {task.title}\n"
            f"Objetivo: {task.objective}\n"
            f"Instrução original: {task.raw_instruction}\n"
            f"Instrução de geração: {task.response_text}\n"
            f"Envios recentes para evitar: {recent_outputs[-8:]}"
        )
        text = await self.service.generate_text(
            prompt,
            max_output_tokens=650,
            timeout_seconds=12,
            model_policy="economy",
            use_cache=False,
        )
        if role_prefix and role_prefix not in text:
            return f"{role_prefix} {text}".strip()
        return text

    async def _recent_scheduled_outputs(self, task_id: str) -> list[str]:
        history = await self.state.get_section(SMART_TASK_HISTORY_SECTION)
        outputs: list[tuple[float, str]] = []
        prefix = f"{task_id}:schedule:"
        for key, value in history.items():
            if not key.startswith(prefix) or not isinstance(value, dict):
                continue
            outputs.append(
                (
                    float(value.get("executed_at", 0)),
                    str(value.get("response_text", ""))[:600],
                )
            )
        return [text for _, text in sorted(outputs)[-10:] if text]

    async def _record_scheduled_execution(
        self,
        task: SmartTask,
        response_text: str,
    ) -> None:
        now = time.time()
        task.execution_count += 1
        task.updated_at = now
        await self.state.update_section(
            SMART_TASKS_SECTION,
            task.task_id,
            task.to_dict(),
        )
        await self.state.update_section(
            SMART_TASK_HISTORY_SECTION,
            f"{task.task_id}:schedule:{int(now)}",
            {
                "task_id": task.task_id,
                "event": "schedule",
                "actor_id": 0,
                "channel_id": task.target_channel_id or 0,
                "response_text": response_text[:1200],
                "executed_at": now,
            },
        )

    async def _reschedule_task(self, task: SmartTask, *, failed: bool) -> None:
        now = datetime.now(DEFAULT_TZ)
        task.next_run_at = (
            (now + timedelta(minutes=5)).timestamp()
            if failed
            else _next_schedule_timestamp(
                task.schedule_frequency,
                task.schedule_hour,
                task.schedule_minute,
                weekday=task.schedule_weekday,
                month_day=task.schedule_month_day,
                now=now,
                after_current=True,
            )
        )
        task.updated_at = time.time()
        await self.state.update_section(
            SMART_TASKS_SECTION,
            task.task_id,
            task.to_dict(),
        )

    async def _interpret_once(self, message: discord.Message) -> SmartTask | None:
        guild = message.guild
        if guild is None:
            return None
        now = datetime.now(DEFAULT_TZ)
        guild_channels = list(guild.channels)
        guild_threads = list(getattr(guild, "threads", []))
        known_channels = [
            *guild_channels,
            *[
                thread
                for thread in guild_threads
                if all(thread.id != channel.id for channel in guild_channels)
            ],
        ]
        channels = [
            {
                "id": channel.id,
                "name": channel.name,
                "category_id": getattr(channel, "category_id", None),
                "type": type(channel).__name__,
            }
            for channel in known_channels
        ]
        referenced_channel_ids = _referenced_channel_ids(
            message,
            known_channels,
        )
        ticket_records = await self.state.get_section(TICKET_RECORD_SECTION)
        ticket_context = []
        for channel_id in referenced_channel_ids:
            record = ticket_records.get(str(channel_id))
            if not isinstance(record, dict):
                continue
            ticket_context.append(
                {
                    "thread_id": channel_id,
                    "creator_id": int(record.get("creator_id", 0)),
                    "creator_name": str(record.get("creator_name", "")),
                    "category": str(record.get("category", "")),
                    "status": str(record.get("status", "")),
                }
            )
        roles = [
            {"id": role.id, "name": role.name}
            for role in guild.roles
            if not role.is_default()
        ]
        prompt = (
            "Interprete UMA tarefa temporária ou persistente que Talisson está "
            "delegando à Bella em linguagem natural. Esta é a única chamada de "
            "IA: depois, o código executará tudo localmente. Extraia objetivo, "
            "gatilho, escopo, ação pronta e encerramento. Não transforme uma ordem "
            "administrativa imediata em tarefa.\n\n"
            "trigger_type permitido: schedule para rotinas por hor?rio como "
            "'todos os dias ?s 19:00', 'toda segunda' ou 'todo m?s'; owner_mention quando algu?m procurar ou "
            "any_message para qualquer mensagem dentro do escopo; direct_message "
            "para mensagens privadas; member_join para entrada; ticket_open para "
            "abertura de ticket.\n"
            "action_type permitido: reply, send_message, dm_user, notify_owner ou "
            "generate_send_message. Use generate_send_message quando o pedido exigir "
            "conte?do novo a cada execu??o, como reflex?o, mensagem motivacional, "
            "dica, curiosidade ou texto que n?o pode repetir.\n"
            "end_condition permitido: datetime, owner_message, manual ou never. "
            "Use owner_message para 'até eu voltar', 'até eu responder' ou "
            "equivalente. Se Talisson disser que o usuário que abriu um ticket deve "
            "receber uma resposta quando mandar mensagem, use trigger_type="
            "any_message, restrinja ao thread e ao creator_id informados em "
            "Tickets mencionados. response_text deve ser a mensagem final pronta para uso "
            "local, sem depender de nova IA. Preserve todos os links informados. "
            "Quando n?o houver target_channel_id use 0; quando n?o houver ends_at "
            "use uma string vazia. Para trigger_type=schedule, preencha "
            "target_channel_id com o canal de publica??o e preserve cargos "
            "mencionados em scope_role_ids. "
            "Use apenas IDs presentes nos catálogos ou na instrução.\n\n"
            f"Agora em São Paulo: {now.isoformat()}\n"
            f"Instrução: {message.content}\n"
            f"Canal atual: {message.channel.id}\n"
            f"Canais: {channels}\n"
            f"Tickets mencionados: {ticket_context}\n"
            f"Cargos: {roles}\n"
            f"Usuários mencionados: {[item.id for item in message.mentions]}\n"
            f"Cargos mencionados: {[item.id for item in message.role_mentions]}\n"
            f"Canais mencionados: {[item.id for item in message.channel_mentions]}"
        )
        data = await self.service.generate_json(
            prompt,
            _task_schema(),
            max_output_tokens=850,
            timeout_seconds=8,
            model_policy="economy",
            use_cache=False,
        )
        content = message.content.strip()[:1800]
        is_schedule_instruction = _looks_like_schedule_instruction(content)
        if float(data.get("confidence", 0)) < (0.70 if is_schedule_instruction else 0.78):
            return None
        allowed_channels = {channel.id for channel in known_channels}
        allowed_categories = {
            channel.id
            for channel in guild_channels
            if isinstance(channel, discord.CategoryChannel)
        }
        allowed_users = {member.id for member in guild.members}
        allowed_roles = {role.id for role in guild.roles}
        ends_at = _parse_datetime(str(data.get("ends_at", "")))
        end_condition = str(data.get("end_condition", "manual"))
        if end_condition == "datetime" and ends_at is None:
            ends_at = _infer_end_timestamp(content, now)
        response_text = str(data.get("response_text", "")).strip()[:1800]
        response_text = _preserve_links(response_text, content)
        if not response_text:
            raise ValueError("A interpretação não gerou a ação da tarefa.")
        task_id = hashlib.sha256(
            f"{guild.id}:{message.id}:{content}".encode("utf-8")
        ).hexdigest()[:16]
        scope_channel_ids = _allowed_ids(
            data.get("scope_channel_ids"),
            allowed_channels,
        )
        if (
            not scope_channel_ids
            and referenced_channel_ids
            and _targets_specific_channel(content)
        ):
            scope_channel_ids = referenced_channel_ids[:25]
        scope_user_ids = _allowed_ids(
            data.get("scope_user_ids"),
            allowed_users,
        )
        if (
            ticket_context
            and _targets_ticket_creator(content)
            and not scope_user_ids
        ):
            scope_user_ids = [
                item["creator_id"]
                for item in ticket_context
                if item["creator_id"] in allowed_users
            ][:25]
        trigger_type = str(data.get("trigger_type", "keyword"))
        if is_schedule_instruction:
            trigger_type = "schedule"
        if ticket_context and _targets_ticket_creator_message(content):
            trigger_type = "any_message"
        if _ends_when_owner_replies(content):
            end_condition = "owner_message"
            ends_at = None
        scope_role_ids = _allowed_ids(
            data.get("scope_role_ids"),
            allowed_roles,
        )
        target_channel_id = _allowed_id(
            data.get("target_channel_id"),
            allowed_channels,
        )
        schedule = _schedule_from_data(data, content, now)
        action_type = str(data.get("action_type", "reply"))
        if trigger_type == "schedule":
            if target_channel_id is None and referenced_channel_ids:
                target_channel_id = referenced_channel_ids[0]
            if target_channel_id is None:
                target_channel_id = getattr(message.channel, "id", None)
            if not response_text:
                response_text = _scheduled_instruction_from_content(content)
            response_text = _ensure_role_mentions(response_text, scope_role_ids)
            if _needs_generated_scheduled_content(content, response_text):
                action_type = "generate_send_message"
            else:
                action_type = "send_message"
        return SmartTask(
            task_id=task_id,
            guild_id=guild.id,
            owner_id=OWNER_DISCORD_ID,
            title=str(data.get("title", "Tarefa da Bella")).strip()[:100]
            or "Tarefa da Bella",
            objective=str(data.get("objective", "")).strip()[:500],
            trigger_type=trigger_type,
            trigger_terms=[
                _normalize(str(value))
                for value in data.get("trigger_terms", [])[:20]
                if str(value).strip()
            ],
            action_type=action_type,
            response_text=response_text,
            scope_channel_ids=scope_channel_ids,
            scope_category_ids=_allowed_ids(
                data.get("scope_category_ids"),
                allowed_categories,
            ),
            scope_user_ids=scope_user_ids,
            scope_role_ids=scope_role_ids,
            target_channel_id=target_channel_id,
            starts_at=time.time(),
            ends_at=ends_at,
            end_condition=end_condition,
            enabled=True,
            raw_instruction=content,
            created_at=time.time(),
            updated_at=time.time(),
            schedule_frequency=str(schedule.get("frequency", "")),
            schedule_hour=_optional_int(schedule.get("hour")),
            schedule_minute=_optional_int(schedule.get("minute")),
            schedule_weekday=_optional_int(schedule.get("weekday")),
            schedule_month_day=_optional_int(schedule.get("month_day")),
            next_run_at=(
                _next_schedule_timestamp(
                    str(schedule.get("frequency", "")),
                    _optional_int(schedule.get("hour")),
                    _optional_int(schedule.get("minute")),
                    weekday=_optional_int(schedule.get("weekday")),
                    month_day=_optional_int(schedule.get("month_day")),
                    now=now,
                )
                if trigger_type == "schedule"
                else None
            ),
        )

    async def _active_tasks(self, guild_id: int | None) -> list[SmartTask]:
        section = await self.state.get_section(SMART_TASKS_SECTION)
        now = time.time()
        tasks = []
        for task_id, raw in section.items():
            if not isinstance(raw, dict):
                continue
            try:
                task = SmartTask.from_dict(raw)
            except (TypeError, ValueError):
                continue
            if not task.enabled:
                continue
            if guild_id is not None and task.guild_id != guild_id:
                continue
            if task.ends_at is not None and task.ends_at <= now:
                raw["enabled"] = False
                raw["updated_at"] = now
                await self.state.update_section(SMART_TASKS_SECTION, task_id, raw)
                continue
            tasks.append(task)
        return tasks

    async def _close_owner_return_tasks(
        self,
        tasks: list[SmartTask],
        message: discord.Message,
    ) -> None:
        if self.looks_like_instruction(message.content):
            return
        for task in tasks:
            if task.end_condition != "owner_message":
                continue
            if (
                task.scope_channel_ids
                and getattr(message.channel, "id", 0)
                not in task.scope_channel_ids
            ):
                continue
            task.enabled = False
            task.updated_at = time.time()
            await self.state.update_section(
                SMART_TASKS_SECTION,
                task.task_id,
                task.to_dict(),
            )

    def _message_matches(
        self,
        task: SmartTask,
        message: discord.Message,
    ) -> bool:
        if message.author.bot or not self._scope_matches(task, message):
            return False
        content = message.content
        if task.trigger_type == "direct_message":
            return message.guild is None
        if task.trigger_type == "owner_mention":
            mentioned_ids = {user.id for user in message.mentions}
            return self.owner_id in mentioned_ids or any(
                name in _normalize(content)
                for name in ("talisson", "talinho", "tali")
            )
        if task.trigger_type == "any_message":
            return True
        if task.trigger_type == "keyword":
            return _contains_term(content, task.trigger_terms)
        return False

    def _scope_matches(
        self,
        task: SmartTask,
        message: discord.Message,
    ) -> bool:
        if message.guild is None:
            return task.trigger_type == "direct_message"
        channel_id = getattr(message.channel, "id", 0)
        if task.scope_channel_ids and channel_id not in task.scope_channel_ids:
            return False
        category_id = getattr(message.channel, "category_id", None)
        if task.scope_category_ids and category_id not in task.scope_category_ids:
            return False
        if task.scope_user_ids and message.author.id not in task.scope_user_ids:
            return False
        if task.scope_role_ids:
            role_ids = {
                role.id for role in getattr(message.author, "roles", [])
            }
            if not role_ids.intersection(task.scope_role_ids):
                return False
        return True

    def _member_matches(self, task: SmartTask, member: discord.Member) -> bool:
        if task.scope_user_ids and member.id not in task.scope_user_ids:
            return False
        if task.scope_role_ids:
            role_ids = {role.id for role in member.roles}
            if not role_ids.intersection(task.scope_role_ids):
                return False
        return True

    async def _execute_message_task(
        self,
        task: SmartTask,
        message: discord.Message,
    ) -> bool:
        response = _render_response(task.response_text, message.author)[:1900]
        try:
            if task.action_type == "dm_user":
                await message.author.send(response)
            elif task.action_type == "notify_owner":
                owner = self.bot.get_user(self.owner_id)
                if owner is None:
                    owner = await self.bot.fetch_user(self.owner_id)
                await owner.send(
                    f"{response}\n\nOrigem: {getattr(message, 'jump_url', '')}"[:1900]
                )
            elif task.action_type == "send_message" and task.target_channel_id:
                channel = self.bot.get_channel(task.target_channel_id)
                if channel is None:
                    channel = await self.bot.fetch_channel(task.target_channel_id)
                if not hasattr(channel, "send"):
                    return False
                await channel.send(
                    response,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            else:
                await message.reply(
                    response,
                    mention_author=False,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            return True
        except (discord.Forbidden, discord.HTTPException):
            return False

    async def _recently_executed(
        self,
        task: SmartTask,
        actor_id: int,
    ) -> bool:
        history = await self.state.get_section(SMART_TASK_HISTORY_SECTION)
        raw = history.get(f"{task.task_id}:{actor_id}", {})
        return bool(
            isinstance(raw, dict)
            and time.time() - float(raw.get("executed_at", 0)) < 120
        )

    async def _record_execution(
        self,
        task: SmartTask,
        *,
        event: str,
        actor_id: int,
        channel_id: int,
    ) -> None:
        now = time.time()
        task.execution_count += 1
        task.updated_at = now
        await self.state.update_section(
            SMART_TASKS_SECTION,
            task.task_id,
            task.to_dict(),
        )
        await self.state.update_section(
            SMART_TASK_HISTORY_SECTION,
            f"{task.task_id}:{actor_id}",
            {
                "task_id": task.task_id,
                "event": event,
                "actor_id": actor_id,
                "channel_id": channel_id,
                "executed_at": now,
            },
        )

    async def _clear_operational_pending(
        self,
        guild_id: int,
        user_id: int,
    ) -> None:
        key = f"{guild_id}:{user_id}"

        def remove(records: dict[str, Any]) -> None:
            records.pop(key, None)

        await self.state.mutate_section(OPERATIONAL_PENDING_SECTION, remove)


def _referenced_channel_ids(
    message: discord.Message,
    known_channels: list[Any],
) -> list[int]:
    allowed = {
        int(channel.id)
        for channel in known_channels
        if getattr(channel, "id", None)
    }
    values = [
        int(channel.id)
        for channel in getattr(message, "channel_mentions", [])
        if getattr(channel, "id", None)
    ]
    values.extend(
        int(value)
        for value in re.findall(r"<#(\d+)>", message.content)
    )
    normalized = _normalize(message.content)
    if not values and any(
        marker in normalized
        for marker in (
            "nesse ticket",
            "neste ticket",
            "ticket aqui",
            "nesse canal",
            "neste canal",
            "canal aqui",
        )
    ):
        current_id = _optional_int(getattr(message.channel, "id", None))
        if current_id is not None:
            values.append(current_id)
    result = []
    for value in values:
        if value in allowed and value not in result:
            result.append(value)
    return result[:25]


def _targets_specific_channel(content: str) -> bool:
    text = _normalize(content)
    return bool(
        re.search(r"<#\d+>", content)
        or any(
            marker in text
            for marker in (
                "nesse ticket",
                "neste ticket",
                "ticket aqui",
                "nesse canal",
                "neste canal",
                "canal aqui",
            )
        )
    )


def _targets_ticket_creator(content: str) -> bool:
    text = _normalize(content)
    return any(
        marker in text
        for marker in (
            "usuario que abriu",
            "usuaria que abriu",
            "pessoa que abriu",
            "membro que abriu",
            "autor do ticket",
            "dono do ticket",
            "quem abriu o ticket",
        )
    )


def _targets_ticket_creator_message(content: str) -> bool:
    text = _normalize(content)
    return _targets_ticket_creator(content) and any(
        marker in text
        for marker in (
            "mandar mensagem",
            "enviar mensagem",
            "me responder",
            "responder",
            "falar",
            "escrever",
        )
    )


def _ends_when_owner_replies(content: str) -> bool:
    text = _normalize(content)
    return any(
        marker in text
        for marker in (
            "assim que eu responder",
            "quando eu responder",
            "se eu responder",
            "ate eu responder",
            "ate eu voltar",
            "quando eu voltar",
            "assim que eu voltar",
            "quando eu mandar mensagem",
        )
    )


def _task_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "objective": {"type": "string"},
            "trigger_type": {
                "type": "string",
                "enum": [
                    "schedule",
                    "owner_mention",
                    "keyword",
                    "any_message",
                    "direct_message",
                    "member_join",
                    "ticket_open",
                ],
            },
            "trigger_terms": {
                "type": "array",
                "items": {"type": "string"},
            },
            "action_type": {
                "type": "string",
                "enum": [
                    "reply",
                    "send_message",
                    "dm_user",
                    "notify_owner",
                    "generate_send_message",
                ],
            },
            "response_text": {"type": "string"},
            "scope_channel_ids": {
                "type": "array",
                "items": {"type": "integer"},
            },
            "scope_category_ids": {
                "type": "array",
                "items": {"type": "integer"},
            },
            "scope_user_ids": {
                "type": "array",
                "items": {"type": "integer"},
            },
            "scope_role_ids": {
                "type": "array",
                "items": {"type": "integer"},
            },
            "target_channel_id": {"type": "integer"},
            "schedule_frequency": {
                "type": "string",
                "enum": ["", "daily", "weekly", "monthly"],
            },
            "schedule_hour": {"type": "integer"},
            "schedule_minute": {"type": "integer"},
            "schedule_weekday": {"type": "integer"},
            "schedule_month_day": {"type": "integer"},
            "ends_at": {"type": "string"},
            "end_condition": {
                "type": "string",
                "enum": ["datetime", "owner_message", "manual", "never"],
            },
            "confidence": {
                "type": "number",
                "minimum": 0,
                "maximum": 1,
            },
        },
        "required": [
            "title",
            "objective",
            "trigger_type",
            "trigger_terms",
            "action_type",
            "response_text",
            "scope_channel_ids",
            "scope_category_ids",
            "scope_user_ids",
            "scope_role_ids",
            "target_channel_id",
            "schedule_frequency",
            "schedule_hour",
            "schedule_minute",
            "schedule_weekday",
            "schedule_month_day",
            "ends_at",
            "end_condition",
            "confidence",
        ],
    }


def _describe_trigger(task: SmartTask) -> str:
    names = {
        "schedule": "rotina agendada",
        "owner_mention": "quando procurarem ou mencionarem Talisson",
        "keyword": (
            "mensagem relacionada a "
            + ", ".join(task.trigger_terms[:5])
            if task.trigger_terms
            else "mensagem relacionada ao assunto configurado"
        ),
        "any_message": "qualquer mensagem dentro do escopo",
        "direct_message": "mensagem privada",
        "member_join": "entrada de membro",
        "ticket_open": "abertura de ticket",
    }
    return names.get(task.trigger_type, task.trigger_type)


def _describe_scope(task: SmartTask) -> str:
    values = []
    values.extend(f"<#{item}>" for item in task.scope_channel_ids)
    values.extend(f"categoria `{item}`" for item in task.scope_category_ids)
    values.extend(f"<@{item}>" for item in task.scope_user_ids)
    values.extend(f"<@&{item}>" for item in task.scope_role_ids)
    return ", ".join(values) if values else "servidor inteiro"


def _describe_ending(task: SmartTask) -> str:
    if task.trigger_type == "schedule":
        next_run = (
            datetime.fromtimestamp(task.next_run_at, DEFAULT_TZ).strftime(
                "%d/%m/%Y às %H:%M"
            )
            if task.next_run_at
            else "não calculada"
        )
        frequency = {
            "daily": "todos os dias",
            "weekly": "toda semana",
            "monthly": "todo mês",
        }.get(task.schedule_frequency, "recorrente")
        return f"{frequency}; próxima execução: {next_run}"
    if task.end_condition == "owner_message":
        return "automaticamente quando Talisson voltar a responder"
    if task.ends_at:
        return datetime.fromtimestamp(task.ends_at, DEFAULT_TZ).strftime(
            "%d/%m/%Y às %H:%M"
        )
    if task.end_condition == "never":
        return "sem prazo definido"
    return "manual"


def _render_response(text: str, user: discord.abc.User) -> str:
    display_name = str(
        getattr(user, "display_name", "")
        or getattr(user, "name", "")
        or user.id
    )
    return (
        text.replace("{usuario}", display_name)
        .replace("{user}", display_name)
        .replace("{mention}", display_name)
    )


def _contains_term(content: str, terms: list[str]) -> bool:
    text = _normalize(content)
    return any(term and term in text for term in terms)


def _preserve_links(response: str, instruction: str) -> str:
    links = re.findall(r"https?://[^\s>)\]]+", instruction)
    missing = [link for link in links if link not in response]
    if not missing:
        return response
    return f"{response.rstrip()}\n\n" + "\n".join(missing)


def _infer_end_timestamp(content: str, now: datetime) -> float | None:
    text = _normalize(content)
    if "hoje" in text:
        return now.replace(hour=23, minute=59, second=59).timestamp()
    if "amanha" in text:
        return (now + timedelta(days=1)).replace(
            hour=23,
            minute=59,
            second=59,
        ).timestamp()
    if "essa semana" in text or "esta semana" in text:
        days = 6 - now.weekday()
        return (now + timedelta(days=days)).replace(
            hour=23,
            minute=59,
            second=59,
        ).timestamp()
    return None


def _looks_like_schedule_instruction(content: str) -> bool:
    text = _normalize(content)
    return any(
        marker in text
        for marker in (
            "todos os dias",
            "todo dia",
            "diariamente",
            "toda semana",
            "semanalmente",
            "todo mes",
            "todo mês",
            "mensalmente",
            "toda segunda",
            "toda terca",
            "toda terça",
            "toda quarta",
            "toda quinta",
            "toda sexta",
            "todo sabado",
            "todo sábado",
            "todo domingo",
        )
    )


def _schedule_from_data(
    data: dict[str, Any],
    content: str,
    now: datetime,
) -> dict[str, int | str | None]:
    parsed = _schedule_from_content(content, now)
    frequency = str(data.get("schedule_frequency", "") or parsed["frequency"])
    hour = _optional_int(data.get("schedule_hour"))
    minute = _optional_int(data.get("schedule_minute"))
    weekday = _optional_int(data.get("schedule_weekday"))
    month_day = _optional_int(data.get("schedule_month_day"))
    return {
        "frequency": frequency or parsed["frequency"],
        "hour": hour if hour is not None and 0 <= hour <= 23 else parsed["hour"],
        "minute": minute if minute is not None and 0 <= minute <= 59 else parsed["minute"],
        "weekday": (
            weekday
            if weekday is not None and 0 <= weekday <= 6
            else parsed["weekday"]
        ),
        "month_day": (
            month_day
            if month_day is not None and 1 <= month_day <= 28
            else parsed["month_day"]
        ),
    }


def _schedule_from_content(
    content: str,
    now: datetime,
) -> dict[str, int | str | None]:
    text = _normalize(content)
    hour, minute = _extract_hour_minute(content)
    frequency = "daily"
    weekday: int | None = None
    month_day: int | None = None
    weekdays = {
        "segunda": 0,
        "terca": 1,
        "terça": 1,
        "quarta": 2,
        "quinta": 3,
        "sexta": 4,
        "sabado": 5,
        "sábado": 5,
        "domingo": 6,
    }
    for name, value in weekdays.items():
        if name in text:
            frequency = "weekly"
            weekday = value
            break
    if "semanalmente" in text or "toda semana" in text:
        frequency = "weekly"
        weekday = now.weekday()
    if "mensalmente" in text or "todo mes" in text or "todo mês" in text:
        frequency = "monthly"
        month_day = min(now.day, 28)
    return {
        "frequency": frequency,
        "hour": hour if hour is not None else 9,
        "minute": minute if minute is not None else 0,
        "weekday": weekday,
        "month_day": month_day,
    }


def _extract_hour_minute(content: str) -> tuple[int | None, int | None]:
    match = re.search(
        r"(?:às|as|a\s+partir\s+das|por volta das|das|de)\s*"
        r"(\d{1,2})(?::(\d{2}))?\s*(?:h|horas?)?",
        content,
        flags=re.IGNORECASE,
    )
    if not match:
        match = re.search(r"\b(\d{1,2}):(\d{2})\b", content)
    if not match:
        return None, None
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        return None, None
    return hour, minute


def _next_schedule_timestamp(
    frequency: str,
    hour: int | None,
    minute: int | None,
    *,
    weekday: int | None = None,
    month_day: int | None = None,
    now: datetime | None = None,
    after_current: bool = False,
) -> float | None:
    if frequency not in {"daily", "weekly", "monthly"}:
        return None
    current = now or datetime.now(DEFAULT_TZ)
    scheduled = current.replace(
        hour=hour if hour is not None else 9,
        minute=minute if minute is not None else 0,
        second=0,
        microsecond=0,
    )
    if frequency == "weekly":
        target_weekday = weekday if weekday is not None else current.weekday()
        days = (target_weekday - current.weekday()) % 7
        scheduled = scheduled + timedelta(days=days)
        if scheduled <= current or after_current:
            scheduled += timedelta(days=7)
    elif frequency == "monthly":
        target_day = month_day if month_day is not None else min(current.day, 28)
        scheduled = scheduled.replace(day=min(target_day, 28))
        if scheduled <= current or after_current:
            month = scheduled.month + 1
            year = scheduled.year
            if month > 12:
                month = 1
                year += 1
            scheduled = scheduled.replace(year=year, month=month, day=min(target_day, 28))
    elif scheduled <= current or after_current:
        scheduled += timedelta(days=1)
    return scheduled.timestamp()


def _scheduled_instruction_from_content(content: str) -> str:
    cleaned = _strip_bot_reference(content)
    cleaned = re.sub(r"<#\d{15,22}>", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:900] or "Gere uma mensagem natural para a rotina configurada."


def _needs_generated_scheduled_content(content: str, response_text: str) -> bool:
    text = _normalize(f"{content} {response_text}")
    return any(
        marker in text
        for marker in (
            "reflexao",
            "reflexão",
            "motivacional",
            "mensagem do dia",
            "frase do dia",
            "dica",
            "curiosidade",
            "conteudo",
            "conteúdo",
            "gerar",
            "gere",
            "nao repetir",
            "não repetir",
            "nao repita",
            "não repita",
            "variada",
            "variado",
            "unica",
            "única",
        )
    )


def _ensure_role_mentions(text: str, role_ids: list[int]) -> str:
    if not role_ids:
        return text
    prefix = " ".join(f"<@&{role_id}>" for role_id in role_ids)
    return text if all(f"<@&{role_id}>" in text for role_id in role_ids) else f"{prefix} {text}".strip()


def _sanitize_scheduled_response(text: str) -> str:
    return (
        str(text or "")
        .replace("@everyone", "@\u200beveryone")
        .replace("@here", "@\u200bhere")
        .strip()
    )


def _parse_datetime(value: str) -> float | None:
    cleaned = value.strip()
    if not cleaned:
        return None
    try:
        parsed = datetime.fromisoformat(cleaned.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=DEFAULT_TZ)
    return parsed.timestamp()


def _allowed_ids(values: Any, allowed: set[int]) -> list[int]:
    if not isinstance(values, list):
        return []
    result = []
    for value in values:
        parsed = _optional_int(value)
        if parsed is not None and parsed in allowed and parsed not in result:
            result.append(parsed)
    return result[:25]


def _allowed_id(value: Any, allowed: set[int]) -> int | None:
    parsed = _optional_int(value)
    return parsed if parsed in allowed else None


def _optional_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _optional_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _strip_bot_reference(value: str) -> str:
    value = re.sub(r"<@!?\d+>", " ", value)
    value = re.sub(r"^\s*(?:senhor\s+)?jarvis\b[\s,:-]*", "", value, flags=re.I)
    return value.strip()


def _normalize(value: str) -> str:
    decomposed = normalize("NFKD", str(value or "").casefold())
    ascii_text = "".join(char for char in decomposed if not combining(char))
    return " ".join(ascii_text.split())
