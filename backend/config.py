"""All tunables in one place. Override with environment variables or a `.env` file in the project root."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader (KEY=value lines, # comments). Real env vars win over the file."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


_load_dotenv(ROOT / ".env")

# "sim" (default, no hardware needed) or "brainflow" (real board via BrainFlow)
SOURCE = os.getenv("EEG_SOURCE", "sim")

WINDOW_S = 2.0          # analysis window length (seconds)
TICK_S = 0.5            # how often we compute a new state estimate
CALIB_SECONDS = float(os.getenv("CALIB_SECONDS", "10"))

DATA_DIR = Path(os.getenv("DATA_DIR", ROOT / "data"))
LOG_DIR = DATA_DIR / "logs"            # NestTrace session logs (one .jsonl per session)
PROFILE_DIR = DATA_DIR / "profiles"    # Concept Delta Memory, one .json per student

# NestTrace: per-session byte budget. Keyframe precision degrades gracefully as a session approaches it;
# events, transitions and episodes are never dropped. See docs/NESTTRACE.md.
LOG_BUDGET_BYTES = int(os.getenv("LOG_BUDGET_BYTES", str(2_000_000)))

# LLM (explanations, aha recaps, rewrite suggestions). Put GEMINI_API_KEY in .env (see .env.example).
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "gemini")        # "gemini" or "none"
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")
LLM_TIMEOUT_S = float(os.getenv("LLM_TIMEOUT_S", "12"))

# BrainFlow settings (only used when EEG_SOURCE=brainflow).
# -1 is BrainFlow's built-in SYNTHETIC board, handy for testing that code path without hardware.
BRAINFLOW_BOARD_ID = int(os.getenv("BRAINFLOW_BOARD_ID", "-1"))
SERIAL_PORT = os.getenv("SERIAL_PORT", "")
