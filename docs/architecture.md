# 架构、数据与开发说明

本文说明 C 盘空间增长监控器的主要组件、数据边界和开发验证方式。项目面向 Windows，运行时只依赖 Python 标准库；PyInstaller 只用于生成发布程序。

## 组件关系

```text
Tk GUI / Windows 托盘
          │
          ├── Service / Scanner ── Windows 文件信息 API
          │          │
          │          └── SQLite Storage
          │
          └── 本地命名管道控制服务
                         │
                    Agent CLI

离线 CLI ── ReadOnlyDatabase / doctor ── SQLite mode=ro
```

GUI 是状态和写入操作的唯一协调者。Agent CLI 在 GUI 运行时通过本地命名管道发送白名单请求；离线历史查询和 `doctor` 使用独立只读路径。

## 主要模块

| 模块 | 职责 |
| --- | --- |
| `ui.py` | 主窗口生命周期、业务状态、事件绑定和视图协调 |
| `settings_view.py` | 设置表单校验和设置窗口视图 |
| `trend_view.py` / `treemap_view.py` | 纯几何计算与命中测试，不持有业务写入职责 |
| `scanner.py` | 迭代目录扫描、取消、排除规则和统计覆盖 |
| `storage.py` | SQLite schema、迁移、快照、会话和保留策略 |
| `readonly.py` | GUI 未运行时的只读历史查询 |
| `agent_control.py` / `control_*` | 当前用户范围内的本地控制协议和传输 |
| `cli.py` | 命令解析、统一 JSON envelope 和离线/在线路由 |
| `diagnostics.py` | 完全只读的本地 doctor 报告 |
| `automation.py` / `tray.py` | 自动低内存策略和 Windows 托盘交互 |
| `accounting.py` / `windows_file_info.py` | 分配大小、稳定文件身份和硬链接记账 |
| `migration_advice.py` | 只读候选筛选、风险排除和目标盘容量估算 |

## 数据模型与口径

程序有意保留多种不可互换的统计口径：

- **磁盘容量变化**来自 Windows 盘符已用容量，可覆盖文件系统元数据等目录扫描不可见部分。
- **文件逻辑大小**是默认扫描口径，稳定且速度较快。
- **路径分配大小**是文件路径对应的实际分配量，只在精确扫描中读取。
- **唯一分配大小**在当前扫描范围内按稳定文件身份去重；代表路径只是确定性记账位置。
- **文件快照、运行期间变化、未监控期间变化**具有不同起止时间，不能直接相加。

schema v3 使用路径字典和每快照目录指标支持深层历史浏览。旧快照缺少新指标时会降级显示，不会补造数据。

## 本地数据

默认应用目录：

```text
%LOCALAPPDATA%\DiskGrowthMonitor\
```

主要内容：

| 路径 | 内容 |
| --- | --- |
| `monitor.db` | 分钟容量、会话、扫描快照和设置 |
| `backups\` | schema 升级前由 SQLite backup API 创建的恢复副本 |
| `ui.log` | 轮转界面日志 |
| `control.endpoint.json` / `control-*.auth` | GUI 运行期间的本地控制元数据和认证材料 |

分钟采样默认保留 30 天；原始目录快照默认保留 90 天；会话汇总长期保留。完整目录指标按来源分别使用保留策略，活动会话引用不会被提前清理。

认证文件和控制端点属于运行时敏感数据，不应上传、写入 Issue 或作为诊断附件分享。

## 安全边界

- 扫描器读取目录项和文件元数据，跳过符号链接、目录联接和无权限对象，不修改被扫描内容。
- 迁移建议只分析已记录文件，不提供复制、移动、删除、执行或回滚入口。
- `doctor` 不初始化、迁移、修复、备份或清理数据库，也不读取认证文件和日志正文。
- 本地控制不监听网络端口，认证密钥不会进入 JSON 响应或日志。
- 正式 `monitor.db` 不能用于测试。数据库验证只能使用临时库或一致副本。

## 开发环境

源码支持 Python 3.10 及以上。运行程序不需要安装第三方包：

```powershell
python run.py
python run_cli.py --version
```

最小开发验证：

```powershell
python -m unittest discover -s tests -v
python -m compileall -q disk_monitor tests run.py run_cli.py
python -m pip install -r requirements-ci.txt
python -m ruff check disk_monitor tests run.py run_cli.py
```

GitHub Actions 在 `windows-latest` 上测试 Python 3.10 和 3.13；Ruff 使用独立的 Python 3.13 job。CI 不访问 secrets、不上传数据库、不运行 PyInstaller，也不产出 Release 附件。

## 构建

```powershell
python -m pip install -r requirements-build.txt
./build.ps1
```

`build.ps1` 是唯一发布构建入口，固定 PyInstaller 版本并关闭 UPX。它生成同版本的 GUI 和 CLI 单文件程序；CI 通过不等于 EXE 构建、隔离冒烟或用户可视验收通过。

## 修改原则

- 统计口径、旧数据库兼容和只读边界优先于表面简化。
- UI 职责按可独立测试的纯计算逐步提取，不一次性重写窗口状态机。
- 扫描器保持顺序 `os.scandir`；现有基准没有证明目录并行化达到启用门槛。
- 新测试使用 `TemporaryDirectory`、临时 SQLite 和固定夹具，不连接正式应用数据。
- 发布版本只有在完整回归、编译、双 EXE、隔离冒烟和用户验收全部完成后才关闭。
