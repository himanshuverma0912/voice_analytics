# ADR 0002 — Configuration is injected, never read at import

**Status:** Accepted
**Date:** 2026-09-10

## Context

The originating service ends `src/config.py` with:

```python
settings = Settings()          # runs at import; reads .env from the CWD
```

Every module then does `from src.config import settings`. That is a reasonable
pattern for an application with a single entry point, but it is unusable in a
library:

- Importing any module reads a `.env` file from the *caller's* working
  directory, which the library has no business inspecting.
- A missing required field raises `ValidationError` at import time, so
  `import voice_analytics` could fail before the caller has configured
  anything. The service's `DB_HOST` is required, so a transcription-only
  consumer would have had to invent a database host.
- Tests cannot vary configuration without reaching into a global.

## Decision

`Settings` is a class. The library never instantiates it.

```python
class Settings(BaseSettings): ...              # no module-level instance

@lru_cache(maxsize=8)
def load_settings(env_file: str | None = ".env") -> Settings: ...
```

`load_settings()` is called **once, explicitly, at the edge** — in a CLI
command or an application's startup — and the resulting object is passed down.
Functions that need configuration take it as a parameter.

## Consequences

**Positive**

- `import voice_analytics` has no side effects and cannot fail on configuration.
  Verified by a test that imports from an unrelated directory with no `.env`.
- Tests construct `Settings(_env_file=None, LLM_BASE_URL=...)` inline, so each
  test controls its own configuration with no global state to reset.
- One process can hold several configurations — different gateways or
  credentials — which a singleton forbids.
- Configuration errors surface at startup with a clear message, not midway
  through a request.

**Negative**

- Slightly more plumbing: `settings` is threaded through call sites rather than
  imported wherever it is wanted.
- Callers must remember to call `load_settings()`. The CLI does this for them.

## Related

The same principle applies to the prompt catalogue, which is loaded from
package data via `importlib.resources` rather than from a path relative to the
working directory.
