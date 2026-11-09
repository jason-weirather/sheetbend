# Mac Studio: oMLX -> Sheetbend -> applications

This uses your existing oMLX server. It does not download models, change its
settings, or turn on an unverified model capability.

## 1. Install the updated checkout

```bash
python -m pip install -e '.[llm,gateway]'
sheetbend version
```

The version is 0.6.0; the config remains schema 2. Existing environment auth still
works. Stop/restart old callers and notebook kernels after upgrading. The optional
LLM integration still has the bounded dependency range in `pyproject.toml`.

## 2. Store the existing oMLX key and a separate gateway token

The following uses the selected Sheetbend config directory, prompts for the
**existing oMLX inference key**, and generates a separate random gateway token.
It refuses to overwrite either file. It never displays either value.

```bash
python - <<'PY'
from getpass import getpass
import os
import secrets
from sheetbend.config import select_config_path
from sheetbend.credentials import header_secret

root = select_config_path()[0].parent
folder = root / "secrets"
folder.mkdir(mode=0o700, parents=True, exist_ok=True)
folder.chmod(0o700)
paths = [folder / "omlx.txt", folder / "laptop-token.txt"]
if any(path.exists() or path.is_symlink() for path in paths):
    raise SystemExit("A credential file already exists; no files were changed.")
omlx = header_secret(getpass("Existing oMLX inference API key: "))
for path, value in zip(paths, [omlx, secrets.token_urlsafe(32)]):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(value + "\n")
print("Credential files created; values were not displayed.")
PY
```

These files are plaintext. Keep them outside Git and shared folders. The file
provider enforces owner-only permissions but is not encryption or a sandbox.
See [credential storage](credentials.md) for a private multi-key TOML file or
macOS Keychain. The provider key and the gateway client token serve different
purposes; do not reuse one as the other.

## 3. Configure the source

Start from [examples/studio-gateway.toml](../examples/studio-gateway.toml). Copy it
only when creating a new config; merge into an existing file instead of replacing
unrelated sources. The normal location is `~/.config/sheetbend/config.toml` unless
XDG_CONFIG_HOME or SHEETBEND_CONFIG selects another file.

Set every `YOUR_CHAT_MODEL_ID` to the exact ID reported by your oMLX catalog. A
catalog-only check does not require that placeholder model to be served, so this
can help discover the IDs after the source and credentials are configured:

```bash
sheetbend sources
sheetbend check studio --test credentials
sheetbend check studio --json
```

The JSON report's `model_ids` lists the server IDs. Replace the placeholder in
`default_model`, the quoted model table key, and the gateway route. Keep the `/v1`
in `base_url`. The example assumes port 8000; use your actual oMLX port.

For an intentionally unauthenticated loopback oMLX, use `auth = { type = "none" }`
instead. The Sheetbend gateway still requires its own client authentication.

## 4. Inspect and test, without a polling storm

```bash
sheetbend dashboard                         # Offline inventory
sheetbend dashboard --probe                 # Credential access + one catalog per source
sheetbend dashboard --probe --watch         # Cached checks, no faster than once/minute
sheetbend dashboard --probe --plain         # ASCII, no ANSI controls
sheetbend dashboard --json                  # Machine-readable inventory
sheetbend dashboard --test text --source studio
```

An explicit dashboard inference test runs once, even with `--watch`. It never
repeats on redraw. `listed` means catalog membership, not residency or successful
inference. The credential table reports retrieval, not provider authorization.
The route table shows configuration, not whether a persistent listener is running.
Probes skip external sources unless `--allow-external` is supplied. Unused
credential references remain unchecked. Restart the dashboard after editing TOML.

Use explicit probes before enabling extra capabilities:

```bash
sheetbend check studio --test schema
sheetbend check studio --test tools
sheetbend check studio --test vision
```

No probe edits your configuration. A successful synthetic schema/tool/vision
observation is not a general accuracy or enforcement guarantee. Reasoning defaults
remain provider-controlled until you declare the actual serving control, as
explained in [reasoning.md](reasoning.md).

## 5. Give an application gateway access

For a one-off application that reads OpenAI-compatible environment variables:

```bash
sheetbend run --source studio --adapter openai --application my-agent -- python agent.py
```

The application must use Chat Completions and the supplied model alias `selected`
(or read SHEETBEND_MODEL). `run` does not proxy Responses-only clients. See
[gateway.md](gateway.md) for a complete client example and the exact adapter contract.

For a stable endpoint shared with authorized clients:

```bash
sheetbend serve
```

The listener is `http://127.0.0.1:8765/v1`; the example exports `studio-chat`.
Keep it in the foreground or manage it through your existing service manager.
In another terminal, `sheetbend top` shows admitted gateway inference requests.

For native decision models, add the real model entry with `system_one=true`,
`chat=false` as appropriate, and a granted gateway route. See
[system-one.md](system-one.md). For a laptop, additional servers, SSH, or HTTPS,
continue with [networking.md](networking.md).
