"""Entidades reais do Discord (usuários, cargos, canais) em memórias e respostas.

Regras:
- IDs vêm da mensagem original (menções nativas e links discord.com/channels/...)
  ou de uma resolução por nome dentro do próprio servidor, só quando o resultado
  é único. Nada de IDs inventados pelo modelo.
- A memória guarda a entidade estruturada (tipo, ID, servidor, nome, menção).
- Na resposta, a aplicação garante a menção nativa (<#id>, <@&id>, <@id>) mesmo
  que o modelo tenha trocado por nome ou link, e limita quem pode ser notificado.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any, Iterable
from unicodedata import combining, normalize

USER_MENTION_RE = re.compile(r"<@!?(\d{15,22})>")
ROLE_MENTION_RE = re.compile(r"<@&(\d{15,22})>")
CHANNEL_MENTION_RE = re.compile(r"<#(\d{15,22})>")
CHANNEL_LINK_RE = re.compile(
    r"https?://(?:www\.|ptb\.|canary\.)?discord(?:app)?\.com/channels/"
    r"(\d{15,22})/(\d{15,22})(?:/(\d{15,22}))?"
)
ANY_MENTION_RE = re.compile(r"<(@!?|@&|#)(\d{15,22})>")


def _norm(value: str) -> str:
    decomposed = normalize("NFKD", str(value or "").casefold())
    text = "".join(char for char in decomposed if not combining(char))
    text = re.sub(r"[^\w\s-]", " ", text)
    return " ".join(text.replace("-", " ").replace("_", " ").split())


@dataclass(frozen=True)
class DiscordEntity:
    type: str            # user | role | text_channel | voice_channel | category | thread | channel
    entity_id: int
    guild_id: int | None
    display_name: str = ""

    @property
    def mention(self) -> str:
        if self.type == "user":
            return f"<@{self.entity_id}>"
        if self.type == "role":
            return f"<@&{self.entity_id}>"
        return f"<#{self.entity_id}>"

    @property
    def is_channel(self) -> bool:
        return self.type in {"text_channel", "voice_channel", "category", "thread", "channel"}

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["mention_format"] = self.mention
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DiscordEntity | None":
        try:
            return cls(
                type=str(data["type"]),
                entity_id=int(data["entity_id"]),
                guild_id=int(data["guild_id"]) if data.get("guild_id") else None,
                display_name=str(data.get("display_name") or ""),
            )
        except (KeyError, TypeError, ValueError):
            return None


def _channel_type(channel: Any) -> str:
    kind = type(channel).__name__.casefold()
    if "voice" in kind or "stage" in kind:
        return "voice_channel"
    if "category" in kind:
        return "category"
    if "thread" in kind:
        return "thread"
    if "text" in kind or "forum" in kind or "news" in kind:
        return "text_channel"
    return "channel"


def _lookup_channel(guild: Any, channel_id: int) -> Any:
    if guild is None:
        return None
    getter = getattr(guild, "get_channel_or_thread", None) or getattr(guild, "get_channel", None)
    try:
        return getter(channel_id) if callable(getter) else None
    except Exception:
        return None


def extract_entities(content: str, guild: Any = None) -> list[DiscordEntity]:
    """Entidades citadas por ID na mensagem (menções nativas e links de canal)."""
    guild_id = getattr(guild, "id", None)
    found: dict[tuple[str, int], DiscordEntity] = {}

    for match in CHANNEL_LINK_RE.finditer(content or ""):
        link_guild, channel_id = int(match.group(1)), int(match.group(2))
        if guild_id and link_guild != guild_id:
            continue  # link de outro servidor: não vira menção aqui
        channel = _lookup_channel(guild, channel_id)
        kind = _channel_type(channel) if channel is not None else "channel"
        name = getattr(channel, "name", "") if channel is not None else ""
        found[("channel", channel_id)] = DiscordEntity(kind, channel_id, link_guild, name)

    for match in CHANNEL_MENTION_RE.finditer(content or ""):
        channel_id = int(match.group(1))
        channel = _lookup_channel(guild, channel_id)
        if guild is not None and channel is None:
            continue
        kind = _channel_type(channel) if channel is not None else "channel"
        found[("channel", channel_id)] = DiscordEntity(
            kind, channel_id, guild_id, getattr(channel, "name", "") if channel else ""
        )

    for match in ROLE_MENTION_RE.finditer(content or ""):
        role_id = int(match.group(1))
        role = guild.get_role(role_id) if guild is not None else None
        if guild is not None and role is None:
            continue
        found[("role", role_id)] = DiscordEntity(
            "role", role_id, guild_id, getattr(role, "name", "") if role else ""
        )

    for match in USER_MENTION_RE.finditer(content or ""):
        user_id = int(match.group(1))
        member = guild.get_member(user_id) if guild is not None else None
        found[("user", user_id)] = DiscordEntity(
            "user", user_id, guild_id, getattr(member, "display_name", "") if member else ""
        )
    return list(found.values())


@dataclass(frozen=True)
class Resolution:
    entity: DiscordEntity | None
    candidates: tuple[DiscordEntity, ...] = ()

    @property
    def ambiguous(self) -> bool:
        return self.entity is None and len(self.candidates) > 1


def resolve_by_name(guild: Any, kind: str, name: str) -> Resolution:
    """Procura cargo/canal pelo nome dentro do servidor. Só aceita resultado único."""
    if guild is None or not name.strip():
        return Resolution(None)
    wanted = _norm(name)
    if kind == "role":
        pool = [
            DiscordEntity("role", role.id, guild.id, role.name)
            for role in getattr(guild, "roles", [])
            if not getattr(role, "is_default", lambda: False)()
        ]
    else:
        pool = []
        for channel in getattr(guild, "channels", []):
            channel_kind = _channel_type(channel)
            if kind == "voice_channel" and channel_kind != "voice_channel":
                continue
            if kind == "text_channel" and channel_kind not in {"text_channel", "thread"}:
                continue
            pool.append(DiscordEntity(channel_kind, channel.id, guild.id, channel.name))
    exact = [item for item in pool if _norm(item.display_name) == wanted]
    if len(exact) == 1:
        return Resolution(exact[0])
    if len(exact) > 1:
        return Resolution(None, tuple(exact))
    partial = [
        item for item in pool
        if wanted and (wanted in _norm(item.display_name) or _norm(item.display_name) in wanted)
        and len(_norm(item.display_name)) >= 3
    ]
    if len(partial) == 1:
        return Resolution(partial[0])
    return Resolution(None, tuple(partial))


def entities_from_dicts(values: Iterable[Any]) -> list[DiscordEntity]:
    result = []
    for value in values or []:
        entity = DiscordEntity.from_dict(value) if isinstance(value, dict) else None
        if entity is not None:
            result.append(entity)
    return result


def describe_for_model(entities: Iterable[DiscordEntity]) -> str:
    """Texto para o modelo usar as menções exatamente como estão."""
    labels = {
        "role": "cargo", "user": "usuário", "voice_channel": "canal de voz",
        "text_channel": "canal de texto", "category": "categoria",
        "thread": "tópico", "channel": "canal",
    }
    return "; ".join(
        f"{labels.get(item.type, 'canal')} {item.display_name or item.entity_id} = {item.mention}"
        for item in entities
    )


def render_mentions(answer: str, entities: Iterable[DiscordEntity]) -> str:
    """Garante a menção nativa de cada entidade, sem estragar o resto do texto."""
    text = str(answer or "")
    for entity in entities:
        if entity.mention in text:
            continue
        if entity.is_channel:
            # Link do canal vira a menção clicável.
            pattern = re.compile(
                r"https?://(?:www\.|ptb\.|canary\.)?discord(?:app)?\.com/channels/\d{15,22}/"
                rf"{entity.entity_id}(?:/\d{{15,22}})?"
            )
            text, count = pattern.subn(entity.mention, text, count=1)
            if count:
                continue
        name = entity.display_name.strip()
        if name and len(name) >= 3:
            prefixed = re.compile(rf"(?<![\w<])[#@]?{re.escape(name)}(?![\w>])", re.IGNORECASE)
            text, count = prefixed.subn(entity.mention, text, count=1)
            if count:
                continue
        text = f"{text.rstrip()} {entity.mention}".strip()
    return text


def sanitize_model_mentions(
    answer: str,
    *,
    guild: Any = None,
    allowed_ids: set[int] | None = None,
) -> str:
    """Remove menções que o modelo inventou ou que não existem neste servidor."""
    allowed = set(allowed_ids or ())

    def keep(match: re.Match) -> str:
        prefix, raw_id = match.group(1), int(match.group(2))
        if raw_id in allowed:
            return match.group(0)
        if guild is None:
            return match.group(0)
        if prefix == "#":
            return match.group(0) if _lookup_channel(guild, raw_id) is not None else ""
        if prefix == "@&":
            return match.group(0) if guild.get_role(raw_id) is not None else ""
        return match.group(0) if guild.get_member(raw_id) is not None else ""

    cleaned = ANY_MENTION_RE.sub(keep, str(answer or ""))
    return re.sub(r"[ \t]{2,}", " ", cleaned).strip()


def allowed_mention_ids(
    *,
    author_id: int | None,
    entities: Iterable[DiscordEntity],
    guild: Any = None,
) -> tuple[list[int], list[int]]:
    """(usuários, cargos) que podem ser notificados nesta resposta.

    Só o autor e usuários/cargos vindos de memória validada. Cargo só notifica
    se existir no servidor e estiver marcado como mencionável (ou se a Bella
    tiver permissão de mencionar todos).
    """
    users = [author_id] if author_id else []
    roles: list[int] = []
    me = getattr(guild, "me", None)
    can_mention_all = bool(
        me is not None and getattr(getattr(me, "guild_permissions", None), "mention_everyone", False)
    )
    for entity in entities:
        if entity.type == "user":
            users.append(entity.entity_id)
        elif entity.type == "role" and guild is not None:
            role = guild.get_role(entity.entity_id)
            if role is not None and (getattr(role, "mentionable", False) or can_mention_all):
                roles.append(entity.entity_id)
    return list(dict.fromkeys(users)), list(dict.fromkeys(roles))
