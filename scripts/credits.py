# Read-only credit check before the jury: will any balance, quota or budget run out during the demo?
# For every address the bank and the agents use: ETH on Base Sepolia and how many transactions of each kind it
# still pays for at the current gas price; EURC / tCZK and how many demo runs they cover; Apify and Tatum account
# usage against their limits; model spend per deal and per eval run against the hackathon AWS budget.
# Gas per transaction kind is measured from the receipts of the Base Sepolia scenario runs (reports/scenarios-
# sepolia*.json) — never assumed. Nothing is signed or sent: eth_call / eth_getBalance / eth_getTransactionReceipt
# and HTTP GETs only. Private keys from .env are used only to derive addresses and are never printed.
# Usage: python -m scripts.credits   -> table on stdout, reports/credits.json
import json
import os
import re
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
from dotenv import dotenv_values
from eth_account import Account

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "reports" / "credits.json"
ENV = {k: (v or "").split(" #")[0].strip().strip('"') for k, v in dotenv_values(ROOT / ".env").items()}
PUBLIC_RPC = "https://sepolia.base.org"
EURC_DEFAULT = "0x808456652fdb597867f38412077A9182bf77359F"

# One demo run = the five buttons on /bank (scripts/demo_scenarios.py) at the seller's agreed minimum price:
# 20 items x 0,25 € = 5,00 € (seller/app.py), tCZK 125,00 Kč at CZK_PER_EUR_DEMO 25.
#   honest     EURC lock 5.00, all captured (rule B: 90 % now, 10 % at window end) -> authorize + 2 captures
#   fake_data  EURC lock 5.00, 17/20 captured = 4.25, 0.75 voided back            -> authorize + 2 captures + void
#   attack     refused by code, nothing moves                                      -> no tx
#   suspicious refused before payment                                              -> no tx
#   tczk       tCZK lock 125.00, all captured (rule B)                             -> authorize + 2 captures
# EURC peak 10.00: honest's last 10 % may still be locked (window not over) when fake_data locks its 5.00.
DEMO_RUN = {"EURC_spent": 5.00 + 4.25, "EURC_peak_lock": 10.00, "tCZK_spent": 125.00, "tCZK_peak_lock": 125.00,
            "operator_txs": {"authorize": 3, "capture": 6, "void": 1}}
# One Sepolia scenario matrix (scripts/scenarios.py --target sepolia, 20 scenarios at SCENARIO_PRICE_EUR 0.10):
# measured in reports/validation-sepolia.md (1.97 EURC and 4.50 Kč left the buyer, 40 landed txs).
AWS_BUDGET_USD = 800.0
EC2_USD_PER_DAY = 0.6  # t4g.small on-demand in eu-central-1, rounded up
CZK_PER_EUR = int(ENV.get("CZK_PER_EUR_DEMO") or 25)


def rpc_url() -> tuple[str, dict]:
    """Public Base Sepolia first (no Tatum credits spent); RPC_URL from .env when CREDITS_RPC=env."""
    if os.getenv("CREDITS_RPC") == "env" and ENV.get("RPC_URL"):
        headers = {"x-api-key": ENV["TATUM_API_KEY"]} if "tatum" in ENV["RPC_URL"] and ENV.get("TATUM_API_KEY") else {}
        return ENV["RPC_URL"], headers
    return PUBLIC_RPC, {}


_client = httpx.Client(timeout=15)


def rpc(method: str, params: list, tries: int = 4):
    url, headers = rpc_url()
    for i in range(tries):
        try:
            r = _client.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                             headers={"Content-Type": "application/json", **headers})
            body = r.json()
            if "error" in body:
                raise RuntimeError(str(body["error"])[:200])
            return body["result"]
        except Exception:  # noqa: BLE001 - public node rate limits: back off and retry
            if i == tries - 1:
                raise
            time.sleep(1.5 * (i + 1))


def erc20_balance(token: str, owner: str) -> int:
    data = "0x70a08231" + owner.lower().removeprefix("0x").rjust(64, "0")
    return int(rpc("eth_call", [{"to": token, "data": data}, "latest"]), 16)


def addr_of(pk_name: str) -> str | None:
    pk = ENV.get(pk_name)
    if not pk:
        return None
    try:
        return Account.from_key(pk).address
    except Exception:  # noqa: BLE001
        return None


def addresses() -> dict[str, dict]:
    """address -> {roles, pays_gas_for}. Keys only for derivation, never kept or printed."""
    roles = [
        ("relayer", addr_of("BANK_RELAYER_PK"), ["x402_settle"]),
        ("admin", addr_of("BANK_ADMIN_PK"), ["mint", "burn", "allow", "freeze"]),
        ("operator", addr_of("BANK_OPERATOR_PK"), ["authorize", "capture", "void"]),
        # no BANK_CLEARING_PK in .env -> bank/config.py falls back to a placeholder fee receiver (nobody holds its key)
        ("clearing (fee receiver)", addr_of("BANK_CLEARING_PK") or ENV.get("BANK_CLEARING_ADDR")
         or "0xC1ea41a6C0000000000000000000000000000bA4", ["transfer"] if ENV.get("BANK_CLEARING_PK") else []),
        ("buyer alfa", addr_of("ALFA_PK"), ["attack_direct_transfer"]),
        ("seller beta", ENV.get("BETA_ADDR"), []),
        ("collector (ERC-3009)", ENV.get("COLLECTOR"), []),
        ("operator token store", ENV.get("OPERATOR_TOKEN_STORE"), []),
        ("escrow contract", ENV.get("ESCROW"), []),
        ("refund collector", ENV.get("REFUND_COLLECTOR") or "0x7a03443724d14798c4AB4622F1DAAcA761Fea486", []),
    ]
    out: dict[str, dict] = {}
    for role, addr, kinds in roles:
        if not addr:
            out.setdefault(f"(missing: {role})", {"roles": [role], "pays_gas_for": kinds, "missing": True})
            continue
        e = out.setdefault(addr, {"roles": [], "pays_gas_for": []})
        e["roles"].append(role)
        e["pays_gas_for"] += [k for k in kinds if k not in e["pays_gas_for"]]
    return out


def scenario_txs() -> list[tuple[str, str]]:
    """(kind, tx_hash) of every landed tx in the Base Sepolia scenario reports + the mint from REFERENCE."""
    seen, out = set(), []
    for f in sorted((ROOT / "reports").glob("scenarios-sepolia*.json")):
        for r in json.loads(f.read_text(encoding="utf-8"))["results"]:
            for t in r.get("txs") or []:
                if t.get("tx_hash") and t["tx_hash"] not in seen:
                    seen.add(t["tx_hash"])
                    out.append((t["kind"], t["tx_hash"]))
    ref = (ROOT / "docs" / "REFERENCE.md").read_text(encoding="utf-8")
    m = re.search(r"minted [^\]]*\]\(https://sepolia\.basescan\.org/tx/(0x[0-9a-f]{64})\)", ref)
    if m:
        out.append(("mint", m.group(1)))
    return out


def gas_by_kind() -> dict:
    txs = scenario_txs()

    def receipt(kh):
        kind, h = kh
        try:
            r = rpc("eth_getTransactionReceipt", [h])
        except Exception as e:  # noqa: BLE001
            return kind, None, str(e)[:120]
        return kind, r, None

    with ThreadPoolExecutor(4) as ex:
        rows = list(ex.map(receipt, txs))
    kinds: dict[str, list] = {}
    errors = 0
    for kind, r, err in rows:
        if r is None:
            errors += 1
            continue
        gas = int(r["gasUsed"], 16)
        eff = int(r.get("effectiveGasPrice") or "0x0", 16)
        l1 = int(r.get("l1Fee") or "0x0", 16)
        kinds.setdefault(kind, []).append({"gas": gas, "l2_fee_wei": gas * eff, "l1_fee_wei": l1,
                                           "status": int(r["status"], 16)})
    out = {}
    for kind, xs in kinds.items():
        g = [x["gas"] for x in xs]
        out[kind] = {"n": len(xs), "effective_gas_price_wei_median": int(statistics.median(
                         x["l2_fee_wei"] // max(1, x["gas"]) for x in xs)), "gas_median": int(statistics.median(g)), "gas_max": max(g), "gas_min": min(g),
                     "l1_fee_wei_median": int(statistics.median(x["l1_fee_wei"] for x in xs)),
                     "paid_wei_median": int(statistics.median(x["l2_fee_wei"] + x["l1_fee_wei"] for x in xs)),
                     "reverted": sum(1 for x in xs if x["status"] == 0)}
    return {"by_kind": out, "receipts": len(rows) - errors, "receipt_errors": errors,
            "source": "eth_getTransactionReceipt of the tx hashes in reports/scenarios-sepolia*.json "
                      "(+ the tCZK mint linked in docs/REFERENCE.md)"}


# fallbacks only when a kind has no landed sample on Base Sepolia
GAS_FALLBACK = {"authorize": 150_000, "capture": 71_000, "void": 64_000, "mint": 50_000, "burn": 50_000,
                "allow": 50_000, "freeze": 50_000, "transfer": 40_000, "x402_settle": 90_000,
                "attack_direct_transfer": 40_000}


def apify() -> dict:
    token = ENV.get("APIFY_TOKEN")
    if not token:
        return {"ok": False, "error": "no APIFY_TOKEN"}
    try:
        r = httpx.get("https://api.apify.com/v2/users/me/limits", headers={"Authorization": f"Bearer {token}"},
                      timeout=10)
        r.raise_for_status()
        d = r.json()["data"]
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:200]}
    lim, cur = d.get("limits") or {}, d.get("current") or {}
    used, cap = cur.get("monthlyUsageUsd"), lim.get("maxMonthlyUsageUsd")
    out = {"ok": True, "cycle": d.get("monthlyUsageCycle"), "monthly_usage_usd": used, "max_monthly_usage_usd": cap,
           "left_usd": round(cap - used, 4) if used is not None and cap is not None else None,
           "actor_compute_units": cur.get("monthlyActorComputeUnits"),
           "max_actor_compute_units": lim.get("maxMonthlyActorComputeUnits"),
           "actor_memory_gbytes": cur.get("actorMemoryGbytes"), "max_actor_memory_gbytes": lim.get("maxActorMemoryGbytes"),
           "active_actor_jobs": cur.get("activeActorJobCount"), "max_concurrent_actor_jobs": lim.get("maxConcurrentActorJobs")}
    # cost of one company-check run, from the last runs of this account's actors (read-only)
    try:
        r = httpx.get("https://api.apify.com/v2/actor-runs", params={"limit": 50, "desc": 1},
                      headers={"Authorization": f"Bearer {token}"}, timeout=10)
        runs = r.json()["data"]["items"]
        usd = [x.get("usageTotalUsd") for x in runs if x.get("usageTotalUsd") is not None]
        secs = [(time.mktime(time.strptime(x["finishedAt"][:19], "%Y-%m-%dT%H:%M:%S"))
                 - time.mktime(time.strptime(x["startedAt"][:19], "%Y-%m-%dT%H:%M:%S")))
                for x in runs if x.get("finishedAt") and x.get("startedAt")]
        out["last_runs"] = {"n": len(runs), "usd_median": round(statistics.median(usd), 5) if usd else None,
                            "usd_max": round(max(usd), 5) if usd else None,
                            "runtime_s_median": round(statistics.median(secs), 2) if secs else None,
                            "succeeded": sum(1 for x in runs if x.get("status") == "SUCCEEDED")}
        if usd and out["left_usd"] is not None and statistics.median(usd) > 0:
            out["runs_left_at_median"] = int(out["left_usd"] / statistics.median(usd))
    except Exception as e:  # noqa: BLE001
        out["last_runs"] = {"error": str(e)[:200]}
    return out


def tatum() -> dict:
    key = ENV.get("TATUM_API_KEY")
    if not key:
        return {"ok": False, "error": "no TATUM_API_KEY"}
    out = {"ok": False}
    for path in ("/v3/tatum/version", "/v3/tatum/usage"):
        try:
            r = httpx.get("https://api.tatum.io" + path, headers={"x-api-key": key}, timeout=10)
            out[path] = r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text[:200]
            out[path + " status"] = r.status_code
            out["ok"] = out["ok"] or r.status_code == 200
        except Exception as e:  # noqa: BLE001
            out[path] = {"error": str(e)[:200]}
    v = out.get("/v3/tatum/version")
    if isinstance(v, dict) and v.get("creditLimit") is not None:
        out["credit_limit"], out["credit_usage"] = v.get("creditLimit"), v.get("usage")
        try:
            out["credits_left"] = int(v["creditLimit"]) - int(v.get("usage") or 0)
        except (TypeError, ValueError):
            pass
    # the bank's incoming-transfer poller (bank/webhooks.py) costs 3 RPC calls per tick when it runs
    poll_s = float(ENV.get("BANK_POLL_S") or 1.0)
    out["poller"] = {"on": not ENV.get("PUBLIC_BASE_URL") and os.getenv("BANK_POLLER", "1") != "0",
                     "rpc_calls_per_hour": int(3 * 3600 / poll_s), "via_tatum": "tatum" in (ENV.get("RPC_URL") or ""),
                     "note": "PUBLIC_BASE_URL set -> Tatum webhooks, poller off" if ENV.get("PUBLIC_BASE_URL")
                     else "poller on: 3 RPC calls per BANK_POLL_S tick against RPC_URL"}
    return out


def bedrock() -> dict:
    """Model spend measured in the live eval run (reports/evals-live.json, list-price estimate, bank/timing.py)."""
    d = json.loads((ROOT / "reports" / "evals-live.json").read_text(encoding="utf-8"))
    tot, inj = d["llm_totals"], d["injections"]
    st = {s["stage"]: s for s in tot["by_stage"]}
    live_deals = inj.get("live_runs") or 1  # one buyer-agent deal per live injection run
    judge_items = d["judge"]["items"]
    buyer = st["buyer"]["cost_usd"] / live_deals
    criteria = st["criteria"]["cost_usd"] / live_deals
    judge_per_item = st["judge"]["cost_usd"] / judge_items
    explain_per_text = st["explain"]["cost_usd"] / max(1, st["explain"]["calls"])
    per_deal = buyer + criteria + 20 * judge_per_item + 2 * explain_per_text  # 20-item delivery, ~2 explanations
    # demo run: 5 buyer-agent deals; 2 deliveries judged (honest + fake_data + tczk = 3 deliveries of 20 items)
    per_demo_run = 5 * (buyer + criteria) + 3 * 20 * judge_per_item + 5 * explain_per_text
    days_left = (time.mktime((2026, 10, 20, 23, 59, 0, 0, 0, -1)) - time.time()) / 86400
    out = {"mode": d["mode"], "eval_run_usd": round(tot["cost_usd"], 4), "eval_run_calls": tot["calls"],
           "per_stage": {k: {"calls": v["calls"], "usd": round(v["cost_usd"], 4),
                             "mean_ms_per_call": round(v["ms"] / v["calls"]),
                             "usd_per_call": round(v["cost_usd"] / v["calls"], 5)} for k, v in st.items()},
           "usd_per_deal": round(per_deal, 4), "usd_per_demo_run": round(per_demo_run, 4),
           "aws_budget_usd": AWS_BUDGET_USD, "ec2_usd_per_day": EC2_USD_PER_DAY,
           "creds": "expired (MOCK=1): AWS Cost Explorer not readable; spend so far estimated from logged tokens",
           "how": "list prices x tokens logged per call (bank/timing.py, EU profile +10 %); buyer+criteria per live "
                  f"agent deal = stage cost / {live_deals} live runs; judge per item = judge cost / {judge_items} items"}
    out["demo_runs_affordable"] = int((AWS_BUDGET_USD - out["eval_run_usd"] * 3 - EC2_USD_PER_DAY * 3) / per_demo_run)
    out["days_left_today"] = round(max(0.0, days_left), 2)
    return out


def main():
    t0 = time.time()
    gas_price = int(rpc("eth_gasPrice", []), 16)
    block = rpc("eth_getBlockByNumber", ["latest", False])
    base_fee = int(block.get("baseFeePerGas") or "0x0", 16)
    try:
        tip = int(rpc("eth_maxPriorityFeePerGas", []), 16)
    except Exception:  # noqa: BLE001
        tip = 0
    max_fee = 2 * base_fee + tip  # web3 build_transaction default: the node checks balance >= gas x maxFee
    gas = gas_by_kind()
    kinds = gas["by_kind"]

    def tx_cost(kind: str) -> dict:
        k = kinds.get(kind)
        g = k["gas_median"] if k else GAS_FALLBACK.get(kind, 100_000)
        l1 = k["l1_fee_wei_median"] if k else 0
        return {"gas": g, "measured": bool(k), "cost_wei_now": g * gas_price + l1, "needs_wei": g * max_fee + l1}

    eurc = ENV.get("EURC") or EURC_DEFAULT
    tczk = ENV.get("TCZK")
    rows = []
    for addr, info in addresses().items():
        row = {"address": addr, **info}
        if info.get("missing"):
            rows.append(row)
            continue
        row["eth_wei"] = int(rpc("eth_getBalance", [addr, "latest"]), 16)
        row["eth"] = row["eth_wei"] / 1e18
        row["eurc"] = erc20_balance(eurc, addr) / 1e6
        row["tczk"] = erc20_balance(tczk, addr) / 100 if tczk else None
        code = rpc("eth_getCode", [addr, "latest"])
        row["is_contract"] = code not in ("0x", "0x0", None)
        row["txs_left"] = {}
        for kind in info["pays_gas_for"]:
            c = tx_cost(kind)
            row["txs_left"][kind] = int(row["eth_wei"] // c["needs_wei"]) if c["needs_wei"] else None
        rows.append(row)

    # what one demo run costs each gas payer
    op_wei = sum(n * tx_cost(k)["cost_wei_now"] for k, n in DEMO_RUN["operator_txs"].items())
    op_need = max(tx_cost(k)["needs_wei"] for k in DEMO_RUN["operator_txs"])
    by_role = {r: x for x in rows for r in x["roles"] if not x.get("missing")}
    op = by_role.get("operator")
    buyer = by_role.get("buyer alfa")
    sepolia_matrix = json.loads((ROOT / "reports" / "validation-sepolia.json").read_text(encoding="utf-8"))
    md = (ROOT / "reports" / "validation-sepolia.md").read_text(encoding="utf-8")
    m = re.search(r"Cost of this run: ([\d.]+) EURC and ([\d.,]+) Kč", md)
    matrix_eurc, matrix_czk = (float(m.group(1)), float(m.group(2).replace(",", "."))) if m else (None, None)
    matrix_gas = int(re.search(r"gas used by landed txs: (\d+)", md).group(1)) if "gas used by landed" in md else None

    runway = {}
    if op:
        runway["operator ETH (escrow txs)"] = {"demo_runs": int(op["eth_wei"] // op_wei) if op_wei else None,
                                               "per_demo_run_wei": op_wei, "min_balance_for_next_tx_wei": op_need}
    if buyer:
        full = lambda bal, spent, peak: max(0, int((bal - peak) // spent) + 1) if bal >= peak else 0  # noqa: E731
        runway["buyer EURC"] = {"demo_runs": full(buyer["eurc"], DEMO_RUN["EURC_spent"], DEMO_RUN["EURC_peak_lock"]),
                                "per_demo_run": DEMO_RUN["EURC_spent"], "balance": buyer["eurc"],
                                "sepolia_matrices": int(buyer["eurc"] // matrix_eurc) if matrix_eurc else None}
        runway["buyer tCZK"] = {"demo_runs": full(buyer["tczk"] or 0, DEMO_RUN["tCZK_spent"], DEMO_RUN["tCZK_peak_lock"]),
                                "per_demo_run": DEMO_RUN["tCZK_spent"], "balance": buyer["tczk"],
                                "sepolia_matrices": int((buyer["tczk"] or 0) // matrix_czk) if matrix_czk else None}
    rel = by_role.get("relayer")
    if rel:
        c = tx_cost("x402_settle")
        runway["relayer ETH (x402 settles)"] = {"settles": int(rel["eth_wei"] // c["needs_wei"])}
    adm = by_role.get("admin")
    if adm:
        c = tx_cost("mint")
        runway["admin ETH (tCZK mint/burn/allow)"] = {"txs": int(adm["eth_wei"] // c["needs_wei"])}
    if matrix_gas and op:
        runway["operator ETH (Sepolia scenario matrices)"] = {
            "matrices": int(op["eth_wei"] // (matrix_gas * gas_price)), "gas_per_matrix": matrix_gas,
            "note": "upper bound: the matrix gas is split between operator and relayer"}
    if op:  # Base Sepolia gas spikes: what is left at 10x and 100x today's price
        runway["operator ETH at 10x gas price"] = {"demo_runs": int(op["eth_wei"] // (op_wei * 10))}
        runway["operator ETH at 100x gas price"] = {"demo_runs": int(op["eth_wei"] // (op_wei * 100))}

    out = {"generated": time.strftime("%Y-%m-%d %H:%M %Z"), "rpc": rpc_url()[0], "network": "Base Sepolia (84532)",
           "gas_price_wei": gas_price, "base_fee_wei": base_fee, "priority_fee_wei": tip, "max_fee_wei": max_fee,
           "block": int(block["number"], 16), "gas": gas,
           "tx_cost_now": {k: tx_cost(k) for k in sorted(set(kinds) | set(GAS_FALLBACK))},
           "addresses": rows, "demo_run": DEMO_RUN, "runway": runway,
           "sepolia_matrix": {"eurc_spent": matrix_eurc, "czk_spent": matrix_czk, "gas": matrix_gas,
                              "txs_landed": sepolia_matrix["summary"].get("txs_landed")},
           "apify": apify(), "tatum": tatum(), "bedrock": bedrock(), "seconds": round(time.time() - t0, 1)}
    out["conclusion"] = conclude(out)
    OUT.write_text(json.dumps(out, indent=1, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
    show(out)


def conclude(o: dict) -> list[str]:
    lines, cands = [], []
    for name, r in o["runway"].items():
        n = r.get("demo_runs")
        if n is not None and "gas price" not in name:
            cands.append((n, name))
    ap = o["apify"]
    if ap.get("runs_left_at_median") is not None:
        cands.append((ap["runs_left_at_median"] // 5, "Apify (5 company checks per demo run, upper bound)"))
    b = o["bedrock"]
    cands.append((b["demo_runs_affordable"], "AWS budget (Bedrock + EC2)"))
    cands.sort()
    if cands:
        n, name = cands[0]
        lines.append(f"First to run out: {name} — after {n} demo runs (five buttons each).")
        lines += [f"  then {name2}: {n2} demo runs" for n2, name2 in cands[1:4]]
    for x in o["addresses"]:
        if x.get("missing"):
            lines.append(f"Missing key/address in .env: {x['roles'][0]} — cannot be checked")
    for name, r in o["runway"].items():
        n = r.get("demo_runs")
        if n is not None and n < 10 and "gas price" not in name:
            lines.append(f"TOP UP before the jury: {name} covers only {n} demo runs")
    sp = [(k, r["demo_runs"]) for k, r in o["runway"].items() if "gas price" in k]
    if sp:
        lines.append("Gas spike risk: operator ETH lasts " + ", ".join(f"{n} demo runs {k.split(' at ')[1]}" for k, n in sp)
                     + " -> top up operator and relayer ETH from a Base Sepolia faucet (cheap insurance)")
    if o["runway"].get("buyer tCZK"):
        lines.append("tCZK needs no faucet: the bank mints it (admin key, bank/tczk.py mint) — a tx the team sends")
    t = o["tatum"]
    if t.get("credits_left") is not None:
        h = t["credits_left"] / t["poller"]["rpc_calls_per_hour"]
        lines.append(f"Tatum credits left: {t['credits_left']:,} of {t['credit_limit']:,} (plan {t['/v3/tatum/version'].get('planName')});"
                     f" the bank poller ({'ON' if t['poller']['on'] else 'off'}) makes {t['poller']['rpc_calls_per_hour']:,}"
                     f" RPC calls/h -> ~{h:.0f} h at 1 credit/call, ~{h / 10:.0f} h at 10 credits/call")
    return lines


def show(o: dict):
    print(f"Base Sepolia block {o['block']}, gas price {o['gas_price_wei'] / 1e9:.6f} gwei "
          f"(base {o['base_fee_wei'] / 1e9:.6f}, maxFee {o['max_fee_wei'] / 1e9:.6f}), rpc {o['rpc']}")
    print("\nGas per tx kind (median of Sepolia receipts; cost now incl. measured L1 fee):")
    for k, c in o["tx_cost_now"].items():
        n = o["gas"]["by_kind"].get(k, {}).get("n", 0)
        print(f"  {k:24s} gas {c['gas']:>8,}  {'n=' + str(n) if c['measured'] else 'assumed':>8}  "
              f"cost now {c['cost_wei_now'] / 1e18:.10f} ETH")
    print("\nAddresses:")
    print(f"  {'role':38s} {'address':44s} {'ETH':>12s} {'EURC':>10s} {'tCZK':>10s}  txs left")
    for x in o["addresses"]:
        if x.get("missing"):
            print(f"  {', '.join(x['roles']):38s} MISSING")
            continue
        tz = "-" if x["tczk"] is None else f"{x['tczk']:.2f}"
        left = ", ".join(f"{k} {v:,}" for k, v in x["txs_left"].items())
        print(f"  {', '.join(x['roles'])[:38]:38s} {x['address']:44s} {x['eth']:12.6f} {x['eurc']:10.4f} {tz:>10s}  {left}")
    print("\nRunway:")
    for k, v in o["runway"].items():
        print(f"  {k}: {v}")
    a = o["apify"]
    print(f"\nApify: {a.get('monthly_usage_usd')} of {a.get('max_monthly_usage_usd')} USD this cycle; "
          f"last runs {a.get('last_runs')}" if a.get("ok") else f"\nApify: {a.get('error')}")
    t = o["tatum"]
    print(f"Tatum: limit {t.get('credit_limit')}, used {t.get('credit_usage')}, left {t.get('credits_left')}; "
          f"poller {t['poller']}" if t.get("ok") else f"Tatum: {t}")
    b = o["bedrock"]
    print(f"Bedrock: eval run ${b['eval_run_usd']} ({b['eval_run_calls']} calls); ${b['usd_per_deal']} per deal; "
          f"${b['usd_per_demo_run']} per demo run; budget ${b['aws_budget_usd']} -> {b['demo_runs_affordable']} demo runs")
    print("\nConclusion:")
    for line in o["conclusion"]:
        print("  " + line)
    print(f"\nwritten {OUT.relative_to(ROOT)} in {o['seconds']} s")


if __name__ == "__main__":
    main()
