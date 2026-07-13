"""Fault composers — engine-agnostic FaultSpec intent -> concrete engine CRs.

Phase 1 ships the Chaos Mesh PodChaos composer; the wider fault library
(NetworkChaos, StressChaos, ...) arrives in Phase 2.
"""

from chaosagent.faults.chaosmesh import UnsupportedFaultError, compose_podchaos

__all__ = ["UnsupportedFaultError", "compose_podchaos"]
