# Changelog

## 3.4.3 - 2026-06-19

- Recados agora são enviados em formato conversacional, sem layout de formulário, categoria, horário ou link visível.
- Recados passam a salvar status `pendente`, `respondido` e `encerrado`, com histórico interno da conversa.
- Talisson pode responder um recado dando reply na mensagem da Bella, e o retorno é encaminhado ao autor original.
- O autor pode responder ao retorno da Bella, mantendo uma conversa assíncrona intermediada.
- Respostas e continuações usam Gemini quando disponível, com fallback local para não quebrar a entrega.

## 3.4.2 - 2026-06-19

- Corrigido falso positivo onde textos de anúncio com a palavra `lembretes` eram tratados como configuração de recados.
- Anúncios conversacionais agora têm prioridade antes do sistema de ausência, recados e lembretes.
- Menções de canal em anúncios não são mais interpretadas como horário de compromissos.

## 3.4.1 - 2026-06-19

- Recados e solicitações de agendamento agora são enviados como mensagem normal, sem embed.
- Todo recado menciona Talisson, menciona o autor, inclui categoria, mensagem completa, canal, horário e link original.
- O link direto da mensagem original passa a ser obrigatório, com fallback montado por guild/canal/mensagem.

## 3.4.0 - 2026-06-19

- Anúncios conversacionais agora publicam texto puro, sem embeds ou painéis.
- Anúncios completos resolvem canal/cargo por menção ou nome e não fazem perguntas extras.
- Gemini gera texto de anúncio variado; fallback local mantém publicação segura.
- Adicionado horário oficial de atendimento de segunda a sexta, das 09h às 18h.
- Pedidos de agendamento entram na central de lembretes como solicitação de agendamento.
- Talisson pode criar compromissos pessoais por linguagem natural com data, horário, assunto e prioridade.
- Lembretes pessoais notificam 24h, 2h e 30min antes e concluem automaticamente após o horário.
- Consultas como `minha agenda de hoje` e `quais compromissos tenho amanhã` usam os dados armazenados.

## 3.3.3 - 2026-06-19

- Corrigida detecção de ausência para rodar antes do sistema de tickets.
- Ausência e retorno fortes do proprietário agora podem ser salvos mesmo sem menção explícita à Bella.
- Adicionados mais padrões naturais para reunião, consulta, aula, trabalho, compromisso, AFK e indisponibilidade.
- Persistido último evento de disponibilidade para facilitar diagnóstico.

## 3.3.2 - 2026-06-19

- Corrigido reconhecimento de recados diretos como `diz pro Talisson olhar o WhatsApp`.
- Recados diretos agora são registrados imediatamente, sem cair no Gemini público.
- A resposta informa quando o recado foi guardado, mas o canal de lembretes não pôde receber o envio.

## 3.3.1 - 2026-06-19

- Adicionado sistema de ausência pessoal do Talisson por linguagem natural.
- Adicionada central de recados com memória temporária para continuações.
- Recados agora são categorizados automaticamente e enviados ao canal configurado.
- Adicionados `/jarvis lembretes canal` e `/jarvis lembretes status`.
- Convites para call, party e pedidos de contato com Talisson viram lembretes.

## 3.3.0 - 2026-06-18

- Adicionada memória operacional persistente para regras ensinadas por Talisson.
- Regras por canal podem apagar mensagens fora do propósito e orientar o usuário.
- Regras salvas são usadas como contexto autorizado nas respostas da Bella.
- Adicionada Bella Anti-Scam Vision, desativado por padrão.
- Adicionados `/jarvis antiscam on`, `/jarvis antiscam off` e `/jarvis antiscam status`.
- Anti-Scam usa hash SHA-256, cache persistente e relatório em canal de segurança.
- Criado `config/antiscam.json` com categorias extensíveis e limites operacionais.
- Adicionada divulgação automática de conteúdos oficiais com deduplicação persistente.
- Adicionados `/conteudo-status` e `/conteudo-verificar`.

## 3.2.3 - 2026-06-12

- Corrigido o proprietário exclusivo para o Discord ID `933780111742877777`.
- Centralizada a identidade para moderação, tickets e anúncios.
- Removido o ID divergente do comando `/setup-ticket`.
- Adicionado teste para `limpe 100 mensagens do chat aq`.

## 3.2.2 - 2026-06-12

- Criado serviço Gemini exclusivo para tickets, priorizando o modelo rápido.
- Recepção do ticket agora aparece imediatamente e é refinada em segundo plano.
- Saudações e conversas cordiais recebem respostas curtas e instantâneas.
- Pedidos para falar com Talisson são escalados imediatamente, mesmo sem Gemini.
- Orçamentos, reuniões, parcerias e propostas têm escalonamento local confiável.
- Problemas técnicos comuns recebem coleta objetiva de erro, página e captura.
- Confirmações como `deu certo, pode fechar` encerram o atendimento.
- Reduzido o histórico e o tamanho das respostas enviados ao Gemini.
- Adicionada deduplicação persistente por mensagem dentro das Threads.
- Modelos com erro 503 entram em cooldown para evitar atrasos repetidos.

## 3.2.1 - 2026-06-12

- Corrigido o comando natural `apague 3 mensagens do chat`.
- Ações administrativas locais agora têm prioridade sobre Gemini e anúncios.
- Impedidas confirmações públicas de ações que ainda não foram executadas.
- Adicionado cooldown de 60 segundos após timeout de um modelo Gemini.
- Redistribuído o prazo para preservar mais tempo para o modelo alternativo.
- Desativado o AFC do Gemini, que não é necessário neste projeto.
- Adicionada coleta de quantidade para pedidos como `limpe este chat`.
- Adicionados `/ticket-status` e `/ticket-prioridade`.

## 3.2.0 - 2026-06-12

- Adicionada central inteligente de tickets com Threads privadas.
- Adicionado `/setup-ticket` com editor de embed, mídia, campos e componentes.
- Adicionadas triagem Gemini, resolução assistida, escalonamento e transcript.
- Adicionadas métricas de volume, resolução, categoria e tempos de atendimento.
- Adicionados `/ticket-fechar`, `/ticket-reabrir` e `/ticket-metricas`.
- Adicionada análise multimodal de anexos e mensagens respondidas.
- Reforçadas respostas de esclarecimento para nunca fingir compreensão.
- Fixado o proprietário administrativo no ID `933780111742877777`.
- Adicionadas ações naturais para trancar e destrancar canais.
- Integrado o diretório oficial de site, redes sociais e Cheetah.

## 3.1.2 - 2026-06-11

- Adicionado conhecimento oficial da hospedagem da Bella na Square Cloud.
- Reduzido o histórico público para mensagens relevantes e sem respostas de bots.
- Removidas duplicações e contaminação entre assuntos independentes.
- Aumentado o limite conversacional para 14 segundos com divisão entre modelos.
- Adicionado treinamento contextual para memes brasileiros, incluindo “beta”.
- Impedida falsa familiaridade com membros sem informações disponíveis.

## 3.1.1 - 2026-06-11

- Atualizado o modelo conversacional para `gemini-3.5-flash`.
- Reservado `gemini-2.5-flash-lite` para análises automáticas.
- Mantido `gemini-3.1-flash-lite` como fallback exclusivo do chat.
- Separadas as filas e cotas lógicas de conversa e monitoramento.
- Corrigido o circuito que repetia chamadas em modelos ainda em cooldown.
- Evitado que eventos automáticos esgotem o modelo principal do chat.

## 3.1.0 - 2026-06-11

- Tornado o Gemini o cérebro principal de toda conversa pública.
- Removidas respostas locais fixas para saudações, identidade, memes e FAQ.
- Mantida a camada local somente para segurança administrativa e contingência.
- Transformada a base do site e do Discord em RAG transparente para o Gemini.
- Adicionada recuperação especializada usando também o histórico recente.
- Reforçada a continuidade de referências e pronomes entre mensagens.

## 3.0.0 - 2026-06-11

- Adicionado roteador local de intenção para conversas, conhecimento e ações.
- Adicionadas memória curta, contexto recente e respostas locais com cache.
- Adicionado índice institucional do Discord com atualização automática.
- Unificadas as fontes de contexto do site, Discord e conversa atual.
- Removida a dependência do Gemini para moderação e consultas simples.
- Adicionadas limpeza de mensagens e criação de eventos por linguagem natural.
- Adicionado fallback local para conversas e publicação de anúncios.
- Limitado o tempo das respostas públicas complexas para evitar travamentos.
- Mantidos detalhes técnicos somente nos canais internos de diagnóstico.

## 2.6.0 - 2026-06-11

- Reorganizada a IA em `src/ai` com responsabilidades independentes.
- Centralizada toda comunicação Gemini em `gemini_service.py`.
- Adicionado modo público para dúvidas gerais, ajuda, tecnologia e memes.
- Adicionado contexto conversacional persistente por usuário e canal.
- Mantido grounding obrigatório no índice oficial para assuntos sobre Talisson.
- Adicionados filtros para frases proibidas e conteúdo sensível.
- Adicionada resolução segura de usuários e cargos por nomes naturais.
- Adicionado sistema conversacional de anúncios sem comandos ou formulários.
- Rascunhos de anúncios sobrevivem a reinicializações e evitam perguntas repetidas.
- Publicações usam embeds profissionais, cargos permitidos e auditoria completa.
- Adicionado aviso administrativo quando a chave Gemini está ausente.

## 2.5.0 - 2026-06-11

- Adicionado Gemini como motor opcional de intenção, validação e resposta.
- Adicionado system prompt permanente do assistente executiva Bella.
- Respostas do Gemini permanecem limitadas ao conteúdo oficial indexado.
- Comandos executivos usam interpretação do Gemini com validação rígida de IDs.
- Mantido o interpretador local como contingência quando a API falha.
- Adicionado resumo profissional de alterações pelo Gemini.
- Adicionada memória administrativa persistente com bloqueio de credenciais.
- Adicionado Google Analytics 4 Data API como fonte gratuita de audiência real.
- Relatórios agora incluem sessões, usuários, páginas, origens e perfil do GA4.
- Adicionados cache, diagnóstico e autenticação segura do Google Analytics.
- Adicionados testes de confiança, IDs administrativos e prioridade do GA4.

## 2.4.3 - 2026-06-10

- Configurado `TRAFFIC_LOG_PATH=data/traffic.ndjson` por padrão.
- Adicionada criação automática do arquivo persistente de audiência.
- Payloads do Vercel Drain agora são gravados em NDJSON antes do relatório.
- Atualizado o relatório vazio para indicar fonte pronta e aguardando eventos.
- Adicionado teste de ingestão, persistência e deduplicação do tráfego.

## 2.4.2 - 2026-06-10

- Ajustado o empacotamento para remover a pasta raiz duplicada na extração.
- Mantido apenas um arquivo `.env.example` na raiz do projeto.

## 2.4.1 - 2026-06-10

- Fixado o proprietário executivo no ID `933780111742877777`.
- Impedido que uma variável de ambiente altere a autorização exclusiva.
- Respostas executivas agora permitem menção somente ao proprietário.
- Reforçada a apresentação de cargos e usuários exclusivamente por nomes legíveis.
- Adicionados testes de identidade imutável e neutralização de menções em nomes.

## 2.4.0 - 2026-06-10

- Adicionado assistente executivo por menção e linguagem natural.
- Acesso exclusivo ao usuário configurado em `TALISSON_USER_ID`.
- Adicionados gerenciamento de cargos, banimento, expulsão e timeout.
- Adicionada desconexão administrativa de usuários em canais de voz.
- Adicionados tickets privados com criação, fechamento e reabertura persistentes.
- Adicionadas consultas e operações do monitor por frases naturais.
- Adicionada configuração natural de canais compatíveis.
- Adicionada auditoria executiva com executor, alvo, cargos, avatares e duração.
- Adicionadas validações de permissões, hierarquia, alvo e ambiguidade.
- Menções à Bella feitas por outros usuários agora são ignoradas sem resposta.

## 2.3.0 - 2026-06-10

- Adicionados `/feedbacks sincronizar` e `/feedbacks status`.
- Adicionados índice automático de páginas públicas, contatos e redes sociais.
- Adicionado reconhecimento de aliases e menção do Discord de Talisson Souza.
- Adicionadas respostas naturais em terceira pessoa, sem copiar a voz do site.
- Adicionados `/voice-status` e `/presence` com rotação de métricas reais.
- Adicionadas seleção, prioridade e mensagens de incidente configuráveis no status.
- Adicionado guard central por `ADMIN_ROLE_IDS`.
- Adicionada auditoria completa, canais configuráveis e detecção de abuso.
- Adicionada persistência SQLite com backup JSON.
- Adicionada separação de usuários, monitor, crawlers e outras automações.
- Adicionados interpreter automático do VS Code e instalador de um clique.

## 2.2.0 - 2026-06-10

- Adicionadas métricas reais de audiência por access log ou Vercel Log Drain.
- Adicionados visitas, visitantes únicos, páginas, origens e atividade diária.
- Adicionado receptor HTTP assinado para Web Analytics Drains da Vercel.
- Adicionada proteção para nunca confundir requisições do monitor com visitas.
- Adicionado comando e reconexão persistente em canal de voz.
- Adicionado sistema temporário de recepção com canal e duração configuráveis.
- Adicionados terceiro embed semanal de audiência e comando `/trafego`.
- Adicionadas dependências oficiais de voz do discord.py (PyNaCl e davey).

## 2.1.0 - 2026-06-10

- Adicionada resposta seletiva baseada somente no conteúdo oficial do site.
- Adicionadas detecção de dúvida, comparação semântica e confiança configurável.
- Adicionada regra de silêncio para mensagens sem correspondência segura.
- Adicionada atualização automática do FAQ e das páginas renderizadas.
- Adicionadas respostas por Reply com o nome visível do usuário.
- Adicionadas métricas de mensagens avaliadas, respondidas e ignoradas.
- Adicionado relatório semanal obrigatório, inclusive sem ocorrências.
- Adicionados comandos de atualização, relatório e canais dedicados.
- Adicionados testes para casos positivos, silêncio e semana sem eventos.

## 2.0.0 - 2026-06-10

- Corrigida a validação da meta tag com bypass de cache e diagnóstico completo.
- Substituído o reenvio por cooldown por um gerenciador persistente de incidentes.
- Adicionados status DND e atividade personalizada.
- Adicionado monitor de feedbacks com sincronização inicial e deduplicação.
- Adicionados embeds profissionais para feedbacks, mudanças e relatórios.
- Adicionado monitor renderizado de páginas React com resumos naturais.
- Adicionadas métricas persistentes e relatórios executivos periódicos.
- Adicionado gerador interativo de embeds com edição posterior.
- Adicionados scripts de setup, `.env.example` e testes automatizados.
