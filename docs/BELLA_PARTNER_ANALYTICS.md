# Bella Partner Analytics

## Visão geral

O Bella Partner Analytics é uma extensão nativa do Programa de Parceiros. Ele reutiliza:

- `PartnerProgramService` e o painel administrativo existente;
- `PartnerProgramManager` e o banco `data/partners.db`;
- as inscrições do banco `data/partner_program.db`;
- o RBAC do Programa de Parceiros;
- a integração `GroqService` já inicializada pela Bella;
- o canal `partners` da Central de Logs;
- a biblioteca Pillow já presente no projeto;
- o sistema de workers assíncronos iniciado em `on_ready`.

Nenhum segundo Programa de Parceiros, sistema de logs ou cliente de IA foi criado.

## Arquitetura

O módulo `website_guard/partner_analytics.py` contém:

1. **Period Engine**: calcula períodos semanais, mensais, anuais, parciais e personalizados com fuso horário.
2. **Analytics Repository**: consulta os dados reais dos dois bancos do programa e persiste configuração/histórico.
3. **Metric Engine**: consolida parceiros, inscrições, indicações, projetos, receita e comissões.
4. **Comparison Engine**: compara períodos equivalentes e trata corretamente bases anteriores iguais a zero.
5. **Chart Renderer**: produz PNG 1600×900 com Pillow, sem IA generativa.
6. **Summary Engine**: solicita resumo ao `GroqService` com JSON validado e usa fallback determinístico em falhas.
7. **Publisher**: envia imagem e relatório em Components V2 na mesma mensagem do Discord e só confirma após o envio.
8. **Scheduler**: verifica agendamentos persistentes e usa chave única por guild, tipo, período e destino.
9. **Admin UX**: abre uma central secundária a partir do botão **Relatórios** do painel existente.

## Arquivos

### Adicionados

- `website_guard/partner_analytics.py`
- `tests/test_partner_analytics.py`
- `docs/BELLA_PARTNER_ANALYTICS.md`

### Modificados

- `bot.py`: inicialização do serviço e worker.
- `website_guard/partner_program.py`: botão **Relatórios** e vínculo do serviço.
- `website_guard/central_logs.py`: evento `relatório` e provisionamento idempotente do canal administrativo de Analytics na categoria Bella Logs.
- `tests/test_partners.py`: validação do botão integrado.
- `README.md`: referência ao módulo.

### Dependências

Nenhuma dependência foi adicionada. A geração gráfica reutiliza Pillow e o restante usa `discord.py`, SQLite e a infraestrutura assíncrona já instalados no projeto.

## Banco de dados

As tabelas são criadas no banco já utilizado pelos parceiros (`data/partners.db`).

### `partner_analytics_config`

Armazena ativação, data, horário, fuso e canal dos relatórios semanais, mensais e anuais, além da política `last_day` ou `skip` para datas inexistentes. Também persiste `analytics_channel_id` e `panel_message_id` para reutilizar o canal e o painel fixo.

### `partner_analytics_reports`

Armazena ID, chave de deduplicação, guild, tipo, período, indicação de parcial, executor manual, snapshots de indicadores e comparação, resumo, destino, mensagem, status, erro e versões.

O histórico é um **snapshot imutável dos indicadores calculados na geração**. A imagem não é armazenada como blob; ela é reconstruída com o snapshot salvo, sem consultar contatos novamente.

## Indicadores e fórmulas

### Parceiros

- **Inscrições cadastradas**: total em `partner_applications` existente até o fim do período.
- **Parceiros aprovados**: total em `partners` existente até o fim do período.
- **Parceiros ativos/inativos**: estado atual do cadastro aprovado.
- **Novos parceiros**: `joined_at` dentro do período.
- **Comercialmente ativos**: parceiros com ao menos uma indicação ou projeto registrado no período.
- **Taxa de aprovação**: aprovações ÷ (aprovações + recusas) decididas no período × 100.

Sem decisões, a taxa aparece como **indisponível**, não `0%`.

### Indicações

- **Novas**: `partner_referrals.created_at` dentro do período.
- **Qualificadas**: novas indicações em `accepted`, `negotiation`, `closed` ou `completed`.
- **Em negociação**: novas indicações em `negotiation`.
- **Convertidas**: novas indicações em `closed` ou `completed`.
- **Perdidas**: novas indicações em `rejected`.
- **Taxa de conversão**: convertidas ÷ novas indicações × 100.

Sem novas indicações, a taxa aparece como **indisponível**.

### Resultados comerciais

- **Contratos fechados**: projetos `closed`/`fechado` criados no período.
- **Receita atribuída**: soma da receita desses projetos.
- **Comissões geradas**: comissões criadas no período.
- **Comissões pendentes**: saldo atualmente pendente criado até o fim do período.
- **Comissões pagas**: pagamentos com `paid_at` dentro do período.

### Comparativos

- Diferença absoluta: atual − anterior.
- Variação percentual: `(atual − anterior) ÷ |anterior| × 100`.
- Se o anterior for zero e o atual positivo, o percentual é **indisponível**, evitando infinito.
- Semanas comparam a semana calendário anterior.
- Meses comparam o mês calendário anterior.
- Anos comparam o ano calendário anterior.
- Períodos personalizados usam o intervalo imediatamente anterior de mesma duração.

## Períodos

- Semana completa: segunda-feira 00:00 até a segunda seguinte, no fuso configurado.
- Mês completo: primeiro dia 00:00 até o primeiro dia do mês seguinte.
- Ano completo: 1º de janeiro 00:00 até 1º de janeiro do ano seguinte.
- Automáticos: último período completo.
- Manuais parciais: identificados explicitamente como `PARCIAL`.
- Fuso padrão: `America/Recife`.

O dia de publicação não muda o período analisado.

## Agendamento e deduplicação

O worker `partner_analytics_worker` executa a cada 60 segundos.

- Semanal: ativação, dia da semana, horário, fuso e canal próprios.
- Mensal: ativação, dia, horário, fuso, canal e política de mês curto.
- Anual: ativação, mês, dia, horário, fuso, canal e política para data inexistente.
- `last_day`: publica no último dia quando 29/30/31 não existir.
- `skip`: não publica naquele mês.

A chave de deduplicação combina guild, tipo, início, fim e destino. Reservas interrompidas há mais de 20 minutos podem ser retomadas. Antes do envio, o sistema também procura o ID do relatório nas mensagens recentes do canal para reduzir duplicação após falha entre envio e persistência.

## Canal de logs

Ao clicar em **Relatórios**, a Bella cria ou reutiliza `📈╺╸parceiros-analytics` dentro da categoria configurada da **Bella Logs**. O canal herda as permissões privadas da categoria, é registrado no registry de canais administrativos e recebe um painel fixo em Components V2. Cliques posteriores apenas sincronizam o canal e atualizam a mesma mensagem.

A resolução segue:

1. canal específico configurado para o tipo;
2. canal `📈╺╸parceiros-analytics`;
3. canal `partners` da Central de Logs como fallback legado.

Sem canal válido, a central informa que o destino não está configurado e o relatório não é marcado como publicado.

## Imagem

- PNG 1600×900;
- fundo escuro e acentos laranja, verde e azul;
- KPIs de indicações, conversões, receita e comissões;
- evolução diária de indicações e contratos nos períodos curtos;
- evolução mensal para relatórios anuais, evitando truncar o ano;
- distribuição de leads quando houver dados;
- resumo comercial quando não houver distribuição, sem gráfico vazio decorativo;
- BRL formatado;
- nenhum contato, telefone ou e-mail.

O dashboard fica no topo de um `MediaGallery`. Abaixo dele, um único `TextDisplay` apresenta título, período e quatro parágrafos editoriais curtos, sem repetir os números em blocos separados. A prévia e a publicação final não utilizam embeds V1.

## IA

A IA recebe somente período, indicadores consolidados e comparação matemática. Ela não recebe nomes ou contatos de leads. O prompt proíbe inferir causas. Falha, timeout ou ausência da API acionam resumo determinístico e não impedem a publicação.

O resumo usa quatro parágrafos curtos: desempenho comercial, parceiros e comissões, comparação anterior e destaque do período. A saída é normalizada antes da publicação para remover títulos redundantes, limitar o tamanho e preservar somente as divisões editoriais úteis. Se a IA não respeitar esse formato, a Bella usa o resumo determinístico validado.

## Painel administrativo

O painel existente ganhou somente o botão **Relatórios**. Ao clicar, a Bella cria/sincroniza o canal privado na categoria Bella Logs, publica ou atualiza o painel fixo V2 e retorna um link direto. A Central de Estatísticas possui:

- **Gerar agora**;
- **Agendamento**;
- **Histórico**.

A geração manual oferece última semana, último mês, último ano, períodos parciais equivalentes e período personalizado. Toda geração abre prévia privada; publicar exige outro clique e não altera o agendamento.

## Histórico

O histórico permite abrir novamente a imagem e o resumo a partir do snapshot. Reabrir a apresentação não recalcula os números.

## Permissões

Views, selects e modais reutilizam `PartnerProgramService.can_manage_application`. São aceitos proprietário, administrador configurado, cargo gestor e permissões administrativas já reconhecidas pelo Programa de Parceiros. Visualizar o painel não concede autorização no backend.

Como os relatórios incluem valores financeiros, a configuração e a publicação recusam canais visíveis ao `@everyone`. O destino precisa ser privado para a equipe autorizada.

Permissões Discord necessárias para publicação: `View Channel`, `Send Messages`, `Embed Links`, `Attach Files` e, para a verificação adicional de duplicidade, `Read Message History`.

## Resiliência e performance

- consultas agregadas ao SQLite;
- SQLite e Pillow executados com `asyncio.to_thread`;
- lock por guild;
- deduplicação persistente;
- retomada de reservas antigas;
- fallback sem IA;
- sucesso confirmado somente após `channel.send`;
- imagens em `BytesIO`, sem arquivos temporários;
- falhas registradas no histórico.

## Testes

`tests/test_partner_analytics.py` cobre indicadores, taxas, ausência de denominador, variação percentual, períodos semanais/mensais/anuais, fusos, mês curto, persistência, comparação, fallback, agregação mensal do gráfico anual, PNG, histórico, deduplicação e RBAC. Os testes usam apenas bancos temporários e dados simulados identificados.

Validação executada em 12/10/2026:

- compilação de `bot.py`, `partner_program.py` e `partner_analytics.py`: aprovada;
- importação completa de `bot.py`: aprovada;
- 60 testes focados em Partner Analytics, Central de Logs, parceiros, cards e personalização: aprovados;
- suíte global: 341 testes executados; 311 aprovados e 30 falhas preexistentes concentradas nos módulos de conversa, Groq e memória permanente. Nenhuma falha global pertence ao Partner Analytics ou à Central de Logs.

## Limitações reais

1. `partner_referrals` mantém o estado atual, não todo o histórico de transições. O snapshot preserva o valor observado, mas um recálculo futuro pode mudar se o lead avançar depois.
2. Parceiros ativos/inativos refletem o estado conhecido na geração; o snapshot evita alteração retroativa do relatório salvo.
3. Projetos são contabilizados pela data de cadastro. O schema atual não tem uma data separada de fechamento efetivo.
4. Rankings exibem parceiros, nunca dados pessoais dos leads.
5. O Discord limita embeds, componentes e anexos; por isso o painel usa uma central secundária.

## Evoluções futuras

- eventos imutáveis para cada mudança de status;
- data de fechamento comercial separada;
- metas configuráveis;
- exportação CSV/PDF;
- filtros por parceiro e origem;
- tendências determinísticas em janelas maiores.
