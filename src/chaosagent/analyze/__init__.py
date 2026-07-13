"""Analysis — turn a finished run into a scored, actionable report.

Pure given the run record: no cluster, no metrics store, and deliberately no
LLM — the score and the suggestions are deterministic so two identical runs
always read identically.
"""

from chaosagent.analyze.report import (
    ExperimentReport,
    HypothesisVerdict,
    PhaseStats,
    Suggestion,
    build_report,
    render_text,
)

__all__ = [
    "ExperimentReport",
    "HypothesisVerdict",
    "PhaseStats",
    "Suggestion",
    "build_report",
    "render_text",
]
