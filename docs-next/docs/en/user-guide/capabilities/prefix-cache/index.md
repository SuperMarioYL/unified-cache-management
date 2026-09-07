# Prefix Cache

## Prefix Cache: A Fundamental Acceleration Component for KVCache and Its Architectural Considerations in Large Language Model Inference

As the simplest and most fundamental acceleration feature of KVCache, Prefix Cache has achieved industry-wide consensus.
With the expanding application scope of large language models (LLMs), the growth of sequence lengths, and the
proliferation of Agent-based applications, the performance gains of Prefix Cache become even more pronounced.

The core performance metric of Prefix Cache is the hit rate, and capacity can improve reuse when the workload contains repeated prefixes.
The result depends on request routing, eviction, prefix distribution, and storage latency;
capacity alone does not guarantee a particular hit rate. In terms of input/output (IO) characteristics, Prefix
Cache primarily demands bandwidth-intensive IO, making it well-suited for storage on Solid-State Drives (SSDs).

Prefix Cache can leverage diverse storage media, including Dynamic Random-Access Memory (DRAM), SSDs, and dedicated
storage systems (e.g., DeepSeek's 3fs, a storage system specifically developed for KVCache). The fundamental design
philosophy involves constructing a **multi-level cache** hierarchy using DRAM, local SSDs, and remote storage.

In practice, the implementation of this hierarchy can be roughly categorized into two architectural directions:

- **Decentralized Architecture**: KVCache is deployed in an isolated manner for each inference instance, with each
  KVCache partition belonging to a distinct inference instance (or server). This distributed KVCache deployment is
  typically paired with upper-layer KVCache-aware affinity scheduling. The goal of such scheduling is to route
  inference requests to instances with higher KVCache hit rates, thereby maximizing overall system performance.

- **Centralized Architecture**: KVCache is stored in a centralized external storage system and shared across all
  computing nodes. This architecture features inherent simplicity; DeepSeek's 3fs adopts this design paradigm, and
  the Prefix Cache module in UCM also tends to prioritize this centralized approach.

## Storage Backends

| Backend | Role | Guide |
| --- | --- | --- |
| Pipeline Store | Composes stages; `Cache\|Posix` combines host buffering with persistent filesystem storage | [Pipeline Store](pipeline.md) |
| NFS Store | Legacy NFS backend and its original recipe; use `Cache\|Posix` for the current filesystem path | [NFS reference](nfs.md) |
| DS3FS Store | Integrates DeepSeek 3FS storage | [DS3FS reference](ds3fs.md) |
| Mooncake Store | Uses a Mooncake memory pool, optionally with Posix persistence | [Mooncake reference](mooncake.md) |
| Compress Store | Adds a compression stage with its own quality and CPU-cost considerations | [Compression reference](compress.md) |

Start with [Pipeline Store](pipeline.md) and the
[engine quickstart](../../quick_start/index.md). The other recipes retain their
original requirements and reported performance; choose only a backend present
in your UCM build and verify external reads and writes in your environment.
