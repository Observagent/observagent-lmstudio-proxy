"""Observe LM Studio's compatible local API; not a Bionic app-wide exporter."""
import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from opentelemetry._logs import SeverityNumber
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import SpanKind, StatusCode, set_span_in_context

log = logging.getLogger('uvicorn.error')
GENERATION_PATHS = {'v1/chat/completions', 'v1/completions', 'v1/embeddings'}
HOP_HEADERS = {'host', 'connection', 'keep-alive', 'transfer-encoding', 'te',
               'trailer', 'upgrade', 'proxy-authorization', 'proxy-authenticate'}


def forward_headers(headers):
    excluded = HOP_HEADERS | {v.strip().lower() for v in headers.get('connection', '').split(',')}
    return {k: v for k, v in headers.items() if k.lower() not in excluded}


def http_base(value):
    parsed = urlsplit(value)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('Use an HTTP(S) base URL without credentials, query, or fragment')
    return value.rstrip('/')


class Telemetry:
    def __init__(self, base, key, session):
        resource = Resource.create({'service.name': 'lmstudio-api', 'agent.id': 'lmstudio-api',
                                    'session.id': session, 'telemetry.source': 'observagent.local-api-proxy'})
        headers = {'Authorization': f'Bearer {key}'}
        self.traces = TracerProvider(resource=resource)
        self.traces.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(
            endpoint=base + '/v1/traces', headers=headers, timeout=10), schedule_delay_millis=1000))
        self.logs = LoggerProvider(resource=resource)
        self.logs.add_log_record_processor(BatchLogRecordProcessor(OTLPLogExporter(
            endpoint=base + '/v1/logs', headers=headers, timeout=10), schedule_delay_millis=1000))
        reader = PeriodicExportingMetricReader(OTLPMetricExporter(
            endpoint=base + '/v1/metrics', headers=headers, timeout=10), export_interval_millis=5000)
        self.metrics = MeterProvider(resource=resource, metric_readers=[reader])
        self.tracer = self.traces.get_tracer('observagent.lmstudio-proxy')
        self.logger = self.logs.get_logger('observagent.lmstudio-proxy')
        meter = self.metrics.get_meter('observagent.lmstudio-proxy')
        self.requests = meter.create_counter('lmstudio.request.count', unit='{request}')
        self.duration = meter.create_histogram('lmstudio.request.duration', unit='s')
        self.tokens = meter.create_counter('lmstudio.token.count', unit='{token}')

    def finish(self, span, started):
        elapsed = time.monotonic() - started
        attrs = dict(span.attributes)
        failed = span.status.status_code == StatusCode.ERROR
        outcome = 'failed' if failed else ('completed' if attrs.get('lmstudio.response.complete') else 'incomplete')
        attrs.update({'event.name': f'lmstudio.request.{outcome}', 'duration_ms': elapsed * 1000})
        dimensions = {'gen_ai.request.model': attrs.get('gen_ai.request.model', 'unknown'),
                      'operation': attrs['operation'], 'outcome': outcome}
        context = set_span_in_context(span)
        try:
            self.requests.add(1, dimensions, context=context)
            self.duration.record(elapsed, dimensions, context=context)
            for direction, name in [('input', 'input_token_count'), ('output', 'output_token_count')]:
                if name in attrs:
                    self.tokens.add(attrs[name], {**dimensions, 'token.type': direction}, context=context)
            self.logger.emit(timestamp=time.time_ns(), context=context,
                severity_number=SeverityNumber.ERROR if failed else SeverityNumber.INFO,
                severity_text='ERROR' if failed else 'INFO', body=attrs['event.name'], attributes=attrs)
            log.info('%s: input=%s output=%s; logs, metrics and trace queued',
                     attrs['event.name'], attrs.get('input_token_count'), attrs.get('output_token_count'))
        except Exception:
            log.exception('Could not queue request telemetry')
        finally:
            span.end()

    def shutdown(self):
        for provider in (self.metrics, self.logs, self.traces):
            try:
                provider.shutdown()
            except Exception:
                log.exception('Telemetry shutdown failed')


class ResponseInspector:
    """Parse JSON or SSE without changing forwarded bytes or retaining content in telemetry."""
    LIMIT = 8 * 1024 * 1024

    def __init__(self, span, sse):
        self.span, self.sse = span, sse
        self.pending = b''
        self.event = []
        self.event_size = 0
        self.disabled = False

    def parse(self, raw):
        if raw.strip() == b'[DONE]':
            self.span.set_attribute('lmstudio.response.complete', True)
            return
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            return
        if not isinstance(value, dict):
            return
        if value.get('error'):
            self.span.set_status(StatusCode.ERROR, 'LM Studio returned an error')
        elif not self.sse:
            self.span.set_attribute('lmstudio.response.complete', True)
        usage = value.get('usage')
        if isinstance(usage, dict):
            for source, target, semantic in [('prompt_tokens', 'input_token_count', 'gen_ai.usage.input_tokens'),
                                              ('completion_tokens', 'output_token_count', 'gen_ai.usage.output_tokens')]:
                count = usage.get(source)
                if type(count) is int and count >= 0:
                    self.span.set_attribute(target, count)
                    self.span.set_attribute(semantic, count)

    def feed(self, chunk):
        if self.disabled:
            return
        self.pending += chunk
        if len(self.pending) + self.event_size > self.LIMIT:
            self.disabled = True
            self.pending, self.event = b'', []
            self.span.set_attribute('lmstudio.usage_capture_truncated', True)
            return
        if self.sse:
            while b'\n' in self.pending:
                line, self.pending = self.pending.split(b'\n', 1)
                self.line(line.rstrip(b'\r'))

    def line(self, line):
        if line.startswith(b'data:'):
            data = line[5:].lstrip(b' ')
            self.event.append(data)
            self.event_size += len(data)
        elif not line and self.event:
            self.parse(b'\n'.join(self.event))
            self.event, self.event_size = [], 0

    def finish(self):
        if self.disabled:
            return
        if self.sse:
            if self.pending:
                self.line(self.pending.rstrip(b'\r'))
            self.line(b'')
        elif self.pending:
            self.parse(self.pending)


@asynccontextmanager
async def lifespan(app):
    base, key = os.environ.get('OBSERVAGENT_ENDPOINT'), os.environ.get('OBSERVAGENT_API_KEY')
    if not base or not key:
        raise RuntimeError('Set OBSERVAGENT_ENDPOINT (base URL) and OBSERVAGENT_API_KEY in this terminal')
    base = http_base(base)
    if urlsplit(base).path.rstrip('/').endswith(('/v1/logs', '/v1/metrics', '/v1/traces')):
        raise ValueError('OBSERVAGENT_ENDPOINT must be the base URL, without /v1/logs, /v1/metrics or /v1/traces')
    upstream = http_base(os.environ.get('LMSTUDIO_UPSTREAM', 'http://127.0.0.1:1234'))
    if urlsplit(upstream).port == 1235 and urlsplit(upstream).hostname in ('localhost', '127.0.0.1', '::1'):
        raise ValueError('Upstream points to the proxy; use the LM Studio server on port 1234')
    app.state.upstream = upstream
    app.state.lm_token = os.environ.get('LMSTUDIO_API_TOKEN')
    telemetry = Telemetry(base, key, os.environ.get('OBSERVAGENT_SESSION_ID', str(uuid4())))
    app.state.telemetry = telemetry
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(600, connect=10), trust_env=False) as client:
            app.state.client = client
            yield
    finally:
        await asyncio.to_thread(telemetry.shutdown)


app = FastAPI(lifespan=lifespan)


@app.api_route('/{path:path}', methods=['GET', 'POST'])
async def proxy(path: str, request: Request):
    observed = request.method == 'POST' and path in GENERATION_PATHS
    if not observed and not (request.method == 'GET' and path == 'v1/models'):
        return JSONResponse({'error': 'This proxy supports /v1/models, /v1/chat/completions, /v1/completions and /v1/embeddings only'}, status_code=404)
    body = await request.body()
    started = time.monotonic()
    telemetry = request.app.state.telemetry
    span = telemetry.tracer.start_span('lmstudio.request', kind=SpanKind.CLIENT) if observed else None
    if span:
        span.set_attribute('operation', path)
        span.set_attribute('gen_ai.provider.name', 'lmstudio')
        span.set_attribute('lmstudio.response.complete', False)
        try:
            payload = json.loads(body)
            if isinstance(payload, dict) and isinstance(payload.get('model'), str):
                span.set_attribute('gen_ai.request.model', payload['model'])
        except (ValueError, UnicodeDecodeError):
            pass
    headers = forward_headers(request.headers)
    headers['accept-encoding'] = 'identity'
    if request.app.state.lm_token:
        headers['authorization'] = 'Bearer ' + request.app.state.lm_token
    url = httpx.URL(request.app.state.upstream + '/' + path).copy_with(query=request.scope['query_string'])
    client = request.app.state.client
    try:
        upstream = await client.send(client.build_request(request.method, url, headers=headers, content=body), stream=True)
    except BaseException as exc:
        if span:
            span.set_status(StatusCode.ERROR, type(exc).__name__)
            telemetry.finish(span, started)
        if isinstance(exc, httpx.HTTPError):
            return JSONResponse({'error': 'Cannot reach the configured LM Studio API server'}, status_code=502)
        raise
    if span:
        span.set_attribute('http.response.status_code', upstream.status_code)
        if upstream.status_code >= 400:
            span.set_status(StatusCode.ERROR, 'Upstream HTTP error')
    inspector = ResponseInspector(span, 'text/event-stream' in upstream.headers.get('content-type', '')) if span else None

    async def stream():
        try:
            async for chunk in upstream.aiter_bytes():
                if inspector:
                    inspector.feed(chunk)
                yield chunk
            if inspector:
                inspector.finish()
        except BaseException as exc:
            if span:
                span.set_status(StatusCode.ERROR, type(exc).__name__)
            raise
        finally:
            if span:
                telemetry.finish(span, started)
            await upstream.aclose()

    response_headers = forward_headers(upstream.headers)
    # aiter_bytes decodes compression; don't retain the encoded length/encoding.
    response_headers.pop('content-encoding', None)
    response_headers.pop('content-length', None)
    return StreamingResponse(stream(), status_code=upstream.status_code, headers=response_headers)
