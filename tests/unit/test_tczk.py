# tCZK on the in-memory chain: the Python fallback deploy (same steps as Deploy.s.sol), admin helpers,
# and the strict-mode rule a client cannot bypass. Money-moving helpers (mint/burn) are tested here.
import pytest
from eth_account import Account
from web3 import Web3

from bank import chain, config, tczk

pytestmark = pytest.mark.chain


@pytest.fixture
def token(local_chain, monkeypatch):
    w3, keys = local_chain
    admin = Account.from_key(keys[0]).address
    store = Account.from_key(keys[9]).address  # stands in for escrow.getTokenStore(operator)
    monkeypatch.setenv("BANK_ADMIN_PK", keys[0])
    config.reload()
    addr = tczk.deploy(store=store, mint_alfa=100_000)
    monkeypatch.setenv("TCZK", addr)
    monkeypatch.setenv("TCZK_NAME", "tCZK Deposit Token")
    config.reload()
    return {"w3": w3, "keys": keys, "admin": admin, "store": store, "address": addr}


ALFA = "0x491BeCCB4E26E1c5781E1E44c48Ad474D04d2140"
BETA = "0x70E8B0ed46DC4FD558ee05bCEba06e5AAf00EBd1"


def test_deploy_sets_up_roles_allowlist_and_mint(token):
    t = tczk.token()
    assert t.functions.name().call() == "tCZK Deposit Token" == config.ASSETS["tCZK"]["name"]
    assert t.functions.decimals().call() == 2
    assert t.functions.strictMode().call() is True
    for who in (ALFA, BETA, config.COLLECTOR, token["store"], token["admin"]):
        assert tczk.is_allowed(who), who
    for router in (token["admin"], config.COLLECTOR, token["store"], config.REFUND_COLLECTOR):
        assert t.functions.hasRole(tczk.ROLES["ROUTER_ROLE"], Web3.to_checksum_address(router)).call()
    assert t.functions.accountRefOf(ALFA).call() == tczk.account_ref("1803729684/9999")
    assert t.functions.balanceOf(ALFA).call() == 100_000
    assert tczk.info()["router"] == {"collector": True, "token_store": False}  # local chain: store differs


def test_account_ref_never_contains_the_number():
    ref = tczk.account_ref("1803729684/9999")
    assert len(ref) == 32 and b"1803729684" not in ref
    assert tczk.bytes32("dep-1") == b"dep-1".ljust(32, b"\0")
    assert len(tczk.bytes32("x" * 40)) == 32


def test_mint_is_idempotent_per_bank_ref(token):
    client = Account.from_key(token["keys"][1]).address
    tczk.allow(client, "1111111111/9999")
    first = tczk.mint(client, 2_500, "dep-42")
    again = tczk.mint(client, 2_500, "dep-42")  # the same bank transaction never mints twice
    assert again["tx_hash"] == first["tx_hash"] and again.get("reused")
    assert tczk.token().functions.balanceOf(client).call() == 2_500
    with pytest.raises(ValueError):
        tczk.mint(client, 0, "dep-43")


def test_mint_to_non_client_reverts(token):
    stranger = Account.from_key(token["keys"][2]).address
    with pytest.raises(chain.TxReverted):
        chain.sender(token["keys"][0]).send(
            tczk.token().functions.mint(stranger, 100, tczk.bytes32("dep-x")), kind="tczk_mint", gas=300_000)
    assert tczk.token().functions.balanceOf(stranger).call() == 0


def test_burn_of_delisted_client(token):
    tczk.disallow(ALFA)
    assert not tczk.is_allowed(ALFA)
    tczk.burn(ALFA, 100_000, "wd-1")
    assert tczk.token().functions.balanceOf(ALFA).call() == 0


def test_freeze_is_on_chain_and_at_the_bank(token):
    tczk.freeze(BETA, case_ref="case-1")
    assert tczk.is_frozen(BETA) and tczk.bank_frozen(BETA.lower())
    tczk.unfreeze(BETA)
    assert not tczk.is_frozen(BETA) and not tczk.bank_frozen(BETA)


def test_strict_mode_direct_transfer_reverts(token):
    """N10 in miniature: a client calling transfer() directly gets a real reverted tx; no money moves."""
    client_key = token["keys"][1]
    client = Account.from_key(client_key).address
    tczk.allow(client, "2222222222/9999")
    tczk.mint(client, 1_000, "dep-50")
    res = chain.sender(client_key).send(tczk.token().functions.transfer(BETA, 100), kind="direct_transfer",
                                        expect_revert=True)
    assert res["status"] == "reverted"
    assert tczk.token().functions.balanceOf(client).call() == 1_000
    assert tczk.token().functions.balanceOf(BETA).call() == 0


def test_artifact_matches_forge_build():
    out = tczk.CONTRACTS / "out" / "TCZK.sol" / "TCZK.json"
    if not out.exists():
        pytest.skip("no forge build output (run forge build in contracts/)")
    import json
    built = json.loads(out.read_text())
    assert built["bytecode"]["object"] == tczk.artifact()["bytecode"], "stale artifact: python -m bank.tczk artifact"
