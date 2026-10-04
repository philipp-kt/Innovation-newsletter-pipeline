# Newsletter Digest Pipeline

Turns a set of newsletter-subscription inboxes (plus optional RSS/podcast
feeds) into one combined daily digest, delivered as an email draft. An
agent runs the pipeline on a schedule; two scripts do the mechanical
classification and rendering so the agent only has to make judgment calls
on a short, pre-filtered shortlist.

## How it works, in short

1. Fetch today's RSS/podcast items (if any configured) and today's
   newsletter emails, per category.
2. Extract every story from each into a flat JSON list.
3. `newsletter_prefilter.py` clusters duplicates and auto-decides the
   unambiguous cases against your `taxonomy.json` (known watchlist
   companies promote automatically; zero-signal stories auto-reject).
4. The agent judges only the remaining shortlist (typically ~35–45% of
   stories) against your taxonomy's themes and rules.
5. `newsletter_render.py` de-duplicates across categories and renders
   HTML, plaintext, Markdown, and a JSON export.
6. The agent creates an email draft (never sends) and delivers the
   Markdown/JSON either as a direct attachment or to a cloud-storage
   archive folder, per your config.

## Repo layout

```
prompt/scheduled-task-prompt.md   the agent's task prompt (fill in placeholders, see below)
scripts/newsletter_common.py      shared matching/tokenizing helpers
scripts/newsletter_prefilter.py   clustering + mechanical auto-decide
scripts/newsletter_render.py      DB insert + HTML/MD/plaintext render
config/taxonomy.example.json      example watchlist/themes/rules — copy and edit
```

## Prerequisites

- An agent runtime that can run on a schedule and has, or can connect to:
  - an **email connector** that can search your inbox and create drafts
    (must support creating a draft without sending)
  - a **cloud-storage connector** (Drive or equivalent) to hold
    `taxonomy.json` and, optionally, archive daily output
- Python 3 (stdlib only — no dependencies to install) for the two scripts
- One inbox address per category you want to track, using a `+tag` alias
  (e.g. `you+newsletter-energy@gmail.com`) if your provider supports it —
  this is what lets one inbox sort newsletters by category automatically

## Setup

**1. Decide your categories.** Pick 3–6 short keys for the topics you
want to track (e.g. `energy`, `security`, `health`). These drive the
Gmail search, the taxonomy's `theme`/category tagging, and the digest's
section headers.

**2. Subscribe newsletters to your per-category alias.** For each
category, subscribe the newsletters you want under
`you+newsletter-<category>@gmail.com`.

**3. Copy and edit `config/taxonomy.example.json` → `taxonomy.json`.**
This is where you define, per the file's own inline `_*_note` fields:
   - `watchlist` — companies/entities to always catch, with optional
     `subject_only` (promote only when they're the story's subject, not
     just mentioned) and an associated `theme`
   - `frontier_labs.entities` — a small set of top-tier entities that are
     *always* subject_only
   - `themes` — the topics that qualify a story for Section 1, each with
     an `id`, a `name`, and a `definition` precise enough to disambiguate
     near-duplicate themes
   - `selection_rules` — `email_mode` (whether the emailed digest
     includes the "everything else" section), `theme_priority_order`,
     `high_bar_themes` (themes that should reject routine PR/funding
     noise and promote only on real breakthroughs), and
     `drive_upload_mode` (`auto` / `always_manual` / `always_drive`)

   Upload the finished `taxonomy.json` to your cloud-storage pipeline
   folder — the agent resolves it **by filename**, not by file ID, each
   run (some connectors mint a new file ID on every edit).


**4. Fill in `prompt/scheduled-task-prompt.md`'s placeholders:**

| Placeholder | What to put there |
|---|---|
| `{{DESTINATION_EMAIL}}` | where the daily draft gets addressed |
| `{{GMAIL_ALIAS_PREFIX}}` | your inbox's `+tag` pattern from step 2 |
| `{{CATEGORIES}}` | your category keys from step 1 |
| `{{DRIVE_PIPELINE_FOLDER_ID}}` | cloud folder holding `taxonomy.json` |
| `{{DRIVE_ARCHIVE_FOLDER_ID}}` | cloud folder for archived output (only matters if `drive_upload_mode` isn't `always_manual`) |
| `{{SCHEDULED_FIRE_TIME}}` | when you schedule the run (affects fetch-approval timing advice) |
| `{{CONSOLIDATION_TASK_NAME}}` | name of a separate periodic task, if you add one to fold daily exports into a durable database (optional — the pipeline itself is stateless per run) |

**6. Schedule it.** Point your agent runtime's scheduler at the filled-in
prompt, running once daily (or however often you want a digest). The
scripts in `scripts/` need to be readable by the agent at run time —
either committed alongside the prompt or written to disk as the first
setup step, per the prompt's own instructions.

## What you're providing, and where

| Input | Lives in | Changes how often |
|---|---|---|
| Which companies/entities matter | `taxonomy.json` → `watchlist`, `frontier_labs` | as your interests shift |
| What counts as "on topic" | `taxonomy.json` → `themes` | rarely, once tuned |
| How strict to be per theme | `taxonomy.json` → `selection_rules.high_bar_themes` | rarely |
| Where digests get delivered | `taxonomy.json` → `selection_rules.drive_upload_mode`; prompt → `{{DESTINATION_EMAIL}}` | rarely |
| Which newsletters to read | your inbox subscriptions (step 2) | as you subscribe/unsubscribe |
| Which RSS/podcast feeds to read | `rss_sources.yaml` | as you add/remove feeds |
| Category set, folder IDs, schedule | `prompt/scheduled-task-prompt.md` placeholders | on initial setup only |

`taxonomy.json` is the one file meant to be tuned continuously — it's
also the one file you should **not** commit with your real watchlist if
you're sharing this repo publicly; keep your live copy in your
cloud-storage folder or a private `.gitignore`'d path, and only commit
`taxonomy.example.json` as the shared template.

## Output

Each run produces `digest.html` (for the email draft body),
`plaintext.txt` (the draft's plaintext alternative), a full
`digest-combined-<date>.md`, and `stories-<date>.json`. The email draft
is created, never sent — you review and send it yourself.
