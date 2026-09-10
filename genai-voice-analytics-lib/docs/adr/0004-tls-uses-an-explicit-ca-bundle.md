# ADR 0004 — TLS verification uses an explicit CA bundle

**Status:** Accepted
**Date:** 2026-09-10

## Context

The LLM gateway runs inside the corporate network and presents a certificate
signed by an **internal CA that is not in any system trust store**. The
originating service carries `SSL_VERIFY: bool = False`, and `Dockerfile.local`
mounts a `custom_ca` secret into the image's trust store at build time —
evidence that a corporate CA is required in practice.

A boolean alone cannot express "verify, using this CA". Setting `SSL_VERIFY=True`
without supplying the CA fails the handshake, which pushes operators toward
disabling verification entirely.

## Decision

Two settings, resolved by `Settings.ssl_verify()`:

| Setting | Effect |
|---|---|
| `SSL_VERIFY` | Boolean. Verify against the system trust store. |
| `SSL_CA_BUNDLE` | Path to a PEM bundle. **Implies verification.** |

`ssl_verify()` returns an `ssl.SSLContext` built with
`ssl.create_default_context(cafile=...)` when a bundle is configured, and the
boolean otherwise. A context is built rather than a path passed through because
httpx deprecated `verify=<str>`.

A missing or malformed bundle raises `ConfigurationError` **at configuration
time**, not as an opaque handshake error mid-request.

A configured bundle takes precedence over a false `SSL_VERIFY`: supplying a CA
is an explicit intent to verify.

The default remains `SSL_VERIFY=False` to match the current service, so
adopting the library changes no behaviour until verification is opted into.

## Consequences

**Positive**

- Verification is achievable in the corporate environment without baking the CA
  into the image, though baking it in still works.
- Misconfiguration fails fast and names the file, rather than surfacing as a
  TLS error during a request.
- Not affected by httpx's deprecation of string paths.

**Negative**

- Two settings instead of one. The precedence rule (bundle wins) has to be
  known, which is why it is documented in `env.sample`, the README and the
  method's docstring.
- The insecure default is retained for compatibility. Deployments should set
  both settings explicitly; leaving the default in production is a weakness
  inherited from the originating service, not one introduced here.

## Verification

Tests generate a real self-signed CA with `openssl` rather than a placeholder
string, so `ssl.create_default_context` is genuinely exercised. That is how the
httpx deprecation was discovered.
