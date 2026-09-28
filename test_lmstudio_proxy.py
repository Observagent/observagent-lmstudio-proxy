"""Offline checks for forwarding and the three telemetry pipelines."""
import json
import unittest
from unittest.mock import patch

import httpx
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from lmstudio_proxy import app, Telemetry, ResponseInspector


class Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk


class ProxyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.traces = InMemorySpanExporter()
        self.logs = InMemoryLogRecordExporter()
        self.reader = InMemoryMetricReader()
        with patch('lmstudio_proxy.OTLPSpanExporter', return_value=self.traces) as traces, \
             patch('lmstudio_proxy.OTLPLogExporter', return_value=self.logs) as logs, \
             patch('lmstudio_proxy.OTLPMetricExporter') as metrics, \
             patch('lmstudio_proxy.PeriodicExportingMetricReader', return_value=self.reader):
            self.telemetry = Telemetry('https://backend.test', 'test-observagent-key', 'test-session')
            for factory, signal in ((traces, 'traces'), (logs, 'logs'), (metrics, 'metrics')):
                self.assertEqual(factory.call_args.kwargs['endpoint'], f'https://backend.test/v1/{signal}')
                self.assertEqual(factory.call_args.kwargs['headers']['Authorization'], 'Bearer test-observagent-key')
        app.state.telemetry = self.telemetry
        app.state.upstream = 'http://127.0.0.1:1234'
        app.state.lm_token = 'test-lm-key'

    def tearDown(self):
        self.telemetry.shutdown()

    async def call(self, chunks, sse=False, status=200, path='/v1/chat/completions', failure=False):
        def upstream(request):
            self.assertEqual(request.url.port, 1234)
            self.assertEqual(request.headers['authorization'], 'Bearer test-lm-key')
            self.assertNotIn('test-observagent-key', str(request.headers))
            if failure:
                raise httpx.ConnectError('offline')
            return httpx.Response(status, stream=Chunks(chunks), headers={
                'content-type': 'text/event-stream' if sse else 'application/json'})
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as upstream_client:
            app.state.client = upstream_client
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://proxy') as client:
                result = await client.post(path, json={'model': 'test-model', 'messages': [{'role': 'user', 'content': 'private prompt'}]})
        self.telemetry.traces.force_flush()
        self.telemetry.logs.force_flush()
        return result

    def captured(self):
        spans = self.traces.get_finished_spans()
        logs = self.logs.get_finished_logs()
        self.assertEqual(len(spans), 1)
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0].log_record.trace_id, spans[0].context.trace_id)
        self.assertEqual(logs[0].resource.attributes['agent.id'], 'lmstudio-api')
        self.assertEqual(logs[0].resource.attributes['session.id'], 'test-session')
        metric_data = self.reader.get_metrics_data()
        metrics = {m.name: m for rm in metric_data.resource_metrics for sm in rm.scope_metrics for m in sm.metrics}
        self.assertEqual(metrics['lmstudio.request.count'].data.data_points[0].value, 1)
        self.assertEqual(metrics['lmstudio.request.duration'].data.data_points[0].count, 1)
        return spans[0], logs[0].log_record, metrics

    async def test_json_all_signals_and_no_content(self):
        payload = {'choices': [{'message': {'content': 'private answer'}}], 'usage': {'prompt_tokens': 7, 'completion_tokens': 3}}
        raw = json.dumps(payload).encode()
        result = await self.call([raw[:21], raw[21:]])
        self.assertEqual(result.content, raw)
        span, record, metrics = self.captured()
        self.assertEqual(record.attributes['event.name'], 'lmstudio.request.completed')
        self.assertEqual(record.attributes['input_token_count'], 7)
        self.assertEqual(record.attributes['output_token_count'], 3)
        self.assertNotIn('private', str(record.attributes) + str(span.attributes) + str(record.body))
        self.assertEqual(sorted(dp.value for dp in metrics['lmstudio.token.count'].data.data_points), [3, 7])

    async def test_sse_split_chunks(self):
        raw = (b'data: {"choices":[{"delta":{"content":"hello"}}],"usage":null}\r\n\r\n'
               b'data: {"usage":{"prompt_tokens":11,"completion_tokens":4}}\n\n'
               b'data: [DONE]\n\n')
        result = await self.call([raw[:5], raw[5:44], raw[44:99], raw[99:]], sse=True)
        self.assertEqual(result.content, raw)
        _, record, _ = self.captured()
        self.assertEqual(record.attributes['event.name'], 'lmstudio.request.completed')
        self.assertEqual(record.attributes['output_token_count'], 4)

    async def test_missing_usage_is_not_zero(self):
        await self.call([b'data: {"choices":[]}\n\ndata: [DONE]\n\n'], sse=True)
        _, record, metrics = self.captured()
        self.assertNotIn('input_token_count', record.attributes)
        self.assertNotIn('lmstudio.token.count', metrics)

    async def test_embeddings_input_only(self):
        await self.call([b'{"data":[],"usage":{"prompt_tokens":9}}'], path='/v1/embeddings')
        _, record, _ = self.captured()
        self.assertEqual(record.attributes['input_token_count'], 9)
        self.assertNotIn('output_token_count', record.attributes)

    async def test_http_error(self):
        response = await self.call([b'{"error":"private server error"}'], status=401)
        self.assertEqual(response.status_code, 401)
        _, record, _ = self.captured()
        self.assertEqual(record.attributes['event.name'], 'lmstudio.request.failed')
        self.assertEqual(record.severity_text, 'ERROR')
        self.assertNotIn('private', str(record.attributes))

    async def test_connection_failure(self):
        response = await self.call([], failure=True)
        self.assertEqual(response.status_code, 502)
        _, record, _ = self.captured()
        self.assertEqual(record.attributes['event.name'], 'lmstudio.request.failed')

    async def test_incomplete_stream(self):
        await self.call([b'data: {"choices":[]}\n\n'], sse=True)
        _, record, _ = self.captured()
        self.assertEqual(record.attributes['event.name'], 'lmstudio.request.incomplete')

    async def test_unsupported_endpoint(self):
        response = await self.call([], path='/v1/responses')
        self.assertEqual(response.status_code, 404)
        self.assertFalse(self.logs.get_finished_logs())

    def test_parser_limit(self):
        span = self.telemetry.tracer.start_span('test')
        parser = ResponseInspector(span, False)
        parser.LIMIT = 10
        parser.feed(b'x' * 11)
        parser.finish()
        self.assertTrue(span.attributes['lmstudio.usage_capture_truncated'])
        self.assertNotIn('input_token_count', span.attributes)
        span.end()


if __name__ == '__main__':
    unittest.main()
