"""Replay a submitted miner using saved R2 model responses and render outcomes."""

import sys


def main() -> int:
    if sys.argv[1:] == ["--worker"]:
        from validation_worker.replay_worker import main as run_replay
    else:
        from validation_worker.manual_replay import main as run_replay

    return run_replay()


if __name__ == "__main__":
    raise SystemExit(main())
