"""Concept Delta Memory (CDM): a constant-size learner model that gives the LLM context without history.

Problem: to explain something well to *this* student, the LLM should know what has confused them before
and what kind of explanation tends to click. Sending the whole session history is slow, costly, leaks more
than it needs to, and grows without bound.

CDM keeps, per student and per signal channel ("confused", "aha", "bored"), at most K *prototypes*.
A prototype is a compressed description of a family of moments: a sparse concept vector (top terms), four
structural traits (math-heavy / procedural / definitional / dense), an evidence weight, a count, and for
"aha" the explanation styles that preceded it.

On every new moment (a confusion onset, an aha, boredom) on segment X with fingerprint f:
  1. Decay   every prototype in that channel by gamma (recent evidence dominates).
  2. Match   f against the prototypes with a blended similarity (concept cosine + trait closeness).
  3. Delta   if the best match is close enough, store only what is *new*: concept terms the prototype
             doesn't have yet (or is under-weighting) and the trait shift. The prototype absorbs the delta
             with a 1/(n+1) running mean. A repeat of the same segment is a pure reinforcement (empty delta).
  4. Novel   otherwise f becomes a new prototype. If that exceeds K, the two most similar prototypes are
             fused (or the weakest is evicted), so memory never grows past K per channel.

Alongside prototypes CDM tracks *trait lift* (how much more math-heavy / procedural confusing segments are
than everything the student has seen) and *modality outcomes* (offered / accepted / followed by aha).

When an explanation is requested for segment Y, CDM renders a context block ranked by relevance to Y:
the 3 confusion prototypes most related to Y, the 2 strongest aha prototypes with what led to them, the
overlap between Y's concepts and past confusion, the dominant trait lift, and modality outcomes. The block
is a few hundred characters no matter how long the student has been learning: O(K), not O(history).
See docs/CONCEPT_DELTA_MEMORY.md.
"""
import json
import math
import time
from pathlib import Path
from typing import Optional

from backend.lessons import TRAITS

K = 6                 # max prototypes per channel
GAMMA = 0.92          # per-observation decay of older evidence
MERGE_SIM = 0.42      # >= this: absorb as a delta into the best prototype
FUSE_SIM = 0.30       # when over capacity, fuse the closest pair if at least this similar, else evict
TOP_TERMS = 8
CHANNELS = ("confused", "aha", "bored")
MODALITIES = ("rephrase", "analogy", "example", "diagram", "quiz")


def cosine(a: dict, b: dict) -> float:
    if not a or not b:
        return 0.0
    dot = sum(v * b.get(k, 0.0) for k, v in a.items())
    na = math.sqrt(sum(v * v for v in a.values()))
    nb = math.sqrt(sum(v * v for v in b.values()))
    return dot / (na * nb) if na and nb else 0.0


def similarity(fp_a: dict, fp_b: dict) -> float:
    c = cosine(fp_a["concepts"], fp_b["concepts"])
    ta, tb = fp_a["traits"], fp_b["traits"]
    t = 1 - sum(abs(x - y) for x, y in zip(ta, tb)) / len(ta)
    return 0.75 * c + 0.25 * t


def _norm(vec: dict) -> dict:
    top = dict(sorted(vec.items(), key=lambda kv: -kv[1])[:TOP_TERMS])
    n = math.sqrt(sum(v * v for v in top.values())) or 1.0
    return {k: round(v / n, 3) for k, v in top.items()}


class ConceptDeltaMemory:
    def __init__(self, user: str, path: Optional[Path] = None):
        self.user, self.path = user, Path(path) if path else None
        self.protos = {c: [] for c in CHANNELS}
        self.exposure = {"n": 0, "traits": [0.0] * len(TRAITS)}            # everything the student has seen
        self.channel_traits = {c: {"n": 0, "traits": [0.0] * len(TRAITS)} for c in CHANNELS}
        self.modality = {m: {"offered": 0, "accepted": 0, "aha": 0} for m in MODALITIES}
        self.seen = set()
        self.next_id = 1
        if self.path and self.path.exists():
            self._load()

    # ---- persistence -----------------------------------------------------------------------------------
    def _load(self) -> None:
        try:
            d = json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError):
            return
        self.protos = {c: d.get("protos", {}).get(c, []) for c in CHANNELS}
        self.exposure = d.get("exposure", self.exposure)
        self.channel_traits.update(d.get("channel_traits", {}))
        for m, v in d.get("modality", {}).items():
            self.modality.setdefault(m, v).update(v)
        self.seen = set(d.get("seen", []))
        self.next_id = d.get("next_id", 1)

    def save(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.to_dict(), separators=(",", ":")))

    def to_dict(self) -> dict:
        return {"user": self.user, "protos": self.protos, "exposure": self.exposure,
                "channel_traits": self.channel_traits, "modality": self.modality,
                "seen": sorted(self.seen)[-200:], "next_id": self.next_id}

    # ---- observations ------------------------------------------------------------------------------------
    def expose(self, ref: str, fp: dict) -> None:
        """Student entered a segment. Builds the baseline that trait lift is measured against."""
        if ref in self.seen:
            return
        self.seen.add(ref)
        self._running(self.exposure, fp["traits"])

    @staticmethod
    def _running(acc: dict, traits: list) -> None:
        acc["n"] += 1
        acc["traits"] = [round(a + (b - a) / acc["n"], 4) for a, b in zip(acc["traits"], traits)]

    def observe(self, channel: str, ref: str, fp: dict, intensity: float = 1.0,
                via: Optional[str] = None, now: Optional[float] = None) -> dict:
        """Fold one moment into memory. Returns what changed (shown in the debug pane and the docs)."""
        now = now or time.time()
        protos = self.protos[channel]
        for p in protos:
            p["w"] = round(p["w"] * GAMMA, 4)
        self._running(self.channel_traits[channel], fp["traits"])

        best, best_sim = None, -1.0
        for p in protos:
            s = similarity(fp, p)
            if s > best_sim:
                best, best_sim = p, s

        if best is not None and best_sim >= MERGE_SIM:
            n = best["n"]
            delta = {k: v for k, v in fp["concepts"].items()
                     if k not in best["concepts"] or v - best["concepts"][k] > 0.15}
            if ref in best["refs"] and not delta:
                op = "reinforce"
            else:
                op = "delta"
                merged = {k: v * n / (n + 1) for k, v in best["concepts"].items()}
                for k, v in fp["concepts"].items():
                    merged[k] = merged.get(k, 0.0) + v / (n + 1)
                best["concepts"] = _norm(merged)
                best["traits"] = [round(a + (b - a) / (n + 1), 3) for a, b in zip(best["traits"], fp["traits"])]
            best["n"] = n + 1
            best["w"] = round(best["w"] + intensity, 4)
            best["last"] = now
            if ref not in best["refs"]:
                best["refs"] = (best["refs"] + [ref])[-5:]
            if via:
                best.setdefault("via", {})[via] = best.get("via", {}).get(via, 0) + 1
            result = {"op": op, "proto": best["id"], "sim": round(best_sim, 2),
                      "delta": sorted(delta, key=lambda k: -fp["concepts"][k])[:5]}
        else:
            p = {"id": f"{channel[0]}{self.next_id}", "concepts": _norm(fp["concepts"]),
                 "traits": list(fp["traits"]), "n": 1, "w": round(intensity, 4), "last": now, "refs": [ref]}
            if via:
                p["via"] = {via: 1}
            self.next_id += 1
            protos.append(p)
            result = {"op": "new", "proto": p["id"], "sim": round(max(best_sim, 0), 2),
                      "delta": list(p["concepts"])[:5]}
            if len(protos) > K:
                result["compacted"] = self._compact(protos)

        if channel == "aha" and via in self.modality:
            self.modality[via]["aha"] += 1
        self.save()
        return {"channel": channel, "ref": ref, **result}

    def _compact(self, protos: list) -> str:
        pair, pair_sim = None, -1.0
        for i in range(len(protos)):
            for j in range(i + 1, len(protos)):
                s = similarity(protos[i], protos[j])
                if s > pair_sim:
                    pair, pair_sim = (i, j), s
        if pair and pair_sim >= FUSE_SIM:
            a, b = protos[pair[0]], protos[pair[1]]
            n = a["n"] + b["n"]
            merged = {}
            for p in (a, b):
                for k, v in p["concepts"].items():
                    merged[k] = merged.get(k, 0.0) + v * p["n"] / n
            a["concepts"] = _norm(merged)
            a["traits"] = [round((x * a["n"] + y * b["n"]) / n, 3) for x, y in zip(a["traits"], b["traits"])]
            a["n"], a["w"], a["last"] = n, round(a["w"] + b["w"], 4), max(a["last"], b["last"])
            a["refs"] = (a["refs"] + [r for r in b["refs"] if r not in a["refs"]])[-5:]
            for m, c in b.get("via", {}).items():
                a.setdefault("via", {})[m] = a.get("via", {}).get(m, 0) + c
            protos.pop(pair[1])
            return f"fused {b['id']} into {a['id']}"
        weakest = min(range(len(protos)), key=lambda i: protos[i]["w"])
        gone = protos.pop(weakest)
        return f"evicted {gone['id']}"

    def offered(self, modality: str) -> None:
        if modality in self.modality:
            self.modality[modality]["offered"] += 1
            self.save()

    def accepted(self, modality: str) -> None:
        if modality in self.modality:
            self.modality[modality]["accepted"] += 1
            self.save()

    # ---- context for the LLM -------------------------------------------------------------------------------
    def lift(self, channel: str) -> list:
        """Traits over-represented in this channel vs. everything seen, e.g. [("math", 0.31)]."""
        ct = self.channel_traits[channel]
        if ct["n"] < 2 or self.exposure["n"] < 2:
            return []
        out = [(name, round(c - e, 2)) for name, c, e in zip(TRAITS, ct["traits"], self.exposure["traits"])]
        return sorted([x for x in out if abs(x[1]) >= 0.12], key=lambda x: -abs(x[1]))[:2]

    def context(self, target_fp: dict, focus: str = "confused", exclude_ref: str = None) -> dict:
        """Compact, target-ranked summary. Returns {"text": prompt block, "parts": structured, "chars": n}.
        Prototypes that only describe the part being explained right now (exclude_ref) are skipped, so the
        LLM hears about the student's *other* struggles rather than an echo of the current one."""
        def usable(p):
            return not (exclude_ref and p["refs"] == [exclude_ref])

        def rank(channel, k):
            scored = [(p["w"] * (0.5 + similarity(target_fp, p)), p) for p in self.protos[channel] if usable(p)]
            return [p for _, p in sorted(scored, key=lambda x: -x[0])[:k]]

        conf = rank("confused", 3)
        aha = rank("aha", 2)
        lines, parts = [], {"confused": [], "aha": [], "overlap": [], "lift": [], "modality": {}}

        for p in conf:
            terms = ", ".join(list(p["concepts"])[:4])
            rel = similarity(target_fp, p)
            parts["confused"].append({"terms": list(p["concepts"])[:4], "n": p["n"], "rel": round(rel, 2)})
            lines.append(f"- struggled with [{terms}] x{p['n']}" + (" (closely related to this part)" if rel > 0.4 else ""))
        overlap = sorted({t for p in self.protos["confused"] if usable(p)
                          for t in p["concepts"] if t in target_fp["concepts"]})
        if overlap:
            parts["overlap"] = overlap[:5]
            lines.append(f"- this part reuses ideas they found hard before: {', '.join(overlap[:5])}")
        for p in aha:
            terms = ", ".join(list(p["concepts"])[:3])
            via = max(p.get("via", {}).items(), key=lambda kv: kv[1])[0] if p.get("via") else None
            parts["aha"].append({"terms": list(p["concepts"])[:3], "via": via})
            lines.append(f"- had an aha on [{terms}]" + (f" right after the {via} explanation" if via else ""))
        lift = self.lift(focus)
        if lift:
            parts["lift"] = lift
            desc = {"math": "math-heavy", "procedural": "step-by-step/procedural", "definitional": "definition-heavy",
                    "density": "dense, long-sentence"}
            bits = [f"{'more' if v > 0 else 'less'} likely on {desc[n]} text" for n, v in lift]
            noun = {"confused": "confusion", "aha": "an aha", "bored": "drifting"}[focus]
            lines.append(f"- {noun} is {' and '.join(bits)}")
        mods = {m: s for m, s in self.modality.items() if s["offered"]}
        if mods:
            parts["modality"] = mods
            best = sorted(mods.items(), key=lambda kv: (-(kv[1]["aha"] / kv[1]["offered"]), -kv[1]["offered"]))
            lines.append("- explanation styles so far: " + "; ".join(
                f"{m} {s['aha']}/{s['offered']} led to aha" for m, s in best[:3]))
        text = "\n".join(lines) if lines else "- no history yet (first struggle)"
        return {"text": text, "parts": parts, "chars": len(text)}

    def snapshot(self) -> dict:
        """Small view for the debug pane / instructor view."""
        return {ch: [{"id": p["id"], "terms": list(p["concepts"])[:5], "n": p["n"], "w": round(p["w"], 2),
                      "via": p.get("via")} for p in sorted(ps, key=lambda p: -p["w"])]
                for ch, ps in self.protos.items()} | {"modality": self.modality, "lift": self.lift("confused")}


def profile_path(profile_dir: Path, user: str) -> Path:
    safe = "".join(c for c in user.lower() if c.isalnum() or c in "-_") or "anon"
    return Path(profile_dir) / f"{safe}.json"
