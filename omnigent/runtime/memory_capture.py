"""Compaction-summary capture into the agent memory plane (FU1 T10, ADR-0132).

When a Layer-2 compaction summary is persisted, rescue it into durable
long-term memory so distilled session knowledge survives the lossy compaction
boundary. Runs SERVER-SIDE (the runner has no DB; it persists items over HTTP),
best-effort: a capture failure never blocks the durable item persist or the
agent turn. The compaction item in ``conversation_items`` stays the source of
truth for the summary text; the memory is a derived, recallable copy that
references it via ``source_compaction_id`` (dedup-guarded, so a re-persist
captures at most once).
"""

from __future__ import annotations

import logging

from omnigent.entities.conversation import CompactionData

_logger = logging.getLogger(__name__)

# conv-summary memories are high-salience and age on a ~30d scale (ADR-0132).
_SUMMARY_WEIGHT = 2.5
_SUMMARY_HALF_LIFE_SECONDS = 30 * 86_400


def capture_compaction_summaries(
    store, conversation_id: str, agent_id: str | None, items
) -> int:
    """Append a memory for each just-persisted compaction item with a summary.

    :param store: The ``SqlAlchemyMemoryStore`` (omnigent's sole memory writer).
    :param conversation_id: The conversation whose items were persisted.
    :param agent_id: The conversation's bound agent id (memory owner). When
        absent, capture is skipped — there is no owner to scope to.
    :param items: The just-persisted conversation items (each exposes ``id`` /
        ``type`` / ``data``; ``data`` is a :class:`CompactionData` when
        ``type == "compaction"``).
    :returns: The number of summaries captured.
    """
    if not agent_id:
        return 0
    captured = 0
    for item in items:
        if getattr(item, "type", None) != "compaction":
            continue
        data = getattr(item, "data", None)
        if not isinstance(data, CompactionData) or not data.summary:
            continue
        compaction_id = getattr(item, "id", None)
        if compaction_id and store.exists_for_compaction(compaction_id):
            continue
        try:
            store.append(
                scope="agent",
                owner=agent_id,
                name=f"conv:{conversation_id}:summary",
                content=data.summary,
                weight=_SUMMARY_WEIGHT,
                half_life_seconds=_SUMMARY_HALF_LIFE_SECONDS,
                source_conversation_id=conversation_id,
                source_compaction_id=compaction_id,
            )
            captured += 1
        except Exception:  # noqa: BLE001
            _logger.warning(
                "compaction-summary capture failed for %s/%s",
                conversation_id,
                compaction_id,
                exc_info=True,
            )
    return captured
