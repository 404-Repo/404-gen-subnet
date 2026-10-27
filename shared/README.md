# Shared competition contracts

## Judging and candidate validation

The judge compares miners' canonical result bundles. It requests validation; the
validator produces a verdict. Replay execution, witness evidence, request matching,
output checks and token accounting belong to the validator. Source audit remains
an independent producer with its existing contract.

All paths below are relative to the competition state repository. The round path
and hotkey identify the submitted source and results for that validation pass.

| Path | Producer | Consumer | Contract |
| --- | --- | --- | --- |
| `rounds/{n}/{hotkey}/generated.json` | Collector | Judge, validator | Prompt stem to `GenerationResult` (`js`, `views`, `failure_reason`, `size`) |
| `rounds/{n}/leader/generated.json` | Leader generator | Judge | Same result schema, one bundle for the previous leader |
| `rounds/{n}/validation_requests.json` | Judge | Validator | Append-only list of `{hotkey}`; one request per round/hotkey |
| `rounds/{n}/validation_results.json` | Validator | Judge | Hotkey to `{verdict: "passed" or "failed", reason}`; one terminal result |
| `rounds/{n}/source_audit.json` | Source auditor | Judge | Existing list of `{hotkey, verdict, reason}` source verdicts |
| `rounds/{n}/matches_matrix.csv` | Judge | Judge | Cached miner-vs-miner margins; unchanged |
| `rounds/{n}/{hotkey}/duels_*.json` | Judge | Diagnostics | Tournament match reports; unchanged filenames |
| `rounds/{n}/winner.json` | Judge | Round manager | `winner_hotkey`, `repo`, `commit` |
| `leader.json` | Round manager | Judge, leader generator, weights setter | Source repository/commit, hotkey, weight, effective block and legacy `docker` string |

`generations.get_generations` and `save_generations` resolve the single result
bundle at `rounds/{n}/{hotkey}/generated.json` for every hotkey, including `leader`.
There is no source selector or repeat index in the API. `validation_requests` provides `ValidationRequest`,
`ValidationRequests`, `get_validation_requests` and `save_validation_requests` for
the hotkey-only request channel. `validation` provides `ValidationResult` and
`ValidationVerdict` (`passed` or `failed`), with `get_validation_results` and
`save_validation_results` for the terminal map. When adding results, that producer
must retain the round's previously published terminal results.

An absent file or absent hotkey means validation is queued or in progress. There
is no pending result object to publish. A failed terminal verdict requires a
nonempty reason. Infrastructure failures remain unresolved and retried by the
producer. Invalid JSON, schema errors and storage exceptions propagate to the
judge's service error handling instead of becoming miner rejections.

Either a validation failure or a source-audit failure rejects a candidate. Both
passes, plus a verified defender, verify a local winner. Every surviving leader
must be verified before finalization. A rejected leader causes a new timeline to
be derived from the existing match cache. Its successors retain their single
validation result even if their defender changes. The base leader remains the
existing verified baseline. See [judge-service](../judge-service/README.md) for
the tournament and unchanged multi-stage VLM judging algorithm.

The old audit matrix, regeneration reports, judge-produced generation-audit
verdicts and audit-duel report options have been removed. They are not replay
validation evidence. `audit_repeats` is no longer competition configuration and
pod diagnostics no longer carry a repeat index. Historical state is not converted
automatically: a migrated round must provide canonical bundles and fresh validator
results, rather than treating old VLM audit verdicts as replay verdicts.
Validation requests now use `validation_requests.json`; the legacy
`require_audit.json` file is not read or converted. Producers must use the renamed
channel at cutover. The source-audit channel, schema and API remain unchanged.

## Required producer migrations

This is a judge/shared semantic migration. The unmodified legacy generation
orchestrator imports removed contracts and cannot run with this shared version.
Complete its migration and provide a replay validator before competition cutover.

- **Generation orchestrator:** `generation_iteration.py` must retire the candidate
  audit/regeneration loop and its generation-report/generation-audit feedback.
  Keep previous-leader generation for new prompts, executing the leader source
  identified by repository/commit. `miner_runner.py` and `pod_session.py` must
  produce `leader/generated.json` through the new execution path, without
  candidate repeats or submitted-versus-regenerated comparisons.
  `generation_summary.py` and the regeneration-report methods in `discord.py`
  depend on the removed `GenerationReport`, `RepeatStats` and
  `GenerationAuditResult` contracts and must be retired with that loop.
- **Replay validator:** consume `validation_requests.json`, replay the exact submitted
  Python source using the published prompts/seed and recorded witness evidence,
  verify the canonical outputs and competition budgets, then publish one terminal
  `ValidationResult` per hotkey. Keep pending/infrastructure work absent from the
  result map. The judge does not implement any of this execution.
- **Source auditor:** keep the `source_audit.json` producer/consumer schema and
  independent eligibility policy. There is no source-audit producer in this repo;
  its operator must audit the submitted Python source for the new competition.

The integrated collector freezes Python repository/commit submissions before
publishing prompts. Its admission rules and download/render behavior are unchanged;
`download.py` and its existing tests use the miner's `generated.json` path in Git.

The integrated round-manager propagates source identity and explicitly writes
`docker=""` for new leaders. `LeaderEntry.docker` retains its empty default for
legacy weight setters, and historical Docker strings remain intact. Python
generation does not use that field. Weight and schedule behavior and the
source-audit storage module are unchanged.

## Legacy-only code and later cleanup

Docker build tracking, provider/pod execution and their diagnostics still belong
to the old orchestrator and miner runtime. The integrated submission models no
longer carry GPU metadata or export GPU defaults. Retiring consumers such as
`build_info.py` and the generation orchestrator still reference those removed
contracts; their migration is outside judge eligibility. Replace the required
execution path as described above; removing unused shared build/pod helpers after
cutover is optional cleanup. No broad legacy extraction or stylistic refactor is
included in the judge migration.

## Development

```bash
poetry install
poetry poe lint
```

Shared request/result round-trip tests run with the judge suite:

```bash
cd ../judge-service
poetry poe test
```
