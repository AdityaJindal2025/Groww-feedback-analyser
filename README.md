# Groww Review Pulse

A weekly pipeline that turns public Google Play reviews of Groww into a one-page pulse (top themes, verbatim quotes, action ideas), published to Google Docs with a Gmail draft, both via MCP.

- What to build: [problemStatement.md](problemStatement.md)
- How it works: [architecture.md](architecture.md)
- Build plan and progress: [implementationPlan.md](implementationPlan.md)
- Spike findings and decisions: [Docs/spikes.md](Docs/spikes.md), [Docs/decisions.md](Docs/decisions.md)

## Setup

Requires Python 3.11+. Then create `.env` as described under [Environment](#environment).

With `uv`:

```sh
uv sync
```

Without `uv` (plain venv):

```sh
python3 -m venv .venv
.venv/bin/pip install -e . --group dev
```

### Environment

Secrets come from the environment or a gitignored `.env` in the repo root. Never commit real values.

| Variable | Needed for | Notes |
|---|---|---|
| `PULSE_ID_SALT` | `ingest` | Random string, 32+ characters, used to hash store review IDs. Generate once and **never change it**, or de-duplication across weekly runs breaks. |
| `ANTHROPIC_API_KEY` | theming, quotes, compose | Optional if you use an `ant auth login` profile instead. |
| `PULSE_RECIPIENT` | delivery | Address or alias that receives the Gmail draft. Its domain must be in the server's `EMAIL_ALLOWED_DOMAINS`. |
| `PULSE_MCP_TOKEN` | delivery | Bearer token of our `gmail-docs-mcp` server (its `MCP_HTTP_BEARER_TOKEN`). Not needed for `--dry-run`. |
| `PULSE_DOC_ID` | delivery | The running pulse Google Doc (URL or ID), shared as Editor with the server's Google account and listed in its `DOCS_ALLOWED_IDS`. |

`.env.example` template:

```sh
ANTHROPIC_API_KEY=
PULSE_RECIPIENT=
PULSE_ID_SALT=
PULSE_MCP_TOKEN=
PULSE_DOC_ID=
```

Generate the salt with:

```sh
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

## Usage

```sh
pulse --help
pulse run                 # the whole week: ingest → classify → compose → validate → deliver (resumable)
pulse run --dry-run       # everything except delivery; planned MCP calls -> runs/<week>/delivery_payloads.json
pulse run --no-deliver    # stop after validation
pulse run --from compose  # rerun from a stage (ingest, classify, aggregate, quotes, compose, validate, deliver)
pulse show                # the week's pulse, stage table, tokens and cost, Doc section and draft
pulse ingest              # fetch, scrub, filter and store reviews for the 12-week window ending last week
pulse ingest --week 2026-W41 --weeks 8
pulse themes discover     # propose up to 5 themes from the window's reviews, check coverage
pulse themes approve v1   # make a version the active taxonomy (pulse themes list shows all)
pulse classify            # label the window's not-yet-classified reviews
pulse compose             # stats, quotes, takeaways, actions, guardrails -> runs/<week>/pulse.md
pulse deliver --dry-run   # planned MCP calls -> runs/<week>/delivery_payloads.json; nothing sent
pulse deliver             # append the week's section to the pulse Doc, then create a Gmail draft
pulse eval export         # 100 reviews to hand-label in data/labelled_sample.jsonl
pulse eval classify       # agreement between Claude's labels and yours (target 80%)
```

Only English reviews with more than 8 words and no emoji are kept (`sources.filters` in `config/settings.yaml`).

Data lives in `data/pulse.db` and per-week run files in `runs/<week>/`; both are gitignored.

## Delivery

Delivery goes through our `gmail-docs-mcp` server on Railway (`delivery.mcp` in `config/settings.yaml`), over Streamable HTTP with a bearer token. The repo holds no Google credentials.

- The pulse lives in **one running Google Doc**; each week appends a section (heading, source line, themes table, quotes, actions). The server can only append, so earlier weeks are never edited.
- Then a Gmail **draft** is created with the plain-text pulse and the Doc link. Nothing is ever sent: the client refuses any tool whose name contains `send`.
- Re-running is safe: progress is saved per Doc part, the Doc's tail is checked for the week's heading, and every write carries a deterministic `client_request_id`. A failed delivery resumes at the first unfinished part. `--force` appends a new "(updated <date>)" section and a new draft.
- Every outgoing text, table cell, subject and body is re-scanned for personal data first; any hit blocks the whole delivery before the first call.
- `python spikes/mcp_probe.py list | info | draft` probes the live server (T7.0).

## Claude calls

Every LLM stage goes through `pulse.llm.client.ClaudeClient` (Claude Opus 5.5, structured JSON output, server-side refusal fallback, cached system prompt). Prompts live in `prompts/*.md`, each starting with a `version:` line.

- `PULSE_LLM=fake` swaps in an offline fake that answers from `tests/fixtures/llm/*.json` (synthetic data only).
- `python spikes/llm_smoke.py` runs a small live test on real stored reviews (needs `ANTHROPIC_API_KEY`) and prints token counts, cache hits and projected cost.

## Development

```sh
pytest                # all offline tests (fake LLM, fake MCP server)
pytest --cov          # plus coverage of privacy/analysis/compose/validate/timeutil (fails below 85%)
pytest -m live        # tests that call the real Play Store and the Railway MCP server
ruff check .
mypy
```

`pulse run` exits with `0` done, `1` unexpected error, `2` validation failed, `3` delivery failed, `4` precondition failed (with a one-line fix).

The golden test (`tests/test_golden.py`) pins `pulse.md`, `pulse.txt` and the planned MCP calls for the hand-written reviews in `tests/fixtures/reviews_synthetic.jsonl`. After an intended change, regenerate with `PULSE_UPDATE_GOLDEN=1 pytest tests/test_golden.py` and review the diff.
