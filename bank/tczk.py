# tCZK admin helpers (SPEC §3, INTERFACES stream T): allowlist, freeze, mint/burn with the bank admin
# key, the reads the facilitator's screening needs, and the Python fallback deploy (no Foundry needed:
# the ABI + bytecode built by forge are committed in contracts/artifacts/TCZK.json).
# Works with the prep fallback token too (TCZK_NAME="Demo Bank tCZK": allowlisted/setAllowlisted, no freeze).
#   python -m bank.tczk deploy      deploy + roles + allowlist §3.3 + demo mint, prints TCZK=...
#   python -m bank.tczk info        roles/allowlist of config.TCZK
#   python -m bank.tczk artifact    rebuild the artifact (forge out/ if present, else py-solc-x)
import json
import sys
import time
from functools import cache

from eth_abi import encode
from web3 import Web3

from bank import chain, config, db

CONTRACTS = config.ROOT / "contracts"
ARTIFACT = CONTRACTS / "artifacts" / "TCZK.json"
PREP_NAME = "Demo Bank tCZK"
ROLES = {r: Web3.keccak(text=r) for r in ("ISSUER_ROLE", "COMPLIANCE_ROLE", "ENFORCER_ROLE", "ROUTER_ROLE")}

PREP_ABI = [  # prep/contracts/DepositToken.sol (disclosed pre-hackathon token, stage fallback)
    {"name": "allowlisted", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "a", "type": "address"}], "outputs": [{"type": "bool"}]},
    {"name": "setAllowlisted", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"name": "a", "type": "address"}, {"name": "allowed", "type": "bool"}], "outputs": []},
    {"name": "mint", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"name": "to", "type": "address"}, {"name": "value", "type": "uint256"}], "outputs": []},
]
ESCROW_ABI = [{"name": "getTokenStore", "type": "function", "stateMutability": "view",
               "inputs": [{"name": "operator", "type": "address"}], "outputs": [{"type": "address"}]}]


@cache
def artifact() -> dict:
    return json.loads(ARTIFACT.read_text())


def is_prep() -> bool:
    return config.TCZK_NAME == PREP_NAME


def token(address: str | None = None):
    address = address or config.TCZK
    if not address:
        raise RuntimeError("TCZK is not set in .env (deploy: python -m bank.tczk deploy)")
    abi = PREP_ABI if is_prep() and address == config.TCZK else artifact()["abi"]
    return chain.w3().eth.contract(address=Web3.to_checksum_address(address), abi=abi)


def account_ref(account_number: str) -> bytes:
    """keccak256(salt ‖ account number): binds a wallet to an account without putting the number on chain."""
    return Web3.keccak(config.ACCOUNT_REF_SALT.encode() + account_number.encode())


def bytes32(ref: str | bytes) -> bytes:
    """Readable refs ("dep-1", "case-7") stay readable on the explorer: left-aligned if ≤ 32 bytes, else hashed."""
    raw = ref if isinstance(ref, bytes) else ref.encode()
    return raw.ljust(32, b"\0") if len(raw) <= 32 else Web3.keccak(raw)


# ---- reads (facilitator screening) ----

def is_allowed(addr: str, address: str | None = None) -> bool:
    t, a = token(address), Web3.to_checksum_address(addr)
    return (t.functions.allowlisted(a) if is_prep() else t.functions.isAllowed(a)).call()


def is_frozen(addr: str, address: str | None = None) -> bool:
    if is_prep():
        return False  # the prep token has no freeze; the bank-side freeze still applies (frozen_wallets)
    return token(address).functions.isFrozen(Web3.to_checksum_address(addr)).call()


def bank_frozen(addr: str) -> bool:
    """Bank-side freeze: applies to every asset the bank moves (EURC too), not only to tCZK on chain."""
    return db.one("SELECT 1 FROM frozen_wallets WHERE lower(address)=lower(?)", (addr,)) is not None


# ---- admin writes (bank admin key) ----

def _send(call, kind, idem_key=None, gas=None, deal_id=None):
    return chain.sender(config.BANK_ADMIN_PK).send(call, kind=kind, deal_id=deal_id, idem_key=idem_key, gas=gas)


def allow(addr: str, ref: str | bytes) -> dict:
    """ref: the client's account number (hashed with the salt) or a ready 32-byte accountRef."""
    a = Web3.to_checksum_address(addr)
    if is_prep():
        return _send(token().functions.setAllowlisted(a, True), "tczk_allow")
    r = ref if isinstance(ref, bytes) and len(ref) == 32 else account_ref(ref)
    return _send(token().functions.allow(a, r), "tczk_allow")


def disallow(addr: str) -> dict:
    a = Web3.to_checksum_address(addr)
    if is_prep():
        return _send(token().functions.setAllowlisted(a, False), "tczk_disallow")
    return _send(token().functions.disallow(a), "tczk_disallow")


def freeze(addr: str, case_ref: str = "") -> dict | None:
    """Freeze at the bank (all assets) and, for tCZK, on chain. The reason never goes on chain (§6.7)."""
    db.run("INSERT OR REPLACE INTO frozen_wallets (address, case_ref, frozen_at) VALUES (?,?,?)",
           (Web3.to_checksum_address(addr), case_ref, time.time()))
    if is_prep():
        return None
    return _send(token().functions.freeze(Web3.to_checksum_address(addr)), "tczk_freeze")


def unfreeze(addr: str) -> dict | None:
    db.run("DELETE FROM frozen_wallets WHERE lower(address)=lower(?)", (addr,))
    if is_prep():
        return None
    return _send(token().functions.unfreeze(Web3.to_checksum_address(addr)), "tczk_unfreeze")


def mint(to: str, amount: int, ref: str) -> dict:
    """Deposit at the bank → tCZK. One mint per bank transaction ref (idempotent)."""
    if amount <= 0:
        raise ValueError("amount must be positive")
    a = Web3.to_checksum_address(to)
    call = token().functions.mint(a, amount) if is_prep() else token().functions.mint(a, amount, bytes32(ref))
    return _send(call, "tczk_mint", idem_key=f"tczk:mint:{ref}")


def burn(frm: str, amount: int, ref: str, gas: int | None = None, deal_id: str | None = None) -> dict:
    """gas: set it when the tokens arrived in the previous tx (a payout right after a capture): a lagging node's gas
    estimate would not see them yet and refuse a burn that will succeed in the next block."""
    if is_prep():
        raise RuntimeError("the prep token has no burn with a bank ref")
    call = token().functions.burn(Web3.to_checksum_address(frm), amount, bytes32(ref))
    return _send(call, "tczk_burn", idem_key=f"tczk:burn:{ref}", gas=gas, deal_id=deal_id)


# ---- deploy (fallback for script/Deploy.s.sol; same steps) ----

SETUP_GAS = 200_000  # explicit: a gas estimate from a lagging node that has not seen the new code yet is ~23k


def deploy(admin_pk: str | None = None, relayer: str | None = None, operator: str | None = None,
           store: str | None = None, mint_alfa: int = 100_000) -> str:
    """Deploy TCZK with the admin key and set it up like Deploy.s.sol. Returns the token address.
    store: the operator's escrow token store; read from the escrow when not given (needs Base Sepolia or a fork)."""
    sender = chain.sender(admin_pk or config.BANK_ADMIN_PK)
    data = artifact()["bytecode"] + encode(["address"], [sender.address]).hex()
    address = sender.send({"data": data}, kind="tczk_deploy")["receipt"]["contractAddress"]
    # reads lag writes on a load-balanced RPC (REFERENCE §8a): wait until the code is visible before configuring
    chain.read_retry(lambda: chain.w3().eth.get_code(address), ok=lambda c: len(c) > 0, tries=20, delay=1.0)
    setup(address, admin_pk, relayer, operator, store, mint_alfa)
    return address


def setup(address: str, admin_pk: str | None = None, relayer: str | None = None, operator: str | None = None,
          store: str | None = None, mint_alfa: int = 100_000) -> dict:
    """Roles, strict-mode routers, the §3.3 allowlist and the demo mint. Idempotent: every step is skipped when the
    chain already shows it, so an interrupted setup can simply be run again."""
    sender = chain.sender(admin_pk or config.BANK_ADMIN_PK)
    admin = sender.address
    relayer = relayer or admin
    if store is None:
        operator = operator or "0x0469ceC178789072bc61435835D0BB38214D4e53"
        esc = chain.w3().eth.contract(address=Web3.to_checksum_address(config.ESCROW), abi=ESCROW_ABI)
        store = esc.functions.getTokenStore(Web3.to_checksum_address(operator)).call()
    t = chain.w3().eth.contract(address=Web3.to_checksum_address(address), abi=artifact()["abi"])
    done = {"roles": 0, "allowed": 0, "minted": 0}

    def send(call, kind="tczk_setup"):
        sender.send(call, kind=kind, gas=SETUP_GAS)

    def grant(role, who):
        who = Web3.to_checksum_address(who)
        if not t.functions.hasRole(ROLES[role], who).call():
            send(t.functions.grantRole(ROLES[role], who))
            done["roles"] += 1

    def allow_(who, ref):
        who = Web3.to_checksum_address(who)
        if not t.functions.isAllowed(who).call():
            send(t.functions.allow(who, ref))
            done["allowed"] += 1

    for role in ("ISSUER_ROLE", "COMPLIANCE_ROLE", "ENFORCER_ROLE"):
        grant(role, admin)
    for router in (relayer, config.COLLECTOR, store, config.REFUND_COLLECTOR):  # strict mode, §3.4
        grant("ROUTER_ROLE", router)
    allow_(admin, account_ref("bank-treasury"))  # allowlist, §3.3
    companies = json.loads((config.ROOT / "fixtures" / "companies.json").read_text())["companies"]
    for c in companies:
        allow_(c["wallet_address"], account_ref(c["account_number"]))
    allow_(config.COLLECTOR, b"\0" * 32)
    allow_(store, b"\0" * 32)
    # the fee receiver of every capture (MODEL_V2 §10): off the allowlist a tCZK capture with a fee reverts
    allow_(config.BANK_CLEARING_ADDR, account_ref("bank-clearing"))
    if mint_alfa:
        alfa = Web3.to_checksum_address(next(c["wallet_address"] for c in companies if c["id"] == "alfa"))
        if t.functions.balanceOf(alfa).call() == 0:
            send(t.functions.mint(alfa, mint_alfa, bytes32("demo-deposit-alfa")), "tczk_mint")
            done["minted"] = mint_alfa
    return done


def build_artifact() -> None:
    """Refresh contracts/artifacts/TCZK.json: forge's out/ if it exists, else compile with py-solc-x."""
    out = CONTRACTS / "out" / "TCZK.sol" / "TCZK.json"
    if out.exists():
        d = json.loads(out.read_text())
        abi, bytecode = d["abi"], d["bytecode"]["object"]
    else:
        import solcx
        solcx.install_solc("0.8.31")
        res = solcx.compile_files(
            [str(CONTRACTS / "src" / "TCZK.sol")], output_values=["abi", "bin"], solc_version="0.8.31",
            import_remappings={"@openzeppelin/contracts/": str(CONTRACTS / "lib/openzeppelin-contracts/contracts/")},
            allow_paths=[str(CONTRACTS)], optimize=True, optimize_runs=200, evm_version="cancun")
        d = next(v for k, v in res.items() if k.endswith(":TCZK"))
        abi, bytecode = d["abi"], "0x" + d["bin"]
    note = "Built from src/TCZK.sol (solc 0.8.31, evm cancun, optimizer 200); refresh: python -m bank.tczk artifact"
    ARTIFACT.write_text(json.dumps({"_note": note, "abi": abi, "bytecode": bytecode}, indent=1))
    artifact.cache_clear()


def info(address: str | None = None) -> dict:
    infra = (("collector", config.COLLECTOR), ("token_store", config.OPERATOR_TOKEN_STORE))
    out = {"address": token(address).address, "name": config.TCZK_NAME,
           "allowed": {n: is_allowed(a, address) for n, a in infra}}
    if not is_prep():
        t = token(address)
        out["name"] = t.functions.name().call()
        out["strict"] = t.functions.strictMode().call()
        out["router"] = {n: t.functions.hasRole(ROLES["ROUTER_ROLE"], Web3.to_checksum_address(a)).call()
                         for n, a in infra}
    return out


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "info"
    if cmd == "deploy":
        addr = deploy()
        print(f'TCZK={addr}\nTCZK_NAME="tCZK Deposit Token"\n{chain.address_link(addr)}')
    elif cmd == "setup":  # resume an interrupted deploy: python -m bank.tczk setup 0x...
        print(json.dumps(setup(sys.argv[2]), indent=1))
    elif cmd == "artifact":
        build_artifact()
        print("wrote", ARTIFACT)
    else:
        print(json.dumps(info(), indent=1))
