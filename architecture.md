# Groww Feedback Analyser — Architecture

> Companion to [problemStatement.md](problemStatement.md). That file says **what** to build; this file says **how**.

## 1. Summary

A Python command-line pipeline (`pulse`) runs once a week and:

1. Pulls public Groww reviews from the last 8–12 weeks from Google Play.
2. Removes personal data, then keeps only English reviews with more than 8 words and no emoji.
3. Uses Claude to sort reviews into at most 5 themes.
4. Computes theme statistics in code and picks the top 3 themes.
5. Uses Claude to pick 3 verbatim quotes and to write 3 action ideas, within a ≤250-word note.
6. Checks the note against hard rules (word count, verbatim quotes, no personal data).
7. Acts as an **MCP client**: creates the Google Doc through a Docs MCP server and a Gmail draft through a Gmail MCP server.

**Guiding principle: the LLM proposes, code checks.** Claude is used only where judgment is needed (naming themes, classifying text, choosing quotes, writing takeaways and actions). Everything that can be checked (counts, rankings, quote fidelity, word limits, personal-data scans) is done in plain code, and the run stops before delivery if any check fails.

## 2. Decisions on Open Questions

These resolve the open questions in [problemStatement.md §10](problemStatement.md#10-open-questions--assumptions). Each one is a config setting, so it can change without code changes.

| # | Question | Decision (default) | Config key |
|---|---|---|---|
| 1 | Include the App Store? | **No.** Google Play is the only store source; App Store reviews are out of scope. | — |
| 2 | Which public review source? | The `google-play-scraper` library, which reads the same public listing anyone sees without logging in. Plus a CSV import adapter for any manually exported public dataset. Confirm this is acceptable under the course rules. | `sources.*` |
| 3 | Weekly note vs. 8–12 week window | Rank themes over the **full rolling window** (default 12 weeks), which matches the brief literally. Show a trend marker for the latest week against the window's weekly average, and prefer quotes from the latest week so the note stays fresh. | `window.weeks`, `window.ranking_scope` |
| 4 | Email recipient | Read from the `PULSE_RECIPIENT` environment variable (self or alias); never committed to the repo. | `pulse.recipient` |
| 5 | One Doc per week, or one running Doc? | **One Doc per week**, so each email links to a single clean page. Re-running the same week updates that Doc rather than creating a new one. | `delivery.docs.mode` |
| 6 | Language | **English only.** Reviews in other scripts and Hinglish (Hindi in Latin letters) are dropped at ingest, along with reviews of 8 words or fewer and any review containing an emoji. | `sources.filters` |

**Week definition:** ISO week, Monday–Sunday, Asia/Kolkata time. The pulse for a week is generated on the following Monday. Example: `2026-W41` covers 5–11 Oct 2026 and runs on 12 Oct 2026.

## 3. System Context

```
 PUBLIC SOURCES             PIPELINE  (Python CLI: pulse run --week 2026-W41)                 DELIVERY
 ──────────────             ─────────────────────────────────────────────────                 ────────
 Google Play reviews ─┐
                      ├──▶ [1] Ingest adapters
 CSV export (manual) ─┘          │  allowlisted fields only
                                 ▼
                           [2] Privacy scrubber ── redact emails, phones, PAN, Aadhaar, UPI IDs, account/order IDs
                                 │
                                 ▼
                           [3] Review filter + store (SQLite) ── >8 words, no emoji, English only; dedupe
                                 │
                                 ▼
                           [4] Theme classifier ────────────────▶ Claude API (Message Batches)
                                 │  ≤5 themes + "other", sentiment
                                 ▼
                           [5] Aggregator (code) ── count, share, avg rating, % negative, trend → top 3
                                 │
                                 ▼
                           [6] Quote selector ──────────────────▶ Claude API
                                 │  verbatim check in code
                                 ▼
                           [7] Composer ────────────────────────▶ Claude API
                                 │  takeaways + 3 actions → template render
                                 ▼
                           [8] Guardrails ── 3/3/3 sections, ≤250 words, verbatim quotes, personal-data scan
                                 │
                                 ▼
                           [9] Delivery (MCP client) ──┬──▶ Google Docs MCP server ──▶ Pulse Doc (URL)
                                                       └──▶ Gmail MCP server ───────▶ Draft email (Doc link + note)
```

## 4. Components

### 4.1 Ingest adapters — `pulse/ingest/`

Each adapter implements one interface and returns normalized `RawReview` records. **Field allowlist, not blocklist:** an adapter copies only the fields listed below. Usernames, avatars, reviewer IDs and developer replies are never read into the pipeline's data model.

| Adapter | Source | Fields kept | Notes |
|---|---|---|---|
| `PlayStoreSource` | `google-play-scraper` `reviews()` with `lang=en`, `country=in`, sorted newest first | text, star rating, date, app version, thumbs-up count, review ID (hashed, see below) | Pages through results with the continuation token and stops at the first review older than the window start. Waits between pages and caps the total review count. |
| `CsvSource` | Local CSV/JSON file | Configurable column mapping onto the same fields | Fallback for a manually exported public dataset. Rejects files that contain mapped identity columns. |

**Review filters** (`sources.filters`), applied after scrubbing and before storage. A review is kept only if it has **more than 8 words**, contains **no emoji**, and is **English**: mostly Latin script and not Hinglish, detected with a list of common Hindi words written in Latin letters. Reviews that fail are never stored. Each ingest also deletes stored reviews that fail the current filters, so a filter change takes effect on the next run. On the 12 weeks to 2026-W41 this keeps about 1,530 of about 12,850 reviews (about 130 a week).

**Review IDs:** the store's review ID is replaced by `review_id = sha256(salt + store + store_review_id)`. This keeps de-duplication across weekly runs without storing a store identifier. The salt lives in an environment variable.

### 4.2 Privacy scrubber — `pulse/privacy/`

Runs on every review's text **before** it is written to storage or sent to Claude.

| Pattern | Example match | Replacement |
|---|---|---|
| Email | `name@domain.com` | `[EMAIL]` |
| Indian phone | `+91 98xxxxxxxx`, `9xxxxxxxxx` | `[PHONE]` |
| PAN | `ABCDE1234F` | `[PAN]` |
| Aadhaar | `1234 5678 9012` | `[AADHAAR]` |
| UPI ID | `name@okaxis` | `[UPI]` |
| Long numeric / alphanumeric IDs | account, demat, order or ticket numbers (9+ digits, or mixed IDs) | `[ID]` |
| URL | `https://…` | `[URL]` |

Regular expressions can't reliably catch personal names inside text (for example "Rahul from support"). Instead, the classifier (§4.4) flags each review with `mentions_person`, and **any review with a redaction token or `mentions_person = true` is excluded from quote candidates**. Quotes are therefore both verbatim and clean: a quote never contains `[PHONE]` and never needs rewording.

### 4.3 Review store — `pulse/store/`

SQLite (`data/pulse.db`, gitignored). One file, no server, supports upserts so weekly runs fetch and classify only new reviews.

```
reviews(review_id PK, store, rating, text, review_date, app_version,
        thumbs_up, lang, word_count, has_redaction, ingested_at)

taxonomies(version PK, created_at, themes_json, approved)        -- ≤5 themes per version

classifications(review_id, taxonomy_version, theme_id, sentiment,
                mentions_person, model, classified_at,
                PRIMARY KEY (review_id, taxonomy_version))

runs(week PK, window_start, window_end, taxonomy_version, status,
     stats_json, pulse_json, doc_id, doc_url, draft_id, created_at, updated_at)
```

Each run also writes inspectable files to `runs/<week>/` (gitignored): `stats.json`, `pulse.json`, `pulse.md` and `manifest.json` (status of each stage, counts, model IDs, timings). Raw API responses are never stored on disk because they contain usernames.

### 4.4 Theming — `pulse/analysis/`

**Why a fixed taxonomy plus LLM classification** rather than unsupervised clustering (embeddings + k-means): theme names stay stable from week to week, so trends are comparable; every assignment can be explained; and the ≤5 limit is enforced by construction. The cost is one LLM call per batch of reviews, which the Batches API halves (§5).

**a) Taxonomy discovery (`pulse themes discover`, run once or on demand)**
- Take a sample of about 400 reviews, spread across star ratings and weeks.
- Claude proposes **at most 5 themes**, each with an `id`, `name`, one-line `definition`, and include/exclude hints. The schema caps the list at 5.
- The result is saved as a new taxonomy version in `config/themes.yaml` with `approved: false`. A person reviews and approves it once; later runs reuse the approved version.
- Likely shape for Groww (to be confirmed by discovery): *App performance & order execution*, *KYC & onboarding*, *Payments, withdrawals & fund transfers*, *Customer support*, *Charges & UI/UX*.

**b) Classification (every run, new reviews only)**
- Very short reviews (for example "good app") are already removed at ingest by the more-than-8-words filter (§4.1), so every stored review is sent to Claude. `min_words_to_classify` remains as a safety net.
- Requests hold about 50 reviews each. The system prompt (instructions + taxonomy) is a stable, cached prefix; the review texts come after it.
- Output uses structured outputs (a JSON schema) and returns one record per review: `{review_id, theme_id ∈ taxonomy ∪ {"other"}, sentiment ∈ {positive, neutral, negative}, mentions_person: bool}`.
- **One primary theme per review**, so theme shares add up to 100% and the counts are simple to read.
- Code checks that every input `review_id` comes back exactly once with a valid `theme_id`. Missing or invalid records are retried once, then marked `unclassified`.
- `"other"` is a bucket, not a theme. If it exceeds `other_share_alert` (default 25%), the run manifest flags that the taxonomy needs rediscovery.

**c) Aggregation (code only)**
For each theme over the window: count, share of classified reviews, average rating, % negative, latest-week count, and a trend marker (latest week vs. the window's weekly average: ▲ / ▼ / ▬ at a ±20% threshold). Themes are ranked by **count** (the brief asks what people talk about most), with average rating as the tie-breaker. The top 3 go into the pulse.

### 4.5 Quote selector — `pulse/analysis/quotes.py`

1. **Candidate pool (code):** for each top-3 theme, up to 15 reviews that are classified into that theme, in a quotable language, 8–60 words long, free of redaction tokens and `mentions_person`, and whose sentiment matches the theme's majority sentiment. The latest week is preferred, falling back to the full window.
2. **Selection (Claude):** returns `{theme_id, review_id, quote}` per theme, where `quote` is a contiguous excerpt of at most `quote_max_words` words.
3. **Verbatim check (code):** `quote` must be an exact substring of that review's stored text (whitespace-normalized only). If the check fails, the next candidate is used. As a last resort, code takes the candidate's first sentence. **Quote text reaches the pulse only from the database, never from model output that hasn't passed this check.**

### 4.6 Composer — `pulse/compose/`

- Claude receives the theme statistics, the 3 approved quotes, and about 10 sample reviews per top theme for context.
- It returns structured JSON:
  - `takeaways`: one per top theme, ≤12 words each
  - `actions`: exactly 3 items of `{theme_id, text}`, ≤20 words each, concrete and owner-actionable (for example "Add UPI withdrawal status tracking in-app", not "Improve payments")
- **Rendering is deterministic:** a Jinja2 template ([problemStatement.md §9](problemStatement.md#9-suggested-pulse-template)) combines stats, takeaways, quotes and actions into `pulse.md` (for the Doc) and a plain-text version (for the email body).
- **Word budget** (≤250 total): header ~20, themes 3 × ~20, quotes 3 × ~32, actions 3 × ~22, so about 240.

### 4.7 Guardrails — `pulse/validate/`

Hard checks; delivery is blocked unless all pass.

| Check | Rule |
|---|---|
| Structure | Exactly 3 themes, 3 quotes, 3 actions |
| Taxonomy | Taxonomy has ≤5 themes; pulse themes ⊆ taxonomy |
| Verbatim | Each quote is a substring of its source review |
| Grounding | Each action's `theme_id` is one of the top 3 |
| Length | Rendered note ≤250 words |
| Privacy | The §4.2 scrubber patterns find nothing in the final text, the Doc body, or the email body |

If a check fails, the composer is re-run once with the failure list as feedback. If it fails again, the run ends with status `failed_validation`; `pulse.md` is kept for inspection, and nothing is sent to Docs or Gmail.

### 4.8 Delivery via MCP — `pulse/deliver/`

The pipeline is an **MCP client** built on the official `mcp` Python SDK. It connects to the servers listed in `config/mcp_servers.json`, over stdio or streamable HTTP. One Google Workspace MCP server that exposes both Docs and Gmail tools works just as well as two separate servers.

**No Google credentials in this codebase.** OAuth consent and token storage belong to the MCP servers. The pipeline only calls their tools.

**Tool mapping in config:** different MCP servers name their tools differently, so the logical operations (`docs.create`, `docs.write`, `gmail.create_draft`) map to real tool names in `settings.yaml`. At startup the client calls `list_tools()` and fails fast if a mapped tool is missing.

**Flow:**
1. Connect to the servers and check the mapped tools exist.
2. **Docs:** if `runs.doc_id` is already set for this week, overwrite that Doc's content. Otherwise create a Doc titled `Groww Review Pulse — Week of 05 Oct 2026` and write the rendered note. Record `doc_id` and `doc_url`.
3. **Gmail:** if `runs.draft_id` is already set, skip (unless `--force`). Otherwise create a **draft** (never sent) to `PULSE_RECIPIENT` with:
   - Subject: `Groww Review Pulse — Week of 05 Oct 2026`
   - Body: the plain-text note plus `Full pulse: <doc_url>`
4. Record IDs and mark the run `delivered`.

**Why the pipeline calls MCP tools directly instead of putting an LLM in the loop:** delivery is a fixed sequence of writes. Calling the tools from code makes it deterministic, idempotent and testable, and there is no risk of a model rewording the checked note on its way out.

**Alternatives (supported, not the default):**
- **Interactive run from Claude Code:** run `pulse run --no-deliver`, then ask Claude Code, with its Google Docs and Gmail connectors, to publish `runs/<week>/pulse.md` and draft the email. Good for demos.
- **Claude API MCP connector** (`mcp_servers` + `mcp_toolset`, beta `mcp-client-2025-11-20`): only applies if the Google MCP servers are hosted at a remote URL, and it puts a model back into the delivery path.

## 5. LLM Usage

All calls go through the official `anthropic` Python SDK. Model and effort are set per stage in config.

| Stage | Default model | Effort | API mode | Output |
|---|---|---|---|---|
| Taxonomy discovery | `claude-opus-5-5` | `high` | Synchronous, `messages.parse()` | ≤5 themes (Pydantic schema) |
| Classification | `claude-opus-5-5` | `low` | **Message Batches** (50% cost, async; weekly job isn't latency-sensitive). Synchronous below `batch_threshold` reviews or with `--no-batch` | Per-review records (JSON schema) |
| Quote selection | `claude-opus-5-5` | `medium` | Synchronous, `messages.parse()` | 3 quote picks |
| Composer | `claude-opus-5-5` | `high` | Synchronous, `messages.parse()` | Takeaways + 3 actions |

Implementation notes:
- **Structured outputs everywhere:** `client.messages.parse(output_format=<PydanticModel>)` for synchronous calls; `output_config.format` with a JSON schema inside each batch request.
- **Prompt caching:** the classification system prompt and taxonomy form an identical prefix across every request in a run, so it is marked for caching.
- **Batches:** results arrive in any order and are keyed by `custom_id` (`<week>:<chunk_no>`). Poll until `processing_status == "ended"`, then handle `succeeded`, `errored` and `expired` results; failed chunks fall back to synchronous calls.
- **Refusals:** check `stop_reason` before reading content. Synchronous calls enable server-side fallbacks (`fallbacks: "default"`, beta `server-side-fallback-2026-07-01`).
- **Opus 5.5 specifics:** thinking can't be turned off, so `effort` is the cost and depth control. Its default is `medium`, so each stage sets effort explicitly.
- **Data sent to Claude:** only scrubbed review text, ratings and dates; no identifiers.
- **Cost levers, in order:** skip very short reviews → classify only new reviews (cached by `review_id` + taxonomy version) → Batches → prompt caching → lower effort. Switching the classifier to a cheaper model is a config change, worth testing against a hand-labelled sample before adopting.

## 6. End-to-End Run Sequence

```
pulse run --week 2026-W41
 │
 ├─ load config, resolve window (12 weeks ending 11 Oct 2026, Asia/Kolkata)
 ├─ ingest      → fetch new public reviews → scrub → upsert into SQLite        [manifest: ingested=N]
 ├─ classify    → unclassified reviews in window → Batches → validate → store  [manifest: classified=N, other=%]
 ├─ aggregate   → stats.json, top 3 themes
 ├─ quotes      → candidate pool → Claude pick → verbatim check
 ├─ compose     → Claude takeaways + actions → render pulse.md / pulse.txt
 ├─ guardrails  → pass? ──no──▶ re-compose once ──still failing──▶ status=failed_validation, stop
 │                 │yes
 ├─ deliver     → MCP: Docs create/update → Gmail draft                         [manifest: doc_url, draft_id]
 └─ status=delivered
```

Every stage records its status in `manifest.json` and is idempotent. `pulse run` on a partially completed week resumes at the first incomplete stage; `--from <stage>` forces a re-run from a given stage.

## 7. CLI

| Command | Purpose |
|---|---|
| `pulse ingest [--weeks 12]` | Fetch and scrub new reviews into the store |
| `pulse themes discover` | Propose a new taxonomy version for review |
| `pulse themes approve <version>` | Mark a taxonomy version as the active one |
| `pulse run --week <ISO week> [--dry-run] [--no-deliver] [--from <stage>] [--force]` | Full pipeline. `--dry-run` writes the Docs/Gmail payloads to `runs/<week>/` without calling MCP. |
| `pulse deliver --week <ISO week>` | Retry delivery only (for example after an MCP auth fix) |
| `pulse show --week <ISO week>` | Print the pulse and the run manifest |

## 8. Configuration

`config/settings.yaml`:

```yaml
app:
  name: Groww
  play_store: { package_id: com.nextbillion.groww, lang: en, country: in }

window:
  weeks: 12                  # 8–12
  timezone: Asia/Kolkata
  ranking_scope: window      # window | latest_week

analysis:
  max_themes: 5
  top_themes: 3
  min_words_to_classify: 4
  other_share_alert: 0.25
  quote_languages: [en, hinglish]

pulse:
  max_words: 250
  quote_max_words: 30
  recipient: ${PULSE_RECIPIENT}

llm:
  discover: { model: claude-opus-5-5, effort: high }
  classify: { model: claude-opus-5-5, effort: low, use_batches: true, batch_threshold: 200, chunk_size: 50 }
  quotes:   { model: claude-opus-5-5, effort: medium }
  compose:  { model: claude-opus-5-5, effort: high }

delivery:
  docs:
    server: google-workspace
    mode: per_week
    title: "Groww Review Pulse — Week of {week_start:%d %b %Y}"
    tools: { create: <server tool name>, write: <server tool name> }
  gmail:
    server: google-workspace
    subject: "Groww Review Pulse — Week of {week_start:%d %b %Y}"
    tools: { create_draft: <server tool name> }
```

`config/mcp_servers.json` (standard MCP client config; the commands depend on which servers you choose):

```json
{
  "mcpServers": {
    "google-workspace": { "command": "<server launch command>", "args": [] }
  }
}
```

**Secrets (environment only, never in the repo):** `ANTHROPIC_API_KEY` (or an `ant auth login` profile), `PULSE_RECIPIENT`, `PULSE_ID_SALT`. Google credentials stay with the MCP server.

## 9. Project Layout

```
groww feedback analyser/
├── problemStatement.md
├── architecture.md
├── pyproject.toml
├── config/
│   ├── settings.yaml
│   ├── themes.yaml              # versioned taxonomies (≤5 themes each)
│   └── mcp_servers.json
├── prompts/                     # versioned prompt text: discover, classify, quotes, compose
├── src/pulse/
│   ├── cli.py                   # Typer entry point
│   ├── config.py                # settings + env resolution
│   ├── models.py                # Pydantic: RawReview, Review, Theme, Classification, Pulse
│   ├── ingest/                  # base.py, play_store.py, csv_import.py
│   ├── privacy/scrubber.py
│   ├── store/db.py
│   ├── llm/client.py            # Anthropic client, batching, retries, refusal handling
│   ├── analysis/                # taxonomy.py, classifier.py, aggregate.py, quotes.py
│   ├── compose/                 # composer.py, templates/pulse.md.j2, pulse.txt.j2
│   ├── validate/guardrails.py
│   └── deliver/                 # mcp_client.py, docs.py, gmail.py
├── tests/
├── data/                        # gitignored: pulse.db
└── runs/                        # gitignored: per-week artifacts
```

**Tech stack:** Python 3.11+, `google-play-scraper`, `pydantic`, `sqlite3` (stdlib), `anthropic`, `mcp`, `jinja2`, `typer`, `pytest`.

## 10. Privacy and Compliance by Design

| Risk | Control |
|---|---|
| Reviewer identity reaches storage | Adapters copy allowlisted fields only; store review IDs are salted hashes |
| Personal data inside review text | Scrubber runs before storage and before any LLM call |
| Names inside text slip into quotes | Reviews flagged `mentions_person` or containing redaction tokens are excluded from quotes |
| Personal data in the final Doc or email | Guardrail scan on the rendered outputs; delivery blocked on any hit |
| Personal data in logs | Logs record counts, IDs, stages and timings only, never review text |
| Store terms of service | Public, logged-out listings only; rate-limited; capped volume; CSV fallback |
| Invented quotes | Quotes are copied from the database and checked as substrings in code |
| Credential handling | None in this codebase: Google auth lives in the MCP servers, Anthropic auth in the environment |

## 11. Failure Handling

| Failure | Behaviour |
|---|---|
| Review source rate-limited or unavailable | Retry with backoff; continue with already-stored reviews; manifest flags `partial_ingest` and the pulse header shows the real sample size |
| Low review volume in the latest week | Pulse still produced from the window; trend markers suppressed below a minimum count |
| Invalid or missing classification records | Retry the chunk once, then mark those reviews `unclassified` (excluded from shares) |
| Batch errors or expiry | Failed chunks re-run synchronously |
| LLM refusal | Server-side fallback; if still refused, that stage fails with a clear manifest entry |
| Guardrail failure | One re-compose with feedback, then stop before delivery |
| MCP server down or Google auth expired | Pulse kept locally; `pulse deliver --week …` retries delivery only |
| Re-run of a delivered week | Doc updated in place; no duplicate draft unless `--force` |

## 12. Testing Strategy

- **Unit tests:** scrubber patterns (Indian phone, PAN, Aadhaar, UPI, email) including near-misses; verbatim checker; word counter; aggregation and ranking; ISO-week and window maths in Asia/Kolkata time.
- **Golden tests:** a fixed set of fixture reviews with recorded LLM responses, so the rendered `pulse.md` is compared byte for byte.
- **LLM contract tests:** schema validation of every stage's output; classification checked against a small hand-labelled sample (target ≥80% agreement) before a taxonomy or model change is adopted.
- **Delivery tests:** a fake in-process MCP server that records tool calls, used to check idempotency (second run updates the Doc and doesn't create a second draft). `--dry-run` for manual checks.
- **End-to-end smoke test:** `pulse run --week <last week> --no-deliver` against live sources, then a manual review of `pulse.md`.

## 13. Scheduling

Runs weekly on **Monday 09:00 IST** for the previous ISO week, via local `cron` or `launchd`. It runs on the same machine because the MCP servers' Google OAuth tokens live there. Moving to CI later means hosting the MCP servers remotely with their own auth; the pipeline code doesn't change.

## 14. Requirement Traceability

| Requirement (problemStatement.md) | Where it is met |
|---|---|
| Reviews from the last 8–12 weeks | §4.1 adapters, `window.weeks` |
| Public exports only; no login scraping | §4.1, §10 |
| ≤5 themes | §4.4 schema cap + guardrail |
| Top 3 themes in the note | §4.4c aggregation |
| 3 verbatim user quotes | §4.5 selector + verbatim check |
| 3 action ideas grounded in themes | §4.6 composer + grounding guardrail |
| ≤250 words, scannable | §4.6 word budget + §4.7 guardrail |
| No personal data in any artifact | §4.2, §4.7, §10 |
| Pulse in Google Docs via MCP | §4.8 |
| Gmail draft to self or alias via MCP | §4.8 |
| No bespoke OAuth or REST client for Google | §4.8: MCP client only |

## 15. Out of Scope

- Apple App Store reviews (Google Play only)
- Sending email automatically (draft only)
- Replying to reviews or writing back to the stores
- Dashboards or a web UI
- Real-time or streaming analysis
- Direct Google API integration
