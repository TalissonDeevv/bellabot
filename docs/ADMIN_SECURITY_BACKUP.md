# Administração, segurança e backup estrutural

## Arquitetura reaproveitada

Os três sistemas seguem a arquitetura já utilizada pela Bella:

- `bot.py` mantém o registro dos comandos, a composição dos serviços e a integração com o Discord.
- `website_guard/state.py` continua sendo a camada persistente principal. Ela grava `data/state.json` atomicamente e mantém o espelho SQLite em `data/state.db`.
- `website_guard/admin.py` continua responsável pelo RBAC dos comandos administrativos e pelo histórico de auditoria.
- As interfaces utilizam `discord.ui.LayoutView` e Components V2, seguindo os painéis administrativos existentes.
- Os módulos de tickets e AutoMod continuam donos de suas configurações; a Central de Logs apenas vincula os canais criados às configurações já existentes.

Não foi criado um segundo banco nem uma infraestrutura paralela de comandos, permissões ou painéis.

## Central de Logs

Comandos:

- `/logs configurar`: abre o mesmo configurador.
- `/logs-sincronizar`: único comando que cria categoria/canais ausentes e reutiliza os existentes.
- `/logs status`: mostra o estado de todos os módulos registrados.
- `/logs setup`: permanece disponível para compatibilidade com a configuração antiga de auditoria.

O registry central fica em `website_guard/central_logs.py`. Cada módulo informa identificador, nome, canal padrão, emoji, tipos de evento, estado padrão e capacidade de gerar alerta.

Seções persistentes:

- `guild_log_config`: aparência, categoria, cargos leitores e módulos habilitados.
- `guild_log_channels`: relação entre módulo e ID do canal.
- `guild_log_panel_channels`: relação entre painéis provisionados e ID do canal.
- `guild_log_events`: histórico técnico limitado dos eventos enviados.

Ao executar `/logs-sincronizar`, a Bella:

1. valida `Manage Channels`;
2. localiza a categoria pelo ID salvo ou pelo nome;
3. aplica overwrites privados a `@everyone`, à própria Bella e aos cargos escolhidos;
4. reutiliza canais salvos ou com o nome esperado;
5. cria somente os canais ausentes;
6. salva os IDs;
7. vincula `tickets`, `automod` e auditoria administrativa às configurações existentes.

Além dos canais de eventos, a sincronização provisiona `backups-painel` dentro da
mesma categoria e publica a Central de Backups da Bella nessa localização. O painel
é registrado em `fixed_admin_panels`, atualizado ao reiniciar e não é duplicado em
execuções seguintes.

Os workers legados também resolvem seus destinos pela Central antes dos IDs antigos.
Monitoramento do site usa `site`, feedbacks do site usam `site_feedbacks`, erros técnicos
usam `errors`, atividades do servidor usam `activity` e incidentes críticos usam
`alerts`. Ao sincronizar, os campos legados são migrados para impedir novos envios
aos canais antigos.

O canal `activity` registra entradas, saídas, primeira mensagem observada, entrada ou
movimentação em voz, comandos da Bella e respostas públicas de outros bots com metadados
de interação. A API do Discord não revela comandos efêmeros de outros bots nem comandos
sem resposta pública, portanto esses casos não podem ser registrados.

Logs operacionais vão ao canal do módulo. Eventos críticos também são duplicados no canal `alerts`, sem transformar todos os eventos em alerta.

## Scanner de Segurança

Comandos:

- `/seguranca`: abre a Central de Segurança em Components V2.
- `/seguranca-painel`: publica, recupera ou recria o painel fixo persistente.
- `/scan-seguranca`: inicia uma análise somente leitura em background.

Áreas analisadas pela API oficial do Discord:

- cargos e permissões sensíveis;
- permission overwrites de categorias e canais;
- regras nativas de AutoMod;
- bots, cargos e permissões efetivas;
- webhooks, sem coletar tokens;
- integrações disponíveis;
- informações recentes do Audit Log quando a Bella possui permissão.

O motor modular, regras, critérios, exceções, IA, monitor e limitações estão detalhados em `docs/BELLA_SECURITY_SCANNER.md`.

O scanner não altera cargos, canais, regras ou permissões. Falta de acesso a uma fonte resulta em scan `PARTIAL`, nunca em falso sucesso.

### Critérios de severidade

- `CRITICAL`: exposição ampla ou controle total com impacto imediato, por exemplo `@everyone` com `Administrator`.
- `HIGH`: controle administrativo relevante fora do RBAC esperado, combinação perigosa de permissões ou exposição provável de área administrativa.
- `MEDIUM`: configuração com risco contextual que merece revisão, mas não comprova comprometimento.
- `LOW`: inconsistência, exceção ampla ou higiene operacional que pode ser intencional.
- `INFO`: inventário ou limitação de coleta sem evidência de risco direto.

A severidade considera contexto como quantidade de membros afetados, cargo gerenciado, RBAC administrativo conhecido, alcance do overwrite e tipo de recurso. `Administrator` não é classificado automaticamente como incidente crítico.

Cada finding possui ID estável, regra, categoria, severidade, título, descrição, evidência, recurso, motivo, recomendação, timestamp e status.

### Histórico e comparação

Seções persistentes:

- `security_scans`;
- `security_findings`;
- `security_snapshots`;
- `security_scan_jobs`;
- `security_scan_config`.
- `security_exceptions`;
- `security_panel_config`;
- `security_monitor_events`.

Snapshots são normalizados antes da comparação. Listas de recursos e valores simples são ordenados, evitando mudanças causadas apenas pela ordem retornada pela API. O painel mostra histórico e diferenças entre a análise atual e a anterior.

## Backup estrutural

Comandos:

- `/backup-servidor criar`;
- `/backup-servidor listar`;
- `/backup-servidor visualizar`;
- `/backup-servidor restaurar`;
- `/backup-servidor aplicar`;
- `/backup-servidor status`.
- `/bella backups-painel` para publicar manualmente ou trocar o canal do painel.

Seções persistentes:

- `server_backups`: metadados, estado e contagens;
- `server_backup_resources`: conteúdo estrutural versionado;
- `backup_restore_jobs`: estado e resultado dos jobs de restauração.
- `restored_backup_messages`: controle de idempotência das mensagens restauradas.

O schema atual é `backup_schema_version: 2`, com leitura compatível do schema 1. O payload é separado em:

- `METADATA`;
- `STRUCTURE`;
- `ROLES`;
- `CHANNELS`;
- `PERMISSIONS`;
- `AUTOMOD`;
- `BELLA_PANELS`;
- `EMBEDS`.

### O que é salvo

- propriedades restauráveis do servidor;
- cargos, permissões, cor, posição, hoist, mentionable e indicação de gerenciamento;
- categorias e canais suportados pela biblioteca instalada;
- tópicos, NSFW, slowmode, parent, bitrate, limite de usuários e propriedades específicas disponíveis;
- permission overwrites com allow/deny;
- regras nativas de AutoMod, triggers, metadata, ações e exceções;
- mensagens de painel cujos IDs estão registrados pela aplicação e cuja autoria é da Bella;
- mensagens recentes independentes da Bella que contenham embeds, com limite de 5.000
  mensagens por canal e 30.000 por servidor;
- embeds e payload disponível dos components desses painéis;
- metadados de anexos, sem baixar indiscriminadamente conteúdo.

Não existe coleta de mensagens comuns de usuários. A varredura limitada serve apenas
para localizar embeds de autoria da conta atual da Bella.

### Fluxo seguro de restauração

1. o administrador escolhe um backup;
2. a Bella calcula a prévia de criação, reutilização, conflitos e limitações;
3. o usuário confirma no painel;
4. uma segunda confirmação exige digitar `RESTAURAR`;
5. a Bella cria automaticamente um snapshot de segurança;
6. a Bella executa um scan de segurança anterior;
7. a restauração executa sob lock por servidor;
8. a Bella executa outro scan e persiste a comparação antes/depois;
9. o job termina como `COMPLETED`, `PARTIAL` ou `FAILED`;
10. a Central de Logs registra o resultado.

A restauração não apaga cargos, canais ou regras. Recursos compatíveis são reutilizados; recursos ausentes são criados.

### Remapeamento de IDs

São construídos mapas separados:

- `old_role_id -> new_role_id`;
- `old_category_id -> new_category_id`;
- `old_channel_id -> new_channel_id`.

A ordem aplicada é:

1. cargos;
2. categorias;
3. canais;
4. permission overwrites;
5. configurações do servidor que apontam para canais;
6. AutoMod e suas referências;
7. mensagens/painéis registrados e embeds independentes da Bella;
8. configurações internas conhecidas da Bella.

### Limitações oficiais

- cargos gerenciados por bots ou integrações não podem ser recriados manualmente;
- tokens de webhooks não são coletados ou restaurados;
- integrações externas não são reinstaladas;
- mensagens comuns e histórico de conversa não são copiados;
- anexos são inventariados por metadados, mas não são baixados em massa;
- embeds independentes são restaurados uma única vez por backup, usando controle de idempotência;
- componentes interativos dependem das views registradas no código; componentes genéricos não são recriados como botões mortos;
- IDs de membros só permanecem válidos no mesmo servidor; overwrite de membro inexistente é ignorado e relatado;
- propriedades não expostas ou não editáveis pela API oficial são preservadas como informação, mas não restauradas;
- a biblioteca `discord.py` trata rate limits HTTP. Jobs grandes usam defer, lock, estados e resultado parcial; não existe rollback transacional na API do Discord, por isso o snapshot automático é a compensação segura.

## Permissões necessárias

O RBAC interno continua exigindo o proprietário configurado ou um dos cargos de `ADMIN_ROLE_IDS`.

Permissões Discord por capacidade:

- Central de Logs: `Manage Channels`, `View Channel`, `Send Messages`, `Embed Links`.
- Scanner completo: `View Audit Log`, `Manage Guild` para AutoMod/integrações e `Manage Webhooks` para inventário de webhooks.
- Backup: `Manage Guild`, `Manage Roles`, `Manage Channels`, `View Audit Log` e acesso às mensagens de painel registradas.
- Restauração: `Manage Guild`, `Manage Roles`, `Manage Channels`, além de posição hierárquica suficiente.

Conceder `Administrator` não é obrigatório. Permissões mínimas são preferíveis.

## Variáveis de ambiente

Nenhuma variável de ambiente nova foi adicionada. Tokens e chaves não são armazenados nos backups.
