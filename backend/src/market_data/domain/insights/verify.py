"""Step 3: accept only what is well-formed.

A provider -- an LLM especially -- can return malformed JSON: a missing field,
the wrong type, an enum value that does not exist. That is checked here, and
anything that fails is returned as a :class:`Rejection` with the reason, not
repaired. Nothing beyond the schema is checked: a pattern's and a suggestion's
reading of the evidence is trusted as the provider states it.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from pydantic import ValidationError

from market_data.domain.insights.models import Pattern, Rejection, Suggestion

# --------------------------------------------------------------------------- #
# Patterns
# --------------------------------------------------------------------------- #


def verify_patterns(drafts: Iterable[object]) -> tuple[list[Pattern], list[Rejection]]:
    kept: list[Pattern] = []
    rejected: list[Rejection] = []
    for draft in drafts:
        try:
            kept.append(Pattern.model_validate(draft))
        except ValidationError as exc:
            rejected.append(Rejection(stage="pattern", item=_label(draft), reason=_schema_reason(exc)))
    return kept, rejected


# --------------------------------------------------------------------------- #
# Suggestions
# --------------------------------------------------------------------------- #


def verify_suggestions(drafts: Iterable[object]) -> tuple[list[Suggestion], list[Rejection]]:
    kept: list[Suggestion] = []
    rejected: list[Rejection] = []
    for draft in drafts:
        try:
            kept.append(Suggestion.model_validate(draft))
        except ValidationError as exc:
            rejected.append(Rejection(stage="suggestion", item=_label(draft), reason=_schema_reason(exc)))
    return kept, rejected


def _label(draft: object) -> str | None:
    if isinstance(draft, Mapping):
        for key in ("id", "title", "type"):
            if isinstance(draft.get(key), str):
                return str(draft[key])
    return None


def _schema_reason(exc: ValidationError) -> str:
    first = exc.errors()[0]
    where = ".".join(str(part) for part in first["loc"]) or "item"
    return f"is malformed: {where}: {first['msg'].lower()}"
