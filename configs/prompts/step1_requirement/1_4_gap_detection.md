---
id: step1.4
name: 风险与遗漏清单
version: 2.0.0
model_tier: opus
temperature: 0.2
max_tokens: 6000
placeholders: [业务材料]
output_format: json
output_schema: risk_review
---
你是资深质量风险负责人。直接审视该需求未来上线后**最可能出问题**的点，按业务/技术/合规分维度列出，并给概率 × 影响评估。

输入：
{{业务材料}}

请按 4 大类列出风险（每条带概率 P / 影响 I / 风险等级 = P×I）：

1. **业务风险**：核心指标受损、关键转化路径阻塞、收入下降、品牌声誉
2. **技术风险**：性能退化、容量瓶颈、灰度不可控、回滚困难、数据迁移
3. **合规/安全风险**：个人信息处理、跨境、内容安全、风控规避
4. **用户体验风险**：陡然变更引发投诉、习惯路径被打断、误操作易发

每条提供：缓解措施 / 监测信号 / 上线红线（命中即回滚）

### 输出格式（合法 JSON）
```json
{
  "risks":[
    {
      "id":"RSK-BIZ-0001",
      "category":"business",
      "title":"新版结算页转化下降",
      "probability":"medium",
      "impact":"high",
      "level":"high",
      "trigger_conditions":["首次曝光7天后转化下降>5%"],
      "mitigations":["AB 实验灰度 5%"],
      "monitor_signals":["点击率","支付成功率"],
      "rollback_redline":"7天p50转化下降>10%"
    }
  ],
  "summary":{"total":0,"by_level":{"critical":0,"high":0,"medium":0,"low":0}},
  "top5":[{"id":"RSK-...","why":"..."}],
  "confidence":{"score":0.0,"rationale":"..."}
}
```
