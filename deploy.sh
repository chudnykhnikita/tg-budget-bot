#!/bin/bash
set -e

SERVER="root@83.166.245.224"

ssh "$SERVER" "
  cd /opt/tg-budget-bot &&
  git pull &&
  docker build -t tg-budget-bot . &&
  docker stop budget-bot &&
  docker rm budget-bot &&
  docker run -d \
    --name budget-bot \
    --restart unless-stopped \
    -v /opt/budget-bot-data:/data \
    --env-file .env \
    tg-budget-bot
"

echo "Deployed successfully"
