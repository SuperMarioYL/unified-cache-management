# Prefix Cache

## 前缀缓存与大模型推理架构

Prefix Cache 是 KVCache 复用的基础能力。大模型应用的序列长度不断增长，多轮对话和 Agent 应用经常重复使用相同前缀，使前缀缓存具有更多复用机会。

命中率是 Prefix Cache 的核心指标。当工作负载存在重复前缀时，增加容量可以扩大复用空间，但最终效果还取决于请求路由、淘汰策略、前缀分布和存储延迟，容量本身不能保证特定命中率。前缀缓存通常需要较高的 I/O 带宽，因此可以使用 SSD 等介质存储。

Prefix Cache 可以使用 DRAM、SSD 和专用存储系统（例如 DeepSeek 3FS），通过主机内存、本地 SSD 与远程存储构建**多级缓存**。

实践中主要有两种架构方向：

- **分散式架构**：每个推理实例或服务器拥有独立的 KVCache 分区。通常结合上层 KVCache-aware 亲和调度，将请求路由到更可能命中缓存的实例。
- **集中式架构**：将 KVCache 放在集中式外部存储中，由计算节点共享。DeepSeek 3FS 采用这种设计，UCM 的 Prefix Cache 也优先考虑集中共享方式。

## 存储后端 {#storage-backends}

| 后端 | 职责 | 指南 |
| --- | --- | --- |
| Pipeline Store | 组合多个阶段；`Cache\|Posix` 将主机 buffer 与持久化文件系统连接起来 | [Pipeline Store](pipeline.md) |
| NFS Store | 旧版 NFS 后端及其原有配置；当前文件系统接入使用 `Cache\|Posix` | [NFS 参考](nfs.md) |
| DS3FS Store | 接入 DeepSeek 3FS 存储 | [DS3FS 参考](ds3fs.md) |
| Mooncake Store | 使用 Mooncake 内存池，可组合 Posix 持久化 | [Mooncake 参考](mooncake.md) |
| Compress Store | 增加压缩阶段，需要评估精度影响和 CPU 开销 | [压缩参考](compress.md) |

先阅读 [Pipeline Store](pipeline.md)和[引擎快速开始](../../quick_start/index.md)。其他后端示例保留其原有要求和性能报告；请确认后端已包含在当前 UCM 构建中，并在实际环境验证外部读写。
