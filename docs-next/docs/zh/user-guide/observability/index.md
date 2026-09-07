# 可观测性

通过 UCM 指标监控和分析 KV cache 的运行情况。

## 主要能力

- **Prometheus 指标**：通过 vLLM connector 导出详细指标。
- **Grafana 仪表盘**：使用现有仪表盘查看指标趋势。
- **运行监控**：跟踪缓存命中率、延迟和吞吐量。

## 使用指南

- [指标](metrics.md)：配置 UCM 指标、Prometheus 和 Grafana。

## 参考

- [指标定义](metrics-reference.md)：指标目录及说明。
- [健康指标](health-metrics.md)：Store 探测计数与熔断器状态。
