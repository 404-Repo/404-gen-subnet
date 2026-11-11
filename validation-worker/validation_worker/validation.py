import asyncio
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx
from botocore.exceptions import BotoCoreError, ClientError
from loguru import logger
from subnet_common.competition.validation import PromptProgress, ValidationProgress
from subnet_common.git_batcher import GitBatcher
from subnet_common.github import GitHubClient
from subnet_common.graceful_shutdown import GracefulShutdown
from subnet_common.r2_client import R2Client
from subnet_common.witness.pricing import BudgetRejected, PromptBudget

from validation_worker.execution import validate_prompt
from validation_worker.inputs import load_validation_inputs
from validation_worker.preparation import SourceArchiveNotFound, load_round_inputs, prepare_source, read_object
from validation_worker.sandbox import prepare_sandbox
from validation_worker.settings import Settings


def transient(error: Exception) -> bool:
    if isinstance(error, httpx.HTTPStatusError):
        return error.response.status_code in (408, 429) or error.response.status_code >= 500
    if isinstance(error, ClientError):
        return error.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0) in (404, 408, 429) or (
            error.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0) >= 500
        )
    return isinstance(error, (httpx.TransportError, BotoCoreError, OSError, RuntimeError))


async def run_validation(
    round_num: int,
    hotkey: str,
    ref: str,
    output: Path,
    git: GitHubClient,
    r2: R2Client,
    http: httpx.AsyncClient,
    settings: Settings,
    shutdown: GracefulShutdown,
) -> None:
    path = f"rounds/{round_num}/{hotkey}/validation.json"
    jail = None
    frozen = None
    batcher = None
    source_failure = None
    with ExitStack() as cleanup:
        for attempt in range(3):
            stopping = shutdown.should_stop
            if stopping:
                return
            try:
                await asyncio.to_thread(output.mkdir, parents=True, exist_ok=True, mode=0o700)
                if batcher is None:
                    batcher = await GitBatcher.create(git=git, branch=settings.github_branch)
                raw = await batcher.read(path)
                progress = (
                    ValidationProgress.model_validate_json(raw) if raw is not None else ValidationProgress(ref=ref)
                )
                inputs = await load_validation_inputs(git, round_num, hotkey, progress.ref)
                if set(progress.prompts) - inputs.generations.keys():
                    raise ValueError("Validation checkpoint contains foreign prompts")
                for prompt_id in inputs.generations:
                    if shutdown.should_stop or source_failure is not None:
                        break
                    state = progress.prompts.setdefault(prompt_id, PromptProgress())
                    deadline = None
                    while state.status == "pending" and state.retryable and state.attempts < 3:
                        if shutdown.should_stop or (state.attempts and await shutdown.wait(timeout=state.attempts)):
                            break
                        now = asyncio.get_running_loop().time()
                        if deadline is None:
                            deadline = now + settings.sandbox_timeout_seconds
                        if deadline <= now:
                            state.retryable = False
                            state.reason = "Validation deadline exhausted"
                            await batcher.write(
                                path, progress.model_dump_json(indent=2), f"Validate round {round_num}, miner {hotkey}"
                            )
                            break
                        state.attempts += 1
                        budget = None
                        try:
                            async with asyncio.timeout_at(deadline):
                                if frozen is None:
                                    frozen = await load_round_inputs(inputs, r2, settings)
                                if jail is None:
                                    cleanup.close()
                                    directory = Path(
                                        cleanup.enter_context(TemporaryDirectory(prefix=".validation-", dir=output))
                                    )
                                    source = await prepare_source(inputs, git, directory)
                                    jail = await asyncio.to_thread(prepare_sandbox, source, directory, settings)
                                pricing = await read_object(
                                    r2, r2.bucket, f"pod-pricing/v1/{settings.witness_deployment}.json"
                                )
                                budget = PromptBudget(pricing)
                                budget.charges = state.charges
                                budget.accept()
                                prompt = next(prompt for prompt in frozen.prompts if prompt.id == prompt_id)
                                result = await validate_prompt(
                                    inputs, prompt, jail, directory, r2, http, settings, budget
                                )
                                state.status = result.verdict.value
                                state.reason = result.reason
                        except SourceArchiveNotFound as error:
                            source_failure = str(error)
                            state.status, state.reason, state.retryable = "failed", source_failure, False
                        except BudgetRejected as error:
                            state.status, state.reason = "failed", str(error)
                        except Exception as error:
                            state.reason = f"{type(error).__name__}: {error}"[:2048]
                            state.retryable = transient(error)
                            if budget is not None:
                                try:
                                    budget.accept()
                                except BudgetRejected as rejected:
                                    state.status, state.reason = "failed", str(rejected)
                            logger.warning(
                                f"Prompt pending: round={round_num}, hotkey={hotkey}, prompt={prompt_id}, "
                                f"attempt={state.attempts}, error={type(error).__name__}"
                            )
                        await batcher.write(
                            path, progress.model_dump_json(indent=2), f"Validate round {round_num}, miner {hotkey}"
                        )
                if not await batcher.flush():
                    raise RuntimeError("Validation checkpoint has not been committed")
                if source_failure is not None:
                    logger.warning(f"Validation failed: round={round_num}, hotkey={hotkey}, reason={source_failure}")
                    return
                complete = all(
                    key in progress.prompts and progress.prompts[key].status != "pending" for key in inputs.generations
                )
                logger.info(f"Validation {'complete' if complete else 'pending'}: round={round_num}, hotkey={hotkey}")
                return
            except Exception as error:
                logger.error(f"Validation interrupted: round={round_num}, hotkey={hotkey}, error={error!r}")
                if not transient(error) or attempt == 2 or await shutdown.wait(timeout=attempt + 1):
                    return
