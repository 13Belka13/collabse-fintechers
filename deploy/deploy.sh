#!/bin/bash
# Runs on the AWS host (first boot, every CD run via SSM, or the pull timer): take main from GitHub, fetch the
# config from SSM Parameter Store, rebuild and restart, wait for /health. Idempotent; prints what it deployed.
set -euo pipefail
ROOT=/opt/collabse
REPO=$ROOT/repo
BRANCH=${BRANCH:-main}
REGION=${AWS_REGION:-eu-central-1}
ENV_PARAM=${ENV_PARAM:-/collabse/env}
UI_PARAM=${UI_PARAM:-/collabse/ui}
exec 9>$ROOT/deploy.lock
flock -n 9 || { echo "another deploy is running"; exit 0; }

cd "$REPO"
git fetch --quiet origin "$BRANCH"
NEW=$(git rev-parse "origin/$BRANCH")
OLD=$(cat $ROOT/deployed 2>/dev/null || echo none)
# the public IP names the site (<ip>.sslip.io); it changes once when the Elastic IP attaches after first boot
IP=$(curl -fsS -H "X-aws-ec2-metadata-token: $(curl -fsS -X PUT http://169.254.169.254/latest/api/token \
  -H 'X-aws-ec2-metadata-token-ttl-seconds: 60')" http://169.254.169.254/latest/meta-data/public-ipv4)
SITE="${IP//./-}.sslip.io"
if [ "${1:-}" = "--if-changed" ] && [ "$NEW" = "$OLD" ] && grep -qx "SITE_HOST=$SITE" $ROOT/caddy.env 2>/dev/null; then
  exit 0
fi
git reset --quiet --hard "$NEW"

umask 077
aws ssm get-parameter --region "$REGION" --name "$ENV_PARAM" --with-decryption --query Parameter.Value \
  --output text > $ROOT/.env.new
mv $ROOT/.env.new $ROOT/.env
UI=$(aws ssm get-parameter --region "$REGION" --name "$UI_PARAM" --with-decryption --query Parameter.Value --output text)
# /collabse/ui = "user:<bcrypt hash>"; base64 keeps the "$" of bcrypt away from compose interpolation
{ echo "SITE_HOST=$SITE"; echo "UI_USER=${UI%%:*}"; echo "UI_HASH=$(printf %s "${UI#*:}" | base64 -w0)"; } \
  > $ROOT/caddy.env

cd "$REPO/deploy"
docker compose up -d --build --remove-orphans
for i in $(seq 1 90); do
  if docker compose exec -T app curl -fsS http://localhost:8000/health >/dev/null 2>&1; then
    echo "$NEW" > $ROOT/deployed
    docker image prune -f >/dev/null
    echo "deployed $NEW ($(git log -1 --format=%s "$NEW")) at https://$SITE"
    exit 0
  fi
  sleep 2
done
echo "health check failed for $NEW; last app logs:" >&2
docker compose logs --tail 60 app >&2
exit 1
