# NestLearn spec

## Goal
Detect confusion (and insight) while a student works through a lesson, adapt in real time, and tell instructors which segments confuse students and which land.

## Pipeline
EEG source -> window (2 s, step 0.5 s) -> features -> estimator -> NestTrace log + Concept Delta Memory -> policy -> LLM explainer -> browser. The instructor view reads the NestTrace files.

## Signals
| Feature | Definition | Meaning |
|---|---|---|
| theta_f | log theta (4-8 Hz) power, Fz/FCz | load / conflict, rises when confused |
| alpha_p | log alpha (8-12 Hz) power, Pz/P3/P4 | falls with effort, rises with mind-wandering |
| eng | log beta / (alpha + theta) | engagement index |
| emg_fp | log gamma (30-45 Hz) power, Fp1/Fp2 | brow-furrow EMG proxy |
| gamma_t | log gamma power, T8 | insight proxy |

All features are z-scored against a 10 s per-user calibration baseline (clipped to +/-4).

## Estimator
- `confusion = 0.55*z(theta_f) - 0.45*z(alpha_p) + 0.2*z(emg_fp) + behavioral evidence`, smoothed with an EMA, mapped to a 0-1 confidence with a sigmoid. State is `confused` at confidence >= 0.65.
- Behavioral evidence (10 s half-life): scroll-back 0.8, long pause 0.5, wrong quiz answer 1.0, slow quiz answer 0.4.
- `aha`: temporal gamma z > 2 within 3 s of a parietal alpha burst (z > 1.5), 10 s cooldown.
- `bored`: eng z < -1 and alpha z > 1.
- Windows with peak-to-peak > 300 uV are rejected as artifacts.

## Adaptation policy
Confusion >= 0.65 sustained 4 s, with a 25 s cooldown, offers one alternative. Candidate modalities are the lesson's alternatives plus a micro-quiz. Choice is by learned per-student preference (+1 for an aha within 20 s of the modality, -0.25 if dismissed), tie-broken by escalation order rephrase -> analogy -> example -> diagram -> quiz. Each modality is offered at most once per segment.

## Explanations
Offered modalities are rephrase, analogy and example (always available via the LLM), plus diagram if the lesson authored one, plus the segment quiz. Content comes from Gemini with Concept Delta Memory context (`docs/CONCEPT_DELTA_MEMORY.md`), falling back to the authored alternative, then a generic message. An aha triggers a one-sentence recap (once per segment).

## WebSocket protocol (`/ws?user=<name>&lesson=<id>`)
Client to server:
- `{type:"segment", id}`
- `{type:"behavior", kind:"scrollback"|"long_pause"}`
- `{type:"quiz", segment, correct, rt}`
- `{type:"intervention_response", id, accepted}`
- `{type:"finish"}`
- `{type:"debug", on}` (debug pane open: server adds `eeg {rate, ch[][]}` to `state`/`calibration` messages for the input graph)
- `{type:"request_alt"}` (manual "explain differently")
- `{type:"sim", state:"neutral"|"confused"|"aha"|"bored"}` (simulator only)

Server to client:
- `hello {mode, session, llm, profile}`
- `calibration {progress}`, `calibrated`
- `state {state, confidence, z, segment, log?}` about every 0.5 s (`log` = NestTrace stats every 2 s)
- `intervention {id, modality, segment, content|null, pending, auto}` then `explanation {id, content, source, ms, context}`
- `insight {content, source}` after an aha, `notice {text}`
- `memory {update, snapshot}` (CDM change), `trace {rec}` (every log record; debug pane)

## Session log (NestTrace)
One JSONL file per session in `data/logs/<lesson>/`. Full format and algorithm in `docs/NESTTRACE.md`. Per segment the instructor view reports confusion and aha rate (share of visiting sessions with at least one onset or aha), share of time confused, quiz accuracy, episode outcomes, and which explanation styles were followed by an aha. Each session has a timeline reconstructed from keyframes.

## Lesson format
See `lessons/gradient-descent.json`: segments with `body`, optional `alternatives` (rephrase / analogy / example / diagram, used as LLM fallbacks), optional `quiz`, optional `concepts`, and `sim_difficulty` (0-1, simulator only). Lesson-level optional fields: `description`, `subject`, `tags`, `order`.

## Moving to real hardware
1. `pip install brainflow`, then `EEG_SOURCE=brainflow BRAINFLOW_BOARD_ID=<id> SERIAL_PORT=<port>`. Board id -1 is BrainFlow's synthetic board, useful to test that code path first.
2. If BrainFlow doesn't support your board (check for the PiEEG Octopus / g.tec), implement `EEGSource.start/read/stop` in `backend/signals/source.py`. `read()` must return new samples as `(n_channels, n)` in microvolts, and `fs` must be set.
3. Edit `CHANNELS` and `GROUPS` in `features.py` to match your electrode montage.
4. Re-tune the estimator weights, the 0.65 threshold and the 0.08 z-score floor against real calibration data. The simulator's separations are cleaner than real EEG.

## Scope for the weekend
- Must: lesson player, live state gauge, one adaptive intervention, event log, simulator (all scaffolded)
- Should: heatmap dashboard (scaffolded), LLM-generated alternatives (`authoring.py`), polish the calibration flow
- Could: stronger aha detection, richer per-student modality learning, notes upload UI

## Privacy
EEG is processed in memory and never stored. Only derived states and interactions are logged. The LLM receives the lesson passage and the CDM summary (term lists, counts, style outcomes), never names, timestamps or signals. Consent is collected before the session starts.
