# Upgrading to 0.6

The single config remains `schema_version=2`. Existing source definitions and
environment-variable auth continue to load. Do not automatically rewrite user
configuration or turn on capabilities based on a model name. Restart old callers,
notebook kernels, and gateways after changing code/configuration.

## Additions

- Named `credentials` references: environment, private raw text/private TOML, and
  macOS Keychain. Azure's native provider is deferred, not silently emulated.
- Optional `gateway` extra; `sheetbend serve` and `sheetbend run`. The listener is
  loopback-only, authenticated, and restricted to explicit routes. Child processes
  receive a temporary gateway token, not provider credentials.
- `Dashboard` and `sheetbend dashboard`, with a packaged version-1 JSON snapshot.
  Offline by default, explicit credential/catalog probes cached for at least 60
  seconds, optional one-shot inference, Rich color, and ASCII fallback.
- `chat` (default true) and `system_one` (default false) model capabilities.
  Native `Source.decide()`, `/v1/systemone` gateway forwarding, and typed response
  checks. No LLM dependency is needed for the native decision path.
- Explicit `catalog_format`: `openai` (default, `data[].id`) or `typesafe`
  (`models[].name`). No URL/catalog guessing.
- Optional bounded admission waits for catalog/gateway operations. Existing
  ordinary Python connection wait behavior is unchanged.

The config/check/check-report/activity schemas have additive vocabulary changes.
The check test vocabulary adds `credentials` and `system_one`; activity operations
add `system_one`. Consumers validating against an older bundled schema must update.
Chat-only `check_all()` still runs the original six tests. Decision-only models
run catalog plus System One; dual-interface models run both sets. A System One
check does not inherit a chat reasoning argument.

## Deliberate boundaries

This does not expose a generic secret-to-environment command, shell `eval` exporter,
arbitrary HTTP proxy, Responses API, Anthropic Messages API, or model-server admin
endpoints. Native Python chat prompting remains LLM's API. System One is a distinct
interface, not another spelling of reasoning off.

The gateway holds resolved keys and grants until restart. Persistent client tokens
are not automatically expiring OAuth tokens. `run` tokens are valid only for that
temporary listener's lifetime. The default child environment is allow-listed;
use `--inherit-env NAME` for necessary additional application settings. This is
not sandboxing against same-user code that can independently read files or connect
to other endpoints.

One gateway can register another as a source. This gives a static aggregate catalog,
not automatic peer enrollment, health-based fallback, globally shared quotas, or
fleet telemetry. Remote computation remains remote even through a loopback tunnel.
Use SSH or a maintained TLS proxy for network exposure, with separate client tokens
and per-client route grants.

## Verification before deployment

```bash
python -m pip install -e '.[llm,gateway,dev]'
SHEETBEND_REQUIRE_LLM_TESTS=1 SHEETBEND_REQUIRE_GATEWAY_TESTS=1 pytest
ruff check .
python -m pip wheel --no-deps --wheel-dir dist .
```

CI installs both integrations and requires them. The regular test suite uses
synthetic loopback providers, not real model accuracy tests. Optional integration
modules may skip in a minimal local installation; a skipped integration is not a
pass. A live Mac/oMLX check, actual Keychain authorization, SSH forwarding, and a
public TLS deployment must be verified in their real environments.

Start with [studio-quickstart.md](studio-quickstart.md), then
[credentials.md](credentials.md), [gateway.md](gateway.md),
[networking.md](networking.md), and [system-one.md](system-one.md).
