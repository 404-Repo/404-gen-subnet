"""Witness Pod service."""

import argparse


def main() -> None:
    argparse.ArgumentParser(description="Witness Pod").parse_args()
    from .pod import main as run_pod

    run_pod()


if __name__ == "__main__":
    main()
