"""Result types for KPI scoring.

Shaped by the wireframe's Flow A: KPIs grouped by objective, each carrying a
justification and the transcript span it came from. There is deliberately **no
overall score** -- see :class:`AnalysisResult`.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator


class EvidenceItem(BaseModel):
    """A transcript span supporting a score.

    ``coerce_numbers_to_str`` because models return ``turn_index`` and
    ``speaker`` inconsistently as numbers or strings.
    """

    model_config = ConfigDict(coerce_numbers_to_str=True)

    turn_index: int = Field(default=0, description="Position in the conversation.")
    speaker: str = Field(default="", description="Who said it, e.g. Agent.")
    quote: str = Field(default="", description="The exact words scored against.")


class KPIScore(BaseModel):
    """One KPI's verdict on one call."""

    model_config = ConfigDict(coerce_numbers_to_str=True)

    section: str = Field(description="The objective this KPI belongs to.")
    kpi_code: str
    kpi_name: str = ""
    score: float | None = Field(
        default=None, description="None when the KPI did not apply."
    )
    raw_score: str | None = Field(
        default=None, description="The model's own wording, e.g. 'Yes' for a binary KPI."
    )
    rationale: str = Field(default="", description="Why this score was given.")
    positive_impact: str = "Not applicable"
    negative_impact: str = "Not applicable"

    # These three separate "the agent failed" from "this did not apply",
    # which a bare score of zero cannot express.
    applicable: bool = Field(
        default=True, description="Did this KPI apply to this call at all?"
    )
    observable: bool = Field(
        default=True, description="Could it be judged from the transcript?"
    )
    attempted: bool = Field(
        default=False, description="Did the agent actually do it?"
    )

    evidence: list[EvidenceItem] = Field(default_factory=list)

    @property
    def has_evidence(self) -> bool:
        """Whether at least one evidence item carries a real quote."""
        return any(item.quote.strip() for item in self.evidence)

    @property
    def is_scored(self) -> bool:
        """Whether this KPI produced a usable number."""
        return self.score is not None and self.applicable


class RiskSummary(BaseModel):
    """Risk flags raised for a call.

    Not part of the wireframe's Flow A, but the originating service stores
    these as indexed columns that dashboards filter on. Parsed when the model
    returns them, omitted otherwise -- never required.
    """

    high_risk_call: bool = False
    manual_review_required: bool = False
    compliance_violation: bool = False
    privacy_violation: bool = False
    mis_selling_alert: bool = False
    risk_reason: dict[str, str | None] = Field(default_factory=dict)
    risk_evidence: dict[str, list[EvidenceItem]] = Field(default_factory=dict)

    @property
    def any_flag_raised(self) -> bool:
        return any(
            (
                self.high_risk_call,
                self.manual_review_required,
                self.compliance_violation,
                self.privacy_violation,
                self.mis_selling_alert,
            )
        )


class CallImpact(BaseModel):
    """How much this call mattered, as judged by the model.

    ``level`` is constrained to High/Low because the originating schema carries
    a check constraint to that effect -- a third value would be rejected at
    write time with an opaque database error.
    """

    level: str | None = Field(
        default=None, description='Either "High" or "Low", or None if not judged.'
    )
    reason: str | None = None

    @field_validator("level")
    @classmethod
    def _only_high_or_low(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalised = value.strip().capitalize()
        return normalised if normalised in {"High", "Low"} else None


class ObjectiveRollup(BaseModel):
    """How one objective fared, matching the wireframe's grouping.

    The UI shows "Compliance -- 8 of 22", so counts lead and the average is
    secondary.
    """

    objective: str
    total: int = Field(description="KPIs evaluated for this objective.")
    scored: int = Field(description="How many produced a usable score.")
    not_applicable: int = 0
    unevidenced: int = Field(
        default=0, description="Scored but with no supporting quote."
    )
    average: float | None = Field(
        default=None, description="Mean of the scored KPIs. None when none scored."
    )


class AnalysisResult(BaseModel):
    """The outcome of scoring one call.

    **There is no overall score.** The wireframe presents KPIs grouped by
    objective and never shows a single headline number, and a shared library is
    the wrong place to fix a weighting policy that is still being decided. A
    caller that needs one computes it from ``kpis`` or ``by_objective``.
    """

    kpis: list[KPIScore] = Field(default_factory=list)
    by_objective: list[ObjectiveRollup] = Field(default_factory=list)
    risk: RiskSummary | None = None
    call_impact: CallImpact | None = Field(
        default=None, description="High/Low impact judgement, when the model gives one."
    )
    customer_experience_drivers: list[str] = Field(
        default_factory=list,
        description="What drove the customer's experience on this call.",
    )
    unevidenced_kpis: list[str] = Field(
        default_factory=list,
        description=(
            "Codes of KPIs that returned a score with no supporting quote. "
            "The specification treats these as failures, not zeros."
        ),
    )
    processing_time_ms: float = 0.0

    @property
    def is_empty(self) -> bool:
        """True when nothing was scored."""
        return not self.kpis

    def objective(self, name: str) -> ObjectiveRollup | None:
        """The rollup for one objective, or ``None`` if it was not scored."""
        return next((row for row in self.by_objective if row.objective == name), None)
