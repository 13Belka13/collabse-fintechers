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


@pytest.fixture
def clock(monkeypatch):
    """Fake clock: time.time() returns clock.now; clock.tick(s) moves it. For deadlines, expiries, windows."""
    import time

    class Clock:
        now = 1_790_000_000.0  # 2026-09-21, fixed

        def tick(self, seconds: float):
            self.now += seconds

    c = Clock()
    monkeypatch.setattr(time, "time", lambda: c.now)
    return c


def pytest_runtest_setup(item):
    if "live" in item.keywords and os.getenv("LIVE") != "1":
        pytest.skip("live test: set LIVE=1 with testnet credentials")
    # CI sets FORK=0: tests that fork Base Sepolia over the public RPC are skipped there (flaky, network)
    if os.getenv("FORK") == "0" and ("fork" in item.keywords or item.path.name.endswith("_fork.py")):
        pytest.skip("anvil fork of Base Sepolia: skipped with FORK=0 (CI)")
