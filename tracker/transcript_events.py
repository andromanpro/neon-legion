"""Shared transcript event predicates without application dependencies."""


def is_compact_summary(event: object) -> bool:
    """Compaction output is synthetic history, never a new human prompt."""
    return isinstance(event, dict) and (
        event.get("isCompactSummary") is True or event.get("type") == "compacted"
    )
