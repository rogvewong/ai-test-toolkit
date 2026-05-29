---
id: net.5
name: 离线缓存与队列测试
version: 2.0.0
model_tier: sonnet
temperature: 0.25
max_tokens: 7000
placeholders: [业务材料]
output_format: json
output_schema: net_offline_cache
---
你是资深离线优先（offline-first）测试专家。直接生成**离线缓存 + 本地队列**用例 + 整体网络容灾结论。

输入：
{{业务材料}}

请输出：

1. **缓存策略测试**
   - 静态资源（图片/字体/JS/CSS）：CDN + Cache-Control
   - 业务数据：localStorage / IndexedDB / Cache API
   - 缓存命中率
   - 缓存过期 / 失效策略
   - 缓存一致性（数据更新后客户端如何感知）

2. **离线队列**
   - 关键写操作进本地队列（创建订单 / 草稿 / 评论）
   - 队列持久化（应用被杀也不丢）
   - 重连后批量同步且只同步一次
   - 同步顺序（FIFO / 业务优先级）
   - 同步失败的处理（保留 / 丢弃 / 提示用户）

3. **Service Worker（PWA）**
   - 是否启用
   - 离线 fallback page
   - 缓存策略（cache-first / network-first / stale-while-revalidate）

4. **空数据降级**
   - 离线时展示什么（缓存数据 + 时间戳标识 / 占位 / 拒绝访问）
   - 操作可点击性（不可写时按钮 disabled）

5. **整体网络容灾结论**
   - 主流程是否可在弱网/断网下闭环
   - 关键风险点（重复扣款 / 数据丢失 / 状态错乱）
   - gate_decision

### 输出格式（合法 JSON）
```json
{
  "cache_cases":[
    {"id":"NTW-CCH-4001","target":"商品列表","strategy":"network-first","ttl_seconds":300,"verify":"断网仍能展示上次列表+'离线缓存'标识"}
  ],
  "queue_cases":[
    {"id":"NTW-QUE-4101","target":"草稿提交","persistence":"IndexedDB","verify":"杀掉 App 重启后队列仍在"}
  ],
  "service_worker":{"enabled":false,"strategy":null,"offline_page":null},
  "degradation":{
    "offline_browse":true,
    "offline_write_buffered":false,
    "user_aware":true
  },
  "overall":{
    "main_flow_offline_works":false,
    "main_flow_recovery_works":true,
    "blocking_risks":["重复扣款风险（idempotency 缺失）"]
  },
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
