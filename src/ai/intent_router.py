from __future__ import annotations

import re
from dataclasses import dataclass
from unicodedata import combining, normalize


@dataclass(frozen=True)
class IntentDecision:
    name: str
    confidence: float
    requires_ai: bool = False
    site_related: bool = False
    server_related: bool = False
    administrative: bool = False


class IntentRouter:
    def classify(self, message: str) -> IntentDecision:
        text = normalize_text(message)
        if not text:
            return IntentDecision("empty", 1.0)

        if _looks_administrative(text):
            name = (
                "moderation"
                if _contains_any(
                    text,
                    "banir",
                    "bane",
                    "expulsar",
                    "expulsa",
                    "silenciar",
                    "silencia",
                    "cargo",
                    "mensagens",
                )
                else "administrative"
            )
            return IntentDecision(name, 0.98, administrative=True)

        if _contains_any(
            text,
            "status do site",
            "status da plataforma",
            "monitoramento",
            "disponibilidade",
            "latencia",
            "health score",
            "intelligence score",
        ):
            return IntentDecision("monitoring", 0.96)

        if _is_identity_question(text):
            return IntentDecision("identity", 0.99)
        if _is_capability_question(text):
            return IntentDecision("capabilities", 0.99)
        if _is_name_memory(text):
            return IntentDecision("short_memory", 0.99)
        if _is_known_meme(text):
            return IntentDecision("meme", 0.99)
        if _is_casual(text):
            return IntentDecision("casual", 0.98)

        if _contains_any(
            text,
            "servidor",
            "comunidade",
            "canal",
            "faq",
            "changelog",
            "roadmap",
            "regras",
            "suporte",
            "onde vejo",
            "onde encontro",
        ):
            return IntentDecision(
                "server_question",
                0.94,
                server_related=True,
            )

        if _contains_any(
            text,
            "talisson",
            "talinho",
            "site",
            "portfolio",
            "projeto",
            "empresa",
            "cheetah",
            "instagram",
            "linkedin",
            "github",
            "whatsapp",
            "contato",
            "servico",
            "experiencia",
        ):
            return IntentDecision(
                "site_question",
                0.94,
                site_related=True,
            )

        if _contains_any(
            text,
            "preciso de ajuda",
            "me ajuda",
            "como resolvo",
            "deu erro",
            "nao funciona",
            "problema",
            "duvida",
        ):
            return IntentDecision("support", 0.92, requires_ai=True)

        if _contains_any(
            text,
            "feedback",
            "sugestao",
            "gostei",
            "nao gostei",
            "poderia melhorar",
        ):
            return IntentDecision("feedback", 0.90, requires_ai=True)

        if "?" in message or _starts_as_question(text):
            return IntentDecision("general_question", 0.86, requires_ai=True)
        return IntentDecision("conversation", 0.78, requires_ai=True)


def normalize_text(value: str) -> str:
    decomposed = normalize("NFKD", value.casefold())
    ascii_text = "".join(char for char in decomposed if not combining(char))
    return " ".join(ascii_text.split())


def _looks_administrative(text: str) -> bool:
    if re.search(
        r"\b(?:apaga|apague|apagar|limpa|limpe|limpar|remove|remova|remover)"
        r"\b(?:\s+(?:as?|os?|ultimas?|últimas?|ultimos?|últimos?))?"
        r"(?:\s+\d{1,4})?\s+mensagens?\b",
        text,
    ):
        return True
    action = _contains_any(
        text,
        "dar cargo",
        "da o cargo",
        "de o cargo",
        "adiciona o cargo",
        "remove o cargo",
        "retira o cargo",
        "banir",
        "bane",
        "expulsar",
        "expulsa",
        "silenciar",
        "silencia",
        "remover timeout",
        "remove timeout",
        "desconecta",
        "desconecte",
        "limpa as",
        "limpe as",
        "limpa este",
        "limpe este",
        "apaga as",
        "apague as",
        "remova as ultimas",
        "remova as últimas",
        "tranque",
        "tranca",
        "destranque",
        "destranca",
        "cria um evento",
        "crie um evento",
        "envia um aviso",
        "envie um aviso",
        "configura o canal",
        "define o canal",
        "sincroniza os feedbacks",
        "atualiza o conhecimento",
        "verifica as alteracoes",
        "envia o relatorio",
    )
    return action or bool(
        re.search(
            r"\b(?:ban|kick|timeout|purge)\b",
            text,
        )
    )


def _is_identity_question(text: str) -> bool:
    return _contains_any(
        text,
        "quem e voce",
        "quem voce e",
        "se apresente",
        "qual e sua identidade",
        "qual sua nacionalidade",
        "qual e sua nacionalidade",
        "voce e brasileiro",
        "de qual pais voce e",
        "torce pra qual selecao",
        "torce para qual selecao",
        "qual e sua selecao",
    )


def _is_capability_question(text: str) -> bool:
    return _contains_any(
        text,
        "o que voce faz",
        "como voce ajuda",
        "no que voce pode ajudar",
        "quais suas funcoes",
        "para que voce serve",
    )


def _is_name_memory(text: str) -> bool:
    return bool(
        re.search(r"\bmeu nome (?:e|eh)\b", text)
        or _contains_any(
            text,
            "qual e meu nome",
            "lembra meu nome",
            "como eu me chamo",
        )
    )


def _is_known_meme(text: str) -> bool:
    return _contains_any(
        text,
        "o que sobra pro beta",
        "o que sobra para o beta",
        "gain ou loss",
        "farialimabets aprova",
        "o pai ta on",
        "f no chat",
        "sigma",
        "based",
        "tankou",
        "nao tankou",
        "six seven",
        "mega brain",
        "top chief",
        "bella acorda",
        "acorda bella",
        "jarvis acorda",
        "acorda jarvis",
        "meu brodi",
        "meu nobre",
        "meu parceiro",
    )


def _is_casual(text: str) -> bool:
    return text.strip(" ?!.,") in {
        "oi",
        "ola",
        "opa",
        "e ai",
        "bom dia",
        "boa tarde",
        "boa noite",
        "bao demais",
        "bao demaize",
        "bom demais",
        "tudo bem",
        "tudo certo",
        "como voce esta",
        "valeu",
        "obrigado",
        "obrigada",
        "tmj",
        "beleza",
        "salve",
        "ta ai",
        "iae",
        "eae",
        "fala comigo",
        "acorda",
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
    }


def _starts_as_question(text: str) -> bool:
    return bool(
        re.match(
            r"^(?:como|qual|quais|quem|onde|quando|quanto|por que|porque|"
            r"o que|voce|pode|consegue|existe|tem)\b",
            text,
        )
    )


def _contains_any(text: str, *values: str) -> bool:
    return any(value in text for value in values)
