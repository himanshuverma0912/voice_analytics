"""The prompt catalogue must load from the package, not the working directory."""

from __future__ import annotations

import os

import pytest

from voice_analytics.exceptions import PromptNotFoundError
from voice_analytics.prompts import PromptManager, default_prompt_manager


def test_catalogue_loads_from_package_data():
    manager = default_prompt_manager()
    assert "banking" in manager.domains()
    assert "transcription" in manager.tasks("banking")


def test_catalogue_loads_regardless_of_working_directory(tmp_path, monkeypatch):
    """The original implementation only worked from the repository root."""
    monkeypatch.chdir(tmp_path)
    assert os.getcwd() == str(tmp_path)
    assert PromptManager.from_package().get(domain="banking", task="transcription")


def test_transcription_prompt_omits_translation_rule_without_a_language():
    prompt = default_prompt_manager().get(
        domain="banking", task="transcription", target_lang=None, romanize=False
    )
    assert "TRANSLATION RULE" not in prompt
    assert "ROMANIZATION RULE" not in prompt


def test_transcription_prompt_includes_translation_rule_with_a_language():
    prompt = default_prompt_manager().get(
        domain="banking", task="transcription", target_lang="Hindi", romanize=False
    )
    assert "TRANSLATION RULE" in prompt
    assert "Hindi" in prompt


def test_transcription_prompt_includes_romanization_rule_when_requested():
    prompt = default_prompt_manager().get(
        domain="banking", task="transcription", target_lang=None, romanize=True
    )
    assert "ROMANIZATION RULE" in prompt


def test_unknown_task_raises_rather_than_returning_an_empty_prompt():
    with pytest.raises(PromptNotFoundError):
        default_prompt_manager().get(domain="banking", task="does-not-exist")


def test_unknown_domain_raises():
    with pytest.raises(PromptNotFoundError):
        default_prompt_manager().get(domain="insurance", task="transcription")


def test_from_file_rejects_a_missing_path(tmp_path):
    with pytest.raises(PromptNotFoundError):
        PromptManager.from_file(tmp_path / "absent.yaml")


def test_from_file_loads_a_caller_supplied_catalogue(tmp_path):
    path = tmp_path / "custom.yaml"
    path.write_text("retail:\n  greeting: |\n    Hello {{ name }}\n", encoding="utf-8")
    manager = PromptManager.from_file(path)
    assert manager.get(domain="retail", task="greeting", name="Priya") == "Hello Priya"


# ---------------------------------------------------------------------------
# The local stub must recognise a hand-written scoring prompt
# ---------------------------------------------------------------------------
# The real prompt comes from PromptHub via the template, so its spelling is
# fixed. A prompt written by hand for local testing is not, and a stub that
# only matched one spelling answered with a transcript instead -- surfacing
# three steps later as "the model returned no KPI scores", which points at the
# model rather than at a missing space.

import importlib.util                                    # noqa: E402
from pathlib import Path                                  # noqa: E402

import pytest                                             # noqa: E402


def _fake_gateway():
    path = Path(__file__).resolve().parents[2] / "scripts" / "fake_gateway.py"
    spec = importlib.util.spec_from_file_location("fake_gateway", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "prompt",
    [
        "KPI Code : rpc_verified",          # what the real template emits
        "KPI CODE : rpc_verified",
        "KPI CODE:rpc_verified",            # no spaces -- a plausible hand edit
        "kpi code: rpc_verified",
        "kpi_code : rpc_verified",
        "KPI  CODE  :  rpc_verified",
    ],
)
def test_the_stub_recognises_every_plausible_spelling(prompt):
    assert _fake_gateway()._KPI_MARKER.search(prompt), prompt


@pytest.mark.parametrize(
    "prompt",
    [
        "Transcribe this audio faithfully.",
        "Summarise the call in three bullet points.",
        "Some prose that mentions a kpi codebase in passing.",
    ],
)
def test_the_stub_does_not_mistake_other_prompts_for_scoring(prompt):
    assert not _fake_gateway()._KPI_MARKER.search(prompt), prompt


def test_the_real_template_still_matches_the_stub():
    """If the template's wording changes, the stub must be updated with it."""
    from voice_analytics.analysis import KPIPrompt, build_consolidated_prompt

    prompt = build_consolidated_prompt(
        "BASE", [KPIPrompt("rpc_verified", "RPC Verification", "Did they?")])
    assert _fake_gateway()._KPI_MARKER.search(prompt)


# ---------------------------------------------------------------------------
# The default use case fallback
# ---------------------------------------------------------------------------
# A use case may define only KPI prompts and rely on a default for the shared
# rules. The originating service supported that; without it, those use cases
# cannot be scored at all.

from voice_analytics.exceptions import PromptHubError            # noqa: E402
from voice_analytics.prompthub import (                          # noqa: E402
    resolve_base_prompt,
    resolve_kpi_prompts,
)


def _entry(name, value, version=1, status="APPROVED"):
    return {"name": name, "value": value, "version": version, "status": status}


def test_the_base_prompt_comes_from_the_use_case_when_it_has_one():
    own = [_entry("base_prompt", "OWN RULES")]
    default = [_entry("base_prompt", "DEFAULT RULES")]
    assert resolve_base_prompt(own, default) == "OWN RULES"


def test_the_base_prompt_falls_back_to_the_default_use_case():
    default = [_entry("base_prompt", "DEFAULT RULES")]
    assert resolve_base_prompt([], default) == "DEFAULT RULES"


def test_a_missing_base_prompt_everywhere_is_fatal():
    """Without it the model has no output format, so it would return plausible
    nonsense rather than fail."""
    with pytest.raises(PromptHubError, match="default use case"):
        resolve_base_prompt([], [_entry("something_else", "x")])

    with pytest.raises(PromptHubError):
        resolve_base_prompt([])


def test_the_highest_approved_version_wins():
    prompts = [
        _entry("base_prompt", "v1", version=1),
        _entry("base_prompt", "v3", version=3),
        _entry("base_prompt", "v2", version=2),
    ]
    assert resolve_base_prompt(prompts) == "v3"


def test_a_version_in_review_does_not_displace_the_approved_one():
    prompts = [
        _entry("base_prompt", "approved", version=1),
        _entry("base_prompt", "in review", version=2, status="PENDING"),
    ]
    assert resolve_base_prompt(prompts) == "approved"


def test_kpis_resolve_from_the_use_case_first_then_the_default():
    own = [_entry("empathy", "OWN empathy")]
    default = [_entry("empathy", "DEFAULT empathy"), _entry("professionalism", "DEFAULT prof")]

    resolved, skipped = resolve_kpi_prompts(
        own, ["empathy", "professionalism"], default)

    assert [(k.kpi_code, k.instructions) for k in resolved] == [
        ("empathy", "OWN empathy"),
        ("professionalism", "DEFAULT prof"),
    ]
    assert skipped == []


def test_a_kpi_in_neither_source_is_skipped_and_reported():
    """Skipped, not fatal -- matching the originating service. But reported,
    which it did not do, so two batches are not silently incomparable."""
    resolved, skipped = resolve_kpi_prompts(
        [_entry("empathy", "x")], ["empathy", "nowhere"], [_entry("other", "y")])

    assert [k.kpi_code for k in resolved] == ["empathy"]
    assert skipped == ["nowhere"]


def test_without_a_default_configured_nothing_changes():
    resolved, skipped = resolve_kpi_prompts([_entry("empathy", "x")],
                                            ["empathy", "nowhere"])
    assert [k.kpi_code for k in resolved] == ["empathy"]
    assert skipped == ["nowhere"]


def test_the_default_key_is_optional_and_off_by_default():
    from voice_analytics.config import Settings

    settings = Settings(LLM_BASE_URL="https://llm.internal/v1")
    assert settings.PROMPTHUB_DEFAULT_LITELLM_KEY == ""


# ---------------------------------------------------------------------------
# Prompt assembly must match the originating service byte for byte
# ---------------------------------------------------------------------------
# The assembled prompt IS the input to the model. A stray newline or a
# reordered block is a different prompt, and the difference shows up as a
# quiet change in scores rather than as a failure. So the original is
# reproduced verbatim and both are rendered over the same input.
#
# Source: prompt_resolver.build_consolidated_prompt, lines 206-228.

import hashlib                                                  # noqa: E402
from jinja2 import DictLoader, Environment, select_autoescape    # noqa: E402

from voice_analytics.analysis import build_consolidated_prompt   # noqa: E402

#: The template both implementations render. Identical in both repositories --
#: src/templates/scalable_prompt_template.j2 there, packaged as
#: analysis/data/kpi_prompt_template.j2 here.
_TEMPLATE = """{{ input_data }}

{% for kpi in kpis %}

=====================================================
KPI : {{ kpi.title }}
KPI Code : {{ kpi.data.kpi_code }}
=====================================================

{{ kpi.instructions }}

{% endfor %}"""


def _original(base_prompt: str, kpi_sections: list[dict]) -> str:
    """The originating implementation, copied. Do not tidy it."""
    env = Environment(
        loader=DictLoader({"t.j2": _TEMPLATE}),
        autoescape=select_autoescape(disabled_extensions=("j2",)),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    return env.get_template("t.j2").render(
        input_data=base_prompt,
        kpis=[
            {
                "enabled": True,
                "title": section["kpi_name"],
                "instructions": section["prompt"],
                "data": {"kpi_code": section["kpi_code"]},
            }
            for section in kpi_sections
        ],
    )


def test_the_packaged_template_matches_the_originating_one():
    from importlib import resources

    packaged = (
        resources.files("voice_analytics.analysis.data")
        .joinpath("kpi_prompt_template.j2")
        .read_text(encoding="utf-8")
    )
    assert packaged == _TEMPLATE


@pytest.mark.parametrize(
    "base, sections",
    [
        ("BASE RULES", [{"kpi_code": "a", "kpi_name": "A", "prompt": "Do A."}]),
        ("BASE", [{"kpi_code": "a", "kpi_name": "A", "prompt": "Do A."},
                  {"kpi_code": "b", "kpi_name": "B", "prompt": "Do B."}]),
        # Non-Latin text, because autoescape being wrong would mangle it.
        ("आधार नियम", [{"kpi_code": "hi", "kpi_name": "हिंदी",
                        "prompt": "क्या एजेंट ने पुष्टि की?"}]),
        # Characters an HTML-escaping template would corrupt.
        ("BASE & <rules>", [{"kpi_code": "x", "kpi_name": "A & B",
                             "prompt": 'Score "high" if <5 & >1.'}]),
        # The real codes, two of which carry a slash and a hyphen.
        ("BASE", [{"kpi_code": "cross-sell/upsell", "kpi_name": "Cross-sell",
                   "prompt": "Was an upsell attempted?"},
                  {"kpi_code": "follow-up_requests", "kpi_name": "Follow-up",
                   "prompt": "Were follow-ups agreed?"}]),
    ],
    ids=["one-kpi", "two-kpis", "devanagari", "html-characters", "awkward-codes"],
)
def test_assembly_is_byte_for_byte_identical(base, sections):
    from voice_analytics.analysis import KPIPrompt

    mine = build_consolidated_prompt(
        base, [KPIPrompt(s["kpi_code"], s["kpi_name"], s["prompt"]) for s in sections])
    assert mine == _original(base, sections)


def test_the_real_prompt_set_renders_identically():
    """All 20 KPIs from the originating service's committed prompt file."""
    import json
    from pathlib import Path

    from voice_analytics.analysis import KPIPrompt

    snapshot = json.loads(
        (Path(__file__).resolve().parents[2]
         / "examples" / "prompts" / "analytics_prompts.json").read_text())

    sections = [
        {"kpi_code": k["kpi_code"], "kpi_name": k["kpi_name"], "prompt": k["prompt"]}
        for s in snapshot["sections"] for k in s["kpis"]
    ]
    mine = build_consolidated_prompt(
        snapshot["base_prompt"],
        [KPIPrompt(s["kpi_code"], s["kpi_name"], s["prompt"]) for s in sections])
    theirs = _original(snapshot["base_prompt"], sections)

    assert len(sections) == 20
    assert hashlib.sha256(mine.encode()).hexdigest() == \
           hashlib.sha256(theirs.encode()).hexdigest()


def test_the_new_guards_refuse_what_the_original_rendered_as_nonsense():
    """The original had no validation: an empty KPI list rendered a prompt with
    no KPIs in it, and the model was asked to score against nothing."""
    from voice_analytics.analysis import KPIPrompt

    assert _original("BASE", []).strip() == "BASE"      # the original's behaviour

    with pytest.raises(ValueError, match="At least one KPI"):
        build_consolidated_prompt("BASE", [])
    with pytest.raises(ValueError, match="base_prompt is required"):
        build_consolidated_prompt("   ", [KPIPrompt("a", "A", "Do A.")])


# ---------------------------------------------------------------------------
# Prompt version resolution must match the originating service
# ---------------------------------------------------------------------------
# Which version of a prompt gets used decides what the model is asked. Picking
# a version in review, or the wrong approved one, changes scores with nothing
# in the data to show why. So the original is reproduced verbatim and both are
# run over the cases that separate a faithful port from an approximate one.
#
# Source: prompt_resolver._resolve_prompt_entry, lines 66-90.

from typing import Optional                                  # noqa: E402

from voice_analytics.prompthub import latest_approved         # noqa: E402


def _original_resolve(prompts, name) -> tuple[Optional[dict], str]:
    """`_resolve_prompt_entry`, copied. Do not tidy it -- its value is being
    identical, including the branch that does nothing."""
    def _latest_approved_version(ps, n):
        approved = [p for p in ps if p.get("name") == n and p.get("status") == "APPROVED"]
        return max(approved, key=lambda p: p.get("version", 0)) if approved else None

    matches = [p for p in prompts if p.get("name") == name]
    if not matches:
        return None, "unresolved"

    has_pending = any(p.get("status") == "PENDING" for p in matches)
    approved = _latest_approved_version(prompts, name)

    if has_pending:
        if approved:
            return approved, "approved"
        return None, "unresolved"

    if approved:
        return approved, "approved"

    return None, "unresolved"


def _v(name, version, status, value):
    return {"name": name, "version": version, "status": status, "value": value}


RESOLUTION_CASES = [
    pytest.param([_v("x", 1, "APPROVED", "v1"),
                  _v("x", 3, "APPROVED", "v3"),
                  _v("x", 2, "APPROVED", "v2")], id="highest-approved-wins"),
    pytest.param([_v("x", 1, "APPROVED", "v1"),
                  _v("x", 9, "PENDING", "review")], id="pending-above-approved"),
    pytest.param([_v("x", 1, "PENDING", "review")], id="only-pending"),
    pytest.param([_v("x", 1, "REJECTED", "no")], id="only-rejected"),
    pytest.param([_v("y", 1, "APPROVED", "other")], id="no-match"),
    pytest.param([{"name": "x", "status": "APPROVED", "value": "nover"},
                  _v("x", 1, "APPROVED", "v1")], id="version-field-missing"),
    pytest.param([_v("x", 5, "REJECTED", "no"),
                  _v("x", 2, "APPROVED", "v2"),
                  _v("x", 7, "PENDING", "review")], id="approved-rejected-pending"),
    pytest.param([], id="empty-list"),
    pytest.param([_v("x", 2, "APPROVED", "first"),
                  _v("x", 2, "APPROVED", "second")], id="duplicate-versions"),
]


@pytest.mark.parametrize("prompts", RESOLUTION_CASES)
def test_version_resolution_matches_the_originating_service(prompts):
    original, _ = _original_resolve(prompts, "x")
    library = latest_approved(prompts, "x")

    assert (original or {}).get("value") == (library or {}).get("value")


def test_a_version_in_review_is_ignored_however_new_it_is():
    """The last approved wording keeps being used until someone approves the
    new one. Both implementations agree, and this is the case that matters."""
    prompts = [_v("x", 1, "APPROVED", "approved"),
               _v("x", 99, "PENDING", "not approved yet")]

    assert latest_approved(prompts, "x")["value"] == "approved"
    assert _original_resolve(prompts, "x")[0]["value"] == "approved"
