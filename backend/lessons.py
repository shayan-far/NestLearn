"""Lesson loading, library metadata, and per-segment concept fingerprints.

Every *.json in lessons/ is a lesson; its id is the file name. Metadata (description, reading time, tags...)
is extracted automatically so a new lesson file shows up in the library with no extra work. Optional
fields a lesson can set to override extraction: "description", "subject", "tags", and per-segment "concepts".

Fingerprints feed Concept Delta Memory (backend/memory.py): a sparse concept vector (TF-IDF over the
lesson's segments, or authored "concepts") plus four dense structural traits describing *how* the
segment explains things.
"""
import json
import math
import re
from collections import Counter
from pathlib import Path

STOP = set("""a an the and or but if then so of to in on at by for with from as is are was were be been being
it its this that these those there here we you your our they them their he she his her i me my not no yes do does
did done can could should would will just only also very more most much many any some each every one two three
than too into out up down over under about again once all both few other such own same how what which who whom why
when where while because until let lets get gets got make makes made like say says said see seen use used using
way thing things kind part bit lot little good better best bad worse new old first last next well even still
trying try tries tell tells told need needs needed take takes took keep keeps go goes going come comes put give
gives sometimes often always never usually really actually simply slightly sensibly whole big small large
common choice want wants know think look looks find finds help helps mean called means sure
""".split())
WORD = re.compile(r"[a-zA-Z][a-zA-Z\-']+")
MATH = re.compile(r"[0-9=+\-*/^()<>]")
PROC = {"step", "steps", "update", "repeat", "then", "first", "next", "until", "each", "iterate", "compute",
        "calculate", "apply", "subtract", "add", "multiply"}
DEFN = {"means", "called", "refers", "defined", "definition", "known", "term", "named", "stands"}
TRAITS = ["math", "procedural", "definitional", "density"]


def _tokens(text: str) -> list:
    return [w.lower().strip("-'") for w in WORD.findall(text)]


def _content_terms(text: str, phrases=frozenset()) -> list:
    """Content words, with known two-word phrases ("learning rate") merged into one term."""
    toks = _tokens(text)
    out, i = [], 0
    while i < len(toks):
        if i + 1 < len(toks) and f"{toks[i]} {toks[i + 1]}" in phrases:
            out.append(f"{toks[i]} {toks[i + 1]}")
            i += 2
            continue
        if toks[i] not in STOP and len(toks[i]) > 2:
            out.append(toks[i])
        i += 1
    return out


def _phrases(texts: list) -> frozenset:
    """Adjacent content-word pairs that recur across the lesson are treated as single concepts."""
    pairs = Counter()
    for t in texts:
        toks = _tokens(t)
        for a, b in zip(toks, toks[1:]):
            if a not in STOP and b not in STOP and len(a) > 2 and len(b) > 2:
                pairs[f"{a} {b}"] += 1
    return frozenset(p for p, c in pairs.items() if c >= 2)


def traits(text: str) -> list:
    """How a segment explains: math-heavy? step-by-step? definitional? dense sentences? Each 0-1."""
    toks = _tokens(text) or [""]
    n = len(toks)
    sentences = max(1, len(re.findall(r"[.!?]", text)))
    math_chars = len(MATH.findall(text)) / max(len(text), 1)
    return [
        round(min(1.0, math_chars * 12), 3),
        round(min(1.0, sum(t in PROC for t in toks) / n * 8), 3),
        round(min(1.0, sum(t in DEFN for t in toks) / n * 12), 3),
        round(min(1.0, max(0.0, (n / sentences - 8) / 20)), 3),
    ]


def _fingerprints(lesson: dict) -> None:
    segs = lesson["segments"]
    # title words count 3x: they name what the segment is about
    texts = [(s.get("title", "") + " ") * 3 + s.get("body", "") for s in segs]
    phrases = _phrases([s.get("title", "") + ". " + s.get("body", "") for s in segs])
    docs = [Counter(_content_terms(t, phrases)) for t in texts]
    df = Counter(t for d in docs for t in d)
    n = len(segs)
    for s, d in zip(segs, docs):
        if s.get("concepts"):
            vec = {c.lower(): 1.0 for c in s["concepts"]}
        else:
            # TF-IDF, smoothed so terms present everywhere still count a little
            scored = {t: c * (math.log((1 + n) / (1 + df[t])) + 0.35) for t, c in d.items()}
            vec = dict(sorted(scored.items(), key=lambda kv: -kv[1])[:8])
        norm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
        s["_fp"] = {"concepts": {k: round(v / norm, 3) for k, v in vec.items()},
                    "traits": traits(s.get("body", "")),
                    "difficulty": s.get("sim_difficulty")}


def _meta(lid: str, lesson: dict) -> dict:
    segs = lesson["segments"]
    words = sum(len(_tokens(s.get("body", ""))) for s in segs)
    quizzes = sum(1 for s in segs if s.get("quiz"))
    first = segs[0].get("body", "") if segs else ""
    desc = lesson.get("description") or (re.split(r"(?<=[.!?])\s", first.strip())[0] if first else "")
    if lesson.get("tags"):
        tags = lesson["tags"]
    else:
        pool = Counter()
        for s in segs:
            for k, v in s["_fp"]["concepts"].items():
                pool[k] += v
        tags = [k for k, _ in pool.most_common(4)]
    diffs = [s["sim_difficulty"] for s in segs if "sim_difficulty" in s]
    return {
        "id": lid, "title": lesson.get("title", lid), "description": desc,
        "subject": lesson.get("subject", ""), "segments": len(segs), "quizzes": quizzes,
        "words": words, "minutes": max(1, round(words / 180 + quizzes * 0.5 + len(segs) * 0.3)),
        "tags": tags, "difficulty": round(sum(diffs) / len(diffs), 2) if diffs else None,
        "outline": [{"id": s["id"], "title": s.get("title", s["id"])} for s in segs],
    }


def load_lessons(directory: Path) -> dict:
    lessons = {}
    for p in sorted(Path(directory).glob("*.json")):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            print(f"[lessons] skipping {p.name}: {e}")
            continue
        if not d.get("segments"):
            print(f"[lessons] skipping {p.name}: no segments")
            continue
        d["id"] = p.stem
        for i, s in enumerate(d["segments"]):
            s.setdefault("id", f"s{i + 1}")
            s.setdefault("alternatives", {})
        _fingerprints(d)
        d["_meta"] = _meta(p.stem, d)
        lessons[p.stem] = d
    # optional "order" field (lower first); unordered lessons sort by title
    return dict(sorted(lessons.items(), key=lambda kv: (kv[1].get("order", 50), kv[1].get("title", ""))))


def public(lesson: dict) -> dict:
    """Lesson as sent to the browser (internal fields stripped)."""
    out = {k: v for k, v in lesson.items() if not k.startswith("_")}
    out["segments"] = [{k: v for k, v in s.items() if not k.startswith("_")} for s in lesson["segments"]]
    out["meta"] = lesson["_meta"]
    return out
