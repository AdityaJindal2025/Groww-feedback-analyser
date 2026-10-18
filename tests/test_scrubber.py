"""Scrubber patterns (T2.1). All personal data below is synthetic."""

from __future__ import annotations

import pytest

from pulse.privacy.scrubber import has_redaction, scan, scrub

POSITIVE = [
    ("URL", "see https://example.com/ticket/123 for details", "see [URL] for details"),
    ("URL", "go to www.example.in now", "go to [URL] now"),
    ("EMAIL", "mail me at test.user+groww@example.co.in please", "mail me at [EMAIL] please"),
    ("UPI", "paid to someone@okaxis yesterday", "paid to [UPI] yesterday"),
    ("UPI", "sent from 9876543210@ybl", "sent from [UPI]"),
    ("PAN", "my PAN ABCDE1234F is verified", "my PAN [PAN] is verified"),
    ("PAN", "pan abcde1234f", "pan [PAN]"),
    ("AADHAAR", "aadhaar 2345 6789 0123 linked", "aadhaar [AADHAAR] linked"),
    ("AADHAAR", "aadhaar 2345-6789-0123", "aadhaar [AADHAAR]"),
    ("AADHAAR", "aadhaar 234567890123.", "aadhaar [AADHAAR]."),
    ("PHONE", "call me on 9876543210", "call me on [PHONE]"),
    ("PHONE", "call +91 98765 43210 now", "call [PHONE] now"),
    ("PHONE", "call +919876543210", "call [PHONE]"),
    ("PHONE", "call 91-98765-43210", "call [PHONE]"),
    ("PHONE", "whatsapp 7012345678.", "whatsapp [PHONE]."),
    ("ID", "demat account 1208160012345678 blocked", "demat account [ID] blocked"),
    ("ID", "order no 123456789 failed", "order no [ID] failed"),
    ("ID", "ticket ORD12345678 still open", "ticket [ID] still open"),
    ("ID", "ref id 5f3a9c2b7d stuck", "ref id [ID] stuck"),
    ("ID", "my ticket id 28036628 and i am struggling", "my ticket id [ID] and i am struggling"),
    ("ID", "Complaint no: 12345678 still open", "Complaint no: [ID] still open"),
    ("ID", "ticket number 4455667 closed", "ticket number [ID] closed"),
    ("ID", "order #556677 failed", "order #[ID] failed"),
    ("ID", "Ticket ID-90817", "Ticket ID-[ID]"),
]

NEAR_MISSES = [
    "5 star",
    "Nifty 50",
    "₹1,00,000",
    "2026",
    "v12.4.1",
    "10x",
    "24/7",
    "T+1",
    "Rs 500 brokerage per order",
    "market opens at 9:15 am",
    "52-week high",
    "1.5% charges",
    "withdrawal on 12/10/2026",
    "lost ₹25000 in F&O",
    "IPO allotment for 3 lots",
    "app version 12.3.0 crashes",
    "contact @Groww on twitter",
    "Q2FY27 results",
    "SIP of 1000 every month",
    "placed an order of 50000 rupees",
    "invested 250000 in mutual funds",
    "my account is blocked since 12 days",
    "ticket raised 3 times",
]


@pytest.mark.parametrize(("kind", "text", "expected"), POSITIVE)
def test_patterns_redact(kind: str, text: str, expected: str) -> None:
    result = scrub(text)
    assert result.text == expected
    assert result.counts == {kind: 1}
    assert scan(result.text) == []


@pytest.mark.parametrize("text", NEAR_MISSES)
def test_near_misses_are_not_redacted(text: str) -> None:
    result = scrub(text)
    assert result.text == text
    assert result.counts == {}
    assert scan(text) == []


def test_multiple_kinds_counted() -> None:
    text = "Call 9876543210 or mail a.b@example.com, PAN ABCDE1234F, again 9123456789"
    result = scrub(text)
    assert result.counts == {"PHONE": 2, "EMAIL": 1, "PAN": 1}
    assert result.text == "Call [PHONE] or mail [EMAIL], PAN [PAN], again [PHONE]"
    assert result.redacted


def test_email_is_not_split_into_upi() -> None:
    assert scrub("x.y@gmail.com").counts == {"EMAIL": 1}


def test_scrub_is_idempotent() -> None:
    once = scrub("call 9876543210 order ORD12345678").text
    assert scrub(once).text == once


def test_scan_reports_positions_not_text() -> None:
    text = "reach me at 9876543210"
    (hit,) = scan(text)
    assert hit.kind == "PHONE"
    assert text[hit.start : hit.end] == "9876543210"
    assert not hasattr(hit, "text")


def test_has_redaction() -> None:
    assert has_redaction("call [PHONE] now")
    assert has_redaction("[ID]")
    assert not has_redaction("call me [maybe]")
    assert not has_redaction("plain text")


def test_text_is_otherwise_untouched() -> None:
    text = "  Worst app!!! Withdrawal pending since 3 days 😡 bahut slow hai  "
    assert scrub(text).text == text
