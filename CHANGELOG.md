# 变更记录

本文记录公开版本和主分支的重要变化。内部实现细节以 Git 提交为准；尚未发布的内容统一放在“未发布”。

## 未发布

### 新增

- 完全只读的 `doctor --json`，检查 SQLite、控制端点、日志目录和 Windows 文件信息能力。
- Windows GitHub Actions：Python 3.10/3.13 回归、`compileall` 和固定版本 Ruff 检查。
- 私密漏洞报告策略、脱敏 Bug/Feature 模板和公开架构/CLI 文档。

### 改进

- 无排除规则时跳过不必要的路径匹配，保留顺序扫描并降低默认扫描开销。
- 将设置表单、设置窗口、趋势几何和 Treemap 布局逐步提取为可独立测试的职责。
- `doctor` 的数据库路径严格使用 `mode=ro` 与 `query_only`，不初始化、迁移、修复或写入正式数据库。

## v0.8.1 — 2026-08-31

源码检查点：[`00d7da3`](https://github.com/Ran-HJ/disk-space-growth-monitor/commit/00d7da3)

- schema v3 保存完整目录指标，程序重启后仍可深层浏览已保存快照。
- 增加排除规则、当前/历史统一查找筛选、最大文件和稳定分页。
- 增加只读迁移建议，保守排除系统、应用管理、硬链接、云占位和已变化对象。
- 修复高 DPI 下的快照操作区布局，并把 GUI 默认搜索改为有界子串模式。
- 包含 v0.8.0 的统计正确性能力：逻辑大小、分配大小、稳定文件身份、硬链接和唯一占用。

该版本已完成用户可视验收，但没有单独创建 GitHub Release；v0.8 系列将在 v0.8.2 工程化与最终验收完成后统一关闭。

## [v0.7.5] — 2026-08-27

- 设置窗口与主窗口生命周期分离，托盘打开设置时不再恢复主窗口。
- 增加 Per-Monitor V2 DPI 感知，统一字体、尺寸和高 DPI 布局。

## [v0.7.4] — 2026-08-25

- 统一 CLI 标准输出与标准错误为 UTF-8，修复中文 JSON 解码问题。

## [v0.7.3] — 2026-08-25

- 增加按进程或系统内存压力触发的自动低内存模式。
- 增加 Windows 托盘和人工优先的模式切换策略。

## [v0.7.2] — 2026-08-25

- 增加本地 Agent CLI、统一 JSON 协议和当前用户范围内的命名管道控制。
- GUI 未运行时，历史查询使用 SQLite 只读连接。

## [v0.7.1] — 2026-08-25

- 加固扫描、存储、迁移、单实例和发布构建流程。
- 固定 PyInstaller 构建依赖并增加隔离 EXE 冒烟。

## [v0.7.0] — 2026-08-23

- 增加手动低内存模式，在释放扫描数据后保留容量采样和历史查看。
- 明确冷启动、模式切换、补扫参考和低内存关闭语义。

## [v0.6.3] — 2026-08-23

- 建立公开发布基线：会话快照、增长来源、Treemap、趋势、历史快照和安全取消。

[v0.7.5]: https://github.com/Ran-HJ/disk-space-growth-monitor/releases/tag/v0.7.5
[v0.7.4]: https://github.com/Ran-HJ/disk-space-growth-monitor/releases/tag/v0.7.4
[v0.7.3]: https://github.com/Ran-HJ/disk-space-growth-monitor/releases/tag/v0.7.3
[v0.7.2]: https://github.com/Ran-HJ/disk-space-growth-monitor/releases/tag/v0.7.2
[v0.7.1]: https://github.com/Ran-HJ/disk-space-growth-monitor/releases/tag/v0.7.1
[v0.7.0]: https://github.com/Ran-HJ/disk-space-growth-monitor/releases/tag/v0.7.0
[v0.6.3]: https://github.com/Ran-HJ/disk-space-growth-monitor/releases/tag/v0.6.3
