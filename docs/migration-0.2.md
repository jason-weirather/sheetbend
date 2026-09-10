# Migrating from Sheetbend 0.1 to 0.2

This is an intentional early-development schema/API update. Stop old clients and
restart notebook kernels after installation. Do not mix 0.1 clients (which have
independent in-process limits) with 0.2 clients and expect shared enforcement.

## Update the configuration

Change `schema_version = 1` to `schema_version = 2`. Move each source's old
capabilities into its default model's entry. For example, replace:

```toml
[sources.work.capabilities]
json_schema = true
```

with, for a source whose `default_model = "your-model"`:

```toml
[sources.work.models."your-model"]
capabilities = { json_schema = true }
```

Every source needs at least one model entry, including sources that previously
omitted capabilities entirely. An empty model table is sufficient to receive
schema defaults:

```toml
[sources.work.models."your-model"]
```

`default_model` must be one of the configured keys. Add an entry for every model
used in normal inference; unconfigured IDs are permitted only for explicit
checks. Shared connection fields and limits stay at the source level. Do not add
extra source entries solely to give different models different capabilities.

No automatic file migration or version-1 compatibility branch is included.
Sheetbend never edits a selected connection configuration behind your back.

## Selection changes

`Registry.source()` now permits local and institutional scopes by default, not
external. Explicitly add external when appropriate:

```python
source = registry.source("external", allowed_scopes={"external"})
```

To allow any defined scope, pass all three values explicitly. `allowed_scopes=None`
now raises instead of disabling restrictions. Organization checks are unchanged.
Offline CLI listing/inspection still shows every configured source.

The CLI requires `--allow-external` for external probes. `source.connect()` now
checks the selected model's capabilities and accepts `requires`, `application`,
and `tool`. Existing `with source.connect(): ...` usage still works when its source
and default model satisfy the updated contract. Inference remains native LLM.

## Shared runtime

File-backed registries using the same canonical config path and source name share
request limits across processes on one host. `Registry.from_dict()` has no durable
file identity and keeps its own registry namespace. Use a file for shared quotas.

Requests create a private host-local runtime ledger. Inspection/import does not.
Configure `SHEETBEND_RUNTIME_DIR` only when a suitable local directory is needed;
never use your NFS home. All participants must agree on the runtime directory.

The old internal `rate_limiter.py` has been removed, not retained as a competing
limiter. `Runtime().snapshot()` is the public activity inspection surface.

## Diagnostic documents

A single check now emits check-schema version 2, including `observed_at` and the
prior capability declaration (`declared`). Combined reports use their own version-1
check-report schema. Activity snapshots use the version-1 activity schema.

Run `sheetbend schema --name check`, `--name check-report`, or `--name activity`
for the complete contracts. Treat a passing probe as a dated observation, not a
promise of arbitrary capability enforcement.
