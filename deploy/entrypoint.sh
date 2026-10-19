#!/bin/sh
# Container start: seed the demo companies (idempotent upsert), start the bank, wait for it, then the seller
# (its x402 middleware asks the bank's facilitator on start). If either process dies, the container exits and
# Docker restarts it.
set -e
python -m scripts.seed
python -m uvicorn bank.app:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips='*' &
BANK=$!
for i in $(seq 1 60); do curl -fsS http://localhost:8000/health >/dev/null 2>&1 && break; sleep 1; done
python -m uvicorn seller.app:app --host 127.0.0.1 --port 8001 &
SELLER=$!
trap 'kill $BANK $SELLER 2>/dev/null' TERM INT
while kill -0 $BANK 2>/dev/null && kill -0 $SELLER 2>/dev/null; do sleep 2; done
echo "a service stopped; exiting so Docker restarts the container" >&2
kill $BANK $SELLER 2>/dev/null || true
exit 1
