# Sistemas administrativos estendidos da Bella

## Visão geral

Os módulos reutilizam `StateStore`, `AdminAudit`, `CentralLogService`, as
views persistentes do `discord.py` e o registro `fixed_admin_panels`. Nenhum módulo
cria canais durante o startup. Os destinos de log são provisionados exclusivamente
por `/logs-sincronizar`.

| Módulo | Canal de log | Painel fixo |
| --- | --- | --- |
| Detector de Alterações Críticas | `alteracoes-criticas` | `/bella detector-painel` |
| Reparador de Painéis | `reparo-paineis` | `/bella reparador-painel` |
| Bella Giveaways | `sorteios` | `/bella sorteios-painel` |
| Bella Feedback | `satisfacao` | `/bella feedback-painel` |
| Feedbacks do site | `feedbacks-site` | `/bella feedbacks-site-painel` |
| Monitor de nicknames | `identidades` | `/bella nicks-painel` |
| Atividade do servidor | `atividade` | monitoramento por eventos |
| Erros internos | `erros` | diagnóstico automático |
| Central de Backups | `backups-painel` | `/bella backups-painel` |

O nome final dos canais respeita emojis e separador configurados na Central de
Logs. Eventos `HIGH` e `CRITICAL` do detector também são enviados ao canal central
de alertas, quando configurado.

## Detector de Alterações Críticas

O detector recebe eventos oficiais do gateway do Discord e não depende de um scan
manual. Ele acompanha:

- criação de cargo já contendo permissões sensíveis;
- cargo que recebe `Administrator`, `Manage Guild`, `Manage Roles`, `Manage Channels`,
  `Manage Webhooks`, `Ban Members`, `Kick Members`, `Mention Everyone` ou
  `Manage Messages`;
- mudança de permission overwrites em canais administrativos, canais de logs e
  canais que hospedam painéis registrados;
- exposição de um canal sensível para `@everyone`;
- regra de AutoMod desativada ou removida;
- criação ou remoção de webhook.

O inventário de webhooks não armazena tokens. Se a Bella não tiver permissão para
consultá-los, o detector não interpreta a falha como remoção em massa. Eventos
iguais possuem uma janela curta de deduplicação. O painel oferece status, eventos
recentes, atualização de baseline e pausa do monitoramento.

## Reparador de Recursos

O reparador audita referências conhecidas em `fixed_admin_panels`; ele não pesquisa
indiscriminadamente todo o histórico dos canais. A exclusão de uma mensagem
registrada é percebida imediatamente por `on_raw_message_delete`. Exclusões de cargos,
canais e embeds da Bella também geram pendências seletivas enquanto o bot está online.
Uma auditoria
periódica também verifica canal ausente, mensagem ausente, autoria incompatível e
mensagem sem embed/componentes.

Cada pendência oferece:

- **Restaurar selecionados:** recria somente os cargos, canais, painéis e embeds marcados;
- **Recriar:** publica uma cópia nova pelo renderer oficial do módulo e atualiza o ID;
- **Relincar:** procura uma mensagem compatível da própria Bella somente entre as
  50 mensagens mais recentes do canal registrado;
- **Ignorar:** fecha a pendência, preservando-a no histórico de auditoria.

Não existe reparo destrutivo ou silencioso. O administrador precisa escolher a ação.
Quando canal e painel são selecionados juntos, o reparador restaura na ordem correta e
remapeia o painel para o novo ID. Exclusões ocorridas com a Bella offline dependem do
snapshot estrutural mais recente.

## Bella Giveaways

O painel permite configurar o canal público e criar sorteios com:

- prêmio;
- descrição detalhada e imagem do embed;
- duração;
- um ou mais vencedores;
- um ou mais cargos obrigatórios;
- cargos que devem ser mencionados na publicação;
- tempo mínimo de permanência no servidor;
- atraso para início;
- recorrência em horas;
- seleção de um sorteio específico para editar, publicar, encerrar, cancelar ou reutilizar;
- listagem dos participantes;
- reroll de sorteios concluídos;
- consulta de ativos/agendados e histórico.

As participações são deduplicadas por usuário. Requisitos são validados no clique.
O worker processa agenda e encerramento a cada 30 segundos. O sorteio usa seleção
criptograficamente adequada (`secrets.SystemRandom`) e registra resultado e rerolls.
Views de sorteios ativos são restauradas quando o bot reinicia.

## Bella Feedback

O painel fixo permite escolher o canal, criar pesquisas, consultar métricas,
visualizar pesquisas ativas, analisar críticas recorrentes e ativar pesquisas
privadas após tickets.

Cada resposta aceita nota de 1 a 5 e comentário opcional. Um usuário pode atualizar
sua própria resposta, mas não gerar várias entradas para a mesma pesquisa. Painéis
administrativos mostram somente:

- total de pesquisas e respostas;
- média;
- percentual de notas 4 e 5;
- distribuição das notas;
- termos recorrentes em comentários com nota até 3.

Comentários completos e IDs dos autores não aparecem nos logs nem nos resumos. A
análise recorrente é local e baseada em frequência; não envia respostas privadas a
um modelo de IA.

Quando a opção pós-ticket está ativa, `SmartTicketManager` chama o serviço depois de
salvar transcript e métricas. A pesquisa é enviada por DM. Falha de DM não impede o
fechamento do ticket.

O painel de feedbacks do site tem uma ação separada de **Reenviar todos**. Ela ignora
o conjunto `processed_feedbacks`; se a leitura atual falhar, reaproveita o último
snapshot salvo para não perder feedbacks que já estavam publicados no site.

## Central de Backups

`/logs-sincronizar` cria o canal `backups-painel` na categoria configurada e publica
o painel fixo automaticamente. O painel oferece criação de backup com motivo, lista
de backups, detalhes, prévia de conflitos, restauração com confirmação dupla,
histórico de jobs e atualização após reinícios.

O comando `/bella backups-painel` continua disponível para publicar manualmente em
outro canal. A mensagem é registrada em `fixed_admin_panels` para ser atualizada e
recriada pelo Reparador de Recursos quando necessário.

## Persistência

O projeto mantém o modelo atual de estado JSON com espelho SQLite. Foram adicionadas
as seções:

- `critical_change_config`, `critical_change_events`, `critical_change_baselines`;
- `panel_repair_config`, `panel_repair_issues`, `panel_repair_history`;
- `giveaway_config`, `giveaways`, `giveaway_entries`, `giveaway_history`;
- `satisfaction_config`, `satisfaction_surveys`, `satisfaction_responses`,
  `satisfaction_history`.

Não há migration SQL separada porque `StateStore` cria e espelha seções de forma
atômica. A configuração da Central de Logs foi elevada para schema 5; configurações
antigas recebem os módulos novos na leitura e só criam os canais quando o
administrador executar `/logs-sincronizar`.

## Permissões necessárias

- Painéis e logs: `View Channel`, `Send Messages`, `Embed Links`, `Read Message History`.
- `/logs-sincronizar`: `Manage Channels`.
- Webhooks: `Manage Webhooks` para inventário completo.
- AutoMod: `Manage Guild` para receber e consultar eventos conforme as permissões
  e intents do Discord.
- Reparador: `Read Message History`; `Manage Messages` apenas para fixar painéis.

O RBAC administrativo existente continua sendo aplicado aos slash commands e às
interações dos painéis.

## Limitações

- O gateway informa que webhooks mudaram, mas não identifica diretamente qual foi
  alterado; a Bella compara inventários normalizados.
- O Discord não fornece evento genérico para toda configuração possível do servidor.
  O Security Scanner manual continua necessário para uma auditoria completa.
- O relink evita falsos vínculos limitando a busca ao mesmo canal, a mensagens da
  Bella e a assinaturas visuais compatíveis.
- Comandos efêmeros de outros bots não são observáveis pela API oficial do Discord;
  apenas respostas públicas com metadados de interação podem ser registradas.
- Sorteios agendados são processados no próximo ciclo do worker quando o bot estiver
  offline no horário exato.
