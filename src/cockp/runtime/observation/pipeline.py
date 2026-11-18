"""Run stages in security-sensitive order, stopping on a handled response."""

from .feedback import feedback
from .function import function
from .imports import imports
from .model import ObservationContext, ObservationPorts
from .narration import narration
from .routing import routing
from .selection import selection
from .sources import sources

STAGES = (sources, selection, routing, function, feedback, imports, narration)


async def observe(ports: ObservationPorts, context: ObservationContext):
    for stage in STAGES:
        result = await stage(ports, context)
        if result is not None:
            return result
    raise RuntimeError("Observation pipeline did not handle its request")
