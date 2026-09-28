"""Verify authenticated OTLP storage for the three Observagent signals."""
import argparse
import os
import time
from urllib.parse import urlsplit
from uuid import uuid4

import requests
from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceRequest
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import ExportMetricsServiceRequest
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest


def resource_for(target):
    for name, value in {'service.name': 'lmstudio-api', 'agent.id': 'lmstudio-api',
                        'session.id': 'lmstudio-delivery-test'}.items():
        attr = target.attributes.add(); attr.key = name; attr.value.string_value = value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--empty', action='store_true', help='Test auth without creating records')
    args = parser.parse_args()
    base, key = os.environ.get('OBSERVAGENT_ENDPOINT', '').rstrip('/'), os.environ.get('OBSERVAGENT_API_KEY', '')
    if not base or not key: parser.error('Set OBSERVAGENT_ENDPOINT and OBSERVAGENT_API_KEY first')
    if urlsplit(base).path.strip('/'): parser.error('OBSERVAGENT_ENDPOINT must be the base URL only')
    traces, logs, metrics = ExportTraceServiceRequest(), ExportLogsServiceRequest(), ExportMetricsServiceRequest()
    if not args.empty:
        tr = traces.resource_spans.add(); resource_for(tr.resource)
        span = tr.scope_spans.add().spans.add(); span.name = 'lmstudio.delivery-test'
        span.trace_id, span.span_id = uuid4().bytes, os.urandom(8); span.start_time_unix_nano = time.time_ns(); span.end_time_unix_nano = span.start_time_unix_nano + 1_000_000
        lr = logs.resource_logs.add(); resource_for(lr.resource)
        record = lr.scope_logs.add().log_records.add(); record.time_unix_nano = time.time_ns(); record.trace_id = span.trace_id; record.span_id = span.span_id; record.severity_number = 9; record.severity_text = 'INFO'; record.body.string_value = 'Synthetic LM Studio delivery check'
        attr = record.attributes.add(); attr.key = 'event.name'; attr.value.string_value = 'lmstudio.delivery-test'
        mr = metrics.resource_metrics.add(); resource_for(mr.resource)
        metric = mr.scope_metrics.add().metrics.add(); metric.name = 'lmstudio.delivery.test'; point = metric.sum.data_points.add(); point.as_int = 1; point.time_unix_nano = time.time_ns()
    failed = False
    for signal, batch in [('traces', traces), ('logs', logs), ('metrics', metrics)]:
        try:
            response = requests.post(f'{base}/v1/{signal}', headers={'Authorization': f'Bearer {key}', 'Content-Type': 'application/x-protobuf'}, data=batch.SerializeToString(), timeout=30, allow_redirects=False)
            print(f'{signal}: HTTP {response.status_code}'); print(response.text); response.raise_for_status()
            result = response.json(); expected = 0 if args.empty else 1
            if result.get('status') != 'ok' or result.get('count') != expected: failed = True
        except (requests.RequestException, ValueError) as exc:
            print(f'{signal}: verification failed ({type(exc).__name__})'); failed = True
    return int(failed)


if __name__ == '__main__': raise SystemExit(main())
