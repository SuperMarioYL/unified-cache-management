# PD分离调度方案

三篇文档分别说明整体功能规划、Router 调度和 Connector 实现设计。新增能力仍待代码实现与设备验证。

- [调度方案](%E8%B0%83%E5%BA%A6%E6%96%B9%E6%A1%88.md)：背景、功能 A～F、前置性能采集及远期规划。
- [Router 实现方案](%E6%96%B9%E6%A1%88%E5%AE%9E%E7%8E%B0%E2%80%94%E2%80%94Router.md)：请求选路、P/D 派发、会话接续及参数配置。
- [Connector 实现方案](%E6%96%B9%E6%A1%88%E5%AE%9E%E7%8E%B0%E2%80%94%E2%80%94Connector.md)：原生 Connector 继承、历史恢复、部分 KV 传输与逐层处理。

`img/` 包含文中使用的高清 PNG 和同名 Excalidraw 源文件；源图可下载后在 Excalidraw 中编辑。
