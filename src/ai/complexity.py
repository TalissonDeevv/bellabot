from __future__ import annotations

import re
from dataclasses import dataclass

from .intent_router import normalize_text
from .social_intelligence import assess_social_intent


@dataclass(frozen=True)
class GenerationMode:
    complex: bool
    social: bool
    response_size: str
    model_policy: str
    max_output_tokens: int
    max_response_chars: int
    timeout_seconds: float
    use_cache: bool
    instruction: str
    task: bool = False


def assess_generation_mode(
    message: str,
    *,
    humor_allowed: bool = True,
) -> GenerationMode:
    text = normalize_text(message)
    if looks_like_task_request(text):
        # "cria um código de um bot" tem poucas palavras e caía em conversa
        # curta (88 tokens, sem histórico): a Bella só fazia perguntas.
        return task_generation_mode()
    assessment = assess_social_intent(message)
    looks_social = _looks_social(message, text)
    social = humor_allowed and looks_social and not assessment.serious
    score = _complexity_score(message, text)
    complex_request = score >= 3
    if social:
        return GenerationMode(
            complex=False,
            social=True,
            response_size="short",
            model_policy="economy",
            max_output_tokens=96,
            max_response_chars=500,
            timeout_seconds=3.5,
            use_cache=False,
            instruction=(
                "Resposta curta de conversa: use uma ou duas frases breves, como uma "
                "pessoa responderia no Discord. Não explique além do que foi pedido, "
                "não transforme a interação em textão e não use frase de atendimento."
            ),
        )
    if looks_social and not humor_allowed:
        return GenerationMode(
            complex=False,
            social=False,
            response_size="short",
            model_policy="economy",
            max_output_tokens=140,
            max_response_chars=900,
            timeout_seconds=3.8,
            use_cache=True,
            instruction=(
                "Este canal não permite modo de zoeira. Responda com naturalidade, "
                "mas sem piadas, provocações, CAPS, memes ou deboche. Se houver "
                "agressividade, reduza a tensão sem devolver a ofensa. Seja breve."
            ),
        )
    if complex_request:
        requested_items = _requested_item_count(text)
        target_chars = 60000 if requested_items >= 40 else 36000
        tokens = 5600 if requested_items >= 40 else 3600
        return GenerationMode(
            complex=True,
            social=False,
            response_size="long",
            model_policy="advanced",
            max_output_tokens=tokens,
            max_response_chars=target_chars,
            timeout_seconds=18.0,
            use_cache=True,
            instruction=(
                "Modo complexo: antes de responder, identifique o objetivo, a "
                "quantidade de itens, o formato e o tom pedido. Entregue a "
                "estrutura solicitada integralmente, sem reduzir listas extensas. "
                "Use títulos, subtítulos, listas e quebras de linha claras."
            ),
        )
    if _wants_explanation(text):
        return GenerationMode(
            complex=True,
            social=False,
            response_size="detailed",
            model_policy="balanced",
            max_output_tokens=1800,
            max_response_chars=14000,
            timeout_seconds=9.0,
            use_cache=True,
            instruction=(
                "Modo explicativo: o usuário demonstrou que quer aprender ou pediu "
                "detalhes. Desenvolva o assunto no nível necessário e prefira "
                "parágrafos naturais. Só use numeração quando o usuário pedir lista, "
                "etapas, ranking ou passo a passo. Respeite o formato solicitado."
            ),
        )
    if _looks_simple_request(message, text):
        return GenerationMode(
            complex=False,
            social=False,
            response_size="concise",
            model_policy="economy",
            max_output_tokens=280,
            max_response_chars=1800,
            timeout_seconds=3.8,
            use_cache=True,
            instruction=(
                "Resposta objetiva: entregue somente o necessário em poucas linhas. "
                "Se uma frase resolver, use uma frase. Não acrescente contexto, "
                "explicações, listas ou conclusões que a pessoa não pediu."
            ),
        )
    return GenerationMode(
        complex=False,
        social=False,
        response_size="standard",
        model_policy="balanced",
        max_output_tokens=600,
        max_response_chars=5000,
        timeout_seconds=4.5,
        use_cache=True,
        instruction=(
            "Modo padrão: responda diretamente e use apenas o espaço necessário. "
            "Não gere textão sem pedido explícito. Use parágrafos curtos quando houver "
            "mais de uma ideia."
        ),
    )


def _complexity_score(message: str, text: str) -> int:
    score = 0
    if len(message) >= 380:
        score += 1
    if len(message) >= 800:
        score += 1
    if len(re.findall(r"^\s*(?:[-*•]|\d+[.)])\s+", message, flags=re.M)) >= 3:
        score += 2
    if _requested_item_count(text) >= 15:
        score += 3
    if _requested_item_count(text) >= 40:
        score += 2
    if len(re.findall(r"\b(?:e|tambem|também|alem disso|além disso|inclua|com|sem)\b", text)) >= 6:
        score += 1
    if any(
        term in text
        for term in (
            "guia completo",
            "documentacao",
            "documentação",
            "planejamento",
            "plano completo",
            "analise",
            "análise",
            "comparacao",
            "comparação",
            "brainstorm",
            "artigo",
            "manual",
            "roteiro",
            "passo a passo",
            "explique detalhadamente",
        )
    ):
        score += 2
    return score


def _requested_item_count(text: str) -> int:
    matches = re.findall(
        r"\b(?:crie|gere|liste|faça|faca|mande|me de|me dê|produza)?\s*(\d{1,3})\s+"
        r"(?:nomes|opcoes|opções|ideias|exemplos|topicos|tópicos|itens|frases|sugestoes|sugestões)\b",
        text,
    )
    values = [int(item) for item in matches if item.isdigit()]
    return max(values) if values else 0


def _wants_explanation(text: str) -> bool:
    return any(
        term in text
        for term in (
            "me explica",
            "me explique",
            "explique detalhadamente",
            "como funciona",
            "quero aprender",
            "me ensina",
            "me ensine",
            "faca um guia",
            "faça um guia",
            "guia completo",
            "crie uma documentacao",
            "crie uma documentação",
            "faca uma documentacao",
            "faça uma documentação",
            "detalhe isso",
            "passo a passo",
        )
    )


def _looks_simple_request(message: str, text: str) -> bool:
    if len(message) > 320 or _requested_item_count(text) >= 8:
        return False
    if re.search(r"^\s*(?:[-*•]|\d+[.)])\s+", message, flags=re.M):
        return False
    if _looks_informational(text):
        return True
    return bool(
        "?" in message
        or re.match(
            r"^(?:o que|quem|como|onde|quando|quanto|por que|porque|qual|quais|"
            r"pode|consegue|tem|existe)\b",
            text,
        )
    )


_TASK_VERBS = re.compile(
    r"\b(?:cri[ae]r?|cria|crie|faz(?:er)?|fa[cç]a|escrev[ae]r?|ger[ae]r?|mont[ae]r?|"
    r"program[ae]r?|desenvolv[ae]r?|codific[ae]r?|traduz(?:ir)?|traduza|corrig[ei]r?|"
    r"corrija|revis[ae]r?|refator[ae]r?|otimiz[ae]r?|implement[ae]r?|adapt[ae]r?|"
    r"converte[r]?|converta|resum[ae]r?|list[ae]r?|calcul[ae]r?|"
    r"me (?:ajuda|ajude) (?:a|com|no|na|nesse|nessa))\b"
)
_TASK_OBJECTS = re.compile(
    r"\b(?:codigo|script|bot|funcao|comando|classe|programa|site|pagina|api|"
    r"html|css|javascript|typescript|python|py|java|sql|regex|query|planilha|"
    r"formula|texto|email|mensagem|post|lista|tabela|resumo|traducao|exemplo|"
    r"template|modelo|arquivo|json|yaml|dockerfile|readme)\b"
)


# Dois níveis, para não recusar perguntas normais ("a função do canal", "o bot
# caiu", "preciso falar do bot"):
# - verbo de CRIAR + algo programável (bot, site, função...);
# - verbo de PEDIR + algo que só pode ser código (código, script, html...).
_CODE_CREATE_VERBS = re.compile(
    r"\b(?:cri[ae]r?|cria|crie|faz(?:er)?|fa[cç]a|escrev[ae]r?|ger[ae]r?|mont[ae]r?|"
    r"program[ae]r?|desenvolv[ae]r?|codific[ae]r?|implement[ae]r?)\b"
)
_CODE_BUILDABLE = re.compile(
    r"\b(?:codigo|codigos|script|scripts|bot|funcao|programa|site|pagina web|"
    r"html|css|javascript|typescript|python|java|sql|regex|query|dockerfile|"
    r"algoritmo|snippet|comando slash|slash command|api)\b"
)
_CODE_ASK_VERBS = re.compile(
    r"\b(?:quero|preciso|me (?:passa|passe|manda|mande|envia|envie)|"
    r"corrig[ei]r?|corrija|refator[ae]r?|complet[ae]|termin[ae])\b"
)
_CODE_ONLY = re.compile(
    r"\b(?:codigo|codigos|script|scripts|html|css|javascript|typescript|sql|regex|"
    r"dockerfile|snippet|trecho de codigo)\b"
)


def looks_like_code_request(text: str) -> bool:
    """Pedido para a Bella escrever código, o que ela não faz no chat."""
    normalized = normalize_text(text)
    if _CODE_CREATE_VERBS.search(normalized) and _CODE_BUILDABLE.search(normalized):
        return True
    return bool(_CODE_ASK_VERBS.search(normalized) and _CODE_ONLY.search(normalized))


def looks_like_task_request(text: str) -> bool:
    """Pedido para produzir algo (código, texto, lista...), não papo casual."""
    normalized = normalize_text(text)
    if "```" in text:
        return True
    return bool(_TASK_VERBS.search(normalized) and _TASK_OBJECTS.search(normalized))


def task_generation_mode() -> GenerationMode:
    return GenerationMode(
        complex=False,
        social=False,
        response_size="standard",
        model_policy="balanced",
        max_output_tokens=1500,
        max_response_chars=6000,
        timeout_seconds=15.0,
        use_cache=False,
        instruction=(
            "Modo tarefa: faça agora o que foi pedido, usando o que já foi dito na "
            "conversa. Não pergunte de novo algo que a pessoa já respondeu. Se faltar "
            "um detalhe, assuma um padrão razoável, diga qual assumiu em uma linha e "
            "entregue. Você não escreve código: se o pedido for código, diga isso em "
            "uma frase. Só pergunte se for impossível fazer sem a resposta, e no "
            "máximo uma vez. Se não conseguir fazer, diga isso em uma frase, sem enrolar."
        ),
        task=True,
    )


def _looks_social(message: str, text: str) -> bool:
    if is_playful_tease(message):
        return True
    if _looks_personal_bella_chat(text):
        return True
    if _professional_context(text):
        return False
    compact = text.strip(" ?!.,")
    if compact in {
        "oi",
        "ola",
        "olá",
        "opa",
        "e ai",
        "e aí",
        "salve",
        "ta ai",
        "tá ai",
        "ta aí",
        "tá aí",
        "iae",
        "eae",
        "bom dia",
        "boa tarde",
        "boa noite",
        "bao demais",
        "bao demaize",
        "bom demais",
        "suave",
        "tranquilo",
        "de boa",
        "firmeza",
        "fala tu",
        "coe",
        "qual foi",
        "tamo junto",
        "e nois",
        "bora",
        "dale",
        "brabo",
        "massa",
        "top",
    }:
        return True
    if any(
        term in text
        for term in (
            "meu brodi",
            "meu nobre",
            "meu parceiro",
            "bella acorda",
            "acorda bella",
            "jarvis acorda",
            "acorda jarvis",
            "mega brain",
            "top chief",
            "six seven",
            "gain ou loss",
            "o pai ta on",
            "o pai tá on",
            "farialimabets",
            "kkkk",
            "f no chat",
            "tankou",
            "bao demais",
            "bao demaize",
            "bom demais",
            "tamo junto",
            "e nois",
            "papo reto",
            "so dale",
            "só dale",
            "o que acha disso",
            "oque acha disso",
            "que acha disso",
            "e isso ai",
            "e isso aí",
            "olha isso",
            "que foi isso",
        )
    ):
        return True
    if re.fullmatch(r"(?:k|ha|hue){3,}", compact):
        return True
    letters = [char for char in message if char.isalpha()]
    if len(letters) >= 5:
        upper_ratio = sum(char.isupper() for char in letters) / len(letters)
        if upper_ratio >= 0.72 and len(text.split()) <= 10:
            return True
    if (
        len(message) <= 180
        and len(text.split()) <= 14
        and not _looks_informational(text)
        and not re.search(r"https?://|```|`[^`]+`", message)
    ):
        return True
    return False


def _looks_personal_bella_chat(text: str) -> bool:
    if not text:
        return False
    asks_bella = any(
        marker in text
        for marker in (
            "bella",
            "voce",
            "você",
            "vc",
            "tu",
        )
    )
    personal_markers = (
        "tudo bem",
        "tudo bom",
        "como ta",
        "como tá",
        "como esta",
        "como está",
        "ta bem",
        "tá bem",
        "ta boa",
        "tá boa",
        "ta bão",
        "ta bao",
        "tá bão",
        "vc e timida",
        "vc é timida",
        "vc é tímida",
        "voce e timida",
        "você é tímida",
        "voce é tímida",
        "quem e vc",
        "quem é vc",
        "quem e voce",
        "quem é você",
        "quem e a bella",
        "quem é a bella",
        "vc e fofa",
        "vc é fofa",
        "você é fofa",
        "voce e fofa",
        "vc sabe programar",
        "você sabe programar",
        "voce sabe programar",
        "oq vc ta fazendo",
        "o que vc ta fazendo",
        "o que você tá fazendo",
        "oq voce ta fazendo",
        "namora comigo",
    )
    return asks_bella and any(marker in text for marker in personal_markers)


def is_playful_tease(message: str) -> bool:
    text = normalize_text(message).strip(" ?!.,")
    if not text:
        return False
    markers = (
        "noiado",
        "noiada",
        "doido",
        "doida",
        "maluco",
        "maluca",
        "lerdo",
        "lerda",
        "chapado",
        "biruta",
        "abestado",
        "besta",
        "tonto",
        "safado",
        "sem vergonha",
        "folgado",
        "emocionado",
        "viajado",
        "viajada",
        "pirado",
        "pirada",
        "bugado",
        "bugada",
    )
    if not any(marker in text for marker in markers):
        return False
    if any(
        term in text
        for term in (
            "ticket",
            "suporte",
            "site",
            "sistema",
            "erro",
            "problema",
            "comando",
            "aplicacao",
            "aplicativo",
        )
    ):
        return False
    direct_target = (
        ("bella" in text or "nyelle" in text or "jarvis" in text)
        or re.search(r"\b(?:tu|voce|vc|ce|seu)\b", text) is not None
        or len(text.split()) <= 2
    )
    return bool(direct_target and len(text.split()) <= 10)


def _looks_informational(text: str) -> bool:
    if any(
        term in text
        for term in (
            "me explique",
            "explique ",
            "como funciona",
            "como fazer",
            "como criar",
            "qual a diferenca",
            "qual a diferença",
            "passo a passo",
            "me ensine",
            "preciso de ajuda",
            "pode me ajudar",
            "consegue explicar",
            "o que significa",
            "resume isso",
            "resuma isso",
            "resuma ",
            "analise isso",
            "analisa isso",
            "avalie isso",
            "conte uma piada",
            "conta uma piada",
            "me conte uma piada",
            "faz uma piada",
            "faca uma piada",
            "faça uma piada",
            "invente uma piada",
            "crie uma piada",
            "conte uma historia",
            "conte uma história",
            "escreva ",
            "crie ",
            "gere ",
            "liste ",
            "invente ",
        )
    ):
        return True
    return bool(
        re.match(
            r"^(?:o que|quem|como|onde|quando|quanto|por que|porque|qual|quais)\b",
            text,
        )
    )


def _professional_context(text: str) -> bool:
    return any(
        term in text
        for term in (
            "ticket",
            "orcamento",
            "orçamento",
            "proposta",
            "projeto",
            "suporte",
            "atendimento",
            "empresa",
            "cheetah",
            "ops framework",
            "formulario",
            "formulário",
            "bug",
            "erro",
            "cargo",
            "banir",
            "limpar",
            "apagar",
            "anuncio",
            "anúncio",
        )
    )
