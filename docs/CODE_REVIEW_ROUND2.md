# 天枢质量 ai_test_toolkit 源码 Review 第二轮修复清单

> 用途：这份文档基于当前源码状态再次 review，记录第一轮修复后仍存在或新暴露的问题。可直接交给 Claude/Codex 按优先级修复。

## 本轮验证范围

重点检查路径：

- `apps/api/main.py`
  - OAuth 登录/退出与子进程清理。
  - TDR 提交、列表、详情、报告中心、导出、删除。
  - URL 抓取和文件提取安全边界。
  - HTML 报告导出。
- `packages/core/llm/client.py`
  - 认证校验是否已下沉。
- `packages/core/auth_config.py`
  - OAuth 登录标记与 API Key 存储。
- `tests/integration/test_tdr.py`
- `tests/integration/test_modules.py`

已执行测试：

```bash
.venv/bin/python -m pytest tests/integration/test_tdr.py tests/integration/test_modules.py -q
```

结果：

```text
24 passed, 1 skipped
```

说明：现有测试通过，但没有覆盖本轮发现的几个路径，例如 TDR 嵌套报告详情/删除/导出、OAuth 子进程清理、URL 抓取流式限流。

## 已确认的正向变化

相比上一轮 review，源码中已经修复或部分修复了这些问题：

- `/tdr` 和 `/api/tdr/submit` 已共用 `_normalize_tdr_comment()`。
- `/api/tdr/reviews` 已同时扫描：
  - `<report_dir>/tdr_<run_id>.json`
  - `<report_dir>/tdr/<run_id>/review.json`
- `/api/reports` 已能列出嵌套 TDR 报告。
- TDR UI `renderResult()` / `loadHistory()` 已补 `escapeHtml()` 和部分 class 白名单。
- `packages/core/llm/client.py::_build_auth_env()` 已下沉 OAuth/API Key 完整认证校验。
- `AuthNotConfiguredError` 已不再触发模型降级。
- `/api/extract-file` 已改为分块读取并加上传大小限制。
- `/api/fetch-url` 已加公网 host 校验、重定向后校验、响应大小上限。
- OAuth 登录默认不再清全局 Claude 凭据，只有 `force_reauth=True` 才清。
- OAuth disconnect 默认 `purge=False`，只清本工具登录态。

## P1-1 OAuth 登录清理子进程时可能误杀当前 App 进程组

### 位置

- `apps/api/main.py`
  - `_cleanup_login_pty()`：约 `6923-6943`
  - `_run_claude_login_install()` 创建子进程：约 `7051-7058`

### 问题描述

`_cleanup_login_pty()` 中有：

```python
os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
```

但创建登录子进程时：

```python
proc = await asyncio.create_subprocess_exec(
    "/usr/bin/script", "-q", "/dev/null", bin_path, "login",
    ...
)
```

没有设置独立 session/process group。子进程默认继承父进程组，因此 `os.killpg(os.getpgid(proc.pid), SIGTERM)` 可能命中当前 FastAPI/.app 自己所在的进程组。

### 影响

- OAuth 登录完成或超时清理时，可能把整个 App 进程也 SIGTERM 掉。
- 问题表现可能是 App 闪退、服务突然不可用、前端轮询断开。
- 这是高风险稳定性问题。

### 建议修复

优先方案：

1. 创建子进程时增加独立 session：

```python
proc = await asyncio.create_subprocess_exec(
    "/usr/bin/script", "-q", "/dev/null", bin_path, "login",
    ...,
    start_new_session=True,
)
```

2. 清理时先确认 pgid：

```python
pgid = os.getpgid(proc.pid)
if pgid != os.getpgrp():
    os.killpg(pgid, signal.SIGTERM)
```

3. kill 后最好等待子进程退出：

```python
try:
    await asyncio.wait_for(proc.wait(), timeout=3)
except asyncio.TimeoutError:
    proc.kill()
```

保守方案：

- 去掉 `killpg`，只 terminate/kill 直接子进程。
- 如果要清孙进程，必须保证登录命令运行在独立 process group。

### 验收点

- OAuth 登录成功、取消、超时后 App 不退出。
- 清理后没有残留 `script` / `claude login` 进程。
- 多次连续点击登录不会产生僵尸进程。

### 建议测试

- 单元测试可 monkeypatch 一个 fake proc，验证 `_cleanup_login_pty()` 不会 kill 当前进程组。
- 手动测试：
  - 启动 App。
  - 点击 OAuth 登录。
  - 不完成授权，等待超时或手动取消。
  - 确认 App 仍可访问 `/healthz`。

## P1-2 TDR 嵌套报告列表可见，但详情/删除/导出仍不支持

### 位置

- `apps/api/main.py`
  - `/api/tdr/reviews`：约 `258-299`
  - `/api/tdr/reviews/{run_id}`：约 `325-331`
  - `/api/reports`：约 `7300-7374`
  - `/api/reports/export`：约 `7377-7398`
  - `/api/reports/{run_id}`：约 `7401-7422`
  - `DELETE /api/reports/{run_id}`：约 `7425-7443`

### 问题描述

当前 `/api/tdr/reviews` 已支持扫描嵌套路径：

```text
<report_dir>/tdr/<run_id>/review.json
```

`/api/reports` 也能把嵌套 TDR 报告列出来。

但以下接口仍只处理顶层文件：

```text
<report_dir>/*_<run_id>.json
<report_dir>/tdr_<run_id>.json
```

问题接口：

- `/api/tdr/reviews/{run_id}`
  - 只读 `<report_dir>/tdr_<run_id>.json`
- `/api/reports/{run_id}`
  - 只 glob `*_<run_id>.json`
- `DELETE /api/reports/{run_id}`
  - 只删除 `*_<run_id>.json`
- `/api/reports/export`
  - zip 只打包顶层 `*.json`

### 影响

raw `/tdr` 生成的报告会出现这些不一致：

- TDR 历史列表能看到。
- 点击 JSON 详情可能 404。
- 报告中心列表能看到。
- 点详情可能 404。
- 删除报告删不掉嵌套文件。
- 导出 zip 漏掉嵌套 TDR 报告。

### 建议修复

抽公共查找函数，统一所有报告相关接口：

```python
def _find_report_file_by_run_id(run_id: str) -> Path | None:
    out_dir = Path(settings.report_output_dir)
    candidates = []
    candidates.extend(out_dir.glob(f"*_{run_id}.json"))
    nested = out_dir / "tdr" / run_id / "review.json"
    if nested.exists():
        candidates.append(nested)
    return candidates[0] if candidates else None
```

再抽枚举函数：

```python
def _iter_saved_report_files() -> Iterable[Path]:
    yield from out_dir.glob("*.json")
    yield from (out_dir / "tdr").glob("*/review.json")
```

使用范围：

- `/api/tdr/reviews/{run_id}`
- `/api/reports/{run_id}`
- `DELETE /api/reports/{run_id}`
- `/api/reports/export`
- 如有 `/api/reports/{run_id}/export.{fmt}`，也应复用。

### 验收点

- raw `/tdr` 提交后的嵌套报告：
  - 能在 `/api/tdr/reviews` 看到。
  - 能通过 `/api/tdr/reviews/{run_id}` 打开。
  - 能在 `/api/reports` 看到。
  - 能通过 `/api/reports/{run_id}` 打开。
  - 能被 `/api/reports/export` 打进 zip。
  - 能被 `DELETE /api/reports/{run_id}` 删除。
- UI `/api/tdr/submit` 生成的顶层 mirror 报告仍兼容。
- 不产生重复列表项。

### 建议测试

新增测试用例：

1. 构造嵌套文件：

```text
tmp_report_dir/tdr/raw-001/review.json
```

2. 调：

```text
GET /api/tdr/reviews
GET /api/tdr/reviews/raw-001
GET /api/reports
GET /api/reports/raw-001
GET /api/reports/export
DELETE /api/reports/raw-001
```

3. 断言都能处理嵌套路径。

## P1-3 OAuth 快路径只凭文件存在就标记登录成功

### 位置

- `apps/api/main.py`
  - `/api/settings/auth/login`：约 `6730-6800`
  - 快路径判断：约 `6743-6775`
  - `/api/settings/auth` ready 判断：约 `6410-6504`

### 问题描述

当 `force_reauth=False` 时，登录接口会检查本机是否存在这些凭据文件：

- `~/.claude/account.json`
- `~/.claude/auth.json`
- `~/Library/Application Support/claude-code/account.json`
- `~/.claude.json#oauthAccount`

只要存在，就直接：

```python
set_auth_mode("oauth")
mark_oauth_logged_in()
```

然后返回：

```json
{
  "status": "succeeded",
  "fast_path": true
}
```

问题是：文件存在不代表凭据有效。文件可能过期、损坏、账号被退出、token 失效，或者是旧版本残留。

`/api/settings/auth` 的 ready 判断又依赖：

```python
current_mode == "oauth"
cli installed
oauth_local_creds
has_session_login
```

快路径会把 `has_session_login` 直接打上，导致 UI 显示已就绪，但实际运行 LLM 时才失败。

### 影响

- 设置页显示 OAuth ready，但工具运行失败。
- 用户不会进入浏览器重新授权流程。
- 问题会表现成 LLM/Claude 调用失败，而不是登录失败。

### 建议修复

不要只凭文件存在就 `mark_oauth_logged_in()`。

建议快路径加一个只读校验：

方案 A：调用 Claude CLI 只读命令。

如果 Claude CLI 有稳定的 auth/account/status 命令，使用该命令验证当前 token 可用。

方案 B：执行一个最小 no-op/低成本校验。

例如通过 SDK 或 CLI 发起极小请求，但注意成本和超时。

方案 C：保守处理。

本机已有凭据时，不直接标记 ready，而是返回：

```json
{
  "status": "needs_confirm",
  "local_creds_present": true,
  "message": "检测到本机 Claude 登录，请点击确认复用或重新登录"
}
```

然后用户确认后再 `mark_oauth_logged_in()`。

### 验收点

- 凭据文件存在但无效时，不会显示 ready。
- 凭据文件有效时，可以快速复用。
- 快路径失败时自动进入浏览器 OAuth，或提示用户重新登录。
- ready 状态与实际工具运行结果一致。

### 建议测试

- 临时创建空 `~/.claude/account.json`，确认不会被标记 ready。
- 临时创建格式错误 `.claude.json`，确认不会被标记 ready。
- 模拟 `_check_claude_login()` 返回 logged_in 但 CLI 校验失败，确认登录接口不返回 succeeded。

## P2-1 `/api/fetch-url` 响应大小限制仍是在完整下载后才检查

### 位置

- `apps/api/main.py`
  - `_is_safe_public_host()`：约 `7656-7680`
  - `/api/fetch-url`：约 `7683-7736`

### 问题描述

当前接口已经增加：

- 仅允许 http/https。
- 解析 host 后拒绝内网/loopback/link-local。
- 重定向后重新校验最终 URL。
- 响应 5MB 上限。

但请求使用：

```python
r = await cli.get(req.url, ...)
```

这会让 httpx 先把响应体完整读入内存。后续才执行：

```python
text = r.text
if len(text.encode("utf-8")) > MAX_BYTES:
    raise HTTPException(413, ...)
```

如果服务端不返回 `Content-Length`，或返回虚假较小值，仍可能先下载很大的响应体。

### 影响

- 大响应会占用内存。
- 慢速大响应会拖住请求。
- 多个并发抓取可能影响本地 App 稳定性。

### 建议修复

改为流式读取：

```python
async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as cli:
    async with cli.stream("GET", req.url, headers={...}) as r:
        r.raise_for_status()
        # 校验 r.url
        chunks = []
        total = 0
        async for chunk in r.aiter_bytes():
            total += len(chunk)
            if total > MAX_BYTES:
                raise HTTPException(413, "响应过大")
            chunks.append(chunk)
        raw = b"".join(chunks)
```

然后根据 content-type/charset 解码文本。

注意：

- 跟随重定向后的最终 URL 仍要校验。
- 如果使用 `follow_redirects=True`，stream 返回时最终 URL 已在 `r.url`。
- 仍保留 Content-Length 提前拒绝作为优化。

### 验收点

- 无 Content-Length 的大响应超过 5MB 后立即中断。
- 正常公网小文档仍能抓取。
- 重定向到内网仍拒绝。
- 响应体不会先完整读入内存。

### 建议测试

- 用本地 mock transport 或测试服务返回分块大响应。
- 验证超过阈值时返回 413。
- 验证实际读取字节不超过阈值太多。

## P2-2 HTML 报告导出存在 class 属性注入风险

### 位置

- `apps/api/main.py`
  - `_build_executive_summary()`：约 `3029-3130`
  - `_build_html_report()`：约 `3278-3566`
  - issue card class 拼接：约 `3368`

### 问题描述

`_build_executive_summary()` 从报告中提取 issue 后，直接使用：

```python
"severity": (it.get("severity") or "medium").lower()
```

`_build_html_report()` 里再拼接：

```python
f'<div class="issue-card sev-{it["severity"]}">'
```

如果报告中的 severity 是异常字符串，例如：

```text
high" onclick="alert(1)
```

会进入 HTML 属性上下文。虽然多数其他字段做了 `_esc()`，但这里属于 class 属性拼接，不应使用原始输入。

### 影响

- 导出的独立 HTML 报告可能被报告数据注入属性。
- 如果报告文件来自外部导入或模型输出，风险更高。

### 建议修复

统一 severity 白名单：

```python
def _normalize_severity(value: Any) -> str:
    s = str(value or "").lower().strip()
    if s in {"critical", "high", "medium", "low", "info"}:
        return s
    if s in {"blocker", "major"}:
        return "high"
    if s in {"minor", "suggestion"}:
        return "low"
    return "medium"
```

应用位置：

- `_build_executive_summary()` 中写入 `natural_issues` 前。
- 统计 `sev_counts` 时也用同一函数。
- 前端 JS 中如有类似 `sev-${it.severity}`，也应做白名单。

### 验收点

- 恶意 severity 字符串不会破坏 HTML 属性。
- blocker/major/minor/suggestion 能合理映射。
- 报告样式仍正常。

### 建议测试

构造报告：

```json
{
  "substeps": {
    "x": {
      "issues": [
        {
          "title": "bad severity",
          "severity": "high\" onclick=\"alert(1)",
          "description": "demo"
        }
      ]
    }
  }
}
```

调用 `_build_html_report()`，断言输出中不包含 `onclick`，且 class 为安全值。

## P3-1 OAuth 登录函数注释与实现不一致

### 位置

- `apps/api/main.py`
  - `_run_claude_login_install()` docstring：约 `6946-6959`
  - 实际 force_reauth 分支：约 `6973-7035`

### 问题描述

当前实现已经改成：

- `force_reauth=False`：不清全局凭据。
- `force_reauth=True`：才清 Keychain、`~/.claude/` 等全局凭据。

但 `_run_claude_login_install()` 的 docstring 仍写：

```text
关键：登录前先清掉本机现有凭据
流程 0. 清掉 ~/.claude/account.json + .claude.json#oauthAccount + claude logout
```

这和实现不一致。

### 影响

- 后续维护者可能按旧注释理解逻辑。
- 容易把已修复的“默认清全局凭据”问题改回来。
- 影响排查 OAuth 问题时的判断。

### 建议修复

更新 docstring：

- 默认路径：不清全局凭据。
- 检测到本机已有凭据时可复用。
- 只有 `force_reauth=True` 才清全局凭据。
- 清全局凭据会影响终端 Claude Code，应谨慎。

### 验收点

- 注释和实际代码一致。
- 搜索“先清掉本机现有凭据”不再出现在默认登录路径说明中。

## 建议修复顺序

1. 修 OAuth 子进程 process group 清理，避免 App 被误杀。
2. 统一报告查找/枚举函数，修 TDR 嵌套报告详情、删除、导出。
3. 修 OAuth 快路径 ready 误判。
4. `/api/fetch-url` 改为流式读取并实时限流。
5. HTML 报告 severity 白名单归一化。
6. 同步 OAuth docstring。

## 建议新增测试清单

### TDR/报告

- 嵌套 TDR 报告能被 `/api/tdr/reviews/{run_id}` 读取。
- 嵌套 TDR 报告能被 `/api/reports/{run_id}` 读取。
- 嵌套 TDR 报告能被 `/api/reports/export` 打包。
- 嵌套 TDR 报告能被 `DELETE /api/reports/{run_id}` 删除。
- 顶层 mirror 和嵌套文件同时存在时不重复展示。

### OAuth

- `_cleanup_login_pty()` 不会 kill 当前进程组。
- `force_reauth=False` 不会删除 Keychain 和 `~/.claude`。
- `force_reauth=True` 才执行全局清理。
- 凭据文件存在但校验失败时，不标记 `oauth_logged_in_at`。

### URL 抓取

- 无 Content-Length 的大响应超过 5MB 时返回 413。
- 重定向到 private IP 被拒绝。
- 公网小文档正常返回。

### HTML 导出

- 恶意 severity 不出现在 class 属性中。
- `onclick`、引号等 payload 不会进入导出 HTML。

## 手动验收清单

- OAuth 登录成功、取消、超时后 App 不闪退，`/healthz` 仍可访问。
- raw `/tdr` 提交报告后：
  - TDR 历史能看到。
  - 点击 JSON 能打开。
  - 报告中心详情能打开。
  - 导出 zip 包含该报告。
  - 删除后文件确实消失。
- 本机存在坏的 Claude 凭据文件时，设置页不会显示 ready。
- 抓取大 URL 时不会卡死或内存暴涨。
- 导出 HTML 报告打开后无脚本执行风险。

## 备注

本轮 review 是在第一轮问题已有部分修复后的基础上进行的。请修复时注意不要回退以下已完成改动：

- OAuth 默认不清全局凭据。
- 认证校验下沉到 `LlmClient`。
- TDR comment normalization 复用。
- TDR UI 的 `escapeHtml()`。
- 文件上传分块大小限制。
- URL host 公网校验。
