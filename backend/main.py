import asyncio

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles

from backend import config, llm
from backend.analytics import segment_stats
from backend.authoring import suggest_rewrite
from backend.lessons import load_lessons, public
from backend.memory import ConceptDeltaMemory, profile_path
from backend.session import Session
from backend.signals.features import CHANNELS
from backend.store import LogStore

app = FastAPI(title="NestLearn")
store = LogStore(config.LOG_DIR)
LESSONS = load_lessons(config.ROOT / "lessons")
DEFAULT_LESSON = next(iter(LESSONS), "")


def _lesson(lesson_id: str) -> dict:
    if lesson_id not in LESSONS:
        raise HTTPException(404, "unknown lesson")
    return LESSONS[lesson_id]


@app.get("/api/status")
def status():
    return {"source": config.SOURCE, "llm": llm.status(), "lessons": len(LESSONS),
            "log_budget": config.LOG_BUDGET_BYTES}


@app.get("/api/lessons")
def list_lessons():
    return [lesson["_meta"] for lesson in LESSONS.values()]


@app.post("/api/lessons/reload")
def reload_lessons():
    LESSONS.clear()
    LESSONS.update(load_lessons(config.ROOT / "lessons"))
    return {"lessons": list(LESSONS)}


@app.get("/api/lessons/{lesson_id}")
def get_lesson(lesson_id: str):
    return public(_lesson(lesson_id))


@app.get("/api/analytics/{lesson_id}")
def analytics(lesson_id: str):
    return segment_stats(store, _lesson(lesson_id))


@app.get("/api/version/{lesson_id}")
def version(lesson_id: str):
    _lesson(lesson_id)
    return {"version": store.version(lesson_id)}


@app.get("/api/sessions/{lesson_id}")
def sessions(lesson_id: str):
    _lesson(lesson_id)
    return store.sessions(lesson_id)


@app.get("/api/sessions/{lesson_id}/{session_id}")
def session_detail(lesson_id: str, session_id: str):
    _lesson(lesson_id)
    s = store.session(lesson_id, session_id)
    if not s:
        raise HTTPException(404, "unknown session")
    user = s["header"].get("user", "")
    mem = ConceptDeltaMemory(user, profile_path(config.PROFILE_DIR, user))
    s["profile"] = mem.snapshot()
    return s


@app.post("/api/rewrite/{lesson_id}/{segment_id}")
async def rewrite(lesson_id: str, segment_id: str):
    lesson = _lesson(lesson_id)
    seg = next((s for s in lesson["segments"] if s["id"] == segment_id), None)
    stats = next((s for s in segment_stats(store, lesson)["segments"] if s["id"] == segment_id), None)
    if not seg or not stats:
        raise HTTPException(404, "unknown segment")
    try:
        text = await asyncio.to_thread(suggest_rewrite, seg, stats)
    except Exception as e:  # missing key / network
        raise HTTPException(503, f"LLM unavailable: {e}")
    return {"suggestion": text}


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket, user: str = "anon", lesson: str = DEFAULT_LESSON):
    await ws.accept()
    if lesson not in LESSONS:
        await ws.close(code=4404)
        return
    session = Session(user[:40] or "anon", LESSONS[lesson], ws.send_json)
    await ws.send_json({"type": "hello", "mode": session.mode, "session": session.id, "llm": llm.status(),
                        "channels": CHANNELS, "calib_seconds": config.CALIB_SECONDS, "profile": session.memory.snapshot()})
    task = asyncio.create_task(session.run())
    reason = "disconnect"
    try:
        while True:
            msg = await ws.receive_json()
            if msg.get("type") == "finish":
                reason = "complete"
            await session.handle(msg)
    except WebSocketDisconnect:
        pass
    finally:
        task.cancel()
        session.close(reason)


class FreshStatic(StaticFiles):
    """Static files that browsers must revalidate, so a page never mixes new HTML with stale cached CSS/JS."""

    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        resp.headers["Cache-Control"] = "no-cache"
        return resp


# Static frontend last so it doesn't shadow the API routes.
app.mount("/", FreshStatic(directory=config.ROOT / "frontend", html=True), name="static")
