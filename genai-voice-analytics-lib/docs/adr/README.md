# Architecture decision records

Short records of decisions that shape this library, why they were made, and
what they cost. Written so a reviewer or a new joiner can understand the design
without reconstructing it from the code.

| # | Decision | Status |
|---|---|---|
| [0001](0001-pure-compute-no-persistence.md) | The library is pure compute; callers own persistence | Accepted |
| [0002](0002-configuration-is-injected.md) | Configuration is injected, never read at import | Accepted |
| [0003](0003-exit-codes-are-the-orchestration-contract.md) | Exit codes are the orchestration contract | Accepted |
| [0004](0004-tls-uses-an-explicit-ca-bundle.md) | TLS verification uses an explicit CA bundle | Accepted |

New decisions get the next number. Records are appended, not rewritten: a
superseded decision is marked `Superseded by NNNN` and left in place, because
the reasoning behind a change is usually more useful than the change itself.
