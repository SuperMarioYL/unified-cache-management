# Capabilities

Choose a capability according to the computation or transfer you need to
avoid. Availability depends on the engine, model, backend build, and runtime
version; see the [support matrix](../support-matrix/index.md).

| Capability | What it changes | Start here |
| --- | --- | --- |
| Prefix Cache | Reuses KV blocks for an identical token prefix across requests | [Prefix Cache and storage backends](prefix-cache/index.md) |
| Sparse Attention | Selects a subset of context KV data for attention computation | [Sparse Attention](sparse-attention/index.md) |
| PD Disaggregation | Runs prefill and decode separately and transfers their KV state | [PD Disaggregation](pd-disaggregation/index.md) |
| ReRoPE | Changes rotary-position handling for context extension | [ReRoPE](rerope.md) |

Start with [Installation](../installation.md) and an
[engine quickstart](../quick_start/index.md) before combining capabilities.
Advanced recipes retain their original version requirements and historical
results; they do not certify all combinations in a newer engine.

For implementation boundaries, read
[Capability principles](../../developer-guide/capability-principles.md).
