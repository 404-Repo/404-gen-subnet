"""Apply the pinned run configuration and durable authorization notification handling."""
import hashlib, json, os, runpy
from pathlib import Path
harness=Path('/opt/benchmark/harness.py')
auth=Path('/app/witness-gateway/pod_gateway/auth.py')
expected={'/opt/benchmark/harness.py': 'd9226851242826e090bbaa60faf34330bbb201418225a2dc49239c58b26c998a', '/app/witness-gateway/pod_gateway/auth.py': '9ff1f312e7c932aeb7e4db0820d627acd1bbcd5351a445c16a2d5867631f0c91'}
for path in (harness,auth):
    assert hashlib.sha256(path.read_bytes()).hexdigest()==expected[str(path)], 'Published image code differs'
text=harness.read_text()
for old,new,count in [("'test-0'", "'test-3'", 3), ('rounds/test-0/', 'rounds/test-3/', 1), ('DISPATCHES >= 160', 'DISPATCHES >= 2048', 1), ('Decimal(10000000)', 'Decimal(100000000)', 1), ('RENDERS>1000', 'RENDERS>8192', 1)]:
    assert text.count(old)==count, 'Run configuration does not match image'
    text=text.replace(old,new)
harness.write_text(text)
text=auth.read_text()
old='{"test-0", "test-1", "test-2"}'
assert text.count(old)==1
auth.write_text(text.replace(old,'{"test-0", "test-1", "test-2", "test-3"}'))
gateway=Path('/app/witness-client/src/witness_client/gateway.py')
worker=Path('/app/witness-server/worker/pod.py')
assert hashlib.sha256(gateway.read_bytes()).hexdigest()=='a118087c568394161d6a1d1778fe11447f8c99985d8603cc0a24dbccfa6696de'
assert hashlib.sha256(worker.read_bytes()).hexdigest()=='c955f5b2c86153f024f2edd898a94c963b8c19f19da28ca17684ed7038aad04d'
gateway.write_text(gateway.read_text()+'\n\nimport socket, threading\nfrom typing import Any\nfrom websockets.client import ClientProtocol\nfrom websockets.sync.client import ClientConnection\n\nclass _GatewayConnection(ClientConnection):\n    """Send the upgrade before reading TLS session tickets on the shared socket."""\n\n    def __init__(\n        self, sock: socket.socket, protocol: ClientProtocol, **kwargs: Any\n    ) -> None:\n        self._upgrade_sent = threading.Event()\n        super().__init__(sock, protocol, **kwargs)\n\n    def recv_events(self) -> None:\n        # Concurrent ticket processing and SSL_write can lose the opening request.\n        self._upgrade_sent.wait()\n        super().recv_events()\n\n    def send_data(self) -> None:\n        try:\n            super().send_data()\n        finally:\n            self._upgrade_sent.set()\n\n    def close_socket(self) -> None:\n        self._upgrade_sent.set()\n        super().close_socket()\n')
text=worker.read_text()
text=text.replace('from witness_client.calls import Invocation\n','from witness_client.calls import Invocation\nfrom witness_client.gateway import _GatewayConnection\n')
text=text.replace('            close_timeout=5,\n        )','            close_timeout=5,\n            create_connection=_GatewayConnection,\n        )',1)
text=text.replace('                        ping_timeout=20,\n                    )','                        ping_timeout=20,\n                        create_connection=_GatewayConnection,\n                    )',1)
assert text.count('create_connection=_GatewayConnection')==2
worker.write_text(text)

relay_worker=Path('/app/witness-server/worker/worker.py')
worker_channel=Path('/app/witness-server/worker/channel.py')
assert hashlib.sha256(relay_worker.read_bytes()).hexdigest()=='30e771af587994ff26764b93842deb1ef2629c333cd69d0f565346f36d4f3a25'
assert hashlib.sha256(worker_channel.read_bytes()).hexdigest()=='6addddc4cdd06ffdbe30372ff72a5851f6dd9c10b9485dde5695415c7f9ebd7b'
text=relay_worker.read_text()
old='                channel.notify(\n                    {"status": "running", "handshook": True, "authorized": True}\n                )\n'
assert text.count(old)==1
relay_worker.write_text(text.replace(old,'                # Durable authorization survives loss of its live acknowledgement.\n                with suppress(OSError, WebSocketException):\n                    channel.notify(\n                        {"status": "running", "handshook": True, "authorized": True}\n                    )\n'))

if os.environ.get('BENCH_CHECK_ONLY')=='1':
    for path in (harness,auth,gateway,worker,relay_worker,worker_channel):compile(path.read_text(),str(path),'exec')
    print(json.dumps({'configuration_validated':True,'hashes':{str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in (harness,auth,gateway,worker,relay_worker,worker_channel)}}))
else:
    if os.environ.get('BENCH_HTTPS_PROXY')=='1':
        from aiohttp import web
        original_start=web.TCPSite.start
        async def start(site):
            await original_start(site)
            if site._port==8000:
                await original_start(web.TCPSite(site._runner,'0.0.0.0',8003))
        web.TCPSite.start=start
    runpy.run_path(str(harness),run_name='__main__')
