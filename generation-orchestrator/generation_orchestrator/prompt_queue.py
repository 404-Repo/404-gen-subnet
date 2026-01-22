from loguru import logger
from pydantic import BaseModel, ConfigDict, Field

from generation_orchestrator.prompts import Prompt


class PromptTask(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    prompt: Prompt
    submitted_png: str | None = None
    attempts: int = 0
    attempted_by: set[str] = Field(default_factory=set)
    completed: bool = False


class PromptQueue:
    """Distributes prompts to workers with retry support.

    - fill(tasks): populates the queue with tasks
    - get(worker_id): returns prompt, increments attempts, removes if exhausted
    - complete(): removes prompt on success
    - Empty queue = all work done (completed or exhausted)
    """

    def __init__(self, log_id: str, max_attempts: int):
        self._log_id = log_id
        self._max_attempts = max_attempts
        self._total = 0
        self._tasks: dict[str, PromptTask] = {}

    def fill(self, tasks: list[PromptTask]) -> None:
        """Populate the queue with tasks. Replaces any existing tasks."""
        self._tasks = {task.prompt.stem: task for task in tasks}
        self._total = len(tasks)

    def get(self, worker_id: str) -> PromptTask | None:
        """Get prompt for worker. Increments attempts, removes if exhausted."""
        if not self._tasks:
            return None

        eligible = [t for t in self._tasks.values() if worker_id not in t.attempted_by]
        if not eligible:
            return None

        task = min(eligible, key=lambda t: t.attempts)
        task.attempted_by.add(worker_id)
        task.attempts += 1

        if task.attempts >= self._max_attempts:
            del self._tasks[task.prompt.stem]

        return task

    def complete(self, task: PromptTask) -> bool:
        """Mark completed. Returns False if already gone."""
        first_completed = self._tasks.pop(task.prompt.stem, None) is not None
        task.completed = True
        if first_completed:
            logger.debug(f"{self._log_id}: {self._total - len(self._tasks)}/{self._total} finished")
        else:
            logger.debug(f"{self._log_id}: redundant completion for {task.prompt.stem}")
        return first_completed

    def empty(self) -> bool:
        """Returns True if all tasks are completed or exhausted."""
        return not self._tasks

    def __len__(self) -> int:
        return len(self._tasks)
