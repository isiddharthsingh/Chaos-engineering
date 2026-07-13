"""Load composers — typed load intent -> k6-operator CRs."""

from chaosagent.load.k6 import LoadSpec, compose_testrun

__all__ = ["LoadSpec", "compose_testrun"]
