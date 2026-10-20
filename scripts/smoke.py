# Smoke test before every run and before the demo (SPEC §19.1 level 4): everything is up and ready, < 60 s.
#   python -m scripts.smoke                    # the running demo stack (BANK_URL, SELLER_URL, keys from .env)
#   python -m scripts.smoke --target fork      # own bank + seller on an anvil fork of Base Sepolia
#   python -m scripts.smoke --target sepolia   # own bank + seller on Base Sepolia (keys from .env)
# Prints a PASS/FAIL table; exit code 0 only if every check passed. The 0.01 EURC payment is real on Base Sepolia.
import argparse
import json
import os
import sys
import time

import httpx
from eth_account import Account

from bank import chain, config, llm, money

ARES_URL = "https://ares.gov.cz/ekonomicke-subjekty-v-be/rest/ekonomicke-subjekty/45274649"  # ČEZ, a.s. (public)
MIN = {  # thresholds; override with SMOKE_MIN_ETH / SMOKE_MIN_EURC / SMOKE_MIN_TCZK (asset units)
    "ETH": float(os.getenv("SMOKE_MIN_ETH", "0.00002")),   # ≈ 20+ txs at 0.006 gwei (SPEC §19.7)
    "EURC": float(os.getenv("SMOKE_MIN_EURC", "1.00")),
    "tCZK": float(os.getenv("SMOKE_MIN_TCZK", "25.00")),
}

llm.register_mock("smoke", lambda messages, tools: {"text": "pong"})


class Smoke:
    def __init__(self, bank_url: str, seller_url: str, keys: dict, label: str):
        self.bank_url, self.seller_url, self.keys, self.label = bank_url, seller_url, keys, label
        self.rows: list[tuple[str, bool, str, float]] = []

    def run(self, name: str, fn):
        t0 = time.perf_counter()
        try:
            ok, detail = fn()
        except Exception as e:  # noqa: BLE001 — a crash is a FAIL with the reason
            ok, detail = False, f"{type(e).__name__}: {str(e)[:160]}"
        self.rows.append((name, bool(ok), str(detail), (time.perf_counter() - t0) * 1000))

    def addr(self, role: str) -> str | None:
        k = self.keys.get(role)
        return Account.from_key(k).address if k else None

    # ---- checks ----
    def health(self, url: str):
        r = httpx.get(f"{url}/health", timeout=5)
        body = r.json()
        return r.status_code == 200 and body.get("ok") is True, f"{body.get('service')} mock={body.get('mock')}"

    def supported(self):
        body = httpx.get(f"{self.bank_url}/x402/supported", timeout=10).json()
        have = {a["asset"] for a in body.get("assets", [])}
        kinds = {(k.get("scheme"), k.get("network")) for k in body.get("kinds", [])}
        ok = {"EURC", "tCZK"} <= have and ("exact", config.NETWORK) in kinds
        return ok, f"assets {sorted(have)}, kinds {sorted(kinds)}"

    def rpc(self):
        w3 = chain.w3()
        cid, block = w3.eth.chain_id, w3.eth.block_number
        return cid == config.CHAIN_ID, f"chain id {cid}, block {block}"

    def balances(self):
        w3, low, parts = chain.w3(), [], []
        for role in ("relayer", "operator", "admin", "alfa"):
            a = self.addr(role)
            if a is None:
                low.append(f"{role}: no key (TODO(credential))")
                continue
            eth = w3.eth.get_balance(a) / 1e18
            parts.append(f"{role} {eth:.6f} ETH")
            if eth < MIN["ETH"]:
                low.append(f"{role} ETH {eth:.6f} < {MIN['ETH']}")
        buyer = self.addr("alfa")
        if buyer:
            for asset in ("EURC", "tCZK"):
                if not config.ASSETS[asset]["address"]:
                    low.append(f"{asset}: address not set")
                    continue
                units = chain.balance(asset, buyer) / 10 ** money.decimals(asset)
                parts.append(f"buyer {units:.2f} {asset}")
                if units < MIN[asset]:
                    low.append(f"buyer {asset} {units:.2f} < {MIN[asset]}")
        return not low, "; ".join(low) if low else ", ".join(parts)

    def issuer(self):
        """Every ramp mint/burn and every payout to a seller's account is sent by the key holding ISSUER_ROLE on tCZK."""
        from bank import tczk
        a = self.addr("admin")
        if a is None:
            return False, "no issuer key (TODO(credential): BANK_ADMIN_PK)"
        if tczk.is_prep():
            return True, "prep token: the ramp is off, no issuer needed"
        role = tczk.token().functions.hasRole(tczk.ROLES["ISSUER_ROLE"], a).call()
        eth = chain.w3().eth.get_balance(a) / 1e18
        return role and eth >= MIN["ETH"], f"{a} ISSUER_ROLE={role}, {eth:.6f} ETH (min {MIN['ETH']})"

    def unpaid_402(self):
        offer = httpx.post(f"{self.seller_url}/negotiate", timeout=config.NEGOTIATE_TIMEOUT_S, json={
            "deal_id": f"smoke-{int(time.time())}", "seller_id": "s-beta", "task": {"criteria": {"count": 1}}}).json()["offer"]
        got = [httpx.get(f"{self.seller_url}/{p}/{offer['offer_id']}", timeout=10).status_code for p in ("order", "data")]
        return got == [402, 402], f"/order → {got[0]}, /data → {got[1]}"

    def llm_call(self):
        resp = llm.create("fast", "Answer with one word.", [{"role": "user", "content": "ping"}], stage="smoke",
                          max_tokens=16)
        text = llm.text_of(resp).strip()
        return bool(text), f"{'MOCK' if config.MOCK else config.MODEL_FAST}: {text[:40]!r}"

    def ares(self):
        r = httpx.get(ARES_URL, timeout=5, headers={"accept": "application/json"})
        return r.status_code == 200 and r.json().get("ico") == "45274649", f"HTTP {r.status_code}"

    def payment(self):
        """One 0.01 EURC x402 payment buyer → Beta through the bank facilitator; the Transfer is read from the chain."""
        from x402 import x402ClientSync
        from x402.http import FacilitatorConfig, HTTPFacilitatorClientSync
        from x402.mechanisms.evm import EthAccountSigner
        from x402.mechanisms.evm.exact import ExactEvmScheme
        from x402.schemas import PaymentRequired, PaymentRequirements

        from scripts.scenarios import transfers_in
        if not self.keys.get("alfa"):
            return False, "no buyer key (TODO(credential): ALFA_PK)"
        companies = json.loads((config.ROOT / "fixtures" / "companies.json").read_text(encoding="utf-8"))["companies"]
        beta = next(c["wallet_address"] for c in companies if c["id"] == "beta")
        amount, m = money.to_atomic("EURC", "0.01"), config.ASSETS["EURC"]
        req = PaymentRequirements(scheme="exact", network=config.NETWORK, asset=m["address"], amount=str(amount),
                                  pay_to=beta, max_timeout_seconds=60, extra={"name": m["name"], "version": m["version"]})
        client = x402ClientSync().set_spend_controls({"max_amount_per_payment": False, "allowed_assets": [
            {"network": config.NETWORK, "asset": m["address"], "max_amount_per_payment": str(amount)}]})
        client.register(config.NETWORK, ExactEvmScheme(EthAccountSigner(Account.from_key(self.keys["alfa"]))))
        payload = client.create_payment_payload(PaymentRequired(accepts=[req]))
        s = HTTPFacilitatorClientSync(FacilitatorConfig(url=f"{self.bank_url}/x402")).settle(payload, req)
        if not s.success:
            return False, f"settle refused: {s.error_reason}"
        moved = chain.read_retry(lambda: transfers_in(s.transaction, "EURC"), ok=bool)
        ok = any(t["to"].lower() == beta.lower() and t["value"] == amount for t in moved)
        where = f"{s.transaction} (anvil fork, no link)" if "127.0.0.1" in config.RPC_URL else chain.tx_link(s.transaction)
        return ok, where

    def all(self) -> bool:
        self.run("bank /health", lambda: self.health(self.bank_url))
        self.run("seller /health", lambda: self.health(self.seller_url))
        self.run("facilitator /x402/supported: EURC + tCZK", self.supported)
        self.run("RPC reachable, chain 84532", self.rpc)
        self.run("ETH / EURC / tCZK above thresholds", self.balances)
        self.run("tCZK issuer key: ISSUER_ROLE + ETH for mint/burn", self.issuer)
        self.run("unpaid request → 402", self.unpaid_402)
        self.run("one LLM call", self.llm_call)
        self.run("ARES reachable", self.ares)
        self.run("one 0.01 EURC payment lands", self.payment)
        return all(ok for _, ok, _, _ in self.rows)

    def table(self) -> str:
        w = max(len(n) for n, *_ in self.rows)
        lines = [f"smoke · {self.label}", ""]
        for name, ok, detail, ms in self.rows:
            lines.append(f"{'PASS' if ok else 'FAIL'}  {name:<{w}}  {ms:7.0f} ms  {detail}")
        lines += ["", f"{sum(ok for _, ok, _, _ in self.rows)}/{len(self.rows)} passed"]
        return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Smoke test (SPEC §19.1): everything up and ready for the demo")
    ap.add_argument("--target", default="running", choices=["running", "fork", "sepolia"])
    a = ap.parse_args(argv)
    t0 = time.time()
    if a.target == "running":
        keys = {"relayer": config.BANK_RELAYER_PK, "operator": config.BANK_OPERATOR_PK, "admin": config.BANK_ADMIN_PK,
                "alfa": config.ALFA_PK}
        smoke = Smoke(config.BANK_URL, config.SELLER_URL, keys, f"running stack, RPC {config.RPC_URL}")
        ok = smoke.all()
    else:
        from scripts.stack import Stack
        with Stack(a.target) as s:
            smoke = Smoke(s.bank_url, s.seller_url, s.keys, s.label)
            ok = smoke.all()
    print(smoke.table())
    print(f"total {time.time() - t0:.1f} s")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
