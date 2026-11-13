from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiohttp
import discord

from src.ai.groq_service import GroqService, MediaInput
from src.ai.moderation_ai import sanitize_output

from .admin import AdminAudit
from .identity import OWNER_DISCORD_ID
from .state import StateStore

LOGGER = logging.getLogger(__name__)
CONFIG_SECTION = "anti_scam_config"
CACHE_SECTION = "anti_scam_hash_cache"
EVENTS_SECTION = "anti_scam_events"
IMAGE_MIME_TYPES = {
    "image/png",
    "image/jpeg",
    "image/webp",
    "image/gif",
}


@dataclass
class AntiScamConfig:
    enabled: bool = False
    logChannelId: int = 0
    confidenceThreshold: float = 0.88
    cacheEnabled: bool = True
    aiEnabled: bool = True
    maxImageBytes: int = 8 * 1024 * 1024
    categories: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AntiScamConfig":
        return cls(
            enabled=bool(data.get("enabled", False)),
            logChannelId=_int(data.get("logChannelId")),
            confidenceThreshold=max(
                0.5,
                min(1.0, _float(data.get("confidenceThreshold"), 0.88)),
            ),
            cacheEnabled=bool(data.get("cacheEnabled", True)),
            aiEnabled=bool(data.get("aiEnabled", data.get("geminiEnabled", True))),
            maxImageBytes=max(
                256 * 1024,
                min(
                    12 * 1024 * 1024,
                    _int(data.get("maxImageBytes")) or 8 * 1024 * 1024,
                ),
            ),
            categories=[
                str(value)[:120]
                for value in data.get("categories", [])
                if str(value).strip()
            ][:50],
        )


@dataclass(frozen=True)
class AntiScamResult:
    classification: str
    confidence: float
    reason: str
    categories: tuple[str, ...] = ()
    indicators: tuple[str, ...] = ()
    source: str = "groq"

    def to_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "categories": list(self.categories),
            "indicators": list(self.indicators),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AntiScamResult":
        classification = str(data.get("classification", "SAFE")).upper()
        if classification not in {"SAFE", "SUSPEITO", "SCAM"}:
            classification = "SUSPEITO"
        return cls(
            classification=classification,
            confidence=max(0.0, min(1.0, _float(data.get("confidence"), 0))),
            reason=sanitize_output(str(data.get("reason", "")), 800),
            categories=tuple(
                sanitize_output(str(value), 100)
                for value in data.get("categories", [])
                if sanitize_output(str(value), 100)
            )[:12],
            indicators=tuple(
                sanitize_output(str(value), 140)
                for value in data.get("indicators", [])
                if sanitize_output(str(value), 140)
            )[:15],
            source=str(data.get("source", "groq")),
        )


class AntiScamVision:
    def __init__(
        self,
        service: GroqService,
        state: StateStore,
        audit: AdminAudit,
        *,
        config_path: str | Path = "config/antiscam.json",
    ) -> None:
        self.service = service
        self.state = state
        self.audit = audit
        self.config_path = Path(config_path)
        self._default = self._load_file()

    async def config(self) -> AntiScamConfig:
        saved = await self.state.get_section(CONFIG_SECTION)
        merged = {**asdict(self._default), **saved}
        return AntiScamConfig.from_dict(merged)

    async def set_enabled(self, enabled: bool) -> AntiScamConfig:
        await self.state.update_section(CONFIG_SECTION, "enabled", enabled)
        await self.state.update_section(
            CONFIG_SECTION,
            "updatedAt",
            datetime.now(timezone.utc).isoformat(),
        )
        return await self.config()

    async def set_log_channel(self, channel_id: int) -> AntiScamConfig:
        await self.state.update_section(
            CONFIG_SECTION,
            "logChannelId",
            channel_id,
        )
        return await self.config()

    async def handle_message(
        self,
        message: discord.Message,
        session: aiohttp.ClientSession,
    ) -> bool:
        if message.guild is None or message.author.bot:
            return False
        config = await self.config()
        if not config.enabled:
            return False
        images = await self._message_images(message, session, config)
        if not images:
            return False

        removal: tuple[str, AntiScamResult] | None = None
        for data, mime_type in images:
            image_hash = hashlib.sha256(data).hexdigest()
            result = await self._cached(image_hash) if config.cacheEnabled else None
            if result is None:
                result = await self._analyze(
                    data,
                    mime_type,
                    message,
                    config,
                )
                if result is None:
                    continue
                if config.cacheEnabled:
                    await self._store_cache(image_hash, result)
            else:
                await self._touch_cache(image_hash)
            if (
                result.classification == "SCAM"
                and result.confidence >= config.confidenceThreshold
            ):
                removal = (image_hash, result)
                break

        if removal is None:
            return False
        image_hash, result = removal
        deleted = False
        delete_error = ""
        try:
            await message.delete()
            deleted = True
        except (discord.Forbidden, discord.HTTPException) as exc:
            delete_error = f"{type(exc).__name__}: {exc}"
        await self._record_event(
            message,
            image_hash,
            result,
            deleted=deleted,
            error=delete_error,
        )
        await self._send_security_report(
            message,
            image_hash,
            result,
            config,
            deleted=deleted,
            error=delete_error,
        )
        return True

    async def _analyze(
        self,
        data: bytes,
        mime_type: str,
        message: discord.Message,
        config: AntiScamConfig,
    ) -> AntiScamResult | None:
        if not config.aiEnabled or not self.service.available:
            await self.state.set(
                "anti_scam_last_error",
                "Análise visual da Groq indisponível; imagem não classificada.",
            )
            return None
        schema = {
            "type": "object",
            "properties": {
                "classification": {
                    "type": "string",
                    "enum": ["SAFE", "SUSPEITO", "SCAM"],
                },
                "confidence": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 1,
                },
                "reason": {"type": "string"},
                "categories": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "indicators": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
            "required": [
                "classification",
                "confidence",
                "reason",
                "categories",
                "indicators",
            ],
            "additionalProperties": False,
        }
        context = " ".join(message.clean_content.split())[:800]
        prompt = (
            "Você atua como classificador visual antifraude para moderação de "
            "Discord. Analise a imagem inteira, incluindo texto visível, interface, "
            "valores, perfis imitados, domínios, promessas e contexto. Classifique "
            "como SAFE, SUSPEITO ou SCAM. Use SCAM somente quando houver evidência "
            "forte de promoção fraudulenta, cassino enganoso, falsa recompensa, "
            "falso saque, dinheiro ou lucro garantido, cripto recebida sem contexto "
            "legítimo, promo code abusivo, urgência artificial, impersonação de "
            "celebridade/influenciador ou combinação equivalente. Use SUSPEITO se "
            "os sinais forem incompletos. Não classifique como SCAM apenas porque "
            "a imagem menciona criptomoeda, aposta ou dinheiro em contexto "
            "jornalístico, educativo ou legítimo. Retorne motivo objetivo e os "
            "indicadores visuais observados. Não invente texto ilegível.\n\n"
            f"Categorias configuradas: {json.dumps(config.categories, ensure_ascii=False)}\n"
            f"Texto da mensagem: {context or 'sem texto'}"
        )
        try:
            response = await self.service.generate_json(
                prompt,
                schema,
                max_output_tokens=650,
                timeout_seconds=12,
                media=[MediaInput(data=data, mime_type=mime_type)],
            )
        except Exception as exc:
            await self.state.set(
                "anti_scam_last_error",
                f"{type(exc).__name__}: {exc}",
            )
            return None
        result = AntiScamResult.from_dict(
            {**response, "source": "groq"}
        )
        await self.state.set("anti_scam_last_error", None)
        return result

    async def _message_images(
        self,
        message: discord.Message,
        session: aiohttp.ClientSession,
        config: AntiScamConfig,
    ) -> list[tuple[bytes, str]]:
        images: list[tuple[bytes, str]] = []
        total = 0
        for attachment in message.attachments[:6]:
            mime_type = (
                attachment.content_type
                or _mime_from_name(attachment.filename)
            ).casefold()
            if mime_type not in IMAGE_MIME_TYPES:
                continue
            if attachment.size > config.maxImageBytes:
                continue
            if total + attachment.size > config.maxImageBytes * 2:
                break
            try:
                data = await attachment.read(use_cached=True)
            except (discord.Forbidden, discord.HTTPException):
                continue
            total += len(data)
            images.append((data, mime_type))
        if images:
            return images

        urls: list[str] = []
        for embed in message.embeds[:4]:
            image = getattr(embed.image, "url", None)
            thumbnail = getattr(embed.thumbnail, "url", None)
            urls.extend(url for url in (image, thumbnail) if url)
        for url in dict.fromkeys(urls):
            try:
                async with session.get(url, allow_redirects=True) as response:
                    mime_type = response.headers.get(
                        "Content-Type",
                        "",
                    ).split(";", 1)[0].casefold()
                    if response.status >= 400 or mime_type not in IMAGE_MIME_TYPES:
                        continue
                    if int(response.headers.get("Content-Length", "0") or 0) > (
                        config.maxImageBytes
                    ):
                        continue
                    data = await response.content.read(config.maxImageBytes + 1)
                    if len(data) <= config.maxImageBytes:
                        images.append((data, mime_type))
            except (aiohttp.ClientError, ValueError):
                continue
        return images[:4]

    async def _cached(self, image_hash: str) -> AntiScamResult | None:
        values = await self.state.get_section(CACHE_SECTION)
        value = values.get(image_hash)
        if not isinstance(value, dict):
            return None
        result = value.get("result")
        return (
            AntiScamResult.from_dict(result)
            if isinstance(result, dict)
            else None
        )

    async def _store_cache(
        self,
        image_hash: str,
        result: AntiScamResult,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()

        def update(values: dict[str, Any]) -> None:
            values[image_hash] = {
                "firstSeenAt": now,
                "lastSeenAt": now,
                "hits": 1,
                "result": result.to_dict(),
            }
            while len(values) > 10000:
                values.pop(next(iter(values)))

        await self.state.mutate_section(CACHE_SECTION, update)

    async def _touch_cache(self, image_hash: str) -> None:
        now = datetime.now(timezone.utc).isoformat()

        def update(values: dict[str, Any]) -> None:
            current = values.get(image_hash)
            if not isinstance(current, dict):
                return
            current["lastSeenAt"] = now
            current["hits"] = int(current.get("hits", 0)) + 1

        await self.state.mutate_section(CACHE_SECTION, update)

    async def _record_event(
        self,
        message: discord.Message,
        image_hash: str,
        result: AntiScamResult,
        *,
        deleted: bool,
        error: str,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()

        def append(values: dict[str, Any]) -> None:
            values[f"{now}:{message.id}"] = {
                "timestamp": now,
                "guildId": message.guild.id,
                "channelId": message.channel.id,
                "messageId": message.id,
                "userId": message.author.id,
                "messageUrl": message.jump_url,
                "imageHash": image_hash,
                "classification": result.classification,
                "confidence": result.confidence,
                "reason": result.reason,
                "categories": list(result.categories),
                "indicators": list(result.indicators),
                "deleted": deleted,
                "error": error or None,
            }
            while len(values) > 10000:
                values.pop(next(iter(values)))

        await self.state.mutate_section(EVENTS_SECTION, append)

    async def _send_security_report(
        self,
        message: discord.Message,
        image_hash: str,
        result: AntiScamResult,
        config: AntiScamConfig,
        *,
        deleted: bool,
        error: str,
    ) -> None:
        channel_id = config.logChannelId or await self.audit.channel_id_for(
            message.guild.id,
            "suspicious",
        )
        if not channel_id:
            LOGGER.warning(
                "Anti-Scam detectou SCAM, mas o canal de segurança não está configurado."
            )
            return
        channel = message.guild.get_channel(channel_id)
        if channel is None:
            try:
                channel = await message.client.fetch_channel(channel_id)
            except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                return
        if not isinstance(channel, discord.abc.Messageable):
            return
        embed = discord.Embed(
            title="Possível golpe detectado",
            description=(
                "Uma imagem foi classificada como golpe visual e passou pela "
                "política automática de segurança."
            ),
            color=discord.Color.from_rgb(218, 54, 51),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(
            name="Usuário",
            value=f"{message.author.mention}\n`{message.author.id}`",
            inline=True,
        )
        embed.add_field(
            name="Canal",
            value=f"{message.channel.mention}\n`{message.channel.id}`",
            inline=True,
        )
        embed.add_field(
            name="Classificação",
            value=(
                f"**{result.classification}**\n"
                f"Confiança: **{result.confidence * 100:.1f}%**"
            ),
            inline=True,
        )
        embed.add_field(
            name="Motivo",
            value=result.reason or "Sinais visuais compatíveis com golpe.",
            inline=False,
        )
        if result.indicators:
            embed.add_field(
                name="Indicadores",
                value="\n".join(f"- {value}" for value in result.indicators)[:1024],
                inline=False,
            )
        embed.add_field(
            name="Mensagem",
            value=f"[Abrir referência]({message.jump_url})",
            inline=False,
        )
        embed.add_field(
            name="Ação",
            value=(
                "Mensagem removida automaticamente."
                if deleted
                else f"Remoção não concluída: {error or 'erro desconhecido'}"
            ),
            inline=False,
        )
        embed.set_footer(text=f"Bella Anti-Scam Vision | hash {image_hash[:16]}")
        try:
            await channel.send(
                embed=embed,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except (discord.Forbidden, discord.HTTPException):
            pass

    def _load_file(self) -> AntiScamConfig:
        try:
            data = json.loads(self.config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
        return AntiScamConfig.from_dict(data)


def antiscam_status_embed(
    config: AntiScamConfig,
    *,
    cache_entries: int,
    events: int,
    last_error: str | None,
) -> discord.Embed:
    ai_enabled = bool(
        getattr(config, "aiEnabled", getattr(config, "geminiEnabled", True))
    )
    active = config.enabled and ai_enabled
    embed = discord.Embed(
        title="Bella Anti-Scam Vision",
        description=(
            "Moderação visual ativa e analisando imagens."
            if active
            else "Moderação visual desativada. Nenhuma imagem é enviada à Groq."
        ),
        color=(
            discord.Color.from_rgb(46, 160, 67)
            if active
            else discord.Color.from_rgb(90, 96, 105)
        ),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(
        name="Estado",
        value="Ativado" if config.enabled else "Desativado",
        inline=True,
    )
    embed.add_field(
        name="Groq Vision",
        value="Permitido" if ai_enabled else "Bloqueado",
        inline=True,
    )
    embed.add_field(
        name="Confiança mínima",
        value=f"{config.confidenceThreshold * 100:.0f}%",
        inline=True,
    )
    embed.add_field(
        name="Cache",
        value=(
            f"{'Ativo' if config.cacheEnabled else 'Desativado'}\n"
            f"{cache_entries} hashes registrados"
        ),
        inline=True,
    )
    embed.add_field(
        name="Eventos",
        value=str(events),
        inline=True,
    )
    embed.add_field(
        name="Canal de segurança",
        value=(
            f"<#{config.logChannelId}>"
            if config.logChannelId
            else "Canal de segurança administrativo"
        ),
        inline=True,
    )
    if last_error:
        embed.add_field(
            name="Última limitação",
            value=sanitize_output(last_error, 1000),
            inline=False,
        )
    embed.set_footer(
        text="Desativado por padrão para evitar consumo desnecessário da API"
    )
    return embed


def _mime_from_name(filename: str) -> str:
    extension = filename.rsplit(".", 1)[-1].casefold() if "." in filename else ""
    return {
        "png": "image/png",
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "webp": "image/webp",
        "gif": "image/gif",
    }.get(extension, "application/octet-stream")


def _int(value: object) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _float(value: object, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
