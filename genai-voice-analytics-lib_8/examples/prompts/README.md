# A snapshot of the KPI prompts, for local testing

`analytics_prompts.json` is copied from `src/resources/analytics_prompts.json`
in the originating service: one `base_prompt` plus 20 KPIs across 4 sections.

## Why it is here and not in the library

KPI prompts are **configuration, not code**. They are authored, reviewed and
approved in PromptHub, versioned there, and resolved per use case at run time.
The library takes an assembled prompt and never reaches for one -- see
`docs/adr/0001` and `voice_analytics.prompthub`.

Shipping this file inside the package would create a second source of truth
that looks authoritative and goes stale silently. A batch scored against a
baked-in copy would differ from one scored against PromptHub, with nothing in
the data to show why.

So it lives under `examples/`, outside the wheel, for exactly one purpose:
**assembling a realistic prompt on a laptop that cannot reach PromptHub.**

## Using it

```bash
python -m voice_analytics build-prompt \
    --from-file examples/prompts/analytics_prompts.json \
    --kpi-codes overall_compliance_score,empathy,customer_sentiment \
    --output kpis.txt
```

Omit `--kpi-codes` to include all 20.

The same `build_consolidated_prompt` runs either way, so the assembled prompt
is byte-identical in structure to one built from PromptHub. Only the *content*
is a snapshot.

## What is in it

| Section | KPIs |
| --- | --- |
| Compliance and Risk | `overall_compliance_score`, `authentication_compliance`, `mandatory_disclosure_compliance`, `data_privacy_and_security_compliance`, `regulatory_risk` |
| Agent Communication | `communication_quality`, `professionalism`, `empathy`, `active_listening`, `customer_sentiment` |
| Customer Experience | `customer_experience_score`, `customer_effort`, `frustration_handling`, `escalation_risk` |
| Business Outcome | `business_outcome_score`, `opportunity_handling`, `follow-up_requests`, `sales_conversion`, `cross-sell/upsell`, `resolution_quality` |

## Two things to know before trusting it

**The scoring scale contradicts itself.** The evaluation instructions say
"Assign a score from 0-10 using the scoring scale below", and the scale below
it defines only 0, 1, 2 and 3. The model is told two different scales in one
prompt. Worth resolving in PromptHub rather than patching here.

**It asks for less than the code reads.** Its `OUTPUT FORMAT` block contains
only `kpis`. `voice_analytics.analysis` also parses `risk_summary`,
`call_impact` and `customer_experience_drivers`, and the database has columns
for all three -- so the live PromptHub version must ask for more than this
snapshot does. Scores built from this file will leave those columns empty.

It also asks for `confidence`, which the originating ORM does not map, so that
value is returned by the model and dropped on the way to the database.
