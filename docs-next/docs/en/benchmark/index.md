# Benchmark

Measure UCM with a fixed model, engine, workload, and storage configuration.
Report service readiness, actual external-cache reuse, and performance as
separate results. A warm engine-memory hit is not an external-store benchmark.

## Existing tools

| Tool | Use | Entry point |
| --- | --- | --- |
| Trace Replay | Replay timestamped requests or generate a workload from a dataset; report TTFT, TPOT, ITL, throughput, and end-to-end latency | `benchmarks/trace_replay.py` and its repository README |
| POSIX AIO | Characterize the storage path independently of model serving | [POSIX AIO guide](../toolkit/user/posix-aio.md) |
| UCM Metrics | Correlate hits, storage reads/writes, and errors with a serving run | [Metrics](../user-guide/observability/metrics.md) |
| Trace Mode | Record request metadata for workload analysis | [Trace Mode](../user-guide/diagnostics/trace-mode.md) |

From a UCM checkout, prepare the vLLM benchmark module required by Trace Replay
and set `BENCHMARK_PATH` to that directory. Pin it to the engine environment you
are testing. Then run against an already-running compatible server:

```bash
export BENCHMARK_PATH=/path/to/vllm/benchmarks
python benchmarks/trace_replay.py \
  --model /models/your-model \
  --backend vllm \
  --trace-path /data/conversation_trace.jsonl \
  --trace-mode trace \
  --host 127.0.0.1 \
  --port 7800 \
  --save-result \
  --save-prompts
```

The script depends on vLLM benchmark APIs; consult its `--help` and the
[Trace Replay README](https://github.com/ModelEngine-Group/unified-cache-management/blob/a336d69bc03a550d44bee3df9da7664e9edfe3a7/benchmarks/README.md)
for supported arguments, trace format, and dependencies. The example requires
a user-supplied trace file; it does not include a fabricated measurement.

## Compare runs

Record the exact UCM revision, engine and device-runtime versions, model and
tokenizer revision, hardware, storage/mount options, cache capacity, block size,
parallelism, prompt/output lengths, concurrency, and dataset or trace source.

1. Run the same workload without UCM to establish full-compute behavior.
2. Enable UCM, populate external storage, and record cache write completion.
3. Restart the serving processes while keeping storage and replay the workload
   to separate external reuse from engine-memory reuse.
4. Record external-hit tokens, load/dump activity and errors alongside latency
   percentiles, completed requests, throughput, and answer correctness.

Report cold and warm runs separately. Keep the same generation settings and
traffic schedule, and disclose any changed cache or storage configuration.

## Historical reports

These are source reports retained with their recorded environments. They have
not been rerun as part of this documentation migration.

- [GLM-5.1 on four Atlas A3 nodes](glm-5.1-a3-4node-pd.md): recorded
  vLLM-Ascend 0.18.0rc1 and UCM v0.17.0 PD deployment.
- [Pipeline Store](../user-guide/capabilities/prefix-cache/pipeline.md#historical-performance-report):
  QwQ-32B and DeepSeek-R1-AWQ with an 80% SSD-hit workload.
- [NFS](../user-guide/capabilities/prefix-cache/nfs.md),
  [DS3FS](../user-guide/capabilities/prefix-cache/ds3fs.md), and
  [compression](../user-guide/capabilities/prefix-cache/compress.md):
  backend-specific reports with the limitations recorded on each page.
