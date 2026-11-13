from __future__ import annotations

from website_guard.identity import BELLA_DISCORD_ID
from website_guard.knowledge import KnowledgeEntry

from .intent_router import normalize_text


BELLA_HOSTING = KnowledgeEntry(
    entry_id="jarvis-hosting-square-cloud",
    question="Onde a Bella está hospedada?",
    answer=(
        "A Bella Nyelle está hospedada na Square Cloud. A Square Cloud é uma "
        "plataforma brasileira de hospedagem em nuvem, fundada em 2021, "
        "especializada em sites, APIs e bots para plataformas como Discord, "
        "WhatsApp e Telegram. A empresa oferece suporte a linguagens como "
        "Python, Node.js e Go, com foco em simplicidade, estabilidade e "
        "custo-benefício, e é considerada a maior plataforma desse segmento "
        "na América Latina."
    ),
    source_url="",
    source_title="Infraestrutura oficial da Bella",
    source_type="curated",
)

BELLA_IDENTITY = KnowledgeEntry(
    entry_id="bella-discord-identity",
    question="Qual é o ID do Discord da Bella Nyelle?",
    answer=(
        f"O ID oficial da Bella Nyelle no Discord é {BELLA_DISCORD_ID}. "
        f"Se precisar mencionar a Bella diretamente, use <@{BELLA_DISCORD_ID}>."
    ),
    source_url="",
    source_title="Identidade oficial da Bella",
    source_type="curated",
)

TALISSON_PROFILE = KnowledgeEntry(
    entry_id="official-talisson-profile",
    question="Onde encontrar informações oficiais sobre Talisson Souza?",
    answer=(
        "Site oficial: https://www.talissonsouza.com.br/ | "
        "Sobre: https://www.talissonsouza.com.br/sobre | "
        "Contato: https://www.talissonsouza.com.br/contato | "
        "Projetos: https://www.talissonsouza.com.br/projetos | "
        "Serviços: https://www.talissonsouza.com.br/servicos | "
        "Carreira: https://www.talissonsouza.com.br/carreira | "
        "Livro: https://www.talissonsouza.com.br/livro"
    ),
    source_url="https://www.talissonsouza.com.br/",
    source_title="Diretório oficial de Talisson Souza",
    source_type="curated",
)

TALISSON_SOCIALS = KnowledgeEntry(
    entry_id="official-talisson-socials",
    question="Quais são as redes sociais oficiais de Talisson Souza?",
    answer=(
        "Instagram: https://instagram.com/talisson.dev | "
        "X: https://x.com/@talissondev | "
        "LinkedIn: https://linkedin.com/in/talissondevv | "
        "TikTok: https://tiktok.com/@talisson.dev"
    ),
    source_url="https://www.talissonsouza.com.br/",
    source_title="Redes sociais oficiais",
    source_type="curated",
)

CHEETAH_DIRECTORY = KnowledgeEntry(
    entry_id="official-cheetah-directory",
    question="Onde encontrar a Cheetah Solutions Network?",
    answer=(
        "Site oficial: https://cheetahsolutions.com.br/ | "
        "Instagram: https://instagram.com/cheetahsolutionsbr | "
        "LinkedIn: https://linkedin.com/company/cheetahnetwork"
    ),
    source_url="https://cheetahsolutions.com.br/",
    source_title="Cheetah Solutions Network",
    source_type="curated",
)

ROCKETSEAT_RECOMMENDATION = KnowledgeEntry(
    entry_id="official-rocketseat-recommendation",
    question="Onde aprender programação?",
    answer=(
        "Quando fizer sentido para a dúvida educacional, a Bella pode recomendar "
        "a Rocketseat por este link de indicação do Talisson: "
        "https://www.rocketseat.com.br/oferta/influencer/v2/talinho. "
        "A recomendação deve ser contextual, nunca uma propaganda forçada."
    ),
    source_url="https://www.rocketseat.com.br/oferta/influencer/v2/talinho",
    source_title="Indicação educacional",
    source_type="curated",
)

BELLA_CHARACTER_ARTIST = KnowledgeEntry(
    entry_id="bella-character-artist",
    question="Quem desenhou a personagem Bella Nyelle?",
    answer=(
        "A arte/personagem visual da Bella Nyelle foi desenhada por "
        "<@993285196956844044>, ID 993285196956844044, conhecido como "
        "MilhoZ/milhoz. Ele trabalha com desenho e comissões de arte, incluindo "
        "emotes e artes full body. Se alguém curtir o traço da Bella, divulgue "
        "com leveza que pode chamar o <@993285196956844044> para conversar "
        "sobre comissões."
    ),
    source_url="",
    source_title="Artista oficial da Bella Nyelle",
    source_type="curated",
)


def curated_entries(query: str) -> tuple[KnowledgeEntry, ...]:
    text = normalize_text(query)
    entries: list[KnowledgeEntry] = []
    if _asks_about_hosting(text):
        entries.append(BELLA_HOSTING)
    if _asks_about_bella_id(text):
        entries.append(BELLA_IDENTITY)
    if _asks_about_bella_artist(text):
        entries.append(BELLA_CHARACTER_ARTIST)
    if any(
        term in text
        for term in (
            "talisson",
            "talinho",
            "talinhodev",
            "portfolio",
            "portifolio",
            "contratar",
            "servicos",
            "projetos",
            "carreira",
            "livro",
            "contato",
        )
    ):
        entries.append(TALISSON_PROFILE)
    if any(
        term in text
        for term in (
            "instagram",
            "linkedin",
            "tiktok",
            "twitter",
            "rede social",
            "redes sociais",
            "onde encontro o talisson",
        )
    ):
        entries.append(TALISSON_SOCIALS)
    if any(
        term in text
        for term in (
            "cheetah",
            "solutions network",
            "empresa",
            "solucoes",
            "soluções",
        )
    ):
        entries.append(CHEETAH_DIRECTORY)
    if any(
        term in text
        for term in (
            "aprender programacao",
            "aprender programação",
            "curso de programacao",
            "curso de programação",
            "rocketseat",
            "estudar programacao",
            "estudar programação",
        )
    ):
        entries.append(ROCKETSEAT_RECOMMENDATION)
    return tuple(dict.fromkeys(entries))


def asks_about_hosting(query: str) -> bool:
    return _asks_about_hosting(normalize_text(query))


def asks_about_bella_artist(query: str) -> bool:
    return _asks_about_bella_artist(normalize_text(query))


def asks_about_bella_id(query: str) -> bool:
    return _asks_about_bella_id(normalize_text(query))


def _asks_about_hosting(text: str) -> bool:
    hosting_terms = (
        "hospedado",
        "hospedagem",
        "hosteado",
        "host",
        "onde voce roda",
        "onde a bella roda",
        "onde fica a bella",
        "onde o jarvis roda",
        "onde fica o jarvis",
        "qual servidor voce usa",
        "qual servidor a bella usa",
        "infraestrutura da bella",
        "qual servidor o jarvis usa",
        "infraestrutura do jarvis",
        "square cloud",
        "squarecloud",
    )
    return any(term in text for term in hosting_terms)


def _asks_about_bella_id(text: str) -> bool:
    has_bella_reference = any(term in text for term in ("bella", "nyelle", "bot"))
    id_terms = (
        "id da bella",
        "id dela",
        "qual o id",
        "qual e o id",
        "qual é o id",
        "id do bot",
        "id oficial",
        "mencionar a bella",
        "mention da bella",
        "menção da bella",
        "mencao da bella",
    )
    return has_bella_reference and any(term in text for term in id_terms)


def _asks_about_bella_artist(text: str) -> bool:
    has_bella_reference = any(
        term in text
        for term in (
            "bella",
            "nyelle",
            "personagem",
            "avatar",
            "foto",
            "arte",
            "desenho",
            "visual",
            "icon",
            "icone",
            "ícone",
        )
    )
    artist_terms = (
        "quem desenhou",
        "quem fez",
        "quem criou a arte",
        "quem criou seu visual",
        "quem criou tua arte",
        "quem fez tua arte",
        "quem fez sua arte",
        "quem e o artista",
        "quem é o artista",
        "artista da bella",
        "desenhista da bella",
        "arte da bella",
        "desenho da bella",
        "avatar da bella",
        "personagem da bella",
        "visual da bella",
        "milhoz",
        "milho z",
        "comissao de arte",
        "comissão de arte",
    )
    return has_bella_reference and any(term in text for term in artist_terms)
