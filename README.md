# Sub2API Batch Checker

一个本地运行的 Sub2API / OpenAI OAuth 账号批量验活工具，提供中文图形界面和命令行备用入口。

## 功能

- 批量读取 Sub2API 导出 JSON 或单个 token JSON
- 支持轻量认证验活 `/v1/models`
- 支持真实最小调用验活 `/v1/responses`
- 支持并发、超时、代理、只测前 N 个
- 自动分类可用、额度/限流/周限额、封禁、认证失效、网络代理失败等状态
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

## 官方 CPAMC 管理台

不重复开发管理后台。CLI Proxy API 官方已经有开源管理台：

- 仓库：`router-for-me/Cli-Proxy-API-Management-Center`
- CLIProxyAPI 6.0.19+ 通常内置页面：`http://127.0.0.1:8317/management.html`
- 本仓库只保留批量文件适配、验活分类、误判保护、可用/异常账号导出能力。

如果你本机已经启动 CLIProxyAPI，可以双击：

```text
打开官方CPAMC管理台.cmd
```

注意：`D:\ai-relay\sub2api` 当前看起来是 Sub2API 服务，不是 CLIProxyAPI；它的配置端口是 `8080`，没有 `remote-management` 配置块。官方 CPAMC 需要连接 CLIProxyAPI 后端和管理密钥。

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

## 云端导入

验活后只把真实可调用的 good 包导入云端。脚本会自动生成临时 SQL、上传到云端、通过 Postgres 容器执行、验证账号状态/代理/分组，并清理临时 SQL。

Sub2API 云端最终吃的是 `accounts` 表记录，不是随便一种 JSON：

- OAuth / Codex JSON 包：导入为 `accounts.platform=openai`、`accounts.type=oauth`，`credentials` 里保留 OAuth 所需字段，例如 `access_token`、`refresh_token`、`client_id`、`chatgpt_account_id`。
- 上游 OpenAI-compatible API key：导入为 `accounts.platform=openai`、`accounts.type=apikey`，`credentials` 里是 `api_key`、`base_url`、`model_mapping`。
- `api_keys` 表是下游用户调用 Sub2API 的 key，不是上游供应商账号池；不要把上游 key 导到那里。

```powershell
python .\ops\import_sub2api_good_bundle.py .\outputs\sub2api_good_accounts.json --import-tag wechat_20260610
```

Windows 也可以把 good 包拖到：

```text
上传可用包到云端.cmd
```

默认不保留本地或远端临时 SQL；需要排查时再显式加 `--keep-local-sql` 或 `--keep-remote-sql`。

## 上游 API key 检测并导入

用户给 `base_url + API key` 时，走单独流程：先测 `/v1/models`，再选一个模型测 `/v1/chat/completions`。两步都通过才生成 good 包并导入云端；任一步失败直接废弃，不导入。

API key 上游账号不由云端号池维护脚本自动暂停、恢复探针或清理，避免网络波动和上游临时异常误伤可用 key；key 方式只走这条单独检测/导入流程。

输入 JSON 形状：

```json
{"name":"hub","base_url":"https://example.com","api_key":"sk-..."}
```

推荐用环境变量传 key，避免进入 shell 历史：

```powershell
$env:SUB2API_TEST_BASE_URL = "https://example.com"
$env:SUB2API_TEST_API_KEY = "sk-..."
python .\ops\check_import_api_key_upstream.py --import-tag api_key_example
```

云端 SSH 参数从本地配置或环境变量读取：

```powershell
$env:SUB2API_CLOUD_SSH_KEY = "C:\path\to\key"
$env:SUB2API_CLOUD_SSH_TARGET = "user@host"
```

## 云端号池维护

`ops/sub2api_cloud_maintenance.py` 只维护 OAuth/token JSON 账号，不处理 `type=apikey` 上游账号。维护入口会分批探测历史遗留的 `active + schedulable=false` 且没有 reset/reason/probe 标记的 OAuth 账号：探针 ok 就恢复调度，明确认证失效、封禁或额度耗尽就软删除，临时限流或网络失败累计到阈值后再清理。

## 状态分类

- `ok`：可用
- `codex_login_only`：Codex 登录有效，但不代表真实推理可用
- `sub2api_compatible`：Sub2API OAuth 兼容可用
- `model_unsupported`：模型不支持
- `request_shape_error`：请求方式待适配，不代表账号坏
- `permission_or_scope_missing`：登录有效但 API 权限或作用域不足
- `quota_or_rate_limited`：额度用尽、限流或周限额；默认不当坏号删除，优先刷新复测或等待周期重置
- `forbidden_or_banned`：权限不足或账号禁用
- `auth_invalid`：认证失效，需要重新登录
- `expired_locally`：本地判断已过期
- `network_or_proxy`：网络或代理失败，建议开代理复测
- `unsupported`：暂不支持的账号格式
- `failed_unknown`：未知失败，建议复测

## License

MIT
