"""Fill the instructor view with SIMULATED sessions, produced by the real pipeline.

Each demo student runs headless through the actual Session code (simulated EEG -> features -> estimator
-> NestTrace -> Concept Delta Memory -> policy) on a fast simulated clock, with a scripted student who
reads, sometimes accepts help, and answers quizzes. So demo logs have exactly the same format as live ones.
Session ids start with 'demo-' and the dashboard labels them as simulated. LLM calls are skipped.

    python scripts/seed_demo.py [n_students] [lesson_id|all]
"""
import asyncio
import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend import config  # noqa: E402
from backend.lessons import load_lessons  # noqa: E402
from backend.session import Session  # noqa: E402
from backend.signals.source import SimulatedSource  # noqa: E402


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


async def run_student(lesson: dict, i: int, start: float, rng: random.Random) -> int:
    inbox = []

    async def send(msg):
        inbox.append(msg)

    clock = Clock(start)
    src = SimulatedSource(seed=1000 + i)
    s = Session(f"demo-student-{i:02d}", lesson, send, clock=clock, use_llm=False,
                session_id=f"demo-{i:02d}", source=src)
    step = int(src.fs * config.TICK_S)

    async def tick():
        clock.t += config.TICK_S
        await s.tick(clock.t, src.generate(step))
        await s.drain()

    while s.calibrating:
        await tick()

    patience = rng.uniform(0.6, 0.95)        # how likely this student accepts offered help
    for seg in lesson["segments"]:
        await s.handle({"type": "segment", "id": seg["id"]}, clock.t)
        d = seg.get("sim_difficulty", 0.3)
        if d < 0.3 and rng.random() < 0.2:
            s.driver.manual("bored", clock.t)
        dwell = rng.uniform(18, 35) + 40 * d
        end, respond_at, pending = clock.t + dwell, None, None
        while clock.t < end:
            await tick()
            for m in inbox:
                if m["type"] == "intervention" and m.get("auto"):
                    pending, respond_at = m, clock.t + rng.uniform(1, 3)
            inbox.clear()
            if pending and clock.t >= respond_at:
                ok = rng.random() < patience
                await s.handle({"type": "intervention_response", "id": pending["id"], "accepted": ok}, clock.t)
                if ok:
                    end = max(end, clock.t + 10)   # reading the alternative takes a moment
                pending = None
            if rng.random() < 0.004:
                await s.handle({"type": "behavior", "kind": "scrollback"}, clock.t)
        if seg.get("quiz"):
            p = 1 - 0.55 * d
            await s.handle({"type": "quiz", "segment": seg["id"], "correct": rng.random() < p,
                            "rt": round(rng.uniform(4, 25), 1)}, clock.t)
            for _ in range(4):
                await tick()
    await s.handle({"type": "finish"}, clock.t)
    s.close("complete")
    return s.trace.bytes


def main(n: int = 16, which: str = "all") -> None:
    lessons = load_lessons(ROOT / "lessons")
    targets = lessons.values() if which == "all" else [lessons[which]]
    if config.PROFILE_DIR.exists():
        for f in config.PROFILE_DIR.glob("demo-student-*.json"):
            f.unlink()   # fresh demo profiles; they then accumulate across lessons, which is the point of CDM
    for lesson in targets:
        d = config.LOG_DIR / lesson["id"]
        if d.exists():
            for f in d.glob("demo-*.jsonl"):
                f.unlink()
        rng = random.Random(7)
        t0 = time.time() - n * 420 - 600
        total = 0
        for i in range(n):
            total += asyncio.run(run_student(lesson, i, t0 + i * 420, rng))
        print(f"{lesson['id']}: {n} simulated sessions, {total / 1024:.0f} KB of NestTrace logs in {d}")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 16, sys.argv[2] if len(sys.argv) > 2 else "all")
