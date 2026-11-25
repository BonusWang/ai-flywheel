"""HTTP route modules, one router factory per business domain.

Each factory takes the runtime container and returns an ``APIRouter``; ``app.py``
only wires them together, so no route module holds global state.
"""
from ai_flywheel.routes.automations import build_automations_router
from ai_flywheel.routes.governance import build_governance_router
from ai_flywheel.routes.overview import build_overview_router
from ai_flywheel.routes.runs import build_runs_router

__all__ = [
    "build_automations_router",
    "build_governance_router",
    "build_overview_router",
    "build_runs_router",
]
