"""Entrevista de orçamento dentro do ticket "Solicitar Orçamento".

Em vez de chamar a equipe assim que o ticket abre, a Bella conduz a consulta
com o cliente seguindo o formulário de "Solicitação de Desenvolvimento de
Sistema" (e, quando o projeto é um bot de Discord, o formulário de bot).

- Pergunta por partes (uma seção do formulário por mensagem).
- A IA lê a resposta livre, liga cada trecho à pergunta certa e marca o que
  ficou vago. Só "sim"/"não" não vale: a pessoa precisa responder com palavras.
- "A definir" e "Não se aplica" são respostas válidas (como no formulário).
- O progresso fica salvo no próprio registro do ticket (sobrevive a reinício).
- No fim, gera o resumo administrativo para o Talisson e só então chama a equipe.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from unicodedata import combining, normalize

LOGGER = logging.getLogger(__name__)


def _norm(value: str) -> str:
    decomposed = normalize("NFKD", str(value or "").casefold())
    return " ".join("".join(c for c in decomposed if not combining(c)).split())


def is_quote_category(category: str) -> bool:
    return "orcamento" in _norm(category).replace(" ", "")


@dataclass(frozen=True)
class Question:
    id: str
    text: str
    hint: str = ""
    options: tuple[str, ...] = ()
    multi: bool = False


@dataclass(frozen=True)
class Block:
    id: str
    title: str
    questions: tuple[Question, ...]
    discord_bot_only: bool = False
    intro: str = ""


Q = Question
GENERAL_BLOCKS: tuple[Block, ...] = (
    Block("identificacao", "Identificação e tipo de serviço", (
        Q("nome", "Qual é o seu nome?"),
        Q("tipo_servico", "Qual é o tipo principal de serviço que você quer?", options=(
            "Website Institucional", "Website Avançado / SaaS", "Sistema Web", "Sistema Desktop",
            "Aplicação Mobile", "Bot (Discord, Telegram, WhatsApp)", "Integração de APIs",
            "Automação de Processos", "E-commerce", "Outro")),
        Q("estado_atual", "O projeto vai começar do zero ou já existe alguma coisa? Se já existe, conta o que precisa (melhorias, correções, terminar o que começou).",
          options=("Projeto do Zero", "Projeto Parcialmente Iniciado", "Projeto Existe que precisa de Melhorias", "Projeto Existe que precisa de Correções")),
    ), intro="Seção 3 do formulário: informações do projeto."),
    Block("projeto", "Sobre o projeto", (
        Q("descricao", "Descreva o projeto com detalhes: objetivo, qual problema ele resolve e o contexto."),
        Q("referencias", "Tem referências, links ou projetos parecidos que te inspiram? (envie o link completo, https://...)"),
        Q("funcionalidades", "Quais são as funcionalidades principais que você espera?"),
    )),
    Block("requisitos", "Requisitos técnicos", (
        Q("recursos", "Quais recursos você espera no projeto? (ex.: login e usuários, painel administrativo, pagamentos, WhatsApp, APIs externas, relatórios, notificações, segurança avançada, SEO, design responsivo, backup automático)",
          options=("Sistema de login e usuários", "Dashboard administrativo", "Sistema de pagamentos", "Integração com WhatsApp",
                   "Integração com APIs externas", "Banco de dados avançado", "Relatórios", "Notificações", "Segurança avançada",
                   "SEO", "Design responsivo", "Backup automático", "Outro"), multi=True),
        Q("plataforma", "Tem preferência de plataforma? (web, Android, iOS, Windows, Discord...) Se não tiver, diga por quê ou o que espera."),
        Q("hospedagem", "Você já tem hospedagem ou servidor? Me conta qual é, ou se precisa de orientação.",
          options=("Sim", "Não", "Preciso de Orientação")),
    ), intro="Seção 4: requisitos técnicos. São preferências iniciais; a definição final passa pela análise técnica."),
    Block("prazo_orcamento", "Prazo e orçamento", (
        Q("faixa_orcamento", "Qual faixa de investimento você tem prevista? (até R$ 1.000, R$ 1.000 a R$ 2.000, R$ 2.500 a R$ 5.000, R$ 5.000 a R$ 10.000 ou acima de R$ 10.000)",
          options=("Até R$ 1.000", "R$ 1.000 a R$ 2.000", "R$ 2.500 a R$ 5.000", "R$ 5.000 a R$ 10.000", "Acima de R$ 10.000")),
        Q("prazo", "Qual prazo você espera para a entrega? (flexível, até 30 dias, até 15 dias, até 7 dias) Se tiver uma data, me diz o motivo dela.",
          options=("Flexível", "Até 30 dias", "Até 15 dias (urgente)", "Até 7 dias (alta prioridade)")),
    ), intro="Seção 5: valores e prazos aqui são referência inicial, não proposta final. Prazos curtos podem ter custo extra."),
    Block("negocio", "Negócio e operação", (
        Q("processo_atual", "Como funciona hoje o processo que o sistema vai resolver, do começo ao fim?"),
        Q("usuarios_perfis", "Quem vai usar o sistema? (administradores, funcionários, clientes, parceiros, fornecedores, público...)",
          options=("Administradores", "Funcionários", "Clientes", "Parceiro", "Fornecedores", "Público", "Outro"), multi=True),
        Q("usuarios_qtd", "Quantas pessoas devem usar o sistema? (até 10, 50, 100, 500, mais de 500 ou ainda não sabe)",
          options=("Até 10", "Até 50", "Até 100", "Até 500", "Acima de 500", "Ainda não sei")),
        Q("niveis_acesso", "Vai ter níveis de acesso diferentes? Quais cargos ou perfis e o que cada um pode fazer?"),
    ), intro="Seção 6: regras de negócio. O que não for informado pode ficar fora do escopo inicial."),
    Block("escopo", "Escopo e entrega", (
        Q("mvp", "O que é indispensável na primeira versão (MVP)? Só o obrigatório para lançar."),
        Q("futuro", "O que pode ficar para versões futuras?"),
        Q("criterio_sucesso", "Como vamos considerar que o projeto foi entregue com sucesso? Quais critérios precisam ser atendidos?"),
    )),
    Block("infra", "Infraestrutura e tecnologias", (
        Q("integracoes", "Vai precisar integrar com outros serviços? (Mercado Pago, Stripe, PagSeguro, Discord, WhatsApp, Telegram, Google, login com Google, GitHub, OpenAI, Supabase, PostgreSQL, Firebase, MySQL, AWS, Azure...)",
          options=("Mercado Pago", "Stripe", "PagSeguro", "Discord", "WhatsApp", "Telegram", "Google", "Google Login", "GitHub",
                   "OpenAI", "Supabase", "PostgreSQL", "Firebase", "MySQL", "AWS", "Azure", "Outro"), multi=True),
        Q("apis_existentes", "Já existe alguma API ou sistema externo usado hoje no projeto? Quais?"),
        Q("importar_dados", "Vai ser preciso importar dados que já existem (migração)? Se sim, de onde e mais ou menos quanto.",
          options=("Sim", "Não", "Ainda não sei")),
        Q("tecnologia_obrigatoria", "Existe alguma tecnologia obrigatória (linguagem, framework, banco, plataforma)?"),
        Q("dominio", "O projeto já tem domínio registrado? Se tiver, qual?", options=("Sim", "Não")),
        Q("homologacao", "Existe ambiente de testes/homologação separado para validar as funcionalidades?",
          options=("Sim", "Não", "Ainda não sei")),
    ), intro="Seção 7: infraestrutura e integrações."),
)

BOT_BLOCKS: tuple[Block, ...] = (
    Block("bot_identidade", "Bot: identidade", (
        Q("bot_nome", "Nome oficial do bot."),
        Q("bot_slogan", "Slogan ou frase principal."),
        Q("bot_proposta", "Qual é a proposta do bot?"),
        Q("bot_problema", "Qual problema ele resolve?"),
        Q("bot_publico", "Para qual público ele foi criado?"),
        Q("bot_diferencial", "Qual é o principal diferencial dele?"),
        Q("bot_visual", "Existe logo, ícone, mascote ou identidade visual?"),
        Q("bot_cores", "Existem cores oficiais? Quais?"),
    ), discord_bot_only=True, intro="Como é um bot de Discord, agora vou usar o formulário de bot. Se alguma pergunta não se aplicar, escreva \"Não se aplica\"; se ainda não souber, \"A definir\"."),
    Block("bot_funcionalidades", "Bot: funcionalidades", (
        Q("bot_func_todas", "Quais são todas as funcionalidades do bot?"),
        Q("bot_func_destaque", "Quais funcionalidades você quer destacar no site?"),
        Q("bot_func_dev", "Tem alguma funcionalidade ainda em desenvolvimento?"),
        Q("bot_func_exclusivas", "Existem recursos exclusivos para determinados servidores ou usuários?"),
    ), discord_bot_only=True),
    Block("bot_comandos", "Bot: comandos", (
        Q("bot_slash", "O bot usa Slash Commands (/)? Explique como."),
        Q("bot_prefixo", "Ele tem prefixo? Qual?"),
        Q("bot_lista", "Existe uma lista completa de comandos? Se puder, envie com nome e descrição de cada um."),
        Q("bot_principais", "Quais são os principais comandos?"),
        Q("bot_cmd_admin", "Existem comandos exclusivos para administradores?"),
        Q("bot_cmd_config", "Existem comandos de configuração?"),
        Q("bot_cmd_premium", "Existem comandos premium?"),
    ), discord_bot_only=True),
    Block("bot_premium", "Bot: sistema premium", (
        Q("bot_tem_premium", "O bot tem versão Premium? Como ela funciona?"),
        Q("bot_gratis", "Quais recursos são gratuitos?"),
        Q("bot_recursos_premium", "Quais recursos são Premium?"),
        Q("bot_planos", "Quais são os planos e o valor de cada um?"),
        Q("bot_cobranca", "A cobrança é mensal, anual ou ambas? Tem período de teste?"),
        Q("bot_limites", "Existem limites por servidor?"),
        Q("bot_pagamento", "Como funciona o pagamento?"),
    ), discord_bot_only=True),
    Block("bot_adicionar", "Bot: adicionar e configurar", (
        Q("bot_link_add", "Qual é o link oficial para adicionar o bot?"),
        Q("bot_permissoes", "Quais permissões ele pede?"),
        Q("bot_config_inicial", "Precisa de alguma configuração depois de adicionar? Existe um passo a passo guiado (onboarding)?"),
        Q("bot_precisa_dashboard", "O usuário precisa acessar um dashboard depois de adicionar o bot?"),
    ), discord_bot_only=True),
    Block("bot_dashboard", "Bot: dashboard", (
        Q("bot_dash_existe", "Existe dashboard? Qual é o link e o login é pelo Discord?"),
        Q("bot_dash_config", "O que o administrador consegue configurar nele?"),
        Q("bot_dash_metricas", "Tem métricas, estatísticas ou logs? Configurações por servidor?"),
    ), discord_bot_only=True),
    Block("bot_suporte", "Bot: suporte", (
        Q("bot_servidor_suporte", "Qual é o servidor oficial de suporte (nome e link)? Tem sistema de tickets?"),
        Q("bot_docs", "Existe documentação ou FAQ?"),
        Q("bot_bugs", "Onde os usuários reportam bugs e enviam sugestões?"),
        Q("bot_email", "Existe algum e-mail oficial?"),
    ), discord_bot_only=True),
    Block("bot_estatisticas", "Bot: estatísticas", (
        Q("bot_servidores", "Em quantos servidores o bot está e quantos usuários ele atende?"),
        Q("bot_uso", "Quantos comandos/ações ele executa (e em qual período)?"),
        Q("bot_stats_publicas", "Tem alguma estatística que pode ser divulgada? Existe página de status e qual é o uptime aproximado?"),
    ), discord_bot_only=True),
    Block("bot_privacidade", "Bot: segurança e privacidade", (
        Q("bot_dados", "Quais dados o bot coleta? Ele guarda mensagens, IDs de usuários/servidores ou logs?"),
        Q("bot_retencao", "Por quanto tempo os dados ficam guardados?"),
        Q("bot_politicas", "Existe Política de Privacidade e Termos de Serviço?"),
        Q("bot_terceiros", "Usa serviços ou APIs de terceiros? Quais?"),
    ), discord_bot_only=True),
    Block("bot_links", "Bot: links oficiais", (
        Q("bot_links", "Envie os links oficiais que existirem (site, Discord, adicionar bot, dashboard, documentação, status, GitHub, termos, privacidade, Instagram, X/Twitter, YouTube, outros). Os que não existirem, diga \"Não se aplica\"."),
    ), discord_bot_only=True),
    Block("bot_referencias", "Bot: referências do site", (
        Q("bot_ref_sites", "Tem sites de bot que você considera boas referências, ou sites que você gosta visualmente? (links)"),
        Q("bot_estilo_nao", "Tem algum estilo que você NÃO quer?"),
        Q("bot_estilo", "Prefere algo mais minimalista, tecnológico, corporativo, gamer...?"),
        Q("bot_paginas", "Quais páginas o site deveria ter?"),
    ), discord_bot_only=True),
    Block("bot_importante", "Bot: informações importantes", (
        Q("bot_obrigatorio", "Tem alguma informação que obrigatoriamente precisa aparecer no site?"),
        Q("bot_proibido", "Tem alguma informação que NÃO pode aparecer?"),
        Q("bot_em_breve", "Tem algo que ainda vai ser lançado e deve aparecer como \"Em breve\"?"),
        Q("bot_obs", "Quer acrescentar detalhes, restrições ou expectativas? (opcional, pode dizer que não tem)"),
    ), discord_bot_only=True),
)

ALL_QUESTIONS = {q.id: q for block in (*GENERAL_BLOCKS, *BOT_BLOCKS) for q in block.questions}
BARE_ANSWERS = {
    "sim", "s", "nao", "n", "ok", "okay", "yes", "no", "talvez", "uhum", "aham", "claro",
    "pode ser", "isso", "nenhum", "nenhuma", "tenho", "nao tenho", "tem", "nao tem", "sei la", "?",
}
OPTIONAL_IDS = {"bot_obs"}


def is_bare_answer(text: str) -> bool:
    cleaned = _norm(re.sub(r"<@!?\d+>", " ", text)).strip(" .!,;:")
    return cleaned in BARE_ANSWERS


def is_valid_placeholder(text: str) -> bool:
    cleaned = _norm(text).strip(" .!")
    return cleaned in {"a definir", "nao se aplica", "ainda nao sei", "nao sei ainda"}


@dataclass
class IntakeState:
    block_index: int = 0
    answers: dict[str, dict[str, Any]] = field(default_factory=dict)
    discord_bot: bool | None = None
    client_questions: list[str] = field(default_factory=list)
    raw_messages: list[str] = field(default_factory=list)
    finished: bool = False
    started_at: str = ""
    finished_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "block_index": self.block_index,
            "answers": self.answers,
            "discord_bot": self.discord_bot,
            "client_questions": self.client_questions[-20:],
            "raw_messages": self.raw_messages[-80:],
            "finished": self.finished,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "IntakeState":
        data = data or {}
        return cls(
            block_index=int(data.get("block_index", 0)),
            answers=dict(data.get("answers") or {}),
            discord_bot=data.get("discord_bot"),
            client_questions=list(data.get("client_questions") or []),
            raw_messages=list(data.get("raw_messages") or []),
            finished=bool(data.get("finished")),
            started_at=str(data.get("started_at") or ""),
            finished_at=str(data.get("finished_at") or ""),
        )

    def blocks(self) -> list[Block]:
        blocks = list(GENERAL_BLOCKS)
        if self.discord_bot:
            blocks += list(BOT_BLOCKS)
        return blocks

    def current_block(self) -> Block | None:
        blocks = self.blocks()
        return blocks[self.block_index] if self.block_index < len(blocks) else None

    def pending(self, block: Block) -> list[Question]:
        return [
            q for q in block.questions
            if q.id not in OPTIONAL_IDS and self.answers.get(q.id, {}).get("status") != "ok"
        ]


@dataclass
class TurnResult:
    reply: str
    finished: bool = False


class QuoteIntake:
    def __init__(self, ai_service: Any | None) -> None:
        self.ai = ai_service

    # ----------------------------------------------------------- mensagens
    def opening(self, display_name: str, state: IntakeState) -> str:
        block = state.current_block()
        return (
            f"Oi, {display_name}! Antes de chamar a equipe, vou fazer algumas perguntas sobre o "
            "seu projeto. Com as respostas eu monto um resumo para o Talisson e ele já chega "
            "sabendo de tudo, sem ficar perguntando de novo. ♡\n\n"
            "Responda **com suas palavras** (só \"sim\" ou \"não\" não ajuda no orçamento). "
            "Se algo não se aplicar, escreva \"Não se aplica\"; se ainda não souber, \"A definir\". "
            "Pode responder tudo numa mensagem só ou em várias.\n\n"
            + self.render_block(state, block)
        )

    def render_block(self, state: IntakeState, block: Block | None, only: list[Question] | None = None) -> str:
        if block is None:
            return ""
        blocks = state.blocks()
        number = blocks.index(block) + 1
        header = f"**Parte {number} de {len(blocks)}: {block.title}**"
        if block.intro and only is None:
            header += f"\n-# {block.intro}"
        questions = only or list(block.questions)
        lines = [f"{index}. {q.text}" for index, q in enumerate(questions, start=1)]
        return header + "\n" + "\n".join(lines)

    # ---------------------------------------------------------------- turno
    async def handle(self, text: str, state: IntakeState) -> TurnResult:
        block = state.current_block()
        if block is None:
            state.finished = True
            return TurnResult("", finished=True)
        text = (text or "").strip()
        state.raw_messages.append(text[:4000])
        pending = state.pending(block)
        if not text:
            return TurnResult(self.render_block(state, block, pending))
        if is_bare_answer(text):
            return TurnResult(
                "Me explica com suas palavras, por favor. Só \"sim\" ou \"não\" não me dá detalhe "
                "suficiente pro orçamento.\n\n" + self.render_block(state, block, pending)
            )
        extracted = await self._extract(text, state, block, pending)
        follow_up = ""
        if extracted is None:
            # IA fora do ar: guarda a resposta crua (nada se perde) e segue.
            for q in pending:
                state.answers[q.id] = {"status": "ok", "value": text[:1500], "raw": True}
        else:
            for item in extracted.get("answers", []):
                qid = str(item.get("id", ""))
                if qid not in ALL_QUESTIONS:
                    continue
                status = str(item.get("status", "missing"))
                value = str(item.get("value", "")).strip()
                if status == "missing" or not value:
                    continue
                if status == "ok" and is_bare_answer(value) and not is_valid_placeholder(value):
                    status = "yes_no_only"
                state.answers[qid] = {
                    "status": "ok" if status == "ok" else status,
                    "value": value[:1500],
                    "options": [str(o) for o in item.get("options", []) or []][:20],
                }
            question = str(extracted.get("client_question", "")).strip()
            if question:
                state.client_questions.append(question[:500])
            follow_up = str(extracted.get("follow_up", "")).strip()
        if block.id == "identificacao" and state.answers.get("tipo_servico", {}).get("status") == "ok":
            state.discord_bot = self._is_discord_bot(state)

        remaining = state.pending(block)
        prefix = ""
        if extracted is not None and extracted.get("client_question"):
            prefix = "Anotei sua dúvida, ela vai junto no resumo pro Talisson responder. "
        if remaining:
            vague = [q for q in remaining if state.answers.get(q.id, {}).get("status") in {"vague", "yes_no_only"}]
            intro = follow_up or (
                "Quase lá! Ficou faltando ou ficou vago:" if vague else "Ficou faltando:"
            )
            return TurnResult((prefix + intro).strip() + "\n\n" + self.render_block(state, block, remaining))
        state.block_index += 1
        next_block = state.current_block()
        if next_block is None:
            state.finished = True
            state.finished_at = datetime.now(timezone.utc).isoformat()
            return TurnResult(
                (prefix + "Prontinho, terminamos! ♡ Vou montar o resumo e chamar a equipe agora.").strip(),
                finished=True,
            )
        return TurnResult((prefix + "Perfeito, anotado!").strip() + "\n\n" + self.render_block(state, next_block))

    def _is_discord_bot(self, state: IntakeState) -> bool:
        service = state.answers.get("tipo_servico", {})
        blob = _norm(" ".join([service.get("value", ""), *service.get("options", []), *state.raw_messages[-3:]]))
        if "bot" not in blob:
            return False
        if "discord" in blob:
            return True
        return not ("telegram" in blob or "whatsapp" in blob)

    async def _extract(
        self,
        text: str,
        state: IntakeState,
        block: Block,
        pending: list[Question],
    ) -> dict[str, Any] | None:
        if self.ai is None:
            return None
        lines = []
        for q in pending:
            line = f"- id={q.id}: {q.text}"
            if q.options:
                line += f" | opções{' (várias)' if q.multi else ''}: {', '.join(q.options)}"
            lines.append(line)
        previous = "; ".join(
            f"{qid}={data.get('value', '')[:80]}" for qid, data in list(state.answers.items())[-8:]
        )
        prompt = (
            "Você é a Bella conduzindo a consulta de orçamento de um cliente num ticket.\n"
            f"MENSAGEM DO CLIENTE: {text[:3500]}\n\n"
            f"PERGUNTAS PENDENTES ({block.title}):\n" + "\n".join(lines) + "\n\n"
            f"RESPOSTAS ANTERIORES (contexto): {previous or 'nenhuma'}\n\n"
            "Ligue cada trecho da mensagem à pergunta certa. Para cada pergunta pendente devolva:\n"
            "- status: ok (respondeu com conteúdo, ou disse 'a definir'/'não se aplica'/'ainda não sei'), "
            "vague (resposta genérica que não serve para orçar, ex.: 'um bot legal', 'várias coisas'), "
            "yes_no_only (só sim/não/tenho sem explicar, quando a pergunta pede detalhe), missing (não falou nada).\n"
            "- value: a resposta do cliente, completa e fiel, em terceira pessoa, sem inventar nada.\n"
            "- options: quando a pergunta tem opções, as opções que correspondem ao que ele disse "
            "(entenda sinônimos: 'uns 3 mil' = 'R$ 2.500 a R$ 5.000', 'sem pressa' = 'Flexível', "
            "'bot pro meu servidor' = 'Bot (Discord, Telegram, WhatsApp)').\n"
            "client_question: se o cliente fez uma pergunta em vez de responder (ex.: preço, prazo), "
            "copie a pergunta; senão vazio.\n"
            "follow_up: se algo ficou vague ou yes_no_only, uma frase curta, gentil e específica "
            "pedindo o detalhe que falta (português do Brasil, sem listar as perguntas). Senão vazio."
        )
        schema = {
            "type": "object",
            "properties": {
                "answers": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "status": {"type": "string"},
                            "value": {"type": "string"},
                            "options": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": ["id", "status", "value"],
                    },
                },
                "client_question": {"type": "string"},
                "follow_up": {"type": "string"},
            },
            "required": ["answers"],
        }
        try:
            return await self.ai.generate_json(
                prompt, schema, max_output_tokens=1800, timeout_seconds=30, use_cache=False,
            )
        except Exception as exc:  # noqa: BLE001 - qualquer falha guarda a resposta crua
            LOGGER.warning("IA indisponível na entrevista de orçamento: %s", type(exc).__name__)
            return None

    # --------------------------------------------------------------- resumo
    def answers_document(self, state: IntakeState, client_name: str) -> str:
        lines = [f"# Solicitação de orçamento: {client_name}", ""]
        for block in state.blocks():
            lines.append(f"## {block.title}")
            for q in block.questions:
                data = state.answers.get(q.id)
                value = data.get("value", "") if data else "(sem resposta)"
                options = data.get("options") if data else None
                lines.append(f"**{q.text}**")
                lines.append(value + (f"  \n_Opções: {', '.join(options)}_" if options else ""))
                lines.append("")
        if state.client_questions:
            lines.append("## Dúvidas do cliente")
            lines += [f"- {item}" for item in state.client_questions]
        return "\n".join(lines)

    async def summary(self, state: IntakeState, client_name: str) -> dict[str, Any]:
        document = self.answers_document(state, client_name)
        if self.ai is not None:
            try:
                data = await self.ai.generate_json(
                    "Monte o RESUMO ADMINISTRATIVO desta solicitação de orçamento para o Talisson "
                    "(desenvolvedor). Seja objetivo, use só o que o cliente disse e aponte o que "
                    "falta ou não fecha (ex.: escopo grande para o orçamento ou prazo).\n\n"
                    + document[:11000],
                    {
                        "type": "object",
                        "properties": {
                            "titulo": {"type": "string"},
                            "resumo": {"type": "string"},
                            "escopo": {"type": "string"},
                            "orcamento_prazo": {"type": "string"},
                            "tecnico": {"type": "string"},
                            "complexidade": {"type": "string"},
                            "pontos_de_atencao": {"type": "array", "items": {"type": "string"}},
                            "proximos_passos": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": ["titulo", "resumo", "escopo", "orcamento_prazo"],
                    },
                    max_output_tokens=1500,
                    timeout_seconds=40,
                    use_cache=False,
                )
                if data.get("resumo"):
                    data["origem"] = "ia"
                    return data
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("Resumo de orçamento sem IA: %s", type(exc).__name__)
        return self._fallback_summary(state, client_name)

    def _fallback_summary(self, state: IntakeState, client_name: str) -> dict[str, Any]:
        def value(qid: str) -> str:
            return state.answers.get(qid, {}).get("value", "não informado")

        return {
            "titulo": f"{value('tipo_servico')} para {client_name}",
            "resumo": value("descricao"),
            "escopo": f"MVP: {value('mvp')}\nFuncionalidades: {value('funcionalidades')}",
            "orcamento_prazo": f"Orçamento: {value('faixa_orcamento')} | Prazo: {value('prazo')}",
            "tecnico": f"Plataforma: {value('plataforma')} | Integrações: {value('integracoes')} | Hospedagem: {value('hospedagem')}",
            "complexidade": "a avaliar",
            "pontos_de_atencao": ["Resumo gerado sem IA; confira as respostas completas no arquivo."],
            "proximos_passos": [],
            "origem": "contingencia",
        }
