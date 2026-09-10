# Reasoning: portable intent, explicit deployment controls

Sheetbend binds a reasoning choice to an LLM connection. LLM still owns prompts,
conversations, schemas, response parsing, and streaming. No model names are matched
and no prompts are modified to switch thinking on or off.

This feature is additive in Sheetbend 0.4.0. Keep `schema_version = 2` in config.
Old config files continue to validate, and no user config is modified. Each model
without a reasoning declaration gets `control="unknown", default="provider"`.
Restart existing Python kernels after installing new code and reopen registries
when their configuration changes.

## Application interface

```python
from sheetbend import Registry

source = Registry.from_file().source("laptop", allowed_scopes={"local"})
plan = source.resolve_reasoning("off")
print(plan)
print(plan.to_dict())

with source.connect(reasoning="off", application="jupyter", tool="reasoning-test") as model:
    response = model.prompt("Explain a hash table in one paragraph.", stream=False)
    text = response.text()

print(text)
```

The returned model remains an LLM model, not a Sheetbend prompt wrapper.
`source.resolve_reasoning(reasoning, model="configured-model-id")` is an offline
public preflight step. It requires a configured model and returns a frozen
`ReasoningPlan`. `sheetbend schema --name reasoning-plan` describes its serialized
form. A plan records intent and request parameters, not an observation of the server.

| Connection choice | Meaning |
| --- | --- |
| Omitted / `None` | Use this model's configured `reasoning.default`. |
| `"provider"` | Send no Sheetbend reasoning override, even if config has another default. |
| `"off"` | Require a declared non-thinking mode or a declared fixed-off model. |
| `"on"` | Require an explicitly declared enabled mode. |
| `"low"`, `"medium"`, `"high"` | Require that exact declared effort choice; no approximation. |

The precedence is **caller choice, otherwise model default, otherwise provider
behavior**. `off` is not a latency guarantee. `low` is not `off`. Effort names are
not comparable numerical compute budgets across different models. `on` is an
explicit enabling preset, not a claim of a particular effort level or intelligence.

Unsupported choices fail with `SelectionError` before credentials, integration
imports, runtime admission, or network traffic. Unknown support must be declared,
not guessed. A source that cannot satisfy the requested mode is not silently
replaced. All reasoning modes still share the same source request/concurrency budget.

For portable applications, put deployment tuning in config and call `connect()`.
When a task genuinely requires a non-thinking response mode, call
`connect(reasoning="off")`: a failed selection is preferable to silently running
an expensive always-thinking model. When reasoning is useful, request `"on"`;
request a named effort only when the application deliberately needs that contract.

## Model declarations

### Ollama Qwen3.5 through Chat Completions

Add this table alongside the existing model capabilities, not inside them:

```toml
[sources.laptop.models."qwen3.5:4b".reasoning]
control = "reasoning_effort"
default = "off"
values = { off = "none", on = "medium" }
```

This sends `reasoning_effort="none"` for `off` and `"medium"` for `on`. The example
uses on/off semantics, not three invented quality tiers for a boolean-style
thinking toggle. Its `default="off"` affects every ordinary Sheetbend connection
and generation probe for this model until explicitly overridden.

Ollama documents `reasoning_effort` on its OpenAI-compatible Chat Completions
endpoint. Its compatibility implementation translates `none` to a disabled
thinking value; this depends on the served model supporting the mode. This is not
the native Ollama `/api/chat` request field `think`.

References: [Ollama OpenAI compatibility](https://docs.ollama.com/api/openai-compatibility),
[compatibility implementation](https://github.com/ollama/ollama/blob/main/openai/openai.go),
[Ollama thinking controls and model limitations](https://docs.ollama.com/capabilities/thinking).

### An endpoint with actual effort levels

```toml
[sources.work.models."your-served-model".reasoning]
control = "reasoning_effort"
default = "low"
values = { on = "medium", low = "low", medium = "medium", high = "high" }
```

There is deliberately no `off` entry here. `connect(reasoning="off")` fails rather
than substituting the smallest effort. Only add `off="none"` when that served
model really supports it. For the `on` preset, the schema accepts the native
values `minimal`, `low`, `medium`, `high`, `xhigh`, or `max`; it never picks one on
the caller's behalf. `low`, `medium`, and `high` map only to their same-named native
values. This version intentionally does not normalize numerical token budgets or
add generic provider-option dictionaries.

OpenAI documents that available effort values and defaults depend on the model.
The fact that a request field exists in a client library does not establish support
for a particular endpoint/model combination.

Reference: [OpenAI reasoning effort](https://developers.openai.com/api/docs/guides/reasoning).

### Qwen-style chat-template toggle

For a compatible deployment whose chat template honors `enable_thinking`:

```toml
[sources.work.models."your-served-model".reasoning]
control = "chat_template_kwargs"
default = "off"
```

`off` produces `{"chat_template_kwargs": {"enable_thinking": false}}` in the JSON
request body; `on` changes the boolean to `true`. The adapter uses the SDK's
`extra_body` mechanism, not a new endpoint or an alternate inference implementation.
No `values` table is accepted because this control is precisely an on/off switch.
Low/medium/high fail rather than becoming `true`.

The Qwen3.5 model card documents this control for compatible self-hosted serving;
vLLM documents the corresponding template parameter. Other families may use
`thinking`, top-level `enable_thinking`, or another control. Do not label those as
this control. A further supported translation should be added deliberately when
there is a concrete need, not through arbitrary request-body configuration.

References: [Qwen3.5-4B model card](https://huggingface.co/Qwen/Qwen3.5-4B),
[vLLM reasoning outputs](https://docs.vllm.ai/en/latest/features/reasoning_outputs/).

### Fixed behavior or unknown support

```toml
# A deployed model with no separate thinking phase:
[sources.work.models."non-thinking-model".reasoning]
control = "fixed-off"

# A model whose reasoning cannot be enabled/disabled by this connection:
[sources.work.models."always-thinking-model".reasoning]
control = "fixed-on"
```

Fixed-off satisfies `off` without sending an unsupported parameter. Fixed-on
satisfies `on` without a parameter and rejects `off` and named effort choices.
These are operator declarations about actual behavior, not inferences from model
names or absent capability flags. Neither accepts a `values` table.

An omitted table, or `control="unknown"`, preserves provider defaults but cannot
satisfy explicit reasoning requests. Declaring `default="off"` with unknown support
fails at configuration loading rather than pretending the desired mode is available.

## Native LLM option escape hatch

For an explicitly declared `reasoning_effort` control, LLM's native option remains
available when the connection is left at `reasoning="provider"`:

```python
with source.connect(reasoning="provider") as model:
    response = model.prompt(
        "Explain an index.",
        stream=False,
        options={"reasoning_effort": "none"},
    )
    print(response.text())
```

The native value must appear in the model's configured `values`. This example is
intentionally provider-specific. The offline connection plan does not include
subsequent native per-prompt overrides, so record those options separately in an
experiment. Do not use this escape hatch in code meant to swap between an
effort-based and a chat-template endpoint. A bound choice such as `reasoning="off"`
or a configured `default="off"` rejects an additional native effort option, even if
it happens to match. There is one controlling choice, not competing defaults.

LLM 0.35 exposes reasoning options when its Chat instance is constructed with
`reasoning=True`. The adapter does that only for the declared effort-based control.
For the chat-template control, it adds the specific request-body setting while
leaving the ordinary LLM options model intact. Sheetbend defines no Pydantic
model or alternate generation API.

Reference: [LLM 0.35 Chat implementation](https://github.com/simonw/llm/blob/0.35/llm/default_plugins/openai_models.py),
[LLM Python model options](https://llm.datasette.io/en/stable/python-api.html#model-options).

## Check the requested mode

```bash
sheetbend check laptop --test text --reasoning off
sheetbend check laptop --test stream --reasoning on
sheetbend check laptop --all --reasoning off
```

`Source.check(test="text", reasoning="off")` and
`Source.check_all(reasoning="off")` use the same resolver. All five generation
probes in `--all` use the choice; the catalog has no reasoning parameter. A
catalog-only check rejects `--reasoning`, rather than ignoring the argument.
An unconfigured diagnostic model has unknown reasoning support and does not borrow
another model's declaration. Configure it before demanding a reasoning mode.

A passing synthetic check means that generation worked under the requested wire
parameters. A server can ignore an unsupported field. A short answer, an empty
reasoning trace, and an HTTP 200 are not proof that internal thinking was disabled.
Check server documentation, benchmark the actual installation, and inspect any
provider-reported usage independently. Sheetbend does not certify hidden compute.
The existing diagnostic and activity schemas remain unchanged; retain
`source.resolve_reasoning(...).to_dict()` separately when saving an experiment.

Hiding reasoning output is different from disabling its generation. vLLM, for
example, documents `include_reasoning=false` as suppressing returned reasoning
while still generating those tokens. Sheetbend never uses output hiding as a
substitute for `off`.

## Notebook comparison

This small benchmark compares client-observed total latency, not pure GPU decode
speed. It excludes connection construction but includes admission waits, transport,
prompt processing, reasoning, and response consumption. It does not print while
timing. Provider output counts can include reasoning tokens and must not be treated
as visible-text token counts. Missing usage stays unknown.

```python
from time import perf_counter
import pandas as pd

source = Registry.from_file().source("laptop", allowed_scopes={"local"})
prompt = "Explain why database indexes improve lookup speed in approximately 150 words."
rows = []

for choice in ("off", "on"):
    plan = source.resolve_reasoning(choice)
    with source.connect(
        reasoning=choice, application="jupyter", tool=f"benchmark:{choice}",
    ) as model:
        # A warm-up for each mode is outside the timed trials.
        model.prompt("Reply with OK.", stream=False).text()
        for trial in range(1, 4):
            started = perf_counter()
            response = model.prompt(prompt, stream=False)
            text = response.text()
            elapsed = perf_counter() - started
            usage = response.usage()  # The response has been fully consumed.
            rows.append({
                "source": source.name,
                "model": source.default_model,
                "reasoning": plan.selected,
                "control": plan.control,
                "parameter": plan.parameter,
                "value": plan.value,
                "trial": trial,
                "seconds": elapsed,
                "input_tokens": usage.input,
                "output_tokens": usage.output,
                "reported_output_tok_s": (
                    usage.output / elapsed if usage.output is not None and elapsed > 0 else None
                ),
                "characters": len(text),
            })

bench = pd.DataFrame(rows)
bench
```

For a serious comparison, alternate or randomize mode order, record software and
model versions, control prompt/cache/context conditions, and avoid other endpoint
traffic. A short repeated prompt can benefit from caching. Compare answer quality
as well as speed. This example deliberately does not claim a measured speedup,
pure decode rate, or token-equivalent work across different reasoning modes.
