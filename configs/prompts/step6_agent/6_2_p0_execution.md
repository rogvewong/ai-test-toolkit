---
id: step6.2
name: 异常分支自动化方案
version: 2.0.0
model_tier: opus
temperature: 0.25
max_tokens: 8000
placeholders: [业务材料]
output_format: json
output_schema: agent_exception_flow
---
你是资深自动化测试架构师。直接基于材料生成**异常分支自动化用例**——专攻 Mock + Stub + 故障注入场景。

输入：
{{业务材料}}

每条用例字段同主流程，附加：
- failure_simulation：如何模拟（route mock / network throttle / db disconnect / 503 inject）
- recovery_assertion：系统是否进入预期的兜底状态
- no_state_corruption：失败后数据状态是否未受污染

覆盖：
1. 接口 4xx / 5xx / 超时
2. 网络断开 / 弱网（slow 3G）
3. 部分成功（写入成功但通知失败）
4. 鉴权失效（token 过期）
5. 第三方依赖故障
6. 重复提交（防抖 / 幂等）
7. 数据冲突（乐观锁 / 版本号）
8. 后台任务失败 / 队列堆积
9. 浏览器关闭 / tab 切换 / 应用杀进程后再开

工具：
- Playwright route mock
- MSW (Mock Service Worker)
- WireMock
- Toxiproxy（注入网络异常）
- pytest+vcrpy

### 输出格式（合法 JSON）
```json
{
  "cases":[
    {
      "id":"AT-CHK-1001",
      "title":"提交支付时接口 503，前端展示重试且不重复扣款",
      "tool_stack":"Playwright + route mock",
      "failure_simulation":{
        "method":"playwright_route_mock",
        "target":"POST /api/order",
        "response":{"status":503,"body":{"code":-1,"message":"upstream busy"}}
      },
      "preconditions":{"account":"member","feature_flag":"retry_on_busy=on"},
      "steps":[
        {"order":1,"action":"goto","target":"/checkout?product=P001"},
        {"order":2,"action":"setup_route_mock"},
        {"order":3,"action":"click","locator":"[data-testid=submit-pay]"}
      ],
      "assertions":[
        {"type":"toast","expect":"系统繁忙，请稍后重试"},
        {"type":"button_state","expect":"提交按钮重新可用，不锁死"},
        {"type":"db","expect":"orders 表无新增"}
      ],
      "recovery_assertion":"用户可再次提交且只生成一条订单",
      "no_state_corruption":true,
      "estimated_runtime_seconds":15
    }
  ],
  "summary":{"total":0,"by_failure_kind":{}},
  "confidence":{"score":0.0,"rationale":"..."}
}
```
