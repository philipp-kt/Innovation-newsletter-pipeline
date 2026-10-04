# Innovation Digest Pipeline — Scheduled Task Prompt (Template)

This is the prompt body for a scheduled/automated agent task. Fill in the
`{{PLACEHOLDER}}` values for your own setup before using it (see
`config/` in this repo for the companion config files).

## Configuration placeholders used below

| Placeholder | Meaning | Example |
|---|---|---|
| `{{DESTINATION_EMAIL}}` | Where the daily draft gets addressed to | `you@example.com` |
| `{{GMAIL_ALIAS_PREFIX}}` | Your Gmail address with a `+newsletter-<category>` alias pattern | `you+newsletter-<category>@gmail.com` |
| `{{CATEGORIES}}` | Your own short topic keys, comma-separated (3–6 recommended) | `topic-a, topic-b, topic-c` |
| `{{DRIVE_PIPELINE_FOLDER_ID}}` | Google Drive folder ID holding `taxonomy.json` and the pipeline scripts | `<your-folder-id>` |
| `{{DRIVE_ARCHIVE_FOLDER_ID}}` | Google Drive folder ID for archived digest output | `<your-folder-id>` |
| `{{SCHEDULED_FIRE_TIME}}` | Local time this task fires | `23:15 CET` |
| `{{CONSOLIDATION_TASK_NAME}}` | Name of a separate weekly task that folds daily exports into a durable DB | `newsletter-consolidate-weekly` |

Your RSS/podcast sources are **not** hardcoded here — define them in
`config/rss_sources.example.yaml` (see repo). Your watchlist companies,
frontier-lab aliases, and theme keywords are **not** hardcoded here either —
they live entirely in `taxonomy.json` (see `config/taxonomy.example.json`).
This prompt only describes the pipeline's *mechanics*.

---

Build the daily COMBINED digest from your newsletter inboxes plus any
configured RSS/podcast feeds (step 0c) and save it as a Gmail draft to
`{{DESTINATION_EMAIL}}`. Run end-to-end; each run starts fresh in a cloud
sandbox with NO local folder access. `taxonomy.json` lives in Google Drive
and is downloaded each run; the three pipeline scripts live in this repo's
`scripts/` folder and are written to disk at the start of each run (or
mounted directly, depending on your runner) — no Drive download needed for
them.

CONSTRAINT: the email tool can only CREATE DRAFTS, not send. Draft only.

Correct selection matters more than speed. Mechanical work (clustering
duplicates, matching watchlist/frontier-lab aliases, keyword-scanning for
themes) is done by scripts, not by reasoning in prose. Judgment is reserved
for the genuinely ambiguous cases: a much shorter list than "all stories."

WHY THIS RUNS IN THE CLOUD: needs no local device — fetches everything from
Google Drive. Scripts ship as repo files, not downloaded at runtime (a
base64 Drive download tokenizes ~4x worse than plain text). `taxonomy.json`
stays in Drive since it's what you tune over time. Each run creates a fresh
empty-schema db and exports that day's rows as JSON to Drive for a separate
weekly LOCAL task (`{{CONSOLIDATION_TASK_NAME}}`) to fold into a durable db
— ephemeral sandboxes can't hold an ever-growing db, and a whole-db daily
re-upload risks tool-call size limits.

RSS/PODCAST FETCH TIMING: if this run fires near your own overnight hours
(here, `{{SCHEDULED_FIRE_TIME}}`), some fetch tools may require a one-time
interactive approval that only a live human can grant — impossible once
you're asleep. If that's a risk for your fetch tool, run step 0c FIRST, to
catch the few minutes you might still be awake at kickoff. It's ordered
purely for that timing reason — nothing downstream depends on 0c finishing
first. RSS/podcast results stay optional: on any failure, retry once, log
to `rss_fetch_issues`, continue — never block the run.

RSS & PODCAST SOURCES (step 0c): defined in `config/rss_sources.example.yaml`,
not hardcoded here. Each entry needs a name, a feed URL, a category, and a
`type` (`journal` or `podcast`, used by `newsletter_render.py` to tag items
distinctly from email newsletters). Some publishers hard-403 bot-protected
RSS fetchers regardless of retries/timing — track any such exclusions in
that same config file with a short note, rather than silently dropping them.

GOOGLE DRIVE FOLDER STRUCTURE:
  Pipeline folder: `{{DRIVE_PIPELINE_FOLDER_ID}}`
  Digests archive subfolder: `{{DRIVE_ARCHIVE_FOLDER_ID}}`
Those two folder IDs are stable and safe to hardcode in your own private
copy of this prompt. `taxonomy.json`'s FILE id is deliberately NOT
hardcoded: if your Drive connector can't update file content in place,
every edit mints a new file id. Step 0d resolves it by title every run —
don't reintroduce a hardcoded id.

PIPELINE SCRIPTS: see `scripts/` in this repo. Read their docstrings once if
unfamiliar (own CLI args + I/O JSON contracts documented there). Use these
— don't hand-roll classification/rendering. `newsletter_render.py` takes NO
`--template` arg.

WORKING DIRECTORY: a scratch dir, e.g. `/tmp/pipeline/`. Not durable;
destroyed when the run ends — why render outputs get delivered before the
run finishes.

## 0a. Get today's date
- Shell `date +%F`. This is the only thing that has to happen before step
  0c — everything else in setup can run after or alongside it.

## 0b. Create the scratch dir
- `mkdir -p` the scratch dir so step 0c has somewhere to write its staged
  output files.

## 0c. RSS & podcast feeds (direct fetch, no subagents) — RUN THIS FIRST if timing requires it
For each source in `config/rss_sources.example.yaml`, fetch its URL with:
"Return every item with pubDate/dc:date exactly <TODAY, UTC>: headline,
summary (feed description or 1-sentence title paraphrase), url
(article/episode page, not audio enclosure). If none published <TODAY>,
reply NONE." Set `companies: []` (alias matching already scans
headline+summary text), and set `type: "journal"` or `"podcast"` per that
source's config entry.

On any fetch error (HTTP error, timeout, an interactive-approval block),
retry once, else count as 0 items and log `"<name>: <error>"` to
`rss_fetch_issues`; never block the run on one failure. A clean NONE is not
an issue.

`--raw.json` for each category does NOT exist yet at this point (inbox
extraction hasn't run). So write each category's RSS/podcast items to a
STAGING file instead: `<scratch>/<category>_rss_staged.json` — plain JSON
array, same story schema as step 2's extraction output, plus `"type"`. One
staged file per category with RSS sources configured; categories with no
RSS sources get `[]` too, so step 2b has nothing to special-case.

## 0d. Setup — the rest
- Discover the connector IDs you'll need this run (they're per-connection
  and can differ run to run, so don't hardcode them — rediscover each
  time): an email connector (search threads, create draft) and a
  cloud-storage connector (search files, download/create file).
- Get `taxonomy.json` into the scratch dir, but do NOT download it via a
  method that returns base64 yourself if a plain-text read is available —
  base64 tokenizes ~4x worse than plain text and can land in context
  twice. Prefer delegating this to a small subagent/subtask if your runner
  supports one, so the base64 round-trip stays out of the main context.
  Resolve the file BY TITLE (`taxonomy.json`) inside the pipeline folder —
  do not assume any file id, since some connectors mint a new file id on
  every content edit. If more than one match comes back, use the most
  recently modified one. Verify it parses and note its `version` and
  watchlist size.
- Do NOT download or seed any newsletters.db — none exists yet on a fresh
  run; `newsletter_render.py` creates a fresh schema automatically when
  `--db-src` points to a path that doesn't exist yet.

## 1. Find today's newsletters
For each category in `{{CATEGORIES}}`, search your inbox for mail sent to
`{{GMAIL_ALIAS_PREFIX}}` with that category substituted in, restricted to
today's date range. Collect the thread IDs per category (skip obvious
ads/promos/event-invites by subject if clear, otherwise keep).

## 2. Parse in SUBAGENTS — file-based handoff (cost control)
Do NOT read full thread bodies yourself in the main context — they can run
60-125K characters each. Spawn ONE subagent PER CATEGORY with that
category's thread IDs and this instruction: fetch each thread in the
plain-text format your connector offers (not a format that also returns
HTML — that roughly doubles payload for no extraction benefit). Extract
EVERY substantive news story (capture all top stories + any "more
stories"/quick-hits section; skip ads, sponsor blurbs, tutorials/how-tos,
event promos, job posts). For each story return: source (newsletter name),
newsletter_subject, headline, summary (ONE clear sentence), url (the
story's own link — clean publisher URL if present, otherwise the
tracking/redirect link AS-IS, never fabricated), companies (list). Do not
web-search. Write the JSON array to `<scratch>/<category>_raw.json` and
reply with only a one-line confirmation (file path + story count), so the
50-150KB blob never round-trips through the parent context.

## 2b. Merge staged RSS/podcast results into the raw JSON files
Step 0c already staged RSS/podcast items at
`<scratch>/<category>_rss_staged.json`. Now that step 2's extraction has
written fresh `<scratch>/<category>_raw.json` files, merge each category's
staged array into the matching raw.json (read both, concatenate the story
lists, write back; create raw.json as `[]` first only if it's unexpectedly
missing) before step 3. Run this uniformly across all categories, even
ones whose staged file is just `[]`, so the step stays simple.

## 3. Mechanized prefilter (script — do not hand-classify)
Run `newsletter_prefilter.py` against the raw JSON files for all
categories, e.g.:
```
python3 newsletter_prefilter.py --taxonomy taxonomy.json --date <date> \
  --category <cat1>=<scratch>/<cat1>_raw.json \
  --category <cat2>=<scratch>/<cat2>_raw.json \
  ... --outdir <scratch>
```
This clusters duplicate stories within each category and auto-decides the
unambiguous cases: a non-subject-only watchlist mention auto-promotes;
zero watchlist/frontier-lab/theme-keyword signal auto-rejects. It writes
`review_needed.json` (the shortlist needing judgment, typically ~35-45% of
raw stories), `auto_rejected_headlines.txt` (everything else, headline
only), and `taxonomy_reference.json` (compact lookup — see step 4).

## 4. Judgment pass — ONLY on the shortlist
Read `review_needed.json` and `taxonomy_reference.json` (do NOT open
`taxonomy.json` directly). Each `review_needed.json` entry already has
`candidate_reasons` — use that as your starting point. `taxonomy_reference.json`'s
theme entries carry both a short name and a one-sentence definition — weigh
each candidate against the definition, not just the name, since some
themes have overlapping names that only the definition disambiguates.

Apply your taxonomy's own refinements as documented in `taxonomy.json`'s
`selection_rules` (e.g., subject-only entities promote only when they're
the SUBJECT of the story, not just mentioned; personnel/leadership moves
promote only if high-level AND at a qualifying company; regulatory/policy
promotes only within the scope your taxonomy defines; some categories may
carry a deliberately HIGH BAR for "breakthrough" themes — see
`selection_rules.*_significance_threshold` — excluding routine
funding/hiring/roadmap talk/conference appearances/opinion pieces unless a
named watchlist company is involved). Any big-tech watchlist entries that
are scoped to a specific sub-theme (e.g. only promote when the story's
subject is specifically that sub-theme, not general business/cloud news
naming the company) should be treated as scoped, not general.

Write decisions to `<scratch>/decisions.json`:
```json
{"cluster_id": {"selected": 1, "matched_themes": "...", "matched_watchlist": "...",
  "frontier_subject": 0_or_1, "match_reasons": "plain-words reason"}}
```
You only need full entries for promotions (`selected: 1`); anything you're
rejecting, or don't mention at all, defaults to Section 2.

SAFETY NET — skim `auto_rejected_headlines.txt` (headlines only, ~1
sec/line): taxonomy keyword lists don't cover every phrasing, so a real
story can auto-reject on a technicality. If obviously on-theme by meaning,
add it to `decisions.json` too. Should catch a small handful, not trigger
a second full pass.

## 5. De-dupe + render (script)
Run `newsletter_render.py`, e.g.:
```
python3 newsletter_render.py --taxonomy taxonomy.json --date <date> \
  --clusters <scratch>/clusters.json --decisions <scratch>/decisions.json \
  --db-src <scratch>/newsletters.db \
  --md-dest <scratch>/digest-combined-<date>.md \
  --stories-json-dest <scratch>/stories-<date>.json --outdir <scratch>
```
No `--db-dest` (db is ephemeral) or `--template`. `--db-src` must point to
a path that doesn't exist yet — schema created fresh there. Cross-category
de-dup: shared company + enough shared headline/summary tokens → suppress
the weaker duplicate (prefer the watchlist hit, else your category
preference order). Cross-topic banner: any company selected in ≥2 topics.
"why" reasons in plain words, never raw theme ids; Section-2 bullets with
"(via <newsletter>)"; sub-header omitted if empty; "no newsletters today"
if a topic had zero mail.

EMAIL MODE: `taxonomy.json`'s `selection_rules.email_mode` controls whether
`digest.html`/`plaintext.txt` include Section 2 (`"two_section_audit"`
default includes it, `"section1_only"` omits it) — a `taxonomy.json` edit
only. `--md-dest` ALWAYS gets both sections. An empty Section 1 shows a
plain "no stories cleared the bar today" message instead of a bare header
— normal, not an error.

## 6. Deliver — directly, no subagents (cost control)
After step 5 the scratch dir holds four artifacts: `digest.html`,
`plaintext.txt`, `digest-combined-<date>.md`, `stories-<date>.json`. Do
these deliveries yourself, directly — do NOT spawn a subagent for this
step; a sandbox `cat` truncation is an environment limit, not a reason to
spawn one — page via a file-read tool and reconstruct instead.

RETRY GUARD (cheap — metadata-only): check for an existing draft for
today's date using a metadata-only list call. If one already exists,
delivery already happened — skip draft creation and report that draft ID.
Likewise, before each cloud-storage upload, check (metadata only) whether
that exact title already exists in the archive folder; skip if so. If this
run retries after an interruption, do NOT redo steps 0-5 — the artifacts
are still in the scratch dir if it persisted, or cheaply re-rendered from
`clusters.json` + `decisions.json` if not.

DELIVERY MODE: read `selection_rules.drive_upload_mode` from
`taxonomy.json` (`auto`/`always_manual`/`always_drive`; missing =
`always_manual`). `always_manual`: skip the archive upload, deliver both
files as direct file attachments. `auto`: attempt the archive upload,
retry once per file on failure, fall back to a direct attachment for that
file. `always_drive`: attempt the archive upload, no fallback. Report
which files went via archive vs. direct delivery.

ARCHIVE UPLOAD (`auto`/`always_drive` only) — for each of the two files
below: read its full content with ONE read call (don't pre-emptively
chunk it, don't re-read or diff it afterward), then upload as plain
UTF-8 text (never base64 for plain text — it adds cost/risk for no
benefit). Both uploads go to `{{DRIVE_ARCHIVE_FOLDER_ID}}`:
  1. `<scratch>/digest-combined-<date>.md` → title `digest-combined-<date>.md`, mimetype `text/markdown`
  2. `<scratch>/stories-<date>.json` → title `stories-<date>.json`, mimetype `application/json`
Never truncate, retype, or paraphrase file content. The stories JSON may
be one very long line — pass it through verbatim.

DIRECT ATTACHMENT (`always_manual`, or an `auto` fallback) — attach that
file's scratch-dir copy to your response to the user. Normal outcome in
`always_manual` mode, not an error — report it plainly.

DRAFT — read `digest.html` and `plaintext.txt` with ONE read call each
(`plaintext.txt` intentionally has lines ending mid-sentence with a literal
"..." — BY DESIGN, preserve it). Then create a draft with
`to: [{{DESTINATION_EMAIL}}]`, `subject: "<Your Digest Name> — Combined — <Weekday DD Mon YYYY>"`,
`htmlBody: <digest.html's exact content>`, `body: <plaintext.txt's exact content>`.
Do NOT send — draft only.

VERIFICATION DISCIPLINE: a single byte-count check on each source file
before submitting is enough — compare and move on. Don't re-read/diff
"just to be sure" — that ritual balloons cost for no benefit. After both
deliveries, confirm the same cheap way (metadata-only draft/file listing).
Reserve a full-body read for when something looks wrong, and even then
read only that one item.

## Final report
End with a short summary: story counts per category, how many needed
judgment, the draft ID, and how the .md/stories JSON were delivered
(archive titles, or direct attachment). Quote the taxonomy version/
watchlist size step 0d resolved. State which `email_mode` rendered. Report
step 0c: sources with items vs. NONE, and `rss_fetch_issues` (or "none").
