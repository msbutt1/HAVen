"""Host-side streaming processing (reference implementation; SIMULATED inputs so far).

    from haven_signals.processing import Processor, ProcessingConfig
    proc = Processor(ProcessingConfig())
    for record in decoded_records:
        for out in proc.feed(record):
            ...  # stream_features / stream_event dicts
    tail = proc.finish()
"""

from .config import ProcessingConfig, StreamConfig
from .processor import OUTPUT_SCHEMA, OUTPUT_VERSION, Processor, ProcessingSummary
from .serialize import to_ndjson_line, write_ndjson

__all__ = ["OUTPUT_SCHEMA", "OUTPUT_VERSION", "ProcessingConfig", "ProcessingSummary",
           "Processor", "StreamConfig", "to_ndjson_line", "write_ndjson"]
