from datetime import UTC, datetime, timedelta


def format_duration(seconds: int | float) -> str:
    """Format seconds as human-readable duration."""
    duration = timedelta(seconds=int(seconds))
    return str(duration)


def calculate_wait_time(
    eta: datetime | None,
    min_wait_seconds: int,
    max_wait_seconds: int,
    *,
    current_time: datetime | None = None,
) -> int:
    """Calculate wait time until ETA, clamped between min and max.

    Args:
        eta: Target datetime (UTC). If None, returns max_wait_seconds.
        min_wait_seconds: Minimum wait time in seconds.
        max_wait_seconds: Maximum wait time in seconds.
        current_time: Current datetime (UTC). Defaults to now if not provided.

    Returns:
        Wait time in seconds, clamped between min and max.
    """
    min_wait = timedelta(seconds=min_wait_seconds)
    max_wait = timedelta(seconds=max_wait_seconds)

    if eta is None:
        wait = max_wait
    else:
        now = current_time or datetime.now(UTC)

        if eta.tzinfo is None:
            eta = eta.replace(tzinfo=UTC)

        wait = max(min_wait, min(max_wait, eta - now))

    return int(wait.total_seconds())
