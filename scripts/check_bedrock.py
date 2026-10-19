# One tiny call per Claude model on Bedrock to see which ones this AWS account can use. The hackathon account
# (AWS Participant Guide) has Claude 4.5 only, through EU inference profiles in eu-central-1; Claude 5 returns
# "not available for this account". Each call is logged with tokens and cost.
# Usage: python -m scripts.check_bedrock   (needs AWS_REGION + portal keys, AWS_PROFILE or AWS_BEARER_TOKEN_BEDROCK)
import os

os.environ["MOCK"] = "0"

from bank import config, llm  # noqa: E402

CANDIDATES = {"main": ["eu.anthropic.claude-opus-4-5-20251101-v1:0", "eu.anthropic.claude-sonnet-4-5-20250929-v1:0"],
              "fast": ["eu.anthropic.claude-haiku-4-5-20251001-v1:0"]}


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
