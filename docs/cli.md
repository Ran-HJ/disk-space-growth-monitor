# CLI 使用说明

`diskmonitor` 是 C 盘空间增长监控器的本地命令行接口。源码入口为 `python run_cli.py`，发布包入口为同版本的 `diskmonitor-cli-vX.Y.Z.exe`。

CLI 使用 UTF-8 输出。带 `--json` 时，响应包含协议版本、状态码、请求编号、UTC 时间和 `data`；错误也使用同一 envelope，不需要解析本地化文本判断结果。

## 运行方式

```powershell
python run_cli.py --version
python run_cli.py app status --json
```

需要指定隔离数据库或控制目录时，把公共参数放在具体命令之后：

```powershell
python run_cli.py doctor `
    --database "D:\temp\monitor.db" `
    --control-directory "D:\temp\control" `
    --json
```

## 命令组

| 命令组 | 用途 | 是否需要 GUI |
| --- | --- | --- |
| `app` | 启动、查询、激活或关闭 GUI | 启动命令除外，其余通常需要 |
| `mode` | 查询或切换全功能/低内存模式 | 是 |
| `automation` | 查询或配置自动低内存策略 | 是 |
| `scan` | 启动、等待、取消或读取扫描结果 | 是 |
| `view` | 打开、搜索当前导航结果或查看最大项 | 是 |
| `snapshot` | 保存、列出、查看、搜索或比较快照 | 保存需要；历史读取可离线 |
| `disk` | 读取指定盘符的即时容量 | 否 |
| `session` | 读取当前或最近会话 | 当前需要；历史读取可离线 |
| `growth` | 读取当前或最近的增长/减少项 | 当前需要；历史读取可离线 |
| `tree` | 读取当前树或历史快照目录树 | 当前需要；历史读取可离线 |
| `advice` | 生成只读迁移候选和风险说明 | 否，可读取历史库 |
| `report` | 导出 JSON、Markdown 或 CSV 报告 | 取决于导出内容 |
| `doctor` | 只读检查数据库与本地运行条件 | 否 |

## 常用流程

### 启动并查询状态

```powershell
python run_cli.py app start --json
python run_cli.py app status --json
```

`app start` 是幂等操作。GUI 已运行时默认不会激活窗口；需要显示窗口时显式添加 `--activate`。

### 切换资源模式

```powershell
python run_cli.py mode set low_memory --json
python run_cli.py mode set full --rescan now --json
python run_cli.py mode set full --rescan later --json
```

低内存模式会拒绝扫描和保存当前文件快照。切回全功能时必须明确选择立即补扫或稍后补扫，CLI 不会替用户猜测。

### 扫描与导航

```powershell
python run_cli.py scan start "D:\data" --json
python run_cli.py scan wait --request-id REQUEST_ID --timeout 300 --json
python run_cli.py view open "D:\data\logs" --scan-if-missing --json
python run_cli.py view search logs --mode substring --limit 20 --json
python run_cli.py view largest --min-size 104857600 --limit 20 --json
```

扫描是异步任务。`scan start` 返回 `request_id`，后续使用相同编号等待或读取结果。

### 历史快照

```powershell
python run_cli.py snapshot list --source closing --limit 20 --json
python run_cli.py snapshot show SNAPSHOT_ID --json
python run_cli.py snapshot search SNAPSHOT_ID logs --mode substring --json
python run_cli.py snapshot largest SNAPSHOT_ID --min-size 104857600 --json
python run_cli.py snapshot compare NEW_ID OLD_ID --deep --path "C:\" --json
python run_cli.py snapshot compare NEW_ID OLD_ID --accounting --json
```

`--deep` 使用已保存的完整目录指标；旧版本或过期快照会诚实降级为浅层视图。`--accounting` 按稳定文件身份比较唯一分配变化，路径别名变化不会被当成真实空间增长。

### 只读迁移建议

```powershell
python run_cli.py advice list `
    --snapshot-id SNAPSHOT_ID `
    --target "D:\" `
    --min-size 104857600 `
    --limit 50 `
    --json
```

该命令只读取已记录数据和目标盘即时容量。它不会移动、复制、重命名、删除或修改任何文件，也不能证明候选文件当前未被占用。

## doctor

```powershell
python run_cli.py doctor --json
```

诊断正文位于响应的 `data`，包含：

- `overall_status`：所有检查汇总状态；
- `database`：数据库是否存在、只读打开、`quick_check`、`foreign_key_check`、schema 和最近扫描摘要；
- `control`：本地控制元数据是否存在、格式是否有效、对应进程是否仍存活；
- `logging`：日志目录和现有日志文件的可访问性，不读取日志正文；
- `file_information`：Windows 文件信息 API 是否可用，不扫描用户文件。

检查状态：

| 状态 | 含义 |
| --- | --- |
| `ok` | 条件正常 |
| `warning` | 可继续使用，但存在需要关注的条件 |
| `error` | 已确认错误，例如数据库损坏或路径类型不正确 |
| `unavailable` | 平台不支持、对象不存在或当前环境无法执行 |

### 只读保证

- 数据库 URI 使用 `mode=ro`，连接后立即设置 `PRAGMA query_only=ON`。
- 不初始化、迁移、修复、备份或 `VACUUM` 数据库。
- `quick_check` 和 `foreign_key_check` 分开执行与报告。
- SQLite 在 WAL 模式下可能为只读连接使用协调 sidecar；这不代表数据库内容或 schema 被修改。
- 控制检查不会读取或输出 `control-*.auth`、命名管道地址或认证材料。
- 日志检查不会创建测试文件，也不会读取日志正文。

即使报告本身不包含认证材料，公开分享前也应检查并脱敏本机用户名和路径。

## 安全边界

- CLI 只提供白名单命令，不执行任意 shell 命令。
- 本地控制使用当前 Windows 用户范围内的命名管道和每次启动更新的认证密钥，不开放 TCP/HTTP 端口。
- GUI 未运行时，支持的历史查询只读打开 SQLite，不会为了查询而初始化或升级数据库。
- 测试、问题复现和示例不得使用正式数据库；应使用临时 SQLite 或一致副本。
