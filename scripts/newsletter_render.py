#!/usr/bin/env python3
"""
newsletter_render.py — ingest decisions, insert into SQLite, render the
Innovation Digest HTML/Markdown/plaintext.

Second half of the pipeline (first half: newsletter_prefilter.py).

USAGE
  python3 newsletter_render.py \
    --taxonomy /path/to/taxonomy.json \
    --date 2026-07-29 \
    --clusters /tmp/work/clusters.json \
    --decisions /tmp/work/decisions.json \
    --db-src /tmp/work/newsletters.db \
    --db-dest "/sessions/<slug>/mnt/Innovation Newsletter/digests/newsletters.db" \
    --md-dest "/sessions/<slug>/mnt/Innovation Newsletter/digests/digest-combined-2026-07-29.md" \
    --stories-json-dest /tmp/work/stories-2026-07-29.json \
    --outdir /tmp/work

--decisions is a small JSON object, ONLY for the clusters that
newsletter_prefilter.py flagged as needs_review=true (see review_needed.json).
Keys are cluster_id, values are:
  {"selected": 1, "matched_themes": "...", "matched_watchlist": "...",
   "frontier_subject": 0 or 1, "match_reasons": "plain-words reason"}
or simply {"selected": 0} to reject. Clusters not mentioned in --decisions
default to selected=0 (safe default — never silently promotes).

--db-src should already exist (copy of the synced newsletters.db, e.g. from
a prior run) or will be created fresh with the right schema if missing.

OUTPUTS
  <outdir>/digest.html         The filled email digest div, ready to
                               paste as the Gmail draft's htmlBody.
  <outdir>/plaintext.txt       Short plaintext fallback (headlines + links
                               only for Section 1, no full-sentence
                               duplication — keep this SHORT, it's a
                               fallback, not a second copy of the digest).
  --md-dest                    Full markdown copy (Section 1 + Section 2),
                               written directly if provided.
  --db-dest                    Updated sqlite db, copied back directly if
                               provided.
  --stories-json-dest          This run's inserted rows (all of them, both
                               sections) as a JSON list, for the weekly
                               consolidation task to fold into the durable
                               db. One object per story occurrence with a
                               "companies" list joined in. Written directly
                               if provided — the caller does NOT need to
                               re-open the db with an ad-hoc script.

Also prints a JSON summary (topic counts, cross-topic companies, any
cross-category suppressions) to stdout.
"""
import argparse
import datetime
import html as htmlmod
import json
import os
import shutil
import sqlite3
import sys
from collections import defaultdict, OrderedDict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from newsletter_common import tokenize, jaccard, url_score, load_taxonomy

TOPIC_ORDER = ["pharma", "ai", "defense"]
TOPIC_LABEL = {"pharma": "Pharma / Biotech", "ai": "AI / Tech / Robotics", "defense": "Defense"}
TOPIC_COLOR = {"pharma": "#7c3aed", "ai": "#2563eb", "defense": "#b45309"}
TOPIC_CROSS_LABEL = {"pharma": "Pharma", "ai": "AI", "defense": "Defense"}
CATEGORY_PREFERENCE = ["defense", "pharma", "ai"]  # tie-break order for cross-category dedup


def esc(s):
    return htmlmod.escape(s or "", quote=False)


def ensure_schema(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS stories(
        id INTEGER PRIMARY KEY, date TEXT, category TEXT, source TEXT,
        newsletter_subject TEXT, rank INTEGER, section TEXT, headline TEXT,
        summary TEXT, url TEXT, cluster_id TEXT, matched_themes TEXT,
        matched_watchlist TEXT, frontier_subject INTEGER, selected INTEGER,
        match_reasons TEXT, story_type TEXT, subgroup TEXT)""")
    conn.execute("CREATE TABLE IF NOT EXISTS story_companies(story_id INTEGER, company TEXT)")
    conn.commit()


def merge_decisions(clusters, decisions):
    """An explicit entry in decisions.json always wins — this is what lets
    the auto_rejected_headlines.txt safety-net skim (see newsletter_prefilter.py
    docstring) promote a story the mechanical keyword pass missed. Clusters
    with no explicit decision fall back to the prefilter's auto_decision;
    clusters with neither default to reject (never silently promotes)."""
    for c in clusters:
        if c["cluster_id"] in decisions:
            d = decisions[c["cluster_id"]]
            c["decision"] = {
                "selected": int(d.get("selected", 0)),
                "matched_themes": d.get("matched_themes", ""),
                "matched_watchlist": d.get("matched_watchlist", ""),
                "frontier_subject": int(d.get("frontier_subject", 0)),
                "match_reasons": d.get("match_reasons", ""),
            }
        elif c.get("auto_decision") is not None:
            c["decision"] = c["auto_decision"]
        else:
            # No decision supplied for a needs_review cluster -> safe default: reject.
            c["decision"] = {"selected": 0, "matched_themes": "", "matched_watchlist": "",
                              "frontier_subject": 0, "match_reasons": ""}
    return clusters


def cross_category_dedup(clusters):
    """Among selected clusters in different categories, suppress the weaker
    duplicate if they share a company and have >=5 shared headline/summary
    tokens. Mutates decision.selected to 0 on the suppressed one and appends
    a note to its match_reasons."""
    selected = [c for c in clusters if c["decision"]["selected"] == 1]
    suppressed_notes = []
    for i in range(len(selected)):
        for j in range(i + 1, len(selected)):
            a, b = selected[i], selected[j]
            if a["category"] == b["category"]:
                continue
            if a["decision"]["selected"] == 0 or b["decision"]["selected"] == 0:
                continue  # one already suppressed by an earlier pair
            comp_a = set(x.lower() for x in a["companies"])
            comp_b = set(x.lower() for x in b["companies"])
            if not (comp_a & comp_b):
                continue
            tok_a = tokenize(a["headline"] + " " + a["summary"])
            tok_b = tokenize(b["headline"] + " " + b["summary"])
            shared = tok_a & tok_b
            if len(shared) < 5:
                continue
            has_watchlist_a = bool(a["decision"]["matched_watchlist"])
            has_watchlist_b = bool(b["decision"]["matched_watchlist"])
            if has_watchlist_a and not has_watchlist_b:
                keep, drop = a, b
            elif has_watchlist_b and not has_watchlist_a:
                keep, drop = b, a
            else:
                pref = CATEGORY_PREFERENCE
                keep, drop = (a, b) if pref.index(a["category"]) < pref.index(b["category"]) else (b, a)
            drop["decision"]["selected"] = 0
            note = f"SUPPRESSED: cross-category duplicate of {keep['category'].title()} item ({keep['decision']['match_reasons']})"
            drop["decision"]["match_reasons"] = note
            suppressed_notes.append({"suppressed": drop["cluster_id"], "kept": keep["cluster_id"], "note": note})
    return suppressed_notes


def insert_into_db(conn, clusters, date):
    cur = conn.cursor()
    next_id = (cur.execute("SELECT MAX(id) FROM stories").fetchone()[0] or 0) + 1
    for c in clusters:
        d = c["decision"]
        for occ in c["occurrences"]:
            cur.execute(
                """INSERT INTO stories (id, date, category, source, newsletter_subject, headline,
                   summary, url, cluster_id, matched_themes, matched_watchlist, frontier_subject,
                   selected, match_reasons) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (next_id, date, c["category"], occ.get("source"), occ.get("newsletter_subject"),
                 occ.get("headline"), occ.get("summary"), occ.get("url"), c["cluster_id"],
                 d["matched_themes"], d["matched_watchlist"], d["frontier_subject"], d["selected"],
                 d["match_reasons"]))
            for comp in occ.get("companies", []):
                cur.execute("INSERT INTO story_companies (story_id, company) VALUES (?,?)", (next_id, comp))
            next_id += 1
    conn.commit()


def export_stories_json(conn, date, path):
    """Export this run's rows as a JSON list for the separate weekly
    consolidation task. This lives here (rather than in an ad-hoc snippet in
    the calling task prompt) because the connection and the row set are
    already open at this point — re-opening the db afterwards just to
    re-derive the same rows was an error-prone hand-written step."""
    cur = conn.cursor()
    cur.execute("""SELECT id, date, category, source, newsletter_subject, headline,
                   summary, url, cluster_id, matched_themes, matched_watchlist,
                   frontier_subject, selected, match_reasons, story_type, subgroup
                   FROM stories WHERE date=? ORDER BY id""", (date,))
    cols = [d[0] for d in cur.description]
    out = []
    for row in cur.fetchall():
        d = dict(zip(cols, row))
        d["companies"] = [r[0] for r in conn.execute(
            "SELECT company FROM story_companies WHERE story_id=?", (d["id"],)).fetchall()]
        out.append(d)
    with open(path, "w") as f:
        json.dump(out, f, indent=1)
    return len(out)


def render(conn, date, template_version):
    cur = conn.cursor()

    cur.execute("""SELECT category, cluster_id, headline, summary, url, source, match_reasons
                   FROM stories WHERE date=? AND selected=1 ORDER BY category, id""", (date,))
    sec1 = defaultdict(lambda: OrderedDict())
    for cat, cid, headline, summary, url, source, reasons in cur.fetchall():
        e = sec1[cat].setdefault(cid, {"summary": summary, "urls": [], "sources": [], "reasons": set()})
        e["urls"].append(url)
        if source not in e["sources"]:
            e["sources"].append(source)
        if reasons:
            e["reasons"].add(reasons)
    for cat in sec1:
        for cid, e in sec1[cat].items():
            e["url"] = sorted(e["urls"], key=url_score)[0]

    cur.execute("""SELECT category, cluster_id, headline, summary, url, source
                   FROM stories WHERE date=? AND selected=0 ORDER BY category, id""", (date,))
    sec2 = defaultdict(lambda: OrderedDict())
    for cat, cid, headline, summary, url, source in cur.fetchall():
        e = sec2[cat].setdefault(cid, {"summary": summary, "urls": []})
        e["urls"].append((url, source))
    for cat in sec2:
        for cid, e in sec2[cat].items():
            e["url"], e["source"] = sorted(e["urls"], key=lambda t: url_score(t[0]))[0]

    cur.execute("""SELECT DISTINCT category FROM stories WHERE date=?""", (date,))
    categories_with_mail = {r[0] for r in cur.fetchall()}

    company_topics = defaultdict(set)
    for cat in sec1:
        for cid, e in sec1[cat].items():
            pass  # companies not tracked per-cluster here; computed below via story_companies join

    cur.execute("""SELECT stories.category, story_companies.company FROM stories
                   JOIN story_companies ON stories.id = story_companies.story_id
                   WHERE stories.date=? AND stories.selected=1""", (date,))
    for cat, comp in cur.fetchall():
        company_topics[comp].add(cat)
    cross = {c: sorted(t) for c, t in company_topics.items() if len(t) >= 2}

    n_pharma = len(sec1.get("pharma", {})) + len(sec2.get("pharma", {}))
    n_ai = len(sec1.get("ai", {})) + len(sec2.get("ai", {}))
    n_defense = len(sec1.get("defense", {})) + len(sec2.get("defense", {}))

    d = datetime.date.fromisoformat(date)
    weekday, datestr = d.strftime("%A"), d.strftime("%d %b %Y")

    p = []
    p.append('<div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#1a1a1a;max-width:700px;line-height:1.5">')
    p.append('')
    p.append('  <h2 style="margin:0 0 2px">Innovation Digest — Combined</h2>')
    p.append(f'  <div style="color:#666;font-size:13px;margin-bottom:8px">{weekday}, {datestr} · Pharma {n_pharma} · AI {n_ai} · Defense {n_defense} stories</div>')
    p.append('')
    if cross:
        items = [f'{esc(c)} <span style="color:#6366f1">({", ".join(TOPIC_CROSS_LABEL[t] for t in topics)})</span>' for c, topics in cross.items()]
        p.append(f'  <div style="background:#eef2ff;border:1px solid #c7d2fe;border-radius:6px;padding:8px 12px;font-size:12.5px;color:#3730a3;margin-bottom:16px"><b>Cross-topic threads:</b> {" · ".join(items)}</div>')
        p.append('')

    p.append('  <!-- ================= SECTION 1 ================= -->')
    p.append('  <div style="font-weight:800;font-size:13px;border-bottom:2px solid #111;padding-bottom:3px;margin:14px 0 8px">SECTION 1 — HIGHLIGHTED</div>')
    p.append('')
    for cat in TOPIC_ORDER:
        items = sec1.get(cat, {})
        if not items:
            continue
        p.append(f'  <div style="font-weight:700;font-size:12px;letter-spacing:.04em;color:{TOPIC_COLOR[cat]};text-transform:uppercase;margin:10px 0 6px">{TOPIC_LABEL[cat]}</div>')
        p.append('')
        for cid, e in items.items():
            reasons = esc("; ".join(sorted(e["reasons"])))
            srcs = esc(" + ".join(e["sources"]))
            p.append('    <div style="margin-bottom:8px">')
            link = f' <a href="{e["url"]}" style="color:#2563eb;text-decoration:none">source&nbsp;&rarr;</a>' if e["url"] else ''
            p.append(f'      <div style="font-size:13.5px;font-weight:600">{esc(e["summary"])}{link}</div>')
            p.append(f'      <div style="font-size:11px;margin-top:1px"><span style="color:#0a7d33">{reasons}</span> · <span style="color:#888">in: {srcs}</span></div>')
            p.append('    </div>')
        p.append('')

    p.append('  <!-- ================= SECTION 2 ================= -->')
    p.append('  <div style="font-weight:800;font-size:13px;border-bottom:2px solid #ccc;padding-bottom:3px;margin:16px 0 8px">SECTION 2 — EVERYTHING ELSE</div>')
    p.append('')
    for cat in TOPIC_ORDER:
        items = sec2.get(cat, {})
        p.append(f'  <div style="font-weight:700;font-size:12px;letter-spacing:.04em;color:{TOPIC_COLOR[cat]};text-transform:uppercase;margin:10px 0 6px">{TOPIC_LABEL[cat]}</div>')
        if cat not in categories_with_mail:
            p.append('  <div style="font-size:12.5px;color:#999;margin:0 0 8px">no newsletters today</div>')
        elif not items:
            p.append('  <div style="font-size:12.5px;color:#999;margin:0 0 8px">nothing outside Section 1 today</div>')
        else:
            p.append('  <ul style="margin:0 0 4px;padding-left:18px;font-size:13px;color:#333">')
            for cid, e in items.items():
                link = f' <a href="{e["url"]}" style="color:#2563eb;text-decoration:none">source&nbsp;&rarr;</a>' if e["url"] else ''
                p.append(f'    <li style="margin-bottom:4px">{esc(e["summary"])}{link} <span style="color:#999">(via {esc(e["source"])})</span></li>')
            p.append('  </ul>')
        p.append('')

    p.append(f'  <div style="border-top:1px solid #e5e7eb;margin-top:16px;padding-top:9px;color:#aaa;font-size:11px">Combined view · taxonomy {esc(template_version)} · newsletter-native links.</div>')
    p.append('</div>')
    html_out = "\n".join(p)

    # ---- Markdown ----
    md = [f"# Innovation Digest — Combined", f"{weekday}, {datestr} · Pharma {n_pharma} · AI {n_ai} · Defense {n_defense} stories\n"]
    if cross:
        md.append("**Cross-topic threads:** " + ", ".join(f"{c} ({'/'.join(TOPIC_CROSS_LABEL[t] for t in topics)})" for c, topics in cross.items()) + "\n")
    md.append("## Section 1 — Highlighted\n")
    for cat in TOPIC_ORDER:
        items = sec1.get(cat, {})
        if not items:
            continue
        md.append(f"### {TOPIC_LABEL[cat]}\n")
        for cid, e in items.items():
            link = f" [source →]({e['url']})" if e['url'] else ''
            md.append(f"- **{e['summary']}**{link}  \n  _{'; '.join(sorted(e['reasons']))} · in: {' + '.join(e['sources'])}_")
        md.append("")
    md.append("## Section 2 — Everything else\n")
    for cat in TOPIC_ORDER:
        items = sec2.get(cat, {})
        md.append(f"### {TOPIC_LABEL[cat]}\n")
        if cat not in categories_with_mail:
            md.append("_no newsletters today_\n")
            continue
        if not items:
            md.append("_nothing outside Section 1 today_\n")
            continue
        for cid, e in items.items():
            link = f" [source →]({e['url']})" if e['url'] else ''
            md.append(f"- {e['summary']}{link} (via {e['source']})")
        md.append("")
    md.append(f"---\n_Combined view · taxonomy {template_version} · newsletter-native links._")
    md_out = "\n".join(md)

    # ---- Plaintext fallback (SHORT — headline+link only, no full-sentence duplication) ----
    pt = [f"INNOVATION DIGEST — COMBINED — {weekday}, {datestr}", "",
          "Section 1 highlights (full digest incl. Section 2 in the HTML version of this email):", ""]
    for cat in TOPIC_ORDER:
        items = sec1.get(cat, {})
        if not items:
            continue
        pt.append(f"-- {TOPIC_LABEL[cat].upper()} --")
        for cid, e in items.items():
            pt.append(f"* {e['url'] if e['url'] else '(no link found)'}")
            pt.append(f"  {e['summary'][:140]}{'...' if len(e['summary']) > 140 else ''}")
        pt.append("")
    if cross:
        pt.append("Cross-topic: " + ", ".join(f"{c} ({'/'.join(TOPIC_CROSS_LABEL[t] for t in topics)})" for c, topics in cross.items()))
        pt.append("")
    pt.append("Full digest (Section 1 + Section 2, all topics) is in the HTML version of this email.")
    pt_out = "\n".join(pt)

    return html_out, md_out, pt_out, {"n_pharma": n_pharma, "n_ai": n_ai, "n_defense": n_defense, "cross_topic": cross}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--taxonomy", required=True)
    ap.add_argument("--date", required=True)
    ap.add_argument("--clusters", required=True)
    ap.add_argument("--decisions", required=True)
    ap.add_argument("--db-src", required=True)
    ap.add_argument("--db-dest")
    ap.add_argument("--md-dest")
    ap.add_argument("--stories-json-dest")
    ap.add_argument("--outdir", required=True)
    args = ap.parse_args()

    os.makedirs(args.outdir, exist_ok=True)
    taxonomy = load_taxonomy(args.taxonomy)
    template_version = taxonomy.get("version", "?")

    with open(args.clusters) as f:
        clusters = json.load(f)
    decisions = {}
    if os.path.exists(args.decisions):
        with open(args.decisions) as f:
            decisions = json.load(f)

    clusters = merge_decisions(clusters, decisions)
    suppressed = cross_category_dedup(clusters)

    conn = sqlite3.connect(args.db_src)
    ensure_schema(conn)
    insert_into_db(conn, clusters, args.date)

    stories_exported = None
    if args.stories_json_dest:
        stories_exported = export_stories_json(conn, args.date, args.stories_json_dest)

    html_out, md_out, pt_out, stats = render(conn, args.date, template_version)
    conn.close()

    with open(os.path.join(args.outdir, "digest.html"), "w") as f:
        f.write(html_out)
    with open(os.path.join(args.outdir, "plaintext.txt"), "w") as f:
        f.write(pt_out)

    if args.md_dest:
        with open(args.md_dest, "w") as f:
            f.write(md_out)
    else:
        with open(os.path.join(args.outdir, f"digest-combined-{args.date}.md"), "w") as f:
            f.write(md_out)

    if args.db_dest:
        shutil.copy(args.db_src, args.db_dest)

    print(json.dumps({
        "stats": stats,
        "cross_category_suppressions": suppressed,
        "html_path": os.path.join(args.outdir, "digest.html"),
        "plaintext_path": os.path.join(args.outdir, "plaintext.txt"),
        "md_dest": args.md_dest or os.path.join(args.outdir, f"digest-combined-{args.date}.md"),
        "db_dest": args.db_dest,
        "stories_json_dest": args.stories_json_dest,
        "stories_exported": stories_exported,
    }, indent=1))


if __name__ == "__main__":
    main()
