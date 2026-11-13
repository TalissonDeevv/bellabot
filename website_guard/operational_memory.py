from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from unicodedata import combining, normalize

import discord

from .identity import OWNER_DISCORD_ID
from .state import StateStore

RULES_SECTION = "operational_rules"
PENDING_SECTION = "operational_rule_pending"
RULE_TRIGGER = re.compile(
    r"\b(?:a partir de agora|lembre(?:-se)? que|salve (?:a )?(?:seguinte )?regra|"
    r"registre (?:essa|esta|a seguinte)?\s*diretriz|nunca faca|sempre faca|"
    r"quando acontecer|regra permanente|diretriz permanente)\b",
    re.IGNORECASE,
)
LIST_TRIGGER = re.compile(
    r"\b(?:liste|listar|mostre|mostrar|quais)\b.*\b"
    r"(?:regras|diretrizes|memorias operacionais)\b",
    re.IGNORECASE,
)
REMOVE_TRIGGER = re.compile(
    r"\b(?:remova|remover|apague|apagar|exclua|excluir)\b.*\b"
    r"(?:regra|diretriz)\b",
    re.IGNORECASE,
)
UPDATE_TRIGGER = re.compile(
    r"\b(?:atualize|atualizar|edite|editar|substitua|substituir)\b.*\b"
    r"(?:regra|diretriz)\b",
    re.IGNORECASE,
)


@dataclass
class OperationalRule:
    rule_id: str
    guild_id: int
    name: str
    instruction: str
    policy_type: str
    channel_id: int | None
    require_question: bool
    require_bot_mention: bool
    delete_noncompliant: bool
    trigger_media: bool
    condition_type: str
    keywords: tuple[str, ...]
    moderation_action: str
    duration_seconds: int | None
    log_channel_id: int | None
    guidance: str
    topic: str
    required_context: tuple[str, ...]
    auto_assist: bool
    enabled: bool
    created_at: str
    updated_at: str
    updated_by: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "OperationalRule":
        return cls(
            rule_id=str(data.get("rule_id", "")),
            guild_id=int(data.get("guild_id", 0)),
            name=str(data.get("name", "Regra operacional")),
            instruction=str(data.get("instruction", "")),
            policy_type=str(data.get("policy_type", "guidance")),
            channel_id=(
                int(data["channel_id"]) if data.get("channel_id") else None
            ),
            require_question=bool(data.get("require_question")),
            require_bot_mention=bool(data.get("require_bot_mention")),
            delete_noncompliant=bool(data.get("delete_noncompliant")),
            trigger_media=bool(data.get("trigger_media")),
            condition_type=str(data.get("condition_type", "")),
            keywords=tuple(str(item) for item in data.get("keywords", [])),
            moderation_action=str(data.get("moderation_action", "none")),
            duration_seconds=(
                int(data["duration_seconds"])
                if data.get("duration_seconds")
                else None
            ),
            log_channel_id=(
                int(data["log_channel_id"])
                if data.get("log_channel_id")
                else None
            ),
            guidance=str(data.get("guidance", "")),
            topic=str(data.get("topic", "")),
            required_context=tuple(
                str(item)
                for item in data.get("required_context", [])
                if str(item).strip()
            ),
            auto_assist=bool(data.get("auto_assist")),
            enabled=bool(data.get("enabled", True)),
            created_at=str(data.get("created_at", "")),
            updated_at=str(data.get("updated_at", "")),
            updated_by=int(data.get("updated_by", 0)),
        )


class OperationalMemory:
    def __init__(
        self,
        state: StateStore,
        bot: discord.Client,
        ai_service: Any | None = None,
    ) -> None:
        self.state = state
        self.bot = bot
        self.ai_service = ai_service

    @staticmethod
    def looks_like_instruction(content: str) -> bool:
        normalized = _normalize(content)
        return bool(
            RULE_TRIGGER.search(content)
            or LIST_TRIGGER.search(content)
            or REMOVE_TRIGGER.search(content)
            or UPDATE_TRIGGER.search(content)
            or _is_rule_trigger(normalized)
            or _is_list_request(normalized)
            or _is_remove_request(normalized)
            or _is_update_request(normalized)
        )

    async def handle_instruction(self, message: discord.Message) -> bool:
        if message.guild is None:
            return False
        pending = await self._pending(message.guild.id, message.author.id)
        looks_like = self.looks_like_instruction(message.content)
        if message.author.id != OWNER_DISCORD_ID:
            return looks_like
        normalized = _normalize(message.content)
        if pending:
            return await self._handle_confirmation(message, pending)
        if LIST_TRIGGER.search(message.content) or _is_list_request(normalized):
            await self._list_rules(message)
            return True
        if REMOVE_TRIGGER.search(message.content) or _is_remove_request(normalized):
            await self._remove_rule(message)
            return True
        if UPDATE_TRIGGER.search(message.content) or _is_update_request(normalized):
            await self._save_rule(message, replace=True)
            return True
        if RULE_TRIGGER.search(message.content) or _is_rule_trigger(normalized):
            await self._save_rule(message, replace=False)
            return True
        if self._looks_like_clear_rule(message):
            await self._save_rule(message, replace=False)
            return True
        if self._looks_like_ambiguous_rule(message):
            await self._save_pending(message)
            await message.reply(
                "Essa instrução deve ser salva como regra operacional permanente?",
                mention_author=False,
            )
            return True
        return False

    async def apply(self, message: discord.Message) -> bool:
        if (
            message.guild is None
            or message.author.bot
            or message.author.id == OWNER_DISCORD_ID
        ):
            return False
        rules = await self.rules_for(
            message.guild.id,
            channel_id=message.channel.id,
        )
        for rule in rules:
            if rule.policy_type == "themed_help_channel":
                missing = self._missing_required_context(message, rule)
                if not missing:
                    continue
                if not rule.delete_noncompliant:
                    continue
                try:
                    await message.delete()
                except (discord.Forbidden, discord.HTTPException):
                    return False
                guidance = self._context_guidance(rule, missing)
                try:
                    await message.channel.send(
                        f"{message.author.mention} {guidance}",
                        allowed_mentions=discord.AllowedMentions(
                            users=[message.author],
                            roles=False,
                            everyone=False,
                        ),
                        delete_after=45,
                    )
                except (discord.Forbidden, discord.HTTPException):
                    pass
                await self._record_application(
                    message,
                    rule,
                    action="insufficient_context_deleted",
                )
                return True
            if rule.policy_type == "moderation_rule":
                if not await self._moderation_rule_matches(message, rule):
                    continue
                await self._apply_moderation_rule(message, rule)
                return True
            if rule.policy_type != "question_only_channel":
                continue
            if self._message_complies(message, rule):
                continue
            if not rule.delete_noncompliant:
                continue
            try:
                await message.delete()
            except (discord.Forbidden, discord.HTTPException):
                return False
            try:
                await message.channel.send(
                    f"{message.author.mention} {rule.guidance}",
                    allowed_mentions=discord.AllowedMentions(
                        users=[message.author],
                        roles=False,
                        everyone=False,
                    ),
                    delete_after=25,
                )
            except (discord.Forbidden, discord.HTTPException):
                pass
            await self._record_application(message, rule)
            return True
        return False

    async def applicable_guidance(
        self,
        guild_id: int,
        channel_id: int,
    ) -> list[str]:
        guidance = []
        for rule in await self.rules_for(guild_id, channel_id=channel_id):
            if not rule.enabled:
                continue
            text = rule.instruction
            if rule.policy_type == "themed_help_channel":
                required = ", ".join(rule.required_context)
                text += (
                    f"\nTema obrigatório: {rule.topic or 'tema configurado'}."
                    f"\nContexto obrigatório: {required}."
                    "\nAjude tecnicamente o usuário quando a mensagem estiver "
                    "completa e dentro do tema."
                )
            guidance.append(text)
        return guidance[:10]

    async def should_auto_assist(
        self,
        guild_id: int,
        channel_id: int,
    ) -> bool:
        return any(
            rule.policy_type == "themed_help_channel" and rule.auto_assist
            for rule in await self.rules_for(guild_id, channel_id=channel_id)
        )

    async def interpret_rule_spec(
        self,
        content: str,
        guild: discord.Guild,
    ) -> dict[str, Any] | None:
        service = self.ai_service
        if not service or not getattr(service, "available", False):
            return None
        guild_channels = getattr(guild, "channels", [])
        if isinstance(guild_channels, dict):
            guild_channels = list(guild_channels.values())
        else:
            guild_channels = list(guild_channels)
        channels = [
            {
                "id": channel.id,
                "name": str(getattr(channel, "name", "")),
                "type": type(channel).__name__,
            }
            for channel in guild_channels[:150]
            if getattr(channel, "id", None)
        ]
        prompt = (
            "Interprete uma política operacional permanente criada pelo "
            "proprietário de um servidor Discord. Retorne somente o schema JSON. "
            "Não execute nada e não invente canais.\n\n"
            "Tipos:\n"
            "- themed_help_channel: canal com tema específico, checklist obrigatório, "
            "ajuda automática da Bella e possível exclusão de mensagens incompletas.\n"
            "- question_only_channel: canal reservado a perguntas, sem checklist "
            "técnico detalhado.\n"
            "- moderation_rule: condição que dispara delete, aviso, timeout, kick ou ban.\n"
            "- guidance: orientação sem ação automática.\n\n"
            "Para canais de ajuda, extraia topic, required_context e auto_assist. "
            "required_context deve conter rótulos objetivos, por exemplo: "
            "linguagem/tecnologia, objetivo, código relevante, mensagem de erro, "
            "o que já tentou. Se mensagens sem contexto devem ser apagadas, use "
            "condition_type=insufficient_context, action=delete e "
            "delete_message=true.\n\n"
            "Outras condições permitidas: image, attachment, link, invite, keyword, "
            "caps, vague, non_question, semantic e any_message. Ações permitidas: "
            "none, delete, warn, timeout, kick, ban_permanent e ban_temporary. "
            "Use somente IDs do catálogo ou menções existentes na instrução. "
            "Se houver ambiguidade real, use confidence baixa.\n\n"
            f"Instrução: {content}\n"
            f"Canais: {channels}"
        )
        try:
            return await service.generate_json(
                prompt,
                _rule_interpretation_schema(),
                max_output_tokens=850,
                timeout_seconds=7,
                model_policy="economy",
                use_cache=True,
            )
        except Exception as exc:
            await self.state.set(
                "operational_rule_ai_last_error",
                f"{type(exc).__name__}: {exc}",
            )
            return None

    async def register_text_rule(
        self,
        *,
        guild_id: int,
        content: str,
        author_id: int,
        guild: discord.Guild | None = None,
        ai_spec: dict[str, Any] | None = None,
    ) -> OperationalRule:
        instruction = self._clean_instruction(content)
        channel_ids = _extract_channel_ids(content, guild)
        channel_id = channel_ids[0] if channel_ids else None
        log_channel_id = channel_ids[1] if len(channel_ids) > 1 else None
        if ai_spec:
            channel_id = channel_id or _optional_int(ai_spec.get("channel_id"))
            log_channel_id = log_channel_id or _optional_int(
                ai_spec.get("log_channel_id")
            )
        parsed = self._parse_rule(
            guild_id,
            instruction,
            channel_id=channel_id,
            log_channel_id=log_channel_id,
            author_id=author_id,
            ai_spec=ai_spec,
        )
        existing = await self._matching_rule(
            guild_id,
            channel_id=channel_id,
            text=instruction,
        )
        governance_types = {"question_only_channel", "themed_help_channel"}
        if existing and (
            parsed.policy_type == existing.policy_type
            or {
                parsed.policy_type,
                existing.policy_type,
            }.issubset(governance_types)
        ):
            parsed.rule_id = existing.rule_id
            parsed.created_at = existing.created_at
            parsed.name = existing.name
        await self.state.update_section(
            RULES_SECTION,
            parsed.rule_id,
            parsed.to_dict(),
        )
        return parsed

    async def rules_for(
        self,
        guild_id: int,
        *,
        channel_id: int | None = None,
    ) -> list[OperationalRule]:
        records = await self.state.get_section(RULES_SECTION)
        result = []
        for value in records.values():
            if not isinstance(value, dict):
                continue
            rule = OperationalRule.from_dict(value)
            if rule.guild_id != guild_id or not rule.enabled:
                continue
            if rule.channel_id is not None and rule.channel_id != channel_id:
                continue
            result.append(rule)
        result.sort(key=lambda item: item.updated_at)
        return result

    async def _save_rule(
        self,
        message: discord.Message,
        *,
        replace: bool,
    ) -> None:
        instruction = self._clean_instruction(message.content)
        if not instruction or len(instruction) < 12:
            await message.reply(
                "Informe a regra completa que deseja registrar.",
                mention_author=False,
            )
            return
        if len(instruction) > 1800:
            await message.reply(
                "A regra é muito extensa. Resuma a instrução em até 1.800 caracteres.",
                mention_author=False,
            )
            return
        channel_id = (
            message.channel_mentions[0].id
            if message.channel_mentions
            else None
        )
        log_channel_id = (
            message.channel_mentions[1].id
            if len(message.channel_mentions) > 1
            else None
        )
        ai_spec = await self.interpret_rule_spec(
            instruction,
            message.guild,
        )
        parsed = self._parse_rule(
            message.guild.id,
            instruction,
            channel_id=channel_id,
            log_channel_id=log_channel_id,
            author_id=message.author.id,
            ai_spec=ai_spec,
        )
        existing = await self._matching_rule(
            message.guild.id,
            channel_id=channel_id,
            text=instruction,
        )
        governance_types = {"question_only_channel", "themed_help_channel"}
        if existing and (
            replace
            or parsed.policy_type == existing.policy_type
            or {
                parsed.policy_type,
                existing.policy_type,
            }.issubset(governance_types)
        ):
            parsed.rule_id = existing.rule_id
            parsed.created_at = existing.created_at
            parsed.name = existing.name
        await self.state.update_section(
            RULES_SECTION,
            parsed.rule_id,
            parsed.to_dict(),
        )
        verb = "atualizada" if existing else "registrada"
        scope = (
            f" para <#{parsed.channel_id}>"
            if parsed.channel_id
            else " para todo o servidor"
        )
        await message.reply(
            f"Regra operacional {verb}{scope}. Ela já está ativa.",
            mention_author=False,
        )

    async def _remove_rule(self, message: discord.Message) -> None:
        channel_id = (
            message.channel_mentions[0].id
            if message.channel_mentions
            else None
        )
        rule = await self._matching_rule(
            message.guild.id,
            channel_id=channel_id,
            text=message.content,
        )
        if rule is None:
            await message.reply(
                "Não encontrei uma regra correspondente.",
                mention_author=False,
            )
            return

        def remove(records: dict[str, Any]) -> None:
            records.pop(rule.rule_id, None)

        await self.state.mutate_section(RULES_SECTION, remove)
        await message.reply(
            f"A regra **{rule.name}** foi removida.",
            mention_author=False,
        )

    async def _list_rules(self, message: discord.Message) -> None:
        rules = await self.rules_for(message.guild.id)
        lines = []
        for index, rule in enumerate(rules, start=1):
            scope = (
                f"<#{rule.channel_id}>"
                if rule.channel_id
                else "Todo o servidor"
            )
            lines.append(
                f"{index}. **{rule.name}** — {scope}\n{rule.instruction}"
            )
        content = (
            "Regras operacionais permanentes:\n\n" + "\n\n".join(lines)
            if lines
            else "Nenhuma regra operacional permanente está registrada."
        )
        try:
            await message.author.send(content[:3900])
            await message.reply(
                "Enviei as regras operacionais em uma mensagem privada.",
                mention_author=False,
            )
        except (discord.Forbidden, discord.HTTPException):
            await message.reply(content[:1900], mention_author=False)

    async def _save_pending(self, message: discord.Message) -> None:
        await self.state.update_section(
            PENDING_SECTION,
            f"{message.guild.id}:{message.author.id}",
            {
                "content": message.content,
                "channel_ids": [
                    channel.id for channel in message.channel_mentions
                ],
                "created_at": datetime.now(timezone.utc).isoformat(),
            },
        )

    async def _pending(
        self,
        guild_id: int,
        user_id: int,
    ) -> dict[str, Any] | None:
        records = await self.state.get_section(PENDING_SECTION)
        key = f"{guild_id}:{user_id}"
        value = records.get(key)
        if not isinstance(value, dict):
            return None
        try:
            created_at = datetime.fromisoformat(str(value["created_at"]))
        except (KeyError, ValueError):
            await self._clear_pending(key)
            return None
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) - created_at > timedelta(minutes=10):
            await self._clear_pending(key)
            return None
        return value

    async def _handle_confirmation(
        self,
        message: discord.Message,
        pending: dict[str, Any],
    ) -> bool:
        normalized = _normalize(message.content)
        key = f"{message.guild.id}:{message.author.id}"
        if normalized in {"nao", "não", "cancelar", "cancela"}:
            await self._clear_pending(key)
            await message.reply("Entendido. A regra não foi salva.", mention_author=False)
            return True
        if normalized not in {"sim", "salve", "salvar", "permanente", "pode salvar"}:
            await message.reply(
                "Responda apenas **sim** para salvar ou **não** para cancelar.",
                mention_author=False,
            )
            return True
        channel_ids = [
            int(value)
            for value in pending.get("channel_ids", [])
            if str(value).isdigit()
        ]
        content = str(pending.get("content", ""))
        channel_id = channel_ids[0] if channel_ids else None
        rule = self._parse_rule(
            message.guild.id,
            self._clean_instruction(content),
            channel_id=channel_id,
            log_channel_id=None,
            author_id=message.author.id,
            ai_spec=None,
        )
        await self.state.update_section(RULES_SECTION, rule.rule_id, rule.to_dict())
        await self._clear_pending(key)
        await message.reply(
            "Regra operacional permanente registrada e ativada.",
            mention_author=False,
        )
        return True

    async def _clear_pending(self, key: str) -> None:
        def remove(records: dict[str, Any]) -> None:
            records.pop(key, None)

        await self.state.mutate_section(PENDING_SECTION, remove)

    async def _matching_rule(
        self,
        guild_id: int,
        *,
        channel_id: int | None,
        text: str,
    ) -> OperationalRule | None:
        rules = await self.rules_for(guild_id)
        if channel_id:
            for rule in reversed(rules):
                if rule.channel_id == channel_id:
                    return rule
        normalized = _normalize(text)
        scored = [
            (
                sum(
                    1
                    for word in _normalize(rule.name).split()
                    if len(word) > 3 and word in normalized
                ),
                rule,
            )
            for rule in rules
        ]
        scored.sort(key=lambda item: item[0], reverse=True)
        return scored[0][1] if scored and scored[0][0] > 0 else None

    def _parse_rule(
        self,
        guild_id: int,
        instruction: str,
        *,
        channel_id: int | None,
        log_channel_id: int | None,
        author_id: int,
        ai_spec: dict[str, Any] | None,
    ) -> OperationalRule:
        normalized = _normalize(instruction)
        spec = _build_moderation_spec(
            instruction,
            channel_id=channel_id,
            log_channel_id=log_channel_id,
            ai_spec=ai_spec,
        )
        themed_help = bool(
            channel_id
            and (
                spec["policy_type"] == "themed_help_channel"
                or spec["required_context"]
            )
        )
        question_only = bool(
            channel_id
            and not themed_help
            and any(term in normalized for term in ("duvida", "duvidas", "pergunta"))
            and any(
                term in normalized
                for term in ("exclusiv", "somente", "apague", "remova")
            )
        )
        moderation_rule = bool(
            channel_id
            and spec["action"] != "none"
            and not question_only
            and not themed_help
        )
        require_bot = question_only and any(
            term in normalized
            for term in (
                "direcionad",
                "para voce",
                "ao jarvis",
                "diretas a voce",
            )
        )
        now = datetime.now(timezone.utc).isoformat()
        identity = f"{guild_id}:{channel_id or 0}:{normalized[:500]}"
        rule_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]
        return OperationalRule(
            rule_id=rule_id,
            guild_id=guild_id,
            name=(
                str(spec["name"])
                if moderation_rule or themed_help
                else "Canal exclusivo para dúvidas"
                if question_only
                else _rule_name(instruction, channel_id)
            ),
            instruction=instruction,
            policy_type=(
                "moderation_rule"
                if moderation_rule
                else "themed_help_channel"
                if themed_help
                else "question_only_channel"
                if question_only
                else "guidance"
            ),
            channel_id=channel_id,
            require_question=question_only,
            require_bot_mention=require_bot,
            delete_noncompliant=bool(
                spec["delete_message"]
                if moderation_rule or themed_help
                else (
                    question_only
                    and any(
                        term in normalized
                        for term in (
                            "apague",
                            "apagar",
                            "remova",
                            "remover",
                            "delete",
                        )
                    )
                )
            ),
            trigger_media=bool(spec["condition_type"] == "image"),
            condition_type=str(spec["condition_type"]),
            keywords=tuple(str(item) for item in spec["keywords"]),
            moderation_action=str(spec["action"]),
            duration_seconds=spec["duration_seconds"],
            log_channel_id=spec["log_channel_id"],
            guidance=(
                str(spec["guidance"])
                if moderation_rule or themed_help
                else "Este canal é destinado exclusivamente a dúvidas para a Bella. "
                "Envie uma pergunta objetiva e mencione a Bella para receber ajuda."
                if require_bot
                else "Este canal é destinado exclusivamente a dúvidas. "
                "Envie uma pergunta objetiva para utilizar o espaço corretamente."
            ),
            topic=str(spec["topic"]),
            required_context=tuple(
                str(item) for item in spec["required_context"]
            ),
            auto_assist=bool(spec["auto_assist"] if themed_help else False),
            enabled=True,
            created_at=now,
            updated_at=now,
            updated_by=author_id,
        )

    @staticmethod
    def _missing_required_context(
        message: discord.Message,
        rule: OperationalRule,
    ) -> list[str]:
        text = " ".join(message.content.split()).strip()
        normalized = _normalize(text)
        attachments = list(getattr(message, "attachments", []))
        missing: list[str] = []
        for requirement in rule.required_context:
            key = _normalize(requirement)
            present = True
            if any(term in key for term in ("linguagem", "tecnologia", "framework")):
                present = _has_technology_context(normalized)
            elif any(term in key for term in ("objetivo", "tentando fazer", "resultado")):
                present = _has_goal_context(normalized)
            elif any(term in key for term in ("codigo", "trecho", "reproducao")):
                present = _has_code_context(text, attachments)
            elif any(term in key for term in ("erro", "exception", "traceback", "falha")):
                present = _has_error_context(normalized)
            elif any(term in key for term in ("ja tentou", "o que tentou", "tentativas")):
                present = _has_attempt_context(normalized)
            else:
                words = [word for word in key.split() if len(word) >= 4]
                present = bool(words) and any(word in normalized for word in words)
            if not present:
                missing.append(requirement)
        if not rule.required_context and _is_vague_message(normalized):
            missing.append("contexto suficiente")
        return missing

    @staticmethod
    def _context_guidance(
        rule: OperationalRule,
        missing: list[str],
    ) -> str:
        requirements = rule.required_context or (
            "tema da dúvida",
            "objetivo",
            "detalhes relevantes",
            "o que já tentou",
        )
        checklist = "\n".join(f"- {item}" for item in requirements)
        missing_text = ", ".join(missing)
        topic = (
            f" O tema deste canal é: {rule.topic}."
            if rule.topic
            else ""
        )
        return (
            f"Sua mensagem foi removida porque faltou contexto suficiente.{topic}\n\n"
            "Envie novamente incluindo:\n"
            f"{checklist}\n\n"
            f"Informações ausentes: {missing_text}."
        )[:1800]

    async def _apply_moderation_rule(
        self,
        message: discord.Message,
        rule: OperationalRule,
    ) -> None:
        reason = _moderation_reason(message, rule)
        action = rule.moderation_action
        deleted = False
        action_done = False
        error = ""
        if rule.delete_noncompliant or action in {
            "delete",
            "warn",
            "timeout",
            "kick",
            "ban_permanent",
            "ban_temporary",
        }:
            try:
                await message.delete()
                deleted = True
            except (discord.Forbidden, discord.HTTPException) as exc:
                error = f"Falha ao apagar mensagem: {type(exc).__name__}: {exc}"
        if action == "warn":
            action_done = await self._send_public_warning(message, rule)
        elif action == "timeout":
            action_done, error = await self._apply_timeout(
                message,
                rule,
                reason,
                previous_error=error,
            )
        elif action == "kick":
            action_done, error = await self._apply_kick(
                message,
                reason,
                previous_error=error,
            )
        elif action in {"ban_permanent", "ban_temporary"}:
            action_done, error = await self._apply_ban(
                message,
                rule,
                reason,
                previous_error=error,
            )
        elif action == "delete":
            action_done = deleted
        await self._send_moderation_report(
            message,
            rule,
            reason=reason,
            deleted=deleted,
            action_done=action_done,
            error=error,
        )
        await self._record_application(
            message,
            rule,
            action=action if action_done else f"{action}_failed",
        )

    async def _send_public_warning(
        self,
        message: discord.Message,
        rule: OperationalRule,
    ) -> bool:
        try:
            await message.channel.send(
                f"{message.author.mention} {rule.guidance}",
                allowed_mentions=discord.AllowedMentions(
                    users=[message.author],
                    roles=False,
                    everyone=False,
                ),
                delete_after=25,
            )
            return True
        except (discord.Forbidden, discord.HTTPException):
            return False

    async def _apply_timeout(
        self,
        message: discord.Message,
        rule: OperationalRule,
        reason: str,
        *,
        previous_error: str,
    ) -> tuple[bool, str]:
        if not hasattr(message.author, "timeout"):
            return False, previous_error or "Autor não é membro do servidor."
        duration = rule.duration_seconds or 3600
        until = datetime.now(timezone.utc) + timedelta(seconds=duration)
        try:
            await message.author.timeout(until, reason=reason[:512])
            return True, previous_error
        except (discord.Forbidden, discord.HTTPException) as exc:
            return False, f"Falha ao aplicar timeout: {type(exc).__name__}: {exc}"

    async def _apply_kick(
        self,
        message: discord.Message,
        reason: str,
        *,
        previous_error: str,
    ) -> tuple[bool, str]:
        if not hasattr(message.author, "kick"):
            return False, previous_error or "Autor não é membro do servidor."
        try:
            await message.author.kick(reason=reason[:512])
            return True, previous_error
        except (discord.Forbidden, discord.HTTPException) as exc:
            return False, f"Falha ao expulsar usuário: {type(exc).__name__}: {exc}"

    async def _apply_ban(
        self,
        message: discord.Message,
        rule: OperationalRule,
        reason: str,
        *,
        previous_error: str,
    ) -> tuple[bool, str]:
        try:
            await message.guild.ban(
                message.author,
                reason=reason[:512],
                delete_message_seconds=0,
            )
        except TypeError:
            try:
                await message.guild.ban(
                    message.author,
                    reason=reason[:512],
                    delete_message_days=0,
                )
            except (discord.Forbidden, discord.HTTPException) as exc:
                return False, f"Falha ao banir usuário: {type(exc).__name__}: {exc}"
        except (discord.Forbidden, discord.HTTPException) as exc:
            return False, f"Falha ao banir usuário: {type(exc).__name__}: {exc}"
        if rule.moderation_action == "ban_temporary" and rule.duration_seconds:
            await self.state.update_section(
                "temporary_bans",
                f"{message.guild.id}:{message.author.id}",
                {
                    "guild_id": message.guild.id,
                    "user_id": message.author.id,
                    "expires_at": (
                        datetime.now(timezone.utc)
                        + timedelta(seconds=rule.duration_seconds)
                    ).isoformat(),
                    "reason": reason,
                },
            )
        return True, previous_error

    async def _send_moderation_report(
        self,
        message: discord.Message,
        rule: OperationalRule,
        *,
        reason: str,
        deleted: bool,
        action_done: bool,
        error: str,
    ) -> None:
        channel = None
        if rule.log_channel_id:
            channel = self.bot.get_channel(rule.log_channel_id)
            if channel is None and message.guild is not None:
                channel = message.guild.get_channel(rule.log_channel_id)
        if channel is None or not hasattr(channel, "send"):
            return
        occurred_at = datetime.now(timezone.utc).astimezone().strftime(
            "%d/%m/%Y às %H:%M:%S"
        )
        content = (
            "Relatório de moderação automática\n\n"
            f"Regra: {rule.name}\n"
            f"Usuário: {message.author.mention} (`{message.author.id}`)\n"
            f"Canal: <#{message.channel.id}>\n"
            f"Mensagem original: {_message_jump_url(message)}\n"
            f"Condição detectada: {rule.condition_type or rule.policy_type}\n"
            f"Motivo: {reason}\n"
            f"Ação: {rule.moderation_action}\n"
            f"Resultado: {'aplicada' if action_done else 'não aplicada'}\n"
            f"Mensagem removida: {'Sim' if deleted else 'Não'}\n"
            f"Horário: {occurred_at}"
        )
        if error:
            content += f"\nObservação técnica: {error}"
        try:
            await channel.send(
                content[:1900],
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except (discord.Forbidden, discord.HTTPException):
            return

    async def _moderation_rule_matches(
        self,
        message: discord.Message,
        rule: OperationalRule,
    ) -> bool:
        text = " ".join(message.content.split()).strip()
        normalized = _normalize(text)
        condition = (rule.condition_type or "").casefold()
        if condition == "image":
            return _message_has_image(message)
        if condition == "attachment":
            return bool(getattr(message, "attachments", []))
        if condition == "link":
            return bool(re.search(r"https?://|www\.", text, flags=re.I))
        if condition == "invite":
            return bool(
                re.search(
                    r"(?:discord\.gg|discord(?:app)?\.com/invite)/",
                    text,
                    flags=re.I,
                )
            )
        if condition == "caps":
            letters = [char for char in text if char.isalpha()]
            return bool(
                len(letters) >= 8
                and sum(char.isupper() for char in letters) / len(letters) >= 0.75
            )
        if condition == "vague":
            return _is_vague_message(normalized)
        if condition == "non_question":
            return not _looks_like_question(text)
        if condition == "keyword":
            return any(
                _normalize(keyword) in normalized
                for keyword in rule.keywords
                if str(keyword).strip()
            )
        if condition == "semantic":
            return await self._semantic_rule_matches(message, rule)
        if condition == "any_message":
            return True
        return False

    async def _semantic_rule_matches(
        self,
        message: discord.Message,
        rule: OperationalRule,
    ) -> bool:
        service = self.ai_service
        if not service or not getattr(service, "available", False):
            return False
        attachments = [
            {
                "filename": str(getattr(item, "filename", "")),
                "content_type": str(getattr(item, "content_type", "")),
            }
            for item in getattr(message, "attachments", [])[:5]
        ]
        prompt = (
            "Avalie se uma mensagem do Discord viola uma regra de moderação "
            "permanente definida pelo proprietário do servidor. Responda apenas "
            "em JSON pelo schema. Seja conservador: para ações graves como ban, "
            "retorne violation=true somente se a violação for clara.\n\n"
            f"Regra: {rule.instruction}\n"
            f"Canal alvo: {rule.channel_id}\n"
            f"Ação configurada: {rule.moderation_action}\n"
            f"Mensagem: {message.content[:1200]}\n"
            f"Anexos: {attachments}"
        )
        schema: dict[str, Any] = {
            "type": "object",
            "properties": {
                "violation": {"type": "boolean"},
                "confidence": {"type": "number"},
                "reason": {"type": "string"},
            },
            "required": ["violation", "confidence", "reason"],
        }
        try:
            data = await service.generate_json(
                prompt,
                schema,
                max_output_tokens=220,
                timeout_seconds=5,
                model_policy="economy",
                use_cache=True,
            )
        except Exception:
            return False
        confidence = _score_float(data.get("confidence"))
        return bool(data.get("violation")) and confidence >= 0.72


    def _message_complies(
        self,
        message: discord.Message,
        rule: OperationalRule,
    ) -> bool:
        text = " ".join(message.content.split()).strip()
        has_question = bool(
            "?" in text
            or re.match(
                r"^(?:como|qual|quais|quando|onde|por que|porque|quem|o que|"
                r"pode|posso|tem|existe|preciso|jarvis)\b",
                _normalize(text),
            )
        )
        if rule.require_question and not has_question:
            return False
        if rule.require_bot_mention:
            return bool(
                self.bot.user
                and any(user.id == self.bot.user.id for user in message.mentions)
            )
        return True

    @staticmethod
    def _clean_instruction(content: str) -> str:
        cleaned = re.sub(r"<@!?\d+>", "Bella", content)
        return " ".join(cleaned.split()).strip()[:1800]

    @staticmethod
    def _looks_like_ambiguous_rule(message: discord.Message) -> bool:
        normalized = _normalize(message.content)
        return bool(
            message.channel_mentions
            and any(
                term in normalized
                for term in (
                    "canal",
                    "duvida",
                    "mensagem",
                    "ajuda",
                    "restricao",
                    "tema",
                )
            )
            and any(
                term in normalized
                for term in (
                    "exclusiv",
                    "destinad",
                    "somente",
                    "para ajuda",
                    "pra ajuda",
                    "deve ",
                    "nao deixe",
                )
            )
        )

    @staticmethod
    def _looks_like_clear_rule(message: discord.Message) -> bool:
        normalized = _normalize(message.content)
        return bool(
            message.channel_mentions
            and any(
                term in normalized
                for term in (
                    "canal",
                    "duvida",
                    "mensagem",
                    "ajuda",
                    "restricao",
                    "tema",
                )
            )
            and any(
                term in normalized
                for term in (
                    "exclusiv",
                    "destinad",
                    "somente",
                    "para ajuda",
                    "pra ajuda",
                    "sem contexto",
                    "nao deixe",
                )
            )
            and any(
                term in normalized
                for term in (
                    "apague",
                    "apagar",
                    "remova",
                    "remover",
                    "delete",
                    "oriente",
                    "orientar",
                )
            )
        )

    async def _record_application(
        self,
        message: discord.Message,
        rule: OperationalRule,
        *,
        action: str = "message_deleted",
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()

        def append(records: dict[str, Any]) -> None:
            records[f"{now}:{message.id}"] = {
                "timestamp": now,
                "rule_id": rule.rule_id,
                "guild_id": message.guild.id,
                "channel_id": message.channel.id,
                "user_id": message.author.id,
                "message_id": message.id,
                "action": action,
            }
            while len(records) > 5000:
                records.pop(next(iter(records)))

        await self.state.mutate_section("operational_rule_history", append)


def _build_moderation_spec(
    instruction: str,
    *,
    channel_id: int | None,
    log_channel_id: int | None,
    ai_spec: dict[str, Any] | None,
) -> dict[str, Any]:
    normalized = _normalize(instruction)
    required_context = _local_required_context(normalized)
    local_policy_type = _local_policy_type(normalized, required_context)
    local = {
        "name": _rule_name(instruction, channel_id),
        "policy_type": local_policy_type,
        "topic": _local_topic(normalized),
        "required_context": required_context,
        "auto_assist": _local_auto_assist(normalized, local_policy_type),
        "condition_type": _local_condition_type(normalized),
        "keywords": _local_keywords(instruction),
        "action": _local_action(normalized),
        "duration_seconds": _parse_duration_seconds(normalized),
        "delete_message": _local_delete_message(normalized),
        "log_channel_id": log_channel_id,
        "guidance": _local_guidance(normalized),
    }
    if not ai_spec:
        return _normalize_spec(local)
    confidence = _score_float(ai_spec.get("confidence"))
    if confidence < 0.55:
        return _normalize_spec(local)
    merged = {
        **local,
        "name": str(ai_spec.get("name") or local["name"]),
        "policy_type": str(
            ai_spec.get("policy_type") or local["policy_type"]
        ),
        "topic": str(ai_spec.get("topic") or local["topic"]),
        "required_context": (
            ai_spec.get("required_context") or local["required_context"]
        ),
        "auto_assist": bool(
            ai_spec.get("auto_assist", local["auto_assist"])
        ),
        "condition_type": str(
            ai_spec.get("condition_type") or local["condition_type"]
        ),
        "keywords": ai_spec.get("keywords") or local["keywords"],
        "action": str(ai_spec.get("action") or local["action"]),
        "duration_seconds": (
            _optional_int(ai_spec.get("duration_seconds"))
            or local["duration_seconds"]
        ),
        "delete_message": bool(
            ai_spec.get("delete_message", local["delete_message"])
        ),
        "log_channel_id": (
            _optional_int(ai_spec.get("log_channel_id"))
            or local["log_channel_id"]
        ),
        "guidance": str(ai_spec.get("guidance") or local["guidance"]),
    }
    return _normalize_spec(merged)


def _normalize_spec(spec: dict[str, Any]) -> dict[str, Any]:
    allowed_policy_types = {
        "guidance",
        "question_only_channel",
        "moderation_rule",
        "themed_help_channel",
    }
    allowed_conditions = {
        "image",
        "attachment",
        "link",
        "invite",
        "keyword",
        "caps",
        "vague",
        "non_question",
        "any_message",
        "semantic",
        "insufficient_context",
    }
    allowed_actions = {
        "none",
        "delete",
        "warn",
        "timeout",
        "kick",
        "ban_permanent",
        "ban_temporary",
    }
    condition = str(spec.get("condition_type") or "").casefold()
    action = str(spec.get("action") or "none").casefold()
    policy_type = str(spec.get("policy_type") or "guidance").casefold()
    duration = _optional_int(spec.get("duration_seconds"))
    if action == "ban_permanent" and duration:
        action = "ban_temporary"
    if action == "ban_temporary" and not duration:
        action = "ban_permanent"
    if condition not in allowed_conditions:
        condition = "semantic" if action != "none" else ""
    if action not in allowed_actions:
        action = "none"
    if policy_type not in allowed_policy_types:
        policy_type = "moderation_rule" if action != "none" else "guidance"
    keywords = tuple(
        str(item).strip()
        for item in spec.get("keywords", [])
        if str(item).strip()
    )[:20]
    return {
        "name": str(spec.get("name") or "Regra de moderação")[:100],
        "policy_type": policy_type,
        "topic": str(spec.get("topic") or "")[:500],
        "required_context": tuple(
            str(item).strip()[:100]
            for item in spec.get("required_context", [])
            if str(item).strip()
        )[:12],
        "auto_assist": bool(spec.get("auto_assist")),
        "condition_type": condition,
        "keywords": keywords,
        "action": action,
        "duration_seconds": duration,
        "delete_message": bool(spec.get("delete_message")),
        "log_channel_id": _optional_int(spec.get("log_channel_id")),
        "guidance": str(
            spec.get("guidance")
            or "Essa mensagem viola uma regra de moderação configurada."
        )[:500],
    }


def _rule_interpretation_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "policy_type": {
                "type": "string",
                "enum": [
                    "guidance",
                    "question_only_channel",
                    "moderation_rule",
                    "themed_help_channel",
                ],
            },
            "channel_id": {"type": "integer"},
            "log_channel_id": {"type": "integer"},
            "topic": {"type": "string"},
            "required_context": {
                "type": "array",
                "items": {"type": "string"},
            },
            "auto_assist": {"type": "boolean"},
            "condition_type": {
                "type": "string",
                "enum": [
                    "image",
                    "attachment",
                    "link",
                    "invite",
                    "keyword",
                    "caps",
                    "vague",
                    "non_question",
                    "insufficient_context",
                    "any_message",
                    "semantic",
                ],
            },
            "keywords": {
                "type": "array",
                "items": {"type": "string"},
            },
            "action": {
                "type": "string",
                "enum": [
                    "none",
                    "delete",
                    "warn",
                    "timeout",
                    "kick",
                    "ban_permanent",
                    "ban_temporary",
                ],
            },
            "duration_seconds": {"type": "integer"},
            "delete_message": {"type": "boolean"},
            "guidance": {"type": "string"},
            "confidence": {"type": "number"},
        },
        "required": [
            "name",
            "policy_type",
            "channel_id",
            "log_channel_id",
            "topic",
            "required_context",
            "auto_assist",
            "condition_type",
            "keywords",
            "action",
            "duration_seconds",
            "delete_message",
            "guidance",
            "confidence",
        ],
    }


def _local_policy_type(
    normalized: str,
    required_context: tuple[str, ...],
) -> str:
    if required_context or (
        any(term in normalized for term in ("canal de ajuda", "canal pra ajuda", "canal para ajuda"))
        and any(term in normalized for term in ("ajude", "ajudar", "responda"))
    ):
        return "themed_help_channel"
    return "guidance"


def _local_topic(normalized: str) -> str:
    if any(
        term in normalized
        for term in (
            "programacao",
            "programar",
            "codigo",
            "codigos",
            "desenvolvimento",
            "linguagem",
            "tecnologia",
        )
    ):
        return "Ajuda sobre programação, código, linguagens e tecnologias."
    return ""


def _local_required_context(normalized: str) -> tuple[str, ...]:
    requirements: list[str] = []
    candidates = (
        (
            "Linguagem ou tecnologia",
            ("linguagem/tecnologia", "linguagem ou tecnologia", "linguagem", "tecnologia"),
        ),
        (
            "O que está tentando fazer",
            ("o que esta tentando fazer", "objetivo", "resultado esperado"),
        ),
        (
            "Código relevante",
            ("codigo relevante", "trecho de codigo", "código relevante"),
        ),
        (
            "Mensagem de erro",
            ("mensagem de erro", "erro exibido", "traceback"),
        ),
        (
            "O que já tentou",
            ("o que ja tentou", "o que tentou", "tentativas realizadas"),
        ),
    )
    for label, terms in candidates:
        if any(_normalize(term) in normalized for term in terms):
            requirements.append(label)
    return tuple(requirements)


def _local_auto_assist(normalized: str, policy_type: str) -> bool:
    if policy_type != "themed_help_channel":
        return False
    return any(
        term in normalized
        for term in (
            "voce deve ajudar",
            "deve ajudar",
            "ajude a pessoa",
            "ajudar a pessoa",
            "responda a pessoa",
            "responder a pessoa",
        )
    )


def _local_condition_type(normalized: str) -> str:
    if any(
        term in normalized
        for term in (
            "sem contexto",
            "contexto insuficiente",
            "faltando contexto",
            "nao informar",
            "não informar",
        )
    ):
        return "insufficient_context"
    if any(term in normalized for term in ("imagem", "imagens", "foto", "fotos")):
        return "image"
    if any(term in normalized for term in ("anexo", "anexos", "arquivo", "midia")):
        return "attachment"
    if any(term in normalized for term in ("convite", "discord.gg", "invite")):
        return "invite"
    if any(term in normalized for term in ("link", "url", "site")):
        return "link"
    if any(term in normalized for term in ("caps", "maiuscula", "gritando")):
        return "caps"
    if any(term in normalized for term in ("mensagem vaga", "mensagens vagas", "sem contexto", "generica")):
        return "vague"
    if any(term in normalized for term in ("nao seja duvida", "nao forem duvidas", "fora de duvida", "fora de contexto")):
        return "non_question"
    if _local_keywords(normalized):
        return "keyword"
    if any(
        term in normalized
        for term in (
            "qualquer mensagem",
            "toda mensagem",
            "todas as mensagens",
            "tudo que enviar",
            "tudo que mandarem",
        )
    ):
        return "any_message"
    return "semantic" if _local_action(normalized) != "none" else ""


def _local_action(normalized: str) -> str:
    duration = _parse_duration_seconds(normalized)
    if any(term in normalized for term in ("banimento", "banir", "bane", "ban permanente", "ban")):
        return "ban_temporary" if duration else "ban_permanent"
    if any(term in normalized for term in ("expulse", "expulsar", "kick", "kickar")):
        return "kick"
    if any(term in normalized for term in ("timeout", "silencie", "silenciar", "mute", "mutar")):
        return "timeout"
    if any(term in normalized for term in ("apague", "apagar", "remova", "remover", "delete", "deletar")):
        return "delete"
    if any(term in normalized for term in ("avise", "avisar", "oriente", "orientar", "responda")):
        return "warn"
    return "none"


def _local_delete_message(normalized: str) -> bool:
    return any(
        term in normalized
        for term in (
            "apague",
            "apagar",
            "remova",
            "remover",
            "delete",
            "deletar",
            "banir",
            "banimento",
            "timeout",
            "silenciar",
            "expulsar",
            "kick",
        )
    )


def _local_keywords(instruction: str) -> tuple[str, ...]:
    quoted = re.findall(r"[\"'“”‘’]([^\"'“”‘’]{2,80})[\"'“”‘’]", instruction)
    if quoted:
        return tuple(item.strip() for item in quoted if item.strip())[:10]
    match = re.search(
        r"(?:palavra|termo|frase|conteudo|conteúdo)\s+(.+?)(?:\s+aplique|\s+apague|\s+remova|\s+mande|\s+envie|$)",
        instruction,
        flags=re.I,
    )
    if not match:
        return ()
    candidate = re.sub(r"<#\d+>", " ", match.group(1))
    candidate = " ".join(candidate.split()).strip(" .,:;")
    return (candidate,) if 2 <= len(candidate) <= 80 else ()


def _local_guidance(normalized: str) -> str:
    if "link" in normalized:
        return "Links não são permitidos neste canal."
    if "convite" in normalized:
        return "Convites externos não são permitidos neste canal."
    if "imagem" in normalized:
        return "Imagens não são permitidas neste canal."
    if "duvida" in normalized:
        return "Este canal deve ser usado apenas para dúvidas objetivas."
    return "Essa mensagem viola uma regra de moderação configurada para este canal."


def _parse_duration_seconds(normalized: str) -> int | None:
    if any(term in normalized for term in ("permanente", "permanentemente")):
        return None
    match = re.search(
        r"\b(\d{1,3})\s*(segundos?|secs?|s|minutos?|mins?|m|horas?|hrs?|h|dias?|d|semanas?|sem|w)\b",
        normalized,
    )
    if not match:
        return None
    amount = int(match.group(1))
    unit = match.group(2)
    if unit.startswith(("seg", "sec", "s")):
        return amount
    if unit.startswith(("min", "m")):
        return amount * 60
    if unit.startswith(("hora", "hr", "h")):
        return amount * 3600
    if unit.startswith(("dia", "d")):
        return amount * 86400
    if unit.startswith(("sem", "w")):
        return amount * 604800
    return None


def _moderation_reason(message: discord.Message, rule: OperationalRule) -> str:
    base = rule.guidance or rule.instruction
    return (
        f"{base} Regra: {rule.name}. "
        f"Canal: <#{message.channel.id}>. Usuário: {message.author.id}."
    )


def _looks_like_question(text: str) -> bool:
    normalized = _normalize(text)
    return bool(
        "?" in text
        or re.match(
            r"^(?:como|qual|quais|quando|onde|por que|porque|quem|o que|"
            r"pode|posso|tem|existe|preciso|jarvis)\b",
            normalized,
        )
    )


def _is_vague_message(normalized: str) -> bool:
    compact = normalized.strip(" ?!.,")
    return compact in {
        "oi",
        "ola",
        "opa",
        "bom dia",
        "boa tarde",
        "boa noite",
        "ajuda",
        "suporte",
        "humano",
        "adm",
        "admin",
        "e ai",
        "eae",
    } or (len(compact) <= 10 and "?" not in compact)


def _has_technology_context(normalized: str) -> bool:
    technologies = (
        "python",
        "javascript",
        "typescript",
        "java",
        "csharp",
        "c#",
        "c++",
        "php",
        "ruby",
        "rust",
        "golang",
        "go ",
        "kotlin",
        "swift",
        "html",
        "css",
        "react",
        "next.js",
        "nextjs",
        "node",
        "discord.py",
        "django",
        "flask",
        "fastapi",
        "sql",
        "supabase",
        "firebase",
        "api",
        "framework",
        "biblioteca",
        "linguagem",
    )
    padded = f" {normalized} "
    return any(term in padded for term in technologies)


def _has_goal_context(normalized: str) -> bool:
    if len(normalized) < 25:
        return False
    return any(
        term in normalized
        for term in (
            "estou tentando",
            "quero fazer",
            "preciso criar",
            "preciso fazer",
            "meu objetivo",
            "resultado esperado",
            "deveria",
            "tento ",
        )
    )


def _has_code_context(text: str, attachments: list[Any]) -> bool:
    if "```" in text or re.search(r"`[^`\n]{3,}`", text):
        return True
    source_extensions = (
        ".py",
        ".js",
        ".ts",
        ".tsx",
        ".jsx",
        ".java",
        ".cs",
        ".cpp",
        ".c",
        ".go",
        ".rs",
        ".php",
        ".rb",
        ".html",
        ".css",
        ".sql",
        ".json",
        ".yaml",
        ".yml",
    )
    if any(
        str(getattr(item, "filename", "")).casefold().endswith(source_extensions)
        for item in attachments
    ):
        return True
    return bool(
        re.search(
            r"(?:def |class |function |const |let |var |import |from |"
            r"async |await |SELECT |INSERT |console\.|print\()",
            text,
        )
    )


def _has_error_context(normalized: str) -> bool:
    return any(
        term in normalized
        for term in (
            "mensagem de erro",
            "erro ",
            "error ",
            "exception",
            "traceback",
            "falha ",
            "failed",
            "nao mostra erro",
            "nao aparece erro",
            "sem mensagem de erro",
            "sem erro",
        )
    )


def _has_attempt_context(normalized: str) -> bool:
    return any(
        term in normalized
        for term in (
            "ja tentei",
            "eu tentei",
            "tentei ",
            "ja fiz",
            "eu fiz",
            "testei ",
            "verifiquei ",
            "mudei ",
            "troquei ",
            "reinstalei ",
            "ainda assim",
            "mas continua",
        )
    )


def _score_float(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def _optional_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _extract_channel_ids(
    content: str,
    guild: discord.Guild | None,
) -> list[int]:
    ids: list[int] = []
    for value in re.findall(r"<#(\d+)>", content):
        channel_id = int(value)
        if channel_id not in ids:
            ids.append(channel_id)
    if guild is None:
        return ids
    guild_channels = getattr(guild, "channels", [])
    if isinstance(guild_channels, dict):
        guild_channels = guild_channels.values()
    channels = {
        _normalize(str(getattr(channel, "name", ""))): int(channel.id)
        for channel in guild_channels
        if getattr(channel, "id", None) and getattr(channel, "name", None)
    }
    for raw_name in re.findall(r"#([\w\-]+)", content, flags=re.UNICODE):
        channel_id = channels.get(_normalize(raw_name))
        if channel_id and channel_id not in ids:
            ids.append(channel_id)
    return ids


def _message_has_image(message: discord.Message) -> bool:
    image_extensions = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".avif")
    for attachment in getattr(message, "attachments", []):
        content_type = str(getattr(attachment, "content_type", "") or "").casefold()
        filename = str(getattr(attachment, "filename", "") or "").casefold()
        if content_type.startswith("image/") or filename.endswith(image_extensions):
            return True
    for embed in getattr(message, "embeds", []):
        image = getattr(getattr(embed, "image", None), "url", None)
        thumbnail = getattr(getattr(embed, "thumbnail", None), "url", None)
        if image or thumbnail:
            return True
    return False


def _message_jump_url(message: discord.Message) -> str:
    jump_url = str(getattr(message, "jump_url", "") or "")
    if jump_url:
        return jump_url
    return (
        "https://discord.com/channels/"
        f"{message.guild.id}/{message.channel.id}/{message.id}"
    )


def _rule_name(instruction: str, channel_id: int | None) -> str:
    words = [
        word
        for word in re.findall(r"\w+", instruction, flags=re.UNICODE)
        if len(word) > 2
    ]
    base = " ".join(words[:7]).strip() or "Regra operacional"
    return f"{base}{f' no canal {channel_id}' if channel_id else ''}"[:100]


def _normalize(value: str) -> str:
    decomposed = normalize("NFKD", value.casefold())
    ascii_text = "".join(char for char in decomposed if not combining(char))
    return " ".join(ascii_text.split())


def _is_rule_trigger(normalized: str) -> bool:
    return any(
        phrase in normalized
        for phrase in (
            "a partir de agora",
            "lembre-se que",
            "lembre se que",
            "salve a seguinte regra",
            "salve regra",
            "registre essa diretriz",
            "registre esta diretriz",
            "nunca faca",
            "sempre faca",
            "quando acontecer",
            "regra permanente",
            "diretriz permanente",
        )
    )


def _is_list_request(normalized: str) -> bool:
    return any(
        word in normalized
        for word in ("liste", "listar", "mostre", "mostrar", "quais")
    ) and any(
        word in normalized
        for word in ("regras", "diretrizes", "memorias operacionais")
    )


def _is_remove_request(normalized: str) -> bool:
    return any(
        word in normalized
        for word in ("remova", "remover", "apague", "apagar", "exclua", "excluir")
    ) and any(word in normalized for word in ("regra", "diretriz"))


def _is_update_request(normalized: str) -> bool:
    return any(
        word in normalized
        for word in (
            "atualize",
            "atualizar",
            "edite",
            "editar",
            "substitua",
            "substituir",
        )
    ) and any(word in normalized for word in ("regra", "diretriz"))
