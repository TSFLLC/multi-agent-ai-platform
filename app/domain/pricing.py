"""FREE / PAID / UNKNOWN pricing classification.

Owner instruction (Section 3 of the MA0 brief): the platform must be able to
derive whether a model is free, paid, or unknown from *current provider
pricing metadata*, never from a hard-coded static list of free model IDs.

This is intentionally a pure function over the nullable pricing columns
already on ``provider_models`` / ``provider_model_snapshots``
(``cost_input_per_mtok`` / ``cost_output_per_mtok``) rather than a stored,
redundant column — classification always reflects the pricing fields it was
computed from, and can never drift out of sync with them the way a cached
column could after a catalog refresh.
"""

from decimal import Decimal
from typing import Optional

from app.db.enums import PricingClassification

Number = Optional[Decimal]


def classify_pricing(input_cost_per_mtok: Number, output_cost_per_mtok: Number) -> PricingClassification:
    """Classify a (Model, Provider) pairing's pricing.

    - Both costs known and both exactly zero -> FREE.
    - Either cost known and strictly positive -> PAID.
    - Anything else (either cost missing, i.e. not yet refreshed from the
      provider) -> UNKNOWN, never guessed.
    """
    if input_cost_per_mtok is None or output_cost_per_mtok is None:
        return PricingClassification.UNKNOWN
    if input_cost_per_mtok == 0 and output_cost_per_mtok == 0:
        return PricingClassification.FREE
    if input_cost_per_mtok > 0 or output_cost_per_mtok > 0:
        return PricingClassification.PAID
    return PricingClassification.UNKNOWN
