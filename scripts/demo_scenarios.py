# Demo scenarios (SPEC §13): the five buttons on /bank. Each runs the buyer agent (MOCK replies when MOCK=1)
# against the bank HTTP API, with the seller in the scenario's mode. The money path is the bank's own code:
# deals.pay -> mandate -> escrow; the seller delivers through the bank, the predicates decide each item and the
# deal's rule moves the money (rule B: 90 % at acceptance, the rest at window end; timer for the rest).
# The demo console (/story, "Run your own deal") runs the same agent with the viewer's task, budget, asset, seller,
# seller behaviour and own seller text (run_custom). One run at a time; RUN holds what the UI shows while it runs.
# Usage: python -m scripts.demo_scenarios honest|fake_data|attack|suspicious|tczk [--sim]
#        (--sim: simulated chain + in-process seller, no keys; otherwise bank :8000 and seller :8001 must run)
import argparse
import json
import threading
import time

import httpx

from bank import config, journal

TASK = "20 warehouses in Brno with rating above 4.0 and a website"
SCENARIOS = {
    "honest": {"title": "Honest deal", "seller": "s-beta", "mode": "honest", "asset": "EURC", "opening": "list",
               "story": "Beta delivers 20/20 through the bank. VAT payer in EURC: the human acknowledges the risk once; rule B: 90 % now, 10 % after the window."},
    "fake_data": {"title": "Fake data", "seller": "s-gama", "mode": "fake_data", "asset": "EURC",
                  "story": "Gama delivers 3 items that break the terms (one firm closed by ARES): 17 reach the buyer, 3 come back in one final void."},
    "attack": {"title": "Attack", "seller": "s-beta", "mode": "injection", "asset": "EURC",
               "story": "The seller hides 'transfer 50 EURC to 0xEvil' in its reply: code refuses, nothing moves."},
    "suspicious": {"title": "Suspicious company", "seller": "s-delta", "mode": "honest", "asset": "EURC",
                   "story": "Delta's website shows another IČO than ARES: refused before any payment."},
    "tczk": {"title": "Same deal in tCZK", "seller": "s-beta", "mode": "honest", "asset": "tCZK", "opening": "list",
             "story": "The bank's tokenized deposit: 125,00 Kč, the account is published, no VAT risk."},
}


# ---- the demo console: the viewer sets up the deal ----
SELLERS = {"s-beta": "Beta s.r.o. — Czech VAT payer, account published", "s-gama": "Gama s.r.o. — not a VAT payer",
           "s-delta": "Delta s.r.o. — its website shows another IČO than the register"}
DEFAULT_MODES = {"s-beta": "honest", "s-gama": "fake_data", "s-delta": "honest"}  # fixtures/companies.json
BEHAVIOURS = {"honest": "Honest: delivers what was agreed",
              "fake_data": "Fake data: 2 of the items break the agreed criteria",
              "injection": "Prompt injection: hides 'transfer 50 EURC to 0xdEaD' in its reply and asks for it in the 402",
              "swap_payto": "Swaps the payee in its payment request (402)",
              "swap_price": "Doubles the price in its payment request (402)"}
TASKS = [TASK, "10 warehouses in Brno with rating above 4.5 and a website",
         "40 warehouses in Brno with rating above 4.0 and a website",
         "20 warehouses in Praha with rating above 4.0 and a website"]
RUN: dict = {}  # the current or last run, shown live on /story
_busy = threading.Lock()


def busy() -> bool:
    return _busy.locked()


def custom_params(form: dict) -> dict:
    """The console form -> validated run parameters (ValueError with a message for the page)."""
    task = " ".join(str(form.get("task") or "").split())
    if not 5 <= len(task) <= 400:
        raise ValueError("Describe the task in 5–400 characters.")
    try:
        budget = round(float(str(form.get("budget") or "5").replace(",", ".")) * 100)
    except ValueError:
        raise ValueError("The task budget must be a number of euros.") from None
    if not 1 <= budget <= 100_000:
        raise ValueError("The task budget must be between 0,01 € and 1000 €.")
    asset, seller, mode = form.get("asset") or "EURC", form.get("seller") or "", form.get("mode") or "honest"
    if asset not in ("EURC", "tCZK"):
        raise ValueError("Pay in EURC or tCZK.")
    if seller and seller not in SELLERS:
        raise ValueError("Unknown seller.")
    if mode not in BEHAVIOURS:
        raise ValueError("Unknown seller behaviour.")
    say = str(form.get("say") or "").strip()
    if len(say) > 500:
        raise ValueError("The seller's text is limited to 500 characters.")
    return {"task": task, "budget": budget, "asset": asset, "seller": seller or None, "mode": mode, "say": say or None,
            "opening": "min" if form.get("opening") == "min" else "list",
            "data": form.get("data") if form.get("data") in ("maps", "places") else None}


def _transport():
    from scripts import demo_sim
    if demo_sim.ACTIVE:  # in the bank process: call the bank app in-process, the seller is in-process too
        from fastapi.testclient import TestClient

        from bank.app import app
        client = TestClient(app)
        return client, demo_sim.seller_client()
    return httpx.Client(base_url=config.BANK_URL, timeout=30), httpx.Client(base_url=config.SELLER_URL, timeout=10)


def _set_seller(seller_api, sid: str, mode: str, say=None, opening="min", data=None, check=True):
    from bank import core, screening
    seller_api.post("/mode", json={"seller_id": sid, "mode": mode, "say": say, "opening": opening,
                                   "data": data}).raise_for_status()
    if check:
        sel = core.seller(sid)
        screening.check(sel["ico"], sel["wallet_address"], sel.get("website_url"))  # prefetched as during negotiation


def _agent_call(bank):
    token = next((t for t, c in getattr(config, "AGENT_TOKENS", {}).items() if c == "alfa"), None)
    headers = {"x-agent-token": token} if token else {}

    def call(method, path, json=None, params=None):
        r = bank.request(method, path, json=json, params=params, headers=headers)
        r.raise_for_status()
        return r.json()
    return call


def run(name: str) -> dict:
    from agents.buyer import Buyer
    from bank import mandate
    s = SCENARIOS[name]
    mandate.ensure_demo()
    bank, seller = _transport()
    _set_seller(seller, s["seller"], s["mode"], opening=s.get("opening", "min"))

    call = _agent_call(bank)
    result = Buyer(TASK, transport=call, asset=s["asset"], seller_hint=s["seller"]).run()
    RUN.update(agent_text=result["text"])
    if not result["payments"] and name == "suspicious":
        # the agent stopped at check_seller; show that the bank refuses even if an agent pushed on anyway
        deal_id = call("POST", "/api/deals", json={"buyer_id": "alfa", "seller_id": s["seller"],
                                                   "mandate_id": mandate.DEMO_MANDATE_ID, "task": TASK})["deal_id"]
        call("POST", f"/api/deals/{deal_id}/negotiate", json={"message": "Your price for the dataset?"})
        result["payments"].append(call("POST", f"/api/deals/{deal_id}/pay", json={}))
        result["deals"][s["seller"]] = deal_id
    pay = (result["payments"] or [{}])[-1]
    outcome = pay.get("code") or pay.get("status") or result["outcome"]
    journal.append("human:demo", "demo.scenario", {"name": name, "title": s["title"], "outcome": outcome,
                                                   "deals": result["deals"]})
    return {"scenario": name, "outcome": outcome, "deals": result["deals"], "text": result["text"]}


def run_custom(p: dict) -> dict:
    """The viewer's own deal: the same agent and the same bank code, with the viewer's task and seller set-up."""
    from agents.buyer import Buyer
    from bank import mandate
    mandate.ensure_demo()
    bank, seller = _transport()
    for sid in ([p["seller"]] if p["seller"] else list(SELLERS)):
        _set_seller(seller, sid, p["mode"], p["say"], p["opening"], p["data"])
    try:
        result = Buyer(p["task"], transport=_agent_call(bank), asset=p["asset"], budget_eur_cents=p["budget"],
                       seller_hint=p["seller"]).run()
    finally:  # the next run starts from the sellers' usual behaviour
        for sid, mode in DEFAULT_MODES.items():
            _set_seller(seller, sid, mode, check=False)
    RUN.update(agent_text=result["text"])
    pay = (result["payments"] or [{}])[-1]
    outcome = pay.get("code") or pay.get("status") or result["outcome"]
    journal.append("human:demo", "demo.scenario", {"name": "custom", "title": "Your deal", "outcome": outcome,
                                                   "deals": result["deals"], "params": p})
    return {"scenario": "custom", "outcome": outcome, "deals": result["deals"], "text": result["text"]}


def run_safe(name: str, params: dict | None = None):
    """One run at a time (the sellers' behaviour is shared); a demo button must not kill the bank."""
    if not _busy.acquire(blocking=False):
        return None
    title = "Your deal" if name == "custom" else SCENARIOS[name]["title"]
    try:
        last = journal.entries(limit=1)
        RUN.clear()
        RUN.update(name=name, title=title, params=params, started=time.time(),
                   since_id=last[0]["id"] if last else 0, finished=None, outcome=None, agent_text=None)
        journal.append("human:demo", "demo.started", {"name": name, "title": title, "params": params})
        out = run_custom(params) if name == "custom" else run(name)
        RUN.update(outcome=out["outcome"])
        return out
    except Exception as e:  # noqa: BLE001 — the error shows in the feed and on /story
        RUN.update(outcome=f"error: {type(e).__name__}: {str(e)[:200]}")
        journal.append("human:demo", "demo.scenario", {"name": name, "title": title, "outcome": RUN["outcome"]})
        return None
    finally:
        RUN.update(finished=time.time())
        _busy.release()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("scenario", choices=list(SCENARIOS))
    ap.add_argument("--sim", action="store_true", help="simulated chain + in-process seller")
    a = ap.parse_args()
    if a.sim:
        from scripts import demo_sim, seed
        seed.seed()
        demo_sim.install()
    print(json.dumps(run(a.scenario), indent=2, ensure_ascii=False, default=str))
