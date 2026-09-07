# Capability principles

UCM features reuse or move KV data in different ways. Keep their correctness
conditions separate when choosing an integration or designing a backend.

| Capability | Reuse or transfer decision | What to verify |
| --- | --- | --- |
| Prefix Cache | Reuse blocks whose token prefix and model-specific identity match | External hits, completed loads, and output correctness |
| Sparse Attention | Select context blocks according to the sparse algorithm | Algorithm/model compatibility, quality, and latency |
| PD Disaggregation | Transfer completed prefill KV state to a decode instance | Routing, cache geometry, transfer completion, and request continuity |
| ReRoPE | Change positional treatment for longer contexts | Model/patch compatibility and long-context quality |

Prefix Cache preserves the prefix block's content; it does not make arbitrary
cached documents interchangeable. Sparse Attention changes which context data
participates in attention and therefore needs its own quality evaluation. PD
transfer is part of a request's execution and must not be confused with reuse
of cache from an earlier request.

The [architecture](architecture.md) keeps engine scheduling and tensor-layout
knowledge in integrations, and backend I/O in Stores. A backend change should
respect that ownership instead of adding model-specific assumptions to storage.
Use the [capability guides](../user-guide/capabilities/index.md) for recipes and
the [benchmark guide](../benchmark/index.md) to record evidence.
