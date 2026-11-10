import asyncio
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx
from loguru import logger
from pydantic import BaseModel, Field
from subnet_common.competition.validation import ValidationResult
from subnet_common.git_batcher import GitBatcher
from subnet_common.github import GitHubClient
from subnet_common.graceful_shutdown import GracefulShutdown
from subnet_common.r2_client import R2Client

from validation_worker.execution import validate_prompt
from validation_worker.inputs import load_validation_inputs
from validation_worker.preparation import load_round_inputs, prepare_source
from validation_worker.sandbox import prepare_sandbox
from validation_worker.settings import Settings


class ValidationProgress(BaseModel):
    ref: str = Field(pattern=r"^[0-9a-f]{40}$")
    prompts: dict[str, ValidationResult] = Field(default_factory=dict)


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
    batcher = None
    jail = None
    directory = output
    with ExitStack() as cleanup:
        while True:
            try:
                await asyncio.to_thread(output.mkdir, parents=True, exist_ok=True, mode=0o700)
                if batcher is None:
                    batcher = await GitBatcher.create(git=git, branch=settings.github_branch)
                raw = await batcher.read(path)
                progress = (
                    ValidationProgress.model_validate_json(raw) if raw is not None else ValidationProgress(ref=ref)
                )
                inputs = await load_validation_inputs(git, round_num, hotkey, progress.ref)
                frozen = await load_round_inputs(inputs, r2, settings)
                if set(progress.prompts) - {prompt.id for prompt in frozen.prompts}:
                    raise ValueError("Validation checkpoint contains foreign prompts")
                if raw is None:
                    await batcher.write(
                        path, progress.model_dump_json(indent=2), f"Validate round {round_num}, miner {hotkey}"
                    )
                if not await batcher.flush():
                    raise RuntimeError("Validation checkpoint has not been committed")
                if len(progress.prompts) == len(frozen.prompts):
                    logger.info(f"Validation complete: round={round_num}, hotkey={hotkey}")
                    return
                if jail is None:
                    cleanup.close()
                    directory = Path(cleanup.enter_context(TemporaryDirectory(prefix=".validation-", dir=output)))
                    source = await prepare_source(inputs, git, directory)
                    jail = await asyncio.to_thread(prepare_sandbox, source, directory, settings)
                for prompt in frozen.prompts:
                    if shutdown.should_stop:
                        return
                    if prompt.id in progress.prompts:
                        continue
                    progress.prompts[prompt.id] = await validate_prompt(
                        inputs,
                        prompt,
                        jail,
                        directory,
                        r2,
                        http,
                        settings,
                    )
                    await batcher.write(
                        path,
                        progress.model_dump_json(indent=2),
                        f"Validate prompt {prompt.id}, round {round_num}, miner {hotkey}",
                    )
                    if not await batcher.flush():
                        raise RuntimeError("Validation checkpoint has not been committed")
                logger.info(f"Validation complete: round={round_num}, hotkey={hotkey}")
                return
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.error(
                    f"Validation interrupted: round={round_num}, hotkey={hotkey}, error={type(error).__name__}"
                )
                await shutdown.wait(timeout=settings.min_check_state_interval_seconds)
            if shutdown.should_stop:
                return
