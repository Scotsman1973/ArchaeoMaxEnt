"""Entry point of the modelling runtime: runs one job and reports back as JSON lines.

Started by runtimeClient.py with the runtime's own Python, never imported by
QGIS. Reads {"protocol", "operation", "args"} from standard input, writes
'log' / 'progress' events and finally one 'result' or 'error' to standard
output. Anything libraries print is redirected to standard error, so it can
never corrupt the reply.
"""
import importlib
import json
import sys
import traceback
from pathlib import Path

PROTOCOL_VERSION = 1


def main() -> int:
    """Run the job given on standard input and return the process exit code."""
    reply = sys.stdout
    sys.stdout = sys.stderr

    def emit(eventType, **fields):
        """Write one event as a JSON line to the reply stream."""
        reply.write(json.dumps({"type": eventType, **fields}, default=str) + "\n")
        reply.flush()

    try:
        job = json.load(sys.stdin)
        if job.get("protocol") != PROTOCOL_VERSION:
            raise ValueError(f"Job protocol {job.get('protocol')!r} not supported (expected {PROTOCOL_VERSION})")
        pluginDir = Path(__file__).resolve().parent
        sys.path.insert(0, str(pluginDir.parent))
        operations = importlib.import_module(f"{pluginDir.name}.workerOperations").OPERATIONS
        operation = operations.get(job.get("operation"))
        if operation is None:
            raise ValueError(f"Unknown operation {job.get('operation')!r}; known: {sorted(operations)}")
        emit("result", value=operation(emit, **job.get("args", {})))
        return 0
    except Exception as exc:
        emit("error", message=str(exc) or type(exc).__name__, errorType=type(exc).__name__,
             traceback=traceback.format_exc())
        return 1


if __name__ == "__main__":
    sys.exit(main())
