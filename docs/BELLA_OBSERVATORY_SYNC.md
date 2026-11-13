# Sincronização com o Observatório

A Bella envia um heartbeat assinado ao site a cada 60 segundos e sincroniza os dados públicos agregados a cada cinco minutos. São publicados estado operacional, métricas consolidadas, comunidade, comandos públicos, atividade editorial e incidentes operacionais. Mensagens, IDs de membros, conteúdo de canais, contatos, tokens e dados privados não são enviados.

## Configuração

Adicione ao `.env` do bot:

```dotenv
BELLA_OBSERVATORY_ENABLED=true
BELLA_OBSERVATORY_URL=https://bellanyellebot.vercel.app
BELLA_OBSERVATORY_KEY_ID=bella-bot-prod
BELLA_OBSERVATORY_SECRET=use-o-mesmo-segredo-da-vercel
BELLA_OBSERVATORY_INTERVAL_SECONDS=60
BELLA_OBSERVATORY_SYNC_INTERVAL_SECONDS=300
BELLA_OBSERVATORY_TIMEOUT_SECONDS=15
BELLA_OBSERVATORY_MAX_ATTEMPTS=3
BELLA_VERSION=2026.10
```

Na Vercel, `BELLA_INGESTION_KEYS` deve conter a mesma chave e o mesmo segredo:

```json
{
  "bella-bot-prod": {
    "secret": "use-o-mesmo-segredo-do-bot-com-ao-menos-32-caracteres",
    "scopes": [
      "status:write",
      "metrics:write",
      "activity:write",
      "infrastructure:write",
      "editorial:write",
      "commands:write",
      "community:write"
    ]
  }
}
```

Nunca coloque o token do Discord no site. Depois de alterar variáveis na Vercel, faça um novo deploy. Depois de alterar o `.env` do bot, reinicie a Bella.

## Operação

- A sincronização automática começa quando o bot fica pronto.
- Falhas transitórias recebem até três tentativas com backoff.
- O mesmo corpo e `eventId` são mantidos durante retries, mas cada tentativa usa nonce novo.
- Métricas do dia usam cursor persistente e outbox para evitar contagem duplicada após reinicializações.
- Métricas históricas usam eventos diários idempotentes e somente dados realmente registrados.
- Use `/observatorio painel` uma vez para publicar a Central fixa em um canal administrativo.
- Depois disso, use **Sincronizar agora**, **Diagnóstico** e **Atualizar painel** sem comandos.
- Use `/observatorio sincronizar` como alternativa administrativa para uma sincronização completa.
- Use `/observatorio status` para conferir a configuração sem revelar o segredo.

## Fontes e limitações

- Interações e comunidade: estatísticas agregadas de mensagens e entradas registradas pela Bella.
- Comandos: execuções concluídas depois da ativação deste coletor.
- Tickets: registros persistentes de abertura e fechamento.
- Segurança: incidentes da moderação autônoma e scans concluídos ou parciais.
- AutoMod: ações recebidas pelo evento oficial do Discord depois da ativação deste coletor.
- Tempo operacional: segundos observados pelo worker enquanto o processo da Bella está ativo.
- Indicadores sem evidência não são publicados como zero fictício; permanecem indisponíveis até existir dado real.
