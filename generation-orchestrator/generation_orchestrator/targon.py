import asyncio

import httpx
from loguru import logger
from subnet_common.graceful_shutdown import GracefulShutdown
from targon.client.serverless import ServerlessResourceListItem

from generation_orchestrator.targon_client import ContainerDeployConfig, TargonClient, TargonClientError


async def wait_for_visible(
    client: TargonClient,
    name: str,
    shutdown: GracefulShutdown,
    *,
    timeout: float,  # noqa: ASYNC109
    check_interval: float = 5.0,
) -> ServerlessResourceListItem | None:
    """
    Wait for the container to become visible (stage 1).
    Targon containers may not appear immediately after deployment.
    """
    deadline = asyncio.get_running_loop().time() + timeout

    while not shutdown.should_stop:
        container = await client.get_container(name)
        if container and container.url:
            return container

        if asyncio.get_running_loop().time() >= deadline:
            logger.warning(f"Container {name} not visible within {timeout}s")
            return None

        await shutdown.wait(timeout=check_interval)

    return None


async def wait_for_healthy(
    url: str,
    shutdown: GracefulShutdown,
    *,
    timeout: float,  # noqa: ASYNC109
    check_interval: float = 5.0,
) -> bool:
    """Wait for the container health endpoint to return 200 (stage 2)."""
    health_url = f"{url}/health"
    deadline = asyncio.get_running_loop().time() + timeout

    async with httpx.AsyncClient(timeout=30.0) as http:
        while not shutdown.should_stop:
            try:
                response = await http.get(health_url)
                if response.status_code == 200:
                    return True
            except httpx.RequestError as e:
                logger.debug(f"Health check failed: {e}")

            if asyncio.get_running_loop().time() >= deadline:
                logger.warning(f"Container at {url} not healthy within {timeout}s")
                return False

            await shutdown.wait(timeout=check_interval)

    return False


async def ensure_running_container(
    client: TargonClient,
    name: str,
    config: ContainerDeployConfig,
    shutdown: GracefulShutdown,
    *,
    reuse_existing: bool = False,
    deploy_timeout: float = 600.0,
    warmup_timeout: float = 300.0,
    check_interval: float = 10.0,
) -> ServerlessResourceListItem | None:
    """
    Ensure a healthy container is running.

    Stages:
    1. Wait for the container to become visible (Targon-specific delay)
    2. Wait for a health check to pass

    Returns container if successful, None if failed.
    """
    # Reuse or cleanup
    if reuse_existing:
        container = await client.get_container(name)
        if container and container.url:
            logger.info(f"Reusing container: {name} ({container.uid})")
            return container
    else:
        deleted = await client.delete_containers_by_name(name)
        if deleted:
            logger.info(f"Deleted {deleted} existing container(s)")

    # Deploy
    deploy_start = asyncio.get_running_loop().time()
    try:
        await client.deploy_container(name, config)
    except TargonClientError:
        return None

    container = await wait_for_visible(
        client,
        name,
        shutdown,
        timeout=deploy_timeout,
        check_interval=check_interval,
    )
    if not container:
        logger.error(f"Container {name} failed to become visible")
        return None

    deploy_time = asyncio.get_running_loop().time() - deploy_start
    logger.info(f"Container {name} ({container.uid}) visible in {deploy_time:.1f}s")

    warmup_start = asyncio.get_running_loop().time()
    if not await wait_for_healthy(
        container.url,
        shutdown,
        timeout=warmup_timeout,
        check_interval=check_interval,
    ):
        logger.error(f"Container {name} failed health check, deleting")
        await client.delete_container(container.uid)
        return None

    warmup_time = asyncio.get_running_loop().time() - warmup_start
    logger.info(f"Container {name} healthy in {warmup_time:.1f}s")

    return container
