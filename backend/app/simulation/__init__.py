"""Data simulation layer (Phase 1).

Produces synthetic Razorpay ``payment.failed`` webhook deliveries that match the
provider's real payload shape, so the pipeline is built against production-shaped
input rather than a convenient simplification.

This package is input generation only. It contains no pipeline logic, and no
pipeline stage may import ground-truth labels from it.
"""

from app.simulation.abandonment_catalog import (
    SCENARIOS as ABANDONMENT_SCENARIOS,
)
from app.simulation.abandonment_catalog import (
    AbandonmentScenario,
    AbandonmentSignal,
)
from app.simulation.abandonment_catalog import (
    covered_root_causes as abandonment_root_causes,
)
from app.simulation.decline_catalog import (
    SCENARIOS,
    DeclineScenario,
    Provenance,
    covered_root_causes,
    scenarios_for_root_cause,
)
from app.simulation.fixtures import (
    batch_to_dict,
    load_fixture,
    webhooks_only,
    write_fixture,
)
from app.simulation.generator import (
    BatchFixture,
    GeneratedEvent,
    SyntheticCustomer,
    generate_batch,
)
from app.simulation.signing import canonical_body, sign_body, signed_delivery

__all__ = [
    "ABANDONMENT_SCENARIOS",
    "SCENARIOS",
    "AbandonmentScenario",
    "AbandonmentSignal",
    "BatchFixture",
    "DeclineScenario",
    "abandonment_root_causes",
    "GeneratedEvent",
    "Provenance",
    "SyntheticCustomer",
    "batch_to_dict",
    "canonical_body",
    "covered_root_causes",
    "generate_batch",
    "load_fixture",
    "scenarios_for_root_cause",
    "sign_body",
    "signed_delivery",
    "webhooks_only",
    "write_fixture",
]
