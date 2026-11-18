"""Public narration lifecycle; private planning never bypasses review."""

from .finalize import finalize
from .generation import generate
from .prepare import prepare
from .workflow_model import NarrationPorts, NarrationRun


async def narrate(ports: NarrationPorts, run: NarrationRun):
    for stage in (prepare, generate, finalize):
        result = await stage(ports, run)
        if result is not None:
            return result
    raise RuntimeError("Narration workflow did not produce an outcome")
