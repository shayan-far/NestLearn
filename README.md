# NestLearn

An adaptive learning platform that uses EEG to notice when a student is confused and automatically offers a different explanation, written by an LLM for that student. It also logs where students get confused and where "aha" moments happen, so instructors know which parts of a lesson to fix and which are strongest.

Two pieces worth pitching, each documented in `docs/`:
- **NestTrace** (`docs/NESTTRACE.md`): tiered, bounded-error session logs. Every event and episode kept, the signal curve compressed with a guaranteed error bound, a hard per-session size cap. Roughly 150-250 KB per hour vs ~29 MB of raw EEG.
- **Concept Delta Memory** (`docs/CONCEPT_DELTA_MEMORY.md`): a fixed-size learner model that stores only what's new about each confusing or aha moment and gives the LLM a few hundred characters of context instead of the student's history.

**Hardware status:** the headset isn't available yet, so everything runs on a **simulated EEG source** that produces raw-looking signals. The same feature extraction and estimator code runs in simulation and on hardware. See `SPEC.md` for how to switch.

## Run it

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env                 # then paste your GEMINI_API_KEY (optional)
python scripts/seed_demo.py          # optional: simulated sessions for the instructor view
uvicorn backend.main:app --reload
```

- Library: http://localhost:8000/ (every `lessons/*.json` shows up automatically)
- Lesson: http://localhost:8000/lesson.html?lesson=<id>
- Instructor view: http://localhost:8000/dashboard.html
- Tests: `pytest`

Pick a lesson, tick consent, press Start, wait out the 10 s calibration, then read. Harder segments (`sim_difficulty` in the lesson JSON) make the simulated student more likely to get confused. During calibration the circle shows your camera so you can keep your face centred (local preview only: nothing is recorded or sent, it turns off when calibration ends, and if you decline the permission you get the plain breathing circle). Press **D** (or the panel icon in the navbar) to open the **debug pane**: live confidence, the raw input signal graph (streamed only while the pane is open, never stored), features, simulator buttons, the compressed LLM context, Concept Delta Memory updates, and the NestTrace log as it's written. Sustained confusion for about 4 s triggers an offer of an alternative explanation. The sun/moon button switches light and dark themes (light is the default; the choice is remembered).

Without a Gemini key everything still works: explanations come from the lesson author's alternatives, then a generic fallback message, and the UI labels which one you got.

## Adding a lesson
Drop a `.json` into `lessons/` and restart (or `POST /api/lessons/reload`). Required: `title`, `segments[]` with `id`, `title`, `body`. Optional: `description`, `subject`, `tags`, `order` (lower sorts first), and per segment `alternatives`, `quiz`, `concepts`, `sim_difficulty`. Description, tags, reading time and concept fingerprints are extracted automatically when not given.

## Layout

```
backend/
  main.py            FastAPI app, REST + WebSocket, serves frontend/
  session.py         one student: source -> features -> estimator -> NestTrace + CDM -> policy -> explainer
  config.py          settings from env vars / .env (EEG_SOURCE, GEMINI_API_KEY, LOG_BUDGET_BYTES, ...)
  lessons.py         lesson loading, library metadata, concept fingerprints
  trace.py           NestTrace writer (tiered, bounded-error session log)
  store.py           NestTrace reader for analytics + instructor view (cached by mtime)
  memory.py          Concept Delta Memory (per-student, fixed-size learner model)
  explainer.py       LLM explanations + aha recaps, with fallbacks
  llm.py             Gemini REST client (no SDK)
  policy.py          when/what to intervene; learns which modality works per student
  analytics.py       per-segment rates, episode outcomes, which explanation styles work
  authoring.py       LLM: notes -> lesson, rewrite suggestions
  signals/
    source.py        EEGSource interface, SimulatedSource, SimDriver, BrainFlowSource
    features.py      band powers -> theta_f, alpha_p, engagement, EMG, gamma
    estimator.py     calibration z-scores -> confused / engaged / bored / aha
frontend/            plain HTML/JS, no build step: index (library), lesson, dashboard, common.js, style.css
lessons/             lesson JSON
docs/                NESTTRACE.md, CONCEPT_DELTA_MEMORY.md
data/                (git-ignored) logs/<lesson>/<session>.jsonl, profiles/<student>.json
scripts/             seed_demo.py (runs the real pipeline headless), author_lesson.py
tests/               pipeline, NestTrace, CDM, LLM (mock server), API
```

## Being honest in the demo

- The estimator is rule-based and calibrated per user, not a trained model. It estimates cognitive load and engagement and interprets them as confusion.
- Sessions marked `demo-` in the dashboard are simulated. Say so.
- Simulator button presses are written to the log as `sim_force` events.
- Not run end to end here: the BrainFlow path, and live Gemini calls (the request/response format is tested against a mock server; set a key and try it before the demo).
