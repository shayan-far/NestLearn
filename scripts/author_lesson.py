"""Turn a notes file into a lesson JSON using an LLM (needs `pip install anthropic` + ANTHROPIC_API_KEY).

    python scripts/author_lesson.py notes.txt "My lesson title"
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.authoring import generate_lesson  # noqa: E402


def main() -> None:
    notes = Path(sys.argv[1]).read_text()
    title = sys.argv[2] if len(sys.argv) > 2 else Path(sys.argv[1]).stem
    lesson = generate_lesson(notes, title)
    slug = re.sub(r"[^a-z0-9]+", "-", lesson.get("title", title).lower()).strip("-")
    out = ROOT / "lessons" / f"{slug}.json"
    out.write_text(json.dumps(lesson, indent=2))
    print(f"Wrote {out}. Restart the server, then open /?lesson={slug}")


if __name__ == "__main__":
    main()
