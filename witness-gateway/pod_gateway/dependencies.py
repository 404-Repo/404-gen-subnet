"""Pod specifications and TLS construction for the process entry points."""

import base64
import hashlib
import ssl
import tempfile
from pathlib import Path

from .models import PodSpec
from .settings import Settings


def worker_spec(settings: Settings) -> PodSpec:
    environment = {
        name: settings.pod_environment[name]
        for name in (
            "R2_ENDPOINT",
            "R2_ACCESS_KEY_ID",
            "R2_SECRET_ACCESS_KEY",
            "R2_BUCKET",
            "WITNESS_GATEWAY_URL",
            "WITNESS_ROUND_END",
        )
    }
    environment.update(
        {
            "WITNESS_ROLE": "worker",
            "WITNESS_PRICING_FILE": "/app/config/pricing.json",
            "WITNESS_PRICING_SHA256": hashlib.sha256(settings.pricing_path.read_bytes()).hexdigest(),
            "WITNESS_IDLE_SECONDS": settings.pod_environment.get("WITNESS_IDLE_SECONDS", "1800"),
            "WITNESS_MAX_CAPTURES": settings.pod_environment.get("WITNESS_MAX_CAPTURES", "2"),
        }
    )
    return PodSpec(
        settings.image,
        cpu_id=settings.worker_cpu_id,
        vcpu_count=settings.worker_vcpu_count,
        disk_gb=settings.worker_disk_gb,
        env=environment,
        capability_secret=settings.secret,
        data_centers=settings.data_centers,
        registry=settings.registry,
    )


def gateway_spec(settings: Settings) -> PodSpec:
    environment = {
        name: settings.pod_environment[name]
        for name in (
            "WITNESS_ACTIVE_ROUND",
            "WITNESS_GATEWAY_OWNER_HOTKEY",
            "WITNESS_GATEWAY_URL",
            "WITNESS_MASTER_KEY",
            "WITNESS_ROUND_END",
            "WITNESS_PROMPT_HASHES",
            "WITNESS_IMAGE",
            "GATEWAY_RUNPOD_API_KEY",
            "WITNESS_MAX_FLEET_HOURLY",
            "WITNESS_POD_HOURLY_CEILING",
            "GATEWAY_HOURLY_CEILING",
            "R2_ENDPOINT",
            "R2_ACCESS_KEY_ID",
            "R2_SECRET_ACCESS_KEY",
            "R2_BUCKET",
            "SUBTENSOR_RPC_URL",
            "GATEWAY_TLS_CERT_B64",
            "GATEWAY_TLS_KEY_B64",
        )
    }
    for name in (
        "WITNESS_CPU_ID",
        "WITNESS_VCPU",
        "WITNESS_DISK_GB",
        "WITNESS_DATA_CENTERS",
        "WITNESS_MAX_PODS",
        "WITNESS_MAX_CAPTURES",
        "WITNESS_IDLE_SECONDS",
        "WITNESS_NETUID",
        "WITNESS_TEST_HOTKEY",
        "WITNESS_REGISTRY",
        "RUNPOD_MIN_REQUEST_INTERVAL",
    ):
        if name in settings.pod_environment:
            # RunPod reserves RUNPOD_* names in Pod environments.
            key = "GATEWAY_MIN_REQUEST_INTERVAL" if name == "RUNPOD_MIN_REQUEST_INTERVAL" else name
            environment[key] = settings.pod_environment[name]
    environment.update(
        {"WITNESS_ROLE": "gateway", "WITNESS_PRICING_FILE": "/app/config/pricing.json", "GATEWAY_PORT": "8443"}
    )
    return PodSpec(
        settings.gateway_image if settings.gateway_image is not None else settings.image,
        cpu_id=settings.gateway_cpu_id,
        vcpu_count=settings.gateway_vcpu_count,
        disk_gb=10,
        port=8443,
        env=environment,
        data_centers=settings.data_centers,
        registry=settings.registry,
    )


def create_tls_context(settings: Settings) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    with tempfile.TemporaryDirectory(prefix="witness-tls-") as temporary:
        certificate, key = Path(temporary) / "cert.pem", Path(temporary) / "key.pem"
        certificate.write_bytes(base64.b64decode(settings.tls_certificate.get_secret_value(), validate=True))
        key.write_bytes(base64.b64decode(settings.tls_key.get_secret_value(), validate=True))
        key.chmod(0o600)
        context.load_cert_chain(certificate, key)
    return context
