# sheetbend

Discover, inspect, and verify intelligence sources from one small, explicit
configuration. Use the same source names in a notebook, a command-line tool, or
another Python library while keeping endpoint details out of application code.

**Sheetbend owns the connection registry. [LLM](https://llm.datasette.io/) owns
inference.** A Sheetbend connection yields an LLM model, so prompting,
conversations, streaming, tools, and schema-shaped responses remain LLM's API.
Sheetbend adds credential binding, source-local request limits, resource lifetime,
and small synthetic diagnostics. It does not implement a second prompt API.

## Install

Python 3.13 or later:

```bash
mamba create -n sheetbend_env -c conda-forge python=3.13 pip
mamba activate sheetbend_env
python -m pip install -e '.[llm]'
```

For registry inspection and the `/models` diagnostic alone, `pip install -e .`
is sufficient. The optional `llm` extra adds actual model connections and generation
probes. The initial adapter targets LLM 0.35 and OpenAI Python 3.x; its bounded
version range is deliberate because it uses LLM's OpenAI-compatible Chat class.
LLM's transitive Pydantic dependency belongs to LLM, not Sheetbend's data model.

## Configure once per environment

The default is `${XDG_CONFIG_HOME:-$HOME/.config}/sheetbend/config.toml`, on both
Linux and macOS. A relative `XDG_CONFIG_HOME` is ignored, as specified by XDG.

```bash
mkdir -p "${XDG_CONFIG_HOME:-$HOME/.config}/sheetbend"
cp examples/config.toml "${XDG_CONFIG_HOME:-$HOME/.config}/sheetbend/config.toml"
```

**Edit the placeholder model names and addresses before probing.** The example
contains local, institutional, and external sources. A minimal local source is:

```toml
schema_version = 1
default_source = "local"

[sources.local]
protocol = "openai-compatible"
base_url = "http://127.0.0.1:8000/v1"
default_model = "your-local-model"
scope = "local"
auth = { type = "none" }

[sources.local.rate_limit]
requests_per_minute = 60
max_concurrent = 2

[sources.local.capabilities]
json_schema = true
```

`protocol = "openai-compatible"` means **Chat Completions** in version 0.1:
`POST <base_url>/chat/completions`, plus `GET <base_url>/models` for the catalog
probe. A server need not expose `/models` to support inference; run a text probe
when its catalog route is unavailable. There is no inferred `/v1`, alternate
endpoint fallback, Responses API routing, or native non-compatible provider plugin
integration in this version.

The complete external contract is
[`src/sheetbend/schemas/config.schema.json`](src/sheetbend/schemas/config.schema.json).
It validates the JSON-compatible value parsed from TOML. Unknown fields, invalid
credential forms, non-finite numbers, TOML dates, and undeclared default sources
fail rather than being silently interpreted. The schema owns defaults; the loader
applies them to an independent copy after validation. Endpoint safety and
cross-reference checks supplement the schema.

### One selected configuration, never a merge

Precedence is an explicit `config_path`, then a nonempty `SHEETBEND_CONFIG`, then
the default above. An explicitly selected missing, unreadable, or invalid file is
an error. A missing implicit default represents an empty registry. A malformed
implicit default or broken symlink is still an error.

```bash
export SHEETBEND_CONFIG="$HOME/.config/sheetbend/cluster.toml"
sheetbend sources

# Or select exactly one file for this invocation:
sheetbend --config-path /path/to/workstation.toml sources
```

No current-directory search, parent-directory search, `.env` discovery, automatic
creation, hostname matching, fallback source selection, or automatic network
requests. `default_source` is optional, but its absence requires an explicit source
name even when exactly one source exists. Reopen the registry to read an edited
configuration; an existing registry is a snapshot.

### Authentication

The `auth` object is required and accepts exactly three forms:

```toml
auth = { type = "none" }
auth = { type = "bearer", env = "MSK_LLM_API_KEY" }
auth = { type = "header", env = "INFERENCE_API_KEY", header = "api-key" }
```

These are alternatives, not three entries to paste into the same source. `bearer`
sends `Authorization: Bearer <value>`; `header` sends the value verbatim in the
named credential header. `Authorization` and routing/transport headers are
reserved; choose `bearer` for bearer authentication.

No plaintext key field, credential command execution, implicit OpenAI key lookup,
LLM key-store lookup, or global environment mutation. Credentials are resolved at
`connect()` entry and once per catalog request, not when importing, loading,
selecting, or displaying sources. Reconnect after rotating a secret.

Environment variables are not a vault. Sheetbend keeps credential references in
config and does not write resolved values to disk. `source.resolve_auth()` is an
explicit low-level escape hatch that returns **secret-bearing HTTP headers**; its
caller owns their use and disclosure. Ordinary `to_dict()`, `repr()`, and CLI
inspection contain only references, never resolved secrets. Keep descriptions,
source/model names, and URL paths free of secrets too.

Ambient `OPENAI_CUSTOM_HEADERS` is rejected for an LLM connection rather than
allowing the SDK to add unrelated headers. The context uses its explicit endpoint,
credential headers, and no ambient OpenAI organization/project. Do not enable
third-party SDK debug logging around sensitive data; Sheetbend cannot control
what external logging or a notebook traceback retains.

### Scopes are declarations, not permissions

`local` means processing stays on the executing host; `institutional` names a
specific organization's processing boundary; `external` is outside those
boundaries. An institutional source must have `organization = "msk"` (or its
actual organization). Other scopes must not include that field. This is not the
OpenAI SDK's organization header.

Local means the Python process's host, not the laptop displaying a remote
notebook. A loopback address might be a tunnel to another machine: neither an
address nor a scope label certifies actual privacy, policy approval, or the absence
of onward forwarding. Sources are never automatically ranked by "trust".

```python
from sheetbend import Registry

registry = Registry.from_file()
source = registry.source(
    "msk",
    allowed_scopes={"institutional"},
    organization="msk",
)
```

A mismatch raises `SelectionError`. Sheetbend does not choose an external source
instead. These checks only enforce the configured declarations; the caller is
responsible for deciding what data may be sent.

HTTPS verifies certificates. Set `ca_bundle` to a custom trust bundle where
needed; a relative path is relative to the selected configuration's directory
(or the construction-time working directory with `Registry.from_dict()`). There
is no `verify = false`. Non-loopback HTTP requires the conspicuous
`allow_insecure_http = true` opt-in. HTTP proxies from the environment are not
used, and redirects are not followed.

## See and verify sources

```bash
sheetbend sources
sheetbend sources --json
sheetbend inspect local
sheetbend inspect msk --json

sheetbend check local                      # GET /models only; no generation
sheetbend check local --test text          # Synthetic prompt; may incur cost
sheetbend check local --test schema        # Request schema output, then validate it
sheetbend check local --test stream        # Consume a synthetic streaming response
sheetbend check local --test text --model another-model --json

sheetbend schema                           # The authoritative config schema
sheetbend schema --name check              # Versioned diagnostic output schema
sheetbend version
```

Global `--config-path` goes **before** the subcommand. `inspect` and `check` may
omit the source name only when `default_source` is configured. Inspection is
strictly offline and does not require valid credential values. `version` and
`schema` also work when the selected config file is broken.

One check performs one requested operation. There is no automatic startup
verification or generation while listing sources. Generation probes send fixed,
small synthetic prompts, not user files, notebook variables, or conversation
history. They request a short answer but do not impose a provider-independent
token ceiling: provider reasoning and billing behavior can still vary. A failure
is a structured `CheckResult`, and the CLI exits 1. Config/selection errors also
exit unsuccessfully. There is no retry or fallback. Ordinary library operations
raise their failures rather than collecting partial successes.

A successful catalog check does not establish inference access. Text checks
require nonempty text, not exact instruction following. Schema checks require
actual JSON satisfying the probe's schema, not merely an HTTP 200 response. A
single passing example is not proof of general server-side schema enforcement.
Streaming checks establish that a response can be consumed in streaming mode,
not a throughput or latency guarantee. No probe updates the hand-edited config.

## Use LLM in a notebook or application

```python
from sheetbend import Registry

registry = Registry.from_file()
print(registry)
source = registry.source("local")
print(source)

# These are independent public workflow stages:
print(source.list_models())
print(source.check(test="text"))

with source.connect() as model:
    response = model.prompt("Say hello in one sentence.", stream=False)
    text = response.text()

print(text)
```

`model` is an LLM `KeyModel` subclass and `response` is an ordinary LLM `Response`.
`source.connect(model="another-model")` explicitly overrides the default model.
It prepares a connection; it does not promise that the server is reachable.

**Consume lazy responses inside the context.** LLM sends the request when you
consume the response, not necessarily at `prompt()`. Starting or resuming an
unfinished request after the context exits fails. Completed response text and
metadata can be retained. Each context owns its HTTP client; reuse one context
for a sequence of prompts. Finish active worker threads before closing their
connection. An abandoned stream's limiter permit is released when its connection
closes; unfinished streams must not be treated as completed responses.

```python
with source.connect() as model:
    conversation = model.conversation()
    first = conversation.prompt("Remember the word knot.", stream=False).text()
    second = conversation.prompt("What word did I give you?", stream=False).text()
```

There is no Sheetbend `prompt()` or `generate()` method to learn. LLM handles
conversation context, attachments, tool definitions, options, and response usage.
The capability declarations must match the selected model. Version 0.1 supplies
synchronous connections only; using Jupyter does not require an async API.

### JSON Schema outputs

Set `capabilities.json_schema = true` for a supporting model. Pass an ordinary
schema dictionary straight to LLM, and independently validate application data:

```python
import json
from jsonschema import Draft202012Validator

output_schema = {
    "type": "object",
    "properties": {"greeting": {"type": "string"}},
    "required": ["greeting"],
    "additionalProperties": False,
}

with source.connect() as model:
    response = model.prompt(
        "Return a friendly greeting.",
        schema=output_schema,
        stream=False,
    )
    result = json.loads(response.text())

Draft202012Validator(output_schema).validate(result)
print(result["greeting"])
```

A schema probe can explicitly try the schema capability before you enable its
normal-use declaration. Endpoint/model support varies. The adapter does not
convert an unsupported schema request into an unconstrained prompt or a mere
JSON-mode request. It does not claim that LLM's request enforces strict schemas on
every server; application validation remains appropriate.

### Declared capabilities and defaults

The schema defines `streaming = true`, `json_schema = false`, `vision = false`,
`tools = false`, and `system_prompt = true`. They configure the LLM model; they are
not discoveries or promises. They apply to the selected model, including an
explicit model override. Use separate named sources when models at the same
endpoint need different declarations. The available model IDs from `/models`
are not silently enrolled or inferred to share capabilities.

## Rate limits without a service daemon

`requests_per_minute` limits request starts in a rolling 60-second window.
`max_concurrent` limits active calls through response consumption. Either can be
omitted, meaning no corresponding client-side limit. Omitting `rate_limit`
means neither limit is imposed. Counts include rejected attempts.

**The limits belong to one `Source` instance.** Repeated `registry.source("local")`
calls return that same instance. Its model connections and catalog probes share
a thread-safe limiter. Another registry, process, notebook kernel, machine, or
source entry has independent limits—even when it points at the same endpoint.
Provider-side quotas remain authoritative. This is not a distributed quota
system, token-per-minute enforcer, scheduler, or guarantee of fairness.

No SDK retries are enabled: a failed attempt fails. The schema's
`timeout_seconds = 30` is the network operation/inactivity timeout, not a total
wall-clock deadline for a whole stream and not a timeout for waiting in the
limiter queue. Consume or close active responses before starting another that
would exceed a concurrency limit. Repeatedly opening new registries is not a
way to obtain shared throttling.

## What about `sheetbend top`?

Not included in 0.1. A future `top` view could observe calls routed through the
adapter, but viewing activity from other processes would require explicit shared
telemetry. A useful record would contain source/model IDs, request IDs, timestamps,
status, latency, and optional token usage—not prompt text, response text, or keys.

This commit creates no telemetry files, database, daemon, registry plugin, or
background thread for monitoring. It cannot observe arbitrary requests made
outside Sheetbend. That boundary can stay small even when monitoring is added.

## Development and verification

```bash
python -m pip install -e '.[llm,dev]'
pytest
ruff check .
python -m pip wheel --no-deps --wheel-dir dist .
```

Tests cover schema/default behavior, explicit discovery, ownership, scope filters,
authentication, CLI exit codes, rate limiting, and model-catalog requests. The
LLM integration module uses the actual LLM and OpenAI packages against a loopback
HTTP fixture to test prompting, conversation history, schema forwarding,
streaming, auth on the wire, failure/no-retry behavior, and connection lifetimes.
It is skipped when the optional LLM extra is absent; a skipped integration module
must not be mistaken for an integration pass. CI installs the extra.

The public API is experimental in this initial release. `pyproject.toml` is the
only authored package version. Both JSON Schemas and the type marker are included
in the wheel. The repository's Apache-2.0 license is unchanged.

### Implementation references

- [XDG Base Directory Specification](https://specifications.freedesktop.org/basedir/latest/)
- [LLM Python API, including lazy responses and dictionary schemas](https://llm.datasette.io/en/stable/python-api.html)
- [LLM's OpenAI-compatible models](https://llm.datasette.io/en/stable/other-models.html)
- [LLM adapter implementation](https://github.com/simonw/llm/blob/main/llm/default_plugins/openai_models.py)
- [JSON Schema defaults are annotations; applications must apply them](https://python-jsonschema.readthedocs.io/en/stable/faq/)
