#!/usr/bin/env bash
# Instala a Bella + FreeLLMAPI num servidor Ubuntu 22.04/24.04 (Contabo, Google
# Cloud, qualquer VPS). Seguro para servidor compartilhado:
#   - não muda fuso horário nem configurações globais;
#   - só cria swap se o servidor tiver menos de 2 GB de RAM e nenhum swap;
#   - não reinstala o Docker se ele já existir;
#   - escolhe outra porta para a FreeLLMAPI se a 3001 estiver ocupada.
#
# Uso, dentro da pasta do repositório (com o .env e a pasta data já no lugar):
#   bash deploy/instalar_servidor.sh              # sem Chromium (menos RAM)
#   bash deploy/instalar_servidor.sh --chromium   # lê o site completo (+~400 MB)
# Pode rodar de novo quando quiser: cada passo confere se já foi feito.
set -euo pipefail

WITH_CHROMIUM=false
for arg in "$@"; do
  case "$arg" in
    --chromium) WITH_CHROMIUM=true ;;
    *) echo "Opção desconhecida: $arg" >&2; exit 2 ;;
  esac
done

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEPLOY_DIR="$APP_DIR/deploy"
USER_NAME="$(id -un)"
SERVICE_NAME="bella-bot"
cd "$APP_DIR"

set_env() { # set_env ARQUIVO CHAVE VALOR  (troca ou acrescenta)
  local file="$1" key="$2" value="$3"
  if grep -q "^${key}=" "$file"; then
    sed -i "s#^${key}=.*#${key}=${value}#" "$file"
  else
    printf '%s=%s\n' "$key" "$value" >> "$file"
  fi
}

echo "==> Bella em $APP_DIR (usuário $USER_NAME)"
if [ "$USER_NAME" = "root" ]; then
  echo "AVISO: rodando como root. O ideal é um usuário próprio (veja o README)." >&2
fi
if [ ! -f .env ]; then
  echo "ERRO: falta o .env em $APP_DIR." >&2
  echo "Ele não vai no repositório: o Talisson manda no pacote bella-dados.tar.gz." >&2
  exit 1
fi
if [ ! -d data ]; then
  echo "AVISO: pasta data não encontrada. A Bella vai começar sem histórico." >&2
  mkdir -p data
fi

echo "==> 1/7 Memória"
ram_mb=$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)
swap_mb=$(awk '/SwapTotal/ {print int($2/1024)}' /proc/meminfo)
echo "   RAM: ${ram_mb} MB | swap: ${swap_mb} MB"
if [ "$ram_mb" -lt 2000 ] && [ "$swap_mb" -lt 512 ] && [ ! -f /swapfile ]; then
  echo "   Pouca RAM e sem swap: criando /swapfile de 2 GB."
  sudo fallocate -l 2G /swapfile
  sudo chmod 600 /swapfile
  sudo mkswap /swapfile >/dev/null
  sudo swapon /swapfile
  grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
else
  echo "   Memória suficiente; nada alterado."
fi

echo "==> 2/7 Pacotes"
sudo apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
  python3-venv python3-pip libopus0 openssl sqlite3 git ffmpeg
if command -v docker >/dev/null 2>&1; then
  echo "   Docker já instalado; mantendo o que existe."
else
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq docker.io
fi
if ! sudo docker compose version >/dev/null 2>&1; then
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq docker-compose-v2 \
    || sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq docker-compose-plugin
fi
sudo systemctl enable --now docker >/dev/null 2>&1 || true

echo "==> 3/7 Ambiente Python da Bella (demora alguns minutos)"
if [ ! -x .venv/bin/python ]; then
  rm -rf .venv   # .venv vinda do Windows não serve no Linux
  python3 -m venv .venv
fi
.venv/bin/python -m pip install -q --upgrade pip
.venv/bin/python -m pip install -q -r requirements.txt

echo "==> 4/7 Ajustes do .env"
sed -i 's/\r$//' .env
chmod 600 .env
mkdir -p logs
if [ "$WITH_CHROMIUM" = true ]; then
  set_env .env RENDER_DYNAMIC_PAGES true
  sudo .venv/bin/python -m playwright install-deps chromium >/dev/null
  .venv/bin/python -m playwright install chromium >/dev/null
  echo "   Chromium instalado (leitura completa do site)."
else
  set_env .env RENDER_DYNAMIC_PAGES false
  echo "   Chromium desligado (economiza RAM)."
fi

echo "==> 5/7 FreeLLMAPI (Docker, aberta só para o próprio servidor)"
FLL_ENV="$DEPLOY_DIR/freellmapi/.env"
if [ ! -f "$FLL_ENV" ]; then
  printf 'ENCRYPTION_KEY=%s\n' "$(openssl rand -hex 32)" > "$FLL_ENV"
  chmod 600 "$FLL_ENV"
  echo "   Chave de criptografia criada em deploy/freellmapi/.env."
fi
port=$(grep -oP '^FREELLMAPI_PORT=\K[0-9]+' "$FLL_ENV" || true)
if [ -z "$port" ]; then
  port=3001
  while sudo ss -ltnH "sport = :$port" | grep -q .; do
    port=$((port + 1))
  done
  set_env "$FLL_ENV" FREELLMAPI_PORT "$port"
fi
echo "   Porta: 127.0.0.1:$port"
(cd "$DEPLOY_DIR/freellmapi" && sudo docker compose up -d)
set_env .env FREELLMAPI_BASE_URL "http://127.0.0.1:${port}/v1"
grep -q '^FREELLMAPI_ENABLED=' .env || set_env .env FREELLMAPI_ENABLED false
sudo usermod -aG docker "$USER_NAME"

echo "==> 6/7 Serviço $SERVICE_NAME (liga no boot e reinicia se cair)"
sed -e 's/\r$//' -e "s#__USER__#$USER_NAME#g" -e "s#__APP_DIR__#$APP_DIR#g" \
  "$DEPLOY_DIR/bella.service" | sudo tee "/etc/systemd/system/$SERVICE_NAME.service" >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME" >/dev/null
sudo systemctl restart "$SERVICE_NAME"

echo "==> 7/7 Backup diário às 04:30 (guarda os 7 últimos em ~/backups-bella)"
mkdir -p "$HOME/backups-bella"
CRON_LINE="30 4 * * * bash $DEPLOY_DIR/backup.sh >> $HOME/backups-bella/backup.log 2>&1"
# Sem crontab ainda, "crontab -l" falha; com pipefail isso pararia o script.
{ crontab -l 2>/dev/null | grep -v 'deploy/backup.sh' || true; echo "$CRON_LINE"; } | crontab -

ip=$(hostname -I 2>/dev/null | awk '{print $1}')
echo
echo "Tudo pronto. A Bella já está online (ainda usando a Groq direta)."
echo
echo "Para ligar a FreeLLMAPI, no seu PC abra o túnel:"
echo "   ssh -L ${port}:127.0.0.1:${port} ${USER_NAME}@${ip:-IP_DO_SERVIDOR}"
echo "e acesse http://localhost:${port} (passo a passo no README)."
echo
echo "Comandos úteis:"
echo "   sudo systemctl status $SERVICE_NAME   |  journalctl -u $SERVICE_NAME -f"
echo "   sudo docker logs -f bella-freellmapi   |  free -h"
