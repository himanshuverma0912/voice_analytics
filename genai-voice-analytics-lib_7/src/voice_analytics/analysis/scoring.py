"""Turning a model's raw response into validated, grouped scores.

Two rules from the specification drive this module:

* **"KPIs grouped by objective."** Results roll up per objective, with counts
  leading -- the interface shows "Compliance -- 8 of 22".
* **"A score without evidence is a failed KPI, not a zero."** A KPI that
  returns a number with no supporting quote is recorded as unevidenced and its
  score is discarded, rather than being averaged in as though it were judged.
"""

from __future__ import annotations

import logging
from typing import Any

from voice_analytics.analysis.models import (
    CallImpact,
    EvidenceItem,
    KPIScore,
    ObjectiveRollup,
    RiskSummary,
)

logger = logging.getLogger(__name__)

#: Cap on evidence items kept per KPI, so a runaway response cannot produce an
#: unbounded record.
MAX_EVIDENCE_ITEMS = 20

#: Alternative key names models use for the same field.
_FIELD_ALIASES = {
    "rationale": ("rationale", "reason", "justification", "explanation"),
    "raw_score": ("raw_score", "raw_value", "rawScore"),
    "kpi_name": ("kpi_name", "name", "title", "kpiName"),
    "section": ("section", "objective", "category", "group"),
    "kpi_code": ("kpi_code", "code", "kpiCode", "id"),
}


def _first_present(item: dict, field: str) -> Any:
    """Read a field, accepting any of its known aliases."""
    for key in _FIELD_ALIASES.get(field, (field,)):
        if key in item and item[key] is not None:
            return item[key]
    return None


def _coerce_score(value: Any) -> float | None:
    """Turn a model's score into a float, or ``None`` when it is not a number.

    Handles the binary KPIs the interface exposes: ``"Yes"``/``"No"`` and
    ``true``/``false`` map onto 1 and 0.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return float(value)

    text = str(value).strip().lower()
    if text in {"yes", "true", "pass", "y"}:
        return 1.0
    if text in {"no", "false", "fail", "n"}:
        return 0.0
    try:
        return float(text)
    except ValueError:
        return None


def parse_evidence(raw: Any) -> list[EvidenceItem]:
    """Validate the evidence array, skipping entries that are not objects."""
    if not isinstance(raw, list):
        return []

    items: list[EvidenceItem] = []
    for entry in raw[:MAX_EVIDENCE_ITEMS]:
        if isinstance(entry, dict):
            items.append(EvidenceItem.model_validate(entry))
        elif isinstance(entry, str) and entry.strip():
            # Some responses give a bare quote rather than an object.
            items.append(EvidenceItem(quote=entry))
    return items


def parse_kpis(raw: Any) -> list[KPIScore]:
    """Validate the model's ``kpis`` array into :class:`KPIScore` objects.

    Entries without a ``kpi_code`` are dropped: a score that cannot be matched
    to a definition is not usable, and keeping it would inflate the counts.
    """
    if isinstance(raw, dict):
        raw = raw.get("kpis")
    if not isinstance(raw, list):
        return []

    scores: list[KPIScore] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue

        code = _first_present(entry, "kpi_code")
        if not code:
            logger.warning(
                "Skipping a KPI with no code; it cannot be matched to a definition."
            )
            continue

        scores.append(
            KPIScore(
                section=str(_first_present(entry, "section") or "Uncategorised"),
                kpi_code=str(code),
                kpi_name=str(_first_present(entry, "kpi_name") or ""),
                score=_coerce_score(entry.get("score")),
                raw_score=(
                    str(_first_present(entry, "raw_score"))
                    if _first_present(entry, "raw_score") is not None
                    else None
                ),
                rationale=str(_first_present(entry, "rationale") or ""),
                positive_impact=str(entry.get("positive_impact") or "Not applicable"),
                negative_impact=str(entry.get("negative_impact") or "Not applicable"),
                applicable=bool(entry.get("applicable", True)),
                observable=bool(entry.get("observable", True)),
                attempted=bool(entry.get("attempted", False)),
                evidence=parse_evidence(entry.get("evidence")),
            )
        )

    return scores


def parse_risk(raw: Any) -> RiskSummary | None:
    """Validate the optional ``risk_summary`` block.

    Returns ``None`` when the model did not provide one -- risk flags are not
    part of the Flow A specification, so their absence is normal.
    """
    if not isinstance(raw, dict):
        return None

    evidence = {
        key: parse_evidence(value)
        for key, value in (raw.get("risk_evidence") or {}).items()
    }
    reasons = {
        key: (str(value) if value is not None else None)
        for key, value in (raw.get("risk_reason") or {}).items()
    }

    return RiskSummary(
        high_risk_call=bool(raw.get("high_risk_call", False)),
        manual_review_required=bool(raw.get("manual_review_required", False)),
        compliance_violation=bool(raw.get("compliance_violation", False)),
        privacy_violation=bool(raw.get("privacy_violation", False)),
        mis_selling_alert=bool(raw.get("mis_selling_alert", False)),
        risk_reason=reasons,
        risk_evidence=evidence,
    )


def parse_call_impact(raw: Any) -> CallImpact | None:
    """Validate the optional ``call_impact`` block.

    Accepts ``impact_level`` or ``level`` -- the originating service reads the
    former, but models return both.
    """
    if not isinstance(raw, dict):
        return None

    level = raw.get("impact_level") or raw.get("level")
    reason = raw.get("reason") or raw.get("impact_reason")

    impact = CallImpact(
        level=str(level) if level is not None else None,
        reason=str(reason) if reason is not None else None,
    )
    return impact if (impact.level or impact.reason) else None


def parse_experience_drivers(raw: Any) -> list[str]:
    """Validate ``customer_experience_drivers`` into a list of strings.

    Accepts a list of strings, or of objects carrying a driver/name/text field,
    which is how models tend to vary.
    """
    if isinstance(raw, str):
        return [raw.strip()] if raw.strip() else []
    if not isinstance(raw, list):
        return []

    drivers: list[str] = []
    for entry in raw[:MAX_EVIDENCE_ITEMS]:
        if isinstance(entry, str) and entry.strip():
            drivers.append(entry.strip())
        elif isinstance(entry, dict):
            value = entry.get("driver") or entry.get("name") or entry.get("text")
            if isinstance(value, str) and value.strip():
                drivers.append(value.strip())
    return drivers


def apply_evidence_rule(kpis: list[KPIScore]) -> list[str]:
    """Discard scores that arrived without supporting evidence.

    From the specification: *"Every score returns a justification and the
    transcript span it came from. A score without evidence is a failed KPI, not
    a zero."*

    An unevidenced score is set to ``None`` so it cannot be averaged in, and its
    code is returned. Treating it as zero would be worse: an unjustifiable zero
    is indistinguishable from a genuine failure, and in a compliance review that
    difference matters.

    KPIs marked not applicable are exempt -- there is nothing to evidence.

    Returns:
        The codes of the KPIs whose scores were discarded.
    """
    unevidenced: list[str] = []

    for kpi in kpis:
        if kpi.score is None or not kpi.applicable:
            continue
        if kpi.has_evidence:
            continue

        unevidenced.append(kpi.kpi_code)
        kpi.score = None
        if not kpi.rationale:
            kpi.rationale = "No supporting transcript span was returned."

    # The caller reports this to the user; logging it here too would duplicate.
    return unevidenced


def roll_up_by_objective(
    kpis: list[KPIScore],
    unevidenced: list[str],
) -> list[ObjectiveRollup]:
    """Group scores by objective, as the interface presents them.

    Objectives appear in the order they are first seen, so the grouping follows
    the order KPIs were requested in rather than an arbitrary sort.
    """
    order: list[str] = []
    grouped: dict[str, list[KPIScore]] = {}

    for kpi in kpis:
        if kpi.section not in grouped:
            grouped[kpi.section] = []
            order.append(kpi.section)
        grouped[kpi.section].append(kpi)

    unevidenced_set = set(unevidenced)
    rollups: list[ObjectiveRollup] = []

    for objective in order:
        members = grouped[objective]
        scored = [kpi for kpi in members if kpi.is_scored]

        rollups.append(
            ObjectiveRollup(
                objective=objective,
                total=len(members),
                scored=len(scored),
                not_applicable=sum(1 for kpi in members if not kpi.applicable),
                unevidenced=sum(
                    1 for kpi in members if kpi.kpi_code in unevidenced_set
                ),
                average=(
                    round(sum(kpi.score for kpi in scored) / len(scored), 2)
                    if scored
                    else None
                ),
            )
        )

    return rollups
