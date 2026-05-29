---
id: step1.2
name: 边界与异常场景识别
version: 2.0.0
model_tier: sonnet
temperature: 0.25
max_tokens: 6000
placeholders: [业务材料]
output_format: json
output_schema: edge_case_review
---
你是资深测试架构师。请直接从需求里挖出**所有**容易被遗漏的边界与异常场景。

输入：
{{业务材料}}

逐类列出（每条带触发条件 / 预期行为 / 当前是否定义 / 严重度）：

1. **空值/初始态**：首次进入、无数据、列表为空、用户未授权
2. **极值**：最大长度、最大数量、最大金额、超长时间、过期
3. **非法值**：特殊字符、SQL/XSS、表情、双引号、零宽字符
4. **并发**：同一资源多次提交、双开页面、抢锁冲突
5. **状态切换异常**：跳跃状态、不可达状态、回退到非法态
6. **时序异常**：早于开始、迟于结束、跨日、夏令时
7. **网络/超时**：请求超时、半成功、回调丢失、重试雪崩
8. **权限切换**：会话过期、降权、跨账号操作
9. **设备/环境**：低存储、低电量、横竖屏、双 SIM、海外 IP

### 输出格式（合法 JSON）
```json
{
  "categories": [
    {
      "category": "extreme_value",
      "scenarios": [
        {
          "id":"EDG-EXT-0001",
          "trigger":"金额输入超过 10^9",
          "expected":"前端拦截 + 后端校验",
          "current_state":"undefined | partial | covered",
          "severity":"high",
          "evidence":"PRD 第 3 章未声明金额上限"
        }
      ]
    }
  ],
  "summary":{"total":0,"undefined":0,"by_severity":{"critical":0,"high":0,"medium":0,"low":0}},
  "top_risks":[{"id":"EDG-...","why":"..."}],
  "confidence":{"score":0.0,"rationale":"..."}
}
```
