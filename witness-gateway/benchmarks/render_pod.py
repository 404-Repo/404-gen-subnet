"""Isolated ordinary-Pod benchmark using production capture and R2 storage."""
from __future__ import annotations

import asyncio, contextlib, hashlib, json, os, secrets, signal, ssl, subprocess, threading, time
from types import SimpleNamespace
from decimal import Decimal
from pathlib import Path
from aiohttp import web, ClientSession, ClientTimeout
from pod_gateway.auth import Authenticator, Membership
from pod_gateway.budget import Budget, BudgetRejected
from pod_gateway.control import DrainControl, Registry
from pod_gateway.lifecycle import Allocator
from pod_gateway.models import Pod, PodSpec, Scope
from pod_gateway.records import CallRecords
from pod_gateway.replay import export_bundle, native_verifier
from pod_gateway.security import capability
from pod_gateway.state import R2StateStore
from pod_gateway.server import Gateway
from storage import R2Store
from pod import PodRuntime
import worker, timing
import pod as pod_module
import httpx

os.umask(0o077)
ROUND = os.environ.get('BENCH_ROUND', 'test-0')
ROOT = Path('/private-benchmark')
ROOT.mkdir(mode=0o700, exist_ok=True)
ADMIN = os.environ['BENCH_ADMIN_TOKEN']
POD = os.environ['RUNPOD_POD_ID']
END = int(os.environ['BENCH_END'])
DEPLOYMENT = os.environ['BENCH_DEPLOYMENT']
HOTKEY = os.environ['BENCH_HOTKEY']
START = os.environ['BENCH_START_ID']
IMAGE = os.environ['BENCH_IMAGE']
LOCK = threading.RLock()
PHASE = 'boot'
ACTIVE = set()
DISPATCHES = 0
RENDERS = 0
RUNTIME = None
RENDERER = None
STORE = None
MEMBERSHIP = None
GATEWAY_RUNNER = None
PRICING = Path('/app/config/pricing.json').read_bytes()
PROOF = hashlib.sha256((worker.PARAMETERS/'verifying.bin').read_bytes()).hexdigest()

def event(name, **data):
    value = dict(event=name, time=time.time(), phase=PHASE, **data)
    with LOCK:
        with (ROOT/'events.jsonl').open('a') as out: out.write(json.dumps(value)+'\n')

original_measure = worker.measure
@contextlib.contextmanager
def measured(name):
    exchange = timing.exchange_id()
    if name == 'provider_response_capture':
        with LOCK: ACTIVE.add(exchange)
        event('provider_capture_start', exchange_id=exchange, simultaneous=len(ACTIVE))
    started = time.monotonic()
    try:
        with original_measure(name): yield
    finally:
        if name == 'provider_response_capture':
            with LOCK: ACTIVE.discard(exchange)
            event('provider_capture_end', exchange_id=exchange, seconds=time.monotonic()-started)
worker.measure = measured

class Storage:
    """Production R2 stores with an owner-prefix guard and private local audit."""
    def __init__(self):
        self.state = R2StateStore.connect(endpoint=os.environ['R2_ENDPOINT'], access_key=os.environ['R2_ACCESS_KEY_ID'],
            secret_key=os.environ['R2_SECRET_ACCESS_KEY'], bucket=os.environ['R2_BUCKET'])
        self.evidence = R2Store()
    def check(self, key):
        if not (('/'+DEPLOYMENT+'/') in key or key.startswith('rounds/'+ROUND+'/'+HOTKEY+'/')):
            raise ValueError('Storage key outside benchmark scope')
    def mirror(self, key, data):
        p=ROOT/'objects'/key;p.parent.mkdir(parents=True,exist_ok=True)
        with LOCK:
            tmp=p.with_name(p.name+'.tmp');tmp.write_bytes(data);tmp.replace(p)
    def load(self, key):
        self.check(key);return self.state.load(key)
    def save(self, key, data, expected):
        self.check(key);started=time.monotonic()
        result=self.state.save(key,data,expected);self.mirror(key,data)
        event('r2_journal_save',seconds=time.monotonic()-started,bytes=len(data))
        return result
    def get(self, key, limit=16*1024*1024):
        self.check(key);return self.evidence.get(key,limit)
    read=get
    def put(self, key, data):
        self.check(key);started=time.monotonic();self.evidence.put(key,data)
        event('r2_evidence_put',seconds=time.monotonic()-started,bytes=len(data))
    def delete(self, key):
        self.check(key);self.evidence.delete(key)

class CappedBudget(Budget):
    def reserve(self, *args):
        global DISPATCHES
        with LOCK:
            if DISPATCHES >= int(os.environ.get('BENCH_DISPATCH_LIMIT', '2048')): raise BudgetRejected('Benchmark dispatch cap')
            known=Decimal(0)
            for p in (ROOT/'objects/pod-usage').rglob('*.json'):
                for charge in json.loads(p.read_bytes()).get('charges',{}).values():
                    if charge.get('credits') is not None: known+=Decimal(charge['credits'])
            if known >= Decimal(os.environ.get('BENCH_MODEL_LEDGER_LIMIT_USD', '6')) * 1000000: raise BudgetRejected('Benchmark ledger cap')
            super().reserve(*args);DISPATCHES+=1
            event('dispatch_reserved',count=DISPATCHES)

class FixedProvider:
    def __init__(self, pod): self.pod=pod
    def list(self, **kwargs): return [self.pod]
    def get(self, identifier): return self.pod if identifier==self.pod.id else None
    def create(self, *args, **kwargs): raise RuntimeError('Infrastructure writes disabled in benchmark container')
    rename=delete=create

def renderer_start():
    global RENDERER
    subprocess.run(['node', '--import', '/opt/renderer/src/software-rendering.js', '-e', ''], check=True)
    Path('/usr/lib/chromium/SwiftShader.ini').chmod(0o644)
    def isolate():
        os.setsid();os.setgroups([]);os.setgid(1001);os.setuid(1001)
    env={k:v for k,v in os.environ.items() if k in ('PATH','LANG','PUPPETEER_EXECUTABLE_PATH')}
    env.update(HOME='/tmp/renderer-home',PORT='8002',STATIC_PORT='3000',RENDER_POOL_SIZE='1',
        VALIDATION_POOL_SIZE='1',RENDER_TIMEOUT_MS='30000',VALIDATION_TIMEOUT_MS='5000',
        RENDER_QUEUE_CAPACITY='8',RENDER_QUEUE_WAIT_MS='120000',
        LD_PRELOAD='/opt/witness-affinity/libwitness-affinity.so')
    Path('/tmp/renderer-home').mkdir(exist_ok=True);os.chown('/tmp/renderer-home',1001,1001)
    RENDERER=subprocess.Popen(['node','src/server.js'],cwd='/opt/renderer',env=env,
        stdout=(ROOT/'renderer.log').open('ab',buffering=0),stderr=subprocess.STDOUT,preexec_fn=isolate)
    event('renderer_start',pid=RENDERER.pid)

def chromium(sig):
    count=0
    for p in Path('/proc').iterdir():
        if not p.name.isdigit():continue
        try:
            if p.stat().st_uid==1001 and b'/usr/lib/chromium/chromium' in (p/'cmdline').read_bytes():
                os.kill(int(p.name),sig);count+=1
        except (OSError,ProcessLookupError):pass
    return count

def sampler():
    while time.time()<END:
        try:
            base=Path('/sys/fs/cgroup');cpu=dict(line.split() for line in (base/'cpu.stat').read_text().splitlines())
            users={0:0,1001:0}
            for p in Path('/proc').iterdir():
                if not p.name.isdigit():continue
                try:
                    uid=p.stat().st_uid
                    if uid in users:
                        stat=(p/'stat').read_text().rsplit(')',1)[1].split();users[uid]+=int(stat[11])+int(stat[12])
                except (OSError,ValueError):pass
            value=dict(event='sample',time=time.time(),phase=PHASE,usage_usec=int(cpu['usage_usec']),
                memory=int((base/'memory.current').read_text()),peak_memory=int((base/'memory.peak').read_text()),
                renderer_cpu_ticks=users[1001],witness_cpu_ticks=users[0],clock_ticks=os.sysconf('SC_CLK_TCK'),
                provider_active=len(ACTIVE),slots=0 if RUNTIME is None else len(RUNTIME.active))
            with (ROOT/'samples.jsonl').open('a') as out:out.write(json.dumps(value)+'\n')
            if MEMBERSHIP is not None:
                now=int(time.time()*1000);MEMBERSHIP.snapshot.update(fetched_at=now,block_time=now)
        except OSError:pass
        time.sleep(.5)

async def renderer_ready(http):
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if RENDERER.poll() is not None:
            raise RuntimeError('Renderer exited during startup')
        try:
            async with http.get('http://127.0.0.1:8002/ping', timeout=ClientTimeout(total=1)) as response:
                if response.status == 200:
                    return
        except (OSError, asyncio.TimeoutError):
            pass
        await asyncio.sleep(.25)
    raise TimeoutError('Renderer readiness deadline reached')

async def main():
    global STORE,RUNTIME,MEMBERSHIP,GATEWAY_RUNNER,PHASE
    http=ClientSession(timeout=ClientTimeout(total=45))
    @web.middleware
    async def auth(request,handler):
        if not secrets.compare_digest(request.headers.get('Authorization',''),'Bearer '+ADMIN):raise web.HTTPUnauthorized()
        return await handler(request)
    app=web.Application(middlewares=[auth],client_max_size=2*1024*1024)
    admin=web.AppRunner(app,access_log=None)

    async def boot(request):
        global STORE,RUNTIME,MEMBERSHIP,GATEWAY_RUNNER,PHASE
        if RUNTIME is not None:raise web.HTTPConflict()
        body=await request.json()
        for key in ('R2_ENDPOINT','R2_ACCESS_KEY_ID','R2_SECRET_ACCESS_KEY','R2_BUCKET'):os.environ[key]=body['storage'][key]
        for name in ('ca.pem','cert.pem','key.pem'):
            p=ROOT/name;p.write_text(body['tls'][name]);p.chmod(0o600)
        os.environ['SSL_CERT_FILE']=str(ROOT/'ca.pem')
        local_trust=ssl.create_default_context(cafile=str(ROOT/'ca.pem'))
        def selection_get(url, **kwargs):
            if not url.startswith('https://localhost:8000/worker/exchanges/'):
                raise RuntimeError('Unexpected benchmark selection origin')
            return httpx.get(url,verify=local_trust,**kwargs)
        pod_module.httpx=SimpleNamespace(get=selection_get)
        tls=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);tls.load_cert_chain(ROOT/'cert.pem',ROOT/'key.pem')
        renderer_start()
        await renderer_ready(http)
        STORE=Storage();records=CallRecords(STORE);budget=CappedBudget(STORE,PRICING)
        secret=secrets.token_bytes(32);scope=Scope(DEPLOYMENT,ROUND,HOTKEY);spec=PodSpec(IMAGE,capability_secret=secret)
        pod=Pod(POD,scope.name(START)+'-active',IMAGE,'RUNNING',scope.markers(START,spec.fingerprint,time.time_ns()),hourly_cost=str(body['hourly']))
        registry=Registry();allocator=Allocator(FixedProvider(pod),DrainControl(registry),spec)
        MEMBERSHIP=Membership(DEPLOYMENT,17,ROUND,HOTKEY);now=int(time.time()*1000)
        MEMBERSHIP.snapshot=dict(version=1,network='finney',netuid=17,block=0,fetched_at=now,block_time=now,hotkeys=[])
        gateway=Gateway(allocator=allocator,records=records,budget=budget,auth=Authenticator(MEMBERSHIP),registry=registry,
            evidence=STORE,deployment=DEPLOYMENT,round_number=ROUND,round_end=END,secret=secret,proof_hash=PROOF,
            prompts=frozenset(body['prompts']),fence=lambda:True,pricing_hash=hashlib.sha256(PRICING).hexdigest())
        GATEWAY_RUNNER=web.AppRunner(gateway.application(),access_log=None);await GATEWAY_RUNNER.setup()
        await web.TCPSite(GATEWAY_RUNNER,'0.0.0.0',8000,ssl_context=tls).start()
        await web.TCPSite(admin,'0.0.0.0',8443,ssl_context=tls).start()
        RUNTIME=PodRuntime(evidence=STORE,records=records,budget=budget,scope=scope,start_id=START,pod_id=POD,
            token=capability(secret,scope,START),origin='https://localhost:8000',proof_hash=PROOF,round_end=END,
            spool=ROOT/'spool',max_captures=12)
        app['records'],app['budget'],app['scope']=records,budget,scope
        threading.Thread(target=RUNTIME.run,daemon=True).start();PHASE=body['phase']
        event('ready',python=os.sys.version.split()[0],node=subprocess.check_output(['node','--version'],text=True).strip(),
            chromium=subprocess.check_output(['/usr/bin/chromium','--version'],text=True).strip(),proof_hash=PROOF)
        return web.json_response({'ready':True,'proof_hash':PROOF,'pricing_hash':budget.pricing_hash})

    async def status(request):
        return web.json_response(dict(phase=PHASE,pod_id=POD,booted=RUNTIME is not None,provider_active=len(ACTIVE),
            slots=0 if RUNTIME is None else len(RUNTIME.active),dispatches=DISPATCHES,renders=RENDERS,
            renderer_alive=RENDERER is not None and RENDERER.poll() is None))
    async def config(request):
        global PHASE
        PHASE=(await request.json())['phase'];event('phase_start')
        return web.json_response({'ok':True})
    async def render(request):
        global RENDERS
        RENDERS+=1
        if RENDERS>8192:raise web.HTTPTooManyRequests()
        started=time.monotonic()
        async with http.post('http://127.0.0.1:8002'+request.rel_url.path_qs,data=await request.read(),headers={'Content-Type':'application/json'}) as response:
            data=await response.read();elapsed=time.monotonic()-started
            event('render_reply',status=response.status,seconds=elapsed,bytes=len(data),
                wait_ms=response.headers.get('X-Render-Queue-Wait-Ms'),png_sha256=hashlib.sha256(data).hexdigest() if response.status==200 else None)
            return web.Response(body=data,status=response.status,headers={'Content-Type':response.headers.get('Content-Type','application/octet-stream'),
                'X-Render-Queue-Wait-Ms':response.headers.get('X-Render-Queue-Wait-Ms',''),'X-Render-Wall-Ms':str(elapsed*1000)})
    async def fault(request):
        kind=(await request.json())['kind']
        if kind=='browser_kill':count=chromium(signal.SIGKILL)
        elif kind=='renderer_restart':
            os.killpg(RENDERER.pid,signal.SIGKILL);RENDERER.wait();renderer_start();count=1
        else:raise web.HTTPBadRequest()
        event('fault',kind=kind,count=count);return web.json_response({'count':count})
    async def export(request):
        generation=(await request.json())['generation']
        if len(generation)!=64 or any(c not in '0123456789abcdef' for c in generation):raise web.HTTPBadRequest()
        output=ROOT/'bundles'/generation;output.parent.mkdir(exist_ok=True)
        if output.exists():return web.json_response({'verified':True,'saved':True})
        await asyncio.to_thread(export_bundle,records=app['records'],evidence=STORE,scope=app['scope'],generation_id=generation,
            output=output,proof_hash=PROOF,pricing_hash=app['budget'].pricing_hash,
            verify=native_verifier(Path('/app/witness-server/worker/verify.mjs'),worker.PARAMETERS))
        event('bundle_verified',generation=generation);return web.json_response({'verified':True})
    async def download(request):
        proc=await asyncio.create_subprocess_exec('tar','czf','-','--exclude=ca.pem','--exclude=cert.pem','--exclude=key.pem',
            '--exclude=objects/rounds','-C',str(ROOT),'.',stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL)
        response=web.StreamResponse(headers={'Content-Type':'application/gzip'});await response.prepare(request)
        while data:=await proc.stdout.read(256*1024):await response.write(data)
        if await proc.wait():raise RuntimeError('Evidence archive failed')
        await response.write_eof();return response
    app.add_routes([web.post('/boot',boot),web.get('/status',status),web.post('/config',config),
        web.post('/render/grid',render),web.post('/render',render),web.post('/fault',fault),web.post('/export',export),web.get('/download',download)])
    await admin.setup();await web.TCPSite(admin,'0.0.0.0',8001).start()
    threading.Thread(target=sampler,daemon=True).start()
    while time.time()<END:await asyncio.sleep(1)
    if RUNTIME is not None:RUNTIME.drain()
    event('deadline_reached');await http.close();await admin.cleanup()
    if GATEWAY_RUNNER is not None:await GATEWAY_RUNNER.cleanup()

asyncio.run(main())
