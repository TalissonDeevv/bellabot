# Bella Security Scanner — relatório técnico

## 1. Arquitetura

O scanner reutiliza o `StateStore`, o RBAC de `AdminAudit`, a Central de Logs, o cliente Groq já configurado, o sistema de backup e os padrões de `discord.ui.LayoutView`/Components V2. Não existe banco, cliente de IA ou sistema de permissões paralelo.

A implementação foi validada com `discord.py 2.7.1`, dentro da faixa declarada em `requirements.txt` (`>=2.7.0,<3.0`).

O fluxo é:

1. `SecurityCollector` coleta somente dados expostos pela API oficial.
2. `SnapshotNormalizer` cria uma representação estável e versionada.
3. `PermissionEngine` implementa combinação de cargos e a ordem oficial dos overwrites.
4. `SecurityRuleEngine` executa regras determinísticas.
5. `RiskEngine` calcula severidade contextual.
6. `RecommendationEngine` produz o plano de hardening.
7. `SecurityCenter` aplica exceções, persiste, compara e atualiza o painel.
8. A IA recebe somente um finding e a pergunta do administrador para explicá-lo.

O scanner é audit-first. Nenhuma regra ou resposta da IA altera o servidor.

## 2. Arquivos adicionados

- `website_guard/security/__init__.py`
- `website_guard/security/collector.py`
- `website_guard/security/normalizer.py`
- `website_guard/security/permissions.py`
- `website_guard/security/rules.py`
- `website_guard/security/risk.py`
- `website_guard/security/recommendations.py`
- `website_guard/security/diff.py`
- `docs/BELLA_SECURITY_SCANNER.md`

## 3. Arquivos modificados

- `website_guard/security_center.py`: facade, jobs, repositório, exceções, histórico, painel fixo V2, revisão e IA.
- `website_guard/critical_changes.py`: envia eventos do monitor para o histórico unificado de segurança.
- `bot.py`: RBAC, IA, view persistente e comando de publicação.
- `tests/test_security_center.py`: cobertura do motor e persistência.
- `README.md` e `docs/ADMIN_SECURITY_BACKUP.md`: operação e permissões.

## 4. Persistência e migrations

O projeto usa seções JSON versionadas, com escrita atômica e espelho SQLite. Não foi necessária migration SQL. O schema de snapshot passou para `2` e o scanner para `2.0.0`.

Seções:

- `security_scans`
- `security_findings`
- `security_snapshots`
- `security_scan_jobs`
- `security_scan_config`
- `security_exceptions`
- `security_panel_config`
- `security_monitor_events`

Registros antigos continuam legíveis; campos novos possuem fallback.

## 5. Comandos

- `/seguranca`: abre uma Central de Segurança privada e temporária.
- `/seguranca-painel canal recriar`: publica, recupera ou recria o painel fixo.
- `/scan-seguranca`: inicia um scan em background para compatibilidade operacional.

Todos os comandos e todos os botões validam novamente o RBAC no backend.

## 6. Painel e componentes

O painel fixo usa Components V2, `timeout=None` e `custom_id` estável. Ele exibe resumo, estado atual, última análise, contagem por severidade e áreas não verificadas. Ações disponíveis:

- iniciar scan;
- diagnóstico e revisão de findings;
- plano de hardening;
- cargos, membros, canais, bots, AutoMod e webhooks;
- histórico, comparação e atualização.

O painel salva `guild_id`, `channel_id` e `message_id`, reutiliza a mensagem existente, detecta mensagem/canal ausente e permite recriação explícita.

## 7. Regras implementadas

| Regra | Tipo | Critério de severidade |
|---|---|---|
| `role-administrator` | FINDING | CRITICAL para `@everyone` ou grande alcance fora do RBAC; HIGH para múltiplos membros fora do RBAC; MEDIUM para cargo esperado/gerenciado |
| `role-control-plane-combination` | WARNING | aumenta por quantidade de permissões estruturais, membros e ausência no RBAC |
| `role-manage-roles-high-hierarchy` | WARNING | HIGH quando `Manage Roles` está fora do RBAC, possui membros e aparece nos 20% superiores da hierarquia |
| `role-mention-everyone` | WARNING | MEDIUM quando cargo amplo fora do RBAC pode mencionar globalmente |
| `member-effective-administrator` | FINDING | HIGH quando a combinação efetiva de cargos concede Administrator fora do RBAC |
| `channel-admin-public` | FINDING | HIGH quando canal de aparência administrativa está visível a `@everyone` |
| `channel-unsynced-permissions` | WARNING | LOW; divergência pode ser intencional |
| `channel-sensitive-overwrite` | WARNING | CRITICAL para controle estrutural concedido a `@everyone`; HIGH/MEDIUM conforme alcance e alvo |
| `automod-unavailable` | RECOMMENDATION | INFO; ausência de leitura não é vulnerabilidade |
| `automod-no-rules` | RECOMMENDATION | INFO; recomenda avaliação sem presumir falha |
| `automod-disabled-rule` | WARNING | LOW |
| `automod-many-exemptions` | WARNING | MEDIUM a partir de dez exceções |
| `bot-broad-privileges` | WARNING | HIGH com Administrator; MEDIUM para combinação estrutural; Bella recebe LOW para evitar falso incidente operacional |
| `webhook-unowned` | WARNING | LOW; comum em aplicações, mas exige inventário |
| `webhook-channel-volume` | WARNING | MEDIUM para seis ou mais webhooks no mesmo canal |
| `integration-admin-role` | WARNING | MEDIUM |
| `guild-low-verification` | RECOMMENDATION | LOW e contextual |
| `guild-admin-mfa-disabled` | RECOMMENDATION | MEDIUM |

As regras são independentes da IA e retornam evidência, justificativa e recomendação.

## 8. Dados analisados

- servidor: owner, quantidade de membros, verificação, filtro explícito, MFA, tier e features;
- cargos: bitfield, permissões sensíveis, hierarquia, membros, managed/bot/integration;
- membros privilegiados: cargos, posição e permissões efetivas do Discord;
- bots: cargos, cargo mais alto e privilégios efetivos;
- canais/categorias: herança, sincronização, acesso público, allow/deny e permissões efetivas de cada alvo com overwrite;
- AutoMod: estado, trigger, metadata, actions e exceções;
- webhooks: IDs, nomes, tipo, canal, criador e aplicação, nunca token/URL secreta;
- integrações: metadados públicos e cargo associado;
- Audit Log recente quando autorizado.

## 9. Jobs, falhas e performance

Estados: `PENDING`, `COLLECTING`, `ANALYZING`, `GENERATING_RECOMMENDATIONS`, `COMPLETED`, `PARTIAL`, `FAILED` e `CANCELLED`.

O botão inicia `asyncio.Task`, responde imediatamente e mantém lock por servidor. As coleções em cache são percorridas uma vez; fontes HTTP são consultadas uma vez por scan. `discord.py` controla rate limits. Erro/permissão ausente em AutoMod, webhooks, integrações ou Audit Log torna o scan `PARTIAL` e registra a área não analisada.

## 10. Exceções

Na revisão de um finding existem:

- **Ignorar uma vez**: altera apenas o scan selecionado.
- **Marcar como intencional**: salva regra, recurso, guild, autor, motivo, data e fingerprint.
- **Restaurar alerta**: remove a exceção.

Uma exceção intencional só é reaplicada quando o fingerprint da evidência permanece igual. Mudança substancial faz o alerta voltar automaticamente. A camada de persistência também aceita validade futura, embora a interface atual ainda não exponha um seletor de data.

## 11. Histórico e diff

Cada scan armazena executor, horários, estado, versões, áreas analisadas/não analisadas, contagens, findings, snapshot e mudanças. O normalizador ordena recursos, bitfields simples e overwrites para evitar falsos positivos por ordem.

O diff informa recursos `ADDED`, `REMOVED` e `CHANGED`, além de novos findings, findings resolvidos e novas recomendações. O painel compara o scan atual ao imediatamente anterior; a persistência permite evoluir para seleção arbitrária A/B.

## 12. Security Monitor

O detector existente continua monitorando eventos suportados por gateway/API:

- criação/alteração de cargos sensíveis;
- mudança de overwrites em canais sensíveis;
- regra de AutoMod desativada/removida;
- criação/exclusão de webhook por comparação de baseline.

Os eventos também são gravados em `security_monitor_events` e enviados à Central de Logs. A Bella não atribui executor sem evidência confiável. A correlação opcional com Audit Log permanece uma evolução futura porque eventos e entradas podem chegar fora de ordem.

## 13. IA

`Perguntar à Bella` envia apenas regra, tipo, severidade, descrição, evidência, justificativa, recomendação e pergunta. Snapshot completo, tokens, URLs secretas e inventário integral não são enviados. A resposta é explicativa e não pode substituir o resultado determinístico ou executar alterações.

## 14. Logs e backup

Scans iniciados, concluídos, parciais/falhos e exceções são registrados no módulo `security`; criticidade alta segue a política existente de `alerts`. O detector de mudanças evita duplicatas por assinatura e janela temporal.

O `ServerBackupService` já executa scan antes e depois de restaurações. O fluxo seguro permanece: snapshot estrutural, scan anterior, alteração confirmada, scan posterior e comparação.

## 15. Permissões necessárias

- acesso básico ao servidor, cargos, membros em cache e canais;
- `Manage Guild` para AutoMod e integrações;
- `Manage Webhooks` para inventário de webhooks;
- `View Audit Log` para contexto de auditoria;
- `View Channel`, `Send Messages`, `Embed Links` e histórico no canal do painel/logs.

`Administrator` não é obrigatório. O privileged member intent precisa permanecer habilitado para inventário confiável de membros em servidores onde o Discord o exige.

## 16. Limitações reais da API

- a API não informa a finalidade de um cargo; a Bella usa RBAC configurado e contexto, sem adivinhar;
- ausência de regra AutoMod não prova vulnerabilidade;
- tokens e URLs secretas de webhook não são coletados;
- integrações externas não expõem toda a configuração interna;
- cache de membros incompleto limita análise individual e deve ser tratado operacionalmente;
- Audit Log pode ter atraso e não é usado para atribuição sem correlação segura;
- eventos de webhook não carregam o webhook alterado; o monitor compara inventários;
- o scanner não mede comportamento, intenção ou confiabilidade de usuários/bots;
- o painel atual compara automaticamente com o scan anterior, não oferece ainda um seletor A/B arbitrário;
- correções assistidas foram apenas preparadas conceitualmente; nenhuma ação administrativa automática foi implementada.

## 17. Testes

`tests/test_security_center.py` cobre:

- combinação de cargos;
- bypass de Administrator;
- ordem de overwrites de canal;
- severidade contextual de cargos;
- privilégio efetivo de membro;
- AutoMod como recomendação contextual;
- normalização de coleções e overwrites;
- diff de permissões;
- plano de hardening;
- persistência do painel;
- exceções e reavaliação por fingerprint.

Comando focal: `python -m unittest tests.test_security_center -v`.

Validação integrada executada no ambiente virtual do projeto:

`python -m unittest tests.test_security_center tests.test_extended_admin_systems tests.test_server_backup tests.test_central_logs -v`

Resultado: **33 testes aprovados**. A descoberta completa do repositório executou 309 testes, mas ainda termina com 26 falhas e 4 erros preexistentes concentrados nos testes de conversa/persona Groq e memória permanente; essas falhas não percorrem o scanner e não foram alteradas para evitar correções fora do escopo.

## 18. Evoluções recomendadas

- seletor arbitrário de Scan A versus Scan B;
- paginação completa acima de 25 findings;
- validade de exceção configurável pela interface;
- correlação conservadora de Audit Log com janela e tipo de evento;
- fixtures de integração com objetos Discord simulados para falhas HTTP e scans parciais;
- futura correção assistida com prévia, backup, confirmação, audit log e scan posterior, nunca acionada autonomamente pela IA.
