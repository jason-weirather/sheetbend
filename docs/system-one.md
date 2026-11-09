# System One is a model capability and a native interface

```toml
[sources.studio.models."YOUR_DECISION_MODEL_ID"]
capabilities = { chat = false, streaming = false, system_one = true }
```

`system_one` defaults to false, like `vision`. `chat` defaults to true for existing
configurations; set it false for decision-only models. `system_one=true` declares
support for typed decisions at `POST <base_url>/systemone`. It does not mean a chat
model with thinking disabled, certify single-pass execution, or guarantee latency,
accuracy, or calibrated probabilities.

The implemented contract is the text/JSON `state` plus named `questions` subset of
the [TypeSafe System One HTTP API](https://api.typesafe.ai/docs). Supported question
types are `noul`, `choice`, and `score`. The shape is also used by oMLX's System One
endpoint; installed server/model support must be checked, not inferred from names.
No image assets, box questions, multipart, `/decisions` translation, or chat-based
emulation is added in this release.

## Python

```python
from sheetbend import Registry

source = Registry.from_file().source("studio", allowed_scopes=("local",))
result = source.decide(
    state="The user asks to list the current working directory.",
    questions={
        "read_only": {
            "type": "noul",
            "instructions": "The requested operation is read-only.",
        },
        "operation": {
            "type": "choice",
            "instructions": "Classify the operation.",
            "criteria": {"read": "Read existing state", "write": "Change stored state"},
        },
    },
    model="YOUR_DECISION_MODEL_ID",
    application="any",
    tool="classify-operation",
)
print(result["answers"])
```

`Source.decide()` checks the declaration before credentials or network work, uses
source auth/TLS/admission, and records a `system_one` request in `sheetbend top`.
It does not import the LLM extra. Use `Source.connect()` for chat, not
`connect(requires={"system_one"})`; the latter explicitly directs callers to the
native decision interface rather than returning the wrong kind of model.

Requests accept exactly `model`, `state`, and `questions`; state is a string,
object, or array. Each of 1-64 questions has a type and string instructions.
`noul` optionally takes true/false description criteria. `choice` takes 2-255
unique names as a list or description mapping. `score` takes an ordered list of
2-255 level descriptions. Providers may impose smaller limits.

Responses must answer every question with its matching type. Noul values and
option probabilities must be finite and in [0,1]. Choice options must match the
request; score distributions use zero-based string level keys. Distributions must
sum to approximately one (absolute tolerance 0.01), and scores must lie on the
requested scale. These are structural checks, not statistical calibration tests.
Provider response fields are retained. Only reported usage counts are recorded;
missing counts remain unknown rather than being guessed as zero.

## Gateway

Add a named route and explicitly grant it to a client:

```toml
[gateway.routes.decision]
source = "studio"
model = "YOUR_DECISION_MODEL_ID"

# Include "decision" in the intended gateway.clients.NAME.routes list.
```

Then post JSON to `/v1/systemone` with `model="decision"` and that client's gateway
bearer token. No arbitrary client-selected upstream model is permitted. `run` uses
`--adapter sheetbend` and exports the alias `selected` for the chosen decision model.

## Tests and catalog formats

```bash
sheetbend check studio --model YOUR_DECISION_MODEL_ID --test system_one
sheetbend dashboard --test system_one --source studio --model YOUR_DECISION_MODEL_ID
```

The diagnostic sends a fixed synthetic noul question, validates a typed probability,
and does not claim to benchmark correctness. An explicit `--test system_one` may
probe an undeclared model without changing config. `--all` uses the original six
checks for chat-capable models and adds System One only when declared; a
System One-only model runs the catalog plus the native decision check. Reasoning
flags apply to chat probes, not credentials, catalogs, or System One.

For oMLX and Sheetbend gateways the default `catalog_format="openai"` reads
`data[].id`. A TypeSafe deployment using `models[].name` can explicitly declare
`catalog_format="typesafe"` on the source. The catalog path remains
`<base_url>/models`. No alternate URL or catalog format is guessed.
