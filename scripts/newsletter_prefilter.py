#!/usr/bin/env python3
"""
newsletter_prefilter.py — mechanized clustering + candidate selection for the
Innovation Digest pipeline.

WHY THIS EXISTS
Selection used to mean an LLM reading all ~80+ extracted stories and reasoning
in prose, one by one, against taxonomy.json's watchlist/frontier/theme rules.
That reasoning pass was the single biggest cost in a run. Most of it is
mechanical:
  - watchlist company mentioned, and that watchlist entry is NOT subject_only
    -> promotes on mention, no judgment needed (rule is unconditional)
  - no watchlist company, no frontier lab mention, no theme keyword anywhere
    in the headline/summary -> nothing to promote on, no judgment needed
Only the genuinely ambiguous cases need an LLM:
  - a frontier lab / subject_only watchlist entity is mentioned -> is it
    actually the SUBJECT of the story, or just mentioned in passing?
  - a theme keyword fired -> is this really on-theme, or a keyword coincidence
    (e.g. "data center" firing on an unrelated noise-complaint story)?
  - refinements that need reading comprehension: personnel/leadership scope,
    AI-vs-non-AI regulatory news, clinical-vs-payer healthcare AI.

This script does the mechanical part and hands back a SHORTLIST for the LLM
to judge, instead of the full story list.

USAGE
  python3 newsletter_prefilter.py \
    --taxonomy /path/to/taxonomy.json \
    --date 2026-07-29 \
    --category pharma=/tmp/work/pharma_raw.json \
    --category ai=/tmp/work/ai_raw.json \
    --category defense=/tmp/work/defense_raw.json \
    --outdir /tmp/work

Each --category value is a JSON file containing a list of story dicts as
returned by the extraction subagents:
  {"source":..., "newsletter_subject":..., "headline":..., "summary":...,
   "url":..., "companies": [...]}

OUTPUTS (written to --outdir)
  clusters.json               Every cluster (deduped within its category),
                               with mechanical hints and, where decidable
                               without judgment, an auto_decision already
                               filled in.
  review_needed.json          The shortlist of clusters that need an LLM
                               judgment call. Compact — headline, summary,
                               companies, sources, and WHY it's a candidate.
  auto_rejected_headlines.txt One line per cluster that was auto-rejected
                               (no watchlist/frontier/theme signal at all).
                               This is a cheap safety net: skim the headlines
                               only (not full reasoning) for anything that
                               obviously should have matched a theme by
                               meaning even though it didn't hit a literal
                               keyword (taxonomy keyword lists are incomplete
                               by nature). Move any such headline's cluster
                               into review_needed.json by hand if so.
  taxonomy_reference.json     A compact lookup — theme id -> display name,
                               frontier-lab canonical names + aliases, and
                               the subject_only watchlist entries — pulled
                               straight from taxonomy.json. Read this instead
                               of opening taxonomy.json yourself during the
                               judgment pass; it exists so the LLM doesn't
                               burn several bash/python calls re-deriving the
                               same structure taxonomy.json already encodes
                               (that exploration was pure overhead, not
                               judgment, in past runs).

Next step in the pipeline: newsletter_render.py, which takes clusters.json
plus a small decisions.json (your judgment calls for review_needed.json) and
does the DB insert + HTML/Markdown/plaintext rendering.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from newsletter_common import tokenize, stems, jaccard, keyword_hit, load_taxonomy, build_alias_index, theme_lookup, contains_phrase


def cluster_category(stories, cat_prefix):
    """Greedy token/company-overlap clustering within one category."""
    clusters = []  # list of {"items":[idx...], "tokens":set, "companies":set(lower)}
    for i, s in enumerate(stories):
        tok = tokenize((s.get("headline") or "") + " " + (s.get("summary") or ""))
        comp = set(c.lower() for c in s.get("companies", []) if c)
        best_idx, best_score = None, 0.0
        for ci, c in enumerate(clusters):
            score = jaccard(tok, c["tokens"])
            shared_company = bool(comp & c["companies"])
            if score >= 0.35 or (shared_company and score >= 0.12 and comp):
                if score > best_score:
                    best_idx, best_score = ci, score
        if best_idx is not None:
            clusters[best_idx]["items"].append(i)
            clusters[best_idx]["tokens"] |= tok
            clusters[best_idx]["companies"] |= comp
        else:
            clusters.append({"items": [i], "tokens": tok, "companies": comp})

    out = []
    for n, c in enumerate(clusters):
        occ = [stories[i] for i in c["items"]]
        rep = occ[0]
        sources = []
        for o in occ:
            if o.get("source") and o["source"] not in sources:
                sources.append(o["source"])
        all_companies = []
        for o in occ:
            for comp in o.get("companies", []):
                if comp not in all_companies:
                    all_companies.append(comp)
        out.append({
            "cluster_id": f"{cat_prefix}_{n:03d}",
            "headline": rep.get("headline", ""),
            "summary": rep.get("summary", ""),
            "companies": all_companies,
            "sources": sources,
            "occurrences": occ,  # full raw rows, used by render.py for DB fidelity
        })
    return out


def mechanical_tag(cluster, watchlist_index, frontier_index, themes):
    text = cluster["headline"] + " " + cluster["summary"]
    text_stems = stems(text)
    headline_stems = stems(cluster["headline"])
    companies_lower = [c.lower() for c in cluster["companies"]]

    watchlist_hits = []       # [{"canonical":..., "subject_only":bool}]
    for c in companies_lower:
        if c in watchlist_index:
            watchlist_hits.append(watchlist_index[c])
    # also check aliases against the free text (word-boundary matched, not
    # naive substring — "FAIR" must not match inside "Affairs")
    for alias, rec in watchlist_index.items():
        if contains_phrase(alias, text) and rec["canonical"] not in [h["canonical"] for h in watchlist_hits]:
            watchlist_hits.append(rec)

    frontier_hits = []        # [{"canonical":..., "in_headline":bool}]
    for alias, rec in frontier_index.items():
        if contains_phrase(alias, text):
            if rec["canonical"] not in [h["canonical"] for h in frontier_hits]:
                frontier_hits.append({"canonical": rec["canonical"], "in_headline": contains_phrase(alias, cluster["headline"])})

    theme_hits = {}           # theme_id -> [matched keyword,...]
    for tid, t in themes.items():
        matched_kw = [kw for kw in t.get("keywords", []) if keyword_hit(kw, text_stems)]
        if matched_kw:
            theme_hits[tid] = matched_kw

    return {
        "watchlist_hits": watchlist_hits,
        "frontier_hits": frontier_hits,
        "theme_hits": theme_hits,
    }


def decide(cluster, mech):
    """Return (auto_decision or None, needs_review: bool, candidate_reasons: [str])."""
    non_subject_watchlist = [h for h in mech["watchlist_hits"] if not h["subject_only"]]
    subject_only_watchlist = [h for h in mech["watchlist_hits"] if h["subject_only"]]

    if non_subject_watchlist:
        hit = non_subject_watchlist[0]
        return (
            {
                "selected": 1,
                "matched_watchlist": hit["canonical"],
                "matched_themes": "",
                "frontier_subject": 0,
                "match_reasons": f"watchlist: {hit['canonical']}",
            },
            False,
            [],
        )

    candidate_reasons = []
    if mech["frontier_hits"]:
        for h in mech["frontier_hits"]:
            candidate_reasons.append(
                f"frontier_lab_mention: {h['canonical']} (in_headline={h['in_headline']})"
            )
    if subject_only_watchlist:
        for h in subject_only_watchlist:
            candidate_reasons.append(f"subject_only_watchlist_mention: {h['canonical']}")
    if mech["theme_hits"]:
        for tid, kws in mech["theme_hits"].items():
            candidate_reasons.append(f"theme_keyword_hit: {tid} ({', '.join(kws)})")

    if candidate_reasons:
        return (None, True, candidate_reasons)

    return (
        {"selected": 0, "matched_watchlist": "", "matched_themes": "", "frontier_subject": 0, "match_reasons": ""},
        False,
        [],
    )


def build_taxonomy_reference(taxonomy):
    """Compact lookup for the judgment pass, so it never has to open
    taxonomy.json itself and re-derive this structure by hand (that
    exploration cost several extra bash/python round trips per run for
    zero judgment value — the data was already sitting in taxonomy.json,
    just not in a shape cheap to skim)."""
    themes = {t["id"]: t.get("name", t["id"]) for t in taxonomy.get("themes", [])}

    frontier_labs = [
        {"canonical": e["canonical"], "aliases": e.get("aliases", [e["canonical"]])}
        for e in taxonomy.get("frontier_labs", {}).get("entities", [])
    ]

    subject_only_watchlist = [
        {"canonical": e["canonical"], "theme": e.get("theme"), "aliases": e.get("aliases", [e["canonical"]])}
        for e in taxonomy.get("watchlist", [])
        if e.get("subject_only")
    ]

    return {
        "themes": themes,
        "frontier_labs_all_subject_only": frontier_labs,
        "watchlist_subject_only": subject_only_watchlist,
        "note": (
            "frontier_labs and watchlist_subject_only entries promote ONLY "
            "when they are the SUBJECT of the story, not just mentioned in "
            "passing. Non-subject-only watchlist mentions already auto-"
            "promoted mechanically and won't appear in review_needed.json."
        ),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--taxonomy", required=True)
    ap.add_argument("--date", required=True)
    ap.add_argument("--category", action="append", required=True, help="name=path/to/raw.json")
    ap.add_argument("--outdir", required=True)
    args = ap.parse_args()

    os.makedirs(args.outdir, exist_ok=True)
    taxonomy = load_taxonomy(args.taxonomy)
    watchlist_index, frontier_index = build_alias_index(taxonomy)
    themes = theme_lookup(taxonomy)

    all_clusters = []
    review_needed = []
    auto_rejected_lines = []
    summary_counts = {}

    for cat_spec in args.category:
        cat, path = cat_spec.split("=", 1)
        with open(path) as f:
            stories = json.load(f)
        clusters = cluster_category(stories, cat)
        n_promoted = n_rejected = n_review = 0
        for c in clusters:
            c["category"] = cat
            mech = mechanical_tag(c, watchlist_index, frontier_index, themes)
            c["mechanical"] = mech
            auto_decision, needs_review, reasons = decide(c, mech)
            c["auto_decision"] = auto_decision
            c["needs_review"] = needs_review
            if needs_review:
                n_review += 1
                review_needed.append({
                    "cluster_id": c["cluster_id"],
                    "category": cat,
                    "headline": c["headline"],
                    "summary": c["summary"],
                    "companies": c["companies"],
                    "sources": c["sources"],
                    "candidate_reasons": reasons,
                })
            elif auto_decision and auto_decision["selected"] == 1:
                n_promoted += 1
            else:
                n_rejected += 1
                auto_rejected_lines.append(f"{cat} | {c['cluster_id']} | {c['headline']}")
            all_clusters.append(c)
        summary_counts[cat] = {
            "raw_stories": len(stories),
            "clusters": len(clusters),
            "auto_promoted": n_promoted,
            "auto_rejected": n_rejected,
            "needs_review": n_review,
        }

    with open(os.path.join(args.outdir, "clusters.json"), "w") as f:
        json.dump(all_clusters, f, indent=1)
    with open(os.path.join(args.outdir, "review_needed.json"), "w") as f:
        json.dump(review_needed, f, indent=1)
    with open(os.path.join(args.outdir, "auto_rejected_headlines.txt"), "w") as f:
        f.write("\n".join(auto_rejected_lines) + "\n")
    with open(os.path.join(args.outdir, "taxonomy_reference.json"), "w") as f:
        json.dump(build_taxonomy_reference(taxonomy), f, indent=1)

    print(json.dumps({"date": args.date, "counts": summary_counts}, indent=1))
    total_review = sum(v["needs_review"] for v in summary_counts.values())
    total_stories = sum(v["raw_stories"] for v in summary_counts.values())
    print(f"\n{total_review} of {total_stories} raw stories need an LLM judgment call "
          f"(see review_needed.json). Everything else was auto-decided.")
    print("For the judgment pass, read taxonomy_reference.json instead of taxonomy.json "
          "directly — it has the theme names / frontier-lab aliases / subject_only "
          "watchlist entries pre-extracted.")


if __name__ == "__main__":
    main()
