"""Pod specifications and TLS construction for the process entry points."""

import base64
import hashlib
import ssl
import tempfile
from pathlib import Path

from .artifacts import ArtifactConfig
from .models import PodSpec
from .rounds import RoundInputs
from .settings import Settings


def worker_spec(settings: Settings, inputs: RoundInputs | None = None) -> PodSpec:
    environment = {
        name: settings.pod_environment[name]
        for name in (
            "R2_ENDPOINT",
            "R2_ACCESS_KEY_ID",
            "R2_SECRET_ACCESS_KEY",
            "R2_BUCKET",
            "WITNESS_GATEWAY_URL",
        )
    }
    if inputs is not None:
        environment["WITNESS_ROUND_INPUTS"] = inputs.model_dump_json()
    elif "WITNESS_ROUND_END" in settings.pod_environment:
        environment["WITNESS_ROUND_END"] = settings.pod_environment["WITNESS_ROUND_END"]
    environment.update(
        {
            "WITNESS_ROLE": "worker",
            "WITNESS_PRICING_FILE": "/app/config/pricing.json",
            "WITNESS_PRICING_SHA256": hashlib.sha256(settings.pricing_path.read_bytes()).hexdigest(),
            "WITNESS_IDLE_SECONDS": settings.pod_environment.get("WITNESS_IDLE_SECONDS", "1800"),
            "WITNESS_MAX_CAPTURES": settings.pod_environment.get("WITNESS_MAX_CAPTURES", "2"),
        }
    )
    profile = settings.profile
    if inputs is not None and inputs.save_submissions and profile is None:
        raise ValueError("Round enables final saving; configure WITNESS_RENDERER_PROFILE")
    if inputs is not None and inputs.save_submissions and profile is not None:
        commits = {} if settings.artifacts is None else ArtifactConfig.model_validate_json(settings.artifacts).commits
        environment["WITNESS_ARTIFACTS"] = ArtifactConfig(
            profile=profile, seed=inputs.seed, prompt_ids=inputs.prompt_ids, commits=commits,
        ).model_dump_json()
    elif inputs is None and settings.artifacts is not None:
        environment["WITNESS_ARTIFACTS"] = settings.artifacts
    if "WITNESS_ARTIFACTS" in environment:
        environment["DINOV3_REVISION"] = str(settings.embedding_revision)
        if settings.hf_token is not None:
            environment["HF_TOKEN"] = settings.hf_token.get_secret_value()
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
            "WITNESS_GATEWAY_OWNER_HOTKEY",
            "WITNESS_GATEWAY_URL",
            "WITNESS_MASTER_KEY",
            "WITNESS_IMAGE",
            "WITNESS_GATEWAY_IMAGE",
            "GATEWAY_RUNPOD_API_KEY",
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
        "WITNESS_COMPETITION_REPO",
        "WITNESS_COMPETITION_BRANCH",
        "WITNESS_GITHUB_TOKEN",
        "WITNESS_ROUND_REFRESH_SECONDS",
        "WITNESS_ROUND_POLICY_SECONDS",
        "WITNESS_RENDERER_PROFILE",
        "WITNESS_CPU_ID",
        "WITNESS_VCPU",
        "WITNESS_DISK_GB",
        "WITNESS_DATA_CENTERS",
        "WITNESS_MAX_PODS",
        "WITNESS_MAX_CAPTURES",
        "WITNESS_IDLE_SECONDS",
        "GATEWAY_CREATE_INTERVAL_SECONDS",
        "GATEWAY_REQUEST_INTERVAL_SECONDS",
        "GATEWAY_POLL_SECONDS",
        "GATEWAY_MAX_REQUESTS",
        "GATEWAY_MAX_QUEUE",
        "WITNESS_NETUID",
        "WITNESS_TEST_HOTKEY",
        "WITNESS_REGISTRY",
        "RUNPOD_MIN_REQUEST_INTERVAL",
        "WITNESS_ARTIFACTS",
        "DINOV3_REVISION",
        "HF_TOKEN",
    ):
        if name in settings.pod_environment:
            # RunPod reserves RUNPOD_* names in Pod environments.
            key = "GATEWAY_MIN_REQUEST_INTERVAL" if name == "RUNPOD_MIN_REQUEST_INTERVAL" else name
            environment[key] = settings.pod_environment[name]
    environment.update(
        {"WITNESS_ROLE": "gateway", "WITNESS_PRICING_FILE": "/app/config/pricing.json", "GATEWAY_PORT": "8443"}
    )
    return PodSpec(
        settings.gateway_image,
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
