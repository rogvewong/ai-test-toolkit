# 天枢质量 ai_test_toolkit 源码 Review 与修复清单

> 目的：这份文档用于交给 Claude/Codex 执行修复。请按优先级从 P1 到 P3 逐项处理，每个问题修复后补充最小必要测试或手动验收步骤。

## 项目结构上下文

核心代码路径：

- `apps/api/main.py`
  - 主后端，FastAPI + 内嵌 HTML/JS，约 1.1 万行。
  - 包含认证设置、OAuth 登录、工具运行、TDR 工作台、报告列表、文件提取、URL 抓取等入口。
- `packages/core/auth_config.py`
  - 认证模式存储，API Key 存取，OAuth 登录标记。
- `packages/core/llm/client.py`
  - LLM 客户端，负责按认证模式构建 Claude CLI 子进程环境。
- `packages/tdr/`
  - TDR review/workstation/signing 逻辑。
- `packages/workflow/base.py`
  - Agent orchestrator 基类，包含 evidence 归档。
- `tests/integration/`
  - 当前已有集成测试，建议修复后优先补覆盖认证/TDR/安全边界。

## 总体结论

当前源码相比已安装 App 已经修复了部分问题，例如：

- `/api/settings/install/{job_id}` 已过滤 `_proc`、`_reader_task` 等内部字段。
- `/api/tools/{tool_id}` 对 `tool_id == "runs"` 做了特判，避免吞掉 `/api/tools/runs`。
- `/api/tools/{tool_id}/run` 已增加认证 412 闸门。
- `/api/tdr/submit` 已对 UI 简短评论格式做宽容映射。

但仍有几类高风险问题需要修复：

- OAuth 登录/退出会删除全局 Claude Code 凭据，可能误伤用户本机其他 Claude 工具。
- API 层认证闸门和底层 LLM 客户端认证校验不一致。
- TDR 存在两套提交路径，字段兼容和报告发现逻辑不一致。
- 内嵌 HTML 使用 `innerHTML` 直接拼接报告数据，存在 XSS 风险。
- `/api/fetch-url` 可抓取任意内网/本机 URL，存在本地 SSRF 风险。
- 文件上传先整文件读内存，没有服务端大小限制。
- workflow evidence 会持久化完整 prompt/响应，可能泄露敏感数据。

## P1-1 OAuth 登录会清除全局 Claude 凭据

### 位置

- `apps/api/main.py`
  - `_run_claude_login_install()`：约 `6831-6918`
  - `/api/settings/auth/disconnect`：约 `6555-6648`

### 现象

OAuth 登录后台流程在真正调起 `claude login` 前，会先执行：

- `claude logout`
- 删除 macOS Keychain 中的 `Claude Code-credentials`
- 删除 `~/.claude/account.json`
- 删除 `~/.claude/auth.json`
- 删除 `~/Library/Application Support/claude-code/account.json`
- 清理 `~/.claude.json` 中的 `oauthAccount`

这意味着用户点击“调起浏览器登录”后，即使后续 OAuth 失败、浏览器没打开、进程超时，用户原本可用的 Claude Code 登录态也已经被清掉。

`/api/settings/auth/disconnect` 也会做同样的全局删除。这个行为不是“断开本 App 认证”，而是“退出整台机器上的 Claude Code 登录”。

### 影响

- 用户可能丢失本机 Claude Code、Claude CLI、其他工具共用的 OAuth 登录态。
- OAuth 登录失败后无法自动恢复原凭据。
- 对桌面 App 来说，这是高风险副作用。

### 建议修复

优先方案：

1. 不要在登录开始前删除全局 Claude 凭据。
2. App 自己维护认证状态：
   - `auth_config.py` 中只记录用户是否选择 OAuth。
   - 使用 `get_oauth_logged_in_at()` 作为本工具是否允许运行的标记。
3. 如果必须强制浏览器 OAuth，不要直接删全局凭据。改为：
   - 使用隔离的 Claude 配置目录，如果 Claude CLI 支持对应环境变量。
   - 或弹出明确危险确认：“会退出本机 Claude Code 登录”，并只在用户确认后执行。
4. OAuth 登录失败时，不应留下“已清除全局凭据但未登录成功”的状态。

折中方案：

- `/api/settings/auth/login` 不再默认清凭据。
- 新增单独按钮或参数 `force_reauth=true`，只在用户显式选择“强制重新登录”时清理。
- 清理前备份 `~/.claude.json` 中相关字段，失败时尝试恢复。

### 验收点

- 本机已经登录 Claude Code 时，点击 OAuth 登录不会直接破坏现有登录态。
- OAuth 登录失败、取消、超时后，原本 Claude CLI 仍可用。
- 只有用户明确点“退出 Claude OAuth”并确认影响时，才执行全局 logout/删除。
- `/api/settings/auth` 返回状态准确，不把旧凭据误判成本 App 新登录。

## P1-2 LLM 客户端认证校验不完整

### 位置

- `packages/core/llm/client.py`
  - `_build_auth_env()`：约 `162-190`
  - `LlmClient.complete()`：约 `288-309`
- `apps/api/main.py`
  - `/api/tools/{tool_id}/run` 认证闸门：约 `3769-3797`

### 现象

API 路由 `/api/tools/{tool_id}/run` 已经检查：

- `mode == unset` 拒绝。
- `mode == oauth` 且无 `get_oauth_logged_in_at()` 拒绝。
- `mode == api_key` 且无 key 拒绝。

但底层 `packages/core/llm/client.py::_build_auth_env()` 只检查：

- `unset`：抛错。
- `api_key`：有 key 则注入 env。
- `oauth`：直接返回空的 `ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN`，强制走 `~/.claude`。

因此任何绕过 API 路由、直接调用 orchestrator 或 `LlmClient` 的路径，都可能在 OAuth 未完成时继续拉起 Claude CLI。

另外 `complete()` 捕获所有 `Exception` 后按 Opus -> Sonnet -> Haiku 降级，认证错误也会被当成模型失败降级，最终报 “all models in degradation chain failed”，用户难以判断真实原因。

### 影响

- 认证策略分散，容易出现新入口漏校验。
- OAuth 未登录时，非工具 API 调用会表现为模型失败，而不是明确认证失败。
- 认证错误会无意义触发多次模型降级/CLI 调用。

### 建议修复

1. 把完整认证校验下沉到 `_build_auth_env()`：
   - `unset`：抛 `AuthNotConfiguredError`
   - `api_key` 无 key：抛 `AuthNotConfiguredError`
   - `oauth` 无 `get_oauth_logged_in_at()`：抛 `AuthNotConfiguredError`
2. API 层 412 闸门可以保留，用于更早返回友好错误。
3. `LlmClient.complete()` 中遇到 `AuthNotConfiguredError` 不要降级，直接抛出。
4. 如果 Claude CLI 返回明确认证错误，也应识别后停止降级。

### 验收点

- 直接调用 `LlmClient.complete()` 时，如果 OAuth 未完成，应立即抛认证错误。
- API 层和非 API 层的错误文案一致。
- 认证错误不会触发 Opus/Sonnet/Haiku 多轮降级。

## P1-3 TDR 两套提交入口行为不一致

### 位置

- `apps/api/main.py`
  - `/tdr`：约 `177-186`
  - `/api/tdr/submit`：约 `283-319`
- `packages/tdr/review.py`
  - `TdrReview.add_comment()`：约 `25-49`

### 现象

`/api/tdr/submit` 对 UI 常见的简短评论格式做了兼容，例如：

- `text`
- `comment`
- `severity`

并补齐：

- `id`
- `dimension`
- `location`
- `observation`
- `suggestion`

但旧入口 `/tdr` 仍然直接执行：

```python
for c in req.comments:
    ws.add_comment(**c)
```

而 `TdrReview.add_comment()` 要求完整字段：

```python
id, dimension, severity, location, observation, suggestion
```

如果调用 `/tdr` 时传入 `{ "severity": "major", "text": "xxx" }` 这类简短评论，会触发 `TypeError` 并返回 500。

### 影响

- 同样是 TDR 提交，两个入口接受的数据格式不同。
- UI 路径正常，API/测试/外部调用路径仍可能 500。
- 后续维护容易只修其中一个入口。

### 建议修复

1. 抽取公共函数，例如：

```python
def _normalize_tdr_comment(c: dict[str, Any]) -> dict[str, Any] | None:
    ...
```

2. `/tdr` 和 `/api/tdr/submit` 都复用该函数。
3. 空评论跳过。
4. 字段无法兼容时返回 422，不要返回 500。
5. 为 raw `/tdr` 添加集成测试：
   - 完整字段评论。
   - 简短字段评论。
   - 空评论。
   - 非法评论字段返回 422。

### 验收点

- `/tdr` 和 `/api/tdr/submit` 对评论格式表现一致。
- 简短评论不会导致 500。
- 错误输入返回可读 422。

## P2-1 TDR 报告落盘路径和列表扫描路径不一致

### 位置

- `packages/tdr/workstation.py`
  - `self._storage = Path(settings.report_output_dir) / "tdr" / run_id`：约 `26`
  - `review.json` 写入：约 `91-95`
- `apps/api/main.py`
  - `/api/tdr/reviews`：约 `222-256`
  - `/api/reports`：约 `7183-7235`

### 现象

`TdrWorkstation.finalize()` 把报告写到：

```text
<report_output_dir>/tdr/<run_id>/review.json
```

但是：

- `/api/tdr/reviews` 只扫描：

```python
base.glob("tdr_*.json")
```

- `/api/reports` 只扫描：

```python
out_dir.glob("*.json")
```

而 `/api/tdr/submit` 又额外 mirror 到：

```text
<report_output_dir>/tdr_<run_id>.json
```

所以 UI 提交能被列表发现，raw `/tdr` 提交不能被发现。

### 影响

- `/tdr` 返回成功，但 TDR 历史列表和报告中心看不到。
- 同一个 TDR review 有两种存储形态，后续导出、清理、检索都容易漏。

### 建议修复

任选一种统一策略：

方案 A：统一使用顶层文件：

```text
<report_output_dir>/tdr_<run_id>.json
```

方案 B：统一使用目录结构：

```text
<report_output_dir>/tdr/<run_id>/review.json
```

并修改 `/api/tdr/reviews` 和 `/api/reports` 同时扫描该结构。

推荐方案 B，更适合未来一个 run 下放附件、签名、证据文件。

### 验收点

- `/tdr` 提交后能在 `/api/tdr/reviews` 出现。
- `/api/tdr/submit` 提交后也能出现。
- `/api/reports` 能列出 TDR 报告。
- 不产生重复记录。

## P2-2 TDR 页面存在 XSS 风险

### 位置

- `apps/api/main.py`
  - `renderResult(data)`：约 `1093-1119`
  - `loadHistory()`：约 `1123-1152`

### 现象

TDR 页面使用 `innerHTML` 直接拼接以下动态数据：

- `dimension` key
- `comment.id`
- `comment.severity`
- `comment.location`
- `comment.observation`
- `comment.suggestion`
- `follow_up_items`
- `data.path`
- `run_id`
- `decision`

示例：

```javascript
<div style="margin-top:4px">${c.observation}</div>
```

如果评论内容是：

```html
<img src=x onerror=alert(1)>
```

页面会执行脚本。

### 影响

- 本地管理页面可被报告内容或用户输入注入脚本。
- 如果报告文件来自导入、共享或自动生成内容，风险更高。
- 可读取当前页面可访问的数据，发起本地接口请求。

### 建议修复

1. 在内嵌 JS 中统一定义：

```javascript
function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}
```

2. 所有插入 HTML 的动态文本都包 `escapeHtml()`。
3. 动态 class 不要直接使用原值，做白名单映射：

```javascript
const allowedSeverity = new Set(["info", "minor", "major", "blocker"]);
```

4. URL/path 拼接时使用 `encodeURIComponent()`。
5. 能用 `textContent` 的地方优先用 DOM API，不用 `innerHTML`。

### 验收点

- 评论里输入 `<script>alert(1)</script>` 页面只显示文本，不执行。
- `run_id` 包含特殊字符时历史链接仍正常。
- severity/decision 非法值不会污染 class。

## P2-3 `/api/fetch-url` 可抓取任意本机/内网 URL

### 位置

- `apps/api/main.py`
  - `/api/fetch-url`：约 `7482-7503`

### 现象

接口允许任意 `http://` 或 `https://` URL，并且：

- 跟随重定向。
- 没有 host allowlist。
- 没有阻止 localhost/private IP/link-local。
- 返回响应文本前 200K。

这意味着它可以被用来读取：

- `http://127.0.0.1:<port>/...`
- `http://localhost:<port>/...`
- `http://192.168.x.x/...`
- `http://10.x.x.x/...`
- 云环境下的 metadata 地址，例如 `169.254.169.254`。

### 影响

- 本地 App 可能被用作内网探测/读取代理。
- 如果页面存在 XSS，该接口会放大风险。
- 用户误粘贴内网地址时，敏感内容会进入工具输入框和 evidence。

### 建议修复

1. 解析 URL host，DNS 解析到 IP 后判断网段。
2. 默认禁止：
   - loopback
   - private
   - link-local
   - multicast
   - unspecified
3. 跟随重定向后也要重新校验最终 URL。
4. 可以加显式 allowlist 或高级开关。
5. 限制响应 Content-Type 和最大字节数，不要只截断字符串。

### 验收点

- `http://127.0.0.1:8081/healthz` 默认被拒绝。
- `http://localhost:8081/healthz` 默认被拒绝。
- 重定向到内网地址时被拒绝。
- 正常公网 HTTPS 文档仍可抓取。

## P2-4 文件上传先整文件读入内存

### 位置

- `apps/api/main.py`
  - `/api/extract-file`：约 `7432-7479`

### 现象

接口一开始执行：

```python
blob = await file.read()
```

后面只限制提取后的 `text` 最大 400K，没有限制上传文件本身大小。大文件会完整进入内存。

### 影响

- 大 PDF/XLSX/二进制文件可能造成内存飙升。
- 多个并发上传可能拖垮本地 App。

### 建议修复

1. 设置服务端最大上传大小，例如 20MB 或 50MB。
2. 优先检查 `Content-Length`。
3. 流式读取并累计大小，超过限制立即 413。
4. 对不同类型设置不同限制：
   - 文本：较小。
   - PDF/DOCX/XLSX：可略大。
   - 图片：只做元信息时限制更小。

### 验收点

- 超过限制的文件返回 413。
- 正常小文件仍能提取。
- 返回错误文案明确说明最大大小。

## P3-1 Workflow evidence 归档可能持久化敏感数据

### 位置

- `packages/workflow/base.py`
  - `_archive_substep()`：约 `276-292`

### 现象

每个 substep 会写入：

- `system_rendered.md`
- `response_raw.txt`
- `parsed.json`

其中 `system_rendered.md` 可能包含：

- 用户上传的需求。
- API 文档。
- 业务规则。
- 账号/token 示例。
- 测试环境地址。
- 内部接口信息。

### 影响

- 敏感信息长期留存在本地 evidence 目录。
- 用户可能不知道这些原文会被持久化。
- 如果报告目录被共享或上传，可能泄露数据。

### 建议修复

1. 增加配置开关：

```text
EVIDENCE_ARCHIVE_MODE=off|failure_only|full
```

2. 默认建议 `failure_only` 或 `off`。
3. 对常见敏感字段做脱敏：
   - `sk-ant-...`
   - `Authorization: Bearer ...`
   - `cookie`
   - `token`
   - `password`
4. 在 UI 中提示 evidence 会落盘。
5. 提供清理 evidence 的入口。

### 验收点

- 默认模式下不会无提示保存完整 prompt。
- 失败时仍有足够诊断信息。
- API Key、Bearer token、Cookie 被脱敏。

## 建议修复顺序

1. 修 OAuth 全局凭据删除问题。
2. 把完整认证校验下沉到 `LlmClient`。
3. 统一 TDR comment normalize。
4. 统一 TDR 报告落盘和扫描路径。
5. 修 TDR 页面 XSS。
6. 限制 `/api/fetch-url` 内网访问。
7. 限制 `/api/extract-file` 上传大小。
8. evidence 归档加开关和脱敏。

## 建议新增测试

### 认证

- `mode=unset` 时 `LlmClient.complete()` 直接抛认证错误。
- `mode=api_key` 但 key 为空时直接抛认证错误。
- `mode=oauth` 但没有 `oauth_logged_in_at` 时直接抛认证错误。
- 认证错误不触发模型降级。

### TDR

- `/tdr` 支持完整评论字段。
- `/tdr` 支持简短评论字段。
- `/tdr` 空评论跳过。
- `/api/tdr/submit` 和 `/tdr` 输出结构一致。
- 两个入口生成的报告都能被 `/api/tdr/reviews` 发现。

### 安全

- TDR 评论中包含 HTML 时页面不执行脚本。
- `/api/fetch-url` 拒绝 localhost/private IP。
- `/api/fetch-url` 拒绝重定向到 private IP。
- `/api/extract-file` 超过大小限制返回 413。

## 手动验收清单

- 打开 App 设置页，已有 Claude CLI 登录态时，点击 OAuth 登录不会直接退出本机 Claude。
- OAuth 登录取消/失败后，终端执行 `claude` 仍保持原可用状态。
- 未登录时运行工具，前端显示明确 412 提示。
- TDR 工作台提交 `{severity, text}` 评论成功。
- TDR 历史能看到 UI 提交和 raw API 提交的报告。
- TDR 评论输入 `<img src=x onerror=alert(1)>` 只显示文本。
- URL 抓取公网文档成功，抓取 `127.0.0.1` 被拒绝。
- 上传超大文件被拒绝且 App 不崩。

## 备注

本 review 未做代码修改，也未跑完整自动化测试。请修复时优先保持现有产品行为不变，只收敛副作用和错误路径。
