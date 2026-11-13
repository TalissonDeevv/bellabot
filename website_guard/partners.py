from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable

import discord

from .identity import OWNER_DISCORD_ID
from .partner_cards import (
    render_approach_card,
    render_commission_card,
    render_evolution_card,
    render_funnel_card,
    render_history_card,
    render_hub_card,
    render_invite_card,
    render_profile_card,
    render_rules_card,
)
from .state import StateStore

LOGGER = logging.getLogger(__name__)

PARTNER_PANEL_CUSTOM_ID = "jarvis:partners:create-invite"
PARTNER_RECORD_SECTION = "smart_tickets"
PARTNER_WORKSPACE_SECTION = "partner_workspace"
PARTNER_COLOR = 0x2F81F7
PARTNER_PRIMARY_COLOR = 0x5865F2
PARTNER_SUCCESS_COLOR = 0x248046
PARTNER_SECONDARY_COLOR = 0x4E5058
PARTNER_V2_ACCENT_COLOR = 0xC27C2B
PARTNER_PROGRAM_URL = "https://www.talissonsouza.com.br/seja-afiliado"
PARTNER_CONTACT_EMAIL = "parcerias@talissonsouza.com.br"
PARTNER_REFERRAL_REPORT_CHANNEL_ID = 1552631041175461979
PARTNER_REFERRAL_CONTACT_TYPES = {
    "whatsapp": "WhatsApp",
    "email": "E-mail",
    "phone": "Telefone",
    "discord": "Discord",
    "other": "Outro contato",
}
PARTNER_REFERRAL_STATUS_LABELS = {
    "submitted": "Enviada para análise",
    "reviewing": "Em análise",
    "accepted": "Aceita",
    "negotiation": "Em negociação",
    "closed": "Projeto fechado",
    "completed": "Projeto finalizado",
    "rejected": "Recusada",
}
DEPRECATED_PARTNER_WORKSPACE_CHANNELS = {
    "networking": "🌐・networking",
    "opportunities": "💼・oportunidades",
    "strategies": "💡・ideias-e-estrategias",
    "results": "🏆・resultados-e-conquistas",
    "shared_materials": "📎・troca-de-materiais",
    "community_questions": "❓・duvidas-parceiros",
}


class _ClosingSQLiteConnection(sqlite3.Connection):
    def __exit__(self, exc_type: object, exc: object, traceback: object) -> bool:
        try:
            return bool(super().__exit__(exc_type, exc, traceback))
        finally:
            self.close()


@dataclass(frozen=True)
class PartnerWorkspaceChannel:
    identifier: str
    name: str
    topic: str
    access: str
    kind: str = "text"


PARTNER_WORKSPACE_CHANNELS: tuple[PartnerWorkspaceChannel, ...] = (
    PartnerWorkspaceChannel(
        "public",
        "📣・seja-parceiro",
        "Inscrição oficial no Programa de Parceiros e Afiliados.",
        "public_readonly",
    ),
    PartnerWorkspaceChannel(
        "hub",
        "🤝・central-parceiros",
        "Painel pessoal, convite, resultados e histórico dos parceiros.",
        "partner_readonly",
    ),
    PartnerWorkspaceChannel(
        "announcements",
        "📢・avisos-parceiros",
        "Comunicados oficiais, mudanças e novidades do Programa de Parceiros.",
        "partner_readonly",
    ),
    PartnerWorkspaceChannel(
        "indications",
        "📨・indicacoes",
        "Registro e acompanhamento privado das indicações enviadas.",
        "partner_readonly",
    ),
    PartnerWorkspaceChannel(
        "materials",
        "📚・materiais-parceiros",
        "Materiais, abordagens, orientações e recursos do programa.",
        "partner_readonly",
    ),
    PartnerWorkspaceChannel(
        "support",
        "🆘・suporte-parceiros",
        "Abra um atendimento privado com a Bella para receber suporte do programa.",
        "partner_readonly",
    ),
    PartnerWorkspaceChannel(
        "chat",
        "💬・chat-parceiros",
        "Conversa, networking e troca de experiências entre parceiros aprovados.",
        "partner",
    ),
    PartnerWorkspaceChannel(
        "voice",
        "🔊・call-parceiros",
        "Canal de voz exclusivo para parceiros aprovados.",
        "partner_voice",
        "voice",
    ),
    PartnerWorkspaceChannel(
        "meetings_voice",
        "🎙️・reunioes-parceiros",
        "Sala de voz para reuniões, alinhamentos e sessões entre parceiros.",
        "partner_voice",
        "voice",
    ),
    PartnerWorkspaceChannel(
        "admin",
        "⚙️・gestao-parceiros",
        "Gestão administrativa de inscrições, indicações e painéis.",
        "admin",
    ),
)


@dataclass(frozen=True)
class PartnerWorkspaceResult:
    category_id: int
    partner_role_id: int
    channel_ids: dict[str, int]
    created_category: bool
    created_role: bool
    created_channels: tuple[str, ...]
    reused_channels: tuple[str, ...]
    removed_channels: tuple[str, ...]
    panel_message_ids: dict[str, int]


@dataclass(frozen=True)
class PartnerSettings:
    guild_id: int
    panel_channel_id: int | None = None
    panel_message_id: int | None = None
    partner_role_id: int | None = None
    log_channel_id: int | None = None
    invite_duration_days: int = 30
    color: int = PARTNER_COLOR

    @classmethod
    def from_row(cls, row: sqlite3.Row | dict[str, Any] | None) -> "PartnerSettings | None":
        if row is None:
            return None
        data = dict(row)
        return cls(
            guild_id=int(data.get("guild_id") or 0),
            panel_channel_id=_optional_int(data.get("panel_channel_id")),
            panel_message_id=_optional_int(data.get("panel_message_id")),
            partner_role_id=_optional_int(data.get("partner_role_id")),
            log_channel_id=_optional_int(data.get("log_channel_id")),
            invite_duration_days=max(1, int(data.get("invite_duration_days") or 30)),
            color=int(data.get("color") or PARTNER_COLOR),
        )


class PartnerDatabase:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, factory=_ClosingSQLiteConnection)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def _init(self) -> None:
        with self._lock, self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS partner_settings (
                    guild_id INTEGER PRIMARY KEY,
                    panel_channel_id INTEGER,
                    panel_message_id INTEGER,
                    partner_role_id INTEGER,
                    log_channel_id INTEGER,
                    invite_duration_days INTEGER NOT NULL DEFAULT 30,
                    color INTEGER NOT NULL DEFAULT 3113463,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS partners (
                    guild_id INTEGER NOT NULL,
                    partner_id INTEGER NOT NULL,
                    role_id INTEGER,
                    status TEXT NOT NULL DEFAULT 'active',
                    joined_at TEXT NOT NULL,
                    removed_at TEXT,
                    notes TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (guild_id, partner_id)
                );

                CREATE TABLE IF NOT EXISTS partner_invites (
                    guild_id INTEGER NOT NULL,
                    partner_id INTEGER NOT NULL,
                    invite_code TEXT NOT NULL,
                    invite_url TEXT NOT NULL,
                    visual_role_id INTEGER,
                    source_channel_id INTEGER,
                    created_at TEXT NOT NULL,
                    expires_at TEXT,
                    status TEXT NOT NULL DEFAULT 'active',
                    PRIMARY KEY (guild_id, invite_code)
                );

                CREATE INDEX IF NOT EXISTS idx_partner_invites_partner
                    ON partner_invites(guild_id, partner_id, status);

                CREATE TABLE IF NOT EXISTS partner_members (
                    guild_id INTEGER NOT NULL,
                    member_id INTEGER NOT NULL,
                    partner_id INTEGER NOT NULL,
                    invite_code TEXT,
                    joined_at TEXT NOT NULL,
                    left_at TEXT,
                    status TEXT NOT NULL DEFAULT 'active',
                    is_client INTEGER NOT NULL DEFAULT 0,
                    project_status TEXT NOT NULL DEFAULT '',
                    observations TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (guild_id, member_id)
                );

                CREATE INDEX IF NOT EXISTS idx_partner_members_partner
                    ON partner_members(guild_id, partner_id, status);

                CREATE TABLE IF NOT EXISTS partner_projects (
                    project_id TEXT PRIMARY KEY,
                    guild_id INTEGER NOT NULL,
                    partner_id INTEGER NOT NULL,
                    member_id INTEGER,
                    name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    revenue REAL NOT NULL DEFAULT 0,
                    notes TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_partner_projects_partner
                    ON partner_projects(guild_id, partner_id, status);

                CREATE TABLE IF NOT EXISTS partner_commissions (
                    commission_id TEXT PRIMARY KEY,
                    guild_id INTEGER NOT NULL,
                    partner_id INTEGER NOT NULL,
                    project_id TEXT,
                    amount REAL NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'pending',
                    description TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    paid_at TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_partner_commissions_partner
                    ON partner_commissions(guild_id, partner_id, status);

                CREATE TABLE IF NOT EXISTS partner_statistics (
                    guild_id INTEGER NOT NULL,
                    partner_id INTEGER NOT NULL,
                    key TEXT NOT NULL,
                    value REAL NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (guild_id, partner_id, key)
                );

                CREATE TABLE IF NOT EXISTS partner_logs (
                    log_id TEXT PRIMARY KEY,
                    guild_id INTEGER NOT NULL,
                    partner_id INTEGER,
                    actor_id INTEGER,
                    event_type TEXT NOT NULL,
                    message TEXT NOT NULL,
                    payload TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_partner_logs_guild
                    ON partner_logs(guild_id, created_at);

                CREATE TABLE IF NOT EXISTS partner_referrals (
                    referral_id TEXT PRIMARY KEY,
                    guild_id INTEGER NOT NULL,
                    partner_id INTEGER NOT NULL,
                    contact_name TEXT NOT NULL,
                    contact_method TEXT NOT NULL,
                    need TEXT NOT NULL,
                    context TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'submitted',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_partner_referrals_partner
                    ON partner_referrals(guild_id, partner_id, created_at);
                """
            )
            referral_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(partner_referrals)").fetchall()
            }
            for column, definition in (
                ("contact_type", "TEXT NOT NULL DEFAULT 'other'"),
                ("report_channel_id", "INTEGER"),
                ("report_message_id", "INTEGER"),
                ("status_note", "TEXT NOT NULL DEFAULT ''"),
                ("reviewed_by", "INTEGER"),
            ):
                if column not in referral_columns:
                    connection.execute(
                        f"ALTER TABLE partner_referrals ADD COLUMN {column} {definition}"
                    )

    def save_settings(self, settings: PartnerSettings) -> None:
        now = _now()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO partner_settings (
                    guild_id, panel_channel_id, panel_message_id, partner_role_id,
                    log_channel_id, invite_duration_days, color, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET
                    panel_channel_id=excluded.panel_channel_id,
                    panel_message_id=excluded.panel_message_id,
                    partner_role_id=excluded.partner_role_id,
                    log_channel_id=excluded.log_channel_id,
                    invite_duration_days=excluded.invite_duration_days,
                    color=excluded.color,
                    updated_at=excluded.updated_at
                """,
                (
                    settings.guild_id,
                    settings.panel_channel_id,
                    settings.panel_message_id,
                    settings.partner_role_id,
                    settings.log_channel_id,
                    settings.invite_duration_days,
                    settings.color,
                    now,
                ),
            )

    def settings(self, guild_id: int) -> PartnerSettings | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM partner_settings WHERE guild_id=?",
                (guild_id,),
            ).fetchone()
        return PartnerSettings.from_row(row)

    def upsert_partner(
        self,
        guild_id: int,
        partner_id: int,
        *,
        role_id: int | None = None,
        notes: str = "",
        joined_at: str | None = None,
    ) -> dict[str, Any]:
        now = _now()
        joined_value = joined_at or now
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO partners (
                    guild_id, partner_id, role_id, status, joined_at, removed_at, notes
                ) VALUES (?, ?, ?, 'active', ?, NULL, ?)
                ON CONFLICT(guild_id, partner_id) DO UPDATE SET
                    role_id=COALESCE(excluded.role_id, partners.role_id),
                    status='active',
                    joined_at=CASE
                        WHEN ? = 1 THEN excluded.joined_at
                        ELSE partners.joined_at
                    END,
                    removed_at=NULL,
                    notes=CASE
                        WHEN excluded.notes != '' THEN excluded.notes
                        ELSE partners.notes
                    END
                """,
                (
                    guild_id,
                    partner_id,
                    role_id,
                    joined_value,
                    notes,
                    1 if joined_at else 0,
                ),
            )
            row = connection.execute(
                "SELECT * FROM partners WHERE guild_id=? AND partner_id=?",
                (guild_id, partner_id),
            ).fetchone()
        return dict(row)

    def remove_partner(self, guild_id: int, partner_id: int) -> None:
        now = _now()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE partners
                   SET status='inactive', removed_at=?
                 WHERE guild_id=? AND partner_id=?
                """,
                (now, guild_id, partner_id),
            )
            connection.execute(
                """
                UPDATE partner_invites
                   SET status='inactive'
                 WHERE guild_id=? AND partner_id=?
                """,
                (guild_id, partner_id),
            )

    def reset_partner(self, guild_id: int, partner_id: int) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                "DELETE FROM partner_members WHERE guild_id=? AND partner_id=?",
                (guild_id, partner_id),
            )
            connection.execute(
                "DELETE FROM partner_projects WHERE guild_id=? AND partner_id=?",
                (guild_id, partner_id),
            )
            connection.execute(
                "DELETE FROM partner_commissions WHERE guild_id=? AND partner_id=?",
                (guild_id, partner_id),
            )
            connection.execute(
                "DELETE FROM partner_statistics WHERE guild_id=? AND partner_id=?",
                (guild_id, partner_id),
            )

    def partner(self, guild_id: int, partner_id: int) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM partners WHERE guild_id=? AND partner_id=?",
                (guild_id, partner_id),
            ).fetchone()
        return dict(row) if row else None

    def active_partners(self, guild_id: int) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM partners
                 WHERE guild_id=? AND status='active'
                 ORDER BY joined_at ASC
                """,
                (guild_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def save_invite(
        self,
        *,
        guild_id: int,
        partner_id: int,
        invite_code: str,
        invite_url: str,
        visual_role_id: int | None,
        source_channel_id: int | None,
        expires_at: str | None,
    ) -> dict[str, Any]:
        now = _now()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO partner_invites (
                    guild_id, partner_id, invite_code, invite_url, visual_role_id,
                    source_channel_id, created_at, expires_at, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active')
                ON CONFLICT(guild_id, invite_code) DO UPDATE SET
                    partner_id=excluded.partner_id,
                    invite_url=excluded.invite_url,
                    visual_role_id=excluded.visual_role_id,
                    source_channel_id=excluded.source_channel_id,
                    expires_at=excluded.expires_at,
                    status='active'
                """,
                (
                    guild_id,
                    partner_id,
                    invite_code,
                    invite_url,
                    visual_role_id,
                    source_channel_id,
                    now,
                    expires_at,
                ),
            )
            row = connection.execute(
                """
                SELECT * FROM partner_invites
                 WHERE guild_id=? AND invite_code=?
                """,
                (guild_id, invite_code),
            ).fetchone()
        return dict(row)

    def active_invite_for_partner(
        self,
        guild_id: int,
        partner_id: int,
    ) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM partner_invites
                 WHERE guild_id=? AND partner_id=? AND status='active'
                 ORDER BY created_at DESC
                 LIMIT 1
                """,
                (guild_id, partner_id),
            ).fetchone()
        return dict(row) if row else None

    def deactivate_partner_invites(
        self,
        guild_id: int,
        partner_id: int,
        *,
        except_code: str | None = None,
    ) -> int:
        with self._lock, self._connect() as connection:
            if except_code:
                cursor = connection.execute(
                    """
                    UPDATE partner_invites
                       SET status='inactive'
                     WHERE guild_id=? AND partner_id=? AND invite_code<>?
                    """,
                    (guild_id, partner_id, except_code),
                )
            else:
                cursor = connection.execute(
                    """
                    UPDATE partner_invites
                       SET status='inactive'
                     WHERE guild_id=? AND partner_id=?
                    """,
                    (guild_id, partner_id),
                )
        return max(0, int(cursor.rowcount or 0))

    def invite_by_code(self, guild_id: int, invite_code: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM partner_invites
                 WHERE guild_id=? AND invite_code=? AND status='active'
                """,
                (guild_id, invite_code),
            ).fetchone()
        return dict(row) if row else None

    def register_member_join(
        self,
        *,
        guild_id: int,
        member_id: int,
        partner_id: int,
        invite_code: str,
    ) -> dict[str, Any]:
        now = _now()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO partner_members (
                    guild_id, member_id, partner_id, invite_code, joined_at,
                    left_at, status
                ) VALUES (?, ?, ?, ?, ?, NULL, 'active')
                ON CONFLICT(guild_id, member_id) DO UPDATE SET
                    partner_id=excluded.partner_id,
                    invite_code=excluded.invite_code,
                    joined_at=excluded.joined_at,
                    left_at=NULL,
                    status='active'
                """,
                (guild_id, member_id, partner_id, invite_code, now),
            )
            row = connection.execute(
                """
                SELECT * FROM partner_members
                 WHERE guild_id=? AND member_id=?
                """,
                (guild_id, member_id),
            ).fetchone()
        return dict(row)

    def mark_member_left(self, guild_id: int, member_id: int) -> dict[str, Any] | None:
        now = _now()
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM partner_members
                 WHERE guild_id=? AND member_id=? AND status='active'
                """,
                (guild_id, member_id),
            ).fetchone()
            if not row:
                return None
            connection.execute(
                """
                UPDATE partner_members
                   SET status='left', left_at=?
                 WHERE guild_id=? AND member_id=?
                """,
                (now, guild_id, member_id),
            )
        data = dict(row)
        data["left_at"] = now
        data["status"] = "left"
        return data

    def member_referral(self, guild_id: int, member_id: int) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT pm.*, pi.invite_url
                  FROM partner_members pm
             LEFT JOIN partner_invites pi
                    ON pi.guild_id=pm.guild_id AND pi.invite_code=pm.invite_code
                 WHERE pm.guild_id=? AND pm.member_id=?
                """,
                (guild_id, member_id),
            ).fetchone()
        return dict(row) if row else None

    def link_member(
        self,
        *,
        guild_id: int,
        member_id: int,
        partner_id: int,
        invite_code: str = "manual",
        observations: str = "",
    ) -> dict[str, Any]:
        now = _now()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO partner_members (
                    guild_id, member_id, partner_id, invite_code, joined_at,
                    left_at, status, observations
                ) VALUES (?, ?, ?, ?, ?, NULL, 'active', ?)
                ON CONFLICT(guild_id, member_id) DO UPDATE SET
                    partner_id=excluded.partner_id,
                    invite_code=excluded.invite_code,
                    status='active',
                    left_at=NULL,
                    observations=excluded.observations
                """,
                (guild_id, member_id, partner_id, invite_code, now, observations),
            )
            row = connection.execute(
                "SELECT * FROM partner_members WHERE guild_id=? AND member_id=?",
                (guild_id, member_id),
            ).fetchone()
        return dict(row)

    def unlink_member(self, guild_id: int, member_id: int) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                "DELETE FROM partner_members WHERE guild_id=? AND member_id=?",
                (guild_id, member_id),
            )

    def register_client(
        self,
        *,
        guild_id: int,
        member_id: int,
        partner_id: int,
        observations: str = "",
    ) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO partner_members (
                    guild_id, member_id, partner_id, invite_code, joined_at,
                    status, is_client, observations
                ) VALUES (?, ?, ?, 'manual', ?, 'active', 1, ?)
                ON CONFLICT(guild_id, member_id) DO UPDATE SET
                    partner_id=excluded.partner_id,
                    is_client=1,
                    observations=CASE
                        WHEN excluded.observations != '' THEN excluded.observations
                        ELSE partner_members.observations
                    END
                """,
                (guild_id, member_id, partner_id, _now(), observations),
            )

    def register_project(
        self,
        *,
        guild_id: int,
        partner_id: int,
        member_id: int | None,
        name: str,
        status: str,
        revenue: float,
        notes: str = "",
    ) -> dict[str, Any]:
        project_id = uuid.uuid4().hex[:12]
        now = _now()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO partner_projects (
                    project_id, guild_id, partner_id, member_id, name, status,
                    revenue, notes, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    project_id,
                    guild_id,
                    partner_id,
                    member_id,
                    name,
                    status,
                    max(0.0, revenue),
                    notes,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM partner_projects WHERE project_id=?",
                (project_id,),
            ).fetchone()
        return dict(row)

    def create_referral(
        self,
        *,
        guild_id: int,
        partner_id: int,
        contact_name: str,
        contact_method: str,
        need: str,
        context: str = "",
        contact_type: str = "other",
    ) -> dict[str, Any]:
        referral_id = uuid.uuid4().hex[:12]
        now = _now()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO partner_referrals (
                    referral_id, guild_id, partner_id, contact_name,
                    contact_method, need, context, status, created_at, updated_at,
                    contact_type
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'submitted', ?, ?, ?)
                """,
                (
                    referral_id,
                    guild_id,
                    partner_id,
                    contact_name[:120],
                    contact_method[:200],
                    need[:500],
                    context[:1000],
                    now,
                    now,
                    contact_type[:30],
                ),
            )
            row = connection.execute(
                "SELECT * FROM partner_referrals WHERE referral_id=?",
                (referral_id,),
            ).fetchone()
        return dict(row)

    def set_referral_report_location(
        self,
        referral_id: str,
        *,
        channel_id: int,
        message_id: int,
    ) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE partner_referrals
                   SET report_channel_id=?, report_message_id=?, updated_at=?
                 WHERE referral_id=?
                """,
                (channel_id, message_id, _now(), referral_id),
            )

    def referral_by_report_message(
        self,
        guild_id: int,
        message_id: int,
    ) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM partner_referrals
                 WHERE guild_id=? AND report_message_id=?
                 LIMIT 1
                """,
                (guild_id, message_id),
            ).fetchone()
        return dict(row) if row else None

    def referral(self, referral_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM partner_referrals WHERE referral_id=?",
                (referral_id,),
            ).fetchone()
        return dict(row) if row else None

    def update_referral_status(
        self,
        referral_id: str,
        *,
        status: str,
        note: str = "",
        reviewed_by: int | None = None,
    ) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE partner_referrals
                   SET status=?, status_note=?, reviewed_by=?, updated_at=?
                 WHERE referral_id=?
                """,
                (status, note[:1000], reviewed_by, _now(), referral_id),
            )
            row = connection.execute(
                "SELECT * FROM partner_referrals WHERE referral_id=?",
                (referral_id,),
            ).fetchone()
        return dict(row) if row else None

    def referrals_for_partner(
        self,
        guild_id: int,
        partner_id: int,
        *,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM partner_referrals
                 WHERE guild_id=? AND partner_id=?
                 ORDER BY created_at DESC
                 LIMIT ?
                """,
                (guild_id, partner_id, max(1, min(100, limit))),
            ).fetchall()
        return [dict(row) for row in rows]

    def referral_statistics(self, guild_id: int, partner_id: int) -> dict[str, int]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT status, COUNT(*) AS total
                  FROM partner_referrals
                 WHERE guild_id=? AND partner_id=?
                 GROUP BY status
                """,
                (guild_id, partner_id),
            ).fetchall()
        statistics = {str(row["status"]): int(row["total"] or 0) for row in rows}
        statistics["total"] = sum(statistics.values())
        return statistics

    def register_commission(
        self,
        *,
        guild_id: int,
        partner_id: int,
        amount: float,
        description: str = "",
        project_id: str | None = None,
    ) -> dict[str, Any]:
        commission_id = uuid.uuid4().hex[:12]
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO partner_commissions (
                    commission_id, guild_id, partner_id, project_id, amount,
                    status, description, created_at
                ) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                (
                    commission_id,
                    guild_id,
                    partner_id,
                    project_id,
                    max(0.0, amount),
                    description,
                    _now(),
                ),
            )
            row = connection.execute(
                "SELECT * FROM partner_commissions WHERE commission_id=?",
                (commission_id,),
            ).fetchone()
        return dict(row)

    def projects_for_partner(
        self,
        guild_id: int,
        partner_id: int,
        *,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM partner_projects
                 WHERE guild_id=? AND partner_id=?
                 ORDER BY created_at DESC
                 LIMIT ?
                """,
                (guild_id, partner_id, max(1, min(50, limit))),
            ).fetchall()
        return [dict(row) for row in rows]

    def commissions_for_partner(
        self,
        guild_id: int,
        partner_id: int,
        *,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM partner_commissions
                 WHERE guild_id=? AND partner_id=?
                 ORDER BY created_at DESC
                 LIMIT ?
                """,
                (guild_id, partner_id, max(1, min(50, limit))),
            ).fetchall()
        return [dict(row) for row in rows]

    def pay_commission(
        self,
        *,
        guild_id: int,
        partner_id: int,
        amount: float | None,
    ) -> float:
        now = _now()
        paid = 0.0
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT commission_id, amount
                  FROM partner_commissions
                 WHERE guild_id=? AND partner_id=? AND status='pending'
                 ORDER BY created_at ASC
                """,
                (guild_id, partner_id),
            ).fetchall()
            remaining = None if amount is None else max(0.0, amount)
            for row in rows:
                value = float(row["amount"] or 0)
                if remaining is not None and remaining <= 0:
                    break
                if remaining is not None and value > remaining:
                    continue
                connection.execute(
                    """
                    UPDATE partner_commissions
                       SET status='paid', paid_at=?
                     WHERE commission_id=?
                    """,
                    (now, row["commission_id"]),
                )
                paid += value
                if remaining is not None:
                    remaining -= value
        return paid

    def profile(self, guild_id: int, partner_id: int) -> dict[str, Any]:
        with self._lock, self._connect() as connection:
            partner = connection.execute(
                "SELECT * FROM partners WHERE guild_id=? AND partner_id=?",
                (guild_id, partner_id),
            ).fetchone()
            invite = connection.execute(
                """
                SELECT * FROM partner_invites
                 WHERE guild_id=? AND partner_id=? AND status='active'
                 ORDER BY created_at DESC
                 LIMIT 1
                """,
                (guild_id, partner_id),
            ).fetchone()
            member_stats = connection.execute(
                """
                SELECT
                    COUNT(*) AS invited_total,
                    SUM(CASE WHEN status='active' THEN 1 ELSE 0 END) AS active_total,
                    SUM(CASE WHEN status='left' THEN 1 ELSE 0 END) AS left_total,
                    SUM(CASE WHEN is_client=1 THEN 1 ELSE 0 END) AS clients_total
                  FROM partner_members
                 WHERE guild_id=? AND partner_id=?
                """,
                (guild_id, partner_id),
            ).fetchone()
            project_stats = connection.execute(
                """
                SELECT
                    SUM(CASE WHEN status IN ('closed', 'fechado') THEN 1 ELSE 0 END)
                        AS closed_projects,
                    SUM(CASE WHEN status IN ('negotiation', 'negociacao', 'em_negociacao')
                        THEN 1 ELSE 0 END) AS negotiating_projects,
                    SUM(CASE WHEN status IN ('closed', 'fechado')
                        THEN revenue ELSE 0 END) AS revenue_total
                  FROM partner_projects
                 WHERE guild_id=? AND partner_id=?
                """,
                (guild_id, partner_id),
            ).fetchone()
            commission_stats = connection.execute(
                """
                SELECT
                    SUM(CASE WHEN status='paid' THEN amount ELSE 0 END) AS paid,
                    SUM(CASE WHEN status='pending' THEN amount ELSE 0 END) AS pending
                  FROM partner_commissions
                 WHERE guild_id=? AND partner_id=?
                """,
                (guild_id, partner_id),
            ).fetchone()
        rank = self.rank_position(guild_id, partner_id, "receita")
        return {
            "partner": dict(partner) if partner else None,
            "invite": dict(invite) if invite else None,
            "members": _row_to_numbers(member_stats),
            "projects": _row_to_numbers(project_stats),
            "commissions": _row_to_numbers(commission_stats),
            "rank": rank,
        }

    def history(
        self,
        guild_id: int,
        partner_id: int,
        *,
        limit: int = 10,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM partner_members
                 WHERE guild_id=? AND partner_id=?
                 ORDER BY joined_at DESC
                 LIMIT ? OFFSET ?
                """,
                (guild_id, partner_id, limit, offset),
            ).fetchall()
        return [dict(row) for row in rows]

    def dashboard(self, guild_id: int) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        week = now - timedelta(days=7)
        month = now - timedelta(days=30)
        with self._lock, self._connect() as connection:
            member_rows = connection.execute(
                """
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN joined_at >= ? THEN 1 ELSE 0 END) AS today,
                    SUM(CASE WHEN joined_at >= ? THEN 1 ELSE 0 END) AS week,
                    SUM(CASE WHEN joined_at >= ? THEN 1 ELSE 0 END) AS month,
                    SUM(CASE WHEN is_client=1 THEN 1 ELSE 0 END) AS clients
                  FROM partner_members
                 WHERE guild_id=?
                """,
                (today.isoformat(), week.isoformat(), month.isoformat(), guild_id),
            ).fetchone()
            project_rows = connection.execute(
                """
                SELECT
                    COUNT(*) AS projects,
                    SUM(CASE WHEN status IN ('closed', 'fechado')
                        THEN revenue ELSE 0 END) AS revenue
                  FROM partner_projects
                 WHERE guild_id=?
                """,
                (guild_id,),
            ).fetchone()
            commission_rows = connection.execute(
                """
                SELECT
                    SUM(CASE WHEN status='pending' THEN amount ELSE 0 END)
                        AS commission_pending,
                    SUM(CASE WHEN status='paid' THEN amount ELSE 0 END)
                        AS commission_paid
                  FROM partner_commissions
                 WHERE guild_id=?
                """,
                (guild_id,),
            ).fetchone()
        members = _row_to_numbers(member_rows)
        projects = _row_to_numbers(project_rows)
        commissions = _row_to_numbers(commission_rows)
        total = max(1, int(members.get("total", 0)))
        conversion = (float(members.get("clients", 0)) / total) * 100
        return {
            **members,
            **projects,
            **commissions,
            "conversion": conversion,
        }

    def ranking(
        self,
        guild_id: int,
        metric: str,
        *,
        limit: int = 10,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        expression = _ranking_expression(metric)
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT
                    p.partner_id,
                    COALESCE(ms.invited_total, 0) AS invited_total,
                    COALESCE(ms.clients_total, 0) AS clients_total,
                    COALESCE(ps.projects_total, 0) AS projects_total,
                    COALESCE(ps.revenue_total, 0) AS revenue_total,
                    COALESCE(cs.commission_total, 0) AS commission_total,
                    {expression} AS score
                  FROM partners p
             LEFT JOIN (
                    SELECT guild_id, partner_id,
                           COUNT(*) AS invited_total,
                           SUM(CASE WHEN is_client=1 THEN 1 ELSE 0 END)
                               AS clients_total
                      FROM partner_members
                  GROUP BY guild_id, partner_id
             ) ms ON ms.guild_id=p.guild_id AND ms.partner_id=p.partner_id
             LEFT JOIN (
                    SELECT guild_id, partner_id,
                           COUNT(*) AS projects_total,
                           SUM(CASE WHEN status IN ('closed', 'fechado')
                               THEN revenue ELSE 0 END) AS revenue_total
                      FROM partner_projects
                  GROUP BY guild_id, partner_id
             ) ps ON ps.guild_id=p.guild_id AND ps.partner_id=p.partner_id
             LEFT JOIN (
                    SELECT guild_id, partner_id,
                           SUM(amount) AS commission_total
                      FROM partner_commissions
                  GROUP BY guild_id, partner_id
             ) cs ON cs.guild_id=p.guild_id AND cs.partner_id=p.partner_id
                 WHERE p.guild_id=? AND p.status='active'
              ORDER BY score DESC, p.joined_at ASC
                 LIMIT ? OFFSET ?
                """,
                (guild_id, limit, offset),
            ).fetchall()
        return [dict(row) for row in rows]

    def rank_position(self, guild_id: int, partner_id: int, metric: str) -> int | None:
        rows = self.ranking(guild_id, metric, limit=1000)
        for index, row in enumerate(rows, start=1):
            if int(row["partner_id"]) == partner_id:
                return index
        return None

    def logs(
        self,
        guild_id: int,
        *,
        partner_id: int | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM partner_logs WHERE guild_id=?"
        params: list[Any] = [guild_id]
        if partner_id is not None:
            query += " AND partner_id=?"
            params.append(partner_id)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self._lock, self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def log(
        self,
        *,
        guild_id: int,
        event_type: str,
        message: str,
        partner_id: int | None = None,
        actor_id: int | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        row = {
            "log_id": uuid.uuid4().hex[:12],
            "guild_id": guild_id,
            "partner_id": partner_id,
            "actor_id": actor_id,
            "event_type": event_type,
            "message": message,
            "payload": json.dumps(payload or {}, ensure_ascii=False),
            "created_at": _now(),
        }
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO partner_logs (
                    log_id, guild_id, partner_id, actor_id, event_type,
                    message, payload, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                tuple(row.values()),
            )
        return row


class PartnerProgramManager:
    def __init__(
        self,
        bot: discord.Client,
        state: StateStore,
        *,
        owner_id: int = OWNER_DISCORD_ID,
        admin_role_ids: Iterable[int] = (),
        ticket_opener: Callable[[discord.Interaction], Awaitable[None]] | None = None,
        ticket_configurator: Callable[
            [discord.Guild, discord.TextChannel, discord.Role | None, discord.TextChannel | None],
            Awaitable[None],
        ]
        | None = None,
    ) -> None:
        self.bot = bot
        self.state = state
        self.db = PartnerDatabase(Path(state.path).parent / "partners.db")
        self.owner_id = owner_id
        self.admin_role_ids = {int(role_id) for role_id in admin_role_ids}
        self.ticket_opener = ticket_opener
        self.ticket_configurator = ticket_configurator
        self.invite_cache: dict[int, dict[str, int]] = {}
        self._views_registered = False

    def register_persistent_views(self) -> None:
        if self._views_registered or not hasattr(self.bot, "add_view"):
            return
        self.bot.add_view(PartnerPanelView(self))
        self.bot.add_view(PartnerWorkspaceView(self))
        self.bot.add_view(PartnerIndicationsView(self))
        self.bot.add_view(PartnerSupportView(self))
        self.bot.add_view(PartnerReferralAdminView(self))
        self._views_registered = True

    def is_admin(self, user: discord.abc.User | discord.Member) -> bool:
        if user.id == self.owner_id:
            return True
        if not isinstance(user, discord.Member):
            return False
        if user.guild_permissions.manage_guild or user.guild_permissions.administrator:
            return True
        return any(role.id in self.admin_role_ids for role in user.roles)

    async def settings(self, guild_id: int) -> PartnerSettings | None:
        return await asyncio.to_thread(self.db.settings, guild_id)

    async def workspace_config(self, guild_id: int) -> dict[str, Any]:
        section = await self.state.get_section(PARTNER_WORKSPACE_SECTION)
        value = section.get(str(guild_id), {})
        return value if isinstance(value, dict) else {}

    async def provision_workspace(
        self,
        *,
        guild: discord.Guild,
        actor: discord.Member,
        partner_role: discord.Role | None = None,
        manager_role: discord.Role | None = None,
        log_channel: discord.TextChannel | None = None,
    ) -> PartnerWorkspaceResult:
        has_manager_role = bool(
            manager_role is not None
            and any(role.id == manager_role.id for role in actor.roles)
        )
        if not self.is_admin(actor) and not has_manager_role:
            raise PermissionError("Somente administradores podem criar a área de parceiros.")
        config = await self.workspace_config(guild.id)
        stored_channels = config.get("channels") if isinstance(config.get("channels"), dict) else {}
        role = partner_role or guild.get_role(_optional_int(config.get("partner_role_id")) or 0)
        created_role = False
        if role is None:
            role = discord.utils.find(lambda item: item.name.casefold() == "parceiro", guild.roles)
        if role is None:
            role = await guild.create_role(
                name="Parceiro",
                permissions=discord.Permissions.none(),
                colour=discord.Color(PARTNER_COLOR),
                mentionable=True,
                reason=f"Área de parceiros criada por {actor}",
            )
            created_role = True

        category = guild.get_channel(_optional_int(config.get("category_id")) or 0)
        if not isinstance(category, discord.CategoryChannel):
            category = discord.utils.find(
                lambda item: item.name.casefold() == "🤝 • parceiros".casefold(),
                guild.categories,
            )
        category_overwrites = self._workspace_overwrites(
            guild,
            role,
            manager_role,
            access="partner",
        )
        created_category = False
        if category is None:
            category = await guild.create_category(
                "🤝 • PARCEIROS",
                overwrites=category_overwrites,
                reason=f"Área de parceiros criada por {actor}",
            )
            created_category = True
        else:
            await category.edit(
                overwrites=category_overwrites,
                reason=f"Permissões da área de parceiros sincronizadas por {actor}",
            )

        removed_channels = await self._remove_deprecated_workspace_channels(
            guild,
            category,
            stored_channels,
            actor=actor,
        )
        channels: dict[str, discord.TextChannel | discord.VoiceChannel] = {}
        created_channels: list[str] = []
        reused_channels: list[str] = []
        for definition in PARTNER_WORKSPACE_CHANNELS:
            channel = guild.get_channel(_optional_int(stored_channels.get(definition.identifier)) or 0)
            expected_type = (
                discord.VoiceChannel
                if definition.kind == "voice"
                else discord.TextChannel
            )
            if not isinstance(channel, expected_type):
                available_channels = (
                    guild.voice_channels
                    if definition.kind == "voice"
                    else guild.text_channels
                )
                channel = discord.utils.find(
                    lambda item, expected=definition.name: (
                        item.name == expected and item.category_id == category.id
                    ),
                    available_channels,
                )
            overwrites = self._workspace_overwrites(
                guild,
                role,
                manager_role,
                access=definition.access,
            )
            if channel is None:
                if definition.kind == "voice":
                    channel = await guild.create_voice_channel(
                        definition.name,
                        category=category,
                        overwrites=overwrites,
                        reason=f"Área de parceiros criada por {actor}",
                    )
                else:
                    channel = await guild.create_text_channel(
                        definition.name,
                        category=category,
                        topic=definition.topic,
                        overwrites=overwrites,
                        reason=f"Área de parceiros criada por {actor}",
                    )
                created_channels.append(definition.identifier)
            else:
                edit_kwargs: dict[str, Any] = {
                    "category": category,
                    "overwrites": overwrites,
                    "reason": f"Canal de parceiros sincronizado por {actor}",
                }
                if definition.kind != "voice":
                    edit_kwargs["topic"] = definition.topic
                await channel.edit(**edit_kwargs)
                reused_channels.append(definition.identifier)
            channels[definition.identifier] = channel

        previous_settings = await self.settings(guild.id)
        settings = PartnerSettings(
            guild_id=guild.id,
            panel_channel_id=channels["hub"].id,
            panel_message_id=previous_settings.panel_message_id if previous_settings else None,
            partner_role_id=role.id,
            log_channel_id=(
                log_channel.id
                if log_channel is not None
                else previous_settings.log_channel_id
                if previous_settings is not None
                else None
            ),
            invite_duration_days=(
                previous_settings.invite_duration_days if previous_settings else 30
            ),
            color=previous_settings.color if previous_settings else PARTNER_COLOR,
        )
        await asyncio.to_thread(self.db.save_settings, settings)
        support_channel = channels.get("support")
        if self.ticket_configurator is not None and isinstance(
            support_channel,
            discord.TextChannel,
        ):
            await self.ticket_configurator(
                guild,
                support_channel,
                manager_role,
                log_channel,
            )
        await self._save_workspace(
            guild.id,
            category_id=category.id,
            partner_role_id=role.id,
            channel_ids={key: value.id for key, value in channels.items()},
        )
        panel_message_ids = await self.publish_workspace_panels(guild, channels, settings)
        hub_message_id = panel_message_ids.get("hub")
        if hub_message_id:
            settings = PartnerSettings(
                guild_id=settings.guild_id,
                panel_channel_id=settings.panel_channel_id,
                panel_message_id=hub_message_id,
                partner_role_id=settings.partner_role_id,
                log_channel_id=settings.log_channel_id,
                invite_duration_days=settings.invite_duration_days,
                color=settings.color,
            )
            await asyncio.to_thread(self.db.save_settings, settings)
        await self.audit_log(
            guild,
            "workspace_provisioned",
            "Categoria, canais e painéis do programa de parceiros foram sincronizados.",
            actor_id=actor.id,
            payload={
                "category_id": category.id,
                "created_channels": created_channels,
                "reused_channels": reused_channels,
                "removed_channels": removed_channels,
            },
        )
        return PartnerWorkspaceResult(
            category_id=category.id,
            partner_role_id=role.id,
            channel_ids={key: value.id for key, value in channels.items()},
            created_category=created_category,
            created_role=created_role,
            created_channels=tuple(created_channels),
            reused_channels=tuple(reused_channels),
            removed_channels=tuple(removed_channels),
            panel_message_ids=panel_message_ids,
        )

    async def _remove_deprecated_workspace_channels(
        self,
        guild: discord.Guild,
        category: discord.CategoryChannel,
        stored_channels: dict[str, Any],
        *,
        actor: discord.Member,
    ) -> list[str]:
        removed: list[str] = []
        for identifier, expected_name in DEPRECATED_PARTNER_WORKSPACE_CHANNELS.items():
            channel = guild.get_channel(_optional_int(stored_channels.get(identifier)) or 0)
            if not isinstance(channel, discord.TextChannel):
                channel = discord.utils.find(
                    lambda item, name=expected_name: (
                        item.name == name and item.category_id == category.id
                    ),
                    guild.text_channels,
                )
            if (
                isinstance(channel, discord.TextChannel)
                and channel.category_id == category.id
                and channel.name == expected_name
            ):
                await channel.delete(
                    reason=f"Canal removido da área de parceiros por {actor}"
                )
                removed.append(identifier)
            stored_channels.pop(identifier, None)
        return removed

    def _workspace_overwrites(
        self,
        guild: discord.Guild,
        partner_role: discord.Role,
        manager_role: discord.Role | None,
        *,
        access: str,
    ) -> dict[discord.abc.Snowflake, discord.PermissionOverwrite]:
        overwrites: dict[discord.abc.Snowflake, discord.PermissionOverwrite] = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
        }
        bot_member = guild.me
        if bot_member is not None:
            overwrites[bot_member] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                send_messages_in_threads=True,
                create_private_threads=True,
                manage_threads=True,
                read_message_history=True,
                manage_messages=True,
                manage_channels=True,
            )
        owner = guild.get_member(self.owner_id)
        if owner is not None:
            overwrites[owner] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                manage_messages=True,
            )
        if manager_role is not None:
            overwrites[manager_role] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                send_messages_in_threads=True,
                manage_threads=True,
                read_message_history=True,
                manage_messages=True,
            )
        if access == "public_readonly":
            overwrites[guild.default_role] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=False,
                read_message_history=True,
            )
            overwrites[partner_role] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=False,
                read_message_history=True,
            )
        elif access == "partner_readonly":
            overwrites[partner_role] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=False,
                send_messages_in_threads=True,
                create_private_threads=False,
                read_message_history=True,
            )
        elif access == "admin":
            overwrites[partner_role] = discord.PermissionOverwrite(view_channel=False)
        elif access == "partner_voice":
            overwrites[partner_role] = discord.PermissionOverwrite(
                view_channel=True,
                connect=True,
                speak=True,
                stream=True,
                use_voice_activation=True,
            )
        else:
            overwrites[partner_role] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                attach_files=True,
                embed_links=True,
                connect=True,
                speak=True,
                stream=True,
            )
        return overwrites

    async def publish_workspace_panels(
        self,
        guild: discord.Guild,
        channels: dict[str, discord.TextChannel | discord.VoiceChannel],
        settings: PartnerSettings,
    ) -> dict[str, int]:
        hub_card = await asyncio.to_thread(render_hub_card)
        payloads = {
            "hub": (
                None,
                PartnerWorkspaceView(
                    self,
                    settings,
                    image=discord.File(hub_card, filename="bella-partner-hub.png"),
                ),
            ),
            "indications": (partner_indications_embed(), PartnerIndicationsView(self)),
            "materials": (partner_materials_embed(), None),
            "support": (partner_support_embed(), PartnerSupportView(self)),
        }
        message_ids: dict[str, int] = {}
        for identifier, (embed, view) in payloads.items():
            channel = channels.get(identifier)
            if not isinstance(channel, discord.TextChannel):
                continue
            message = await self._upsert_workspace_panel(
                guild.id,
                identifier,
                channel,
                embed,
                view,
            )
            message_ids[identifier] = message.id
        return message_ids

    async def _upsert_workspace_panel(
        self,
        guild_id: int,
        identifier: str,
        channel: discord.TextChannel,
        embed: discord.Embed | None,
        view: discord.ui.View | discord.ui.LayoutView | None,
    ) -> discord.Message:
        config = await self.workspace_config(guild_id)
        messages = config.get("messages") if isinstance(config.get("messages"), dict) else {}
        message_id = _optional_int(messages.get(identifier))
        message = None
        if message_id:
            try:
                message = await channel.fetch_message(message_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                message = None
        components_v2 = isinstance(view, discord.ui.LayoutView)
        current_file = getattr(view, "current_file", None)
        if message is None:
            if components_v2:
                kwargs: dict[str, Any] = {
                    "view": view,
                    "allowed_mentions": discord.AllowedMentions.none(),
                }
                if current_file is not None:
                    kwargs["file"] = current_file
                message = await channel.send(**kwargs)
            else:
                message = await channel.send(
                    embed=embed,
                    view=view,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
        else:
            if components_v2:
                kwargs = dict(
                    content=None,
                    embeds=[],
                    view=view,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                if current_file is not None:
                    kwargs["attachments"] = [current_file]
                await message.edit(**kwargs)
            else:
                await message.edit(
                    embed=embed,
                    view=view,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
        if not message.pinned:
            try:
                await message.pin(reason="Painel fixo do Programa de Parceiros")
            except (discord.Forbidden, discord.HTTPException):
                LOGGER.warning("Não foi possível fixar o painel de parceiros em %s.", channel.id)
        await self._save_workspace(guild_id, message_ids={identifier: message.id})
        return message

    async def _save_workspace(
        self,
        guild_id: int,
        *,
        category_id: int | None = None,
        partner_role_id: int | None = None,
        channel_ids: dict[str, int] | None = None,
        message_ids: dict[str, int] | None = None,
    ) -> None:
        def mutate(section: dict[str, Any]) -> None:
            current = section.setdefault(str(guild_id), {})
            if not isinstance(current, dict):
                current = {}
                section[str(guild_id)] = current
            if category_id is not None:
                current["category_id"] = int(category_id)
            if partner_role_id is not None:
                current["partner_role_id"] = int(partner_role_id)
            if channel_ids:
                channels = current.setdefault("channels", {})
                if not isinstance(channels, dict):
                    channels = {}
                    current["channels"] = channels
                channels.update({str(key): int(value) for key, value in channel_ids.items()})
            if message_ids:
                messages = current.setdefault("messages", {})
                if not isinstance(messages, dict):
                    messages = {}
                    current["messages"] = messages
                messages.update({str(key): int(value) for key, value in message_ids.items()})
            current["updated_at"] = _now()

        await self.state.mutate_section(PARTNER_WORKSPACE_SECTION, mutate)

    async def publish_panel(
        self,
        *,
        channel: discord.TextChannel,
        actor: discord.Member,
        partner_role: discord.Role | None,
        log_channel: discord.TextChannel | None,
        invite_duration_days: int,
    ) -> discord.Message:
        existing = await self.settings(channel.guild.id)
        settings = PartnerSettings(
            guild_id=channel.guild.id,
            panel_channel_id=channel.id,
            panel_message_id=existing.panel_message_id if existing else None,
            partner_role_id=partner_role.id if partner_role else None,
            log_channel_id=log_channel.id if log_channel else (
                existing.log_channel_id if existing else None
            ),
            invite_duration_days=max(1, min(365, invite_duration_days)),
            color=existing.color if existing else PARTNER_COLOR,
        )
        await asyncio.to_thread(self.db.save_settings, settings)
        embed = partner_panel_embed(settings)
        message = await channel.send(embed=embed, view=PartnerPanelView(self))
        settings = PartnerSettings(
            guild_id=settings.guild_id,
            panel_channel_id=settings.panel_channel_id,
            panel_message_id=message.id,
            partner_role_id=settings.partner_role_id,
            log_channel_id=settings.log_channel_id,
            invite_duration_days=settings.invite_duration_days,
            color=settings.color,
        )
        await asyncio.to_thread(self.db.save_settings, settings)
        await self.audit_log(
            channel.guild,
            "painel_publicado",
            "Painel do programa de parceiros publicado.",
            actor_id=actor.id,
            payload={"channel_id": channel.id, "message_id": message.id},
        )
        return message

    async def handle_panel_button(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "Use este botão dentro do servidor.",
                ephemeral=True,
            )
            return
        settings = await self.settings(interaction.guild.id)
        if settings is None or settings.panel_channel_id is None:
            await interaction.response.send_message(
                "O programa de parceiros ainda não foi configurado.",
                ephemeral=True,
            )
            return
        if not await self._member_is_partner(interaction.user, settings):
            await interaction.response.send_message(
                "Você precisa possuir o cargo Parceiro para cadastrar seu link.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        profile = await self.partner_profile(interaction.guild, interaction.user)
        existing = profile.get("invite") or {}
        if existing and not existing.get("expires_at"):
            embed = partner_invite_embed(
                interaction.guild,
                interaction.user,
                existing,
                profile,
                title="Seu link personalizado",
            )
            invite_url = str(existing["invite_url"])
        else:
            embed = discord.Embed(
                title="Cadastrar link permanente",
                description=(
                    "Informe um convite permanente deste servidor para a Bella monitorar as "
                    "entradas vinculadas ao seu perfil."
                ),
                color=discord.Color(PARTNER_COLOR),
            )
            embed.add_field(
                name="Requisitos",
                value=(
                    "O convite não pode expirar, possuir limite de usos ou pertencer a outro servidor."
                ),
                inline=False,
            )
            embed.set_footer(text=f"{interaction.guild.name} | Programa de Parceiros")
            invite_url = None
        card = await asyncio.to_thread(
            render_invite_card,
            interaction.user.display_name,
            profile,
        )
        view = PartnerInviteV2View(
            self,
            embed,
            invite_url,
            image=discord.File(card, filename="bella-partner-invite.png"),
        )
        await interaction.followup.send(
            file=view.current_file,
            view=view,
            ephemeral=True,
        )

    async def open_invite_modal(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Use este painel dentro do servidor.", ephemeral=True)
            return
        settings = await self.settings(interaction.guild.id)
        if settings is None or not await self._member_is_partner(interaction.user, settings):
            await interaction.response.send_message(
                "Este recurso é exclusivo para parceiros aprovados.",
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(PartnerInviteModal(self))

    async def show_partner_profile(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Use este painel dentro do servidor.", ephemeral=True)
            return
        settings = await self.settings(interaction.guild.id)
        if settings is None or not await self._member_is_partner(interaction.user, settings):
            await interaction.response.send_message(
                "Este painel é exclusivo para parceiros aprovados.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        profile = await self.partner_profile(interaction.guild, interaction.user)
        card = await asyncio.to_thread(
            render_profile_card,
            interaction.user.display_name,
            profile,
        )
        view = PartnerProfileV2View(
            self,
            interaction.guild,
            interaction.user,
            profile,
            image=discord.File(card, filename="bella-partner-profile.png"),
        )
        await interaction.followup.send(
            file=view.current_file,
            view=view,
            ephemeral=True,
        )

    async def show_partner_history(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Use este painel dentro do servidor.", ephemeral=True)
            return
        settings = await self.settings(interaction.guild.id)
        if settings is None or not await self._member_is_partner(interaction.user, settings):
            await interaction.response.send_message(
                "Este painel é exclusivo para parceiros aprovados.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        rows = await asyncio.to_thread(
            self.db.history,
            interaction.guild.id,
            interaction.user.id,
            limit=10,
            offset=0,
        )
        card = await asyncio.to_thread(
            render_history_card,
            interaction.user.display_name,
            rows,
            0,
        )
        view = PartnerHistoryV2View(
            self,
            interaction.guild,
            interaction.user.id,
            interaction.user.display_name,
            rows,
            page=0,
            image=discord.File(card, filename="bella-partner-history.png"),
        )
        await interaction.followup.send(
            file=view.current_file,
            view=view,
            ephemeral=True,
        )

    async def show_partner_evolution(self, interaction: discord.Interaction) -> None:
        if not await self._validate_partner_panel_access(interaction):
            return
        assert interaction.guild is not None
        assert isinstance(interaction.user, discord.Member)
        await interaction.response.defer(ephemeral=True, thinking=True)
        profile, referrals = await asyncio.gather(
            asyncio.to_thread(self.db.profile, interaction.guild.id, interaction.user.id),
            asyncio.to_thread(
                self.db.referral_statistics,
                interaction.guild.id,
                interaction.user.id,
            ),
        )
        evolution = _partner_evolution_summary(profile, referrals)
        card = await asyncio.to_thread(
            render_evolution_card,
            interaction.user.display_name,
            evolution,
        )
        embed = partner_evolution_embed(interaction.guild, interaction.user, profile, referrals)
        view = PartnerResultV2View(
            embed,
            accent_color=PARTNER_V2_ACCENT_COLOR,
            image=discord.File(card, filename="bella-partner-evolution.png"),
            image_description="Jornada visual do parceiro",
        )
        await interaction.followup.send(
            file=view.current_file,
            view=view,
            ephemeral=True,
        )

    async def show_partner_funnel(self, interaction: discord.Interaction) -> None:
        if not await self._validate_partner_panel_access(interaction):
            return
        assert interaction.guild is not None
        assert isinstance(interaction.user, discord.Member)
        await interaction.response.defer(ephemeral=True, thinking=True)
        profile, referrals = await asyncio.gather(
            asyncio.to_thread(self.db.profile, interaction.guild.id, interaction.user.id),
            asyncio.to_thread(
                self.db.referral_statistics,
                interaction.guild.id,
                interaction.user.id,
            ),
        )
        metrics = _partner_funnel_metrics(profile, referrals)
        diagnosis = _partner_funnel_diagnosis(profile, referrals)
        card = await asyncio.to_thread(
            render_funnel_card,
            interaction.user.display_name,
            metrics,
            diagnosis,
        )
        embed = partner_funnel_embed(interaction.guild, interaction.user, profile, referrals)
        view = PartnerResultV2View(
            embed,
            accent_color=PARTNER_V2_ACCENT_COLOR,
            image=discord.File(card, filename="bella-partner-funnel.png"),
            image_description="Funil inteligente do parceiro",
        )
        await interaction.followup.send(
            file=view.current_file,
            view=view,
            ephemeral=True,
        )

    async def open_commission_simulator(self, interaction: discord.Interaction) -> None:
        if not await self._validate_partner_panel_access(interaction):
            return
        await interaction.response.send_modal(PartnerCommissionSimulatorModal())

    async def open_approach_kit(self, interaction: discord.Interaction) -> None:
        if not await self._validate_partner_panel_access(interaction):
            return
        await interaction.response.send_modal(PartnerApproachKitModal())

    async def _validate_partner_panel_access(
        self,
        interaction: discord.Interaction,
    ) -> bool:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "Use este painel dentro do servidor.",
                ephemeral=True,
            )
            return False
        settings = await self.settings(interaction.guild.id)
        if settings is None or not await self._member_is_partner(interaction.user, settings):
            await interaction.response.send_message(
                "Este recurso é exclusivo para parceiros aprovados.",
                ephemeral=True,
            )
            return False
        return True

    async def open_referral_modal(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Use este painel dentro do servidor.", ephemeral=True)
            return
        settings = await self.settings(interaction.guild.id)
        if settings is None or not await self._member_is_partner(interaction.user, settings):
            await interaction.response.send_message(
                "Somente parceiros aprovados podem registrar indicações.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            "Escolha como a pessoa ou empresa autorizou o contato.",
            view=PartnerReferralContactTypeView(self),
            ephemeral=True,
        )

    async def submit_referral(
        self,
        interaction: discord.Interaction,
        *,
        contact_name: str,
        contact_type: str,
        contact_method: str,
        need: str,
        context: str,
        consent: str,
    ) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Use este formulário dentro do servidor.", ephemeral=True)
            return
        settings = await self.settings(interaction.guild.id)
        if settings is None or not await self._member_is_partner(interaction.user, settings):
            await interaction.response.send_message(
                "Somente parceiros aprovados podem registrar indicações.",
                ephemeral=True,
            )
            return
        if consent.strip().casefold() != "confirmo":
            await interaction.response.send_message(
                "A indicação não foi registrada. Digite `CONFIRMO` para declarar que o contato foi obtido de forma legítima e pode ser utilizado neste atendimento.",
                ephemeral=True,
            )
            return
        try:
            normalized_contact = _normalize_referral_contact(
                contact_type,
                contact_method,
            )
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        referral = await asyncio.to_thread(
            self.db.create_referral,
            guild_id=interaction.guild.id,
            partner_id=interaction.user.id,
            contact_name=contact_name.strip(),
            contact_method=normalized_contact,
            need=need.strip(),
            context=context.strip(),
            contact_type=contact_type,
        )
        report_sent = await self._send_referral_report(
            interaction.guild,
            interaction.user,
            referral,
            source_channel=interaction.channel,
        )
        await self.audit_log(
            interaction.guild,
            "referral_submitted",
            "Uma nova indicação foi registrada por parceiro.",
            partner_id=interaction.user.id,
            actor_id=interaction.user.id,
            payload={
                "referral_id": referral["referral_id"],
                "contact_name": referral["contact_name"],
                "contact_type": referral["contact_type"],
                "need": referral["need"],
                "report_sent": report_sent,
            },
        )
        await interaction.followup.send(
            content=(
                None
                if report_sent
                else "A indicação foi salva, mas não consegui entregar o relatório administrativo. Avise a equipe."
            ),
            embed=partner_referral_created_embed(referral),
            ephemeral=True,
        )

    async def _send_referral_report(
        self,
        guild: discord.Guild,
        partner: discord.Member,
        referral: dict[str, Any],
        *,
        source_channel: discord.abc.GuildChannel | discord.Thread | None,
    ) -> bool:
        channel = guild.get_channel(PARTNER_REFERRAL_REPORT_CHANNEL_ID)
        if not isinstance(channel, discord.TextChannel):
            try:
                fetched = await guild.fetch_channel(PARTNER_REFERRAL_REPORT_CHANNEL_ID)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                LOGGER.exception(
                    "Canal administrativo de indicações indisponível: %s",
                    PARTNER_REFERRAL_REPORT_CHANNEL_ID,
                )
                return False
            channel = fetched if isinstance(fetched, discord.TextChannel) else None
        if channel is None:
            LOGGER.error(
                "O canal %s não é um canal de texto para relatórios de indicações.",
                PARTNER_REFERRAL_REPORT_CHANNEL_ID,
            )
            return False
        try:
            message = await channel.send(
                content=f"<@{self.owner_id}> nova indicação registrada.",
                embed=partner_referral_admin_embed(
                    guild,
                    partner,
                    referral,
                    source_channel=source_channel,
                ),
                view=PartnerReferralAdminView(self),
                allowed_mentions=discord.AllowedMentions(
                    users=[discord.Object(id=self.owner_id)],
                    roles=False,
                    everyone=False,
                ),
            )
        except (discord.Forbidden, discord.HTTPException):
            LOGGER.exception(
                "Falha ao enviar indicação %s para o canal administrativo %s.",
                referral.get("referral_id"),
                PARTNER_REFERRAL_REPORT_CHANNEL_ID,
            )
            return False
        await asyncio.to_thread(
            self.db.set_referral_report_location,
            str(referral["referral_id"]),
            channel_id=channel.id,
            message_id=message.id,
        )
        return True

    async def referral_from_admin_interaction(
        self,
        interaction: discord.Interaction,
    ) -> dict[str, Any] | None:
        if interaction.guild is None or interaction.message is None:
            return None
        return await asyncio.to_thread(
            self.db.referral_by_report_message,
            interaction.guild.id,
            interaction.message.id,
        )

    async def update_referral_status(
        self,
        interaction: discord.Interaction,
        *,
        status: str,
        note: str = "",
        referral_id: str | None = None,
    ) -> None:
        if interaction.guild is None or not self.is_admin(interaction.user):
            await interaction.response.send_message(
                "Somente a administração pode alterar o status desta indicação.",
                ephemeral=True,
            )
            return
        if status not in PARTNER_REFERRAL_STATUS_LABELS:
            await interaction.response.send_message("Status inválido.", ephemeral=True)
            return
        referral = (
            await asyncio.to_thread(self.db.referral, referral_id)
            if referral_id
            else await self.referral_from_admin_interaction(interaction)
        )
        if referral is None:
            await interaction.response.send_message(
                "Não encontrei o registro ligado a este relatório.",
                ephemeral=True,
            )
            return
        updated = await asyncio.to_thread(
            self.db.update_referral_status,
            str(referral["referral_id"]),
            status=status,
            note=note.strip(),
            reviewed_by=interaction.user.id,
        )
        if updated is None:
            await interaction.response.send_message(
                "A indicação não existe mais no banco.",
                ephemeral=True,
            )
            return
        report_message = interaction.message
        if report_message is None:
            report_channel = interaction.guild.get_channel(
                int(updated.get("report_channel_id") or 0)
            )
            if isinstance(report_channel, discord.TextChannel):
                try:
                    report_message = await report_channel.fetch_message(
                        int(updated.get("report_message_id") or 0)
                    )
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    report_message = None
        if report_message is not None:
            embed = _updated_referral_admin_embed(report_message, updated)
            if interaction.message is not None:
                await interaction.response.edit_message(
                    embed=embed,
                    view=PartnerReferralAdminView(self),
                )
            else:
                await interaction.response.defer(ephemeral=True, thinking=True)
                await report_message.edit(
                    embed=embed,
                    view=PartnerReferralAdminView(self),
                )
        else:
            await interaction.response.defer(ephemeral=True, thinking=True)
        partner_id = int(updated.get("partner_id") or 0)
        partner = interaction.guild.get_member(partner_id)
        if partner is None:
            try:
                partner = await self.bot.fetch_user(partner_id)
            except (discord.NotFound, discord.HTTPException):
                partner = None
        notified = False
        if partner is not None:
            try:
                await partner.send(embed=partner_referral_status_embed(updated))
                notified = True
            except (discord.Forbidden, discord.HTTPException):
                notified = False
        await self.audit_log(
            interaction.guild,
            "referral_status_updated",
            "Status de indicação atualizado pela administração.",
            partner_id=partner_id,
            actor_id=interaction.user.id,
            payload={
                "referral_id": updated["referral_id"],
                "status": status,
                "note": note.strip(),
                "partner_notified": notified,
            },
        )
        notification = (
            "O parceiro recebeu a atualização por DM."
            if notified
            else "A DM não pôde ser entregue; o status já aparece em **Minhas indicações**."
        )
        await interaction.followup.send(
            f"Status atualizado para **{PARTNER_REFERRAL_STATUS_LABELS[status]}**. {notification}",
            ephemeral=True,
        )

    async def open_referral_rejection_modal(
        self,
        interaction: discord.Interaction,
    ) -> None:
        if interaction.guild is None or not self.is_admin(interaction.user):
            await interaction.response.send_message(
                "Somente a administração pode recusar indicações.",
                ephemeral=True,
            )
            return
        referral = await self.referral_from_admin_interaction(interaction)
        if referral is None:
            await interaction.response.send_message(
                "Não encontrei o registro ligado a este relatório.",
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(
            PartnerReferralRejectModal(self, str(referral["referral_id"]))
        )

    async def show_partner_referrals(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Use este painel dentro do servidor.", ephemeral=True)
            return
        settings = await self.settings(interaction.guild.id)
        if settings is None or not await self._member_is_partner(interaction.user, settings):
            await interaction.response.send_message(
                "Este painel é exclusivo para parceiros aprovados.",
                ephemeral=True,
            )
            return
        referrals = await asyncio.to_thread(
            self.db.referrals_for_partner,
            interaction.guild.id,
            interaction.user.id,
            limit=20,
        )
        await interaction.response.send_message(
            embed=partner_referrals_embed(interaction.guild, interaction.user, referrals),
            ephemeral=True,
        )

    async def open_partner_support_ticket(
        self,
        interaction: discord.Interaction,
    ) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "Use este painel dentro do servidor.",
                ephemeral=True,
            )
            return
        settings = await self.settings(interaction.guild.id)
        if settings is None or not await self._member_is_partner(interaction.user, settings):
            await interaction.response.send_message(
                "Este atendimento é exclusivo para parceiros aprovados.",
                ephemeral=True,
            )
            return
        if self.ticket_opener is None:
            await interaction.response.send_message(
                "O atendimento privado ainda não foi conectado ao sistema de tickets.",
                ephemeral=True,
            )
            return
        await self.ticket_opener(interaction)

    async def register_partner_invite(
        self,
        interaction: discord.Interaction,
        invite_url: str,
    ) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Use este formulário dentro do servidor.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        settings = await self.settings(interaction.guild.id)
        if settings is None or not await self._member_is_partner(interaction.user, settings):
            await interaction.followup.send(
                "Este recurso é exclusivo para parceiros aprovados.",
                ephemeral=True,
            )
            return
        invite_code = _invite_code_from_url(invite_url)
        if not invite_code:
            await interaction.followup.send(
                "Esse endereço não parece um convite válido do Discord. Envie algo como `https://discord.gg/codigo`.",
                ephemeral=True,
            )
            return
        try:
            guild_invites = await interaction.guild.invites()
        except discord.Forbidden:
            await interaction.followup.send(
                "Não consegui conferir o convite. A Bella precisa da permissão **Gerenciar Servidor** para ler os convites.",
                ephemeral=True,
            )
            return
        except discord.HTTPException:
            await interaction.followup.send(
                "O Discord não deixou eu conferir esse convite agora. Tenta de novo em alguns instantes.",
                ephemeral=True,
            )
            return
        invite = discord.utils.find(
            lambda item: item.code.casefold() == invite_code.casefold(),
            guild_invites,
        )
        if invite is None:
            await interaction.followup.send(
                "Não encontrei esse convite entre os links deste servidor. Confirme se ele pertence a este servidor e ainda está ativo.",
                ephemeral=True,
            )
            return
        if not _invite_is_permanent(invite):
            await interaction.followup.send(
                "Esse convite não é permanente. Crie no Discord um link **sem expiração e sem limite de usos** e envie ele aqui.",
                ephemeral=True,
            )
            return
        assigned = await asyncio.to_thread(
            self.db.invite_by_code,
            interaction.guild.id,
            invite.code,
        )
        if assigned and int(assigned.get("partner_id") or 0) != interaction.user.id:
            await interaction.followup.send(
                "Esse link já está vinculado a outro parceiro. Cada parceiro precisa usar um convite diferente.",
                ephemeral=True,
            )
            return
        await asyncio.to_thread(
            self.db.upsert_partner,
            interaction.guild.id,
            interaction.user.id,
            role_id=settings.partner_role_id,
        )
        await asyncio.to_thread(
            self.db.deactivate_partner_invites,
            interaction.guild.id,
            interaction.user.id,
            except_code=invite.code,
        )
        source_channel = getattr(invite, "channel", None)
        saved = await asyncio.to_thread(
            self.db.save_invite,
            guild_id=interaction.guild.id,
            partner_id=interaction.user.id,
            invite_code=invite.code,
            invite_url=str(invite.url),
            visual_role_id=None,
            source_channel_id=_optional_int(getattr(source_channel, "id", None)),
            expires_at=None,
        )
        self.invite_cache[interaction.guild.id] = {
            item.code: int(item.uses or 0)
            for item in guild_invites
        }
        await self.audit_log(
            interaction.guild,
            "convite_cadastrado",
            "Link permanente de parceiro validado e cadastrado para monitoramento.",
            partner_id=interaction.user.id,
            actor_id=interaction.user.id,
            payload={
                "invite_code": invite.code,
                "channel_id": _optional_int(getattr(source_channel, "id", None)),
                "uses_at_registration": int(invite.uses or 0),
            },
        )
        profile = await self.partner_profile(interaction.guild, interaction.user)
        await interaction.followup.send(
            embed=partner_invite_embed(
                interaction.guild,
                interaction.user,
                saved,
                profile,
                title="Link permanente cadastrado",
            ),
            view=PartnerInviteInfoView(self, str(saved["invite_url"])),
            ephemeral=True,
        )

    async def handle_member_join(self, member: discord.Member) -> None:
        invite = await self._detect_used_invite(member.guild)
        if invite is None:
            return
        partner_invite = await asyncio.to_thread(
            self.db.invite_by_code,
            member.guild.id,
            invite.code,
        )
        if not partner_invite:
            return
        record = await asyncio.to_thread(
            self.db.register_member_join,
            guild_id=member.guild.id,
            member_id=member.id,
            partner_id=int(partner_invite["partner_id"]),
            invite_code=invite.code,
        )
        await self._assign_visual_role(member, partner_invite)
        await self.audit_log(
            member.guild,
            "entrada",
            "Novo membro entrou por convite de parceiro.",
            partner_id=int(partner_invite["partner_id"]),
            actor_id=member.id,
            payload={"member_id": member.id, "invite_code": invite.code},
        )
        await self._notify_partner_join(member, partner_invite, record)

    async def handle_member_remove(self, member: discord.Member) -> None:
        record = await asyncio.to_thread(
            self.db.mark_member_left,
            member.guild.id,
            member.id,
        )
        if not record:
            return
        await self.audit_log(
            member.guild,
            "saida",
            "Membro indicado saiu do servidor.",
            partner_id=int(record["partner_id"]),
            actor_id=member.id,
            payload={"member_id": member.id},
        )

    async def refresh_invite_cache(self, guild: discord.Guild) -> None:
        try:
            invites = await guild.invites()
        except (discord.Forbidden, discord.HTTPException):
            LOGGER.warning("Não foi possível atualizar convites do servidor %s", guild.id)
            return
        self.invite_cache[guild.id] = {
            invite.code: int(invite.uses or 0)
            for invite in invites
        }

    async def refresh_all_invite_caches(self) -> None:
        for guild in self.bot.guilds:
            await self.refresh_invite_cache(guild)

    async def apply_ticket_context(
        self,
        *,
        guild: discord.Guild,
        member: discord.Member,
        thread: discord.Thread,
        category: str,
    ) -> None:
        referral = await asyncio.to_thread(
            self.db.member_referral,
            guild.id,
            member.id,
        )
        if not referral:
            return
        partner_id = int(referral["partner_id"])
        values = await self.state.get_section(PARTNER_RECORD_SECTION)
        ticket_record = values.get(str(thread.id), {})
        if isinstance(ticket_record, dict):
            ticket_record["partner_referral"] = {
                "partner_id": partner_id,
                "member_id": member.id,
                "invite_code": referral.get("invite_code"),
                "joined_at": referral.get("joined_at"),
                "category": category,
            }
            await self.state.update_section(
                PARTNER_RECORD_SECTION,
                str(thread.id),
                ticket_record,
            )
        embed = discord.Embed(
            title="Origem do atendimento",
            description="Este usuário entrou no servidor por indicação de parceiro.",
            color=discord.Color(PARTNER_COLOR),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Parceiro responsável", value=f"<@{partner_id}>")
        embed.add_field(name="Convite utilizado", value=f"`{referral.get('invite_code')}`")
        embed.add_field(
            name="Data da indicação",
            value=_format_dt(referral.get("joined_at")),
            inline=False,
        )
        await thread.send(embed=embed)

    async def partner_profile(
        self,
        guild: discord.Guild,
        member: discord.Member | discord.User,
    ) -> dict[str, Any]:
        await asyncio.to_thread(self.db.upsert_partner, guild.id, member.id)
        return await asyncio.to_thread(self.db.profile, guild.id, member.id)

    async def invite_embed_for(
        self,
        guild: discord.Guild,
        member: discord.Member | discord.User,
    ) -> tuple[discord.Embed, discord.ui.View | None]:
        profile = await self.partner_profile(guild, member)
        invite = profile.get("invite")
        if not invite:
            embed = discord.Embed(
                title="Link ainda não cadastrado",
                description=(
                    "Esse parceiro ainda não informou um convite permanente deste servidor. "
                    "Use **Cadastrar/ver link** para validar e iniciar o monitoramento."
                ),
                color=discord.Color(PARTNER_COLOR),
            )
            return embed, None
        return (
            partner_invite_embed(guild, member, invite, profile),
            PartnerInviteInfoView(self, str(invite["invite_url"])),
        )

    async def audit_log(
        self,
        guild: discord.Guild,
        event_type: str,
        message: str,
        *,
        partner_id: int | None = None,
        actor_id: int | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        row = await asyncio.to_thread(
            self.db.log,
            guild_id=guild.id,
            event_type=event_type,
            message=message,
            partner_id=partner_id,
            actor_id=actor_id,
            payload=payload,
        )
        settings = await self.settings(guild.id)
        channel = guild.get_channel(settings.log_channel_id or 0) if settings else None
        if not isinstance(channel, discord.TextChannel):
            return
        try:
            await channel.send(embed=partner_log_embed(row))
        except (discord.Forbidden, discord.HTTPException):
            LOGGER.exception("Falha ao enviar log do programa de parceiros")

    async def _member_is_partner(
        self,
        member: discord.Member,
        settings: PartnerSettings,
    ) -> bool:
        existing = await asyncio.to_thread(self.db.partner, member.guild.id, member.id)
        if existing and existing.get("status") == "active":
            return True
        if settings.partner_role_id and any(
            role.id == settings.partner_role_id for role in member.roles
        ):
            return True
        return any(role.name.casefold() == "parceiro" for role in member.roles)

    async def _detect_used_invite(self, guild: discord.Guild) -> discord.Invite | None:
        try:
            invites = await guild.invites()
        except (discord.Forbidden, discord.HTTPException):
            LOGGER.warning("Não foi possível ler convites do servidor %s", guild.id)
            return None
        previous = self.invite_cache.get(guild.id, {})
        current = {invite.code: int(invite.uses or 0) for invite in invites}
        self.invite_cache[guild.id] = current
        candidates = [
            invite
            for invite in invites
            if int(invite.uses or 0) > int(previous.get(invite.code, 0))
        ]
        if candidates:
            return max(
                candidates,
                key=lambda item: int(item.uses or 0) - int(previous.get(item.code, 0)),
            )
        return None

    async def _assign_visual_role(
        self,
        member: discord.Member,
        partner_invite: dict[str, Any],
    ) -> None:
        role_id = _optional_int(partner_invite.get("visual_role_id"))
        role = member.guild.get_role(role_id or 0)
        if role is None:
            return
        bot_member = member.guild.me
        if bot_member is None or role >= bot_member.top_role:
            await self.audit_log(
                member.guild,
                "erro",
                "Não consegui atribuir cargo visual do parceiro por hierarquia.",
                partner_id=int(partner_invite["partner_id"]),
                actor_id=member.id,
                payload={"role_id": role.id},
            )
            return
        try:
            await member.add_roles(
                role,
                reason="Identificação automática do programa de parceiros",
            )
        except (discord.Forbidden, discord.HTTPException):
            await self.audit_log(
                member.guild,
                "erro",
                "Não consegui atribuir cargo visual do parceiro ao membro indicado.",
                partner_id=int(partner_invite["partner_id"]),
                actor_id=member.id,
                payload={"role_id": role.id},
            )

    async def _notify_partner_join(
        self,
        member: discord.Member,
        partner_invite: dict[str, Any],
        record: dict[str, Any],
    ) -> None:
        partner_id = int(partner_invite["partner_id"])
        partner = member.guild.get_member(partner_id)
        if partner is None:
            try:
                partner = await member.guild.fetch_member(partner_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                partner = None
        if partner is None:
            return
        profile = await asyncio.to_thread(self.db.profile, member.guild.id, partner_id)
        total = int(profile["members"].get("invited_total", 0))
        try:
            await partner.send(
                (
                    "Uma nova pessoa entrou utilizando o seu convite.\n\n"
                    f"Usuário: {member.mention}\n"
                    f"Total de pessoas convidadas: {total}"
                ),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except (discord.Forbidden, discord.HTTPException):
            await self.audit_log(
                member.guild,
                "erro",
                "Não consegui enviar DM ao parceiro sobre nova entrada.",
                partner_id=partner_id,
                actor_id=member.id,
                payload={"member_id": member.id, "record": record},
            )


class PartnerWorkspaceView(discord.ui.LayoutView):
    def __init__(
        self,
        manager: PartnerProgramManager,
        settings: PartnerSettings | None = None,
        *,
        image: discord.File | None = None,
    ) -> None:
        super().__init__(timeout=None)
        self.manager = manager
        self.current_file = image
        panel_settings = settings or PartnerSettings(guild_id=0)
        container = discord.ui.Container(
            accent_color=discord.Color(PARTNER_V2_ACCENT_COLOR),
        )
        if image is not None:
            container.add_item(
                discord.ui.MediaGallery(
                    discord.MediaGalleryItem(
                        image,
                        description="Programa de Parceiros da Bella",
                    )
                )
            )
            container.add_item(
                discord.ui.Separator(spacing=discord.SeparatorSpacing.small)
            )
        _add_embed_to_v2_container(container, partner_panel_embed(panel_settings))
        container.add_item(
            discord.ui.Separator(spacing=discord.SeparatorSpacing.small)
        )

        first_row = discord.ui.ActionRow()
        first_row.add_item(
            _v2_button(
                "Meu painel",
                discord.ButtonStyle.primary,
                "bella:partners:workspace:profile:v1",
                self._profile,
            )
        )
        first_row.add_item(
            _v2_button(
                "Cadastrar/ver link",
                discord.ButtonStyle.success,
                "bella:partners:workspace:invite:v1",
                self._invite,
            )
        )
        first_row.add_item(
            _v2_button(
                "Histórico",
                discord.ButtonStyle.secondary,
                "bella:partners:workspace:history:v1",
                self._history,
            )
        )
        container.add_item(first_row)

        intelligence_row = discord.ui.ActionRow()
        intelligence_row.add_item(
            _v2_button(
                "Minha evolução",
                discord.ButtonStyle.primary,
                "bella:partners:workspace:evolution:v1",
                self._evolution,
            )
        )
        intelligence_row.add_item(
            _v2_button(
                "Funil inteligente",
                discord.ButtonStyle.primary,
                "bella:partners:workspace:funnel:v1",
                self._funnel,
            )
        )
        intelligence_row.add_item(
            _v2_button(
                "Simular ganhos",
                discord.ButtonStyle.success,
                "bella:partners:workspace:commission-simulator:v1",
                self._commission_simulator,
            )
        )
        container.add_item(intelligence_row)

        tools_row = discord.ui.ActionRow()
        tools_row.add_item(
            _v2_button(
                "Kit personalizado",
                discord.ButtonStyle.secondary,
                "bella:partners:workspace:approach-kit:v1",
                self._approach_kit,
            )
        )
        tools_row.add_item(
            _v2_button(
                "Regras",
                discord.ButtonStyle.secondary,
                "bella:partners:workspace:rules:v1",
                self._rules,
            )
        )
        tools_row.add_item(
            discord.ui.Button(
                label="Página oficial",
                style=discord.ButtonStyle.link,
                url=PARTNER_PROGRAM_URL,
            )
        )
        container.add_item(tools_row)
        self.add_item(container)

    async def _profile(self, interaction: discord.Interaction) -> None:
        await self.manager.show_partner_profile(interaction)

    async def _invite(self, interaction: discord.Interaction) -> None:
        await self.manager.handle_panel_button(interaction)

    async def _history(self, interaction: discord.Interaction) -> None:
        await self.manager.show_partner_history(interaction)

    async def _evolution(self, interaction: discord.Interaction) -> None:
        await self.manager.show_partner_evolution(interaction)

    async def _funnel(self, interaction: discord.Interaction) -> None:
        await self.manager.show_partner_funnel(interaction)

    async def _commission_simulator(self, interaction: discord.Interaction) -> None:
        await self.manager.open_commission_simulator(interaction)

    async def _approach_kit(self, interaction: discord.Interaction) -> None:
        await self.manager.open_approach_kit(interaction)

    async def _rules(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        card = await asyncio.to_thread(render_rules_card)
        view = PartnerResultV2View(
            partner_rules_embed(),
            accent_color=PARTNER_V2_ACCENT_COLOR,
            image=discord.File(card, filename="bella-partner-rules.png"),
            image_description="Regras do Programa de Parceiros",
        )
        await interaction.followup.send(
            file=view.current_file,
            view=view,
            ephemeral=True,
        )


class PartnerResultV2View(discord.ui.LayoutView):
    def __init__(
        self,
        embed: discord.Embed,
        *,
        accent_color: int,
        image: discord.File | None = None,
        image_description: str = "",
    ) -> None:
        super().__init__(timeout=180)
        self.current_file = image
        container = discord.ui.Container(
            accent_color=discord.Color(accent_color),
        )
        if image is not None:
            container.add_item(
                discord.ui.MediaGallery(
                    discord.MediaGalleryItem(
                        image,
                        description=image_description[:1024] or None,
                    )
                )
            )
            container.add_item(
                discord.ui.Separator(spacing=discord.SeparatorSpacing.small)
            )
        _add_embed_to_v2_container(container, embed)
        self.add_item(container)


class PartnerProfileV2View(discord.ui.LayoutView):
    def __init__(
        self,
        manager: PartnerProgramManager,
        guild: discord.Guild,
        partner: discord.Member | discord.User,
        profile: dict[str, Any],
        *,
        image: discord.File,
    ) -> None:
        super().__init__(timeout=180)
        self.manager = manager
        self.guild = guild
        self.partner = partner
        self.profile = profile
        self.current_file = image
        self._render()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.partner.id:
            return True
        await interaction.response.send_message(
            "Somente o titular pode usar este painel privado.",
            ephemeral=True,
        )
        return False

    def _render(self) -> None:
        self.clear_items()
        container = discord.ui.Container(
            accent_color=discord.Color(PARTNER_V2_ACCENT_COLOR),
        )
        container.add_item(
            discord.ui.MediaGallery(
                discord.MediaGalleryItem(
                    self.current_file,
                    description="Perfil privado do parceiro",
                )
            )
        )
        container.add_item(
            discord.ui.Separator(spacing=discord.SeparatorSpacing.small)
        )
        _add_embed_to_v2_container(
            container,
            partner_profile_embed(self.guild, self.partner, self.profile),
        )
        container.add_item(
            discord.ui.Separator(spacing=discord.SeparatorSpacing.small)
        )
        row = discord.ui.ActionRow()
        row.add_item(
            _v2_button(
                "Atualizar",
                discord.ButtonStyle.primary,
                "bella:partners:profile:refresh:v2",
                self._refresh,
            )
        )
        row.add_item(
            _v2_button(
                "Cadastrar/ver link",
                discord.ButtonStyle.success,
                "bella:partners:profile:invite:v2",
                self._invite,
            )
        )
        row.add_item(
            _v2_button(
                "Histórico",
                discord.ButtonStyle.secondary,
                "bella:partners:profile:history:v2",
                self._history,
            )
        )
        container.add_item(row)
        self.add_item(container)

    async def _refresh(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        self.profile = await self.manager.partner_profile(self.guild, self.partner)
        card = await asyncio.to_thread(
            render_profile_card,
            getattr(self.partner, "display_name", self.partner.name),
            self.profile,
        )
        self.current_file = discord.File(card, filename="bella-partner-profile.png")
        self._render()
        await interaction.edit_original_response(
            attachments=[self.current_file],
            view=self,
        )

    async def _invite(self, interaction: discord.Interaction) -> None:
        await self.manager.handle_panel_button(interaction)

    async def _history(self, interaction: discord.Interaction) -> None:
        await self.manager.show_partner_history(interaction)


class PartnerInviteV2View(discord.ui.LayoutView):
    def __init__(
        self,
        manager: PartnerProgramManager,
        embed: discord.Embed,
        invite_url: str | None,
        *,
        image: discord.File,
    ) -> None:
        super().__init__(timeout=180)
        self.manager = manager
        self.current_file = image
        container = discord.ui.Container(
            accent_color=discord.Color(PARTNER_V2_ACCENT_COLOR),
        )
        container.add_item(
            discord.ui.MediaGallery(
                discord.MediaGalleryItem(
                    image,
                    description="Monitoramento do link permanente",
                )
            )
        )
        container.add_item(
            discord.ui.Separator(spacing=discord.SeparatorSpacing.small)
        )
        _add_embed_to_v2_container(container, embed)
        container.add_item(
            discord.ui.Separator(spacing=discord.SeparatorSpacing.small)
        )
        row = discord.ui.ActionRow()
        if invite_url:
            row.add_item(
                discord.ui.Button(
                    label="Abrir link",
                    style=discord.ButtonStyle.link,
                    url=invite_url,
                )
            )
        row.add_item(
            _v2_button(
                "Trocar link" if invite_url else "Cadastrar link permanente",
                discord.ButtonStyle.success,
                "bella:partners:invite:replace:v2",
                self._replace,
            )
        )
        container.add_item(row)
        self.add_item(container)

    async def _replace(self, interaction: discord.Interaction) -> None:
        await self.manager.open_invite_modal(interaction)


class PartnerHistoryV2View(discord.ui.LayoutView):
    def __init__(
        self,
        manager: PartnerProgramManager,
        guild: discord.Guild,
        partner_id: int,
        partner_name: str,
        rows: list[dict[str, Any]],
        *,
        page: int,
        image: discord.File,
    ) -> None:
        super().__init__(timeout=180)
        self.manager = manager
        self.guild = guild
        self.partner_id = partner_id
        self.partner_name = partner_name
        self.rows = rows
        self.page = page
        self.current_file = image
        self._render()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.partner_id:
            return True
        await interaction.response.send_message(
            "Somente o titular pode consultar este histórico.",
            ephemeral=True,
        )
        return False

    def _render(self) -> None:
        self.clear_items()
        container = discord.ui.Container(
            accent_color=discord.Color(PARTNER_V2_ACCENT_COLOR),
        )
        container.add_item(
            discord.ui.MediaGallery(
                discord.MediaGalleryItem(
                    self.current_file,
                    description="Histórico rastreado do parceiro",
                )
            )
        )
        container.add_item(
            discord.ui.Separator(spacing=discord.SeparatorSpacing.small)
        )
        _add_embed_to_v2_container(
            container,
            partner_history_embed(
                self.guild,
                self.partner_id,
                self.rows,
                page=self.page,
            ),
        )
        container.add_item(
            discord.ui.Separator(spacing=discord.SeparatorSpacing.small)
        )
        row = discord.ui.ActionRow()
        previous = _v2_button(
            "Anterior",
            discord.ButtonStyle.secondary,
            "bella:partners:history:previous:v2",
            self._previous,
        )
        previous.disabled = self.page <= 0
        row.add_item(previous)
        row.add_item(
            _v2_button(
                "Próxima",
                discord.ButtonStyle.secondary,
                "bella:partners:history:next:v2",
                self._next,
            )
        )
        container.add_item(row)
        self.add_item(container)

    async def _previous(self, interaction: discord.Interaction) -> None:
        self.page = max(0, self.page - 1)
        await self._update(interaction)

    async def _next(self, interaction: discord.Interaction) -> None:
        self.page += 1
        await self._update(interaction)

    async def _update(self, interaction: discord.Interaction) -> None:
        rows = await asyncio.to_thread(
            self.manager.db.history,
            self.guild.id,
            self.partner_id,
            limit=10,
            offset=self.page * 10,
        )
        if not rows and self.page > 0:
            self.page -= 1
            rows = await asyncio.to_thread(
                self.manager.db.history,
                self.guild.id,
                self.partner_id,
                limit=10,
                offset=self.page * 10,
            )
        self.rows = rows
        card = await asyncio.to_thread(
            render_history_card,
            self.partner_name,
            self.rows,
            self.page,
        )
        self.current_file = discord.File(card, filename="bella-partner-history.png")
        self._render()
        await interaction.response.edit_message(
            attachments=[self.current_file],
            view=self,
        )


class PartnerIndicationsView(discord.ui.View):
    def __init__(self, manager: PartnerProgramManager) -> None:
        super().__init__(timeout=None)
        self.manager = manager

    @discord.ui.button(
        label="Registrar indicação",
        style=discord.ButtonStyle.success,
        custom_id="bella:partners:workspace:referral-create:v1",
        row=0,
    )
    async def create_referral(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button[Any],
    ) -> None:
        await self.manager.open_referral_modal(interaction)

    @discord.ui.button(
        label="Minhas indicações",
        style=discord.ButtonStyle.secondary,
        custom_id="bella:partners:workspace:referrals:v1",
        row=0,
    )
    async def referrals(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button[Any],
    ) -> None:
        await self.manager.show_partner_referrals(interaction)


class PartnerSupportView(discord.ui.View):
    def __init__(self, manager: PartnerProgramManager) -> None:
        super().__init__(timeout=None)
        self.manager = manager

    @discord.ui.button(
        label="Abrir ticket privado",
        style=discord.ButtonStyle.primary,
        custom_id="bella:partners:workspace:support-ticket:v1",
    )
    async def open_ticket(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button[Any],
    ) -> None:
        await self.manager.open_partner_support_ticket(interaction)


class PartnerReferralAdminView(discord.ui.View):
    def __init__(self, manager: PartnerProgramManager) -> None:
        super().__init__(timeout=None)
        self.manager = manager

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.manager.is_admin(interaction.user):
            return True
        await interaction.response.send_message(
            "Somente a administração pode gerenciar esta indicação.",
            ephemeral=True,
        )
        return False

    @discord.ui.button(
        label="Em análise",
        style=discord.ButtonStyle.primary,
        custom_id="bella:partners:referral:reviewing:v1",
        row=0,
    )
    async def reviewing(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button[Any],
    ) -> None:
        await self.manager.update_referral_status(interaction, status="reviewing")

    @discord.ui.button(
        label="Aceitar",
        style=discord.ButtonStyle.success,
        custom_id="bella:partners:referral:accepted:v1",
        row=0,
    )
    async def accepted(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button[Any],
    ) -> None:
        await self.manager.update_referral_status(interaction, status="accepted")

    @discord.ui.button(
        label="Negociação",
        style=discord.ButtonStyle.primary,
        custom_id="bella:partners:referral:negotiation:v1",
        row=0,
    )
    async def negotiation(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button[Any],
    ) -> None:
        await self.manager.update_referral_status(interaction, status="negotiation")

    @discord.ui.button(
        label="Projeto fechado",
        style=discord.ButtonStyle.success,
        custom_id="bella:partners:referral:closed:v1",
        row=1,
    )
    async def closed(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button[Any],
    ) -> None:
        await self.manager.update_referral_status(interaction, status="closed")

    @discord.ui.button(
        label="Finalizar",
        style=discord.ButtonStyle.success,
        custom_id="bella:partners:referral:completed:v1",
        row=1,
    )
    async def completed(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button[Any],
    ) -> None:
        await self.manager.update_referral_status(interaction, status="completed")

    @discord.ui.button(
        label="Recusar",
        style=discord.ButtonStyle.danger,
        custom_id="bella:partners:referral:rejected:v1",
        row=1,
    )
    async def rejected(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button[Any],
    ) -> None:
        await self.manager.open_referral_rejection_modal(interaction)


class PartnerReferralRejectModal(discord.ui.Modal, title="Recusar indicação"):
    reason = discord.ui.TextInput(
        label="Motivo para o parceiro",
        style=discord.TextStyle.paragraph,
        placeholder="Explique de forma objetiva e respeitosa.",
        max_length=1000,
    )

    def __init__(self, manager: PartnerProgramManager, referral_id: str) -> None:
        super().__init__()
        self.manager = manager
        self.referral_id = referral_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.manager.update_referral_status(
            interaction,
            status="rejected",
            note=str(self.reason.value),
            referral_id=self.referral_id,
        )


class PartnerReferralContactTypeView(discord.ui.View):
    def __init__(self, manager: PartnerProgramManager) -> None:
        super().__init__(timeout=180)
        self.add_item(PartnerReferralContactTypeSelect(manager))


class PartnerReferralContactTypeSelect(discord.ui.Select):
    def __init__(self, manager: PartnerProgramManager) -> None:
        self.manager = manager
        super().__init__(
            placeholder="Selecione o tipo de contato",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(label=label, value=value)
                for value, label in PARTNER_REFERRAL_CONTACT_TYPES.items()
            ],
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(
            PartnerReferralModal(self.manager, self.values[0])
        )


class PartnerReferralModal(discord.ui.Modal):
    def __init__(self, manager: PartnerProgramManager, contact_type: str) -> None:
        label = PARTNER_REFERRAL_CONTACT_TYPES.get(contact_type, "Outro contato")
        super().__init__(title=f"Registrar indicação • {label}")
        self.manager = manager
        self.contact_type = contact_type
        placeholders = {
            "whatsapp": "+55 11 99999-9999",
            "email": "contato@empresa.com.br",
            "phone": "+55 11 3333-3333",
            "discord": "ID, @usuário ou perfil do Discord",
            "other": "Informe o canal e o contato autorizado",
        }
        self.contact_name = discord.ui.TextInput(
            label="Nome da pessoa ou empresa",
            max_length=120,
        )
        self.contact_method = discord.ui.TextInput(
            label=f"Contato via {label}"[:45],
            placeholder=placeholders.get(contact_type, placeholders["other"]),
            max_length=200,
        )
        self.need = discord.ui.TextInput(
            label="O que ela precisa?",
            style=discord.TextStyle.paragraph,
            max_length=500,
        )
        self.context = discord.ui.TextInput(
            label="Contexto da oportunidade",
            style=discord.TextStyle.paragraph,
            max_length=1000,
            required=False,
        )
        self.consent = discord.ui.TextInput(
            label="Confirmação de origem legítima",
            placeholder="Digite CONFIRMO",
            max_length=20,
        )
        for item in (
            self.contact_name,
            self.contact_method,
            self.need,
            self.context,
            self.consent,
        ):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.manager.submit_referral(
            interaction,
            contact_name=str(self.contact_name.value),
            contact_type=self.contact_type,
            contact_method=str(self.contact_method.value),
            need=str(self.need.value),
            context=str(self.context.value),
            consent=str(self.consent.value),
        )


class PartnerInviteModal(discord.ui.Modal, title="Cadastrar link permanente"):
    invite_url = discord.ui.TextInput(
        label="Link permanente deste servidor",
        placeholder="https://discord.gg/seu-codigo",
        max_length=200,
    )

    def __init__(self, manager: PartnerProgramManager) -> None:
        super().__init__()
        self.manager = manager

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.manager.register_partner_invite(
            interaction,
            str(self.invite_url.value).strip(),
        )


class PartnerCommissionSimulatorModal(discord.ui.Modal, title="Simular ganhos"):
    project_value = discord.ui.TextInput(
        label="Valor estimado do projeto",
        placeholder="Ex.: 5000 ou 5.000,00",
        max_length=30,
    )
    projects_per_month = discord.ui.TextInput(
        label="Projetos desse valor por mês",
        placeholder="Ex.: 2",
        default="1",
        max_length=3,
    )
    probability = discord.ui.TextInput(
        label="Chance estimada de fechamento (%)",
        placeholder="Ex.: 40",
        default="50",
        max_length=3,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            project_value = _parse_partner_currency(str(self.project_value.value))
            projects_per_month = int(str(self.projects_per_month.value).strip())
            probability = int(str(self.probability.value).strip())
            if not 1 <= projects_per_month <= 100:
                raise ValueError("A quantidade mensal precisa ficar entre 1 e 100.")
            if not 1 <= probability <= 100:
                raise ValueError("A chance de fechamento precisa ficar entre 1% e 100%.")
        except ValueError as error:
            await interaction.response.send_message(str(error), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        card = await asyncio.to_thread(
            render_commission_card,
            project_value,
            projects_per_month,
            probability,
        )
        embed = partner_commission_simulation_embed(
            project_value,
            projects_per_month,
            probability,
        )
        view = PartnerResultV2View(
            embed,
            accent_color=PARTNER_V2_ACCENT_COLOR,
            image=discord.File(card, filename="bella-partner-simulation.png"),
            image_description="Simulação visual de comissão",
        )
        await interaction.followup.send(
            file=view.current_file,
            view=view,
            ephemeral=True,
        )


class PartnerApproachKitModal(discord.ui.Modal, title="Kit personalizado de abordagem"):
    target_name = discord.ui.TextInput(
        label="Pessoa ou empresa",
        placeholder="Ex.: Clínica Horizonte",
        max_length=100,
    )
    business_area = discord.ui.TextInput(
        label="Segmento",
        placeholder="Ex.: clínica odontológica",
        max_length=100,
    )
    need = discord.ui.TextInput(
        label="Necessidade percebida",
        placeholder="Ex.: precisa organizar os agendamentos pelo site",
        style=discord.TextStyle.paragraph,
        max_length=350,
    )
    relationship = discord.ui.TextInput(
        label="Como você conhece esse contato?",
        placeholder="Ex.: cliente antigo, amigo, contato profissional",
        max_length=150,
    )
    tone = discord.ui.TextInput(
        label="Tom desejado",
        placeholder="Ex.: direto, leve, técnico ou descontraído",
        required=False,
        max_length=60,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        values = {
            "target_name": str(self.target_name.value),
            "business_area": str(self.business_area.value),
            "need": str(self.need.value),
            "relationship": str(self.relationship.value),
            "tone": str(self.tone.value),
        }
        content = _partner_approach_content(**values)
        await interaction.response.defer(ephemeral=True, thinking=True)
        card = await asyncio.to_thread(
            render_approach_card,
            target_name=content["target"],
            business_area=content["area"],
            tone=content["tone"],
            opening=content["opening"],
            follow_up=content["follow_up"],
            handoff=content["handoff"],
        )
        embed = partner_approach_kit_embed(**values)
        view = PartnerResultV2View(
            embed,
            accent_color=PARTNER_V2_ACCENT_COLOR,
            image=discord.File(card, filename="bella-partner-approach-kit.png"),
            image_description="Kit visual personalizado de abordagem",
        )
        await interaction.followup.send(
            file=view.current_file,
            view=view,
            ephemeral=True,
        )


class PartnerPanelView(discord.ui.View):
    def __init__(self, manager: PartnerProgramManager) -> None:
        super().__init__(timeout=None)
        self.manager = manager

    @discord.ui.button(
        label="Cadastrar meu link permanente",
        style=discord.ButtonStyle.primary,
        custom_id=PARTNER_PANEL_CUSTOM_ID,
    )
    async def create_invite(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[Any],
    ) -> None:
        await self.manager.handle_panel_button(interaction)


class PartnerInviteInfoView(discord.ui.View):
    def __init__(self, manager: PartnerProgramManager, invite_url: str) -> None:
        super().__init__(timeout=180)
        self.manager = manager
        self.add_item(
            discord.ui.Button(
                label="Abrir link",
                style=discord.ButtonStyle.link,
                url=invite_url,
            )
        )

    @discord.ui.button(label="Trocar link", style=discord.ButtonStyle.secondary)
    async def replace_link(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button[Any],
    ) -> None:
        await self.manager.open_invite_modal(interaction)


class PartnerProfileView(discord.ui.View):
    def __init__(
        self,
        manager: PartnerProgramManager,
        guild: discord.Guild,
        partner_id: int,
    ) -> None:
        super().__init__(timeout=180)
        self.manager = manager
        self.guild = guild
        self.partner_id = partner_id

    @discord.ui.button(label="Meu link", style=discord.ButtonStyle.secondary)
    async def invite(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[Any],
    ) -> None:
        member = self.guild.get_member(self.partner_id) or interaction.user
        embed, view = await self.manager.invite_embed_for(self.guild, member)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @discord.ui.button(label="Atualizar", style=discord.ButtonStyle.primary)
    async def refresh(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[Any],
    ) -> None:
        member = self.guild.get_member(self.partner_id) or interaction.user
        profile = await self.manager.partner_profile(self.guild, member)
        await interaction.response.edit_message(
            embed=partner_profile_embed(self.guild, member, profile),
            view=self,
        )

    @discord.ui.button(label="Ver histórico", style=discord.ButtonStyle.secondary)
    async def history(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[Any],
    ) -> None:
        rows = await asyncio.to_thread(
            self.manager.db.history,
            self.guild.id,
            self.partner_id,
            limit=10,
            offset=0,
        )
        await interaction.response.send_message(
            embed=partner_history_embed(self.guild, self.partner_id, rows, page=0),
            view=PartnerHistoryView(self.manager, self.guild, self.partner_id, 0),
            ephemeral=True,
        )


class PartnerHistoryView(discord.ui.View):
    def __init__(
        self,
        manager: PartnerProgramManager,
        guild: discord.Guild,
        partner_id: int,
        page: int,
    ) -> None:
        super().__init__(timeout=180)
        self.manager = manager
        self.guild = guild
        self.partner_id = partner_id
        self.page = page

    @discord.ui.button(label="Anterior", style=discord.ButtonStyle.secondary)
    async def previous(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[Any],
    ) -> None:
        self.page = max(0, self.page - 1)
        await self._update(interaction)

    @discord.ui.button(label="Próxima", style=discord.ButtonStyle.secondary)
    async def next(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[Any],
    ) -> None:
        self.page += 1
        await self._update(interaction)

    async def _update(self, interaction: discord.Interaction) -> None:
        rows = await asyncio.to_thread(
            self.manager.db.history,
            self.guild.id,
            self.partner_id,
            limit=10,
            offset=self.page * 10,
        )
        if not rows and self.page > 0:
            self.page -= 1
            rows = await asyncio.to_thread(
                self.manager.db.history,
                self.guild.id,
                self.partner_id,
                limit=10,
                offset=self.page * 10,
            )
        await interaction.response.edit_message(
            embed=partner_history_embed(
                self.guild,
                self.partner_id,
                rows,
                page=self.page,
            ),
            view=self,
        )


def partner_panel_embed(settings: PartnerSettings) -> discord.Embed:
    embed = discord.Embed(
        title="Programa de Parceiros",
        description=(
            "Você indica uma oportunidade real, a equipe conduz negociação, projeto, "
            "entrega e suporte, e a comissão é liberada quando a indicação for validada "
            "e o projeto estiver finalizado.\n\n"
            "A comissão oficial do programa é de **50% do valor do projeto**."
        ),
        color=discord.Color(settings.color),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(
        name="Como funciona",
        value=(
            "1. Registre a indicação com contexto e autorização.\n"
            "2. A equipe analisa, negocia e executa o projeto.\n"
            "3. O status aparece no seu painel.\n"
            "4. Após validação e finalização, a comissão é liberada."
        ),
        inline=False,
    )
    embed.add_field(
        name="Seu espaço",
        value=(
            "Use este painel para consultar seu perfil, cadastrar o link permanente e acompanhar "
            "seu histórico. Novas oportunidades ficam no canal de indicações; conversa e networking "
            "ficam no chat exclusivo dos parceiros."
        ),
        inline=False,
    )
    embed.add_field(
        name="Link personalizado",
        value="O parceiro envia um convite sem expiração e sem limite de usos para a Bella monitorar.",
        inline=True,
    )
    embed.add_field(
        name="Partner Intelligence",
        value=(
            "Acompanhe nível, conquistas, missões e um funil que identifica gargalos usando somente "
            "os seus dados reais do programa."
        ),
        inline=False,
    )
    embed.add_field(
        name="Ferramentas exclusivas",
        value=(
            "Simule ganhos em diferentes cenários e gere um kit de abordagem personalizado com "
            "consentimento, primeiro contato e follow-up sem pressão."
        ),
        inline=False,
    )
    embed.add_field(
        name="Privacidade",
        value="Perfil, evolução, funil, simulações e kits são exibidos somente para você.",
        inline=False,
    )
    embed.set_footer(text="Bella Partner Intelligence • indicações legítimas e transparentes")
    return embed


def partner_indications_embed() -> discord.Embed:
    embed = discord.Embed(
        title="Central de Indicações",
        description=(
            "Registre oportunidades reais para sites, sistemas, automações, bots para Discord "
            "ou outras soluções tecnológicas. Você não precisa vender, negociar ou executar o projeto."
        ),
        color=discord.Color(PARTNER_COLOR),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(
        name="Antes de enviar",
        value=(
            "Confirme que o contato foi obtido de forma legítima, que existe uma necessidade real "
            "e que os dados podem ser utilizados para esse atendimento. Spam, compra de leads e "
            "dados obtidos ilegalmente não são aceitos."
        ),
        inline=False,
    )
    embed.add_field(
        name="Contato obrigatório",
        value=(
            "Escolha **WhatsApp**, **E-mail**, **Telefone**, **Discord** ou **Outro contato** e "
            "informe o dado correto da pessoa ou empresa. A Bella valida o formato antes de enviar."
        ),
        inline=False,
    )
    embed.add_field(
        name="Acompanhamento",
        value=(
            "Use **Minhas indicações** para consultar os registros enviados. A equipe valida cada "
            "oportunidade antes de qualquer comissão."
        ),
        inline=False,
    )
    embed.set_footer(text=f"Contato oficial: {PARTNER_CONTACT_EMAIL}")
    return embed


def partner_materials_embed() -> discord.Embed:
    embed = discord.Embed(
        title="Materiais para Parceiros",
        description=(
            "Espaço para conteúdos de prospecção, qualificação de leads, networking, comunicação, "
            "posicionamento, negociação e relacionamento profissional."
        ),
        color=discord.Color(PARTNER_COLOR),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(
        name="Materiais previstos",
        value=(
            "Modelos de mensagens para WhatsApp e e-mail, scripts de abordagem, apresentações, "
            "banners, respostas para objeções e boas práticas de captação."
        ),
        inline=False,
    )
    embed.add_field(
        name="Uso responsável",
        value="Adapte os materiais ao contexto. Não faça promessas, não pressione contatos e não envie spam.",
        inline=False,
    )
    embed.set_footer(text="O conteúdo deste canal pode ser atualizado conforme o programa evoluir.")
    return embed


def partner_support_embed() -> discord.Embed:
    embed = discord.Embed(
        title="Suporte aos Parceiros",
        description=(
            "Abra um ticket privado para tirar dúvidas sobre indicações, status, regras, materiais, "
            "formalização e acompanhamento do programa. A Bella responde dentro do atendimento "
            "usando o contexto do Programa de Parceiros e chama a equipe quando for necessário."
        ),
        color=discord.Color(PARTNER_COLOR),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(
        name="Canais oficiais",
        value=f"Discord oficial e `{PARTNER_CONTACT_EMAIL}`.",
        inline=False,
    )
    embed.add_field(
        name="Importante",
        value=(
            "O canal fica somente para o painel. Use **Abrir ticket privado** e não publique "
            "documentos pessoais, dados bancários ou outras informações sensíveis aqui."
        ),
        inline=False,
    )
    return embed


def partner_rules_embed() -> discord.Embed:
    embed = discord.Embed(
        title="Regras do Programa de Parceiros",
        description=(
            "A participação depende de análise, aceite das regras e formalização quando aplicável. "
            "A inscrição ou indicação não gera aprovação automática."
        ),
        color=discord.Color(PARTNER_COLOR),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(
        name="Comissão",
        value=(
            "A comissão é de **50% do valor do projeto** e só é liberada após validação da indicação, "
            "contratação e finalização do projeto conforme as regras do programa."
        ),
        inline=False,
    )
    embed.add_field(
        name="Participação",
        value=(
            "Não existe meta, horário fixo, obrigação de vender, exclusividade, vínculo empregatício, "
            "sociedade, franquia ou representação comercial."
        ),
        inline=False,
    )
    embed.add_field(
        name="Segurança e legalidade",
        value=(
            "Indicações devem respeitar privacidade, consumidor e legislação brasileira. Participantes "
            "de 16 ou 17 anos dependem de autorização expressa do responsável legal."
        ),
        inline=False,
    )
    embed.set_footer(text=f"Regulamento completo: {PARTNER_PROGRAM_URL}")
    return embed


def partner_referral_created_embed(referral: dict[str, Any]) -> discord.Embed:
    embed = discord.Embed(
        title="Indicação registrada",
        description=(
            "A indicação foi salva e ficará disponível para análise da equipe. O registro não garante "
            "aceite, contratação ou comissão. A Bella avisará por DM quando o status mudar; você "
            "também pode acompanhar tudo em **Minhas indicações**."
        ),
        color=discord.Color.green(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="ID", value=f"`{referral.get('referral_id')}`", inline=True)
    embed.add_field(
        name="Status",
        value=PARTNER_REFERRAL_STATUS_LABELS.get(
            str(referral.get("status") or "submitted"),
            "Enviada para análise",
        ),
        inline=True,
    )
    embed.add_field(name="Pessoa/empresa", value=str(referral.get("contact_name") or "—")[:1024], inline=False)
    embed.add_field(
        name=PARTNER_REFERRAL_CONTACT_TYPES.get(
            str(referral.get("contact_type") or "other"),
            "Contato",
        ),
        value=f"`{str(referral.get('contact_method') or '—')[:1000]}`",
        inline=False,
    )
    embed.add_field(name="Necessidade", value=str(referral.get("need") or "—")[:1024], inline=False)
    return embed


def partner_referral_admin_embed(
    guild: discord.Guild,
    partner: discord.Member,
    referral: dict[str, Any],
    *,
    source_channel: discord.abc.GuildChannel | discord.Thread | None,
) -> discord.Embed:
    contact_type = str(referral.get("contact_type") or "other")
    embed = discord.Embed(
        title="Nova indicação de parceiro",
        description=(
            "Relatório administrativo completo. Os dados de contato abaixo devem ser usados "
            "somente para analisar e atender esta oportunidade."
        ),
        color=discord.Color(PARTNER_COLOR),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="ID da indicação", value=f"`{referral.get('referral_id')}`", inline=True)
    embed.add_field(
        name="Status",
        value=PARTNER_REFERRAL_STATUS_LABELS.get(
            str(referral.get("status") or "submitted"),
            "Enviada para análise",
        ),
        inline=True,
    )
    embed.add_field(
        name="Origem",
        value=getattr(source_channel, "mention", "Canal não identificado"),
        inline=True,
    )
    embed.add_field(
        name="Parceiro responsável",
        value=f"{partner.mention}\nID: `{partner.id}`",
        inline=False,
    )
    embed.add_field(
        name="Pessoa ou empresa indicada",
        value=str(referral.get("contact_name") or "Não informado")[:1024],
        inline=False,
    )
    embed.add_field(
        name=PARTNER_REFERRAL_CONTACT_TYPES.get(contact_type, "Contato autorizado"),
        value=f"`{str(referral.get('contact_method') or 'Não informado')[:1000]}`",
        inline=False,
    )
    embed.add_field(
        name="Necessidade",
        value=str(referral.get("need") or "Não informada")[:1024],
        inline=False,
    )
    embed.add_field(
        name="Contexto da oportunidade",
        value=str(referral.get("context") or "Nenhum contexto adicional informado.")[:1024],
        inline=False,
    )
    embed.add_field(
        name="Consentimento declarado",
        value=(
            "O parceiro confirmou que o contato foi obtido legitimamente e pode ser utilizado "
            "para este atendimento."
        ),
        inline=False,
    )
    embed.set_footer(text=f"{guild.name} | Programa de Parceiros")
    return embed


def _updated_referral_admin_embed(
    message: discord.Message,
    referral: dict[str, Any],
) -> discord.Embed:
    embed = (
        message.embeds[0].copy()
        if message.embeds
        else discord.Embed(title="Indicação de parceiro", color=discord.Color(PARTNER_COLOR))
    )
    status_value = PARTNER_REFERRAL_STATUS_LABELS.get(
        str(referral.get("status") or "submitted"),
        str(referral.get("status") or "Enviada para análise"),
    )
    status_index = next(
        (index for index, field in enumerate(embed.fields) if field.name == "Status"),
        None,
    )
    if status_index is None:
        embed.add_field(name="Status", value=status_value, inline=True)
    else:
        embed.set_field_at(status_index, name="Status", value=status_value, inline=True)
    decision_index = next(
        (index for index, field in enumerate(embed.fields) if field.name == "Última decisão"),
        None,
    )
    decision_value = (
        f"**{status_value}**\n"
        f"Responsável: <@{int(referral.get('reviewed_by') or 0)}>\n"
        f"Motivo/observação: {str(referral.get('status_note') or 'Nenhuma observação informada.')[:850]}\n"
        f"Atualizado: {_format_dt(referral.get('updated_at'))}"
    )
    if decision_index is None:
        embed.add_field(name="Última decisão", value=decision_value[:1024], inline=False)
    else:
        embed.set_field_at(
            decision_index,
            name="Última decisão",
            value=decision_value[:1024],
            inline=False,
        )
    embed.color = discord.Color.green() if referral.get("status") in {"accepted", "closed", "completed"} else (
        discord.Color.red() if referral.get("status") == "rejected" else discord.Color(PARTNER_COLOR)
    )
    return embed


def partner_referral_status_embed(referral: dict[str, Any]) -> discord.Embed:
    status = str(referral.get("status") or "submitted")
    label = PARTNER_REFERRAL_STATUS_LABELS.get(status, status.replace("_", " ").title())
    embed = discord.Embed(
        title="Atualização da sua indicação",
        description=(
            f"A indicação de **{str(referral.get('contact_name') or 'contato indicado')[:200]}** "
            f"agora está com o status **{label}**."
        ),
        color=(
            discord.Color.green()
            if status in {"accepted", "closed", "completed"}
            else discord.Color.red()
            if status == "rejected"
            else discord.Color(PARTNER_COLOR)
        ),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="ID", value=f"`{referral.get('referral_id')}`", inline=True)
    embed.add_field(name="Status", value=label, inline=True)
    note = str(referral.get("status_note") or "").strip()
    if note:
        embed.add_field(name="Observação da equipe", value=note[:1024], inline=False)
    embed.add_field(
        name="Acompanhamento",
        value="Você também pode consultar o estado atualizado em **Minhas indicações**.",
        inline=False,
    )
    embed.set_footer(text="Bella Partner Program")
    return embed


def partner_referrals_embed(
    guild: discord.Guild,
    partner: discord.abc.User,
    referrals: list[dict[str, Any]],
) -> discord.Embed:
    embed = discord.Embed(
        title="Minhas indicações",
        description=f"Registros enviados por {partner.mention}.",
        color=discord.Color(PARTNER_COLOR),
        timestamp=datetime.now(timezone.utc),
    )
    if not referrals:
        embed.description = "Você ainda não registrou nenhuma indicação."
    for referral in referrals[:10]:
        status = str(referral.get("status") or "submitted")
        note = str(referral.get("status_note") or "").strip()
        note_line = f"\nObservação da equipe: {note[:450]}" if note else ""
        embed.add_field(
            name=f"{referral.get('contact_name') or 'Indicação'} • `{referral.get('referral_id')}`",
            value=(
                f"Status: **{PARTNER_REFERRAL_STATUS_LABELS.get(status, status.replace('_', ' ').title())}**\n"
                f"Necessidade: {str(referral.get('need') or 'Não informada')[:500]}"
                f"{note_line}\n"
                f"Enviada: {_format_dt(referral.get('created_at'))}"
            )[:1024],
            inline=False,
        )
    embed.set_footer(text=f"{guild.name} • informações visíveis somente para você")
    return embed


def partner_invite_embed(
    guild: discord.Guild,
    partner: discord.abc.User,
    invite: dict[str, Any],
    profile: dict[str, Any],
    *,
    title: str = "Convite do parceiro",
) -> discord.Embed:
    members = profile.get("members", {})
    embed = discord.Embed(
        title=title,
        description=(
            "O link permanente informado por você foi validado e está sendo monitorado. "
            "Quando alguém entrar por ele enquanto a Bella estiver online, a entrada será "
            "vinculada ao seu perfil."
        ),
        color=discord.Color(PARTNER_COLOR),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="Parceiro", value=f"{partner.mention}\n`{partner.id}`")
    embed.add_field(name="Link monitorado", value=str(invite.get("invite_url", "Indisponível")))
    embed.add_field(
        name="Pessoas que entraram",
        value=str(int(members.get("invited_total", 0))),
    )
    channel_id = _optional_int(invite.get("source_channel_id"))
    embed.add_field(
        name="Canal de destino",
        value=f"<#{channel_id}>" if channel_id else "Não identificado",
    )
    embed.add_field(
        name="Criado em",
        value=_format_dt(invite.get("created_at")),
    )
    embed.add_field(
        name="Status",
        value="Permanente e monitorado",
    )
    embed.set_footer(text=f"{guild.name} | Programa de Parceiros")
    avatar = getattr(partner, "display_avatar", None)
    if avatar:
        embed.set_thumbnail(url=str(avatar.url))
    return embed


def partner_profile_embed(
    guild: discord.Guild,
    partner: discord.abc.User,
    profile: dict[str, Any],
) -> discord.Embed:
    partner_row = profile.get("partner") or {}
    members = profile.get("members", {})
    projects = profile.get("projects", {})
    commissions = profile.get("commissions", {})
    invite = profile.get("invite") or {}
    embed = discord.Embed(
        title="Perfil do Parceiro",
        description=f"Resumo operacional de {partner.mention}.",
        color=discord.Color(PARTNER_COLOR),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="Nome", value=getattr(partner, "display_name", partner.name))
    embed.add_field(name="ID", value=f"`{partner.id}`")
    embed.add_field(
        name="Entrada no programa",
        value=_format_dt(partner_row.get("joined_at")),
    )
    embed.add_field(
        name="Cargo atual",
        value=f"<@&{partner_row.get('role_id')}>" if partner_row.get("role_id") else "Parceiro",
    )
    embed.add_field(
        name="Link personalizado",
        value=str(invite.get("invite_url", "Ainda não cadastrado"))[:1024],
        inline=False,
    )
    embed.add_field(
        name="Indicações",
        value=(
            f"Entraram: **{int(members.get('invited_total', 0))}**\n"
            f"Ativos: **{int(members.get('active_total', 0))}**\n"
            f"Saíram: **{int(members.get('left_total', 0))}**"
        ),
    )
    embed.add_field(
        name="Comercial",
        value=(
            f"Clientes: **{int(members.get('clients_total', 0))}**\n"
            f"Projetos fechados: **{int(projects.get('closed_projects', 0))}**\n"
            f"Em negociação: **{int(projects.get('negotiating_projects', 0))}**"
        ),
    )
    embed.add_field(
        name="Financeiro",
        value=(
            f"Receita: **{_money(projects.get('revenue_total', 0))}**\n"
            f"Comissão recebida: **{_money(commissions.get('paid', 0))}**\n"
            f"Comissão pendente: **{_money(commissions.get('pending', 0))}**\n"
            f"Lucro líquido estimado: **{_money(_partner_net_profit(profile))}**"
        ),
    )
    embed.add_field(
        name="Ranking",
        value=f"#{profile.get('rank')}" if profile.get("rank") else "Sem posição",
    )
    avatar = getattr(partner, "display_avatar", None)
    if avatar:
        embed.set_thumbnail(url=str(avatar.url))
    embed.set_footer(text=f"{guild.name} | Partner Intelligence")
    return embed


def partner_evolution_embed(
    guild: discord.Guild,
    partner: discord.abc.User,
    profile: dict[str, Any],
    referrals: dict[str, int],
) -> discord.Embed:
    evolution = _partner_evolution_summary(profile, referrals)
    embed = discord.Embed(
        title="Jornada do Parceiro",
        description=(
            f"Evolução privada de {partner.mention}, calculada com atividades reais do programa. "
            "Os pontos servem para orientar a jornada e não substituem a validação das indicações."
        ),
        color=discord.Color(PARTNER_COLOR),
        timestamp=datetime.now(timezone.utc),
    )
    next_level = evolution["next_level"]
    progress = (
        f"Próximo nível: **{next_level['name']}** em **{next_level['remaining']} pontos**."
        if next_level
        else "Você alcançou o nível máximo atual do programa."
    )
    embed.add_field(
        name="Nível atual",
        value=(
            f"**{evolution['level']}**\n"
            f"Pontuação: **{evolution['score']}**\n"
            f"{progress}"
        ),
        inline=False,
    )
    embed.add_field(
        name="Conquistas desbloqueadas",
        value="\n".join(f"• {badge}" for badge in evolution["badges"])
        if evolution["badges"]
        else "Nenhuma ainda. A primeira missão já está disponível.",
        inline=False,
    )
    embed.add_field(
        name="Próximas missões",
        value="\n".join(f"{index}. {mission}" for index, mission in enumerate(evolution["missions"], 1)),
        inline=False,
    )
    embed.add_field(
        name="Como os pontos são formados",
        value=(
            "Link permanente, entradas rastreadas, indicações legítimas, avanço no funil, "
            "projetos finalizados e comissões recebidas. Nenhum valor é comprado ou inserido manualmente."
        ),
        inline=False,
    )
    embed.set_footer(text=f"{guild.name} | Jornada recalculada em tempo real")
    return embed


def partner_funnel_embed(
    guild: discord.Guild,
    partner: discord.abc.User,
    profile: dict[str, Any],
    referrals: dict[str, int],
) -> discord.Embed:
    metrics = _partner_funnel_metrics(profile, referrals)
    diagnosis = _partner_funnel_diagnosis(profile, referrals)
    embed = discord.Embed(
        title="Funil Inteligente do Parceiro",
        description=(
            f"Leitura privada do funil de {partner.mention}. A Bella cruza convites, indicações, "
            "negociações e projetos para sugerir o próximo movimento mais útil."
        ),
        color=discord.Color(PARTNER_COLOR),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(
        name="Pipeline de indicações",
        value=(
            f"Enviadas: **{metrics['total']}**\n"
            f"Aguardando análise: **{metrics['waiting']}**\n"
            f"Aceitas: **{metrics['accepted']}**\n"
            f"Em negociação: **{metrics['negotiation']}**\n"
            f"Fechadas/finalizadas: **{metrics['closed']}**\n"
            f"Recusadas: **{metrics['rejected']}**"
        ),
        inline=True,
    )
    embed.add_field(
        name="Indicadores",
        value=(
            f"Qualificação: **{_format_rate(metrics['qualification_rate'])}**\n"
            f"Fechamento: **{_format_rate(metrics['closing_rate'])}**\n"
            f"Retenção dos convites: **{_format_rate(metrics['retention_rate'])}**\n"
            f"Confiança da leitura: **{metrics['confidence']}**"
        ),
        inline=True,
    )
    embed.add_field(
        name="Diagnóstico da Bella",
        value=diagnosis["diagnosis"],
        inline=False,
    )
    embed.add_field(
        name="Próxima melhor ação",
        value=diagnosis["next_action"],
        inline=False,
    )
    embed.add_field(
        name="Nota de leitura",
        value=(
            "Taxas sem volume suficiente aparecem como `—`. O diagnóstico não inventa vendas, "
            "não acessa conversas privadas e utiliza somente registros do Programa de Parceiros."
        ),
        inline=False,
    )
    embed.set_footer(text=f"{guild.name} | Partner Intelligence")
    return embed


def partner_commission_simulation_embed(
    project_value: float,
    projects_per_month: int,
    probability: int,
) -> discord.Embed:
    commission = project_value * 0.5
    monthly_commission = commission * projects_per_month
    weighted_monthly = monthly_commission * (probability / 100)
    embed = discord.Embed(
        title="Laboratório de Ganhos",
        description=(
            "Simulação privada baseada na comissão oficial de **50%**. Ela ajuda a visualizar "
            "cenários, mas não representa promessa de contratação ou pagamento."
        ),
        color=discord.Color.green(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="Valor do projeto", value=_money(project_value), inline=True)
    embed.add_field(name="Comissão por projeto", value=_money(commission), inline=True)
    embed.add_field(name="Projetos no cenário", value=str(projects_per_month), inline=True)
    embed.add_field(
        name="Potencial mensal bruto",
        value=_money(monthly_commission),
        inline=True,
    )
    embed.add_field(
        name=f"Projeção ponderada ({probability}%)",
        value=_money(weighted_monthly),
        inline=True,
    )
    embed.add_field(
        name="Projeção anual ponderada",
        value=_money(weighted_monthly * 12),
        inline=True,
    )
    embed.add_field(
        name="Mapa de cenários",
        value=(
            f"Conservador (25%): **{_money(monthly_commission * 0.25)}**\n"
            f"Informado ({probability}%): **{_money(weighted_monthly)}**\n"
            f"Forte (75%): **{_money(monthly_commission * 0.75)}**"
        ),
        inline=False,
    )
    embed.set_footer(text="Simulação educacional • valores reais dependem da validação e finalização")
    return embed


def partner_approach_kit_embed(
    *,
    target_name: str,
    business_area: str,
    need: str,
    relationship: str,
    tone: str,
) -> discord.Embed:
    content = _partner_approach_content(
        target_name=target_name,
        business_area=business_area,
        need=need,
        relationship=relationship,
        tone=tone,
    )
    embed = discord.Embed(
        title="Kit Personalizado de Abordagem",
        description=(
            "Roteiros criados com os dados informados, sem promessa de resultado e com uma etapa "
            "clara de consentimento antes da indicação."
        ),
        color=discord.Color(PARTNER_COLOR),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(
        name="Primeiro contato",
        value=f"```{content['opening'][:950]}```",
        inline=False,
    )
    embed.add_field(
        name="Follow-up sem pressão",
        value=f"```{content['follow_up'][:950]}```",
        inline=False,
    )
    embed.add_field(
        name="Resumo para registrar na Bella",
        value=f"```{content['handoff'][:950]}```",
        inline=False,
    )
    embed.add_field(
        name="Proteção da indicação",
        value=(
            "Confirme a autorização do contato antes de registrar. Não envie listas compradas, "
            "dados coletados sem consentimento ou mensagens em massa."
        ),
        inline=False,
    )
    embed.set_footer(text="Kit privado • revise a mensagem antes de enviar")
    return embed


def partner_history_embed(
    guild: discord.Guild,
    partner_id: int,
    rows: list[dict[str, Any]],
    *,
    page: int,
) -> discord.Embed:
    embed = discord.Embed(
        title="Histórico do Parceiro",
        description=f"Registros vinculados a <@{partner_id}>.",
        color=discord.Color(PARTNER_COLOR),
        timestamp=datetime.now(timezone.utc),
    )
    if not rows:
        embed.description = "Nenhum registro encontrado para este parceiro."
    for row in rows[:10]:
        status = "Ativo" if row.get("status") == "active" else "Saiu"
        embed.add_field(
            name=f"<@{row.get('member_id')}>",
            value=(
                f"Entrou em: {_format_dt(row.get('joined_at'))}\n"
                f"Saiu em: {_format_dt(row.get('left_at'))}\n"
                f"Status: **{status}**\n"
                f"Cliente: **{'sim' if row.get('is_client') else 'não'}**\n"
                f"Projeto: {row.get('project_status') or 'não informado'}\n"
                f"Observações: {row.get('observations') or 'nenhuma'}"
            )[:1024],
            inline=False,
        )
    embed.set_footer(text=f"Página {page + 1} | {guild.name}")
    return embed


def partner_dashboard_embed(guild: discord.Guild, data: dict[str, Any]) -> discord.Embed:
    embed = discord.Embed(
        title="Dashboard do Programa de Parceiros",
        description="Visão geral das indicações e resultados comerciais.",
        color=discord.Color(PARTNER_COLOR),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(
        name="Entradas",
        value=(
            f"Hoje: **{int(data.get('today', 0))}**\n"
            f"7 dias: **{int(data.get('week', 0))}**\n"
            f"30 dias: **{int(data.get('month', 0))}**\n"
            f"Total: **{int(data.get('total', 0))}**"
        ),
    )
    embed.add_field(
        name="Comercial",
        value=(
            f"Clientes: **{int(data.get('clients', 0))}**\n"
            f"Projetos: **{int(data.get('projects', 0))}**\n"
            f"Conversão: **{float(data.get('conversion', 0)):.1f}%**"
        ),
    )
    embed.add_field(
        name="Financeiro",
        value=(
            f"Receita: **{_money(data.get('revenue', 0))}**\n"
            f"Comissão prevista: **{_money(data.get('commission_pending', 0))}**\n"
            f"Comissão paga: **{_money(data.get('commission_paid', 0))}**"
        ),
    )
    embed.set_footer(text=f"{guild.name} | Programa de Parceiros")
    return embed


def partner_ranking_embed(
    guild: discord.Guild,
    rows: list[dict[str, Any]],
    metric: str,
    *,
    page: int = 0,
) -> discord.Embed:
    labels = {
        "clientes": "clientes",
        "projetos": "projetos",
        "receita": "receita",
        "comissao": "comissão",
    }
    embed = discord.Embed(
        title=f"Ranking de Parceiros por {labels.get(metric, metric).title()}",
        color=discord.Color(PARTNER_COLOR),
        timestamp=datetime.now(timezone.utc),
    )
    medals = ["🥇", "🥈", "🥉"]
    lines = []
    for index, row in enumerate(rows, start=1 + page * 10):
        prefix = medals[index - 1] if index <= 3 else f"{index}."
        score = row.get("score", 0)
        if metric in {"receita", "comissao"}:
            score_text = _money(score)
        else:
            score_text = str(int(score or 0))
        lines.append(f"{prefix} <@{row['partner_id']}> — **{score_text}**")
    embed.description = "\n".join(lines) or "Nenhum parceiro no ranking ainda."
    embed.set_footer(text=f"Top 10 | {guild.name}")
    return embed


def partner_statistics_embed(
    guild: discord.Guild,
    partner: discord.abc.User,
    profile: dict[str, Any],
) -> discord.Embed:
    members = profile.get("members", {})
    projects = profile.get("projects", {})
    commissions = profile.get("commissions", {})
    total = max(1, int(members.get("invited_total", 0)))
    conversion = int(members.get("clients_total", 0)) / total * 100
    embed = discord.Embed(
        title="Estatísticas do Convite",
        description=f"Indicadores do convite de {partner.mention}.",
        color=discord.Color(PARTNER_COLOR),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(
        name="Membros",
        value=(
            f"Entraram: **{int(members.get('invited_total', 0))}**\n"
            f"Saíram: **{int(members.get('left_total', 0))}**\n"
            f"Ativos: **{int(members.get('active_total', 0))}**"
        ),
    )
    embed.add_field(
        name="Conversão",
        value=(
            f"Clientes: **{int(members.get('clients_total', 0))}**\n"
            f"Projetos fechados: **{int(projects.get('closed_projects', 0))}**\n"
            f"Em negociação: **{int(projects.get('negotiating_projects', 0))}**\n"
            f"Taxa: **{conversion:.1f}%**"
        ),
    )
    embed.add_field(
        name="Resultado",
        value=(
            f"Receita: **{_money(projects.get('revenue_total', 0))}**\n"
            f"Comissão paga: **{_money(commissions.get('paid', 0))}**\n"
            f"Comissão pendente: **{_money(commissions.get('pending', 0))}**"
        ),
    )
    embed.set_footer(text=f"{guild.name} | Estatísticas do parceiro")
    return embed


def partner_logs_embed(
    guild: discord.Guild,
    rows: list[dict[str, Any]],
) -> discord.Embed:
    embed = discord.Embed(
        title="Logs do Programa de Parceiros",
        color=discord.Color(PARTNER_COLOR),
        timestamp=datetime.now(timezone.utc),
    )
    if not rows:
        embed.description = "Nenhum log encontrado."
        return embed
    for row in rows[:10]:
        embed.add_field(
            name=f"{row.get('event_type', 'evento')} • {_format_dt(row.get('created_at'))}",
            value=(
                f"{row.get('message')}\n"
                f"Parceiro: {_mention_or_dash(row.get('partner_id'))}\n"
                f"Executor: {_mention_or_dash(row.get('actor_id'))}"
            )[:1024],
            inline=False,
        )
    embed.set_footer(text=guild.name)
    return embed


def partner_log_embed(row: dict[str, Any]) -> discord.Embed:
    embed = discord.Embed(
        title="Registro do Programa de Parceiros",
        description=str(row.get("message", ""))[:4096],
        color=discord.Color(PARTNER_COLOR),
        timestamp=_parse_dt(row.get("created_at")) or datetime.now(timezone.utc),
    )
    embed.add_field(name="Evento", value=str(row.get("event_type", "evento")))
    embed.add_field(name="Parceiro", value=_mention_or_dash(row.get("partner_id")))
    embed.add_field(name="Executor", value=_mention_or_dash(row.get("actor_id")))
    embed.set_footer(text=f"ID do log: {row.get('log_id')}")
    return embed


def _ranking_expression(metric: str) -> str:
    return {
        "clientes": "COALESCE(ms.clients_total, 0)",
        "projetos": "COALESCE(ps.projects_total, 0)",
        "receita": "COALESCE(ps.revenue_total, 0)",
        "comissao": "COALESCE(cs.commission_total, 0)",
    }.get(metric, "COALESCE(ps.revenue_total, 0)")


def _row_to_numbers(row: sqlite3.Row | None) -> dict[str, float]:
    if row is None:
        return {}
    return {key: float(row[key] or 0) for key in row.keys()}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        text = str(value).replace("Z", "+00:00")
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _format_dt(value: Any) -> str:
    parsed = _parse_dt(value)
    if parsed is None:
        return "Não informado"
    return f"<t:{int(parsed.timestamp())}:f>"


def _money(value: Any) -> str:
    amount = float(value or 0)
    formatted = f"{amount:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"R$ {formatted}"


def _normalize_referral_contact(contact_type: str, value: str) -> str:
    kind = str(contact_type or "").strip().casefold()
    text = str(value or "").strip()
    if kind not in PARTNER_REFERRAL_CONTACT_TYPES:
        raise ValueError("Escolha um tipo de contato válido antes de continuar.")
    if not text:
        raise ValueError("Informe o contato autorizado da pessoa ou empresa.")
    if kind == "email":
        normalized = text.casefold()
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", normalized):
            raise ValueError("Esse e-mail não parece válido. Confira e tente novamente.")
        return normalized[:200]
    if kind in {"whatsapp", "phone"}:
        digits = re.sub(r"\D", "", text)
        if digits.startswith("00"):
            digits = digits[2:]
        if len(digits) in {10, 11}:
            digits = f"55{digits}"
        if not 10 <= len(digits) <= 15:
            label = PARTNER_REFERRAL_CONTACT_TYPES[kind]
            raise ValueError(
                f"O número de {label} precisa ter DDD e um telefone válido."
            )
        return f"+{digits}"
    if kind == "discord" and len(text) < 2:
        raise ValueError("Informe o ID, usuário ou perfil do Discord.")
    if kind == "other" and len(text) < 3:
        raise ValueError("Explique qual é o canal de contato e informe o endereço correspondente.")
    return text[:200]


def _partner_net_profit(profile: dict[str, Any]) -> float:
    projects = profile.get("projects") or {}
    commissions = profile.get("commissions") or {}
    revenue = float(projects.get("revenue_total", 0) or 0)
    paid = float(commissions.get("paid", 0) or 0)
    pending = float(commissions.get("pending", 0) or 0)
    return max(0.0, revenue - paid - pending)


def _partner_evolution_summary(
    profile: dict[str, Any],
    referrals: dict[str, int],
) -> dict[str, Any]:
    members = profile.get("members") or {}
    commissions = profile.get("commissions") or {}
    has_invite = bool(profile.get("invite"))
    invited = int(members.get("invited_total", 0))
    referral_total = int(referrals.get("total", 0))
    qualified = sum(
        int(referrals.get(status, 0))
        for status in ("accepted", "negotiation", "closed", "completed")
    )
    closed = int(referrals.get("closed", 0)) + int(referrals.get("completed", 0))
    paid = float(commissions.get("paid", 0) or 0)
    score = (
        (15 if has_invite else 0)
        + min(invited, 10) * 3
        + min(referral_total, 5) * 8
        + min(qualified, 3) * 10
        + min(closed, 3) * 25
        + (20 if paid > 0 else 0)
    )
    levels = (
        (0, "Primeiros passos"),
        (25, "Conector"),
        (60, "Descobridor de oportunidades"),
        (110, "Estrategista"),
        (180, "Embaixador"),
    )
    level = levels[0][1]
    next_level: dict[str, Any] | None = None
    for index, (threshold, name) in enumerate(levels):
        if score >= threshold:
            level = name
            next_level = (
                {
                    "name": levels[index + 1][1],
                    "remaining": max(0, levels[index + 1][0] - score),
                }
                if index + 1 < len(levels)
                else None
            )

    badges: list[str] = []
    if has_invite:
        badges.append("Link permanente ativo")
    if invited:
        badges.append("Primeira entrada rastreada")
    if referral_total:
        badges.append("Radar de oportunidade")
    if qualified:
        badges.append("Indicação validada")
    if int(referrals.get("negotiation", 0)):
        badges.append("Negociação iniciada")
    if closed:
        badges.append("Projeto conquistado")
    if paid > 0:
        badges.append("Comissão recebida")

    missions: list[str] = []
    if not has_invite:
        missions.append("Cadastrar um convite permanente para ativar o rastreamento.")
    if invited == 0:
        missions.append("Conseguir a primeira entrada legítima pelo seu convite.")
    if referral_total == 0:
        missions.append("Registrar a primeira oportunidade com contato autorizado.")
    if referral_total and qualified == 0:
        missions.append("Acompanhar suas indicações até a primeira validação da equipe.")
    if qualified and closed == 0:
        missions.append("Acompanhar uma oportunidade validada até o primeiro fechamento.")
    if closed and paid <= 0:
        missions.append("Concluir o primeiro ciclo até o recebimento da comissão.")
    if not missions:
        missions.append("Manter consistência e construir novas oportunidades legítimas.")
    return {
        "score": score,
        "level": level,
        "next_level": next_level,
        "badges": badges,
        "missions": missions[:3],
    }


def _partner_funnel_diagnosis(
    profile: dict[str, Any],
    referrals: dict[str, int],
) -> dict[str, str]:
    members = profile.get("members") or {}
    commissions = profile.get("commissions") or {}
    total = int(referrals.get("total", 0))
    waiting = int(referrals.get("submitted", 0)) + int(referrals.get("reviewing", 0))
    accepted = int(referrals.get("accepted", 0))
    negotiation = int(referrals.get("negotiation", 0))
    closed = int(referrals.get("closed", 0)) + int(referrals.get("completed", 0))
    invited = int(members.get("invited_total", 0))
    active = int(members.get("active_total", 0))
    if not profile.get("invite"):
        return {
            "diagnosis": "O rastreamento de audiência ainda está incompleto porque não existe link permanente cadastrado.",
            "next_action": "Cadastre um convite permanente e use-o somente em contextos relevantes, sem divulgação em massa.",
        }
    if total == 0:
        return {
            "diagnosis": "Existe estrutura para atrair pessoas, mas ainda não há oportunidades registradas para medir a conversão comercial.",
            "next_action": "Identifique uma necessidade real, confirme a autorização do contato e registre a primeira indicação completa.",
        }
    if waiting > 0 and waiting >= max(accepted + negotiation + closed, 1):
        return {
            "diagnosis": "A maior concentração está no início do funil. Existem indicações aguardando validação ou análise administrativa.",
            "next_action": "Acompanhe os status em Minhas indicações e mantenha os dados de contexto completos; não pressione o contato enquanto a equipe analisa.",
        }
    if accepted > negotiation + closed:
        return {
            "diagnosis": "As oportunidades estão sendo aceitas, mas poucas avançaram para negociação. Esse é o gargalo atual.",
            "next_action": "Confira se o contexto, urgência e melhor canal de contato estão claros para facilitar o próximo passo da equipe.",
        }
    if negotiation > closed:
        return {
            "diagnosis": "O funil possui negociações ativas. O principal ganho agora vem da qualidade do acompanhamento, não do volume.",
            "next_action": "Evite criar pressão paralela. Atualize a equipe somente quando houver informação nova e relevante sobre a necessidade.",
        }
    if closed and float(commissions.get("pending", 0) or 0) > 0:
        return {
            "diagnosis": "Há projeto fechado e comissão pendente registrada. O ciclo comercial avançou para a etapa financeira.",
            "next_action": "Acompanhe o painel financeiro e aguarde a confirmação de finalização e liberação da comissão.",
        }
    if invited >= 5 and _rate(active, invited) is not None and _rate(active, invited) < 50:
        return {
            "diagnosis": "O link gera entradas, porém a permanência no servidor está abaixo de 50%. A qualidade da origem pode ser melhorada.",
            "next_action": "Compartilhe o convite junto de uma explicação clara sobre a comunidade e priorize pessoas realmente interessadas.",
        }
    return {
        "diagnosis": "O funil está equilibrado para o volume atual e não apresenta um gargalo dominante.",
        "next_action": "Mantenha a qualidade das indicações e use o kit personalizado para abordar somente contatos com necessidade real.",
    }


def _partner_funnel_metrics(
    profile: dict[str, Any],
    referrals: dict[str, int],
) -> dict[str, Any]:
    members = profile.get("members") or {}
    total = int(referrals.get("total", 0))
    waiting = int(referrals.get("submitted", 0)) + int(referrals.get("reviewing", 0))
    accepted = int(referrals.get("accepted", 0))
    negotiation = int(referrals.get("negotiation", 0))
    closed = int(referrals.get("closed", 0)) + int(referrals.get("completed", 0))
    rejected = int(referrals.get("rejected", 0))
    qualified = accepted + negotiation + closed
    invited = int(members.get("invited_total", 0))
    active = int(members.get("active_total", 0))
    sample_size = total + invited
    return {
        "total": total,
        "waiting": waiting,
        "accepted": accepted,
        "negotiation": negotiation,
        "closed": closed,
        "rejected": rejected,
        "qualification_rate": _rate(qualified, total),
        "closing_rate": _rate(closed, qualified),
        "retention_rate": _rate(active, invited),
        "confidence": "Alta" if sample_size >= 20 else "Média" if sample_size >= 8 else "Inicial",
    }


def _rate(part: int, total: int) -> float | None:
    if total <= 0:
        return None
    return round((part / total) * 100, 1)


def _format_rate(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:.1f}%".replace(".", ",")


def _parse_partner_currency(value: str) -> float:
    normalized = str(value).strip().lower().replace("r$", "").replace(" ", "")
    if not normalized:
        raise ValueError("Informe o valor estimado do projeto.")
    if "," in normalized:
        normalized = normalized.replace(".", "").replace(",", ".")
    elif normalized.count(".") > 1:
        normalized = normalized.replace(".", "")
    elif normalized.count(".") == 1:
        integer, decimal = normalized.split(".", 1)
        if len(decimal) == 3 and integer.isdigit() and decimal.isdigit():
            normalized = integer + decimal
    if not re.fullmatch(r"\d+(?:\.\d{1,2})?", normalized):
        raise ValueError("Use um valor válido, como 5000 ou 5.000,00.")
    parsed = float(normalized)
    if not 1 <= parsed <= 1_000_000_000:
        raise ValueError("O valor precisa ficar entre R$ 1,00 e R$ 1 bilhão.")
    return round(parsed, 2)


def _safe_template_text(value: str, limit: int) -> str:
    return re.sub(r"\s+", " ", str(value).replace("`", "").strip())[:limit]


def _partner_approach_content(
    *,
    target_name: str,
    business_area: str,
    need: str,
    relationship: str,
    tone: str,
) -> dict[str, str]:
    target = _safe_template_text(target_name, 100)
    area = _safe_template_text(business_area, 100)
    opportunity = _safe_template_text(need, 260)
    connection = _safe_template_text(relationship, 120)
    selected_tone = _safe_template_text(tone, 60) or "natural e respeitoso"
    return {
        "target": target,
        "area": area,
        "tone": selected_tone,
        "opening": (
            f"Oi, {target}. Como a gente se conhece por {connection}, quis te perguntar uma coisa "
            f"de forma bem {selected_tone}: percebi que {opportunity}. Conheço um profissional que "
            f"desenvolve soluções para negócios de {area}. Posso apresentar vocês sem compromisso?"
        ),
        "follow_up": (
            f"Oi, {target}. Passando só para confirmar se faz sentido conversar sobre {opportunity}. "
            "Se não for prioridade agora, fica tranquilo; eu só faço a apresentação se você autorizar."
        ),
        "handoff": (
            f"Contato: {target}\nSegmento: {area}\nRelação com o parceiro: {connection}\n"
            f"Necessidade percebida: {opportunity}\nConsentimento para contato: confirmar antes do envio"
        ),
    }


def _v2_button(
    label: str,
    style: discord.ButtonStyle,
    custom_id: str,
    callback: Callable[[discord.Interaction], Awaitable[None]],
) -> discord.ui.Button[Any]:
    button = discord.ui.Button(
        label=label[:80],
        style=style,
        custom_id=custom_id[:100],
    )
    button.callback = callback  # type: ignore[method-assign]
    return button


def _embed_to_v2_blocks(embed: discord.Embed) -> list[str]:
    blocks: list[str] = []
    header: list[str] = []
    if embed.title:
        header.append(f"## {embed.title}")
    if embed.description:
        header.append(str(embed.description))
    if header:
        blocks.append("\n".join(header))

    inline_fields: list[tuple[str, str]] = []

    def flush_inline_fields() -> bool:
        if not inline_fields:
            return True
        rows: list[str] = []
        current_row: list[str] = []
        current_length = 0
        for name, value in inline_fields:
            compact_value = " · ".join(
                line.strip()
                for line in value.splitlines()
                if line.strip()
            ) or "-"
            block = f"**{name}:** {compact_value}"
            projected = current_length + len(block) + (5 if current_row else 0)
            if current_row and (len(current_row) >= 2 or projected > 110):
                rows.append("  •  ".join(current_row))
                current_row = []
                current_length = 0
            current_row.append(block)
            current_length += len(block) + (5 if len(current_row) > 1 else 0)
        if current_row:
            rows.append("  •  ".join(current_row))
        inline_fields.clear()
        for row in rows:
            candidate = "\n".join([*blocks, row])
            if len(candidate) > 3700:
                blocks.append(
                    "-# Outros detalhes continuam disponíveis nos controles deste painel."
                )
                return False
            blocks.append(row)
        return True

    for field in embed.fields:
        if field.inline:
            inline_fields.append((str(field.name), str(field.value)))
            continue
        if not flush_inline_fields():
            break
        block = f"**{field.name}**\n{field.value}"
        candidate = "\n".join([*blocks, block])
        if len(candidate) > 3700:
            blocks.append("-# Outros detalhes continuam disponíveis nos controles deste painel.")
            break
        blocks.append(block)
    else:
        flush_inline_fields()
    footer = str(getattr(embed.footer, "text", "") or "").strip()
    if footer:
        candidate = "\n".join([*blocks, f"-# {footer}"])
        if len(candidate) <= 3900:
            blocks.append(f"-# {footer}")
    return blocks or ["\u200b"]


def _embed_to_v2_markdown(embed: discord.Embed) -> str:
    return "\n\n".join(_embed_to_v2_blocks(embed))[:3900]


def _add_embed_to_v2_container(
    container: discord.ui.Container[Any],
    embed: discord.Embed,
) -> None:
    container.add_item(discord.ui.TextDisplay(_embed_to_v2_markdown(embed)))


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _invite_code_from_url(value: str) -> str | None:
    match = re.fullmatch(
        r"(?:https?://)?(?:www\.)?(?:discord\.gg/|discord(?:app)?\.com/invite/)"
        r"([A-Za-z0-9-]+)(?:[/?#].*)?",
        str(value).strip(),
        flags=re.IGNORECASE,
    )
    return match.group(1) if match else None


def _invite_is_permanent(invite: Any) -> bool:
    return (
        int(getattr(invite, "max_age", 0) or 0) == 0
        and int(getattr(invite, "max_uses", 0) or 0) == 0
        and not bool(getattr(invite, "temporary", False))
        and getattr(invite, "expires_at", None) is None
    )


def _mention_or_dash(value: Any) -> str:
    parsed = _optional_int(value)
    return f"<@{parsed}>" if parsed else "—"
