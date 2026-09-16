"""KPI scoring.

Two behaviours from the specification are tested as contracts rather than
implementation details:

* KPIs are grouped by objective, with counts leading.
* "A score without evidence is a failed KPI, not a zero."
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from openai import AuthenticationError, RateLimitError

from voice_analytics.analysis import (
    KPIPrompt,
    analyse,
    build_consolidated_prompt,
)
from voice_analytics.analysis.scoring import (
    _coerce_score,
    apply_evidence_rule,
    parse_evidence,
    parse_kpis,
    parse_risk,
    roll_up_by_objective,
)
from voice_analytics.exceptions import (
    AnalysisError,
    LLMAuthenticationError,
    LLMRateLimitError,
)

MODEL = "qwen3-30b-a3b-instruct"
TRANSCRIPT = "[00:02] [Agent]: Namaste. [00:15] [Customer]: Haan ji."
PROMPT = "BASE RULES\n\n=== KPI : X ==="


def _kpi(code, section="Compliance", score=8.0, evidence=True, **extra):
    entry = {
        "section": section,
        "kpi_code": code,
        "kpi_name": code.replace("_", " ").title(),
        "score": score,
        "rationale": "Because.",
        "applicable": True,
        "observable": True,
        "attempted": True,
        "evidence": (
            [{"turn_index": 1, "speaker": "Agent", "quote": "Namaste."}]
            if evidence
            else []
        ),
    }
    entry.update(extra)
    return entry


def _reply(payload) -> SimpleNamespace:
    content = payload if isinstance(payload, str) else json.dumps(payload)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
    )


def _client(payload) -> AsyncMock:
    client = AsyncMock()
    client.chat.completions.create = AsyncMock(return_value=_reply(payload))
    return client


def _client_raising(exc) -> AsyncMock:
    client = AsyncMock()
    client.chat.completions.create = AsyncMock(side_effect=exc)
    return client


def _rate_limit() -> RateLimitError:
    request = httpx.Request("POST", "https://llm.example/v1/chat/completions")
    return RateLimitError(
        "slow down", response=httpx.Response(429, request=request), body=None
    )


def _auth_error() -> AuthenticationError:
    request = httpx.Request("POST", "https://llm.example/v1/chat/completions")
    return AuthenticationError(
        "bad key", response=httpx.Response(401, request=request), body=None
    )


# ---------------------------------------------------------------------------
# Prompt assembly
# ---------------------------------------------------------------------------


def test_prompt_contains_the_base_then_one_block_per_kpi():
    prompt = build_consolidated_prompt(
        "BASE RULES",
        [
            KPIPrompt("rpc_verified", "RPC Verification", "Did they verify identity?"),
            KPIPrompt("disclosure", "Disclosure", "Was the rate disclosed?"),
        ],
    )

    assert prompt.index("BASE RULES") < prompt.index("rpc_verified")
    assert "KPI Code : rpc_verified" in prompt
    assert "KPI Code : disclosure" in prompt
    assert "Did they verify identity?" in prompt


def test_prompt_falls_back_to_the_code_when_no_name_is_given():
    prompt = build_consolidated_prompt("BASE", [KPIPrompt("only_code", "", "Do it.")])
    assert "KPI : only_code" in prompt


def test_prompt_template_loads_from_any_working_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert build_consolidated_prompt("BASE", [KPIPrompt("c", "N", "I")])


def test_blank_base_prompt_is_rejected():
    with pytest.raises(ValueError, match="base_prompt is required"):
        build_consolidated_prompt("   ", [KPIPrompt("c", "N", "I")])


def test_scoring_with_no_kpis_is_rejected():
    """An empty result would look like a successful run."""
    with pytest.raises(ValueError, match="At least one KPI is required"):
        build_consolidated_prompt("BASE", [])


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (8, 8.0), (8.5, 8.5), ("7", 7.0),
        (True, 1.0), (False, 0.0),
        ("Yes", 1.0), ("no", 0.0), ("PASS", 1.0), ("fail", 0.0),
        (None, None), ("not a number", None),
    ],
)
def test_score_coercion_handles_binary_and_numeric_kpis(value, expected):
    """The interface exposes Binary KPIs alongside numeric ones."""
    assert _coerce_score(value) == expected


def test_kpi_field_aliases_are_accepted():
    """Models use 'reason' and 'raw_value' as often as the canonical names."""
    kpis = parse_kpis([{"code": "x", "objective": "Compliance",
                        "reason": "why", "raw_value": "Yes", "score": 9}])
    assert kpis[0].kpi_code == "x"
    assert kpis[0].section == "Compliance"
    assert kpis[0].rationale == "why"
    assert kpis[0].raw_score == "Yes"


def test_kpis_without_a_code_are_dropped():
    """A score that cannot be matched to a definition is not usable."""
    assert parse_kpis([_kpi("keep"), {"score": 5, "rationale": "no code"}]) != []
    assert len(parse_kpis([_kpi("keep"), {"score": 5}])) == 1


def test_missing_section_becomes_uncategorised():
    kpis = parse_kpis([{"kpi_code": "x", "score": 5}])
    assert kpis[0].section == "Uncategorised"


def test_parse_kpis_accepts_the_wrapped_shape():
    assert len(parse_kpis({"kpis": [_kpi("a"), _kpi("b")]})) == 2


@pytest.mark.parametrize("raw", [None, "text", 42, {"other": 1}])
def test_parse_kpis_survives_unexpected_shapes(raw):
    assert parse_kpis(raw) == []


def test_evidence_accepts_objects_and_bare_quotes():
    items = parse_evidence([
        {"turn_index": 2, "speaker": "Agent", "quote": "Hello."},
        "a bare quote",
        12345,                       # skipped
    ])
    assert len(items) == 2
    assert items[1].quote == "a bare quote"


def test_evidence_is_capped():
    assert len(parse_evidence([{"quote": f"q{i}"} for i in range(50)])) == 20


def test_risk_is_optional():
    """Risk flags are not part of the Flow A specification."""
    assert parse_risk(None) is None
    assert parse_risk("nonsense") is None


def test_risk_is_parsed_when_present():
    risk = parse_risk({
        "high_risk_call": True, "compliance_violation": True,
        "risk_reason": {"compliance_violation": "Identity not verified"},
        "risk_evidence": {"compliance_violation": [{"quote": "..."}]},
    })
    assert risk.any_flag_raised
    assert risk.risk_reason["compliance_violation"] == "Identity not verified"
    assert len(risk.risk_evidence["compliance_violation"]) == 1


# ---------------------------------------------------------------------------
# The evidence rule
# ---------------------------------------------------------------------------


def test_a_score_without_evidence_is_discarded_not_zeroed():
    """The rule: a score without evidence is a failed KPI, not a zero."""
    kpis = parse_kpis([_kpi("no_proof", score=9.0, evidence=False)])

    unevidenced = apply_evidence_rule(kpis)

    assert unevidenced == ["no_proof"]
    assert kpis[0].score is None          # discarded, NOT set to 0
    assert not kpis[0].is_scored


def test_evidence_rule_leaves_evidenced_scores_alone():
    kpis = parse_kpis([_kpi("proven", score=9.0, evidence=True)])
    assert apply_evidence_rule(kpis) == []
    assert kpis[0].score == 9.0


def test_blank_quotes_do_not_count_as_evidence():
    kpis = parse_kpis([_kpi("blank", evidence=False)])
    kpis[0].evidence = parse_evidence([{"quote": "   "}])
    assert apply_evidence_rule(kpis) == ["blank"]


def test_not_applicable_kpis_are_exempt_from_the_evidence_rule():
    """There is nothing to evidence when the KPI did not apply."""
    kpis = parse_kpis([_kpi("na", score=None, evidence=False, applicable=False)])
    assert apply_evidence_rule(kpis) == []


# ---------------------------------------------------------------------------
# Grouping by objective
# ---------------------------------------------------------------------------


def test_rollup_groups_by_objective_in_first_seen_order():
    kpis = parse_kpis([
        _kpi("a", section="Compliance", score=8),
        _kpi("b", section="Empathy", score=6),
        _kpi("c", section="Compliance", score=10),
    ])
    rollups = roll_up_by_objective(kpis, [])

    assert [r.objective for r in rollups] == ["Compliance", "Empathy"]
    assert rollups[0].total == 2
    assert rollups[0].scored == 2
    assert rollups[0].average == 9.0


def test_rollup_counts_not_applicable_and_unevidenced_separately():
    kpis = parse_kpis([
        _kpi("scored", score=8),
        _kpi("na", score=None, applicable=False, evidence=False),
        _kpi("noproof", score=7, evidence=False),
    ])
    unevidenced = apply_evidence_rule(kpis)
    rollup = roll_up_by_objective(kpis, unevidenced)[0]

    assert rollup.total == 3
    assert rollup.scored == 1              # only the evidenced one counts
    assert rollup.not_applicable == 1
    assert rollup.unevidenced == 1
    assert rollup.average == 8.0           # unevidenced score excluded


def test_objective_with_nothing_scored_has_no_average():
    kpis = parse_kpis([_kpi("na", score=None, applicable=False)])
    assert roll_up_by_objective(kpis, [])[0].average is None


# ---------------------------------------------------------------------------
# analyse()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_returns_scores_grouped_by_objective():
    client = _client({"kpis": [
        _kpi("disclosure", section="Compliance", score=9),
        _kpi("tone", section="Empathy and tone", score=7),
    ]})

    result = await analyse(client, TRANSCRIPT, PROMPT, MODEL)

    assert len(result.kpis) == 2
    assert [r.objective for r in result.by_objective] == [
        "Compliance", "Empathy and tone"
    ]
    assert result.objective("Compliance").average == 9.0


@pytest.mark.asyncio
async def test_there_is_no_overall_score():
    """The interface groups by objective and never shows a single number."""
    client = _client({"kpis": [_kpi("a"), _kpi("b")]})
    result = await analyse(client, TRANSCRIPT, PROMPT, MODEL)

    assert not hasattr(result, "overall_score")
    assert not hasattr(result, "overall")


@pytest.mark.asyncio
async def test_model_name_is_forwarded_not_hardcoded():
    """The originating service hardcoded the model, ignoring its own argument."""
    client = _client({"kpis": [_kpi("a")]})
    await analyse(client, TRANSCRIPT, PROMPT, "some-other-model")

    assert client.chat.completions.create.await_args.kwargs["model"] == "some-other-model"


@pytest.mark.asyncio
async def test_agent_name_reaches_the_prompt_when_known():
    client = _client({"kpis": [_kpi("a")]})
    await analyse(client, TRANSCRIPT, PROMPT, MODEL, agent_name="Rahul Sharma")

    user = client.chat.completions.create.await_args.kwargs["messages"][1]["content"]
    assert "Rahul Sharma" in user


@pytest.mark.asyncio
async def test_unevidenced_kpis_are_reported_on_the_result():
    client = _client({"kpis": [
        _kpi("good", score=8, evidence=True),
        _kpi("bad", score=9, evidence=False),
    ]})

    result = await analyse(client, TRANSCRIPT, PROMPT, MODEL)

    assert result.unevidenced_kpis == ["bad"]
    assert result.objective("Compliance").scored == 1


@pytest.mark.asyncio
async def test_no_scores_at_all_is_an_error_not_an_empty_success():
    """An empty result would record the call as scored when nothing was judged."""
    client = _client({"kpis": []})

    with pytest.raises(AnalysisError, match="no KPI scores"):
        await analyse(client, TRANSCRIPT, PROMPT, MODEL)


@pytest.mark.asyncio
async def test_unparseable_response_raises_analysis_error():
    client = _client("I cannot help with that.")
    with pytest.raises(AnalysisError, match="Could not parse"):
        await analyse(client, TRANSCRIPT, PROMPT, MODEL)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [("transcript", "  "), ("consolidated_prompt", ""), ("model_name", "")],
)
async def test_required_arguments_are_validated(field, value):
    kwargs = {
        "client": _client({"kpis": [_kpi("a")]}),
        "transcript": TRANSCRIPT,
        "consolidated_prompt": PROMPT,
        "model_name": MODEL,
        field: value,
    }
    with pytest.raises(ValueError, match=f"{field} is required"):
        await analyse(**kwargs)


@pytest.mark.asyncio
async def test_authentication_failure_is_not_retried(monkeypatch):
    slept: list[int] = []
    monkeypatch.setattr(
        "voice_analytics.analysis.service.asyncio.sleep",
        AsyncMock(side_effect=lambda d: slept.append(d)),
    )
    with pytest.raises(LLMAuthenticationError):
        await analyse(_client_raising(_auth_error()), TRANSCRIPT, PROMPT, MODEL)
    assert slept == []


@pytest.mark.asyncio
async def test_rate_limit_retries_with_backoff(monkeypatch):
    slept: list[int] = []
    monkeypatch.setattr(
        "voice_analytics.analysis.service.asyncio.sleep",
        AsyncMock(side_effect=lambda d: slept.append(d)),
    )
    with pytest.raises(LLMRateLimitError):
        await analyse(_client_raising(_rate_limit()), TRANSCRIPT, PROMPT, MODEL)
    assert slept == [5, 10]


# ---------------------------------------------------------------------------
# call_impact and customer_experience_drivers
# ---------------------------------------------------------------------------


def test_call_impact_accepts_both_key_spellings():
    from voice_analytics.analysis.scoring import parse_call_impact

    a = parse_call_impact({"impact_level": "High", "reason": "Disputed amount"})
    b = parse_call_impact({"level": "High", "impact_reason": "Disputed amount"})
    assert a.level == b.level == "High"
    assert a.reason == b.reason == "Disputed amount"


@pytest.mark.parametrize("value", ["high", "HIGH", " Low "])
def test_call_impact_level_is_normalised(value):
    from voice_analytics.analysis.scoring import parse_call_impact

    assert parse_call_impact({"level": value}).level in {"High", "Low"}


def test_call_impact_rejects_values_the_schema_forbids():
    """The originating schema constrains this to High/Low with a check."""
    from voice_analytics.analysis.scoring import parse_call_impact

    assert parse_call_impact({"level": "Medium", "reason": "x"}).level is None


@pytest.mark.parametrize("raw", [None, "text", {}, {"level": None, "reason": None}])
def test_call_impact_is_optional(raw):
    from voice_analytics.analysis.scoring import parse_call_impact

    assert parse_call_impact(raw) is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (["wait time", "tone"], ["wait time", "tone"]),
        ([{"driver": "wait time"}, {"name": "tone"}], ["wait time", "tone"]),
        ("single driver", ["single driver"]),
        ([{"unknown": "x"}, "", 42], []),
        (None, []),
    ],
)
def test_experience_drivers_accept_the_shapes_models_return(raw, expected):
    from voice_analytics.analysis.scoring import parse_experience_drivers

    assert parse_experience_drivers(raw) == expected


@pytest.mark.asyncio
async def test_analyse_surfaces_impact_and_drivers():
    client = _client({
        "kpis": [_kpi("a")],
        "call_impact": {"impact_level": "High", "reason": "Customer disputed."},
        "customer_experience_drivers": ["long hold", "unclear explanation"],
    })

    result = await analyse(client, TRANSCRIPT, PROMPT, MODEL)

    assert result.call_impact.level == "High"
    assert result.call_impact.reason == "Customer disputed."
    assert result.customer_experience_drivers == ["long hold", "unclear explanation"]


@pytest.mark.asyncio
async def test_impact_and_drivers_are_absent_when_not_returned():
    result = await analyse(_client({"kpis": [_kpi("a")]}), TRANSCRIPT, PROMPT, MODEL)
    assert result.call_impact is None
    assert result.customer_experience_drivers == []
