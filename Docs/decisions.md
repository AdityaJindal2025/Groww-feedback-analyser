# Decisions

Answers to the open questions in [problemStatement.md §10](../problemStatement.md#10-open-questions--assumptions), recorded for T0.5. "Default" means the architecture's choice ([architecture.md §2](../architecture.md#2-decisions-on-open-questions)) stands until the project owner says otherwise.

| # | Question | Answer | Status |
|---|---|---|---|
| 1 | Include the App Store? | **No.** Google Play only. | Decided (arch §2, 17 Oct 2026) |
| 2 | Which public review source? | `google-play-scraper`: the public, logged-out Play listing, rate-limited (1.5 s between pages, 30,000-review cap). The CSV/JSON import adapter is the fallback for a manual export. | Default. **Owner to confirm** this meets the course's "public review exports" rule (plan §2 item 1) |
| 3 | Weekly note vs. 8–12 week window | Rank over the full 12-week window; trend marker for the latest week; prefer latest-week quotes. | Default per arch §2 |
| 4 | Email recipient | `PULSE_RECIPIENT` environment variable (self or alias), never committed. | Default. **Owner to set** the address before P7 (plan §2 item 4) |
| 5 | One Doc per week or one running Doc? | **One running Doc**, with a new section appended each week; re-runs append nothing. | Changed 18 Oct 2026: our `gmail-docs-mcp` server can only append to existing Docs (no create, no replace) |
| 6 | Language | **English only.** Non-Latin scripts and Hinglish are dropped at ingest, together with reviews of 8 words or fewer and any review with an emoji (`sources.filters`). | Decided by owner, 17 Oct 2026 |

## Other decisions made during Phases 0–2

| Decision | Why |
|---|---|
| MCP server: `workspace-mcp` (see [spikes.md](spikes.md#t04-mcp-server-selection)) | Docs and Gmail in one server, with a full-content replace for idempotent re-runs, and drafts |
| Keep only reviews with more than 8 words, no emoji, English (Hinglish detected with a Hindi-word marker list rather than a dictionary check) | Owner's request. On the 12 weeks to W41 this keeps about 1,530 of 12,853 reviews (dropped: about 11,400 short, about 190 emoji, about 250 non-English). The marker list caught Hinglish accurately in a manual check; a dictionary check let Hinglish through and needed a macOS-only word file |
| Ingest re-reads the full window each run (no incremental fetch) | About 13.5k reviews take about 2 minutes to fetch |
| Edited reviews (same ID, new text or rating) are updated and lose their classifications | They are classified again with the new text |
| Play `at` timestamps are read as local machine time | That's how `google-play-scraper` builds them |
| Possible 24-hour publication delay on the listing | Re-check before fixing the schedule in T10.1; may move the run to Tuesday |
| Taxonomy v1 approved as proposed (owner, 17 Oct 2026): Ease of Use Praise, Features & UI Changes, Support, Funds & Accounts, App Reliability & Execution, Brokerage & Charges | Data-driven, 5.8% "other" over the full window. Accepted trade-offs: a praise theme can rank in the top 3 (it did in W41, so its quote is praise and the three actions go to the other two themes), and support, withdrawals and KYC share one theme. Revisit with `pulse themes discover` if actions feel too broad |
| Redact 5–8 digit numbers after an identifier word ("ticket id …", "complaint no: …", "order #…") | The first live pulse quoted an 8-digit ticket number, which the 9+ digit rule missed. Bare 5–8 digit numbers (often amounts) are still kept. 8 stored reviews were re-scrubbed and re-classified |
| `stats.json` shares rounded to 4 decimals | Long decimals tripped the ID pattern in privacy scans of run files |
| Delivery through our own `gmail-docs-mcp` server on Railway (Streamable HTTP + bearer token) instead of `workspace-mcp` | The owner built and deployed it; it keeps Google credentials off the Mac. Needs `ENABLE_DRAFTS=true` for Gmail drafts. The pipeline never calls its `gmail_send_email` tool |
