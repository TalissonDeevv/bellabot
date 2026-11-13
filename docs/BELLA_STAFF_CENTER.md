# Bella Staff Center

## Arquitetura

O Bella Staff Center reutiliza o `StateStore`, o `CentralLogService`, o RBAC administrativo e o `SmartTicketManager`. Não cria um segundo sistema de tickets ou de logs.

Dados persistidos:

- `staff_center_configs`: cargos, categoria, canais e painel fixo.
- `staff_center_occurrences`: ocorrências administrativas.
- `staff_center_warnings`: advertências internas.
- `staff_center_interventions`: solicitações à liderança.
- `staff_center_training`: progresso de treinamento.
- `staff_center_activity`: trilha técnica de ações.

O `StateStore` mantém JSON com espelho SQLite e gravações atômicas conforme a infraestrutura atual da Bella.

## Hierarquia oficial

- Presidente: `1467982180469833979`
- Vice-Presidente: `1504605819923206204`
- Ajudante: `1563200377095061534`

O cargo Ajudante recebe somente permissões operacionais: visualizar canais autorizados, conversar, responder em threads, ler histórico, reagir, anexar, incorporar links e usar comandos. Não recebe Administrator, Manage Guild, Manage Roles, Manage Channels, Manage Webhooks, Ban Members, Kick Members, Moderate Members ou Mention Everyone.

## Configuração

`/staff configurar` apresenta uma prévia privada e exige confirmação explícita. Somente após a confirmação a Bella cria ou reutiliza:

- Categoria `🛡️ Administração`.
- Canais compartilhados com emoji e divisor `╺╸`: central administrativa, comunicados, ocorrências, solicitações, treinamento e relatórios.
- Canais de liderança com o mesmo divisor: presidência, vice-presidência, decisões e auditoria.

A operação é idempotente: IDs persistidos e recursos com os mesmos nomes são reutilizados. Os canais da liderança negam visualização ao cargo Ajudante.

## Integração com tickets

O acesso do Ajudante é validado no backend e na inclusão em threads. Categorias permitidas incluem dúvidas simples, suporte, feedback, sugestões, problemas técnicos, denúncias de membros e solicitações gerais. Categorias de orçamento, financeiro, comercial, parceria, reunião, assuntos administrativos e qualquer categoria desconhecida falham de forma restritiva.

Presidente, Vice-Presidente e a equipe já configurada no sistema de tickets preservam as permissões existentes. Tickets antigos ou não classificados não são abertos automaticamente ao Ajudante.

## Comandos

- `/staff configurar`: prévia e confirmação da estrutura.
- `/staff painel`: publica ou restaura o painel persistente.
- `/staff ocorrencias`: lista registros recentes.
- `/staff membro`: mostra ficha administrativa.
- `/staff solicitar`: abre solicitação para a liderança.
- `/staff treinamento`: consulta ou atualiza progresso.
- `/staff relatorio`: gera resumo administrativo.
- `/staff permissoes`: explica a matriz de acesso.
- `/staff diagnostico`: verifica cargos, canais, painel e tickets.

O painel fixo oferece Ocorrência, Consulta de membro, Solicitação, Treinamento, Relatório, Copiloto e Diagnóstico. Todos os componentes revalidam autorização no backend.

## Copiloto

O Copiloto usa o serviço de IA existente apenas para orientação. Ele não aplica punições, não altera cargos e não substitui revisão humana. Em falha da IA, exibe uma orientação determinística de indisponibilidade sem interromper os registros administrativos.

## Formação dos Ajudantes

O canal `🎓╺╸treinamento-staff` possui painel persistente com três módulos, situações práticas, XP e progresso individual. O botão Quiz abre uma thread privada que funciona como ticket de treinamento. A Bella apresenta questões, explica respostas pela IA existente e conversa com o Ajudante como tutora.

1. Moderação responsável: responsabilidade, hierarquia, regras, prevenção e segurança.
2. Decisão e proporcionalidade: fato, regra, contexto, risco, medida proporcional, escalação e pós-incidente.
3. Aprendizado aplicado: canais claros, contexto, histórico útil, prática acompanhada e evidência de evolução.

O gerador combina 40 conceitos, 10 formatos de pergunta e ordens variadas de alternativas, oferecendo 9.600 combinações determinísticas baseadas nos artigos públicos de Talisson Souza sobre moderação, treinamento de moderadores e Discord como ambiente de aprendizado. Cinco acertos inéditos concluem o quiz de cada módulo; questões repetidas não geram XP novamente.

Ao encerrar a thread, a Bella gera um relatório de aprendizagem com indicadores, ponto forte e próximos estudos. O relatório vai para `📚╺╸relatorios-treinamento`, dentro da categoria Bella Logs, visível somente para Presidente e Vice-Presidente. O relatório não copia o conteúdo integral das conversas privadas. A conclusão da trilha não concede cargos ou permissões automaticamente.

## Central de Comandos

O canal `🛡️╺╸central-comandos`, dentro da categoria administrativa `1563248235274047649`, permite que Ajudantes usem botões da Bella sem receber permissões perigosas no próprio cargo. Ajudantes podem visualizar o painel, mas não podem escrever, apagar mensagens, criar threads ou usar comandos de aplicação naquele canal.

As ações disponíveis são banimento, kick, mute, advertência, consulta de membro e registro de ocorrência. Banimento, kick, mute e advertência exigem ID, justificativa e uma imagem anexada pelo componente de upload do Discord.

Progressão automática:

- 3 advertências: mute de 1 hora.
- 6 advertências: kick.
- 12 advertências: banimento permanente.

A Bella valida autorização, hierarquia do executor, hierarquia do alvo e posição do próprio cargo antes de agir. Todos os resultados, inclusive falhas, são registrados em `⚖️╺╸moderacao-staff`, na categoria Bella Logs `1551649692125896744`, visível apenas para Presidente e Vice-Presidente.

## Sugestões

O canal `1563301545091534868` contém o painel de envio. A sugestão é publicada em Components V2 no canal privado `1563301109550817400`, mostrando conteúdo, benefício, autor e status. Presidente e Vice-Presidente podem aprovar ou recusar; o painel passa a mostrar a decisão e o responsável pela revisão.

## Canais funcionais

- `📊╺╸relatorios-staff`: resumo de ocorrências, solicitações, advertências, treinamento, moderação e sugestões.
- `📋╺╸auditoria-staff`: histórico de ações com executor e data.
- `📥╺╸solicitacoes`: formulário de intervenção e consulta de pendências pela liderança.
- `🚨╺╸ocorrencias`: formulário e consulta de registros recentes.

## Central Administrativa Suprema

O canal `👑╺╸presidencia` mantém um painel persistente de emergência. Cada botão revalida no backend o cargo oficial Presidente (`1467982180469833979`); proprietário, administrador e Vice-Presidente não recebem acesso implícito sem esse cargo.

A central permite banimento em massa por cargo com prévia congelada e confirmação digitada, lockdown restaurável dos canais, bloqueio dos sistemas públicos da Bella, contenção total, restauração e diagnóstico. Bots, proprietário do servidor, Bella e membros com o cargo Presidente são excluídos do banimento em massa. Todas as ações são registradas em `🚨╺╸comando-supremo`, visível apenas para Presidente e Bella.

Antes do lockdown a Bella exige motivo, canal público e texto do anúncio. Após aplicar as restrições, publica no canal escolhido uma mensagem com `@everyone @here` e o anúncio informado. A Bella recebe explicitamente permissão de envio e menção nesse canal durante o lockdown. Se o Discord impedir a menção, a operação informa a limitação em vez de declarar o anúncio como concluído.

O lockdown salva os overwrites de cada canal antes de alterar permissões e persiste o progresso canal a canal. A restauração recompõe os overwrites anteriores ou volta a sincronizar canais que estavam sincronizados com a categoria. Contas com `Administrator` continuam ignorando overwrites por regra da API do Discord; a central não remove cargos nem permissões administrativas silenciosamente.

## Limitações

- A criação real da estrutura depende das permissões Discord da Bella e só ocorre após confirmação em `/staff configurar`.
- Ajudantes não são adicionados retroativamente a threads privadas já abertas.
- A atribuição de autoria de ações externas continua dependendo de evidência confiável do Audit Log do Discord.

## Testes

`tests/test_staff_center.py` cobre hierarquia, permissões seguras, persistência, integração estruturada com tickets, categorias autorizadas, categorias restritas, catálogo de milhares de questões, progressão por questões inéditas, falha fechada para categorias desconhecidas, proteção do banimento em massa, ações persistentes da Central Suprema e coleta obrigatória do anúncio de lockdown.
