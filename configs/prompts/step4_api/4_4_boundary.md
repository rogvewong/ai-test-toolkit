---
id: step4.4
name: 接口边界与异常测试
version: 2.0.0
model_tier: sonnet
temperature: 0.25
max_tokens: 7000
placeholders: [业务材料]
output_format: json
output_schema: api_boundary
---
你是资深接口测试工程师。直接为以下接口生成**边界值 + 异常处理**测试用例。

输入：
{{业务材料}}

逐接口逐字段覆盖：

1. **数值边界**：null / 0 / -1 / 最小值 / 最大值 / 最大值+1 / 整数溢出
2. **字符串边界**：空串 / 单字符 / 最大长度 / 超长 / 特殊字符 / emoji / RTL / 零宽空格
3. **数组边界**：空数组 / 单元素 / 最大长度 / 重复元素 / null 元素
4. **日期/时间**：最早 / 最晚 / 跨时区 / 闰年 2/29 / DST / 未来时间 / 过去时间
5. **枚举**：合法所有值 / 非枚举值 / 大小写 / 中英文混
6. **Content-Type**：错误的 Content-Type / 缺失 / 多余字段
7. **请求大小**：body 1 byte / 1MB / 超过限制（如 10MB）
8. **并发**：同一资源同 idempotency-key 同时多次
9. **乱序请求**：未登录直接调写接口 / 终态资源再调修改
10. **依赖故障**：DB 不可用 / Redis 超时 / 第三方 5xx → 接口该返回什么
11. **超时**：服务端处理超过 5s 的兜底
12. **非幂等重试**：客户端 retry 时服务端如何防重

每条用例字段：
- id（BND-XXX-NNNN）
- endpoint + field（field 为空时表示影响整条请求）
- input
- expected_status
- expected_error_code
- expected_message_pattern
- severity_if_fails

### 输出格式（合法 JSON）
```json
{
  "cases":[
    {
      "id":"BND-ORD-4001",
      "endpoint":"POST /api/order",
      "field":"body.qty",
      "input":0,
      "expected_status":422,
      "expected_error_code":"qty_must_be_positive",
      "expected_message_pattern":"^数量必须大于0$",
      "severity_if_fails":"high"
    },
    {
      "id":"BND-ORD-4002",
      "endpoint":"POST /api/order",
      "field":"<request_body_size>",
      "input":"10MB JSON",
      "expected_status":413,
      "severity_if_fails":"medium"
    }
  ],
  "summary":{"total":0,"by_kind":{}},
  "confidence":{"score":0.0,"rationale":"..."}
}
```
