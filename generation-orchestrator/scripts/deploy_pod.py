"""Deploy one pod end-to-end and wait for `/status=ready`.

Reads the same `.env` as the orchestrator (it needs provider credentials). GPU type /
count / timeouts default to your Settings but can be overridden from the command line.

Usage:
    python scripts/deploy_pod.py --provider runpod --image registry/test:latest
    python scripts/deploy_pod.py --provider targon --image ... --gpu-count 1 --keep

Flags:
    --provider {targon,verda,runpod}   which GPU provider to use
    --image   <docker-image>            required
    --name    <pod-name>                default: test-pod-<timestamp>
    --gpu-type, --gpu-count             override Settings.gpu_type/gpu_count
    --timeout-seconds                   override warmup budget (default: Settings.pod_warmup_timeout_seconds)
    --keep                              don't delete the pod after success (for manual inspection)
    --cleanup-only                      just delete any pods with the given --name prefix and exit
"""

import argparse
import asyncio
import signal
import sys
import time
from pathlib import Path


# Bootstrap: make the orchestrator package importable when run as `python scripts/...`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loguru import logger  # noqa: E402

from generation_orchestrator.generation_stop import GenerationStop  # noqa: E402
from generation_orchestrator.gpu_provider import GPUProviderManager  # noqa: E402
from generation_orchestrator.gpu_provider.common import GPUProvider  # noqa: E402
from generation_orchestrator.settings import Settings  # noqa: E402


PROVIDER_MAP = {
    "targon": GPUProvider.TARGON,
    "verda": GPUProvider.VERDA,
    "runpod": GPUProvider.RUNPOD,
}


def _setup_logging() -> None:
    logger.remove()
    logger.add(
        sys.stderr,
        level="DEBUG",
        format="<green>{time:HH:mm:ss.SSS}</green> | "
        "<level>{level: <8}</level> | "
        "<cyan>{name}</cyan> | "
        "<level>{message}</level>",
        colorize=True,
    )


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--provider", required=True, choices=list(PROVIDER_MAP))
    p.add_argument(
        "--image", required=False, help="Docker image to deploy (required unless --list-only/--cleanup-only)"
    )
    p.add_argument("--name", default=None, help="Pod name (default: test-pod-<timestamp>)")
    p.add_argument("--gpu-type", default=None, help="GPU type (default: Settings.gpu_type)")
    p.add_argument("--gpu-count", type=int, default=None, help="GPU count (default: Settings.gpu_count)")
    p.add_argument("--timeout-seconds", type=float, default=None, help="Override Settings.pod_warmup_timeout_seconds")
    p.add_argument("--keep", action="store_true", help="Skip teardown after success")
    p.add_argument("--cleanup-only", action="store_true", help="Delete pods by --name prefix and exit")
    p.add_argument(
        "--list-only",
        action="store_true",
        help="List containers (all, or matching --name prefix), print them, and exit. No deploys, no deletes.",
    )
    return p.parse_args()


def _install_stop_handler(stop: GenerationStop) -> None:
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: stop.cancel("signal"))


async def _cleanup_only(manager: GPUProviderManager, name_prefix: str) -> int:
    logger.info(f"Cleanup-only: deleting any pods matching prefix '{name_prefix}'")
    deleted = await manager.cleanup_by_prefix(name_prefix)
    logger.info(f"Deleted {deleted} pod(s)")
    return 0


async def _list_only(
    manager: GPUProviderManager,
    provider: GPUProvider,
    prefix: str | None,
) -> int:
    """Call list_containers against the provider and print what we get. No deletes."""
    logger.info(f"List-only: provider={provider.value} prefix={prefix!r}")
    try:
        async with manager._create_client(provider) as client:  # type: ignore[attr-defined]
            containers = await client.list_containers(prefix=prefix) if prefix else await client.list_containers()
    except Exception as e:
        logger.error(f"list_containers failed on {provider.value}: {type(e).__name__}: {e}")
        return 1

    logger.info(f"Found {len(containers)} container(s) on {provider.value}")
    for c in containers:
        logger.info(f"  {c}")
    return 0


async def _deploy(
    manager: GPUProviderManager,
    provider: GPUProvider,
    name: str,
    image: str,
    gpu_type: str,
    gpu_count: int,
    timeout_override: float | None,
    keep: bool,
    stop: GenerationStop,
) -> int:
    """Deploy one pod and wait for /status=ready. Returns exit code."""
    if timeout_override is not None:
        # Apply the override by monkey-patching settings on the manager's cached reference.
        # Safe here since this script owns the manager.
        manager._settings.pod_warmup_timeout_seconds = timeout_override  # type: ignore[misc]
        logger.info(f"Overriding pod_warmup_timeout_seconds = {timeout_override}s")

    start = time.monotonic()
    logger.info(f"Deploying '{name}' on {provider.value} with image={image} gpu={gpu_count}x{gpu_type}")

    deployed = await manager.get_healthy_pod(
        name=name,
        image=image,
        gpu_type=gpu_type,
        gpu_count=gpu_count,
        stop=stop,
        replacements_remaining=manager._settings.max_replacements,  # type: ignore[misc]
        provider=provider,
    )
    elapsed = time.monotonic() - start

    if deployed is None:
        logger.error(f"Pod failed to become ready after {elapsed:.1f}s")
        # Best-effort cleanup if anything leaked.
        await manager.cleanup_by_prefix(name)
        return 1

    logger.success(f"Pod ready in {elapsed:.1f}s")
    logger.info(f"  name:  {deployed.info.name}")
    logger.info(f"  url:   {deployed.info.url}")
    logger.info(f"  uid:   {deployed.info.delete_identifier}")
    logger.info(f"  token: {'<set>' if deployed.generation_token else '<none>'}")

    if keep:
        logger.warning(f"--keep set: leaving pod alive. Clean up with: --cleanup-only --name {name}")
        return 0

    logger.info(f"Tearing down pod '{name}'")
    await manager.delete_container(deployed.info)
    logger.info("Done")
    return 0


async def _main() -> int:
    args = _parse_args()
    _setup_logging()

    settings = Settings()  # type: ignore[call-arg]
    manager = GPUProviderManager(settings)
    stop = GenerationStop()
    _install_stop_handler(stop)

    provider = PROVIDER_MAP[args.provider]
    name = args.name or f"test-pod-{int(time.time())}"

    if args.list_only:
        # If --name was supplied, treat it as a filter prefix; otherwise list everything.
        return await _list_only(manager, provider, prefix=args.name)

    if args.cleanup_only:
        return await _cleanup_only(manager, name)

    if not args.image:
        logger.error("--image is required unless --list-only or --cleanup-only is set")
        return 2

    gpu_type = args.gpu_type or settings.gpu_type
    gpu_count = args.gpu_count if args.gpu_count is not None else settings.gpu_count

    try:
        return await _deploy(
            manager=manager,
            provider=provider,
            name=name,
            image=args.image,
            gpu_type=gpu_type,
            gpu_count=gpu_count,
            timeout_override=args.timeout_seconds,
            keep=args.keep,
            stop=stop,
        )
    except Exception as e:
        logger.exception(f"Unhandled error: {e}")
        # If we crashed mid-deploy, scrub any leftovers.
        await manager.cleanup_by_prefix(name)
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
