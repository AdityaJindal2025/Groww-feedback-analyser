# Spike Findings

Phase 0 spikes from [implementationPlan.md](../implementationPlan.md) §3. Aggregates only: no review text, usernames or other identifiers are recorded here.

## T0.3 Play Store volume and data shape

Run on 17 Oct 2026, 12:34 IST, with `python spikes/play_volume.py --cap 20000 --delay 1.0`.

- Package `com.nextbillion.groww`, `lang=en`, `country=in`, sort NEWEST, 200 reviews per page.
- Window: 12 weeks ending `2026-W41`, 20 Jul to 11 Oct 2026 IST.
- Fetched 13,600 reviews in 68 pages in 100 s (about 1.5 s per page including a 1 s delay). No empty or failed pages. Reached the window start, well under the 20,000 cap.
- 12,853 reviews fall inside the window. Another 628 are from the current, incomplete week.

### Reviews per ISO week

| Week | Reviews | Per day |
|---|---|---|
| 2026-W30 | 866 | 124 |
| 2026-W31 | 1,117 | 160 |
| 2026-W32 | 1,223 | 175 |
| 2026-W33 | 1,113 | 159 |
| 2026-W34 | 1,084 | 155 |
| 2026-W35 | 1,358 | 194 |
| 2026-W36 | 1,197 | 171 |
| 2026-W37 | 1,060 | 151 |
| 2026-W38 | 1,000 | 143 |
| 2026-W39 | 1,056 | 151 |
| 2026-W40 | 870 | 124 |
| 2026-W41 | 909 | 130 |

Mean 1,071 per week (min 866, max 1,358).

### Shape of in-window reviews

| Measure | Value |
|---|---|
| Under 4 words (not sent to Claude) | 70% (9,058) |
| 8–60 words (quote length) | 15% |
| Word count | median 2, p90 15, max 129 |
| Script | Latin 97%, other 3%, Devanagari 1% |
| Ratings | 5★ 77%, 4★ 8%, 3★ 3%, 2★ 2%, 1★ 10% |
| Empty text | 0 |
| Missing `reviewCreatedVersion` | 1,002 (`appVersion` is the same field, so it's missing too) |

Fields returned by the library: `appVersion, at, content, repliedAt, replyContent, reviewCreatedVersion, reviewId, score, thumbsUpCount, userImage, userName`. The adapter reads only the allowlisted ones.

### `at` field and timing

- `at` is a **naive** `datetime`, built by the library with `datetime.fromtimestamp()`, so it is in the machine's local time zone (IST on this Mac). The adapter converts it to UTC by reading it as local time (`at.astimezone(UTC)`).
- The newest review was exactly 24 hours (1,440 minutes) older than the time of the fetch. This looks like a publication delay on the public listing. **Consequence:** a Monday 09:00 IST run would not yet see reviews posted on Sunday after about 09:00, so the latest week would be slightly undercounted. Options: run on Tuesday morning, or accept it. Check again on a later run before changing the schedule (T10.1).

### Library behaviour that shaped the adapter

- `google_play_scraper.reviews()` catches every exception and returns an empty page with no continuation token, so a network error looks like the end of the data. The adapter retries empty or failed pages 3 times with backoff (2 s, 4 s). If results still end before the window start, it flags `partial_ingest`.

### After the review filters (17 Oct 2026)

With `sources.filters` on (more than 8 words, no emoji, English only), 1,534 reviews remain in the window: about 130 a week (72–202). Every stored review is now classified, so the first run classifies about 1,530 (Batches) and weekly runs about 130 new ones, which is below `batch_threshold: 200`, so weekly runs use synchronous calls (about 3 requests).

### Recommended settings (before the filters)

| Setting | Value | Why |
|---|---|---|
| `sources.play_store.max_reviews` | 30,000 | About 2.2× the 12-week volume |
| `llm.classify.chunk_size` | 50 (unchanged) | About 3,800 classifiable reviews in the first run → about 77 requests |
| `llm.classify.batch_threshold` | 200 (unchanged) | The first run (about 3,800) and weekly runs (about 320 new) both use Batches |
| `max_classify_per_week` | Not needed | About 320 classifiable reviews per week |
| Incremental fetching | Not needed | Re-reading the whole 12-week window takes about 2 minutes |

### M1 check (live `pulse ingest`, 17 Oct 2026)

A full `pulse ingest --week 2026-W41`, run with a throwaway salt into a scratch database outside the repo, stored 13,481 reviews: 12,853 in the window, with the **same weekly counts as the spike** (within ±5%). 14 redactions: ID 9, PHONE 2, EMAIL 1, UPI 1. The privacy scan of every stored text was clean. A few `[ID]` hits look like over-redaction of numbers in ordinary text, which the plan accepts: those reviews only drop out of quote candidates.

## T0.4 MCP server selection

> **Superseded (18 Oct 2026).** Delivery now uses our own `gmail-docs-mcp` server, deployed at `https://mcp-server-production-64cb.up.railway.app/mcp` (Streamable HTTP, bearer token; `/healthz` OK, `/mcp` returns 401 without the token). Its tools: `gdocs_get_document_info`, `gdocs_append_text`, `gdocs_append_table`, `gmail_create_draft` (only with `ENABLE_DRAFTS=true`) and `gmail_send_email` (never used). Docs are append-only, so the pulse is one running Doc with a section per week. See implementationPlan.md Phase 7; the live probe is T7.0. The `workspace-mcp` notes below are kept for reference only.

**Recommended: [`workspace-mcp`](https://github.com/taylorwilsdon/google_workspace_mcp)**, a Python server that covers both Docs and Gmail.

- Launch: `uvx workspace-mcp --tools docs gmail` (stdio), already set in [config/mcp_servers.json](../config/mcp_servers.json). Streamable HTTP is also supported.
- OAuth: handled by the server, using `GOOGLE_OAUTH_CLIENT_ID` / `GOOGLE_OAUTH_CLIENT_SECRET` from an OAuth client in your own Google Cloud project. Consent runs in a browser on first tool use, and tokens are stored locally by the server. No Google credentials go into this codebase.
- Probe: `python spikes/mcp_probe.py list --server google-workspace` listed 35 tools on 17 Oct 2026 **without Google sign-in** (full schemas are saved to `spikes/output/`).

### Tool mapping

| Logical operation | Tool | Arguments used |
|---|---|---|
| `docs.create` | `create_doc` | `user_google_email`, `title`, `content?` |
| `docs.write` (replace content) | `manage_doc_tab` | `user_google_email`, `document_id`, `action="populate_from_markdown"`, `markdown_text`, `replace_existing=true` (the default; it clears the tab first) |
| `gmail.create_draft` | `draft_gmail_message` | `user_google_email`, `to`, `subject`, `body`, `body_format="plain"` |

Alternatives on the same server: `find_and_replace_doc`, and `batch_update_doc` (append with `end_of_segment=true`).

### Against the selection criteria

| # | Criterion | Status |
|---|---|---|
| 1 | Create a Doc and write body text; returns ID and URL | Create and write ✓. **ID/URL return format not yet confirmed** (needs sign-in) |
| 2 | Replace or clear an existing Doc's content | ✓ `manage_doc_tab` `populate_from_markdown` with `replace_existing=true`. It also renders Markdown headings and bold, so T7.2 formatting comes for free. Needs a live check that it works on the default tab without a `tab_id` |
| 3 | Create a Gmail draft, not only send | ✓ `draft_gmail_message`. The server also exposes `send_gmail_message`, so the client-side "refuse any tool containing `send`" rule (T7.3) matters |
| 4 | Handles OAuth itself, stores tokens locally | ✓ |
| 5 | stdio or streamable HTTP; maintained | ✓ both; active repository (MIT) |

### Gaps and follow-ups

- **Every tool needs `user_google_email`.** Add a `delivery.user_google_email: ${PULSE_GOOGLE_EMAIL}` setting in T7.1.
- **Still to do (needs your Google sign-in):** create a Google Cloud OAuth client (Desktop app type, with the Docs and Gmail APIs enabled), put its ID and secret in the server's `env`, then run

  ```sh
  python spikes/mcp_probe.py call --server google-workspace --tool create_doc \
    --args '{"user_google_email": "<you>", "title": "Pulse probe", "content": "hello"}'
  python spikes/mcp_probe.py call --server google-workspace --tool draft_gmail_message \
    --args '{"user_google_email": "<you>", "to": "<you>", "subject": "Pulse probe", "body": "hello"}'
  ```

  Record the exact text returned (the Doc ID/URL and draft ID formats) here. T7.1's extractors depend on it.
- `config/settings.yaml` keeps placeholder tool names until delivery is built and the live probe passes, so `check_delivery_ready()` keeps blocking delivery until then.
- With `mcp` 2.x, the client API in the T7.1 sketch is simpler: `async with Client(StdioServerParameters(...)) as client` (or `Client("<url>")` for HTTP), then `list_tools()` / `call_tool()`.

## T3.7 LLM smoke test (live) — 2026-10-17 15:20

- Model `claude-opus-5-5`, effort `low`, chunk size 50, window 2026-W41 (1534 reviews, 31 chunks)
- Classify system prompt (placeholder taxonomy): **1195 tokens** (Opus 5.5 caches from 512)
- Review input: **98,450 tokens** total, **64.2 per review** (estimate was about 50)

| Chunk | Reviews | Seconds | Input | Output | Cache write | Cache read | Missing | Unknown | Bad theme | Cost |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | 50 | 15.6 | 2898 | 1853 | 1565 | 0 | 0 | 0 | 0 | $0.0565 |
| 2 | 50 | 14.0 | 3184 | 1840 | 0 | 1565 | 0 | 0 | 0 | $0.0498 |

- Output tokens per review (incl. thinking): **36.9**
- Cache hit on a later call: **yes**
- Fallback used: no
- Label mix (placeholder taxonomy): other 39, app_performance 34, charges_brokerage 13, withdrawals_payments 8, customer_support 4, kyc_onboarding 2
- Projected cost: first full run (1534 reviews) **$1.54**; weekly run (~128 reviews) **$0.13**

**Findings**

- Every label came back for every review: no missing, unknown or invalid IDs in either chunk. `chunk_size: 50` stays.
- The cached prefix is 1,565 tokens (the system prompt plus the output schema). Chunk 2 read all of it from cache, so T3.4's acceptance check passes.
- Reviews are about 64 tokens each, not the 50 estimated. Output, including thinking at effort `low`, is about 37 tokens per review. A 50-review chunk takes about 15 s and costs about $0.05.
- Projected classification cost: **about $1.54 for the first full run and $0.13 a week.** Both are under the plan's estimates, so batching stays deferred: it would save under $1 once.
- The 39% `other` share comes from the *placeholder* taxonomy (synthetic test themes), not the real one. The real taxonomy from T4.2 should bring it well under the 25% alert.

## Live Phase 4–6 runs (17 Oct 2026, week 2026-W41)

| Stage | Calls | Time | Cost | Result |
|---|---|---|---|---|
| Discover (effort `high`) + 200-review coverage check | 1 + 4 | 1 min 45 s | $0.71 | v1, 5 themes, 9% "other" on the sample |
| Classify the rest of the window (effort `low`) | 27 | 6 min 35 s | $1.47 | 1,334 labelled, 0 unclassified |
| Quotes (effort `medium`) | 1 | – | $0.02 | 3 of 3 model picks passed the verbatim check |
| Compose (effort `high`) | 1 | – | $0.05 | 201 words, guardrails green on the first attempt |

Theme mix over the window (1,534 reviews): Ease of Use Praise 24.8%, Features & UI Changes 23.3%, Support, Funds & Accounts 18.8%, App Reliability & Execution 17.7%, Brokerage & Charges 9.5%, other 5.8%. All within the T3.7 estimate (first run about $1.5–2.5).


## T7.0 Live probe of `gmail-docs-mcp` (18 Oct 2026)

- `GET /healthz` returns `{"ok":true}`. `POST /mcp` with a wrong bearer token returns HTTP 401; the `mcp` 2.3 Python SDK surfaces it as `MCPError(-32000, 'Unauthorized')` inside an exception group, which the client maps to "check PULSE_MCP_TOKEN".
- `python spikes/mcp_probe.py list` against the live server: five tools, **drafts enabled**. `tests/fixtures/mcp/tools.json` is identical to the server's committed contract snapshot (repo commit `1e45f3e`):

| Tool | Required inputs | `structuredContent` |
|---|---|---|
| `gdocs_get_document_info` | `document` (+ `tail_chars` ≤ 5000) | `document_id`, `title`, `url`, `revision_id`, `tabs`, `end_index`, `tail_text` |
| `gdocs_append_text` | `document`, `text` (+ `paragraph_style`, `start_on_new_line`, `client_request_id`) | `document_id`, `title`, `url`, `tab_id`, `inserted_chars`, `start_index`, `end_index`, `revision_id` |
| `gdocs_append_table` | `document`, `rows` (+ `bold_header`, `client_request_id`) | `document_id`, `title`, `url`, `tab_id`, `row_count`, `column_count`, `start_index`, `end_index`, `revision_id` |
| `gmail_create_draft` | `to`, `subject`, `body` (+ `body_format`, `client_request_id`) | `draft_id`, `message_id` |
| `gmail_send_email` | never called | n/a |

- `info` on the pulse Doc ("Groww Pulse Agent", one tab `t.0`, empty: `end_index` 2): the token, sharing and `DOCS_ALLOWED_IDS` are correct. The URL comes back as `https://docs.google.com/document/d/<id>/edit`.
- `draft`: `{"draft_id": "r-2956390884323324728", "message_id": "1a14d75a865b65c2"}`, text "Draft created for … It has NOT been sent".
- Error shape: `isError: true` with one text block, never containing Doc or email text. Seen live:
  - `Not signed in to Google. Ask the server owner to run \`npx gmail-docs-mcp auth\` …`: `GOOGLE_TOKEN_JSON` was not reaching the deployment. (A set-but-bad value gives "not valid JSON" or "missing required fields" instead.)
  - `Blocked recipient(s): … This server only sends to: …`: `EMAIL_ALLOWED_DOMAINS` holds **domains** (`gmail.com`, or `.example.com` for subdomains), not full addresses.
- A reused `client_request_id` with different arguments is an `IDEMPOTENCY_CONFLICT` error, so request IDs include a hash of the content.

## T7.7 Live delivery check (18 Oct 2026)

- `pulse deliver --week 2026-W41`: section appended at indexes 1–1255 (8 parts), draft `r-8568990621970491174` created, nothing sent. `runs` row: status `delivered`, section indexes and draft ID stored.
- Re-run: Doc section `skipped`, draft `skipped`; flags `doc_already_has_week`, `draft_already_exists`; zero writes. The Doc's tail ends with the W41 actions.

## T9.4 Cost and runtime baseline (2026-W41, first live run)

From `runs/2026-W41/manifest.json` and the run logs (17–18 Oct 2026, Claude Opus 5.5, list prices).

| Stage | Calls | Input tokens (incl. cache) | Output tokens | Est. cost | Wall-clock |
|---|---|---|---|---|---|
| ingest (Play Store, 12 weeks) | – | – | – | – | 132 s |
| discover (whole window + 200-review coverage check) | 5 | 123,132 | 12,310 | $0.71 | ≈ 80 s |
| classify (first full run, 1,334 reviews in 27 chunks) | 27 | not kept, see note | not kept | ≈ $1.35 (27 × ≈ $0.05, T3.7 rate) | 6 min 33 s |
| quotes | 1 | 3,586 | 193 | $0.015 | 5 s |
| compose (1 attempt, 199 words) | 1 | 4,922 | 1,130 | $0.039 | 15 s |
| deliver (8 Doc parts + draft) | 10 MCP calls | – | – | – | 1–2 s |
| **First run total** | | | | **≈ $2.1** | **≈ 10 min** |

- **Against the T3.7 estimate:** the first run (≈ $2.1) is inside the planned $1.5–2.5. Discovery is a third of it, and it only runs when the taxonomy is redone.
- **Weekly run (projected):** about 130 new reviews, so 3 classify chunks (≈ $0.15, ≈ 45 s), plus quotes and compose (≈ $0.05). That's **≈ $0.20 a week** and ≈ 3–4 minutes, mostly ingest. It's at the bottom of the planned $0.20–0.40.
- **Decision:** don't build the batch runner (T3.3). Batches would save about half the classification cost: under $1 once, and ≈ $0.07 a week. A cheaper classifier model (Sonnet 5.5, about half the price) would save ≈ $0.07 a week. It's only worth testing if `pulse eval classify` stays ≥ 80% on it, which isn't measured yet.
- **Note:** the classify stage's token counts in the manifest come from the last classify call (a 1-call top-up). A stage that runs again replaces its usage record, so the first full run's exact tokens weren't kept. Its cost above uses the T3.7 per-chunk rate and the 27 chunks in `runs/classify.log`. `pulse show` reports per-stage usage for the most recent run of each stage.
