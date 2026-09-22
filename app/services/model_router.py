"""Model Router — Section 11, 14. Resolves a model policy into a
(Model, Provider) pair.

The routing logic lives in ``app.model_resolution`` (MA3 manual/auto
resolution, extended in MA8.1 with structured decisions and the
``model_routing_decisions`` audit record); this service is only the
Control Plane name for it, so there is exactly one router. MA8.1 is
deterministic — evidence-based routing is MA8.2.

FREE_ONLY/PREFER_FREE/ANY (Owner requirement, Section 3) are read from
``ModelPolicy.auto_policy`` (app.db.enums.RouterFreePolicy).
"""

from typing import Optional

from app.model_resolution import RoutingDecision, route
from app.services.base import BaseService


class ModelRouter(BaseService):
    def route(self, model_policy: Optional[dict]) -> RoutingDecision:
        return route(self.db, model_policy)
