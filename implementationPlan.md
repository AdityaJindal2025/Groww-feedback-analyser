# Groww Feedback Analyser — Implementation Plan

> Builds the system described in [architecture.md](architecture.md) to meet [problemStatement.md](problemStatement.md).
> Section references such as "arch §4.5" point to architecture.md; "PS §7" points to problemStatement.md.

## 0. How to Use This Plan

- Work is split into **11 phases** (P0–P10), grouped into **5 milestones** (M1–M5).
- Each task has an ID (`T<phase>.<n>`), the files it touches, implementation notes, and **acceptance criteria (AC)**. A task is done only when its AC pass.
- Tick the checkboxes as tasks complete; this file doubles as the progress tracker.
- Estimates assume one developer familiar with Python. They are working-day estimates, not commitments.

## 1. Milestones at a Glance

| Milestone | Phases | Outcome | Est. days |
|---|---|---|---|
| **M1 — Data in** | P0 Setup & spikes, P1 Foundations, P2 Ingestion & privacy | Scrubbed Groww Play Store reviews for the last 12 weeks sit in SQLite; no personal data stored | 3.5 |
| **M2 — Themes** | P3 LLM client, P4 Theming | An approved taxonomy of ≤5 themes; every eligible review classified | 3 |
| **M3 — Pulse** | P5 Aggregation & quotes, P6 Compose & guardrails | A validated ≤250-word `pulse.md` for a real week, produced locally | 2.5 |
| **M4 — Delivered** | P7 MCP delivery, P8 Orchestration & CLI | `pulse run` appends the week's section to the pulse Google Doc and creates a Gmail draft through our `gmail-docs-mcp` server, idempotently | 3 |
| **M5 — Production-ready** | P9 Quality, P10 Ops & first live run | Golden and privacy tests green, weekly schedule live, first real pulse delivered | 2 |
| | | **Total** | **≈14** |

### Dependency graph

```
P0 ──▶ P1 ──┬──▶ P2 ──────────────┐
            │                     ▼
            ├──▶ P3 ──────────▶ P4 ──▶ P5 ──▶ P6 ──┐
            │                                      ▼
            └──▶ P7 (needs the deployed MCP server, T7.0) ──▶ P8 ──▶ P9 ──▶ P10
```

- P2 and P3 can run in parallel after P1.
- P7 (MCP delivery) needs the deployed `gmail-docs-mcp` server and its prerequisites (Phase 7 intro); with the fake server (T7.6) it can be built against a fixture `pulse.md`.

### Suggested 3-week schedule

| Week | Focus | Exit check |
|---|---|---|
| Week 1 | P0, P1, P2, P3 | `pulse ingest` fills the DB; LLM wrapper passes tests against the fake client |
| Week 2 | P4, P5, P6 (+ P7 in parallel if a second person is available) | Local `pulse.md` for last week passes all guardrails |
| Week 3 | P7, P8, P9, P10 | Doc + draft created via MCP; schedule installed; first live pulse reviewed |

## 2. Before Writing Code: Prerequisites and Decisions

| # | Item | Owner | Needed by |
|---|---|---|---|
| 1 | Confirm that reading public Play Store reviews with `google-play-scraper` (logged out, rate-limited) satisfies the course's "public review exports" rule (PS §10 Q2) | Project owner | P2 |
| 2 | Anthropic API access: `ANTHROPIC_API_KEY` set, or an `ant auth login` profile | Developer | P3 |
| 3 | `gmail-docs-mcp` on Railway, signed in to the Google account that owns the Doc and drafts; `ENABLE_DRAFTS=true` (re-run `auth` for `gmail.compose`), `EMAIL_ALLOWED_DOMAINS`, `DOCS_ALLOWED_IDS` set | Project owner | P7 |
| 3a | The pulse Google Doc, created once and shared as Editor with that account; its ID in `PULSE_DOC_ID` | Project owner | P7 |
| 3b | The server's bearer token in `.env` as `PULSE_MCP_TOKEN` | Project owner | P7 |
| 4 | Recipient address or alias for the draft (`PULSE_RECIPIENT`) (PS §10 Q4) | Project owner | P7 |
| 5 | Python 3.11+ and `uv` installed | Developer | P0 |

The App Store question (PS §10 Q1) is already decided: Google Play only (arch §2).

---

## 3. Phase 0 — Setup and Spikes (≈1 day)

Goal: a runnable skeleton, plus answers to the two biggest unknowns: **how many reviews arrive per week** (drives cost and batching) and **what the chosen MCP server's tools actually look like** (drives P7).

- [x] **T0.1 Repository skeleton** _(done with a plain venv: `uv` isn't installed on this machine, so there's no `uv.lock` yet; run `uv sync` once it is)_
  - Files: `pyproject.toml`, `.gitignore`, `src/pulse/__init__.py`, `src/pulse/cli.py`, `tests/test_smoke.py`, `README.md`
  - `git init`; `uv init --package`; add dependencies: `anthropic`, `mcp`, `google-play-scraper`, `pydantic`, `pydantic-settings`, `pyyaml`, `jinja2`, `typer`, `python-dotenv`. Dev: `pytest`, `pytest-asyncio`, `ruff`, `mypy`. Pin through `uv.lock`.
  - `.gitignore`: `data/`, `runs/`, `.env`, `.venv/`, `__pycache__/`, `*.db`, `spikes/output/`
  - Console script entry point `pulse = "pulse.cli:app"`.
  - **AC:** `uv run pulse --help` prints the command list; `uv run pytest` passes; `uv run ruff check .` is clean.

- [x] **T0.2 Environment and secrets** _(`.env.example` couldn't be written because a permission rule blocks `.env*` files; its template is in README.md)_
  - Files: `.env.example`, `src/pulse/env.py`
  - Variables: `ANTHROPIC_API_KEY` (optional when an `ant` profile exists), `PULSE_RECIPIENT`, `PULSE_ID_SALT` (random 32+ chars, generated once and never changed, or de-duplication breaks).
  - **AC:** a missing required variable stops the program with a message naming the variable; `.env` is gitignored.

- [x] **T0.3 Spike: Play Store volume and data shape**
  - File: `spikes/play_volume.py` (throwaway, not part of the package)
  - Fetch the newest `com.nextbillion.groww` reviews (`lang=en`, `country=in`) back to 12 weeks ago, or until 20,000 reviews.
  - Print **aggregates only**: reviews per ISO week, share under 4 words, script mix, rating distribution, and the timezone behaviour of the `at` field. Keep the raw response in memory only; never write it to disk (it contains usernames).
  - Record results in `docs/spikes.md`.
  - **AC:** `docs/spikes.md` has a weekly volume table and recommended values for `classify.chunk_size`, `batch_threshold` and (if volume is high) `max_classify_per_week`.

- [x] **T0.4 Spike: MCP server selection** _(superseded, 18 Oct 2026: delivery uses our own `gmail-docs-mcp` server on Railway, see Phase 7. The live probe moved to T7.0)_
  - File: `spikes/mcp_probe.py`, `docs/spikes.md`
  - Selection criteria for the Google Docs / Gmail MCP server(s):
    1. Can create a Doc **and** write body text, and returns the Doc ID and URL
    2. Can replace or clear an existing Doc's content (needed for idempotent re-runs)
    3. Can create a Gmail **draft** (not only send)
    4. Handles OAuth itself and stores tokens locally; minimal scopes
    5. stdio (or streamable HTTP) transport; actively maintained
  - Probe: connect with the `mcp` client, call `list_tools()`, save each tool's name and input schema, then create one test Doc and one test draft.
  - **AC:** `docs/spikes.md` lists the chosen server, its launch command, the exact tool names and argument shapes for *create doc*, *write/replace content* and *create draft*, and how each returns IDs/URLs. Gaps against criterion 2 are written down (fallback is in T7.2).

- [x] **T0.5 Record decisions**
  - Write the outcomes of §2 items 1 and 4 into `docs/decisions.md`, plus the App Store decision (Google Play only, arch §2).
  - **AC:** each open question in PS §10 has a recorded answer or an explicit "default per arch §2".

---

## 4. Phase 1 — Foundations (≈1 day)

- [x] **T1.1 Configuration loader**
  - Files: `config/settings.yaml`, `src/pulse/config.py`, `tests/test_config.py`
  - Pydantic settings model mirroring arch §8. `${ENV_VAR}` interpolation for `pulse.recipient`.
  - Validation rules: `8 ≤ window.weeks ≤ 12`; `1 ≤ analysis.max_themes ≤ 5`; `analysis.top_themes == 3`; `pulse.max_words ≤ 250`; every `llm.*.effort` ∈ {low, medium, high, xhigh, max}; delivery tool names not left as placeholders unless `--no-deliver`/`--dry-run`.
  - **AC:** invalid values fail with an error naming the key; tests cover each rule.

- [x] **T1.2 Domain models**
  - File: `src/pulse/models.py`
  - Pydantic models: `RawReview`, `Review`, `Theme`, `Taxonomy`, `Classification`, `ThemeStats`, `PulseStats`, `QuotePick`, `Action`, `Takeaway`, `PulseContent`, `StageStatus`, `RunManifest`.
  - `RawReview` holds only allowlisted fields: `store, store_review_id, rating, text, review_date, app_version, thumbs_up`. **No field exists for usernames, avatars or replies**, so adapters can't pass them on by accident.
  - **AC:** JSON round-trip tests for each model; `mypy` passes.

- [x] **T1.3 Week and window maths**
  - File: `src/pulse/timeutil.py`, `tests/test_timeutil.py`
  - Functions: `parse_iso_week("2026-W41") -> (start, end)`, `window_for(week, weeks) -> (start, end)`, `last_completed_week(now) -> str`, `week_of(dt) -> str`. All in `Asia/Kolkata` (`zoneinfo`).
  - **AC (test cases):**
    - `2026-W41` → 2026-10-05 00:00 IST to 2026-10-11 23:59:59 IST
    - 12-week window ending `2026-W41` → starts 2026-07-20
    - `2026-W01` → 2025-12-29 to 2026-01-04 (year boundary)
    - `last_completed_week(2026-10-16 10:00 IST)` → `2026-W41`

- [x] **T1.4 SQLite store**
  - File: `src/pulse/store/db.py`, `tests/test_store.py`
  - Schema from arch §4.3, plus:
    - `reviews.script` (`latin` | `devanagari` | `other`); this is what arch §4.3 calls `lang`
    - `classifications.status` (`ok` | `unclassified`) and `classifications.prompt_version`
    - `schema_version` table; indexes on `reviews(review_date)` and `classifications(taxonomy_version, theme_id)`
  - Repository functions: `upsert_reviews`, `reviews_in_window`, `unclassified_reviews(window, taxonomy_version, min_words)`, `save_classifications`, `save_taxonomy`, `active_taxonomy`, `get_run`, `upsert_run`.
  - **AC:** upserting the same reviews twice leaves the row count unchanged; tests run against a temporary DB; a test asserts the `reviews` table has no columns beyond the allowlist.

- [x] **T1.5 Logging and run manifest**
  - Files: `src/pulse/logging.py`, `src/pulse/manifest.py`
  - JSON-lines logging. A logging filter **drops any record carrying `text` or `quote` fields**, so review content can't reach logs.
  - Manifest at `runs/<week>/manifest.json`: per stage `status` (pending/running/done/failed), timestamps, counts, model IDs, prompt versions, token usage, `batch_id`, errors.
  - **AC:** a test that logs a review's text finds it absent from the output; manifest read/write round-trips.

---

## 5. Phase 2 — Ingestion and Privacy (≈1.5 days)

- [x] **T2.1 Privacy scrubber**
  - Files: `src/pulse/privacy/scrubber.py`, `tests/test_scrubber.py`
  - API: `scrub(text) -> ScrubResult(text, counts: dict[str, int])` and `scan(text) -> list[Hit]` (used again by guardrails in T6.4).
  - Pattern order matters (earlier patterns stop later ones from mislabelling the same text). Starting points, to be refined by tests:

    | Order | Token | Starting regex (refine with tests) |
    |---|---|---|
    | 1 | `[URL]` | `https?://\S+\|www\.\S+` |
    | 2 | `[EMAIL]` | `[\w.+-]+@[\w-]+\.[\w.-]+` |
    | 3 | `[UPI]` | `\b[\w.-]{2,}@[A-Za-z]{2,}\b` (runs after email, so only dot-less handles remain) |
    | 4 | `[PAN]` | `\b[A-Za-z]{5}\d{4}[A-Za-z]\b` |
    | 5 | `[AADHAAR]` | `\b[2-9]\d{3}[\s-]?\d{4}[\s-]?\d{4}\b` |
    | 6 | `[PHONE]` | `(?:\+?91[\s-]?)?\b[6-9]\d{4}[\s-]?\d{5}\b` |
    | 7 | `[ID]` | `\b\d{9,18}\b` |
    | 8 | `[ID]` | mixed letters and digits, 8+ chars: `\b(?=[A-Za-z0-9]*\d)(?=[A-Za-z0-9]*[A-Za-z])[A-Za-z0-9]{8,}\b` |

  - **AC:** each pattern has positive tests plus **near-miss tests that must not be redacted**: `5 star`, `Nifty 50`, `₹1,00,000`, `2026`, `v12.4.1`, `10x`, `24/7`, `T+1`. Over-redaction is acceptable only in pattern 8 (such reviews simply drop out of quote candidates).

- [x] **T2.2 Normalisation helpers**
  - File: `src/pulse/ingest/normalize.py`
  - `detect_script(text)` (share of Latin vs. Devanagari letters; `latin` if ≥80% Latin), `word_count(text)`, `has_redaction(text)`.
  - Stored text is the scrubbed text **as written**: no lowercasing, no spelling fixes, no emoji removal. Whitespace normalisation happens only at comparison time (T5.4).
  - **AC:** unit tests for English, Hinglish, Devanagari, emoji-only and mixed reviews.

- [x] **T2.3 Source interface and ID hashing**
  - File: `src/pulse/ingest/base.py`
  - `class ReviewSource(Protocol): name: str; def fetch(self, since: datetime) -> Iterator[RawReview]`
  - `hash_review_id(store, store_review_id) = sha256(PULSE_ID_SALT + store + store_review_id).hexdigest()`; the store's own ID is never persisted.
  - **AC:** same input gives the same hash; a different salt gives a different hash.

- [x] **T2.4 Google Play adapter**
  - File: `src/pulse/ingest/play_store.py`, `tests/test_play_store.py`
  - Call `google_play_scraper.reviews(package, lang="en", country="in", sort=Sort.NEWEST, count=200, continuation_token=token)` in a loop.
  - Map **only**: `content → text`, `score → rating`, `at → review_date` (convert using the timezone finding from T0.3), `reviewCreatedVersion` (fallback `appVersion`) → `app_version`, `thumbsUpCount → thumbs_up`, `reviewId → store_review_id`. Ignore `userName`, `userImage`, `replyContent`, `repliedAt`.
  - Stop when a page's oldest review is older than `since`, when there's no continuation token, or at `max_reviews`. Sleep 1–2 s between pages; exponential backoff on errors (3 tries).
  - **AC:** fixture test with **synthetic** responses (fake names in the fixture prove they are dropped); a `@pytest.mark.live` test fetches one page for real and asserts no identity fields survive into `RawReview`.

- [x] **T2.5 CSV import adapter**
  - File: `src/pulse/ingest/csv_import.py`
  - Column mapping from config. Refuses to run if a mapped column name matches `name|user|author|email|device|phone` (case-insensitive).
  - **AC:** tests for a valid mapping and for refusal on an identity column.

- [x] **T2.6 Ingest service and `pulse ingest`**
  - File: `src/pulse/ingest/service.py`, CLI wiring
  - For each enabled source: fetch since window start → scrub → normalise → **filter** (more than 8 words, no emoji, English only; `sources.filters`) → hash ID → upsert. Stored reviews that fail the current filters are pruned. Returns counts: fetched, filtered by reason, new, duplicates, pruned, redactions by type.
  - **AC:** running `pulse ingest` twice in a row reports 0 new on the second run; a scan of every `reviews.text` in the DB with `scan()` returns no hits; manifest records counts.

**✅ Milestone M1 exit:** the last 12 weeks of Groww reviews are in `data/pulse.db`; counts match the T0.3 spike within ±5%; privacy scan is clean.

---

## 6. Phase 3 — LLM Client (≈1 day)

### Data this phase is sized for (`data/pulse.db`, 17 Oct 2026)

After the ingest filters (more than 8 words, no emoji, English only), the 12-week window ending `2026-W41` holds:

| Measure | Value | What it means for Phase 3 |
|---|---|---|
| Reviews in window | **1,534** (72–202 a week, mean 128) | First classification run: about 31 chunks of 50. Weekly runs: about 130 new reviews, so 3 chunks |
| Review length | mean 28 words / 157 chars; p90 61 words; max 101 words / 500 chars (Play Store cap) | About 50 input tokens per review; a 50-review chunk is about 2.6k tokens. No truncation needed |
| Whole-window input | about 80k tokens of review text | The whole first run is small. Batching matters little (below) |
| Ratings | 1★ 42%, 2★ 6%, 3★ 9%, 4★ 9%, 5★ 34% | The filters removed mostly short 5★ reviews, so the kept set skews negative. Note for the P4 sampler and prompts: don't assume most reviews are praise |
| Reviews with redactions | 4 | `[PHONE]`-style tokens rarely appear in prompts; the prompt still explains them |
| Quote-length (9–60 words) | 1,378 (90%) | Plenty of quote candidates for P5 |

**Cost estimate** (Claude Opus 5.5: $4 / $20 per MTok, cache reads $0.20/MTok; system prompt and taxonomy cached; effort `low`): the first full classification is about 1.6k reviews × (50 input + about 40 output and thinking tokens) ≈ **$1.5–2.5**; a weekly run is **about $0.20–0.40**. The other three stages (discover, quotes, compose) are a few calls each, under $1 per week. The Batches API would save about $1 once, on the first run, and nothing on weekly runs, because 130 reviews is below `batch_threshold: 200`. **So batching is deferred (T3.3) and every call is synchronous.** That also removes the batch-resume logic from T8.1.

**Prerequisite:** `ANTHROPIC_API_KEY` in `.env` (or an `ant auth login` profile) before T3.4 and T3.7. Everything else runs offline against the fake client.

- [x] **T3.1 Structured-call wrapper**
  - File: `src/pulse/llm/client.py`
  - `call_structured(stage: str, system: str, user: str, schema: type[BaseModel]) -> tuple[BaseModel, Usage]`
  - Reads `model`, `effort`, `max_tokens` from `llm.<stage>`. Sends `output_config = {"effort": ..., "format": {"type": "json_schema", "schema": <strict schema>}}`. **Always set `effort` explicitly:** Opus 5.5 defaults to `medium`, and the plan uses `low` for classification and `high` for discovery and compose.
  - Enables server-side refusal fallback: `client.beta.messages.create(..., betas=["server-side-fallback-2026-07-01"], fallbacks="default")`. It routes by refusal category, so no fallback model list to maintain.
  - Before reading content, check `stop_reason`: `refusal` → raise `LLMRefusal(category)` from `stop_details` (only reached when the fallback also refused); `max_tokens` → retry once with double `max_tokens`, then raise `LLMTruncated`.
  - Parse the first `text` block with `schema.model_validate_json` (skip `thinking` and `fallback` blocks). On a validation error, retry once with the error appended to the user turn.
  - Don't send `thinking`, `temperature`/`top_p` or a prefill: on Opus 5.5 thinking can't be disabled, sampling parameters and prefills return 400, and `effort` is the control.
  - `max_tokens`: 4,000 for classify (50 labels of about 30 tokens each plus low-effort thinking, with headroom); 16,000 for the other stages (the non-streaming default).
  - Errors: rely on the SDK's built-in retries for 429/5xx/connection errors (`max_retries=3`); catch `anthropic.RateLimitError` → `APIStatusError` → `APIConnectionError` in that order and record the failure in the manifest.
  - **AC:** unit tests with a mocked SDK client for success, refusal, truncation, invalid JSON and a fallback response (one where a `fallback` block precedes the text).

- [x] **T3.2 Strict schema helper** _(stage output models with their validators are in `src/pulse/llm/outputs.py`)_
  - File: `src/pulse/llm/schema.py`
  - Convert a Pydantic model's JSON schema into a structured-output schema: `additionalProperties: false` on every object, all properties `required`, unsupported keywords stripped. **Count limits (≤5 themes, exactly 3 picks or actions, one label per review) are enforced in Pydantic validators, not trusted to the JSON schema.**
  - **AC:** a test per stage schema checks the transformed output.

- [ ] **T3.3 Batch runner** _(deferred: optional, build only if volume grows)_
  - At 1,534 reviews per window and about 130 a week, Batches save about $1 once and add hours of latency plus resume logic. Revisit if weekly classifiable volume passes `batch_threshold` (200) for several weeks, or if the filters are loosened.
  - When built: `run_batch(stage, items, schema) -> BatchOutcome(succeeded, failed)` via `client.messages.batches.create` / `retrieve` / `results`, results keyed by `custom_id`, `batch_id` written to the manifest immediately for resume. Server-side fallbacks are rejected on Batches, so refused or failed items are re-run through T3.1.
  - Config now: `llm.classify.use_batches: false`.

- [x] **T3.4 Prompt caching and usage tracking** _(live check: second call read 1,565 cached tokens; Docs/spikes.md)_
  - Mark the system block (instructions + taxonomy) with `cache_control: {"type": "ephemeral"}` so it's a stable prefix. No timestamps, run IDs or unsorted JSON in system prompts.
  - Opus 5.5 caches prefixes of **512 tokens or more**. The classify system prompt (rules + 5 themes with definitions and includes/excludes) will be about 1–2k tokens, so it caches. Weekly runs make 3 chunk calls in quick succession, well inside the 5-minute TTL.
  - Record `input_tokens`, `output_tokens`, `cache_read_input_tokens`, `cache_creation_input_tokens` per stage in the manifest, plus an estimated cost per stage at the prices above.
  - **AC:** in a live run of ≥2 synchronous classification calls, the second shows `cache_read_input_tokens > 0`.

- [x] **T3.5 Prompt files**
  - Files: `prompts/discover.md`, `prompts/classify.md`, `prompts/quotes.md`, `prompts/compose.md`, `src/pulse/llm/prompts.py`
  - Each file starts with a `version:` line; the loader returns `(text, version)` and the version is recorded in the manifest and in `classifications.prompt_version`.
  - **Untrusted input rule (all prompts):** review texts are wrapped as `<review id="r12" rating="1">…</review>`, and the system prompt says reviews are data to analyse, never instructions to follow.
  - Prompts state what the data looks like, so the model isn't surprised by it: English Play Store reviews of an investing app, 9–100 words, often informal or misspelt ("brokrage", "custmer"), with personal data replaced by tokens such as `[PHONE]` and `[ID]`, and skewed towards complaints.
  - **AC:** loader test; every prompt contains the untrusted-input instruction.

- [x] **T3.6 Fake LLM for tests** _(the full pipeline doesn't exist yet; every stage's call runs offline against the fake)_
  - File: `src/pulse/llm/fake.py`
  - Selected with `PULSE_LLM=fake`; returns canned responses per stage from `tests/fixtures/llm/*.json`.
  - Fixtures use **synthetic** reviews modelled on the real profile (length, rating mix, typos), never text copied from `pulse.db`.
  - **AC:** the full pipeline runs offline in tests.

- [x] **T3.7 Live smoke test and token baseline** _(about 64 input and 37 output tokens per review; first run about $1.54, weekly about $0.13; Batches stay deferred)_
  - File: `spikes/llm_smoke.py` (throwaway)
  - Use `client.messages.count_tokens` on all 1,534 window reviews, formatted as classify user turns, to replace the 4-characters-per-token estimate above.
  - Send 2 chunks of 50 real reviews from `pulse.db` through `call_structured` with a provisional classify prompt and a placeholder 5-theme taxonomy (the real one comes from T4.2). Measure latency, output and thinking tokens per review at effort `low`, and cache hits on the second call.
  - Record the results and a revised cost estimate in `Docs/spikes.md`. Keep `chunk_size: 50` unless a chunk drops or duplicates labels; then try 25.
  - **AC:** `Docs/spikes.md` has measured tokens per review, cost per chunk, a confirmed cache hit, and a go/no-go for keeping Batches deferred.

**Config changes in this phase:** `llm.classify.use_batches: false`; add `max_tokens: 4000` to `llm.classify`. Leave `batch_threshold` and `chunk_size` (50) as they are.

---

## 7. Phase 4 — Theming (≈2 days)

- [x] **T4.1 Discovery input and coverage sample**
  - File: `src/pulse/analysis/taxonomy.py`
  - **Discovery reads the whole window, not a sample.** After the ingest filters, the window is about 1,534 reviews, about 80k tokens, which fits easily in one request. A sample would only lose rare themes. Fall back to a stratified sample of about 800 only if the window ever exceeds about 3,000 reviews.
  - Separately, build a **coverage sample** of 200 reviews, stratified by rating × week, with a fixed seed. The ratings are skewed (1★ 42%, 5★ 34%, 2★/3★/4★ only 6–9% each), so take at least 15 per star rating and fill the rest proportionally. This sample is used for the coverage check in T4.2 and the hand-labelling in T4.6.
  - No word-count or script filtering here: every stored review already passed the ingest filters.
  - **AC:** the coverage sample has ≥15 reviews per star rating and is otherwise within ±5 points of the window's rating mix; same seed → same sample.

- [x] **T4.2 Taxonomy discovery — `pulse themes discover`** _(live: v1 from 1,534 reviews, 9% "other" on the coverage sample, $0.71)_
  - Prompt `prompts/discover.md`: propose at most 5 **mutually exclusive** themes that together cover most reviews; product-specific to an investing app; no "Other" theme (the code adds it). It's one synchronous `call_structured("discover", …)` over all window reviews at effort `high` (about $0.5), reviews wrapped as `<review id="r1" rating="1">…</review>`.
  - Output schema:
    ```python
    class ProposedTheme(BaseModel):
        id: str             # snake_case, e.g. "kyc_onboarding"
        name: str           # ≤ 4 words, e.g. "KYC & Onboarding"
        definition: str     # one sentence
        includes: list[str] # topics that belong here
        excludes: list[str] # near-misses that belong elsewhere
    class TaxonomyProposal(BaseModel):
        themes: list[ProposedTheme]   # validator: 1 ≤ len ≤ 5, unique ids, no "other"
    ```
  - Save to `config/themes.yaml` as the next version (`v1`, `v2`, …) with `approved: false`, and to the `taxonomies` table.
  - Then classify the 200-review coverage sample (4 chunk calls) and print each theme's share and the "other" share, so the reviewer can judge coverage before approving.
  - **AC:** command prints the proposal and sample distribution; validator rejects 6 themes.

- [x] **T4.3 Taxonomy approval — `pulse themes approve <version>` and `pulse themes list`** _(v1 approved by the owner, 17 Oct 2026; `pulse classify` and `pulse compose` refuse without one until `pulse run` exists in P8)_
  - Exactly one approved version is active; approving a new one deactivates the old one. Changing the taxonomy means earlier classifications don't count for the new version (they're keyed by version).
  - **AC:** `pulse run` refuses to start without an approved taxonomy and says how to create one.

- [x] **T4.4 Classification prompt and schema**
  - System prompt (cached): rules + approved taxonomy (ids, names, definitions, includes/excludes) + the `"other"` rule + the `mentions_person` rule ("true if the review names or clearly identifies a specific person, including support agents").
  - User turn: up to `chunk_size` (50) reviews as `<review id="r1" rating="2">…</review>`, about 2.6k tokens. Short local IDs (`r1…r50`) are mapped back to `review_id` in code; hashes never go to the model.
  - The system prompt must be ≥512 tokens to cache on Opus 5.5 (T3.4). Rules plus 5 themes with includes/excludes will exceed that.
  - Output:
    ```python
    class ReviewLabel(BaseModel):
        rid: str
        theme_id: str        # a taxonomy id or "other"
        sentiment: Literal["positive", "neutral", "negative"]
        mentions_person: bool
    class LabelBatch(BaseModel):
        items: list[ReviewLabel]
    ```
  - **AC:** prompt renders deterministically (same inputs → identical bytes, which keeps the cache valid).

- [x] **T4.5 Classifier service** _(live: 1,534 classified, 0 unclassified, 5.8% "other", $1.48)_
  - File: `src/pulse/analysis/classifier.py`
  - Steps:
    1. Select unclassified reviews in the window for the active taxonomy. Every stored review is eligible, because the ingest filters already removed short ones; `min_words_to_classify` stays as a safety net. No per-week cap: about 130 a week.
    2. Chunk them (50 per chunk) and classify each chunk **synchronously** through `call_structured("classify", …)`. Batching is deferred (T3.3). The first run is about 31 calls; a weekly run is about 3. Run the chunks one after another so each call reuses the cached system prompt. **Save each chunk's results as soon as it returns**, so an interrupted run resumes with only the unfinished chunks.
    3. Validate each chunk: every `rid` exactly once, `theme_id` valid. Missing or invalid → one synchronous retry of just those reviews → still bad → `status = unclassified`.
    4. Store results with model, prompt version and taxonomy version.
  - **AC:** a second run classifies 0 reviews; manifest records counts by theme, the "other" share, token usage and estimated cost, and a `taxonomy_review_needed` flag when "other" > `other_share_alert`.

- [ ] **T4.6 Classification quality check** _(tooling done; `data/labelled_sample.jsonl` exported with 100 reviews and is waiting for hand labels, then `pulse eval classify`)_
  - Files: `data/labelled_sample.jsonl`, `src/pulse/eval.py`, CLI `pulse eval classify`
  - Hand-label 100 reviews from the T4.1 coverage sample against the approved taxonomy (scrubbed text + label). This file contains real, scrubbed review text: keep it under `data/` (gitignored), not `tests/fixtures/`, unless it's replaced with synthetic look-alikes.
  - The command classifies them and reports agreement overall and per theme, plus a confusion table.
  - **AC:** ≥80% agreement before M2 is signed off. If it's lower, refine the definitions/excludes (new taxonomy version) or the prompt, and re-run.

**✅ Milestone M2 exit:** approved taxonomy with ≤5 themes; all eligible reviews in the window classified; "other" share recorded; quality check ≥80%.

---

## 8. Phase 5 — Aggregation and Quotes (≈1 day)

- [x] **T5.1 Aggregator**
  - File: `src/pulse/analysis/aggregate.py`, `tests/test_aggregate.py`
  - Definitions (write them in the module docstring so the numbers are explainable):
    - `classified_n` = reviews with `status = ok` in the window (including "other")
    - `share` = theme count ÷ `classified_n` (themes + "other" = 100%)
    - `avg_rating`, `pct_negative` per theme
    - `weekly_avg` = theme count ÷ `window.weeks`; `latest_week_count` = count in the pulse week
    - Trend: `latest ÷ weekly_avg ≥ 1.2` → ▲, `≤ 0.8` → ▼, otherwise ▬; no marker if `latest_week_count < min_trend_count` (**default 10**: at about 130 reviews a week across 5 themes, a theme gets roughly 10–50 a week, so 20 would hide most trends)
    - Overall: reviews in the window (these are the **filtered** reviews: English, more than 8 words, no emoji; short reviews are no longer stored), their average rating, `classified_n`, date range. The average is of the filtered set (about 2.9★ for W30–W41), not the store rating, and the pulse header must say so.
  - Ranking: count descending, then lower average rating first (more pressing). `ranking_scope = latest_week` ranks on the latest week's counts instead.
  - Writes `runs/<week>/stats.json`.
  - **AC:** tests with synthetic data cover ties, a theme with zero latest-week reviews, trend suppression and both ranking scopes.

- [x] **T5.2 Quote candidate pool**
  - File: `src/pulse/analysis/quotes.py`
  - Per top-3 theme, select reviews where: theme matches, `status = ok`, at most 60 words (every stored review already has more than 8 and is English), `has_redaction = false`, `mentions_person = false`, and sentiment equals the theme's majority sentiment. About 90% of stored reviews are quote-length, so pools will be full.
  - Order: latest week first, then `thumbs_up` descending, then closeness to 25 words. Take up to 15.
  - **AC:** test that redacted or person-mentioning reviews never appear; a theme with too few latest-week candidates falls back to the full window.

- [x] **T5.3 Quote selection call**
  - Prompt `prompts/quotes.md`: pick, for each theme, the single most representative candidate; return a **contiguous, unedited excerpt** of ≤ `quote_max_words` words that starts and ends at a sentence or clause boundary; don't fix spelling or punctuation; three different reviews.
  - Candidates are labelled `c1…c15` per theme; mapped back in code.
  - Output:
    ```python
    class QuoteChoice(BaseModel):
        theme_id: str
        cid: str
        quote: str
    class QuoteChoices(BaseModel):
        picks: list[QuoteChoice]   # validator: exactly one per top theme
    ```

- [x] **T5.4 Verbatim verifier and fallback**
  - `verify(quote, source_text)`: `collapse_whitespace(quote) in collapse_whitespace(source_text)`, ≤ `quote_max_words`, no redaction token, non-empty. Nothing else is normalised (curly vs straight quotes count as different).
  - Fallback chain per theme: next-ranked candidate's model pick → first sentence of the top candidate if it's ≤ `quote_max_words` → next candidate's first sentence. The rendered quote text is always taken from **the stored review**, using the verified span.
  - **AC:** tests: exact match passes; changed word, fixed typo, added ellipsis, or swapped apostrophe style fails; fallback produces a quote when the model's pick fails; three quotes come from three different reviews.

---

## 9. Phase 6 — Compose and Guardrails (≈1.5 days)

- [x] **T6.1 Composer call**
  - File: `src/pulse/compose/composer.py`, `prompts/compose.md`
  - Input: overall stats, top-3 theme stats, the 3 verified quotes, about 10 sample review snippets per top theme. One synchronous `call_structured("compose", …)` at effort `high`.
  - Rules in the prompt: takeaways ≤12 words, plain language; actions start with a verb, are specific enough that a team could own them this quarter, ≤20 words, each tied to one top theme; no numbers beyond those supplied; no quotes (code inserts them).
  - Output:
    ```python
    class Takeaway(BaseModel):
        theme_id: str
        text: str
    class Action(BaseModel):
        theme_id: str
        text: str
    class ComposerOutput(BaseModel):
        takeaways: list[Takeaway]   # validator: one per top theme
        actions: list[Action]       # validator: exactly 3
    ```

- [x] **T6.2 Templates**
  - Files: `src/pulse/compose/templates/pulse.md.j2` (Doc), `pulse.txt.j2` (email)
  - Layout from PS §9: title line, source line (`1,534 English Play Store reviews (9+ words), 20 Jul – 11 Oct 2026 | Avg 2.9★`), TOP THEMES (name, share, trend, takeaway), WHAT USERS SAID (quote, rating, theme), ACTION IDEAS. The source line names the filter so readers don't compare the average with the store's overall rating.
  - **AC:** rendering is deterministic (same inputs → identical bytes).

- [x] **T6.3 Word counter**
  - File: `src/pulse/validate/words.py`
  - Definition: split the rendered **plain-text note** on whitespace and count tokens containing at least one letter or digit (so `•`, `—`, `★` alone don't count). The email's `Full pulse: <url>` line is excluded because it's not part of the note.
  - **AC:** tests on sample strings, including bullets, emoji and hyphenated words.

- [x] **T6.4 Guardrails**
  - File: `src/pulse/validate/guardrails.py`
  - `validate(pulse, stats, taxonomy, store) -> ValidationReport(passed: bool, failures: list[str])`, checking every row of arch §4.7: structure 3/3/3; taxonomy ≤5 and themes ⊆ taxonomy; verbatim (re-checked against the DB); actions grounded in top-3 themes; ≤ `max_words`; `scrubber.scan()` finds nothing in the rendered Markdown and plain text.
  - Writes `runs/<week>/validation.json`.
  - **AC:** one failing test per check, and a passing golden case.

- [x] **T6.5 Re-compose loop**
  - On failure: one composer retry with the failure list appended. Failures in quotes or stats aren't sent back to the composer (it doesn't control them); they fail the run directly.
  - Still failing → run status `failed_validation`, exit code 2, no delivery.
  - **AC:** test with a fake composer that first exceeds the word limit and then complies.

- [ ] **T6.6 Format review with a stakeholder** _(`runs/2026-W41/pulse.md` is ready to share: 201 words, guardrails green; built with `pulse compose --week 2026-W41`)_
  - Produce `pulse.md` for `2026-W41` with `pulse run --week 2026-W41 --no-deliver` (or the stage commands available at this point) and share it with one person from the audience in PS §4.
  - **AC:** feedback recorded in `docs/decisions.md`; template tweaks applied while still ≤250 words.

**✅ Milestone M3 exit:** a real-week `pulse.md` that passes every guardrail and has been read by one stakeholder.

---

## 10. Phase 7 — MCP Delivery (≈2 days)

### The server this phase targets

Delivery goes through our own **`gmail-docs-mcp`** server ([repo](https://github.com/AdityaJindal2025/mcp-server)), deployed on Railway:

| | |
|---|---|
| Endpoint | `https://mcp-server-production-64cb.up.railway.app/mcp`: stateless Streamable HTTP, POST only, MCP `2026-07-28` and `2025-11-25`. `/healthz` returns `{"ok":true}` (checked 18 Oct 2026). |
| Port | The container listens on Railway's `PORT` (8080). That port is **internal**; clients use the public HTTPS domain on 443 (8080 isn't reachable from outside). |
| Auth | `Authorization: Bearer <MCP_HTTP_BEARER_TOKEN>`; requests without it get 401. The pipeline reads the token from `PULSE_MCP_TOKEN`. The server acts as **one Google account** (signed in once with `auth`; its token lives in Railway's `GOOGLE_TOKEN_JSON`), so the bearer token is as sensitive as that mailbox's send permission. |

Its tools (from the README's generated tool reference) and how the pipeline uses them:

| Tool | Inputs used | Output (`structuredContent`) | Pipeline use |
|---|---|---|---|
| `gdocs_get_document_info` | `document` (URL or ID), `tail_chars` (≤5000) | `document_id`, `title`, `url`, `revision_id`, `tabs`, `end_index`, `tail_text` | Check access to the pulse Doc, get its URL, and see if this week's section is already at the end |
| `gdocs_append_text` | `document`, `text`, `paragraph_style` (`NORMAL_TEXT`/`TITLE`/`HEADING_1–3`), `start_on_new_line`, `client_request_id` | `document_id`, `url`, `start_index`, `end_index`, `revision_id`, … | Append the week's heading, source line, quotes and actions |
| `gdocs_append_table` | `document`, `rows` (first row is the header, bold), `client_request_id` | `row_count`, `column_count`, `start_index`, `end_index`, `revision_id`, … | Append the Top themes table (Theme, Share, Trend, Takeaway) |
| `gmail_create_draft` | `to` (array), `subject`, `body`, `body_format: "text"`, `client_request_id` | `draft_id`, `message_id` | The weekly draft. **Only registered when the server runs with `ENABLE_DRAFTS=true`** (needs the restricted `gmail.compose` scope and a fresh `auth`) |
| `gmail_send_email` | n/a | n/a | **Never called.** It sends immediately; the client refuses any tool whose name contains `send` |

What this changes from the original design:

- **No Doc creation and no replace.** The Docs tools are append-only, so the pulse lives in **one running Google Doc**, created by hand once and shared with the server's Google account. Each week **appends a new section**; earlier weeks are never edited. This replaces "one Doc per week, re-run replaces its content" (arch §2 Q5, §4.8).
- **Idempotency** comes from three things: the delivery state stored in `runs` (`doc_section_end_index`, `draft_id`); a check of the Doc's tail for this week's heading; and a deterministic `client_request_id` on every write, which the server de-duplicates for 24 hours.
- **No ID extractors needed.** The server returns IDs and URLs in `structuredContent`; errors come back as `isError` with a fix hint (for example "Not signed in to Google … run auth").
- **Server-side guardrails that must be configured on Railway:** `EMAIL_ALLOWED_DOMAINS` must include the recipient's domain, and `DOCS_ALLOWED_IDS` must include the pulse Doc's ID.

**Prerequisites (owner, before T7.0):**

1. Railway variables: `ENABLE_DRAFTS=true`, then run `auth` again locally (tick `gmail.compose`) and update `GOOGLE_TOKEN_JSON`. Also set `EMAIL_ALLOWED_DOMAINS` and `DOCS_ALLOWED_IDS`.
2. Create the pulse Doc (for example "Groww Review Pulse"), share it as Editor with the server's Google account, and note its ID.
3. In this repo's `.env`: `PULSE_MCP_TOKEN` (the server's `MCP_HTTP_BEARER_TOKEN`), `PULSE_DOC_ID`, `PULSE_RECIPIENT`.

- [x] **T7.0 Live probe of the deployed server** (replaces the rest of T0.4)
  - File: `spikes/mcp_probe.py` (switch it to the remote URL with the bearer header)
  - `list_tools()`: save names and input schemas to `tests/fixtures/mcp/tools.json` (tool schemas only, no secrets) for the contract test in T9.5. Confirm `gmail_create_draft` is listed (drafts enabled).
  - `gdocs_get_document_info` on the pulse Doc: confirms the token, sharing and `DOCS_ALLOWED_IDS`.
  - One `gmail_create_draft` to `PULSE_RECIPIENT` with subject "Pulse probe", deleted by hand afterwards. Record the exact `structuredContent` and error shapes in `Docs/spikes.md`.
  - **AC:** `Docs/spikes.md` lists the five tools, confirms drafts are enabled and the Doc is reachable, and shows a real draft result. `tests/fixtures/mcp/tools.json` is committed.

- [x] **T7.1 MCP client wrapper**
  - Files: `src/pulse/deliver/mcp_client.py`, settings `delivery.mcp`
  - Remote Streamable HTTP with the `mcp` 2.x SDK: `http = create_mcp_http_client(headers={"Authorization": f"Bearer {token}"})`, then `streamable_http_client(url, http_client=http)` inside a session (`Client` / `ClientSession`, confirmed against the installed SDK). One connection per delivery; `list_tools()` first.
  - Settings (replace the placeholder `delivery` block and retire `config/mcp_servers.json`):
    ```yaml
    delivery:
      mcp: { url: https://mcp-server-production-64cb.up.railway.app/mcp, token: ${PULSE_MCP_TOKEN}, timeout_seconds: 60 }
      docs:
        document: ${PULSE_DOC_ID}
        title: "Groww Review Pulse — Week of {week_start:%d %b %Y}"
        tools: { info: gdocs_get_document_info, append_text: gdocs_append_text, append_table: gdocs_append_table }
      gmail:
        subject: "Groww Review Pulse — Week of {week_start:%d %b %Y}"
        tools: { create_draft: gmail_create_draft }
    ```
  - `call(logical_op, **args)` maps logical operations to tool names. Fail fast if a mapped tool is missing, with a fix hint ("gmail_create_draft missing: set ENABLE_DRAFTS=true on the server and re-run auth").
  - **Send guard:** refuse to call, or even map, any tool whose name contains `send`. The server does expose `gmail_send_email`.
  - Results: read `structuredContent`. `isError` → `DeliveryError` carrying the server's message (which never contains document or email text). HTTP 401 → "check PULSE_MCP_TOKEN"; connection error or 5xx → retryable. 60 s per call; no automatic retries of writes except an immediate retry with the **same** `client_request_id`.
  - **AC:** against the fake server (T7.6): missing tool → clear error before any write; `send` tools can't be called; 401 and `isError` map to the right errors; IDs are read from `structuredContent`.

- [x] **T7.2 Google Docs publisher (append-only running Doc)**
  - File: `src/pulse/deliver/docs.py`
  - Steps for a week:
    1. `gdocs_get_document_info(document, tail_chars=5000)` gives `url`, `title`, `tail_text`.
    2. If `runs.doc_section_end_index` is set for this week, or `tail_text` already contains this week's heading, **skip** and record `doc_already_has_week`. (`--force` instead appends a new section headed "… (updated <date>)"; the old section stays, because the server can't edit it.)
    3. Append the section in order:
       - `HEADING_1`: the week's title
       - `NORMAL_TEXT`: the source line
       - `HEADING_2` "Top themes", then a table (Theme, Share, Trend, Takeaway) via `gdocs_append_table`
       - `HEADING_2` "What users said", then the quotes, one per paragraph
       - `HEADING_2` "Action ideas", then the numbered actions

       That's about 7 calls.
    4. Each part gets `client_request_id = "pulse:{week}:{sha256(section)[:12]}:{part}"`, and progress is recorded per part in the manifest. A failed delivery resumes at the first unfinished part without duplicating earlier ones (the server de-duplicates within 24 hours; the manifest covers anything later).
    5. Store `doc_id`, `doc_url`, the section's `start_index`/`end_index` and `revision_id` in `runs`.
  - The content matches `pulse.md`; only the layout differs (table for themes, Docs heading styles instead of Markdown).
  - **AC:** fake-server tests for: first delivery (call order, styles and the table), resume after a failure at part 4 (parts 1–3 not re-sent), a skip when the week is already in the Doc, and `--force` appending an "updated" section.

- [x] **T7.3 Gmail draft creator**
  - File: `src/pulse/deliver/gmail.py`
  - `gmail_create_draft(to=[PULSE_RECIPIENT], subject=…, body=pulse.txt + "\n\nFull pulse: " + doc_url, body_format="text", client_request_id="pulse:{week}:draft:{sha[:12]}")`. Store `draft_id` and `message_id` in `runs`.
  - If the run already has a `draft_id`: skip, unless `--force`.
  - Created only after the Doc section succeeds, so the link always points at content that exists.
  - The server rejects recipients outside `EMAIL_ALLOWED_DOMAINS` ("Blocked recipient(s)"): surface that as a precondition error with the fix.
  - **AC:** fake-server tests: second run makes no draft call; `--force` makes one; no `send` tool is ever called; the body ends with the Doc URL.

- [x] **T7.4 Pre-delivery privacy re-scan**
  - Run `scrubber.scan()` on **every outgoing payload**: each appended text, every table cell, the draft subject and body. Any hit blocks the whole delivery before the first write.
  - **AC:** a test that injects a phone number into one table cell blocks delivery, with zero calls made.

- [x] **T7.5 Dry-run mode**
  - `--dry-run` writes `runs/<week>/delivery_payloads.json` (the ordered tool calls and arguments, with `PULSE_RECIPIENT` and the Doc ID masked) and makes **no** MCP calls. The server's own `dry_run` exists only for `gmail_send_email`, which is never used, so dry-run is client-side.
  - **AC:** the payload file matches, call for call, what the fake server receives in a real-mode test.

- [x] **T7.6 Fake MCP server for tests**
  - File: `tests/fakes/fake_gmail_docs_server.py`, built with the `mcp` SDK's server API (`MCPServer`).
  - Mirrors the real contract: the five tool names and input schemas from `tests/fixtures/mcp/tools.json` (T7.0), and `structuredContent` shaped like the README. Includes `gmail_send_email`, so the send guard is tested. Keeps an in-memory Doc (appended text and tables), honours `client_request_id` de-duplication, records every call, and can inject failures (`isError`, 401, timeout) at a chosen call.
  - Tests connect in-process (`Client(server)`). One test also goes through the HTTP transport with a bearer header.
  - **AC:** used by T7.1–T7.5 tests; a contract test asserts the fake's tool schemas equal `tools.json`.

- [x] **T7.7 Live delivery check**
  - Against the Railway server: deliver the M3 pulse for `2026-W41`.
  - **AC:** the pulse Doc ends with the W41 section (heading styles and themes table correct, content matching `pulse.md`); a draft is in Gmail Drafts with the right subject, body and Doc link, and nothing was sent; IDs are stored in `runs`; a re-run appends nothing and creates no second draft.

---

## 11. Phase 8 — Orchestration and CLI (≈1 day)

- [x] **T8.1 Stage runner**
  - File: `src/pulse/pipeline.py`
  - Stages in order: `ingest → classify → aggregate → quotes → compose → validate → deliver`. Each is a function `(RunContext) -> StageResult` and records its status in the manifest. Reuse the stage functions already in `src/pulse/stages.py` (`classify_window`, `build_pulse`) and the ingest service; `deliver` is the T7.2 + T7.3 pair (Doc section first, then draft).
  - Resume: `pulse run` skips stages already `done` for that week; `--from <stage>` resets that stage and later ones.
  - Lock file `runs/<week>/.lock` prevents two runs of the same week at once.
  - **AC:** kill the process during `classify` after some chunks are saved; re-run, and it classifies only the remaining reviews (no chunk is sent twice) and completes.

- [x] **T8.2 CLI commands**
  - File: `src/pulse/cli.py` (Typer)
  - Already built: `ingest`, `themes discover | approve | list`, `classify`, `compose`, `eval export | classify`. Add `run`, `deliver`, `show`, as in arch §7. `--week` defaults to `last_completed_week(now)`.
  - `pulse deliver --week …` retries delivery only (for example after fixing the token or re-running `auth` on the server). It resumes at the first unfinished Doc part, then creates the draft if missing.
  - Flags on `run`: `--dry-run`, `--no-deliver`, `--from`, `--force`. (`--no-batch` is dropped while batching is deferred; add it back with T3.3.)
  - **AC:** `--help` text for every command; CLI tests via Typer's test runner.

- [x] **T8.3 Exit codes and preconditions**
  - Exit codes: `0` delivered / done, `1` unexpected error, `2` validation failed, `3` delivery failed, `4` precondition failed.
  - Preconditions checked before any work:
    - `PULSE_ID_SALT`; Anthropic credentials via `ANTHROPIC_API_KEY` or an `ant` profile, unless `PULSE_LLM=fake`; an approved taxonomy.
    - Unless `--no-deliver` or `--dry-run`: `PULSE_MCP_TOKEN`, `PULSE_DOC_ID` and `PULSE_RECIPIENT` set; `GET /healthz` OK; `list_tools()` shows every mapped tool, including `gmail_create_draft`. The precondition check reads only; nothing is written.
  - **AC:** each precondition failure exits with code 4 and a one-line fix hint.

- [x] **T8.4 `pulse show`**
  - Prints the rendered pulse, stage table, counts, token usage and estimated cost, the Doc URL (and the week's section position) and the draft ID for a week.

**✅ Milestone M4 exit:** `pulse run --week 2026-W41` from a clean checkout (after `themes approve`) ingests, classifies, composes, validates, appends the W41 section to the pulse Doc and creates a Gmail draft; re-running is a complete no-op (no second section, no second draft).

---

## 12. Phase 9 — Quality (≈1 day)

- [x] **T9.1 Golden end-to-end test**
  - `tests/fixtures/reviews_synthetic.jsonl` (~60 hand-written reviews across 5 themes, no real people) + fake LLM responses + fake MCP server. The fixtures must pass the ingest filters (English, more than 8 words, no emoji), or ingest drops them; add a few that fail on purpose, to check they never reach the pulse. Mirror the real rating skew (mostly 1★ and 5★).
  - Asserts byte-for-byte equality of `pulse.md`, `pulse.txt` and `delivery_payloads.json` (the ordered append and draft calls) with checked-in golden files, and that the fake server's in-memory Doc ends with exactly one W41 section after two runs.

- [x] **T9.2 Privacy end-to-end test**
  - Seed fixtures with fake emails, phones, PAN- and Aadhaar-shaped strings, UPI handles, and a review naming a support agent.
  - After a full run, assert zero `scan()` hits in: the DB, every file under `runs/`, the delivery payloads (every Doc append, table cell and draft field the fake server received), and captured logs. Assert the person-naming review is never quoted.
  - The pipeline's own 64-character hashed review IDs (in `pulse.json`) match the mixed-ID pattern by design; the test allow-lists exactly those values and nothing else.

- [x] **T9.3 Static checks and coverage**
  - `ruff`, `mypy --strict` on `src/`; coverage ≥85% for non-I/O modules (`privacy`, `analysis`, `compose`, `validate`, `timeutil`).

- [x] **T9.4 Cost and runtime baseline**
  - From a live run's manifest, record tokens and estimated cost per stage and total wall-clock in `Docs/spikes.md`, and compare with the T3.7 estimate (first run about $1.5–2.5, weekly about $0.20–0.40 for classification). Use this to decide whether building the batch runner (T3.3) or testing a cheaper classifier model is worth it (the latter only if `pulse eval classify` stays ≥80%).

- [x] **T9.5 MCP contract check**
  - Offline: the fake server's tool names and input schemas equal `tests/fixtures/mcp/tools.json`.
  - `@pytest.mark.live`: `list_tools()` on the Railway server equals the same file. A change in the server repo (renamed tool, new required field) fails here before it fails a Monday run. The server's own README treats such changes as breaking.

**AC for the phase:** all of the above green in one `uv run pytest` run (live tests excluded by default; `-m live` runs them).

---

## 13. Phase 10 — Ops and First Live Run (≈1 day)

- [ ] **T10.1 Weekly schedule (macOS `launchd`)**
  - Files: `scripts/weekly.sh`, `ops/com.groww.pulse.plist`
  - `weekly.sh`: `cd` to the repo, load `.env`, run `uv run pulse run`, append output to `~/Library/Logs/groww-pulse/<week>.log`.
  - `launchd` `StartCalendarInterval`: Monday 09:00 local time (the Mac is on IST). If the Mac is asleep at that time, `launchd` runs the job on wake. The pipeline runs on the Mac (database, Claude calls); only delivery goes to the always-on Railway server, so no Google tokens live on the Mac.
  - Re-check the 24-hour Play Store publication lag (Docs/spikes.md) on a real Monday run; if Sunday reviews are missing, move the schedule to Tuesday 09:00.
  - **AC:** `launchctl kickstart` triggers a run that delivers (or no-ops for an already-delivered week); log file written.

- [ ] **T10.2 README and runbook**
  - Setup (uv, env, first `themes discover` / `approve`; the pulse Doc and its sharing; `.env` values `PULSE_MCP_TOKEN`, `PULSE_DOC_ID`, `PULSE_RECIPIENT`; server variables `ENABLE_DRAFTS`, `EMAIL_ALLOWED_DOMAINS`, `DOCS_ALLOWED_IDS`), daily-use commands, and a runbook:
    - rate-limited source;
    - Claude rate limit or refusal (re-run `pulse run`; classification resumes from saved chunks);
    - guardrail failure;
    - **server errors**: 401 (rotate the bearer token in both places), "Not signed in to Google" / `invalid_grant` (run `auth` locally, update `GOOGLE_TOKEN_JSON` on Railway; with an External consent screen in Testing mode this happens **every 7 days**), "does not have edit access" (share the Doc), "Blocked recipient(s)" (`EMAIL_ALLOWED_DOMAINS`), `gmail_create_draft` missing (`ENABLE_DRAFTS`), server down (check Railway; then `pulse deliver --week …`);
    - a wrong section appended (fix it by hand in the Doc; the server can't edit);
    - "other" share alert (`themes discover` → review → `approve`);
    - changing the ingest filters (`sources.filters`; the next ingest prunes or re-adds reviews).

- [ ] **T10.3 First live pulse**
  - Run for the last completed week. Review the new Doc section and the draft by hand against the checklist in §16, then send the draft yourself.
  - **AC:** every item in §16 is ticked.

---

## 14. Refinements to the Architecture

Decisions made while planning that refine arch.md. Fold them into architecture.md when convenient.

| # | Architecture says | Plan does | Why |
|---|---|---|---|
| 1 | `messages.parse()` for synchronous calls (arch §5) | `client.beta.messages.create` with `output_config.format` JSON schema + Pydantic validation (T3.1) | Refusal fallbacks (`fallbacks: "default"`) need the beta messages namespace; the same request shape works in a batch if T3.3 is ever built |
| 2 | "The schema caps the list at 5" (arch §4.4a) | Caps and exact counts enforced by Pydantic validators (T3.2) | Don't rely on JSON-schema count keywords being enforced |
| 3 | Classification via Message Batches (arch §5) | **Batches deferred; all calls synchronous** (T3.3, T4.5) | After the ingest filters, about 130 reviews a week (below `batch_threshold: 200`); Batches would save about $1 once and add latency and resume logic |
| 4 | Reviews keyed by `review_id` | Short local IDs (`r1…`, `c1…`) in prompts, mapped back in code (T4.4, T5.3) | Fewer tokens; hashes never sent to the model |
| 5 | `reviews.lang` | `reviews.script` (latin / devanagari / other); "en" and "hinglish" both map to `latin` (T1.4) | English vs. Hinglish can't be told apart reliably in code; script can |
| 6 | — | `classifications.status`, `prompt_version`; results saved per chunk (T1.4, T4.5) | Resume after a crash without re-sending chunks; trace which prompt produced a label |
| 8 | Taxonomy discovery on a sample of about 400 (arch §4.4a) | Discovery reads the whole filtered window; a 200-review stratified sample is used only for coverage and labelling (T4.1) | About 1,534 reviews ≈ 80k tokens fits in one call; a sample would miss rare themes |
| 7 | — | Client refuses any tool whose name contains `send` (T7.1, T7.3) | Hard guarantee that only a draft is created. Essential, because the server exposes `gmail_send_email` |
| 9 | One Doc per week; re-runs replace its content (arch §2 Q5, §4.8) | **One running Doc**, created once; each week appends a new section (heading, source line, themes table, quotes, actions) (T7.2) | `gmail-docs-mcp` can only append to existing Docs; it can't create or replace them |
| 10 | Local stdio MCP servers from `config/mcp_servers.json`; Google OAuth on the Mac (arch §4.8, §13) | One remote server over Streamable HTTP with a bearer token (`delivery.mcp`); Google OAuth lives on Railway (T7.1) | Our own server is already deployed; the pipeline holds no Google credentials at all |
| 11 | Per-server result extractors (regex or JSON path) | Read IDs and URLs from `structuredContent`; idempotency from stored state, a Doc tail check and `client_request_id` (T7.1–T7.3) | The server returns typed outputs and de-duplicates writes for 24 hours |

## 15. Risks and Mitigations

| Risk | Likelihood | Impact | Mitigation | Task |
|---|---|---|---|---|
| `google-play-scraper` breaks or is throttled | Medium | No fresh data | Backoff; continue on stored reviews with `partial_ingest` flag; CSV import fallback | T2.4, T2.5 |
| Course rules reject the scraper approach | Low–Med | Source must change | Decide before P2 (§2 item 1); adapter interface keeps the swap local | T0.5 |
| Review volume far higher than expected | Low | Cost and runtime | Ingest filters keep about 130 a week; classify only new reviews; build the deferred batch runner if weekly volume passes 200 | T2.6, T3.3, T4.5 |
| Filters leave too few reviews in a week (72 in the quietest week so far) | Medium | Thin themes, noisy trends | Rank over the 12-week window; `min_trend_count` 10; filters are configurable (`sources.filters`) | T5.1 |
| Append-only Doc: a re-run or crash duplicates part of a section, or a bad section can't be replaced | Medium | Messy Doc | Stored per-part progress, Doc tail check, `client_request_id`; `--force` appends a clearly marked "updated" section; fix by hand if needed | T7.2 |
| Drafts not enabled on the server (`gmail_create_draft` missing) | High until configured | No draft | Precondition check fails before any write, with the fix (`ENABLE_DRAFTS=true` + `auth`) | T7.0, T8.3 |
| Google refresh token expires (every 7 days with an External consent screen in Testing) | High unless Internal or published | Delivery fails | Precondition check; runbook (re-run `auth`, update `GOOGLE_TOKEN_JSON`); prefer an Internal Workspace app | T8.3, T10.2 |
| Bearer token leaks (it gives send access to the mailbox) | Low | Abuse of the mailbox | Token only in `.env`/Railway; `EMAIL_ALLOWED_DOMAINS`; rotate on suspicion; the pipeline never calls send tools | T7.1, T10.2 |
| Railway server down or redeploying | Low–Med | Delivery fails | `/healthz` precondition; pulse kept locally; `pulse deliver --week …` retry | T8.3 |
| Server contract changes (tool renamed, field required) | Low | Delivery breaks | Contract test against `tools.json`; live `list_tools()` check | T9.5 |
| Classification quality too low | Medium | Wrong themes | Hand-labelled check ≥80%; taxonomy versions; better definitions/excludes | T4.6 |
| ≤250 words too tight | Medium | Guardrail failures | Fixed word budget per section; one re-compose; template trimmed during format review | T6.1, T6.6 |
| Personal name leaks in a quote | Low | Privacy breach | `mentions_person` exclusion; final scan; privacy e2e test | T5.2, T9.2 |
| Model rewrites a quote | Medium | Invented wording | Verbatim verifier; quote text always copied from DB | T5.4 |
| LLM refusal on review content | Low | Stage fails | Server-side fallback (`fallbacks: "default"`) on every call; the chunk is marked `unclassified` if it's still refused | T3.1, T4.5 |
| Mac asleep or off on Monday | Medium | Late pulse | `launchd` runs on wake; resume-safe pipeline | T10.1 |

## 16. Definition of Done (Project)

Maps to PS §8 and §7.

- [ ] Review dataset covers the last 12 weeks (within 8–12), PII-stripped (privacy e2e test green; DB scan clean)
- [ ] Approved taxonomy of ≤5 themes; every eligible review classified; quality check ≥80%
- [ ] Weekly pulse ≤250 words with top 3 themes, 3 verbatim quotes, 3 action ideas (guardrails green)
- [ ] The week's section appended to the pulse Google Doc **via MCP** (`gmail-docs-mcp`); the link opens and the content matches `pulse.md`
- [ ] Draft (not sent) in Gmail **via MCP** to self or alias, with the note and the Doc link
- [ ] No usernames, emails, device IDs or other identifiers in any artifact: DB, `runs/`, Doc, email, logs
- [ ] No Google OAuth or REST client code and no Google credentials in this repository (`grep -ri "googleapis\|oauth" src/` returns nothing relevant); the only delivery secret is the MCP bearer token in `.env`
- [ ] Re-running a delivered week appends nothing new and creates no duplicate draft
- [ ] Weekly schedule installed and verified with a manual trigger
- [ ] README and runbook let someone new set up and run the pipeline end to end
