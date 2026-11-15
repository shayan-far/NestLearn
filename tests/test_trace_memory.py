import asyncio
import json
import math
import random

import pytest

from backend import config
from backend.lessons import load_lessons
from backend.memory import K, ConceptDeltaMemory
from backend.session import Session
from backend.signals.source import SimulatedSource
from backend.store import LogStore
from backend.trace import BASE_TOL, SessionTrace, SwingingDoor, reconstruct

LESSONS = load_lessons(config.ROOT / "lessons")
GD = LESSONS["gradient-descent"]


# ---- NestTrace -------------------------------------------------------------------------------------
def test_swinging_door_error_bound():
    rng = random.Random(3)
    tol = [0.05, 0.5]
    sdt = SwingingDoor(2)
    pts, kept = [], []
    for i in range(2000):
        t = i * 0.5
        v = [0.5 + 0.4 * math.sin(t / 7) + rng.gauss(0, 0.01), 2 * math.cos(t / 11) + rng.gauss(0, 0.1)]
        pts.append((t, v))
        p = sdt.push(t, v, tol)
        if p:
            kept.append(p)
    if sdt.pending():
        kept.append(sdt.pending())
    assert len(kept) < len(pts) / 4                    # real compression
    for t, v in pts:                                   # ...with a hard error bound
        r = reconstruct(kept, t)
        assert all(abs(a - b) <= tl + 1e-9 for a, b, tl in zip(r, v, tol))


def _out(conf, state="engaged"):
    return {"state": state, "confidence": conf, "z": {k: 0.0 for k in ["theta_f", "alpha_p", "eng", "emg_fp", "gamma_t"]}}


def test_trace_debounce_episode_and_footer(tmp_path):
    tr = SessionTrace(tmp_path / "s.jsonl", {"session": "x", "user": "u", "lesson": "l"}, 100_000, t0=0.0)
    tr.segment(0.0, "s1")
    t = 0.0
    for state, secs in [("engaged", 3), ("confused", 0.5), ("engaged", 2), ("confused", 6)]:
        for _ in range(int(secs * 2)):
            t += 0.5
            tr.tick(t, _out(0.9 if state == "confused" else 0.2, state))
    tr.intervention(t, "analogy", True)
    tr.intervention_response(t, True)
    t += 0.5
    tr.tick(t, _out(0.3, "aha"))
    tr.close(t + 1)
    recs = [json.loads(line) for line in open(tmp_path / "s.jsonl")]
    st = [r for r in recs if r["k"] == "st"]
    assert [r["to"] for r in st] == ["confused", "aha"]        # the 0.5 s blip was debounced away
    ep = [r for r in recs if r["k"] == "ep"]
    assert len(ep) == 1 and ep[0]["outcome"] == "aha" and ep[0]["helped_by"] == "analogy"
    assert recs[-1]["k"] == "ftr" and recs[-1]["episodes"] == 1


def test_trace_relapse_merges_into_one_episode(tmp_path):
    tr = SessionTrace(tmp_path / "s.jsonl", {}, 100_000, t0=0.0)
    tr.segment(0.0, "s1")
    t = 0.0
    for state, secs in [("confused", 4), ("engaged", 5), ("confused", 4), ("engaged", 30)]:
        for _ in range(int(secs * 2)):
            t += 0.5
            tr.tick(t, _out(0.9 if state == "confused" else 0.2, state))
    tr.close(t)
    eps = [json.loads(line) for line in open(tmp_path / "s.jsonl") if '"k":"ep"' in line]
    assert len(eps) == 1 and eps[0]["relapses"] == 1 and eps[0]["outcome"] == "faded"


def test_trace_budget_degrades_keyframes_not_events(tmp_path):
    rng = random.Random(1)
    tr = SessionTrace(tmp_path / "s.jsonl", {}, budget=12_000, t0=0.0)
    tr.segment(0.0, "s1")
    for i in range(4000):
        o = _out(rng.random())
        o["z"] = {k: rng.uniform(-3, 3) for k in o["z"]}
        tr.tick(i * 0.5, o)
        if i % 400 == 0:
            tr.event(i * 0.5, "quiz", correct=True, rt=3)
    tr.close(2000)
    recs = [json.loads(line) for line in open(tmp_path / "s.jsonl")]
    assert sum(r["k"] == "ev" and r["kind"] == "quiz" for r in recs) == 10   # every event survived
    assert any(r["k"] == "budget" for r in recs)
    assert recs[-1]["tol_mult"] is None                                     # keyframes switched off
    assert (tmp_path / "s.jsonl").stat().st_size < 12_000 * 1.3


# ---- Concept Delta Memory ---------------------------------------------------------------------------
def test_memory_delta_reinforce_and_new():
    m = ConceptDeltaMemory("t")
    s3, s4 = GD["segments"][2], GD["segments"][3]
    a = m.observe("confused", "gd:s3", s3["_fp"])
    b = m.observe("confused", "gd:s3", s3["_fp"])
    assert a["op"] == "new" and b["op"] == "reinforce" and b["delta"] == []
    c = m.observe("confused", "gd:s4", s4["_fp"])
    assert c["op"] in ("new", "delta")


def test_memory_is_bounded_and_context_is_constant_size():
    m = ConceptDeltaMemory("t")
    rng = random.Random(0)
    vocab = [f"term{i}" for i in range(200)]
    sizes = []
    for i in range(300):
        fp = {"concepts": {w: rng.random() for w in rng.sample(vocab, 6)}, "traits": [rng.random() for _ in range(4)]}
        m.observe(rng.choice(["confused", "aha"]), f"x:{i}", fp, via=rng.choice(["analogy", "example"]))
        m.offered("analogy")
        sizes.append(m.context(fp)["chars"])
    assert all(len(p) <= K for p in m.protos.values())
    assert max(sizes) < 900


def test_memory_persists(tmp_path):
    m = ConceptDeltaMemory("p", tmp_path / "p.json")
    m.observe("confused", "gd:s3", GD["segments"][2]["_fp"])
    m2 = ConceptDeltaMemory("p", tmp_path / "p.json")
    assert len(m2.protos["confused"]) == 1


# ---- lessons + end-to-end headless session ------------------------------------------------------------
def test_lesson_metadata_extracted():
    meta = GD["_meta"]
    assert meta["segments"] == 5 and meta["minutes"] >= 1 and meta["tags"] and meta["description"]


def test_headless_session_writes_readable_log(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CALIB_SECONDS", 3)
    sent = []

    async def send(msg):
        sent.append(msg)

    async def go():
        clock = [1000.0]
        src = SimulatedSource(seed=5)
        s = Session("tester", GD, send, clock=lambda: clock[0], use_llm=False, session_id="t-1", source=src,
                    log_dir=tmp_path / "logs", profile_dir=tmp_path / "prof")

        async def tick():
            clock[0] += 0.5
            await s.tick(clock[0], src.generate(125))
            await s.drain()

        while s.calibrating:
            await tick()
        await s.handle({"type": "segment", "id": "s3"}, clock[0])
        s.driver.manual("confused", clock[0])
        for _ in range(20):
            await tick()
        await s.handle({"type": "request_alt"}, clock[0])
        await s.drain()
        s.close("complete")

    asyncio.run(go())
    kinds = {m["type"] for m in sent}
    assert {"calibrated", "state", "intervention", "explanation", "memory", "trace"} <= kinds
    expl = next(m for m in sent if m["type"] == "explanation")
    assert expl["source"] in ("lesson", "fallback") and expl["content"]
    store = LogStore(tmp_path / "logs")
    sess = store.sessions("gradient-descent")
    assert sess[0]["onsets"] >= 1
    detail = store.session("gradient-descent", "t-1")
    assert detail["curve"] and detail["footer"]["reason"] == "complete"


def test_explainer_falls_back_without_key(monkeypatch):
    from backend import explainer
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    seg = dict(GD["segments"][0])
    seg["alternatives"] = {}
    r = asyncio.run(explainer.explain(GD, seg, "analogy"))
    assert r["source"] == "fallback" and r["text"] == explainer.FALLBACK


@pytest.mark.parametrize("path", ["/api/lessons", "/api/status", "/api/analytics/gradient-descent",
                                  "/api/sessions/gradient-descent", "/", "/lesson.html", "/dashboard.html"])
def test_api_routes(path):
    from fastapi.testclient import TestClient

    from backend.main import app
    assert TestClient(app).get(path).status_code == 200


def test_gemini_request_and_parse(monkeypatch):
    """Spins up a fake Gemini endpoint to check the request we send and that we parse the reply."""
    import http.server
    import threading

    from backend import explainer, llm
    seen = {}

    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            seen["path"], seen["key"] = self.path, self.headers.get("x-goog-api-key")
            seen["body"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            out = json.dumps({"candidates": [{"content": {"parts": [{"text": "thinking", "thought": True},
                                                                  {"text": "Picture a hill in fog."}]}}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setattr(llm, "GEMINI_URL", f"http://127.0.0.1:{srv.server_port}/v1beta/models/{{model}}:generateContent")
    monkeypatch.setattr(config, "GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(config, "LLM_PROVIDER", "gemini")
    mem = ConceptDeltaMemory("t")
    mem.observe("confused", "gradient-descent:s2", GD["segments"][1]["_fp"])
    r = asyncio.run(explainer.explain(GD, GD["segments"][2], "analogy", mem))
    srv.shutdown()
    assert r["source"] == "llm" and r["text"] == "Picture a hill in fog."
    assert seen["key"] == "test-key" and seen["path"].endswith(f"{config.GEMINI_MODEL}:generateContent")
    prompt = seen["body"]["contents"][0]["parts"][0]["text"]
    assert "LEARNER NOTES" in prompt and "struggled with" in prompt and "systemInstruction" in seen["body"]
