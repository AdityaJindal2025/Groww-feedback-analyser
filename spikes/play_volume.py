"""T0.3 spike: Groww Play Store review volume and data shape. Throwaway, not part of the package.

Fetches the newest `com.nextbillion.groww` reviews (lang=en, country=in) back to 12 weeks before
the last completed week, or until the cap. Raw responses stay in memory and are never written
to disk (they contain usernames). Prints aggregates only, as Markdown for Docs/spikes.md.

    python spikes/play_volume.py [--cap 20000] [--weeks 12] [--delay 1.0]
"""

from __future__ import annotations

import argparse
import statistics
import time
from collections import Counter
from datetime import UTC, datetime

from google_play_scraper import Sort, reviews

from pulse.ingest.normalize import detect_script, word_count
from pulse.timeutil import IST, last_completed_week, week_of, window_for

PACKAGE = "com.nextbillion.groww"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cap", type=int, default=20000)
    parser.add_argument("--weeks", type=int, default=12)
    parser.add_argument("--delay", type=float, default=1.0)
    parser.add_argument("--page-size", type=int, default=200)
    args = parser.parse_args()

    now = datetime.now(UTC)
    week = last_completed_week(now)
    since, until = window_for(week, args.weeks)

    rows: list[dict] = []  # in memory only
    token = None
    pages = 0
    empty_retries = 0
    started = time.monotonic()
    reached_since = False
    while len(rows) < args.cap:
        page, next_token = reviews(
            PACKAGE,
            lang="en",
            country="in",
            sort=Sort.NEWEST,
            count=args.page_size,
            continuation_token=token,
        )
        if not page:
            empty_retries += 1
            if empty_retries > 3:
                break
            time.sleep(2**empty_retries)
            continue
        pages += 1
        rows.extend(page)
        oldest = min(item["at"] for item in page)
        if oldest.astimezone(UTC) < since:
            reached_since = True
            break
        if next_token.token is None:
            break
        token = next_token
        time.sleep(args.delay)
    elapsed = time.monotonic() - started

    # --- timezone behaviour of `at` ---
    sample_at = rows[0]["at"]
    newest = max(item["at"] for item in rows)
    newest_utc = newest.astimezone(UTC)
    in_window = [r for r in rows if since <= r["at"].astimezone(UTC) <= until]
    after_window = [r for r in rows if r["at"].astimezone(UTC) > until]

    print(f"## T0.3 Play Store volume spike — run {now.astimezone(IST):%Y-%m-%d %H:%M} IST\n")
    print(f"- Package `{PACKAGE}`, lang=en, country=in, sort=NEWEST, page size {args.page_size}")
    print(f"- Window: {args.weeks} weeks ending {week}: {since:%Y-%m-%d} – {until:%Y-%m-%d} IST")
    print(
        f"- Fetched {len(rows)} reviews in {pages} pages, {elapsed:.0f} s "
        f"({elapsed / max(pages, 1):.1f} s/page incl. {args.delay}s delay); "
        f"empty-page retries: {empty_retries}"
    )
    print(f"- Reached window start: {'yes' if reached_since else 'NO (cap or end of pages)'}")
    print(f"- Oldest fetched: {min(r['at'] for r in rows).astimezone(IST):%Y-%m-%d %H:%M} IST")
    print(f"- In window: {len(in_window)}; newer than window (current week): {len(after_window)}")
    print("\n### `at` field")
    print(f"- type `{type(sample_at).__name__}`, tzinfo `{sample_at.tzinfo}`")
    print(
        f"- newest `at` read as local time: {newest_utc:%Y-%m-%d %H:%M} UTC; "
        f"now: {now:%Y-%m-%d %H:%M} UTC; lag {(now - newest_utc).total_seconds() / 60:.0f} min"
    )
    print(f"- machine local UTC offset: {datetime.now().astimezone().strftime('%z')}")

    print("\n### Reviews per ISO week (in window)")
    per_week = Counter(week_of(r["at"].astimezone(UTC)) for r in in_window)
    print("| Week | Reviews | Per day |")
    print("|---|---|---|")
    for wk in sorted(per_week):
        print(f"| {wk} | {per_week[wk]} | {per_week[wk] / 7:.0f} |")
    if after_window:
        print(f"| current (partial) | {len(after_window)} | |")
    full = [per_week[w] for w in per_week]
    if full:
        print(f"\nMean per week: {statistics.mean(full):.0f}; max {max(full)}; min {min(full)}")

    texts = [r.get("content") or "" for r in in_window or rows]
    wc = [word_count(t) for t in texts]
    print("\n### Shape (in-window reviews)")
    under4 = sum(1 for n in wc if n < 4)
    print(f"- Under 4 words: {under4} ({under4 / len(wc):.0%})")
    print(f"- 8–60 words (quote-length): {sum(1 for n in wc if 8 <= n <= 60) / len(wc):.0%}")
    print(
        f"- Word count median {statistics.median(wc):.0f}, p90 "
        f"{statistics.quantiles(wc, n=10)[-1]:.0f}, max {max(wc)}"
    )
    scripts = Counter(detect_script(t) for t in texts)
    print(
        "- Script mix: " + ", ".join(f"{k} {v / len(texts):.0%}" for k, v in scripts.most_common())
    )
    ratings = Counter(r["score"] for r in in_window or rows)
    total = sum(ratings.values())
    print("- Ratings: " + ", ".join(f"{s}★ {ratings[s] / total:.0%}" for s in range(5, 0, -1)))
    empty = sum(1 for t in texts if not t.strip())
    print(
        f"- Empty text: {empty}; missing reviewCreatedVersion: "
        f"{sum(1 for r in in_window if not r.get('reviewCreatedVersion'))}"
    )
    print(f"- Fields returned: {sorted(rows[0].keys())}")


if __name__ == "__main__":
    main()
