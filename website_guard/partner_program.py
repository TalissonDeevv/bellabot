from __future__ import annotations

import asyncio
import difflib
import json
import logging
import random
import re
import sqlite3
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import discord

from .maintenance_center import guard_public_interaction

from src.ai.groq_service import GroqService
from src.ai.moderation_ai import neutralize_mass_mentions
from src.ai.personality import BELLA_PERSONA_RUNTIME

from .embed_builder import (
    DraftButton,
    DraftField,
    EMBED_AI_SCHEMA,
    EmbedDraft,
    EmbedEditorSession,
    V2ContainerDraft,
    V2EditorSession,
    V2ItemDraft,
)
from .partners import (
    PartnerProgramManager,
    PartnerWorkspaceResult,
    _invite_code_from_url,
    _invite_is_permanent,
    partner_profile_embed,
)
from .state import StateStore

LOGGER = logging.getLogger(__name__)

PANEL_TYPE_PARTNER = "partner_program"
DB_PATH = Path("data/partner_program.db")
DEFAULT_COLOR = 0x5865F2
ACTIVE_APPLICATION_STATUSES = {
    "started",
    "interviewing",
    "under_review",
    "awaiting_information",
}
BLOCKING_APPLICATION_STATUSES = ACTIVE_APPLICATION_STATUSES | {"approved"}
APPLICATION_STATUS_LABELS = {
    "started": "🟡 Em andamento",
    "interviewing": "🟡 Em andamento",
    "under_review": "🔵 Aguardando análise",
    "approved": "🟢 Aprovado",
    "rejected": "🔴 Recusado",
    "awaiting_information": "🟠 Informações adicionais solicitadas",
    "cancelled": "⚫ Cancelado",
}

PROGRAM_KNOWLEDGE = """
Programa de Parceiros de Talisson Souza.

O programa funciona por indicação: a pessoa identifica uma oportunidade real, registra a indicação pelos canais oficiais e a organização conduz análise, negociação, escopo, desenvolvimento, entrega e suporte.

A comissão oficial é de 50% do valor do projeto. Ela só é liberada quando a indicação é válida, o projeto é contratado e finalizado e as regras do programa são cumpridas. O exemplo oficial é um projeto de R$ 5.000 com comissão de R$ 2.500.

Podem participar pessoas com boa rede de contatos, designers, social medias, gestores, donos de comunidades, criadores de conteúdo, profissionais de tecnologia, freelancers, empreendedores e pessoas que possam conectar oportunidades reais. Não é obrigatório ser desenvolvedor, vender projetos ou desenvolver soluções.

Benefícios possíveis: cargo de Parceiro no Discord, canal exclusivo, materiais de apoio, acompanhamento, conteúdos sobre prospecção, networking, comunicação, negociação, relacionamento com clientes, status transparente das indicações e reconhecimento quando fizer sentido.

Não existe horário fixo, meta obrigatória, quantidade mínima de indicações, rotina obrigatória, exclusividade, vínculo empregatício, sociedade, franquia ou representação.

A entrada oficial acontece apenas após análise, aprovação e formalização contratual quando aplicável. A entrevista não significa aprovação automática.

Menores de 16 ou 17 anos podem exigir etapa adicional, autorização do responsável legal e documentação em fluxo próprio. A Bella não deve solicitar documentos sensíveis durante a conversa.

Não são permitidos spam, compra de leads, abordagem enganosa, dados obtidos ilegalmente ou práticas que violem privacidade, consumidor e legislação brasileira.

Oportunidades que podem fazer sentido: sites, landing pages, sistemas web, sistemas personalizados, automações, bots Discord, integrações, dashboards, ferramentas internas, plataformas digitais, sistemas administrativos e soluções tecnológicas sob medida.

Se uma informação específica não estiver nesta base, a Bella deve dizer que prefere confirmar antes de passar algo errado.
""".strip()

PARTNER_PROGRAM_URL = "https://www.talissonsouza.com.br/seja-afiliado"
PARTNER_FAQ_CACHE_KEY = "partner_program_official_page_cache"
PARTNER_FAQ_CACHE_TTL_SECONDS = 6 * 60 * 60
LEGACY_DEFAULT_PARTNER_PANEL_DESCRIPTION = (
    "Quer indicar oportunidades reais e participar de um programa organizado de parceiros. "
    "A Bella conduz uma conversa inicial para entender seu perfil e encaminhar sua inscrição para análise."
)
DEFAULT_PARTNER_PANEL_DESCRIPTION = (
    "Indique oportunidades reais para sites, sistemas, automações, bots para Discord e outras "
    "soluções tecnológicas. A equipe conduz negociação, desenvolvimento, entrega e suporte. "
    "Quando a indicação for validada e o projeto estiver finalizado, a comissão é de 50% do valor do projeto."
)


INTERVIEW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "reply": {"type": "string"},
        "memory_updates": {"type": "object"},
        "should_finish": {"type": "boolean"},
        "finish_reason": {"type": "string"},
    },
    "required": ["reply", "memory_updates", "should_finish"],
}

REPORT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "identification": {"type": "string"},
        "profile": {"type": "string"},
        "clients": {"type": "string"},
        "prospecting": {"type": "string"},
        "networking": {"type": "string"},
        "motivation": {"type": "string"},
        "learning": {"type": "string"},
        "program_understanding": {"type": "string"},
        "summary": {"type": "string"},
        "attention_points": {"type": "string"},
        "compatibility": {"type": "string"},
        "communication": {"type": "string"},
        "opportunity_potential": {"type": "string"},
        "understanding": {"type": "string"},
        "notes": {"type": "string"},
    },
    "required": ["summary"],
}


@dataclass(slots=True)
class PartnerPanel:
    id: int
    guild_id: int
    name: str
    panel_type: str
    channel_id: int | None
    message_id: int | None
    status: str
    created_by: int
    admin_user_id: int | None
    manager_role_id: int | None
    report_channel_id: int | None
    partner_role_id: int | None
    render_mode: str
    created_at: str
    updated_at: str
    published_at: str | None


class PartnerProgramDatabase:
    def __init__(self, path: Path = DB_PATH) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def _init_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS interactive_panels (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    panel_type TEXT NOT NULL,
                    channel_id INTEGER,
                    message_id INTEGER,
                    status TEXT NOT NULL DEFAULT 'draft',
                    created_by INTEGER NOT NULL,
                    admin_user_id INTEGER,
                    manager_role_id INTEGER,
                    report_channel_id INTEGER,
                    partner_role_id INTEGER,
                    render_mode TEXT NOT NULL DEFAULT 'v1',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    published_at TEXT
                );

                CREATE TABLE IF NOT EXISTS interactive_panel_embeds (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    panel_id INTEGER NOT NULL,
                    position INTEGER NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    title TEXT,
                    description TEXT,
                    url TEXT,
                    color INTEGER,
                    author_name TEXT,
                    author_url TEXT,
                    author_icon_url TEXT,
                    thumbnail_url TEXT,
                    image_url TEXT,
                    image_position TEXT NOT NULL DEFAULT 'end',
                    footer_text TEXT,
                    footer_icon_url TEXT,
                    fields_json TEXT NOT NULL DEFAULT '[]',
                    show_timestamp INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS interactive_panel_buttons (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    panel_id INTEGER NOT NULL,
                    position INTEGER NOT NULL,
                    label TEXT NOT NULL,
                    emoji TEXT,
                    style TEXT NOT NULL,
                    action_type TEXT NOT NULL,
                    action_config TEXT NOT NULL DEFAULT '{}',
                    custom_id TEXT,
                    enabled INTEGER NOT NULL DEFAULT 1
                );

                CREATE TABLE IF NOT EXISTS interactive_panel_editor_payloads (
                    panel_id INTEGER PRIMARY KEY,
                    v1_payload_json TEXT NOT NULL DEFAULT '{}',
                    v2_payload_json TEXT NOT NULL DEFAULT '{}',
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS partner_applications (
                    id TEXT PRIMARY KEY,
                    guild_id INTEGER NOT NULL,
                    panel_id INTEGER,
                    discord_user_id INTEGER NOT NULL,
                    ticket_id INTEGER,
                    status TEXT NOT NULL,
                    memory_json TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    reviewed_at TEXT,
                    reviewed_by INTEGER,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS partner_application_counters (
                    year INTEGER PRIMARY KEY,
                    last_number INTEGER NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS partner_application_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    application_id TEXT NOT NULL,
                    author_type TEXT NOT NULL,
                    discord_user_id INTEGER,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS partner_application_analysis (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    application_id TEXT NOT NULL UNIQUE,
                    report_json TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS partner_application_blocks (
                    guild_id INTEGER NOT NULL,
                    discord_user_id INTEGER NOT NULL,
                    blocked_by INTEGER,
                    reason TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (guild_id, discord_user_id)
                );

                CREATE TABLE IF NOT EXISTS partner_program_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER,
                    actor_id INTEGER,
                    action TEXT NOT NULL,
                    target TEXT,
                    details TEXT,
                    created_at TEXT NOT NULL
                );
                """
            )
            try:
                connection.execute(
                    "ALTER TABLE interactive_panels ADD COLUMN render_mode TEXT NOT NULL DEFAULT 'v1'"
                )
            except sqlite3.OperationalError:
                pass
            try:
                connection.execute(
                    "ALTER TABLE interactive_panels ADD COLUMN manager_role_id INTEGER"
                )
            except sqlite3.OperationalError:
                pass
            try:
                connection.execute(
                    "ALTER TABLE interactive_panel_embeds ADD COLUMN fields_json TEXT NOT NULL DEFAULT '[]'"
                )
            except sqlite3.OperationalError:
                pass
            try:
                connection.execute(
                    "ALTER TABLE interactive_panel_embeds ADD COLUMN image_position TEXT NOT NULL DEFAULT 'end'"
                )
            except sqlite3.OperationalError:
                pass

    async def create_default_panel(
        self,
        *,
        guild_id: int,
        name: str,
        created_by: int,
        report_channel_id: int | None,
        partner_role_id: int | None,
    ) -> PartnerPanel:
        now = _now()
        async with self._lock:
            with self._connect() as connection:
                cursor = connection.execute(
                    """
                    INSERT INTO interactive_panels
                    (guild_id, name, panel_type, status, created_by, admin_user_id, manager_role_id,
                     report_channel_id, partner_role_id, render_mode, created_at, updated_at)
                    VALUES (?, ?, ?, 'draft', ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        guild_id,
                        name,
                        PANEL_TYPE_PARTNER,
                        created_by,
                        created_by,
                        None,
                        report_channel_id,
                        partner_role_id,
                        "v1",
                        now,
                        now,
                    ),
                )
                panel_id = int(cursor.lastrowid)
                connection.execute(
                    """
                    INSERT INTO interactive_panel_embeds
                    (panel_id, position, title, description, color, image_url, footer_text)
                    VALUES (?, 1, ?, ?, ?, ?, ?)
                    """,
                    (
                        panel_id,
                        "Programa de Parceiros",
                        DEFAULT_PARTNER_PANEL_DESCRIPTION,
                        DEFAULT_COLOR,
                        "",
                        "Programa de Parceiros | Talisson Souza",
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO interactive_panel_buttons
                    (panel_id, position, label, style, action_type, action_config, custom_id)
                    VALUES (?, 1, ?, 'primary', 'partner_application', '{}', ?)
                    """,
                    (panel_id, "Quero me inscrever", f"jarvis:partner:apply:{panel_id}"),
                )
                connection.execute(
                    """
                    INSERT INTO interactive_panel_buttons
                    (panel_id, position, label, style, action_type, action_config)
                    VALUES (?, 2, ?, 'link', 'url', ?)
                    """,
                    (
                        panel_id,
                        "Saiba mais",
                        json.dumps(
                            {"url": "https://www.talissonsouza.com.br/seja-afiliado"},
                            ensure_ascii=False,
                        ),
                    ),
                )
        panel = await self.panel(panel_id)
        if panel is None:
            raise RuntimeError("Não foi possível criar o painel de parceiros.")
        return panel

    async def panel(self, panel_id: int) -> PartnerPanel | None:
        async with self._lock:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT * FROM interactive_panels WHERE id = ?",
                    (panel_id,),
                ).fetchone()
        return _panel_from_row(row) if row else None

    async def latest_panel(self, guild_id: int) -> PartnerPanel | None:
        async with self._lock:
            with self._connect() as connection:
                row = connection.execute(
                    """
                    SELECT * FROM interactive_panels
                    WHERE guild_id = ? AND panel_type = ? AND status != 'deleted'
                    ORDER BY id DESC LIMIT 1
                    """,
                    (guild_id, PANEL_TYPE_PARTNER),
                ).fetchone()
        return _panel_from_row(row) if row else None

    async def panels(self, guild_id: int) -> list[PartnerPanel]:
        async with self._lock:
            with self._connect() as connection:
                rows = connection.execute(
                    """
                    SELECT * FROM interactive_panels
                    WHERE guild_id = ? AND panel_type = ? AND status != 'deleted'
                    ORDER BY id DESC
                    """,
                    (guild_id, PANEL_TYPE_PARTNER),
                ).fetchall()
        return [_panel_from_row(row) for row in rows]

    async def all_panels(self) -> list[PartnerPanel]:
        async with self._lock:
            with self._connect() as connection:
                rows = connection.execute(
                    """
                    SELECT * FROM interactive_panels
                    WHERE panel_type = ? AND status != 'deleted'
                    ORDER BY id DESC
                    """,
                    (PANEL_TYPE_PARTNER,),
                ).fetchall()
        return [_panel_from_row(row) for row in rows]

    async def published_panels(self) -> list[PartnerPanel]:
        async with self._lock:
            with self._connect() as connection:
                rows = connection.execute(
                    """
                    SELECT * FROM interactive_panels
                    WHERE panel_type = ? AND status = 'published' AND message_id IS NOT NULL
                    """,
                    (PANEL_TYPE_PARTNER,),
                ).fetchall()
        return [_panel_from_row(row) for row in rows]

    async def embeds(self, panel_id: int) -> list[dict[str, Any]]:
        async with self._lock:
            with self._connect() as connection:
                rows = connection.execute(
                    """
                    SELECT * FROM interactive_panel_embeds
                    WHERE panel_id = ? AND enabled = 1
                    ORDER BY position ASC, id ASC
                    """,
                    (panel_id,),
                ).fetchall()
        return [dict(row) for row in rows]

    async def buttons(self, panel_id: int) -> list[dict[str, Any]]:
        async with self._lock:
            with self._connect() as connection:
                rows = connection.execute(
                    """
                    SELECT * FROM interactive_panel_buttons
                    WHERE panel_id = ? AND enabled = 1
                    ORDER BY position ASC, id ASC
                    """,
                    (panel_id,),
                ).fetchall()
        return [dict(row) for row in rows]

    async def editor_payload(self, panel_id: int, mode: str) -> dict[str, Any]:
        column = "v2_payload_json" if str(mode).lower() == "v2" else "v1_payload_json"
        async with self._lock:
            with self._connect() as connection:
                row = connection.execute(
                    f"SELECT {column} AS payload FROM interactive_panel_editor_payloads WHERE panel_id = ?",
                    (panel_id,),
                ).fetchone()
        if row is None:
            return {}
        value = _json_loads(str(row["payload"] or "{}"), {})
        return dict(value) if isinstance(value, dict) else {}

    async def save_editor_payload(
        self,
        panel_id: int,
        mode: str,
        payload: dict[str, Any],
    ) -> None:
        column = "v2_payload_json" if str(mode).lower() == "v2" else "v1_payload_json"
        encoded = json.dumps(payload, ensure_ascii=False)
        async with self._lock:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO interactive_panel_editor_payloads
                    (panel_id, v1_payload_json, v2_payload_json, updated_at)
                    VALUES (?, '{}', '{}', ?)
                    ON CONFLICT(panel_id) DO NOTHING
                    """,
                    (panel_id, _now()),
                )
                connection.execute(
                    f"UPDATE interactive_panel_editor_payloads SET {column} = ?, updated_at = ? WHERE panel_id = ?",
                    (encoded, _now(), panel_id),
                )

    async def update_panel_publication(
        self,
        panel_id: int,
        *,
        channel_id: int | None,
        message_id: int | None,
        status: str,
    ) -> None:
        now = _now()
        published_at = now if status == "published" else None
        async with self._lock:
            with self._connect() as connection:
                connection.execute(
                    """
                    UPDATE interactive_panels
                    SET channel_id = ?, message_id = ?, status = ?, updated_at = ?,
                        published_at = COALESCE(?, published_at)
                    WHERE id = ?
                    """,
                    (channel_id, message_id, status, now, published_at, panel_id),
                )

    async def configure_panel(
        self,
        panel_id: int,
        *,
        report_channel_id: int | None = None,
        partner_role_id: int | None = None,
        admin_user_id: int | None = None,
        manager_role_id: int | None = None,
        name: str | None = None,
    ) -> None:
        panel = await self.panel(panel_id)
        if panel is None:
            return
        async with self._lock:
            with self._connect() as connection:
                connection.execute(
                    """
                    UPDATE interactive_panels
                    SET report_channel_id = ?, partner_role_id = ?, admin_user_id = ?,
                        manager_role_id = ?, name = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        report_channel_id if report_channel_id is not None else panel.report_channel_id,
                        partner_role_id if partner_role_id is not None else panel.partner_role_id,
                        admin_user_id if admin_user_id is not None else panel.admin_user_id,
                        manager_role_id if manager_role_id is not None else panel.manager_role_id,
                        name or panel.name,
                        _now(),
                        panel_id,
                    ),
                )

    async def update_main_embed(
        self,
        panel_id: int,
        *,
        title: str | None = None,
        description: str | None = None,
        color: int | None = None,
        image_url: str | None = None,
        thumbnail_url: str | None = None,
    ) -> None:
        async with self._lock:
            with self._connect() as connection:
                row = connection.execute(
                    """
                    SELECT id FROM interactive_panel_embeds
                    WHERE panel_id = ?
                    ORDER BY position ASC, id ASC LIMIT 1
                    """,
                    (panel_id,),
                ).fetchone()
                if row is None:
                    connection.execute(
                        """
                        INSERT INTO interactive_panel_embeds
                        (panel_id, position, title, description, color, image_url, thumbnail_url)
                        VALUES (?, 1, ?, ?, ?, ?, ?)
                        """,
                        (
                            panel_id,
                            title or "Programa de Parceiros",
                            description or "",
                            color or DEFAULT_COLOR,
                            image_url or "",
                            thumbnail_url or "",
                        ),
                    )
                else:
                    connection.execute(
                        """
                        UPDATE interactive_panel_embeds
                        SET title = COALESCE(?, title),
                            description = COALESCE(?, description),
                            color = COALESCE(?, color),
                            image_url = COALESCE(?, image_url),
                            thumbnail_url = COALESCE(?, thumbnail_url)
                        WHERE id = ?
                        """,
                        (title, description, color, image_url, thumbnail_url, int(row["id"])),
                    )

    async def replace_main_embed(
        self,
        panel_id: int,
        *,
        draft: EmbedDraft,
        show_timestamp: bool = False,
    ) -> None:
        fields_json = json.dumps([field.to_dict() for field in draft.fields[:25]], ensure_ascii=False)
        async with self._lock:
            with self._connect() as connection:
                row = connection.execute(
                    """
                    SELECT id FROM interactive_panel_embeds
                    WHERE panel_id = ?
                    ORDER BY position ASC, id ASC LIMIT 1
                    """,
                    (panel_id,),
                ).fetchone()
                if row is None:
                    connection.execute(
                        """
                        INSERT INTO interactive_panel_embeds
                        (panel_id, position, title, description, color, author_name,
                         author_icon_url, thumbnail_url, image_url, image_position, footer_text,
                         footer_icon_url, fields_json, show_timestamp)
                        VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            panel_id,
                            draft.title,
                            draft.description,
                            int(draft.color),
                            draft.author,
                            draft.author_icon_url,
                            draft.thumbnail_url,
                            draft.image_url,
                            _panel_image_position(draft.image_position),
                            draft.footer,
                            draft.footer_icon_url,
                            fields_json,
                            int(show_timestamp),
                        ),
                    )
                else:
                    connection.execute(
                        """
                        UPDATE interactive_panel_embeds
                        SET title = ?, description = ?, color = ?, author_name = ?,
                            author_icon_url = ?, thumbnail_url = ?, image_url = ?,
                            image_position = ?, footer_text = ?, footer_icon_url = ?, fields_json = ?,
                            show_timestamp = ?
                        WHERE id = ?
                        """,
                        (
                            draft.title,
                            draft.description,
                            int(draft.color),
                            draft.author,
                            draft.author_icon_url,
                            draft.thumbnail_url,
                            draft.image_url,
                            _panel_image_position(draft.image_position),
                            draft.footer,
                            draft.footer_icon_url,
                            fields_json,
                            int(show_timestamp),
                            int(row["id"]),
                        ),
                    )

    async def replace_url_buttons(self, panel_id: int, buttons: list[DraftButton]) -> None:
        async with self._lock:
            with self._connect() as connection:
                connection.execute(
                    "DELETE FROM interactive_panel_buttons WHERE panel_id = ? AND action_type = 'url'",
                    (panel_id,),
                )
                for index, button in enumerate(buttons[:4], start=2):
                    if not _valid_url_or_none(button.url):
                        continue
                    connection.execute(
                        """
                        INSERT INTO interactive_panel_buttons
                        (panel_id, position, label, emoji, style, action_type, action_config, enabled)
                        VALUES (?, ?, ?, ?, 'link', 'url', ?, 1)
                        """,
                        (
                            panel_id,
                            index,
                            _trim(button.label, 80) or "Abrir link",
                            button.emoji or "",
                            json.dumps({"url": button.url}, ensure_ascii=False),
                        ),
                    )

    async def set_buttons(
        self,
        panel_id: int,
        *,
        signup_label: str,
        info_url: str | None,
    ) -> None:
        async with self._lock:
            with self._connect() as connection:
                cursor = connection.execute(
                    """
                    UPDATE interactive_panel_buttons
                    SET label = ?, custom_id = ?, enabled = 1
                    WHERE panel_id = ? AND action_type = 'partner_application'
                    """,
                    (signup_label, f"jarvis:partner:apply:{panel_id}", panel_id),
                )
                if cursor.rowcount == 0:
                    connection.execute(
                        """
                        INSERT INTO interactive_panel_buttons
                        (panel_id, position, label, style, action_type, action_config, custom_id)
                        VALUES (?, 1, ?, 'primary', 'partner_application', '{}', ?)
                        """,
                        (panel_id, signup_label, f"jarvis:partner:apply:{panel_id}"),
                    )
                connection.execute(
                    "DELETE FROM interactive_panel_buttons WHERE panel_id = ? AND action_type = 'url'",
                    (panel_id,),
                )
                if info_url:
                    connection.execute(
                        """
                        INSERT INTO interactive_panel_buttons
                        (panel_id, position, label, style, action_type, action_config)
                        VALUES (?, 2, 'Saiba mais', 'link', 'url', ?)
                        """,
                        (
                            panel_id,
                            json.dumps({"url": info_url}, ensure_ascii=False),
                        ),
                    )

    async def duplicate_panel(self, panel_id: int, created_by: int) -> int | None:
        panel = await self.panel(panel_id)
        if panel is None:
            return None
        source_embeds = await self.embeds(panel_id)
        source_buttons = await self.buttons(panel_id)
        async with self._lock:
            with self._connect() as connection:
                now = _now()
                cursor = connection.execute(
                    """
                    INSERT INTO interactive_panels
                    (guild_id, name, panel_type, status, created_by, admin_user_id, manager_role_id,
                     report_channel_id, partner_role_id, render_mode, created_at, updated_at)
                    VALUES (?, ?, ?, 'draft', ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        panel.guild_id,
                        f"{panel.name} (cópia)",
                        panel.panel_type,
                        created_by,
                        panel.admin_user_id,
                        panel.manager_role_id,
                        panel.report_channel_id,
                        panel.partner_role_id,
                        panel.render_mode,
                        now,
                        now,
                    ),
                )
                new_id = int(cursor.lastrowid)
                for embed in source_embeds:
                    connection.execute(
                        """
                        INSERT INTO interactive_panel_embeds
                        (panel_id, position, enabled, title, description, url, color,
                         author_name, author_url, author_icon_url, thumbnail_url, image_url,
                         footer_text, footer_icon_url, fields_json, show_timestamp)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            new_id,
                            int(embed.get("position") or 1),
                            int(embed.get("enabled") or 1),
                            embed.get("title"),
                            embed.get("description"),
                            embed.get("url"),
                            embed.get("color"),
                            embed.get("author_name"),
                            embed.get("author_url"),
                            embed.get("author_icon_url"),
                            embed.get("thumbnail_url"),
                            embed.get("image_url"),
                            embed.get("footer_text"),
                            embed.get("footer_icon_url"),
                            embed.get("fields_json") or "[]",
                            int(embed.get("show_timestamp") or 0),
                        ),
                    )
                for button in source_buttons:
                    action_type = str(button.get("action_type") or "")
                    custom_id = (
                        f"jarvis:partner:apply:{new_id}"
                        if action_type == "partner_application"
                        else None
                    )
                    connection.execute(
                        """
                        INSERT INTO interactive_panel_buttons
                        (panel_id, position, label, emoji, style, action_type,
                         action_config, custom_id, enabled)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            new_id,
                            int(button.get("position") or 1),
                            str(button.get("label") or "Botão"),
                            button.get("emoji"),
                            str(button.get("style") or "secondary"),
                            action_type,
                            str(button.get("action_config") or "{}"),
                            custom_id,
                            int(button.get("enabled") or 1),
                        ),
                    )
                return new_id

    async def set_panel_status(self, panel_id: int, status: str) -> None:
        async with self._lock:
            with self._connect() as connection:
                connection.execute(
                    "UPDATE interactive_panels SET status = ?, updated_at = ? WHERE id = ?",
                    (status, _now(), panel_id),
                )

    async def set_panel_render_mode(self, panel_id: int, mode: str) -> None:
        render_mode = "v2" if str(mode).lower() == "v2" else "v1"
        async with self._lock:
            with self._connect() as connection:
                connection.execute(
                    "UPDATE interactive_panels SET render_mode = ?, updated_at = ? WHERE id = ?",
                    (render_mode, _now(), panel_id),
                )

    async def set_manager_role(self, panel_id: int, role_id: int | None) -> None:
        async with self._lock:
            with self._connect() as connection:
                connection.execute(
                    "UPDATE interactive_panels SET manager_role_id = ?, updated_at = ? WHERE id = ?",
                    (role_id, _now(), panel_id),
                )

    async def blocking_application_for_user(
        self,
        guild_id: int,
        user_id: int,
    ) -> dict[str, Any] | None:
        async with self._lock:
            with self._connect() as connection:
                row = connection.execute(
                    """
                    SELECT * FROM partner_applications
                    WHERE guild_id = ? AND discord_user_id = ?
                    AND status IN ({})
                    ORDER BY created_at DESC LIMIT 1
                    """.format(",".join("?" for _ in BLOCKING_APPLICATION_STATUSES)),
                    (guild_id, user_id, *BLOCKING_APPLICATION_STATUSES),
                ).fetchone()
        return dict(row) if row else None

    async def application_block_for_user(
        self,
        guild_id: int,
        user_id: int,
    ) -> dict[str, Any] | None:
        async with self._lock:
            with self._connect() as connection:
                row = connection.execute(
                    """
                    SELECT * FROM partner_application_blocks
                    WHERE guild_id = ? AND discord_user_id = ?
                    """,
                    (guild_id, user_id),
                ).fetchone()
        return dict(row) if row else None

    async def set_application_block(
        self,
        guild_id: int,
        user_id: int,
        *,
        blocked_by: int,
        reason: str,
    ) -> None:
        now = _now()
        async with self._lock:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO partner_application_blocks
                    (guild_id, discord_user_id, blocked_by, reason, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(guild_id, discord_user_id) DO UPDATE SET
                        blocked_by = excluded.blocked_by,
                        reason = excluded.reason,
                        updated_at = excluded.updated_at
                    """,
                    (guild_id, user_id, blocked_by, reason[:1000], now, now),
                )

    async def remove_application_block(self, guild_id: int, user_id: int) -> bool:
        async with self._lock:
            with self._connect() as connection:
                cursor = connection.execute(
                    "DELETE FROM partner_application_blocks WHERE guild_id = ? AND discord_user_id = ?",
                    (guild_id, user_id),
                )
                return cursor.rowcount > 0

    async def application_blocks(self, guild_id: int, *, limit: int = 20) -> list[dict[str, Any]]:
        async with self._lock:
            with self._connect() as connection:
                rows = connection.execute(
                    """
                    SELECT * FROM partner_application_blocks
                    WHERE guild_id = ?
                    ORDER BY updated_at DESC
                    LIMIT ?
                    """,
                    (guild_id, limit),
                ).fetchall()
        return [dict(row) for row in rows]

    async def applications_for_user(
        self,
        guild_id: int,
        user_id: int,
        *,
        statuses: set[str] | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        params: list[Any] = [guild_id, user_id]
        query = "SELECT * FROM partner_applications WHERE guild_id = ? AND discord_user_id = ?"
        if statuses:
            query += " AND status IN ({})".format(",".join("?" for _ in statuses))
            params.extend(sorted(statuses))
        query += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)
        async with self._lock:
            with self._connect() as connection:
                rows = connection.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    async def create_application(
        self,
        *,
        guild_id: int,
        panel_id: int,
        discord_user_id: int,
        ticket_id: int,
    ) -> dict[str, Any]:
        now = _now()
        year = datetime.now(timezone.utc).year
        memory = _default_memory()
        async with self._lock:
            with self._connect() as connection:
                counter_row = connection.execute(
                    "SELECT last_number FROM partner_application_counters WHERE year = ?",
                    (year,),
                ).fetchone()
                if counter_row:
                    next_number = int(counter_row["last_number"]) + 1
                else:
                    count_row = connection.execute(
                        "SELECT COUNT(*) AS total FROM partner_applications WHERE id LIKE ?",
                        (f"PAR-{year}-%",),
                    ).fetchone()
                    next_number = int(count_row["total"] if count_row else 0) + 1
                while connection.execute(
                    "SELECT 1 FROM partner_applications WHERE id = ?",
                    (f"PAR-{year}-{next_number:04d}",),
                ).fetchone():
                    next_number += 1
                application_id = f"PAR-{year}-{next_number:04d}"
                connection.execute(
                    """
                    INSERT INTO partner_application_counters (year, last_number, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(year) DO UPDATE SET
                        last_number = excluded.last_number,
                        updated_at = excluded.updated_at
                    """,
                    (year, next_number, now),
                )
                connection.execute(
                    """
                    INSERT INTO partner_applications
                    (id, guild_id, panel_id, discord_user_id, ticket_id, status,
                     memory_json, started_at, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, 'interviewing', ?, ?, ?, ?)
                    """,
                    (
                        application_id,
                        guild_id,
                        panel_id,
                        discord_user_id,
                        ticket_id,
                        json.dumps(memory, ensure_ascii=False),
                        now,
                        now,
                        now,
                    ),
                )
        application = await self.application(application_id)
        if application is None:
            raise RuntimeError("Não foi possível criar a inscrição.")
        return application

    async def application(self, application_id: str) -> dict[str, Any] | None:
        async with self._lock:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT * FROM partner_applications WHERE id = ?",
                    (application_id,),
                ).fetchone()
        return dict(row) if row else None

    async def application_by_thread(self, thread_id: int) -> dict[str, Any] | None:
        async with self._lock:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT * FROM partner_applications WHERE ticket_id = ? ORDER BY created_at DESC LIMIT 1",
                    (thread_id,),
                ).fetchone()
        return dict(row) if row else None

    async def applications_for_review(self) -> list[dict[str, Any]]:
        async with self._lock:
            with self._connect() as connection:
                rows = connection.execute(
                    """
                    SELECT * FROM partner_applications
                    WHERE status IN ('under_review', 'awaiting_information')
                    ORDER BY updated_at DESC
                    """
                ).fetchall()
        return [dict(row) for row in rows]

    async def applications_with_open_threads(self) -> list[dict[str, Any]]:
        async with self._lock:
            with self._connect() as connection:
                rows = connection.execute(
                    """
                    SELECT * FROM partner_applications
                    WHERE ticket_id IS NOT NULL
                    AND status IN ('started', 'interviewing', 'under_review', 'awaiting_information')
                    ORDER BY updated_at DESC
                    """
                ).fetchall()
        return [dict(row) for row in rows]

    async def applications(
        self,
        guild_id: int,
        *,
        status: str | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        params: list[Any] = [guild_id]
        query = "SELECT * FROM partner_applications WHERE guild_id = ?"
        if status:
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)
        async with self._lock:
            with self._connect() as connection:
                rows = connection.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    async def update_application(self, application_id: str, **values: Any) -> None:
        if not values:
            return
        values["updated_at"] = _now()
        fields = ", ".join(f"{key} = ?" for key in values)
        async with self._lock:
            with self._connect() as connection:
                connection.execute(
                    f"UPDATE partner_applications SET {fields} WHERE id = ?",
                    (*values.values(), application_id),
                )

    async def add_message(
        self,
        application_id: str,
        *,
        author_type: str,
        discord_user_id: int | None,
        content: str,
    ) -> None:
        async with self._lock:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO partner_application_messages
                    (application_id, author_type, discord_user_id, content, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (application_id, author_type, discord_user_id, content, _now()),
                )

    async def messages(self, application_id: str, limit: int = 30) -> list[dict[str, Any]]:
        async with self._lock:
            with self._connect() as connection:
                rows = connection.execute(
                    """
                    SELECT * FROM partner_application_messages
                    WHERE application_id = ?
                    ORDER BY id DESC LIMIT ?
                    """,
                    (application_id, limit),
                ).fetchall()
        return [dict(row) for row in reversed(rows)]

    async def save_analysis(self, application_id: str, report: dict[str, Any]) -> None:
        now = _now()
        summary = str(report.get("summary") or "Resumo indisponível.")
        async with self._lock:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO partner_application_analysis
                    (application_id, report_json, summary, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(application_id) DO UPDATE SET
                        report_json = excluded.report_json,
                        summary = excluded.summary,
                        updated_at = excluded.updated_at
                    """,
                    (
                        application_id,
                        json.dumps(report, ensure_ascii=False),
                        summary,
                        now,
                        now,
                    ),
                )

    async def analysis(self, application_id: str) -> dict[str, Any] | None:
        async with self._lock:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT report_json FROM partner_application_analysis WHERE application_id = ?",
                    (application_id,),
                ).fetchone()
        if not row:
            return None
        return _json_loads(str(row["report_json"]), {})

    async def audit(
        self,
        *,
        guild_id: int | None,
        actor_id: int | None,
        action: str,
        target: str = "",
        details: str = "",
    ) -> None:
        async with self._lock:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO partner_program_audit
                    (guild_id, actor_id, action, target, details, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (guild_id, actor_id, action, target, details, _now()),
                )


class PartnerProgramService:
    def __init__(
        self,
        bot: discord.Client,
        state: StateStore,
        ai_service: GroqService,
        *,
        owner_id: int,
        affiliate_manager: PartnerProgramManager | None = None,
    ) -> None:
        self.bot = bot
        self.state = state
        self.ai_service = ai_service
        self.owner_id = int(owner_id)
        self.affiliate_manager = affiliate_manager
        self.db = PartnerProgramDatabase()
        self.analytics: Any = None
        self._views_registered = False
        self._application_locks: dict[str, asyncio.Lock] = {}

    async def _save_admin_panel_location(
        self,
        *,
        panel_id: int,
        guild_id: int,
        channel_id: int,
        message_id: int,
    ) -> None:
        def mutate(section: dict[str, Any]) -> None:
            guilds = section.setdefault("guilds", {})
            if not isinstance(guilds, dict):
                guilds = {}
                section["guilds"] = guilds
            panels = guilds.setdefault(str(guild_id), {})
            if not isinstance(panels, dict):
                panels = {}
                guilds[str(guild_id)] = panels
            entries = panels.setdefault("partner_admin", [])
            if not isinstance(entries, list):
                entries = []
            entries = [
                item
                for item in entries
                if isinstance(item, dict)
                and int(item.get("message_id") or 0) != int(message_id)
            ]
            entries.append(
                {
                    "panel_id": int(panel_id),
                    "channel_id": int(channel_id),
                    "message_id": int(message_id),
                    "updated_at": _now(),
                }
            )
            panels["partner_admin"] = entries[-10:]

        await self.state.mutate_section("fixed_admin_panels", mutate)

    async def refresh_admin_panel_messages(self, guild: discord.Guild) -> int:
        section = await self.state.get_section("fixed_admin_panels")
        guilds = section.get("guilds") if isinstance(section.get("guilds"), dict) else {}
        panels = guilds.get(str(guild.id)) if isinstance(guilds, dict) else None
        entries = panels.get("partner_admin") if isinstance(panels, dict) else []
        if not isinstance(entries, list):
            return 0
        refreshed = 0
        for item in entries:
            if not isinstance(item, dict):
                continue
            try:
                panel_id = int(item.get("panel_id") or 0)
                channel_id = int(item.get("channel_id") or 0)
                message_id = int(item.get("message_id") or 0)
            except (TypeError, ValueError):
                continue
            panel = await self.db.panel(panel_id)
            if panel is None:
                continue
            channel = self.bot.get_channel(channel_id)
            if not isinstance(channel, discord.TextChannel):
                continue
            try:
                message = await channel.fetch_message(message_id)
                await message.edit(
                    embed=await self.admin_embed(panel),
                    view=PartnerAdminView(self, panel.id),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                refreshed += 1
            except (discord.NotFound, discord.Forbidden, discord.HTTPException, TypeError, ValueError):
                LOGGER.warning("Não foi possível atualizar painel administrativo de parceiros %s.", message_id)
        return refreshed

    def is_admin(self, user: discord.abc.User, panel: PartnerPanel | None = None) -> bool:
        if user.id == self.owner_id or (panel is not None and user.id == panel.admin_user_id):
            return True
        if panel is None or not panel.manager_role_id or not isinstance(user, discord.Member):
            return False
        return any(role.id == panel.manager_role_id for role in user.roles)

    def can_manage_application(self, user: discord.abc.User, panel: PartnerPanel | None = None) -> bool:
        if self.is_admin(user, panel):
            return True
        permissions = getattr(user, "guild_permissions", None)
        return bool(
            permissions
            and (
                getattr(permissions, "manage_threads", False)
                or getattr(permissions, "manage_channels", False)
                or getattr(permissions, "manage_guild", False)
                or getattr(permissions, "administrator", False)
            )
        )

    async def generate_panel_embed_payload(self, prompt: str) -> dict[str, Any]:
        generation_prompt = (
            "Crie uma mensagem de Discord com embed profissional a partir do pedido abaixo.\n"
            "Retorne somente JSON no formato esperado do editor de embeds.\n"
            "Não copie o briefing do administrador. Transforme o pedido em um texto final pronto para publicação.\n"
            "Use parágrafos curtos, campos quando melhorar a leitura e preserve links/menções de cargos/canais.\n"
            "Nunca use @everyone nem @here.\n\n"
            f"Pedido:\n{prompt}"
        )
        try:
            raw_text = await self.ai_service.generate_text(
                generation_prompt,
                max_output_tokens=1200,
                timeout_seconds=10,
                model_policy="economy",
                use_cache=False,
                system_prompt=(
                    "Você gera somente JSON válido para um editor de embeds do Discord. "
                    "Não use markdown, comentários ou texto fora do objeto JSON."
                ),
            )
            return _extract_json_object(raw_text)
        except Exception as exc:
            LOGGER.warning("Gerador de embed do painel de parceiros falhou: %s: %s", type(exc).__name__, exc)
            return {}

    async def _thread_for_application(self, application: dict[str, Any]) -> discord.Thread | None:
        guild = self.bot.get_guild(int(application.get("guild_id") or 0))
        thread_id = int(application.get("ticket_id") or 0)
        if guild is None or not thread_id:
            return None
        thread = guild.get_thread(thread_id)
        if thread is not None:
            return thread
        try:
            fetched = await self.bot.fetch_channel(thread_id)
        except discord.HTTPException:
            return None
        return fetched if isinstance(fetched, discord.Thread) else None

    async def _rename_application_thread(
        self,
        application: dict[str, Any],
        *,
        status: str | None = None,
        member_name: str | None = None,
    ) -> None:
        thread = await self._thread_for_application(application)
        if thread is None:
            return
        new_name = _application_thread_name(application, status=status, member_name=member_name)
        if thread.name == new_name:
            return
        try:
            await thread.edit(name=new_name, reason=f"Status da inscrição {application.get('id')} atualizado")
        except discord.HTTPException:
            LOGGER.warning("Não foi possível renomear a thread da inscrição %s.", application.get("id"))

    async def _set_application_status(
        self,
        application: dict[str, Any],
        status: str,
        *,
        actor_id: int | None = None,
        memory_json: str | None = None,
        completed_at: str | None = None,
        reviewed: bool = False,
    ) -> dict[str, Any]:
        values: dict[str, Any] = {"status": status}
        if memory_json is not None:
            values["memory_json"] = memory_json
        if completed_at is not None:
            values["completed_at"] = completed_at
        if reviewed:
            values["reviewed_at"] = _now()
            values["reviewed_by"] = actor_id
        await self.db.update_application(str(application["id"]), **values)
        updated = dict(application)
        updated.update(values)
        await self._rename_application_thread(updated, status=status)
        return updated

    def register_persistent_views(self) -> None:
        if self._views_registered or not hasattr(self.bot, "add_view"):
            return
        self._views_registered = True

        async def runner() -> None:
            for panel in await self.db.all_panels():
                try:
                    self.bot.add_view(PartnerAdminView(self, panel.id))
                    if self.analytics is not None:
                        self.analytics.register_persistent_view(panel.id)
                except Exception:
                    LOGGER.warning("Não foi possível registrar painel admin de parceiros %s.", panel.id)
            for panel in await self.db.published_panels():
                try:
                    kwargs = await self.build_panel_message_kwargs(panel.id, disabled=False)
                    self.bot.add_view(kwargs["view"])
                except Exception:
                    LOGGER.warning("Não foi possível registrar painel público de parceiros %s.", panel.id)
                    if str(panel.render_mode).lower() == "v2":
                        await self.db.set_panel_render_mode(panel.id, "v1")
                        await self.refresh_public_panel_message(panel.id)
            for application in await self.db.applications_with_open_threads():
                try:
                    self.bot.add_view(
                        PartnerApplicationThreadView(
                            self,
                            str(application["id"]),
                            int(application["discord_user_id"]),
                        )
                    )
                except Exception:
                    LOGGER.warning("Não foi possível registrar thread view de inscrição %s.", application.get("id"))
            for application in await self.db.applications_for_review():
                try:
                    self.bot.add_view(PartnerReviewView(self, str(application["id"])))
                except Exception:
                    LOGGER.warning("Não foi possível registrar review view de inscrição %s.", application.get("id"))

        asyncio.create_task(runner())

    async def create_default_panel(
        self,
        *,
        guild: discord.Guild,
        name: str,
        created_by: int,
        report_channel: discord.TextChannel | None,
        partner_role: discord.Role | None,
    ) -> PartnerPanel:
        panel = await self.db.create_default_panel(
            guild_id=guild.id,
            name=name,
            created_by=created_by,
            report_channel_id=report_channel.id if report_channel else None,
            partner_role_id=partner_role.id if partner_role else None,
        )
        await self.db.audit(
            guild_id=guild.id,
            actor_id=created_by,
            action="panel_created",
            target=str(panel.id),
            details=name,
        )
        return panel

    async def open_admin_panel(
        self,
        interaction: discord.Interaction,
        *,
        channel: discord.TextChannel | None = None,
        pin: bool = False,
    ) -> None:
        if interaction.guild is None:
            await _interaction_reply(interaction, "Use esse painel dentro de um servidor.")
            return
        panel = await self.db.latest_panel(interaction.guild.id)
        if panel is None:
            panel = await self.create_default_panel(
                guild=interaction.guild,
                name="Programa de Parceiros",
                created_by=interaction.user.id,
                report_channel=None,
                partner_role=None,
            )
        if not self.is_admin(interaction.user, panel):
            await _interaction_reply(interaction, "Somente Talisson ou o administrador configurado pode gerenciar esse painel.")
            return
        embed = await self.admin_embed(panel)
        view = PartnerAdminView(self, panel.id)
        if channel is not None:
            message = await channel.send(
                embed=embed,
                view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            pinned_text = ""
            if pin:
                try:
                    await message.pin(reason=f"Painel administrativo de parceiros criado por {interaction.user}")
                    pinned_text = " Também deixei ele fixado."
                except discord.HTTPException:
                    pinned_text = " Não consegui fixar automaticamente, mas o painel foi enviado."
            await self.db.audit(
                guild_id=interaction.guild.id,
                actor_id=interaction.user.id,
                action="admin_panel_published",
                target=str(panel.id),
                details=f"channel={channel.id}; message={message.id}",
            )
            await self._save_admin_panel_location(
                panel_id=panel.id,
                guild_id=interaction.guild.id,
                channel_id=channel.id,
                message_id=message.id,
            )
            await _interaction_reply(
                interaction,
                f"Painel administrativo enviado em {channel.mention}.{pinned_text}",
            )
            return
        await _interaction_reply(interaction, embed=embed, view=view)

    async def provision_partner_workspace(
        self,
        interaction: discord.Interaction,
        panel_id: int,
    ) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await _interaction_reply(interaction, "Use esse painel dentro de um servidor.")
            return
        panel = await self.db.panel(panel_id)
        if panel is None:
            await _interaction_reply(interaction, "Painel não encontrado.")
            return
        if not self.can_manage_application(interaction.user, panel):
            await _interaction_reply(interaction, "Você não pode criar a área de parceiros.")
            return
        if self.affiliate_manager is None:
            await _interaction_reply(interaction, "O módulo de afiliados não está disponível nesta instalação.")
            return
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            partner_role = interaction.guild.get_role(panel.partner_role_id or 0)
            manager_role = interaction.guild.get_role(panel.manager_role_id or 0)
            log_channels = await self.state.get_section("guild_log_channels")
            guild_logs = log_channels.get(str(interaction.guild.id), {})
            log_channel = None
            if isinstance(guild_logs, dict):
                candidate = interaction.guild.get_channel(_optional_int(guild_logs.get("partners")) or 0)
                if isinstance(candidate, discord.TextChannel):
                    log_channel = candidate
            result = await self.affiliate_manager.provision_workspace(
                guild=interaction.guild,
                actor=interaction.user,
                partner_role=partner_role,
                manager_role=manager_role,
                log_channel=log_channel,
            )
            await self.db.configure_panel(
                panel.id,
                report_channel_id=result.channel_ids["admin"],
                partner_role_id=result.partner_role_id,
            )
            current_embeds = await self.db.embeds(panel.id)
            current_description = (
                str(current_embeds[0].get("description") or "").strip()
                if current_embeds
                else ""
            )
            if not current_description or current_description == LEGACY_DEFAULT_PARTNER_PANEL_DESCRIPTION:
                await self.db.update_main_embed(
                    panel.id,
                    description=DEFAULT_PARTNER_PANEL_DESCRIPTION,
                )
            updated = await self.db.panel(panel.id)
            if updated is None:
                raise RuntimeError("O painel deixou de existir durante a configuração.")
            public_channel = interaction.guild.get_channel(result.channel_ids["public"])
            admin_channel = interaction.guild.get_channel(result.channel_ids["admin"])
            if not isinstance(public_channel, discord.TextChannel):
                raise RuntimeError("O canal público do programa não foi criado corretamente.")
            if not isinstance(admin_channel, discord.TextChannel):
                raise RuntimeError("O canal administrativo do programa não foi criado corretamente.")
            old_message = None
            old_channel = interaction.guild.get_channel(updated.channel_id or 0)
            if isinstance(old_channel, discord.TextChannel) and old_channel.id == public_channel.id and updated.message_id:
                try:
                    old_message = await old_channel.fetch_message(updated.message_id)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    old_message = None
            public_message = await self.send_or_edit_public_panel_message(
                updated.id,
                public_channel,
                old_message,
                disabled=False,
                status="published",
            )
            if (
                isinstance(old_channel, discord.TextChannel)
                and old_channel.id != public_channel.id
                and updated.message_id
            ):
                try:
                    previous = await old_channel.fetch_message(updated.message_id)
                    await previous.delete()
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass
            admin_message = await self._publish_workspace_admin_panel(updated, admin_channel)
            try:
                kwargs = await self.build_panel_message_kwargs(updated.id, disabled=False)
                self.bot.add_view(kwargs["view"])
            except Exception:
                LOGGER.warning("Não foi possível registrar o painel público provisionado %s.", updated.id)
            await self.db.audit(
                guild_id=interaction.guild.id,
                actor_id=interaction.user.id,
                action="workspace_provisioned",
                target=str(updated.id),
                details=(
                    f"category={result.category_id}; public={public_message.id}; "
                    f"admin={admin_message.id}"
                ),
            )
        except (discord.Forbidden, discord.HTTPException, PermissionError, RuntimeError) as exc:
            await interaction.followup.send(
                f"Não consegui criar a área de parceiros: {type(exc).__name__}: {exc}",
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            embed=_workspace_result_embed(result),
            ephemeral=True,
        )

    async def approved_partners(self, guild_id: int) -> list[dict[str, Any]]:
        if self.affiliate_manager is None:
            return []
        return await asyncio.to_thread(
            self.affiliate_manager.db.active_partners,
            guild_id,
        )

    async def approved_partner_profile(
        self,
        guild: discord.Guild,
        partner_id: int,
    ) -> tuple[discord.abc.User, dict[str, Any]] | None:
        if self.affiliate_manager is None:
            return None
        row = await asyncio.to_thread(
            self.affiliate_manager.db.partner,
            guild.id,
            partner_id,
        )
        if row is None or str(row.get("status")) != "active":
            return None
        user = guild.get_member(partner_id)
        if user is None:
            try:
                user = await self.bot.fetch_user(partner_id)
            except (discord.NotFound, discord.HTTPException):
                return None
        profile = await asyncio.to_thread(
            self.affiliate_manager.db.profile,
            guild.id,
            partner_id,
        )
        return user, profile

    async def add_approved_partner(
        self,
        interaction: discord.Interaction,
        *,
        panel_id: int,
        member_id: int,
        joined_at: str,
        invite_url: str,
        notes: str,
    ) -> None:
        panel = await self.db.panel(panel_id)
        if (
            interaction.guild is None
            or not self.can_manage_application(interaction.user, panel)
            or self.affiliate_manager is None
        ):
            await _interaction_reply(interaction, "Você não pode adicionar parceiros por aqui.")
            return
        member = interaction.guild.get_member(member_id)
        if member is None:
            await _interaction_reply(interaction, "A pessoa selecionada não está mais neste servidor.")
            return
        if member.bot:
            await _interaction_reply(interaction, "Bots não podem ser cadastrados como parceiros.")
            return
        try:
            joined_at_value = _parse_partner_joined_at(joined_at)
        except ValueError as exc:
            await _interaction_reply(interaction, str(exc))
            return
        invite_code = _invite_code_from_url(invite_url) if invite_url.strip() else None
        if invite_url.strip() and invite_code is None:
            await _interaction_reply(
                interaction,
                "O link personalizado precisa ser um convite Discord válido, como `https://discord.gg/codigo`.",
            )
            return
        settings = await self.affiliate_manager.settings(interaction.guild.id)
        role_id = (panel.partner_role_id if panel is not None else None) or (
            settings.partner_role_id if settings is not None else None
        )
        role = interaction.guild.get_role(role_id or 0)
        if role is None:
            await _interaction_reply(
                interaction,
                "Configure primeiro o **Cargo de parceiro** neste painel.",
            )
            return
        bot_member = interaction.guild.me
        if role.managed or bot_member is None or role >= bot_member.top_role:
            await _interaction_reply(
                interaction,
                "Não consigo atribuir o cargo configurado. Coloque o cargo da Bella acima do cargo de parceiro.",
            )
            return

        existing = await asyncio.to_thread(
            self.affiliate_manager.db.partner,
            interaction.guild.id,
            member.id,
        )
        already_active = bool(existing and str(existing.get("status")) == "active")
        added_role = role not in member.roles
        await interaction.response.defer(ephemeral=True, thinking=True)

        selected_invite = None
        guild_invites: list[discord.Invite] = []
        if invite_code is not None:
            try:
                guild_invites = await interaction.guild.invites()
            except discord.Forbidden:
                await interaction.followup.send(
                    "Não consegui validar o convite. A Bella precisa de **Gerenciar Servidor**.",
                    ephemeral=True,
                )
                return
            except discord.HTTPException:
                await interaction.followup.send(
                    "O Discord não permitiu consultar os convites agora. Tente novamente em instantes.",
                    ephemeral=True,
                )
                return
            selected_invite = discord.utils.find(
                lambda item: item.code.casefold() == invite_code.casefold(),
                guild_invites,
            )
            if selected_invite is None:
                await interaction.followup.send(
                    "Esse convite não pertence a este servidor ou não está mais ativo.",
                    ephemeral=True,
                )
                return
            if not _invite_is_permanent(selected_invite):
                await interaction.followup.send(
                    "O convite precisa estar sem expiração e sem limite de usos.",
                    ephemeral=True,
                )
                return
            assigned = await asyncio.to_thread(
                self.affiliate_manager.db.invite_by_code,
                interaction.guild.id,
                selected_invite.code,
            )
            if assigned and int(assigned.get("partner_id") or 0) != member.id:
                await interaction.followup.send(
                    "Esse convite já está vinculado a outro parceiro.",
                    ephemeral=True,
                )
                return
        if added_role:
            try:
                await member.add_roles(
                    role,
                    reason=f"Parceiro adicionado manualmente por {interaction.user}",
                )
            except (discord.Forbidden, discord.HTTPException) as exc:
                await interaction.followup.send(
                    f"Não consegui atribuir {role.mention}: {type(exc).__name__}.",
                    ephemeral=True,
                )
                return
        try:
            await asyncio.to_thread(
                self.affiliate_manager.db.upsert_partner,
                interaction.guild.id,
                member.id,
                role_id=role.id,
                joined_at=joined_at_value,
                notes=(notes.strip()[:1000] or (
                    f"Adicionado manualmente pelo painel administrativo por "
                    f"{interaction.user} ({interaction.user.id})."
                )),
            )
        except Exception:
            if added_role:
                try:
                    await member.remove_roles(role, reason="Rollback: falha ao registrar parceiro")
                except (discord.Forbidden, discord.HTTPException):
                    LOGGER.exception("Falha ao reverter cargo após erro no cadastro manual de parceiro.")
            LOGGER.exception("Falha ao cadastrar parceiro manualmente: guild=%s member=%s", interaction.guild.id, member.id)
            await interaction.followup.send(
                "Não consegui salvar o parceiro. O cargo foi revertido quando possível.",
                ephemeral=True,
            )
            return

        if selected_invite is not None:
            try:
                await asyncio.to_thread(
                    self.affiliate_manager.db.deactivate_partner_invites,
                    interaction.guild.id,
                    member.id,
                    except_code=selected_invite.code,
                )
                source_channel = getattr(selected_invite, "channel", None)
                await asyncio.to_thread(
                    self.affiliate_manager.db.save_invite,
                    guild_id=interaction.guild.id,
                    partner_id=member.id,
                    invite_code=selected_invite.code,
                    invite_url=str(selected_invite.url),
                    visual_role_id=None,
                    source_channel_id=_optional_int(getattr(source_channel, "id", None)),
                    expires_at=None,
                )
                self.affiliate_manager.invite_cache[interaction.guild.id] = {
                    item.code: int(item.uses or 0)
                    for item in guild_invites
                }
            except Exception:
                LOGGER.exception(
                    "Parceiro cadastrado, mas o convite não pôde ser persistido: guild=%s member=%s",
                    interaction.guild.id,
                    member.id,
                )
                await interaction.followup.send(
                    "O parceiro foi cadastrado, mas o link não pôde ser salvo. Tente cadastrar o link novamente pelo perfil.",
                    ephemeral=True,
                )

        await self.affiliate_manager.audit_log(
            interaction.guild,
            "partner_added_manually",
            "Parceiro adicionado manualmente pelo painel administrativo.",
            partner_id=member.id,
            actor_id=interaction.user.id,
            payload={
                "panel_id": panel_id,
                "role_id": role.id,
                "already_active": already_active,
                "joined_at": joined_at_value,
                "invite_code": selected_invite.code if selected_invite is not None else None,
            },
        )
        await self.db.audit(
            guild_id=interaction.guild.id,
            actor_id=interaction.user.id,
            action="partner_added_manually",
            target=str(member.id),
            details=(
                f"panel={panel_id}; role={role.id}; joined_at={joined_at_value}; "
                f"invite={selected_invite.code if selected_invite is not None else 'none'}; "
                f"already_active={already_active}"
            ),
        )
        profile_result = await self.approved_partner_profile(interaction.guild, member.id)
        if profile_result is None:
            await interaction.followup.send(
                f"{member.mention} foi cadastrado como parceiro.",
                ephemeral=True,
            )
            return
        partner, profile = profile_result
        await interaction.followup.send(
            content=(
                "Esse parceiro já estava ativo; cargo e cadastro foram sincronizados."
                if already_active
                else "Parceiro cadastrado e cargo atribuído com sucesso."
            ),
            embed=partner_profile_embed(interaction.guild, partner, profile),
            view=PartnerAcceptedActionView(self, panel_id, member.id),
            ephemeral=True,
        )

    async def register_partner_result(
        self,
        interaction: discord.Interaction,
        *,
        panel_id: int,
        partner_id: int,
        project_name: str,
        status: str,
        revenue: str,
        commission: str,
        notes: str,
    ) -> None:
        panel = await self.db.panel(panel_id)
        if (
            interaction.guild is None
            or not self.can_manage_application(interaction.user, panel)
            or self.affiliate_manager is None
        ):
            await _interaction_reply(interaction, "Você não pode registrar resultados por aqui.")
            return
        normalized_status = _partner_project_status(status)
        if normalized_status is None:
            await _interaction_reply(
                interaction,
                "Status inválido. Use `fechado`, `negociação` ou `cancelado`.",
            )
            return
        try:
            revenue_value = _parse_partner_money(revenue)
            commission_value = (
                _parse_partner_money(commission)
                if commission.strip()
                else revenue_value * 0.5
                if normalized_status == "closed"
                else 0.0
            )
        except ValueError:
            await _interaction_reply(
                interaction,
                "Informe os valores como `5000`, `5000,00` ou `R$ 5.000,00`.",
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        project = await asyncio.to_thread(
            self.affiliate_manager.db.register_project,
            guild_id=interaction.guild.id,
            partner_id=partner_id,
            member_id=None,
            name=project_name.strip()[:160],
            status=normalized_status,
            revenue=revenue_value,
            notes=notes.strip()[:1000],
        )
        if normalized_status == "closed" and commission_value > 0:
            await asyncio.to_thread(
                self.affiliate_manager.db.register_commission,
                guild_id=interaction.guild.id,
                partner_id=partner_id,
                amount=commission_value,
                description=f"Comissão do projeto {project_name.strip()[:160]}",
                project_id=str(project["project_id"]),
            )
        await self.affiliate_manager.audit_log(
            interaction.guild,
            "partner_result_registered",
            "Resultado comercial registrado pelo painel administrativo.",
            partner_id=partner_id,
            actor_id=interaction.user.id,
            payload={
                "project_id": project["project_id"],
                "status": normalized_status,
                "revenue": revenue_value,
                "commission": commission_value,
            },
        )
        profile_result = await self.approved_partner_profile(interaction.guild, partner_id)
        if profile_result is None:
            await interaction.followup.send("Resultado salvo.", ephemeral=True)
            return
        partner, profile = profile_result
        await interaction.followup.send(
            content="Resultado salvo e indicadores recalculados.",
            embed=partner_profile_embed(interaction.guild, partner, profile),
            view=PartnerAcceptedActionView(self, panel_id, partner_id),
            ephemeral=True,
        )

    async def mark_partner_commissions_paid(
        self,
        interaction: discord.Interaction,
        *,
        panel_id: int,
        partner_id: int,
        confirmation: str,
    ) -> None:
        panel = await self.db.panel(panel_id)
        if (
            interaction.guild is None
            or not self.can_manage_application(interaction.user, panel)
            or self.affiliate_manager is None
        ):
            await _interaction_reply(interaction, "Você não pode alterar comissões por aqui.")
            return
        if confirmation.strip().upper() != "PAGAR":
            await _interaction_reply(interaction, "Operação cancelada. Digite `PAGAR` para confirmar.")
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        paid = await asyncio.to_thread(
            self.affiliate_manager.db.pay_commission,
            guild_id=interaction.guild.id,
            partner_id=partner_id,
            amount=None,
        )
        await self.affiliate_manager.audit_log(
            interaction.guild,
            "partner_commissions_paid",
            "Comissões pendentes marcadas como pagas.",
            partner_id=partner_id,
            actor_id=interaction.user.id,
            payload={"paid": paid},
        )
        await interaction.followup.send(
            f"Comissões marcadas como pagas: **{_partner_money(paid)}**.",
            ephemeral=True,
        )

    async def deactivate_approved_partner(
        self,
        interaction: discord.Interaction,
        *,
        panel_id: int,
        partner_id: int,
        confirmation: str,
    ) -> None:
        panel = await self.db.panel(panel_id)
        if (
            interaction.guild is None
            or not self.can_manage_application(interaction.user, panel)
            or self.affiliate_manager is None
        ):
            await _interaction_reply(interaction, "Você não pode remover parceiros por aqui.")
            return
        if confirmation.strip().upper() != "REMOVER":
            await _interaction_reply(interaction, "Operação cancelada. Digite `REMOVER` para confirmar.")
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await asyncio.to_thread(
            self.affiliate_manager.db.remove_partner,
            interaction.guild.id,
            partner_id,
        )
        settings = await self.affiliate_manager.settings(interaction.guild.id)
        member = interaction.guild.get_member(partner_id)
        role = interaction.guild.get_role(settings.partner_role_id or 0) if settings else None
        if member is not None and role is not None:
            try:
                await member.remove_roles(role, reason=f"Parceiro removido por {interaction.user}")
            except (discord.Forbidden, discord.HTTPException):
                LOGGER.warning("Não foi possível remover o cargo do parceiro %s.", partner_id)
        await self.affiliate_manager.audit_log(
            interaction.guild,
            "partner_deactivated",
            "Parceiro desativado pelo painel administrativo.",
            partner_id=partner_id,
            actor_id=interaction.user.id,
        )
        await interaction.followup.send(
            f"<@{partner_id}> foi removido da lista de parceiros ativos.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _publish_workspace_admin_panel(
        self,
        panel: PartnerPanel,
        channel: discord.TextChannel,
    ) -> discord.Message:
        section = await self.state.get_section("fixed_admin_panels")
        guilds = section.get("guilds") if isinstance(section.get("guilds"), dict) else {}
        guild_panels = guilds.get(str(channel.guild.id), {}) if isinstance(guilds, dict) else {}
        entries = guild_panels.get("partner_admin", []) if isinstance(guild_panels, dict) else []
        message = None
        if isinstance(entries, list):
            for item in entries:
                if not isinstance(item, dict):
                    continue
                if (
                    _optional_int(item.get("panel_id")) == panel.id
                    and _optional_int(item.get("channel_id")) == channel.id
                ):
                    try:
                        message = await channel.fetch_message(int(item.get("message_id") or 0))
                    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                        message = None
                    break
        embed = await self.admin_embed(panel)
        view = PartnerAdminView(self, panel.id)
        if message is None:
            message = await channel.send(
                embed=embed,
                view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        else:
            await message.edit(
                embed=embed,
                view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        if not message.pinned:
            try:
                await message.pin(reason="Painel administrativo do Programa de Parceiros")
            except (discord.Forbidden, discord.HTTPException):
                LOGGER.warning("Não foi possível fixar o painel administrativo de parceiros.")
        await self._save_admin_panel_location(
            panel_id=panel.id,
            guild_id=channel.guild.id,
            channel_id=channel.id,
            message_id=message.id,
        )
        return message

    async def admin_embed(self, panel: PartnerPanel) -> discord.Embed:
        embeds = await self.db.embeds(panel.id)
        buttons = await self.db.buttons(panel.id)
        embed = discord.Embed(
            title="Programa de Parceiros",
            description=(
                "Painel administrativo do Programa de Parceiros.\n\n"
                "Por aqui você cria o painel público, visualiza o preview, publica, atualiza "
                "a mensagem original e acompanha inscrições sem transformar o fluxo em formulário."
            ),
            color=discord.Color(DEFAULT_COLOR),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(
            name="Painel",
            value=(
                f"ID: `{panel.id}`\n"
                f"Nome: **{panel.name}**\n"
                f"Status: `{panel.status}`\n"
                f"Visual: **{'Embed V2' if panel.render_mode == 'v2' else 'Embed V1'}**\n"
                f"Canal: {f'<#{panel.channel_id}>' if panel.channel_id else 'não publicado'}"
            ),
            inline=False,
        )
        embed.add_field(
            name="Configuração",
            value=(
                f"Embeds ativos: **{len(embeds)}**\n"
                f"Botões ativos: **{len(buttons)}**\n"
                f"Relatórios: {f'<#{panel.report_channel_id}>' if panel.report_channel_id else 'não configurado'}\n"
                f"Cargo de parceiro: {f'<@&{panel.partner_role_id}>' if panel.partner_role_id else 'não configurado'}\n"
                f"Cargo gestor: {f'<@&{panel.manager_role_id}>' if panel.manager_role_id else 'somente administradores'}"
            ),
            inline=False,
        )
        embed.set_footer(text="Bella Partner Program")
        return embed

    async def build_panel_embeds(self, panel_id: int) -> list[discord.Embed]:
        payload = await self.db.editor_payload(panel_id, "v1")
        raw_embeds = payload.get("embeds")
        if isinstance(raw_embeds, list) and raw_embeds:
            custom: list[discord.Embed] = []
            for raw in raw_embeds[:10]:
                if not isinstance(raw, dict):
                    continue
                draft = EmbedDraft.from_dict(raw)
                draft.buttons = []
                custom.extend(draft.to_embeds())
                if len(custom) >= 10:
                    break
            if custom:
                return custom[:10]
        result: list[discord.Embed] = []
        for raw in (await self.db.embeds(panel_id))[:10]:
            color = _safe_color(raw.get("color"), DEFAULT_COLOR)
            image_url = _valid_url_or_none(raw.get("image_url"))
            image_position = _panel_image_position(raw.get("image_position", "end"))
            title_text = _trim(raw.get("title"), 256)
            if image_url and image_position == "top":
                result.append(_partner_image_embed(image_url, color, title_text))
            embed = discord.Embed(
                title=title_text or None,
                description=_trim(raw.get("description"), 4096) or None,
                url=_valid_url_or_none(raw.get("url")),
                color=discord.Color(color),
                timestamp=datetime.now(timezone.utc) if raw.get("show_timestamp") else None,
            )
            if raw.get("author_name"):
                kwargs: dict[str, Any] = {"name": _trim(raw.get("author_name"), 256)}
                if _valid_url_or_none(raw.get("author_url")):
                    kwargs["url"] = raw.get("author_url")
                if _valid_url_or_none(raw.get("author_icon_url")):
                    kwargs["icon_url"] = raw.get("author_icon_url")
                embed.set_author(**kwargs)
            if raw.get("footer_text"):
                kwargs = {"text": _trim(raw.get("footer_text"), 2048)}
                if _valid_url_or_none(raw.get("footer_icon_url")):
                    kwargs["icon_url"] = raw.get("footer_icon_url")
                embed.set_footer(**kwargs)
            if _valid_url_or_none(raw.get("thumbnail_url")):
                embed.set_thumbnail(url=str(raw["thumbnail_url"]))
            if image_url and image_position == "end":
                embed.set_image(url=image_url)
            for field in _json_loads(str(raw.get("fields_json") or "[]"), [])[:25]:
                if not isinstance(field, dict):
                    continue
                name = _trim(field.get("name"), 256) or "\u200b"
                value = _trim(field.get("value"), 1024) or "\u200b"
                embed.add_field(name=name, value=value, inline=bool(field.get("inline", False)))
            result.append(embed)
            if image_url and image_position == "middle":
                result.append(_partner_image_embed(image_url, color, title_text))
        if not result:
            result.append(
                discord.Embed(
                    title="Programa de Parceiros",
                    description="Clique no botão para iniciar sua inscrição.",
                    color=discord.Color(DEFAULT_COLOR),
                )
            )
        return result

    async def build_panel_view(self, panel_id: int, *, disabled: bool = False) -> discord.ui.View:
        view = PartnerPanelView(self, panel_id)
        for raw in await self.db.buttons(panel_id):
            action_type = str(raw.get("action_type") or "")
            label = _trim(raw.get("label"), 80) or "Botão"
            if action_type == "url":
                config = _json_loads(str(raw.get("action_config") or "{}"), {})
                url = _valid_url_or_none(config.get("url"))
                if url and not disabled:
                    view.add_item(discord.ui.Button(label=label, style=discord.ButtonStyle.link, url=url))
                elif url and disabled:
                    view.add_item(discord.ui.Button(label=label, style=discord.ButtonStyle.secondary, disabled=True))
            elif action_type == "partner_application":
                view.add_item(
                    PartnerApplyButton(
                        service=self,
                        panel_id=panel_id,
                        label=label,
                        style=_button_style(str(raw.get("style") or "primary")),
                        disabled=disabled,
                    )
                )
        return view

    async def build_panel_v2_view(self, panel_id: int, *, disabled: bool = False) -> discord.ui.LayoutView:
        rows = await self.db.embeds(panel_id)
        buttons = await self.db.buttons(panel_id)
        color = DEFAULT_COLOR
        if rows:
            color = int(rows[0].get("color") or DEFAULT_COLOR)
        view = discord.ui.LayoutView(timeout=None)
        payload = await self.db.editor_payload(panel_id, "v2")
        message_content = _trim(payload.get("content"), 4000)
        if message_content:
            view.add_item(discord.ui.TextDisplay(message_content))
        raw_containers = payload.get("containers")
        containers = [
            V2ContainerDraft.from_dict(item)
            for item in (raw_containers or [])[:5]
            if isinstance(item, dict)
        ]
        for draft in containers:
            draft.items = [
                item
                for item in draft.items
                if item.kind not in {"link_button", "interactive_button"}
            ]
        if not containers:
            for raw in rows[:4]:
                items: list[V2ItemDraft] = []
                for field in _json_loads(str(raw.get("fields_json") or "[]"), [])[:12]:
                    if isinstance(field, dict):
                        name = _trim(field.get("name"), 120)
                        value = _trim(field.get("value"), 700)
                        if name or value:
                            items.append(V2ItemDraft(kind="text", content=f"### {name}\n{value}".strip()))
                image_url = _valid_url_or_none(raw.get("image_url"))
                if image_url:
                    media = V2ItemDraft(kind="media", url=image_url)
                    if _panel_image_position(raw.get("image_position", "end")) == "top":
                        items.insert(0, media)
                    else:
                        items.append(media)
                footer = _trim(raw.get("footer_text"), 500)
                if footer:
                    items.append(V2ItemDraft(kind="text", content=f"-# {footer}"))
                containers.append(
                    V2ContainerDraft(
                        title=_trim(raw.get("title"), 256),
                        description=_trim(raw.get("description"), 3200),
                        color=_safe_color(raw.get("color"), DEFAULT_COLOR),
                        thumbnail_url=_trim(raw.get("thumbnail_url"), 4000),
                        items=items,
                    )
                )
        if not containers:
            containers = [V2ContainerDraft(title="Programa de Parceiros", description="")]
        for draft in containers:
            view.add_item(draft.to_container(preview=False))
        container = discord.ui.Container(accent_color=discord.Color(color))
        if disabled:
            closed_title = (
                _trim(payload.get("closed_title"), 256)
                if "closed_title" in payload
                else "Inscrições pausadas no momento"
            )
            closed_text = (
                _trim(payload.get("closed_template"), 1800)
                if "closed_template" in payload
                else "Este painel está temporariamente fechado. Quando o programa for reaberto, a inscrição ficará disponível novamente."
            )
            block = "\n".join(value for value in (f"### {closed_title}" if closed_title else "", closed_text) if value)
            if block:
                container.add_item(discord.ui.TextDisplay(block))
        if container.children:
            view.add_item(container)
        built_buttons: list[discord.ui.Button[Any]] = []
        for raw in buttons[:20]:
            action_type = str(raw.get("action_type") or "")
            label = _trim(raw.get("label"), 80) or "Botão"
            if action_type == "url":
                config = _json_loads(str(raw.get("action_config") or "{}"), {})
                url = _valid_url_or_none(config.get("url"))
                if url and not disabled:
                    built_buttons.append(
                        discord.ui.Button(label=label, style=discord.ButtonStyle.link, url=url)
                    )
                elif url and disabled:
                    built_buttons.append(
                        discord.ui.Button(label=label, style=discord.ButtonStyle.secondary, disabled=True)
                    )
            elif action_type == "partner_application":
                built_buttons.append(
                    PartnerApplyButton(
                        service=self,
                        panel_id=panel_id,
                        label=label,
                        style=_button_style(str(raw.get("style") or "primary")),
                        disabled=disabled,
                    )
                )
        for start in range(0, len(built_buttons), 5):
            view.add_item(discord.ui.ActionRow(*built_buttons[start:start + 5]))
        return view

    async def build_panel_message_kwargs(
        self,
        panel_id: int,
        *,
        disabled: bool = False,
    ) -> dict[str, Any]:
        panel = await self.db.panel(panel_id)
        render_mode = str(panel.render_mode if panel else "v1").lower()
        payload = await self.db.editor_payload(panel_id, render_mode)
        content = _trim(payload.get("content"), 2000) or None
        if render_mode == "v2":
            return {
                "content": None,
                "embeds": [],
                "view": await self.build_panel_v2_view(panel_id, disabled=disabled),
                "allowed_mentions": discord.AllowedMentions.none(),
            }
        embeds = await self.build_panel_embeds(panel_id)
        if disabled and embeds:
            closed_title = (
                _trim(payload.get("closed_title"), 256)
                if "closed_title" in payload
                else "Inscrições pausadas no momento"
            )
            closed_text = (
                _trim(payload.get("closed_template"), 1024)
                if "closed_template" in payload
                else "Este painel está temporariamente fechado. Quando o programa for reaberto, o botão de inscrição ficará disponível novamente."
            )
            embeds[-1].add_field(
                name=closed_title or "\u200b",
                value=closed_text or "\u200b",
                inline=False,
            )
        return {
            "content": content,
            "embeds": embeds[:10],
            "view": await self.build_panel_view(panel_id, disabled=disabled),
            "allowed_mentions": discord.AllowedMentions.none(),
        }

    async def send_or_edit_public_panel_message(
        self,
        panel_id: int,
        channel: discord.TextChannel,
        message: discord.Message | None = None,
        *,
        disabled: bool = False,
        status: str | None = None,
    ) -> discord.Message:
        panel = await self.db.panel(panel_id)
        desired_status = status or (str(panel.status) if panel else "published")
        kwargs = await self.build_panel_message_kwargs(panel_id, disabled=disabled)
        try:
            if message is None:
                sent = await channel.send(**kwargs)
                await self.db.update_panel_publication(
                    panel_id,
                    channel_id=channel.id,
                    message_id=sent.id,
                    status=desired_status,
                )
                return sent
            await message.edit(**kwargs)
            await self.db.update_panel_publication(
                panel_id,
                channel_id=channel.id,
                message_id=message.id,
                status=desired_status,
            )
            return message
        except (discord.HTTPException, TypeError, ValueError):
            if str(panel.render_mode if panel else "v1").lower() != "v2":
                raise
            await self.db.set_panel_render_mode(panel_id, "v1")
            fallback_kwargs = await self.build_panel_message_kwargs(panel_id, disabled=disabled)
            if message is not None:
                try:
                    await message.delete()
                except discord.HTTPException:
                    pass
            sent = await channel.send(**fallback_kwargs)
            await self.db.update_panel_publication(
                panel_id,
                channel_id=channel.id,
                message_id=sent.id,
                status=desired_status,
            )
            return sent

    async def refresh_public_panel_message(self, panel_id: int) -> None:
        panel = await self.db.panel(panel_id)
        if panel is None or not panel.channel_id or not panel.message_id:
            return
        channel = self.bot.get_channel(panel.channel_id)
        if not isinstance(channel, discord.TextChannel):
            return
        disabled = panel.status != "published"
        try:
            message = await channel.fetch_message(panel.message_id)
            await self.send_or_edit_public_panel_message(
                panel_id,
                channel,
                message,
                disabled=disabled,
                status=panel.status,
            )
        except (discord.NotFound, discord.Forbidden, discord.HTTPException, TypeError, ValueError):
            LOGGER.warning("Não foi possível atualizar o painel público de parceiros %s.", panel.message_id)

    async def publish_panel(
        self,
        interaction: discord.Interaction,
        *,
        panel_id: int,
        channel: discord.TextChannel,
    ) -> None:
        panel = await self.db.panel(panel_id)
        if panel is None:
            await _interaction_reply(interaction, "Painel não encontrado.")
            return
        if not self.is_admin(interaction.user, panel):
            await _interaction_reply(interaction, "Você não pode publicar esse painel.")
            return
        if panel.message_id and panel.channel_id:
            old_channel = interaction.guild.get_channel(panel.channel_id) if interaction.guild else None
            if isinstance(old_channel, discord.TextChannel):
                try:
                    message = await old_channel.fetch_message(panel.message_id)
                    await self.send_or_edit_public_panel_message(
                        panel_id,
                        old_channel,
                        message,
                        disabled=False,
                        status="published",
                    )
                    await _interaction_reply(interaction, f"Painel atualizado em {old_channel.mention}.")
                    return
                except (discord.NotFound, discord.Forbidden, discord.HTTPException, TypeError, ValueError):
                    pass
        message = await self.send_or_edit_public_panel_message(
            panel_id,
            channel,
            None,
            disabled=False,
            status="published",
        )
        try:
            kwargs = await self.build_panel_message_kwargs(panel_id, disabled=False)
            self.bot.add_view(kwargs["view"])
        except Exception:
            LOGGER.warning("Não foi possível registrar view persistente do painel de parceiros %s.", panel_id)
        await self.db.audit(
            guild_id=channel.guild.id,
            actor_id=interaction.user.id,
            action="panel_published",
            target=str(panel_id),
            details=f"{channel.id}/{message.id}",
        )
        await _interaction_reply(interaction, f"Painel publicado em {channel.mention}.")

    async def preview_panel(self, interaction: discord.Interaction, panel_id: int) -> None:
        panel = await self.db.panel(panel_id)
        if panel is None:
            await _interaction_reply(interaction, "Painel não encontrado.")
            return
        if not self.is_admin(interaction.user, panel):
            await _interaction_reply(interaction, "Você não pode visualizar esse painel.")
            return
        kwargs = await self.build_panel_message_kwargs(panel_id)
        try:
            await _interaction_reply(
                interaction,
                content="Preview do painel público:",
                embeds=kwargs.get("embeds"),
                view=kwargs.get("view"),
            )
        except (discord.HTTPException, TypeError, ValueError):
            await self.db.set_panel_render_mode(panel_id, "v1")
            fallback = await self.build_panel_message_kwargs(panel_id)
            await _interaction_reply(
                interaction,
                content="Preview do painel público em modo estável V1:",
                embeds=fallback.get("embeds"),
                view=fallback.get("view"),
            )

    async def start_application(
        self,
        interaction: discord.Interaction,
        *,
        panel_id: int,
    ) -> None:
        if await guard_public_interaction(interaction, "parcerias"):
            return
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await _interaction_reply(interaction, "Essa inscrição precisa ser iniciada dentro do servidor.")
            return
        panel = await self.db.panel(panel_id)
        if panel is None or panel.guild_id != interaction.guild.id:
            await _interaction_reply(interaction, "Esse painel não está mais disponível.")
            return
        blocked = await self.db.application_block_for_user(interaction.guild.id, interaction.user.id)
        if blocked:
            await _interaction_reply(
                interaction,
                "No momento não consigo abrir uma nova inscrição para você. Se achar que isso é um engano, fale com a equipe.",
            )
            await self.db.audit(
                guild_id=interaction.guild.id,
                actor_id=interaction.user.id,
                action="application_blocked_attempt",
                target=str(interaction.user.id),
                details=str(blocked.get("reason") or ""),
            )
            return
        existing = await self.db.blocking_application_for_user(interaction.guild.id, interaction.user.id)
        if existing:
            ticket_id = int(existing.get("ticket_id") or 0)
            thread = interaction.guild.get_thread(ticket_id)
            status = str(existing.get("status") or "")
            if thread:
                await _interaction_reply(interaction, f"Você já possui uma inscrição em andamento em {thread.mention}.")
            elif status == "approved":
                await _interaction_reply(interaction, "Sua inscrição já consta como aprovada. Se precisar de ajuda, fale com a equipe.")
            else:
                await _interaction_reply(interaction, "Você já possui uma inscrição registrada. A equipe pode retomar o atendimento se necessário.")
            return

        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            await _interaction_reply(interaction, "Não consegui criar a thread a partir deste canal.")
            return
        bot_member = interaction.guild.me
        if bot_member is None:
            await _interaction_reply(interaction, "A Bella ainda está iniciando.")
            return
        permissions = channel.permissions_for(bot_member)
        if not permissions.create_private_threads or not permissions.send_messages_in_threads:
            await _interaction_reply(interaction, "Preciso das permissões Criar Threads Privadas e Enviar Mensagens em Threads.")
            return
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True, thinking=True)

        thread = await channel.create_thread(
            name=f"parceiro-{_slug(interaction.user.display_name)[:40]}-abrindo",
            type=discord.ChannelType.private_thread,
            invitable=False,
            reason=f"Inscrição no Programa de Parceiros por {interaction.user} ({interaction.user.id})",
        )
        await thread.add_user(interaction.user)
        if panel.admin_user_id:
            admin = interaction.guild.get_member(panel.admin_user_id)
            if admin:
                try:
                    await thread.add_user(admin)
                except discord.HTTPException:
                    pass
        application = await self.db.create_application(
            guild_id=interaction.guild.id,
            panel_id=panel.id,
            discord_user_id=interaction.user.id,
            ticket_id=thread.id,
        )
        await self._rename_application_thread(
            application,
            status="interviewing",
            member_name=interaction.user.display_name,
        )
        thread_view = PartnerApplicationThreadView(
            self,
            str(application["id"]),
            interaction.user.id,
        )
        self.bot.add_view(thread_view)
        await self.db.add_message(
            str(application["id"]),
            author_type="jarvis",
            discord_user_id=None,
            content=_opening_message(interaction.user.display_name),
        )
        opening_embed = discord.Embed(
            title="Inscrição iniciada",
            description=_opening_message(interaction.user.display_name),
            color=discord.Color(DEFAULT_COLOR),
            timestamp=datetime.now(timezone.utc),
        )
        opening_embed.add_field(
            name="Status",
            value=_application_status_label(str(application.get("status") or "interviewing")),
            inline=False,
        )
        opening_embed.set_footer(text=f"ID da inscrição: {application['id']}")
        await thread.send(
            content=interaction.user.mention,
            embed=opening_embed,
            view=thread_view,
            allowed_mentions=discord.AllowedMentions(users=[interaction.user], roles=False, everyone=False),
        )
        await interaction.followup.send(f"Sua inscrição foi aberta em {thread.mention}.", ephemeral=True)
        await self.db.audit(
            guild_id=interaction.guild.id,
            actor_id=interaction.user.id,
            action="application_started",
            target=str(application["id"]),
            details=str(thread.id),
        )

    async def handle_message(self, message: discord.Message) -> bool:
        if (
            message.guild is None
            or message.author.bot
            or not isinstance(message.channel, discord.Thread)
        ):
            return False
        application = await self.db.application_by_thread(message.channel.id)
        if not application:
            return False
        status = str(application.get("status") or "")
        candidate_id = int(application.get("discord_user_id") or 0)
        if message.author.id != candidate_id:
            return True
        if status not in {"interviewing", "awaiting_information"}:
            if (
                status == "under_review"
                and _bot_was_invoked(message, self.bot.user)
                and _looks_like_status_followup(message.content)
            ):
                await message.reply(
                    "Já deixei sua inscrição em análise. Se a equipe precisar de mais alguma coisa, ela chama por aqui.",
                    mention_author=False,
                )
            return True
        content = message.content.strip()
        if not content:
            return True
        lock = self._application_locks.setdefault(str(application["id"]), asyncio.Lock())
        async with lock:
            current = await self.db.application(str(application["id"]))
            if not current or str(current.get("status") or "") not in {"interviewing", "awaiting_information"}:
                return True
            await self.db.add_message(
                str(current["id"]),
                author_type="candidate",
                discord_user_id=message.author.id,
                content=content,
            )
            async with message.channel.typing():
                await self._continue_interview(message, current, content)
        return True

    async def _continue_interview(
        self,
        message: discord.Message,
        application: dict[str, Any],
        latest_content: str,
    ) -> None:
        application_id = str(application["id"])
        memory = _ensure_memory_schema(
            _json_loads(str(application.get("memory_json") or "{}"), _default_memory())
        )
        history = await self.db.messages(application_id, limit=12)
        official_page = await self._official_program_knowledge()
        if str(application.get("status") or "") == "awaiting_information":
            application = await self._set_application_status(application, "interviewing")
        if _is_confused_response(latest_content):
            reply = _clarification_reply(
                _last_jarvis_question(history),
                memory,
                candidate_name=message.author.display_name,
            )
            await self.db.update_application(
                application_id,
                memory_json=json.dumps(memory, ensure_ascii=False),
                status="interviewing",
            )
            await self.db.add_message(
                application_id,
                author_type="jarvis",
                discord_user_id=None,
                content=reply,
            )
            for chunk in _chunks(reply):
                await message.channel.send(chunk)
            return
        local_updates = _local_memory_updates(latest_content, memory, history=history)
        if local_updates:
            memory = _merge_memory(memory, local_updates)
        prompt = _interview_prompt(
            application=application,
            memory=memory,
            history=history,
            latest_content=latest_content,
            candidate_name=message.author.display_name,
            official_page=official_page,
        )
        try:
            data = await self.ai_service.generate_json(
                prompt,
                INTERVIEW_SCHEMA,
                max_output_tokens=650,
                timeout_seconds=10,
                model_policy="economy",
                use_cache=False,
            )
        except Exception as exc:
            LOGGER.warning("Entrevista de parceiros falhou: %s: %s", type(exc).__name__, exc)
            if (
                _essential_memory_ready(memory)
                and _local_should_finish(memory, history)
            ):
                await self.db.update_application(
                    application_id,
                    memory_json=json.dumps(memory, ensure_ascii=False),
                    status="interviewing",
                )
                await self._finish_interview(message, application_id, memory)
                return
            reply = _local_interview_reply(
                memory=memory,
                latest_content=latest_content,
                candidate_name=message.author.display_name,
                official_page=official_page,
            )
            await self.db.update_application(
                application_id,
                memory_json=json.dumps(memory, ensure_ascii=False),
                status="interviewing",
            )
            await self.db.add_message(
                application_id,
                author_type="jarvis",
                discord_user_id=None,
                content=reply,
            )
            for chunk in _chunks(reply):
                await message.channel.send(chunk)
            if (
                _essential_memory_ready(memory)
                and _local_should_finish(memory, history)
            ):
                await self._finish_interview(message, application_id, memory)
            return

        reply = neutralize_mass_mentions(_trim(data.get("reply"), 1800))
        if (
            not reply
            or _reply_is_repetitive(reply, history)
            or _reply_does_not_advance(reply, memory)
        ):
            reply = _local_interview_reply(
                memory=memory,
                latest_content=latest_content,
                candidate_name=message.author.display_name,
                official_page=official_page,
            )
        ai_updates = _safe_ai_memory_updates(
            data.get("memory_updates") if isinstance(data.get("memory_updates"), dict) else {},
            latest_content=latest_content,
            memory=memory,
            history=history,
        )
        memory = _merge_memory(
            memory,
            ai_updates,
        )
        if (
            _essential_memory_ready(memory)
            and _local_should_finish(memory, history)
        ):
            await self.db.update_application(
                application_id,
                memory_json=json.dumps(memory, ensure_ascii=False),
                status="interviewing",
            )
            await self._finish_interview(message, application_id, memory)
            return
        await self.db.update_application(
            application_id,
            memory_json=json.dumps(memory, ensure_ascii=False),
            status="interviewing",
        )
        await self.db.add_message(
            application_id,
            author_type="jarvis",
            discord_user_id=None,
            content=reply,
        )
        for chunk in _chunks(reply):
            await message.channel.send(chunk)

        if (
            bool(data.get("should_finish"))
            and _essential_memory_ready(memory)
        ):
            await self._finish_interview(message, application_id, memory)

    async def _official_program_knowledge(self) -> str:
        cached = await self.state.get(PARTNER_FAQ_CACHE_KEY, {})
        now = time.time()
        if isinstance(cached, dict):
            text = str(cached.get("text") or "").strip()
            fetched_at = float(cached.get("fetched_at") or 0)
            if text and _partner_program_knowledge_is_stale(text):
                await self.state.set(
                    PARTNER_FAQ_CACHE_KEY,
                    {
                        "url": PARTNER_PROGRAM_URL,
                        "text": PROGRAM_KNOWLEDGE,
                        "fetched_at": now,
                    },
                )
                return PROGRAM_KNOWLEDGE
            if (
                text
                and now - fetched_at < PARTNER_FAQ_CACHE_TTL_SECONDS
            ):
                return text
        page_text = await asyncio.to_thread(_fetch_partner_program_page)
        if not page_text or _partner_program_knowledge_is_stale(page_text):
            text = PROGRAM_KNOWLEDGE
        elif page_text.strip() == PROGRAM_KNOWLEDGE:
            text = page_text
        else:
            text = (
                f"{PROGRAM_KNOWLEDGE}\n\n"
                "Conteúdo complementar da página oficial:\n"
                f"{page_text}"
            )
        text = text[:8000]
        await self.state.set(
            PARTNER_FAQ_CACHE_KEY,
            {
                "url": PARTNER_PROGRAM_URL,
                "text": text,
                "fetched_at": now,
            },
        )
        return text

    async def _finish_interview(
        self,
        message: discord.Message,
        application_id: str,
        memory: dict[str, Any],
    ) -> None:
        application = await self.db.application(application_id)
        if not application or str(application.get("status") or "") == "under_review":
            return
        closing = (
            "Fechou. Já tenho contexto suficiente pra encaminhar sua inscrição para análise.\n\n"
            "Vou organizar o que você me contou e a equipe avalia com calma. "
            "Se precisarem de algum complemento, chamam você por aqui."
        )
        application = await self._set_application_status(
            application,
            "under_review",
            completed_at=_now(),
            memory_json=json.dumps(memory, ensure_ascii=False),
        )
        await self.db.add_message(application_id, author_type="jarvis", discord_user_id=None, content=closing)
        await message.channel.send(closing)
        report = await self._build_report(application_id, memory)
        await self.db.save_analysis(application_id, report)
        await self._send_internal_report(application_id, report)


    async def _build_report(self, application_id: str, memory: dict[str, Any]) -> dict[str, Any]:
        messages = await self.db.messages(application_id, limit=80)
        prompt = (
            "Gere um relatório interno da inscrição no Programa de Parceiros.\n"
            "Use apenas fatos observados na conversa e na memória. Não invente dados, não aprove e não recuse.\n"
            "Se algo não foi confirmado, diga que não foi confirmado. Não use emojis.\n"
            "Inclua quando existir: nome, faixa etária, e-mail, WhatsApp, redes sociais, área, motivação, experiência, rede de contatos, estratégia, interesse em conteúdos, oportunidade real e entendimento das regras.\n\n"
            f"Memória:\n{json.dumps(memory, ensure_ascii=False)}\n\n"
            f"Histórico:\n{_history_text(messages, limit=80)}"
        )
        try:
            data = await self.ai_service.generate_json(
                prompt,
                REPORT_SCHEMA,
                max_output_tokens=1500,
                timeout_seconds=16,
                model_policy="quality",
                use_cache=False,
            )
            return {key: str(value) for key, value in data.items() if value is not None}
        except Exception as exc:
            LOGGER.warning("Relatório de parceiros via IA falhou: %s: %s", type(exc).__name__, exc)
            return _local_report(memory, messages)

    async def _send_internal_report(self, application_id: str, report: dict[str, Any]) -> None:
        application = await self.db.application(application_id)
        if not application:
            return
        panel = await self.db.panel(int(application.get("panel_id") or 0))
        guild = self.bot.get_guild(int(application.get("guild_id") or 0))
        if guild is None:
            return
        channel = None
        if panel and panel.report_channel_id:
            maybe = guild.get_channel(panel.report_channel_id)
            if isinstance(maybe, discord.TextChannel):
                channel = maybe
        if channel is None:
            maybe_thread = guild.get_thread(int(application.get("ticket_id") or 0))
            if isinstance(maybe_thread, discord.Thread):
                channel = maybe_thread
        if channel is None:
            return
        admin_id = panel.admin_user_id if panel and panel.admin_user_id else self.owner_id
        candidate = guild.get_member(int(application["discord_user_id"]))
        content = f"<@{admin_id}>\nNova inscrição para análise: `{application_id}`"
        embed = self.application_report_embed(application, report, candidate=candidate)
        ticket_url = (
            f"https://discord.com/channels/{application['guild_id']}/{application['ticket_id']}"
            if application.get("ticket_id")
            else None
        )
        view = PartnerReviewView(self, application_id, ticket_url=ticket_url)
        self.bot.add_view(view)
        await channel.send(
            content=content,
            embed=embed,
            view=view,
            allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
        )

    def application_report_embed(
        self,
        application: dict[str, Any],
        report: dict[str, Any],
        *,
        candidate: discord.Member | None,
    ) -> discord.Embed:
        embed = discord.Embed(
            title="NOVA INSCRIÇÃO — PROGRAMA DE PARCEIROS",
            description=(
                f"ID: `{application['id']}`\n"
                f"Status: **{_application_status_label(str(application.get('status') or 'under_review'))}**\n\n"
                "A decisão final é manual. A Bella apenas organiza a conversa e destaca pontos para análise."
            ),
            color=discord.Color(DEFAULT_COLOR),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(
            name="Candidato",
            value=(
                f"{candidate.mention if candidate else f'<@{application['discord_user_id']}>'}\n"
                f"ID: `{application['discord_user_id']}`\n"
                f"Status: **{_application_status_label(str(application['status']))}**"
            ),
            inline=False,
        )
        fields = [
            ("Dados básicos", "identification"),
            ("Perfil profissional", "profile"),
            ("Experiência com clientes", "clients"),
            ("Prospecção e estratégia", "prospecting"),
            ("Rede de contatos", "networking"),
            ("Motivação", "motivation"),
            ("Interesse nos conteúdos", "learning"),
            ("Entendimento das regras", "program_understanding"),
            ("Resumo da conversa", "summary"),
            ("Pontos para análise", "attention_points"),
            ("Observações da Bella", "notes"),
        ]
        for title, key in fields:
            value = _trim(report.get(key), 1000)
            if value:
                embed.add_field(name=title, value=value, inline=False)
        scores = "\n".join(
            f"{label}: **{_trim(report.get(key), 80) or 'não avaliado'}**"
            for label, key in (
                ("Compatibilidade", "compatibility"),
                ("Comunicação", "communication"),
                ("Potencial de oportunidades", "opportunity_potential"),
                ("Entendimento", "understanding"),
            )
        )
        embed.add_field(name="Indicadores auxiliares", value=scores, inline=False)
        embed.set_footer(text="Você toma a decisão. A análise da Bella é apenas auxiliar.")
        return embed

    async def approve_application(self, interaction: discord.Interaction, application_id: str) -> None:
        application = await self.db.application(application_id)
        if not application:
            await _interaction_reply(interaction, "Inscrição não encontrada.")
            return
        panel = await self.db.panel(int(application.get("panel_id") or 0))
        if not self.is_admin(interaction.user, panel):
            await _interaction_reply(interaction, "Você não pode revisar essa inscrição.")
            return
        guild = interaction.guild
        approved_member = (
            guild.get_member(int(application["discord_user_id"]))
            if guild is not None
            else None
        )
        if guild and panel and panel.partner_role_id:
            role = guild.get_role(panel.partner_role_id)
            if approved_member and role:
                try:
                    await approved_member.add_roles(role, reason=f"Inscrição {application_id} aprovada")
                except discord.HTTPException:
                    LOGGER.warning("Não foi possível atribuir cargo de parceiro a %s.", approved_member.id)
        application = await self._set_application_status(
            application,
            "approved",
            actor_id=interaction.user.id,
            reviewed=True,
        )
        if self.affiliate_manager is not None and guild is not None and approved_member is not None:
            await asyncio.to_thread(
                self.affiliate_manager.db.upsert_partner,
                guild.id,
                approved_member.id,
                role_id=panel.partner_role_id if panel is not None else None,
            )
        await self._notify_thread(
            application,
            "Sua inscrição foi aprovada para a próxima etapa. A entrada definitiva ainda pode depender da formalização contratual e das orientações da equipe.",
        )
        await self.db.audit(
            guild_id=int(application.get("guild_id") or 0),
            actor_id=interaction.user.id,
            action="application_approved",
            target=application_id,
        )
        await _interaction_reply(interaction, "Inscrição aprovada e candidato avisado.")

    async def request_information(
        self,
        interaction: discord.Interaction,
        application_id: str,
        message: str,
    ) -> None:
        application = await self.db.application(application_id)
        if not application:
            await _interaction_reply(interaction, "Inscrição não encontrada.")
            return
        panel = await self.db.panel(int(application.get("panel_id") or 0))
        if not self.is_admin(interaction.user, panel):
            await _interaction_reply(interaction, "Você não pode revisar essa inscrição.")
            return
        application = await self._set_application_status(
            application,
            "awaiting_information",
            actor_id=interaction.user.id,
            reviewed=True,
        )
        text = message.strip() or "A equipe pediu mais algumas informações para continuar a análise. Pode complementar por aqui?"
        await self._notify_thread(application, text)
        await self.db.audit(
            guild_id=int(application.get("guild_id") or 0),
            actor_id=interaction.user.id,
            action="application_information_requested",
            target=application_id,
            details=text,
        )
        await _interaction_reply(interaction, "Pedido de informação enviado ao candidato.")

    async def reject_application(
        self,
        interaction: discord.Interaction,
        application_id: str,
        reason: str,
    ) -> None:
        application = await self.db.application(application_id)
        if not application:
            await _interaction_reply(interaction, "Inscrição não encontrada.")
            return
        panel = await self.db.panel(int(application.get("panel_id") or 0))
        if not self.is_admin(interaction.user, panel):
            await _interaction_reply(interaction, "Você não pode revisar essa inscrição.")
            return
        application = await self._set_application_status(
            application,
            "rejected",
            actor_id=interaction.user.id,
            reviewed=True,
        )
        await self._notify_thread(
            application,
            "A equipe analisou sua inscrição e, por enquanto, não vai seguir com ela. Obrigado por conversar com a gente e demonstrar interesse no programa.",
        )
        await self.db.audit(
            guild_id=int(application.get("guild_id") or 0),
            actor_id=interaction.user.id,
            action="application_rejected",
            target=application_id,
            details=reason,
        )
        await _interaction_reply(interaction, "Inscrição recusada e candidato avisado.")

    async def _notify_thread(self, application: dict[str, Any], text: str) -> None:
        guild = self.bot.get_guild(int(application.get("guild_id") or 0))
        if guild is None:
            return
        thread = guild.get_thread(int(application.get("ticket_id") or 0))
        if thread is None:
            try:
                fetched = await self.bot.fetch_channel(int(application.get("ticket_id") or 0))
                thread = fetched if isinstance(fetched, discord.Thread) else None
            except discord.HTTPException:
                thread = None
        if thread:
            await thread.send(neutralize_mass_mentions(text))

    async def mention_candidate_from_thread(
        self,
        interaction: discord.Interaction,
        application_id: str,
        candidate_id: int,
    ) -> None:
        application = await self.db.application(application_id)
        if not application:
            await _interaction_reply(interaction, "Inscrição não encontrada.")
            return
        panel = await self.db.panel(int(application.get("panel_id") or 0))
        if not self.can_manage_application(interaction.user, panel):
            await _interaction_reply(interaction, "Você não pode mencionar o candidato por aqui.")
            return
        if interaction.guild is None:
            await _interaction_reply(interaction, "Use esse botão dentro do servidor.")
            return
        member = interaction.guild.get_member(candidate_id)
        await interaction.response.send_message(
            f"<@{candidate_id}> a equipe te chamou por aqui.",
            allowed_mentions=discord.AllowedMentions(
                users=[member] if member else True,
                roles=False,
                everyone=False,
            ),
        )

    async def close_application_thread(
        self,
        interaction: discord.Interaction,
        application_id: str,
        candidate_id: int,
    ) -> None:
        application = await self.db.application(application_id)
        if not application:
            await _interaction_reply(interaction, "Inscrição não encontrada.")
            return
        panel = await self.db.panel(int(application.get("panel_id") or 0))
        is_candidate = interaction.user.id == candidate_id
        if not is_candidate and not self.can_manage_application(interaction.user, panel):
            await _interaction_reply(interaction, "Você não pode fechar essa inscrição.")
            return
        if str(application.get("status") or "") not in {"approved", "rejected", "cancelled"}:
            application = await self._set_application_status(
                application,
                "cancelled",
                actor_id=interaction.user.id,
                reviewed=not is_candidate,
            )
        await self.db.audit(
            guild_id=int(application.get("guild_id") or 0),
            actor_id=interaction.user.id,
            action="application_closed_from_thread",
            target=application_id,
            details=f"candidate={candidate_id}",
        )
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True, thinking=False)
        if isinstance(interaction.channel, discord.Thread):
            try:
                await interaction.channel.send("Inscrição encerrada. Vou arquivar esta thread.")
            except discord.HTTPException:
                pass
            try:
                await interaction.channel.edit(
                    archived=True,
                    locked=True,
                    reason=f"Inscrição {application_id} encerrada",
                )
            except discord.HTTPException:
                pass
        await interaction.followup.send("Fechado. A inscrição foi encerrada.", ephemeral=True)

    async def reset_applications_for_user(
        self,
        guild: discord.Guild,
        user: discord.abc.User,
        *,
        actor_id: int,
        reason: str = "",
        archive_threads: bool = True,
    ) -> int:
        applications = await self.db.applications_for_user(
            guild.id,
            user.id,
            statuses=BLOCKING_APPLICATION_STATUSES,
            limit=50,
        )
        if not applications:
            return 0
        now = _now()
        for application in applications:
            updated = await self._set_application_status(
                application,
                "cancelled",
                actor_id=actor_id,
                completed_at=str(application.get("completed_at") or now),
                reviewed=True,
            )
            await self.db.add_message(
                str(application["id"]),
                author_type="system",
                discord_user_id=actor_id,
                content=f"Inscrição resetada pela administração. Motivo: {reason or 'não informado'}",
            )
            await self.db.audit(
                guild_id=guild.id,
                actor_id=actor_id,
                action="application_reset",
                target=str(application["id"]),
                details=f"user={user.id}; reason={reason}",
            )
            thread = await self._thread_for_application(updated)
            if thread is None:
                continue
            try:
                await thread.send(
                    "Essa inscrição foi resetada pela administração. Se necessário, uma nova inscrição poderá ser iniciada pelo painel."
                )
            except discord.HTTPException:
                pass
            if archive_threads:
                try:
                    await thread.edit(
                        archived=True,
                        locked=True,
                        reason=f"Inscrição {application['id']} resetada",
                    )
                except discord.HTTPException:
                    pass
        return len(applications)

    async def block_applications_for_user(
        self,
        guild: discord.Guild,
        user: discord.abc.User,
        *,
        actor_id: int,
        reason: str,
    ) -> int:
        await self.db.set_application_block(
            guild.id,
            user.id,
            blocked_by=actor_id,
            reason=reason or "Bloqueado pela administração.",
        )
        reset_count = await self.reset_applications_for_user(
            guild,
            user,
            actor_id=actor_id,
            reason=f"Bloqueio administrativo: {reason or 'sem motivo informado'}",
            archive_threads=True,
        )
        await self.db.audit(
            guild_id=guild.id,
            actor_id=actor_id,
            action="application_user_blocked",
            target=str(user.id),
            details=reason,
        )
        return reset_count

    async def unblock_applications_for_user(
        self,
        guild: discord.Guild,
        user: discord.abc.User,
        *,
        actor_id: int,
    ) -> bool:
        removed = await self.db.remove_application_block(guild.id, user.id)
        await self.db.audit(
            guild_id=guild.id,
            actor_id=actor_id,
            action="application_user_unblocked",
            target=str(user.id),
            details=f"removed={removed}",
        )
        return removed

    async def admin_cancel_application(
        self,
        interaction: discord.Interaction,
        application_id: str,
        *,
        reason: str = "Encerrada pela administração.",
    ) -> None:
        application = await self.db.application(application_id)
        if not application:
            await _interaction_reply(interaction, "Inscrição não encontrada.")
            return
        panel = await self.db.panel(int(application.get("panel_id") or 0))
        if not self.can_manage_application(interaction.user, panel):
            await _interaction_reply(interaction, "Você não pode encerrar essa inscrição.")
            return
        if str(application.get("status") or "") not in {"approved", "rejected", "cancelled"}:
            application = await self._set_application_status(
                application,
                "cancelled",
                actor_id=interaction.user.id,
                completed_at=str(application.get("completed_at") or _now()),
                reviewed=True,
            )
        await self.db.add_message(
            str(application["id"]),
            author_type="system",
            discord_user_id=interaction.user.id,
            content=f"Inscrição encerrada pela administração. Motivo: {reason}",
        )
        await self.db.audit(
            guild_id=int(application.get("guild_id") or 0),
            actor_id=interaction.user.id,
            action="application_admin_cancelled",
            target=str(application["id"]),
            details=reason,
        )
        thread = await self._thread_for_application(application)
        if thread is not None:
            try:
                await thread.send(
                    "Essa inscrição foi encerrada pela equipe. Vou arquivar a conversa por aqui."
                )
            except discord.HTTPException:
                pass
            try:
                await thread.edit(
                    archived=True,
                    locked=True,
                    reason=f"Inscrição {application_id} encerrada pela administração",
                )
            except discord.HTTPException:
                pass
        await _interaction_reply(interaction, f"Inscrição `{application_id}` encerrada.")


class PartnerPanelView(discord.ui.View):
    def __init__(self, service: PartnerProgramService, panel_id: int) -> None:
        super().__init__(timeout=None)
        self.service = service
        self.panel_id = int(panel_id)


class PartnerApplyButton(discord.ui.Button):
    def __init__(
        self,
        *,
        service: PartnerProgramService,
        panel_id: int,
        label: str,
        style: discord.ButtonStyle,
        disabled: bool = False,
    ) -> None:
        super().__init__(
            label=label,
            style=style,
            custom_id=f"jarvis:partner:apply:{panel_id}",
            disabled=disabled,
        )
        self.service = service
        self.panel_id = panel_id

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.service.start_application(interaction, panel_id=self.panel_id)


class PartnerApplicationThreadView(discord.ui.View):
    def __init__(self, service: PartnerProgramService, application_id: str, candidate_id: int) -> None:
        super().__init__(timeout=None)
        self.service = service
        self.application_id = application_id
        self.candidate_id = int(candidate_id)
        mention = discord.ui.Button(
            label="Mencionar usuário",
            style=discord.ButtonStyle.secondary,
            custom_id=f"jarvis:partner:thread:mention:{application_id}:{candidate_id}",
        )
        mention.callback = self._mention_candidate  # type: ignore[method-assign]
        close = discord.ui.Button(
            label="Fechar inscrição",
            style=discord.ButtonStyle.danger,
            custom_id=f"jarvis:partner:thread:close:{application_id}:{candidate_id}",
        )
        close.callback = self._close_thread  # type: ignore[method-assign]
        self.add_item(mention)
        self.add_item(close)

    async def _mention_candidate(self, interaction: discord.Interaction) -> None:
        await self.service.mention_candidate_from_thread(
            interaction,
            self.application_id,
            self.candidate_id,
        )

    async def _close_thread(self, interaction: discord.Interaction) -> None:
        await self.service.close_application_thread(
            interaction,
            self.application_id,
            self.candidate_id,
        )


class PartnerPublicChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, service: PartnerProgramService, panel_id: int) -> None:
        self.service = service
        self.panel_id = int(panel_id)
        super().__init__(
            placeholder="Selecione o canal onde o painel público ficará",
            channel_types=[discord.ChannelType.text],
            min_values=1,
            max_values=1,
            custom_id=f"jarvis:partner:admin:channel-select:{panel_id}",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await _interaction_reply(interaction, "Use dentro de um servidor.")
            return
        channel = await _resolve_selected_text_channel(
            interaction,
            self.values[0] if self.values else None,
        )
        if channel is None:
            await _interaction_reply(interaction, "Selecione um canal de texto.")
            return
        panel = await self.service.db.panel(self.panel_id)
        if panel is None:
            await _interaction_reply(interaction, "Painel não encontrado.")
            return
        if panel.message_id and panel.channel_id and panel.channel_id != channel.id:
            old_channel = interaction.guild.get_channel(panel.channel_id)
            if isinstance(old_channel, discord.TextChannel):
                try:
                    old_message = await old_channel.fetch_message(panel.message_id)
                    await old_message.delete()
                except discord.HTTPException:
                    pass
        next_status = "published" if panel.status == "draft" else panel.status
        message = await self.service.send_or_edit_public_panel_message(
            self.panel_id,
            channel,
            None,
            disabled=next_status != "published",
            status=next_status,
        )
        await _interaction_reply(interaction, f"Painel público publicado em {message.jump_url}.")


class PartnerPublicChannelSelectView(discord.ui.View):
    def __init__(self, service: PartnerProgramService, panel_id: int) -> None:
        super().__init__(timeout=180)
        self.add_item(PartnerPublicChannelSelect(service, panel_id))


class PartnerManagerRoleSelect(discord.ui.RoleSelect):
    def __init__(self, service: PartnerProgramService, panel_id: int) -> None:
        self.service = service
        self.panel_id = int(panel_id)
        super().__init__(
            placeholder="Selecione o cargo que poderá gerenciar este painel",
            min_values=0,
            max_values=1,
            custom_id=f"jarvis:partner:admin:manager-role-select:{panel_id}",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        role = self.values[0] if self.values else None
        await self.service.db.set_manager_role(self.panel_id, role.id if isinstance(role, discord.Role) else None)
        await _interaction_reply(
            interaction,
            f"Cargo gestor definido como {role.mention}." if isinstance(role, discord.Role) else "Cargo gestor removido.",
        )


class PartnerManagerRoleSelectView(discord.ui.View):
    def __init__(self, service: PartnerProgramService, panel_id: int) -> None:
        super().__init__(timeout=180)
        self.add_item(PartnerManagerRoleSelect(service, panel_id))


def _partner_draft_from_row(
    row: dict[str, Any] | None,
    buttons: list[dict[str, Any]] | None = None,
) -> EmbedDraft:
    row = row or {}
    draft = EmbedDraft(
        title=_trim(row.get("title"), 256) or "Programa de Parceiros",
        description=_trim(row.get("description"), 4096),
        color=_safe_color(row.get("color"), DEFAULT_COLOR),
        author=_trim(row.get("author_name"), 256),
        author_icon_url=_trim(row.get("author_icon_url"), 4000),
        footer=_trim(row.get("footer_text"), 2048),
        footer_icon_url=_trim(row.get("footer_icon_url"), 4000),
        thumbnail_url=_trim(row.get("thumbnail_url"), 4000),
        image_url=_trim(row.get("image_url"), 4000),
        image_position=_panel_image_position(row.get("image_position", "end")),
        timestamp=bool(row.get("show_timestamp", False)),
        fields=[
            DraftField.from_dict(item)
            for item in _json_loads(str(row.get("fields_json") or "[]"), [])
            if isinstance(item, dict)
        ][:25],
    )
    for button in buttons or []:
        if str(button.get("action_type") or "") != "url":
            continue
        config = _json_loads(str(button.get("action_config") or "{}"), {})
        url = _valid_url_or_none(config.get("url"))
        if url:
            draft.buttons.append(
                DraftButton(
                    label=_trim(button.get("label"), 80) or "Abrir link",
                    url=url,
                    emoji=_trim(button.get("emoji"), 80) or None,
                )
            )
    return draft


async def _save_partner_embed_editor(
    service: PartnerProgramService,
    panel_id: int,
    interaction: discord.Interaction,
    session: EmbedEditorSession,
) -> str:
    for draft in session.drafts:
        draft.buttons = []
    await service.db.save_editor_payload(
        panel_id,
        "v1",
        {
            "content": session.message_content,
            "embeds": [draft.to_dict() for draft in session.drafts[:10]],
        },
    )
    await service.db.replace_main_embed(
        panel_id,
        draft=session.drafts[0],
        show_timestamp=session.drafts[0].timestamp,
    )
    await service.refresh_public_panel_message(panel_id)
    return "Embed V1 do Programa de Parceiros salvo. O painel público também foi atualizado."


def _partner_v2_session(
    service: PartnerProgramService,
    panel_id: int,
    interaction: discord.Interaction,
    draft: EmbedDraft,
    payload: dict[str, Any] | None = None,
) -> V2EditorSession:
    async def save_callback(
        save_interaction: discord.Interaction,
        session: V2EditorSession,
    ) -> str:
        for container in session.containers:
            container.items = [
                item
                for item in container.items
                if item.kind not in {"link_button", "interactive_button"}
            ]
        await service.db.save_editor_payload(panel_id, "v2", session.export_payload())
        await service.refresh_public_panel_message(panel_id)
        return "Container V2 do Programa de Parceiros salvo. O painel público também foi atualizado."

    session = V2EditorSession(
        interaction.user.id,
        interaction.channel or interaction,
        ai_generator=service.generate_panel_embed_payload,
        save_callback=save_callback,
        allow_component_buttons=False,
    )
    if isinstance(payload, dict) and isinstance(payload.get("containers"), list) and payload.get("containers"):
        session.apply_payload(payload)
        return session
    container = V2ContainerDraft(
        title=draft.title or "Programa de Parceiros",
        description=draft.description,
        color=draft.color,
        thumbnail_url=draft.thumbnail_url,
        items=[],
    )
    if draft.author:
        container.items.append(V2ItemDraft(kind="text", content=f"-# {draft.author}"))
    if draft.image_url and draft.image_position == "top":
        container.items.append(V2ItemDraft(kind="media", url=draft.image_url))
    for field in draft.fields[:12]:
        container.items.append(V2ItemDraft(kind="text", content=f"### {field.name}\n{field.value}".strip()))
        container.items.append(V2ItemDraft(kind="separator"))
    if draft.image_url and draft.image_position != "top":
        container.items.append(V2ItemDraft(kind="media", url=draft.image_url))
    for button in draft.buttons[:5]:
        container.items.append(V2ItemDraft(kind="link_button", label=button.label, url=button.url, emoji=button.emoji))
    if draft.footer:
        container.items.append(V2ItemDraft(kind="text", content=f"-# {draft.footer}"))
    session.containers = [container]
    return session


class PartnerClosedBlockModal(discord.ui.Modal, title="Personalizar painel pausado"):
    def __init__(
        self,
        service: PartnerProgramService,
        panel: PartnerPanel,
        payload: dict[str, Any],
    ) -> None:
        super().__init__(timeout=None)
        self.service = service
        self.panel = panel
        self.title_text = discord.ui.TextInput(
            label="Título (pode ficar vazio)",
            default=str(payload.get("closed_title") or "Inscrições pausadas no momento")[:256],
            required=False,
            max_length=256,
        )
        self.template = discord.ui.TextInput(
            label="Conteúdo do aviso",
            default=str(payload.get("closed_template") or "Este painel está temporariamente fechado.")[:1800],
            required=False,
            max_length=1800,
            style=discord.TextStyle.paragraph,
        )
        self.add_item(self.title_text)
        self.add_item(self.template)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        payload = await self.service.db.editor_payload(self.panel.id, self.panel.render_mode)
        payload["closed_title"] = str(self.title_text).strip()
        payload["closed_template"] = str(self.template).strip()
        await self.service.db.save_editor_payload(self.panel.id, self.panel.render_mode, payload)
        await self.service.refresh_public_panel_message(self.panel.id)
        await _interaction_reply(interaction, "Aviso do painel pausado atualizado.")



class PartnerAdminView(discord.ui.View):
    def __init__(self, service: PartnerProgramService, panel_id: int) -> None:
        super().__init__(timeout=None)
        self.service = service
        self.panel_id = int(panel_id)
        buttons = (
            ("preview", "Preview público", discord.ButtonStyle.primary, 0, self._preview),
            ("applications", "Inscrições", discord.ButtonStyle.success, 0, self._applications),
            ("edit", "Editar texto", discord.ButtonStyle.secondary, 0, self._edit_text),
            ("config", "Configurar", discord.ButtonStyle.secondary, 0, self._configure),
            ("render", "Alternar V1/V2", discord.ButtonStyle.secondary, 0, self._toggle_render),
            ("publish", "Publicar aqui", discord.ButtonStyle.success, 1, self._publish),
            ("blocks", "Bloqueios", discord.ButtonStyle.secondary, 1, self._blocks),
            ("refresh", "Atualizar painel", discord.ButtonStyle.secondary, 1, self._refresh_panel),
            ("duplicate", "Duplicar", discord.ButtonStyle.secondary, 1, self._duplicate),
            ("deactivate", "Desativar", discord.ButtonStyle.danger, 1, self._deactivate),
            ("metrics", "Métricas", discord.ButtonStyle.secondary, 2, self._metrics),
            ("guide", "Como usar", discord.ButtonStyle.secondary, 2, self._guide),
            ("channel", "Canal público", discord.ButtonStyle.secondary, 2, self._channel),
            ("manager-role", "Cargo gestor", discord.ButtonStyle.secondary, 2, self._manager_role),
            ("workspace", "Criar/sincronizar canais", discord.ButtonStyle.success, 3, self._workspace),
            ("partners", "Parceiros aprovados", discord.ButtonStyle.primary, 3, self._partners),
            ("add-partner", "Adicionar parceiro", discord.ButtonStyle.success, 3, self._add_partner),
            ("analytics", "Relatórios", discord.ButtonStyle.primary, 4, self._analytics),
            ("closed-block", "Painel pausado", discord.ButtonStyle.secondary, 4, self._closed_block),
        )
        for action, label, style, row, callback in buttons:
            button = discord.ui.Button(
                label=label,
                style=style,
                row=row,
                custom_id=f"jarvis:partner:admin:{action}:{self.panel_id}",
            )
            button.callback = callback  # type: ignore[method-assign]
            self.add_item(button)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        panel = await self.service.db.panel(self.panel_id)
        if panel is None:
            await _interaction_reply(interaction, "Painel não encontrado.")
            return False
        if self.service.can_manage_application(interaction.user, panel):
            return True
        await _interaction_reply(interaction, "Somente Talisson, o administrador configurado ou a equipe autorizada pode usar este painel.")
        return False

    async def _preview(self, interaction: discord.Interaction) -> None:
        await self.service.preview_panel(interaction, self.panel_id)

    async def _edit_text(self, interaction: discord.Interaction) -> None:
        panel = await self.service.db.panel(self.panel_id)
        rows = await self.service.db.embeds(self.panel_id)
        draft = _partner_draft_from_row(rows[0] if rows else None)
        if panel is not None and panel.render_mode == "v2":
            payload = await self.service.db.editor_payload(self.panel_id, "v2")
            session = _partner_v2_session(self.service, self.panel_id, interaction, draft, payload)
            await interaction.response.send_message(view=session.editor_view(), ephemeral=True)
            return
        payload = await self.service.db.editor_payload(self.panel_id, "v1")
        raw_embeds = payload.get("embeds")
        drafts = [
            EmbedDraft.from_dict(item)
            for item in (raw_embeds or [])[:10]
            if isinstance(item, dict)
        ]
        if not drafts:
            drafts = [_partner_draft_from_row(row) for row in rows[:10]] or [draft]
        for item in drafts:
            item.buttons = []
        session = EmbedEditorSession(
            interaction.user.id,
            interaction.channel or interaction,
            drafts=drafts,
            message_content=_trim(payload.get("content"), 2000),
            ai_generator=self.service.generate_panel_embed_payload,
            save_callback=lambda save_interaction, editor_session: _save_partner_embed_editor(
                self.service,
                self.panel_id,
                save_interaction,
                editor_session,
            ),
            allow_link_buttons=False,
        )
        await interaction.response.send_message(
            content="Pré-visualização do editor do Programa de Parceiros.",
            embeds=session.editor_embeds(),
            view=session.preview_view(),
            ephemeral=True,
        )

    async def _configure(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(PartnerPanelConfigModal(self.service, self.panel_id))

    async def _channel(self, interaction: discord.Interaction) -> None:
        await _interaction_reply(
            interaction,
            "Escolha o canal onde o painel público de parceiros deve ficar.",
            view=PartnerPublicChannelSelectView(self.service, self.panel_id),
        )

    async def _manager_role(self, interaction: discord.Interaction) -> None:
        await _interaction_reply(
            interaction,
            "Escolha o cargo que poderá gerenciar este painel além de Talisson/admins.",
            view=PartnerManagerRoleSelectView(self.service, self.panel_id),
        )

    async def _workspace(self, interaction: discord.Interaction) -> None:
        await self.service.provision_partner_workspace(interaction, self.panel_id)

    async def _partners(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or self.service.affiliate_manager is None:
            await _interaction_reply(interaction, "O módulo de parceiros aprovados não está disponível.")
            return
        partners = await self.service.approved_partners(interaction.guild.id)
        if not partners:
            await _interaction_reply(interaction, "Ainda não há parceiros aprovados ativos neste servidor.")
            return
        embed = discord.Embed(
            title="Parceiros aprovados",
            description=(
                "Selecione um parceiro para consultar indicações, projetos, receita, comissões, "
                "lucro líquido estimado e ações administrativas."
            ),
            color=discord.Color(DEFAULT_COLOR),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Ativos", value=str(len(partners)), inline=True)
        await _interaction_reply(
            interaction,
            embed=embed,
            view=PartnerAcceptedListView(self.service, self.panel_id, interaction.guild, partners),
        )

    async def _add_partner(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or self.service.affiliate_manager is None:
            await _interaction_reply(interaction, "O módulo de parceiros aprovados não está disponível.")
            return
        await _interaction_reply(
            interaction,
            "Selecione a pessoa que será cadastrada como parceira. A Bella mostrará uma confirmação antes de atribuir o cargo.",
            view=PartnerManualAddView(self.service, self.panel_id),
        )

    async def _analytics(self, interaction: discord.Interaction) -> None:
        if self.service.analytics is None:
            await _interaction_reply(interaction, "O Bella Partner Analytics não está disponível.")
            return
        await self.service.analytics.open_center(interaction, self.panel_id)

    async def _toggle_render(self, interaction: discord.Interaction) -> None:
        panel = await self.service.db.panel(self.panel_id)
        if panel is None:
            await _interaction_reply(interaction, "Painel não encontrado.")
            return
        next_mode = "v1" if panel.render_mode == "v2" else "v2"
        await self.service.db.set_panel_render_mode(self.panel_id, next_mode)
        await self.service.refresh_public_panel_message(self.panel_id)
        updated = await self.service.db.panel(self.panel_id)
        await interaction.response.edit_message(
            embed=await self.service.admin_embed(updated or panel),
            view=PartnerAdminView(self.service, self.panel_id),
        )

    async def _closed_block(self, interaction: discord.Interaction) -> None:
        panel = await self.service.db.panel(self.panel_id)
        if panel is None:
            await _interaction_reply(interaction, "Painel não encontrado.")
            return
        payload = await self.service.db.editor_payload(self.panel_id, panel.render_mode)
        await interaction.response.send_modal(PartnerClosedBlockModal(self.service, panel, payload))

    async def _publish(self, interaction: discord.Interaction) -> None:
        panel = await self.service.db.panel(self.panel_id)
        if panel is None:
            await _interaction_reply(interaction, "Painel não encontrado.")
            return
        if not isinstance(interaction.channel, discord.TextChannel):
            await _interaction_reply(interaction, "Use esse botão em um canal de texto.")
            return
        await self.service.publish_panel(interaction, panel_id=self.panel_id, channel=interaction.channel)

    async def _applications(self, interaction: discord.Interaction) -> None:
        panel = await self.service.db.panel(self.panel_id)
        if panel is None or interaction.guild is None:
            await _interaction_reply(interaction, "Painel não encontrado.")
            return
        applications = await self.service.db.applications(interaction.guild.id, limit=25)
        if not applications:
            await _interaction_reply(interaction, "Ainda não existe inscrição registrada nesse servidor.")
            return
        embed = discord.Embed(
            title="Inscrições do Programa de Parceiros",
            description="Selecione uma inscrição abaixo para abrir ações como aprovar, solicitar informações, recusar, resetar ou fechar.",
            color=discord.Color(DEFAULT_COLOR),
            timestamp=datetime.now(timezone.utc),
        )
        lines = []
        for application in applications[:10]:
            lines.append(
                f"`{application['id']}` — <@{application['discord_user_id']}> — "
                f"{_application_status_label(str(application.get('status') or ''))}"
            )
        embed.add_field(name="Recentes", value="\n".join(lines), inline=False)
        view = PartnerApplicationListView(self.service, self.panel_id, applications)
        await _interaction_reply(interaction, embed=embed, view=view)

    async def _blocks(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await _interaction_reply(interaction, "Use esse painel dentro do servidor.")
            return
        blocks = await self.service.db.application_blocks(interaction.guild.id, limit=20)
        if not blocks:
            await _interaction_reply(interaction, "Nenhum usuário bloqueado para inscrições.")
            return
        lines = []
        for item in blocks:
            user_id = int(item.get("discord_user_id") or 0)
            reason = str(item.get("reason") or "Sem motivo informado.")
            lines.append(f"<@{user_id}> (`{user_id}`)\nMotivo: {reason[:180]}")
        await _interaction_reply(interaction, "\n\n".join(lines))

    async def _metrics(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await _interaction_reply(interaction, "Use esse painel dentro do servidor.")
            return
        applications = await self.service.db.applications(interaction.guild.id, limit=200)
        blocks = await self.service.db.application_blocks(interaction.guild.id, limit=200)
        by_status: dict[str, int] = {}
        for application in applications:
            status = str(application.get("status") or "unknown")
            by_status[status] = by_status.get(status, 0) + 1
        status_lines = [
            f"**{_application_status_label(status)}:** {count}"
            for status, count in sorted(by_status.items())
        ]
        embed = discord.Embed(
            title="Métricas do Programa de Parceiros",
            description="Resumo rápido das inscrições e bloqueios deste servidor.",
            color=discord.Color(DEFAULT_COLOR),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Total de inscrições", value=str(len(applications)), inline=True)
        embed.add_field(name="Bloqueios ativos", value=str(len(blocks)), inline=True)
        embed.add_field(name="Estados", value="\n".join(status_lines) or "Sem inscrições ainda.", inline=False)
        if self.service.affiliate_manager is not None:
            dashboard = await asyncio.to_thread(
                self.service.affiliate_manager.db.dashboard,
                interaction.guild.id,
            )
            revenue = float(dashboard.get("revenue", 0) or 0)
            pending = float(dashboard.get("commission_pending", 0) or 0)
            paid = float(dashboard.get("commission_paid", 0) or 0)
            embed.add_field(
                name="Operação dos aprovados",
                value=(
                    f"Parceiros ativos: **{len(await self.service.approved_partners(interaction.guild.id))}**\n"
                    f"Receita: **{_partner_money(revenue)}**\n"
                    f"Comissões: **{_partner_money(pending + paid)}**\n"
                    f"Lucro líquido estimado: **{_partner_money(max(0.0, revenue - pending - paid))}**"
                ),
                inline=False,
            )
        await _interaction_reply(interaction, embed=embed)

    async def _guide(self, interaction: discord.Interaction) -> None:
        await _interaction_reply(
            interaction,
            (
                "**Fluxo recomendado**\n"
                "1. Configure texto, cargo e canal de relatório.\n"
                "2. Use **Preview público** para conferir a mensagem.\n"
                "3. Use **Publicar aqui** no canal onde o painel público deve ficar.\n"
                "4. Acompanhe candidaturas em **Inscrições**.\n"
                "5. Use **Desativar** para fechar temporariamente o botão público.\n\n"
                "Se V2 der erro no Discord, a Bella troca o painel público para V1 automaticamente e salva o novo ID."
            ),
        )

    async def _refresh_panel(self, interaction: discord.Interaction) -> None:
        panel = await self.service.db.panel(self.panel_id)
        if panel is None:
            await _interaction_reply(interaction, "Painel não encontrado.")
            return
        embed = await self.service.admin_embed(panel)
        if interaction.response.is_done():
            await interaction.edit_original_response(embed=embed, view=self)
        else:
            await interaction.response.edit_message(embed=embed, view=self)

    async def _duplicate(self, interaction: discord.Interaction) -> None:
        new_id = await self.service.db.duplicate_panel(self.panel_id, interaction.user.id)
        await _interaction_reply(interaction, f"Painel duplicado com ID `{new_id}`." if new_id else "Não consegui duplicar o painel.")

    async def _deactivate(self, interaction: discord.Interaction) -> None:
        await self.service.db.set_panel_status(self.panel_id, "inactive")
        await self.service.refresh_public_panel_message(self.panel_id)
        await _interaction_reply(interaction, "Painel marcado como inativo e inscrição bloqueada no painel público.")


class PartnerManualAddView(discord.ui.View):
    def __init__(self, service: PartnerProgramService, panel_id: int) -> None:
        super().__init__(timeout=300)
        self.service = service
        self.panel_id = int(panel_id)
        self.add_item(PartnerManualMemberSelect(self))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        panel = await self.service.db.panel(self.panel_id)
        if self.service.can_manage_application(interaction.user, panel):
            return True
        await _interaction_reply(interaction, "Você não pode adicionar parceiros por aqui.")
        return False


class PartnerManualMemberSelect(discord.ui.UserSelect):
    def __init__(self, parent_view: PartnerManualAddView) -> None:
        self.parent_view = parent_view
        super().__init__(
            placeholder="Selecione uma pessoa do servidor",
            min_values=1,
            max_values=1,
            custom_id=f"jarvis:partner:manual:member:{parent_view.panel_id}",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await _interaction_reply(interaction, "Use este painel dentro do servidor.")
            return
        selected = self.values[0]
        member = interaction.guild.get_member(selected.id)
        if member is None:
            await _interaction_reply(interaction, "Selecione uma pessoa que esteja neste servidor.")
            return
        if member.bot:
            await _interaction_reply(interaction, "Bots não podem ser cadastrados como parceiros.")
            return
        embed = discord.Embed(
            title="Confirmar novo parceiro",
            description=(
                f"Você está prestes a cadastrar **{member.display_name}** (`{member.id}`) como parceiro.\n\n"
                "A Bella atribuirá o cargo configurado, ativará o perfil financeiro e registrará a ação na auditoria."
            ),
            color=discord.Color(DEFAULT_COLOR),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        await interaction.response.edit_message(
            content=None,
            embed=embed,
            view=PartnerManualAddConfirmView(
                self.parent_view.service,
                self.parent_view.panel_id,
                member.id,
            ),
        )


class PartnerManualAddConfirmView(discord.ui.View):
    def __init__(self, service: PartnerProgramService, panel_id: int, member_id: int) -> None:
        super().__init__(timeout=300)
        self.service = service
        self.panel_id = int(panel_id)
        self.member_id = int(member_id)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        panel = await self.service.db.panel(self.panel_id)
        if self.service.can_manage_application(interaction.user, panel):
            return True
        await _interaction_reply(interaction, "Você não pode confirmar este cadastro.")
        return False

    @discord.ui.button(label="Confirmar parceiro", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(
            PartnerManualDetailsModal(
                self.service,
                self.panel_id,
                self.member_id,
            )
        )

    @discord.ui.button(label="Cancelar", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.edit_message(
            content="Cadastro cancelado.",
            embed=None,
            view=None,
        )


class PartnerManualDetailsModal(discord.ui.Modal, title="Cadastrar parceiro"):
    joined_at = discord.ui.TextInput(
        label="Data de entrada",
        placeholder="DD/MM/AAAA",
        max_length=10,
    )
    invite_url = discord.ui.TextInput(
        label="Link personalizado permanente",
        placeholder="https://discord.gg/codigo",
        max_length=200,
        required=False,
    )
    notes = discord.ui.TextInput(
        label="Observações internas",
        placeholder="Contrato, origem da parceria ou contexto importante",
        style=discord.TextStyle.paragraph,
        max_length=1000,
        required=False,
    )

    def __init__(self, service: PartnerProgramService, panel_id: int, member_id: int) -> None:
        super().__init__()
        self.service = service
        self.panel_id = int(panel_id)
        self.member_id = int(member_id)
        self.joined_at.default = datetime.now(timezone.utc).strftime("%d/%m/%Y")

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.service.add_approved_partner(
            interaction,
            panel_id=self.panel_id,
            member_id=self.member_id,
            joined_at=str(self.joined_at.value),
            invite_url=str(self.invite_url.value),
            notes=str(self.notes.value),
        )


class PartnerAcceptedListView(discord.ui.View):
    def __init__(
        self,
        service: PartnerProgramService,
        panel_id: int,
        guild: discord.Guild,
        partners: list[dict[str, Any]],
    ) -> None:
        super().__init__(timeout=300)
        self.service = service
        self.panel_id = int(panel_id)
        self.guild = guild
        self.partners = partners[:25]
        self.add_item(PartnerAcceptedSelect(self))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        panel = await self.service.db.panel(self.panel_id)
        if self.service.can_manage_application(interaction.user, panel):
            return True
        await _interaction_reply(interaction, "Você não pode gerenciar parceiros por aqui.")
        return False


class PartnerAcceptedSelect(discord.ui.Select):
    def __init__(self, parent_view: PartnerAcceptedListView) -> None:
        self.parent_view = parent_view
        options: list[discord.SelectOption] = []
        for row in parent_view.partners:
            partner_id = int(row.get("partner_id") or 0)
            member = parent_view.guild.get_member(partner_id)
            name = getattr(member, "display_name", None) or f"Usuário {partner_id}"
            options.append(
                discord.SelectOption(
                    label=_trim(str(name), 100),
                    description=_trim(
                        f"ID {partner_id} • aprovado em {str(row.get('joined_at') or '')[:10]}",
                        100,
                    ),
                    value=str(partner_id),
                )
            )
        super().__init__(
            placeholder="Escolha um parceiro aprovado",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        partner_id = int(self.values[0])
        if interaction.guild is None:
            await _interaction_reply(interaction, "Use este painel dentro do servidor.")
            return
        result = await self.parent_view.service.approved_partner_profile(
            interaction.guild,
            partner_id,
        )
        if result is None:
            await _interaction_reply(interaction, "Esse parceiro não está mais ativo.")
            return
        partner, profile = result
        await interaction.response.edit_message(
            embed=partner_profile_embed(interaction.guild, partner, profile),
            view=PartnerAcceptedActionView(
                self.parent_view.service,
                self.parent_view.panel_id,
                partner_id,
            ),
        )


class PartnerAcceptedActionView(discord.ui.View):
    def __init__(
        self,
        service: PartnerProgramService,
        panel_id: int,
        partner_id: int,
    ) -> None:
        super().__init__(timeout=300)
        self.service = service
        self.panel_id = int(panel_id)
        self.partner_id = int(partner_id)
        actions = (
            ("refresh", "Atualizar", discord.ButtonStyle.secondary, 0, self._refresh),
            ("result", "Registrar resultado", discord.ButtonStyle.success, 0, self._result),
            ("history", "Ver lançamentos", discord.ButtonStyle.primary, 0, self._history),
            ("paid", "Marcar comissões pagas", discord.ButtonStyle.secondary, 1, self._paid),
            ("remove", "Remover parceiro", discord.ButtonStyle.danger, 1, self._remove),
            ("back", "Voltar", discord.ButtonStyle.secondary, 1, self._back),
        )
        for action, label, style, row, callback in actions:
            button = discord.ui.Button(
                label=label,
                style=style,
                row=row,
                custom_id=f"jarvis:partner:accepted:{action}:{self.partner_id}",
            )
            button.callback = callback  # type: ignore[method-assign]
            self.add_item(button)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        panel = await self.service.db.panel(self.panel_id)
        if self.service.can_manage_application(interaction.user, panel):
            return True
        await _interaction_reply(interaction, "Você não pode gerenciar esse parceiro.")
        return False

    async def _refresh(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await _interaction_reply(interaction, "Use este painel dentro do servidor.")
            return
        result = await self.service.approved_partner_profile(
            interaction.guild,
            self.partner_id,
        )
        if result is None:
            await _interaction_reply(interaction, "Esse parceiro não está mais ativo.")
            return
        partner, profile = result
        await interaction.response.edit_message(
            embed=partner_profile_embed(interaction.guild, partner, profile),
            view=PartnerAcceptedActionView(self.service, self.panel_id, self.partner_id),
        )

    async def _result(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(
            PartnerFinancialResultModal(self.service, self.panel_id, self.partner_id)
        )

    async def _history(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or self.service.affiliate_manager is None:
            await _interaction_reply(interaction, "Histórico financeiro indisponível.")
            return
        projects, commissions = await asyncio.gather(
            asyncio.to_thread(
                self.service.affiliate_manager.db.projects_for_partner,
                interaction.guild.id,
                self.partner_id,
                limit=10,
            ),
            asyncio.to_thread(
                self.service.affiliate_manager.db.commissions_for_partner,
                interaction.guild.id,
                self.partner_id,
                limit=10,
            ),
        )
        await interaction.response.edit_message(
            embed=_partner_financial_history_embed(
                interaction.guild,
                self.partner_id,
                projects,
                commissions,
            ),
            view=PartnerAcceptedActionView(self.service, self.panel_id, self.partner_id),
        )

    async def _paid(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(
            PartnerCommissionPaymentModal(self.service, self.panel_id, self.partner_id)
        )

    async def _remove(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(
            PartnerDeactivateModal(self.service, self.panel_id, self.partner_id)
        )

    async def _back(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await _interaction_reply(interaction, "Use este painel dentro do servidor.")
            return
        partners = await self.service.approved_partners(interaction.guild.id)
        if not partners:
            await interaction.response.edit_message(
                content="Não há parceiros aprovados ativos.",
                embed=None,
                view=None,
            )
            return
        embed = discord.Embed(
            title="Parceiros aprovados",
            description="Selecione um parceiro para consultar e gerenciar.",
            color=discord.Color(DEFAULT_COLOR),
            timestamp=datetime.now(timezone.utc),
        )
        await interaction.response.edit_message(
            embed=embed,
            view=PartnerAcceptedListView(
                self.service,
                self.panel_id,
                interaction.guild,
                partners,
            ),
        )


class PartnerFinancialResultModal(discord.ui.Modal, title="Registrar resultado do parceiro"):
    project_name = discord.ui.TextInput(
        label="Projeto",
        placeholder="Ex.: Site institucional",
        max_length=160,
    )
    status = discord.ui.TextInput(
        label="Status",
        placeholder="fechado, negociação ou cancelado",
        max_length=30,
    )
    revenue = discord.ui.TextInput(
        label="Valor do projeto",
        placeholder="Ex.: 5000,00",
        max_length=40,
    )
    commission = discord.ui.TextInput(
        label="Comissão (vazio = 50% se fechado)",
        placeholder="Ex.: 2500,00",
        max_length=40,
        required=False,
    )
    notes = discord.ui.TextInput(
        label="Detalhes",
        style=discord.TextStyle.paragraph,
        max_length=1000,
        required=False,
    )

    def __init__(self, service: PartnerProgramService, panel_id: int, partner_id: int) -> None:
        super().__init__()
        self.service = service
        self.panel_id = int(panel_id)
        self.partner_id = int(partner_id)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.service.register_partner_result(
            interaction,
            panel_id=self.panel_id,
            partner_id=self.partner_id,
            project_name=str(self.project_name.value),
            status=str(self.status.value),
            revenue=str(self.revenue.value),
            commission=str(self.commission.value),
            notes=str(self.notes.value),
        )


class PartnerCommissionPaymentModal(discord.ui.Modal, title="Confirmar pagamento de comissões"):
    confirmation = discord.ui.TextInput(
        label="Digite PAGAR",
        placeholder="PAGAR",
        max_length=20,
    )

    def __init__(self, service: PartnerProgramService, panel_id: int, partner_id: int) -> None:
        super().__init__()
        self.service = service
        self.panel_id = int(panel_id)
        self.partner_id = int(partner_id)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.service.mark_partner_commissions_paid(
            interaction,
            panel_id=self.panel_id,
            partner_id=self.partner_id,
            confirmation=str(self.confirmation.value),
        )


class PartnerDeactivateModal(discord.ui.Modal, title="Remover parceiro aprovado"):
    confirmation = discord.ui.TextInput(
        label="Digite REMOVER",
        placeholder="REMOVER",
        max_length=20,
    )

    def __init__(self, service: PartnerProgramService, panel_id: int, partner_id: int) -> None:
        super().__init__()
        self.service = service
        self.panel_id = int(panel_id)
        self.partner_id = int(partner_id)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.service.deactivate_approved_partner(
            interaction,
            panel_id=self.panel_id,
            partner_id=self.partner_id,
            confirmation=str(self.confirmation.value),
        )


class PartnerApplicationListView(discord.ui.View):
    def __init__(self, service: PartnerProgramService, panel_id: int, applications: list[dict[str, Any]]) -> None:
        super().__init__(timeout=300)
        self.service = service
        self.panel_id = int(panel_id)
        self.applications = applications[:25]
        self.add_item(PartnerApplicationSelect(self))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        panel = await self.service.db.panel(self.panel_id)
        if self.service.can_manage_application(interaction.user, panel):
            return True
        await _interaction_reply(interaction, "Você não pode gerenciar inscrições por aqui.")
        return False


class PartnerApplicationSelect(discord.ui.Select):
    def __init__(self, view: PartnerApplicationListView) -> None:
        self.parent_view = view
        options: list[discord.SelectOption] = []
        for application in view.applications[:25]:
            application_id = str(application.get("id") or "PAR")
            status = _application_status_label(str(application.get("status") or ""))
            user_id = int(application.get("discord_user_id") or 0)
            options.append(
                discord.SelectOption(
                    label=_trim(f"{application_id} — {status}", 100),
                    description=_trim(f"Usuário {user_id} — thread {application.get('ticket_id') or 'sem thread'}", 100),
                    value=application_id,
                )
            )
        super().__init__(
            placeholder="Escolha uma inscrição para gerenciar",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        application_id = self.values[0]
        application = await self.parent_view.service.db.application(application_id)
        if not application:
            await _interaction_reply(interaction, "Inscrição não encontrada.")
            return
        candidate = interaction.guild.get_member(int(application.get("discord_user_id") or 0)) if interaction.guild else None
        embed = _application_admin_embed(application, candidate=candidate)
        await interaction.response.edit_message(
            embed=embed,
            view=PartnerApplicationActionView(self.parent_view.service, self.parent_view.panel_id, application),
        )


class PartnerApplicationActionView(discord.ui.View):
    def __init__(self, service: PartnerProgramService, panel_id: int, application: dict[str, Any]) -> None:
        super().__init__(timeout=300)
        self.service = service
        self.panel_id = int(panel_id)
        self.application_id = str(application.get("id") or "")
        ticket_id = int(application.get("ticket_id") or 0)
        if ticket_id:
            self.add_item(
                discord.ui.Button(
                    label="Ir para thread",
                    style=discord.ButtonStyle.link,
                    url=f"https://discord.com/channels/{application['guild_id']}/{ticket_id}",
                    row=0,
                )
            )
        for action, label, style, row, callback in (
            ("refresh", "Atualizar", discord.ButtonStyle.secondary, 0, self._refresh),
            ("mention", "Mencionar candidato", discord.ButtonStyle.primary, 0, self._mention),
            ("approve", "Aprovar", discord.ButtonStyle.success, 1, self._approve),
            ("info", "Solicitar informações", discord.ButtonStyle.primary, 1, self._request_info),
            ("reject", "Recusar", discord.ButtonStyle.danger, 1, self._reject),
            ("close", "Fechar inscrição", discord.ButtonStyle.danger, 2, self._close),
            ("reset", "Resetar usuário", discord.ButtonStyle.secondary, 2, self._reset_user),
            ("back", "Voltar", discord.ButtonStyle.secondary, 2, self._back),
        ):
            button = discord.ui.Button(
                label=label,
                style=style,
                row=row,
                custom_id=f"jarvis:partner:application:{action}:{self.application_id}",
            )
            button.callback = callback  # type: ignore[method-assign]
            self.add_item(button)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        panel = await self.service.db.panel(self.panel_id)
        if self.service.can_manage_application(interaction.user, panel):
            return True
        await _interaction_reply(interaction, "Você não pode gerenciar essa inscrição.")
        return False

    async def _refresh(self, interaction: discord.Interaction) -> None:
        application = await self.service.db.application(self.application_id)
        if not application:
            await _interaction_reply(interaction, "Inscrição não encontrada.")
            return
        candidate = interaction.guild.get_member(int(application.get("discord_user_id") or 0)) if interaction.guild else None
        await interaction.response.edit_message(
            embed=_application_admin_embed(application, candidate=candidate),
            view=PartnerApplicationActionView(self.service, self.panel_id, application),
        )

    async def _mention(self, interaction: discord.Interaction) -> None:
        application = await self.service.db.application(self.application_id)
        if not application:
            await _interaction_reply(interaction, "Inscrição não encontrada.")
            return
        await self.service._notify_thread(
            application,
            f"<@{application['discord_user_id']}> a equipe te chamou por aqui.",
        )
        await _interaction_reply(interaction, "Candidate mencionado na thread da inscrição.")

    async def _approve(self, interaction: discord.Interaction) -> None:
        await self.service.approve_application(interaction, self.application_id)

    async def _request_info(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(PartnerRequestInfoModal(self.service, self.application_id))

    async def _reject(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(PartnerRejectModal(self.service, self.application_id))

    async def _close(self, interaction: discord.Interaction) -> None:
        await self.service.admin_cancel_application(interaction, self.application_id)

    async def _reset_user(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await _interaction_reply(interaction, "Use dentro do servidor.")
            return
        application = await self.service.db.application(self.application_id)
        if not application:
            await _interaction_reply(interaction, "Inscrição não encontrada.")
            return
        user_id = int(application.get("discord_user_id") or 0)
        user = interaction.guild.get_member(user_id) or await self.service.bot.fetch_user(user_id)
        total = await self.service.reset_applications_for_user(
            interaction.guild,
            user,
            actor_id=interaction.user.id,
            reason=f"Reset pelo painel administrativo a partir de {self.application_id}",
            archive_threads=True,
        )
        await _interaction_reply(interaction, f"Reset feito. Inscrições canceladas: **{total}**.")

    async def _back(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await _interaction_reply(interaction, "Use dentro do servidor.")
            return
        applications = await self.service.db.applications(interaction.guild.id, limit=25)
        embed = discord.Embed(
            title="Inscrições do Programa de Parceiros",
            description="Selecione uma inscrição abaixo para gerenciar.",
            color=discord.Color(DEFAULT_COLOR),
            timestamp=datetime.now(timezone.utc),
        )
        await interaction.response.edit_message(
            embed=embed,
            view=PartnerApplicationListView(self.service, self.panel_id, applications),
        )


class PartnerPanelTextModal(discord.ui.Modal, title="Texto do painel"):
    title_input = discord.ui.TextInput(label="Título", max_length=256, required=False)
    description = discord.ui.TextInput(label="Descrição", style=discord.TextStyle.paragraph, max_length=4000, required=False)
    color = discord.ui.TextInput(label="Cor hexadecimal", placeholder="#5865F2", max_length=16, required=False)
    image = discord.ui.TextInput(label="Imagem principal URL", required=False)
    thumbnail = discord.ui.TextInput(label="Thumbnail URL", required=False)

    def __init__(
        self,
        service: PartnerProgramService,
        panel_id: int,
        current: dict[str, Any] | None = None,
    ) -> None:
        super().__init__()
        self.service = service
        self.panel_id = panel_id
        current = current or {}
        self.title_input.default = _trim(current.get("title"), 256)
        self.description.default = _trim(current.get("description"), 4000)
        self.color.default = f"#{_safe_color(current.get('color'), DEFAULT_COLOR):06X}" if current else ""
        self.image.default = _trim(current.get("image_url"), 4000)
        self.thumbnail.default = _trim(current.get("thumbnail_url"), 4000)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.service.db.update_main_embed(
            self.panel_id,
            title=str(self.title_input.value).strip() or None,
            description=str(self.description.value).strip() or None,
            color=_safe_color(str(self.color.value).strip(), DEFAULT_COLOR) if str(self.color.value).strip() else None,
            image_url=str(self.image.value).strip() or None,
            thumbnail_url=str(self.thumbnail.value).strip() or None,
        )
        await _interaction_reply(interaction, "Texto do painel atualizado.")


class PartnerPanelConfigModal(discord.ui.Modal, title="Configuração do painel"):
    report_channel_id = discord.ui.TextInput(label="Canal de relatórios ID", required=False)
    partner_role_id = discord.ui.TextInput(label="Cargo de Parceiro ID", required=False)
    admin_user_id = discord.ui.TextInput(label="Administrador responsável ID", required=False)
    signup_label = discord.ui.TextInput(label="Texto do botão de inscrição", default="Quero me inscrever", max_length=80)
    info_url = discord.ui.TextInput(label="URL Saiba mais", default="https://www.talissonsouza.com.br/seja-afiliado", required=False)

    def __init__(self, service: PartnerProgramService, panel_id: int) -> None:
        super().__init__()
        self.service = service
        self.panel_id = panel_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.service.db.configure_panel(
            self.panel_id,
            report_channel_id=_optional_int(str(self.report_channel_id.value)),
            partner_role_id=_optional_int(str(self.partner_role_id.value)),
            admin_user_id=_optional_int(str(self.admin_user_id.value)),
        )
        url = _valid_url_or_none(str(self.info_url.value).strip())
        await self.service.db.set_buttons(
            self.panel_id,
            signup_label=str(self.signup_label.value).strip() or "Quero me inscrever",
            info_url=url,
        )
        await _interaction_reply(interaction, "Configuração atualizada.")


class PartnerReviewView(discord.ui.View):
    def __init__(
        self,
        service: PartnerProgramService,
        application_id: str,
        *,
        ticket_url: str | None = None,
    ) -> None:
        super().__init__(timeout=None)
        self.service = service
        self.application_id = application_id
        approve = discord.ui.Button(
            label="Aprovar",
            style=discord.ButtonStyle.success,
            custom_id=f"jarvis:partner:review:approve:{application_id}",
        )
        approve.callback = self._approve  # type: ignore[method-assign]
        more = discord.ui.Button(
            label="Solicitar informações",
            style=discord.ButtonStyle.primary,
            custom_id=f"jarvis:partner:review:info:{application_id}",
        )
        more.callback = self._request_info  # type: ignore[method-assign]
        reject = discord.ui.Button(
            label="Recusar",
            style=discord.ButtonStyle.danger,
            custom_id=f"jarvis:partner:review:reject:{application_id}",
        )
        reject.callback = self._reject  # type: ignore[method-assign]
        self.add_item(approve)
        if ticket_url:
            self.add_item(
                discord.ui.Button(
                    label="Ir pro ticket",
                    style=discord.ButtonStyle.link,
                    url=ticket_url,
                )
            )
        self.add_item(more)
        self.add_item(reject)

    async def _approve(self, interaction: discord.Interaction) -> None:
        await self.service.approve_application(interaction, self.application_id)

    async def _request_info(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(PartnerRequestInfoModal(self.service, self.application_id))

    async def _reject(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(PartnerRejectModal(self.service, self.application_id))


class PartnerRequestInfoModal(discord.ui.Modal, title="Solicitar informações"):
    message = discord.ui.TextInput(
        label="Mensagem para o candidato",
        style=discord.TextStyle.paragraph,
        max_length=1000,
        required=True,
    )

    def __init__(self, service: PartnerProgramService, application_id: str) -> None:
        super().__init__()
        self.service = service
        self.application_id = application_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.service.request_information(interaction, self.application_id, str(self.message.value))


class PartnerRejectModal(discord.ui.Modal, title="Recusar inscrição"):
    reason = discord.ui.TextInput(
        label="Motivo interno",
        style=discord.TextStyle.paragraph,
        max_length=1000,
        required=True,
    )

    def __init__(self, service: PartnerProgramService, application_id: str) -> None:
        super().__init__()
        self.service = service
        self.application_id = application_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.service.reject_application(interaction, self.application_id, str(self.reason.value))


def _interview_prompt(
    *,
    application: dict[str, Any],
    memory: dict[str, Any],
    history: list[dict[str, Any]],
    latest_content: str,
    candidate_name: str,
    official_page: str,
) -> str:
    missing = _missing_goals(memory)
    memory_snapshot = _prompt_memory_snapshot(memory)
    next_question = _next_local_question(memory)
    return (
        "Você é a Bella entrevistando um candidato do Programa de Parceiros. "
        f"Persona oficial da Bella:\n{BELLA_PERSONA_RUNTIME}\n\n"
        "Não seja formulário. Não diga pergunta 1, memória, objetivo interno ou próximo tópico. "
        "Converse naturalmente, use a resposta anterior e faça no máximo uma pergunta principal.\n"
        "Antes de perguntar, reaja brevemente ao que a pessoa disse, como alguém conversando de verdade. "
        "Não copie a pergunta obrigatória literalmente; transforme em uma continuação natural do papo. "
        "Evite começar toda mensagem com 'Boa', 'Entendi' ou 'Fechou'. Varie o ritmo.\n"
        "Regra forte: a entrevista precisa avançar para a pergunta obrigatória desta rodada. "
        "Não pule para motivação se a etapa atual for idade, clientes, origem das oportunidades ou networking. "
        "Não comece com 'Entendi que'. Não repita 'boa oportunidade para você'. Não use a mesma estrutura em mensagens seguidas.\n\n"
        "Isso não é prova, teste de vendas ou processo seletivo de emprego. O objetivo é conhecer a pessoa, entender o perfil, "
        "verificar se ela compreendeu o programa e identificar compatibilidade. Nunca pergunte nota/capacidade de vendas, "
        "quantos clientes ela consegue trazer, quanto dinheiro quer ganhar ou algo que pareça cobrança de meta.\n\n"
        "Se o candidato disser que não entendeu, não avance a entrevista e não registre essa resposta como confirmação. "
        "Explique a pergunta anterior com exemplos simples e pergunte novamente de outro jeito.\n\n"
        f"Pergunta obrigatória desta rodada, reescrita de forma natural: {next_question or 'encerrar com naturalidade'}\n\n"
        "Segurança: não aprove, não recuse, não prometa comissão/ganhos, não invente regras, "
        "não peça CPF/RG/senhas/dados bancários/documentos e não mencione admin durante a entrevista.\n\n"
        "Base oficial resumida: o parceiro indica oportunidades reais; a organizacao negocia, desenvolve, entrega e da suporte; "
        "a comissao oficial e de 50% do valor do projeto e so existe se a indicacao for valida e o projeto for contratado e finalizado; nao ha meta, horario fixo, exclusividade ou vinculo; "
        "aprovacao depende de analise e possivel contrato; menores de 16/17 exigem etapa propria.\n\n"
        "Trecho da pagina oficial /seja-afiliado para FAQ:\n"
        f"{official_page[:2200]}\n\n"
        "Se o candidato perguntar FAQ, responda com essa base e volte para a entrevista. Se nao souber, diga que prefere confirmar.\n\n"
        "Memória interna já descoberta:\n"
        f"{json.dumps(memory_snapshot, ensure_ascii=False)}\n\n"
        "Campos ainda pouco cobertos:\n"
        f"{json.dumps(missing, ensure_ascii=False)[:1400]}\n\n"
        f"CANDIDATO: {candidate_name} ({application.get('discord_user_id')})\n"
        f"STATUS: {application.get('status')}\n\n"
        "Historico recente:\n"
        f"{_history_text(history, limit=10)}\n\n"
        f"MENSAGEM MAIS RECENTE DO CANDIDATO:\n{latest_content}\n\n"
        "Retorne JSON com: reply, memory_updates, should_finish e finish_reason. "
        "memory_updates deve usar as chaves exatas das categorias/campos existentes apenas quando a informacao estiver clara na mensagem do candidato. "
        "Nao invente campos descobertos so para acelerar o processo."
    )


def _default_memory() -> dict[str, Any]:
    categories = {
        "candidate_profile": [
            "name",
            "full_name",
            "preferred_name",
            "email",
            "whatsapp",
            "social_links",
            "age_range",
            "profession",
            "occupation",
            "studies",
            "works",
            "business",
            "freelancer",
            "agency",
            "area",
        ],
        "client_experience": [
            "has_clients",
            "client_types",
            "client_relationship",
            "referral_experience",
            "sales_experience",
            "negotiation_experience",
        ],
        "lead_generation": [
            "prospecting_experience",
            "lead_generation_experience",
            "lead_qualification_experience",
            "approach_experience",
            "sales_channels",
            "marketing_experience",
        ],
        "networking": [
            "network_size_estimate",
            "communities",
            "companies",
            "entrepreneurs",
            "creators",
            "professionals",
            "business_owners",
            "potential_sources",
            "contact_channels",
            "client_type_to_refer",
            "real_opportunity",
        ],
        "motivation": [
            "reason_for_joining",
            "how_discovered_program",
            "financial_interest",
            "networking_interest",
            "learning_interest",
            "professional_interest",
            "expectations",
        ],
        "program_understanding": [
            "understands_referral_model",
            "understands_commission_condition",
            "understands_validation",
            "understands_payment_confirmation",
            "understands_no_employment",
            "understands_no_fixed_schedule",
            "understands_no_required_targets",
            "understands_no_exclusivity",
            "understands_contract_if_approved",
            "understands_official_channels",
            "accepted_regulation",
            "accepted_privacy",
            "truth_confirmation",
        ],
        "learning": [
            "prospecting_interest",
            "lead_generation_interest",
            "lead_qualification_interest",
            "networking_interest",
            "marketing_interest",
            "communication_interest",
            "negotiation_interest",
            "customer_relationship_interest",
            "intends_to_apply_learning",
        ],
        "practical_profile": [
            "hypothetical_scenario_response",
            "communication_style",
            "initiative",
            "clarity",
            "understanding",
            "consistency",
        ],
        "legal": ["age_category", "needs_guardian_authorization"],
    }
    return {
        category: {
            field: {
                "value": "",
                "status": "unknown",
                "source": "",
                "confidence": "low",
            }
            for field in fields
        }
        for category, fields in categories.items()
    }


def _ensure_memory_schema(memory: dict[str, Any]) -> dict[str, Any]:
    base = _default_memory()
    if not isinstance(memory, dict):
        return base
    for category, fields in memory.items():
        if not isinstance(fields, dict):
            continue
        base.setdefault(category, {})
        for field, payload in fields.items():
            if not isinstance(payload, dict):
                continue
            base[category][field] = {
                **base.get(category, {}).get(
                    field,
                    {"value": "", "status": "unknown", "source": "", "confidence": "low"},
                ),
                **payload,
            }
    return base


def _merge_memory(base: dict[str, Any], updates: dict[str, Any]) -> dict[str, Any]:
    result = json.loads(json.dumps(base, ensure_ascii=False))
    for category, fields in updates.items():
        if not isinstance(fields, dict) or category not in result:
            continue
        for field, payload in fields.items():
            if field not in result[category] or not isinstance(payload, dict):
                continue
            current = result[category][field]
            value = str(payload.get("value") or current.get("value") or "").strip()
            status = str(payload.get("status") or current.get("status") or "unknown").strip()
            source = str(payload.get("source") or current.get("source") or "").strip()
            confidence = str(payload.get("confidence") or current.get("confidence") or "low").strip()
            if status not in {"unknown", "partial", "confirmed"}:
                status = "partial" if value else "unknown"
            if source not in {"direct", "spontaneous", "contextual", ""}:
                source = "contextual"
            if confidence not in {"low", "medium", "high"}:
                confidence = "medium"
            status_rank = {"unknown": 0, "partial": 1, "confirmed": 2}
            current_status = str(current.get("status") or "unknown")
            if status_rank.get(status, 0) < status_rank.get(current_status, 0):
                status = current_status
            if current_status == "confirmed" and current.get("value"):
                value = str(current.get("value") or "")
            elif current.get("value") and not value:
                value = str(current.get("value") or "")
            current.update(
                {
                    "value": value,
                    "status": status,
                    "source": source,
                    "confidence": confidence,
                }
            )
    return result


def _local_memory_updates(
    text: str,
    memory: dict[str, Any],
    *,
    history: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    normalized = _normalize_text(text)
    original = text.strip()
    updates: dict[str, Any] = {}
    if _is_confused_response(text):
        return updates

    def set_field(
        category: str,
        field: str,
        value: str,
        *,
        status: str = "confirmed",
        confidence: str = "medium",
    ) -> None:
        if not value:
            return
        existing_memory = memory.get(category, {}).get(field, {})
        if (
            isinstance(existing_memory, dict)
            and str(existing_memory.get("status") or "") == "confirmed"
            and str(existing_memory.get("value") or "").strip()
        ):
            return
        if (
            isinstance(existing_memory, dict)
            and str(existing_memory.get("value") or "").strip()
            and _normalize_text(value) in {"sim", "s", "ss", "entendi", "sim entendi", "tranquilo", "beleza", "certo"}
            and category not in {"program_understanding", "learning", "practical_profile"}
        ):
            return
        existing_update = updates.get(category, {}).get(field, {})
        if (
            isinstance(existing_update, dict)
            and str(existing_update.get("status") or "") == "confirmed"
            and status != "confirmed"
        ):
            return
        updates.setdefault(category, {})[field] = {
            "value": value[:240],
            "status": status,
            "source": "direct",
            "confidence": confidence,
        }

    profession_terms = {
        "programador": "programador",
        "desenvolvedor": "desenvolvedor",
        "dev": "desenvolvedor",
        "designer": "designer",
        "social media": "social media",
        "criador de conteudo": "criador de conteúdo",
        "criadora de conteudo": "criadora de conteúdo",
        "content creator": "criador de conteúdo",
        "youtuber": "criador de conteúdo",
        "streamer": "criador de conteúdo",
        "gestor": "gestor",
        "empreendedor": "empreendedor",
        "freelancer": "freelancer",
        "freela": "freelancer",
    }
    for key, value in profession_terms.items():
        if key in normalized:
            if value not in {"freelancer"}:
                set_field("candidate_profile", "profession", value, confidence="high")
                set_field("candidate_profile", "occupation", original, confidence="medium")
            break
    if any(term in normalized for term in ("freelancer", "freela", "autonomo", "autonomo", "por conta propria")):
        set_field("candidate_profile", "freelancer", "sim", confidence="high")
        set_field("candidate_profile", "works", "trabalha por conta própria", confidence="medium")
    if any(term in normalized for term in ("trabalho fixo", "fixo mesmo", "emprego fixo", "clt", "registrado")):
        set_field("candidate_profile", "works", "trabalho fixo", confidence="high")
    if any(term in normalized for term in ("independente", "por conta propria", "autonomo", "autonomo", "freela", "freelancer")):
        set_field("candidate_profile", "works", "trabalho independente", confidence="high")
    if "conteudo" in normalized and any(term in normalized for term in ("sou criador", "sou criadora", "criador", "criadora", "crio", "faco", "faço", "produzo", "canal")):
        set_field("candidate_profile", "profession", "criador de conteúdo", confidence="high")
        set_field("candidate_profile", "occupation", original, confidence="medium")
    if any(term in normalized for term in ("programacao", "programação", "codigo", "código", "tecnologia", "linux", "dev", " ti")):
        set_field("candidate_profile", "area", original, status="partial", confidence="medium")
    if any(term in normalized for term in ("agencia", "empresa propria", "meu negocio", "minha empresa")):
        set_field("candidate_profile", "business", "possui negócio ou agência", confidence="medium")
        set_field("candidate_profile", "agency", "possível agência/negócio", status="partial")
    if "cliente" in normalized or "clientes" in normalized:
        set_field("client_experience", "has_clients", "tem ou teve contato com clientes", confidence="medium")
        set_field("client_experience", "client_relationship", original, status="partial")
    if any(term in normalized for term in ("indicacao", "indicação", "indicam", "indica", "referencia")):
        set_field("client_experience", "referral_experience", original, confidence="medium")
    if any(term in normalized for term in ("vendo", "vendas", "negocio", "negociacao", "negociação")):
        set_field("client_experience", "sales_experience", original, status="partial")
        set_field("client_experience", "negotiation_experience", original, status="partial")
        set_field("lead_generation", "prospecting_experience", original, status="partial")
    if any(term in normalized for term in ("discord", "comunidade", "networking", "contato", "conheco gente", "conheço gente")):
        set_field("networking", "communities", original, status="partial")
        set_field("networking", "potential_sources", original, status="partial")
    if any(term in normalized for term in ("comissao", "comissão", "dinheiro", "ganhar", "renda")):
        set_field("motivation", "financial_interest", original, confidence="medium")
    if any(term in normalized for term in ("aprender", "conteudo", "prospeccao", "prospecção", "marketing", "networking")):
        set_field("learning", "prospecting_interest", original, status="partial")
        set_field("learning", "intends_to_apply_learning", original, status="partial")
    if any(term in normalized for term in ("entendi", "sim entendi", "faz sentido", "certo", "beleza")):
        set_field("program_understanding", "understands_referral_model", "confirmou entendimento parcial", status="partial")
    email_match = re.search(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", original)
    if email_match:
        set_field("candidate_profile", "email", email_match.group(0), confidence="high")
    phone_match = re.search(r"(?:\+?55\s*)?(?:\(?\d{2}\)?\s*)?(?:9\s*)?\d{4}[-\s]?\d{4}", original)
    if phone_match and len(re.sub(r"\D", "", phone_match.group(0))) >= 8:
        set_field("candidate_profile", "whatsapp", phone_match.group(0).strip(), confidence="medium")
    social_parts = re.findall(
        r"(?:https?://)?(?:www\.)?(?:instagram\.com|linkedin\.com|x\.com|twitter\.com|tiktok\.com)/[^\s,;]+|@[A-Za-z0-9._]{3,32}",
        original,
        flags=re.IGNORECASE,
    )
    if social_parts:
        set_field("candidate_profile", "social_links", ", ".join(social_parts[:5]), confidence="medium")

    name_match = re.search(
        r"(?:meu nome(?: completo)?\s*(?:é|e|\?)\s*)([^,.:\n]+)",
        original,
        flags=re.IGNORECASE,
    )
    if name_match:
        full_name = name_match.group(1).strip()
        full_name = re.split(r"\b(?:pode me chamar|prefiro ser chamado|me chama)\b", full_name, flags=re.IGNORECASE)[0].strip()
        set_field("candidate_profile", "full_name", full_name, confidence="high")
    preferred_match = re.search(
        r"(?:pode me chamar de|prefiro ser chamado de|me chama de)\s+([^,.:\n]+)",
        original,
        flags=re.IGNORECASE,
    )
    if preferred_match:
        set_field("candidate_profile", "preferred_name", preferred_match.group(1).strip(), confidence="high")

    if any(term in normalized for term in ("quero participar", "participar do programa", "me inscrever", "entrar no programa")):
        set_field("motivation", "reason_for_joining", original, status="partial")
    if any(term in normalized for term in ("eu indico", "minha parte", "conecto", "conectar oportunidade", "talisson negocia", "talisson desenvolve", "talisson entrega")):
        set_field("program_understanding", "understands_referral_model", original, status="partial")
    if any(term in normalized for term in ("comissao depende", "comissao so", "projeto fechar", "pagamento", "cliente pagar", "sem garantia de comissao")):
        set_field("program_understanding", "understands_commission_condition", original, status="partial")
        set_field("program_understanding", "understands_payment_confirmation", original, status="partial")
    if any(term in normalized for term in ("nao tem meta", "sem meta", "nao tem horario", "sem horario", "nao tem exclusividade", "sem exclusividade")):
        set_field("program_understanding", "understands_no_fixed_schedule", original, status="partial")
        set_field("program_understanding", "understands_no_required_targets", original, status="partial")
        set_field("program_understanding", "understands_no_exclusivity", original, status="partial")
    if "regulamento" in normalized and any(term in normalized for term in ("aceito", "li", "estou de acordo", "de acordo")):
        set_field("program_understanding", "accepted_regulation", original, status="partial")
    if any(term in normalized for term in ("privacidade", "politica de privacidade")) and any(term in normalized for term in ("aceito", "li", "estou de acordo", "de acordo")):
        set_field("program_understanding", "accepted_privacy", original, status="partial")
    if "canais oficiais" in normalized:
        set_field("program_understanding", "understands_official_channels", original, status="partial")
    if any(term in normalized for term in ("informacoes verdadeiras", "informacao verdadeira", "dados verdadeiros")):
        set_field("program_understanding", "truth_confirmation", original, status="partial")
    channels = [
        label
        for key, label in (
            ("whatsapp", "WhatsApp"),
            ("discord", "Discord"),
            ("comunidade", "comunidades"),
            ("empresas", "empresas"),
            ("instagram", "Instagram"),
            ("redes sociais", "redes sociais"),
            ("linkedin", "LinkedIn"),
            ("evento", "eventos"),
            ("amigo", "amigos/conhecidos"),
        )
        if key in normalized
    ]
    if channels:
        set_field("networking", "contact_channels", ", ".join(dict.fromkeys(channels)), confidence="medium")
    age_match = re.search(r"\b(1[6-9]|[2-6][0-9])\b", normalized)
    if age_match:
        age = int(age_match.group(1))
        if age in {16, 17}:
            set_field("candidate_profile", "age_range", "16–17", confidence="high")
            set_field("legal", "age_category", "16–17", confidence="high")
            set_field("legal", "needs_guardian_authorization", "necessário", confidence="high")
        elif age >= 18:
            set_field("candidate_profile", "age_range", f"{age} anos", confidence="high")
            set_field("legal", "age_category", "maior de idade", confidence="high")
    elif any(term in normalized for term in ("18+", "maior de idade", "tenho 18", "18 anos ou mais")):
        set_field("candidate_profile", "age_range", "18 anos ou mais", confidence="high")
        set_field("legal", "age_category", "maior de idade", confidence="high")

    last_question = _last_jarvis_question(history or [])
    last_normalized = _normalize_text(last_question)
    short_answer = len(normalized.split()) <= 8
    low_value_answers = {
        "oi",
        "ola",
        "olá",
        "opa",
        "bom dia",
        "boa tarde",
        "boa noite",
        "sim",
        "s",
        "ss",
        "nao",
        "não",
        "n",
        "nn",
    }
    if last_question and normalized not in low_value_answers:
        if any(term in last_normalized for term in ("o que voce faz atualmente", "o que você faz atualmente", "me conta um pouco sobre o que voce faz", "me conta um pouco sobre o que você faz")):
            set_field("candidate_profile", "profession", original, status="partial", confidence="medium")
            set_field("candidate_profile", "occupation", original, status="partial", confidence="medium")
        if any(term in last_normalized for term in ("trabalho fixo", "forma independente", "freelance", "freela")):
            set_field("candidate_profile", "works", original, status="partial", confidence="medium")
        if any(term in last_normalized for term in ("conteudo sobre", "conteúdo sobre", "tipo de conteudo", "tipo de conteúdo")):
            set_field("candidate_profile", "area", original, status="partial", confidence="medium")
        if any(term in last_normalized for term in ("motiva voce", "motiva você", "por que voce cria", "por que você cria")):
            set_field("motivation", "professional_interest", original, status="partial", confidence="medium")
    if short_answer and last_question:
        if any(term in last_normalized for term in ("idade", "faixa etaria", "faixa etária")):
            set_field("candidate_profile", "age_range", original, status="partial")
            set_field("legal", "age_category", original, status="partial")
        if any(term in last_normalized for term in ("nome completo", "prefere ser chamado", "registrar seu nome")):
            set_field("candidate_profile", "full_name", original, status="partial")
            set_field("candidate_profile", "preferred_name", original.split()[0] if original.split() else original, status="partial")
        if any(term in last_normalized for term in ("email", "e-mail", "whatsapp", "redes sociais")):
            if not email_match:
                set_field("candidate_profile", "email", original, status="partial")
            if not phone_match:
                set_field("candidate_profile", "whatsapp", original, status="partial")
            if not social_parts:
                set_field("candidate_profile", "social_links", original, status="partial")
        if any(term in last_normalized for term in ("clientes", "servico", "serviço", "pessoas pedindo")):
            set_field("client_experience", "has_clients", original, status="partial")
            set_field("client_experience", "client_relationship", original, status="partial")
        if any(term in last_normalized for term in ("indicacao", "rede social", "procura ativamente")):
            set_field("client_experience", "referral_experience", original, status="partial")
            set_field("lead_generation", "sales_channels", original, status="partial")
        if any(term in last_normalized for term in ("sua rede", "comunidade", "empresas", "donos de negocio")):
            set_field("networking", "potential_sources", original, status="partial")
        if any(term in last_normalized for term in ("onde voce costuma", "possiveis clientes", "possíveis clientes")):
            set_field("networking", "contact_channels", original, status="partial")
        if any(term in last_normalized for term in ("tipo de cliente", "conseguiria indicar")):
            set_field("networking", "client_type_to_refer", original, status="partial")
        if any(term in last_normalized for term in ("chamou atencao", "programa", "comissao", "aprendizado")):
            set_field("motivation", "reason_for_joining", original, status="partial")
        if any(term in last_normalized for term in ("faz sentido", "sua parte", "conectar uma oportunidade")):
            set_field("program_understanding", "understands_referral_model", original, status="partial")
        if any(term in last_normalized for term in ("comissao so acontece", "pagamento for confirmado")):
            set_field("program_understanding", "understands_commission_condition", original, status="partial")
            set_field("program_understanding", "understands_payment_confirmation", original, status="partial")
        if any(term in last_normalized for term in ("horario fixo", "meta obrigatoria", "exclusividade")):
            set_field("program_understanding", "understands_no_fixed_schedule", original, status="partial")
            set_field("program_understanding", "understands_no_required_targets", original, status="partial")
            set_field("program_understanding", "understands_no_exclusivity", original, status="partial")
        if any(term in last_normalized for term in ("aprender mais", "prospeccao", "relacionamento com clientes")):
            set_field("learning", "prospecting_interest", original, status="partial")
        if any(term in last_normalized for term in ("prospeccao", "vendas", "networking ou indicacao", "pouca experiencia")):
            set_field("lead_generation", "prospecting_experience", original, status="partial")
        if any(term in last_normalized for term in ("como voce pretende encontrar", "oportunidades para indicacao")):
            set_field("lead_generation", "lead_generation_experience", original, status="partial")
        if any(term in last_normalized for term in ("seus proprios servicos", "produtos ou projetos", "seus próprios serviços")):
            set_field("learning", "intends_to_apply_learning", original, status="partial")
        if any(term in last_normalized for term in ("alguem em mente", "alguém em mente", "oportunidade real")):
            set_field("networking", "real_opportunity", original, status="partial")
        if any(term in last_normalized for term in ("regulamento", "politica de privacidade", "política de privacidade", "canais oficiais", "informacoes verdadeiras", "informações verdadeiras")):
            set_field("program_understanding", "accepted_regulation", original, status="partial")
            set_field("program_understanding", "accepted_privacy", original, status="partial")
            set_field("program_understanding", "understands_official_channels", original, status="partial")
            set_field("program_understanding", "truth_confirmation", original, status="partial")
        if (
            any(term in last_normalized for term in ("empresa que precisa", "primeira conversa", "conduziria"))
            and (
                len(normalized.split()) > 5
                or any(term in normalized for term in ("pergunt", "falaria", "entender", "problema", "necessidade", "conduzir"))
            )
        ):
            set_field("practical_profile", "hypothetical_scenario_response", original, status="partial")
        if any(term in last_normalized for term in ("valeu a pena participar", "daqui a alguns meses")):
            set_field("motivation", "expectations", original, status="partial")
        if any(term in last_normalized for term in ("seus proprios servicos", "produtos ou projetos", "seus próprios serviços")):
            set_field("learning", "intends_to_apply_learning", original, status="partial")
        if any(term in last_normalized for term in ("alguem em mente", "alguém em mente", "oportunidade real")):
            set_field("networking", "real_opportunity", original, status="partial")
        if any(term in last_normalized for term in ("regulamento", "politica de privacidade", "política de privacidade", "canais oficiais", "informacoes verdadeiras", "informações verdadeiras")):
            set_field("program_understanding", "accepted_regulation", original, status="partial")
            set_field("program_understanding", "accepted_privacy", original, status="partial")
            set_field("program_understanding", "understands_official_channels", original, status="partial")
            set_field("program_understanding", "truth_confirmation", original, status="partial")

    if last_question and not short_answer:
        if any(term in last_normalized for term in ("nome completo", "prefere ser chamado", "registrar seu nome")):
            set_field("candidate_profile", "full_name", original, status="partial")
            set_field("candidate_profile", "preferred_name", original.split()[0] if original.split() else original, status="partial")
        if any(term in last_normalized for term in ("email", "e-mail", "whatsapp", "redes sociais")):
            if not email_match:
                set_field("candidate_profile", "email", original, status="partial")
            if not phone_match:
                set_field("candidate_profile", "whatsapp", original, status="partial")
            if not social_parts:
                set_field("candidate_profile", "social_links", original, status="partial")
        if any(term in last_normalized for term in ("tipo de cliente", "conseguiria indicar")):
            set_field("networking", "client_type_to_refer", original, status="partial")
        if any(term in last_normalized for term in ("onde voce costuma", "possiveis clientes", "possíveis clientes")):
            set_field("networking", "contact_channels", original, status="partial")
        if any(term in last_normalized for term in ("prospeccao", "vendas", "networking ou indicacao", "pouca experiencia")):
            set_field("lead_generation", "prospecting_experience", original, status="partial")
        if any(term in last_normalized for term in ("como voce pretende encontrar", "oportunidades para indicacao")):
            set_field("lead_generation", "lead_generation_experience", original, status="partial")

    if last_question:
        if (
            any(term in last_normalized for term in ("empresa que precisa", "primeira conversa", "conduziria"))
            and (
                len(normalized.split()) > 5
                or any(term in normalized for term in ("pergunt", "falaria", "entender", "problema", "necessidade", "conduzir"))
            )
        ):
            set_field("practical_profile", "hypothetical_scenario_response", original, status="partial")
        if any(term in last_normalized for term in ("valeu a pena participar", "daqui a alguns meses")):
            set_field("motivation", "expectations", original, status="partial")

    return updates


def _safe_ai_memory_updates(
    updates: dict[str, Any],
    *,
    latest_content: str,
    memory: dict[str, Any],
    history: list[dict[str, Any]],
) -> dict[str, Any]:
    if not isinstance(updates, dict):
        return {}
    normalized_latest = _normalize_text(latest_content)
    latest_words = set(re.findall(r"[a-z0-9]{4,}", normalized_latest))
    short_answer = len(normalized_latest.split()) <= 5
    last_question = _normalize_text(_last_jarvis_question(history))
    allowed_short_fields: set[tuple[str, str]] = set()
    if any(term in last_question for term in ("o que voce faz atualmente", "me conta um pouco sobre o que voce faz")):
        allowed_short_fields.update(
            {
                ("candidate_profile", "profession"),
                ("candidate_profile", "occupation"),
                ("candidate_profile", "area"),
                ("candidate_profile", "works"),
                ("candidate_profile", "freelancer"),
                ("candidate_profile", "business"),
                ("candidate_profile", "agency"),
            }
        )
    if any(term in last_question for term in ("trabalho fixo", "forma independente", "freelance", "freela")):
        allowed_short_fields.update(
            {
                ("candidate_profile", "works"),
                ("candidate_profile", "freelancer"),
                ("candidate_profile", "business"),
            }
        )
    if any(term in last_question for term in ("conteudo sobre", "tipo de conteudo", "tipo de area", "area")):
        allowed_short_fields.update(
            {
                ("candidate_profile", "area"),
                ("candidate_profile", "occupation"),
                ("motivation", "professional_interest"),
            }
        )
    if any(term in last_question for term in ("faz sentido", "sua parte", "conectar uma oportunidade")):
        allowed_short_fields.add(("program_understanding", "understands_referral_model"))
    if any(term in last_question for term in ("comissao so acontece", "pagamento for confirmado")):
        allowed_short_fields.update(
            {
                ("program_understanding", "understands_commission_condition"),
                ("program_understanding", "understands_payment_confirmation"),
            }
        )
    if any(term in last_question for term in ("horario fixo", "meta obrigatoria", "exclusividade")):
        allowed_short_fields.update(
            {
                ("program_understanding", "understands_no_fixed_schedule"),
                ("program_understanding", "understands_no_required_targets"),
                ("program_understanding", "understands_no_exclusivity"),
            }
        )
    if any(term in last_question for term in ("regulamento", "politica de privacidade", "canais oficiais", "informacoes enviadas")):
        allowed_short_fields.update(
            {
                ("program_understanding", "accepted_regulation"),
                ("program_understanding", "accepted_privacy"),
                ("program_understanding", "understands_official_channels"),
                ("program_understanding", "truth_confirmation"),
                ("program_understanding", "understands_commission_condition"),
            }
        )

    safe: dict[str, Any] = {}
    for category, fields in updates.items():
        if not isinstance(fields, dict) or category not in memory:
            continue
        for field, payload in fields.items():
            if field not in memory.get(category, {}) or not isinstance(payload, dict):
                continue
            value = str(payload.get("value") or "").strip()
            if not value:
                continue
            if short_answer and (category, field) not in allowed_short_fields:
                continue
            normalized_value = _normalize_text(value)
            value_words = {
                word
                for word in re.findall(r"[a-z0-9]{4,}", normalized_value)
                if word not in {"confirmou", "entendimento", "parcial", "candidato", "pessoa", "programa"}
            }
            if value_words and not (value_words & latest_words):
                continue
            if (
                (category, field)
                in {
                    ("candidate_profile", "age_range"),
                    ("legal", "age_category"),
                    ("legal", "needs_guardian_authorization"),
                }
                and re.search(r"\b(16|17)\b", normalized_value)
            ):
                payload = dict(payload)
                if field == "needs_guardian_authorization":
                    payload["value"] = "necessário"
                else:
                    payload["value"] = "16–17"
                payload["status"] = "confirmed"
            safe.setdefault(category, {})[field] = payload
    return safe


def _local_interview_reply(
    *,
    memory: dict[str, Any],
    latest_content: str,
    candidate_name: str,
    official_page: str = "",
) -> str:
    faq = _local_program_faq_answer(latest_content, official_page=official_page)
    next_question = _next_local_question(memory)
    if faq:
        return f"{faq}\n\n{next_question}" if next_question else faq

    normalized = _normalize_text(latest_content)
    if next_question:
        if re.search(r"\b(16|17)\b", normalized):
            return (
                "Entendi. Pessoas de 16 e 17 anos podem participar, mas existe uma etapa adicional com responsável legal e possível documentação se a inscrição avançar.\n\n"
                "Não precisa mandar documento nenhum aqui na conversa.\n\n"
                f"{next_question}"
            )
        if any(term in normalized for term in ("programador", "desenvolvedor", "dev")):
            bridge = random.choice(
                [
                    "Boa. Então você já vem do lado de tecnologia.",
                    "Show, isso já dá um contexto bom do seu perfil.",
                    "Fechou. Tech é um ponto bem útil pra esse tipo de parceria.",
                ]
            )
            return f"{bridge}\n\n{next_question}"
        if "freelancer" in normalized or "freela" in normalized:
            bridge = random.choice(
                [
                    "Boa. Então você já tem uma vivência mais direta com cliente e entrega.",
                    "Interessante. Freela costuma dar uma noção bem real de oportunidade e relacionamento.",
                    "Fechou, isso ajuda a entender como você se movimenta hoje.",
                ]
            )
            return f"{bridge}\n\n{next_question}"
        if normalized in {"sim", "s", "ss", "bastante", "tenho", "ja", "já"}:
            return f"{random.choice(['Boa.', 'Show.', 'Perfeito.', 'Fechado, isso ajuda.'])}\n\n{next_question}"
        if normalized in {"nao", "não", "n", "nn"}:
            return f"{random.choice(['Tranquilo.', 'Sem problema.', 'Fechado, sem crise.'])}\n\n{next_question}"
        return next_question

    return (
        "Boa, acho que já tenho uma visão bem melhor do seu perfil.\n\n"
        "Vou organizar o que você me passou e encaminhar sua inscrição para análise."
    )


def _next_local_question(memory: dict[str, Any]) -> str:
    if not _field_has_value(memory, "candidate_profile", "profession"):
        return "Me conta um pouco sobre o que você faz atualmente."
    if not _field_has_value(memory, "candidate_profile", "age_range"):
        return (
            "Boa. Antes de avançar: qual sua idade ou faixa etária?\n\n"
            "Pergunto só porque, se for 16 ou 17, existe uma etapa adicional com responsável legal. Não precisa enviar documento nenhum por aqui."
        )
    if not _field_has_value(memory, "candidate_profile", "full_name"):
        return "Pra eu registrar sua inscrição certinho: qual é seu nome completo e por qual nome você prefere ser chamado?"
    if (
        not _field_has_value(memory, "candidate_profile", "email")
        or not _field_has_value(memory, "candidate_profile", "whatsapp")
        or not _field_has_value(memory, "candidate_profile", "social_links")
    ):
        return (
            "Me manda também um e-mail, um WhatsApp e suas principais redes sociais.\n\n"
            "Não precisa enviar documento nem nada sensível. É só contato mesmo, pra caso sua inscrição avance."
        )
    if not _field_has_value(memory, "client_experience", "has_clients"):
        return "Você já teve contato com clientes ou pessoas pedindo algum serviço seu, mesmo que informalmente?"
    if not _field_has_value(memory, "client_experience", "referral_experience"):
        return "Quando aparece cliente ou oportunidade pra você, geralmente vem por indicação, Instagram/rede social ou você procura ativamente?"
    if not _field_has_value(memory, "networking", "contact_channels"):
        return "Onde você costuma ter contato com possíveis clientes hoje? Pode ser WhatsApp, Discord, comunidades, empresas, redes sociais, eventos, amigos/conhecidos..."
    if not _field_has_value(memory, "networking", "potential_sources"):
        return "Sua rede hoje é mais de amigos/comunidade ou você também conversa com empresas, donos de negócio e pessoas que podem gerar oportunidades?"
    if not _field_has_value(memory, "networking", "client_type_to_refer"):
        return "Que tipo de cliente você acredita que conseguiria indicar com mais facilidade?"
    if not _field_has_value(memory, "motivation", "reason_for_joining"):
        return "O que te chamou atenção no programa? Foi mais a comissão, networking, aprendizado ou uma mistura das coisas?"
    if not _field_has_value(memory, "lead_generation", "prospecting_experience"):
        return "Você já trabalhou com prospecção, vendas, networking ou indicação de clientes? Pode ser pouca experiência também."
    if not _field_has_value(memory, "lead_generation", "lead_generation_experience"):
        return "Como você pretende encontrar oportunidades para indicação se entrar no programa?"
    if not _field_has_value(memory, "program_understanding", "understands_referral_model"):
        return (
            "Só pra alinhar certinho: nesse programa sua parte é conectar uma oportunidade real. "
            "A análise, negociação, desenvolvimento, entrega e suporte ficam com a organização. Isso faz sentido pra você?"
        )
    if not _field_has_value(memory, "program_understanding", "understands_commission_condition"):
        return (
            "E uma parte importante: a comissão é de 50% do valor do projeto, mas só acontece se a indicação for válida e o projeto for contratado e finalizado. "
            "Você entende essa lógica?"
        )
    if not _field_has_value(memory, "program_understanding", "understands_no_fixed_schedule"):
        return "Também não existe horário fixo, meta obrigatória ou exclusividade. Você participaria no seu ritmo. Tranquilo pra você?"
    if not _field_has_value(memory, "learning", "prospecting_interest"):
        return "Além da comissão, você teria interesse em aprender mais sobre prospecção, abordagem, networking e relacionamento com clientes?"
    if not _field_has_value(memory, "learning", "intends_to_apply_learning"):
        return "Você pretende usar esse aprendizado também nos seus próprios serviços, produtos ou projetos?"
    if not _field_has_value(memory, "networking", "real_opportunity"):
        return "Se você já tem alguém em mente que poderia precisar dos meus serviços, pode contar um pouco sobre essa oportunidade. Se não tiver agora, tudo bem também."
    if not _field_has_value(memory, "program_understanding", "accepted_regulation"):
        return (
            "Pra finalizar a parte de regras: você confirma que leu e concorda com o regulamento e a política de privacidade, "
            "entende que comissão não é garantida, que as indicações precisam passar pelos canais oficiais e que as informações enviadas são verdadeiras?"
        )
    return ""


def _local_program_faq_answer(text: str, *, official_page: str = "") -> str:
    normalized = _normalize_text(text)
    page_snippet = _official_page_snippet(official_page, normalized)
    if any(term in normalized for term in ("quanto ganho", "comissao", "comissão", "porcentagem", "recebo")):
        return (
            "Sobre comissão: ela corresponde a 50% do valor do projeto e só é registrada quando a indicação é válida e o projeto é contratado e finalizado conforme as regras do programa. "
            "Por exemplo, um projeto de R$ 5.000 gera R$ 2.500 de comissão."
            + (f"\n\nPelo material oficial: {page_snippet}" if page_snippet else "")
        )
    if any(term in normalized for term in ("preciso vender", "tenho que vender", "sou obrigado vender")):
        return "Não. A ideia não é você vender ou executar o projeto. Sua parte é principalmente conectar uma oportunidade real; negociação e entrega ficam com a organização."
    if any(term in normalized for term in ("meta", "horario", "horário", "obrigatorio", "obrigatório")):
        return "Não existe meta obrigatória, horário fixo, rotina obrigatória ou exclusividade. Você participa no seu ritmo, quando surgir uma oportunidade real."
    if "contrato" in normalized:
        return "A inscrição não ativa a parceria automaticamente. Se avançar, pode existir uma etapa de formalização contratual antes da ativação definitiva."
    if any(term in normalized for term in ("menor", "16", "17")) and "idade" in normalized:
        return "Para 16 ou 17 anos existe uma etapa adicional com autorização do responsável legal. Isso não precisa ser tratado com documentos aqui na entrevista."
    if page_snippet and any(term in normalized for term in ("programa", "afiliado", "parceiro", "beneficio", "indicar", "indicacao")):
        return f"Pelo que consta na pagina oficial, {page_snippet}"
    return ""


def _is_confused_response(text: str) -> bool:
    normalized = _normalize_text(text)
    compact = re.sub(r"[^a-z0-9]+", "", normalized)
    confusion_phrases = (
        "nao entendi",
        "não entendi",
        "nn entendi",
        "n entendi",
        "nao compreendi",
        "não compreendi",
        "nn compreendi",
        "como assim",
        "explica melhor",
        "explica dnv",
        "explica de novo",
        "nao sei responder",
        "não sei responder",
        "nn sei responder",
        "oq quer dizer",
        "o que quer dizer",
        "nao ficou claro",
        "não ficou claro",
        "nn ficou claro",
        "fiquei perdido",
        "to perdido",
        "n entendi nada",
    )
    compact_phrases = (
        "naoentendi",
        "nnentendi",
        "nentendi",
        "naocompreendi",
        "nncompreendi",
        "explicamelhor",
        "explicadnv",
        "comoaassim",
        "com assim",
    )
    return any(phrase in normalized for phrase in confusion_phrases) or any(
        phrase in compact for phrase in compact_phrases
    )


def _clarification_reply(
    last_question: str,
    memory: dict[str, Any],
    *,
    candidate_name: str,
) -> str:
    normalized = _normalize_text(last_question)
    if not normalized:
        next_question = _next_local_question(memory)
        return (
            "Tranquilo, vou explicar melhor.\n\n"
            "A ideia dessa conversa não é te testar. Eu só preciso entender seu perfil e como você poderia participar do programa.\n\n"
            f"Vamos por partes: {next_question}"
        )
    if any(term in normalized for term in ("comissao", "pagamento", "projeto fechar")):
        return (
            "Claro. Essa parte é sobre quando uma comissão realmente passa a existir.\n\n"
            "Funciona assim: a comissão é de 50% do valor do projeto, mas só é considerada se a indicação for válida e o projeto for contratado e finalizado conforme as regras.\n\n"
            "Ou seja, não é só indicar alguém e pronto. Precisa virar um projeto real.\n\n"
            "Com essa explicação, essa lógica ficou mais clara pra você?"
        )
    if (
        any(term in normalized for term in ("como voce pretende encontrar", "oportunidades para indicacao"))
        or ("pretende encontrar" in normalized and "oportun" in normalized)
        or ("entrar no programa" in normalized and "oportun" in normalized)
    ):
        return (
            "Boa, explico melhor.\n\n"
            "Eu queria entender de onde você acha que poderiam surgir oportunidades pra indicar. Por exemplo: pessoas no Instagram, contatos no WhatsApp, comunidades no Discord, clientes antigos, amigos, empresas pequenas ou gente que já comentou que precisa de site, sistema, automação ou suporte técnico.\n\n"
            "Não precisa ter uma estratégia perfeita. Só me conta: por onde você acha que conseguiria encontrar essas oportunidades?"
        )
    if any(term in normalized for term in ("prospeccao", "vendas", "networking", "indicacao de clientes")):
        return (
            "Sem problema. Quando falo de prospecção, vendas, networking ou indicação, quero dizer qualquer experiência em conversar com pessoas, perceber uma necessidade e conectar essa pessoa com alguém que pode resolver.\n\n"
            "Pode ser algo simples, tipo indicar um designer pra um amigo, mandar cliente pra alguém, conversar com empresa no Instagram ou conseguir trabalho por contato.\n\n"
            "Você já fez algo parecido, mesmo que de forma informal?"
        )
    if any(term in normalized for term in ("possiveis clientes", "whatsapp", "discord", "comunidades", "redes sociais")):
        return (
            "Claro. Aqui eu só quero saber onde você costuma ter contato com pessoas que poderiam precisar de algum serviço.\n\n"
            "Pode ser WhatsApp, Instagram, Discord, comunidades, empresas, eventos, amigos, conhecidos ou clientes que você já atende.\n\n"
            "Onde você costuma conversar mais com esse tipo de pessoa?"
        )
    if any(term in normalized for term in ("tipo de cliente", "conseguiria indicar")):
        return (
            "Tranquilo. Estou perguntando qual perfil de pessoa ou empresa você acha que teria mais chance de indicar.\n\n"
            "Exemplos: pequenos empresários, criadores de conteúdo, lojas, comunidades de Discord, freelancers, agências, pessoas que precisam de site, sistema, bot ou automação.\n\n"
            "Que tipo de cliente você acha que apareceria mais na sua rede?"
        )
    if any(term in normalized for term in ("conectar uma oportunidade", "negociacao", "desenvolvimento", "entrega", "suporte")):
        return (
            "Vou simplificar.\n\n"
            "No programa, você não precisa vender nem executar o projeto. Sua parte principal é perceber uma oportunidade real e fazer essa ponte.\n\n"
            "Depois disso, a análise, negociação, escopo, desenvolvimento, entrega e suporte ficam com o Talisson/equipe.\n\n"
            "Faz sentido essa divisão de papéis?"
        )
    if any(term in normalized for term in ("horario fixo", "meta obrigatoria", "exclusividade")):
        return (
            "Sem crise. Essa pergunta é só pra confirmar que você entendeu que o programa é livre.\n\n"
            "Você não tem horário obrigatório, não tem meta mínima, não precisa indicar toda semana e não fica preso com exclusividade.\n\n"
            "Você participa quando surgir uma oportunidade real. Tranquilo pra você?"
        )
    if any(term in normalized for term in ("regulamento", "privacidade", "canais oficiais", "informacoes verdadeiras")):
        return (
            "Claro. Essa parte é só uma confirmação final de regras.\n\n"
            "Você precisa estar de acordo que leu/aceita o regulamento e a política de privacidade, entende que comissão não é garantida, sabe que as indicações devem ir pelos canais oficiais e confirma que as informações que mandou são verdadeiras.\n\n"
            "Se estiver de acordo com isso, pode responder algo como: 'li e estou de acordo'."
        )
    next_question = _next_local_question(memory)
    return (
        "Tranquilo, vou reformular.\n\n"
        f"Eu tinha perguntado isso aqui: {last_question.strip()}\n\n"
        "O que eu preciso entender é só essa parte do seu perfil, sem resposta perfeita.\n\n"
        f"Respondendo do seu jeito: {next_question or 'quer me explicar qual parte ficou confusa?'}"
    )


def _local_should_finish(memory: dict[str, Any], history: list[dict[str, Any]]) -> bool:
    candidate_messages = [
        item
        for item in history
        if str(item.get("author_type") or "") == "candidate"
    ]
    return len(candidate_messages) >= 12


def _field_has_value(memory: dict[str, Any], category: str, field: str) -> bool:
    payload = memory.get(category, {}).get(field, {})
    if not isinstance(payload, dict):
        return False
    return bool(str(payload.get("value") or "").strip()) or str(payload.get("status") or "") in {"partial", "confirmed"}


def _normalize_text(value: str) -> str:
    text = value.lower()
    replacements = {
        "á": "a",
        "à": "a",
        "ã": "a",
        "â": "a",
        "é": "e",
        "ê": "e",
        "í": "i",
        "ó": "o",
        "ô": "o",
        "õ": "o",
        "ú": "u",
        "ç": "c",
    }
    for source, target in replacements.items():
        text = text.replace(source, target)
    return text


def _missing_goals(memory: dict[str, Any]) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for category, fields in memory.items():
        if not isinstance(fields, dict):
            continue
        missing = [
            field
            for field, payload in fields.items()
            if isinstance(payload, dict) and str(payload.get("status") or "unknown") != "confirmed"
        ]
        if missing:
            result[category] = missing[:8]
    return result


def _essential_memory_ready(memory: dict[str, Any]) -> bool:
    required_fields = (
        ("candidate_profile", "profession"),
        ("candidate_profile", "age_range"),
        ("candidate_profile", "full_name"),
        ("candidate_profile", "email"),
        ("candidate_profile", "whatsapp"),
        ("motivation", "reason_for_joining"),
        ("program_understanding", "understands_commission_condition"),
        ("program_understanding", "accepted_regulation"),
    )
    for category, field in required_fields:
        if not _field_has_value(memory, category, field):
            return False
    groups = {
        "candidate_profile": 2,
        "client_experience": 1,
        "networking": 1,
        "motivation": 1,
        "program_understanding": 2,
    }
    for category, minimum in groups.items():
        fields = memory.get(category)
        if not isinstance(fields, dict):
            return False
        confirmed = sum(
            1
            for payload in fields.values()
            if isinstance(payload, dict) and str(payload.get("status") or "") == "confirmed"
        )
        partial = sum(
            1
            for payload in fields.values()
            if isinstance(payload, dict) and str(payload.get("status") or "") == "partial"
        )
        if confirmed + partial < minimum:
            return False
    return True


def _local_report(memory: dict[str, Any], messages: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "identification": _memory_summary(memory, "candidate_profile"),
        "profile": _memory_summary(memory, "candidate_profile"),
        "clients": _memory_summary(memory, "client_experience"),
        "prospecting": _memory_summary(memory, "lead_generation"),
        "networking": _memory_summary(memory, "networking"),
        "motivation": _memory_summary(memory, "motivation"),
        "learning": _memory_summary(memory, "learning"),
        "program_understanding": _memory_summary(memory, "program_understanding"),
        "summary": "Relatório local gerado com base nas respostas registradas. A conversa precisa ser analisada pela equipe.",
        "attention_points": "Sem pontos críticos confirmados automaticamente.",
        "compatibility": "Média",
        "communication": "Média",
        "opportunity_potential": "Médio",
        "understanding": "Médio",
        "notes": f"Mensagens registradas: {len(messages)}.",
    }


def _memory_summary(memory: dict[str, Any], category: str) -> str:
    fields = memory.get(category)
    if not isinstance(fields, dict):
        return "Não confirmado."
    parts = []
    for field, payload in fields.items():
        if not isinstance(payload, dict):
            continue
        value = str(payload.get("value") or "").strip()
        status = str(payload.get("status") or "").strip()
        if value:
            parts.append(f"{field}: {value} ({status or 'sem status'})")
    return "\n".join(parts[:8]) or "Não confirmado."


def _opening_message(display_name: str) -> str:
    name = display_name.strip() or "você"
    return (
        f"Fala, {name}. Vi que você quer participar do Programa de Parceiros.\n\n"
        "Antes de qualquer coisa, quero te conhecer um pouco melhor. Não precisa responder como formulário nem tentar dar resposta perfeita. "
        "Isso aqui não é prova; é só uma conversa pra entender seu perfil, sua rede e se o programa faz sentido para você.\n\n"
        "Pra começar: o que você faz atualmente?"
    )


def _history_text(messages: list[dict[str, Any]], *, limit: int) -> str:
    lines = []
    for item in messages[-limit:]:
        author = str(item.get("author_type") or "mensagem")
        content = str(item.get("content") or "").strip()
        if content:
            lines.append(f"{author}: {content[:1000]}")
    return "\n".join(lines) or "Sem histórico."


def _chunks(text: str, limit: int = 1850) -> list[str]:
    text = text.strip()
    if len(text) <= limit:
        return [text] if text else []
    parts: list[str] = []
    current = ""
    for paragraph in re.split(r"(\n\n+)", text):
        if len(current) + len(paragraph) <= limit:
            current += paragraph
            continue
        if current.strip():
            parts.append(current.strip())
        current = paragraph
        while len(current) > limit:
            parts.append(current[:limit].strip())
            current = current[limit:]
    if current.strip():
        parts.append(current.strip())
    return parts


def _panel_from_row(row: sqlite3.Row) -> PartnerPanel:
    return PartnerPanel(
        id=int(row["id"]),
        guild_id=int(row["guild_id"]),
        name=str(row["name"]),
        panel_type=str(row["panel_type"]),
        channel_id=_optional_int(row["channel_id"]),
        message_id=_optional_int(row["message_id"]),
        status=str(row["status"]),
        created_by=int(row["created_by"]),
        admin_user_id=_optional_int(row["admin_user_id"]),
        manager_role_id=(
            _optional_int(row["manager_role_id"])
            if "manager_role_id" in row.keys()
            else None
        ),
        report_channel_id=_optional_int(row["report_channel_id"]),
        partner_role_id=_optional_int(row["partner_role_id"]),
        render_mode=(
            "v2"
            if "render_mode" in row.keys()
            and str(row["render_mode"]).lower() == "v2"
            else "v1"
        ),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        published_at=str(row["published_at"]) if row["published_at"] else None,
    )


def _parse_partner_joined_at(value: str) -> str:
    raw = value.strip()
    if not raw:
        return datetime.now(timezone.utc).isoformat()
    parsed: datetime | None = None
    for pattern in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y"):
        try:
            parsed = datetime.strptime(raw, pattern).replace(tzinfo=timezone.utc)
            break
        except ValueError:
            continue
    if parsed is None:
        raise ValueError("Data inválida. Use `DD/MM/AAAA`, por exemplo `09/10/2026`.")
    now = datetime.now(timezone.utc)
    if parsed.date() > now.date():
        raise ValueError("A data de entrada não pode estar no futuro.")
    if parsed.year < 2015:
        raise ValueError("A data de entrada precisa ser de 2015 em diante.")
    return parsed.isoformat()


def _parse_partner_money(value: str) -> float:
    text = str(value or "").strip().upper().replace("R$", "").replace(" ", "")
    if not text:
        return 0.0
    if "," in text:
        text = text.replace(".", "").replace(",", ".")
    elif text.count(".") > 1:
        text = text.replace(".", "")
    amount = float(text)
    if amount < 0:
        raise ValueError("negative amount")
    return amount


def _partner_project_status(value: str) -> str | None:
    normalized = str(value or "").strip().casefold()
    return {
        "fechado": "closed",
        "finalizado": "closed",
        "closed": "closed",
        "negociacao": "negotiation",
        "negociação": "negotiation",
        "em negociacao": "negotiation",
        "em negociação": "negotiation",
        "negotiation": "negotiation",
        "cancelado": "cancelled",
        "cancelled": "cancelled",
        "canceled": "cancelled",
    }.get(normalized)


def _partner_money(value: Any) -> str:
    amount = float(value or 0)
    formatted = f"{amount:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"R$ {formatted}"


def _partner_financial_history_embed(
    guild: discord.Guild,
    partner_id: int,
    projects: list[dict[str, Any]],
    commissions: list[dict[str, Any]],
) -> discord.Embed:
    embed = discord.Embed(
        title="Lançamentos do Parceiro",
        description=f"Projetos e comissões recentes de <@{partner_id}>.",
        color=discord.Color(DEFAULT_COLOR),
        timestamp=datetime.now(timezone.utc),
    )
    status_labels = {
        "closed": "Fechado",
        "fechado": "Fechado",
        "negotiation": "Em negociação",
        "negociacao": "Em negociação",
        "cancelled": "Cancelado",
    }
    for project in projects[:10]:
        status = str(project.get("status") or "")
        embed.add_field(
            name=_trim(str(project.get("name") or "Projeto"), 256),
            value=(
                f"Status: **{status_labels.get(status, status.title() or 'Não informado')}**\n"
                f"Valor: **{_partner_money(project.get('revenue', 0))}**\n"
                f"ID: `{project.get('project_id')}`"
            ),
            inline=True,
        )
    for commission in commissions[:10]:
        paid = str(commission.get("status") or "pending") == "paid"
        embed.add_field(
            name=f"Comissão • {'Paga' if paid else 'Pendente'}",
            value=(
                f"Valor: **{_partner_money(commission.get('amount', 0))}**\n"
                f"Projeto: `{commission.get('project_id') or 'sem vínculo'}`\n"
                f"ID: `{commission.get('commission_id')}`"
            ),
            inline=True,
        )
    if not projects and not commissions:
        embed.description = f"Ainda não há lançamentos para <@{partner_id}>."
    embed.set_footer(text=f"{guild.name} | Gestão financeira de parceiros")
    return embed


async def _interaction_reply(
    interaction: discord.Interaction,
    content: str | None = None,
    *,
    embed: discord.Embed | None = None,
    embeds: list[discord.Embed] | None = None,
    view: discord.ui.View | None = None,
) -> None:
    kwargs = {
        "content": content,
        "embed": embed,
        "embeds": embeds,
        "view": view,
        "ephemeral": True,
        "allowed_mentions": discord.AllowedMentions.none(),
    }
    kwargs = {key: value for key, value in kwargs.items() if value is not None}
    if interaction.response.is_done():
        await interaction.followup.send(**kwargs)
    else:
        await interaction.response.send_message(**kwargs)


def _button_style(value: str) -> discord.ButtonStyle:
    value = value.lower().strip()
    return {
        "primary": discord.ButtonStyle.primary,
        "secondary": discord.ButtonStyle.secondary,
        "success": discord.ButtonStyle.success,
        "danger": discord.ButtonStyle.danger,
    }.get(value, discord.ButtonStyle.primary)


def _panel_image_position(value: Any) -> str:
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


def _partner_image_embed(image_url: str, color: int, title: str | None = None) -> discord.Embed:
    embed = discord.Embed(color=discord.Color(_safe_color(color, DEFAULT_COLOR)))
    embed.set_image(url=image_url)
    if title:
        embed.set_footer(text=_trim(title, 2048))
    return embed


def _safe_color(value: Any, default: int) -> int:
    if value is None:
        return default
    try:
        if isinstance(value, str):
            value = value.strip().removeprefix("#")
            return int(value, 16)
        return int(value)
    except (TypeError, ValueError):
        return default


def _optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _valid_url_or_none(value: Any) -> str | None:
    text = str(value or "").strip()
    if text.startswith(("https://", "http://")):
        return text
    return None


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


def _trim(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def _json_loads(value: str, default: Any) -> Any:
    try:
        data = json.loads(value)
        return data
    except (TypeError, json.JSONDecodeError):
        return default


def _extract_json_object(value: str) -> dict[str, Any]:
    text = str(value or "").strip()
    if text.startswith("```"):
        text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        text = text[start : end + 1]
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("A IA não retornou um objeto JSON.")
    return data


def _slug(value: str) -> str:
    text = re.sub(r"[^a-zA-Z0-9]+", "-", value.lower()).strip("-")
    return text or "usuario"


def application_status_label(status: str) -> str:
    return APPLICATION_STATUS_LABELS.get(str(status or "").strip(), str(status or "desconhecido"))


def _application_status_label(status: str) -> str:
    return application_status_label(status)


def _application_thread_name(
    application: dict[str, Any],
    *,
    status: str | None = None,
    member_name: str | None = None,
) -> str:
    status_text = _application_status_label(status or str(application.get("status") or "interviewing"))
    application_id = str(application.get("id") or "PAR").lower()
    fallback_name = f"usuario-{application.get('discord_user_id') or 'candidato'}"
    candidate_slug = _slug(member_name or fallback_name)[:28]
    return _trim(f"{status_text} · {application_id} · {candidate_slug}", 100)


def _application_admin_embed(
    application: dict[str, Any],
    *,
    candidate: discord.Member | None = None,
) -> discord.Embed:
    ticket_id = int(application.get("ticket_id") or 0)
    ticket_url = (
        f"https://discord.com/channels/{application['guild_id']}/{ticket_id}"
        if ticket_id
        else "sem thread"
    )
    user_id = int(application.get("discord_user_id") or 0)
    embed = discord.Embed(
        title=f"Inscrição {application.get('id')}",
        description=(
            f"Candidato: {candidate.mention if candidate else f'<@{user_id}>'}\n"
            f"ID do usuário: `{user_id}`\n"
            f"Status: **{_application_status_label(str(application.get('status') or ''))}**\n"
            f"Thread: {ticket_url}"
        ),
        color=discord.Color(DEFAULT_COLOR),
        timestamp=datetime.now(timezone.utc),
    )
    if application.get("started_at"):
        embed.add_field(name="Início", value=str(application["started_at"])[:19], inline=True)
    if application.get("completed_at"):
        embed.add_field(name="Conclusão", value=str(application["completed_at"])[:19], inline=True)
    if application.get("reviewed_at"):
        embed.add_field(name="Revisão", value=str(application["reviewed_at"])[:19], inline=True)
    embed.set_footer(text="Painel administrativo de parceiros")
    return embed


def _workspace_result_embed(result: PartnerWorkspaceResult) -> discord.Embed:
    embed = discord.Embed(
        title="Área de parceiros sincronizada",
        description=(
            "A categoria, os canais e os painéis fixos do Programa de Parceiros foram "
            "configurados. Executar o botão novamente reutiliza o que já existe."
        ),
        color=discord.Color.green(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(
        name="Estrutura",
        value=(
            f"Categoria: <#{result.category_id}>\n"
            f"Cargo: <@&{result.partner_role_id}>\n"
            f"Categoria criada agora: **{'sim' if result.created_category else 'não'}**\n"
            f"Cargo criado agora: **{'sim' if result.created_role else 'não'}**"
        ),
        inline=False,
    )
    embed.add_field(
        name="Canais criados",
        value=", ".join(result.created_channels) or "Nenhum; a estrutura existente foi reutilizada.",
        inline=False,
    )
    if result.removed_channels:
        embed.add_field(
            name="Canais antigos removidos",
            value=", ".join(result.removed_channels),
            inline=False,
        )
    embed.add_field(
        name="Canais ativos",
        value="\n".join(
            f"**{identifier}:** <#{channel_id}>"
            for identifier, channel_id in result.channel_ids.items()
        )[:1024],
        inline=False,
    )
    embed.set_footer(text="Bella Partner Program")
    return embed


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _last_jarvis_question(history: list[dict[str, Any]]) -> str:
    for item in reversed(history):
        if str(item.get("author_type") or "") == "jarvis":
            return str(item.get("content") or "")
    return ""


def _prompt_memory_snapshot(memory: dict[str, Any]) -> dict[str, Any]:
    snapshot: dict[str, Any] = {}
    for category, fields in memory.items():
        if not isinstance(fields, dict):
            continue
        values: dict[str, Any] = {}
        for field, payload in fields.items():
            if not isinstance(payload, dict):
                continue
            value = str(payload.get("value") or "").strip()
            status = str(payload.get("status") or "").strip()
            if value or status in {"partial", "confirmed"}:
                values[field] = {
                    "value": value,
                    "status": status,
                    "confidence": str(payload.get("confidence") or "low"),
                }
        if values:
            snapshot[category] = values
    return snapshot


def _reply_is_repetitive(reply: str, history: list[dict[str, Any]]) -> bool:
    normalized = _normalize_text(reply)
    if not normalized:
        return True
    if normalized.startswith("entendi que"):
        return True
    recent = [
        _normalize_text(str(item.get("content") or ""))
        for item in history[-8:]
        if str(item.get("author_type") or "") == "jarvis"
    ]
    opening = normalized[:120]
    if sum(1 for previous in recent if previous[:120] == opening) >= 1:
        return True
    repeated_markers = (
        "programa de parceiros pode ser uma boa oportunidade",
        "quero saber mais sobre como voce se sente em relacao ao programa",
        "como voce se sente em relacao ao programa",
        "agora quero saber mais sobre",
        "entendi que voce",
    )
    if any(marker in normalized and any(marker in item for item in recent) for marker in repeated_markers):
        return True
    compact = " ".join(normalized.split())
    for previous in recent:
        previous_compact = " ".join(previous.split())
        if len(compact) > 80 and difflib.SequenceMatcher(None, compact, previous_compact).ratio() >= 0.72:
            return True
    return False


def _reply_does_not_advance(reply: str, memory: dict[str, Any]) -> bool:
    normalized = _normalize_text(reply)
    if "?" not in reply:
        return True
    if "como voce se sente em relacao ao programa" in normalized:
        return True
    if "programa de parceiros pode ser uma boa oportunidade" in normalized:
        return True
    if "agora quero saber mais sobre como voce se sente" in normalized:
        return True
    if normalized.startswith("entendi que"):
        return True
    return False


def _official_page_snippet(page_text: str, normalized_query: str) -> str:
    if not page_text:
        return ""
    sentences = re.split(r"(?<=[.!?])\s+", page_text)
    query_terms = {
        term
        for term in re.findall(r"[a-zA-ZÀ-ÿ0-9]{4,}", normalized_query)
        if term not in {"jarvis", "voce", "sobre", "como", "quanto", "quero"}
    }
    if not query_terms:
        return ""
    scored: list[tuple[int, str]] = []
    for sentence in sentences:
        normalized_sentence = _normalize_text(sentence)
        score = sum(1 for term in query_terms if term in normalized_sentence)
        if score:
            scored.append((score, sentence.strip()))
    if not scored:
        return ""
    scored.sort(key=lambda item: item[0], reverse=True)
    snippet = " ".join(sentence for _, sentence in scored[:2])
    return _trim(snippet, 500)


def _partner_program_knowledge_is_stale(text: str) -> bool:
    normalized = _normalize_text(text)
    return (
        "comissao de 10%" in normalized
        or "comissao de dez por cento" in normalized
        or ("r$ 5.000" in text and "r$ 500" in text)
    )


def _fetch_partner_program_page() -> str:
    request = urllib.request.Request(
        PARTNER_PROGRAM_URL,
        headers={
            "User-Agent": "BellaBot/1.0 (+https://www.talissonsouza.com.br)",
            "Accept": "text/html,application/xhtml+xml",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            raw = response.read(500_000)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        LOGGER.warning("Nao foi possivel buscar pagina oficial de afiliado: %s", exc)
        return PROGRAM_KNOWLEDGE
    html = raw.decode("utf-8", errors="replace")
    html = re.sub(r"(?is)<script.*?</script>|<style.*?</style>", " ", html)
    html = re.sub(r"(?is)<[^>]+>", " ", html)
    html = re.sub(r"\s+", " ", html).strip()
    if len(html) < 300:
        return f"{PROGRAM_KNOWLEDGE}\n\nFonte oficial consultada: {PARTNER_PROGRAM_URL}\nTitulo detectado: {html}"
    return html


def _bot_was_invoked(message: discord.Message, bot_user: discord.ClientUser | None) -> bool:
    if bot_user is None:
        return False
    if any(user.id == bot_user.id for user in message.mentions):
        return True
    content = message.content.lower()
    return "jarvis" in content


def _looks_like_status_followup(content: str) -> bool:
    normalized = _normalize_text(content)
    return any(
        term in normalized
        for term in (
            "status",
            "andamento",
            "analise",
            "aprovou",
            "aprovado",
            "resultado",
            "inscricao",
            "minha aplicacao",
            "minha candidatura",
            "quando sai",
            "e agora",
            "proximos passos",
        )
    )
