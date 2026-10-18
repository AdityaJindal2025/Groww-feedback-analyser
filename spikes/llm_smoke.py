"""T3.7 spike: live smoke test and token baseline for classification. Throwaway.

Needs Claude credentials (ANTHROPIC_API_KEY in .env, or an `ant auth login` profile).
Prints aggregates only, never review text, as Markdown for Docs/spikes.md.

    python spikes/llm_smoke.py [--week 2026-W41] [--chunks 2]

1. Counts input tokens for every review in the window, formatted as classify user turns
   (`messages.count_tokens`, free), replacing the 4-characters-per-token estimate.
2. Classifies the first N chunks of 50 real reviews with the provisional classify prompt and the
   synthetic placeholder taxonomy from tests/fixtures/llm/discover.json (the real taxonomy
   comes from T4.2). Measures latency, output tokens per review, cache hits and label coverage.
3. Projects the cost of a first full run and of a weekly run.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

import anthropic

from pulse import env
from pulse.config import load_settings
from pulse.llm.client import PRICES, ClaudeClient, Usage, has_credentials
from pulse.llm.outputs import LabelBatch
from pulse.llm.prompts import format_reviews, load_prompt
from pulse.store import db
from pulse.timeutil import window_for

PLACEHOLDER_THEMES = Path("tests/fixtures/llm/discover.json")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--week", default="2026-W41")
    parser.add_argument("--chunks", type=int, default=2)
    args = parser.parse_args()

    env.load_env()
    if not has_credentials():
        sys.exit("No Claude credentials: set ANTHROPIC_API_KEY in .env or run `ant auth login`.")
    settings = load_settings()
    cfg = settings.llm.classify
    size = cfg.chunk_size

    conn = db.connect(settings.paths.db)
    start, end = window_for(args.week, settings.window.weeks)
    reviews = db.reviews_in_window(conn, start, end)
    chunks = [reviews[i : i + size] for i in range(0, len(reviews), size)]

    themes = json.loads(PLACEHOLDER_THEMES.read_text())["response"]["themes"]
    prompt = load_prompt("classify", settings.paths.prompts)
    system = prompt.render(themes=themes)

    sdk = anthropic.Anthropic(max_retries=3)

    # 1. Token counts (free endpoint)
    def count(user: str) -> int:
        result = sdk.messages.count_tokens(
            model=cfg.model, system=system, messages=[{"role": "user", "content": user}]
        )
        return int(result.input_tokens)

    system_tokens = count("x") - 1
    chunk_tokens = [count(format_reviews(chunk)[0]) - system_tokens for chunk in chunks]
    review_tokens = sum(chunk_tokens)

    # 2. Live classification of the first N chunks
    client = ClaudeClient(settings.llm, client=sdk)
    rows: list[dict[str, Any]] = []
    total = Usage(model=cfg.model)
    themes_seen: Counter[str] = Counter()
    for n, chunk in enumerate(chunks[: args.chunks], start=1):
        user, ids = format_reviews(chunk)
        started = time.monotonic()
        result = client.call_structured("classify", system, user, LabelBatch)
        elapsed = time.monotonic() - started
        labels = result.output.items
        returned = [item.rid for item in labels]
        valid_themes = {t["id"] for t in themes} | {"other"}
        rows.append(
            {
                "chunk": n,
                "reviews": len(chunk),
                "seconds": elapsed,
                "usage": result.usage,
                "missing": len(set(ids) - set(returned)),
                "unknown": len(set(returned) - set(ids)),
                "bad_theme": sum(1 for item in labels if item.theme_id not in valid_themes),
            }
        )
        themes_seen.update(item.theme_id for item in labels)
        total = total + result.usage

    # 3. Report
    price = PRICES[cfg.model]
    labelled = sum(int(r["reviews"]) for r in rows)
    out_per_review = total.output_tokens / max(labelled, 1)
    in_per_review = review_tokens / max(len(reviews), 1)

    def run_cost(n_reviews: int) -> float:
        n_chunks = -(-n_reviews // size)
        input_cost = (
            n_reviews * in_per_review * price.input
            + system_tokens * price.cache_write
            + system_tokens * max(n_chunks - 1, 0) * price.cache_read
        )
        output_cost = n_reviews * out_per_review * price.output
        return (input_cost + output_cost) / 1_000_000

    weekly = round(len(reviews) / settings.window.weeks)
    print(f"## T3.7 LLM smoke test — {time.strftime('%Y-%m-%d %H:%M')}\n")
    print(
        f"- Model `{cfg.model}`, effort `{cfg.effort}`, chunk size {size}, "
        f"window {args.week} ({len(reviews)} reviews, {len(chunks)} chunks)"
    )
    print(
        f"- Classify system prompt (placeholder taxonomy): **{system_tokens} tokens** "
        f"(Opus 5.5 caches from 512)"
    )
    print(
        f"- Review input: **{review_tokens:,} tokens** total, "
        f"**{in_per_review:.1f} per review** (estimate was about 50)"
    )
    print(
        "\n| Chunk | Reviews | Seconds | Input | Output | Cache write | Cache read "
        "| Missing | Unknown | Bad theme | Cost |"
    )
    print("|---|---|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        u: Usage = r["usage"]
        print(
            f"| {r['chunk']} | {r['reviews']} | {r['seconds']:.1f} | {u.input_tokens} | "
            f"{u.output_tokens} | {u.cache_creation_input_tokens} | "
            f"{u.cache_read_input_tokens} | {r['missing']} | {r['unknown']} | "
            f"{r['bad_theme']} | ${u.cost_usd:.4f} |"
        )
    print(f"\n- Output tokens per review (incl. thinking): **{out_per_review:.1f}**")
    print(
        f"- Cache hit on a later call: "
        f"**{'yes' if any(r['usage'].cache_read_input_tokens for r in rows[1:]) else 'NO'}**"
    )
    print(f"- Fallback used: {'yes' if total.fallback else 'no'}")
    print(
        "- Label mix (placeholder taxonomy): "
        + ", ".join(f"{k} {v}" for k, v in themes_seen.most_common())
    )
    print(
        f"- Projected cost: first full run ({len(reviews)} reviews) "
        f"**${run_cost(len(reviews)):.2f}**; weekly run (~{weekly} reviews) "
        f"**${run_cost(weekly):.2f}**"
    )


if __name__ == "__main__":
    main()
