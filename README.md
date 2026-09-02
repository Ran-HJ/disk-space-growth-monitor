# C 盘空间增长监控器

[![Windows CI](https://github.com/Ran-HJ/disk-space-growth-monitor/actions/workflows/windows-ci.yml/badge.svg)](https://github.com/Ran-HJ/disk-space-growth-monitor/actions/workflows/windows-ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

一个面向 Windows 的本地磁盘空间监控工具。它通过容量采样、目录快照和会话对比回答两个问题：**空间什么时候发生了变化，以及变化主要来自哪里**。

程序只分析和记录，不会自动删除、移动、清理或修复用户文件。图形界面、SQLite 数据库和 Agent 控制接口均在本机运行，控制接口不监听网络端口。

> 当前源码版本为 **v0.8.1**，`main` 正在完成 v0.8.2 的工程化收尾。最新可下载的 GitHub Release 是 [v0.7.5](https://github.com/Ran-HJ/disk-space-growth-monitor/releases/latest)；v0.8.2 尚未进入发布候选阶段。

## 核心能力

- **增长定位**：记录分钟容量、启动基线和正常关闭快照，区分运行期间变化与未监控期间变化。
- **可视浏览**：使用 Treemap、增长树、趋势图和面包屑逐层查看空间分布。
- **历史分析**：深层浏览和比较已保存快照，支持路径、类型、扩展名、大小和日期筛选。
- **两种资源模式**：全功能模式执行目录扫描；低内存模式只保留容量采样和历史查看，可由用户、进程规则或内存压力切换。
- **明确的统计口径**：默认统计逻辑大小；可选精确模式区分分配大小、硬链接和去重后的唯一占用。
- **只读建议与诊断**：迁移建议不执行文件操作；`doctor` 只检查本地运行条件和数据库可读性。
- **本地自动化**：独立 CLI 通过当前用户的 Windows 命名管道控制 GUI，并提供稳定的 UTF-8 JSON 响应。

更完整的模块和数据边界见 [架构与开发说明](docs/architecture.md)，CLI 命令见 [CLI 使用说明](docs/cli.md)。

## 快速开始

### 使用已发布程序

从 [Releases](https://github.com/Ran-HJ/disk-space-growth-monitor/releases) 下载 GUI 程序和同版本 CLI。当前 GitHub Release 仍是 v0.7.5；仓库中的 v0.8.x 功能将在 v0.8.2 验收完成后统一发布。

### 从源码运行

需要 Windows 和 Python 3.10 或更高版本。运行时只使用 Python 标准库：

```powershell
git clone https://github.com/Ran-HJ/disk-space-growth-monitor.git
cd disk-space-growth-monitor
python run.py
```

Agent CLI 源码入口：

```powershell
python run_cli.py --version
python run_cli.py app status --json
python run_cli.py doctor --json
```

## 基本使用

1. 启动程序。全功能模式会建立目录基线；低内存模式只记录磁盘容量。
2. 在“空间分布”中浏览当前目录，在“空间变化”中查看增长或减少来源。
3. 需要新数据时重新扫描当前目录；扫描可以安全取消。
4. 使用带备注的手动快照标记清理前后等时刻，再到“快照历史”中对比。
5. 正常关闭可保存结束快照；快速或异常退出不会伪装成完整文件基线。
6. “迁移建议”只展示候选、风险和目标盘空间估算，实际操作仍由用户在 Windows 或原应用中完成。

数据默认保存在：

```text
%LOCALAPPDATA%\DiskGrowthMonitor\monitor.db
```

数据库升级备份和界面日志分别位于：

```text
%LOCALAPPDATA%\DiskGrowthMonitor\backups\
%LOCALAPPDATA%\DiskGrowthMonitor\ui.log
```

## Agent CLI

发布包包含 GUI 和 CLI 两个程序。常用操作示例：

```powershell
# 幂等启动 GUI；已运行时不会抢窗口焦点
.\diskmonitor-cli-v0.8.1.exe app start --json

# 明确切换资源模式
.\diskmonitor-cli-v0.8.1.exe mode set low_memory --json
.\diskmonitor-cli-v0.8.1.exe mode set full --rescan later --json

# 启动并等待一次扫描
.\diskmonitor-cli-v0.8.1.exe scan start "D:\data" --json
.\diskmonitor-cli-v0.8.1.exe scan wait --request-id REQUEST_ID --json

# 只读历史查询
.\diskmonitor-cli-v0.8.1.exe snapshot search SNAPSHOT_ID logs --mode substring --json
.\diskmonitor-cli-v0.8.1.exe snapshot compare NEW_ID OLD_ID --deep --path "C:\" --json
.\diskmonitor-cli-v0.8.1.exe advice list --snapshot-id SNAPSHOT_ID --target "D:\" --json
```

所有 JSON 响应都包含协议版本、状态码、请求编号和 UTC 时间。CLI 不会静默切换模式，也不支持删除文件、执行任意命令或远程网络访问。

### 只读 doctor

```powershell
python run_cli.py doctor --json
```

`doctor` 报告数据库、控制端点、日志目录和 Windows 文件信息能力：

| 状态 | 含义 |
| --- | --- |
| `ok` | 检查正常 |
| `warning` | 可继续使用，但存在需要关注的条件 |
| `error` | 检查发现明确错误 |
| `unavailable` | 当前环境无法执行或不适用该检查 |

该命令保持只读边界：

- SQLite 只以 `mode=ro` 打开，并立即启用 `query_only`；不会初始化、迁移、修复、备份或 `VACUUM` 数据库。
- `quick_check` 与 `foreign_key_check` 分开报告。SQLite 的只读 WAL 协调可能使用自己的 sidecar，但不会写入数据库内容或 schema。
- 不读取或输出认证文件、命名管道地址和日志正文；日志权限检查不会创建探针文件。
- 文件信息能力检查不会扫描用户文件。

分享诊断结果前仍应移除个人路径和其他敏感信息。完整字段说明见 [CLI 使用说明](docs/cli.md)。

## 构建与验证

```powershell
python -m unittest discover -s tests -v
python -m compileall -q disk_monitor tests run.py run_cli.py
python -m pip install -r requirements-build.txt
./build.ps1
```

GitHub Actions 只执行 Windows 下的测试、编译和固定版本 Ruff 检查，不读取 secrets、不使用正式数据库，也不构建发布附件。PyInstaller 发布构建仍由本地 `build.ps1` 负责。

测试和本地验证必须使用临时目录、临时 SQLite 数据库或一致副本，禁止把正式 `monitor.db` 作为测试输入。

## 项目文档

- [CLI 使用说明](docs/cli.md)
- [架构、数据与开发说明](docs/architecture.md)
- [版本变更记录](CHANGELOG.md)
- [安全策略与私密报告](SECURITY.md)
- [GitHub Releases](https://github.com/Ran-HJ/disk-space-growth-monitor/releases)

## 已知边界

- 默认扫描统计文件逻辑大小；精确分配与硬链接统计明显更慢，必须显式开启。
- 无权限目录、扫描期间变化或文件系统能力不足会形成部分覆盖，程序不会把未知值显示成完整结果。
- 文件快照、运行期间磁盘变化和未监控期间磁盘变化的时间范围与统计口径不同，不能直接相加。
- 低内存期间没有文件级实时明细，切回后的补扫不能证明全部变化都发生在低内存期间。
- 迁移建议不能判断文件是否正在使用，也不会替用户执行移动、删除或清理。

## 反馈与安全

普通缺陷和功能建议请使用 [Issues](https://github.com/Ran-HJ/disk-space-growth-monitor/issues/new/choose)。提交前请移除个人路径、日志原文和其他敏感信息。

安全漏洞不要提交公开 Issue，请通过 [Private Vulnerability Reporting](https://github.com/Ran-HJ/disk-space-growth-monitor/security/advisories/new) 私密报告，具体要求见 [SECURITY.md](SECURITY.md)。

## 许可证

本项目采用 [MIT License](LICENSE)，版权所有 © 2026 Ran-HJ。
