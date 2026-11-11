import unittest

import httpx

from validation_service.execution import list_worker_pods


class PodDiscoveryTest(unittest.IsolatedAsyncioTestCase):
    async def test_reuses_owned_pod_on_previous_image(self) -> None:
        payload = {
            "pods": [
                {
                    "id": "owned",
                    "name": "validation-example-0123456789abcdef",
                    "status": "RUNNING",
                    "image": "registry.example/validation-worker:previous",
                },
                {
                    "id": "other",
                    "name": "validation-example-canary-0123456789abcdef",
                    "status": "RUNNING",
                    "image": "registry.example/validation-worker:previous",
                },
            ],
            "pagination": {"hasNextPage": False},
        }
        transport = httpx.MockTransport(lambda _: httpx.Response(200, json=payload))
        async with httpx.AsyncClient(base_url="https://runpod.test/v2/", transport=transport) as runpod:
            pods = await list_worker_pods(runpod, "validation-example")
        self.assertEqual([pod.id for pod in pods.values()], ["owned"])
