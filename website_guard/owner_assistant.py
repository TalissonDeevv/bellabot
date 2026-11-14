"""Assistente pessoal do Talisson no canal privado dele.

No canal privado (OWNER_PRIVATE_CHANNEL_ID) a conversa é natural: o Talisson
não precisa mencionar a Bella. Este módulo cuida de:

- Agenda do dia ("tem algo pendente pra hoje?") direto dos dados, sem IA.
- Agenda falada: áudio bagunçado vira tarefas e lembretes com horário.
- Cofre de decisões: "anota: decidi não pegar projeto abaixo de R$500" e
  aviso quando algo vai contra uma regra dele.
- Resumo da semana (sexta à noite): feito, pendente, dinheiro e próxima semana.
- Conteúdo a partir do servidor: dúvidas mais repetidas da comunidade.
- Alerta de saída em massa e de canal parado.
- Ranking mensal da staff nos tickets.
- Clientes sumidos há 60 dias: rascunho de mensagem para ele aprovar.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import tempfile
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unicodedata import combining, normalize
from zoneinfo import ZoneInfo

import discord

LOGGER = logging.getLogger(__name__)

DECISIONS_SECTION = "owner_decisions"
QUESTIONS_SECTION = "owner_community_questions"
ACTIVITY_SECTION = "owner_client_activity"
META_SECTION = "owner_assistant_meta"
DRAFTS_SECTION = "owner_client_drafts"
LEAVES_SECTION = "owner_member_leaves"
IDEAS_SECTION = "owner_ideas"
BILLS_SECTION = "owner_bills"
LOG_SECTION = "owner_activity_log"
INBOX_SECTION = "agent_inbox"
MESSAGES_SECTION = "talisson_reminders"
MONTHS_PT = ("", "janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho",
             "agosto", "setembro", "outubro", "novembro", "dezembro")
TODO_SECTION = "agent_todos"
PAYMENT_SECTION = "agent_payment_links"
TICKET_SECTION = "smart_tickets"
REMINDER_SECTION = "bella_reminders"

DEFAULT_OWNER_CHANNEL_ID = 1570783779574780176
WEEKDAYS = ("segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo")

AGENDA_RE = re.compile(
    r"\b(?:pendente|pendencia|pendencias|o que (?:eu )?(?:tenho|preciso fazer)|minha agenda|"
    r"agenda (?:de )?(?:hoje|amanha)|compromissos?|tarefas? (?:de|pra|para) hoje|"
    r"tenho (?:algo|alguma coisa|coisa) (?:pra|para) (?:hoje|fazer|amanha))"
)
DECISION_SAVE_RE = re.compile(
    r"^(?:bella[,:]?\s*)?(?:anota|registra|guarda|salva)\w*[:,]?\s+(?:que\s+)?(?:eu\s+)?"
    r"(?:decidi|decisao|regra|a partir de agora|nao vou mais|nunca mais)"
)
DECISION_LIST_RE = re.compile(r"\b(?:minhas decisoes|minhas regras|decisoes que (?:eu )?(?:tomei|anotei)|cofre de decisoes|regras que (?:eu )?criei)\b")
DECISION_FORGET_RE = re.compile(r"\b(?:esquece|apaga|remove|revoga)\w* (?:a )?(?:decisao|regra)\b")
WEEKLY_RE = re.compile(r"\bresumo da semana\b|\bcomo foi (?:a|minha) semana\b")
CONTENT_RE = re.compile(r"\b(?:duvidas? (?:mais )?(?:repetidas?|frequentes?|comuns?)|ideias? de (?:conteudo|artigo|post)|o que (?:o pessoal|a galera) (?:mais )?pergunta)\b")
RANKING_RE = re.compile(r"\b(?:ranking|desempenho|avaliacao) da (?:staff|equipe|moderacao)\b")
CLIENTS_RE = re.compile(r"\bclientes? (?:sumidos?|inativos?|parados?|antigos?)\b")
APPROVE_RE = re.compile(r"^(?:pode mandar|manda(?: sim| ai| pra ele| pra ela)?|envia|pode enviar|aprovado|aprovo)\b")
REJECT_RE = re.compile(r"^(?:nao manda|descarta|deixa pra la|cancela (?:a )?mensagem|nao envia)\b")
TRAVEL_RE = re.compile(
    r"\b(?:vou (?:ficar fora|viajar|estar fora|sair de ferias|entrar de ferias|tirar ferias)|"
    r"(?:to|estou|estarei|vou estar|vou ficar) (?:de ferias|viajando|fora)|modo (?:viagem|ferias)|"
    r"ferias ate|viagem ate|fora ate)\b"
)
RETURN_RE = re.compile(r"^(?:bella[,:]?\s*)?(?:voltei|cheguei|to de volta|estou de volta|acabou (?:a viagem|as ferias)|sai do modo (?:viagem|ferias))\b")
IDEA_RE = re.compile(r"^(?:bella[,:]?\s*)?(?:ideia|tive uma ideia|anota (?:essa |uma |a )?ideia|nova ideia|ideia nova)\s*[:,\-]?\s*")
IDEA_LIST_RE = re.compile(r"\b(?:minhas ideias|caixa de ideias|lista de ideias|quais ideias)\b")
BILL_RE = re.compile(
    r"\b(?:vence|vencimento|pago|pagar|pagamento|cobra\w*|boleto|fatura|conta)\b.*\bdia (\d{1,2})\b"
    r"|\btodo (?:dia|mes no dia) (\d{1,2})\b"
)
MONTHLY_RE = re.compile(r"\b(?:todo mes|todos os meses|mensal\w*|de cada mes|todo dia \d{1,2}|todo mes no dia)\b")
BILL_LIST_RE = re.compile(r"\b(?:minhas contas|contas (?:a|pra|para) pagar|contas do mes|quais contas)\b")
BILL_PAID_RE = re.compile(r"^(?:bella[,:]?\s*)?(?:ja )?pag(?:uei|amos|o)\s+(?:a |o |as |os )?(?:conta (?:da |do |de )?)?(.+)$")
BILL_REMOVE_RE = re.compile(r"\b(?:remove|apaga|tira|cancela|exclui)\w* (?:a )?conta (?:da |do |de )?(.+)$")
RELAY_SECTION = "owner_relays"
RELAY_RE = re.compile(
    r"\b(?:manda|mande|mandar|envia|envie|enviar|passa|passe|deixa|deixe|da|de|dar)\w*\s+"
    r"(?:um |uma |o |a |esse |essa |meu |minha |)(?:recado|aviso|mensagem|msg|lembrete)\b"
    r"|\b(?:avisa|avise|avisar|fala|fale|falar|diz|diga|dizer|lembra|lembre|lembrar|comunica|comunique)\s+"
    r"(?:pro|pra|para|ao|a|o|à|pros|pras)\s+(?!(?:mim|eu|me|voce|vc|ti|nos|gente|que|quando|se)\b)(?:@|<@|[a-z])"
    r"|\bmanda\w*\s+(?:na|no)\s+(?:dm|pv|privado|direct)\b"
)
DM_RE = re.compile(r"\b(?:dm|pv|privado|privada|direct|inbox)\b")
CHANNEL_DELIVERY_RE = re.compile(r"(?:\b(?:no|na|em|pelo|pela|canal)\s*)<#(\d{15,22})>")
DOING_RE = re.compile(r"\b(?:o que (?:eu )?(?:tava|estava) fazendo|onde (?:eu )?parei|o que ficou (?:pela metade|pendente)|em que (?:eu )?parei)\b")
COMMIT_RE = re.compile(r"\b(?:aceit\w*|fech\w*|peg\w*|cobr\w*|faz\w*|vou fazer)\b")
MONEY_RE = re.compile(r"(?:r\$\s*\d|\d+\s*(?:reais|conto|mil)\b)")
QUESTION_START = re.compile(
    r"^(?:como|qual|quais|quanto|quando|onde|por que|porque|pq|alguem sabe|alguem ja|tem como|da pra|"
    r"e possivel|o que|vale a pena|preciso de ajuda)\b"
)


def _norm(value: str) -> str:
    decomposed = normalize("NFKD", str(value or "").casefold())
    return " ".join("".join(c for c in decomposed if not combining(c)).split())


def _iso(value: Any) -> datetime | None:
    try:
        moment = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _strip_lead(content: str) -> str:
    text = re.sub(r"<@!?\d+>", " ", content or "")
    text = re.sub(r"(?i)^\s*bella\s*[,:]?\s*", "", text.strip())
    return " ".join(text.split())


def _brl(value: float) -> str:
    text = f"{value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"R$ {text}"


class OwnerAssistant:
    def __init__(
        self,
        bot: Any,
        state: Any,
        *,
        owner_id: int,
        ai_service: Any | None = None,
        agent: Any | None = None,
        reminders: Any | None = None,
        channel_id: int | None = None,
        timezone_name: str = "America/Recife",
        availability: Any | None = None,
        refresh_presence: Any | None = None,
    ) -> None:
        self.availability = availability
        self.refresh_presence = refresh_presence
        self.pause_hours = float(os.getenv("OWNER_PAUSE_HOURS", "3") or 3)
        self.bot = bot
        self.state = state
        self.owner_id = int(owner_id)
        self.ai = ai_service
        self.agent = agent
        self.reminders = reminders
        self.channel_id = int(channel_id or os.getenv("OWNER_PRIVATE_CHANNEL_ID", "") or DEFAULT_OWNER_CHANNEL_ID)
        self.tz = ZoneInfo(timezone_name)
        self.leave_threshold = int(os.getenv("OWNER_LEAVE_ALERT_COUNT", "5") or 5)
        self.leave_window = timedelta(minutes=int(os.getenv("OWNER_LEAVE_ALERT_MINUTES", "30") or 30))
        self.client_idle_days = int(os.getenv("OWNER_CLIENT_IDLE_DAYS", "60") or 60)
        self.quiet_channel_days = int(os.getenv("OWNER_QUIET_CHANNEL_DAYS", "7") or 7)
        self._audio_tasks: set[asyncio.Task] = set()

    def now(self) -> datetime:
        return datetime.now(self.tz)

    # -------------------------------------------------------------- canal
    def is_owner_channel(self, message: Any) -> bool:
        return bool(
            getattr(message.author, "id", 0) == self.owner_id
            and getattr(message.channel, "id", 0) == self.channel_id
        )

    def channel(self) -> Any:
        return self.bot.get_channel(self.channel_id)

    async def say(self, text: str, **kwargs: Any) -> None:
        channel = self.channel()
        if channel is None:
            LOGGER.warning("Canal privado do dono (%s) não encontrado.", self.channel_id)
            return
        for start in range(0, len(text), 1900):
            await channel.send(text[start:start + 1900], allowed_mentions=discord.AllowedMentions.none(), **kwargs)

    # --------------------------------------------------------- mensagens
    async def handle(self, message: Any, content: str) -> str | None:
        """Resposta para o dono, ou None para seguir o fluxo normal da Bella."""
        if getattr(message.author, "id", 0) != self.owner_id:
            return None
        in_channel = self.is_owner_channel(message)
        text = _norm(re.sub(r"<@!?\d+>", " ", content)).strip(" ?!.")
        welcome = await self._track_activity(content, in_channel=in_channel)
        if DOING_RE.search(text):
            return await self.what_was_i_doing()
        if welcome:
            await self.say(welcome)
        if RELAY_RE.search(text) and getattr(message, "guild", None) is not None:
            relay = await self.send_relay(message, content)
            if relay:
                return relay
        if in_channel and self._audio(message) is not None:
            return await self.start_spoken_agenda(message)
        if RETURN_RE.search(text) and await self._travel():
            return await self.end_travel()
        if TRAVEL_RE.search(text):
            return await self.start_travel(content, message)
        if MONTHLY_RE.search(text) and BILL_RE.search(text):
            return await self.save_bill(content)
        if BILL_LIST_RE.search(text):
            return await self.bills_text()
        removed = BILL_REMOVE_RE.search(text)
        if removed:
            return await self.remove_bill(removed.group(1))
        paid = BILL_PAID_RE.search(text)
        if paid:
            answer = await self.mark_bill_paid(paid.group(1))
            if answer:
                return answer
        if AGENDA_RE.search(text):
            return await self.agenda_text(tomorrow="amanha" in text)
        if not in_channel:
            return None
        if IDEA_RE.search(text) and len(text) > 12:
            return await self.save_idea(content)
        if IDEA_LIST_RE.search(text):
            return await self.ideas_text()
        review = await self._idea_review_answer(text)
        if review:
            return review
        drafts = await self.state.get_section(DRAFTS_SECTION)
        open_drafts = [d for d in drafts.values() if isinstance(d, dict) and d.get("status") == "waiting"]
        if open_drafts and APPROVE_RE.search(text):
            return await self.send_draft(open_drafts[-1])
        if open_drafts and REJECT_RE.search(text):
            await self._set_draft(open_drafts[-1]["id"], status="discarded")
            return "ok, não mando."
        if DECISION_SAVE_RE.search(text):
            return await self.save_decision(content)
        if DECISION_LIST_RE.search(text):
            return await self.decisions_text()
        if DECISION_FORGET_RE.search(text):
            return await self.forget_decision(content)
        if WEEKLY_RE.search(text):
            return await self.weekly_summary()
        if CONTENT_RE.search(text):
            return await self.content_ideas()
        if RANKING_RE.search(text):
            return await self.staff_ranking()
        if CLIENTS_RE.search(text):
            listing = await self.idle_clients_text()
            if await self.propose_client_drafts():
                listing += "\n\nescrevi umas mensagens aí em cima pra você aprovar."
            return listing
        if COMMIT_RE.search(text) and MONEY_RE.search(text):
            warning = await self.check_decisions(content)
            if warning:
                return warning
        return None

    # ------------------------------------------------------------ agenda
    async def agenda_text(self, *, tomorrow: bool = False) -> str:
        day = (self.now() + timedelta(days=1 if tomorrow else 0)).date()
        label = "amanhã" if tomorrow else "hoje"
        todos = [t for t in await self._todos() if not t.get("done")]
        reminders = await self._reminders_on(day)
        lines: list[str] = []
        if reminders:
            lines.append(f"**compromissos de {label}:**")
            lines += [f"• {r['hora']} — {r['description']}" for r in reminders]
        if todos:
            lines.append("**tarefas pendentes:**")
            lines += [f"• {t['text']}" for t in todos[:20]]
            if len(todos) > 20:
                lines.append(f"… e mais {len(todos) - 20}.")
        if not lines:
            return f"nada pendente pra {label}: sem compromissos marcados e sem tarefas abertas. ♡"
        return "\n".join(lines)

    async def _todos(self) -> list[dict[str, Any]]:
        section = await self.state.get_section(TODO_SECTION)
        return list(section.get(str(self.owner_id), []))

    async def _reminders_on(self, day) -> list[dict[str, Any]]:
        result = []
        for value in (await self.state.get_section(REMINDER_SECTION)).values():
            if not isinstance(value, dict) or int(value.get("user_id", 0)) != self.owner_id:
                continue
            if value.get("status") not in {"PENDING", "PROCESSING"}:
                continue
            event = _iso(value.get("event_at_utc"))
            if event is None:
                continue
            local = event.astimezone(self.tz)
            if local.date() == day:
                result.append({"hora": local.strftime("%Hh%M").replace("h00", "h"), "description": value.get("description", ""), "at": local})
        return sorted(result, key=lambda item: item["at"])

    # ----------------------------------------------------- agenda falada
    @staticmethod
    def _audio(message: Any) -> Any:
        for attachment in getattr(message, "attachments", []) or []:
            kind = (getattr(attachment, "content_type", "") or "").split(";")[0]
            name = getattr(attachment, "filename", "").casefold()
            if kind.startswith("audio/") or name.endswith((".ogg", ".mp3", ".m4a", ".wav", ".opus", ".webm")):
                return attachment
        return None

    async def start_spoken_agenda(self, message: Any) -> str:
        attachment = self._audio(message)
        task = asyncio.create_task(self._spoken_agenda_job(attachment))
        self._audio_tasks.add(task)
        task.add_done_callback(self._audio_tasks.discard)
        return "recebi o áudio, tô ouvindo e já separo em tarefas e lembretes. ♡"

    async def _spoken_agenda_job(self, attachment: Any) -> None:
        from src.ai.audio_transcriber import TranscriptionError, transcribe_audio

        try:
            with tempfile.TemporaryDirectory(prefix="bella-agenda-") as folder:
                workdir = Path(folder)
                source = workdir / ("audio" + (Path(attachment.filename).suffix or ".ogg"))
                await attachment.save(source)
                transcript, _ = await transcribe_audio(source, workdir)
        except TranscriptionError as exc:
            await self.say(f"não consegui ouvir o áudio: {exc}")
            return
        except Exception as exc:  # noqa: BLE001
            LOGGER.exception("Falha na agenda falada.")
            await self.say(f"deu erro ao ouvir o áudio ({type(exc).__name__}). manda de novo daqui a pouquinho?")
            return
        await self.say(await self.organize_agenda(transcript))

    async def organize_agenda(self, transcript: str) -> str:
        """Texto (transcrição) -> tarefas e lembretes salvos. Devolve o relato."""
        if self.ai is None:
            return f"ouvi isso, mas tô sem IA pra organizar agora:\n> {transcript[:1500]}"
        now = self.now()
        try:
            data = await self.ai.generate_json(
                f"AGORA: {WEEKDAYS[now.weekday()]}, {now.strftime('%Y-%m-%d %H:%M')} ({self.tz}).\n"
                f"O Talisson falou isso num áudio:\n{transcript[:6000]}\n\n"
                "Separe em itens. Para cada item: text (curto, no infinitivo, ex.: 'ligar pro "
                "Fulano'), datetime_local (AAAA-MM-DD HH:MM se ele disse dia E hora; vazio se não "
                "disse hora) e day (AAAA-MM-DD se disse só o dia, senão vazio). Não invente horários.",
                {
                    "type": "object",
                    "properties": {
                        "items": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "text": {"type": "string"},
                                    "datetime_local": {"type": "string"},
                                    "day": {"type": "string"},
                                },
                                "required": ["text"],
                            },
                        }
                    },
                    "required": ["items"],
                },
                max_output_tokens=900,
                timeout_seconds=25,
                use_cache=False,
            )
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("IA indisponível na agenda falada: %s", type(exc).__name__)
            await self._add_todo(f"(áudio) {transcript[:280]}")
            return "ouvi, mas a IA não respondeu pra separar os itens. guardei o áudio inteiro como uma tarefa pra você não perder."
        reminders, tasks = [], []
        for item in data.get("items", [])[:30]:
            text = " ".join(str(item.get("text", "")).split())[:200]
            if not text:
                continue
            when = None
            raw = str(item.get("datetime_local") or "").strip()
            if raw:
                try:
                    when = datetime.strptime(raw[:16], "%Y-%m-%d %H:%M").replace(tzinfo=self.tz)
                except ValueError:
                    when = None
            if when is not None and when > now and self.reminders is not None:
                try:
                    await self.reminders.create(
                        user_id=self.owner_id, description=text, event_at=when, channel_id=self.channel_id,
                    )
                    reminders.append(f"• {when.strftime('%d/%m %Hh%M').replace('h00', 'h')}: {text}")
                    continue
                except ValueError:
                    pass
            day = str(item.get("day") or "").strip()
            label = f"{text} (dia {day[8:10]}/{day[5:7]})" if len(day) >= 10 else text
            await self._add_todo(label)
            tasks.append(f"• {label}")
        if not reminders and not tasks:
            return "ouvi o áudio, mas não achei nenhuma tarefa nele."
        parts = []
        if reminders:
            parts.append("**lembretes (te aviso 5 min antes):**\n" + "\n".join(reminders))
        if tasks:
            parts.append("**tarefas sem horário:**\n" + "\n".join(tasks))
        return "\n\n".join(parts) + "\n\nse algo ficou errado, me fala que eu ajusto."

    async def _add_todo(self, text: str) -> None:
        def mutate(section: dict[str, Any]) -> None:
            todos = list(section.get(str(self.owner_id), []))
            todos.append({"text": text, "done": False, "created_at": self.now().isoformat(), "origin": "agenda_falada"})
            section[str(self.owner_id)] = todos[-200:]

        await self.state.mutate_section(TODO_SECTION, mutate)

    # -------------------------------------------------- cofre de decisões
    async def save_decision(self, content: str) -> str:
        text = re.sub(r"(?i)^\s*(?:<@!?\d+>\s*)?(?:bella[,:]?\s*)?(?:anota|registra|guarda|salva)\w*[:,]?\s*", "", content).strip()
        text = text[:500]
        if len(text) < 8:
            return "qual decisão eu anoto?"
        decision_id = f"d{int(time.time() * 1000)}"
        await self.state.update_section(
            DECISIONS_SECTION,
            decision_id,
            {"id": decision_id, "text": text, "created_at": self.now().isoformat(), "active": True},
        )
        return f"anotado no cofre: \"{text}\". se você for contra isso, eu te lembro. ♡"

    async def _decisions(self) -> list[dict[str, Any]]:
        values = (await self.state.get_section(DECISIONS_SECTION)).values()
        return sorted((v for v in values if isinstance(v, dict) and v.get("active")), key=lambda v: v.get("created_at", ""))

    async def decisions_text(self) -> str:
        decisions = await self._decisions()
        if not decisions:
            return "o cofre tá vazio. é só falar \"anota: decidi ...\"."
        return "**suas decisões:**\n" + "\n".join(
            f"{i}. {d['text']} ({d['created_at'][:10]})" for i, d in enumerate(decisions, start=1)
        )

    async def forget_decision(self, content: str) -> str:
        decisions = await self._decisions()
        words = {w for w in re.findall(r"[a-z0-9$]+", _norm(content)) if len(w) >= 4} - {"esquece", "apaga", "remove", "decisao", "regra", "aquela"}
        scored = sorted(
            ((sum(1 for w in words if w in _norm(d["text"])), d) for d in decisions),
            key=lambda item: -item[0],
        )
        if not scored or scored[0][0] == 0:
            return "não achei essa decisão no cofre."
        if len(scored) > 1 and scored[1][0] == scored[0][0]:
            return "achei mais de uma: " + "; ".join(f"\"{d['text']}\"" for s, d in scored[:3] if s == scored[0][0]) + ". qual delas?"
        target = scored[0][1]
        target["active"] = False
        await self.state.update_section(DECISIONS_SECTION, target["id"], target)
        return f"tirei do cofre: \"{target['text']}\"."

    async def check_decisions(self, situation: str) -> str:
        """Aviso se a situação vai contra alguma decisão do cofre ('' se não vai)."""
        decisions = await self._decisions()
        if not decisions:
            return ""
        local = self._local_money_conflict(situation, decisions)
        if local:
            return local
        if self.ai is None:
            return ""
        try:
            data = await self.ai.generate_json(
                "Decisões que o Talisson tomou:\n"
                + "\n".join(f"- {d['text']}" for d in decisions[-20:])
                + f"\n\nSituação agora: {situation[:1500]}\n\n"
                "A situação contraria alguma decisão? conflict true só se contrariar claramente. "
                "decision: o texto da decisão contrariada.",
                {"type": "object", "properties": {"conflict": {"type": "boolean"}, "decision": {"type": "string"}}, "required": ["conflict"]},
                max_output_tokens=200,
                timeout_seconds=12,
                use_cache=False,
            )
        except Exception:  # noqa: BLE001
            return ""
        if data.get("conflict") and data.get("decision"):
            return f"ei, lembra da sua regra? \"{data['decision']}\". quer seguir mesmo assim?"
        return ""

    @staticmethod
    def _local_money_conflict(situation: str, decisions: list[dict[str, Any]]) -> str:
        values = [float(v.replace(".", "").replace(",", ".")) for v in re.findall(r"r\$\s*([\d.,]+)", _norm(situation))]
        if not values:
            return ""
        amount = max(values)
        for decision in decisions:
            norm = _norm(decision["text"])
            limit = re.search(r"(?:abaixo|menos) de r\$\s*([\d.,]+)", norm)
            if limit:
                minimum = float(limit.group(1).replace(".", "").replace(",", "."))
                if amount < minimum:
                    return f"ei, lembra da sua regra? \"{decision['text']}\". esse é de {_brl(amount)}. quer seguir mesmo assim?"
        return ""

    # ------------------------------------------- observação do servidor
    async def observe_message(self, message: Any) -> None:
        if getattr(message, "guild", None) is None or getattr(message.author, "bot", False):
            return
        if message.author.id == self.owner_id:
            return
        now_iso = datetime.now(timezone.utc).isoformat()
        await self.state.update_section(ACTIVITY_SECTION, str(message.author.id), {"last_seen": now_iso, "name": message.author.display_name})
        text = " ".join((message.content or "").split())
        norm = _norm(text)
        if 15 <= len(text) <= 400 and ("?" in text or QUESTION_START.search(norm)):
            entry = {"at": now_iso, "user_id": message.author.id, "channel_id": message.channel.id, "text": text}

            def mutate(section: dict[str, Any]) -> None:
                values = list(section.get("items", []))
                values.append(entry)
                cutoff = (datetime.now(timezone.utc) - timedelta(days=45)).isoformat()
                section["items"] = [v for v in values if v.get("at", "") >= cutoff][-1500:]

            await self.state.mutate_section(QUESTIONS_SECTION, mutate)

    async def content_ideas(self, *, days: int = 30) -> str:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        items = [i for i in (await self.state.get_section(QUESTIONS_SECTION)).get("items", []) if i.get("at", "") >= cutoff]
        if len(items) < 3:
            return "ainda não tenho dúvidas suficientes da comunidade pra sugerir conteúdo."
        if self.ai is not None:
            try:
                data = await self.ai.generate_json(
                    f"Perguntas feitas pela comunidade do Discord nos últimos {days} dias:\n"
                    + "\n".join(f"- (u{i['user_id']}) {i['text'][:200]}" for i in items[-300:])
                    + "\n\nAgrupe por assunto. Para os assuntos com mais pessoas diferentes, diga: topic, "
                    "people (quantas pessoas diferentes perguntaram), example (uma pergunta real) e idea "
                    "(título de artigo pro site do Talisson). No máximo 5, do maior pro menor.",
                    {"type": "object", "properties": {"topics": {"type": "array", "items": {"type": "object", "properties": {
                        "topic": {"type": "string"}, "people": {"type": "integer"}, "example": {"type": "string"}, "idea": {"type": "string"}},
                        "required": ["topic", "people", "idea"]}}}, "required": ["topics"]},
                    max_output_tokens=900,
                    timeout_seconds=30,
                    use_cache=False,
                )
                topics = [t for t in data.get("topics", []) if int(t.get("people") or 0) >= 2]
                if topics:
                    lines = [f"**dúvidas que mais apareceram ({days} dias):**"]
                    for topic in topics[:5]:
                        lines.append(
                            f"• **{topic['topic']}**: {topic['people']} pessoas perguntaram"
                            + (f" (ex.: \"{str(topic.get('example', ''))[:120]}\")" if topic.get("example") else "")
                            + f"\n  dá pra virar artigo: _{topic['idea']}_"
                        )
                    return "\n".join(lines)
            except Exception:  # noqa: BLE001
                LOGGER.warning("IA indisponível para agrupar dúvidas; usando contagem simples.")
        stop = {"como", "qual", "quais", "quanto", "para", "pra", "voce", "alguem", "sabe", "fazer", "isso", "esse", "essa", "tem", "que", "uma", "um", "com", "por", "mais"}
        people: dict[str, set[int]] = {}
        for item in items:
            for word in set(re.findall(r"[a-z]{4,}", _norm(item["text"]))) - stop:
                people.setdefault(word, set()).add(int(item["user_id"]))
        top = sorted(people.items(), key=lambda kv: -len(kv[1]))[:5]
        return "**assuntos mais perguntados:**\n" + "\n".join(f"• {word}: {len(users)} pessoas" for word, users in top)

    # ----------------------------------------------------- saídas/canais
    async def member_left(self, member: Any) -> None:
        if getattr(member, "bot", False):
            return
        now = datetime.now(timezone.utc)
        recent: list[dict[str, Any]] = []

        def mutate(section: dict[str, Any]) -> None:
            values = [v for v in section.get("items", []) if _iso(v.get("at")) and now - _iso(v["at"]) < timedelta(days=2)]
            values.append({"at": now.isoformat(), "user_id": member.id, "name": getattr(member, "display_name", ""), "guild_id": member.guild.id})
            section["items"] = values[-500:]
            recent.extend(v for v in values if now - _iso(v["at"]) <= self.leave_window)

        await self.state.mutate_section(LEAVES_SECTION, mutate)
        if len(recent) < self.leave_threshold:
            return
        meta = await self.state.get_section(META_SECTION)
        last = _iso(meta.get("last_leave_alert"))
        if last and now - last < timedelta(hours=2):
            return
        await self.state.update_section(META_SECTION, "last_leave_alert", now.isoformat())
        cause = await self._probable_leave_cause(member.guild, now - self.leave_window - timedelta(hours=2))
        names = ", ".join(v["name"] or str(v["user_id"]) for v in recent[-10:])
        await self.say(
            f"⚠️ **{len(recent)} pessoas saíram** do servidor nos últimos {int(self.leave_window.total_seconds() // 60)} minutos ({names}).\n{cause}"
        )

    async def _probable_leave_cause(self, guild: Any, since: datetime) -> str:
        candidates = []
        for channel in getattr(guild, "text_channels", []):
            last_id = getattr(channel, "last_message_id", None)
            if not last_id:
                continue
            created = discord.utils.snowflake_time(last_id)
            name = _norm(channel.name)
            if created >= since and (getattr(channel, "is_news", lambda: False)() or re.search(r"anunc|aviso|news|novidade|comunicad", name)):
                candidates.append(channel)
        if not candidates:
            return "não vi nenhum anúncio recente; pode ser raid de contas ou limpeza de inativos."
        lines = []
        for channel in candidates[:3]:
            try:
                last = [m async for m in channel.history(limit=1)]
            except (discord.Forbidden, discord.HTTPException):
                continue
            if last:
                lines.append(f"• {channel.mention}: \"{(last[0].content or '(sem texto)')[:200]}\"")
        if not lines:
            return ""
        return "o que saiu logo antes, pode ter relação:\n" + "\n".join(lines)

    async def check_quiet_channels(self, guild: Any) -> None:
        now = datetime.now(timezone.utc)
        meta = await self.state.get_section(META_SECTION)
        alerted = dict(meta.get("quiet_alerts", {}))
        quiet = []
        default_role = getattr(guild, "default_role", None)
        for channel in getattr(guild, "text_channels", []):
            perms = channel.permissions_for(default_role) if default_role is not None else None
            if perms is None or not perms.view_channel or not perms.send_messages:
                continue  # canal de leitura (regras, anúncios) ou privado
            last_id = getattr(channel, "last_message_id", None)
            if not last_id:
                continue
            idle = now - discord.utils.snowflake_time(last_id)
            if timedelta(days=self.quiet_channel_days) <= idle <= timedelta(days=60):
                previous = _iso(alerted.get(str(channel.id)))
                if previous is None or now - previous > timedelta(days=30):
                    quiet.append((channel, idle.days))
                    alerted[str(channel.id)] = now.isoformat()
        if not quiet:
            return
        await self.state.update_section(META_SECTION, "quiet_alerts", alerted)
        lines = [f"• {c.mention}: {d} dias sem mensagem" for c, d in sorted(quiet, key=lambda x: -x[1])[:10]]
        await self.say(
            "📉 **canais parados:**\n" + "\n".join(lines)
            + "\nsugestão: puxar um assunto, juntar com outro canal ou arquivar."
        )

    # ------------------------------------------------------ staff/tickets
    async def staff_ranking(self, *, days: int = 30) -> str:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        stats: dict[int, dict[str, Any]] = {}
        for record in (await self.state.get_section(TICKET_SECTION)).values():
            if not isinstance(record, dict):
                continue
            created = _iso(record.get("created_at"))
            if created is None or created < cutoff:
                continue
            responder = int(record.get("human_first_responder") or record.get("assigned_to") or 0)
            if responder and responder != int(record.get("creator_id", 0)):
                item = stats.setdefault(responder, {"tickets": 0, "seconds": [], "closed": 0})
                item["tickets"] += 1
                first = _iso(record.get("human_first_response_at"))
                if first:
                    item["seconds"].append((first - created).total_seconds())
            closer = int(record.get("closed_by") or 0)
            if closer and closer != int(record.get("creator_id", 0)):
                stats.setdefault(closer, {"tickets": 0, "seconds": [], "closed": 0})["closed"] += 1
        bot_id = getattr(getattr(self.bot, "user", None), "id", None)
        stats.pop(bot_id or -1, None)
        if not stats:
            return f"nenhum ticket atendido pela staff nos últimos {days} dias."

        def avg(values: list[float]) -> float | None:
            return sum(values) / len(values) if values else None

        ranking = sorted(stats.items(), key=lambda kv: (-(kv[1]["tickets"] + kv[1]["closed"]), avg(kv[1]["seconds"]) or 1e12))
        lines = [f"**ranking da staff ({days} dias):**"]
        for position, (user_id, item) in enumerate(ranking[:15], start=1):
            mean = avg(item["seconds"])
            tempo = "sem medição" if mean is None else (f"{mean / 60:.0f} min" if mean < 3600 else f"{mean / 3600:.1f} h")
            lines.append(f"{position}. <@{user_id}>: {item['tickets']} atendidos, {item['closed']} resolvidos, 1ª resposta média {tempo}")
        return "\n".join(lines)

    # ---------------------------------------------------- clientes sumidos
    async def clients(self) -> dict[int, dict[str, Any]]:
        result: dict[int, dict[str, Any]] = {}
        for link in (await self.state.get_section(PAYMENT_SECTION)).values():
            if isinstance(link, dict) and link.get("customer_id"):
                uid = int(link["customer_id"])
                entry = result.setdefault(uid, {"projects": [], "since": link.get("created_at", "")})
                entry["projects"].append(f"{link.get('service', '')} ({link.get('amount', '')})")
        for record in (await self.state.get_section(TICKET_SECTION)).values():
            if isinstance(record, dict) and record.get("quote_summary") and record.get("creator_id"):
                uid = int(record["creator_id"])
                entry = result.setdefault(uid, {"projects": [], "since": record.get("created_at", "")})
                entry["projects"].append(str(record["quote_summary"].get("titulo", "orçamento")))
                entry["name"] = record.get("creator_name", "")
        return result

    async def idle_clients(self) -> list[tuple[int, dict[str, Any], int]]:
        activity = await self.state.get_section(ACTIVITY_SECTION)
        now = datetime.now(timezone.utc)
        idle = []
        for uid, data in (await self.clients()).items():
            seen = _iso((activity.get(str(uid)) or {}).get("last_seen")) or _iso(data.get("since"))
            if seen is None:
                continue
            days = (now - seen).days
            if days >= self.client_idle_days:
                data["name"] = data.get("name") or (activity.get(str(uid)) or {}).get("name", "")
                idle.append((uid, data, days))
        return sorted(idle, key=lambda item: -item[2])

    async def idle_clients_text(self) -> str:
        idle = await self.idle_clients()
        if not idle:
            return f"nenhum cliente sumido há mais de {self.client_idle_days} dias."
        return "**clientes sumidos:**\n" + "\n".join(
            f"• <@{uid}>: {days} dias sem aparecer, projeto: {', '.join(d['projects'][:2])}" for uid, d, days in idle[:15]
        )

    async def propose_client_drafts(self) -> int:
        drafts = await self.state.get_section(DRAFTS_SECTION)
        now = datetime.now(timezone.utc)
        recent = {int(d["user_id"]) for d in drafts.values() if isinstance(d, dict) and _iso(d.get("created_at")) and now - _iso(d["created_at"]) < timedelta(days=self.client_idle_days)}
        proposed = 0
        for uid, data, days in await self.idle_clients():
            if uid in recent or proposed >= 2:
                continue
            text = await self._client_message(data)
            draft_id = f"c{uid}-{int(time.time())}"
            await self.state.update_section(DRAFTS_SECTION, draft_id, {
                "id": draft_id, "user_id": uid, "text": text, "status": "waiting", "created_at": now.isoformat(),
            })
            await self.say(
                f"💬 a/o <@{uid}> não aparece há **{days} dias** (projeto: {', '.join(data['projects'][:2])}). "
                f"pensei em mandar isso no privado:\n> {text}\n\nposso mandar? (responde \"manda\" ou \"não manda\")"
            )
            proposed += 1
        return proposed

    async def _client_message(self, data: dict[str, Any]) -> str:
        project = ", ".join(data["projects"][:2]) or "o projeto"
        if self.ai is not None:
            try:
                text = await self.ai.generate_text(
                    f"Escreva uma mensagem curta de DM do Talisson (desenvolvedor) para um cliente antigo "
                    f"que sumiu. Projeto que fizeram juntos: {project}. Tom leve e pessoal, perguntando "
                    "como está o projeto e se precisa de algo, sem vender agressivo. Uma ou duas frases.",
                    max_output_tokens=150, timeout_seconds=12, use_cache=False,
                )
                text = " ".join(str(text or "").split())
                if len(text) > 20:
                    return text[:500]
            except Exception:  # noqa: BLE001
                pass
        return f"e aí, tudo certo? passando pra saber como tá {project}. se precisar de algum ajuste ou ideia nova, é só me chamar!"

    async def send_draft(self, draft: dict[str, Any]) -> str:
        try:
            user = self.bot.get_user(int(draft["user_id"])) or await self.bot.fetch_user(int(draft["user_id"]))
            await user.send(draft["text"], allowed_mentions=discord.AllowedMentions.none())
        except (discord.Forbidden, discord.NotFound, discord.HTTPException) as exc:
            await self._set_draft(draft["id"], status="failed", error=type(exc).__name__)
            return f"não consegui mandar pra <@{draft['user_id']}> (DM fechada ou conta inacessível)."
        await self._set_draft(draft["id"], status="sent", sent_at=datetime.now(timezone.utc).isoformat())
        return f"mandei pra <@{draft['user_id']}>. ♡"

    async def _set_draft(self, draft_id: str, **fields: Any) -> None:
        def mutate(section: dict[str, Any]) -> None:
            if isinstance(section.get(draft_id), dict):
                section[draft_id].update(fields)

        await self.state.mutate_section(DRAFTS_SECTION, mutate)

    # ------------------------------------------------------ resumo semanal
    async def _revenue(self, since: datetime) -> str:
        stripe = getattr(self.agent, "stripe", None)
        if stripe is not None and getattr(stripe, "configured", False):
            try:
                payload = await stripe._request(
                    "GET", "/charges", params={"created[gte]": str(int(since.timestamp())), "limit": "100"},
                )
                total = sum(c.get("amount", 0) for c in payload.get("data", []) if c.get("paid") and not c.get("refunded"))
                count = sum(1 for c in payload.get("data", []) if c.get("paid") and not c.get("refunded"))
                return f"{_brl(total / 100)} em {count} pagamento(s) no Stripe" + (" (modo teste)" if stripe.test_mode else "")
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("Stripe indisponível no resumo: %s", exc)
        links = [l for l in (await self.state.get_section(PAYMENT_SECTION)).values()
                 if isinstance(l, dict) and (_iso(l.get("created_at")) or since) >= since]
        if not links:
            return "nenhum pagamento registrado (Stripe não configurado)" if not getattr(stripe, "configured", False) else "nenhum pagamento"
        total = sum(float(l.get("amount") or 0) for l in links)
        return f"{len(links)} link(s) de pagamento criados, somando {_brl(total)} (sem confirmação de pagamento)"

    async def weekly_summary(self) -> str:
        now = self.now()
        start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        next_week_end = start + timedelta(days=14)
        todos = await self._todos()
        done = [t for t in todos if t.get("done") and (_iso(t.get("done_at")) or datetime.min.replace(tzinfo=timezone.utc)) >= start]
        pending = [t for t in todos if not t.get("done")]
        reminders = [r for r in (await self.state.get_section(REMINDER_SECTION)).values()
                     if isinstance(r, dict) and int(r.get("user_id", 0)) == self.owner_id]
        upcoming = sorted(
            (r for r in reminders if r.get("status") == "PENDING" and (_iso(r.get("event_at_utc")) or start) >= now
             and _iso(r.get("event_at_utc")) <= next_week_end),
            key=lambda r: r["event_at_utc"],
        )
        tickets = [t for t in (await self.state.get_section(TICKET_SECTION)).values() if isinstance(t, dict)]
        closed = sum(1 for t in tickets if (_iso(t.get("closed_at")) or start - timedelta(days=1)) >= start)
        quotes = [t for t in tickets if t.get("quote_summary") and (_iso(t.get("created_at")) or start - timedelta(days=1)) >= start]
        lines = [f"**resumo da semana ({start.strftime('%d/%m')} a {now.strftime('%d/%m')})** ♡", ""]
        lines.append("**feito:**")
        lines += [f"• {t['text']}" for t in done[:15]] or ["• nenhuma tarefa marcada como feita"]
        lines.append(f"• {closed} ticket(s) encerrado(s)")
        if quotes:
            lines.append(f"• {len(quotes)} pedido(s) de orçamento: " + "; ".join(str(q['quote_summary'].get('titulo', ''))[:60] for q in quotes[:5]))
        lines += ["", f"**dinheiro:** {await self._revenue(start.astimezone(timezone.utc))}", ""]
        lines.append(f"**pendente ({len(pending)}):**")
        lines += [f"• {t['text']}" for t in pending[:10]] or ["• nada"]
        lines += ["", "**próximos dias:**"]
        lines += [
            f"• {_iso(r['event_at_utc']).astimezone(self.tz).strftime('%a %d/%m %Hh%M')}: {r['description']}"
            for r in upcoming[:10]
        ] or ["• nenhum compromisso marcado"]
        ideas = await self.content_ideas(days=7)
        if not ideas.startswith("ainda não"):
            lines += ["", ideas]
        return "\n".join(lines)

    # ------------------------------------------------------------- recados
    def _relay_targets(self, message: Any, content: str) -> tuple[list[Any], str]:
        """Pessoas que vão receber o recado. Devolve (membros, pergunta_se_ambíguo)."""
        bot_id = getattr(getattr(self.bot, "user", None), "id", None)
        targets = [
            m for m in getattr(message, "mentions", []) or []
            if getattr(m, "id", None) not in {bot_id, self.owner_id} and not getattr(m, "bot", False)
        ]
        if targets:
            return list({m.id: m for m in targets}.values()), ""
        guild = getattr(message, "guild", None)
        name_match = re.search(
            r"\b(?:pro|pra|para o|para a|para|ao|à)\s+([\wÀ-ÿ.\- ]{2,40}?)(?=\s+(?:de que|que|dizendo|falando|avisando|sobre|na dm|no pv|no privado)\b|[,.:]|$)",
            _strip_lead(content), re.IGNORECASE,
        )
        if guild is None or not name_match:
            return [], "pra quem eu mando? menciona a pessoa (@fulano)."
        wanted = _norm(name_match.group(1)).strip()
        members = [m for m in getattr(guild, "members", []) if not getattr(m, "bot", False)]
        exact = [m for m in members if wanted in {_norm(m.display_name), _norm(getattr(m, "name", ""))}]
        partial = exact or [m for m in members if wanted and wanted in _norm(m.display_name)]
        if len(partial) == 1:
            return partial, ""
        if len(partial) > 1:
            names = ", ".join(m.display_name for m in partial[:5])
            return [], f"achei mais de uma pessoa com esse nome ({names}). menciona quem é, por favor."
        return [], f"não achei ninguém chamado \"{name_match.group(1).strip()}\" no servidor. menciona a pessoa (@fulano)."

    def _relay_destination(self, message: Any, content: str) -> tuple[str, Any]:
        """('dm', None) ou ('canal', canal)."""
        norm = _norm(re.sub(r"<[@#][!&]?\d+>", " ", content))
        if DM_RE.search(norm):
            return "dm", None
        delivery = CHANNEL_DELIVERY_RE.search(content)
        if delivery:
            channel = self.bot.get_channel(int(delivery.group(1)))
            guild = getattr(message, "guild", None)
            if channel is None and guild is not None:
                getter = getattr(guild, "get_channel_or_thread", None) or getattr(guild, "get_channel", None)
                channel = getter(int(delivery.group(1))) if callable(getter) else None
            if channel is not None:
                return "canal", channel
        return "dm", None

    async def _compose_relay(self, instruction: str, target: Any, destination: str) -> tuple[str, str]:
        name = getattr(target, "display_name", "") or getattr(target, "name", "")
        fallback_body = re.sub(
            r"(?i)^.*?\b(?:de que|dizendo que|falando que|avisando que|que)\s+", "", instruction, count=1,
        )
        fallback_body = re.sub(r"(?i)[.,]?\s*(?:mand[ae]|envi[ae])\w*\s+(?:na|no|pela|pelo)\s+(?:dm|pv|privado|direct|canal)\b.*$", "", fallback_body).strip(" .")
        fallback = f"Oi, {name}! Tudo bem? O Talisson me pediu pra te passar um recado: {fallback_body}."
        if self.ai is not None:
            where = "na mensagem privada (DM) dela" if destination == "dm" else "num canal do servidor, então comece mencionando a pessoa"
            try:
                text = await self.ai.generate_text(
                    "Você é a Bella, assistente do Talisson num servidor do Discord. O Talisson te pediu "
                    "para dar um recado a uma pessoa. Escreva SÓ a mensagem que vai ser enviada para ela, "
                    f"{where}.\n\n"
                    f"PESSOA: {name}\n"
                    f"PEDIDO DO TALISSON: {instruction}\n\n"
                    f"Atenção às pessoas: no pedido, 'eu', 'mim', 'me' e 'comigo' são o Talisson; 'ele', "
                    f"'ela', 'dele', 'dela' são {name}. Na mensagem, fale com {name} usando 'você' e "
                    "chame o Talisson pelo nome (ex.: 'falta você passar as informações pro Talisson').\n\n"
                    "Regras: cumprimente a pessoa pelo nome; diga que o Talisson pediu para você dar o "
                    "recado; reescreva o conteúdo com suas palavras, falando direto com ela (você), de "
                    "forma natural e simpática; mantenha TODOS os fatos e pedidos, sem inventar nada; "
                    "não inclua as instruções de entrega (\"manda na DM\", \"no canal\"); mantenha links e "
                    "menções <#...> exatamente; de 1 a 4 frases; pode usar ♡ no máximo uma vez; nunca "
                    "chame ninguém de 'Senhor'.",
                    max_output_tokens=300,
                    timeout_seconds=25,
                    use_cache=False,
                )
                text = str(text or "").strip().strip('"')
                if 20 <= len(text) <= 1800:
                    return text, "ia"
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("IA indisponível para escrever o recado: %s", type(exc).__name__)
        return fallback, "contingencia"

    async def send_relay(self, message: Any, content: str) -> str:
        targets, problem = self._relay_targets(message, content)
        if problem:
            explicit = re.search(r"\b(?:recado|aviso|mensagem|msg)\b", _norm(content))
            return problem if explicit or getattr(message, "mentions", None) else ""
        destination, channel = self._relay_destination(message, content)
        bot_id = getattr(getattr(self.bot, "user", None), "id", 0)
        instruction = re.sub(rf"<@!?{bot_id}>", " ", content)
        for member in targets:
            instruction = re.sub(rf"<@!?{member.id}>", getattr(member, "display_name", "a pessoa"), instruction)
        instruction = _strip_lead(instruction)[:1500]
        reports = []
        for member in targets[:10]:
            text, origin = await self._compose_relay(instruction, member, destination)
            try:
                if destination == "canal":
                    if f"<@{member.id}>" not in text and f"<@!{member.id}>" not in text:
                        text = f"<@{member.id}> {text}"
                    sent = await channel.send(text[:2000], allowed_mentions=discord.AllowedMentions(users=[member], roles=False, everyone=False))
                    where = getattr(channel, "mention", "o canal")
                else:
                    sent = await member.send(text[:2000], allowed_mentions=discord.AllowedMentions.none())
                    where = "na DM"
            except discord.Forbidden:
                reports.append(
                    f"não consegui mandar pro {member.display_name}: "
                    + ("a DM dele tá fechada. quer que eu mande num canal? é só dizer qual." if destination == "dm" else "não tenho permissão nesse canal.")
                )
                continue
            except discord.HTTPException as exc:
                reports.append(f"o Discord recusou o recado pro {member.display_name} ({exc.status}). tenta de novo daqui a pouco?")
                continue
            await self.state.update_section(RELAY_SECTION, f"r{int(time.time() * 1000)}-{member.id}", {
                "to": member.id, "where": destination, "channel_id": getattr(channel, "id", None),
                "message_id": getattr(sent, "id", None), "text": text[:2000], "origin": origin,
                "instruction": instruction, "at": datetime.now(timezone.utc).isoformat(),
            })
            reports.append(f"mandei pro **{member.display_name}** {where}: ♡\n> " + text[:900].replace("\n", "\n> "))
        return "\n\n".join(reports)

    # ------------------------------------------- "o que eu tava fazendo?"
    async def _track_activity(self, content: str, *, in_channel: bool) -> str:
        """Registra o pedido e, se ele voltou de uma pausa, devolve o 'bem-vindo de volta'."""
        now = datetime.now(timezone.utc)
        section = await self.state.get_section(LOG_SECTION)
        items = list(section.get("items", []))
        last = _iso(items[-1]["at"]) if items else None
        text = " ".join(re.sub(r"<@!?\d+>", " ", content).split())[:300]
        if text:
            items.append({"at": now.isoformat(), "text": text})
            await self.state.update_section(LOG_SECTION, "items", items[-60:])
        if not in_channel or last is None or now - last < timedelta(hours=self.pause_hours):
            return ""
        if DOING_RE.search(_norm(text)):
            return ""
        summary = await self.what_was_i_doing(before=last + timedelta(seconds=1), quiet=True)
        return f"bem-vindo de volta! ♡ {summary}" if summary else ""

    async def what_was_i_doing(self, *, before: datetime | None = None, quiet: bool = False) -> str:
        items = (await self.state.get_section(LOG_SECTION)).get("items", [])
        if before is not None:
            items = [i for i in items if (_iso(i.get("at")) or before) < before]
        else:
            items = items[:-1]  # tira a própria pergunta
        session: list[dict[str, Any]] = []
        for item in reversed(items):
            moment = _iso(item.get("at"))
            if session and moment and _iso(session[-1]["at"]) - moment > timedelta(hours=self.pause_hours):
                break
            session.append(item)
        session.reverse()
        since = _iso(session[0]["at"]) if session else datetime.now(timezone.utc) - timedelta(days=1)
        open_tasks = [t for t in await self._todos() if not t.get("done") and (_iso(t.get("created_at")) or since) >= since - timedelta(minutes=1)]
        lines: list[str] = []
        if session:
            when = _iso(session[-1]["at"]).astimezone(self.tz).strftime("%d/%m às %Hh%M").replace("h00", "h")
            lines.append(f"da última vez ({when}) você estava em:")
            lines += [f"• {i['text'][:160]}" for i in session[-5:]]
        if open_tasks:
            lines.append("ficou pela metade:")
            lines += [f"• {t['text']}" for t in open_tasks[-8:]]
        soon = await self._reminders_on(self.now().date())
        if soon:
            lines.append("e hoje ainda tem:")
            lines += [f"• {r['hora']} — {r['description']}" for r in soon[:5]]
        if not lines:
            return "" if quiet else "não achei nada recente: nenhum pedido seu nem tarefa aberta."
        return "\n".join(lines)

    # ------------------------------------------------------ modo viagem
    async def _travel(self) -> dict[str, Any] | None:
        travel = (await self.state.get_section(META_SECTION)).get("travel")
        return travel if isinstance(travel, dict) and travel.get("active") else None

    def _parse_until(self, text: str) -> datetime | None:
        today = self.now().replace(hour=0, minute=0, second=0, microsecond=0)
        match = re.search(r"\bate (?:o )?(?:dia )?(\d{1,2})(?:/(\d{1,2}))?(?:/(\d{2,4}))?\b", text)
        if match:
            day = int(match.group(1))
            month = int(match.group(2) or today.month)
            year = int(match.group(3) or today.year)
            year = year + 2000 if year < 100 else year
            month_word = re.search(r"\bate (?:o )?(?:dia )?\d{1,2} de (" + "|".join(_norm(m) for m in MONTHS_PT[1:]) + r")\b", text)
            if month_word:
                month = [_norm(m) for m in MONTHS_PT].index(month_word.group(1))
            try:
                candidate = today.replace(year=year, month=month, day=day)
            except ValueError:
                return None
            if candidate < today and not match.group(2):
                next_month = month % 12 + 1
                candidate = candidate.replace(year=year + (month == 12), month=next_month)
            return candidate
        if re.search(r"\bate (?:depois de )?amanha\b", text):
            return today + timedelta(days=2 if "depois" in text else 1)
        weekday = re.search(r"\bate (?:a |o )?(?:proxima |proximo )?(segunda|terca|quarta|quinta|sexta|sabado|domingo)\b", text)
        if weekday:
            target = [_norm(d) for d in WEEKDAYS].index(weekday.group(1))
            ahead = (target - today.weekday()) % 7 or 7
            return today + timedelta(days=ahead)
        weeks = re.search(r"\bpor (\d+|uma|duas|tres) semanas?\b", text)
        if weeks:
            amount = {"uma": 1, "duas": 2, "tres": 3}.get(weeks.group(1)) or int(weeks.group(1))
            return today + timedelta(days=7 * amount)
        return None

    async def start_travel(self, content: str, message: Any) -> str:
        text = _norm(content)
        until = self._parse_until(text)
        if until is None:
            return "anotei que você vai ficar fora, mas até quando? (ex.: \"até dia 20\")"
        label = f"{until.day:02d}/{until.month:02d}"
        reason = "férias" if "feria" in text else "viagem"
        travel = {
            "active": True, "reason": reason, "until": until.isoformat(), "label": label,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "previous_presence": await self.state.get("custom_presence_override"),
        }
        await self.state.update_section(META_SECTION, "travel", travel)
        if self.availability is not None:
            try:
                await self.availability.set_status(
                    reason=f"{reason} até {label}", original_text=content,
                    guild_id=getattr(getattr(message, "guild", None), "id", 0) or 0,
                    channel_id=message.channel.id, message_id=getattr(message, "id", 0) or 0,
                )
            except Exception:  # noqa: BLE001
                LOGGER.exception("Falha ao marcar a ausência no modo viagem.")
        await self.state.set("custom_presence_override", {
            "enabled": True, "status": "idle", "activity_type": "custom",
            "text": f"Talisson de {reason} • volta dia {label}",
        })
        await self._refresh_presence()
        return (
            f"modo {reason} ligado até **{label}**. ♡\n"
            "• mudei meu status;\n• quem te chamar fica sabendo quando você volta e eu anoto o recado;\n"
            "• guardo tudo (menções, recados, tickets, orçamentos, pagamentos) e te entrego um resumo na volta.\n"
            "quando voltar é só falar \"voltei\" (ou eu encerro sozinha no dia seguinte)."
        )

    async def end_travel(self) -> str:
        travel = await self._travel()
        if travel is None:
            return "você não estava no modo viagem."
        travel["active"] = False
        travel["ended_at"] = datetime.now(timezone.utc).isoformat()
        await self.state.update_section(META_SECTION, "travel", travel)
        if self.availability is not None:
            try:
                await self.availability.clear_status()
            except Exception:  # noqa: BLE001
                LOGGER.exception("Falha ao limpar a ausência do modo viagem.")
        previous = travel.get("previous_presence")
        await self.state.set("custom_presence_override", previous if isinstance(previous, dict) else {"enabled": False})
        await self._refresh_presence()
        return "bem-vindo de volta! ♡\n\n" + await self.away_summary(_iso(travel.get("started_at")) or datetime.now(timezone.utc))

    async def _refresh_presence(self) -> None:
        if self.refresh_presence is None:
            return
        try:
            await self.refresh_presence()
        except Exception:  # noqa: BLE001
            LOGGER.warning("Não consegui atualizar o status da Bella.")

    async def away_summary(self, since: datetime) -> str:
        lines = [f"**enquanto você esteve fora (desde {since.astimezone(self.tz).strftime('%d/%m')}):**"]
        inbox = [
            item for values in (await self.state.get_section(INBOX_SECTION)).values() if isinstance(values, list)
            for item in values if (_iso(item.get("at")) or since) >= since
        ]
        urgent = [i for i in inbox if i.get("urgent")]
        lines.append(f"\n**te chamaram {len(inbox)} vez(es)**" + (f", {len(urgent)} parecendo urgente:" if urgent else ":" if inbox else "."))
        for item in sorted(inbox, key=lambda i: (not i.get("urgent"), i.get("at", "")))[:10]:
            flag = "🔴 " if item.get("urgent") else "• "
            lines.append(f"{flag}{item.get('author_name', '?')} em #{item.get('channel_name', '?')}: \"{item.get('snippet', '')[:120]}\" {item.get('jump_url', '')}")
        notes = [v for v in (await self.state.get_section(MESSAGES_SECTION)).values()
                 if isinstance(v, dict) and (_iso(v.get("created_at")) or since) >= since]
        if notes:
            lines.append(f"\n**recados ({len(notes)}):**")
            lines += [f"• {n.get('author_name', '?')} ({n.get('category', 'Contato')}): {str(n.get('subject', ''))[:140]}" for n in notes[:10]]
        tickets = [t for t in (await self.state.get_section(TICKET_SECTION)).values()
                   if isinstance(t, dict) and (_iso(t.get("created_at")) or since) >= since]
        quotes = [t for t in tickets if t.get("quote_summary")]
        if tickets:
            lines.append(f"\n**tickets:** {len(tickets)} aberto(s)" + (f", {len(quotes)} de orçamento:" if quotes else "."))
            lines += [f"• {t.get('creator_name', '?')}: {str(t['quote_summary'].get('titulo', ''))[:100]}" for t in quotes[:5]]
        lines.append(f"\n**dinheiro:** {await self._revenue(since)}")
        pending = [t for t in await self._todos() if not t.get("done")]
        if pending:
            lines.append(f"\n**suas tarefas abertas ({len(pending)}):**")
            lines += [f"• {t['text']}" for t in pending[:8]]
        return "\n".join(lines)

    # ------------------------------------------------------ caixa de ideias
    async def save_idea(self, content: str) -> str:
        text = IDEA_RE.sub("", _strip_lead(content), count=1).strip(" :-,") or content.strip()
        text = re.sub(r"(?i)^(?:ideia|tive uma ideia|anota (?:essa |uma |a )?ideia|nova ideia|ideia nova)\s*[:,\-]?\s*", "", text).strip()
        if len(text) < 4:
            return "qual é a ideia?"
        idea_id = f"i{int(time.time() * 1000)}"
        await self.state.update_section(IDEAS_SECTION, idea_id, {
            "id": idea_id, "text": text[:500], "status": "open", "created_at": self.now().isoformat(),
        })
        total = len(await self._ideas())
        return f"guardei na caixa de ideias: \"{text[:200]}\" ({total} na caixa). todo mês eu te pergunto quais valem tirar do papel."

    async def _ideas(self, status: str = "open") -> list[dict[str, Any]]:
        values = (await self.state.get_section(IDEAS_SECTION)).values()
        return sorted((v for v in values if isinstance(v, dict) and v.get("status") == status), key=lambda v: v.get("created_at", ""))

    async def ideas_text(self) -> str:
        ideas = await self._ideas()
        if not ideas:
            return "a caixa de ideias tá vazia. é só mandar \"ideia: ...\" aqui."
        return "**caixa de ideias:**\n" + "\n".join(f"{i}. {d['text']}" for i, d in enumerate(ideas, start=1))

    async def monthly_idea_review(self) -> None:
        ideas = await self._ideas()
        if not ideas:
            return
        await self.state.update_section(META_SECTION, "idea_review", {
            "ids": [d["id"] for d in ideas[:20]], "at": datetime.now(timezone.utc).isoformat(),
        })
        await self.say(
            "💡 **revisão mensal da caixa de ideias**\n"
            + "\n".join(f"{i}. {d['text']}" for i, d in enumerate(ideas[:20], start=1))
            + "\n\nquais valem tirar do papel? responde com os números (ex.: \"a 1 e a 3\"). "
            "pra jogar fora, \"descarta a 2\"."
        )

    async def _idea_review_answer(self, text: str) -> str:
        review = (await self.state.get_section(META_SECTION)).get("idea_review")
        if not isinstance(review, dict) or not review.get("ids"):
            return ""
        opened = _iso(review.get("at"))
        if opened is None or datetime.now(timezone.utc) - opened > timedelta(days=5):
            return ""
        numbers = [int(n) for n in re.findall(r"\b(\d{1,2})\b", text)]
        if not numbers or len(text) > 120:
            return ""
        ids = review["ids"]
        discard = bool(re.search(r"\b(?:descarta|joga fora|apaga|tira|remove)\b", text))
        section = await self.state.get_section(IDEAS_SECTION)
        picked = []
        for number in numbers:
            if 1 <= number <= len(ids) and isinstance(section.get(ids[number - 1]), dict):
                idea = section[ids[number - 1]]
                idea["status"] = "discarded" if discard else "chosen"
                idea["decided_at"] = self.now().isoformat()
                await self.state.update_section(IDEAS_SECTION, idea["id"], idea)
                if not discard:
                    await self._add_todo(f"tirar do papel: {idea['text'][:250]}")
                picked.append(idea["text"])
        if not picked:
            return ""
        if discard:
            return "descartei: " + "; ".join(picked[:5]) + "."
        return "boa! virou tarefa:\n" + "\n".join(f"• {p}" for p in picked)

    # ------------------------------------------------------- contas a pagar
    async def save_bill(self, content: str) -> str:
        norm = _norm(content)
        match = BILL_RE.search(norm)
        day = int(match.group(1) or match.group(2))
        if not 1 <= day <= 31:
            return "esse dia não existe. qual é o dia do vencimento?"
        raw = _strip_lead(content)
        name = re.split(r"(?i)\s+(?:vence|vencimento|é todo|e todo|todo dia|todo m[eê]s|dia \d)", raw)[0]
        name = re.sub(r"(?i)^(?:a |o |eu )?(?:pago |pagar |pagamento |boleto |fatura |conta )?(?:da |do |de |a |o )?", "", name).strip(" ,.:-")
        name = re.sub(r"(?i)\s*r\$\s*[\d.,]+", "", name).strip() or "conta"
        amount_match = re.search(r"r\$\s*([\d.,]+)", norm)
        amount = None
        if amount_match:
            try:
                amount = float(amount_match.group(1).replace(".", "").replace(",", "."))
            except ValueError:
                amount = None
        bill_id = f"b{_norm(name).replace(' ', '-')[:40]}"
        await self.state.update_section(BILLS_SECTION, bill_id, {
            "id": bill_id, "name": name[:80], "day": day, "amount": amount, "active": True,
            "created_at": self.now().isoformat(), "notified_for": "", "paid_for": "",
        })
        value = f" ({_brl(amount)})" if amount else ""
        return f"anotado: **{name}**{value} vence todo dia {day}. te lembro 2 dias antes, todo mês. ♡"

    async def _bills(self) -> list[dict[str, Any]]:
        values = (await self.state.get_section(BILLS_SECTION)).values()
        return sorted((v for v in values if isinstance(v, dict) and v.get("active")), key=lambda v: v["day"])

    def _next_due(self, day: int):
        today = self.now().date()
        for offset in (0, 1):
            month = (today.month - 1 + offset) % 12 + 1
            year = today.year + (today.month - 1 + offset) // 12
            last_day = (datetime(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1)).day
            due = today.replace(year=year, month=month, day=min(day, last_day))
            if due >= today:
                return due
        return today

    async def bills_text(self) -> str:
        bills = await self._bills()
        if not bills:
            return "nenhuma conta cadastrada. ex.: \"Contabo vence dia 10 todo mês\"."
        lines = ["**contas do mês:**"]
        for bill in bills:
            due = self._next_due(bill["day"])
            paid = " ✅ paga" if bill.get("paid_for") == due.isoformat() else ""
            value = f" — {_brl(bill['amount'])}" if bill.get("amount") else ""
            lines.append(f"• {bill['name']}{value}: dia {bill['day']} (próximo {due.strftime('%d/%m')}){paid}")
        return "\n".join(lines)

    async def _find_bill(self, name: str) -> dict[str, Any] | None:
        wanted = _norm(name).strip(" .!")
        for bill in await self._bills():
            bill_name = _norm(bill["name"])
            if bill_name and (bill_name in wanted or wanted in bill_name):
                return bill
        return None

    async def mark_bill_paid(self, name: str) -> str:
        bill = await self._find_bill(name)
        if bill is None:
            return ""
        bill["paid_for"] = self._next_due(bill["day"]).isoformat()
        await self.state.update_section(BILLS_SECTION, bill["id"], bill)
        return f"marquei **{bill['name']}** como paga neste mês. ♡"

    async def remove_bill(self, name: str) -> str:
        bill = await self._find_bill(name)
        if bill is None:
            return "não achei essa conta."
        bill["active"] = False
        await self.state.update_section(BILLS_SECTION, bill["id"], bill)
        return f"tirei **{bill['name']}** das contas."

    async def check_bills(self) -> int:
        today = self.now().date()
        sent = 0
        for bill in await self._bills():
            due = self._next_due(bill["day"])
            key = due.isoformat()
            if bill.get("paid_for") == key or bill.get("notified_for") == key:
                continue
            days = (due - today).days
            if days > 2:
                continue
            when = "hoje" if days == 0 else "amanhã" if days == 1 else f"em {days} dias"
            value = f" ({_brl(bill['amount'])})" if bill.get("amount") else ""
            await self.say(f"💸 **{bill['name']}**{value} vence {when}, dia {due.strftime('%d/%m')}. quando pagar, me fala \"paguei a {bill['name']}\".")
            bill["notified_for"] = key
            await self.state.update_section(BILLS_SECTION, bill["id"], bill)
            sent += 1
        return sent

    # ------------------------------------------------------------- worker
    async def worker(self) -> None:
        wait = getattr(self.bot, "wait_until_ready", None)
        if callable(wait):
            await wait()
        while not getattr(self.bot, "is_closed", lambda: False)():
            try:
                await self.tick()
            except Exception:
                LOGGER.exception("Falha no worker do assistente do dono.")
            await asyncio.sleep(60)

    async def tick(self) -> None:
        now = self.now()
        meta = await self.state.get_section(META_SECTION)
        week = now.strftime("%G-W%V")
        if now.weekday() == 4 and now.hour >= 20 and meta.get("weekly_sent") != week:
            await self.state.update_section(META_SECTION, "weekly_sent", week)
            await self.say(await self.weekly_summary())
        month = now.strftime("%Y-%m")
        if now.day == 1 and now.hour >= 9 and meta.get("ranking_sent") != month:
            await self.state.update_section(META_SECTION, "ranking_sent", month)
            await self.say(await self.staff_ranking(days=31))
        if now.day == 1 and now.hour >= 11 and meta.get("ideas_review_sent") != month:
            await self.state.update_section(META_SECTION, "ideas_review_sent", month)
            await self.monthly_idea_review()
        today = now.strftime("%Y-%m-%d")
        if now.hour >= 9 and meta.get("bills_checked") != today:
            await self.state.update_section(META_SECTION, "bills_checked", today)
            await self.check_bills()
        travel = meta.get("travel")
        if isinstance(travel, dict) and travel.get("active"):
            until = _iso(travel.get("until"))
            if until is not None and now >= until.astimezone(self.tz) + timedelta(days=1, hours=9):
                await self.say(await self.end_travel())
        if now.hour >= 10 and meta.get("daily_checks") != today:
            await self.state.update_section(META_SECTION, "daily_checks", today)
            channel = self.channel()
            guild = getattr(channel, "guild", None)
            if guild is not None:
                await self.check_quiet_channels(guild)
            await self.propose_client_drafts()
