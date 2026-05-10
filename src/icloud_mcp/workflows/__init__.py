"""Cross-cutting workflow tools that compose calendar + mail.

Single-call summaries and chained operations live here. Each workflow tool
is a thin orchestrator over the calendar and mail clients — no new
data-fetching primitives, just useful compositions.
"""
