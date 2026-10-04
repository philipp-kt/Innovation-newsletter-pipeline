"""
newsletter_common.py — shared helpers for the Innovation Digest pipeline.

Used by newsletter_prefilter.py and newsletter_render.py. No third-party
dependencies (stdlib only) so it runs in a bare sandbox with no pip install.

Part of the "innovation-newsletter-daily" scheduled task. See SKILL.md
(Claude > Scheduled > innovation-newsletter-daily) for how these scripts
fit into the end-to-end pipeline.
"""
import re
import json

# Common English stopwords + newsletter-boilerplate words that would otherwise
# dominate token-overlap comparisons without carrying meaning.
STOPWORDS = set("""
a an the to of in on for with and or is are be as by at from into after before
over under new says said will would could should its it s their his her they
he she we our your you s this that these those has have had not no yes but if
than then so up out about via amid per while during within without more most
less least than up down off out than announced announces reports according
""".split())


def tokenize(text):
    """Lowercase word tokens, stopwords removed. Keeps 2-letter tokens
    (AI, ML, VC, C2, EU...) — this taxonomy leans heavily on short
    acronyms, so filtering them out silently breaks keyword matching."""
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    return set(w for w in words if w not in STOPWORDS and len(w) >= 2)


def contains_phrase(alias, text):
    """Whole-word/whole-phrase, case-insensitive match with word
    boundaries on both ends. Plain substring search is NOT safe here —
    e.g. the alias "FAIR" (Meta's FAIR lab) is a substring of "affairs",
    and would false-positive on "Department of Veterans Affairs" without
    boundary anchoring."""
    pattern = r"(?<![a-z0-9])" + re.escape(alias.lower()) + r"(?![a-z0-9])"
    return re.search(pattern, (text or "").lower()) is not None


def stem(word):
    """Very light suffix-stripping stemmer — good enough for bag-of-stems
    keyword matching, not meant to be linguistically rigorous."""
    w = word.lower()
    for suf in ("ations", "ation", "ing", "edly", "ed", "ies", "ied", "es", "s"):
        if w.endswith(suf) and len(w) - len(suf) >= 3:
            return w[: -len(suf)]
    return w


def stems(text):
    return set(stem(w) for w in tokenize(text))


def jaccard(a, b):
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def keyword_hit(keyword_phrase, text_stem_set):
    """A theme keyword phrase 'hits' if every significant word in the phrase
    (as a stem) appears SOMEWHERE in the story's text (order independent).
    This is deliberately generous (recall over precision) — false positives
    just land in the review_needed shortlist for a human/LLM judgment call,
    false negatives silently lose a story, which is the worse failure mode.
    """
    phrase_stems = stems(keyword_phrase)
    if not phrase_stems:
        return False
    return phrase_stems.issubset(text_stem_set)


def load_taxonomy(path):
    with open(path) as f:
        return json.load(f)


def build_alias_index(taxonomy):
    """Returns two dicts mapping lowercased alias -> record:
      watchlist_index[alias_lower] = {"canonical":..., "subject_only": bool}
      frontier_index[alias_lower] = {"canonical":...}   # frontier_labs are ALL subject_only
    """
    watchlist_index = {}
    for entry in taxonomy.get("watchlist", []):
        subject_only = bool(entry.get("subject_only", False))
        for alias in entry.get("aliases", [entry["canonical"]]):
            watchlist_index[alias.lower()] = {
                "canonical": entry["canonical"],
                "subject_only": subject_only,
                "theme": entry.get("theme"),
            }

    frontier_index = {}
    fl = taxonomy.get("frontier_labs", {})
    for entry in fl.get("entities", []):
        for alias in entry.get("aliases", [entry["canonical"]]):
            frontier_index[alias.lower()] = {"canonical": entry["canonical"]}

    return watchlist_index, frontier_index


def theme_lookup(taxonomy):
    """theme id -> {"name":..., "keywords":[...]}"""
    return {t["id"]: t for t in taxonomy.get("themes", [])}


TRACKING_URL_HINTS = [
    "marketing.statnews.com", "links.tldrnewsletter.com", "threadreaderapp.com",
    "click.", "trk.", "list-manage.com", "hubspotlinks.com", "mailchi.mp",
    "sendgrid.net", "e3t.", "ct.sailthru.com", "cur.at", "url.avanan.click",
]


def url_score(u):
    """0 = looks like a clean publisher URL (preferred), 1 = looks like an
    ESP/tracking redirect (used only if nothing cleaner is available),
    2 = missing/empty (worst — extraction couldn't find a URL at all)."""
    if not u:
        return 2
    return 1 if any(h in u for h in TRACKING_URL_HINTS) else 0
