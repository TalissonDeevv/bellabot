"""Lembretes por linguagem natural com DM 5 minutos antes do evento.

- Persistência no banco de estado (sobrevive a reinício, deploy e queda).
- Horários guardados em UTC com o fuso e o texto local originais.
- Scheduler por consulta periódica (não depende de asyncio.sleep até o evento):
  reserva atômica (PENDING -> PROCESSING), recuperação de reservas presas,
  tentativas limitadas com espera crescente e registro de cada entrega.
- Texto da notificação gerado pela FreeLLMAPI; se ela falhar, mensagem de
  contingência. Uma falha da IA nunca perde o lembrete.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from unicodedata import combining, normalize
from zoneinfo import ZoneInfo

import discord

LOGGER = logging.getLogger(__name__)

SECTION = "bella_reminders"
LOG_SECTION = "bella_reminder_delivery_logs"
STATES = ("PENDING", "PROCESSING", "SENT", "FAILED", "CANCELLED", "EXPIRED")
WEEKDAYS = {
    "segunda": 0, "terca": 1, "quarta": 2, "quinta": 3, "sexta": 4, "sabado": 5, "domingo": 6,
}
MONTHS = {
    "janeiro": 1, "fevereiro": 2, "marco": 3, "abril": 4, "maio": 5, "junho": 6, "julho": 7,
    "agosto": 8, "setembro": 9, "outubro": 10, "novembro": 11, "dezembro": 12,
}
MONTH_NAMES = [
    "", "janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho",
    "agosto", "setembro", "outubro", "novembro", "dezembro",
]
INTENT_RE = re.compile(
    r"\b(?:me lembr\w*|lembr[ae](?:-me)? (?:de|que|da|do|disso|dessa|desse)|lembrete\w*|"
    r"me avis\w* (?:quando|antes|no dia|dia|amanha|hoje|as|na|no)|"
    r"(?:quais|meus|que) (?:lembretes|compromissos)|"
    r"o que (?:eu )?tenho (?:marcado|agendado|pra|para|amanha|hoje)|"
    r"(?:cancel\w*|apag\w*|remov\w*|exclu\w*) (?:o |aquele |esse |meu )?(?:lembrete|compromisso)|"
    r"(?:mud\w*|adi\w*|remarc\w*|alter\w*) (?:minha |meu |o |a |esse |essa |aquele |aquela )?"
    r"(?:reuniao|lembrete|compromisso|evento|consulta)|me lembra novamente)"
)
FALLBACK_TEXTS = (
    "oie! passando pra te lembrar: {desc}, daqui a {lead} minutinhos ({hora}). ♡",
    "ei, não esquece! daqui a {lead} minutos ({hora}) tem: {desc}.",
    "vim cumprir minha promessa: {desc} começa às {hora}. já tá tudo pronto por aí?",
)


def _norm(value: str) -> str:
    decomposed = normalize("NFKD", str(value or "").casefold())
    return " ".join("".join(c for c in decomposed if not combining(c)).split())


def looks_like_reminder_request(text: str) -> bool:
    return bool(INTENT_RE.search(_norm(text)))


def parse_when(text: str, now: datetime) -> tuple[datetime | None, str]:
    """Data e hora em linguagem natural (pt-BR). Devolve (datahora, motivo_se_faltar)."""
    t = _norm(text)
    tz = now.tzinfo
    rel = re.search(r"\bdaqui a (\d+|um|uma|duas|dois|meia) (minuto|minutos|hora|horas)\b", t)
    if rel:
        words = {"um": 1, "uma": 1, "dois": 2, "duas": 2, "meia": 0.5}
        amount = float(words.get(rel.group(1), rel.group(1)))
        delta = timedelta(minutes=amount) if rel.group(2).startswith("minuto") else timedelta(hours=amount)
        return (now + delta).replace(second=0, microsecond=0), ""

    hour_match = re.search(
        r"\b(?:as|às|a|pelas|pela)?\s*(\d{1,2})(?:(?:h|:|horas?\s*e?\s*)(\d{2})?\s*(?:h|horas?|min)?|\s*(?:h|horas?))\b"
        r"(?:\s*(da manha|da tarde|da noite))?",
        t,
    )
    if hour_match is None:
        # "às 7 da noite", "às 15", "pelas 9:30" (sem o "h")
        hour_match = re.search(
            r"\b(?:as|pelas|pela)\s+(\d{1,2})(?::(\d{2}))?(?!\s*/)\b(?:\s*(da manha|da tarde|da noite))?",
            t,
        )
    hour = minute = None
    if hour_match:
        hour = int(hour_match.group(1))
        minute = int(hour_match.group(2) or 0)
        period = hour_match.group(3) or ""
        if period in {"da tarde", "da noite"} and hour < 12:
            hour += 12
        if hour > 23 or minute > 59:
            return None, "horário inválido"

    day: datetime | None = None
    date_num = re.search(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b", t)
    date_word = re.search(r"\b(\d{1,2}) de (" + "|".join(MONTHS) + r")(?: de (\d{4}))?\b", t)
    weekday = re.search(r"\b(?:(?:na|no|nessa|nesta|proxima|proximo)\s+)*(" + "|".join(WEEKDAYS) + r")(?:-feira| feira)?\b", t)
    if date_num:
        d, m = int(date_num.group(1)), int(date_num.group(2))
        y = int(date_num.group(3)) if date_num.group(3) else now.year
        y = y + 2000 if y < 100 else y
        try:
            day = datetime(y, m, d, tzinfo=tz)
        except ValueError:
            return None, "data inválida"
        if not date_num.group(3) and day.date() < now.date():
            day = day.replace(year=y + 1)
    elif date_word:
        d, m = int(date_word.group(1)), MONTHS[date_word.group(2)]
        y = int(date_word.group(3)) if date_word.group(3) else now.year
        try:
            day = datetime(y, m, d, tzinfo=tz)
        except ValueError:
            return None, "data inválida"
        if not date_word.group(3) and day.date() < now.date():
            day = day.replace(year=y + 1)
    elif re.search(r"\bdepois de amanha\b", t):
        day = now + timedelta(days=2)
    elif re.search(r"\bamanha\b", t):
        day = now + timedelta(days=1)
    elif re.search(r"\bhoje\b", t):
        day = now
    elif weekday:
        target = WEEKDAYS[weekday.group(1)]
        ahead = (target - now.weekday()) % 7
        if ahead == 0 or "proxim" in weekday.group(0):
            ahead = ahead or 7
        day = now + timedelta(days=ahead)

    if day is None and hour is None:
        return None, "faltou a data e o horário"
    if hour is None:
        return None, "faltou o horário"
    if day is None:
        day = now
        candidate = day.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= now:
            candidate += timedelta(days=1)
        return candidate, ""
    return day.replace(hour=hour, minute=minute, second=0, microsecond=0), ""


def describe_when(moment: datetime) -> str:
    return f"{moment.day} de {MONTH_NAMES[moment.month]}, às {moment.strftime('%Hh%M').replace('h00', 'h')}"


class ReminderService:
    def __init__(
        self,
        bot: Any,
        state: Any,
        *,
        ai_service: Any | None = None,
        timezone_name: str | None = None,
        lead_minutes: int | None = None,
        poll_seconds: float = 20.0,
        max_attempts: int = 4,
        grace_hours: float = 6.0,
    ) -> None:
        self.bot = bot
        self.state = state
        self.ai_service = ai_service
        self.tz = ZoneInfo(timezone_name or os.getenv("REMINDER_TIMEZONE", "America/Recife"))
        self.lead = timedelta(minutes=int(lead_minutes or os.getenv("REMINDER_LEAD_MINUTES", "5") or 5))
        self.poll_seconds = poll_seconds
        self.max_attempts = max_attempts
        self.grace = timedelta(hours=grace_hours)

    def now(self) -> datetime:
        return datetime.now(self.tz)

    # --------------------------------------------------------------- dados
    async def create(
        self,
        *,
        user_id: int,
        description: str,
        event_at: datetime,
        guild_id: int | None = None,
        channel_id: int | None = None,
        source_message_id: int | None = None,
    ) -> dict[str, Any]:
        if event_at.tzinfo is None:
            event_at = event_at.replace(tzinfo=self.tz)
        if event_at <= self.now():
            raise ValueError("esse horário já passou")
        description = " ".join(description.split())[:300]
        if not description:
            raise ValueError("faltou dizer do que é pra lembrar")
        notify_at = max(event_at - self.lead, self.now())
        now_iso = datetime.now(timezone.utc).isoformat()
        record = {
            "id": uuid.uuid4().hex[:12],
            "user_id": int(user_id),
            "guild_id": guild_id,
            "channel_id": channel_id,
            "source_message_id": source_message_id,
            "description": description,
            "event_at_utc": event_at.astimezone(timezone.utc).isoformat(),
            "event_local": event_at.astimezone(self.tz).strftime("%Y-%m-%d %H:%M"),
            "timezone": str(self.tz),
            "notify_at_utc": notify_at.astimezone(timezone.utc).isoformat(),
            "status": "PENDING",
            "attempts": 0,
            "created_at": now_iso,
            "updated_at": now_iso,
            "sent_at": None,
            "message_id": None,
            "history": [{"at": now_iso, "event": "created"}],
        }
        await self.state.update_section(SECTION, record["id"], record)
        return record

    async def list_for(
        self,
        user_id: int,
        *,
        day: datetime | None = None,
        include_done: bool = False,
    ) -> list[dict[str, Any]]:
        values = [
            value
            for value in (await self.state.get_section(SECTION)).values()
            if isinstance(value, dict) and int(value.get("user_id", 0)) == int(user_id)
            and (include_done or value.get("status") in {"PENDING", "PROCESSING"})
        ]
        if day is not None:
            values = [v for v in values if v.get("event_local", "").startswith(day.strftime("%Y-%m-%d"))]
        return sorted(values, key=lambda v: v.get("event_at_utc", ""))

    async def find(self, user_id: int, query: str) -> list[dict[str, Any]]:
        words = {w for w in re.findall(r"[a-z0-9]+", _norm(query)) if len(w) >= 4}
        scored = []
        for value in await self.list_for(user_id):
            blob = _norm(value.get("description", ""))
            score = sum(1 for w in words if w in blob)
            scored.append((score, value))
        scored.sort(key=lambda item: (-item[0], item[1].get("event_at_utc", "")))
        best = [value for score, value in scored if score == scored[0][0]] if scored else []
        return best if (scored and scored[0][0] > 0) else [value for _, value in scored]

    async def _set(self, reminder_id: str, event: str, **fields: Any) -> dict[str, Any] | None:
        result: list[dict[str, Any]] = []

        def mutate(section: dict[str, Any]) -> None:
            record = section.get(reminder_id)
            if not isinstance(record, dict):
                return
            now_iso = datetime.now(timezone.utc).isoformat()
            record.update(fields)
            record["updated_at"] = now_iso
            record.setdefault("history", []).append({"at": now_iso, "event": event})
            record["history"] = record["history"][-30:]
            result.append(dict(record))

        await self.state.mutate_section(SECTION, mutate)
        return result[0] if result else None

    async def cancel(self, reminder_id: str, user_id: int) -> bool:
        record = (await self.state.get_section(SECTION)).get(reminder_id)
        if not record or int(record.get("user_id", 0)) != int(user_id):
            return False
        return await self._set(reminder_id, "cancelled", status="CANCELLED") is not None

    async def reschedule(self, reminder_id: str, user_id: int, event_at: datetime) -> dict[str, Any]:
        record = (await self.state.get_section(SECTION)).get(reminder_id)
        if not record or int(record.get("user_id", 0)) != int(user_id):
            raise ValueError("lembrete não encontrado")
        if event_at <= self.now():
            raise ValueError("esse horário já passou")
        notify_at = max(event_at - self.lead, self.now())
        updated = await self._set(
            reminder_id,
            "rescheduled",
            status="PENDING",
            attempts=0,
            next_attempt_at=None,
            event_at_utc=event_at.astimezone(timezone.utc).isoformat(),
            event_local=event_at.astimezone(self.tz).strftime("%Y-%m-%d %H:%M"),
            notify_at_utc=notify_at.astimezone(timezone.utc).isoformat(),
        )
        return updated or {}

    # ------------------------------------------------------ linguagem natural
    async def handle_natural(self, message: Any, content: str) -> str | None:
        """Cria, lista, cancela ou remarca. Devolve a resposta, ou None se não é lembrete."""
        if not looks_like_reminder_request(content):
            return None
        user_id = message.author.id
        plan = await self._interpret(content)
        operation = plan.get("operation", "create")
        if operation == "list":
            day = None
            when = plan.get("datetime_local") or ""
            if when:
                try:
                    day = datetime.strptime(when[:10], "%Y-%m-%d").replace(tzinfo=self.tz)
                except ValueError:
                    day = None
            items = await self.list_for(user_id, day=day)
            if not items:
                return "não tem nenhum lembrete marcado" + (" pra esse dia." if day else " por enquanto.")
            lines = [
                f"• {describe_when(datetime.fromisoformat(i['event_at_utc']).astimezone(self.tz))}: {i['description']}"
                for i in items[:15]
            ]
            return "seus lembretes:\n" + "\n".join(lines)
        if operation in {"cancel", "update"}:
            matches = await self.find(user_id, plan.get("target") or content)
            if not matches:
                return "não achei nenhum lembrete seu com isso."
            if len(matches) > 1:
                options = "; ".join(
                    f"{m['description']} ({describe_when(datetime.fromisoformat(m['event_at_utc']).astimezone(self.tz))})"
                    for m in matches[:4]
                )
                return f"achei mais de um: {options}. qual deles?"
            target = matches[0]
            if operation == "cancel":
                await self.cancel(target["id"], user_id)
                return f"cancelado: {target['description']}."
            new_time, missing = self._resolve_time(plan.get("datetime_local", ""), content, base=target)
            if new_time is None:
                return f"pra quando eu mudo? {missing}."
            try:
                await self.reschedule(target["id"], user_id, new_time)
            except ValueError as exc:
                return f"não deu pra remarcar: {exc}."
            return f"remarcado! {target['description']} agora é {describe_when(new_time)}. te aviso {int(self.lead.total_seconds() // 60)} minutinhos antes ♡"
        # create
        description = (plan.get("description") or "").strip()
        when, missing = self._resolve_time(plan.get("datetime_local", ""), content)
        if plan.get("needs_clarification"):
            return str(plan["needs_clarification"])[:300]
        if when is None:
            return f"pra eu agendar, me fala {missing.replace('faltou ', '')}?"
        if not description:
            return "do que é pra eu te lembrar?"
        try:
            await self.create(
                user_id=user_id,
                description=description,
                event_at=when,
                guild_id=getattr(getattr(message, "guild", None), "id", None),
                channel_id=getattr(getattr(message, "channel", None), "id", None),
                source_message_id=getattr(message, "id", None),
            )
        except ValueError as exc:
            return f"não consegui agendar: {exc}."
        lead = int(self.lead.total_seconds() // 60)
        return f"anotadinho! ♡ vou te lembrar de {description} no dia {describe_when(when)}. te aviso {lead} minutinhos antes!"

    def _resolve_time(self, ai_value: str, content: str, *, base: dict | None = None) -> tuple[datetime | None, str]:
        local, missing = parse_when(content, self.now())
        if ai_value:
            try:
                candidate = datetime.strptime(ai_value.strip()[:16], "%Y-%m-%d %H:%M").replace(tzinfo=self.tz)
                if local is None or abs((candidate - local).total_seconds()) > 60:
                    # A IA entende "próxima sexta" e afins; o parser local valida o resto.
                    local, missing = candidate, ""
            except ValueError:
                pass
        if local is not None and local <= self.now():
            return None, "um horário no futuro"
        return local, missing

    async def _interpret(self, content: str) -> dict[str, Any]:
        if self.ai_service is None:
            return {"operation": "create", "description": _guess_description(content)}
        now = self.now()
        weekdays = ("segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo")
        try:
            data = await self.ai_service.generate_json(
                f"AGORA: {weekdays[now.weekday()]}, {now.strftime('%Y-%m-%d %H:%M')} ({self.tz}).\n"
                f"MENSAGEM: {content}\n\n"
                "Entenda o pedido de lembrete. operation: create (criar), list (quais "
                "lembretes / o que tenho marcado), cancel (cancelar/apagar), update (mudar "
                "horário/adiar). description: do que lembrar, curto, sem a data (ex.: "
                "'reunião sobre o projeto'). datetime_local: AAAA-MM-DD HH:MM do evento no "
                "fuso indicado, calculado a partir de AGORA (vazio se não disse; em list, a "
                "data consultada). target: em cancel/update, palavras que identificam o "
                "lembrete. needs_clarification: pergunta curta se a data/hora for ambígua, "
                "senão vazio.",
                {
                    "type": "object",
                    "properties": {
                        "operation": {"type": "string"},
                        "description": {"type": "string"},
                        "datetime_local": {"type": "string"},
                        "target": {"type": "string"},
                        "needs_clarification": {"type": "string"},
                    },
                    "required": ["operation", "description", "datetime_local"],
                },
                max_output_tokens=400,
                timeout_seconds=15,
                use_cache=False,
            )
        except Exception:
            LOGGER.warning("IA indisponível para interpretar o lembrete; usando o parser local.")
            return {"operation": "create", "description": _guess_description(content)}
        operation = str(data.get("operation") or "create").casefold()
        return {
            "operation": operation if operation in {"create", "list", "cancel", "update"} else "create",
            "description": str(data.get("description") or "").strip(),
            "datetime_local": str(data.get("datetime_local") or "").strip(),
            "target": str(data.get("target") or "").strip(),
            "needs_clarification": str(data.get("needs_clarification") or "").strip(),
        }

    # ------------------------------------------------------------ scheduler
    async def worker(self) -> None:
        wait = getattr(self.bot, "wait_until_ready", None)
        if callable(wait):
            await wait()
        while not getattr(self.bot, "is_closed", lambda: False)():
            try:
                await self.tick()
            except Exception:
                LOGGER.exception("Falha no scheduler de lembretes.")
            await asyncio.sleep(self.poll_seconds)

    async def tick(self) -> int:
        now_utc = datetime.now(timezone.utc)
        claimed: list[dict[str, Any]] = []

        def claim(section: dict[str, Any]) -> None:
            for record in section.values():
                if not isinstance(record, dict):
                    continue
                status = record.get("status")
                if status == "PROCESSING":
                    claimed_at = datetime.fromisoformat(record.get("claimed_at") or record["updated_at"])
                    if now_utc - claimed_at > timedelta(minutes=10):
                        record["status"] = "PENDING"  # processo caiu no meio do envio
                        status = "PENDING"
                if status != "PENDING":
                    continue
                event_at = datetime.fromisoformat(record["event_at_utc"])
                if now_utc - event_at > self.grace:
                    record["status"] = "EXPIRED"
                    record["updated_at"] = now_utc.isoformat()
                    continue
                if datetime.fromisoformat(record["notify_at_utc"]) > now_utc:
                    continue
                retry_at = record.get("next_attempt_at")
                if retry_at and datetime.fromisoformat(retry_at) > now_utc:
                    continue
                record["status"] = "PROCESSING"
                record["claimed_at"] = now_utc.isoformat()
                record["attempts"] = int(record.get("attempts", 0)) + 1
                record["updated_at"] = now_utc.isoformat()
                claimed.append(dict(record))

        await self.state.mutate_section(SECTION, claim)
        for record in claimed:
            await self._deliver(record)
        return len(claimed)

    async def _compose(self, record: dict[str, Any]) -> tuple[str, str]:
        event_at = datetime.fromisoformat(record["event_at_utc"]).astimezone(self.tz)
        minutes = max(0, int((event_at - self.now()).total_seconds() // 60))
        hora = event_at.strftime("%Hh%M").replace("h00", "h")
        if self.ai_service is not None:
            try:
                text = await self.ai_service.generate_text(
                    "Escreva a mensagem privada da Bella lembrando a pessoa de um compromisso.\n"
                    f"Compromisso: {record['description']}\n"
                    f"Horário: {hora} (daqui a {minutes} minutos)\n"
                    "Uma ou duas frases, carinhosa e natural, em português do Brasil, pode "
                    "usar ♡ no máximo uma vez. Não invente detalhes além do compromisso.",
                    max_output_tokens=160,
                    timeout_seconds=12,
                    use_cache=False,
                )
                text = " ".join(str(text or "").split())
                if text and record["description"].split()[0].casefold()[:4] in text.casefold() or len(text) > 25:
                    return text[:600], "ia"
            except Exception as exc:  # noqa: BLE001 - qualquer falha usa a contingência
                LOGGER.warning("IA indisponível para a notificação %s: %s", record["id"], type(exc).__name__)
        template = random.choice(FALLBACK_TEXTS)
        return template.format(desc=record["description"], lead=minutes or 0, hora=hora), "contingencia"

    async def _deliver(self, record: dict[str, Any]) -> None:
        started = time.monotonic()
        text, origin = await self._compose(record)
        outcome = "sent"
        error = ""
        message_id = None
        via = "dm"
        try:
            user = self.bot.get_user(record["user_id"]) or await self.bot.fetch_user(record["user_id"])
            sent = await user.send(text, allowed_mentions=discord.AllowedMentions.none())
            message_id = getattr(sent, "id", None)
        except discord.Forbidden:
            # DM fechada: avisa no canal de origem, mencionando só a pessoa.
            channel = self.bot.get_channel(record.get("channel_id") or 0)
            if channel is not None:
                try:
                    sent = await channel.send(
                        f"<@{record['user_id']}> {text}",
                        allowed_mentions=discord.AllowedMentions(users=[discord.Object(record["user_id"])]),
                    )
                    message_id, via = getattr(sent, "id", None), "canal"
                except (discord.Forbidden, discord.HTTPException) as exc:
                    outcome, error = "failed", f"DM bloqueada e canal falhou: {type(exc).__name__}"
            else:
                outcome, error = "failed", "DM bloqueada"
        except discord.NotFound:
            outcome, error = "failed", "usuário inacessível"
        except (discord.HTTPException, asyncio.TimeoutError, OSError) as exc:
            outcome, error = "retry", f"{type(exc).__name__}: {exc}"[:200]
        if outcome == "sent":
            await self._set(
                record["id"], f"sent via {via}", status="SENT", sent_at=datetime.now(timezone.utc).isoformat(),
                message_id=message_id, text_origin=origin, delivered_via=via,
            )
        elif outcome == "retry" and record["attempts"] < self.max_attempts:
            delay = min(600, 30 * 2 ** (record["attempts"] - 1)) * random.uniform(0.8, 1.2)
            await self._set(
                record["id"], "retry", status="PENDING", last_error=error,
                next_attempt_at=(datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat(),
            )
        else:
            await self._set(record["id"], "failed", status="FAILED", last_error=error)
        entry = {
            "reminder_id": record["id"], "at": datetime.now(timezone.utc).isoformat(),
            "attempt": record["attempts"], "outcome": outcome, "via": via,
            "text_origin": origin, "error": error, "ms": int((time.monotonic() - started) * 1000),
        }

        def log(section: dict[str, Any]) -> None:
            values = list(section.get("log", []))
            values.append(entry)
            section["log"] = values[-1000:]

        await self.state.mutate_section(LOG_SECTION, log)
        LOGGER.info("lembrete %s: %s via %s (tentativa %s) %s", record["id"], outcome, via, record["attempts"], error)


def _guess_description(content: str) -> str:
    text = re.sub(r"<@!?\d+>", " ", content)
    text = re.sub(r"(?i)\b(bella|me lembr\w*|lembra(?:-me)?|que|de|por favor|pfv)\b", " ", text, count=3)
    text = re.sub(r"\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b|\b(?:às|as)\s*\d{1,2}(?:h\d{0,2}|:\d{2})?\b", " ", text)
    return " ".join(text.split()).strip(" ,.")[:200]
