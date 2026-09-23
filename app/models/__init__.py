"""Import every model module so ``Base.metadata`` is fully populated.

Alembic's ``env.py`` imports this package (not individual model modules) so
autogeneration always sees the complete schema.
"""

from app.models import (
    agents,
    artifacts_eval,
    concepts,
    evaluation_definitions,
    evaluation_runs,
    execution,
    governance,
    identity,
    learner,
    observability,
    providers,
    radar,
    reviews,
    tasks,
    taxonomy,
    workflow,
)

__all__ = [
    "agents",
    "artifacts_eval",
    "concepts",
    "evaluation_definitions",
    "evaluation_runs",
    "execution",
    "governance",
    "identity",
    "learner",
    "observability",
    "providers",
    "radar",
    "reviews",
    "tasks",
    "taxonomy",
    "workflow",
]
