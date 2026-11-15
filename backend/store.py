"""Reads NestTrace session logs (data/logs/<lesson>/<session>.jsonl) for analytics and the instructor view.

Writing happens in backend/trace.py. Files are parsed lazily and cached by (mtime, size), so the
instructor view can poll every few seconds and only re-parse the sessions that actually grew.
Only derived states and interactions are stored, never raw EEG.
"""
import hashlib
import json
import time
from pathlib import Path

LIVE_WINDOW_S = 20   # a session whose file changed this recently and has no footer is shown as live


class LogStore:
    def __init__(self, log_dir):
        self.dir = Path(log_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._cache: dict = {}

    # ---- parsing ------------------------------------------------------------------------------------
    def _files(self, lesson_id: str) -> list:
        d = self.dir / lesson_id
        return sorted(d.glob("*.jsonl")) if d.exists() else []

    def _parse(self, path: Path) -> dict:
        st = path.stat()
        key = (st.st_mtime_ns, st.st_size)
        hit = self._cache.get(path)
        if hit and hit[0] == key:
            return hit[1]
        recs = []
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    recs.append(json.loads(line))
                except json.JSONDecodeError:
                    continue   # a half-written last line while the session is live
        hdr = next((r for r in recs if r.get("k") == "hdr"), {})
        ftr = next((r for r in reversed(recs) if r.get("k") == "ftr"), None)
        parsed = {"path": path, "hdr": hdr, "ftr": ftr, "recs": recs, "bytes": st.st_size, "mtime": st.st_mtime}
        self._cache[path] = (key, parsed)
        return parsed

    def _sessions(self, lesson_id: str) -> list:
        return [self._parse(p) for p in self._files(lesson_id)]

    def version(self, lesson_id: str) -> str:
        h = hashlib.sha1()
        for p in self._files(lesson_id):
            st = p.stat()
            h.update(f"{p.name}:{st.st_mtime_ns}:{st.st_size};".encode())
        return h.hexdigest()[:12]

    # ---- flat event rows (what analytics.py consumes) -------------------------------------------------
    def events(self, lesson_id: str) -> list:
        rows = []
        for s in self._sessions(lesson_id):
            t0, sid, user = s["hdr"].get("t0", 0), s["hdr"].get("session", s["path"].stem), s["hdr"].get("user", "?")
            for r in s["recs"]:
                if r.get("k") != "ev":
                    continue
                payload = {k: v for k, v in r.items() if k not in ("k", "ts", "seg", "kind")}
                rows.append({"ts": t0 + r["ts"], "session": sid, "user": user, "segment": r.get("seg"),
                             "kind": r["kind"], "payload": payload})
        rows.sort(key=lambda r: r["ts"])
        return rows

    def records(self, lesson_id: str, kind: str) -> list:
        out = []
        for s in self._sessions(lesson_id):
            sid = s["hdr"].get("session", s["path"].stem)
            out += [{**r, "session": sid, "user": s["hdr"].get("user")} for r in s["recs"] if r.get("k") == kind]
        return out

    # ---- per-session views ------------------------------------------------------------------------------
    def sessions(self, lesson_id: str) -> list:
        now, out = time.time(), []
        for s in self._sessions(lesson_id):
            h, recs = s["hdr"], s["recs"]
            evs = [r for r in recs if r.get("k") == "ev"]
            last_ts = max((r.get("ts", 0) for r in recs if "ts" in r), default=0)
            ticks = s["ftr"]["ticks"] if s["ftr"] else None
            kfs = sum(1 for r in recs if r.get("k") == "kf")
            out.append({
                "session": h.get("session", s["path"].stem), "user": h.get("user", "?"),
                "start": h.get("t0"), "duration": round(last_ts, 1),
                "demo": str(h.get("session", "")).startswith("demo-"),
                "live": s["ftr"] is None and now - s["mtime"] < LIVE_WINDOW_S,
                "onsets": sum(1 for r in evs if r["kind"] == "confusion_onset"),
                "ahas": sum(1 for r in evs if r["kind"] == "aha"),
                "episodes": sum(1 for r in recs if r.get("k") == "ep"),
                "segments": len({r.get("seg") for r in evs if r["kind"] == "segment_enter"}),
                "bytes": s["bytes"], "keyframes": kfs,
                "kf_ratio": s["ftr"].get("kf_ratio") if s["ftr"] else None, "ticks": ticks,
            })
        out.sort(key=lambda x: x["start"] or 0, reverse=True)
        return out

    def session(self, lesson_id: str, session_id: str):
        for s in self._sessions(lesson_id):
            if s["hdr"].get("session", s["path"].stem) == session_id:
                recs = s["recs"]
                curve = [{"ts": r["ts"], "v": r["v"], "st": r.get("to")} for r in recs if r.get("k") in ("kf", "st")]
                curve.sort(key=lambda p: p["ts"])
                return {
                    "header": s["hdr"], "footer": s["ftr"], "bytes": s["bytes"],
                    "curve": curve,
                    "transitions": [r for r in recs if r.get("k") == "st"],
                    "events": [r for r in recs if r.get("k") == "ev"],
                    "episodes": [r for r in recs if r.get("k") == "ep"],
                    "segments": [r for r in recs if r.get("k") == "seg"],
                    "budget_notes": [r for r in recs if r.get("k") == "budget"],
                }
        return None
