# Test stack for smoke.py and scenarios.py (SPEC §19): the bank and the seller as real processes on free ports,
# each run with its own SQLite under data/q-runs/<stamp>/, against one of two targets:
#   fork    — anvil fork of Base Sepolia (chain id 84532 kept, so EIP-712 domains and escrow hashes match the live
#             chain). Fresh deterministic keys get gas by anvil_setBalance, EURC by impersonating the demo buyer
#             (or the token's master minter), and a fresh tCZK is deployed and allowlisted like Deploy.s.sol.
#   sepolia — the real Base Sepolia with the keys and addresses from .env; nothing is funded or deployed here.
# The same scenario code runs on both; only RPC_URL and the keys differ.
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
from eth_account import Account
from web3 import Web3

from bank import chain, config, db

ROOT = config.ROOT
ANVIL = os.getenv("ANVIL") or shutil.which("anvil") or str(Path.home() / ".foundry" / "bin" / "anvil")
FORK_URL = os.getenv("FORK_URL", "https://sepolia.base.org")
FUNDED_BUYER = "0x491BeCCB4E26E1c5781E1E44c48Ad474D04d2140"  # demo buyer on Base Sepolia, holds testnet EURC
ROLES = ("relayer", "admin", "operator", "alfa")
KEY_VARS = {"relayer": "BANK_RELAYER_PK", "admin": "BANK_ADMIN_PK", "operator": "BANK_OPERATOR_PK", "alfa": "ALFA_PK"}
FIAT_ABI = [
    {"name": "masterMinter", "type": "function", "stateMutability": "view", "inputs": [],
     "outputs": [{"type": "address"}]},
    {"name": "configureMinter", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"name": "minter", "type": "address"}, {"name": "amount", "type": "uint256"}],
     "outputs": [{"type": "bool"}]},
    {"name": "mint", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"name": "to", "type": "address"}, {"name": "amount", "type": "uint256"}],
     "outputs": [{"type": "bool"}]},
]
# Tokens for the bank API (the integrator's shared-secret auth; defaults match bank.config).
AGENT_TOKEN = os.getenv("AGENT_TOKEN_ALFA", "dev-agent-alfa")
HUMAN_TOKEN = os.getenv("HUMAN_TOKEN", "dev-human")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def price_per_item_cents() -> int:
    """Small amounts for the matrix (SPEC §19.2): SCENARIO_PRICE_EUR per 20-item deal, at least 1 cent per item
    (the seller prices whole euro cents per item, so 0.10 € becomes 20 × 0.01 € = 0.20 €)."""
    if os.getenv("SELLER_PRICE_PER_ITEM_EUR_CENTS"):
        return int(os.environ["SELLER_PRICE_PER_ITEM_EUR_CENTS"])
    return max(1, round(float(config.SCENARIO_PRICE_EUR) * 100) // 20)


def fork_key(role: str) -> str:
    """Deterministic throwaway key for the fork (never used on a public chain)."""
    return Web3.keccak(text=f"collabse-q-fork-{role}").to_0x_hex()


def wait_http(url: str, proc: subprocess.Popen | None = None, timeout: float = 60) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if proc is not None and proc.poll() is not None:
            return False
        try:
            if httpx.get(url, timeout=2).status_code == 200:
                return True
        except httpx.HTTPError:
            pass
        time.sleep(0.3)
    return False


class Stack:
    """with Stack("fork") as s: ...  — s.bank_url, s.seller_url, s.env, s.keys, s.addr, s.w3, s.label"""

    def __init__(self, target: str = "fork", faults: str = "", run_dir: Path | None = None, eurc_fund: int = 400_000_000,
                 tczk_fund: int = 200_000):
        if target not in ("fork", "sepolia"):
            raise ValueError("target must be fork or sepolia")
        self.target, self.faults = target, faults
        self.run_dir = run_dir or ROOT / "data" / "q-runs" / time.strftime("%Y%m%d-%H%M%S")
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.eurc_fund, self.tczk_fund = eurc_fund, tczk_fund
        self.procs: dict[str, subprocess.Popen] = {}
        self.anvil = None
        self.fork_block = None
        self.profile = None

    # ---- lifecycle ----
    def __enter__(self):
        try:
            self.start()
        except BaseException:
            self.stop()
            raise
        return self

    def __exit__(self, *exc):
        self.stop()

    def start(self):
        self.bank_port, self.seller_port = free_port(), free_port()
        self.bank_url, self.seller_url = f"http://127.0.0.1:{self.bank_port}", f"http://127.0.0.1:{self.seller_port}"
        if self.target == "fork":
            self._start_fork()
        else:
            self._use_sepolia()
        self.use_profile("main", self.faults)
        self._start_seller()

    def stop(self):
        for p in list(self.procs.values()) + ([self.anvil] if self.anvil else []):
            if p and p.poll() is None:
                p.terminate()
                try:
                    p.wait(5)
                except subprocess.TimeoutExpired:
                    p.kill()
        self.procs.clear()
        chain.set_w3(None)
        db.close()

    @property
    def label(self) -> str:
        if self.target == "fork":
            return f"anvil fork of Base Sepolia (chain id 84532) at block {self.fork_block}"
        return "Base Sepolia (chain id 84532)"

    # ---- targets ----
    def _start_fork(self):
        if not Path(ANVIL).exists():
            raise SystemExit("anvil not found: install Foundry (curl -L https://foundry.paradigm.xyz | bash)")
        port = free_port()
        self.anvil = subprocess.Popen([ANVIL, "--fork-url", FORK_URL, "--port", str(port), "--silent"],
                                      stdout=subprocess.DEVNULL, stderr=open(self.run_dir / "anvil.log", "w"))
        self.rpc_url = f"http://127.0.0.1:{port}"
        w3 = Web3(Web3.HTTPProvider(self.rpc_url, request_kwargs={"timeout": 60}))
        for _ in range(120):
            if self.anvil.poll() is not None:
                raise SystemExit(f"anvil fork failed, see {self.run_dir / 'anvil.log'}")
            try:
                if w3.eth.chain_id == 84532:
                    break
            except Exception:  # noqa: BLE001 — not up yet
                time.sleep(0.5)
        else:
            raise SystemExit("anvil fork did not start (is https://sepolia.base.org reachable?)")
        self.fork_block = w3.eth.block_number
        self.keys = {r: fork_key(r) for r in ROLES}
        self.addr = {r: Account.from_key(k).address for r, k in self.keys.items()}
        for a in self.addr.values():
            w3.provider.make_request("anvil_setBalance", [a, hex(10**18)])
        self.base_env = {
            "RPC_URL": self.rpc_url, "TCZK_NAME": "tCZK Deposit Token", "ALFA_ADDR": self.addr["alfa"],
            "BANK_OPERATOR_ADDR": self.addr["operator"], "MOCK": "1",
            # a fork run never reaches live services, even when started from a checkout whose .env has real tokens
            "TATUM_API_KEY": "", "APIFY_TOKEN": "", "MOSTLYRIGHT_API_KEY": "",
            # the runner signs F04/F05 webhooks with this throwaway secret; no public URL, so the bank's poller runs
            "TATUM_HMAC_SECRET": "q-fork-webhook-secret", "PUBLIC_BASE_URL": "",
            **{KEY_VARS[r]: k for r, k in self.keys.items()}}
        self._apply_env({**self.base_env, "DB_PATH": str(self.run_dir / "setup.db"), "MOCK": "1", "FAULTS": ""})
        self._fund_eurc(w3, self.addr["alfa"], self.eurc_fund)
        from bank import escrow, tczk
        store = escrow.contract().functions.getTokenStore(self.addr["operator"]).call()
        token = tczk.deploy(self.keys["admin"], relayer=self.addr["relayer"], store=store, mint_alfa=0)
        self.base_env["TCZK"] = token
        self._apply_env({**self.base_env, "DB_PATH": str(self.run_dir / "setup.db"), "MOCK": "1", "FAULTS": ""})
        tczk.allow(self.addr["alfa"], "1803729684/9999")  # Alfa's account number (fixtures/companies.json)
        tczk.mint(self.addr["alfa"], self.tczk_fund, "q-fork-deposit-alfa")
        self.w3 = chain.w3()

    def _fund_eurc(self, w3, to: str, amount: int):
        """EURC on the fork: mint through the token's master minter; fall back to the demo buyer's balance."""
        eurc = w3.eth.contract(address=Web3.to_checksum_address(config.EURC), abi=chain.ERC20_ABI + FIAT_ABI)
        try:
            mm = eurc.functions.masterMinter().call()
            w3.provider.make_request("anvil_impersonateAccount", [mm])
            w3.provider.make_request("anvil_setBalance", [mm, hex(10**18)])
            h = eurc.functions.configureMinter(mm, amount).transact({"from": mm})
            w3.eth.wait_for_transaction_receipt(h)
            h = eurc.functions.mint(to, amount).transact({"from": mm})
            ok = w3.eth.wait_for_transaction_receipt(h)["status"] == 1
            w3.provider.make_request("anvil_stopImpersonatingAccount", [mm])
            if ok:
                self.funding = f"minted {amount / 1e6:.2f} EURC via the token's master minter (fork only)"
                return
        except Exception:  # noqa: BLE001 — not a FiatToken with a master minter: use the funded buyer
            pass
        have = eurc.functions.balanceOf(FUNDED_BUYER).call()
        w3.provider.make_request("anvil_impersonateAccount", [FUNDED_BUYER])
        w3.provider.make_request("anvil_setBalance", [FUNDED_BUYER, hex(10**18)])
        h = eurc.functions.transfer(to, min(have, amount)).transact({"from": FUNDED_BUYER})
        w3.eth.wait_for_transaction_receipt(h)
        w3.provider.make_request("anvil_stopImpersonatingAccount", [FUNDED_BUYER])
        self.funding = f"moved {min(have, amount) / 1e6:.2f} EURC from the demo buyer by impersonation (fork only)"

    def _use_sepolia(self):
        missing = [v for v in (*KEY_VARS.values(), "TCZK") if not os.getenv(v, "").strip()]
        if missing:
            raise SystemExit(f"TODO(credential): set {', '.join(missing)} in .env for --target sepolia")
        self.rpc_url = config.RPC_URL
        self.keys = {r: os.environ[KEY_VARS[r]].strip() for r in ROLES}
        self.addr = {r: Account.from_key(k).address for r, k in self.keys.items()}
        self.base_env = {"BANK_OPERATOR_ADDR": self.addr["operator"], "ALFA_ADDR": self.addr["alfa"]}
        self.funding = "funded from faucets by hand (Base Sepolia)"
        self._apply_env({**self.base_env, "DB_PATH": str(self.run_dir / "setup.db"), "MOCK": os.getenv("MOCK", "1")})
        self.w3 = chain.w3()

    # ---- environment + processes ----
    def env_for(self, db_path: str, faults: str) -> dict:
        return {"MOCK": os.getenv("MOCK", "1"), **self.base_env, "DB_PATH": db_path, "FAULTS": faults,
                "BANK_URL": self.bank_url, "BANK_FACILITATOR_URL": f"{self.bank_url}/x402", "SELLER_URL": self.seller_url,
                "SELLER_FACILITATOR_URL": f"{self.bank_url}/x402",
                "SCENARIO_PRICE_EUR": config.SCENARIO_PRICE_EUR,
                "SELLER_PRICE_PER_ITEM_EUR_CENTS": str(price_per_item_cents()), "PYTHONPATH": str(ROOT),
                # the runner drives the dispute timer itself (deadlines.tick(now + window)); a second timer thread in
                # the bank process would act on the same deals from another process (disputes locks are per process)
                "BANK_TIMER": "0"}

    def _apply_env(self, env: dict):
        """The runner reads the same chain and DB as the bank (journal, onchain_txs, timings)."""
        os.environ.update(env)
        config.reload()
        chain.set_w3(None)
        db.close()

    def use_profile(self, name: str, faults: str = ""):
        """(Re)start the bank with its own DB (seeded) and FAULTS. The seller keeps running."""
        self.profile = name
        self.db_path = str(self.run_dir / f"bank-{name}.db")
        self.env = self.env_for(self.db_path, faults)
        self._apply_env(self.env)
        from scripts.seed import seed
        seed()
        db.run("UPDATE sellers SET base_url=?", (self.seller_url,))
        old = self.procs.pop("bank", None)
        if old and old.poll() is None:
            old.terminate()
            old.wait(10)
        self.procs["bank"] = self._spawn("bank", "bank.app:app", self.bank_port, self.env)
        if not wait_http(f"{self.bank_url}/health", self.procs["bank"]):
            raise SystemExit(f"bank did not start, see {self.run_dir / f'bank-{name}.log'}")
        from bank import facilitator
        facilitator.reset()

    def _start_seller(self):
        self.procs["seller"] = self._spawn("seller", "seller.app:app", self.seller_port, self.env)
        if not wait_http(f"{self.seller_url}/health", self.procs["seller"]):
            raise SystemExit(f"seller did not start, see {self.run_dir / 'seller.log'}")

    def _spawn(self, name, app, port, env):
        log = open(self.run_dir / (f"{name}-{self.profile}.log" if name == "bank" else f"{name}.log"), "a")
        return subprocess.Popen([sys.executable, "-m", "uvicorn", app, "--host", "127.0.0.1", "--port", str(port),
                                 "--log-level", "warning"], cwd=ROOT, env={**os.environ, **env},
                                stdout=log, stderr=subprocess.STDOUT)

    def describe(self) -> dict:
        return {"target": self.target, "label": self.label, "rpc": "anvil (local)" if self.target == "fork" else "Base Sepolia",
                "fork_block": self.fork_block, "funding": getattr(self, "funding", None),
                "addresses": self.addr, "tczk": self.base_env.get("TCZK") or config.TCZK, "run_dir": str(self.run_dir)}


if __name__ == "__main__":  # manual check: python -m scripts.stack [fork|sepolia]
    with Stack(sys.argv[1] if len(sys.argv) > 1 else "fork") as s:
        print(json.dumps(s.describe(), indent=1))
        print(httpx.get(f"{s.bank_url}/health").json(), httpx.get(f"{s.seller_url}/health").json())
