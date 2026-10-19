# Live Bedrock eval run (SPEC §19.3 "live Bedrock once after the Toolbox"): the same harness with MOCK=0.
# Skipped unless LIVE=1 and AWS credentials are present. Writes reports/evals-live.json for scripts/report.py.
#   LIVE=1 MOCK=0 pytest tests/evals/test_live_bedrock.py -s
import json
import os

import pytest

from bank import config, timing
from tests.evals import harness

pytestmark = [pytest.mark.live,
              pytest.mark.skipif(not os.getenv("AWS_REGION") or not (os.getenv("AWS_ACCESS_KEY_ID")
                                                                      or os.getenv("AWS_PROFILE")),
                                 reason="TODO(credential): AWS_REGION + AWS credentials for Bedrock")]


def test_live_evals(monkeypatch):
    monkeypatch.setenv("MOCK", "0")
    config.reload()
    out = harness.run_all()
    out["llm_totals"] = timing.llm_totals()
    (config.ROOT / "reports" / "evals-live.json").write_text(json.dumps(out, indent=1, default=str), encoding="utf-8")
    inj = out["injections"]
    assert inj["money_blocked"] == inj["variants"]  # 100 % at the money level, whatever the live model did
    assert out["explanations"]["client_clean"] == out["explanations"]["client_texts"]
