import fcntl
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .models import Job


def atomic_write(path: Path, data: bytes) -> None:
    """Replace a private file only after its contents reach durable storage."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def exclusive_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("a") as stream:
        path.chmod(0o600)
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another service owns this state or subscription login") from None
        yield


class JobStore:
    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def load(self, number: int) -> Job | None:
        path = self.directory / "rounds" / f"{number}.json"
        if not path.exists():
            return None
        job = Job.model_validate_json(path.read_bytes())
        if job.snapshot.inputs.number != number:
            raise ValueError("Journal round differs")
        return job

    def jobs(self) -> list[Job]:
        return [
            Job.model_validate_json(path.read_bytes()) for path in sorted((self.directory / "rounds").glob("*.json"))
        ]

    def save(self, job: Job) -> None:
        atomic_write(self.directory / "rounds" / f"{job.snapshot.inputs.number}.json", job.model_dump_json().encode())

    def run_directory(self, job: Job) -> Path:
        if job.allocation is None:
            raise ValueError("Job has no allocation")
        return self.directory / "runs" / str(job.allocation.id)
