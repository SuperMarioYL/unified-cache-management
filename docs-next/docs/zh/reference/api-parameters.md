# 集成 API

UCM 接入推理引擎的 KV cache 生命周期。HTTP 路由、鉴权、请求和响应格式以及模型生成均由推理引擎负责。UCM 不提供独立的 OpenAI 兼容 HTTP 服务，也不要求在补全请求中添加专有字段。

## vLLM 与 vLLM-Ascend

通过引擎的 `--kv-transfer-config` 传入以下字段：

| 字段 | 取值或含义 |
| --- | --- |
| `kv_connector` | `UCMConnector` |
| `kv_connector_module_path` | `ucm.integration.vllm.ucm_connector` |
| `kv_role` | 单个服务实例同时保存和加载缓存时使用 `kv_both` |
| `kv_connector_extra_config.UCM_CONFIG_FILE` | 引擎可读取的 UCM YAML 文件绝对路径 |

启动引擎前设置 `ENABLE_UCM_PATCH=1`，启用 UCM 运行时 patch hook。该变量不会选择 CUDA/Ascend 后端，也不会安装依赖。[安装选择器](../user-guide/installation.md)提供匹配的后端包。`PLATFORM` 是源码构建选项，安装发布 Wheel 时不能用它代替 backend extra。

YAML 根节点控制 connector 级行为，如 `use_layerwise`、`enable_event_sync` 和指标采集。`ucm_connectors[].ucm_connector_config` 配置 Store 参数，如 `storage_backends`、`timeout_ms` 和 `store_health`。参见[配置参数](config-parameters.md)和 [vLLM 快速开始](../user-guide/quick_start/quickstart_vllm.md)。

## SGLang

设置 `--hicache-storage-backend dynamic`，并通过 `--hicache-storage-backend-extra-config` 传入 `backend_name`、`module_path`、`class_name`、`interface_v1` 和 `kv_connector_extra_config`。完整示例参见 [SGLang 快速开始](../user-guide/quick_start/quickstart_sglang.md)。适配类是 `ucm.integration.sglang.unifiedcache_store` 中的 `UnifiedCacheStore`，要求 `page_first` 主机内存布局，以及零拷贝 `batch_get_v1` / `batch_set_v1` API。

未内联提供 `kv_connector_extra_config` 时，可用 `UNIFIEDCACHE_CONFIG_FILE` 指向包含该配置的 YAML 文件。这是 SGLang 适配器的输入，与 vLLM JSON 字段 `UCM_CONFIG_FILE` 不同。由于 SGLang 已负责主机缓存，适配器会将管线设为 `Posix`。

## MindIE-LLM

将 `BackendConfig.kvPoolConfig.backend` 设为 `unifiedcache`，`configPath` 设为 UCM JSON 配置的绝对路径。`asyncWrite` 控制 MindIE 异步缓存写入。MindIE 专用构建会在导入时为 Python 集成模块应用补丁；构建标志和服务配置参见 [MindIE 快速开始](../user-guide/quick_start/quickstart_mindie_llm.md)。这里的 `storage_backends` 是 JSON 列表，而 vLLM/SGLang 适配器使用冒号分隔的路径字符串。

## Store 扩展接口

`UcmKVStoreBaseV1` 定义 Store 侧的 Python 接口。Connector 工厂通过注册的 Store 名称及可选模块路径解析实现。Pipeline Store 实现查询、预取、异步 `load` / `dump`（以及基于地址的 `load_data` / `dump_data`），再通过 `check` / `wait` 确认完成。提交任务并不代表数据已经持久化或可以复用。

这些是内部集成接口，不是通用的稳定 HTTP 缓存 API。新增后端前，请阅读[扩展 Store](../developer-guide/extending-store.md)和[架构](../developer-guide/architecture.md)。
