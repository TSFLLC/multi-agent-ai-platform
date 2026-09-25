"""Import every model module so ``Base.metadata`` is fully populated.

Alembic's ``env.py`` imports this package (not individual model modules) so
autogeneration always sees the complete schema.
"""

from app.models import (
    academy,
    agents,
    artifacts_eval,
    assessment,
    concepts,
    evaluation_definitions,
    evaluation_runs,
    execution,
    governance,
    identity,
    lab,
    learner,
    learning_review,
    observability,
    providers,
    radar,
    reviews,
    tasks,
    taxonomy,
    workflow,
)

__all__ = [
    "academy",
    "agents",
    "artifacts_eval",
    "assessment",
    "concepts",
    "evaluation_definitions",
    "evaluation_runs",
    "execution",
    "governance",
    "identity",
    "lab",
    "learner",
    "learning_review",
    "observability",
    "providers",
    "radar",
    "reviews",
    "tasks",
    "taxonomy",
    "workflow",
]
