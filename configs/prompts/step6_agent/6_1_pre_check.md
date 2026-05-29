---
id: step6.1
name: 主流程自动化方案
version: 2.0.0
model_tier: opus
temperature: 0.2
max_tokens: 8000
placeholders: [业务材料]
output_format: json
output_schema: agent_main_flow
---
你是资深自动化测试架构师。直接基于以下材料生成**主流程自动化用例方案**——可被 Agent 或脚本直接执行。

输入：
{{业务材料}}

请输出每条主流程自动化用例：
1. id（AT-XXX-NNNN）
2. title
3. 工具栈（Playwright / Cypress / WebdriverIO / pytest+requests / Postman / Appium）
4. 前置：测试账号（角色、token 来源）/ 数据准备（fixtures、SQL、API 预置）/ 环境（host、feature flag）
5. 步骤：每一步含 action + locator/endpoint + payload + assertion
6. 断言点：URL / DOM 文本 / 接口响应 / 数据库 / 监控埋点
7. 清理：测试数据回收、状态复位
8. 失败截图 / 录屏 / trace
9. 估算运行时间

要求：
- 每条用例独立可运行（不依赖前一条）
- locator 优先 data-testid / aria-label / role，避免脆弱的 nth-child
- API 测试优先 schema 校验 + 关键字段断言
- 不出现"打开页面后凭感觉点击"，每一步都明确

### 输出格式（合法 JSON）
```json
{
  "cases":[
    {
      "id":"AT-CHK-0001",
      "title":"登录态用户提交订单成功",
      "tool_stack":"Playwright",
      "preconditions":{
        "account":"role=member, token from /test/login",
        "data":"product P001 库存>0",
        "env":"staging, feature_flag=new_checkout=on"
      },
      "steps":[
        {"order":1,"action":"goto","target":"/checkout?product=P001"},
        {"order":2,"action":"click","locator":"[data-testid=submit-pay]"},
        {"order":3,"action":"wait_for_response","endpoint":"POST /api/order","timeout_ms":5000}
      ],
      "assertions":[
        {"step":2,"type":"network","expect":"POST /api/order returns 201"},
        {"step":3,"type":"dom","expect":"页面 url 含 /pay/success"},
        {"step":3,"type":"db","expect":"orders table 有新行 status=pending"}
      ],
      "cleanup":["DELETE /api/order/{order_id}"],
      "evidence":["screenshot on each step","trace.zip"],
      "estimated_runtime_seconds":12
    }
  ],
  "summary":{"total":0,"by_tool":{}},
  "confidence":{"score":0.0,"rationale":"..."}
}
```
