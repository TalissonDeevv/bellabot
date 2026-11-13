from __future__ import annotations

import asyncio
import logging
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord

from .state import StateStore


LOGGER = logging.getLogger(__name__)
BIRTHDAY_CONFIG_KEY = "birthday_celebration_config"
DEFAULT_BIRTH_DATE = "2004-11-10"
DEFAULT_TIMEZONE = "America/Recife"
DEFAULT_BIRTHDAY_CHANNEL_ID = 1467981739937759304


@dataclass(slots=True)
class BirthdayConfig:
    enabled: bool
    user_id: int
    channel_id: int
    birth_date: str
    timezone: str
    hour: int
    minute: int
    last_sent_year: int
    last_message_id: int
    last_sent_at: str
    last_error: str

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "BirthdayConfig":
        return cls(
            enabled=bool(value.get("enabled", True)),
            user_id=int(value.get("user_id") or 0),
            channel_id=int(value.get("channel_id") or 0),
            birth_date=str(value.get("birth_date") or DEFAULT_BIRTH_DATE),
            timezone=str(value.get("timezone") or DEFAULT_TIMEZONE),
            hour=max(0, min(23, int(value.get("hour", 0) or 0))),
            minute=max(0, min(59, int(value.get("minute", 0) or 0))),
            last_sent_year=int(value.get("last_sent_year") or 0),
            last_message_id=int(value.get("last_message_id") or 0),
            last_sent_at=str(value.get("last_sent_at") or ""),
            last_error=str(value.get("last_error") or ""),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "user_id": self.user_id,
            "channel_id": self.channel_id,
            "birth_date": self.birth_date,
            "timezone": self.timezone,
            "hour": self.hour,
            "minute": self.minute,
            "last_sent_year": self.last_sent_year,
            "last_message_id": self.last_message_id,
            "last_sent_at": self.last_sent_at,
            "last_error": self.last_error,
        }


def birthday_age(birth_date: date, current_date: date) -> int:
    years = current_date.year - birth_date.year
    if (current_date.month, current_date.day) < (birth_date.month, birth_date.day):
        years -= 1
    return max(0, years)


def birthday_is_due(config: BirthdayConfig, now: datetime) -> bool:
    if not config.enabled or not config.user_id or not config.channel_id:
        return False
    try:
        birth_date = date.fromisoformat(config.birth_date)
        zone = ZoneInfo(config.timezone)
    except (ValueError, ZoneInfoNotFoundError):
        return False
    local_now = now.astimezone(zone)
    if (local_now.month, local_now.day) != (birth_date.month, birth_date.day):
        return False
    if (local_now.hour, local_now.minute) < (config.hour, config.minute):
        return False
    return config.last_sent_year != local_now.year


def next_birthday(config: BirthdayConfig, now: datetime) -> tuple[datetime, int, bool]:
    birth_date = date.fromisoformat(config.birth_date)
    zone = ZoneInfo(config.timezone)
    local_now = now.astimezone(zone)
    candidate = datetime(
        local_now.year,
        birth_date.month,
        birth_date.day,
        config.hour,
        config.minute,
        tzinfo=zone,
    )
    is_today = local_now.date() == candidate.date()
    if not is_today and candidate <= local_now:
        candidate = candidate.replace(year=candidate.year + 1)
    return candidate, birthday_age(birth_date, candidate.date()), is_today


def _normalize_text(value: str) -> str:
    text = unicodedata.normalize("NFKD", value)
    return "".join(character for character in text if not unicodedata.combining(character)).casefold()


def _neutralize_mass_mentions(value: str) -> str:
    return re.sub(
        r"@\s*(everyone|here)\b",
        lambda match: f"@\u200b{match.group(1)}",
        value,
        flags=re.IGNORECASE,
    )


def _remove_birth_details(value: str) -> str:
    sanitized = re.sub(
        r"(?i)\b(?:data de nascimento|ano de nascimento|nasceu em|nascido em)\b[^.!?\n]*(?:[.!?]|$)",
        "",
        value,
    )
    sanitized = re.sub(r"\b10[/-]11[/-]2004\b|\b2004\b", "", sanitized)
    sanitized = re.sub(r"[ \t]{2,}", " ", sanitized)
    sanitized = re.sub(r"\n{3,}", "\n\n", sanitized)
    return sanitized.strip()


class BirthdayCelebrationService:
    def __init__(
        self,
        bot: discord.Client,
        state: StateStore,
        ai_service: Any,
        *,
        user_id: int,
        channel_id: int,
    ) -> None:
        self.bot = bot
        self.state = state
        self.ai_service = ai_service
        self.default_user_id = int(user_id)
        self.default_channel_id = int(channel_id or DEFAULT_BIRTHDAY_CHANNEL_ID)
        self._lock = asyncio.Lock()

    async def ensure_config(self) -> BirthdayConfig:
        stored = await self.state.get(BIRTHDAY_CONFIG_KEY, {})
        payload = stored if isinstance(stored, dict) else {}
        defaults = BirthdayConfig(
            enabled=True,
            user_id=self.default_user_id,
            channel_id=self.default_channel_id,
            birth_date=DEFAULT_BIRTH_DATE,
            timezone=DEFAULT_TIMEZONE,
            hour=0,
            minute=0,
            last_sent_year=0,
            last_message_id=0,
            last_sent_at="",
            last_error="",
        ).to_dict()
        merged = {**defaults, **payload}
        config = BirthdayConfig.from_dict(merged)
        if payload != config.to_dict():
            await self.state.set(BIRTHDAY_CONFIG_KEY, config.to_dict())
        return config

    async def run_due(self, *, now: datetime | None = None) -> bool:
        async with self._lock:
            config = await self.ensure_config()
            checked_at = now or datetime.now(timezone.utc)
            if not birthday_is_due(config, checked_at):
                return False
            try:
                message = await self._send(config, checked_at)
            except Exception as exc:
                config.last_error = f"{type(exc).__name__}: {exc}"[:1000]
                await self.state.set(BIRTHDAY_CONFIG_KEY, config.to_dict())
                LOGGER.exception("Falha ao publicar a celebração anual de aniversário")
                return False

            zone = ZoneInfo(config.timezone)
            local_now = checked_at.astimezone(zone)
            config.last_sent_year = local_now.year
            config.last_message_id = int(message.id)
            config.last_sent_at = datetime.now(timezone.utc).isoformat()
            config.last_error = ""
            await self.state.set(BIRTHDAY_CONFIG_KEY, config.to_dict())
            LOGGER.info(
                "Celebração de aniversário publicada no canal %s para o usuário %s.",
                config.channel_id,
                config.user_id,
            )
            return True

    async def handle_message(self, message: discord.Message) -> bool:
        if message.author.bot or not self._is_birthday_question(message):
            return False
        config = await self.ensure_config()
        if not config.enabled:
            return False
        now = datetime.now(timezone.utc)
        try:
            target, age, is_today = next_birthday(config, now)
        except (ValueError, ZoneInfoNotFoundError):
            return False
        timestamp = int(target.timestamp())
        response = await self._countdown_text(age, target, is_today, timestamp)
        await message.channel.send(
            response[:2000],
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return True

    def _is_birthday_question(self, message: discord.Message) -> bool:
        content = _normalize_text(message.content)
        if not re.search(r"\b(aniversario|niver)\b", content):
            return False
        asks_date = bool(
            re.search(
                r"\b(quanto falta|quando|que dia|qual dia|data|quantos dias|qual idade|quantos anos)\b",
                content,
            )
        )
        if not asks_date:
            return False
        if message.author.id == self.default_user_id and re.search(r"\b(meu|minha)\b", content):
            return True
        return bool(
            "talisson" in content
            or f"<@{self.default_user_id}>" in message.content
            or f"<@!{self.default_user_id}>" in message.content
        )

    async def _send(self, config: BirthdayConfig, now: datetime) -> discord.Message:
        channel = self.bot.get_channel(config.channel_id)
        if channel is None:
            channel = await self.bot.fetch_channel(config.channel_id)
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            raise RuntimeError("O canal configurado para o aniversário não aceita mensagens.")

        birth_date = date.fromisoformat(config.birth_date)
        local_date = now.astimezone(ZoneInfo(config.timezone)).date()
        age = birthday_age(birth_date, local_date)
        ai_text = await self._birthday_text(age, local_date.year)
        content = (
            f"@everyone @here <@{config.user_id}>\n\n"
            f"# 🎂 Feliz aniversário, Talisson!\n"
            f"Hoje você está completando **{age} anos**.\n\n"
            f"{ai_text}"
        )[:2000]
        return await channel.send(
            content,
            allowed_mentions=discord.AllowedMentions(
                everyone=True,
                users=True,
                roles=False,
                replied_user=False,
            ),
        )

    async def _birthday_text(self, age: int, year: int) -> str:
        prompt = (
            "Escreva uma mensagem de feliz aniversário da Bella Nyelle para Talisson Souza. "
            f"Ele está completando {age} anos em 10 de novembro de {year}. "
            "Use a personalidade carinhosa, alegre, natural e levemente brincalhona da Bella, "
            "sem exagerar, sem criar dependência emocional, sem romance e sem inventar fatos. "
            "Não revele data de nascimento, ano de nascimento ou o ano 2004. "
            "Escreva de dois a quatro parágrafos curtos em português brasileiro. "
            "Não use @everyone, @here, menções, título ou bloco de código."
        )
        try:
            generated = await self.ai_service.generate_text(
                prompt,
                max_output_tokens=420,
                timeout_seconds=30,
                use_cache=False,
                system_prompt=(
                    "Você é Bella Nyelle. Escreva felicitações pessoais, calorosas e responsáveis, "
                    "preservando sua linguagem natural e sem inventar acontecimentos."
                ),
                prompt_char_limit=3000,
            )
            text = str(generated or "").strip()
            text = _remove_birth_details(_neutralize_mass_mentions(text))
            if text:
                return text[:1450]
        except Exception:
            LOGGER.exception("A IA não conseguiu gerar o texto de aniversário; usando fallback.")
        return (
            "Que este novo ciclo venha cheio de saúde, tranquilidade e conquistas que façam "
            "sentido para você. Espero continuar acompanhando seus projetos, suas ideias e cada "
            "passo dessa caminhada com muito carinho.\n\n"
            "Aproveite bastante o seu dia e celebre tudo o que você já construiu. Parabéns, hihihi! ❤️"
        )

    async def _countdown_text(
        self,
        age: int,
        target: datetime,
        is_today: bool,
        timestamp: int,
    ) -> str:
        absolute_token = f"<t:{timestamp}:F>"
        relative_token = f"<t:{timestamp}:R>"
        prompt = (
            "Responda como Bella Nyelle a uma pergunta sobre quanto falta para o aniversário "
            "de Talisson Souza. "
            f"A data calculada é {target.strftime('%d/%m/%Y às %H:%M')} no fuso {target.tzinfo}. "
            f"Nessa data ele completa {age} anos. "
            f"O aniversário é hoje: {'sim' if is_today else 'não'}. "
            f"Inclua naturalmente no texto o timestamp absoluto `{absolute_token}` e o relativo `{relative_token}`. "
            "Escreva duas ou três frases naturais, carinhosas, alegres e levemente brincalhonas em português brasileiro. "
            "Não use listas, títulos, campos, rótulos como Data/Contagem/Idade, nem formato de relatório. "
            "Não revele data de nascimento, ano de nascimento ou o ano 2004. "
            "Não calcule outra data e não invente fatos."
        )
        try:
            generated = await self.ai_service.generate_text(
                prompt,
                max_output_tokens=180,
                timeout_seconds=20,
                use_cache=False,
                system_prompt=(
                    "Você é Bella Nyelle. Responda usando somente os fatos de aniversário fornecidos."
                ),
                prompt_char_limit=1800,
            )
            text = str(generated or "").strip()
            text = _remove_birth_details(_neutralize_mass_mentions(text))
            if text:
                missing_facts = (
                    absolute_token not in text
                    or relative_token not in text
                    or str(age) not in text
                )
                if missing_facts:
                    text = (
                        f"{text.rstrip()} "
                        f"A comemoração chega {relative_token}, exatamente em {absolute_token}, "
                        f"quando ele completa {age} anos."
                    )
                return text[:1200]
        except Exception:
            LOGGER.exception("A IA não conseguiu gerar a contagem do aniversário; usando fallback.")
        if is_today:
            return (
                f"É hoje, hihihi! O aniversário do Talisson chegou {relative_token}, "
                f"em {absolute_token}, e ele está completando {age} anos. 🎂"
            )
        return (
            f"Já estou contando os dias, hihihi! O aniversário do Talisson chega {relative_token}, "
            f"em {absolute_token}, quando ele vai completar {age} anos. 🎂"
        )
