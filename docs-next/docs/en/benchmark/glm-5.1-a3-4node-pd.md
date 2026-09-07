# Evaluate GLM-5.1 PD on Four Ascend Nodes

Use this experiment design to determine whether external prefix reuse improves
GLM-5.1 serving on a four-node Ascend allocation. The experiment must establish
model correctness, P-to-D transfer, and actual external-cache reuse before
comparing latency or throughput.

This page contains no measured results. The current Chart has no GLM-5.1
four-node profile, so model compatibility, memory fit, quantization, and the
parallel layout must be established for the selected image on your hardware.
A generic Qwen PD profile is not evidence that GLM will start with the same
arguments.

## Freeze the environment

Record the following before preparing the deployment:

| Area | Required record |
| --- | --- |
| Model | Weight and tokenizer revision, quantization format, loaded model path and served name |
| Software | UCM revision, engine and device-runtime versions, complete image digest, connector classes |
| Hardware | Node inventory, accelerator type/count/memory, CPU and host memory, network interfaces and links |
| Topology | Serving groups, P/D instances, workers per instance, per-role DP/TP/EP settings and device allocation |
| Storage | Backend, mount identity/options, capacity, buffer budget, cache namespace and retention method |
| Workload | Prompt source, token lengths, shared-prefix construction, output length, arrival schedule and generation settings |

Choose the image and Chart coordinates through
[Installation](../user-guide/installation.md). Preserve the rendered resources
and effective engine/UCM configurations with every run. Do not substitute an
unrecorded image tag between the baseline and the cache-enabled runs.

## Prepare a four-node topology

Express the deployment using `modelSpec.roles[]`, `pd.kvTransfer`, and
`unifiedcacheConfig`, following the
[Kubernetes workflow](../user-guide/frameworks/kubernetes/deploy.md).
The [parallelism guide](../user-guide/capabilities/pd-disaggregation/large-scale-ep.md)
explains the difference between instance replicas and workers.

Two candidate allocations illustrate the decision that must be made:

| Allocation | Engine Pods | Condition to verify |
| --- | --- | --- |
| Two single-node P instances and two single-node D instances | Four | Each role's model partition fits a single node and its selected transport supports the layout |
| One two-node P instance and one two-node D instance | Four | Each role has one worker, and global/local parallel sizes match the two-node execution group |

Neither is a prescribed GLM configuration. Select the one whose engine
configuration has passed model startup and answer validation. Account
separately for router/master resources, and inspect anti-affinity and device
requests to confirm the engine Pods occupy the intended four nodes.

Do not use the bundled `2p2-2d2` Qwen profile unchanged: it requests eight engine
Pods because each of its four role instances has a worker. Do not reduce that
profile's worker count without also reviewing its DP settings and serving mode.

For transport-based PD with UCM, the current Chart composes UCM on the Mooncake
producer and leaves decode as a transport-only consumer. Verify this effective
configuration, as described in
[Transport with UCM](../user-guide/capabilities/pd-disaggregation/distributed.md).

## Establish correctness and transfer first

1. Run a small, fixed set of representative prompts using the chosen model and
   generation settings; retain generated text or task scores as the reference.
2. Send the same prompts through the PD router with a cold cache. Confirm
   successful P-to-D transfer, expected output lengths, and equivalent task
   correctness. Inspect failures instead of excluding them from the result.
3. Enable UCM and verify completed prefill writes with a prompt spanning
   several cache blocks. Restart serving processes while preserving the store,
   then replay it and confirm prefill-side external hits and successful loads.
4. Include a prompt with a changed prefix. A changed request should not appear
   as an identical full-prefix hit merely because its length matches.

Byte-identical output is useful when the selected engine configuration is
reproducible. Otherwise define the task-level acceptance rule in advance and
use it consistently across all cases. Lower latency does not override a
correctness regression.

## Compare cache states with a fixed deployment

| Case | Preparation | What it measures |
| --- | --- | --- |
| UCM disabled | Retain PD transport; disable the Chart's UCM configuration and record patch environment settings | PD serving baseline |
| UCM cold | Enable UCM with a dedicated empty experiment store and fresh engine processes | Miss, compute, and save overhead |
| UCM externally warm | Populate the intended prefixes, verify write completion, restart engines while preserving storage | External reuse after removal of process-local cache state |
| UCM plus engine-memory warm | Repeat without restart | Combined cache behavior, reported separately |

Use isolated test storage for cold runs rather than deleting a shared cache.
A target prefix percentage is a workload input, not an observed hit ratio.
Record actual UCM-hit tokens and engine-memory-hit tokens for each request or
run, together with the token denominator used for the ratio.

Use identical prompt/output lengths, traffic schedules, generation settings,
transport configuration, and P/D allocation. Repeat runs under comparable
background load and keep individual results; do not present only the best run.

## Generate traffic and retain evidence

The repository's `benchmarks/trace_replay.py` can replay a timestamped trace or
construct requests from trace metadata. It depends on vLLM benchmark APIs
provided through `BENCHMARK_PATH`; first verify its `--help` and a small run in
the selected engine environment. The [Benchmark overview](index.md) describes
its invocation and result collection.

For the final run, target the kthena-router gateway, not a prefill/decode
engine Service. Set `--model` to the served model name and `--tokenizer` to the
pinned tokenizer location. Retain the trace, generated prompts when requested,
client output, engine logs, and metrics exports under the same run identifier.
Synthetic trace-generated text tests serving behavior; use meaningful task
prompts separately for answer validation.

Collect client TTFT percentiles, TPOT, completed requests, token throughput,
request failures, and actual output-token counts. Collect prefill UCM hits,
load/save activity, and storage errors over the same time window. Where
available, retain router queueing, engine-stage, and transport timings. If a
stage is not instrumented, label it unavailable rather than deriving a precise
duration from end-to-end latency alone.

## Report a conclusion that the evidence supports

The result should connect cache state to work avoided: external hits increased,
loads completed, prefill compute decreased, and client latency or throughput
changed under equivalent traffic. Report absolute values and variability as
well as relative differences.

If external reuse succeeds but TTFT does not improve, inspect store-read cost,
transfer waiting, and queueing before tuning more parameters. If requests fail
or output quality changes, resolve that failure before comparing speed. A
completed report should include enough environment and workload detail for a
second operator to reproduce the experiment; it should not reuse numbers from
a different model, image, topology, or earlier documentation page.
