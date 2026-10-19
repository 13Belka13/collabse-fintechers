# Shared fixtures: every test gets its own temp SQLite, MOCK=1, no faults; optional in-memory chain.
import os

os.environ["COLLABSE_NO_DOTENV"] = "1"  # before bank.config is imported: tests never see the real .env

import pytest  # noqa: E402

from bank import chain, config, db  # noqa: E402


@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "bank.db"))
    monkeypatch.setenv("MOCK", "1")
    monkeypatch.setenv("FAULTS", "")
    db.close()
    config.reload()
    yield tmp_path / "bank.db"
    db.close()
    config.reload()


@pytest.fixture
def seeded():
    from scripts.seed import seed
    return seed()


@pytest.fixture
def local_chain():
    """In-memory eth-tester chain; returns (w3, [funded private keys as hex])."""
    from web3 import EthereumTesterProvider, Web3
    provider = EthereumTesterProvider()
    w3 = Web3(provider)
    keys = [k.to_hex() for k in provider.ethereum_tester.backend.account_keys]
    chain.set_w3(w3)
    yield w3, keys
    chain.set_w3(None)


def pytest_runtest_setup(item):
    if "live" in item.keywords and os.getenv("LIVE") != "1":
        pytest.skip("live test: set LIVE=1 with testnet credentials")
