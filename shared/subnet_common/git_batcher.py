import asyncio

from loguru import logger
from pydantic import BaseModel, PrivateAttr

from subnet_common.github import GitHubClient


class GitBatcher(BaseModel):
    """
    Swap-pattern GitHub commit batcher.

    - No locks, fully asyncio-safe (mutations before await are atomic)
    - Commit swaps pending -> empty and commits the snapshot
    - Any exception during commit treated as "conflict"
    - On failure: pending restored *without overwriting newer writes*, an interval shortened
    - On success: pending cleared, interval reset
    - flush() returns True/False depending on whether commit succeeded and nothing is pending
    """

    git: GitHubClient
    branch: str
    base_sha: str

    interval: float = 600.0  # normal interval (success)
    conflict_interval: float = 60.0  # short interval (on failure)

    _pending_files: dict[str, str] = PrivateAttr(default_factory=dict)
    _pending_messages: set[str] = PrivateAttr(default_factory=set)

    _last_commit_time: float = PrivateAttr(default=0.0)
    _current_interval: float = PrivateAttr(default=600.0)  # starts equal to an interval
    _commit_in_progress: bool = PrivateAttr(default=False)

    model_config = {"arbitrary_types_allowed": True}

    @classmethod
    async def create(cls, git: GitHubClient, branch: str, base_sha: str | None = None) -> "GitBatcher":
        if base_sha is None:
            base_sha = await git.get_ref_sha(branch)
        return cls(git=git, branch=branch, base_sha=base_sha)

    async def read(self, path: str) -> str | None:
        """Read-your-own-writes: check pending files first, then git."""
        content = self._pending_files.get(path)
        if content is not None:
            return content
        return await self.git.get_file(path, ref=self.base_sha)

    async def write(self, path: str, content: str, message: str) -> None:
        """
        Queue a change. If enough time passed, perform a commit.
        No await inside mutation => atomic with respect to other coroutines.
        """
        self._pending_files[path] = content
        self._pending_messages.add(message)
        await self._maybe_commit()

    async def write_many(self, files: dict[str, str], message: str) -> None:
        """
        Queue multiple file changes.
        """
        self._pending_files.update(files)
        self._pending_messages.add(message)
        await self._maybe_commit()

    async def flush(self) -> bool:
        """
        Attempt to commit immediately.
        Returns:
            True  -> no pending left after commit
            False -> pending left (likely conflict or commit failure)
        """
        if not self._pending_files:
            return True  # nothing to do

        success = await self._commit()
        # success == True means commit succeeded, and we left pending cleared,
        # but new writes may have arrived after commit; return True only if nothing pending.
        return success and not self._pending_files

    async def _maybe_commit(self) -> None:
        if not self._pending_files:
            return

        if self._commit_in_progress:
            return

        now = asyncio.get_running_loop().time()
        if now - self._last_commit_time >= self._current_interval:
            await self._commit()

    async def _commit(self) -> bool:
        """
        Commit once. No retry.
        On any exception:
            - pending is kept (restored without overwriting newer writes)
            - interval shortened
        On success:
            - pending cleared
            - interval reset
        Returns a success flag.
        """
        if self._commit_in_progress:
            return False

        # Snapshot and swap (atomic before any await!)
        files_snapshot = self._pending_files
        messages_snapshot = self._pending_messages

        if not files_snapshot:
            return True

        self._commit_in_progress = True

        # Swap to fresh containers so writers during commit go to the new ones
        self._pending_files = {}
        self._pending_messages = set()

        message = "\n".join(sorted(messages_snapshot))
        file_count = len(files_snapshot)

        try:
            new_sha = await self.git.commit_files(
                files=files_snapshot,
                message=message,
                branch=self.branch,
                base_sha=self.base_sha,
            )

            self.base_sha = new_sha
            self._last_commit_time = asyncio.get_running_loop().time()
            self._current_interval = self.interval  # back to a long interval

            logger.info(f"Committed {file_count} files at {new_sha[:7]}: {message[:80]}")

            return True

        except Exception as e:
            # Treat any exception as a conflict/failure path
            logger.warning(f"Commit failed (treated as conflict): {e!r}")

            # Attempt to refresh base_sha; failure to refresh is non-fatal here
            try:
                self.base_sha = await self.git.get_ref_sha(self.branch)
            except Exception as e2:
                logger.debug(f"Failed to refresh base_sha after commit failure: {e2!r}")

            # Restore pending WITHOUT overwriting newer writes that arrived during commit.
            # For files: only add back keys that are currently missing.
            # For messages: union the sets (messages are idempotent).
            self._restore_pending_without_overwrite(files_snapshot, messages_snapshot)

            # Shorten the interval so we try again soon
            self._current_interval = self.conflict_interval
            return False

        finally:
            self._commit_in_progress = False

    def _restore_pending_without_overwrite(self, files: dict[str, str], messages: set[str]) -> None:
        """
        Restore snapshot into pending, but do NOT overwrite any keys that
        were added/changed while the commit was happening.
        This is atomic because it contains no await.
        """
        # For files: only set keys that are missing now.
        for k, v in files.items():
            if k not in self._pending_files:
                self._pending_files[k] = v

        # For messages: union (safe)
        self._pending_messages.update(messages)

    async def refresh_base_sha(self) -> str:
        """Refresh base_sha from the current branch HEAD."""
        self.base_sha = await self.git.get_ref_sha(self.branch)
        return self.base_sha

    @property
    def pending_count(self) -> int:
        return len(self._pending_files)
