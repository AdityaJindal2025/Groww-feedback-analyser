# Groww Feedback Analyser — Weekly Review Pulse

## 1. Overview

Build a pipeline that turns raw app-store reviews of **Groww** into a **weekly one-page pulse** a team can scan in minutes:

- **What** users care about (themes)
- **What they actually said** (verbatim quotes)
- **What to do next** (action ideas)

The reviews are already public. The job is to **aggregate → theme → summarize → deliver**, using familiar surfaces:

- **Google Docs** for the written pulse
- **Gmail** for a draft email you can send to yourself

Both integrations go through **MCP servers**, so the project never handles Google credentials or REST wiring itself.

## 2. Product in Scope

| Item | Detail |
|---|---|
| Product | Groww: Stocks, Mutual Fund, IPO |
| Play Store listing | https://play.google.com/store/apps/details?id=com.nextbillion.groww&hl=en_IN |
| Play Store package ID | `com.nextbillion.groww` |
| Locale | `en_IN` (India) |
| Stores | Google Play Store (primary, link provided); Apple App Store (mentioned in the brief, link not yet provided) |

## 3. End-to-End Flow (Definition of Done)

```
┌──────────────────┐   ┌──────────────────┐   ┌──────────────────┐   ┌──────────────────┐   ┌──────────────────┐
│ 1. Ingest        │──▶│ 2. Clean & strip │──▶│ 3. Cluster into  │──▶│ 4. Write pulse   │──▶│ 5. Draft email   │
│ Play/App Store   │   │ PII, filter to   │   │ ≤ 5 themes, rank │   │ to Google Docs   │   │ in Gmail (MCP)   │
│ reviews (8–12 wk)│   │ date window      │   │ pick top 3       │   │ (MCP)            │   │ to self / alias  │
└──────────────────┘   └──────────────────┘   └──────────────────┘   └──────────────────┘   └──────────────────┘
```

1. **Pull** recent App Store and Play Store reviews for Groww (within the rules in §7).
2. **Cluster** them into a small set of themes and distill a one-page weekly note.
3. **Publish** that note where stakeholders can read it (Google Docs).
4. **Draft** an email to yourself (or an alias) that contains or links to the pulse (Gmail).

## 4. Who This Helps

| Audience | Why it matters to them |
|---|---|
| Product / Growth | Prioritize fixes and improvements from real signals |
| Support | Align messaging with what users are actually saying |
| Leadership | One-page health check without drowning in raw reviews |

## 5. Functional Requirements

### 5.1 Review ingestion
- Import reviews from roughly the **last 8–12 weeks**.
- Capture whatever fields the export provides, for example:
  - rating (1–5 stars)
  - title (when available; Play Store reviews often have none)
  - review text
  - date
  - store (Play / App Store), if both are used
- Drop reviewer identity fields (name, avatar, user ID, device info) at ingestion, before anything is stored or processed further (see §7, Privacy).

### 5.2 Theming / clustering
- Group reviews into **at most 5 themes**.
- Choose themes that fit Groww. Examples from the brief: onboarding, KYC, payments, statements, withdrawals. Other likely candidates for an investing app: order execution / app performance during market hours, mutual fund / SIP experience, IPO applications, customer support, charges / brokerage, UI/UX.
- Rank themes by volume (and optionally by negative-sentiment share or rating impact) so the top 3 can be selected.

### 5.3 Weekly one-page pulse
The note must contain:

| Section | Requirement |
|---|---|
| **Top 3 themes** | A subset of the ≤ 5 themes, showing what people talk about most |
| **3 user quotes** | Verbatim snippets from real reviews, no invented or paraphrased wording, anonymized |
| **3 action ideas** | Concrete next steps, each grounded in the themes / quotes |

- Length: **≤ 250 words**, scannable.

### 5.4 Delivery
- **Google Docs:** create or update the pulse document via a Docs MCP server.
- **Gmail:** create a **draft** email (not sent automatically) to yourself or an alias that contains the note or a clear link to the Doc, via a Gmail MCP server.

## 6. Integrations: MCP-First

- Use **MCP (Model Context Protocol) servers** for Google Docs and Gmail, for example to create or update the pulse document and to create the draft message.
- **Do not** build a bespoke OAuth client plus Google REST client as the primary integration path.
- Rationale: MCP servers expose tools the agent or app can call. This keeps Docs and Gmail consistent with the course tooling and avoids duplicating auth and HTTP plumbing.
- Any MCP servers or connectors the environment provides for Docs and Gmail are acceptable. The requirement is MCP-first, not "call Google APIs manually."

## 7. Key Constraints

| Constraint | Rule |
|---|---|
| **Review source** | Public review exports only. No scraping behind store logins and no automation that violates store Terms of Service. |
| **Themes** | Maximum 5 themes for clustering; the pulse highlights the top 3. |
| **Length** | Note is scannable and ≤ 250 words. |
| **Privacy / PII** | No usernames, emails, device IDs or other identifiable reviewer data in **any** artifact (data files, Doc, email, logs). Quotes must be anonymous; redact any PII that appears inside review text (e.g. phone numbers, emails, names, order/account IDs). |
| **Quote fidelity** | Quotes are verbatim; trimming to a snippet is fine, rewording is not. |

## 8. Deliverables Checklist

- [ ] Review dataset covering the last ~8–12 weeks (PII-stripped)
- [ ] Theme set (≤ 5) with reviews assigned to each
- [ ] Weekly one-page pulse (≤ 250 words) with top 3 themes, 3 verbatim quotes and 3 action ideas
- [ ] Pulse published to Google Docs via MCP
- [ ] Draft email in Gmail (via MCP) to self or alias, containing the note or a link to it

## 9. Suggested Pulse Template

```
Groww App Review Pulse — Week of <DD MMM YYYY>
Source: <N> Play Store / App Store reviews, <start date> – <end date> | Avg rating: <x.x>★

TOP THEMES
1. <Theme A> — <share>% of reviews, <one-line takeaway>
2. <Theme B> — <share>% of reviews, <one-line takeaway>
3. <Theme C> — <share>% of reviews, <one-line takeaway>

WHAT USERS SAID
• "<verbatim quote>" — <rating>★, <theme>
• "<verbatim quote>" — <rating>★, <theme>
• "<verbatim quote>" — <rating>★, <theme>

ACTION IDEAS
1. <Concrete next step tied to Theme A>
2. <Concrete next step tied to Theme B>
3. <Concrete next step tied to Theme C>
```

## 10. Open Questions / Assumptions

1. **App Store coverage:** the brief says "App Store and Play Store" but provides only the Play Store link. Should the iOS app be included, and if so, which App Store listing and region?
2. **Review source mechanism:** which "public review export" will be used? This must comply with §7: no logged-in scraping and no ToS-violating automation.
3. **Weekly vs. 8–12 week window:** is each weekly pulse built from the full rolling 8–12 week window, or from the latest week with the longer window used as a trend baseline?
4. **Email recipient:** the user's own address or a team alias?
5. **Doc strategy:** a new Google Doc each week, or one running Doc with a new section appended per week?
6. **Language:** English-only reviews, or also Hindi / Hinglish reviews (which are common for `en_IN`)?
