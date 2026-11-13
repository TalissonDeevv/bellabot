from __future__ import annotations

import hashlib
import logging
import re
import time
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import discord

from src.ai.personality import BELLA_PERSONA_RUNTIME

from .identity import OWNER_DISCORD_ID
from .state import StateStore

LOGGER = logging.getLogger(__name__)
AUTOMATIONS_SECTION = "jarvis_automations"
AUTOMATION_HISTORY_SECTION = "jarvis_automation_history"
DEFAULT_TZ = ZoneInfo("America/Sao_Paulo")


class KnowledgeAutomationCenter:
    def __init__(self, state: StateStore, service: Any | None = None) -> None:
        self.state = state
        self.service = service

    async def define_from_text(
        self,
        content: str,
        *,
        user_id: int,
        guild: discord.Guild | None,
        item_id: str | None = None,
    ) -> dict[str, str] | None:
        if user_id != OWNER_DISCORD_ID:
            raise PermissionError("Somente o proprietario pode definir automacoes.")
        parsed = _parse_automation(content, guild)
        if parsed is None:
            return None
        key = _safe_key(item_id or parsed["title"] or content)
        now = time.time()
        existing = (await self.state.get_section(AUTOMATIONS_SECTION)).get(key)
        next_run_at = (
            float(existing.get("next_run_at", 0))
            if isinstance(existing, dict)
            else 0.0
        )
        if next_run_at <= now:
            next_run_at = _next_run_timestamp(parsed, now=now)
        record = {
            **parsed,
            "key": key,
            "enabled": True,
            "created_by": user_id,
            "updated_at": now,
            "created_at": (
                existing.get("created_at", now)
                if isinstance(existing, dict)
                else now
            ),
            "next_run_at": next_run_at,
            "run_count": (
                int(existing.get("run_count", 0))
                if isinstance(existing, dict)
                else 0
            ),
        }
        await self.state.update_section(AUTOMATIONS_SECTION, key, record)
        return {
            "id": key,
            "type": "automation",
            "title": str(record["title"]),
            "next_run_at": _format_timestamp(next_run_at),
        }

    async def run_due(self, bot: discord.Client, *, limit: int = 3) -> int:
        records = await self.state.get_section(AUTOMATIONS_SECTION)
        if not records:
            return 0
        now = time.time()
        due = [
            (str(key), item)
            for key, item in records.items()
            if isinstance(item, dict)
            and item.get("enabled", True)
            and float(item.get("next_run_at", 0)) <= now
        ]
        due.sort(key=lambda pair: float(pair[1].get("next_run_at", 0)))
        sent = 0
        for key, record in due[:limit]:
            if await self._run_one(bot, key, record):
                sent += 1
            await asyncio_sleep()
        return sent

    async def _run_one(
        self,
        bot: discord.Client,
        key: str,
        record: dict[str, Any],
    ) -> bool:
        channel_id = int(record.get("channel_id") or 0)
        if not channel_id:
            await self._schedule_next(key, record, failed=True)
            return False
        channel = bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await bot.fetch_channel(channel_id)
            except (discord.Forbidden, discord.HTTPException):
                await self._schedule_next(key, record, failed=True)
                return False
        if not hasattr(channel, "send"):
            await self._schedule_next(key, record, failed=True)
            return False
        history = await self._history_for(key)
        body = await self._generate_message(record, history)
        role_mentions = " ".join(
            f"<@&{role_id}>"
            for role_id in record.get("role_ids", [])
            if str(role_id).isdigit()
        )
        content = f"{role_mentions}\n\n{body}".strip()
        try:
            message = await channel.send(
                content[:1900],
                allowed_mentions=discord.AllowedMentions(
                    roles=True,
                    users=False,
                    everyone=False,
                ),
            )
        except (discord.Forbidden, discord.HTTPException) as exc:
            LOGGER.warning("Falha ao executar automacao %s: %s", key, exc)
            await self._schedule_next(key, record, failed=True)
            return False
        await self._record_history(key, record, body, getattr(message, "id", 0))
        await self._schedule_next(key, record, failed=False)
        return True

    async def _generate_message(
        self,
        record: dict[str, Any],
        history: list[str],
    ) -> str:
        raw = str(record.get("raw", ""))
        if (
            self.service
            and getattr(self.service, "available", False)
            and getattr(self.service, "has_ready_model", lambda: True)()
        ):
            prompt = (
                "Gere o conteúdo de uma automação recorrente do Discord criada "
                "por Talisson. O texto deve ser único, natural, útil e não pode "
                "repetir os conteúdos recentes. Responda apenas com a mensagem "
                "final para publicar.\n\n"
                f"Persona oficial da Bella:\n{BELLA_PERSONA_RUNTIME}\n\n"
                f"Instrução original: {raw}\n"
                f"Histórico recente: {history[-8:]}"
            )
            try:
                text = await self.service.generate_text(
                    prompt,
                    max_output_tokens=650,
                    timeout_seconds=12,
                    model_policy="economy",
                    use_cache=False,
                )
                cleaned = _clean_message(text)
                if cleaned:
                    return cleaned
            except Exception as exc:
                LOGGER.warning("Fallback local para automacao %s: %s", record.get("key"), exc)
        return _local_automation_message(raw, history)

    async def _history_for(self, key: str) -> list[str]:
        history = await self.state.get_section(AUTOMATION_HISTORY_SECTION)
        items = history.get(key, [])
        if not isinstance(items, list):
            return []
        return [
            str(item.get("content", ""))[:400]
            for item in items[-12:]
            if isinstance(item, dict)
        ]

    async def _record_history(
        self,
        key: str,
        record: dict[str, Any],
        content: str,
        message_id: int,
    ) -> None:
        now = time.time()

        def mutate(section: dict[str, Any]) -> None:
            items = section.get(key, [])
            if not isinstance(items, list):
                items = []
            items.append(
                {
                    "sent_at": now,
                    "channel_id": record.get("channel_id"),
                    "message_id": message_id,
                    "content_hash": hashlib.sha256(content.encode("utf-8")).hexdigest()[:16],
                    "content": content[:900],
                }
            )
            section[key] = items[-60:]

        await self.state.mutate_section(AUTOMATION_HISTORY_SECTION, mutate)

    async def _schedule_next(
        self,
        key: str,
        record: dict[str, Any],
        *,
        failed: bool,
    ) -> None:
        now = time.time()
        next_run_at = _next_run_timestamp(record, now=now + (300 if failed else 60))

        def mutate(section: dict[str, Any]) -> None:
            current = section.get(key, {})
            if not isinstance(current, dict):
                current = {}
            current.update(
                {
                    **record,
                    "next_run_at": next_run_at,
                    "last_run_at": now,
                    "last_status": "failed" if failed else "sent",
                    "run_count": int(record.get("run_count", 0)) + (0 if failed else 1),
                }
            )
            section[key] = current

        await self.state.mutate_section(AUTOMATIONS_SECTION, mutate)


def _parse_automation(
    content: str,
    guild: discord.Guild | None,
) -> dict[str, Any] | None:
    normalized = _normalize(content)
    if not _looks_like_automation(normalized):
        return None
    channel_id = _extract_channel_id(content, guild)
    if not channel_id:
        raise ValueError("Informe o canal da automação com #canal ou <#id>.")
    hour, minute = _extract_time(normalized)
    frequency, weekdays, monthly_day = _extract_frequency(normalized)
    role_ids = re.findall(r"<@&(\d+)>", content)
    return {
        "title": _automation_title(normalized),
        "raw": content.strip()[:1800],
        "frequency": frequency,
        "weekdays": weekdays,
        "monthly_day": monthly_day,
        "hour": hour,
        "minute": minute,
        "channel_id": int(channel_id),
        "role_ids": role_ids[:8],
    }


def _looks_like_automation(normalized: str) -> bool:
    return any(
        term in normalized
        for term in (
            "todos os dias",
            "todo dia",
            "diariamente",
            "toda segunda",
            "toda terca",
            "toda quarta",
            "toda quinta",
            "toda sexta",
            "todo sabado",
            "todo domingo",
            "semanalmente",
            "mensalmente",
            "todo mes",
            "primeiro dia de cada mes",
        )
    ) and any(term in normalized for term in ("mande", "envie", "publique", "lembre", "gere"))


def _extract_frequency(normalized: str) -> tuple[str, list[int], int | None]:
    weekdays_map = {
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
    weekdays = [index for name, index in weekdays_map.items() if name in normalized]
    if "mensal" in normalized or "todo mes" in normalized or "primeiro dia de cada mes" in normalized:
        return "monthly", [], 1
    if weekdays or "semanal" in normalized:
        return "weekly", weekdays or [0]
    return "daily", [], None


def _extract_time(normalized: str) -> tuple[int, int]:
    match = re.search(r"(?:as|às|a?s)\s*(\d{1,2})(?:[:h](\d{2}))?", normalized)
    if match:
        return min(23, int(match.group(1))), int(match.group(2) or 0)
    if "manha" in normalized or "manhã" in normalized:
        return 8, 0
    if "tarde" in normalized:
        return 14, 0
    if "noite" in normalized:
        return 20, 0
    return 9, 0


def _next_run_timestamp(record: dict[str, Any], *, now: float) -> float:
    current = datetime.fromtimestamp(now, DEFAULT_TZ)
    hour = int(record.get("hour", 9))
    minute = int(record.get("minute", 0))
    frequency = str(record.get("frequency", "daily"))
    candidate = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if frequency == "daily":
        if candidate.timestamp() <= now:
            candidate += timedelta(days=1)
        return candidate.timestamp()
    if frequency == "weekly":
        weekdays = [int(item) for item in record.get("weekdays", [])] or [0]
        for offset in range(0, 14):
            option = candidate + timedelta(days=offset)
            if option.weekday() in weekdays and option.timestamp() > now:
                return option.timestamp()
        return (candidate + timedelta(days=7)).timestamp()
    monthly_day = int(record.get("monthly_day") or 1)
    candidate = candidate.replace(day=min(monthly_day, 28))
    if candidate.timestamp() <= now:
        month = candidate.month + 1
        year = candidate.year + (1 if month > 12 else 0)
        month = 1 if month > 12 else month
        candidate = candidate.replace(year=year, month=month, day=min(monthly_day, 28))
    return candidate.timestamp()


def _extract_channel_id(content: str, guild: discord.Guild | None) -> int | None:
    mention = re.search(r"<#(\d+)>", content)
    if mention:
        return int(mention.group(1))
    if guild is None:
        return None
    channels = getattr(guild, "channels", [])
    if isinstance(channels, dict):
        channels = channels.values()
    names = {
        _normalize(str(getattr(channel, "name", ""))): int(channel.id)
        for channel in channels
        if getattr(channel, "id", None) and getattr(channel, "name", None)
    }
    for raw in re.findall(r"#([\w\-]+)", content, flags=re.UNICODE):
        channel_id = names.get(_normalize(raw))
        if channel_id:
            return channel_id
    return None


def _automation_title(normalized: str) -> str:
    if "reflex" in normalized:
        return "Reflexões Diárias"
    if "motivacional" in normalized or "motivacao" in normalized:
        return "Mensagens Motivacionais"
    if "meta" in normalized:
        return "Metas Recorrentes"
    if "relatorio" in normalized or "resumo" in normalized:
        return "Relatório Recorrente"
    return "Automação Recorrente"


def _local_automation_message(raw: str, history: list[str]) -> str:
    normalized = _normalize(raw)
    stamp = datetime.now(DEFAULT_TZ).strftime("%d/%m/%Y")
    if "reflex" in normalized:
        return (
            "Reflexão do dia\n\n"
            "A evolução real costuma aparecer nos detalhes: na consistência, "
            "na forma como lidamos com pequenas decisões e na coragem de ajustar "
            "a rota antes que o problema cresça.\n\n"
            f"Que hoje seja um dia de clareza, presença e execução. ({stamp})"
        )
    if "motivacional" in normalized or "motivacao" in normalized:
        return (
            "Mensagem do dia\n\n"
            "Faça o básico com excelência hoje. O resultado grande geralmente "
            "nasce da soma de decisões pequenas bem executadas."
        )
    return (
        "Lembrete automático\n\n"
        "Passando para reforçar a rotina configurada por Talisson. Mantenham a "
        "atenção no que foi combinado e sigam com foco."
    )


def _clean_message(text: str) -> str:
    cleaned = re.sub(r"```(?:\w+)?", "", str(text)).replace("```", "").strip()
    return cleaned[:1800]


def _format_timestamp(value: float) -> str:
    return datetime.fromtimestamp(value, DEFAULT_TZ).strftime("%d/%m/%Y às %H:%M")


def _safe_key(value: str) -> str:
    key = _normalize(value).replace(" ", "-")
    key = re.sub(r"[^a-z0-9:_-]", "", key)
    return key[:100] or hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _normalize(value: str) -> str:
    normalized = value.casefold()
    replacements = str.maketrans("áàãâéêíóôõúüç", "aaaaeeiooouuc")
    return " ".join(normalized.translate(replacements).split())


async def asyncio_sleep() -> None:
    import asyncio

    await asyncio.sleep(0.4)
