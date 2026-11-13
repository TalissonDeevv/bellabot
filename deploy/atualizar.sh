#!/usr/bin/env bash
# Atualiza a Bella com a versão mais nova do repositório e reinicia.
# Uso: bash deploy/atualizar.sh            (só a Bella)
#      bash deploy/atualizar.sh --tudo     (Bella + imagem nova da FreeLLMAPI)
# O .env, a pasta data e o deploy/freellmapi/.env não são tocados.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$APP_DIR"

echo "==> Baixando atualizações"
git pull --ff-only
sed -i 's/\r$//' deploy/*.sh deploy/bella.service

echo "==> Dependências"
.venv/bin/python -m pip install -q -r requirements.txt

if [ "${1:-}" = "--tudo" ]; then
  echo "==> FreeLLMAPI"
  (cd deploy/freellmapi && sudo docker compose pull && sudo docker compose up -d)
fi

echo "==> Reiniciando a Bella"
sudo systemctl restart bella-bot
sleep 5
sudo systemctl --no-pager --lines=15 status bella-bot
