import os
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor


def instrument(app,name):
    provider=TracerProvider(resource=Resource.create({'service.name':name,'deployment.environment.name':'demo'}))
    endpoint=os.getenv('OTEL_EXPORTER_OTLP_TRACES_ENDPOINT')
    if endpoint:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint,timeout=3)))
    FastAPIInstrumentor.instrument_app(app,tracer_provider=provider,excluded_urls='health,telemetry,admin,metrics')
    # One app per production process. Tests can create multiple isolated app instances.
    if not HTTPXClientInstrumentor().is_instrumented_by_opentelemetry:
        HTTPXClientInstrumentor().instrument(tracer_provider=provider)
    return provider.get_tracer(name)


def trace_id():
    context=trace.get_current_span().get_span_context()
    return format(context.trace_id,'032x') if context.is_valid else None
