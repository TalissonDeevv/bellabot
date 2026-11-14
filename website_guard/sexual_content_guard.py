"""Bloqueio de conteúdo sexual e divulgação +18 que escapa do AutoMod do Discord.

O AutoMod nativo barra a mensagem antes de ela chegar; o que passa (com
emojis, letras trocadas, @ de Telegram...) cai aqui:

1. Filtro local gratuito: normaliza o texto (leetspeak, letras repetidas,
   pontuação no meio) e pontua termos sexuais, zoofilia, convite para
   Telegram/WhatsApp e emojis típicos.
2. Só os suspeitos vão para a IA, que identifica o tema, a que se refere e se
   é mesmo violação (evita punir gíria comum como "é foda").
3. Confirmado: apaga a mensagem, aplica mute pesado, avisa no chat geral
   mencionando a pessoa (texto escrito pela IA, sem repetir o conteúdo) e manda
   o caso para o Talisson decidir com botões (banir, tirar o mute, manter).
"""

from __future__ import annotations

import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from unicodedata import combining, normalize

import discord

LOGGER = logging.getLogger(__name__)

CASES_SECTION = "sexual_guard_cases"
DEFAULT_DECISION_LOG_CHANNEL_ID = 1551649724598063125
LEET = str.maketrans({"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s", "!": "i"})

SEXUAL_TERMS = (
    "foder", "fuder", "fode", "fodendo", "fudendo", "transar", "transando", "sexo", "sexual", "piroca", "pica",
    "buceta", "boceta", "xoxota", "xereca", "punheta", "siririca", "gozar", "gozei", "gozando", "tesao", "tesuda",
    "tesudo", "safada", "safado", "putaria", "nudes", "nude", "pelada", "pelado", "boquete", "mamada", "chupar",
    "chupada", "rola", "caralho grosso", "penis", "vagina", "anal", "orgasmo", "porno", "porn", "pornografia",
    "xvideos", "onlyfans", "only fans", "privacy", "pack do pe", "packs", "conteudo adulto", "conteudo +18",
    "sexy", "horny", "fuck", "fucking", "pussy", "dick", "cock", "cum", "nsfw", "hentai", "milf",
)
SQUASHED_TERMS = ("foder", "fuder", "piroca", "buceta", "boceta", "xoxota", "punheta", "siririca", "putaria",
                  "boquete", "onlyfans", "xvideos", "pornografia", "nudes")
ANIMALS = ("cachorro", "cachorra", "cao", "cavalo", "egua", "gato", "gata", "bode", "cabra", "animal", "dog", "horse", "pet")
ZOO_VERBS = ("foder", "fuder", "fode", "transar", "comer", "come", "meter", "mete", "chupar", "sexo", "gozar", "fuck")
MINORS = ("crianca", "menor de idade", "menorzinha", "novinha de 1", "10 anos", "11 anos", "12 anos", "13 anos", "14 anos", "15 anos", "16 anos", "17 anos", "cp ", "loli", "lolicon")
SOLICITATION = re.compile(
    r"\b(?:me add|me adiciona\w*|adiciona\s*(?:ai|aí)|me chama|chama no|vem (?:ver|no)|link na bio|confere|da uma olhada|"
    r"telegram|telegran|tg|whats?app|whats|zap|wpp|insta|instagram|pv|dm|inbox|privado|vendo|vendendo|packs?|\+18|18\+)\b"
)
HANDLE = re.compile(r"@[A-Za-z0-9_]{4,}|t\.me/\S+|wa\.me/\S+|chat\.whatsapp\.com/\S+")
EMOJIS = ("💦", "🍆", "🍑", "😈", "🔞", "👅", "🥵", "🍒", "💋", "🫦")


def _deobfuscate(text: str) -> str:
    """Desfaz fontes personalizadas, letras de emoji, letras 'gêmeas' e caracteres invisíveis."""
    out = []
    for char in str(text or ""):
        code = ord(char)
        if 0x1F1E6 <= code <= 0x1F1FF:            # 🇦..🇿 (letras de bandeira)
            out.append(chr(ord("a") + code - 0x1F1E6))
        elif 0x1F170 <= code <= 0x1F189:          # 🅰..🆉
            out.append(chr(ord("a") + code - 0x1F170))
        elif 0x1F150 <= code <= 0x1F169:          # 🅐..🅩
            out.append(chr(ord("a") + code - 0x1F150))
        elif 0x1F130 <= code <= 0x1F149:          # 🄰..🅉
            out.append(chr(ord("a") + code - 0x1F130))
        elif char in "​‌‍⁠﻿­️":
            continue
        else:
            out.append(char)
    # NFKC transforma 𝓯𝓾𝓭𝓮𝓻, 𝐟𝐮𝐝𝐞𝐫, ｆｕｄｅｒ, ⓕⓤⓓⓔⓡ e afins em letras comuns.
    return normalize("NFKC", "".join(out)).translate(CONFUSABLES)


# Letras de outros alfabetos que parecem latinas, letras pequenas e símbolos usados no lugar de letras.
CONFUSABLES = str.maketrans({
    "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "у": "y", "х": "x", "к": "k", "м": "m", "т": "t", "н": "h",
    "в": "b", "і": "i", "ј": "j", "ѕ": "s", "ԁ": "d", "ɡ": "g", "ո": "n", "υ": "u", "ν": "v", "ο": "o", "α": "a",
    "ε": "e", "ρ": "p", "τ": "t", "κ": "k", "ι": "i", "η": "n", "ѵ": "v", "ᴀ": "a", "ʙ": "b", "ᴄ": "c", "ᴅ": "d",
    "ᴇ": "e", "ғ": "f", "ɢ": "g", "ʜ": "h", "ɪ": "i", "ᴊ": "j", "ᴋ": "k", "ʟ": "l", "ᴍ": "m", "ɴ": "n", "ᴏ": "o",
    "ᴘ": "p", "ʀ": "r", "ꜱ": "s", "ᴛ": "t", "ᴜ": "u", "ᴠ": "v", "ᴡ": "w", "ʏ": "y", "ᴢ": "z", "ɑ": "a", "ǝ": "e",
})
SYMBOLS = str.maketrans({"&": "e", "%": "o", "#": "h", "|": "i", "8": "b", "9": "g", "€": "e", "¡": "i"})


def normalize_text(text: str) -> tuple[str, list[str]]:
    """(texto normalizado com espaços, 'palavras' com o disfarce removido).

    Letras repetidas viram uma só (fuuuder -> fuder), mas palavras com letra
    dobrada de verdade (cachorro) são conferidas também na forma original.
    Pontuação, emojis e espaços entre letras são tirados só dentro da mesma
    palavra disfarçada (f.u.d.e.r, f u d e r), nunca juntando palavras normais.
    """
    decomposed = normalize("NFKD", _deobfuscate(text).casefold())
    plain = "".join(c for c in decomposed if not combining(c))
    for shape, letter in (("|_|", "u"), ("(_)", "u"), ("/\\", "a"), ("\\/", "v"), ("|\\|", "n"), ("|<", "k"), ("()", "o")):
        plain = plain.replace(shape, letter)
    leet = plain.translate(LEET).translate(SYMBOLS)
    leet = re.sub(r"([a-z])\1{2,}", r"\1", leet)            # fuuuuder -> fuder
    collapsed = re.sub(r"([a-z])\1", r"\1", leet)            # fuuder -> fuder
    spaced = " ".join(re.sub(r"[^a-z0-9+./ ]+", " ", leet).split())
    spaced_collapsed = " ".join(re.sub(r"[^a-z0-9+./ ]+", " ", collapsed).split())
    # Palavras disfarçadas: cada pedaço separado por espaço, com letras soltas
    # seguidas (f u d e r) juntadas numa palavra só.
    # Cada palavra marcada como "disfarçada" (tinha símbolo/emoji no meio ou
    # veio de letras soltas) ou normal.
    tokens: list[tuple[str, bool]] = []
    run: list[str] = []
    for raw in leet.split():
        letters = re.sub(r"[^a-z]+", "", raw)
        if 0 < len(letters) <= 2 and len(raw) <= 3:
            run.append(letters)
            continue
        if run:
            tokens.append(("".join(run), len(run) > 1))
            run = []
        if letters:
            inner = raw.strip(".,!?;:\"'()[]{}<>")
            tokens.append((letters, bool(re.search(r"[^a-z]", inner))))
    if run:
        tokens.append(("".join(run), len(run) > 1))
    words = sorted(
        {(re.sub(r"([a-z])\1+", r"\1", token), disguised) for token, disguised in tokens if len(token) >= 4}
    )
    return f"{spaced} | {spaced_collapsed}", words


def local_score(text: str) -> tuple[int, list[str]]:
    spaced, words = normalize_text(text)
    padded = f" {spaced} "
    score, reasons = 0, []
    single_terms = {t for t in (*SQUASHED_TERMS, *SEXUAL_TERMS) if " " not in t and len(t) >= 4}
    disguised = {
        t for t in single_terms for w, hidden in words
        if t == w or t == re.sub(r"([a-z])\1+", r"\1", t) == w or (hidden and t in w)
    }
    sexual = sorted({t for t in SEXUAL_TERMS if f" {t} " in padded} | disguised)
    if sexual:
        score += 2 + min(4, len(sexual))
        reasons.append("termos sexuais: " + ", ".join(sexual[:5]))
    if any(f" {a} " in padded for a in ANIMALS) and any(f" {v} " in padded for v in ZOO_VERBS):
        score += 6
        reasons.append("possível zoofilia")
    if any(m in padded for m in MINORS) and (sexual or any(f" {v} " in padded for v in ZOO_VERBS)):
        score += 10
        reasons.append("possível envolvimento de menores")
    if SOLICITATION.search(spaced):
        score += 2
        reasons.append("convite para contato/divulgação")
    if HANDLE.search(text or ""):
        score += 2
        reasons.append("@/link de contato externo")
    emoji_hits = sum(1 for e in EMOJIS if e in (text or ""))
    if emoji_hits:
        score += min(3, emoji_hits)
        reasons.append("emojis de conotação sexual")
    return score, reasons


class SexualContentGuard:
    def __init__(
        self,
        bot: Any,
        state: Any,
        *,
        ai_service: Any | None = None,
        owner_id: int,
        log_channel_id: int | None = None,
        exempt_role_ids: set[int] | None = None,
    ) -> None:
        self.bot = bot
        self.state = state
        self.ai = ai_service
        self.owner_id = int(owner_id)
        self.log_channel_id = int(log_channel_id or 0)
        self.exempt_role_ids = set(exempt_role_ids or ())
        self.enabled = os.getenv("SEXUAL_GUARD_ENABLED", "true").strip().casefold() not in {"0", "false", "nao", "off"}
        self.timeout_hours = float(os.getenv("SEXUAL_GUARD_TIMEOUT_HOURS", "168") or 168)  # 7 dias
        self.ai_threshold = int(os.getenv("SEXUAL_GUARD_AI_THRESHOLD", "3") or 3)
        self.offline_threshold = int(os.getenv("SEXUAL_GUARD_OFFLINE_THRESHOLD", "8") or 8)
        self.general_channel_id = int(os.getenv("GENERAL_CHAT_CHANNEL_ID", "") or 0)
        # Depois da decisão do Talisson, o registro da punição vai para este canal.
        self.decision_log_channel_id = int(
            os.getenv("SEXUAL_GUARD_DECISION_LOG_CHANNEL_ID", "") or DEFAULT_DECISION_LOG_CHANNEL_ID
        )

    def _exempt(self, member: Any) -> bool:
        if member.id in {self.owner_id, getattr(member.guild, "owner_id", None)} or getattr(member, "bot", False):
            return True
        perms = getattr(member, "guild_permissions", None)
        if perms is not None and (perms.administrator or perms.manage_messages or perms.moderate_members):
            return True
        return bool({role.id for role in getattr(member, "roles", [])} & self.exempt_role_ids)

    async def handle_message(self, message: Any) -> bool:
        """True se a mensagem foi tratada (apagada e punida)."""
        if not self.enabled or message.guild is None or not isinstance(message.author, discord.Member):
            return False
        if self._exempt(message.author):
            return False
        text = message.content or ""
        if len(text.strip()) < 4:
            return False
        score, reasons = local_score(text)
        if score < self.ai_threshold:
            return False
        # Suspeita: some do chat ANTES da análise, para ninguém ficar vendo.
        deleted = await self._delete(message)
        # O Talisson acompanha a análise no canal privado; o mesmo card é atualizado no fim.
        live = await self._announce_analysis(message, score, reasons, deleted)
        verdict = await self._classify(text, reasons)
        if verdict is None:
            if score < self.offline_threshold:
                await self._log(message, score, reasons, None,
                                actions=[deleted, "sem mute: a IA estava indisponível para confirmar; deixei apagada por precaução"],
                                case_id=None, existing=live)
                return True
            verdict = {"violation": True, "category": "conteúdo sexual (filtro local)", "theme": "; ".join(reasons),
                       "refers_to": "não analisado (IA indisponível)", "severity": 4, "confidence": 0.8}
        if not verdict.get("violation") or float(verdict.get("confidence") or 0) < 0.7:
            await self._restore(message)
            await self._log(message, score, reasons, verdict,
                            actions=[deleted, "falso alarme: a IA não viu conteúdo sexual; devolvi a mensagem ao canal"],
                            case_id=None, existing=live, cleared=True)
            return True
        return await self._punish(message, score, reasons, verdict, deleted=deleted, live=live)

    async def _announce_analysis(self, message: Any, score: int, reasons: list[str], deleted: str) -> Any:
        channel = self.bot.get_channel(self.log_channel_id) if self.log_channel_id else None
        if channel is None:
            return None
        embed = discord.Embed(
            title="🔎 Analisando mensagem suspeita…",
            description="apaguei do chat e a IA está lendo agora. atualizo este card com o resultado.",
            color=discord.Color.light_grey(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Quem", value=f"{message.author.mention} ({message.author})\nID `{message.author.id}`", inline=True)
        embed.add_field(name="Canal", value=getattr(message.channel, "mention", "?"), inline=True)
        embed.add_field(name="Mensagem (ocultada)", value=f"||{(message.content or '')[:900]}||", inline=False)
        embed.add_field(name="Filtro local", value=f"{score} pontos: " + "; ".join(reasons)[:900], inline=False)
        embed.add_field(name="Até agora", value=deleted, inline=False)
        try:
            return await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        except (discord.Forbidden, discord.HTTPException):
            return None

    async def _delete(self, message: Any) -> str:
        try:
            await message.delete()
            return "mensagem apagada na hora (antes da análise)"
        except discord.NotFound:
            return "mensagem já tinha sumido"
        except (discord.Forbidden, discord.HTTPException) as exc:
            return f"não consegui apagar ({type(exc).__name__})"

    async def _restore(self, message: Any) -> None:
        """Falso alarme: devolve o texto da pessoa ao canal."""
        text = (message.content or "").strip()
        if not text:
            return
        quoted = "\n".join(f"> {line}" for line in text[:1700].splitlines())
        try:
            await message.channel.send(
                f"**{message.author.display_name}** disse:\n{quoted}\n-# (eu tinha segurado essa mensagem pra conferir, tá tudo certo ♡)",
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except (discord.Forbidden, discord.HTTPException):
            LOGGER.warning("Não consegui devolver a mensagem de %s.", message.author.id)

    async def _classify(self, text: str, reasons: list[str]) -> dict[str, Any] | None:
        if self.ai is None:
            return None
        try:
            return await self.ai.generate_json(
                "Você modera um servidor do Discord de tecnologia e negócios (público geral, pode ter menores). "
                "Analise a mensagem abaixo de um membro.\n"
                f"MENSAGEM: {text[:1500]}\n"
                f"MESMA MENSAGEM SEM DISFARCE (fontes, símbolos e emojis no meio das letras removidos): "
                f"{normalize_text(text)[0].split(' | ')[0][:1500]}\n"
                f"SINAIS DO FILTRO LOCAL: {'; '.join(reasons)}\n\n"
                "Atenção: pessoas disfarçam palavras com fontes especiais, letras de outros alfabetos, "
                "símbolos (@ # % & $), números no lugar de letras, emojis e espaços entre as letras. "
                "Leia a versão sem disfarce para entender o sentido real.\n\n"
                "violation: true se for conteúdo sexual explícito, insinuação sexual, divulgação de "
                "conteúdo +18/venda de packs, convite sexual para Telegram/WhatsApp, zoofilia ou qualquer "
                "coisa sexual envolvendo menores. false para gíria comum sem conotação sexual (ex.: 'é "
                "foda', 'que porra'), xingamento sem tema sexual, ou assunto técnico. "
                "category: uma de sexual_explicito, divulgacao_mais18, zoofilia, menores, assedio_sexual, nenhum. "
                "theme: o tema da mensagem em poucas palavras. refers_to: a que ou a quem ela se refere "
                "(ex.: 'ato sexual com animal e convite para perfil no Telegram'). severity: 1 a 5. "
                "confidence: 0 a 1. Responda em português.",
                {
                    "type": "object",
                    "properties": {
                        "violation": {"type": "boolean"},
                        "category": {"type": "string"},
                        "theme": {"type": "string"},
                        "refers_to": {"type": "string"},
                        "severity": {"type": "integer"},
                        "confidence": {"type": "number"},
                    },
                    "required": ["violation", "category", "theme", "refers_to", "confidence"],
                },
                max_output_tokens=500,
                timeout_seconds=20,
                use_cache=False,
            )
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("IA indisponível para classificar conteúdo sexual: %s", type(exc).__name__)
            return None

    async def _punish(self, message: Any, score: int, reasons: list[str], verdict: dict[str, Any], *, deleted: str, live: Any = None) -> bool:
        member = message.author
        actions: list[str] = [deleted]
        hours = min(self.timeout_hours, 24 * 28)
        try:
            await member.timeout(
                timedelta(hours=hours),
                reason=f"Bella: conteúdo sexual ({verdict.get('category', '?')}). Caso enviado ao Talisson.",
            )
            actions.append(f"mute de {int(hours)}h")
        except (discord.Forbidden, discord.HTTPException) as exc:
            actions.append(f"não consegui mutar ({type(exc).__name__}; confira a hierarquia de cargos)")
        case_id = f"{message.guild.id}-{member.id}-{int(time.time())}"
        await self.state.update_section(CASES_SECTION, case_id, {
            "guild_id": message.guild.id, "user_id": member.id, "user_name": str(member), "channel_id": message.channel.id,
            "content": (message.content or "")[:1800], "verdict": verdict, "local": {"score": score, "reasons": reasons},
            "actions": actions, "status": "aguardando decisão", "at": datetime.now(timezone.utc).isoformat(),
        })
        await self._warn_publicly(message, verdict)
        await self._log(message, score, reasons, verdict, actions=actions, case_id=case_id, existing=live)
        return True

    def _general_channel(self, guild: Any, fallback: Any) -> Any:
        if self.general_channel_id:
            channel = guild.get_channel(self.general_channel_id)
            if channel is not None:
                return channel
        for channel in getattr(guild, "text_channels", []):
            name = normalize("NFKD", channel.name.casefold())
            if re.search(r"bate-?papo|geral|chat-?geral|general", name):
                return channel
        return fallback

    async def _warn_publicly(self, message: Any, verdict: dict[str, Any]) -> None:
        member = message.author
        text = ""
        if self.ai is not None:
            try:
                text = await self.ai.generate_text(
                    "Você é a Bella, moderadora simpática mas firme de um servidor do Discord. Um membro mandou "
                    f"uma mensagem com conteúdo sexual impróprio (tema: {verdict.get('theme', 'conteúdo sexual')}). "
                    "A mensagem já foi apagada e a pessoa foi silenciada. Escreva um aviso curto (2 frases) para o "
                    f"chat, começando com {member.mention}, dizendo que esse tipo de conteúdo não é permitido aqui e "
                    "que o caso foi encaminhado para a equipe. NÃO repita nem descreva o conteúdo. Português do Brasil, "
                    "sem emojis exagerados.",
                    max_output_tokens=200,
                    timeout_seconds=15,
                    use_cache=False,
                )
            except Exception:  # noqa: BLE001
                text = ""
        text = " ".join(str(text or "").split())
        if member.mention not in text and f"<@!{member.id}>" not in text:
            text = ""
        if not text or len(text) > 600:
            text = (
                f"{member.mention}, conteúdo sexual não é permitido aqui. A mensagem foi removida, você foi "
                "silenciado(a) e o caso foi encaminhado para a equipe."
            )
        channel = self._general_channel(message.guild, message.channel)
        try:
            await channel.send(text, allowed_mentions=discord.AllowedMentions(users=[member], roles=False, everyone=False))
        except (discord.Forbidden, discord.HTTPException):
            LOGGER.warning("Não consegui avisar no chat geral sobre %s.", member.id)

    async def _log(self, message: Any, score: int, reasons: list[str], verdict: dict[str, Any] | None,
                   *, actions: list[str], case_id: str | None, existing: Any = None, cleared: bool = False) -> None:
        channel = self.bot.get_channel(self.log_channel_id) if self.log_channel_id else None
        if channel is None and existing is None:
            return
        member = message.author
        created = getattr(member, "created_at", None)
        joined = getattr(member, "joined_at", None)
        now = datetime.now(timezone.utc)
        title = "🚫 Conteúdo sexual bloqueado" if verdict else "⚠️ Mensagem suspeita (não punida)"
        if cleared:
            title = "✅ Falso alarme: mensagem devolvida"
        if verdict and verdict.get("category") == "menores":
            title = "🚨 URGENTE: conteúdo sexual envolvendo menores"
        color = discord.Color.green() if cleared else discord.Color.red() if verdict else discord.Color.orange()
        embed = discord.Embed(title=title, color=color, timestamp=now)
        embed.add_field(name="Quem", value=f"{member.mention} ({member})\nID `{member.id}`", inline=True)
        embed.add_field(
            name="Conta",
            value=(f"criada há {(now - created).days} dias" if created else "?") + "\n" + (f"no servidor há {(now - joined).days} dias" if joined else ""),
            inline=True,
        )
        embed.add_field(name="Canal", value=getattr(message.channel, "mention", "?"), inline=True)
        embed.add_field(name="Mensagem (ocultada)", value=f"||{(message.content or '')[:900]}||", inline=False)
        if verdict:
            embed.add_field(name="Análise da IA", value=(
                f"**Categoria:** {verdict.get('category', '?')}\n**Tema:** {verdict.get('theme', '?')}\n"
                f"**Refere-se a:** {verdict.get('refers_to', '?')}\n"
                f"**Gravidade:** {verdict.get('severity', '?')}/5 • **Confiança:** {float(verdict.get('confidence') or 0):.0%}"
            )[:1024], inline=False)
        embed.add_field(name="Filtro local", value=f"{score} pontos: " + "; ".join(reasons)[:900], inline=False)
        embed.add_field(name="O que eu fiz", value="\n".join(f"• {a}" for a in actions)[:1024], inline=False)
        view = None
        if case_id:
            embed.set_footer(text=f"caso {case_id}")
            view = discord.ui.View(timeout=None)
            for action, label, style in (("ban", "Banir", discord.ButtonStyle.danger),
                                         ("unmute", "Tirar o mute", discord.ButtonStyle.success),
                                         ("keep", "Manter o mute", discord.ButtonStyle.secondary)):
                view.add_item(GuardCaseButton(self, action, case_id, label=label, style=style))
        ping = f"<@{self.owner_id}> caso pra você decidir:" if case_id else None
        try:
            if existing is not None:
                await existing.edit(content=ping, embed=embed, view=view)
                if ping:
                    # Edição não notifica: um aviso curto avisa que tem caso esperando.
                    await existing.channel.send(
                        f"<@{self.owner_id}> ⬆️ caso pronto pra decisão.",
                        allowed_mentions=discord.AllowedMentions(users=[discord.Object(self.owner_id)]),
                        delete_after=3600,
                    )
            else:
                await channel.send(
                    content=ping, embed=embed, view=view,
                    allowed_mentions=discord.AllowedMentions(users=[discord.Object(self.owner_id)], roles=False, everyone=False),
                )
        except (discord.Forbidden, discord.HTTPException):
            LOGGER.warning("Não consegui mandar o log do caso %s.", case_id)

    async def decide(self, interaction: discord.Interaction, action: str, case_id: str) -> None:
        user = interaction.user
        perms = getattr(user, "guild_permissions", None)
        if user.id != self.owner_id and not (perms and (perms.ban_members or perms.administrator)):
            await interaction.response.send_message("só o Talisson ou a administração decide esse caso.", ephemeral=True)
            return
        case = (await self.state.get_section(CASES_SECTION)).get(case_id)
        if not isinstance(case, dict):
            await interaction.response.send_message("não achei esse caso.", ephemeral=True)
            return
        if case.get("status") not in {"aguardando decisão"}:
            await interaction.response.send_message(f"esse caso já foi decidido: {case.get('status')}.", ephemeral=True)
            return
        guild = self.bot.get_guild(int(case["guild_id"]))
        member = guild.get_member(int(case["user_id"])) if guild else None
        result = ""
        try:
            if action == "ban":
                target = member or discord.Object(int(case["user_id"]))
                await guild.ban(target, reason=f"Conteúdo sexual (caso {case_id}), decidido por {user}", delete_message_seconds=24 * 3600)
                result = "banido"
            elif action == "unmute":
                if member is not None:
                    await member.timeout(None, reason=f"Caso {case_id} revisado por {user}: liberado")
                result = "mute retirado"
            else:
                result = "mute mantido"
        except (discord.Forbidden, discord.HTTPException) as exc:
            await interaction.response.send_message(f"não consegui: {type(exc).__name__}. confere minhas permissões.", ephemeral=True)
            return
        case.update(status=result, decided_by=user.id, decided_at=datetime.now(timezone.utc).isoformat())
        await self.state.update_section(CASES_SECTION, case_id, case)
        embed = interaction.message.embeds[0] if interaction.message and interaction.message.embeds else None
        if embed is not None:
            embed.add_field(name="Decisão", value=f"{result} por {user.mention}", inline=False)
        await interaction.response.edit_message(embed=embed, view=None)
        await self._decision_log(case, case_id, result, user)

    async def _decision_log(self, case: dict[str, Any], case_id: str, result: str, user: Any) -> None:
        channel = self.bot.get_channel(self.decision_log_channel_id) if self.decision_log_channel_id else None
        if channel is None:
            LOGGER.warning("Canal de logs de punição (%s) não encontrado.", self.decision_log_channel_id)
            return
        verdict = case.get("verdict") or {}
        color = {"banido": discord.Color.dark_red(), "mute retirado": discord.Color.green()}.get(result, discord.Color.orange())
        embed = discord.Embed(title=f"⚖️ Punição: {result}", color=color, timestamp=datetime.now(timezone.utc))
        embed.add_field(name="Membro", value=f"<@{case['user_id']}> ({case.get('user_name', '?')})\nID `{case['user_id']}`", inline=True)
        embed.add_field(name="Decidido por", value=getattr(user, "mention", str(user)), inline=True)
        embed.add_field(name="Canal da mensagem", value=f"<#{case.get('channel_id')}>", inline=True)
        embed.add_field(name="Motivo", value=(
            f"{verdict.get('category', 'conteúdo sexual')}: {verdict.get('theme', '?')}\n"
            f"refere-se a: {verdict.get('refers_to', '?')}"
        )[:1024], inline=False)
        embed.add_field(name="Mensagem (ocultada)", value=f"||{str(case.get('content', ''))[:900]}||", inline=False)
        embed.add_field(name="Ações automáticas antes da decisão", value="\n".join(f"• {a}" for a in case.get("actions", []))[:1024] or "-", inline=False)
        embed.set_footer(text=f"caso {case_id}")
        try:
            await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        except (discord.Forbidden, discord.HTTPException):
            LOGGER.warning("Não consegui mandar o log da decisão do caso %s.", case_id)


class GuardCaseButton(discord.ui.DynamicItem[discord.ui.Button], template=r"bella:sxguard:(?P<action>ban|unmute|keep):(?P<case>[0-9\-]+)"):
    """Botão do log que continua funcionando depois de reiniciar."""

    guard: SexualContentGuard | None = None

    def __init__(self, guard: SexualContentGuard | None, action: str, case_id: str, *, label: str = "", style: discord.ButtonStyle = discord.ButtonStyle.secondary) -> None:
        super().__init__(discord.ui.Button(label=label or action, style=style, custom_id=f"bella:sxguard:{action}:{case_id}"))
        if guard is not None:
            GuardCaseButton.guard = guard
        self.action = action
        self.case_id = case_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str]):
        return cls(None, match["action"], match["case"], label=item.label or "", style=item.style)

    async def callback(self, interaction: discord.Interaction) -> None:
        if GuardCaseButton.guard is None:
            await interaction.response.send_message("a moderação ainda está iniciando, tenta de novo.", ephemeral=True)
            return
        await GuardCaseButton.guard.decide(interaction, self.action, self.case_id)
