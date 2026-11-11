# Manual leader generation

Use the [operator command](../README.md#operator-generation) with a checkout of
the leader source pinned in the competition's `leader.json`. Use the wallet
whose hotkey is set as `leader_generation_hotkey` in the competition's
`config.json`.

The launcher reads the published prompts and seed from the selected competition
branch. Before each pass, it reads CDN `submitted.json` and skips completed
prompts. Rerun the same command to resume unfinished work.

The witness saves results under the operator hotkey. During collection, the
submission collector reads those results and writes
`rounds/{n}/leader/generated.json` for the judge.
