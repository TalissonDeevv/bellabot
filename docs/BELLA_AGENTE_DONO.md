# Bella como agente do Talisson

Este documento cobre as funções novas: canal privado do dono, entrevista de
orçamento nos tickets, lembretes por DM, memória com escopos, menções do
Discord e a robustez da FreeLLMAPI.

## Canal privado do dono

Canal: `OWNER_PRIVATE_CHANNEL_ID` (padrão `1570783779574780176`). Nele o
Talisson conversa com a Bella sem precisar mencionar: toda mensagem dele é
tratada como falada com ela (`website_guard/owner_assistant.py`).

| Pedido (linguagem natural) | O que acontece |
|---|---|
| "tem algo pendente pra hoje?", "minha agenda de amanhã" | Lista compromissos e tarefas direto dos dados, **sem IA** (funciona mesmo sem cota). Vale em qualquer canal quando ele chama a Bella. |
| Áudio no canal ("amanhã tenho que ligar pro Fulano às 15h, terminar o site...") | Transcreve, separa em lembretes (com horário, DM 5 min antes) e tarefas. Se a IA falhar, o áudio inteiro vira uma tarefa. |
| "anota: decidi não pegar projeto abaixo de R$500" | Guarda no cofre de decisões (`owner_decisions`). |
| "minhas decisões", "esquece a regra do R$500" | Lista / desativa. |
| "vou aceitar o projeto do Fulano por R$300" | Avisa se contraria uma decisão. O link de pagamento do Stripe também mostra o aviso antes de confirmar. |
| "resumo da semana" | Feito, dinheiro (Stripe se configurado), pendente, próximos dias e dúvidas da comunidade. Também é enviado sozinho **sexta a partir das 20h**. |
| "dúvidas mais repetidas", "ideias de conteúdo" | Agrupa as perguntas da comunidade (últimos 30 dias) e sugere artigos. |
| "ranking da staff" | Tickets atendidos, resolvidos e tempo médio da 1ª resposta. Enviado sozinho **dia 1 de cada mês**. |
| "clientes sumidos" | Clientes (links de pagamento e tickets de orçamento) sem aparecer há 60+ dias, com rascunho de mensagem. **Nada é enviado sem ele responder "manda".** |
| "vou ficar fora até dia 20", "vou viajar até 20/12", "férias até segunda" | **Modo viagem**: marca a ausência (quem te chama fica sabendo o dia da volta e pode deixar recado), muda o status da Bella e guarda tudo. "voltei" (ou automaticamente no dia seguinte à volta, 9h) entrega o resumo: menções (urgentes primeiro), recados, tickets/orçamentos, dinheiro e tarefas. |
| "ideia: fazer curso de bots" (no canal) | **Caixa de ideias**. "minhas ideias" lista. Dia 1 de cada mês (11h) ela pergunta quais tirar do papel: "a 1 e a 3" vira tarefa, "descarta a 2" arquiva. |
| "Contabo vence dia 10 todo mês R$ 40" | **Contas a pagar**: lembra 2 dias antes, todo mês, uma vez por vencimento. "paguei a Contabo", "minhas contas", "remove a conta da Contabo". |
| "manda um recado pro @fulano de que o bot dele tá pronto, manda na DM" / "avisa o @fulano no #geral que..." | **Recado**: a IA reescreve o pedido com as palavras da Bella (cumprimenta, diz que o Talisson pediu, mantém todos os fatos) e envia na DM ou no canal mencionado (notificando só a pessoa). Aceita nome sem menção; se houver duas pessoas com o mesmo nome, pergunta. DM fechada: avisa e sugere canal. Sem canal indicado, vai na DM. Registro em `owner_relays`. |
| "o que eu tava fazendo?", "onde parei?" | Últimos pedidos da sessão, tarefas abertas nela e compromissos de hoje. Ao voltar depois de `OWNER_PAUSE_HOURS` (padrão 3 h) sem falar no canal, ela manda isso sozinha como "bem-vindo de volta". |

Alertas automáticos no canal:

- **Saída em massa**: `OWNER_LEAVE_ALERT_COUNT` saídas (padrão 5) em
  `OWNER_LEAVE_ALERT_MINUTES` (padrão 30), com o anúncio mais recente como
  possível causa. No máximo um alerta a cada 2 horas.
- **Canais parados**: canais abertos para conversa sem mensagem há
  `OWNER_QUIET_CHANNEL_DAYS` dias (padrão 7). Um aviso por canal a cada 30 dias.
- **Clientes sumidos**: verificação diária às 10h, até 2 rascunhos por dia.

## Entrevista de orçamento (tickets)

Quando o assunto do ticket contém "orçamento" (ex.: "Solicitar Orçamento"):

1. A equipe **não é adicionada nem mencionada** na abertura.
2. A Bella conduz a consulta seguindo o formulário de "Solicitação de
   Desenvolvimento de Sistema", uma seção por mensagem. Se o projeto for um bot
   de Discord, entra também o formulário de bot.
3. A resposta é livre. A IA liga cada trecho à pergunta certa e mapeia as
   opções (ex.: "uns 3 mil" vira "R$ 2.500 a R$ 5.000").
4. Só "sim"/"não" não vale como resposta: ela pede para explicar. "A definir" e
   "Não se aplica" valem.
5. Perguntas do cliente (ex.: preço) vão anotadas no resumo.
6. No fim: resumo administrativo (embed) + arquivo com todas as respostas, e
   só então a equipe é adicionada e mencionada.

O progresso fica no registro do ticket (`smart_tickets.<thread>.quote_intake`),
então sobrevive a reinício. Se a IA cair, a resposta crua é guardada e a
entrevista segue. O botão "Solicitar equipe" do cliente responde que falta
pouco; um membro da equipe pode encerrar a entrevista pelo botão.

## Lembretes por DM

`website_guard/reminder_service.py`. Exemplos: "me lembra da reunião amanhã às
15h", "quais lembretes eu tenho?", "cancela o lembrete da reunião", "adia a
reunião pra sexta às 10h".

- Fuso `REMINDER_TIMEZONE` (padrão `America/Recife`); guardado em UTC com o
  horário local original.
- DM `REMINDER_LEAD_MINUTES` antes (padrão 5).
- Estados: PENDING, PROCESSING, SENT, FAILED, CANCELLED, EXPIRED.
- Scheduler por consulta a cada 20 s, reserva atômica (não envia duas vezes),
  recupera reservas presas, tenta de novo com espera crescente e registra cada
  entrega em `bella_reminder_delivery_logs`.
- DM fechada: avisa no canal de origem mencionando só a pessoa.
- Texto escrito pela IA; se ela falhar, mensagem de contingência.

## Memória com escopos

`save_memory` em `src/ai/permanent_memory.py`: escopos GLOBAL, USER, GUILD e
PROJECT, prioridade, categoria, validade, entidades do Discord e auditoria
(`bella_memory_audit`). Só o dono (e `BELLA_OWNER_IDS`) grava memória global;
cada pessoa pode gravar a própria (USER).

## Menções do Discord

`website_guard/discord_entities.py`. Links `discord.com/channels/G/C`,
`<#id>`, `<@&id>` e `<@id>` viram entidades guardadas na memória e voltam como
menções nativas. Nas respostas, só o autor e as entidades validadas podem ser
notificados; cargo só se for mencionável; `@everyone`/`@here` nunca. Menções
inventadas pela IA que não existem no servidor são removidas.

## FreeLLMAPI

`src/ai/freellmapi_client.py`:

- Lista de modelos em ordem; modelo lento, vazio ou sem cota passa para o próximo.
- 5xx e conexão: nova tentativa com espera exponencial e jitter
  (`FREELLMAPI_MAX_RETRIES`, `FREELLMAPI_BACKOFF_SECONDS`); `Retry-After` curto é respeitado.
- 401/403 falha na hora; prazo total `FREELLMAPI_TOTAL_TIMEOUT_SECONDS` (40 s).
- Log por chamada (`llm_request id=... conv=servidor:canal:pessoa model=... status=... ms=...`)
  e contadores em `METRICS`.
- Uma resposta por vez por conversa (servidor, canal, pessoa).
- Em falha, a resposta é sempre: "Ops, tive uma instabilidade ao processar sua
  mensagem. Pode tentar novamente daqui a pouquinho? ♡".

**Limitação:** com só uma chave da Groq e uma do Gemini, a cota por minuto acaba
rápido ("All models exhausted"). Cadastre mais provedores grátis no painel da
FreeLLMAPI (Cerebras, OpenRouter, Mistral, GitHub Models, NVIDIA).

## Seções do banco de estado

`bella_reminders`, `bella_reminder_delivery_logs`, `owner_decisions`,
`owner_community_questions`, `owner_client_activity`, `owner_client_drafts`,
`owner_member_leaves`, `owner_assistant_meta`, `owner_ideas`, `owner_bills`,
`owner_activity_log`, `bella_memory_audit`,
`jarvis_universal_knowledge`.

## Testes

```
.venv/Scripts/python -m pytest -q tests
```

Arquivos novos: `test_owner_assistant.py`, `test_quote_intake.py`,
`test_reminders_mentions.py`, `test_freellmapi_client.py` (retry/backoff),
`test_bella_direct_and_memory.py`.
