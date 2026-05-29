---
id: step4.5
name: 接口契约 / 兼容性测试
version: 2.0.0
model_tier: sonnet
temperature: 0.2
max_tokens: 7000
placeholders: [业务材料]
output_format: json
output_schema: api_contract
---
你是资深契约与兼容性测试工程师。直接基于以下接口资料生成**契约 + 跨版本兼容**用例——保证服务变更不破坏调用方。

输入：
{{业务材料}}

请覆盖：

1. **JSON Schema 校验**
   - 每个响应字段：类型、是否必填、format（uuid/date-time/uri/email）、enum 范围、min/max
   - 嵌套对象 / 数组 item schema
   - 老字段是否仍存在（向后兼容）

2. **请求版本兼容**
   - 老客户端调新接口：缺新增字段 → 后端默认值是否合理
   - 新客户端调老接口：传新字段 → 是否被忽略而不报错

3. **响应版本兼容**
   - 新增字段：必须是 nullable 或 optional，不能是 required
   - 删除字段：必须先标 deprecated 一个版本
   - 字段重命名：必须双写一段时间

4. **错误码契约**
   - 错误码列表是否齐全（每种业务失败都有专属 code）
   - 错误码不会复用不同语义
   - 错误响应结构稳定（{code, message, request_id}）

5. **Header 契约**
   - 业务必备 header（X-Trace-Id / X-Request-Id / X-Tenant-Id）是否声明
   - rate-limit header (X-RateLimit-Remaining / Retry-After)
   - 缓存 header (Cache-Control / ETag / Last-Modified)

6. **i18n / 时区**
   - 错误 message 是否支持多语言
   - 时间字段是否带时区（ISO 8601 with offset）

7. **接口降级**
   - 接口变更前后对调用方影响：哪些客户端需要同步发版
   - 灰度策略：是否有 feature flag 可关闭新行为

每条用例字段：id（CTR-XXX-NNNN）/ kind / target / expected / severity_if_fails

### 输出格式（合法 JSON）
```json
{
  "cases":[
    {
      "id":"CTR-ORD-5001",
      "kind":"schema_response",
      "target":"GET /api/order/{id} response",
      "expected":"data.amount_cents 必填，integer，>=0；data.status enum [pending,paid,cancelled,done]",
      "severity_if_fails":"high"
    },
    {
      "id":"CTR-ORD-5010",
      "kind":"backward_compat",
      "target":"老客户端不传新字段 currency",
      "expected":"后端默认 CNY 处理，返回成功",
      "severity_if_fails":"critical"
    }
  ],
  "schema_summary":{
    "endpoints_with_schema":0,
    "missing_schema":[]
  },
  "breaking_changes":[
    {"endpoint":"POST /api/order","change":"required field added: idempotency_key","mitigation":"老客户端兼容 60 天双跑"}
  ],
  "summary":{"total":0,"by_kind":{}},
  "gate_decision":{
    "action":"reject_with_report | proceed_with_warning | proceed",
    "reasons":["..."]
  },
  "confidence":{"score":0.0,"rationale":"..."}
}
```

---

### 必须额外满足的"统一报告契约"

无论本工具特有的 schema 如何，本步骤(`*_5_*`/`finalize`)输出 JSON **必须**额外满足以下顶层契约（来自 meta.yaml 统一约束）：

```json
{
  "verdict": "通过 | 有条件通过 | 不通过",
  "verdict_summary": "≤120 字的一句话核心结论",
  "risks": [{"id":"R-001","title":"...","impact":"...","why":"...","severity":"high|medium|low"}],
  "blockers": [{"id":"B-001","title":"...","why_blocking":"...","what_to_unblock":"...","owner_role":"product|backend|frontend|test|devops|security|data","estimated_hours":0}],
  "issues": [{
    "issue_id":"...","title":"...",
    "severity":"critical|high|medium|low|info",
    "priority":"P0|P1|P2|P3",
    "module":"...","current_behavior":"...","expected_behavior":"...",
    "fix_suggestion":"...","reproduce_steps":[...],"acceptance_criteria":"...",
    "related_test_cases":[...],"owner_role":"...","estimated_hours":0,
    "impact_scope":"...","evidence":"..."
  }],
  "cases": [{
    "id":"...","title":"...","priority":"P0|P1|P2|P3","type":"main|exception|boundary|security|perf|compat",
    "preconditions":"...","steps":[...],"expected":"...",
    "automation_tag":"auto|semi_auto|manual",
    "status":"designed|executed_pass|executed_fail|skipped|blocked"
  }]
}
```

**硬要求**：
- 已有的工具特有字段保留不删，但必须额外补齐以上五个数组 + verdict / verdict_summary。
- `issues` 必须按 severity(critical>high>medium>low>info) × priority(P0>P1>P2>P3) 排序。
- `cases` 必须按 priority(P0→P3)排序。
- 空数组写 `[]`，不要省略字段。
- `blockers` 和 `risks` 严格区分：blockers = "不解开就不能继续"；risks = "可能出问题但不阻塞当前流程"。
