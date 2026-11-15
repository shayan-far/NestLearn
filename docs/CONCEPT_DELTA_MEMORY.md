# Concept Delta Memory (CDM): context for the LLM without the history

**One line for the pitch:** NestLearn's tutor knows what has confused *this* student before, and what kind of explanation clicks for them, by sending the LLM a few hundred characters that stay the same size whether the student has studied for five minutes or five months.

## The problem
When the headset says a student is stuck, we ask an LLM (Gemini by default) for a different explanation. A generic re-explanation is fine. A good tutor does better: "you found the chain rule hard last week, and analogies worked for you, so here's an analogy that builds on the bit you got." Doing that by uploading the full history is slow, costly, leaks more data than needed, and grows without limit.

## The idea
Keep a small, fixed-size model of the student, and only ever store **what's new**.

### Fingerprints (what a lesson part is about)
Every segment gets a fingerprint when lessons load (`backend/lessons.py`), with no LLM call:

- **Concepts:** a sparse vector of its top terms (TF-IDF across the lesson, recurring two-word phrases like "learning rate" kept together), or the author's own `concepts` list.
- **Traits:** four numbers for *how* it explains: math-heavy, procedural (step-by-step), definitional, dense sentences.

### Prototypes (what the student's moments look like)
For each student and each signal (`confused`, `aha`, `bored`) CDM holds at most **K = 6 prototypes**. A prototype is a compressed description of a family of moments: top concept terms, traits, a count, an evidence weight, which parts it came from, and (for aha) which explanation styles came right before.

### The update rule, on every confusion onset / aha / drift
1. **Decay** the channel's prototypes (x0.92), so recent evidence counts more.
2. **Match** the segment's fingerprint to the closest prototype (75% concept cosine + 25% trait closeness).
3. **Delta:** if it's close enough (≥ 0.42), store only the difference: concept terms the prototype doesn't have yet or under-weights, and the trait shift, folded in as a running mean. If it's the same part again with nothing new, it's a pure **reinforcement** and nothing new is stored.
4. **Novel:** otherwise it becomes a new prototype. If that makes more than K, the two most similar prototypes **fuse**, or the weakest is **evicted**.

So memory per student is bounded (K x 3 small records + counters, a few KB on disk in `data/profiles/<student>.json`) and follows them across lessons.

CDM also tracks:
- **Trait lift:** whether confusion happens more on math-heavy, procedural, definitional or dense text than on everything the student has read.
- **Explanation-style outcomes:** for each of rephrase / analogy / example / diagram / quiz, how often it was offered, accepted, and followed by an aha.

### What the LLM actually receives
When an explanation is needed for part Y, CDM ranks prototypes by `weight x (0.5 + similarity to Y)` and renders a short block. Prototypes that only describe Y itself are left out, so the model hears about the student's *other* struggles rather than an echo of this one. Real output after a student worked through gradient descent and started backpropagation:

```
- struggled with [chain, effect, rule, depends] x1
- struggled with [learning rate, parameter, update, controls] x1
- struggled with [gradient, parameter, loss, changes] x1
- had an aha on [learning rate, parameter, update] right after the analogy explanation
- had an aha on [gradient, parameter, loss] right after the rephrase explanation
- confusion is less likely on definition-heavy text
- explanation styles so far: rephrase 1/1 led to aha; analogy 1/1 led to aha
```

That block, the passage, and a style instruction (rephrase / analogy / worked example / diagram) are the whole prompt. A test feeds 300 random observations and checks that memory never exceeds K per channel and the context stays under 900 characters.

On an aha, the same memory (aha channel) drives a one-sentence "lock it in" recap shown as a toast.

## Why it's good for students, and why it's ours
- **Constant cost:** prompt size is O(K), not O(history).
- **Privacy by design:** no transcripts, no raw signals, no timestamps go to the LLM. Only term lists, counts and style outcomes.
- **Explainable:** every update is logged as `new` / `delta (+terms)` / `reinforce` / `fused` / `evicted`, and the debug pane shows it live. Teachers can read a student's profile in the instructor view.
- **Learns across lessons:** confusion about "gradient" in one lesson informs the explanation of "backpropagation" in the next.

## Fallbacks
Explainer chain (`backend/explainer.py`): Gemini → the lesson author's hand-written alternative for that style → a generic fallback message. With no API key the app runs fully on the last two, and the UI labels the source of each explanation ("Generated for you", "From the lesson author", "Offline fallback").

## Tuning knobs (`backend/memory.py`)
`K`, `GAMMA` (decay), `MERGE_SIM` (delta vs new), `FUSE_SIM` (fuse vs evict), `TOP_TERMS`.
