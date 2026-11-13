# Confiabilidade da IA pública

## Fluxo protegido

Toda mensagem passa pelo `on_message`, pelos módulos locais e pelo assistente público.
Uma menção direta válida agora termina obrigatoriamente em uma resposta normal ou em
um aviso curto de indisponibilidade. Exceções na preparação de contexto, anexos,
políticas, geração ou envio são registradas e não encerram silenciosamente o fluxo.

Respostas pendentes mantêm o contexto de `force_reply`, têm backoff limitado e no
máximo seis tentativas. O worker não depende exclusivamente da disponibilidade da
Groq, pois o provedor primário pode ser Gemini e os fallbacks locais continuam úteis.

## Provedores

- O modelo configurado na Groq é preservado quando suportado.
- Um erro 404 troca para o próximo modelo de texto disponível.
- Resposta vazia também troca de modelo dentro do fluxo normal de retry.
- Erros 429, timeouts e falhas transitórias aplicam retry/cooldown sem segurar o
  semáforo durante a espera.
- Gemini e Groq possuem limites independentes de concorrência.
- Uma thread Gemini que excedeu o timeout continua ocupando sua vaga até terminar,
  impedindo crescimento descontrolado de chamadas bloqueadas.

## Estado e rollback

`StateStore` mantém compatibilidade de leitura com JSON e SQLite, mas grava as seções
mais pesadas em `data/state_sections/` por padrão:

- `operational_events`;
- `operational_analysis_cache`;
- `server_backup_resources`.

Antes da primeira externalização, o JSON original é copiado para
`data/state.pre-externalization.backup.json`. Para rollback, pare a Bella, preserve os
arquivos atuais e restaure esse backup como `data/state.json` usando uma versão antiga
do código. Nenhum dado é removido durante a migração.

## Validação segura

Os testes de regressão não acessam Discord nem fazem chamadas reais às APIs. Eles
cobrem preservação do modelo configurado, retry após resposta vazia, retorno explícito
na deescalação e persistência/reinicialização das seções externalizadas.
