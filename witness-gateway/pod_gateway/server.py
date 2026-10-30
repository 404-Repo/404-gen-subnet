"""Miner authentication, Pod allocation and bounded lifecycle control."""

from __future__ import annotations

import asyncio
import hmac
import json
import math
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from functools import partial
from typing import Protocol, TypeVar

from aiohttp import WSMsgType, web
from pydantic import BaseModel, ConfigDict, ValidationError

from .admission import CapacityReached, CreateDeferred, PendingStart, StartQueue
from .auth import Authenticator, AuthRejected
from .control import Peer, Registry
from .lifecycle import AllocationConflict, Allocator, InventoryConflict
from .models import Pod, PodSpec, Scope, Session, identifier, object_dict
from .rounds import RoundController, RoundInputs
from .runpod import ProviderUnavailable
from .security import capability, miner_access
from .state import StateConflict, WriteUncertain

T = TypeVar("T")
STARTUP_SECONDS = 600


class Evidence(Protocol):
    def read(self, key: str, limit: int = 16 * 1024 * 1024) -> bytes | None: ...


class Start(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    start_id: str
    replaces: str | None = None


class Gateway:
    def __init__(self, *, allocator: Allocator, auth: Authenticator, registry: Registry,
                 deployment: str, round_number: str, round_end: int | None, secret: bytes,
                 proof_hash: str, fence: Callable[[], bool], pricing_hash: str,
                 create_interval: float = 10,
                 request_interval: float = 10, poll_seconds: float = 30,
                 max_requests: int = 64, max_queue: int = 1024,
                 refresh_seconds: float = 2, body_timeout: float = 5,
                 rounds: RoundController | None = None,
                 spec_for_round: Callable[[RoundInputs], PodSpec] | None = None) -> None:
        self.allocator, self.auth, self.registry = allocator, auth, registry
        self.deployment, self.round, self.round_end = deployment, round_number, round_end
        self.secret, self.proof_hash, self.fence = secret, proof_hash, fence
        self.pricing_hash = pricing_hash
        self.rounds, self.spec_for_round = rounds, spec_for_round
        if allocator.selection is None:
            raise ValueError("Gateway admission requires durable selection storage")
        if (request_interval < 0 or poll_seconds <= 0 or refresh_seconds <= 0 or
                body_timeout <= 0 or max_requests < 1):
            raise ValueError("Invalid gateway request bounds")
        self.queue = StartQueue(allocator.selection.store, deployment,
                                interval=create_interval, capacity=max_queue, clock=allocator.clock)
        allocator.before_create = self._authorize_create
        allocator.pending_creates = self.queue.unresolved
        self.request_interval, self.poll_seconds = request_interval, poll_seconds
        self.max_requests, self.refresh_seconds, self.body_timeout = max_requests, refresh_seconds, body_timeout
        self._active = self._authenticating = self._journal_pending = 0
        self._rate: dict[str, tuple[float, float]] = {}
        self._busy: set[str] = set()
        self._tokens, self._token_time = 128.0, time.monotonic()
        self._worker = ThreadPoolExecutor(1, thread_name_prefix="gateway-lifecycle")
        self._journal = ThreadPoolExecutor(1, thread_name_prefix="gateway-admission")
        self._verifiers = ThreadPoolExecutor(4, thread_name_prefix="gateway-auth")
        self._fencing = ThreadPoolExecutor(1, thread_name_prefix="gateway-fence")
        self._loaded = self._fence_ok = False
        self._fence_at = 0.0
        self._pods: dict[str, Pod] = {}
        self._inventory: list[Pod] = []
        self._current: dict[Scope, Session] = {}
        self._sessions: dict[tuple[Scope, str], Session] = {}
        self._grants: dict[Scope, dict[str, object]] = {}
        self._due: dict[Scope, float] = {}
        self._startup_due: dict[Scope, float] = {}
        self._grant_due: dict[Scope, float] = {}
        self._retiring: set[str] = set()
        self._wake = asyncio.Event()
        self._changed = asyncio.Event()
        self._progress: dict[Scope, Session] = {}
        self._connecting: set[str] = set()

    def scope(self, hotkey: str) -> Scope:
        return Scope(self.deployment, self.round, hotkey)

    def _live(self) -> bool:
        return self._loaded and self._fence_ok and time.monotonic() - self._fence_at <= max(2, 2 * self.refresh_seconds)

    def _authorize_create(self, scope: Scope, start_id: str) -> None:
        if scope.round != self.round or not self.fence():
            raise AuthRejected("gateway_fenced", 503)
        self.auth.membership.require(scope.hotkey)
        self._require_round(scope)
        self.queue.authorize(scope, start_id)
        if not self.fence():
            raise AuthRejected("gateway_fenced", 503)
        self._require_round(scope)

    def _require_round(self, scope: Scope | None = None) -> None:
        if self.rounds is not None:
            try:
                current = self.rounds.require()
            except AuthRejected:
                previous = None if scope is None else self.queue.attempt(scope)
                if previous is None or not previous.submitted or previous.rejected:
                    raise
                # Previously assigned miners can replace a lost Pod for lookup
                # and recording recovery. Its policy still denies new work.
                current = self.rounds.require(generation=False)
            if scope is not None and current.round != scope.round:
                raise AuthRejected("round_closed", 403)
        elif self.round_end is not None and time.time() >= self.round_end:
            raise AuthRejected("round_closed", 403)

    async def round_changed(self) -> None:
        if self.rounds is None:
            return
        inputs = self.rounds.inputs
        if inputs is not None:
            assert self.spec_for_round is not None
            spec = self.spec_for_round(inputs)
            changed = inputs.round != self.round
            if changed:
                await self._write(partial(self.queue.rollover, inputs.round))
            def use_round() -> None:
                with self.allocator.lock:
                    self.allocator.spec = spec
                    self.round = inputs.round
            await self._run(self._worker, use_round)
        await self.publish_policy()
        self._wake.set()

    async def publish_policy(self) -> None:
        for peer in list(self.registry.peers.values()):
            if not peer.socket.closed:
                with suppress(ConnectionError, RuntimeError):
                    await self._send_policy(peer)

    async def _send_policy(self, peer: Peer) -> None:
        if self.rounds is not None:
            policy = self.rounds.policy if self._live() else None
            await peer.socket.send_json({
                "type": "round_policy", "pod_id": peer.pod_id, "start_id": peer.start_id,
                "policy": None if policy is None else policy.model_dump(mode="json"),
            })

    def _control_scope(self, request: web.Request) -> Scope:
        pod = self._pods.get(request.headers.get("x-witness-pod", ""))
        scope = None if pod is None else pod.scope()
        if scope is None or scope.hotkey != request.match_info["hotkey"]:
            raise AuthRejected("pod_identity_changed", 409)
        return scope

    async def _run(self, executor: ThreadPoolExecutor, function: Callable[[], T]) -> T:
        return await asyncio.get_running_loop().run_in_executor(executor, function)

    async def _write(self, function: Callable[[], T]) -> T:
        if self._journal_pending >= self.max_requests:
            raise AuthRejected("admission_busy", 503)
        self._journal_pending += 1
        future = asyncio.get_running_loop().run_in_executor(self._journal, function)

        def done(_: asyncio.Future[T]) -> None:
            self._journal_pending -= 1
            if not _.cancelled():
                _.exception()

        future.add_done_callback(done)
        return await asyncio.wait_for(asyncio.shield(future), 5)

    def _retry(self, code: str = "request_rate_limited") -> web.Response:
        return web.json_response({"error": code}, status=429,
                                 headers={"Retry-After": str(max(1, math.ceil(self.request_interval))),
                                          "Cache-Control": "no-store"})

    def _limited(self, hotkey: str) -> bool:
        if hotkey in self._busy:
            return True
        tokens, at = self._rate.get(hotkey, (2.0, time.monotonic()))
        return self.request_interval > 0 and tokens + (time.monotonic() - at) / self.request_interval < 1

    @web.middleware
    async def middleware(
        self,
        request: web.Request,
        handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
    ) -> web.StreamResponse:
        admitted = False
        hotkey: str | None = None
        try:
            if request.query_string or request.raw_path != request.path:
                raise AuthRejected("unknown_route", 404)
            if request.match_info.http_exception is not None or request.method not in ("GET", "POST"):
                raise AuthRejected("unknown_route", 404)
            if sum(len(name) + len(value) for name, value in request.headers.items()) > 8192:
                raise ValueError("Headers exceed limit")
            if request.path == "/health":
                return await handler(request)
            if request.path.startswith("/v1/control/"):
                scope = self._control_scope(request)
                start_id = identifier(request.match_info["start_id"])
                if (len(request.headers.getall("authorization", [])) != 1 or
                        len(request.headers.getall("x-witness-pod", [])) != 1 or not hmac.compare_digest(
                            request.headers.get("Authorization", ""),
                            "Bearer " + capability(self.secret, scope, start_id))):
                    raise AuthRejected("invalid_pod_capability")
                if not self._live():
                    raise AuthRejected("gateway_fenced", 503)
                return await handler(request)
            for name in (
                "x-bittensor-version", "x-bittensor-hotkey", "x-bittensor-nonce",
                "x-bittensor-signature", "x-bittensor-receiver",
            ):
                if len(request.headers.getall(name, [])) != 1:
                    raise AuthRejected("invalid_hotkey_auth")
            candidate = request.headers["x-bittensor-hotkey"]
            if self._limited(candidate):
                return self._retry()
            now = time.monotonic()
            self._tokens = min(128, self._tokens + (now - self._token_time) * 64)
            self._token_time = now
            if self._active >= self.max_requests or self._tokens < 1:
                return self._retry("authentication_busy")
            self._tokens -= 1
            self._active += 1
            admitted = True
            if (request.content_length is not None and request.content_length > 16384 or
                    request.method == "GET" and request.can_read_body):
                raise ValueError("Invalid body size")
            try:
                body = await asyncio.wait_for(request.read(), self.body_timeout)
            except TimeoutError:
                raise web.HTTPRequestTimeout(headers={"Connection": "close"}) from None
            if len(body) > 16384 or request.method == "GET" and body:
                raise ValueError("Invalid body size")
            if self._authenticating >= 4:
                return self._retry("authentication_busy")
            self._authenticating += 1
            verified = asyncio.get_running_loop().run_in_executor(
                self._verifiers, partial(self.auth.verify, request.method, request.path, body, request.headers)
            )

            def done(_: asyncio.Future[str]) -> None:
                self._authenticating -= 1
                if not _.cancelled():
                    _.exception()

            verified.add_done_callback(done)
            owner = await asyncio.shield(verified)
            if self._limited(owner):
                return self._retry()
            now = time.monotonic()
            if len(self._rate) >= 4097:
                self._rate = {key: value for key, value in self._rate.items()
                              if now - value[1] < max(60, 2 * self.request_interval)}
                if owner not in self._rate and len(self._rate) >= 4097:
                    return self._retry("authentication_busy")
            tokens, at = self._rate.get(owner, (2.0, now))
            self._rate[owner] = (min(2, tokens + (now - at) / self.request_interval) - 1
                                 if self.request_interval > 0 else 2, now)
            self._busy.add(owner)
            hotkey = owner
            request["hotkey"] = owner
            if not self._live():
                raise AuthRejected("gateway_fenced", 503)
            # Authentication is complete; the hotkey slot bounds the remaining request.
            self._active -= 1
            admitted = False
            return await handler(request)
        except AuthRejected as error:
            return web.json_response(
                {"error": str(error)},
                status=error.status,
                headers={"Cache-Control": "no-store", **({"Retry-After": "60"} if error.status in (429, 503) else {})},
            )
        except CapacityReached:
            return web.json_response(
                {"error": "capacity_reached"}, status=429, headers={"Retry-After": "60"}
            )
        except (AllocationConflict, InventoryConflict) as error:
            return web.json_response({"error": type(error).__name__}, status=409)
        except (ValidationError, ValueError, KeyError):
            return web.json_response({"error": "invalid_request"}, status=400)
        except (StateConflict, WriteUncertain, ProviderUnavailable, TimeoutError, CreateDeferred):
            return web.json_response(
                {"error": "control_plane_unavailable"},
                status=503,
                headers={"Retry-After": "60"},
            )
        except web.HTTPException:
            raise
        except Exception:  # noqa: BLE001 - never expose provider/SDK diagnostics
            # Do not expose SDK diagnostics, environment variables, or provider errors.
            return web.json_response({"error": "control_plane_unavailable"}, status=503)
        finally:
            if admitted:
                self._active -= 1
            if hotkey is not None:
                self._busy.discard(hotkey)

    def _scope(self, request: web.Request) -> Scope:
        return self.scope(str(request["hotkey"]))

    async def health(self, request: web.Request) -> web.Response:
        ready = self._live()
        try:
            self._require_round()
        except AuthRejected:
            ready = False
        return web.json_response(
            {"status": "ready", "transport": "pods-v1", "receiver": "404-witness",
             "round": self.round, "admission_ready": ready}
        )

    def _cached(self, scope: Scope) -> Session | None:
        pending = self.queue.pending(scope)
        if pending is not None:
            current = self._current.get(scope)
            if current is not None and current.start_id == pending.start_id:
                return current
            progress = self._progress.get(scope)
            if progress is not None and progress.start_id == pending.start_id and progress.phase != "lost":
                return progress
            return Session(pending.start_id, pending.replaces, "creating" if pending.submitted else "queued",
                           self.allocator.spec.fingerprint, self.allocator.spec.image)
        current = self._current.get(scope)
        if current is not None:
            return current
        previous = self.queue.attempt(scope)
        return None if previous is None else Session(
            previous.start_id, None, "rejected" if previous.rejected else "lost",
            self.allocator.spec.fingerprint, self.allocator.spec.image,
        )

    async def _reply(self, request: web.Request, scope: Scope, session: Session | None,
                     *, status: int = 200) -> web.Response:
        preference = request.headers.get("Prefer", "")
        wait = (min(20, int(preference[5:])) if preference.startswith("wait=") and
                len(preference) <= 7 and preference[5:].isdigit() else 0)
        deadline = time.monotonic() + wait
        if wait and session is not None and session.phase == "allocated":
            self._grants.pop(scope, None)
            self._grant_due.pop(scope, None)
            self._wake.set()
        while True:
            changed = self._changed
            endpoint = await self._endpoint(scope, session)
            remaining = deadline - time.monotonic()
            if (not wait or endpoint is not None or session is None or
                    session.phase in ("rejected", "lost", "retired") or remaining <= 0):
                break
            if request.transport is None or request.transport.is_closing():
                break
            if not self._live():
                raise AuthRejected("gateway_fenced", 503)
            with suppress(TimeoutError):
                await asyncio.wait_for(changed.wait(), remaining)
            session = self._session(scope, session.start_id)
        if wait and not self._live():
            raise AuthRejected("gateway_fenced", 503)
        pending = self.queue.pending(scope)
        retry_after = 0 if wait else self.poll_seconds
        return web.json_response(
            {"session": None if session is None else session.to_dict(), "witness": endpoint, "ready": endpoint is not None,
             "round": scope.round, "retry_after": retry_after,
             "next_eligible_at": None if pending is None else pending.eligible_at},
            status=200 if wait and endpoint is not None else status,
            headers={"Cache-Control": "no-store", "Retry-After": str(math.ceil(retry_after))},
        )

    async def current(self, request: web.Request) -> web.Response:
        scope = self._scope(request)
        return await self._reply(request, scope, self._cached(scope))

    async def start(self, request: web.Request) -> web.Response:
        data = Start.model_validate_json(await request.read())
        identifier(data.start_id)
        if data.replaces is not None:
            identifier(data.replaces)
        scope = self._scope(request)
        pending = self.queue.pending(scope)
        current = self._cached(scope)
        if pending is not None:
            if pending.start_id == data.start_id and pending.replaces != data.replaces:
                raise AllocationConflict("Connection ID has different parameters")
            await self._write(partial(self.queue.enqueue, scope, data.start_id, data.replaces))
            return await self._reply(request, scope, current, status=202)
        existing = self._sessions.get((scope, data.start_id))
        if existing is not None:
            if existing.replaces != data.replaces:
                raise AllocationConflict("Connection ID has different parameters")
            return await self._reply(request, scope, existing)
        previous = self.queue.attempt(scope, data.start_id)
        if previous is not None:
            return await self._reply(request, scope, self._sessions.get((scope, previous.start_id)) or Session(
                previous.start_id, None, "rejected" if previous.rejected else "lost",
                self.allocator.spec.fingerprint, self.allocator.spec.image,
            ), status=202)
        self._require_round(scope)
        await self._write(partial(self.queue.enqueue, scope, data.start_id, data.replaces))
        self._wake.set()
        return await self._reply(request, scope, self._cached(scope), status=202)

    async def pod_status(self, request: web.Request) -> web.Response:
        scope, start_id = (
            self._scope(request),
            identifier(request.match_info["start_id"]),
        )
        if (scope, start_id) not in self._sessions:
            scope = next((owner for owner, start in self._sessions
                          if owner.hotkey == scope.hotkey and start == start_id), scope)
        session = self._session(scope, start_id)
        if session is None:
            raise AuthRejected("session_not_found", 404)
        return await self._reply(request, scope, session)

    def _session(self, scope: Scope, start_id: str) -> Session | None:
        pending = self.queue.pending(scope)
        session = (self._cached(scope) if pending is not None and pending.start_id == start_id else
                   self._sessions.get((scope, start_id)))
        previous = self.queue.attempt(scope, start_id)
        if session is None and previous is not None:
            session = self._sessions.get((scope, previous.start_id)) or Session(
                previous.start_id, None, "rejected" if previous.rejected else "lost",
                self.allocator.spec.fingerprint, self.allocator.spec.image,
            )
        return session

    async def _endpoint(self, scope: Scope, session: Session | None) -> dict[str, object] | None:
        if session is None or session.phase != "allocated" or session.pod_id is None:
            return None
        peer = self.registry.peers.get(session.pod_id)
        if peer is None or not self.registry.ready(session.pod_id) or peer.certificate is None:
            return None
        grant = self._grants.get(scope)
        if (grant is None or grant.get("pod_id") != session.pod_id or grant.get("start_id") != session.start_id or
                grant.get("certificate") != peer.certificate or not isinstance(grant.get("expires"), int) or
                int(str(grant["expires"])) <= time.time() + 30):
            return None
        return grant

    def _selected(self, scope: Scope, start_id: str, pod_id: str) -> bool:
        session, pod = self._current.get(scope), self._pods.get(pod_id)
        return (session is not None and session.phase == "allocated" and session.start_id == start_id and
                session.pod_id == pod_id and pod is not None and pod.status == "RUNNING" and pod.lifecycle == "active")

    def _publish(self, inventory: list[Pod]) -> None:
        previous = self._current
        grouped: dict[Scope, list[Pod]] = {}
        self._inventory = []
        for pod in inventory:
            scope = pod.scope()
            if scope is not None and scope.deployment == self.deployment and scope.round != "gateway":
                self._inventory.append(pod)
                grouped.setdefault(scope, []).append(pod)
        self._pods = {pod.id: pod for pods in grouped.values() for pod in pods}
        self._current = {}
        self._sessions = {}
        for scope, pods in grouped.items():
            winner = self.allocator.winner(pods)
            assert winner is not None
            if scope not in previous or previous[scope].pod_id != winner.id:
                self._grant_due.pop(scope, None)
                self._startup_due.pop(scope, None)
            self._current[scope] = winner.session()
            for pod in pods:
                key = (scope, pod.markers["WITNESS_START_ID"])
                session = self._sessions.get(key)
                if session is None or pod.id == winner.id:
                    self._sessions[key] = pod.session()
        self._grants = {scope: grant for scope, grant in self._grants.items()
                        if scope in self._current and grant.get("pod_id") == self._current[scope].pod_id}
        retained = set(grouped) | {scope for scope, _ in self.queue.entries()}
        self._due = {scope: at for scope, at in self._due.items() if scope in retained}
        self._startup_due = {scope: at for scope, at in self._startup_due.items() if scope in grouped}
        self._grant_due = {scope: at for scope, at in self._grant_due.items() if scope in grouped}
        self._retiring.intersection_update(self._pods)

    def _advance(self, scope: Scope, entry: PendingStart) -> Session | None:
        if not self.fence():
            raise AuthRejected("gateway_fenced", 503)
        self.auth.membership.require(scope.hotkey)
        operation = self.allocator.operations.get(scope)
        if (entry.submitted and operation is not None and not operation.submitted
                and operation.session.start_id == entry.start_id):
            self.queue.release_authorization(scope, entry.start_id)
            entry = entry.model_copy(update={"submitted": False})
        if not entry.submitted:
            self._require_round(scope)
        if entry.submitted:
            # A restarted or uncertain admission can only discover its original create.
            if operation is None or not operation.submitted:
                inventory = self.allocator.provider.list()
                if not any(p.matches(scope, Session(entry.start_id, entry.replaces, "creating",
                           self.allocator.spec.fingerprint, self.allocator.spec.image)) for p in inventory):
                    return None
            return self.allocator.reconcile(scope)
        if operation is not None and operation.session.start_id == entry.start_id:
            return self.allocator.reconcile(scope)
        return self.allocator.acquire(scope, entry.start_id, replaces=entry.replaces)

    def _grant(self, scope: Scope, session: Session, certificate: str) -> dict[str, object] | None:
        if session.pod_id is None or not self.fence():
            return None
        pod = self.allocator.provider.get(session.pod_id)
        selection = self.allocator.selection
        if (pod is None or not pod.matches(scope, session) or pod.lifecycle != "active" or pod.status != "RUNNING" or
                self.allocator.spec.port not in pod.tcp or
                not self.allocator.selected(scope, session.start_id, session.pod_id) or selection is None or
                not selection.grant(pod)):
            return None
        address, port = pod.tcp[self.allocator.spec.port]
        expires = int(time.time()) + 600
        return {"address": address, "port": port, "server_name": "witness.internal", "certificate": certificate,
                "pod_id": session.pod_id, "start_id": session.start_id,
                "access": miner_access(capability(self.secret, scope, session.start_id), scope,
                                       session.start_id, session.pod_id, expires), "expires": expires}

    def _expire_unserved(self, scope: Scope, session: Session) -> Session | None:
        selection = self.allocator.selection
        if session.pod_id is None or selection is None or not self.fence():
            return None
        pod = self.allocator.provider.get(session.pod_id)
        if (pod is None or not pod.matches(scope, session) or
                self.allocator.clock() - pod.epoch_ns / 1_000_000_000 < STARTUP_SECONDS or
                not selection.revoke_unserved(pod)):
            return None
        return self.allocator.retire(scope, session.start_id, pod.id, drain=False)

    async def _fences(self) -> None:
        while True:
            refresh_at = time.monotonic() + self.refresh_seconds
            try:
                self._fence_ok = await self._run(self._fencing, self.fence)
                self._fence_at = time.monotonic()
            except Exception:  # noqa: BLE001 - stale fences stop admission, not paid capture
                self._fence_at = 0
            await asyncio.sleep(max(0, refresh_at - time.monotonic()))

    async def _work(self) -> None:
        refresh_at = 0.0
        journal_at = time.monotonic() + self.poll_seconds
        while True:
            self._wake.clear()
            try:
                now = time.monotonic()
                if now >= journal_at:
                    await self._run(self._journal, self.queue.load)
                    journal_at = time.monotonic() + self.poll_seconds
                if now >= refresh_at:
                    self._publish(await self._run(self._worker, self.allocator.provider.list))
                    self._loaded = True
                    refresh_at = time.monotonic() + self.refresh_seconds
                if self._live():
                    await self._step()
            except Exception:  # noqa: BLE001 - retain durable intents without request-driven retries
                await asyncio.sleep(max(1, self.poll_seconds))
            self._changed.set()
            self._changed = asyncio.Event()
            with suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), min(1, self.refresh_seconds))

    async def _step(self) -> None:
        round_ready = self.rounds is None or (
            self.rounds.inputs is not None
            and self.allocator.spec.env.get("WITNESS_ROUND_INPUTS") == self.rounds.inputs.model_dump_json()
        )
        now = self.allocator.clock()
        entries = self.queue.entries() if round_ready else []
        grouped: dict[Scope, list[Pod]] = {}
        visible: set[tuple[Scope, str]] = set()
        for pod in self._inventory:
            scope = pod.scope()
            if scope is not None:
                grouped.setdefault(scope, []).append(pod)
                visible.add((scope, pod.markers["WITNESS_START_ID"]))
        capacity_available = True
        if self.allocator.admission is not None:
            pending = self.queue.unresolved()
            try:
                self.allocator.admission.check(self._inventory, self.deployment, pending=len(pending - visible))
            except CapacityReached:
                capacity_available = False
        for scope, entry in entries:
            if scope.round != self.round:
                continue
            if self._due.get(scope, 0) > now:
                if not entry.submitted:
                    break
                continue
            self._due[scope] = now + self.poll_seconds
            result = self._progress.get(scope)
            finished = (result is not None and result.start_id == entry.start_id and
                        result.phase in ("allocated", "rejected", "retired"))
            if not finished:
                if entry.submitted and (scope, entry.start_id) not in visible:
                    operation = self.allocator.operations.get(scope)
                    if (operation is None or operation.submitted
                            or operation.session.start_id != entry.start_id):
                        continue
                if not entry.submitted and not capacity_available and not any(
                    owner.hotkey == scope.hotkey for owner in grouped
                ):
                    break
                try:
                    result = await self._run(self._worker, partial(self._advance, scope, entry))
                except CreateDeferred as error:
                    self._due[scope] = error.retry_at
                    break
                except AuthRejected as error:
                    if error.status != 403:
                        raise
                    if not entry.submitted and str(error) in ("hotkey_not_registered", "hotkey_not_admitted"):
                        await self._run(self._worker, partial(self.allocator.cancel, scope, entry.start_id))
                        if await self._write(partial(self.queue.cancel, scope, entry.start_id)):
                            self._progress.pop(scope, None)
                    break
            if result is not None and result.start_id == entry.start_id:
                if result.phase != "lost":
                    self._progress[scope] = result
                if result.phase in ("allocated", "rejected", "retired"):
                    self._current[scope] = result
                    self._sessions[scope, entry.start_id] = result
                    await self._write(partial(self.queue.complete, scope, entry.start_id,
                                              rejected=result.phase == "rejected"))
                    self._progress.pop(scope, None)
            break
        for scope, session in sorted(self._current.items(), key=lambda item: self._startup_due.get(item[0], 0)):
            if not round_ready:
                break
            if (scope in self._grants or self.queue.pending(scope) is not None or
                    self._startup_due.get(scope, 0) > now):
                continue
            starting_pod = self._pods.get(session.pod_id or "")
            if starting_pod is None or now - starting_pod.epoch_ns / 1_000_000_000 < STARTUP_SECONDS:
                continue
            self._startup_due[scope] = now + self.poll_seconds
            try:
                expired = await self._run(self._worker, partial(self._expire_unserved, scope, session))
            except Exception:  # noqa: BLE001 - retry this worker without delaying other workers' access
                break
            if expired is not None:
                self._current[scope] = self._sessions[scope, session.start_id] = expired
            break
        for scope, session in self._current.items():
            if not round_ready:
                continue
            if self.queue.pending(scope) is not None or self._due.get(scope, 0) > now:
                continue
            pods = grouped.get(scope, [])
            drained = next((pod for pod in pods if pod.id in self._retiring), None)
            if drained is not None:
                # A permanent drain report remains valid after its socket closes.
                self._due[scope] = now + self.poll_seconds
                await self._run(self._worker, partial(self.allocator.retire, scope,
                                                     drained.markers["WITNESS_START_ID"], drained.id, drain=False))
                break
            if len(pods) > 1 or any(p.lifecycle != "active" or p.status in ("EXITED", "ERROR") or
                                    p.id in self._retiring for p in pods):
                self._due[scope] = now + self.poll_seconds
                await self._run(self._worker, partial(self.allocator.reconcile, scope))
                break
        for scope, session in self._current.items():
            peer = self.registry.peers.get(session.pod_id or "")
            if (peer is None or not self.registry.ready(session.pod_id or "") or peer.certificate is None or
                    self._grant_due.get(scope, 0) > now):
                continue
            grant = self._grants.get(scope)
            if (grant is not None and grant.get("certificate") == peer.certificate and
                    int(str(grant["expires"])) > time.time() + 60):
                continue
            self._grant_due[scope] = now + self.poll_seconds
            result_grant = await self._run(self._worker, partial(self._grant, scope, session, peer.certificate))
            if result_grant is not None:
                self._grants[scope] = result_grant
            return

    async def _lifecycle(self, _: web.Application) -> AsyncIterator[None]:
        await self._run(self._journal, self.queue.load)
        if self.rounds is None:
            await self._run(self._journal, partial(self.queue.discard_old, self.round))
        tasks = [asyncio.create_task(self._fences()), asyncio.create_task(self._work())]
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            for executor in (self._worker, self._journal, self._verifiers, self._fencing):
                await asyncio.to_thread(partial(executor.shutdown, wait=True, cancel_futures=True))

    async def pod_control(self, request: web.Request) -> web.WebSocketResponse:
        scope = self._control_scope(request)
        start_id, pod_id = (
            identifier(request.match_info["start_id"]),
            request.headers.get("x-witness-pod", ""),
        )
        token = capability(self.secret, scope, start_id)
        if not hmac.compare_digest(
            request.headers.get("Authorization", ""), "Bearer " + token
        ):
            raise AuthRejected("invalid_pod_capability")
        pod = self._pods.get(pod_id)
        if (
            pod is None
            or pod.scope() != scope
            or pod.markers["WITNESS_START_ID"] != start_id
        ):
            raise AuthRejected("pod_identity_changed", 409)
        if pod.lifecycle == "pending" or (
            pod.lifecycle == "active" and pod.status != "RUNNING"
        ):
            raise AuthRejected("pod_starting", 503)
        if pod_id in self.registry.peers or pod_id in self._connecting:
            raise AuthRejected("pod_already_connected", 409)
        socket = web.WebSocketResponse(max_msg_size=16384, heartbeat=15)
        self._connecting.add(pod_id)
        try:
            await socket.prepare(request)
        finally:
            self._connecting.discard(pod_id)
        peer = Peer(socket, scope, start_id, pod_id)
        self.registry.peers[pod_id] = peer
        retirement: asyncio.Task[None] | None = None

        async def retire() -> None:
            try:
                retired = await self._run(self._worker, partial(self.allocator.retire, scope, start_id, pod_id,
                                                               drain=False))
                if retired.phase == "retired" and not socket.closed:
                    await socket.send_json({"type": "retired"})
            except Exception:  # noqa: BLE001 - retain permanent drain for lifecycle retries
                self._wake.set()

        try:
            selected = self._selected(scope, start_id, pod_id)
            await self._send_policy(peer)
            await socket.send_json(
                {
                    "type": "activate" if selected else "drain",
                    "pod_id": pod_id,
                    "id": "selection",
                }
            )
            async for message in socket:
                if message.type != WSMsgType.TEXT:
                    break
                data = object_dict(json.loads(message.data))
                peer.seen = time.monotonic()
                certificate = data.get("certificate")
                if isinstance(certificate, str) and len(certificate) <= 8192:
                    peer.certificate = certificate
                if data.get("type") in ("ready", "heartbeat"):
                    selected = self._fence_ok and self._selected(scope, start_id, pod_id)
                    peer.ready = (
                        selected
                        and data.get("ready") is True
                        and data.get("proof_key_sha256") == self.proof_hash
                        and data.get("pricing_sha256") == self.pricing_hash
                    )
                    self._wake.set()
                    if not selected and self._fence_at > 0:
                        await socket.send_json({"type": "drain", "id": "selection"})
                elif data.get("type") == "reply":
                    pending = peer.replies.get(str(data.get("id")))
                    if pending is not None and not pending.done():
                        pending.set_result(data)
                elif data.get("type") == "drained":
                    peer.ready = False
                    peer.drained = True
                    self._retiring.add(pod_id)
                    if retirement is None or retirement.done():
                        retirement = asyncio.create_task(retire())
                else:
                    break
        finally:
            peer.ready = False
            if self.registry.peers.get(pod_id) is peer:
                self.registry.peers.pop(pod_id, None)
            for future in peer.replies.values():
                if not future.done():
                    future.set_result({})
            if retirement is not None:
                retirement.cancel()
                await asyncio.gather(retirement, return_exceptions=True)
            await socket.close()
        return socket

    def application(self) -> web.Application:
        app = web.Application(middlewares=[self.middleware], client_max_size=16384,
                              handler_args={"max_headers": 32, "max_field_size": 4096,
                                            "max_line_size": 2048, "keepalive_timeout": 30})
        app.cleanup_ctx.append(self._lifecycle)
        app.add_routes([
            web.get("/health", self.health),
            web.get("/v1/pods/current", self.current),
            web.post("/v1/pods/start", self.start),
            web.get("/v1/pods/{start_id}", self.pod_status),
            web.get("/v1/control/{hotkey}/{start_id}", self.pod_control),
        ])
        return app
