from __future__ import annotations

import asyncio
import io
import math
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from PIL import Image, ImageDraw, ImageFont

from .metrics import WeeklyMetricsSnapshot
from .state import StateStore

STATISTICS_SECTION = "server_message_statistics"
BACKGROUND = (2, 2, 3)
PANEL = (14, 14, 17)
TEXT = (250, 250, 252)
MUTED = (184, 187, 196)
GRID = (48, 49, 56)
CYAN = (55, 214, 255)
BLUE = (73, 122, 255)
PURPLE = (155, 105, 255)
GREEN = (66, 211, 146)
RED = (255, 100, 122)
AMBER = (255, 190, 77)


@dataclass(frozen=True)
class MessageStatisticsSnapshot:
    period_start: str
    period_end: str
    total_messages: int
    active_users: int
    active_channels: int
    attachment_messages: int
    total_characters: int
    new_members: int
    voice_seconds: int
    daily: dict[str, int]
    channels: dict[str, int]
    users: dict[str, int]
    hourly: dict[str, int]
    voice_channels: dict[str, int]
    voice_users: dict[str, int]


class MessageStatisticsRecorder:
    def __init__(
        self,
        state: StateStore,
        *,
        timezone_name: str = "America/Sao_Paulo",
        retention_days: int = 120,
    ) -> None:
        self.state = state
        self.retention_days = max(30, retention_days)
        try:
            self.timezone = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError:
            self.timezone = timezone.utc
        self._pending: dict[str, dict[str, dict[str, Any]]] = {}
        self._lock = asyncio.Lock()
        self._voice_sessions: dict[
            tuple[int, int],
            tuple[int, datetime],
        ] = {}

    async def record(
        self,
        *,
        guild_id: int,
        channel_id: int,
        user_id: int,
        created_at: datetime,
        character_count: int,
        attachment_count: int,
    ) -> None:
        local_time = created_at.astimezone(self.timezone)
        guild_key = str(guild_id)
        day_key = local_time.date().isoformat()
        async with self._lock:
            guild = self._pending.setdefault(guild_key, {})
            bucket = guild.setdefault(day_key, _empty_message_bucket())
            bucket["messages"] = int(bucket.get("messages", 0)) + 1
            bucket["characters"] = int(bucket.get("characters", 0)) + max(
                0,
                character_count,
            )
            if attachment_count:
                bucket["attachment_messages"] = (
                    int(bucket.get("attachment_messages", 0)) + 1
                )
            _increment(bucket.setdefault("channels", {}), str(channel_id))
            _increment(bucket.setdefault("users", {}), str(user_id))
            _increment(bucket.setdefault("hours", {}), str(local_time.hour))

    async def record_member_join(
        self,
        *,
        guild_id: int,
        joined_at: datetime,
    ) -> None:
        local_time = joined_at.astimezone(self.timezone)
        async with self._lock:
            guild = self._pending.setdefault(str(guild_id), {})
            bucket = guild.setdefault(
                local_time.date().isoformat(),
                _empty_message_bucket(),
            )
            bucket["new_members"] = int(bucket.get("new_members", 0)) + 1

    async def voice_state_update(
        self,
        *,
        guild_id: int,
        user_id: int,
        before_channel_id: int | None,
        after_channel_id: int | None,
        occurred_at: datetime,
    ) -> None:
        key = (guild_id, user_id)
        async with self._lock:
            existing = self._voice_sessions.pop(key, None)
            if existing is not None:
                channel_id, started_at = existing
                duration = max(0, int((occurred_at - started_at).total_seconds()))
                self._add_voice_duration_locked(
                    guild_id=guild_id,
                    channel_id=channel_id,
                    user_id=user_id,
                    occurred_at=occurred_at,
                    duration_seconds=duration,
                )
            if after_channel_id is not None:
                self._voice_sessions[key] = (after_channel_id, occurred_at)

    async def bootstrap_voice_session(
        self,
        *,
        guild_id: int,
        channel_id: int,
        user_id: int,
        started_at: datetime,
    ) -> None:
        async with self._lock:
            self._voice_sessions.setdefault(
                (guild_id, user_id),
                (channel_id, started_at),
            )

    def _add_voice_duration_locked(
        self,
        *,
        guild_id: int,
        channel_id: int,
        user_id: int,
        occurred_at: datetime,
        duration_seconds: int,
    ) -> None:
        if duration_seconds <= 0:
            return
        local_time = occurred_at.astimezone(self.timezone)
        guild = self._pending.setdefault(str(guild_id), {})
        bucket = guild.setdefault(
            local_time.date().isoformat(),
            _empty_message_bucket(),
        )
        bucket["voice_seconds"] = int(bucket.get("voice_seconds", 0)) + duration_seconds
        voice_channels = bucket.setdefault("voice_channels", {})
        voice_channels[str(channel_id)] = (
            int(voice_channels.get(str(channel_id), 0)) + duration_seconds
        )
        voice_users = bucket.setdefault("voice_users", {})
        voice_users[str(user_id)] = (
            int(voice_users.get(str(user_id), 0)) + duration_seconds
        )

    async def flush(self) -> None:
        async with self._lock:
            if not self._pending:
                return
            pending = self._pending
            self._pending = {}

        for guild_key, daily_updates in pending.items():
            def mutate(section: dict[str, Any]) -> None:
                guild = section.setdefault(guild_key, {})
                if not isinstance(guild, dict):
                    guild = {}
                    section[guild_key] = guild
                daily = guild.setdefault("daily", {})
                if not isinstance(daily, dict):
                    daily = {}
                    guild["daily"] = daily
                for day_key, update in daily_updates.items():
                    current = daily.setdefault(day_key, _empty_message_bucket())
                    if not isinstance(current, dict):
                        current = _empty_message_bucket()
                        daily[day_key] = current
                    _merge_message_bucket(current, update)
                cutoff = (
                    datetime.now(self.timezone).date()
                    - timedelta(days=self.retention_days)
                ).isoformat()
                for day_key in list(daily):
                    if day_key < cutoff:
                        del daily[day_key]

            await self.state.mutate_section(STATISTICS_SECTION, mutate)

    async def snapshot(
        self,
        guild_id: int,
        *,
        days: int = 7,
    ) -> MessageStatisticsSnapshot:
        await self.flush()
        period_days = max(1, min(90, days))
        end = datetime.now(self.timezone).date()
        start = end - timedelta(days=period_days - 1)
        section = await self.state.get_section(STATISTICS_SECTION)
        guild = section.get(str(guild_id), {})
        daily_source = guild.get("daily", {}) if isinstance(guild, dict) else {}
        daily: dict[str, int] = {}
        channels: Counter[str] = Counter()
        users: Counter[str] = Counter()
        hourly: Counter[str] = Counter()
        total_messages = 0
        attachment_messages = 0
        total_characters = 0
        new_members = 0
        voice_seconds = 0
        voice_channels: Counter[str] = Counter()
        voice_users: Counter[str] = Counter()

        for offset in range(period_days):
            day_key = (start + timedelta(days=offset)).isoformat()
            bucket = (
                daily_source.get(day_key, {})
                if isinstance(daily_source, dict)
                else {}
            )
            if not isinstance(bucket, dict):
                bucket = {}
            count = int(bucket.get("messages", 0))
            daily[day_key] = count
            total_messages += count
            attachment_messages += int(bucket.get("attachment_messages", 0))
            total_characters += int(bucket.get("characters", 0))
            new_members += int(bucket.get("new_members", 0))
            voice_seconds += int(bucket.get("voice_seconds", 0))
            channels.update(_integer_mapping(bucket.get("channels", {})))
            users.update(_integer_mapping(bucket.get("users", {})))
            hourly.update(_integer_mapping(bucket.get("hours", {})))
            voice_channels.update(
                _integer_mapping(bucket.get("voice_channels", {}))
            )
            voice_users.update(_integer_mapping(bucket.get("voice_users", {})))

        return MessageStatisticsSnapshot(
            period_start=start.isoformat(),
            period_end=end.isoformat(),
            total_messages=total_messages,
            active_users=len(users),
            active_channels=len(channels),
            attachment_messages=attachment_messages,
            total_characters=total_characters,
            new_members=new_members,
            voice_seconds=voice_seconds,
            daily=daily,
            channels=dict(channels),
            users=dict(users),
            hourly={str(hour): int(hourly.get(str(hour), 0)) for hour in range(24)},
            voice_channels=dict(voice_channels),
            voice_users=dict(voice_users),
        )


class StatisticsImageRenderer:
    width = 1600
    height = 900

    def render_site(
        self,
        snapshot: WeeklyMetricsSnapshot,
        *,
        site_name: str,
    ) -> io.BytesIO:
        image, draw = self._canvas()
        self._header(
            draw,
            title="Website Intelligence",
            subtitle=f"{site_name}  •  {self._period(snapshot.period_start, snapshot.period_end)}",
            badge="SITE",
        )
        incident_count = snapshot.errors_detected + snapshot.changes_detected
        cards = (
            (
                "DISPONIBILIDADE",
                f"{snapshot.availability_percent:.2f}%",
                "Operação no período",
                GREEN if snapshot.availability_percent >= 99 else AMBER,
            ),
            (
                "LATÊNCIA MÉDIA",
                f"{snapshot.average_response_ms:.0f} ms",
                f"Pico de {_number(snapshot.maximum_response_ms)} ms",
                CYAN,
            ),
            (
                "VERIFICAÇÕES",
                _compact_number(snapshot.site_checks),
                f"{snapshot.site_successes} bem-sucedidas",
                BLUE,
            ),
            (
                "OCORRÊNCIAS",
                _compact_number(incident_count),
                (
                    f"{snapshot.errors_detected} erros  •  "
                    f"{snapshot.changes_detected} alterações"
                ),
                RED if incident_count else PURPLE,
            ),
        )
        self._kpi_row(draw, cards)

        daily_latency: list[float] = []
        labels: list[str] = []
        daily_availability: list[float] = []
        for day_key in self._date_keys(
            snapshot.period_start,
            snapshot.period_end,
        ):
            bucket = snapshot.daily.get(day_key, {})
            timed = int(bucket.get("timed_requests", 0))
            total_ms = int(bucket.get("response_time_total_ms", 0))
            daily_latency.append(total_ms / timed if timed else 0)
            checks = int(bucket.get("site_checks", 0))
            successes = int(bucket.get("site_successes", 0))
            daily_availability.append(
                (successes / checks) * 100 if checks else 100.0
            )
            labels.append(self._short_day(day_key))

        self._panel(draw, (70, 330, 1010, 815), "Evolução da latência")
        self._line_chart(
            image,
            draw,
            bounds=(105, 420, 970, 765),
            values=daily_latency,
            labels=labels,
            accent=CYAN,
            unit="ms",
        )
        self._panel(draw, (1035, 330, 1530, 815), "Saúde operacional")
        self._availability_gauge(
            draw,
            center=(1282, 505),
            value=snapshot.availability_percent,
        )
        self._mini_availability_bars(
            draw,
            bounds=(1080, 635, 1485, 750),
            values=daily_availability,
            labels=labels,
        )
        self._footer(draw)
        return self._output(image)

    def render_server(
        self,
        snapshot: MessageStatisticsSnapshot,
        *,
        guild_name: str,
        channel_names: dict[str, str],
    ) -> io.BytesIO:
        image, draw = self._canvas()
        self._header(
            draw,
            title="Community Analytics",
            subtitle=f"{guild_name}  •  {self._period(snapshot.period_start, snapshot.period_end)}",
            badge="DISCORD",
        )
        period_days = max(1, len(snapshot.daily))
        average = snapshot.total_messages / period_days
        cards = (
            (
                "MENSAGENS",
                _compact_number(snapshot.total_messages),
                f"Média de {average:.1f} por dia",
                CYAN,
            ),
            (
                "MEMBROS ATIVOS",
                _compact_number(snapshot.active_users),
                "Participaram no período",
                PURPLE,
            ),
            (
                "CANAIS ATIVOS",
                _compact_number(snapshot.active_channels),
                "Com novas mensagens",
                BLUE,
            ),
            (
                "COM MÍDIA",
                _compact_number(snapshot.attachment_messages),
                f"{_compact_number(snapshot.total_characters)} caracteres",
                GREEN,
            ),
        )
        self._kpi_row(draw, cards)

        labels = [self._short_day(day) for day in snapshot.daily]
        values = [float(value) for value in snapshot.daily.values()]
        self._panel(draw, (70, 330, 1010, 815), "Mensagens por dia")
        self._bar_line_chart(
            image,
            draw,
            bounds=(105, 395, 970, 765),
            values=values,
            labels=labels,
        )
        self._panel(draw, (1035, 330, 1530, 815), "Distribuição da atividade")
        top_channels = sorted(
            snapshot.channels.items(),
            key=lambda item: item[1],
            reverse=True,
        )[:5]
        named_channels = [
            (channel_names.get(channel_id, f"Canal {channel_id}"), count)
            for channel_id, count in top_channels
        ]
        self._top_bars(
            draw,
            bounds=(1075, 392, 1490, 585),
            values=named_channels,
        )
        self._hourly_chart(
            draw,
            bounds=(1075, 650, 1490, 755),
            hourly=snapshot.hourly,
        )
        self._footer(draw)
        return self._output(image)

    def render_summary(
        self,
        *,
        title: str,
        subtitle: str,
        badge: str,
        metrics: tuple[
            tuple[str, str, str, tuple[int, int, int]],
            ...,
        ],
        left_title: str,
        left_rows: list[tuple[str, str]],
        right_title: str,
        right_rows: list[tuple[str, str]],
    ) -> io.BytesIO:
        image, draw = self._canvas()
        self._header(
            draw,
            title=title,
            subtitle=subtitle,
            badge=badge,
        )
        normalized_metrics = list(metrics[:4])
        while len(normalized_metrics) < 4:
            normalized_metrics.append(("INDICADOR", "0", "Sem dados", BLUE))
        self._kpi_row(draw, tuple(normalized_metrics))
        self._panel(draw, (70, 330, 790, 815), left_title)
        self._summary_rows(
            draw,
            bounds=(105, 395, 755, 770),
            rows=left_rows,
            accent=CYAN,
        )
        self._panel(draw, (815, 330, 1530, 815), right_title)
        self._summary_rows(
            draw,
            bounds=(850, 395, 1495, 770),
            rows=right_rows,
            accent=PURPLE,
        )
        self._footer(draw)
        return self._output(image)

    def render_profile(
        self,
        *,
        display_name: str,
        username: str,
        user_id: int,
        guild_name: str,
        avatar_bytes: bytes | None,
        main_role: str,
        joined_at: str,
        created_at: str,
        messages: int,
        tickets: int,
        days_in_server: int,
    ) -> io.BytesIO:
        image, draw = self._canvas()
        self._header(
            draw,
            title="Member Profile",
            subtitle=guild_name,
            badge="PROFILE",
        )
        draw.rounded_rectangle(
            (70, 170, 1530, 815),
            radius=28,
            fill=PANEL,
            outline=(52, 53, 61),
            width=2,
        )
        self._avatar(
            image,
            avatar_bytes,
            bounds=(110, 225, 350, 465),
        )
        draw.text(
            (395, 228),
            _truncate(display_name, 34),
            font=_font(38, bold=True),
            fill=TEXT,
        )
        draw.text(
            (398, 282),
            f"@{username}  •  {user_id}",
            font=_font(18),
            fill=MUTED,
        )
        draw.rounded_rectangle(
            (395, 335, 730, 389),
            radius=18,
            fill=(20, 20, 24),
            outline=(64, 66, 76),
            width=1,
        )
        self._center_text(
            draw,
            (395, 335, 730, 389),
            _truncate(main_role, 28),
            _font(16, bold=True),
            CYAN,
        )
        profile_cards = (
            ("MENSAGENS", _compact_number(messages), "No período registrado", CYAN),
            ("TICKETS", _compact_number(tickets), "Atendimentos criados", PURPLE),
            ("TEMPO NO SERVIDOR", f"{days_in_server}d", "Desde a entrada", GREEN),
        )
        card_width = 330
        for index, (label, value, detail, accent) in enumerate(profile_cards):
            left = 395 + index * (card_width + 25)
            bounds = (left, 445, left + card_width, 580)
            draw.rounded_rectangle(
                bounds,
                radius=20,
                fill=(18, 18, 22),
                outline=(52, 53, 61),
                width=2,
            )
            draw.text(
                (left + 25, 468),
                label,
                font=_font(14, bold=True),
                fill=MUTED,
            )
            draw.text(
                (left + 25, 499),
                value,
                font=_font(32, bold=True),
                fill=accent,
            )
            draw.text(
                (left + 25, 545),
                detail,
                font=_font(13),
                fill=MUTED,
            )
        self._summary_rows(
            draw,
            bounds=(110, 625, 1490, 765),
            rows=[
                ("Entrada no servidor", joined_at),
                ("Conta criada em", created_at),
                ("Cargo principal", main_role),
            ],
            accent=BLUE,
            columns=3,
        )
        self._footer(draw)
        return self._output(image)

    def _canvas(self) -> tuple[Image.Image, ImageDraw.ImageDraw]:
        image = Image.new("RGB", (self.width, self.height), BACKGROUND)
        draw = ImageDraw.Draw(image)
        draw.line((0, 145, self.width, 145), fill=(31, 32, 38), width=1)
        return image, ImageDraw.Draw(image)

    def _header(
        self,
        draw: ImageDraw.ImageDraw,
        *,
        title: str,
        subtitle: str,
        badge: str,
    ) -> None:
        draw.text((70, 52), title, font=_font(44, bold=True), fill=TEXT)
        draw.text((72, 108), subtitle, font=_font(20), fill=MUTED)
        badge_box = (1390, 62, 1530, 108)
        draw.rounded_rectangle(
            badge_box,
            radius=22,
            fill=(18, 19, 23),
            outline=(64, 66, 76),
            width=2,
        )
        self._center_text(draw, badge_box, badge, _font(17, bold=True), CYAN)

    def _kpi_row(
        self,
        draw: ImageDraw.ImageDraw,
        cards: tuple[tuple[str, str, str, tuple[int, int, int]], ...],
    ) -> None:
        card_width = 347
        for index, (label, value, detail, accent) in enumerate(cards):
            left = 70 + index * (card_width + 25)
            bounds = (left, 170, left + card_width, 295)
            draw.rounded_rectangle(
                bounds,
                radius=22,
                fill=PANEL,
                outline=(52, 53, 61),
                width=2,
            )
            draw.rounded_rectangle(
                (left + 20, 191, left + 27, 274),
                radius=4,
                fill=accent,
            )
            draw.text(
                (left + 48, 190),
                label,
                font=_font(15, bold=True),
                fill=MUTED,
            )
            draw.text(
                (left + 48, 216),
                value,
                font=_font(34, bold=True),
                fill=TEXT,
            )
            draw.text(
                (left + 48, 263),
                detail,
                font=_font(14),
                fill=MUTED,
            )

    def _panel(
        self,
        draw: ImageDraw.ImageDraw,
        bounds: tuple[int, int, int, int],
        title: str,
    ) -> None:
        draw.rounded_rectangle(
            bounds,
            radius=26,
            fill=PANEL,
            outline=(52, 53, 61),
            width=2,
        )
        draw.text(
            (bounds[0] + 35, bounds[1] + 28),
            title,
            font=_font(21, bold=True),
            fill=TEXT,
        )

    def _line_chart(
        self,
        image: Image.Image,
        draw: ImageDraw.ImageDraw,
        *,
        bounds: tuple[int, int, int, int],
        values: list[float],
        labels: list[str],
        accent: tuple[int, int, int],
        unit: str,
    ) -> None:
        left, top, right, bottom = bounds
        maximum = max(values or [1])
        maximum = max(1, maximum * 1.18)
        for index in range(5):
            y = top + index * (bottom - top) / 4
            draw.line((left, y, right, y), fill=GRID, width=1)
            value = maximum * (1 - index / 4)
            draw.text(
                (left, y - 22),
                f"{value:.0f} {unit}",
                font=_font(12),
                fill=MUTED,
            )
        points = self._points(values, (left, top + 15, right, bottom - 30), maximum)
        if len(points) >= 2:
            overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
            overlay_draw = ImageDraw.Draw(overlay)
            polygon = [points[0], *points, (points[-1][0], bottom), (points[0][0], bottom)]
            overlay_draw.polygon(polygon, fill=(*accent, 34))
            image.paste(Image.alpha_composite(image.convert("RGBA"), overlay).convert("RGB"))
            draw = ImageDraw.Draw(image)
            draw.line(points, fill=accent, width=5, joint="curve")
        for x, y in points:
            draw.ellipse((x - 6, y - 6, x + 6, y + 6), fill=accent)
            draw.ellipse((x - 2, y - 2, x + 2, y + 2), fill=TEXT)
        self._axis_labels(draw, labels, left, right, bottom + 8)

    def _bar_line_chart(
        self,
        image: Image.Image,
        draw: ImageDraw.ImageDraw,
        *,
        bounds: tuple[int, int, int, int],
        values: list[float],
        labels: list[str],
    ) -> None:
        left, top, right, bottom = bounds
        maximum = max(1, max(values or [1]))
        count = max(1, len(values))
        slot = (right - left) / count
        bar_width = max(8, min(34, int(slot * 0.55)))
        points: list[tuple[float, float]] = []
        for index, value in enumerate(values):
            x = left + slot * index + slot / 2
            height = (bottom - top - 25) * (value / maximum)
            bar_top = bottom - height
            draw.rounded_rectangle(
                (x - bar_width / 2, bar_top, x + bar_width / 2, bottom),
                radius=max(3, bar_width // 4),
                fill=(60, 65, 78),
            )
            points.append((x, bar_top))
        if len(points) >= 2:
            draw.line(points, fill=CYAN, width=4, joint="curve")
        for x, y in points:
            draw.ellipse((x - 5, y - 5, x + 5, y + 5), fill=CYAN)
        for index in range(5):
            y = top + index * (bottom - top) / 4
            draw.line((left, y, right, y), fill=GRID, width=1)
        self._axis_labels(draw, labels, left, right, bottom + 8)

    def _availability_gauge(
        self,
        draw: ImageDraw.ImageDraw,
        *,
        center: tuple[int, int],
        value: float,
    ) -> None:
        x, y = center
        bounds = (x - 106, y - 106, x + 106, y + 106)
        draw.arc(bounds, start=135, end=405, fill=GRID, width=22)
        end = 135 + 270 * max(0, min(100, value)) / 100
        color = GREEN if value >= 99 else AMBER if value >= 95 else RED
        draw.arc(bounds, start=135, end=end, fill=color, width=22)
        value_text = f"{value:.2f}%"
        value_box = draw.textbbox((0, 0), value_text, font=_font(38, bold=True))
        draw.text(
            (x - (value_box[2] - value_box[0]) / 2, y - 28),
            value_text,
            font=_font(38, bold=True),
            fill=TEXT,
        )
        label = "DISPONIBILIDADE"
        label_box = draw.textbbox((0, 0), label, font=_font(13, bold=True))
        draw.text(
            (x - (label_box[2] - label_box[0]) / 2, y + 26),
            label,
            font=_font(13, bold=True),
            fill=MUTED,
        )

    def _mini_availability_bars(
        self,
        draw: ImageDraw.ImageDraw,
        *,
        bounds: tuple[int, int, int, int],
        values: list[float],
        labels: list[str],
    ) -> None:
        left, top, right, bottom = bounds
        draw.text(
            (left, top - 34),
            "Disponibilidade diária",
            font=_font(15, bold=True),
            fill=MUTED,
        )
        count = max(1, len(values))
        slot = (right - left) / count
        width = max(4, min(18, int(slot * 0.48)))
        for index, value in enumerate(values):
            height = max(4, (bottom - top) * max(0, min(100, value)) / 100)
            x = left + index * slot + slot / 2
            color = GREEN if value >= 99 else AMBER if value >= 95 else RED
            draw.rounded_rectangle(
                (x - width / 2, bottom - height, x + width / 2, bottom),
                radius=max(2, width // 3),
                fill=color,
            )
        self._axis_labels(draw, labels, left, right, bottom + 8)

    def _top_bars(
        self,
        draw: ImageDraw.ImageDraw,
        *,
        bounds: tuple[int, int, int, int],
        values: list[tuple[str, int]],
    ) -> None:
        left, top, right, _ = bounds
        draw.text(
            (left, top - 2),
            "Canais com mais mensagens",
            font=_font(15, bold=True),
            fill=MUTED,
        )
        maximum = max([value for _, value in values] or [1])
        for index, (name, value) in enumerate(values):
            y = top + 37 + index * 31
            label = _truncate(name, 22)
            draw.text((left, y), label, font=_font(13), fill=TEXT)
            bar_left = left + 176
            bar_right = right - 42
            draw.rounded_rectangle(
                (bar_left, y + 4, bar_right, y + 16),
                radius=6,
                fill=GRID,
            )
            fill_right = bar_left + (bar_right - bar_left) * value / maximum
            draw.rounded_rectangle(
                (bar_left, y + 4, max(bar_left + 6, fill_right), y + 16),
                radius=6,
                fill=PURPLE,
            )
            draw.text(
                (right - 34, y),
                str(value),
                font=_font(13, bold=True),
                fill=MUTED,
            )
        if not values:
            draw.text(
                (left, top + 50),
                "Aguardando atividade suficiente.",
                font=_font(15),
                fill=MUTED,
            )

    def _summary_rows(
        self,
        draw: ImageDraw.ImageDraw,
        *,
        bounds: tuple[int, int, int, int],
        rows: list[tuple[str, str]],
        accent: tuple[int, int, int],
        columns: int = 1,
    ) -> None:
        left, top, right, bottom = bounds
        visible = rows[: max(1, columns) * 7]
        if not visible:
            draw.text(
                (left, top + 20),
                "Aguardando dados suficientes.",
                font=_font(16),
                fill=MUTED,
            )
            return
        columns = max(1, columns)
        rows_per_column = max(1, math.ceil(len(visible) / columns))
        column_width = (right - left) / columns
        row_height = min(58, (bottom - top) / max(1, rows_per_column))
        for index, (label, value) in enumerate(visible):
            column = index // rows_per_column
            row = index % rows_per_column
            x = left + column * column_width
            y = top + row * row_height
            draw.rounded_rectangle(
                (x, y + 7, x + 6, y + row_height - 10),
                radius=3,
                fill=accent,
            )
            draw.text(
                (x + 22, y + 4),
                _truncate(label, 32),
                font=_font(14, bold=True),
                fill=MUTED,
            )
            value_text = _truncate(str(value), 48 if columns == 1 else 28)
            draw.text(
                (x + 22, y + 27),
                value_text,
                font=_font(18, bold=True),
                fill=TEXT,
            )
            if row < rows_per_column - 1:
                draw.line(
                    (x + 22, y + row_height - 3, x + column_width - 22, y + row_height - 3),
                    fill=GRID,
                    width=1,
                )

    def _avatar(
        self,
        image: Image.Image,
        avatar_bytes: bytes | None,
        *,
        bounds: tuple[int, int, int, int],
    ) -> None:
        left, top, right, bottom = bounds
        size = min(right - left, bottom - top)
        if avatar_bytes:
            try:
                avatar = Image.open(io.BytesIO(avatar_bytes)).convert("RGB")
                avatar = avatar.resize((size, size), Image.Resampling.LANCZOS)
            except (OSError, ValueError):
                avatar = Image.new("RGB", (size, size), (34, 35, 42))
        else:
            avatar = Image.new("RGB", (size, size), (34, 35, 42))
        mask = Image.new("L", (size, size), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, size - 1, size - 1), fill=255)
        ring = Image.new("RGB", (size + 16, size + 16), CYAN)
        ring_mask = Image.new("L", ring.size, 0)
        ImageDraw.Draw(ring_mask).ellipse(
            (0, 0, ring.size[0] - 1, ring.size[1] - 1),
            fill=255,
        )
        image.paste(ring, (left - 8, top - 8), ring_mask)
        image.paste(avatar, (left, top), mask)

    def _hourly_chart(
        self,
        draw: ImageDraw.ImageDraw,
        *,
        bounds: tuple[int, int, int, int],
        hourly: dict[str, int],
    ) -> None:
        left, top, right, bottom = bounds
        draw.text(
            (left, top - 35),
            "Atividade por horário",
            font=_font(15, bold=True),
            fill=MUTED,
        )
        values = [int(hourly.get(str(hour), 0)) for hour in range(24)]
        maximum = max(values or [1]) or 1
        slot = (right - left) / 24
        width = max(3, int(slot * 0.55))
        for hour, value in enumerate(values):
            height = max(3, (bottom - top) * value / maximum)
            x = left + hour * slot + slot / 2
            color = CYAN if value == maximum and value else BLUE
            draw.rounded_rectangle(
                (x - width / 2, bottom - height, x + width / 2, bottom),
                radius=3,
                fill=color,
            )
        for hour in (0, 6, 12, 18, 23):
            x = left + hour * slot + slot / 2
            draw.text(
                (x - 10, bottom + 7),
                f"{hour:02d}h",
                font=_font(11),
                fill=MUTED,
            )

    def _footer(self, draw: ImageDraw.ImageDraw) -> None:
        draw.text(
            (70, 855),
            "BELLA INTELLIGENCE CENTER  •  v2026.06",
            font=_font(13, bold=True),
            fill=(145, 148, 158),
        )
        generated = datetime.now().astimezone().strftime("%d/%m/%Y  •  %H:%M")
        text_box = draw.textbbox((0, 0), generated, font=_font(13))
        draw.text(
            (1530 - (text_box[2] - text_box[0]), 855),
            generated,
            font=_font(13),
            fill=(145, 148, 158),
        )

    def _points(
        self,
        values: list[float],
        bounds: tuple[int, int, int, int],
        maximum: float,
    ) -> list[tuple[float, float]]:
        left, top, right, bottom = bounds
        if not values:
            return []
        if len(values) == 1:
            return [((left + right) / 2, bottom - (bottom - top) * values[0] / maximum)]
        return [
            (
                left + index * (right - left) / (len(values) - 1),
                bottom - (bottom - top) * value / maximum,
            )
            for index, value in enumerate(values)
        ]

    def _axis_labels(
        self,
        draw: ImageDraw.ImageDraw,
        labels: list[str],
        left: int,
        right: int,
        y: int,
    ) -> None:
        if not labels:
            return
        indices = _label_indices(len(labels), maximum=7)
        for index in indices:
            x = (
                (left + right) / 2
                if len(labels) == 1
                else left + index * (right - left) / (len(labels) - 1)
            )
            label = labels[index]
            box = draw.textbbox((0, 0), label, font=_font(11))
            draw.text(
                (x - (box[2] - box[0]) / 2, y),
                label,
                font=_font(11),
                fill=MUTED,
            )

    def _center_text(
        self,
        draw: ImageDraw.ImageDraw,
        bounds: tuple[int, int, int, int],
        text: str,
        font: ImageFont.ImageFont,
        fill: tuple[int, int, int],
    ) -> None:
        box = draw.textbbox((0, 0), text, font=font)
        x = bounds[0] + (bounds[2] - bounds[0] - (box[2] - box[0])) / 2
        y = bounds[1] + (bounds[3] - bounds[1] - (box[3] - box[1])) / 2 - 2
        draw.text((x, y), text, font=font, fill=fill)

    def _output(self, image: Image.Image) -> io.BytesIO:
        output = io.BytesIO()
        image.save(output, format="PNG", optimize=True)
        output.seek(0)
        return output

    def _period(self, start: str, end: str) -> str:
        return (
            f"{datetime.fromisoformat(start).strftime('%d/%m/%Y')} — "
            f"{datetime.fromisoformat(end).strftime('%d/%m/%Y')}"
        )

    def _date_keys(self, start: str, end: str) -> list[str]:
        first = datetime.fromisoformat(start).date()
        last = datetime.fromisoformat(end).date()
        days = max(0, (last - first).days)
        return [(first + timedelta(days=offset)).isoformat() for offset in range(days + 1)]

    def _short_day(self, value: str) -> str:
        return datetime.fromisoformat(value).strftime("%d/%m")


def _empty_message_bucket() -> dict[str, Any]:
    return {
        "messages": 0,
        "characters": 0,
        "attachment_messages": 0,
        "new_members": 0,
        "voice_seconds": 0,
        "channels": {},
        "users": {},
        "hours": {},
        "voice_channels": {},
        "voice_users": {},
    }


def _merge_message_bucket(current: dict[str, Any], update: dict[str, Any]) -> None:
    for key in (
        "messages",
        "characters",
        "attachment_messages",
        "new_members",
        "voice_seconds",
    ):
        current[key] = int(current.get(key, 0)) + int(update.get(key, 0))
    for key in ("channels", "users", "hours", "voice_channels", "voice_users"):
        destination = current.setdefault(key, {})
        if not isinstance(destination, dict):
            destination = {}
            current[key] = destination
        for item, value in _integer_mapping(update.get(key, {})).items():
            destination[item] = int(destination.get(item, 0)) + value


def _increment(values: dict[str, Any], key: str) -> None:
    values[key] = int(values.get(key, 0)) + 1


def _integer_mapping(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, int] = {}
    for key, count in value.items():
        try:
            result[str(key)] = int(count)
        except (TypeError, ValueError):
            continue
    return result


def _font(size: int, *, bold: bool = False) -> ImageFont.ImageFont:
    candidates = (
        Path("C:/Windows/Fonts/seguisb.ttf" if bold else "C:/Windows/Fonts/segoeui.ttf"),
        Path(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
            if bold
            else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
        ),
        Path(
            "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf"
            if bold
            else "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf"
        ),
    )
    for path in candidates:
        if path.exists():
            return ImageFont.truetype(str(path), size=size)
    return ImageFont.load_default()


def _gaussian_blur(radius: int):
    from PIL import ImageFilter

    return ImageFilter.GaussianBlur(radius=radius)


def _compact_number(value: int) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M".replace(".0", "")
    if value >= 1_000:
        return f"{value / 1_000:.1f}K".replace(".0", "")
    return str(value)


def _number(value: int | None) -> str:
    return str(value) if value is not None else "0"


def _truncate(value: str, length: int) -> str:
    clean = " ".join(value.split())
    return clean if len(clean) <= length else f"{clean[: length - 1]}…"


def _label_indices(total: int, *, maximum: int) -> list[int]:
    if total <= maximum:
        return list(range(total))
    step = (total - 1) / (maximum - 1)
    return sorted({min(total - 1, round(index * step)) for index in range(maximum)})
