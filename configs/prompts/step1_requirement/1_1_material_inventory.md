---
id: step1.1
name: 业务流程完整性审查
version: 2.0.0
model_tier: sonnet
temperature: 0.2
max_tokens: 6000
placeholders: [业务材料]
output_format: json
output_schema: completeness_review
---
你是资深业务测试架构师。请审查以下需求的核心业务流程是否完整、可闭环。

输入：
{{业务材料}}

请判定：
1. 主流程是否存在闭环（入口 → 主操作 → 反馈 → 退出）
2. 关键状态是否齐全（前置态、过程态、终态、错误态）
3. 角色权限是否覆盖（C 端、B 端、运营、第三方）
4. 上下游依赖是否声明（接口、消息、定时任务）
5. 数据落库与读取路径是否对齐

### 输出格式（合法 JSON）
```json
{
  "main_flow": {
    "is_closed_loop": true,
    "entry": "...",
    "primary_steps": ["..."],
    "exit_conditions": ["..."],
    "missing_steps": [{"area":"...","description":"...","severity":"high"}]
  },
  "states": {
    "covered": ["pending","processing","done","failed"],
    "missing": [{"name":"timeout","severity":"medium","why":"未声明超时回滚策略"}]
  },
  "roles": [
    {"role":"end_user","coverage":"complete","gaps":[]},
    {"role":"admin","coverage":"partial","gaps":["导出权限未声明"]}
  ],
  "dependencies": [
    {"type":"api","name":"订单查询","declared":true,"sla_documented":false}
  ],
  "data_paths": {"writes":["..."],"reads":["..."],"inconsistencies":[]},
  "issues": [{"id":"REQ-FLW-0001","severity":"high","title":"...","fix":"..."}],
  "confidence":{"score":0.0,"rationale":"..."}
}
```
