# Bella — Assistente Executivo e Observabilidade

Bot em `discord.py` que combina assistência conversacional, conhecimento do site
e do Discord, moderação administrativa, anúncios e observabilidade da plataforma
de Talisson Souza.

## Hospedagem (Contabo ou qualquer VPS Ubuntu)

Tutorial para quem vai hospedar a Bella + FreeLLMAPI num servidor Ubuntu
22.04/24.04. Funciona em servidor compartilhado: o instalador não mexe no fuso,
não reinstala o Docker se ele já existir e escolhe outra porta se a 3001
estiver ocupada.

**Consumo medido:** Bella ~180 MB de RAM (pico ~280 MB) + FreeLLMAPI ~150–300 MB.
Disco: ~1,5 GB. CPU: quase nada. Nenhuma porta precisa ser aberta na internet.

### Antes: o Talisson manda o pacote privado

O repositório **não** tem o `.env` (token do Discord, chaves de IA e Stripe) nem a
pasta `data` (bancos da Bella). No PC dele:

1. Clique com o botão direito em `deploy\empacotar_dados.ps1` > **Executar com o
   PowerShell**. Ele desliga a Bella do PC e cria `bella-dados.tar.gz` na Área de
   Trabalho.
2. Mande esse arquivo **por canal privado** (DM, pendrive, Drive restrito).
   Nunca no GitHub.

> A Bella não pode rodar no PC e no servidor ao mesmo tempo: com o mesmo token,
> ela responderia em dobro.

### 1. Usuário próprio para a Bella

Num servidor compartilhado, crie um usuário só para ela (como root ou com sudo):

```bash
sudo adduser bella
sudo usermod -aG sudo bella
```

Se o firewall do servidor estiver desligado, ligue liberando só o SSH (a Bella
não precisa de nenhuma porta aberta):

```bash
sudo ufw allow OpenSSH && sudo ufw --force enable
```

### 2. Baixar o código

```bash
ssh bella@IP_DO_SERVIDOR
git clone https://github.com/TalissonDeevv/bellabot.git ~/bellabot
```

Se o repositório for privado, o Talisson precisa adicionar sua conta do GitHub
como colaboradora (ou te passar um token de leitura).

### 3. Colocar o pacote privado

No computador onde está o `bella-dados.tar.gz`:

```bash
scp bella-dados.tar.gz bella@IP_DO_SERVIDOR:~
```

No servidor:

```bash
tar -xzf ~/bella-dados.tar.gz -C ~/bellabot && rm ~/bella-dados.tar.gz
```

### 4. Instalar

```bash
cd ~/bellabot && bash deploy/instalar_servidor.sh
```

Leva 5–10 minutos. Ele instala Python, Docker (se faltar), as dependências da
Bella, sobe a FreeLLMAPI em Docker (aberta só para o próprio servidor), cria o
serviço `bella-bot` (liga no boot e reinicia se cair) e agenda um backup diário.

No fim a Bella já está online no Discord usando a Groq direta. O instalador
mostra a **porta da FreeLLMAPI** e o comando do túnel; anote.

Com RAM sobrando, `bash deploy/instalar_servidor.sh --chromium` liga a leitura
completa do site (~400 MB a mais nos picos).

### 5. Configurar a FreeLLMAPI

O painel não fica aberto na internet. No **seu computador**, abra um túnel
(troque 3001 se o instalador mostrou outra porta):

```bash
ssh -L 3001:127.0.0.1:3001 bella@IP_DO_SERVIDOR
```

Com essa janela aberta:

1. Acesse **http://localhost:3001**.
2. Na primeira vez ele pede um **código de configuração**. No servidor:
   ```bash
   sudo docker logs bella-freellmapi 2>&1 | grep -i -A2 code
   ```
3. Crie o usuário admin do painel.
4. Página **Keys**: cadastre as chaves dos provedores grátis (o Talisson passa as
   da Groq e do Gemini; dá para criar mais em Cerebras, OpenRouter, Mistral etc.).
   Quanto mais provedores, menos a Bella bate limite.
5. Copie a **chave unificada** (`freellmapi-...`) no topo da página Keys.

### 6. Ligar a FreeLLMAPI na Bella

```bash
nano ~/bellabot/.env
```

Ajuste e salve (Ctrl+O, Enter, Ctrl+X):

```dotenv
FREELLMAPI_ENABLED=true
FREELLMAPI_API_KEY=freellmapi-...
```

```bash
sudo systemctl restart bella-bot
journalctl -u bella-bot -n 40 --no-pager | grep -i freellmapi
```

Deve aparecer `IA via FreeLLMAPI em http://127.0.0.1:3001/v1 com a Groq de reserva`.

### Dia a dia

| O quê | Comando |
|---|---|
| Status da Bella | `sudo systemctl status bella-bot` |
| Log ao vivo | `journalctl -u bella-bot -f` |
| Reiniciar | `sudo systemctl restart bella-bot` |
| Parar | `sudo systemctl stop bella-bot` |
| FreeLLMAPI | `sudo docker ps` / `sudo docker logs -f bella-freellmapi` |
| Memória | `free -h` |
| Atualizar para a versão nova do GitHub | `cd ~/bellabot && bash deploy/atualizar.sh` |
| Atualizar também a FreeLLMAPI | `cd ~/bellabot && bash deploy/atualizar.sh --tudo` |
| Backups (diário às 04:30, guarda 7) | `ls -lh ~/backups-bella` |

### Cuidados

- O `.env` e o `deploy/freellmapi/.env` têm segredos. O instalador deixa os dois
  com permissão só do usuário `bella`. Guarde uma cópia do
  `deploy/freellmapi/.env` fora do servidor: sem a chave de criptografia, as
  chaves salvas no painel da FreeLLMAPI ficam ilegíveis.
- Nunca abra a porta da FreeLLMAPI no firewall; o acesso é só pelo túnel SSH.
- Para remover tudo:
  ```bash
  sudo systemctl disable --now bella-bot && sudo rm /etc/systemd/system/bella-bot.service
  cd ~/bellabot/deploy/freellmapi && sudo docker compose down -v
  crontab -l | grep -v deploy/backup.sh | crontab -
  ```

Alternativa grátis com 1 GB de RAM: [deploy/GOOGLE_CLOUD.md](deploy/GOOGLE_CLOUD.md).

## Bella Partner Analytics

O Programa de Parceiros possui relatórios semanais, mensais, anuais e personalizados com indicadores reais, gráficos PNG, comparativos, resumo narrativo por IA com fallback determinístico, agendamento persistente, histórico imutável e publicação privada na Central de Logs. Consulte `docs/BELLA_PARTNER_ANALYTICS.md` para arquitetura, fórmulas, permissões e limitações.

A presença da Bella pode ser administrada pelo painel V2 persistente criado com `/presence painel`. Por padrão, a agenda usa `America/Sao_Paulo`: das 21h às 07h fica ausente com a mensagem de descanso e, das 07h às 21h, usa não perturbe com a mensagem operacional. A mesma central alterna, de forma exclusiva, entre agenda, presença personalizada, playlist NEFFEX, latência da hospedagem Square Cloud, rotação dinâmica e modo streamer. Também permite escolher os quatro estados, seis tipos de atividade, texto, URL de transmissão, horários e fuso, com persistência após reinicializações.

## Instalação

1. Execute `INSTALAR_E_INICIAR.bat` ou os comandos abaixo.
2. Preencha `DISCORD_TOKEN` no `.env` criado automaticamente.
3. No VS Code, use `Python: Select Interpreter` e escolha
   `.venv\Scripts\python.exe`.
4. No Discord Developer Portal, habilite **Message Content Intent** e
   **Server Members Intent**.

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\setup.ps1
.\run_bot.ps1
```

O setup instala as dependências e o Chromium headless usado para ler corretamente
as páginas React. O projeto inclui `.vscode/settings.json`, que aponta o Pylance
para a `.venv` e elimina os avisos de imports ausentes após a instalação.

Não execute `python bot.py` com o Python global. Isso causa erros como
`ModuleNotFoundError: rapidfuzz`. Use `.\run_bot.ps1` ou
`.\.venv\Scripts\python.exe bot.py`.

## Observatório público

A Bella publica estado operacional, métricas agregadas, comunidade, catálogo público, atividade editorial e incidentes no Observatório hospedado na Vercel. Os eventos usam HMAC, retry e identificadores idempotentes. Configure as variáveis `BELLA_OBSERVATORY_*` descritas em `docs/BELLA_OBSERVATORY_SYNC.md`. Use `/observatorio painel` uma vez para publicar a Central fixa; depois, a sincronização e o diagnóstico ficam disponíveis por botões persistentes.

## Logs, segurança e backup estrutural

- `/logs configurar`: configura aparência, módulos e cargos leitores sem criar canais.
- `/logs-sincronizar`: é o único comando que cria canais ausentes e mantém os IDs existentes.
- A sincronização também cria `backups-painel` dentro da categoria e publica/atualiza a Central de Backups automaticamente.
- `/seguranca`: abre a Central de Segurança em Components V2.
- `/seguranca-painel`: publica ou recupera o painel fixo persistente em um canal privado.
- `/scan-seguranca`: inicia em background a auditoria de servidor, cargos, membros privilegiados, canais, AutoMod, bots, webhooks e integrações sem alterar o servidor.
- `/backup-servidor criar`: salva estrutura, cargos, canais, permissões, AutoMod, painéis e embeds independentes enviados pela Bella.
- `/backup-servidor restaurar` ou `/backup-servidor aplicar`: mostra uma prévia, exige confirmação em duas etapas e cria snapshot automático antes da restauração.
- `/bella backups-painel`: publica manualmente a Central de Backups caso seja necessário trocar o canal.

O backup não copia conversas comuns. A busca de embeds é limitada às mensagens recentes
da própria Bella, com limites por canal e servidor, e nunca salva mensagens comuns de usuários.
Detalhes de arquitetura, permissões, severidade, remapeamento de IDs e limitações da API estão em
`docs/ADMIN_SECURITY_BACKUP.md`.
O relatório técnico completo do scanner está em `docs/BELLA_SECURITY_SCANNER.md`.

## Automação administrativa e comunidade

Os módulos administrativos trabalham com painéis fixos e possuem canais próprios na
Central de Logs. Depois de atualizar o bot, execute `/logs-sincronizar` para criar
somente os canais novos que estiverem faltando.

- `/bella detector-painel`: publica o painel do Detector de Alterações Críticas.
- `/bella reparador-painel`: publica o painel do Reparador de Painéis.
- `/bella sorteios-painel`: publica o painel do Bella Giveaways.
- `/bella feedback-painel`: publica o painel do Bella Feedback.
- `/bella feedbacks-site-painel`: publica o painel do monitor de feedbacks do site.
- `/bella nicks-painel`: publica o painel do monitoramento de nicknames.
- `/bella backups-painel`: publica manualmente a Central de Backups.

Cada comando aceita `canal` e `fixar`. Sem informar um canal, a Bella abre o painel
somente para o administrador. Os painéis reduzem o uso de slash commands e concentram
configuração, execução, status e histórico. Detalhes técnicos estão em
`docs/EXTENDED_ADMIN_SYSTEMS.md`.

## Programa de Parceiros e Afiliados

Use `/bella parceiros painel` para abrir ou publicar o painel administrativo já
existente. No painel, o botão **Criar/sincronizar canais** provisiona e sincroniza uma área
completa sem duplicar recursos:

- categoria privada `🤝 • PARCEIROS` e cargo `Parceiro`;
- canal público `📣・seja-parceiro` com o painel de inscrição;
- `🤝・central-parceiros` com perfil, link permanente, resultados e histórico;
- `📨・indicacoes` com registro privado de oportunidades;
- `📚・materiais-parceiros` e `🆘・suporte-parceiros`, com abertura de ticket privado;
- `📢・avisos-parceiros` para comunicados oficiais em somente leitura;
- `💬・chat-parceiros` para a comunicação geral da comunidade;
- `🔊・call-parceiros` e `🎙️・reunioes-parceiros` como salas de voz exclusivas;
- `⚙️・gestao-parceiros` com o painel administrativo fixo.

Cada canal de painel possui somente as ações relacionadas à sua função. A central
não repete mais os botões de indicação nos canais de materiais e suporte. Execute
**Criar/sincronizar canais** novamente para ajustar painéis existentes e criar o
chat e a call sem duplicar os demais canais.

A `central-parceiros` também oferece uma camada privada de **Partner Intelligence**:

- **Minha evolução** calcula nível, pontos, conquistas e próximas missões usando
  somente link, entradas, indicações, projetos e comissões registrados;
- **Funil inteligente** mostra pipeline, qualificação, fechamento, retenção,
  confiança da amostra, gargalo atual e a próxima melhor ação;
- **Simular ganhos** calcula comissão por projeto, cenários mensais e projeção
  ponderada sem transformar a estimativa em promessa de pagamento;
- **Kit personalizado** cria primeiro contato, follow-up sem pressão e resumo
  operacional a partir do segmento, necessidade e relação real com o contato.

Esses quatro recursos respondem de forma efêmera: outros parceiros não conseguem
ver métricas, simulações ou textos personalizados de outra pessoa. Cada resposta
também inclui um card PNG de `1652x952`, renderizado dinamicamente sobre o fundo
oficial em `website_guard/assets/partners/partner-intelligence-background.png`.
O card usa os mesmos dados apresentados no embed e é gerado fora do loop principal
para não travar a resposta da Bella. A área superior direita é reservada para a
assinatura `TS`, e o renderizador restaura os pixels da marca antes de salvar o PNG
para impedir que painéis, indicadores ou textos cubram a logo.

A central fixa e todas as funções privadas do parceiro usam Components V2 com uma
arte no topo. Isso inclui **Meu painel**, **Cadastrar/ver link**, **Histórico**,
**Minha evolução**, **Funil inteligente**, **Simular ganhos**, **Kit personalizado**
e **Regras**. A borda dos `Container` e a paleta das artes usam o dourado-laranja
`#C27C2B`. Os botões mantêm as cores nativas permitidas pelo Discord, pois a API não
aceita uma cor hexadecimal personalizada em botões.
Os campos originalmente marcados como `inline` nos embeds são agrupados em linhas
compactas no Components V2; métricas com várias linhas usam separadores curtos, evitando
o painel estreito e excessivamente vertical exibido anteriormente. Abaixo da imagem, o
conteúdo usa um único `TextDisplay` no estilo visual de um embed tradicional: título,
descrição e seções em negrito separadas por parágrafos discretos. Não existem linhas
divisórias entre cada campo; apenas a divisão entre imagem, conteúdo e botões.

O botão **Abrir ticket privado** do suporte cria uma thread privada pelo mesmo
sistema central de tickets. A Bella responde com IA usando o contexto específico
do Programa de Parceiros e pode encaminhar o caso para a equipe. No painel
administrativo, **Parceiros aprovados** permite consultar receita, comissões e
lucro líquido estimado, registrar resultados, marcar comissões pagas, visualizar
lançamentos e remover o acesso de um parceiro com confirmação.

O sistema usa a regra oficial de comissão de **50% do valor do projeto** após
validação, contratação e finalização. Convites personalizados, entradas,
indicações, projetos, comissões e auditoria permanecem persistidos em SQLite.
O canal da Central de Logs registrado para `partners` é reutilizado quando
existir; crie canais de logs ausentes com `/logs-sincronizar`.

O link personalizado não é criado automaticamente. Cada parceiro informa um
convite pertencente ao servidor, sem expiração e sem limite de usos. A Bella
valida o código pela API do Discord, impede que o mesmo link seja atribuído a
duas pessoas e monitora novas entradas enquanto estiver online. O canal de
materiais não recebe botões ou controles desse sistema.

Ao registrar uma indicação, o parceiro primeiro escolhe o tipo de contato
(WhatsApp, e-mail, telefone, Discord ou outro) e depois informa o dado autorizado.
A Bella valida o formato, guarda tipo e valor separadamente e envia o relatório
administrativo completo para o canal `1552631041175461979`, incluindo parceiro,
pessoa/empresa, contato, necessidade, contexto e confirmação de origem legítima.
Nesse relatório, a administração pode marcar a indicação como **Em análise**,
**Aceita**, **Em negociação**, **Projeto fechado**, **Finalizada** ou **Recusada**.
Cada alteração atualiza **Minhas indicações** e tenta avisar o parceiro por DM; a
recusa exige um motivo. A sincronização remove os canais temáticos antigos apenas
quando ainda possuem exatamente os nomes gerados pela Bella.

## Hospedagem na Square Cloud

O projeto inclui `squarecloud.app` pronto para deploy na Square Cloud. Para subir
o bot, envie o ZIP do projeto com `bot.py`, `requirements.txt`, `.env` e
`squarecloud.app` na raiz. O passo a passo completo está em
`SQUARE_CLOUD_DEPLOY.md`.

### Central de Hospedagem

- `/hosting`: abre a Central de Hospedagem com botões para visão geral, métricas,
  health check, logs, deploys, apps e ações rápidas.
- `/hosting canal:#canal fixar:true`: envia o painel fixo em um canal para
  consultar tudo pelos botões.
- As imagens do painel usam ícones SVG locais do Lucide em
  `website_guard/assets/lucide`.
- `/apps`: lista as aplicações disponíveis na Square Cloud e mostra o ID correto.
- `/hosting-configurar app_id:<ID>`: define qual aplicação será monitorada. Use
  este comando quando a Square devolver `APP_NOT_FOUND`.
- `/status`, `/estatisticas-hosting`, `/hosting-logs`, `/deploys`, `/health`,
  `/domains`, `/backups`, `/database`, `/network`, `/billing`, `/restart`,
  `/start` e `/stop` continuam disponíveis para acesso direto.

## Aprenda Inglês com Bella

O módulo `/ingles` transforma a Bella em uma central de aprendizado de inglês
com foco em tecnologia, gamificação e progresso visual. Os dados ficam em
`data/english_learning.db`.

- `/ingles perfil`: gera um perfil premium em imagem com avatar, XP, nível,
  sequência, ranking, conquistas e progresso.
- `/ingles painel`: envia o painel público para iniciar estudos. Ao clicar em
  **Começar agora**, a Bella cria uma thread privada de aprendizado para o
  aluno, no mesmo estilo dos tickets. Antes da primeira aula, ele pergunta por
  que a pessoa quer aprender inglês e quais pontos deseja melhorar.
- `/ingles dashboard`: mostra evolução visual com gráficos de XP e tempo.
- `/ingles nivelamento`: executa teste interativo e estima o nível A1 até C2.
- `/ingles objetivo`, `/ingles plano` e `/ingles continuar`: definem objetivo,
  mostram plano personalizado e retomam os estudos.
- `/ingles quiz`: gera quizzes inéditos com Groq conforme nível, objetivo e foco
  do aluno, com botão para continuar para o próximo quiz.
- `/ingles conversar`, `/ingles entrevista`, `/ingles corrigir`, `/ingles revisar`,
  `/ingles palavra` e `/ingles daily`: cobrem conversação, escrita, vocabulário,
  Tech English e missões diárias.
- `/ingles ranking`, `/ingles conquistas`, `/ingles nivel`, `/ingles sequencia`,
  `/ingles estatisticas`, `/ingles historico`, `/ingles favoritos` e
  `/ingles amigo`: adicionam gamificação, histórico e amigos de estudo.

O módulo usa lógica local para XP, níveis, ranking, conquistas e histórico. A IA
é utilizada somente nas partes que precisam de linguagem natural, como conversa,
correção de texto, entrevista e lições personalizadas.

## Modos de operação

A Bella possui dois modos separados:

- **Público:** disponível para todos quando o bot é mencionado, chamado por
  `Bella, ...` ou recebe Reply em uma mensagem dele. Responde dúvidas gerais,
  tecnologia, programação, negócios, cultura, pedidos de ajuda, conversas e
  referências de memes. A Groq é o cérebro principal de todas essas respostas.
- **Executivo:** exclusivo ao Discord ID `933780111742877777`. Interpreta ações
  administrativas localmente, valida alvos e executa somente depois das
  verificações de permissão, hierarquia e segurança do código.

Por segurança e para não interromper conversas do servidor, o modo público exige
menção por padrão. Defina `AI_PUBLIC_REQUIRE_MENTION=false` somente se desejar
que todas as mensagens sejam avaliadas. Perguntas não mencionadas continuam
podendo usar o modo seletivo legado quando possuem correspondência oficial forte.

Antes de responder, a Bella combina até 30 mensagens recentes do canal, memória
temporária do usuário, conteúdo oficial do site e canais institucionais do
servidor. A Groq interpreta a intenção, entende continuidade e produz a resposta
final mesmo em saudações, memes e perguntas simples. A camada local existe apenas
para ações administrativas seguras e contingência quando a API está indisponível.

## Assistente executivo por linguagem natural

Quando o própria Bella é mencionado, a camada administrativa é avaliada antes
do modo público. Somente o usuário definido em
`933780111742877777` pode acioná-la. Esse ID é fixo na camada executiva e não
pode ser substituído por uma configuração acidental. Outros usuários são
encaminhados ao modo público e nunca recebem acesso às ações administrativas.

Exemplos suportados:

```text
@Bella adiciona o cargo @Moderador ao @João
@Bella remove o cargo @Moderador do @João
@Bella troca o cargo @Cliente por @Moderador no @João
@Bella lista os cargos do @João
@Bella bane @João
@Bella tire o banimento do usuário ID 1362513159193366711
@Bella revogue todas as punições do usuário ID 1362513159193366711
@Bella expulsa @João
@Bella silencia @João por 30 minutos
@Bella remove o silenciamento do @João
@Bella desconecta @João do canal de voz
@Bella apague as últimas 100 mensagens
@Bella apague 3 mensagens do chat
@Bella limpe este chat
@Bella tranque este canal
@Bella destranque este canal
@Bella mude o nome deste canal para novidades
@Bella coloque neste canal o tópico Atualizações oficiais da comunidade
@Bella ative o modo lento por 30 segundos
@Bella crie um canal de texto chamado comunicados
@Bella crie um canal de voz chamado Reunião
@Bella envie em #anuncios o texto A reunião começa às 20h
@Bella fixe esta mensagem
@Bella crie um cargo chamado Parceiro
@Bella renomeie o cargo @Parceiro para Parceiro Oficial
@Bella altere o apelido do @João para João Silva
@Bella mova o @João para #Reunião
@Bella crie um evento chamado Reunião amanhã às 20h em #voz
@Bella verifica o status da plataforma
@Bella sincroniza os feedbacks
@Bella atualiza o conhecimento do site
@Bella verifica as alterações do site
@Bella envia o relatório semanal
@Bella lembre que prefiro relatórios detalhados
@Bella esqueça a preferência formato_relatorio
@Bella liste suas memórias administrativas
```

Configurações naturais compatíveis:

```text
@Bella usa #auditoria para os logs de auditoria
@Bella define #feedbacks como canal de feedbacks
@Bella configura #alteracoes como canal de alterações
@Bella usa #relatorios para os relatórios
@Bella define #semanal como canal semanal
@Bella configura #geral para recepção
```

A Groq interpreta pedidos administrativos em linguagem natural antes da
conversa pública, inclusive quando a ordem é escrita como pergunta ou usa
referências como “este canal”, “essa mensagem” e “esse ticket”. O executor é um
registro extensível de ações reais do Discord; ele não executa código, shell ou
operações fora desse catálogo. O código resolve somente membros, cargos e canais
reais, rejeita negações, alvos ambíguos e solicitações incompletas. Banimentos,
expulsões, exclusões, cargos e silenciamentos só são executados depois de validar
o proprietário, as permissões e a hierarquia do bot. Toda tentativa é auditada.
Ao revogar um banimento, a Bella aceita o ID mesmo que o usuário não esteja no
servidor, remove o registro de ban temporário, envia uma DM e inclui o convite
oficial definido em `SERVER_INVITE_URL`.

## Agente pessoal (website_guard/bella_agent.py)

Tudo em linguagem natural, só para o proprietário, sem frase fixa:

- **Pedidos compostos**: "cria um canal privado suporte-x, libera pro @x e manda
  boas-vindas lá" vira um plano com etapas e botões Executar/Cancelar.
- **Desfazer e continuidade**: "desfaz isso" reverte a última ação (ban, timeout,
  cargos, trancar canal, mensagem enviada, enquete, canal criado, acesso).
  "faz o mesmo com o @y" reaproveita a última ação. Expulsão, exclusões e limpeza
  de mensagens não têm volta e por isso sempre pedem confirmação antes.
- **Posts com aprovação**: mensagem para outro canal mostra prévia com
  Publicar/Editar/Cancelar.
- **Briefing**: "o que rolou enquanto eu tava fora?" manda por DM menções ao
  Talisson (urgentes primeiro), tickets, alertas de moderação, novos membros,
  pendências e lembretes de hoje. Também chega sozinho todo dia às
  `AGENT_BRIEFING_HOUR`.
- **Sugestão de moderação**: com a moderação autônoma ligada, ocorrências de
  risco chegam por DM com Banir/Silenciar 1h/Ignorar.
- **Memória**: "anota que preciso X", "o que eu tenho pendente?", "terminei a 2",
  "me lembra disso amanhã às 10h" (em reply guarda o link da mensagem),
  "o que o @x falou sobre preço semana passada?".
- **Enquetes**: "faz uma enquete se a live é sábado ou domingo" usa a enquete
  nativa do Discord.
- **Stripe**: "cria um pagamento de 1.500 pro site institucional do João" mostra
  valor, serviço e cliente e só cria o link (uso único) depois do botão Criar
  link. Se o cliente foi @mencionado, oferece mandar o link por DM. Configure
  `STRIPE_SECRET_KEY` (comece com `sk_test_`) e `STRIPE_CURRENCY`.

## Agente do dono, orçamento por entrevista e lembretes por DM

Detalhes em [docs/BELLA_AGENTE_DONO.md](docs/BELLA_AGENTE_DONO.md):

- **Canal privado do dono**: conversa sem mencionar; agenda do dia sem IA, agenda falada por áudio, cofre de decisões, resumo da semana (sexta 20h), dúvidas mais repetidas da comunidade, ranking mensal da staff, clientes sumidos (com aprovação antes de enviar), alertas de saída em massa e canal parado.
- **Ticket "Solicitar Orçamento"**: a Bella entrevista o cliente com o formulário de desenvolvimento (e o de bot de Discord, quando for o caso), recusa respostas só "sim/não" e entrega um resumo administrativo antes de chamar a equipe.
- **Lembretes por DM** 5 minutos antes, persistentes, com fuso America/Recife.
- **Memória com escopos** (GLOBAL/USER/GUILD/PROJECT) e **menções reais do Discord** com notificação restrita.
- **FreeLLMAPI** com nova tentativa, backoff, prazo total, logs por chamada e mensagem única de instabilidade.

## FreeLLMAPI (IA grátis com vários provedores)

A Bella pode usar a [FreeLLMAPI](https://github.com/tashfeenahmed/freellmapi), que
junta vários provedores gratuitos (Groq, Gemini, Cerebras, OpenRouter, Mistral...)
num endpoint compatível com a OpenAI e troca de provedor sozinha quando um bate
o limite. O cliente fica em `src/ai/freellmapi_client.py`.

1. Rode a FreeLLMAPI na mesma máquina (Docker ou app desktop) e cadastre as
   chaves dos provedores no painel dela (página Keys). Inclua a chave da Groq e
   a do Gemini que a Bella já usa.
2. Copie a chave unificada (`freellmapi-...`) para `FREELLMAPI_API_KEY`.
3. Troque `FREELLMAPI_ENABLED=true` e reinicie a Bella.

Com isso, toda a IA da Bella (conversa, tickets, agente, visão) passa pela
FreeLLMAPI. Se ela estiver fora do ar ou sem cota, a mesma chamada é repetida
na Groq direta (se `GROQ_API_KEY` existir). A conversa deixa de usar o Gemini
direto, cuja cota diária é pequena, e o tempo limite social sobe para 8 s.
Modelos: `FREELLMAPI_MODEL` (conversa), `FREELLMAPI_JSON_MODEL` (interpretação
de ordens) e `FREELLMAPI_VISION_MODEL` (imagens). Cada um é uma lista em ordem,
separada por vírgula (ex.: `gemini-2.5-flash,gpt-oss-120b`): se um modelo falhar
ou demorar mais que `FREELLMAPI_ATTEMPT_TIMEOUT_SECONDS`, a Bella tenta o próximo.
Prefira modelos específicos: medido no servidor, respondem em 0,3–1,2 s, enquanto
`auto` testa o catálogo inteiro e passou de 20 s.

Para economizar RAM em servidores pequenos, `RENDER_DYNAMIC_PAGES=false` desliga
o Chromium: o FAQ do site continua sendo lido e as páginas aprendidas na última
leitura completa são mantidas.

## Central inteligente de tickets

Use `/setup-ticket` para escolher canal de abertura, categoria organizacional,
canal de logs, cargo de suporte e cargo administrativo. O painel público utiliza
Discord Components V2 com `Container`, `TextDisplay`, `MediaGallery` e Select
Menu. O configurador permite editar:

- Author, ícone, título, descrição, campos e cor.
- Footer, ícone, thumbnail, banner e timestamp.
- Texto e emoji dos botões.
- Placeholder e opções do menu Select.
- Modo de abertura: somente menu, somente botão ou menu com botão.
- Orientações usadas pela Groq na recepção e no encerramento.

Depois clique em **Publicar painel**. Cada atendimento cria uma **Thread privada**,
nunca um novo canal. O usuário e a equipe configurada são adicionados à Thread.
O modo padrão é **Somente menu**: o usuário escolhe claramente o tipo de
atendimento e o ticket já nasce com a categoria selecionada, sem o botão geral.
A Bella recebe o usuário com texto dinâmico, processa as mensagens com Groq,
classifica categoria e prioridade, orienta dúvidas simples e encaminha
orçamentos, reuniões, propostas, negociações e solicitações diretas.

Ao escalar, o sistema menciona o proprietário `933780111742877777`, apresenta
um resumo executivo e registra o status. Ao encerrar, gera uma despedida
contextual, transcript completo, duração, participantes, categoria e resumo,
enviando tudo ao canal de logs. A equipe usa `/ticket-fechar`,
`/ticket-reabrir`, `/ticket-status`, `/ticket-prioridade` e `/ticket-metricas`.

O proprietário também pode administrar o ticket sem comandos:

```text
@Bella mude o nome desse ticket pra Pendente Orçamento
@Bella deixe o status como Aguardando cliente
@Bella coloque a prioridade como Alta
@Bella atribua este atendimento à @Anna
@Bella encaminhe este ticket para a equipe
@Bella encerre este atendimento
```

Essas ordens passam pelo mesmo interpretador Groq usado no restante do servidor.
Status personalizados, como `Pendente Orçamento`, são mantidos como rótulos de
fluxo sem quebrar o estado técnico necessário para o ticket continuar ativo.

Os tickets possuem uma rota rápida independente do chat público. Saudações,
problemas técnicos evidentes, pedidos comerciais e solicitações diretas ao
Talisson são tratados imediatamente. Casos complexos usam um serviço Groq
dedicado que prioriza o modelo rápido, trabalha com histórico compacto e possui
fallback local. A recepção é exibida sem aguardar a API e pode ser refinada em
segundo plano.

## Ausência, recados e lembretes

A Bella atua como secretário executivo do Talisson. Apenas o Discord ID
`933780111742877777` pode ativar ou remover a própria indisponibilidade por
linguagem natural:

```text
Bella amanhã vou estar em consulta médica.
Bella estarei ocupado hoje.
Bella vou viajar.
amanhã eu vou ficar ausente por causa de uma reunião.
Bella voltei.
Bella estou disponível novamente.
```

Quando Talisson está indisponível e alguém menciona ou pergunta por ele, o
Bella explica a ausência com linguagem natural e oferece registrar um recado.
A próxima mensagem do usuário é tratada como continuação da conversa, sem exigir
novo comando.

Mensagens fortes de ausência ou retorno enviadas pelo próprio Talisson também
são entendidas mesmo sem mencionar a Bella, para evitar que o status pessoal
caia no fluxo comum de tickets ou conversa pública.

Também são registrados convites e pedidos de contato, como:

```text
Bella convida o Talisson para minha party.
Bella fala para o Talisson entrar na call.
Bella preciso falar com o Talisson.
Bella diz pro Talisson olhar o WhatsApp.
```

Os recados recebem categoria automática, como Parceria, Suporte, Bug, Convite,
Ideia, Sugestão, Reclamação, Contato ou Urgente, e são enviados ao canal
configurado como mensagens normais do Discord, sem embed. Todo recado menciona
Talisson, menciona o autor, inclui conteúdo completo, categoria, canal de
origem, horário e link direto para a mensagem original.

Comandos exclusivos de Talisson:

```text
/bella lembretes canal
/bella lembretes status
```

## Anúncios inteligentes

Talisson pode pedir anúncios em linguagem natural. Quando a solicitação já traz
assunto, canal, cargo e link, a Bella interpreta tudo e publica diretamente sem
perguntas adicionais.

```text
Bella anuncie que a Cheetah Solutions Network está procurando desenvolvedores Full Stack.
Mencione o cargo Desenvolvedor e envie no canal de vagas.
Use este link: https://exemplo.com/vaga
```

O texto é gerado com Groq quando disponível e publicado como mensagem normal,
sem embed, painel ou cartão. Se a Groq estiver indisponível, a Bella usa uma
versão local segura com os dados fornecidos por Talisson.

## Agenda e compromissos

O horário oficial de atendimento é de segunda a sexta, das 09h às 18h, sempre
mediante agendamento prévio. Sábado e domingo ficam indisponíveis para
atendimentos.

Perguntas como `O Talisson está disponível?`, `Qual o horário de atendimento?`
ou `Como faço para marcar um horário?` recebem resposta natural. Pedidos de
agendamento criam uma continuação temporária e a resposta seguinte do usuário é
registrada no canal de lembretes como solicitação de agendamento.

Talisson também pode criar compromissos pessoais:

```text
Bella amanhã às 19h tenho uma reunião sobre a Cheetah.
Bella me lembre sexta-feira às 14h de ligar para um cliente.
Bella quais compromissos tenho amanhã?
```

A Bella salva título, descrição, data, horário, criador, prioridade e status. As
notificações são enviadas no canal de lembretes configurado, por padrão 24 horas,
2 horas e 30 minutos antes do compromisso. Depois do horário, o compromisso é
marcado como concluído.

## Memória operacional persistente

Talisson pode ensinar novas regras permanentes à Bella por linguagem natural.
Apenas o Discord ID `933780111742877777` pode criar, editar ou remover regras.
Tentativas de outros usuários são ignoradas.

Exemplos:

```text
Bella, a partir de agora responda dúvidas técnicas com mais objetividade.
Bella, lembre-se que o canal #duvidas é exclusivo para dúvidas direcionadas a você.
Bella, registre essa diretriz: nunca exponha detalhes técnicos em canais públicos.
Bella, liste todas as regras salvas.
Bella, remova a regra do canal #duvidas.
Bella, atualize a regra do canal #duvidas para permitir apenas perguntas.
```

Também é possível definir tema, checklist e atendimento automático em uma única
ordem:

```text
@Bella o canal #ajuda-codigo é para ajuda de programação. Exija linguagem ou
tecnologia, objetivo, código relevante, mensagem de erro e o que a pessoa já
tentou. Ajude quem enviar a dúvida completa e apague mensagens sem contexto.
```

O Groq transforma essa ordem uma única vez em uma política persistente. Depois,
a validação do checklist e a exclusão de mensagens incompletas funcionam
localmente, sem consumir IA a cada mensagem. Quando a solicitação está completa,
o canal ativa a Bella automaticamente, mesmo sem nova menção, e a Groq responde
considerando o tema, o código, o erro e as tentativas informadas.

Quando a regra é clara, ela é salva automaticamente. Quando a intenção parece
ambígua, a Bella pergunta se a instrução deve se tornar permanente. Regras
vinculadas a canal podem ser aplicadas automaticamente; por exemplo, um canal
exclusivo para dúvidas pode remover mensagens fora de contexto e orientar o
usuário com uma resposta curta. As regras também entram como contexto autorizado
nas respostas da Groq, sem permitir execução arbitrária de moderação.

## Bella Anti-Scam Vision

O Anti-Scam Vision é uma moderação visual opcional baseada em Groq Vision. Ele
fica **desativado por padrão** para evitar consumo desnecessário da API. Quando
ativado, imagens publicadas no Discord passam por cache local de hash SHA-256
antes de qualquer chamada à Groq.

Comandos exclusivos de Talisson:

```text
/bella antiscam on
/bella antiscam off
/bella antiscam status
```

Fluxo:

- Imagem nova: calcula hash, consulta cache e envia ao Groq Vision se necessário.
- Resultado `SAFE` ou `SUSPEITO`: registra cache e não expõe detalhes publicamente.
- Resultado `SCAM` acima da confiança mínima: apaga a mensagem, salva o hash e
  envia relatório ao canal de segurança.
- Imagem repetida: reutiliza o resultado salvo sem nova chamada à Groq.

Configuração modular em `config/antiscam.json`:

```json
{
  "enabled": false,
  "logChannelId": 0,
  "confidenceThreshold": 0.88,
  "cacheEnabled": true,
  "aiEnabled": true
}
```

As categorias de golpe ficam no mesmo arquivo e podem ser ampliadas sem mudar a
arquitetura principal. O relatório técnico fica somente nos logs administrativos;
nenhuma imagem removida é reenviada ao canal público.

Todas as tentativas autorizadas geram uma embed no canal de auditoria configurado,
incluindo executor, alvo, cargo, ação, servidor, canal, resultado e duração. A
resposta pública contém somente uma confirmação amigável, sem IDs ou detalhes
técnicos. Ela usa Reply, menciona somente o proprietário e apresenta cargos e
usuários pelos nomes legíveis, nunca pelas menções técnicas do Discord.

Permissões necessárias para todas as ações:

- Gerenciar Cargos.
- Expulsar Membros.
- Banir Membros.
- Moderar Membros.
- Mover Membros.
- Gerenciar Mensagens.
- Gerenciar Eventos.
- Gerenciar Canais.
- Criar Threads Privadas.
- Enviar Mensagens em Threads.
- Gerenciar Threads.
- Ver, enviar e ler mensagens nos canais envolvidos.

O maior cargo da Bella deve permanecer acima dos cargos e usuários que ele
precisa administrar. Configure o canal interno com `/logs setup` ou pela frase
natural de auditoria antes de usar as ações.

## Conhecimento oficial

A base é atualizada automaticamente a cada seis horas, por padrão:

- As perguntas e respostas publicadas em `/faq`.
- As páginas definidas em `KNOWLEDGE_PAGES`.
- Todas as páginas públicas internas descobertas pelos links do site.
- Redes sociais, e-mails e contatos encontrados no DOM publicado.

O site atual é uma SPA. O bot encontra o bundle JavaScript publicado e extrai o
FAQ oficial, além de usar Chromium para ler o conteúdo que o visitante realmente
vê. A última base válida permanece disponível caso uma atualização temporária
falhe.

A comparação usa intenção interrogativa, entidades, normalização de português,
conceitos equivalentes, similaridade de termos, relação com o site e qualidade
da resposta. Talisson, Tali, Talinho, TalinhoDev, TS, CEO/fundador da Cheetah e
a menção configurada são tratados como a mesma entidade. As respostas são
reescritas em terceira pessoa e não copiam a voz em primeira pessoa do site.

A busca local funciona como RAG: recupera silenciosamente os trechos oficiais
mais relevantes e os envia à Groq junto com a conversa. O modelo decide quando
esse conhecimento é pertinente e constrói uma resposta natural, sem copiar o site
ou revelar a origem interna. Perguntas gerais continuam sendo respondidas
normalmente pela Groq, mesmo quando não possuem relação com Talisson.

Configuração:

```dotenv
MESSAGE_CONTENT_INTENT=true
KNOWLEDGE_ENABLED=true
KNOWLEDGE_CHANNEL_IDS=
KNOWLEDGE_REFRESH_INTERVAL_SECONDS=21600
KNOWLEDGE_MIN_CONFIDENCE=0.90
KNOWLEDGE_MATCH_MIN=0.78
QUESTION_INTENT_MIN=0.90
KNOWLEDGE_USER_COOLDOWN_SECONDS=30
KNOWLEDGE_MAX_MESSAGE_LENGTH=500
KNOWLEDGE_PAGES=/,/sobre,/feedbacks,/faq,/contato,/politica-de-privacidade
KNOWLEDGE_AUTO_DISCOVER=true
KNOWLEDGE_MAX_PAGES=20
TALISSON_USER_ID=933780111742877777
```

Com `KNOWLEDGE_CHANNEL_IDS` vazio, todos os canais de texto do servidor podem ser
avaliados. Use `/definir-canal-conhecimento` para restringir o recurso a um canal.

### Conhecimento do Discord

A Bella cria um índice separado para canais institucionais como `#sobre`,
`#faq`, `#anuncios`, `#roadmap`, `#changelog`, `#status`, `#suporte` e
`#feedbacks`. O índice considera nome, categoria, tópico, mensagens fixadas e
mensagens informativas publicadas pela equipe. Canais técnicos, privados, de
auditoria, tickets e logs são descartados.

```dotenv
DISCORD_KNOWLEDGE_ENABLED=true
DISCORD_KNOWLEDGE_CHANNEL_IDS=
DISCORD_KNOWLEDGE_REFRESH_SECONDS=1800
DISCORD_KNOWLEDGE_MAX_CHANNELS=30
DISCORD_KNOWLEDGE_MESSAGES_PER_CHANNEL=40
```

Com a lista de IDs vazia, o bot seleciona automaticamente canais institucionais
públicos. O índice é atualizado periodicamente e também quando canais ou
mensagens institucionais mudam.

### Conhecimento oficial da Bella

O projeto também possui fatos institucionais curados que não dependem do
conteúdo do site. A hospedagem oficial da Bella é a **Square Cloud**. Quando
questionado sobre infraestrutura ou hospedagem, a Groq recebe esse fato como
contexto prioritário e responde naturalmente.

A personalidade reconhece gírias e memes brasileiros. Por exemplo, “o que sobra
pro beta?” é tratado como meme — não como referência a uma versão beta de
software. O histórico do canal é usado apenas quando necessário para entender
continuidade e não deve contaminar perguntas independentes.

### Groq

A chave nunca fica hardcoded no código. Gere uma chave no Groq Console e
configure somente no arquivo `.env` local:

```dotenv
GROQ_ENABLED=true
GROQ_API_KEY=sua_nova_chave
GROQ_MODEL=openai/gpt-oss-20b
GROQ_VISION_MODEL=qwen/qwen3.6-27b
GROQ_TIMEOUT_SECONDS=15
GROQ_MAX_OUTPUT_TOKENS=650
GROQ_MAX_JSON_OUTPUT_TOKENS=1200
GROQ_MAX_PROMPT_CHARS=2600
GROQ_CONCURRENCY=2
GROQ_RETRY_DELAY_SECONDS=1.2
GROQ_CACHE_TTL_SECONDS=7200
GROQ_DAILY_REQUEST_LIMIT=150
GROQ_DAILY_RESET_TIMEZONE=America/Sao_Paulo
AI_MIN_CONFIDENCE=0.90
AI_PUBLIC_ENABLED=true
AI_PUBLIC_REQUIRE_MENTION=true
AI_ALLOWED_GUILD_IDS=1467976297689911377
AI_PUBLIC_MIN_CONFIDENCE=0.72
AI_CONTEXT_MESSAGES=5
AI_CONTEXT_TTL_MINUTES=15
AI_MAX_RESPONSE_CHARS=1800
```

As ações administrativas comuns não dependem de IA. O gateway utiliza um único
modelo Groq, repete uma vez somente em falhas transitórias e aplica um cooldown
curto para evitar ciclos de erro. Para contas gratuitas/on-demand da Groq,
`GROQ_CONCURRENCY=2`, prompts compactos e `GROQ_MAX_OUTPUT_TOKENS=650` equilibram
estouro de TPM, que é limite de tokens por minuto, não limite diário local.

O cliente Gemini usa uma fila própria (`GEMINI_CONCURRENCY=2`). Quando uma chamada
síncrona excede o timeout, a vaga permanece ocupada até a thread realmente terminar,
evitando acumular requisições invisíveis no processo.

As seções volumosas do estado podem ser externalizadas com
`STATE_EXTERNAL_SECTIONS`. Na primeira migração, a Bella preserva automaticamente
`data/state.pre-externalization.backup.json`; os arquivos laterais ficam em
`data/state_sections/`, enquanto SQLite continua como espelho periódico compatível.

Se `GROQ_ENABLED=true` e a chave estiver ausente, o módulo não inicia, o erro é
registrado em `data/state.db` e o restante do bot continua operacional.

A Groq é o mecanismo principal de intenção e resposta no modo público. Ela não
executa comandos administrativos: essas solicitações são interceptadas e
validadas localmente antes do fluxo conversacional por motivos de segurança.

O modelo `openai/gpt-oss-20b` atende conversas rápidas, econômicas e
interpretação estruturada. Monitoramento e relatórios determinísticos continuam
funcionando localmente para não consumir requisições desnecessárias.
`GROQ_VISION_MODEL=qwen/qwen3.6-27b` mantém um modelo multimodal dedicado. Imagens são
enviadas pela URL temporária do Discord, preservando OCR e análise contextual sem
estourar o limite de payload base64.

Preferências definidas pelo proprietário podem ser mantidas na seção
`executive_memory` do banco SQLite. A listagem é enviada por mensagem privada.
O sistema recusa memórias que pareçam conter tokens, senhas, segredos ou chaves.

### Arquitetura da IA

Toda chamada à API passa exclusivamente por `src/ai/groq_service.py`:

```text
src/
└── ai/
    ├── intent_router.py
    ├── local_conversation.py
    ├── channel_context.py
    ├── server_knowledge.py
    ├── personality.py
    ├── personality_engine.py
    ├── prompts.py
    ├── memory.py
    ├── context.py
    ├── knowledge.py
    ├── groq_service.py
    ├── moderation_ai.py
    └── public_assistant.py
```

`intent_router.py` protege e separa ações administrativas antes da conversa;
`local_conversation.py` fornece somente contingência quando a API falha;
`channel_context.py` acompanha a conversa recente; `server_knowledge.py` indexa
o Discord; `context.py` unifica as fontes; `memory.py` mantém fatos temporários;
e `moderation_ai.py` bloqueia frases proibidas e vazamento de segredos.

## Bella Intelligence Center

O monitoramento utiliza eventos operacionais estruturados. Alertas, incidentes,
recuperações, alterações semânticas, feedbacks, anomalias de performance e
relatórios são enviados à Groq com métricas, histórico, evidências e contexto
da página. A resposta pública contém interpretação, impacto, tendência, pontos de
atenção e recomendações baseadas nos dados coletados.

Os destinos são separados:

- **Canal de monitoramento:** resumos executivos, indicadores, análises e
  recomendações.
- **Canal técnico:** exceções, evidências brutas, falhas de coleta e informações
  de diagnóstico.

Configure o canal técnico com `/definir-canal-tecnico`. Enquanto ele não estiver
configurado, detalhes internos permanecem apenas nos arquivos de log locais e
nunca são publicados nos canais executivos.

O monitor de páginas acompanha conteúdo, botões, formulários, links, imagens,
seções, inventário de páginas, SEO, dados estruturados, tecnologias detectadas e
métricas de carregamento. Mudanças como experiência profissional, contatos e
novos serviços são enviadas à Groq com o trecho anterior e o atual para
interpretação semântica.

O Health Score e o Intelligence Score são calculados dinamicamente a partir de
disponibilidade, performance, cobertura do monitor, conteúdo, SEO, feedbacks,
segurança e estabilidade. Os relatórios incluem tendências de 7, 30 e 90 dias.

## Relatórios automáticos

Os relatórios diário, semanal e mensal são enviados mesmo quando não há
incidentes novos. Cada execução utiliza Groq e contém:

- Resumo executivo e principais eventos.
- Health Score, Intelligence Score e indicadores operacionais.
- Pontos de atenção, tendências, conclusão e recomendações.
- Audiência real quando GA4, access log ou Vercel Drain estiver disponível.

Na primeira inicialização, a Bella registra a janela diária e mensal atual sem
duplicar relatórios antigos. Os envios seguintes respeitam os horários
configurados. O relatório semanal mantém sua janela própria.

```dotenv
WEEKLY_REPORT_ENABLED=true
WEEKLY_REPORT_WEEKDAY=0
WEEKLY_REPORT_HOUR=9
WEEKLY_REPORT_MINUTE=0
WEEKLY_REPORT_TIMEZONE=America/Sao_Paulo
DAILY_REPORT_ENABLED=true
DAILY_REPORT_HOUR=8
DAILY_REPORT_MINUTE=0
MONTHLY_REPORT_ENABLED=true
MONTHLY_REPORT_DAY=1
MONTHLY_REPORT_HOUR=9
MONTHLY_REPORT_MINUTE=0
```

`WEEKLY_REPORT_WEEKDAY` usa `0` para segunda-feira e `6` para domingo.

### Fonte das visitas

O bot não trata as próprias verificações como visitas. As métricas de audiência
são calculadas somente a partir de fontes reais. A opção recomendada, gratuita e
sem Vercel Pro é o **Google Analytics 4 Data API**. Ela fornece:

- Visualizações de página.
- Sessões e usuários.
- Páginas e origens mais acessadas.
- Países, dispositivos e navegadores.
- Atividade diária para o relatório semanal.

Primeiro crie uma propriedade GA4 e um fluxo Web para
`https://www.talissonsouza.com.br`. Instale no site a Google tag `G-...` exibida
em **Administrador > Fluxos de dados > Web > Ver instruções da tag** e publique
o site. O Google recomenda manter a medição otimizada habilitada para coletar
pageviews automaticamente.

Depois habilite a **Google Analytics Data API** no projeto Google Cloud. Existem
duas formas de autenticação:

1. Conta de serviço: conceda ao e-mail da conta o papel `Viewer` na propriedade
   GA4 e salve o JSON como `data/google-analytics-credentials.json`.
2. Conta do usuário: execute o login de Application Default Credentials abaixo
   e deixe `GOOGLE_APPLICATION_CREDENTIALS` vazio.

```powershell
gcloud auth application-default login --scopes="https://www.googleapis.com/auth/cloud-platform,https://www.googleapis.com/auth/analytics.readonly"
```

Configuração da Bella:

```dotenv
GA4_PROPERTY_ID=123456789
GOOGLE_APPLICATION_CREDENTIALS=data/google-analytics-credentials.json
GA4_CACHE_MINUTES=30
```

`GA4_PROPERTY_ID` é o número da propriedade, não o Measurement ID `G-...`. Para
usar as credenciais da conta do usuário, deixe
`GOOGLE_APPLICATION_CREDENTIALS=` vazio. A Bella consulta a Data API no momento
do relatório e mantém um cache curto para respeitar as cotas.

O GA4 não fornece status HTTP nem separação detalhada de crawlers pela Data API.
Para essas métricas técnicas, use como fonte secundária um access log
Common/Combined de Nginx, Apache, Caddy, Cloudflare ou outro proxy:

```dotenv
TRAFFIC_LOG_PATH=data/traffic.ndjson
TRAFFIC_HOSTS=www.talissonsouza.com.br,talissonsouza.com.br
TRAFFIC_SCAN_INTERVAL_SECONDS=60
TRAFFIC_SESSION_MINUTES=30
TRAFFIC_RETENTION_DAYS=45
TRAFFIC_MAX_EVENTS=100000
```

O arquivo e sua pasta são criados automaticamente. O coletor separa usuários, o
monitor Bella, crawlers, outras automações e arquivos estáticos, remove query
strings e guarda apenas um hash irreversível do IP combinado com o navegador.

O receptor de Vercel Drain continua disponível apenas como compatibilidade
opcional. Em planos compatíveis, acesse
**Settings > Drains**, crie um **Custom endpoint**, selecione **Web Analytics**
com formato JSON ou NDJSON e informe a URL HTTPS pública que encaminha para o
receptor da Bella. Esse formato fornece sessões e dispositivos próprios da
Vercel, além de país, navegador e tipo de dispositivo quando disponíveis.

O bot também pode receber o Drain diretamente:

```dotenv
TRAFFIC_INGEST_ENABLED=true
TRAFFIC_INGEST_HOST=127.0.0.1
TRAFFIC_INGEST_PORT=8787
TRAFFIC_INGEST_PATH=/vercel-analytics
TRAFFIC_SIGNATURE_SECRET=segredo_de_assinatura_do_drain
```

Exponha esse endpoint por HTTPS usando o proxy ou túnel da sua infraestrutura e
cadastre a URL no Vercel. O corpo bruto é validado pelo header
`x-vercel-signature` com HMAC-SHA1 e comparação em tempo constante antes de
qualquer evento ser aceito. Depois da validação, cada lote é salvo em
`data/traffic.ndjson` e processado imediatamente.

Drains exigem um plano compatível da Vercel. Eles não são necessários quando o
GA4 está configurado.

## Status público, voz e recepção

Use `/conectar-voz` escolhendo um canal, ou execute o comando enquanto estiver
conectado a um. O canal fica salvo e um worker restaura a conexão após quedas ou
reinicializações. `/desconectar-voz` encerra a conexão e desativa a reconexão.
O bot entra surdo e mudo por padrão, sem reproduzir ou gravar áudio.
O setup instala as dependências oficiais `discord.py[voice]`, incluindo suporte
ao protocolo DAVE exigido pelas versões atuais do Discord.

O grupo `/voice-status` transforma um canal de voz em painel público. O setup
bloqueia a conexão de membros, mantém a Bella conectado, restaura a configuração
após reinicializações e alterna nomes seguros com latência, disponibilidade e
estado operacional. O intervalo mínimo é de cinco minutos para respeitar os
limites do Discord.

O grupo `/presence` controla a presença dinâmica com Playing, Watching,
Listening, Competing e Custom Status. Incidentes mostram apenas mensagens
públicas genéricas; IPs, tokens, endpoints e detalhes internos nunca entram na
presença nem no nome do canal.

`/presence painel` publica a Central de Presença em Components V2. Os botões
ativam os sistemas reais já existentes da Bella: agenda diária, NEFFEX,
Square Cloud, rotação dinâmica e streamer. Os selects e o modal de personalizado
permitem definir Online, Ausente, Não perturbe ou Invisível com atividade
Custom, Playing, Watching, Listening, Competing ou Streaming.

Em `/presence setup`, `status_ativos` aceita uma lista ordenada por vírgulas. A
ordem define a prioridade. Valores disponíveis: `infraestrutura`, `site`,
`disponibilidade`, `resposta`, `monitoramento`, `tls`, `sistemas`, `plataforma`,
`health` e `personalizado`. O mesmo setup permite personalizar as mensagens
públicas de degradação, indisponibilidade e verificação.

Use `/configurar-recepcao` para definir o canal geral e o tempo entre 5 e 300
segundos. Quando um membro entra, o bot menciona a pessoa, apresenta o
**Escritório do Talisson Souza** em uma embed e remove a mensagem
automaticamente. `/desativar-recepcao` interrompe o recurso.

## Outros recursos

- Monitor HTTP, latência, TLS, headers e conteúdo obrigatório.
- Validação da meta tag com bypass de cache e diagnóstico.
- Incidentes persistentes com abertura, lembrete e recuperação.
- Status DND e atividade `Developed by Talisson Souza`.
- Sincronização dos 13 feedbacks atuais com IDs persistentes.
- Comparação renderizada de páginas e resumo natural de alterações.
- Métricas acumuladas e tempos individuais.
- Relatórios executivos periódicos.
- Gerador de embeds com prévia, campos, imagens, links e edição.
- Leitura opcional de access logs.

## Painéis de estatísticas

A Bella gera gráficos em PNG localmente, sem consumir Groq ou qualquer outra
API de inteligência artificial. Todos os comandos abaixo usam **Discord
Components V2**: `Container` com cor de destaque, `TextDisplay`, indicadores,
separadores, `MediaGallery` e botões de navegação. Esses painéis não utilizam
embeds clássicos nem conteúdo legado. Os PNGs usam fundo preto, painéis de alto
contraste e cores reservadas aos indicadores para preservar a legibilidade:

- `/estatistica site` abre um painel interativo com Visão Geral, Tráfego,
  Audiência e Confiabilidade. Inclui páginas mais visitadas, visitas, visitantes,
  origens, países, dispositivos, navegadores, status HTTP e performance.
- `/estatistica servidor` abre um painel interativo com Visão Geral, Mensagens,
  Voz, Tickets e Moderação. Inclui membros, canais, cargos, boosts, mensagens,
  canais mais utilizados, horários, tempo em voz, rankings de voz e tickets.
- `/estatistica bella` mostra CPU, memória, uptime, latência, Groq, cache,
  banco de dados, respostas, usuários atendidos e tarefas internas.
- `/estatistica perfil`, `/estatistica ranking`, `/estatistica boosts` e
  `/estatistica conhecimentos` geram cards visuais especializados.

Cada painel também pode ser publicado automaticamente, sem repetir o comando:

```text
@Bella envie as estatísticas do site todos os dias às 08:30 em #metricas-site
@Bella publique as estatísticas do servidor toda segunda às 09:00 em #relatorios
@Bella envie o ranking mensalmente no dia 1 às 10:00 em #ranking
@Bella envie o perfil da @Anna toda sexta às 18:00 em #equipe
@Bella liste as agendas de estatísticas
@Bella pare o envio automático das estatísticas do site
```

Site, servidor, Bella, perfil, ranking, boosts e conhecimentos possuem agendas
independentes. Canal, frequência, horário, período analisado e usuário do perfil
ficam persistidos e são restaurados após reinicialização. As publicações
automáticas mantêm o layout Components V2 e os botões ativos por 24 horas.

Os dois comandos aceitam períodos de 7, 30 ou 90 dias. As métricas de mensagens
são coletadas em lote, as sessões de voz são registradas por eventos e os dados
permanecem salvos após reinícios. Somente Talisson pode consultar esses painéis.

## Monitoramento local de identidade

O monitor de nicknames funciona somente por eventos do Discord, sem IA. Ele
normaliza acentos, espaços, caracteres especiais, fullwidth, leetspeak e letras
Unicode semelhantes. Quando encontra um termo bloqueado, restaura o nickname
para o username da conta, envia uma DM e registra o resultado.

Use o painel fixo para administrar o sistema sem espalhar comandos separados:

```text
/bella nicks-painel canal:#canal fixar:true
```

No painel é possível ativar ou pausar, definir o canal de logs, examinar membros,
gerenciar termos bloqueados, nomes permitidos e exceções por usuário ou cargo.

A API oficial do Discord não fornece o campo **About Me** de outros usuários
para bots. Por isso o filtro de biografias é aplicado de forma administrativa
com `/verificar-biografia`, reutilizando as listas, exceções, DM e logs locais:

```text
/monitorar-biografias
/adicionar-biografia-proibida
/remover-biografia-proibida
/listar-biografias-proibidas
/verificar-biografia
/adicionar-biografia-permitida
/autorizar-usuario-biografia
/autorizar-cargo-biografia
```

## Moderação autônoma local

A Bella analisa flood, repetição, menções excessivas, convites, links
suspeitos, phishing, linguagem de golpe, idade da conta e surtos de entrada.
Cada ocorrência alimenta um score com redução progressiva e cache local.
Mensagens podem ser removidas e timeouts aplicados conforme o limite configurado.
Nenhuma chamada à Groq é realizada nesse fluxo.

```text
/configurar-moderacao-autonoma
/ativar-moderacao-autonoma
/desativar-moderacao-autonoma
/risco-usuario
/autorizar-cargo-moderacao
```

## Canal-isca de segurança

O canal-isca é uma proteção local e determinística. Quando um membro publica
qualquer conteúdo no canal configurado, a Bella remove a mensagem, localiza
cópias idênticas enviadas pelo mesmo autor em outros canais, apaga essas cópias
e aplica banimento permanente. O relatório é enviado em Discord Components V2,
sem uso de Groq. Antes da exclusão, a Bella gera uma captura visual contendo
avatar, nome, ID, canal, data, horário, texto original, nomes dos anexos e a
imagem enviada. Essa evidência aparece no `MediaGallery` do relatório de logs.

```text
/configurar-canal-isca
/status-canal-isca
/desativar-canal-isca
```

Na configuração, selecione o canal monitorado, o canal de logs, a quantidade de
mensagens examinadas por canal e a janela histórica. A busca combina um índice
em memória com o histórico recente acessível pela API do Discord.

## Contexto recente

Antes de responder, a Bella lê até 30 mensagens anteriores diretamente do
histórico do canal, incluindo as próprias respostas. Expressões como “essas
ideias”, “essa lista”, “o que você mandou” e “mensagem anterior” ativam a
continuidade contextual mesmo após uma reinicialização do bot, desde que ele
possua a permissão **Ler histórico de mensagens** no canal.

## Comandos

- `/status`
- `/estatistica site`
- `/estatistica servidor`
- `/estatistica bella`
- `/estatistica perfil`
- `/estatistica ranking`
- `/estatistica boosts`
- `/estatistica conhecimentos`
- `/relatorio`
- `/relatorio-diario`
- `/relatorio-semanal`
- `/relatorio-mensal`
- `/logs resumo`
- `/logs setup`
- `/logs status`
- `/trafego`
- `/verificar-alteracoes`
- `/atualizar-conhecimento`
- `/definir-canal`
- `/definir-canal-alteracoes`
- `/definir-canal-relatorios`
- `/definir-canal-semanal`
- `/definir-canal-tecnico`
- `/definir-canal-conhecimento`
- `/conectar-voz`
- `/desconectar-voz`
- `/voice-status setup|canal|iniciar|parar|status|intervalo`
- `/presence painel|setup|iniciar|parar|status|intervalo|personalizar|restaurar|squarecloud|neffex|neffex-parar|neffex-status`
- `/streamer ativar|desativar|status`
- `/configurar-recepcao`
- `/desativar-recepcao`
- `/embed-criar`
- `/embed-editar`
- `/bella migrar-embeds-jarvis`
- `/bella embeds-jarvis-status`
- `/resumir-servidor`
- `/bella feedbacks-site-painel`
- `/bella nicks-painel`

### Migração de embeds antigos

Use `/bella migrar-embeds-jarvis` para localizar embeds antigos enviados pelo
Jarvis nos canais mapeados, copiar conteúdo/embeds para a Bella e normalizar os
textos visíveis para a identidade Bella Nyelle. Por padrão o comando roda em
simulação; execute com `simular:false` para enviar as cópias. Use
`apagar_antigas:true` somente se a Bella tiver permissão **Gerenciar Mensagens**.

`/resumir-servidor` lê os canais de texto que a Bella consegue acessar, usa a
Groq para criar um resumo factual e salva uma versão única na memória permanente.
Ele é limitado ao servidor oficial e a Talisson/cargos administrativos.

## Permissões e auditoria

Todos os comandos administrativos passam por um guard central. Somente membros
com pelo menos um ID de `ADMIN_ROLE_IDS` podem executá-los:

```dotenv
ADMIN_ROLE_IDS=1467982180469833979,1504605819923206204
```

Tentativas autorizadas, negadas e falhas são persistidas em SQLite e podem ser
enviadas para canais configurados por `/logs setup`. Mais de cinco negações em
cinco minutos geram um alerta de comportamento suspeito. Respostas de acesso
negado são sempre ephemeral.

## Feedbacks e alterações

Na primeira sincronização, todos os feedbacks atuais são enviados. Depois, somente
IDs ainda não processados. Um item só é marcado como processado após confirmação
do Discord.

O painel `/bella feedbacks-site-painel` permite sincronizar, ver status e última
análise, configurar ou testar o destino e reenviar todos após confirmação. O botão
de reenvio ignora a marcação de processado e, se o site estiver temporariamente
indisponível, usa a última leitura salva. Se o
canal estiver vazio, a sincronização repopula os feedbacks; caso contrário, envia
somente IDs pendentes. O histórico fica em `data/state.db`, com `state.json`
mantido como backup legível.

A primeira comparação de páginas cria uma linha de base. As seguintes descrevem
trechos adicionados, removidos e modificados. O desafio matemático aleatório do
formulário de contato é normalizado para não gerar falsos alertas.

## Testes

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Consulte `ANALISE_E_CORRECOES.md` para a investigação do alerta original.
# Economia de IA e lembretes locais

- A Groq é reservada para conversas, tickets, anúncios, conteúdo gerativo,
  interpretação semântica de moderação, conhecimento salvo e Anti-Scam Vision.
- Monitoramento, alertas, status e relatórios diário, semanal e mensal são
  consolidados localmente por métricas, contagens, deduplicação e regras.
- O cache local configurado por `GROQ_CACHE_TTL_SECONDS` evita repetir chamadas
  idênticas dentro do período definido.
- O contador local é reiniciado diariamente à meia-noite no fuso configurado em
  `GROQ_DAILY_RESET_TIMEZONE`. Esse reset não altera a cota mantida pela Groq.
- Use `/definir-lembrete` com data, horário, mensagem e antecedência. O texto é
  armazenado e enviado sem resumo, interpretação ou reescrita por IA.

## AutoMod Oficial do Discord

- `/automod` abre o painel administrativo com componentes V2 para gerenciar regras oficiais do AutoMod do Discord.
- `/automod-config palavra`, `/automod-config dominio`, `/automod-config mencoes`,
  `/automod-config logs`, `/automod-config template`, `/automod-config sincronizar`
  e `/automod-config exportar` fazem configurações rápidas por comando.
- O painel cria, edita e remove regras usando a API oficial: palavras proibidas, spam, links maliciosos, convites e menções em massa.
- O bot precisa da permissão `Gerenciar Servidor`, e o administrador também precisa dessa permissão.
- Recursos próprios da Bella, como nicknames, biografias, anti-scam vision e relatórios, continuam separados do AutoMod nativo.

## Estatísticas Automáticas

- O envio semanal do painel de servidor usa `DEFAULT_SERVER_STATISTICS_CHANNEL_ID`.
- O envio semanal do painel do site usa `DEFAULT_SITE_STATISTICS_CHANNEL_ID`.
- Por padrão, ambos são publicados semanalmente às segundas-feiras, 09:00, com período de 7 dias.

## Monitoramento do Site e Repositório

- `SITE_REPOSITORY` define o repositório GitHub monitorado para alterações do site.
- A Bella monitora disponibilidade, páginas, feedbacks, alterações semânticas, auditoria funcional e commits do repositório.
- Alterações sensíveis, indícios de ataque ou mudanças em arquivos críticos são enviadas como alerta e mencionam Talisson.
