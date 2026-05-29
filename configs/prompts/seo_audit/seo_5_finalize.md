---
id: seo.5
name: SEO 整改清单与发布建议
version: 1.0.0
model_tier: sonnet
temperature: 0.2
max_tokens: 6000
placeholders: [抓取审计, 元数据审计, 内容审计, 性能审计, 业务定位]
output_format: json
output_schema: seo_finalize
---
请整合上面 4 步的审计，输出一份《SEO 深度审计报告》，按优先级排出整改清单 + 发布建议 gate。

输入：
- 抓取审计：{{抓取审计}}
- 元数据审计：{{元数据审计}}
- 内容审计：{{内容审计}}
- 性能审计：{{性能审计}}
- 业务定位：{{业务定位}}

请按以下结构输出：

1. **总览健康分**（0–100）
   - 抓取健康 / 元数据 / 内容 / 性能 四个维度分别打分
   - 总分 = 加权平均（性能 30 / 元数据 25 / 内容 25 / 抓取 20）

2. **整改清单（按 ROI 排序）**
   - 每条：area / 严重度 / 描述 / 修复方案 / 估计工时（小时） / 上线后预期收益（low/medium/high）
   - 优先列影响搜索可见性 + 不需要大改造的项（"低成本高价值"）

3. **快速胜利（quick wins）**
   - 1 天内可上线的前 5 条修复（如：补 og:image / 修 robots.txt / 加 alt）

4. **结构化整改（>1 周）**
   - 需要架构调整或内容重写的项

5. **gate_decision**
   - 如果是新站点上线前审计：title 缺失 / robots 屏蔽全站 / sitemap 大量 404 等致命项 → reject_with_report
   - 如果是迭代审计：仅当出现"全站 noindex 误配 / canonical 错指 / 性能严重退化"时 reject

### 输出格式（必须是合法 JSON）
```json
{
  "scores": {
    "crawl": 0,
    "meta": 0,
    "content": 0,
    "performance": 0,
    "overall": 0
  },
  "fix_list": [
    {
      "id":"SEO-FIN-0001",
      "area":"meta",
      "severity":"critical",
      "title":"全站缺 og:image",
      "fix":"在 layout.tsx 注入默认 og:image",
      "effort_hours":2,
      "expected_impact":"high",
      "owner_hint":"frontend"
    }
  ],
  "quick_wins": ["..."],
  "structural_fixes": ["..."],
  "gate_decision": {
    "action": "reject_with_report | proceed_with_warning | proceed",
    "reasons": ["..."]
  },
  "executive_summary": "...",
  "confidence": {"score": 0.0, "rationale": "..."}
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
