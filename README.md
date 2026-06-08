# Sub2API Batch Checker

一个本地运行的 Sub2API / OpenAI OAuth 账号批量验活工具，提供中文图形界面和命令行备用入口。

## 功能

- 批量读取 Sub2API 导出 JSON 或单个 token JSON
- 支持轻量认证验活 `/v1/models`
- 支持真实最小调用验活 `/v1/responses`
- 支持并发、超时、代理、只测前 N 个
- 自动分类可用、额度限流、封禁、认证失效、网络代理失败等状态
- 生成 CSV 总表、可用账号导入包、坏账号包
- 图形界面支持按失败类型移动或送入 Windows 回收站
- 本地运行，不在日志里打印 `access_token` / `refresh_token` / `id_token`

## 安全提醒

本工具会读取包含凭据的 JSON 文件。开源仓库不应该包含真实账号文件、检测结果或导出的可用/坏账号包。

仓库已提供 `.gitignore`，默认忽略：

- `outputs/`
- `token_*.json`
- `tokens/`
- `sub_json/`
- `*_good*.json`
- `*_bad*.json`
- `*.csv`

提交前请务必看一眼 `git status`，确认没有真实 token 文件。

## 图形版使用

Windows 下可双击：

```text
启动Sub2API批量验活图形版.cmd
```

界面里可以选择文件夹、单个 JSON、多个 JSON，设置代理和并发后点击“开始验活”。

默认输入目录可以用环境变量覆盖：

```powershell
$env:SUB2API_CHECKER_DEFAULT_INPUT = "D:\your-token-folder"
```

## 命令行使用

轻量认证验活：

```powershell
python -m sub2api_batch_checker.cli .\your-token-folder --endpoint https://api.openai.com/v1/models --concurrency 10 --no-refresh
```

真实调用验活：

```powershell
python -m sub2api_batch_checker.cli .\your-token-folder --endpoint https://api.openai.com/v1/responses --concurrency 10 --timeout 30
```

使用 HTTP 代理：

```powershell
python -m sub2api_batch_checker.cli .\your-token-folder --endpoint https://api.openai.com/v1/models --proxy http://127.0.0.1:7890
```

## 状态分类

- `ok`：可用
- `quota_or_rate_limited`：额度用尽或限流
- `forbidden_or_banned`：权限不足或账号禁用
- `auth_invalid`：认证失效，需要重新登录
- `expired_locally`：本地判断已过期
- `network_or_proxy`：网络或代理失败，建议开代理复测
- `unsupported`：暂不支持的账号格式
- `failed_unknown`：未知失败，建议复测

## License

MIT
