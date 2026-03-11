"""
OpenTelemetry tracing — distributed tracing across all agents.
Same setup as agent-pipeline: Cloud Trace in prod, console in dev.
"""
import os
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    ConsoleSpanExporter,
    SimpleSpanProcessor,
    BatchSpanProcessor,
)
from opentelemetry.sdk.resources import Resource

ENVIRONMENT = os.getenv("ENVIRONMENT", "development")
SERVICE_NAME = os.getenv("K_SERVICE", "intervention-platform")


def init_tracing(service_name: str = None):
    """
    Initialize OpenTelemetry tracing.
    
    Production: exports to Cloud Trace
    Development: exports to console
    """
    name = service_name or SERVICE_NAME
    
    resource = Resource.create({
        "service.name": name,
        "service.version": "1.0.0",
        "deployment.environment": ENVIRONMENT,
    })
    
    provider = TracerProvider(resource=resource)
    
    if ENVIRONMENT == "production":
        from opentelemetry.exporter.cloud_trace import CloudTraceSpanExporter
        exporter = CloudTraceSpanExporter()
        provider.add_span_processor(BatchSpanProcessor(exporter))
    else:
        provider.add_span_processor(
            SimpleSpanProcessor(ConsoleSpanExporter())
        )
    
    trace.set_tracer_provider(provider)
    return trace.get_tracer(name)


def get_tracer(name: str = None):
    """Get a tracer for the given service."""
    return trace.get_tracer(name or SERVICE_NAME)
