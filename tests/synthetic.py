"""Synthetic reviews for offline tests (no real people, no text from pulse.db).

Shaped like the real filtered data: English, 9–40 words, often informal, skewed towards 1★ and
5★. Theme keywords match tests/fixtures/llm/classify.json so the fake LLM labels them.
"""

from __future__ import annotations

import hashlib
from datetime import timedelta

from pulse.models import Review
from pulse.timeutil import window_for

WEEK = "2026-W41"
START, END = window_for(WEEK, 12)

# (theme, rating, text). Counts per theme decide the ranking: withdrawals > app > charges.
TEMPLATES: list[tuple[str, int, str]] = [
    (
        "withdrawals_payments",
        1,
        "withdrawal pending since 4 days and no update in the app. money stuck",
    ),
    (
        "withdrawals_payments",
        1,
        "I tried to withdraw my funds three times and every request failed",
    ),
    (
        "withdrawals_payments",
        2,
        "UPI add money keeps failing, the amount was debited but not credited",
    ),
    ("withdrawals_payments", 1, "refund for the failed order still not received after a full week"),
    (
        "app_performance",
        1,
        "app hangs every morning right at market open and I cannot exit my trade",
    ),
    ("app_performance", 2, "the chart is so slow and the login screen crash happens daily now"),
    ("app_performance", 1, "my order got stuck in pending during the volatile session, very bad"),
    ("charges_brokerage", 3, "brokrage is too high for small investors like me, please reduce it"),
    ("charges_brokerage", 1, "hidden charges on every trade and the tax breakdown is confusing"),
    ("customer_support", 1, "customer care never replies to my ticket, raised it two weeks ago"),
    ("kyc_onboarding", 2, "kyc verification rejected twice for no reason, cannot start investing"),
    ("other", 5, "very nice and simple to use for beginners, I really like this one"),
]


def reviews(per_template: int = 10) -> list[Review]:
    """`per_template` copies of each template, spread over the 12-week window."""
    out: list[Review] = []
    span = (END - START).total_seconds()
    n = 0
    for t_index, (_, rating, text) in enumerate(TEMPLATES):
        for copy in range(per_template):
            n += 1
            when = START + timedelta(seconds=span * ((n * 7919) % 1000) / 1000)
            body = f"{text} ({copy + 1})" if copy else text
            out.append(
                Review(
                    review_id=hashlib.sha256(f"{t_index}-{copy}".encode()).hexdigest(),
                    store="play_store",
                    rating=rating,
                    text=body,
                    review_date=when,
                    app_version="12.3.0",
                    thumbs_up=copy,
                    script="latin",
                    word_count=len(body.split()),
                    has_redaction=False,
                    ingested_at=END,
                )
            )
    return out
