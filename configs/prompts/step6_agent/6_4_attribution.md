---
id: step6.4
name: 失败归因与稳定性
version: 2.0.0
model_tier: sonnet
temperature: 0.2
max_tokens: 7000
placeholders: [业务材料]
output_format: json
output_schema: agent_attribution
---
你是资深自动化稳定性专家。直接基于材料给出**失败归因模型 + flaky 控制**方案——降低误报率。

输入：
{{业务材料}}

请输出：

1. **归因分类规则**
   - product_bug：代码缺陷（断言失败 / 接口返回错误）
   - env_issue：环境问题（DB 连不上 / 服务启动失败）
   - data_issue：测试数据被改 / 脏数据
   - script_issue：locator 失效 / 等待时间不足
   - flaky：偶发不稳定（无法稳定复现）
   - third_party：依赖第三方故障

2. **归因依据**
   - 错误堆栈关键词（Timeout / 404 / 500 / DOM not found）
   - 失败时间分布（夜间集中失败大概率环境问题）
   - 重试是否通过（重试通过 → flaky）
   - 历史失败率
   - 是否伴随基础设施告警

3. **flaky 治理**
   - 失败 → 自动重试 1 次（CI 层面）
   - 连续 3 次失败才告警
   - 失败截图 + trace + 网络日志全保留
   - flaky 率 > 5% 的用例标 quarantine
   - 周报输出 flaky 排行榜

4. **失败可定位性**
   - 日志含 trace_id / request_id
   - 截图按步骤命名
   - 录屏分割到失败点
   - 失败上下文（用户、时间、机器、版本）

5. **告警与升级**
   - 主流程失败 → 立即 IM 告警
   - 普通失败 → 日报
   - 致命失败（数据污染）→ 中断 CI

### 输出格式（合法 JSON）
```json
{
  "classification_rules":[
    {"signal":"Timeout 30000ms exceeded","attribution":"flaky","action":"auto_retry"},
    {"signal":"AssertionError: status 500","attribution":"product_bug","action":"alert"},
    {"signal":"connect ECONNREFUSED","attribution":"env_issue","action":"hold_and_alert_devops"}
  ],
  "flaky_strategy":{
    "auto_retry_count":1,
    "consecutive_fail_threshold":3,
    "quarantine_flaky_rate":">5%",
    "evidence_retention_days":30
  },
  "diagnosis_artifacts":["trace_id","screenshots","video","har","db_snapshot"],
  "alerting":{
    "main_flow_fail":"im_realtime",
    "normal_fail":"daily_digest",
    "data_corruption":"halt_pipeline"
  },
  "stability_kpis":{
    "ci_pass_rate_target":">98%",
    "flaky_rate_target":"<2%",
    "p95_runtime_target":"<8min"
  },
  "confidence":{"score":0.0,"rationale":"..."}
}
```
