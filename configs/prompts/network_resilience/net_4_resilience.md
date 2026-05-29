---
id: net.4
name: 重试与幂等测试
version: 2.0.0
model_tier: opus
temperature: 0.2
max_tokens: 7000
placeholders: [业务材料]
output_format: json
output_schema: net_retry_idempotent
---
你是资深可靠性工程师。直接给出**重试 + 幂等**专项测试用例。

输入：
{{业务材料}}

请覆盖：

1. **重试策略**
   - 是否区分幂等 vs 非幂等接口（写不能盲重试）
   - 指数退避 1s/2s/4s/8s 是否使用
   - 退避加抖动（避免雷霆雪崩）
   - 最大重试次数（建议 3）
   - 全局熔断（连续失败暂停一段时间）

2. **幂等**
   - 写接口必须携带 idempotency-key（UUID 或客户端生成）
   - 服务端识别相同 key 返回首次结果
   - key 的有效期（24h/7d）
   - 不同请求体相同 key：服务端应拒绝（避免数据污染）

3. **重试风暴**
   - 服务端 503 持续，客户端不应循环死打
   - 多实例同时重试时是否限流（漏桶 / 令牌桶）

4. **业务侧重试**
   - 用户主动点重试 vs 自动重试的区别
   - 重试时进度条是否累计

5. **测试方法**
   - Toxiproxy 注入 5xx
   - 后端 mock 慢响应 + 间歇成功
   - 验证 idempotency-key 重复时的 DB 行为

### 输出格式（合法 JSON）
```json
{
  "cases":[
    {
      "id":"NTW-RTY-3001",
      "kind":"idempotency_key_dedup",
      "target":"POST /api/order",
      "test_method":"同一 idempotency-key 连发 5 次",
      "expected":{
        "first_request":"201 Created order_id=X",
        "subsequent_requests":"200 + 同一 order_id（不创建新订单）",
        "db_rows_added":1
      },
      "severity_if_fails":"critical"
    },
    {
      "id":"NTW-RTY-3010",
      "kind":"exponential_backoff",
      "target":"POST /api/order with 503",
      "test_method":"前 3 次返回 503，第 4 次成功",
      "expected":{
        "retry_intervals_ms":[1000,2000,4000],
        "jitter_present":true,
        "final_state":"order created on 4th",
        "no_storm":true
      },
      "severity_if_fails":"high"
    }
  ],
  "summary":{"total":0,"by_kind":{}},
  "confidence":{"score":0.0,"rationale":"..."}
}
```
