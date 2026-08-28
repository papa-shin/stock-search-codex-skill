from __future__ import annotations

import sys


def write_stdout_utf8(value: str) -> None:
    """Write the public CLI protocol as UTF-8 regardless of legacy code pages."""
    stream = sys.stdout
    buffer = getattr(stream, "buffer", None)
    if buffer is None:
        stream.write(value)
        return
    stream.flush()
    buffer.write(value.encode("utf-8"))
    buffer.flush()
