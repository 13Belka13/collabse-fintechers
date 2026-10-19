# One tiny call per Claude model on Bedrock to see which ones this AWS account can use (SPEC §7: Opus 5.5 /
# Haiku 5.5 are "See Access"; fallbacks Opus 4.8 / Haiku 4.5). Each call is logged with tokens and cost.
# Usage: python -m scripts.check_bedrock      (needs AWS_REGION + AWS_PROFILE or AWS_BEARER_TOKEN_BEDROCK in .env)
import os

os.environ["MOCK"] = "0"

from bank import config, llm  # noqa: E402

CANDIDATES = {"main": ["anthropic.claude-opus-5-5", "anthropic.claude-opus-4-8"],
              "fast": ["anthropic.claude-haiku-5-5", "anthropic.claude-haiku-4-5"]}


def main():
    config.reload()
    for role, models in CANDIDATES.items():
        for model in models:
            setattr(config, "MODEL_MAIN" if role == "main" else "MODEL_FAST", model)
            try:
                r = llm.create(role, "Reply with one word.", [{"role": "user", "content": "ping"}],
                               stage="check_bedrock", effort="low", max_tokens=16)
                print(f"OK    {model:32} -> {llm.text_of(r)[:20]!r}")
            except Exception as e:  # noqa: BLE001 — report and try the next model
                print(f"NO    {model:32} -> {type(e).__name__}: {str(e)[:120]}")
    print("Put the first OK model per role into MODEL_MAIN / MODEL_FAST in .env.")


if __name__ == "__main__":
    main()
