# NestTrace: how NestLearn logs a brain-state session

**One line for the pitch:** NestTrace keeps every meaningful moment of a session and a faithful picture of the signal in between, at about 1/150th the size of the raw EEG, with a hard error guarantee and a hard size cap.

## The problem

A session produces three very different kinds of data:

| Stream | Rate | Size for 1 hour |
|---|---|---|
| Raw EEG (8 channels, 250 Hz, float32) | 2,000 samples/s | ~29 MB |
| Estimator output (confidence + 5 z-features) | 2 per second | ~0.7 MB as JSON |
| Things that happen (confusion onset, aha, help offered, quiz) | a few per minute | a few KB |

Logging only events loses the shape of the session (how long confusion lasted, how close someone got to the threshold). Logging every tick is wasteful and still grows without bound. We never store raw EEG at all (privacy).

## The design: four tiers in one append-only file

`data/logs/<lesson>/<session>.jsonl`, one JSON record per line, so it can be tailed live and survives a crash mid-session.

| Tier | Record | What it holds | Can be dropped? |
|---|---|---|---|
| 1 | `ev`, `st` | Every event, and every **debounced** state transition with an exact feature snapshot | Never |
| 2 | `ep` | Confusion **episodes**: onset, relapses merged, peak, mean feature vector, which help was offered/accepted, outcome | Never |
| 3 | `seg` | One summary per segment visit: dwell, share of time in each state, counts | Never |
| 4 | `kf` | **Keyframes** of the continuous signal, compressed with a bounded-error filter | Resolution degrades under budget pressure |

Plus a header (`hdr`) and footer (`ftr`) with compression stats.

### 1. Debounced transitions (noise out, moments in)
The estimator can flicker between "engaged" and "confused" near the threshold. A state only counts once it has held for 1 s (3 s for "drifting", which is a slow state). "Aha" is a moment, so it counts instantly. A test checks that a 0.5 s blip is not logged as a transition.

### 2. Episodes with relapse merging
Confusion that ends and comes back within 15 s on the same segment is the **same episode** (`relapses += 1`), not two. When an episode ends it waits up to 15 s to learn its outcome:

- `aha`: an aha happened (and `helped_by` names the explanation style the student accepted, if any)
- `faded`: confusion went away on its own
- `moved_on`: the student left the segment still confused (the strongest "rewrite this" signal)
- `session_end`

This is what lets the instructor view say "The gradient: 10 episodes, 8 resolved, 2 unresolved, rephrase helped 6 times".

### 3. Strict multi-channel swinging-door compression (the keyframes)
We treat the 6 channels (confidence + 5 features) as one vector and run a strict variant of the swinging-door trending algorithm used in industrial process historians:

- Keep an anchor (the last stored point). For each channel keep the range of slopes a straight line from the anchor could take and still pass within tolerance of every point seen since.
- A new point is accepted if its own slope fits in every channel's range. Otherwise the previous point is stored and becomes the anchor.

**Guarantee:** linear interpolation between stored keyframes reproduces every dropped tick within the per-channel tolerance. `tests/test_trace_memory.py::test_swinging_door_error_bound` checks this on 2,000 points.

Two NestLearn-specific twists:

- **Decision-aware tolerance.** The confidence channel is held to ±0.04 near the 0.65 confusion threshold, where a small error would flip "confused" vs "not", and relaxed up to ±0.12 far from it. Precision is spent where decisions happen.
- **Trend vs. snapshot.** Keyframes store a smoothed feature trend (EMA); every state transition stores the exact raw values. So the curve is cheap and the moments are exact. Transitions also reset the compressor, so they double as exact keyframes.

### 4. Byte budget with graceful degradation
Each session has a budget (`LOG_BUDGET_BYTES`, default 2 MB). As the file fills, keyframe tolerances widen:

| Budget used | Keyframe tolerance |
|---|---|
| < 50% | x1 |
| 50% | x2 |
| 70% | x4 |
| 85% | x8 |
| 95% | keyframes off |

Tiers 1-3 are never dropped (above 120% of budget, only low-value `behavior` pings are dropped). Each change is written as a `budget` record so readers know where resolution changed. A test drives 4,000 noisy ticks into a 12 KB budget and checks that every event survives and the file stays near the cap.

## Measured on simulated sessions

From `scripts/seed_demo.py` (real pipeline, simulated EEG, ~4 min sessions):

- ~5-10 ticks per keyframe in the curve
- ~7-15 KB per session, i.e. roughly **150-250 KB per hour** vs ~29 MB of raw EEG
- Every onset, aha, offer, response and quiz kept

## Reading it back
`backend/store.py` parses files lazily and caches them by (mtime, size), so the instructor view polls `/api/version/<lesson>` every 3 s and only re-parses sessions that grew. The timeline you see in the instructor view is drawn straight from the keyframes + transitions.

## Record reference
```
hdr  {v, t0, budget, channels, tol, session, user, lesson, mode}
ev   {ts, seg, kind, ...}         kind: calibrated, segment_enter, confusion_onset, aha, bored_onset,
                                        intervention, explanation, intervention_response, quiz, behavior,
                                        recap, sim_force, lesson_complete
st   {ts, seg, from, to, dwell, v[6]}
kf   {ts, v[6]}                   v = [confidence, theta_f, alpha_p, eng, emg_fp, gamma_t]
ep   {seg, start, end, dur, peak, mean_z[5], relapses, offers[], outcome, helped_by}
seg  {seg, enter, exit, dwell, share{state: fraction}, counts{}}
budget {frac, tol_mult}
ftr  {ts, reason, ticks, keyframes, transitions, events, episodes, bytes, tol_mult, kf_ratio}
```
`ts` is seconds since `t0`. `sim_force` records when someone used the simulator buttons, so the log stays honest about forced states.
