# MindIE-LLM 快速开始

本指南说明如何安装支持 MindIE-LLM 的 UCM、应用所需 Python 模块补丁、配置 `mindie_llm`，并在昇腾平台启动使用 UCM KV cache 后端的 `mindie_llm_server`。

## 前提条件

- MindIE-LLM 2.3.0
- Python >= 3.10
- 已安装 Ascend runtime/toolkit，且当前环境能够使用。详情参见 [MindIE-LLM 官方文档](https://gitcode.com/Ascend/MindIE-LLM/blob/dev/docs/zh/user_guide/quick_start/quick_start.md)。

## 第 1 步：安装 UCM

在[安装](../installation.md)中检查是否提供明确标记的 MindIE 制品。若没有，请按[从源码构建](../../developer-guide/build_from_source.md#mindie-llm-ascend-platform)的 MindIE-LLM 小节操作，在构建前设置 `UCM_ENABLE_MINDIE=1` 和匹配的 `UCM_CXX11_ABI`。普通 vLLM Wheel/Image 不代表包含 MindIE 集成。

开发和定制也可以采用[从源码构建和安装 UCM](../../developer-guide/build_from_source.md)的方式。

## 第 2 步：准备 UCM 配置

UCM 通过打补丁后的 MindIE-LLM 模块提供 Prefix Cache 集成。可以使用包内的 `ucm/integration/mindie/ucm_config.json`，也可以提供自己的配置文件。

最小示例：

```json
{
  "storage_backends": ["/path/to/kvcache"],
  "mindie_config_path": "/usr/local/lib/python3.11/site-packages/mindie_llm/conf/config.json",
  "block_elem_size": 2
}
```

将其保存为 `ucm_config.json`，并替换为实际环境中的路径。

注意以下行为：

- 安装 UCM 后，默认在首次导入 `mindie_llm` 时应用补丁。
- hook 将修改后的 `uc_utils.py`、`unifiedcache_mempool.py` 和 `prefix_cache_plugin.py` 复制到已安装的 `mindie_llm` 包中。
- MindIE 专用镜像可能在镜像构建时应用补丁。请核实其启动行为，不能假定任意 UCM 镜像都包含此集成。

## 第 3 步：启动带 UCM 的 MindIE-LLM

定位已安装的 `mindie_llm` 包：

```bash
python -c "import mindie_llm, os; print(os.path.dirname(mindie_llm.__file__))"
```

然后找到该目录下的 `conf/config.json`。

确保服务用户可以读取配置文件，例如：

```bash
chmod 640 <path-to-mindie_llm>/conf/config.json
```

在 `config.json` 的 `BackendConfig` 下新增或更新 `kvPoolConfig`：

```json
"BackendConfig": {
  "kvPoolConfig": {
    "backend": "unifiedcache",
    "configPath": "/path/to/your/ucm_config.json",
    "asyncWrite": true
  }
}
```

同时设置服务 IP、端口、模型路径及 MindIE-LLM 要求的其他配置。

运行 `mindie_llm_server` 启动服务。以下日志表示服务启动成功：

```bash
Daemon start success!
```

启动后检查 MindIE-LLM 日志，确认成功加载 `unifiedcache` 后端。

## 验证服务与外部缓存 { #verify-the-service-and-external-cache }

确认日志中包含 `[UC]: Initialize unifiedcache success.` 以及预期的后端路径。根据服务配置使用正确的 TLS 和鉴权设置，访问 MindIE 服务地址的 `/v1/models`，再通过其支持的推理端点发送补全请求。

验证缓存时，使用长度超过数个 KV 块的相同提示词。等待异步写入完成，确认存储目录内存在持久化 KV 文件。正常停止服务、保留存储，以相同的模型和缓存块布局重启，再发送相同提示词。在诊断运行前设置 `MINDIE_UC_TIME_STAT=1`，以显示适配器各项操作的耗时；确认首次运行有成功的 `put` 活动，第二次运行有 `get` 活动，且不存在 dump/load 异常。这样可将外部缓存复用与引擎内存中的前缀缓存区分开。

健康响应成功或第二次请求变快都不足以单独证明外部命中。应结合 MindIE 服务日志、缓存统计和 UCM 操作日志判断；vLLM Prometheus 指标示例不适用于 MindIE 端点。参见[故障排查](../../reference/troubleshooting.md)。完成后停止测试服务，并仅处理本次专用测试缓存目录。

## 故障排查

### MindIE-LLM 代码未应用补丁

- 确认安装前已设置 `UCM_ENABLE_MINDIE=1`。
- 确认安装前正确设置了 `UCM_CXX11_ABI=0` 或 `1`。
- 重新安装 UCM。
- 确认已安装 `mindie_llm`。

### 找不到 `configPath`

- 使用绝对路径。
- 确保服务用户有文件读取权限。

### 服务已启动，但 UCM 后端未启用

- 核对 `BackendConfig.kvPoolConfig`。
- 检查 MindIE-LLM 启动日志。
