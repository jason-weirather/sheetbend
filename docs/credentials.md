# Credential references

Sheetbend resolves credentials; it does not maintain an encrypted secret database.
`config.toml` contains references, not passwords. The same reference contract is
used by Python connections, gateway upstreams, and gateway client authentication.
Inspection and configuration validation never resolve credentials.

## Supported providers

```toml
[credentials.omlx]
provider = "file"
path = "secrets/omlx.txt"

[sources.studio.auth]
type = "bearer"
credential = "omlx"
```

Use exactly one of `auth.credential` or the existing `auth.env`. Transport stays
separate: `type = "bearer"` sends Authorization, and `type = "header"` sends the
configured `header`, such as `api-key`. `none` and `bearer-placeholder` take neither
credential reference. A named credential may be shared by multiple sources; that
does not merge their source-level rate buckets.

### Plain text: one secret per file

`secrets/omlx.txt` contains the API key itself, with one optional final newline.
There is no `export`, `KEY=`, shell expansion, command substitution, or evaluation.
Leading/trailing spaces and multiple lines are rejected rather than silently fixed.
Paths expand `~`; relative paths resolve against the selected config's directory,
not whichever project happened to launch the process.

Files must be owned by the executing user, have no group/other permission bits,
be regular files with a single hard link, and not be symlinks. Use `0600` or `0400`.
The check is on the opened descriptor. Reads are bounded to 64 KiB and credentials
to 8 KiB. Keep the containing directory private (`0700`), outside repositories,
shared folders, and cloud-synced project directories. Ancestor directories and OS
ACLs are not a separately audited security boundary.

**This is plaintext storage, not encryption.** File permissions do not protect
against another program running as your user, backups containing the file, or
someone with administrative access. Use it deliberately for development keys.

The [Studio quickstart](studio-quickstart.md) creates files with hidden input and
exclusive creation, so existing keys are not overwritten or put into shell history.

### Plain TOML: several named values in a private file

A separate, owner-only `secrets.toml` can contain a flat mapping:

```toml
omlx = "PASTE_THE_EXISTING_OMLX_KEY_HERE"
other_service = "ANOTHER_DEVELOPMENT_KEY"
```

Reference exactly one top-level key:

```toml
[credentials.omlx]
provider = "file"
path = "secrets.toml"
key = "omlx"
```

The presence of `key` selects TOML parsing. Dots in `key` are literal characters,
not nested traversal. Missing or non-string values fail. No `.env` file is searched
or sourced, and no file's complete contents are exported into an environment.

### Environment variables

Both forms remain valid. The named form is useful for gateway client tokens:

```toml
[credentials.omlx]
provider = "env"
name = "OMLX_API_KEY"

# Existing shorthand, inside a source:
# auth = { type = "bearer", env = "OMLX_API_KEY" }
```

Missing, empty, non-ASCII, or HTTP-header-unsafe values fail. There is no fallback
to OPENAI_API_KEY, a password manager, or another provider.

### macOS Keychain

```toml
[credentials.omlx]
provider = "keychain"
service = "ai.omlx.inference"
account = "YOUR_MAC_ACCOUNT_NAME"
```

Create a generic-password item with the matching service and account using your
trusted Keychain management workflow. The `account` string is literal: TOML does
not interpolate `$USER`. Sheetbend retrieves it with the fixed command
`/usr/bin/security find-generic-password`, without a shell or putting a secret in
command arguments. It captures output privately and imposes a 15-second timeout.
It fails explicitly on non-macOS systems. Sheetbend does not create, unlock, or
change Keychain items, and does not loosen their access controls.

macOS may prompt for access. A background service needs an appropriate logged-in,
unlocked user session and approved access; test that service context separately.
See Apple's [Keychain access controls](https://support.apple.com/en-gb/guide/mac-help/kychn002/26/mac/26).

### Azure Key Vault

A native Azure provider is **deferred in 0.6**. `provider = "azure"` is rejected,
not treated as an environment variable or silently replaced. An approved external
vault launcher can populate an environment variable for `sheetbend serve` or
`sheetbend run`; the existing environment credential provider then consumes it.
Do not copy the old loader's padded/truncated encryption-key implementation into
Sheetbend. A future provider should use an explicitly selected Azure identity,
lazy secret retrieval, bounded failures, and this same named-reference contract.
No Azure SDK dependency is introduced in this release.

## Resolution, lifetime, and tests

`source.connect()` resolves on connection entry; catalogs and `source.decide()`
resolve per operation. A gateway resolves only its configured client tokens and
exported sources' credentials at startup. It holds them in process memory for that
gateway's lifetime. Rotate a key and **restart the gateway**; reconnect Python
clients. Neither file changes nor token revocation are automatically watched.

```bash
sheetbend check studio --test credentials
sheetbend dashboard                 # No secret reads or network work
sheetbend dashboard --probe         # Explicit access/catalog observations
```

Readable means the reference resolved to a header-safe string. It does **not** mean
the provider accepted the key. A readable credential and an unavailable catalog
are different statuses. Unused references remain unchecked. Probe results contain
no secret values. The existing explicit escape hatch `source.resolve_auth()` and
`registry.credentials.resolve(name)` return secrets to their caller: do not log them.

## Child-process boundary

`sheetbend run` injects a new gateway token, never the upstream secret. It does not
modify the parent environment. By default it inherits only ordinary path, locale,
terminal, temporary-directory, and Python/Conda environment settings. Additional
variables require explicit `--inherit-env NAME`; do not opt in upstream API keys
or cloud authentication variables casually. See [gateway adapters](gateway.md).

A child is not sandboxed. It can print its own token, read accessible files, or
use independent credentials. Gateway access control governs requests through the
gateway, not arbitrary network traffic from the machine.
