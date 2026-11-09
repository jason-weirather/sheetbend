# Authenticated gateway and process launcher

The gateway exposes a restricted HTTP inference surface over the same source
registry and shared request ledger used by `Source.connect()`. No other model
server or secrets package is required. Install the optional HTTP server:

```bash
python -m pip install -e '.[llm,gateway]'
```

`llm` is needed for Python chat connections and chat generation checks, not for
HTTP forwarding, catalogs, credential checks, or native System One calls.

## Temporary gateway: `sheetbend run`

```bash
sheetbend run --source studio --adapter openai -- python agent.py
sheetbend run --source studio --model EXACT_SERVER_ID --reasoning off \
  --application any --adapter openai -- python agent.py
```

An ephemeral loopback listener is started on an available port. The selected model
is exposed under the single alias **`selected`**. Credentials resolve before the
child starts; the child receives a random, single-route session token. The server
and token cease to work when the command exits. No upstream API keys are injected.
There is no requirement to configure persistent gateway routes/clients for `run`.

| Adapter | Variables supplied to the child |
| --- | --- |
| `sheetbend` | `SHEETBEND_BASE_URL`, `SHEETBEND_API_KEY`, `SHEETBEND_MODEL=selected` |
| `openai` | All of the above, plus `OPENAI_BASE_URL` and `OPENAI_API_KEY` |

The OpenAI adapter requires a chat-capable model. Use the Sheetbend adapter for a
System One-only model. There is no universally recognized model environment
variable: applications must read `SHEETBEND_MODEL` or be configured to use
`selected`. Applications that ignore the documented variables are not rerouted.
Native Sheetbend Python applications can continue using `Registry`/`Source`
directly; wrapping them does not force their independent connections through HTTP.

The environment is constructed for the child, without modifying `os.environ`.
By default it includes PATH/HOME/USER/LOGNAME/SHELL, LANG/LC_*, TERM/COLORTERM/
NO_COLOR, TMPDIR/TMP/TEMP, VIRTUAL_ENV, CONDA_PREFIX/CONDA_DEFAULT_ENV/CONDA_SHLVL,
PYTHONPATH/PYTHONUTF8/PYTHONIOENCODING, and XDG config/cache/data/state directories.
Proxy and unrelated credential variables are not inherited. Both NO_PROXY spellings
are set for loopback. Use repeated `--inherit-env NAME` for necessary additional
application settings. Such opt-ins can expose secrets; they are not a sandbox.

The child uses inherited stdin/stdout/stderr. Sheetbend does not print its token,
credentials, or the child command arguments. The child can still print its own
secrets. Ordinary exit codes are preserved. SIGINT/SIGTERM/SIGHUP are forwarded to
the child process group; after five seconds an unresponsive group is killed.
Signal termination is reported as `128 + signal`. Keep work in the foreground:
background descendants do not keep the temporary gateway alive after the parent
command exits. Persistent services should use `serve` under their service manager.

## Persistent gateway

A runnable single-machine example is [studio-gateway.toml](../examples/studio-gateway.toml).
The significant additions to an existing configuration are:

```toml
[credentials.laptop]
provider = "file"
path = "secrets/laptop-token.txt"

[gateway]
port = 8765
allowed_scopes = ["local", "institutional"]

[gateway.routes.studio-chat]
source = "studio"
model = "EXACT_CONFIGURED_SERVER_ID"
# reasoning = "off"  # Only when the model declares support.

[gateway.clients.laptop]
credential = "laptop"
routes = ["studio-chat"]
```

Client tokens must be distinct, at least 32 non-whitespace characters, and should
be randomly generated. Route lists are mandatory; there is no wildcard or
anonymous client. Route source/model references, scope permission, and reasoning
settings validate offline. A named route has an exact target, not a fallback list.
External sources require `external` in `gateway.allowed_scopes` before export.
A caller with a token may use that token's explicitly granted routes; it cannot
widen its scope by supplying request fields or another upstream URL.

```bash
sheetbend serve
sheetbend serve --port 8766
```

`serve` binds **127.0.0.1 only**. It is intentionally not a public TLS server.
Use SSH forwarding or an authenticated HTTPS reverse proxy for other machines;
see [networking](networking.md). It is a foreground process, not an installed
LaunchAgent or daemon. Configuration, credentials, and client grants are startup
snapshots; restart after changing them.

## HTTP surface

All endpoints require `Authorization: Bearer <gateway client token>`.

| Method | Endpoint | Behavior |
| --- | --- | --- |
| GET | `/v1/models` | Static catalog of this client's granted aliases and declared capabilities. No upstream probe. |
| POST | `/v1/chat/completions` | Validated Chat Completions forwarding, JSON or SSE. |
| POST | `/v1/systemone` | Native typed decisions, JSON only. |

The `model` request field is the **gateway alias**, not the upstream model ID.
The gateway rewrites it upstream and rewrites the top-level response/SSE `model`
field back to the alias. Tool-call and reasoning response fields are retained.
Upstream HTTP error bodies and response headers are not relayed. Errors use an
`error` object with a safe `message` and `type`. Upstream 3xx responses are never
followed. Once streaming starts, a failure is a sanitized SSE error event; it is
not marked as a successful completion. Client libraries may retry independently;
the gateway itself does not retry or route around a failure.

Example client for `sheetbend run --adapter openai -- python agent.py`:

```python
import os
import httpx

response = httpx.post(
    os.environ["OPENAI_BASE_URL"] + "/chat/completions",
    headers={"Authorization": "Bearer " + os.environ["OPENAI_API_KEY"]},
    json={
        "model": os.environ["SHEETBEND_MODEL"],
        "messages": [{"role": "user", "content": "Reply with OK."}],
        "stream": False,
    },
    timeout=180,
    trust_env=False,
)
response.raise_for_status()
print(response.json()["choices"][0]["message"]["content"])
```

For persistent or remote clients, supply that gateway's base URL, authorized
client token, and exported model alias using the application's own configuration.
Gateway client tokens are separate from oMLX/provider keys.

## Compatibility and limits

This is a deliberately bounded Chat Completions subset, not a universal reverse
proxy. Supported request fields are `model`, `messages`, `stream`, `stream_options`,
`temperature`, `top_p`, `n`, `max_tokens`, `max_completion_tokens`, `stop`,
`presence_penalty`, `frequency_penalty`, `logit_bias`, `logprobs`, `top_logprobs`,
`seed`, `user`, `tools`, `tool_choice`, `parallel_tool_calls`, `response_format`,
`reasoning_effort`, and the declared `chat_template_kwargs.enable_thinking` control.
Unknown top-level or message fields are rejected rather than forwarded blindly.
The upstream still validates detailed parameter ranges and tool/schema semantics.
Only function tool definitions are accepted; provider-hosted web/MCP tools are
not part of this interface. Tools are proposed calls; Sheetbend never executes them.

Inline base64 PNG/JPEG/WebP images are supported when `vision=true`. URL/file
images, audio, video, multipart uploads, arbitrary chat-template fields, and
undeclared capability use are rejected. A bound reasoning policy cannot be
contradicted; an identical incoming override is accepted for gateway chaining.
Provider-mode native overrides require the corresponding explicit declaration.
`off` never means `low` or suppression of returned reasoning text.

There is no Responses API, Anthropic Messages API, completions endpoint, admin API,
embeddings API, browser/CORS access, credential-export endpoint, remote config
editing, or raw URL proxy. A client that requires one of these is not compatible
merely because it has an "OpenAI-compatible" setting.

Defaults are 16 MiB request bodies, 32 MiB total responses/streams, 1 MiB per SSE
event, 64 server connections/tasks, a 10-second admission deadline, a 15-second
maximum body-read deadline, and a 300-second overall request deadline. Supported
configurable limits are in `sheetbend schema --name config`. The source HTTP
inactivity timeout also applies. Gateway framing uses Uvicorn, not Python's
standard-library development HTTP server. Request/access logs are disabled.

## Accounting and trust boundary

Forwarded inference calls share the source's canonical-config-path bucket with
other participating Python applications on that host. All clients of the same
server/source share that budget. A slot remains active until response consumption
or cancellation. Disconnects cancel waiting admission and close upstream streams;
this cannot promise that a provider cancels already accepted computation.

`sheetbend top` shows the gateway process PID, the authenticated configured client
label (`gateway:laptop`), the actual source/model, state, and provider-reported
usage. It does not invent the remote client's PID or forward caller-supplied
identity labels. `run` uses its explicitly supplied application label. A static
GET `/v1/models` is an inventory lookup and does not consume an upstream quota.

This is cooperative host-local accounting, not a distributed quota database or
a durable audit trail. Separate gateway hosts have separate ledgers. Routes through
multiple gateways are accounted at each hop. Direct provider access and independent
credentials bypass Sheetbend. Request data is necessarily visible in gateway
memory; it is not written to the telemetry ledger. This implementation is not an
independent security certification for public hosting or institutional data use.
