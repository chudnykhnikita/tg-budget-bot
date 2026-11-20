#!/bin/bash
set -e

SERVER="root@83.166.245.224"
SSH=(ssh -o ConnectTimeout=15 -o "ProxyCommand=nc -b en0 %h %p" "$SERVER")

"${SSH[@]}" "
  set -e
  cd /opt/tg-budget-bot &&
  git pull &&
  docker tag tg-budget-bot tg-budget-bot:before-\$(date +%Y%m%d%H%M%S) || true &&
  docker build -t tg-budget-bot . &&
  docker stop budget-bot &&
  docker rm budget-bot &&
  docker run -d \
    --name budget-bot \
    --restart unless-stopped \
    -v /opt/budget-bot-data:/data \
    -v /opt/tg-budget-bot/credentials.json:/app/credentials.json:ro \
    --env-file .env \
    tg-budget-bot
"

echo "Deployed successfully"
