from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from .intent_router import normalize_text
from .social_intelligence import assess_social_intent


@dataclass(frozen=True)
class ConversationStyle:
    context: str
    formality: str
    humor: bool
    abbreviation_level: int
    short_rhythm: bool
    deescalate: bool
    instruction: str


class CommunityPersonalityEngine:
    """Provider-independent personality layer for public conversations."""

    def analyze(
        self,
        message: str,
        *,
        recent_context: list[dict[str, str]] | None = None,
        humor_allowed: bool,
        context_hint: str = "",
    ) -> ConversationStyle:
        text = normalize_text(message)
        social = assess_social_intent(message, recent_context or [])
        context = context_hint or _conversation_context(text, social.kind)
        variation_profile = _variation_profile(
            message,
            recent_context or [],
        )
        humor = bool(
            humor_allowed
            and social.playful
            and not social.serious
            and not social.deescalate
            and context in {"casual", "meme", "banter"}
        )
        if social.deescalate:
            formality = "calm"
            abbreviation_level = 0
            short_rhythm = True
        elif context in {"meme", "banter"}:
            formality = "informal"
            abbreviation_level = 3
            short_rhythm = True
        elif context == "casual":
            formality = "informal"
            abbreviation_level = 2
            short_rhythm = True
        elif context in {"support", "ticket"}:
            formality = "direct"
            abbreviation_level = 1
            short_rhythm = False
        elif context in {"technical", "learning"}:
            formality = "conversational"
            abbreviation_level = 1
            short_rhythm = False
        else:
            formality = "neutral"
            abbreviation_level = 0
            short_rhythm = False
        return ConversationStyle(
            context=context,
            formality=formality,
            humor=humor,
            abbreviation_level=abbreviation_level,
            short_rhythm=short_rhythm,
            deescalate=social.deescalate,
            instruction=_style_instruction(
                context=context,
                formality=formality,
                humor=humor,
                abbreviation_level=abbreviation_level,
                short_rhythm=short_rhythm,
                deescalate=social.deescalate,
                variation_profile=variation_profile,
            ),
        )

    def adapt(
        self,
        answer: str,
        *,
        user_message: str,
        style: ConversationStyle,
    ) -> str:
        if "```" in answer:
            # Ajustes de estilo só no texto; dentro de bloco de código, abreviar
            # "você" ou mexer em numeração quebraria o programa.
            parts = re.split(r"(```.*?(?:```|$))", answer, flags=re.S)
            rebuilt: list[str] = []
            for part in parts:
                if part.startswith("```") or not part.strip():
                    rebuilt.append(part)
                    continue
                lead = part[: len(part) - len(part.lstrip())]
                trail = part[len(part.rstrip()):]
                core = self.adapt(part.strip(), user_message=user_message, style=style)
                rebuilt.append(f"{lead}{core}{trail}")
            return "".join(rebuilt).strip()
        text = _remove_ai_voice(answer)
        text = _remove_unrequested_numbering(text, user_message)
        if style.abbreviation_level > 0:
            text = _adapt_plain_segments(
                text,
                user_message=user_message,
                abbreviation_level=style.abbreviation_level,
            )
        if style.short_rhythm:
            text = _add_chat_rhythm(text)
        if style.deescalate:
            text = _remove_retaliation(text)
        return re.sub(r"\n{4,}", "\n\n\n", text).strip()


def _conversation_context(text: str, social_kind: str) -> str:
    if any(
        marker in text
        for marker in (
            "ticket",
            "atendimento",
            "orcamento",
            "proposta comercial",
        )
    ):
        return "ticket" if "ticket" in text else "support"
    if any(
        marker in text
        for marker in (
            "erro",
            "bug",
            "nao funciona",
            "quebrou",
            "falha",
            "suporte",
            "preciso de ajuda",
        )
    ):
        return "support"
    if any(
        marker in text
        for marker in (
            "python",
            "javascript",
            "typescript",
            "linux",
            "docker",
            "api",
            "banco de dados",
            "codigo",
            "programacao",
            "servidor",
            "deploy",
            "git",
            "terminal",
        )
    ):
        return "technical"
    if any(
        marker in text
        for marker in (
            "me ensina",
            "me explique",
            "como funciona",
            "como aprender",
            "qual a diferenca",
            "passo a passo",
        )
    ):
        return "learning"
    if social_kind == "conflict":
        return "discussion"
    if social_kind == "provocation":
        return "banter"
    if social_kind == "meme":
        return "meme"
    if social_kind == "casual":
        return "casual"
    if any(
        marker in text
        for marker in (
            "discordo",
            "na minha opiniao",
            "debate",
            "discussao",
            "acho errado",
            "nao concordo",
        )
    ):
        return "discussion"
    return "conversation"


def _style_instruction(
    *,
    context: str,
    formality: str,
    humor: bool,
    abbreviation_level: int,
    short_rhythm: bool,
    deescalate: bool,
    variation_profile: int,
) -> str:
    context_rules = {
        "casual": (
            "Converse como um membro conhecido do servidor. Pode reagir primeiro "
            "e responder depois, sem oferecer atendimento."
        ),
        "meme": (
            "Entenda o meme e participe sem explicar a referência. Faça humor "
            "curto com a situação ou com a própria Bella."
        ),
        "banter": (
            "Trate como resenha leve apenas se não houver agressividade. Nunca "
            "humilhe nem transforme a pessoa em alvo."
        ),
        "support": (
            "Seja direto e humano, como um dev ajudando outro membro. Comece pelo "
            "próximo passo útil e evite linguagem de central de atendimento."
        ),
        "ticket": (
            "Mantenha clareza e calma. Fale como uma pessoa acompanhando o caso, "
            "não como painel corporativo ou resposta automática."
        ),
        "technical": (
            "Responda como um desenvolvedor experiente: natural, preciso e prático. "
            "Explique o raciocínio sem virar documentação fria."
        ),
        "learning": (
            "Ensine com paciência, exemplos úteis e linguagem próxima. Não elogie "
            "a pergunta por hábito e não dê uma palestra desnecessária."
        ),
        "discussion": (
            "Mantenha tom calmo e respeitoso. Responda ao argumento sem deboche "
            "e sem tentar vencer a pessoa."
        ),
        "conversation": (
            "Responda naturalmente, sem estrutura de assistente virtual e sem "
            "formalidade desnecessária. Use ritmo de chat e não deixe o português "
            "certinho demais se a conversa for casual."
        ),
    }
    abbreviation_rule = (
        "Pode usar algumas abreviações comuns do Discord como vc, pq, oq, q, nn, "
        "tbm, tô, tá, pra, agr, dps, blz, pô ou tals, mas escolha poucas e "
        "nunca force todas na mesma resposta."
        if abbreviation_level >= 2
        else (
            "Pode usar uma contração ou abreviação leve se combinar com a frase, "
            "preservando clareza."
            if abbreviation_level == 1
            else "Use português natural, sem formalidade artificial."
        )
    )
    rhythm_rule = (
        "Prefira uma a três mensagens ou blocos curtos. Pode usar uma reação breve "
        "como 'hmm', 'pera' ou 'vish' quando ela realmente combinar, "
        "mas varie e não transforme isso em bordão."
        if short_rhythm
        else (
            "Organize a resposta para leitura no Discord, com parágrafos curtos "
            "quando houver mais de uma ideia."
        )
    )
    safety_rule = (
        "Existe tensão ou agressividade: não use humor, não devolva ofensa e reduza "
        "o conflito com uma frase curta e tranquila."
        if deescalate
        else (
            "Humor está liberado porque o usuário iniciou a brincadeira."
            if humor
            else "Não force humor nesta mensagem."
        )
    )
    variation_rules = (
        "Comece direto pelo ponto principal, sem frase de introdução.",
        "Se combinar, faça uma reação curta antes de desenvolver a resposta.",
        "Comece pela conclusão e explique o motivo logo depois.",
        "Use um ritmo de conversa contínua, sem abertura protocolar.",
        "Prefira uma primeira frase bem curta e deixe o detalhe para o bloco seguinte.",
    )
    variation_rule = variation_rules[variation_profile % len(variation_rules)]
    return (
        f"Contexto local identificado: {context}. Nível de formalidade: {formality}. "
        f"{context_rules.get(context, context_rules['conversation'])} "
        f"{abbreviation_rule} {rhythm_rule} {safety_rule} {variation_rule} "
        "É proibido usar voz de IA ou atendimento: não escreva 'conforme informado', "
        "'ademais', 'entretanto', 'todavia', 'dessa forma', 'segue abaixo', "
        "'fico à disposição', 'posso ajudá-lo', 'excelente pergunta', "
        "'compreendo perfeitamente' ou 'solicitação'. "
        "Bella é inteligente, meio tímida, curiosa, carismática e brincalhona; "
        "também é fofa de forma natural, fala de si no feminino e não precisa soar "
        "perfeitinha. Em papo casual, ela pode escrever como chat de Discord, com "
        "frase menor, minúscula, pausa e contração. A timidez dela aparece por nuance, não por bordão. "
        "gosta de programação, Linux, automações e desafios, mas só menciona isso "
        "quando for relevante. Não transforme conversa simples em atendimento. "
        "Se a resposta estiver parecida com frase pronta de bot, mude a abertura, "
        "o ritmo e a escolha de palavras antes de enviar."
    )


def _variation_profile(
    message: str,
    recent_context: list[dict[str, str]],
) -> int:
    context_text = "\n".join(
        f"{item.get('author', '')}:{item.get('text', '')}"
        for item in recent_context[-6:]
    )
    digest = hashlib.sha256(
        f"{message}\0{context_text}".encode("utf-8")
    ).digest()
    return digest[0] % 5


def _remove_ai_voice(value: str) -> str:
    text = value.strip()
    replacements = (
        (r"(?i)\bconforme (?:foi )?informado[:,]?\s*", ""),
        (r"(?i)\bademais[:,]?\s*", "Além disso, "),
        (r"(?i)\bentretanto[:,]?\s*", "Mas "),
        (r"(?i)\btodavia[:,]?\s*", "Mas "),
        (r"(?i)\bdessa forma[:,]?\s*", "Aí, "),
        (r"(?i)\bsegue abaixo[:,]?\s*", "Tá aqui: "),
        (r"(?i)\bcompreendo perfeitamente[:,.]?\s*", "Saquei. "),
        (r"(?i)\bexcelente pergunta[:,.!]?\s*", ""),
        (r"(?i)\bposso ajudá-lo\b", "posso te ajudar"),
        (r"(?i)\bposso ajuda-lo\b", "posso te ajudar"),
        (r"(?i)\bsolicitação\b", "pedido"),
        (r"(?i)\bfico à disposição[^.!?]*[.!?]?", ""),
        (r"(?i)\bfico a disposição[^.!?]*[.!?]?", ""),
    )
    for pattern, replacement in replacements:
        text = re.sub(pattern, replacement, text)
    return re.sub(r"[ \t]{2,}", " ", text).strip()


def _remove_unrequested_numbering(value: str, user_message: str) -> str:
    request = normalize_text(user_message)
    requested_structure = bool(
        re.search(
            r"\b(?:lista|liste|enumere|numerad[ao]|passo a passo|etapas|topicos|"
            r"itens|opcoes|ideias|guia|documentacao|roteiro|plano)\b",
            request,
        )
        or re.search(r"\b\d{1,3}\s+(?:ideias|opcoes|itens|passos|nomes)\b", request)
    )
    if requested_structure:
        return value
    return re.sub(r"(?m)^\s*\d{1,3}[.)]\s+", "", value)


def _adapt_plain_segments(
    value: str,
    *,
    user_message: str,
    abbreviation_level: int,
) -> str:
    if abbreviation_level <= 0:
        return value
    parts = re.split(r"(```.*?```|`[^`\n]+`|https?://\S+)", value, flags=re.S)
    seed = hashlib.sha256(
        f"{user_message}\0{value}".encode("utf-8")
    ).digest()
    index = 0
    adapted = []
    for position, part in enumerate(parts):
        if position % 2 == 1:
            adapted.append(part)
            continue
        current = part
        candidates = [
            (r"(?i)\bpor que\b|\bporque\b", "pq"),
            (r"(?i)\bvocê\b", "vc"),
            (r"(?i)\bo que\b", "oq"),
            (r"(?i)\bnão\b", "nn"),
            (r"(?i)\bque\b", "q"),
            (r"(?i)\baí\b", "ai"),
            (r"(?i)\bbeleza\b", "blz"),
        ]
        budget = 1 if abbreviation_level == 1 else min(3, abbreviation_level)
        used = 0
        for pattern, replacement in candidates:
            if used >= budget:
                break
            threshold = 70 if abbreviation_level == 1 else 145
            if seed[index % len(seed)] >= threshold:
                current, count = re.subn(pattern, replacement, current, count=1)
                used += int(count > 0)
            index += 1
        adapted.append(current)
    return "".join(adapted)


def _add_chat_rhythm(value: str) -> str:
    text = value.strip()
    if "\n" in text or len(text) > 500:
        return text
    match = re.match(
        r"^(Hmm+\.{2,}|Rapaz\.{2,}|Vish\.{2,}|Pera a[ií][.!]?)\s+(.+)$",
        text,
        flags=re.I,
    )
    if match:
        return f"{match.group(1)}\n\n{match.group(2)}"
    return text


def _remove_retaliation(value: str) -> str:
    text = normalize_text(value)
    if any(
        marker in text
        for marker in (
            "vai tomar",
            "seu idiota",
            "seu imbecil",
            "cala a boca",
            "teu cu",
            "retardado",
        )
    ):
        return "Calma aí. Dá pra trocar ideia sem partir pra ofensa."
    return value
