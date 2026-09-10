# A local Sheetbend endpoint on a 24 GB Apple Silicon Mac

## Recommendation

Start with **Ollama and `qwen3.5:4b`**. This prioritizes a convenient local endpoint,
model installation, and a service lifecycle, not a claim that it is the fastest
possible Apple Silicon inference backend.

Ollama's library currently lists these download sizes:

| Model | Listed download | Suggested role |
| --- | ---: | --- |
| `qwen3.5:2b` | 2.7 GB | Smaller smoke-test model |
| `qwen3.5:4b` | 3.4 GB | Recommended development default |
| `qwen3.5:9b` | 6.6 GB | Optional larger comparison, not the permanent default |

These are package sizes, **not measured resident RAM**. Runtime buffers, context,
image processing, and concurrent requests add memory. The listed 4B package is
only 0.7 GB larger than 2B, so the smaller model does not save a different class of
memory here. A 9B model is not automatically too large for 24 GB; it simply leaves
less room for the rest of the workstation. Start with one parallel request and
an 8,192-token context rather than the largest advertised context window.

Sources: [Ollama Qwen3.5 library](https://ollama.com/library/qwen3.5),
[Apple Silicon support](https://docs.ollama.com/macos),
[context settings](https://docs.ollama.com/context-length).
No speed or memory measurement on your Mac was performed for this guide.

## 1. Install the server and its persistent settings

With Homebrew installed, use its formula rather than simultaneously running the
Ollama desktop application. Quit an existing desktop Ollama before enabling the
service on the same port. The endpoint runs separately from your Python/mamba
notebook environment.

```bash
brew update
brew install ollama
mkdir -p "${HOMEBREW_USER_CONFIG_HOME:-$HOME/.homebrew}/services"
```

Create `ollama.env` in that `services` directory with the following content. Merge
these settings into an existing file instead of replacing unrelated settings:

```text
OLLAMA_HOST=127.0.0.1:11434
OLLAMA_NO_CLOUD=1
OLLAMA_CONTEXT_LENGTH=8192
OLLAMA_NUM_PARALLEL=1
OLLAMA_MAX_LOADED_MODELS=1
OLLAMA_KEEP_ALIVE=-1
```

For a new file, this shell block refuses to overwrite an existing one:

```bash
(
  set -C
  cat > "${HOMEBREW_USER_CONFIG_HOME:-$HOME/.homebrew}/services/ollama.env" <<'EOF'
OLLAMA_HOST=127.0.0.1:11434
OLLAMA_NO_CLOUD=1
OLLAMA_CONTEXT_LENGTH=8192
OLLAMA_NUM_PARALLEL=1
OLLAMA_MAX_LOADED_MODELS=1
OLLAMA_KEEP_ALIVE=-1
EOF
)
```

Homebrew's current services interface supports persistent `KEY=value` files at
`$HOMEBREW_USER_CONFIG_HOME/services/<formula>.env`, with `~/.homebrew` as its
default user config home. Changes take effect on a service restart and persist
across upgrades. There is no need to write a custom LaunchAgent just to set these
variables. Keep Homebrew current for this interface.

Here, loopback binding avoids exposing an unauthenticated endpoint to the network,
cloud features are disabled, one model/one parallel request controls memory
pressure, and negative keep-alive requests that loaded weights remain resident.
These settings do not impose a hard process-RAM cap.

Sources: [Homebrew services and environment files](https://docs.brew.sh/Manpage#services-subcommand),
[Ollama environment variables and local-only mode](https://docs.ollama.com/faq).

## 2. Start now and automatically at login

```bash
brew services start ollama
ollama pull qwen3.5:4b
curl --fail --silent --show-error http://127.0.0.1:11434/v1/models
brew services info ollama
```

When modifying an already running service's settings, use `brew services restart
ollama` instead of `start` so the changed environment is loaded.

Without `sudo`, Homebrew registers a per-user service that starts **after login**,
not before the login screen or FileVault unlock. Closing Terminal does not stop
it. A sleeping or powered-off laptop cannot serve requests; this setup does not
prevent sleep or arrange wake-on-demand. Use it again after the Mac wakes.

Model downloads need internet. Inference uses the downloaded local tag, not a
`:cloud` model. Disabling cloud features does not make package downloads work
without a network.

Source: [Homebrew service startup semantics](https://docs.brew.sh/Manpage#services-subcommand),
[Ollama formula](https://formulae.brew.sh/formula/ollama).

## 3. Warm the model and check residency

The server starts at login. The model loads when first requested. To load it now
without asking for a generated answer:

```bash
curl --fail --silent --show-error http://127.0.0.1:11434/api/generate \
  -H 'Content-Type: application/json' \
  -d '{"model":"qwen3.5:4b","keep_alive":-1,"stream":false}'
ollama ps
```

`OLLAMA_KEEP_ALIVE=-1` also applies to subsequent requests unless a request overrides
it. This is requested retention, not a guarantee against explicit unloads, server
restarts, or Ollama's memory management. RAM does not survive reboot. At the next
login the endpoint starts automatically, and the first request loads the model
again. Automatic preloading before the first request would need an additional
login job; it is deliberately not necessary for this setup.

Free the model's RAM without stopping the API service:

```bash
ollama stop qwen3.5:4b
```

For a gentler default, change `OLLAMA_KEEP_ALIVE=-1` to `OLLAMA_KEEP_ALIVE=5m` and
restart the service. Then idle weights unload while the endpoint remains available.

Sources: [Ollama preloading and keep-alive](https://docs.ollama.com/faq),
[native generation endpoint](https://docs.ollama.com/api/generate).

## 4. Connect Sheetbend

From the updated Sheetbend repository:

```bash
python -m pip install -e '.[llm]'
mkdir -p "${XDG_CONFIG_HOME:-$HOME/.config}/sheetbend"
test ! -e "${XDG_CONFIG_HOME:-$HOME/.config}/sheetbend/config.toml" && \
  cp examples/mac-local-endpoint.toml \
     "${XDG_CONFIG_HOME:-$HOME/.config}/sheetbend/config.toml"
```

The guarded copy does not overwrite an existing configuration. Merge the example's
`laptop` source into an existing schema-2 file instead when necessary.

The supplied example uses:

```toml
schema_version = 2
default_source = "laptop"

[sources.laptop]
protocol = "openai-compatible"
base_url = "http://127.0.0.1:11434/v1"
default_model = "qwen3.5:4b"
scope = "local"
auth = { type = "none" }
timeout_seconds = 120

[sources.laptop.rate_limit]
requests_per_minute = 60
max_concurrent = 1

[sources.laptop.models."qwen3.5:4b"]
capabilities = { streaming = true, json_schema = true, vision = true, tools = true }
```

Capabilities are declarations to verify on your installed endpoint, not results
from a live test on your Mac. The longer network inactivity timeout accommodates
local loading/reasoning delays; it is not a wall-clock request or queue deadline.

```bash
sheetbend sources
sheetbend check laptop
sheetbend check laptop --test text
sheetbend check laptop --all
```

The first check is catalog-only. `--all` performs five generation probes plus the
catalog. Local checks consume compute, not hosted API credits, and may include
initial load latency. They are not scientific accuracy evaluations.

Run `sheetbend top` in another terminal. Then in a notebook:

```python
from sheetbend import Registry

source = Registry.from_file().source("laptop", allowed_scopes={"local"})
with source.connect(application="notebook", tool="smoke-test") as model:
    response = model.prompt("Say hello in one sentence.", stream=False)
    print(response.text())
```

The kernel needs Sheetbend and its `llm` extra in its own environment. Use the same
config-file and runtime-directory conventions across applications to share quotas.
The server does not need to be installed in the notebook's Python environment.

Sources: [Ollama OpenAI-compatible routes](https://docs.ollama.com/api/openai-compatibility),
[structured outputs](https://docs.ollama.com/capabilities/structured-outputs).

## Service maintenance

```bash
brew services restart ollama     # Reload configuration/restart
brew services stop ollama        # Stop and unregister automatic login startup
brew services start ollama       # Start and register again
brew services list
ollama ps                       # Currently loaded models and reported allocation
ollama list                     # Downloaded models
```

A native Ollama or other client's direct request does not appear in Sheetbend's
`top` or consume Sheetbend's client-side rate budget. Ollama's server settings
still govern server-side parallelism. The Mac deployment commands in this guide
were researched but not executed on a Mac in the patch-building environment.
