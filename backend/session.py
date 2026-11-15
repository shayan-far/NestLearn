"""One live student session: source -> features -> estimator -> NestTrace log + Concept Delta Memory
-> policy -> LLM explainer -> browser.

The session is transport-agnostic: it pushes messages through an async `send` callable and takes an
injectable clock, so the same code runs behind the WebSocket and headless (scripts/seed_demo.py, tests).
"""
import asyncio
import time
import traceback
import uuid
import zlib

import numpy as np

from backend import config, explainer
from backend.memory import ConceptDeltaMemory, profile_path
from backend.policy import LADDER, AdaptationPolicy
from backend.signals.estimator import Estimator
from backend.signals.features import CHANNELS, extract, is_artifact
from backend.signals.source import SimDriver, SimulatedSource, make_source
from backend.trace import SessionTrace, session_path

LLM_MODALITIES = ["rephrase", "analogy", "example"]   # the explainer can always produce these


class Session:
    def __init__(self, user: str, lesson: dict, send, clock=time.time, use_llm: bool = True,
                 session_id: str = None, source=None, log_dir=None, profile_dir=None):
        self.user, self.lesson, self.lesson_id = user, lesson, lesson["id"]
        self.send_raw, self.clock, self.use_llm = send, clock, use_llm
        self.segments = {s["id"]: s for s in lesson["segments"]}
        self.id = session_id or f"{user}-{uuid.uuid4().hex[:6]}"
        seed = zlib.crc32(user.encode()) % 10_000
        self.source = source or make_source(seed=seed)
        self.source.start()
        self.driver = SimDriver(self.source, seed=seed) if isinstance(self.source, SimulatedSource) else None
        self.mode = "sim" if self.driver else config.SOURCE
        self.est, self.policy = Estimator(), AdaptationPolicy()
        self.win = int(self.source.fs * config.WINDOW_S)
        self.buf = np.zeros((len(CHANNELS), 0))
        self.segment = None
        self.calibrating = True
        now = clock()
        self.calib_end = now + config.CALIB_SECONDS
        self.trace = SessionTrace(
            session_path(log_dir or config.LOG_DIR, self.lesson_id, self.id),
            {"session": self.id, "user": user, "lesson": self.lesson_id, "mode": self.mode},
            config.LOG_BUDGET_BYTES, now)
        self.trace.listeners.append(self._on_trace)
        self.memory = ConceptDeltaMemory(user, profile_path(profile_dir or config.PROFILE_DIR, user))
        self.tasks: set = set()
        self.n_offer = 0
        self.offers: dict = {}            # id -> modality
        self.recapped: set = set()
        self.ticks = 0
        self.debug = False                # browser's debug pane is open -> also stream the input signal

    # ---- plumbing --------------------------------------------------------------------------------------
    async def send(self, msg: dict) -> None:
        try:
            await self.send_raw(msg)
        except Exception:
            pass   # socket gone; the run loop notices on its next tick

    def _on_trace(self, rec: dict) -> None:
        # forward everything the log writes to the debug pane (fire-and-forget)
        self._spawn(self.send({"type": "trace", "rec": rec}))

    def _spawn(self, coro) -> None:
        try:
            t = asyncio.get_running_loop().create_task(coro)
        except RuntimeError:
            coro.close()
            return
        self.tasks.add(t)
        t.add_done_callback(self.tasks.discard)

    async def drain(self) -> None:
        while self.tasks:
            await asyncio.gather(*list(self.tasks), return_exceptions=True)

    def ref(self, seg_id=None) -> str:
        return f"{self.lesson_id}:{seg_id or self.segment}"

    def _input(self) -> dict:
        """Last tick of the raw input, decimated to ~50 Hz, for the debug pane's input graph.
        Display only: sent while the pane is open, never written to the log."""
        n = max(1, int(self.source.fs * config.TICK_S))
        step = max(1, self.source.fs // 50)
        chunk = self.buf[:, -n:][:, ::step]
        return {"rate": self.source.fs // step, "ch": [[round(float(x), 1) for x in row] for row in chunk]}

    def available(self) -> list:
        seg = self.segments.get(self.segment, {})
        mods = set(seg.get("alternatives", {}).keys()) | set(LLM_MODALITIES)
        if seg.get("quiz"):
            mods.add("quiz")
        return [m for m in LADDER if m in mods]

    # ---- interventions -------------------------------------------------------------------------------------
    async def offer(self, modality: str, auto: bool) -> None:
        now = self.clock()
        seg = self.segments[self.segment]
        self.n_offer += 1
        oid = self.n_offer
        self.offers[oid] = modality
        self.memory.offered(modality)
        self.trace.intervention(now, modality, auto, source="quiz" if modality == "quiz" else "pending")
        if modality == "quiz":
            await self.send({"type": "intervention", "id": oid, "modality": modality, "segment": self.segment,
                             "content": seg["quiz"], "auto": auto, "source": "lesson"})
            return
        await self.send({"type": "intervention", "id": oid, "modality": modality, "segment": self.segment,
                         "content": None, "pending": True, "auto": auto})
        self._spawn(self._explain(oid, seg, modality))

    async def _explain(self, oid: int, seg: dict, modality: str) -> None:
        res = await explainer.explain(self.lesson, seg, modality, self.memory, self.use_llm)
        self.trace.event(self.clock(), "explanation", id=oid, modality=modality, source=res["source"],
                         ms=res["ms"], ctx_chars=res["context"]["chars"], error=res.get("error"))
        await self.send({"type": "explanation", "id": oid, "segment": seg["id"], "modality": modality,
                         "content": res["text"], "source": res["source"], "ms": res["ms"],
                         "context": res["context"]["text"], "error": res.get("error")})

    async def _recap(self, seg: dict) -> None:
        res = await explainer.recap(self.lesson, seg, self.memory, self.use_llm)
        self.trace.event(self.clock(), "recap", source=res["source"])
        await self.send({"type": "insight", "segment": seg["id"], "content": res["text"], "source": res["source"],
                         "context": res["context"]["text"]})

    # ---- background loop ------------------------------------------------------------------------------------
    async def run(self) -> None:
        while True:
            await asyncio.sleep(config.TICK_S)
            try:
                await self.tick()
            except RuntimeError:
                return
            except Exception:
                traceback.print_exc()

    async def tick(self, now: float = None, samples: np.ndarray = None) -> None:
        now = now if now is not None else self.clock()
        new = samples if samples is not None else self.source.read()
        if new.shape[1]:
            self.buf = np.concatenate([self.buf, new], axis=1)[:, -self.win:]
        if self.buf.shape[1] < self.win:
            return
        if is_artifact(self.buf):
            await self.send({"type": "state", "state": "artifact", "confidence": 0, "segment": self.segment})
            return
        feats = extract(self.buf, self.source.fs)

        if self.calibrating:
            if self.driver:
                self.driver.force_neutral()
            self.est.add_baseline(feats)
            if now >= self.calib_end and len(self.est.base) >= 5:
                self.est.finish_calibration()
                self.calibrating = False
                self.trace.event(now, "calibrated", windows=len(self.est.base))
                await self.send({"type": "calibrated"})
            else:
                progress = min(1.0, 1 - (self.calib_end - now) / config.CALIB_SECONDS)
                msg = {"type": "calibration", "progress": max(0.0, progress)}
                if self.debug:
                    msg["eeg"] = self._input()
                await self.send(msg)
            return

        if self.driver:
            self.driver.tick(now)
        out = self.est.update(feats, now)
        self.ticks += 1
        msg = {"type": "state", **out, "segment": self.segment}
        if self.ticks % 4 == 0:
            msg["log"] = self.trace.summary()
        if self.debug:
            msg["eeg"] = self._input()
        await self.send(msg)

        for state, ts in self.trace.tick(now, out):
            await self._on_transition(state, out)

        modality = self.policy.update(out, self.segment, self.available(), now)
        if modality:
            await self.offer(modality, auto=True)

    async def _on_transition(self, state: str, out: dict) -> None:
        if not self.segment:
            return
        seg = self.segments[self.segment]
        fp = seg["_fp"]
        if state == "confused":
            upd = self.memory.observe("confused", self.ref(), fp, intensity=out["confidence"], now=self.clock())
        elif state == "aha":
            last = self.trace.last_offer
            via = last[0] if last and self.clock() - last[1] < 20 else None
            upd = self.memory.observe("aha", self.ref(), fp, intensity=1.0, via=via, now=self.clock())
            if self.segment not in self.recapped:
                self.recapped.add(self.segment)
                self._spawn(self._recap(seg))
        elif state == "bored":
            upd = self.memory.observe("bored", self.ref(), fp, intensity=0.5, now=self.clock())
        else:
            return
        await self.send({"type": "memory", "update": upd, "snapshot": self.memory.snapshot()})

    # ---- messages from the browser ------------------------------------------------------------------------
    async def handle(self, msg: dict, now: float = None) -> None:
        now = now if now is not None else self.clock()
        t = msg.get("type")
        if t == "segment" and msg.get("id") in self.segments:
            self.segment = msg["id"]
            self.trace.segment(now, self.segment)
            self.memory.expose(self.ref(), self.segments[self.segment]["_fp"])
            if self.driver:
                self.driver.on_segment(self.segments[self.segment].get("sim_difficulty", 0.3), now)
        elif t == "debug":
            self.debug = bool(msg.get("on"))
        elif t == "behavior":
            self.est.add_evidence(msg.get("kind", ""), now)
            self.trace.event(now, "behavior", what=msg.get("kind"))
        elif t == "quiz":
            correct = bool(msg.get("correct"))
            self.trace.event(now, "quiz", correct=correct, rt=msg.get("rt"))
            if not correct:
                self.est.add_evidence("quiz_wrong", now)
            elif (msg.get("rt") or 0) > 20:
                self.est.add_evidence("quiz_slow", now)
        elif t == "sim" and self.driver:
            self.driver.manual(msg.get("state", "neutral"), now)
            self.trace.event(now, "sim_force", state=msg.get("state"))   # so the log is honest about it
        elif t == "intervention_response":
            accepted = bool(msg.get("accepted"))
            self.trace.intervention_response(now, accepted)
            if accepted:
                m = self.offers.get(msg.get("id")) or (self.policy.last[0] if self.policy.last else None)
                if m:
                    self.memory.accepted(m)
                if self.driver:
                    self.driver.on_intervention_accepted(now)
            else:
                self.policy.rejected()
        elif t == "request_alt" and self.segment:
            m = self.policy.force(self.segment, self.available(), now)
            if m:
                await self.offer(m, auto=False)
            else:
                await self.send({"type": "notice", "text": "That's every alternative for this part. "
                                                           "Try the next part, then come back."})
        elif t == "finish":
            self.trace.event(now, "lesson_complete")

    def close(self, reason: str = "disconnect") -> None:
        for t in list(self.tasks):
            t.cancel()
        self.trace.listeners.clear()
        self.trace.close(self.clock(), reason)
        self.source.stop()
