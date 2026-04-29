# Judge Service

Decides round winners by comparing miner-generated 3D models against a reference image.

The service runs two layers:

- a **tournament loop** that turns many miners into a single round winner, and
- a **duel judge** that decides a single head-to-head match (one miner vs another, on one prompt).

This document focuses on the logic and reasoning behind each layer. For exact thresholds and prompt strings, the code in `judge_service/judges/multi_stage.py` is the source of truth.

## Round flow (tournament layer)

Each round produces one winner from a pool of miners.

1. **Qualification.** Every miner first plays the previous round's winner ("base leader"). They have to win by a margin of at least 5% across all prompts to qualify. This stops weak submissions from polluting the timeline and gives the previous champion a meaningful defence.

2. **Main timeline.** Qualified miners are ordered by submission time and play sequentially. Whoever beats the current leader becomes the new leader. Every miner who held the lead is sent for verification — we only crown a winner whose source-and-build has been independently confirmed to actually produce their submission.

3. **Exploratory duels.** Verification is slow, so while we wait we precompute the remaining pairwise matches. If verification fails we already have results in hand to assemble a different timeline without re-judging from scratch.

4. **Verification check.** All leaders pass verification → the round is over. Any leader fails → that timeline is discarded; we restart with the remaining miners.

5. **Repeat** until a verified timeline emerges.

### Verification: how a miner is "audited"

Verification answers a single question: *if we re-run the miner's own Docker image on the same prompts, does it actually produce something at least as good as what they submitted?* If yes, the submission is real. If the regeneration is clearly worse than the submission, the submission was likely not produced by the source the miner published — and the leader is rejected.

The flow is split between two services:

1. **Judge service requests an audit** by adding the miner's hotkey to `require_audit.json`. This happens automatically the moment a miner becomes a (qualifying or local) leader.
2. **Generation orchestrator picks up the request, regenerates, and renders.** It runs the miner's Docker image on the round's prompts, then renders the resulting models into the same view bundles the judge consumes. Its job ends there — *it does not grade the result.*
3. **Judge service runs a verification duel** between the miner's submitted bundle and the freshly regenerated bundle, prompt-by-prompt, using the same multi-stage judge described below.
4. **Pass condition: 0% margin in favor of `generated`.** The audit passes if the duel comes back as a draw *or* with `generated` winning. Equivalently, the miner only fails if their submission strictly beats their own regeneration on a net of prompts — a strong sign the submission was not produced by the published code.

This 0% margin is intentionally tighter than the 5% used in the tournament. The tournament margin exists to avoid coin-flip wins between two independent miners; the audit is a self-check, so any net loss for `generated` is suspicious.

#### Why the orchestrator no longer grades

A previous design had the generation orchestrator decide pass/reject directly — by computing DINOv3 distances between submitted and regenerated outputs, plus a match-rate threshold. We moved away from this because individual generations are not deterministic: the same source code re-run on the same prompt will not produce a pixel- or geometry-identical model, and a per-prompt distance metric is too fragile a basis for accept/reject decisions. Putting the verdict in the same VLM-based duel pipeline as the tournament turns out to be both more forgiving of harmless variance and more reliable at catching real source/submission mismatches, because the same multi-stage gates that make tournament duels robust also apply here.

## Duel judge (the multi-stage judge)

A duel is "given a reference image and two 3D models, which one is a more faithful reproduction?" The output is `LEFT`, `RIGHT`, or `DRAW`. The exact same pipeline runs both kinds of duels:

- **Tournament duel:** miner A's submission vs miner B's submission. The aggregator demands a 5% margin to commit a winner.
- **Verification duel:** one miner's submission vs that same miner's freshly regenerated outputs. The aggregator passes the audit on any non-loss for `generated` (0% margin — see [Verification](#verification-how-a-miner-is-audited)).

Inside a single duel, both cases use identical logic; only the surrounding match-level threshold differs.

The judge uses a vision-language model (VLM), but a single VLM call is too noisy and too biased to trust on its own. So instead of one big question, the judge runs a pipeline of cheaper, narrower questions, each targeting a specific failure mode of 3D generation. Earlier stages can short-circuit the rest; later stages only run when the picture is still ambiguous.

### What the judge gets, per duel

- a reference image URL (the prompt),
- two prerendered "view bundles", one per model. Each bundle contains:
  - 8 white-background views: 4 front-ish (`front_left`, `front_right`, `front_below`, `front_above`) and 4 around-the-object (`right`, `back`, `left`, `top_down`),
  - 4 gray-background front-ish views (`front` plus three near-front angles),
  - one 2×2 grid image,
  - a small `embeddings.npz` of DINOv3 image embeddings for each view plus the prompt.

These are all produced upstream and live behind a CDN prefix; the judge fetches what it needs over HTTP.

### Design principles

- **Cheap before expensive.** Front prompt-match runs first because the front is the most informative angle and most obvious wins show up there immediately.
- **Order-bias mirroring.** Every comparison is run twice with sides swapped (`A,B` and `B,A`). VLMs systematically prefer one position; averaging the two cancels that out. When the two answers disagree, the angle is treated as noise.
- **Commit only on a real gap.** Each stage requires a minimum score gap before it picks a winner. Small differences are treated as draws and pass through to the next stage.
- **Fail safe, not loud.** A VLM call that fails JSON parsing falls back to a neutral score; missing embeddings skip the relevant sub-judge instead of aborting the duel.
- **One stage runs no matter what.** Side guard (Stage 4) always runs at the end — see below.

### Stage 1 — Front prompt match

**Why:** if one model is obviously a better likeness from the front, you don't need anything else.

The judge takes 4 front-ish white-background views per side and asks the VLM, for each angle, to give both models a 0–10 penalty against the reference. Each angle is asked twice (sides swapped), so 4 angles × 2 = 8 VLM calls.

All 4 angles are weighted equally, penalties are averaged, and the difference is compared to a draw threshold:

- If the weighted average penalty is high on both sides and the gap is small, both models are bad — declare a draw and let later stages decide.
- If the gap is below the draw threshold, draw.
- If signed angle votes split roughly evenly between the two sides, draw — the front view alone can't decide this one.
- Otherwise, declare a winner and stop.

If Stage 1 picks a winner, the duel ends there. Otherwise we drop into Stage 2.

### Stage 2 — Three specialist sub-judges

**Why:** when the fronts look comparable, the deciding evidence is somewhere else — the best view, rendering quality across angles, or specific features. Three lightweight specialists look at three different signals in parallel.

**2A. Best view (DINOv3 + VLM).** For each model, pick the single view that is most similar to the prompt according to DINOv3 embeddings — i.e. the model's "best foot forward". Then ask the VLM to compare those two best views (AB + BA = 2 calls). This rewards models that nail the prompt from at least one angle.

**2B. Artifact compare.** Show the VLM both models' 2×2 grids next to the reference and ask it to grade rendering quality on four axes: bounding-box artifacts (stray walls/floors), color/material accuracy, surface quality (noise, seams), and geometric detail. AB + BA = 2 calls.

**2C. Checklist verify.** First ask the VLM to break the reference image into 4–7 concrete checkable features (e.g. "4 legs", "round body", "red accent"). Then for each model, ask the VLM to verify each feature against its 2×2 grid as `yes` / `partial` / `no`. Score = sum of feature scores + bonus if the main object was named correctly. 1 + 2 = 3 calls.

Each specialist either votes for a side or abstains (gap below its own minimum). Then we combine:

- **Strong-voter override.** If best view or checklist comes back with an exceptionally large gap, that single specialist can decide on its own. The reasoning: a 7-point checklist gap or a large best-view penalty gap is hard to argue with — overriding it on a 2-of-3 vote rule would just reintroduce noise. If two strong specialists fire on opposite sides, override is suppressed.
- **Otherwise, consensus.** If at least 2 of the 3 specialists agree, that side wins. Ties (1-1-abstain, 1-1-1, all-abstain) become a draw and fall through.

### Stage 3 — Gray-background rescue (only fires when needed)

**Why:** a known gaming pattern is to produce thin, near-white geometry that is invisible against the default white background. On white the model "looks fine"; on a neutral gray it shows up as transparent or hollow. But re-running on gray costs VLM calls, so we don't always do it.

So Stage 3 is gated. The judge fetches the two gray-background `front` PNGs and computes pixel statistics on the foreground (ignoring the gray background). If the foreground is mostly pale **and** mostly low-saturation, the gate fires — that's the signature of a render that the white background was hiding. Otherwise, gray adds no information and the stage is skipped.

When the gate fires, the judge does AB + BA gray-background comparisons of the front view and applies a wider draw threshold. A clear gap on gray can promote a Stage 2 draw into a winner; an ambiguous result leaves the draw in place.

### Stage 4 — Side guard (always runs)

**Why:** every previous stage looks mostly at the front. A model could realistically win every front-facing comparison while having no back, no top, or unrelated geometry where the sides should be. Without a final check on side angles, that model would walk away with the duel.

For each model, the judge takes the four non-front angles (`right`, `back`, `left`, `top_down`) and asks the VLM, with a near-front reference view (`front_left`) shown alongside, a strict question per angle: "is this side view consistent with the reference, or is the model essentially missing / replaced / paper-thin / boxed in?" 4 angles × 2 sides = 8 VLM calls. The verdict for each angle is `OK` or `GARBAGE`.

If at least K of the 4 angles come back `GARBAGE`, that side is flagged. Then we apply a step-down rule against the primary winner from earlier stages:

- both sides flagged or both clean → keep the primary verdict,
- only the winner is flagged → demote the winner (winner → draw, draw → other side wins),
- only the loser is flagged → keep the primary verdict (it just got reinforced).

The asymmetry is intentional: side garbage is strong negative evidence, but a clean back doesn't override a clear front-side loss.

### What the judge persists

For every duel, the judge writes a compact `detail` blob with the per-stage scores: which stage decided it, the penalty/score numbers, the checklist features, side-guard verdicts, and so on. The free-text "issues" the VLM produces are dropped on purpose — they're not used by the algorithm and would balloon the per-round artifacts. The numbers are enough to reconstruct any decision after the fact.

## State files

The service reads from and writes to the competition Git repository:

| File | R/W | Description |
|------|-----|-------------|
| `state.json` | R | Current round and stage |
| `rounds/{n}/matches_matrix.csv` | R/W | All match results |
| `rounds/{n}/require_audit.json` | W | Miners the judge wants regenerated for an audit (consumed by generation orchestrator) |
| `rounds/{n}/{hotkey}/generated.json` | R | Freshly regenerated GLB/PNG locations produced by the orchestrator |
| `rounds/{n}/generation_audits.json` | R/W | Pass/reject verdict from the judge's `submitted` vs `generated` duel |
| `rounds/{n}/source_audit.json` | R | Source-level audit results (independent check on the published code) |
| `rounds/{n}/{hotkey}/duels_*.json` | W | Detailed match reports |
| `rounds/{n}/winner.json` | W | Final winner |

## Development

```bash
poetry install
poetry poe lint
```

## Running

```bash
poetry run python -m judge_service.main
```
