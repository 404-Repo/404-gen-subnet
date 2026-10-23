# Judge Service

Decides round winners by comparing miner-generated 3D models against a reference image.

The service runs two layers:

- a **tournament loop** that turns many miners into a single round winner, and
- a **duel judge** that decides a single head-to-head match (one miner vs another, on one prompt).

This document focuses on the logic and reasoning behind each layer. For exact thresholds and prompt strings, the code in `judge_service/judges/multi_stage.py` is the source of truth.

## Round flow (tournament layer)

Each competitor provides one result bundle: miners use
`rounds/{n}/{hotkey}/submitted.json`, and the previous leader uses
`rounds/{n}/leader/generated_1.json`. Both use the same result schema. The same
Three.js modules, rendered views and embeddings are compared throughout the round.
How those outputs were produced is outside the judge's boundary.

1. **Qualification.** Miners play the base leader. A margin of at least
   `config.win_margin` qualifies them. The judge follows the collector's order
   in `submissions.json`. The collector sorts entries by
   `(revealed_at_block, tie_breaker)`. Every entry must include `tie_breaker`,
   which the collector stores as the lowercase hex BLAKE2b digest of
   `f"{seed}:{hotkey}".encode()`.
2. **Timeline.** Qualified miners play the current chain defender in that order.
   A margin of at least `win_margin` advances the challenger. Every local winner
   gets one validation request, including provisional leaders whose own checks
   are still pending.
3. **Exploratory duels.** While external verdicts are pending, remaining pairwise
   matches between non-rejected qualifiers fill the existing match cache.
4. **Eligibility.** A failed validation **or** a failed source audit rejects a
   candidate. Its successor is evaluated against the remaining chain using cached
   matches; changing defenders never requires another validation pass.
5. **Finalization.** Every local winner in the surviving chain must have both a
   passed validation and a passed source audit. Verification also requires a
   verified defender. Missing verdicts keep the round open. The base leader is the
   existing verified baseline; with no qualifiers it retains the round.

A single candidate-validation pass does not limit the number of timeline rebuilds,
miner matches, or stages inside a VLM duel.

## Judging and validation boundary

The judge owns tournament comparisons, match reports, ordering, caching and winner
selection. The validator owns replay execution, recorded request/response matching,
output verification and budget checks. The judge neither regenerates candidate
outputs nor compares a submission with a regenerated copy.

Validation uses its own Git request and result channels. For round 7:

`rounds/7/validation_requests.json` (judge writes, validator reads):

```json
[
  {"hotkey": "miner-a"},
  {"hotkey": "miner-b"}
]
```

Requests are append-only and deduplicated by hotkey within the round, including
across judge restarts. There is no defender or repeat index in a request.

`rounds/7/validation_results.json` (validator writes, judge reads):

```json
{
  "miner-a": {"verdict": "passed", "reason": ""},
  "miner-b": {"verdict": "failed", "reason": "Replay output differs for prompt chair"}
}
```

The round path and map key identify one validation result. The validator publishes
one terminal verdict per requested hotkey, retains previously published results
when adding candidates, and supplies a nonempty reason for a failure. A missing
file or missing hotkey means queued/in-progress validation: the judge waits. The
validator must leave infrastructure failures unresolved and retry them, rather
than publish a miner failure. Storage/read errors and malformed result files
propagate as service errors; the judge does not convert them into a rejection.

Source audit remains independent: its producer still uses `save_source_audits`
and writes `source_audit.json` as a list of `{hotkey, verdict, reason}` objects.
The judge reads it through `get_source_audits`. A source failure rejects immediately,
even if validation passed or is absent; a missing source audit blocks verification,
even when validation passed. A validation failure similarly rejects without waiting
for a source verdict. Neither service infers or overwrites the other's verdict.
The judge announces eligibility through timeline changes.

Final winner metadata comes from the submitted repository and commit, or the
existing leader record on a defense. `winner.json` needs no Docker image or GPU
declaration. The integrated round-manager uses this source identity and writes
`docker=""` for new leaders; the shared leader model retains that empty-default
string for legacy weight setters. The pending producer migrations are listed in
[shared/README.md](../shared/README.md#required-producer-migrations).

## Duel judge (the multi-stage judge)

A duel is "given a reference image and two 3D models, which one is a more faithful
reproduction?" The output is `LEFT`, `RIGHT`, or `DRAW`. This pipeline is used for
miner-vs-miner tournament matches. Validation verdicts come from the separate replay
validator and do not run through the VLM pipeline.

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
| `state.json` | R/W | Current round and stage |
| `rounds/{n}/matches_matrix.csv` | R/W | All match results |
| `rounds/{n}/validation_requests.json` | W | One validation request per local winner (validator consumes) |
| `rounds/{n}/{hotkey}/submitted.json` | R | Miner's Three.js results, views and failure details |
| `rounds/{n}/leader/generated_1.json` | R | Previous leader's single result bundle for this round's prompts |
| `rounds/{n}/validation_results.json` | R | Validator's terminal passed/failed result per hotkey; absent while pending |
| `rounds/{n}/source_audit.json` | R | Source-level audit results (independent check on the published code) |
| `rounds/{n}/{hotkey}/duels_*.json` | W | Detailed match reports |
| `rounds/{n}/winner.json` | W | Winner hotkey and submitted source repository/commit |

## Development

```bash
poetry install
poetry poe lint
poetry poe test
```

## Running

```bash
poetry run python main.py
```
