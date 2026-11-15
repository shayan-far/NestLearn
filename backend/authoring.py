"""LLM helpers: turn notes into a lesson, and suggest rewrites for confusing segments.
Uses the shared client in backend/llm.py (Gemini by default; key in .env)."""
import json
import re

from backend import llm

LESSON_SCHEMA = """{
  "title": "...",
  "segments": [
    {"id": "s1", "title": "...", "body": "2-4 short paragraphs",
     "alternatives": {"rephrase": "...", "analogy": "...", "example": "..."},
     "quiz": {"question": "...", "options": ["...", "...", "..."], "answer": 0},
     "sim_difficulty": 0.3}
  ]
}"""


def _ask(prompt: str, max_tokens: int = 4000) -> str:
    return llm.generate(prompt, max_tokens=max_tokens, timeout=60)


def generate_lesson(notes: str, title: str = "") -> dict:
    text = _ask(
        "Turn these study notes into an interactive lesson of 4-8 short segments. For every segment write "
        "three alternative explanations (rephrase = simpler wording, analogy, worked example) and, where it "
        "makes sense, a one-question multiple-choice quiz (answer = index of the correct option). "
        f"Return ONLY JSON matching this schema:\n{LESSON_SCHEMA}\n\nTitle hint: {title}\n\nNOTES:\n{notes}",
        max_tokens=8000,
    )
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    return json.loads(text)


def suggest_rewrite(segment: dict, stats: dict) -> str:
    helped = stats.get("helped_by") or {}
    helped_txt = ", ".join(f"{m} ({n})" for m, n in sorted(helped.items(), key=lambda kv: -kv[1])) or "none yet"
    return _ask(
        "Students wearing EEG headsets often got confused on this lesson segment "
        f"(confusion rate {stats['confusion_rate']:.0%}, quiz accuracy {stats['quiz_accuracy']}, "
        f"median confusion episode {stats.get('median_episode_s')} s). Alternative explanations that were "
        f"followed by an aha: {helped_txt}. "
        "Suggest a clearer rewrite that borrows from what worked, and explain in 2 bullets what was probably "
        "confusing.\n\n"
        f"TITLE: {segment['title']}\nTEXT:\n{segment['body']}",
        max_tokens=1200,
    )
