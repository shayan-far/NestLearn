"""Generates alternative explanations (on confusion) and short recaps (on aha) with an LLM.

Context comes from Concept Delta Memory (backend/memory.py), never from raw history. Fallback chain:
LLM -> the lesson's hand-written alternative for that modality -> a generic fallback message.
"""
import asyncio
import time

from backend import llm

STYLE = {
    "rephrase": "Re-explain it in simpler, plainer words. Shorter sentences, no new jargon.",
    "analogy": "Explain it through one concrete everyday analogy, then connect the analogy back to the idea in one sentence.",
    "example": "Walk through one small, concrete worked example with real numbers or a specific case.",
    "diagram": "Describe a simple picture the student could sketch (what is on each axis or box, what the arrows mean), then what it shows.",
}

SYSTEM = (
    "You are a patient tutor inside an adaptive lesson. The student appears to be stuck on the passage below. "
    "Write ONE alternative explanation of that passage only. Use the learner notes to choose framing: lean on "
    "styles that worked before, avoid repeating framings that confused them, and bridge from ideas that clicked. "
    "Do not mention brain signals, EEG, monitoring, or the notes themselves. Plain text, no headings, "
    "at most 110 words. Address the student as 'you'."
)
SYSTEM_AHA = (
    "You are a tutor. The student just had an 'aha' moment on the passage below. Write ONE short sentence "
    "(max 30 words) that names the key idea they just got, so it sticks. Warm, not gushing. No emoji. "
    "Do not mention brain signals or monitoring."
)

FALLBACK = ("Fresh explanations aren't available right now. Try reading the part above one sentence at a time, "
            "and pause on the first sentence that feels unclear. That's usually where the gap is.")
FALLBACK_AHA = "Nice, that one clicked. Try saying the main idea of \"{title}\" back to yourself in one sentence."


def _prompt(lesson: dict, seg: dict, modality: str, ctx: str, extra: str = "") -> str:
    return (f"LESSON: {lesson.get('title', '')}\nPART: {seg.get('title', '')}\n\nPASSAGE:\n{seg.get('body', '')}\n\n"
            f"LEARNER NOTES (compressed history):\n{ctx}\n\n{extra}").strip()


async def explain(lesson: dict, seg: dict, modality: str, memory=None, use_llm: bool = True) -> dict:
    t0 = time.time()
    ref = f"{lesson['id']}:{seg['id']}"
    ctx = memory.context(seg["_fp"], "confused", exclude_ref=ref) if memory else {"text": "- none", "parts": {}, "chars": 6}
    authored = seg.get("alternatives", {}).get(modality)
    if use_llm and llm.available():
        extra = f"STYLE: {STYLE.get(modality, STYLE['rephrase'])}"
        if authored:
            extra += f"\n\nThe author's own {modality} (do not copy it; offer a different angle):\n{authored}"
        try:
            text = await asyncio.to_thread(llm.generate, _prompt(lesson, seg, modality, ctx["text"], extra), SYSTEM, 400)
            return {"text": text, "source": "llm", "ms": int((time.time() - t0) * 1000), "context": ctx}
        except llm.LLMError as e:
            err = str(e)
    else:
        err = "llm disabled"
    if authored:
        return {"text": authored, "source": "lesson", "ms": int((time.time() - t0) * 1000), "context": ctx, "error": err}
    return {"text": FALLBACK, "source": "fallback", "ms": int((time.time() - t0) * 1000), "context": ctx, "error": err}


async def recap(lesson: dict, seg: dict, memory=None, use_llm: bool = True) -> dict:
    ctx = memory.context(seg["_fp"], "aha", exclude_ref=f"{lesson['id']}:{seg['id']}") if memory else {"text": "- none", "parts": {}, "chars": 6}
    if use_llm and llm.available():
        try:
            text = await asyncio.to_thread(llm.generate, _prompt(lesson, seg, "recap", ctx["text"]), SYSTEM_AHA, 120)
            return {"text": text, "source": "llm", "context": ctx}
        except llm.LLMError as e:
            return {"text": FALLBACK_AHA.format(title=seg.get("title", "this part")), "source": "fallback",
                    "context": ctx, "error": str(e)}
    return {"text": FALLBACK_AHA.format(title=seg.get("title", "this part")), "source": "fallback", "context": ctx}
