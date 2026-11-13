from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import discord

from .maintenance_center import guard_public_interaction

from .embed_builder import (
    DraftButton,
    DraftField,
    EmbedDraft,
    EmbedEditorSession,
    V2ContainerDraft,
    V2EditorSession,
    V2ItemDraft,
)
from .state import StateStore


SHOPEE_FINDS_SECTION = "shopee_dev_finds"
DEFAULT_COLOR = 0xF97316
MAX_PUBLIC_PRODUCTS = 25
MAX_PRODUCT_URL_LENGTH = 1800
MAX_DISCORD_BUTTON_URL_LENGTH = 512


@dataclass(slots=True)
class ShopeeProduct:
    id: str
    name: str
    url: str
    price: str = ""
    category: str = "Geral"
    description: str = ""
    image_url: str = ""
    coupon: str = ""
    embed_title: str = ""
    embed_color: int = DEFAULT_COLOR
    author_name: str = ""
    author_icon_url: str = ""
    thumbnail_url: str = ""
    footer_text: str = ""
    buttons: list[dict[str, str]] | None = None
    fields: list[dict[str, Any]] | None = None
    created_by: int = 0
    created_at: str = ""
    updated_at: str = ""

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ShopeeProduct":
        now = _now()
        return cls(
            id=_clean_id(str(payload.get("id") or payload.get("name") or "produto")),
            name=str(payload.get("name") or "Produto").strip()[:100],
            url=str(payload.get("url") or "").strip()[:MAX_PRODUCT_URL_LENGTH],
            price=str(payload.get("price") or "").strip()[:80],
            category=str(payload.get("category") or "Geral").strip()[:80] or "Geral",
            description=str(payload.get("description") or "").strip()[:1800],
            image_url=str(payload.get("image_url") or "").strip()[:500],
            coupon=str(payload.get("coupon") or "").strip()[:80],
            embed_title=str(payload.get("embed_title") or "").strip()[:256],
            embed_color=_safe_color(payload.get("embed_color"), DEFAULT_COLOR),
            author_name=str(payload.get("author_name") or "").strip()[:256],
            author_icon_url=str(payload.get("author_icon_url") or "").strip()[:500],
            thumbnail_url=str(payload.get("thumbnail_url") or "").strip()[:500],
            footer_text=str(payload.get("footer_text") or "").strip()[:2048],
            buttons=_normalize_buttons(payload.get("buttons")),
            fields=_normalize_fields(payload.get("fields")),
            created_by=int(payload.get("created_by") or 0),
            created_at=str(payload.get("created_at") or now),
            updated_at=str(payload.get("updated_at") or now),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "url": self.url,
            "price": self.price,
            "category": self.category,
            "description": self.description,
            "image_url": self.image_url,
            "coupon": self.coupon,
            "embed_title": self.embed_title,
            "embed_color": self.embed_color,
            "author_name": self.author_name,
            "author_icon_url": self.author_icon_url,
            "thumbnail_url": self.thumbnail_url,
            "footer_text": self.footer_text,
            "buttons": self.buttons or [],
            "fields": self.fields or [],
            "created_by": self.created_by,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(slots=True)
class ShopeeProductDraftSeed:
    name: str
    url: str
    price: str = ""
    category: str = "Geral"
    coupon: str = ""
    render_mode: str = "v2"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _discord_date(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        timestamp = int(parsed.timestamp())
        return f"<t:{timestamp}:f>"
    except (TypeError, ValueError, OverflowError):
        return "Data não registrada"


def _normalize(value: str) -> str:
    text = unicodedata.normalize("NFKD", value)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return text.casefold()


def _clean_id(value: str) -> str:
    base = _normalize(value)
    base = re.sub(r"[^a-z0-9]+", "-", base).strip("-")
    return (base or "produto")[:42]


def _valid_url(value: str) -> bool:
    return bool(re.fullmatch(r"https?://\S{4,500}", value.strip()))


def _valid_product_url(value: str) -> bool:
    return bool(re.fullmatch(rf"https?://\S{{4,{MAX_PRODUCT_URL_LENGTH}}}", value.strip()))


def _image_position(value: Any) -> str:
    normalized = (
        str(value or "")
        .strip()
        .casefold()
        .replace("í", "i")
        .replace("ó", "o")
        .replace("é", "e")
        .replace("ã", "a")
        .replace("ç", "c")
    )
    if normalized in {"top", "topo", "inicio", "início", "antes"}:
        return "top"
    if normalized in {"middle", "meio", "centro"}:
        return "middle"
    return "end"


def _v2_media_position(items: list[V2ItemDraft]) -> str:
    media_index = next(
        (
            index
            for index, item in enumerate(items)
            if item.kind == "media" and str(item.url or "").strip()
        ),
        None,
    )
    if media_index is None:
        return "end"
    if media_index == 0:
        return "top"
    if media_index < len(items) - 1:
        return "middle"
    return "end"


async def _resolve_selected_text_channel(
    interaction: discord.Interaction,
    selected: Any,
) -> discord.TextChannel | None:
    if isinstance(selected, discord.TextChannel):
        return selected
    channel_id = int(getattr(selected, "id", 0) or 0)
    if interaction.guild is None or not channel_id:
        return None
    channel = interaction.guild.get_channel(channel_id) or interaction.client.get_channel(channel_id)
    if channel is None:
        try:
            channel = await interaction.guild.fetch_channel(channel_id)
        except discord.HTTPException:
            return None
    return channel if isinstance(channel, discord.TextChannel) else None


def _safe_color(value: Any, default: int = DEFAULT_COLOR) -> int:
    if isinstance(value, int) and 0 <= value <= 0xFFFFFF:
        return value
    text = str(value or "").strip().removeprefix("#")
    try:
        color = int(text, 16)
    except (TypeError, ValueError):
        return default
    return color if 0 <= color <= 0xFFFFFF else default


def _normalize_buttons(value: Any) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    if not isinstance(value, list):
        return result
    for item in value[:5]:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or "").strip()[:80]
        url = str(item.get("url") or "").strip()[:MAX_PRODUCT_URL_LENGTH]
        if label and _valid_product_url(url):
            result.append({"label": label, "url": url})
    return result


def _normalize_fields(value: Any) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    if not isinstance(value, list):
        return result
    for item in value[:10]:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()[:256]
        field_value = str(item.get("value") or "").strip()[:1024]
        inline = bool(item.get("inline", False))
        if name and field_value:
            result.append({"name": name, "value": field_value, "inline": inline})
    return result


def _parse_button_lines(raw: str) -> list[dict[str, str]]:
    buttons: list[dict[str, str]] = []
    for line in str(raw or "").splitlines():
        clean = line.strip()
        if not clean:
            continue
        parts = [part.strip() for part in clean.split("|", 1)]
        if len(parts) != 2:
            continue
        label, url = parts
        if label and _valid_product_url(url):
            buttons.append({"label": label[:80], "url": url[:MAX_PRODUCT_URL_LENGTH]})
        if len(buttons) >= 5:
            break
    return buttons


def _parse_field_lines(raw: str) -> list[dict[str, Any]]:
    fields: list[dict[str, Any]] = []
    for line in str(raw or "").splitlines():
        clean = line.strip()
        if not clean:
            continue
        parts = [part.strip() for part in clean.split("|")]
        if len(parts) < 2:
            continue
        inline_text = _normalize(parts[2]) if len(parts) >= 3 else ""
        fields.append(
            {
                "name": parts[0][:256],
                "value": parts[1][:1024],
                "inline": inline_text in {"sim", "s", "true", "1", "inline"},
            }
        )
        if len(fields) >= 10:
            break
    return fields


def _button_lines(buttons: list[dict[str, str]] | None) -> str:
    return "\n".join(f"{button.get('label', '')}|{button.get('url', '')}" for button in (buttons or []))


def _field_lines(fields: list[dict[str, Any]] | None) -> str:
    return "\n".join(
        f"{field.get('name', '')}|{field.get('value', '')}|{'sim' if field.get('inline') else 'não'}"
        for field in (fields or [])
    )


def _trim(value: str, limit: int) -> str:
    value = str(value or "").strip()
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 1)].rstrip() + "…"


def _parse_extra_details(raw: str) -> dict[str, Any]:
    description_lines: list[str] = []
    image_url = ""
    thumbnail_url = ""
    coupon = ""
    footer_text = ""
    buttons: list[dict[str, str]] = []
    fields: list[dict[str, Any]] = []
    for line in str(raw or "").splitlines():
        clean = line.strip()
        if not clean:
            continue
        lower = _normalize(clean)
        if lower.startswith(("imagem:", "image:", "foto:")):
            image_url = clean.split(":", 1)[1].strip()[:500]
            continue
        if lower.startswith(("thumbnail:", "thumb:")):
            thumbnail_url = clean.split(":", 1)[1].strip()[:500]
            continue
        if lower.startswith(("cupom:", "coupon:")):
            coupon = clean.split(":", 1)[1].strip()[:80]
            continue
        if lower.startswith(("rodape:", "rodapé:", "footer:")):
            footer_text = clean.split(":", 1)[1].strip()[:2048]
            continue
        if lower.startswith(("botao:", "botão:", "button:")):
            button = _parse_button_lines(clean.split(":", 1)[1].strip())
            if button:
                buttons.extend(button[: max(0, 5 - len(buttons))])
            continue
        if lower.startswith(("campo:", "field:")):
            field = _parse_field_lines(clean.split(":", 1)[1].strip())
            if field:
                fields.extend(field[: max(0, 10 - len(fields))])
            continue
        description_lines.append(clean)
    return {
        "description": "\n".join(description_lines).strip()[:1800],
        "image_url": image_url,
        "thumbnail_url": thumbnail_url,
        "coupon": coupon,
        "footer_text": footer_text,
        "buttons": buttons,
        "fields": fields,
    }


class ShopeeFindsService:
    def __init__(self, state: StateStore, *, owner_id: int) -> None:
        self.state = state
        self.owner_id = int(owner_id)

    async def settings_for(self, guild_id: int) -> dict[str, Any]:
        section = await self.state.get_section(SHOPEE_FINDS_SECTION)
        raw = section.get(str(guild_id))
        if not isinstance(raw, dict):
            return _default_settings()
        payload = {**_default_settings(), **raw}
        products = payload.get("products")
        payload["products"] = products if isinstance(products, dict) else {}
        payload["enabled"] = bool(payload.get("enabled", True))
        payload["render_mode"] = "v2" if str(payload.get("render_mode", "v1")).lower() == "v2" else "v1"
        payload["color"] = _safe_color(payload.get("color"), DEFAULT_COLOR)
        payload["image_position"] = _image_position(payload.get("image_position", "top"))
        payload["manager_role_ids"] = [
            int(role_id)
            for role_id in (payload.get("manager_role_ids") or [])
            if str(role_id).isdigit()
        ][:10]
        try:
            payload["notification_role_id"] = int(payload.get("notification_role_id") or 0)
        except (TypeError, ValueError):
            payload["notification_role_id"] = 0
        announced = payload.get("announced_product_ids")
        payload["announced_product_ids"] = [
            str(product_id)
            for product_id in (announced if isinstance(announced, list) else [])
            if str(product_id).strip()
        ]
        payload["fields"] = _normalize_fields(payload.get("fields"))
        return payload

    async def save_settings(self, guild_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        current = await self.settings_for(guild_id)
        merged = {**current, **payload, "updated_at": _now()}
        await self.state.update_section(SHOPEE_FINDS_SECTION, str(guild_id), merged)
        return merged

    async def products(self, guild_id: int) -> list[ShopeeProduct]:
        settings = await self.settings_for(guild_id)
        products = settings.get("products")
        if not isinstance(products, dict):
            return []
        result = [ShopeeProduct.from_dict(item) for item in products.values() if isinstance(item, dict)]
        result.sort(key=lambda item: (_normalize(item.category), _normalize(item.name)))
        return result

    async def product(self, guild_id: int, product_id: str) -> ShopeeProduct | None:
        settings = await self.settings_for(guild_id)
        products = settings.get("products")
        if not isinstance(products, dict):
            return None
        raw = products.get(product_id)
        return ShopeeProduct.from_dict(raw) if isinstance(raw, dict) else None

    async def add_product(
        self,
        guild_id: int,
        *,
        name: str,
        url: str,
        price: str = "",
        category: str = "Geral",
        description: str = "",
        image_url: str = "",
        coupon: str = "",
        embed_title: str = "",
        embed_color: int = DEFAULT_COLOR,
        author_name: str = "",
        author_icon_url: str = "",
        thumbnail_url: str = "",
        footer_text: str = "",
        buttons: list[dict[str, str]] | None = None,
        fields: list[dict[str, Any]] | None = None,
        created_by: int = 0,
    ) -> ShopeeProduct:
        if not name.strip():
            raise ValueError("Informe o nome do produto.")
        if not _valid_product_url(url):
            raise ValueError(f"Informe um link válido começando com http:// ou https:// com até {MAX_PRODUCT_URL_LENGTH} caracteres.")
        if image_url and not _valid_url(image_url):
            raise ValueError("A imagem precisa ser uma URL válida ou ficar vazia.")
        if thumbnail_url and not _valid_url(thumbnail_url):
            raise ValueError("A thumbnail precisa ser uma URL válida ou ficar vazia.")
        if author_icon_url and not _valid_url(author_icon_url):
            raise ValueError("O ícone do autor precisa ser uma URL válida ou ficar vazio.")
        settings = await self.settings_for(guild_id)
        products = settings.setdefault("products", {})
        product_id = _unique_product_id(products, name)
        product = ShopeeProduct(
            id=product_id,
            name=name.strip()[:100],
            url=url.strip()[:MAX_PRODUCT_URL_LENGTH],
            price=price.strip()[:80],
            category=category.strip()[:80] or "Geral",
            description=description.strip()[:1800],
            image_url=image_url.strip()[:500],
            coupon=coupon.strip()[:80],
            embed_title=embed_title.strip()[:256],
            embed_color=_safe_color(embed_color, DEFAULT_COLOR),
            author_name=author_name.strip()[:256],
            author_icon_url=author_icon_url.strip()[:500],
            thumbnail_url=thumbnail_url.strip()[:500],
            footer_text=footer_text.strip()[:2048],
            buttons=_normalize_buttons(buttons or [{"label": "Abrir na Shopee", "url": url.strip()[:MAX_PRODUCT_URL_LENGTH]}]),
            fields=_normalize_fields(fields or []),
            created_by=int(created_by or 0),
            created_at=_now(),
            updated_at=_now(),
        )
        products[product.id] = product.to_dict()
        await self.save_settings(guild_id, settings)
        return product

    async def remove_product(self, guild_id: int, product_id: str) -> ShopeeProduct | None:
        settings = await self.settings_for(guild_id)
        products = settings.setdefault("products", {})
        raw = products.pop(str(product_id), None)
        await self.save_settings(guild_id, settings)
        return ShopeeProduct.from_dict(raw) if isinstance(raw, dict) else None

    async def update_product(
        self,
        guild_id: int,
        product_id: str,
        **updates: Any,
    ) -> ShopeeProduct | None:
        settings = await self.settings_for(guild_id)
        products = settings.setdefault("products", {})
        raw = products.get(str(product_id))
        if not isinstance(raw, dict):
            return None
        payload = {**raw, **updates, "updated_at": _now()}
        product = ShopeeProduct.from_dict(payload)
        if not product.name.strip() or not _valid_product_url(product.url):
            raise ValueError("Produto precisa manter nome e link válidos.")
        for label, value in {
            "imagem": product.image_url,
            "thumbnail": product.thumbnail_url,
            "ícone do autor": product.author_icon_url,
        }.items():
            if value and not _valid_url(value):
                raise ValueError(f"A URL de {label} precisa começar com http:// ou https://.")
        product.buttons = _normalize_buttons(product.buttons or [{"label": "Abrir na Shopee", "url": product.url}])
        product.fields = _normalize_fields(product.fields or [])
        products[product.id] = product.to_dict()
        await self.save_settings(guild_id, settings)
        return product

    async def clear_products(self, guild_id: int) -> int:
        settings = await self.settings_for(guild_id)
        products = settings.setdefault("products", {})
        count = len(products)
        settings["products"] = {}
        await self.save_settings(guild_id, settings)
        return count

    async def set_enabled(self, guild_id: int, enabled: bool, reason: str = "") -> dict[str, Any]:
        current = await self.settings_for(guild_id)
        current["enabled"] = bool(enabled)
        if reason.strip():
            current["closed_reason"] = reason.strip()[:500]
        return await self.save_settings(guild_id, current)

    async def set_render_mode(self, guild_id: int, mode: str) -> dict[str, Any]:
        current = await self.settings_for(guild_id)
        current["render_mode"] = "v2" if str(mode).lower() == "v2" else "v1"
        return await self.save_settings(guild_id, current)

    async def save_public_panel(self, guild_id: int, channel_id: int, message_id: int) -> dict[str, Any]:
        return await self.save_settings(
            guild_id,
            {
                "panel_channel_id": int(channel_id),
                "panel_message_id": int(message_id),
            },
        )

    async def save_admin_panel(self, guild_id: int, channel_id: int, message_id: int) -> dict[str, Any]:
        return await self.save_settings(
            guild_id,
            {
                "admin_channel_id": int(channel_id),
                "admin_message_id": int(message_id),
            },
        )

    async def update_text(
        self,
        guild_id: int,
        *,
        message_content: str,
        title: str,
        description: str,
        footer: str,
        closed_reason: str,
    ) -> dict[str, Any]:
        return await self.save_settings(
            guild_id,
            {
                "message_content": message_content.strip()[:1800],
                "title": title.strip()[:120] or "Achadinhos do Dev",
                "description": description.strip()[:2000] or _default_settings()["description"],
                "footer": footer.strip()[:120] or "Bella Finds Center",
                "closed_reason": closed_reason.strip()[:500] or _default_settings()["closed_reason"],
            },
        )

    async def update_visual(
        self,
        guild_id: int,
        *,
        color: str,
        author_name: str,
        author_icon_url: str,
        thumbnail_url: str,
        image_url: str,
        image_position: str | None = None,
    ) -> dict[str, Any]:
        urls = {
            "author_icon_url": author_icon_url.strip(),
            "thumbnail_url": thumbnail_url.strip(),
            "image_url": image_url.strip(),
        }
        for label, value in urls.items():
            if value and not _valid_url(value):
                raise ValueError(f"A URL de {label} precisa começar com http:// ou https://.")
        payload: dict[str, Any] = {
            "color": _safe_color(color, DEFAULT_COLOR),
            "author_name": author_name.strip()[:120],
            **{key: value[:500] for key, value in urls.items()},
        }
        if image_position is not None:
            payload["image_position"] = _image_position(image_position)
        return await self.save_settings(guild_id, payload)

    async def set_manager_roles(self, guild_id: int, role_ids: list[int]) -> dict[str, Any]:
        return await self.save_settings(
            guild_id,
            {"manager_role_ids": [int(role_id) for role_id in role_ids[:10]]},
        )

    async def set_notification_role(self, guild_id: int, role_id: int | None) -> dict[str, Any]:
        return await self.save_settings(guild_id, {"notification_role_id": int(role_id or 0)})

    async def mark_announced(self, guild_id: int, product_ids: list[str]) -> dict[str, Any]:
        settings = await self.settings_for(guild_id)
        current = {str(product_id) for product_id in settings.get("announced_product_ids", [])}
        current.update(str(product_id) for product_id in product_ids if str(product_id).strip())
        settings["announced_product_ids"] = sorted(current)
        return await self.save_settings(guild_id, settings)


def _default_settings() -> dict[str, Any]:
    return {
        "enabled": True,
        "closed_reason": "Os achadinhos estão pausados no momento. Volte um pouco mais tarde.",
        "render_mode": "v1",
        "message_content": "",
        "color": DEFAULT_COLOR,
        "author_name": "",
        "author_icon_url": "",
        "thumbnail_url": "",
        "image_url": "",
        "image_position": "top",
        "title": "Achadinhos do Dev",
        "description": (
            "Uma curadoria rápida de produtos úteis para setup, programação, estudo, "
            "organização e rotina dev.\n\n"
            "Selecione um produto no menu abaixo para ver detalhes, preço, categoria e link."
        ),
        "footer": "Bella Finds Center",
        "panel_channel_id": None,
        "panel_message_id": None,
        "admin_channel_id": None,
        "admin_message_id": None,
        "manager_role_ids": [],
        "notification_role_id": 0,
        "announced_product_ids": [],
        "fields": [],
        "products": {},
    }


def _unique_product_id(products: dict[str, Any], name: str) -> str:
    base = _clean_id(name)
    candidate = base
    index = 2
    while candidate in products:
        candidate = f"{base}-{index}"[:48]
        index += 1
    return candidate


def admin_embed(settings: dict[str, Any], product_count: int) -> discord.Embed:
    enabled = bool(settings.get("enabled", True))
    render = "Embed V2" if str(settings.get("render_mode", "v1")).lower() == "v2" else "Embed V1"
    channel_id = int(settings.get("panel_channel_id") or 0)
    message_id = int(settings.get("panel_message_id") or 0)
    manager_roles = [
        int(role_id)
        for role_id in (settings.get("manager_role_ids") or [])
        if str(role_id).isdigit()
    ]
    notification_role_id = int(settings.get("notification_role_id") or 0)
    notification_role_text = f"<@&{notification_role_id}>" if notification_role_id else "**não configurado**"
    embed = discord.Embed(
        title="Central de Achadinhos do Dev",
        description=(
            "Painel administrativo para gerenciar a curadoria de produtos da Shopee.\n"
            "Você pode cadastrar produtos, publicar o painel público, pausar a vitrine e alternar o visual."
        ),
        color=discord.Color(_safe_color(settings.get("color"), DEFAULT_COLOR) if enabled else 0xF59E0B),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(
        name="Operação",
        value=(
            f"Estado: **{'Aberto' if enabled else 'Pausado'}**\n"
            f"Visual público: **{render}**\n"
            f"Produtos cadastrados: **{product_count}**\n"
            f"Cor: `#{_safe_color(settings.get('color'), DEFAULT_COLOR):06X}`"
        ),
        inline=True,
    )
    embed.add_field(
        name="Painel público",
        value=(
            f"Canal: {f'<#{channel_id}>' if channel_id else '**não configurado**'}\n"
            f"Mensagem publicada: **{'sim' if message_id else 'não'}**\n"
            f"Gestores: {', '.join(f'<@&{role_id}>' for role_id in manager_roles[:8]) if manager_roles else '**somente administradores**'}\n"
            f"Cargo de avisos: {notification_role_text}"
        ),
        inline=True,
    )
    embed.add_field(
        name="Visual do embed",
        value=(
            f"Autor: **{str(settings.get('author_name') or 'não configurado')[:80]}**\n"
            f"Thumbnail: **{'sim' if settings.get('thumbnail_url') else 'não'}**\n"
            f"Imagem/banner: **{'sim' if settings.get('image_url') else 'não'}**\n"
            f"Mensagem junto: **{'sim' if str(settings.get('message_content') or '').strip() else 'não'}**"
        ),
        inline=False,
    )
    if not enabled:
        embed.add_field(
            name="Mensagem de pausa",
            value=_trim(str(settings.get("closed_reason") or ""), 500),
            inline=False,
        )
    embed.set_footer(text="Bella Shopee Finds • painel administrativo")
    return embed


def public_embed(
    settings: dict[str, Any],
    products: list[ShopeeProduct],
    *,
    include_image: bool = True,
) -> discord.Embed:
    enabled = bool(settings.get("enabled", True))
    embed = discord.Embed(
        title=str(settings.get("title") or "Achadinhos do Dev")[:120],
        description=str(settings.get("description") or _default_settings()["description"])[:3000],
        color=discord.Color(_safe_color(settings.get("color"), DEFAULT_COLOR) if enabled else 0xF59E0B),
        timestamp=datetime.now(timezone.utc),
    )
    author_name = str(settings.get("author_name") or "").strip()
    author_icon_url = str(settings.get("author_icon_url") or "").strip()
    if author_name:
        kwargs: dict[str, Any] = {"name": author_name[:256]}
        if _valid_url(author_icon_url):
            kwargs["icon_url"] = author_icon_url
        embed.set_author(**kwargs)
    thumbnail_url = str(settings.get("thumbnail_url") or "").strip()
    if _valid_url(thumbnail_url):
        embed.set_thumbnail(url=thumbnail_url)
    image_url = str(settings.get("image_url") or "").strip()
    if include_image and _valid_url(image_url):
        embed.set_image(url=image_url)
    for field in _normalize_fields(settings.get("fields")):
        embed.add_field(
            name=str(field.get("name") or "\u200b")[:256],
            value=str(field.get("value") or "-")[:1024],
            inline=bool(field.get("inline", False)),
        )
    categories: dict[str, int] = {}
    for product in products:
        categories[product.category] = categories.get(product.category, 0) + 1
    summary = "\n".join(
        f"**{category}:** {count} produto(s)"
        for category, count in sorted(categories.items())[:8]
    )
    embed.add_field(
        name="Curadoria",
        value=summary or "Nenhum produto cadastrado ainda.",
        inline=False,
    )
    if not enabled:
        embed.add_field(
            name="Vitrine pausada",
            value=str(settings.get("closed_reason") or _default_settings()["closed_reason"])[:1024],
            inline=False,
        )
    role_id = int(settings.get("notification_role_id") or 0)
    if role_id:
        embed.add_field(
            name="Avisos de novos achadinhos",
            value=f"Use o botão abaixo para entrar ou sair do cargo <@&{role_id}>.",
            inline=False,
        )
    embed.set_footer(text=str(settings.get("footer") or "Bella Finds Center")[:120])
    return embed


def public_banner_embed(settings: dict[str, Any]) -> discord.Embed | None:
    image_url = str(settings.get("image_url") or "").strip()
    if not _valid_url(image_url):
        return None
    enabled = bool(settings.get("enabled", True))
    embed = discord.Embed(
        color=discord.Color(_safe_color(settings.get("color"), DEFAULT_COLOR) if enabled else 0xF59E0B),
    )
    embed.set_image(url=image_url)
    return embed


def product_embed(product: ShopeeProduct) -> discord.Embed:
    description = product.description or "Produto selecionado nos Achadinhos do Dev."
    embed_url = product.url if len(product.url) <= 2048 else None
    embed = discord.Embed(
        title=(product.embed_title or product.name)[:256],
        description=_trim(description, 1800),
        color=discord.Color(_safe_color(product.embed_color, DEFAULT_COLOR)),
        timestamp=datetime.now(timezone.utc),
        url=embed_url,
    )
    if product.author_name:
        kwargs: dict[str, Any] = {"name": _trim(product.author_name, 256)}
        if _valid_url(product.author_icon_url):
            kwargs["icon_url"] = product.author_icon_url
        embed.set_author(**kwargs)
    embed.add_field(name="Categoria", value=product.category or "Geral", inline=True)
    if product.price:
        embed.add_field(name="Preço", value=product.price, inline=True)
    if product.coupon:
        embed.add_field(name="Cupom", value=product.coupon, inline=True)
    for field in _normalize_fields(product.fields or []):
        embed.add_field(
            name=str(field.get("name") or "Campo")[:256],
            value=str(field.get("value") or "-")[:1024],
            inline=bool(field.get("inline", False)),
        )
    if _valid_url(product.thumbnail_url):
        embed.set_thumbnail(url=product.thumbnail_url)
    if _valid_url(product.image_url):
        embed.set_image(url=product.image_url)
    embed.set_footer(text=(product.footer_text or f"ID: {product.id}")[:2048])
    return embed


def product_options(products: list[ShopeeProduct]) -> list[discord.SelectOption]:
    if not products:
        return [
            discord.SelectOption(
                label="Nenhum produto cadastrado",
                value="none",
                description="A administração ainda não adicionou achadinhos.",
            )
        ]
    options: list[discord.SelectOption] = []
    for product in products[:MAX_PUBLIC_PRODUCTS]:
        description_parts = [product.category]
        if product.price:
            description_parts.append(product.price)
        options.append(
            discord.SelectOption(
                label=_trim(product.name, 100),
                value=product.id,
                description=_trim(" • ".join(description_parts), 100),
            )
        )
    return options


def _draft_from_public_settings(settings: dict[str, Any]) -> EmbedDraft:
    return EmbedDraft(
        title=str(settings.get("title") or "Achadinhos do Dev"),
        description=str(settings.get("description") or _default_settings()["description"]),
        color=_safe_color(settings.get("color"), DEFAULT_COLOR),
        author=str(settings.get("author_name") or ""),
        author_icon_url=str(settings.get("author_icon_url") or ""),
        footer=str(settings.get("footer") or "Bella Finds Center"),
        thumbnail_url=str(settings.get("thumbnail_url") or ""),
        image_url=str(settings.get("image_url") or ""),
        image_position=str(settings.get("image_position") or "top"),
        fields=[
            DraftField(
                name=str(field.get("name") or ""),
                value=str(field.get("value") or ""),
                inline=bool(field.get("inline", False)),
            )
            for field in _normalize_fields(settings.get("fields"))
        ][:25],
    )


def _v2_session_from_public_settings(
    interaction: discord.Interaction,
    service: ShopeeFindsService,
    settings: dict[str, Any],
) -> V2EditorSession:
    async def save_callback(
        save_interaction: discord.Interaction,
        session: V2EditorSession,
    ) -> str:
        if save_interaction.guild is None:
            return "Use dentro de um servidor."
        container = session.container
        image_url = ""
        footer = ""
        fields: list[dict[str, Any]] = []
        for item in container.items:
            if item.kind == "media" and item.url and not image_url:
                image_url = item.url
            elif item.kind == "text" and item.content:
                content = item.content.strip()
                if content.startswith("-#"):
                    footer = content.replace("-#", "", 1).strip()
                    continue
                if content.startswith("### "):
                    name, _, value = content[4:].partition("\n")
                    fields.append({"name": name.strip(), "value": value.strip() or "-", "inline": False})
                elif content != container.description:
                    fields.append({"name": "", "value": content[:1024], "inline": False})
        await service.save_settings(
            save_interaction.guild.id,
            {
                "message_content": session.message_content.strip()[:1800],
                "title": container.title.strip()[:120] or "Achadinhos do Dev",
                "description": container.description.strip()[:2000] or _default_settings()["description"],
                "color": int(container.color),
                "thumbnail_url": container.thumbnail_url.strip()[:500],
                "image_url": image_url.strip()[:500],
                "image_position": _v2_media_position(container.items),
                "footer": footer.strip()[:120] or "Bella Finds Center",
                "fields": _normalize_fields(fields),
            },
        )
        await refresh_public_panel_from_interaction(service, save_interaction)
        return "Container V2 dos achadinhos salvo. O painel público também foi atualizado."

    session = V2EditorSession(
        interaction.user.id,
        interaction.channel or interaction,
        save_callback=save_callback,
    )
    draft = _draft_from_public_settings(settings)
    container = V2ContainerDraft(
        title=draft.title,
        description=draft.description,
        color=draft.color,
        thumbnail_url=draft.thumbnail_url,
        items=[],
    )
    if draft.image_url and draft.image_position == "top":
        container.items.append(V2ItemDraft(kind="media", url=draft.image_url))
    for field in draft.fields[:12]:
        container.items.append(V2ItemDraft(kind="text", content=f"### {field.name}\n{field.value}".strip()))
        container.items.append(V2ItemDraft(kind="separator"))
    if draft.image_url and draft.image_position != "top":
        container.items.append(V2ItemDraft(kind="media", url=draft.image_url))
    if draft.footer:
        container.items.append(V2ItemDraft(kind="text", content=f"-# {draft.footer}"))
    session.containers = [container]
    session.message_content = str(settings.get("message_content") or "")
    return session


def _product_draft_from_seed(seed: ShopeeProductDraftSeed) -> EmbedDraft:
    description_parts = [
        "Produto selecionado para a curadoria dos Achadinhos do Dev.",
        "Use o editor abaixo para personalizar descrição, imagem, campos e botões antes de salvar.",
    ]
    if seed.price:
        description_parts.append(f"Preço/observação: {seed.price}")
    draft = EmbedDraft(
        title=seed.name,
        description="\n\n".join(description_parts),
        color=DEFAULT_COLOR,
        footer="Achadinhos do Dev",
        fields=[],
        buttons=[DraftButton(label="Ver produto", url=seed.url)],
    )
    if seed.coupon:
        draft.fields.append(DraftField(name="Cupom", value=seed.coupon, inline=True))
    return draft


def _product_v2_session_from_seed(
    interaction: discord.Interaction,
    service: ShopeeFindsService,
    seed: ShopeeProductDraftSeed,
) -> V2EditorSession:
    saved_product_id: str | None = None

    async def save_callback(
        save_interaction: discord.Interaction,
        session: V2EditorSession,
    ) -> str:
        nonlocal saved_product_id
        if save_interaction.guild is None:
            return "Use dentro de um servidor."
        if saved_product_id:
            return f"Esse produto já foi salvo com o ID `{saved_product_id}`."
        product = await _create_product_from_v2_session(
            service,
            save_interaction.guild.id,
            save_interaction.user.id,
            seed,
            session,
        )
        saved_product_id = product.id
        await refresh_public_panel_from_interaction(service, save_interaction)
        await refresh_admin_panel_from_interaction(service, save_interaction)
        return f"Produto `{product.name}` salvo nos achadinhos com o ID `{product.id}`."

    session = V2EditorSession(
        interaction.user.id,
        interaction.channel or interaction,
        save_callback=save_callback,
    )
    description = "Produto selecionado para a curadoria dos Achadinhos do Dev."
    if seed.price:
        description += f"\n\nPreço/observação: {seed.price}"
    items: list[V2ItemDraft] = [
        V2ItemDraft(
            kind="text",
            content="Personalize este container antes de salvar. Você pode adicionar imagem, campos, separadores e botões.",
        )
    ]
    if seed.coupon:
        items.extend(
            [
                V2ItemDraft(kind="separator"),
                V2ItemDraft(kind="text", content=f"### Cupom\n{seed.coupon}"),
            ]
        )
    if len(seed.url) <= MAX_DISCORD_BUTTON_URL_LENGTH:
        items.extend(
            [
                V2ItemDraft(kind="separator"),
                V2ItemDraft(kind="link_button", label="Ver produto", url=seed.url),
            ]
        )
    else:
        items.extend(
            [
                V2ItemDraft(kind="separator"),
                V2ItemDraft(
                    kind="text",
                    content=(
                        "O link deste produto é grande demais para um botão direto do Discord. "
                        "Ao salvar, a Bella cria um botão seguro que entrega o link completo ao usuário."
                    ),
                ),
            ]
        )
    session.containers = [
        V2ContainerDraft(
            title=seed.name,
            description=description,
            color=DEFAULT_COLOR,
            items=items,
        )
    ]
    return session


def _draft_from_product(product: ShopeeProduct) -> EmbedDraft:
    buttons = [
        DraftButton(label=button["label"], url=button["url"])
        for button in _normalize_buttons(product.buttons or [{"label": "Ver produto", "url": product.url}])
    ]
    return EmbedDraft(
        title=product.embed_title or product.name,
        description=product.description or "Produto selecionado nos Achadinhos do Dev.",
        color=_safe_color(product.embed_color, DEFAULT_COLOR),
        author=product.author_name,
        author_icon_url=product.author_icon_url,
        footer=product.footer_text or f"ID: {product.id}",
        thumbnail_url=product.thumbnail_url,
        image_url=product.image_url,
        fields=[
            DraftField(
                name=str(field.get("name") or ""),
                value=str(field.get("value") or ""),
                inline=bool(field.get("inline", False)),
            )
            for field in _normalize_fields(product.fields or [])
        ],
        buttons=buttons[:5],
    )


async def _create_product_from_embed_session(
    service: ShopeeFindsService,
    guild_id: int,
    user_id: int,
    seed: ShopeeProductDraftSeed,
    session: EmbedEditorSession,
) -> ShopeeProduct:
    draft = session.draft
    buttons = [
        {"label": button.label, "url": button.url}
        for button in draft.buttons[:5]
        if button.label.strip() and _valid_product_url(button.url)
    ]
    return await service.add_product(
        guild_id,
        name=seed.name,
        url=seed.url,
        price=seed.price,
        category=seed.category,
        description=draft.description,
        image_url=draft.image_url,
        coupon=seed.coupon,
        embed_title=draft.title,
        embed_color=int(draft.color),
        author_name=draft.author,
        author_icon_url=draft.author_icon_url,
        thumbnail_url=draft.thumbnail_url,
        footer_text=draft.footer,
        buttons=buttons or [{"label": "Ver produto", "url": seed.url}],
        fields=[field.to_dict() for field in draft.fields[:25]],
        created_by=user_id,
    )


async def _update_product_from_embed_session(
    service: ShopeeFindsService,
    guild_id: int,
    product: ShopeeProduct,
    session: EmbedEditorSession,
) -> ShopeeProduct | None:
    draft = session.draft
    buttons = [
        {"label": button.label, "url": button.url}
        for button in draft.buttons[:5]
        if button.label.strip() and _valid_product_url(button.url)
    ]
    return await service.update_product(
        guild_id,
        product.id,
        description=draft.description,
        image_url=draft.image_url,
        embed_title=draft.title,
        embed_color=int(draft.color),
        author_name=draft.author,
        author_icon_url=draft.author_icon_url,
        thumbnail_url=draft.thumbnail_url,
        footer_text=draft.footer,
        buttons=buttons or [{"label": "Ver produto", "url": product.url}],
        fields=[field.to_dict() for field in draft.fields[:25]],
    )


async def _create_product_from_v2_session(
    service: ShopeeFindsService,
    guild_id: int,
    user_id: int,
    seed: ShopeeProductDraftSeed,
    session: V2EditorSession,
) -> ShopeeProduct:
    container = session.container
    image_url = ""
    footer_text = ""
    fields: list[dict[str, Any]] = []
    buttons: list[dict[str, str]] = []
    for item in container.items:
        if item.kind == "media" and item.url and not image_url:
            image_url = item.url
            continue
        if item.kind == "link_button" and item.label and _valid_product_url(item.url):
            buttons.append({"label": item.label[:80], "url": item.url[:MAX_PRODUCT_URL_LENGTH]})
            continue
        if item.kind != "text" or not item.content.strip():
            continue
        content = item.content.strip()
        if content.startswith("-#"):
            footer_text = content.replace("-#", "", 1).strip()
            continue
        if content.startswith("Link grande salvo no produto:"):
            continue
        if content.startswith("### "):
            name, _, value = content[4:].partition("\n")
            fields.append(
                {
                    "name": name.strip()[:256],
                    "value": value.strip()[:1024] or "-",
                    "inline": False,
                }
            )
            continue
        fields.append({"name": "", "value": content[:1024], "inline": False})
    return await service.add_product(
        guild_id,
        name=seed.name,
        url=seed.url,
        price=seed.price,
        category=seed.category,
        description=container.description,
        image_url=image_url,
        coupon=seed.coupon,
        embed_title=container.title,
        embed_color=int(container.color),
        thumbnail_url=container.thumbnail_url,
        footer_text=footer_text or "Achadinhos do Dev",
        buttons=buttons or [{"label": "Ver produto", "url": seed.url}],
        fields=_normalize_fields(fields),
        created_by=user_id,
    )


async def _update_product_from_v2_session(
    service: ShopeeFindsService,
    guild_id: int,
    product: ShopeeProduct,
    session: V2EditorSession,
) -> ShopeeProduct | None:
    container = session.container
    image_url = ""
    footer_text = ""
    fields: list[dict[str, Any]] = []
    buttons: list[dict[str, str]] = []
    for item in container.items:
        if item.kind == "media" and item.url and not image_url:
            image_url = item.url
            continue
        if item.kind == "link_button" and item.label and _valid_product_url(item.url):
            buttons.append({"label": item.label[:80], "url": item.url[:MAX_PRODUCT_URL_LENGTH]})
            continue
        if item.kind != "text" or not item.content.strip():
            continue
        content = item.content.strip()
        if content.startswith("-#"):
            footer_text = content.replace("-#", "", 1).strip()
            continue
        if content.startswith("Link grande salvo no produto:"):
            continue
        if content.startswith("### "):
            name, _, value = content[4:].partition("\n")
            fields.append(
                {
                    "name": name.strip()[:256],
                    "value": value.strip()[:1024] or "-",
                    "inline": False,
                }
            )
            continue
        fields.append({"name": "", "value": content[:1024], "inline": False})
    return await service.update_product(
        guild_id,
        product.id,
        description=container.description,
        image_url=image_url,
        embed_title=container.title,
        embed_color=int(container.color),
        thumbnail_url=container.thumbnail_url,
        footer_text=footer_text or product.footer_text or "Achadinhos do Dev",
        buttons=buttons or [{"label": "Ver produto", "url": product.url}],
        fields=_normalize_fields(fields),
    )


def _v2_session_from_product(
    interaction: discord.Interaction,
    service: ShopeeFindsService,
    product: ShopeeProduct,
) -> V2EditorSession:
    async def save_callback(
        save_interaction: discord.Interaction,
        session: V2EditorSession,
    ) -> str:
        if save_interaction.guild is None:
            return "Use dentro de um servidor."
        updated = await _update_product_from_v2_session(
            service,
            save_interaction.guild.id,
            product,
            session,
        )
        if updated is None:
            return "Produto não encontrado."
        await refresh_public_panel_from_interaction(service, save_interaction)
        await refresh_admin_panel_from_interaction(service, save_interaction)
        return f"Produto `{updated.name}` atualizado pelo editor V2."

    session = V2EditorSession(
        interaction.user.id,
        interaction.channel or interaction,
        save_callback=save_callback,
    )
    items: list[V2ItemDraft] = []
    for field in _normalize_fields(product.fields or []):
        items.append(V2ItemDraft(kind="text", content=f"### {field['name']}\n{field['value']}".strip()))
        items.append(V2ItemDraft(kind="separator"))
    if product.image_url:
        items.append(V2ItemDraft(kind="media", url=product.image_url))
        items.append(V2ItemDraft(kind="separator"))
    buttons = _normalize_buttons(product.buttons or [{"label": "Ver produto", "url": product.url}])
    for button in buttons[:5]:
        if len(button["url"]) <= MAX_DISCORD_BUTTON_URL_LENGTH:
            items.append(V2ItemDraft(kind="link_button", label=button["label"], url=button["url"]))
        else:
            items.append(
                V2ItemDraft(
                    kind="text",
                    content=(
                        f"Link grande salvo no produto: **{button['label']}**.\n"
                        "A Bella usa um botão seguro no painel público para entregar o link completo."
                    ),
                )
            )
    if product.footer_text:
        items.append(V2ItemDraft(kind="text", content=f"-# {product.footer_text}"))
    session.containers = [
        V2ContainerDraft(
            title=product.embed_title or product.name,
            description=product.description or "Produto selecionado nos Achadinhos do Dev.",
            color=_safe_color(product.embed_color, DEFAULT_COLOR),
            thumbnail_url=product.thumbnail_url,
            items=items[:20],
        )
    ]
    return session


def _product_action_buttons(product: ShopeeProduct) -> list[discord.ui.Button]:
    result: list[discord.ui.Button] = []
    buttons = _normalize_buttons(product.buttons or [{"label": "Abrir na Shopee", "url": product.url}])
    if not buttons and _valid_product_url(product.url):
        buttons = [{"label": "Abrir na Shopee", "url": product.url}]
    for index, button in enumerate(buttons[:5]):
        label = button["label"]
        url = button["url"]
        if len(url) <= MAX_DISCORD_BUTTON_URL_LENGTH:
            result.append(
                discord.ui.Button(
                    label=label,
                    style=discord.ButtonStyle.link,
                    url=url,
                )
            )
            continue
        action_button = discord.ui.Button(
            label=label,
            style=discord.ButtonStyle.primary,
            custom_id=f"jarvis:shopee-finds:long-url:{product.id}:{index}",
        )

        async def send_full_link(
            interaction: discord.Interaction,
            *,
            selected_label: str = label,
            selected_url: str = url,
        ) -> None:
            message = f"Link completo de **{selected_label}**:\n{selected_url}"
            await interaction.response.send_message(
                message[:2000],
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )

        action_button.callback = send_full_link  # type: ignore[method-assign]
        result.append(action_button)
    return result


class ShopeeProductActionView(discord.ui.View):
    def __init__(self, product: ShopeeProduct) -> None:
        super().__init__(timeout=300)
        for button in _product_action_buttons(product):
            self.add_item(button)


class ShopeeProductDetailV2View(discord.ui.LayoutView):
    def __init__(self, product: ShopeeProduct) -> None:
        super().__init__(timeout=300)
        container = discord.ui.Container(
            accent_color=discord.Color(_safe_color(product.embed_color, DEFAULT_COLOR)),
        )

        title = (product.embed_title or product.name).strip()[:256]
        description = (product.description or "Produto selecionado nos Achadinhos do Dev.").strip()[:3000]
        header_parts = []
        if product.author_name:
            header_parts.append(f"-# {product.author_name[:180]}")
        header_parts.append(f"## {title}")
        if description:
            header_parts.append(description)
        header_text = "\n".join(header_parts)
        if _valid_url(product.thumbnail_url):
            container.add_item(
                discord.ui.Section(
                    header_text,
                    accessory=discord.ui.Thumbnail(product.thumbnail_url),
                )
            )
        else:
            container.add_item(discord.ui.TextDisplay(header_text))

        details = [
            "### Informações do achadinho",
            f"**Categoria:** {product.category or 'Geral'}",
        ]
        if product.price:
            details.append(f"**Preço informado:** {product.price}")
        else:
            details.append("**Preço informado:** consulte na página do produto")
        if product.coupon:
            details.append(f"**Cupom:** `{product.coupon}`")
        details.extend(
            [
                f"**Adicionado à vitrine:** {_discord_date(product.created_at)}",
                "**Disponibilidade:** preço, estoque, frete e prazo podem mudar na Shopee.",
            ]
        )
        container.add_item(discord.ui.TextDisplay("\n".join(details)))

        fields = _normalize_fields(product.fields or [])
        if fields:
            blocks = []
            for field in fields[:12]:
                name = str(field.get("name") or "Informação").strip()[:120]
                value = str(field.get("value") or "-").strip()[:700]
                blocks.append(f"### {name}\n{value}")
            container.add_item(discord.ui.TextDisplay("\n\n".join(blocks)[:3900]))

        if _valid_url(product.image_url):
            container.add_item(
                discord.ui.MediaGallery(
                    discord.MediaGalleryItem(product.image_url)
                )
            )

        container.add_item(
            discord.ui.TextDisplay(
                "### Antes de comprar\n"
                "Confira a reputação do vendedor, as avaliações recentes, a variação escolhida, "
                "o valor do frete e o prazo de entrega. A Bella apenas organiza a curadoria e "
                "não processa pagamentos nem garante estoque ou alterações de preço."
            )
        )
        footer = (product.footer_text or "Achadinhos do Dev • Curadoria Bella").strip()
        container.add_item(
            discord.ui.TextDisplay(
                f"-# {footer[:420]} • ID: {product.id}"
            )
        )
        self.add_item(container)

        action_buttons = _product_action_buttons(product)
        if action_buttons:
            action_row = discord.ui.ActionRow()
            for button in action_buttons:
                action_row.add_item(button)
            self.add_item(action_row)


class ShopeeProductSelect(discord.ui.Select):
    def __init__(
        self,
        service: ShopeeFindsService,
        products: list[ShopeeProduct],
        *,
        disabled: bool = False,
    ) -> None:
        self.service = service
        super().__init__(
            placeholder="Selecione um achadinho para ver detalhes",
            min_values=1,
            max_values=1,
            options=product_options(products),
            custom_id="jarvis:shopee-finds:public-select",
            disabled=disabled or not products,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Use dentro de um servidor.", ephemeral=True)
            return
        selected = self.values[0] if self.values else ""
        if selected == "none":
            await interaction.response.send_message("Ainda não tem produto cadastrado por aqui.", ephemeral=True)
            return
        product = await self.service.product(interaction.guild.id, selected)
        if product is None:
            await interaction.response.send_message("Esse produto não está mais disponível.", ephemeral=True)
            return
        await interaction.response.send_message(
            view=ShopeeProductDetailV2View(product),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )


class ShopeeNotificationRoleButton(discord.ui.Button):
    def __init__(self, service: ShopeeFindsService, *, disabled: bool = False) -> None:
        super().__init__(
            label="Receber avisos",
            style=discord.ButtonStyle.primary,
            custom_id="jarvis:shopee-finds:notification-role",
            disabled=disabled,
        )
        self.service = service

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Use dentro de um servidor.", ephemeral=True)
            return
        settings = await self.service.settings_for(interaction.guild.id)
        role_id = int(settings.get("notification_role_id") or 0)
        role = interaction.guild.get_role(role_id)
        if role is None:
            await interaction.response.send_message("O cargo de avisos ainda não foi configurado.", ephemeral=True)
            return
        me = interaction.guild.me
        if me is None or not interaction.channel.permissions_for(me).manage_roles:
            await interaction.response.send_message("Não consigo gerenciar cargos neste servidor.", ephemeral=True)
            return
        if role >= me.top_role:
            await interaction.response.send_message("Meu cargo precisa ficar acima do cargo de avisos para eu conseguir entregar/remover.", ephemeral=True)
            return
        try:
            if role in interaction.user.roles:
                await interaction.user.remove_roles(role, reason="Usuário saiu dos avisos de achadinhos.")
                await interaction.response.send_message("Pronto. Tirei você dos avisos de novos achadinhos.", ephemeral=True)
            else:
                await interaction.user.add_roles(role, reason="Usuário entrou nos avisos de achadinhos.")
                await interaction.response.send_message("Boa. Vou te avisar quando tiver achadinho novo.", ephemeral=True)
        except discord.HTTPException:
            await interaction.response.send_message("Não consegui alterar seu cargo agora. Verifique as permissões da Bella.", ephemeral=True)


class ShopeeFindsPublicView(discord.ui.View):
    def __init__(
        self,
        service: ShopeeFindsService,
        products: list[ShopeeProduct] | None = None,
        *,
        disabled: bool = False,
    ) -> None:
        super().__init__(timeout=None)
        self.add_item(ShopeeProductSelect(service, products or [], disabled=disabled))
        self.add_item(ShopeeNotificationRoleButton(service, disabled=disabled))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return not await guard_public_interaction(interaction, "achadinhos")


class ShopeeFindsPublicV2View(discord.ui.LayoutView):
    def __init__(
        self,
        service: ShopeeFindsService,
        settings: dict[str, Any],
        products: list[ShopeeProduct],
    ) -> None:
        super().__init__(timeout=None)
        enabled = bool(settings.get("enabled", True))
        container = discord.ui.Container(
            accent_color=discord.Color(_safe_color(settings.get("color"), DEFAULT_COLOR) if enabled else 0xF59E0B),
        )

        author_name = str(settings.get("author_name") or "").strip()
        title = str(settings.get("title") or "Achadinhos do Dev").strip()[:120]
        description = str(settings.get("description") or _default_settings()["description"]).strip()[:3000]
        image_url = str(settings.get("image_url") or "").strip()
        image_position = _image_position(settings.get("image_position", "top"))
        if image_position == "top" and _valid_url(image_url):
            container.add_item(
                discord.ui.MediaGallery(
                    discord.MediaGalleryItem(
                        image_url,
                    )
                )
            )
            container.add_item(discord.ui.Separator(spacing=discord.SeparatorSpacing.small))
        header_parts = []
        if author_name:
            header_parts.append(f"-# {author_name[:180]}")
        header_parts.append(f"## {title}")
        if description:
            header_parts.append(description)
        thumbnail_url = str(settings.get("thumbnail_url") or "").strip()
        header_text = "\n".join(header_parts)
        if _valid_url(thumbnail_url):
            container.add_item(
                discord.ui.Section(
                    header_text,
                    accessory=discord.ui.Thumbnail(thumbnail_url),
                )
            )
        else:
            container.add_item(discord.ui.TextDisplay(header_text))
        if image_position == "middle" and _valid_url(image_url):
            container.add_item(discord.ui.Separator(spacing=discord.SeparatorSpacing.small))
            container.add_item(
                discord.ui.MediaGallery(
                    discord.MediaGalleryItem(
                        image_url,
                    )
                )
            )
        fields = _normalize_fields(settings.get("fields"))
        if fields:
            blocks = []
            for field in fields[:12]:
                name = str(field.get("name") or "").strip()[:120]
                value = str(field.get("value") or "-").strip()[:700]
                if name and not name.casefold().startswith("informa"):
                    blocks.append(f"### {name}\n{value or '-'}")
                else:
                    blocks.append(value or "-")
            container.add_item(discord.ui.Separator(spacing=discord.SeparatorSpacing.small))
            container.add_item(discord.ui.TextDisplay("\n\n".join(blocks)[:3900]))
        if not enabled:
            container.add_item(discord.ui.Separator(spacing=discord.SeparatorSpacing.small))
            container.add_item(
                discord.ui.TextDisplay(
                    "### Vitrine pausada\n"
                    f"{str(settings.get('closed_reason') or _default_settings()['closed_reason'])[:900]}"
                )
            )
        elif products:
            preview = "\n".join(
                f"- **{product.name}** — {product.category}{f' • {product.price}' if product.price else ''}"
                for product in products[:8]
            )
            container.add_item(discord.ui.Separator(spacing=discord.SeparatorSpacing.small))
            container.add_item(discord.ui.TextDisplay(f"### Produtos em destaque\n{preview}"))
        else:
            container.add_item(discord.ui.TextDisplay("Nenhum produto cadastrado ainda."))
        footer = str(settings.get("footer") or "Bella Shopee Finds • selecione um produto para abrir detalhes só para você").strip()
        if footer:
            container.add_item(discord.ui.TextDisplay(f"-# {footer[:600]}"))
        if image_position == "end" and _valid_url(image_url):
            container.add_item(discord.ui.Separator(spacing=discord.SeparatorSpacing.small))
            container.add_item(
                discord.ui.MediaGallery(
                    discord.MediaGalleryItem(
                        image_url,
                    )
                )
            )
        self.add_item(container)

        product_row = discord.ui.ActionRow()
        product_row.add_item(ShopeeProductSelect(service, products, disabled=not enabled))
        self.add_item(product_row)
        if int(settings.get("notification_role_id") or 0):
            notify_row = discord.ui.ActionRow()
            notify_row.add_item(ShopeeNotificationRoleButton(service, disabled=not enabled))
            self.add_item(notify_row)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return not await guard_public_interaction(interaction, "achadinhos")


def public_message_kwargs(
    service: ShopeeFindsService,
    settings: dict[str, Any],
    products: list[ShopeeProduct],
) -> dict[str, Any]:
    enabled = bool(settings.get("enabled", True))
    content = str(settings.get("message_content") or "").strip()[:1800] or None
    if str(settings.get("render_mode", "v1")).lower() == "v2":
        return {
            "content": content,
            "embeds": [],
            "view": ShopeeFindsPublicV2View(service, settings, products),
            "allowed_mentions": discord.AllowedMentions.none(),
        }
    image_position = _image_position(settings.get("image_position", "top"))
    embeds = [public_embed(settings, products, include_image=image_position == "end")]
    banner = public_banner_embed(settings)
    if banner is not None and image_position == "top":
        embeds.insert(0, banner)
    elif banner is not None and image_position == "middle":
        embeds.append(banner)
    return {
        "content": content,
        "embeds": embeds[:10],
        "view": ShopeeFindsPublicView(service, products, disabled=not enabled),
        "allowed_mentions": discord.AllowedMentions.none(),
    }


class ShopeeFindsConfigModal(discord.ui.Modal, title="Configurar Achadinhos"):
    title_input = discord.ui.TextInput(
        label="Título do painel público",
        max_length=120,
        required=True,
    )
    description_input = discord.ui.TextInput(
        label="Descrição",
        style=discord.TextStyle.paragraph,
        max_length=2000,
        required=True,
    )
    message_content_input = discord.ui.TextInput(
        label="Mensagem junto com o painel",
        style=discord.TextStyle.paragraph,
        max_length=1800,
        required=False,
    )
    footer_input = discord.ui.TextInput(
        label="Rodapé",
        max_length=120,
        required=False,
    )
    closed_reason_input = discord.ui.TextInput(
        label="Mensagem quando estiver pausado",
        style=discord.TextStyle.paragraph,
        max_length=500,
        required=True,
    )

    def __init__(self, service: ShopeeFindsService, settings: dict[str, Any]) -> None:
        super().__init__(timeout=None)
        self.service = service
        self.title_input.default = str(settings.get("title") or "Achadinhos do Dev")
        self.description_input.default = str(settings.get("description") or _default_settings()["description"])
        self.message_content_input.default = str(settings.get("message_content") or "")
        self.footer_input.default = str(settings.get("footer") or "Bella Finds Center")
        self.closed_reason_input.default = str(settings.get("closed_reason") or _default_settings()["closed_reason"])

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Use dentro de um servidor.", ephemeral=True)
            return
        settings = await self.service.update_text(
            interaction.guild.id,
            message_content=str(self.message_content_input),
            title=str(self.title_input),
            description=str(self.description_input),
            footer=str(self.footer_input),
            closed_reason=str(self.closed_reason_input),
        )
        products = await self.service.products(interaction.guild.id)
        await interaction.response.edit_message(
            embed=admin_embed(settings, len(products)),
            view=ShopeeFindsAdminView(self.service),
        )


class ShopeeFindsVisualModal(discord.ui.Modal, title="Visual do embed"):
    color_input = discord.ui.TextInput(
        label="Cor hexadecimal",
        placeholder="#F97316",
        max_length=20,
        required=False,
    )
    author_name_input = discord.ui.TextInput(
        label="Autor do embed",
        max_length=120,
        required=False,
    )
    author_icon_input = discord.ui.TextInput(
        label="URL do ícone do autor",
        max_length=500,
        required=False,
    )
    thumbnail_input = discord.ui.TextInput(
        label="URL da thumbnail",
        max_length=500,
        required=False,
    )
    image_input = discord.ui.TextInput(
        label="URL da imagem/banner",
        max_length=500,
        required=False,
    )

    def __init__(self, service: ShopeeFindsService, settings: dict[str, Any]) -> None:
        super().__init__(timeout=None)
        self.service = service
        self.color_input.default = f"#{_safe_color(settings.get('color'), DEFAULT_COLOR):06X}"
        self.author_name_input.default = str(settings.get("author_name") or "")
        self.author_icon_input.default = str(settings.get("author_icon_url") or "")
        self.thumbnail_input.default = str(settings.get("thumbnail_url") or "")
        self.image_input.default = str(settings.get("image_url") or "")

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Use dentro de um servidor.", ephemeral=True)
            return
        try:
            settings = await self.service.update_visual(
                interaction.guild.id,
                color=str(self.color_input),
                author_name=str(self.author_name_input),
                author_icon_url=str(self.author_icon_input),
                thumbnail_url=str(self.thumbnail_input),
                image_url=str(self.image_input),
            )
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        products = await self.service.products(interaction.guild.id)
        await interaction.response.edit_message(
            embed=admin_embed(settings, len(products)),
            view=ShopeeFindsAdminView(self.service),
        )


class ShopeeAddProductModal(discord.ui.Modal, title="Adicionar produto"):
    name_input = discord.ui.TextInput(label="Nome do produto", max_length=100)
    price_input = discord.ui.TextInput(label="Preço ou observação", max_length=80, required=False)
    url_input = discord.ui.TextInput(
        label="Link da Shopee",
        style=discord.TextStyle.paragraph,
        max_length=MAX_PRODUCT_URL_LENGTH,
    )
    category_input = discord.ui.TextInput(label="Categoria", max_length=80, default="Geral", required=False)
    details_input = discord.ui.TextInput(
        label="Descrição, imagem, campos e botões",
        style=discord.TextStyle.paragraph,
        placeholder=(
            "Descrição do produto.\n"
            "Imagem: https://...\n"
            "Thumbnail: https://...\n"
            "Cupom: DEV10\n"
            "Campo: Benefício|Entrega rápida|sim\n"
            "Botão: Comprar agora|https://..."
        ),
        max_length=1900,
        required=False,
    )

    def __init__(self, service: ShopeeFindsService) -> None:
        super().__init__(timeout=None)
        self.service = service

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Use dentro de um servidor.", ephemeral=True)
            return
        details = _parse_extra_details(str(self.details_input))
        try:
            product = await self.service.add_product(
                interaction.guild.id,
                name=str(self.name_input),
                url=str(self.url_input),
                price=str(self.price_input),
                category=str(self.category_input),
                description=str(details.get("description") or ""),
                image_url=str(details.get("image_url") or ""),
                thumbnail_url=str(details.get("thumbnail_url") or ""),
                coupon=str(details.get("coupon") or ""),
                footer_text=str(details.get("footer_text") or ""),
                buttons=details.get("buttons") or None,
                fields=details.get("fields") or None,
                created_by=interaction.user.id,
            )
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        settings = await self.service.settings_for(interaction.guild.id)
        products = await self.service.products(interaction.guild.id)
        await interaction.response.edit_message(
            embed=admin_embed(settings, len(products)),
            view=ShopeeFindsAdminView(self.service),
        )
        try:
            await interaction.followup.send(
                f"Produto `{product.name}` adicionado aos achadinhos.",
                ephemeral=True,
            )
        except discord.HTTPException:
            pass


class ShopeeNewProductBasicModal(discord.ui.Modal):
    def __init__(self, service: ShopeeFindsService, render_mode: str) -> None:
        title = "Novo produto com Embed V2" if render_mode == "v2" else "Novo produto com Embed V1"
        super().__init__(title=title, timeout=None)
        self.service = service
        self.render_mode = "v2" if render_mode == "v2" else "v1"
        self.name_input = discord.ui.TextInput(label="Nome do produto", max_length=100)
        self.url_input = discord.ui.TextInput(
            label="Link completo do produto",
            style=discord.TextStyle.paragraph,
            max_length=MAX_PRODUCT_URL_LENGTH,
            placeholder="Cole o link completo da Shopee. Links grandes são aceitos.",
        )
        self.price_input = discord.ui.TextInput(label="Preço ou observação", max_length=80, required=False)
        self.category_input = discord.ui.TextInput(label="Categoria", max_length=80, default="Geral", required=False)
        self.coupon_input = discord.ui.TextInput(label="Cupom opcional", max_length=80, required=False)
        for item in (
            self.name_input,
            self.url_input,
            self.price_input,
            self.category_input,
            self.coupon_input,
        ):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Use dentro de um servidor.", ephemeral=True)
            return
        url = str(self.url_input).strip()
        if not _valid_product_url(url):
            await interaction.response.send_message(
                f"Informe um link válido começando com http:// ou https:// com até {MAX_PRODUCT_URL_LENGTH} caracteres.",
                ephemeral=True,
            )
            return
        seed = ShopeeProductDraftSeed(
            name=str(self.name_input).strip()[:100] or "Produto",
            url=url,
            price=str(self.price_input).strip()[:80],
            category=str(self.category_input).strip()[:80] or "Geral",
            coupon=str(self.coupon_input).strip()[:80],
            render_mode=self.render_mode,
        )
        if self.render_mode == "v2":
            session = _product_v2_session_from_seed(interaction, self.service, seed)
            await interaction.response.send_message(view=session.editor_view(), ephemeral=True)
            return

        saved_product_id: str | None = None

        async def save_callback(
            save_interaction: discord.Interaction,
            session: EmbedEditorSession,
        ) -> str:
            nonlocal saved_product_id
            if save_interaction.guild is None:
                return "Use dentro de um servidor."
            if saved_product_id:
                return f"Esse produto já foi salvo com o ID `{saved_product_id}`."
            product = await _create_product_from_embed_session(
                self.service,
                save_interaction.guild.id,
                save_interaction.user.id,
                seed,
                session,
            )
            saved_product_id = product.id
            await refresh_public_panel_from_interaction(self.service, save_interaction)
            await refresh_admin_panel_from_interaction(self.service, save_interaction)
            return f"Produto `{product.name}` salvo nos achadinhos com o ID `{product.id}`."

        session = EmbedEditorSession(
            interaction.user.id,
            interaction.channel or interaction,
            draft=_product_draft_from_seed(seed),
            save_callback=save_callback,
        )
        await interaction.response.send_message(
            content="Pré-visualização do novo produto. Personalize e clique em Enviar Rápido para salvar nos achadinhos.",
            embeds=session.editor_embeds(),
            view=session.preview_view(),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )


class ShopeeAddProductModeView(discord.ui.View):
    def __init__(self, service: ShopeeFindsService) -> None:
        super().__init__(timeout=180)
        self.service = service

    @discord.ui.button(label="Criar produto V1", style=discord.ButtonStyle.primary)
    async def create_v1(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(ShopeeNewProductBasicModal(self.service, "v1"))

    @discord.ui.button(label="Criar produto V2", style=discord.ButtonStyle.primary)
    async def create_v2(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(ShopeeNewProductBasicModal(self.service, "v2"))

    @discord.ui.button(label="Cadastro rápido", style=discord.ButtonStyle.secondary)
    async def quick_create(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(ShopeeAddProductModal(self.service))


class ShopeeRemoveSelect(discord.ui.Select):
    def __init__(self, service: ShopeeFindsService, products: list[ShopeeProduct]) -> None:
        self.service = service
        super().__init__(
            placeholder="Escolha o produto que deseja remover",
            min_values=1,
            max_values=1,
            options=product_options(products),
            custom_id="jarvis:shopee-finds:admin-remove-select",
            disabled=not products,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Use dentro de um servidor.", ephemeral=True)
            return
        removed = await self.service.remove_product(interaction.guild.id, self.values[0])
        await interaction.response.send_message(
            f"Removi `{removed.name}` dos achadinhos." if removed else "Esse produto já não existia mais.",
            ephemeral=True,
        )


class ShopeeRemoveView(discord.ui.View):
    def __init__(self, service: ShopeeFindsService, products: list[ShopeeProduct]) -> None:
        super().__init__(timeout=180)
        self.add_item(ShopeeRemoveSelect(service, products))


async def send_or_edit_public_panel(
    service: ShopeeFindsService,
    guild_id: int,
    channel: discord.TextChannel,
    message: discord.Message | None = None,
) -> discord.Message:
    settings = await service.settings_for(guild_id)
    products = await service.products(guild_id)
    kwargs = public_message_kwargs(service, settings, products)
    try:
        if message is None:
            sent = await channel.send(**kwargs)
        else:
            await message.edit(**kwargs)
            sent = message
    except (discord.HTTPException, TypeError, ValueError):
        if message is not None:
            try:
                await message.delete()
            except discord.HTTPException:
                pass
        if str(settings.get("render_mode", "v1")).lower() == "v2":
            settings = await service.set_render_mode(guild_id, "v1")
        products = await service.products(guild_id)
        sent = await channel.send(**public_message_kwargs(service, settings, products))
    await service.save_public_panel(guild_id, channel.id, sent.id)
    return sent


class ShopeePublicChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, service: ShopeeFindsService) -> None:
        self.service = service
        super().__init__(
            placeholder="Selecione o canal público dos achadinhos",
            channel_types=[discord.ChannelType.text],
            min_values=1,
            max_values=1,
            custom_id="jarvis:shopee-finds:admin:channel-select",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Use dentro de um servidor.", ephemeral=True)
            return
        channel = await _resolve_selected_text_channel(
            interaction,
            self.values[0] if self.values else None,
        )
        if channel is None:
            await interaction.response.send_message("Selecione um canal de texto.", ephemeral=True)
            return
        settings = await self.service.settings_for(interaction.guild.id)
        old_channel_id = int(settings.get("panel_channel_id") or 0)
        old_message_id = int(settings.get("panel_message_id") or 0)
        if old_channel_id and old_message_id and old_channel_id != channel.id:
            old_channel = interaction.guild.get_channel(old_channel_id)
            if isinstance(old_channel, discord.TextChannel):
                try:
                    old_message = await old_channel.fetch_message(old_message_id)
                    await old_message.delete()
                except discord.HTTPException:
                    pass
        message = await send_or_edit_public_panel(self.service, interaction.guild.id, channel)
        await interaction.response.send_message(
            f"Painel público dos achadinhos publicado em {message.jump_url}.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )


class ShopeePublicChannelSelectView(discord.ui.View):
    def __init__(self, service: ShopeeFindsService) -> None:
        super().__init__(timeout=180)
        self.add_item(ShopeePublicChannelSelect(service))


class ShopeeManagerRoleSelect(discord.ui.RoleSelect):
    def __init__(self, service: ShopeeFindsService) -> None:
        self.service = service
        super().__init__(
            placeholder="Selecione cargos que podem gerenciar os achadinhos",
            min_values=0,
            max_values=10,
            custom_id="jarvis:shopee-finds:admin:manager-role-select",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Use dentro de um servidor.", ephemeral=True)
            return
        roles = [role for role in self.values if isinstance(role, discord.Role)]
        await self.service.set_manager_roles(interaction.guild.id, [role.id for role in roles])
        await interaction.response.send_message(
            "Cargos gestores atualizados." if roles else "Cargos gestores removidos.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )


class ShopeeManagerRoleSelectView(discord.ui.View):
    def __init__(self, service: ShopeeFindsService) -> None:
        super().__init__(timeout=180)
        self.add_item(ShopeeManagerRoleSelect(service))


class ShopeeNotificationRoleSelect(discord.ui.RoleSelect):
    def __init__(self, service: ShopeeFindsService) -> None:
        self.service = service
        super().__init__(
            placeholder="Selecione o cargo que receberá avisos de novos achadinhos",
            min_values=0,
            max_values=1,
            custom_id="jarvis:shopee-finds:admin:notification-role-select",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Use dentro de um servidor.", ephemeral=True)
            return
        role = self.values[0] if self.values else None
        role_id = role.id if isinstance(role, discord.Role) else 0
        await self.service.set_notification_role(interaction.guild.id, role_id)
        await refresh_public_panel_from_interaction(self.service, interaction)
        await interaction.response.send_message(
            f"Cargo de avisos definido como {role.mention}." if isinstance(role, discord.Role) else "Cargo de avisos removido.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )


class ShopeeNotificationRoleSelectView(discord.ui.View):
    def __init__(self, service: ShopeeFindsService) -> None:
        super().__init__(timeout=180)
        self.add_item(ShopeeNotificationRoleSelect(service))


async def _can_manage_shopee(service: ShopeeFindsService, interaction: discord.Interaction) -> bool:
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        return False
    if interaction.user.id == service.owner_id or interaction.user.guild_permissions.manage_guild:
        return True
    settings = await service.settings_for(interaction.guild.id)
    manager_roles = {
        int(role_id)
        for role_id in (settings.get("manager_role_ids") or [])
        if str(role_id).isdigit()
    }
    return bool(manager_roles and any(role.id in manager_roles for role in interaction.user.roles))


class ShopeeEditProductBasicModal(discord.ui.Modal, title="Editar produto"):
    name_input = discord.ui.TextInput(label="Nome", max_length=100)
    price_input = discord.ui.TextInput(label="Preço ou observação", max_length=80, required=False)
    url_input = discord.ui.TextInput(
        label="Link principal",
        style=discord.TextStyle.paragraph,
        max_length=MAX_PRODUCT_URL_LENGTH,
    )
    category_input = discord.ui.TextInput(label="Categoria", max_length=80, required=False)
    description_input = discord.ui.TextInput(label="Descrição", style=discord.TextStyle.paragraph, max_length=1800, required=False)

    def __init__(self, service: ShopeeFindsService, product: ShopeeProduct) -> None:
        super().__init__(timeout=None)
        self.service = service
        self.product = product
        self.name_input.default = product.name
        self.price_input.default = product.price
        self.url_input.default = product.url
        self.category_input.default = product.category
        self.description_input.default = product.description

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Use dentro de um servidor.", ephemeral=True)
            return
        try:
            product = await self.service.update_product(
                interaction.guild.id,
                self.product.id,
                name=str(self.name_input),
                price=str(self.price_input),
                url=str(self.url_input),
                category=str(self.category_input),
                description=str(self.description_input),
            )
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        if product is None:
            await interaction.response.send_message("Produto não encontrado.", ephemeral=True)
            return
        await interaction.response.edit_message(
            embed=product_embed(product),
            view=ShopeeProductEditorView(self.service, product.id),
        )


class ShopeeEditProductVisualModal(discord.ui.Modal, title="Visual do produto"):
    title_input = discord.ui.TextInput(label="Título do embed", max_length=256, required=False)
    color_input = discord.ui.TextInput(label="Cor hexadecimal", placeholder="#F97316", max_length=20, required=False)
    author_input = discord.ui.TextInput(label="Autor", max_length=256, required=False)
    thumbnail_input = discord.ui.TextInput(label="Thumbnail URL", max_length=500, required=False)
    image_input = discord.ui.TextInput(label="Imagem/banner URL", max_length=500, required=False)

    def __init__(self, service: ShopeeFindsService, product: ShopeeProduct) -> None:
        super().__init__(timeout=None)
        self.service = service
        self.product = product
        self.title_input.default = product.embed_title
        self.color_input.default = f"#{_safe_color(product.embed_color, DEFAULT_COLOR):06X}"
        self.author_input.default = product.author_name
        self.thumbnail_input.default = product.thumbnail_url
        self.image_input.default = product.image_url

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Use dentro de um servidor.", ephemeral=True)
            return
        try:
            product = await self.service.update_product(
                interaction.guild.id,
                self.product.id,
                embed_title=str(self.title_input),
                embed_color=_safe_color(str(self.color_input), DEFAULT_COLOR),
                author_name=str(self.author_input),
                thumbnail_url=str(self.thumbnail_input),
                image_url=str(self.image_input),
            )
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        if product is None:
            await interaction.response.send_message("Produto não encontrado.", ephemeral=True)
            return
        await interaction.response.edit_message(
            embed=product_embed(product),
            view=ShopeeProductEditorView(self.service, product.id),
        )


class ShopeeEditProductComponentsModal(discord.ui.Modal, title="Campos e botões"):
    coupon_input = discord.ui.TextInput(label="Cupom", max_length=80, required=False)
    footer_input = discord.ui.TextInput(label="Rodapé", max_length=2048, required=False)
    author_icon_input = discord.ui.TextInput(label="Ícone do autor URL", max_length=500, required=False)
    fields_input = discord.ui.TextInput(
        label="Campos: nome|valor|inline",
        style=discord.TextStyle.paragraph,
        max_length=1800,
        required=False,
    )
    buttons_input = discord.ui.TextInput(
        label="Botões: texto|url",
        style=discord.TextStyle.paragraph,
        max_length=4000,
        required=False,
    )

    def __init__(self, service: ShopeeFindsService, product: ShopeeProduct) -> None:
        super().__init__(timeout=None)
        self.service = service
        self.product = product
        self.coupon_input.default = product.coupon
        self.footer_input.default = product.footer_text
        self.author_icon_input.default = product.author_icon_url
        self.fields_input.default = _field_lines(product.fields)
        self.buttons_input.default = _button_lines(product.buttons or [{"label": "Abrir na Shopee", "url": product.url}])

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Use dentro de um servidor.", ephemeral=True)
            return
        try:
            product = await self.service.update_product(
                interaction.guild.id,
                self.product.id,
                coupon=str(self.coupon_input),
                footer_text=str(self.footer_input),
                author_icon_url=str(self.author_icon_input),
                fields=_parse_field_lines(str(self.fields_input)),
                buttons=_parse_button_lines(str(self.buttons_input)),
            )
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        if product is None:
            await interaction.response.send_message("Produto não encontrado.", ephemeral=True)
            return
        await interaction.response.edit_message(
            embed=product_embed(product),
            view=ShopeeProductEditorView(self.service, product.id),
        )


class ShopeeProductEditorView(discord.ui.View):
    def __init__(self, service: ShopeeFindsService, product_id: str) -> None:
        super().__init__(timeout=300)
        self.service = service
        self.product_id = product_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if await _can_manage_shopee(self.service, interaction):
            return True
        await interaction.response.send_message("Você não pode editar esse produto.", ephemeral=True)
        return False

    async def _product(self, interaction: discord.Interaction) -> ShopeeProduct | None:
        if interaction.guild is None:
            return None
        return await self.service.product(interaction.guild.id, self.product_id)

    @discord.ui.button(label="Editor V1 completo", style=discord.ButtonStyle.primary, row=0)
    async def edit_v1(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        product = await self._product(interaction)
        if product is None or interaction.guild is None:
            await interaction.response.send_message("Produto não encontrado.", ephemeral=True)
            return

        async def save_callback(
            save_interaction: discord.Interaction,
            session: EmbedEditorSession,
        ) -> str:
            if save_interaction.guild is None:
                return "Use dentro de um servidor."
            updated = await _update_product_from_embed_session(
                self.service,
                save_interaction.guild.id,
                product,
                session,
            )
            if updated is None:
                return "Produto não encontrado."
            await refresh_public_panel_from_interaction(self.service, save_interaction)
            await refresh_admin_panel_from_interaction(self.service, save_interaction)
            return f"Produto `{updated.name}` atualizado pelo editor V1."

        session = EmbedEditorSession(
            interaction.user.id,
            interaction.channel or interaction,
            draft=_draft_from_product(product),
            save_callback=save_callback,
        )
        await interaction.response.send_message(
            "Editor V1 completo do produto. Edite como no gerador de embeds e clique em **Enviar Personalizado** ou **Enviar Rápido** para salvar.",
            embeds=session.editor_embeds(),
            view=session.preview_view(),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @discord.ui.button(label="Editor V2 completo", style=discord.ButtonStyle.primary, row=0)
    async def edit_v2(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        product = await self._product(interaction)
        if product is None:
            await interaction.response.send_message("Produto não encontrado.", ephemeral=True)
            return
        session = _v2_session_from_product(interaction, self.service, product)
        await interaction.response.send_message(
            "Editor V2 completo do produto. Edite containers, itens, mídia e botões. Ao enviar, o produto será salvo.",
            view=session.editor_view(),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @discord.ui.button(label="Editar básico", style=discord.ButtonStyle.secondary, row=1)
    async def edit_basic(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        product = await self._product(interaction)
        if product is None:
            await interaction.response.send_message("Produto não encontrado.", ephemeral=True)
            return
        await interaction.response.send_modal(ShopeeEditProductBasicModal(self.service, product))

    @discord.ui.button(label="Editar visual", style=discord.ButtonStyle.secondary, row=1)
    async def edit_visual(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        product = await self._product(interaction)
        if product is None:
            await interaction.response.send_message("Produto não encontrado.", ephemeral=True)
            return
        await interaction.response.send_modal(ShopeeEditProductVisualModal(self.service, product))

    @discord.ui.button(label="Campos e botões", style=discord.ButtonStyle.secondary, row=1)
    async def edit_components(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        product = await self._product(interaction)
        if product is None:
            await interaction.response.send_message("Produto não encontrado.", ephemeral=True)
            return
        await interaction.response.send_modal(ShopeeEditProductComponentsModal(self.service, product))

    @discord.ui.button(label="Atualizar prévia", style=discord.ButtonStyle.secondary, row=1)
    async def refresh_preview(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        product = await self._product(interaction)
        if product is None:
            await interaction.response.send_message("Produto não encontrado.", ephemeral=True)
            return
        await interaction.response.edit_message(embed=product_embed(product), view=ShopeeProductEditorView(self.service, product.id))


class ShopeeEditProductSelect(discord.ui.Select):
    def __init__(self, service: ShopeeFindsService, products: list[ShopeeProduct]) -> None:
        self.service = service
        super().__init__(
            placeholder="Escolha um produto para editar como embed",
            min_values=1,
            max_values=1,
            options=product_options(products),
            disabled=not products,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Use dentro de um servidor.", ephemeral=True)
            return
        product = await self.service.product(interaction.guild.id, self.values[0])
        if product is None:
            await interaction.response.send_message("Produto não encontrado.", ephemeral=True)
            return
        await interaction.response.send_message(
            "Prévia do produto. Use os botões para personalizar o embed e os botões clicáveis.",
            embed=product_embed(product),
            view=ShopeeProductEditorView(self.service, product.id),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )


class ShopeeEditProductSelectView(discord.ui.View):
    def __init__(self, service: ShopeeFindsService, products: list[ShopeeProduct]) -> None:
        super().__init__(timeout=180)
        self.add_item(ShopeeEditProductSelect(service, products))


async def announce_new_products(
    service: ShopeeFindsService,
    interaction: discord.Interaction,
) -> tuple[bool, str]:
    if interaction.guild is None:
        return False, "Use dentro de um servidor."
    settings = await service.settings_for(interaction.guild.id)
    role_id = int(settings.get("notification_role_id") or 0)
    if not role_id:
        return False, "Configure primeiro o cargo de avisos dos achadinhos."
    role = interaction.guild.get_role(role_id)
    if role is None:
        return False, "O cargo de avisos configurado não existe mais."
    products = await service.products(interaction.guild.id)
    announced = {str(product_id) for product_id in settings.get("announced_product_ids", [])}
    pending = [product for product in products if product.id not in announced]
    if not pending:
        return False, "Não encontrei produtos novos pendentes de anúncio."
    channel_id = int(settings.get("panel_channel_id") or 0)
    channel = interaction.guild.get_channel(channel_id) if channel_id else None
    if not isinstance(channel, discord.TextChannel):
        channel = interaction.channel if isinstance(interaction.channel, discord.TextChannel) else None
    if not isinstance(channel, discord.TextChannel):
        return False, "Configure um canal público para os achadinhos antes de anunciar."
    embed = discord.Embed(
        title="Novos achadinhos disponíveis",
        description=(
            "Atualizei a curadoria com novos produtos. Abra o painel dos achadinhos "
            "e selecione um item para ver detalhes, imagens e link completo."
        ),
        color=discord.Color(_safe_color(settings.get("color"), DEFAULT_COLOR)),
        timestamp=datetime.now(timezone.utc),
    )
    lines = [
        f"- **{product.name}** — {product.category}{f' • {product.price}' if product.price else ''}"
        for product in pending[:12]
    ]
    if len(pending) > 12:
        lines.append(f"- e mais {len(pending) - 12} produto(s)")
    embed.add_field(name="Produtos adicionados", value="\n".join(lines)[:1024], inline=False)
    embed.set_footer(text="Use o botão do painel para entrar ou sair dos avisos.")
    await channel.send(
        content=role.mention,
        embed=embed,
        allowed_mentions=discord.AllowedMentions(roles=True, users=False, everyone=False),
    )
    await service.mark_announced(interaction.guild.id, [product.id for product in pending])
    return True, f"Anunciei {len(pending)} produto(s) novo(s) em {channel.mention}."


class ShopeeFindsAdminView(discord.ui.View):
    def __init__(self, service: ShopeeFindsService) -> None:
        super().__init__(timeout=None)
        self.service = service

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Use dentro de um servidor.", ephemeral=True)
            return False
        if await _can_manage_shopee(self.service, interaction):
            return True
        await interaction.response.send_message("Você precisa de Gerenciar Servidor para mexer nos achadinhos.", ephemeral=True)
        return False

    async def _refresh_admin(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            return
        settings = await self.service.settings_for(interaction.guild.id)
        products = await self.service.products(interaction.guild.id)
        await interaction.response.edit_message(
            embed=admin_embed(settings, len(products)),
            view=ShopeeFindsAdminView(self.service),
        )

    @discord.ui.button(label="Adicionar produto", style=discord.ButtonStyle.success, custom_id="jarvis:shopee-finds:admin:add", row=0)
    async def add_product(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_message(
            "Escolha como deseja montar o produto. V1 e V2 abrem o mesmo gerador visual dos embeds.",
            view=ShopeeAddProductModeView(self.service),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @discord.ui.button(label="Editar produto", style=discord.ButtonStyle.primary, custom_id="jarvis:shopee-finds:admin:edit-product", row=0)
    async def edit_product(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Use dentro de um servidor.", ephemeral=True)
            return
        products = await self.service.products(interaction.guild.id)
        await interaction.response.send_message(
            "Selecione o produto que deseja personalizar.",
            view=ShopeeEditProductSelectView(self.service, products),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @discord.ui.button(label="Remover produto", style=discord.ButtonStyle.danger, custom_id="jarvis:shopee-finds:admin:remove", row=0)
    async def remove_product(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Use dentro de um servidor.", ephemeral=True)
            return
        products = await self.service.products(interaction.guild.id)
        await interaction.response.send_message(
            "Selecione o produto que deseja remover.",
            view=ShopeeRemoveView(self.service, products),
            ephemeral=True,
        )

    @discord.ui.button(label="Listar produtos", style=discord.ButtonStyle.secondary, custom_id="jarvis:shopee-finds:admin:list", row=0)
    async def list_products(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Use dentro de um servidor.", ephemeral=True)
            return
        products = await self.service.products(interaction.guild.id)
        if not products:
            await interaction.response.send_message("Nenhum produto cadastrado ainda.", ephemeral=True)
            return
        lines = [
            f"`{product.id}` — **{product.name}** — {product.category}{f' — {product.price}' if product.price else ''}"
            for product in products[:40]
        ]
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @discord.ui.button(label="Publicar aqui", style=discord.ButtonStyle.primary, custom_id="jarvis:shopee-finds:admin:publish", row=0)
    async def publish_here(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.guild is None or not isinstance(interaction.channel, discord.TextChannel):
            await interaction.response.send_message("Use em um canal de texto.", ephemeral=True)
            return
        message = await send_or_edit_public_panel(self.service, interaction.guild.id, interaction.channel)
        await interaction.response.send_message(f"Painel público dos achadinhos publicado em {message.jump_url}.", ephemeral=True)

    @discord.ui.button(label="Atualizar público", style=discord.ButtonStyle.secondary, custom_id="jarvis:shopee-finds:admin:refresh-public", row=1)
    async def refresh_public(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        ok = await refresh_public_panel_from_interaction(self.service, interaction)
        await interaction.followup.send("Painel público atualizado." if ok else "Ainda não existe painel público publicado.", ephemeral=True)

    @discord.ui.button(label="Pausar", style=discord.ButtonStyle.danger, custom_id="jarvis:shopee-finds:admin:pause", row=1)
    async def pause(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Use dentro de um servidor.", ephemeral=True)
            return
        settings = await self.service.set_enabled(interaction.guild.id, False)
        await refresh_public_panel_from_interaction(self.service, interaction)
        products = await self.service.products(interaction.guild.id)
        await interaction.response.edit_message(embed=admin_embed(settings, len(products)), view=ShopeeFindsAdminView(self.service))

    @discord.ui.button(label="Reabrir", style=discord.ButtonStyle.success, custom_id="jarvis:shopee-finds:admin:open", row=1)
    async def reopen(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Use dentro de um servidor.", ephemeral=True)
            return
        settings = await self.service.set_enabled(interaction.guild.id, True)
        await refresh_public_panel_from_interaction(self.service, interaction)
        products = await self.service.products(interaction.guild.id)
        await interaction.response.edit_message(embed=admin_embed(settings, len(products)), view=ShopeeFindsAdminView(self.service))

    @discord.ui.button(label="Configurar texto", style=discord.ButtonStyle.secondary, custom_id="jarvis:shopee-finds:admin:config", row=1)
    async def configure(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Use dentro de um servidor.", ephemeral=True)
            return
        settings = await self.service.settings_for(interaction.guild.id)
        await interaction.response.send_modal(ShopeeFindsConfigModal(self.service, settings))

    @discord.ui.button(label="Canal público", style=discord.ButtonStyle.secondary, custom_id="jarvis:shopee-finds:admin:channel", row=2)
    async def public_channel(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_message(
            "Escolha o canal onde o painel público dos achadinhos deve ficar.",
            view=ShopeePublicChannelSelectView(self.service),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @discord.ui.button(label="Cargos gestores", style=discord.ButtonStyle.secondary, custom_id="jarvis:shopee-finds:admin:roles", row=2)
    async def manager_roles(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_message(
            "Escolha os cargos que podem gerenciar os achadinhos.",
            view=ShopeeManagerRoleSelectView(self.service),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @discord.ui.button(label="Visual do embed", style=discord.ButtonStyle.secondary, custom_id="jarvis:shopee-finds:admin:visual", row=2)
    async def visual(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Use dentro de um servidor.", ephemeral=True)
            return
        settings = await self.service.settings_for(interaction.guild.id)
        if str(settings.get("render_mode", "v1")).lower() == "v2":
            session = _v2_session_from_public_settings(interaction, self.service, settings)
            await interaction.response.send_message(view=session.editor_view(), ephemeral=True)
            return

        async def save_callback(
            save_interaction: discord.Interaction,
            session: EmbedEditorSession,
        ) -> str:
            if save_interaction.guild is None:
                return "Use dentro de um servidor."
            draft = session.draft
            await self.service.save_settings(
                save_interaction.guild.id,
                {
                    "message_content": session.message_content.strip()[:1800],
                    "title": draft.title.strip()[:120] or "Achadinhos do Dev",
                    "description": draft.description.strip()[:2000] or _default_settings()["description"],
                    "color": int(draft.color),
                    "author_name": draft.author.strip()[:120],
                    "author_icon_url": draft.author_icon_url.strip()[:500],
                    "thumbnail_url": draft.thumbnail_url.strip()[:500],
                    "image_url": draft.image_url.strip()[:500],
                    "footer": draft.footer.strip()[:120] or "Bella Finds Center",
                    "fields": [field.to_dict() for field in draft.fields[:25]],
                },
            )
            await refresh_public_panel_from_interaction(self.service, save_interaction)
            return "Embed V1 dos achadinhos salvo. O painel público também foi atualizado."

        session = EmbedEditorSession(
            interaction.user.id,
            interaction.channel or interaction,
            draft=_draft_from_public_settings(settings),
            message_content=str(settings.get("message_content") or ""),
            save_callback=save_callback,
        )
        await interaction.response.send_message(
            content="Pré-visualização do editor dos achadinhos.",
            embeds=session.editor_embeds(),
            view=session.preview_view(),
            ephemeral=True,
        )

    @discord.ui.button(label="Cargo avisos", style=discord.ButtonStyle.secondary, custom_id="jarvis:shopee-finds:admin:notification-role", row=2)
    async def notification_role(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_message(
            "Escolha o cargo que as pessoas poderão resgatar para receber avisos de novos achadinhos.",
            view=ShopeeNotificationRoleSelectView(self.service),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @discord.ui.button(label="Anunciar novos", style=discord.ButtonStyle.primary, custom_id="jarvis:shopee-finds:admin:announce-new", row=2)
    async def announce_new(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        ok, message = await announce_new_products(self.service, interaction)
        await interaction.followup.send(message, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @discord.ui.button(label="Alternar V1/V2", style=discord.ButtonStyle.secondary, custom_id="jarvis:shopee-finds:admin:render", row=1)
    async def toggle_render(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Use dentro de um servidor.", ephemeral=True)
            return
        settings = await self.service.settings_for(interaction.guild.id)
        next_mode = "v1" if str(settings.get("render_mode", "v1")).lower() == "v2" else "v2"
        settings = await self.service.set_render_mode(interaction.guild.id, next_mode)
        await refresh_public_panel_from_interaction(self.service, interaction)
        products = await self.service.products(interaction.guild.id)
        await interaction.response.edit_message(embed=admin_embed(settings, len(products)), view=ShopeeFindsAdminView(self.service))


async def refresh_public_panel_from_interaction(
    service: ShopeeFindsService,
    interaction: discord.Interaction,
) -> bool:
    if interaction.guild is None:
        return False
    settings = await service.settings_for(interaction.guild.id)
    channel_id = int(settings.get("panel_channel_id") or 0)
    message_id = int(settings.get("panel_message_id") or 0)
    if not channel_id or not message_id:
        return False
    channel = interaction.client.get_channel(channel_id)
    if not isinstance(channel, discord.TextChannel):
        return False
    try:
        message = await channel.fetch_message(message_id)
        await send_or_edit_public_panel(service, interaction.guild.id, channel, message)
        return True
    except (discord.NotFound, discord.Forbidden, discord.HTTPException, TypeError, ValueError):
        return False


async def refresh_admin_panel_from_interaction(
    service: ShopeeFindsService,
    interaction: discord.Interaction,
) -> bool:
    if interaction.guild is None:
        return False
    settings = await service.settings_for(interaction.guild.id)
    channel_id = int(settings.get("admin_channel_id") or 0)
    message_id = int(settings.get("admin_message_id") or 0)
    if not channel_id or not message_id:
        return False
    channel = interaction.client.get_channel(channel_id)
    if not isinstance(channel, discord.TextChannel):
        return False
    try:
        message = await channel.fetch_message(message_id)
        products = await service.products(interaction.guild.id)
        await message.edit(
            embed=admin_embed(settings, len(products)),
            view=ShopeeFindsAdminView(service),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return True
    except (discord.NotFound, discord.Forbidden, discord.HTTPException, TypeError, ValueError):
        return False
