from datetime import timedelta


def format_duration(seconds: int) -> str:
    """Format seconds as human-readable duration."""
    duration = timedelta(seconds=seconds)
    return str(duration)
