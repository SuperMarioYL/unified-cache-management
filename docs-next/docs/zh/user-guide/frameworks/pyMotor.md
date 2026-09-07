# pyMotor

**pyMotor**（MindIE-Motor）是面向昇腾平台的分布式推理服务框架，可在 Atlas 硬件上运行 vLLM 风格的 prefill/decode（PD）分离服务。集群由 Coordinator 和 Engine Pod 组成，通过 `user_config.json` 与 `deploy.py` 描述和部署。

pyMotor 为昇腾平台提供兼容 vLLM 的引擎，并支持手动扩缩容、主备切换、请求跟踪、PD 角色重调度及容器快照。

UCM 通过 `UCMConnector` / `UcmPipelineStore` 作为持久化 KV cache 后端接入 pyMotor。Prefill 阶段写入 KVCache，后续共享前缀的请求可以复用缓存，减少重复计算。

## 部署

安装和部署步骤以 MindIE-Motor 官方文档为准：

| 任务 | 指南 |
| --- | --- |
| 文档首页 | [MindIE-Motor 文档](https://mindie-motor.readthedocs.io/zh-cn/latest/) |
| 环境准备 | [官方指南](https://mindie-motor.readthedocs.io/zh-cn/latest/user_guide/environment_preparation/) |
| 快速开始 | [官方指南](https://mindie-motor.readthedocs.io/zh-cn/latest/user_guide/quick_start_motor/) |
| Kubernetes 部署 — PD 分离 | [官方指南](https://mindie-motor.readthedocs.io/zh-cn/latest/user_guide/deployment/k8s/pd_disaggregation_deployment/) |
| Kubernetes 部署 — PD 混合 | [官方指南](https://mindie-motor.readthedocs.io/zh-cn/latest/user_guide/deployment/k8s/pd_aggregation_deployment/) |
| 单独部署 Coordinator | [官方指南](https://mindie-motor.readthedocs.io/zh-cn/latest/user_guide/deployment/standalone/) |

## 使用 UCM 作为 KV cache 后端

参阅专门的集成指南：

- [pyMotor KV cache store 的 UCM 后端](https://mindie-motor.readthedocs.io/zh-cn/latest/user_guide/features/kv_cache_store/backend/ucm/)

指南介绍如何在 prefill 角色安装 UCM wheel、通过 `motor_deploy_config.storage` 挂载共享缓存存储、为 prefill 配置 `MultiConnector`（Mooncake 在前，`UCMConnector` 位于第 2 个位置），以及通过重复请求验证 KVCache 命中。
