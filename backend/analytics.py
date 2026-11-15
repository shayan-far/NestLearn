"""Aggregate NestTrace logs into what the instructor view shows: per-segment confusion / aha rates,
episode outcomes, dwell, and which explanation styles actually resolve confusion."""
from collections import Counter
from statistics import median


def segment_stats(store, lesson: dict) -> dict:
    lid = lesson["id"]
    rows = store.events(lid)
    episodes = store.records(lid, "ep")
    seg_visits = store.records(lid, "seg")
    segs = {
        s["id"]: {
            "id": s["id"], "title": s["title"],
            "visited": set(), "confused": set(), "aha": set(),
            "quiz": [], "interventions": Counter(), "accepted": 0, "aha_after_intervention": 0,
            "episodes": [], "dwell": [], "confused_share": [],
        }
        for s in lesson["segments"]
    }
    sessions = set()
    modality = {}
    for r in rows:
        sessions.add(r["session"])
        s = segs.get(r["segment"])
        if not s:
            continue
        k, p = r["kind"], r["payload"]
        if k == "segment_enter":
            s["visited"].add(r["session"])
        elif k == "confusion_onset":
            s["confused"].add(r["session"])
        elif k == "aha":
            s["aha"].add(r["session"])
            if p.get("after"):
                s["aha_after_intervention"] += 1
                modality.setdefault(p["after"], Counter())["aha"] += 1
        elif k == "quiz":
            s["quiz"].append(1 if p.get("correct") else 0)
        elif k == "intervention":
            m = p.get("modality", "?")
            s["interventions"][m] += 1
            modality.setdefault(m, Counter())["offered"] += 1
        elif k == "intervention_response" and p.get("accepted"):
            s["accepted"] += 1
        elif k == "explanation":
            src = p.get("source", "?")
            modality.setdefault(p.get("modality", "?"), Counter())[f"src_{src}"] += 1

    for e in episodes:
        s = segs.get(e.get("seg"))
        if s:
            s["episodes"].append(e)
    for v in seg_visits:
        s = segs.get(v.get("seg"))
        if s:
            s["dwell"].append(v.get("dwell", 0))
            s["confused_share"].append(v.get("share", {}).get("confused", 0))

    out = []
    for s in segs.values():
        n = max(len(s["visited"]), 1)
        eps = s["episodes"]
        outcomes = Counter(e["outcome"] for e in eps)
        helped = Counter(e["helped_by"] for e in eps if e.get("helped_by"))
        out.append({
            "id": s["id"], "title": s["title"], "visits": len(s["visited"]),
            "confusion_rate": round(len(s["confused"]) / n, 3),
            "aha_rate": round(len(s["aha"]) / n, 3),
            "quiz_accuracy": round(sum(s["quiz"]) / len(s["quiz"]), 3) if s["quiz"] else None,
            "interventions": dict(s["interventions"]),
            "accepted": s["accepted"],
            "aha_after_intervention": s["aha_after_intervention"],
            "episodes": len(eps),
            "outcomes": dict(outcomes),
            "helped_by": dict(helped),
            "median_episode_s": round(median(e["dur"] for e in eps), 1) if eps else None,
            "relapses": sum(e.get("relapses", 0) for e in eps),
            "median_dwell_s": round(median(s["dwell"]), 1) if s["dwell"] else None,
            "time_confused": round(sum(s["confused_share"]) / len(s["confused_share"]), 3) if s["confused_share"] else None,
        })

    demo = sum(1 for x in sessions if x.startswith("demo-"))
    mod = {m: {"offered": c["offered"], "aha": c["aha"],
               "aha_rate": round(c["aha"] / c["offered"], 3) if c["offered"] else None,
               "llm": c["src_llm"], "lesson": c["src_lesson"], "fallback": c["src_fallback"]}
           for m, c in modality.items() if m != "?"}
    all_eps = [e for s in segs.values() for e in s["episodes"]]
    resolved = sum(1 for e in all_eps if e["outcome"] == "aha")
    return {
        "lesson": lid, "title": lesson.get("title"),
        "sessions": {"live": len(sessions) - demo, "demo": demo},
        "segments": out, "modality": mod,
        "totals": {"episodes": len(all_eps), "resolved_by_aha": resolved,
                   "resolution_rate": round(resolved / len(all_eps), 3) if all_eps else None},
        "version": store.version(lid),
    }
