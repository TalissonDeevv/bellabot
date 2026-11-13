# Hospedagem na Square Cloud

Este projeto já está preparado para upload na Square Cloud.

## Arquivos principais

- `squarecloud.app`: configuração usada pela Square Cloud.
- `bot.py`: arquivo principal iniciado pela hospedagem.
- `requirements.txt`: dependências instaladas automaticamente.
- `.env`: variáveis do bot. Não compartilhe este arquivo publicamente.

## Como hospedar

1. Entre no painel da Square Cloud.
2. Crie uma nova aplicação.
3. Envie o ZIP gerado do projeto.
4. Confirme que o arquivo `squarecloud.app` está na raiz do ZIP.
5. Inicie ou reinicie a aplicação.

## Variáveis importantes

O bot precisa, no mínimo, destas variáveis:

```env
DISCORD_TOKEN=
GROQ_API_KEY=
GROQ_MODEL=openai/gpt-oss-20b
GROQ_VISION_MODEL=qwen/qwen3.6-27b
GROQ_MAX_OUTPUT_TOKENS=650
GROQ_MAX_JSON_OUTPUT_TOKENS=1200
GROQ_MAX_PROMPT_CHARS=2600
GROQ_CACHE_TTL_SECONDS=7200
GROQ_DAILY_REQUEST_LIMIT=150
GROQ_CONCURRENCY=1
AI_PUBLIC_REQUIRE_MENTION=true
AI_ALLOWED_GUILD_IDS=1467976297689911377
AI_CONTEXT_MESSAGES=5
```

O `.env` atual já foi incluído no pacote porque este bot depende de muitas
configurações de canais, monitoramento e IA. Caso prefira mais segurança, remova
o `.env` do ZIP e cadastre as variáveis diretamente no painel da Square Cloud.

## Observações

- `run_bot.ps1`, `setup.ps1` e `INSTALAR_E_INICIAR.bat` são apenas para uso local.
- Na Square Cloud o início é controlado por `squarecloud.app`.
- A memória está configurada como `1024 MB` porque o projeto usa monitoramento,
  voz, imagens e componentes que podem consumir mais RAM.
- Se quiser economizar RAM, desative recursos pesados no `.env`, como páginas
  dinâmicas com navegador headless.

## Checklist antes de subir

- `DISCORD_TOKEN` válido.
- `GROQ_API_KEY` válida.
- Intents habilitadas no Discord Developer Portal.
- Bot convidado no servidor com permissões necessárias.
- Canais configurados no `.env` existem no servidor.
