"""Model Router — Section 11, 14. Resolves task model requirements into a
(Model, Provider) pair; manual mode first (MA3), simple policy-based Auto
next, empirical/intelligent routing later (MA8).

FREE_ONLY/PREFER_FREE/ANY (Owner requirement, Section 3) are represented on
``router_policy_versions.free_policy`` (app.db.enums.RouterFreePolicy) so
this service can consume them without a schema change when built.
"""

from app.services.base import BaseService


class ModelRouter(BaseService):
    def resolve_manual(self, *args, **kwargs):
        raise NotImplementedError("ModelRouter manual mode lands in MA3")

    def resolve_auto(self, *args, **kwargs):
        raise NotImplementedError("ModelRouter auto mode lands in MA3 (simple) / MA8 (intelligent)")
