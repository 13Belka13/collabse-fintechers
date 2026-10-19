# Deploy to AWS (the hackathon team account)

One small EC2 host in **eu-central-1** runs the whole demo in Docker: the bank (API, x402 facilitator, UI on
`/story`, `/bank`, …), the demo seller, and Caddy in front with HTTPS on `https://<ip-with-dashes>.sslip.io`
and a password. Testnet only (Base Sepolia).

```
GitHub main ──CI green──> deploy.yml ──OIDC──> SSM Run Command ──> host: deploy/deploy.sh
                                                      (fallback: the host checks main every 2 min)
host: git reset to main → .env from SSM Parameter Store → docker compose up --build → /health
```

- **No access keys anywhere.** The host's instance role calls Bedrock (Claude 4.5 EU profiles) and reads its
  config; GitHub Actions assumes `collabse-github-deploy` through OIDC. So the 12-hour portal keys do not
  matter for the deployed demo.
- **Config:** SSM SecureString `/collabse/env` = the app's `.env` without AWS keys (`MOCK=0`, testnet keys,
  Tatum, Apify); `/collabse/ui` = `demo:<bcrypt hash>` for the password. The plain password is `UI_PASSWORD` in
  the local `.env`.
- **Versions:** the image installs `requirements.txt` with `deploy/constraints.txt` (the `pip freeze` the
  test suite passed on).
- **Cost:** t4g.small + 30 GB gp3 + Elastic IP, about $0.6 a day, plus Bedrock per deal (~$0.06 on Opus 4.5).

## First setup (once)

```bash
aws ssm put-parameter --name /collabse/env --type SecureString --value file://server.env --overwrite
aws ssm put-parameter --name /collabse/ui  --type SecureString --value "demo:$(caddy hash-password --plaintext '…')" --overwrite
aws cloudformation deploy --stack-name collabse-github-oidc --template-file infra/aws/github-oidc.yaml --capabilities CAPABILITY_NAMED_IAM
aws cloudformation deploy --stack-name collabse-demo --template-file infra/aws/stack.yaml --capabilities CAPABILITY_IAM
aws cloudformation describe-stacks --stack-name collabse-demo --query "Stacks[0].Outputs"
```

After that every green CI run on `main` deploys. Run a deploy by hand: GitHub → Actions → deploy → Run
workflow, or `aws ssm send-command --document-name AWS-RunShellScript --targets Key=tag:app,Values=collabse
--parameters 'commands=["/usr/local/bin/collabse-deploy"]'`. Shell on the host: `aws ssm start-session
--target <instance-id>`; logs: `cd /opt/collabse/repo/deploy && sudo docker compose logs -f app`.

## Notes

- GitHub's OIDC `sub` claim now carries ids: `repo:13Belka13@62833220/collabse-fintechers@1425394405:ref:refs/heads/main`
  (seen in CloudTrail when the first deploy was refused). The deploy role accepts both the new and the old form.
- Only one bank may use the testnet keys at a time: stop the local live stack while the AWS one runs
  (both would send from the same operator wallet).
- Change the config: update `/collabse/env`, then deploy again (the host rereads it every deploy).
- Before the accounts are frozen: `aws cloudformation delete-stack --stack-name collabse-demo` (the SQLite DB
  lives on the instance volume and goes with it).
