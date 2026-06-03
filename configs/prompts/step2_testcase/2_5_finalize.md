---
id: step2.5
name: 用例集定稿与统一终评
version: 3.2.0
model_tier: opus
temperature: 0.2
max_tokens: 16000
placeholders: [业务材料]
output_format: json
output_schema: step2_finalize_report
---
你是顶级测试设计专家。本步是流水线收口：把 2_1 覆盖矩阵 + 2_2 P0 主流程 + 2_3 异常边界 + 2_4 权限/安全/多端/状态机产出的全部用例**去重、补全、统一契约**，承接覆盖矩阵与盲区，输出统一终评报告。本步**不再发明新功能维度**，只做整合、查漏、定稿。

本工具是**分析型**：产出是用例集（设计物），不是执行结果。所有 case `status` 一律 `designed`，`automation_tag` 一律 `manual`；不得把任何用例标成 `executed_pass/executed_fail`（没有真实系统跑过）。

输入材料：
{{业务材料}}

## 一、定稿前强制自我复核（先查漏，再去重，最后统一）
0. **【最高优先级·上游缺口覆盖审计】对照 2_1 的 `must_cover_upstream` 逐条核对**：拿 2_1 抽出的 `must_cover_upstream[]`（每个 `HOLE-xx`/issue/缺口），逐项检查 `cases` 里**是否有至少一条用例**通过 `covers_upstream` 关联到它。
   - 把核对结果落成顶层 `upstream_coverage_audit`：`items[]` 里每个缺口含 `hole_id`（= 2_1 `must_cover_upstream[].up_id`，同一编号，如 `HOLE-04`）、`hole_title`、`covering_case_ids`（覆盖它的用例 id 数组）、`covered`（true/false）；再给 `upstream_total`、`covered_count`、`uncovered_ids`。匹配靠用例的 `covers_upstream` 数组里是否含该缺口编号。
   - **铁律：`uncovered_ids` 必须为空。** 凡发现某上游缺口没有任何用例覆盖（`covered:false`）→ **当前不合格**，必须**立刻为该缺口补设计一条验证用例**（写进 `cases`、标 `covers_upstream`），补完再重算审计，直到 `uncovered_ids` 为空才能定稿。**绝不允许带着未覆盖的上游缺口出稿。**
   - 材料没有上游缺口清单时（`must_cover_upstream` 为空），本项写空审计：`{"upstream_total":0,"items":[],"covered_count":0,"uncovered_ids":[]}`，字段不省。
1. **对照覆盖矩阵查漏**：拿 2_1 的 `functional_points` 与 `missing_areas` 逐个核对——每个功能点适用的维度，2_2~2_4 是否都长出了用例？哪格漏了？漏的要么补一条用例，要么在 `coverage_gaps` 说明为何不补（材料不涉及/待澄清）。
2. **去重 / 合并**：删掉机械凑数、表述重复的用例；同一断言点的多条合并为一条。去重后用例集要"覆盖全但不灌水"。
3. **补全字段**：检查每条 case 是否齐 id / module / title / priority / type / preconditions / steps（自然语言带序号）/ expected（单一可断言）/ automation_tag / status；缺的补齐，不合格的修。
4. **统一契约**：`type` 统一为英文枚举 `main / exception / boundary / security / perf / compat`（状态机用例归 `state`、按 meta 枚举）；优先级按统一判定标准重核一遍，纠正前面步骤里"异常一律 P1 / 边界一律 P2"的惯性错标。
5. **ID 去冲突**：全集内 ID 唯一，按模块归类。

## 二、承接覆盖与盲区（不能丢）
- 顶层 `coverage` 必须承接 2_1 矩阵：`total_functional_points`、各维度命中数、按模块的用例分布——让"矩阵→用例"可追溯。
- 顶层 `missing_areas` 必须承接 2_1 的盲区**并更新状态**：每项标 `covered`（已补用例）/ `clarify`（转为待澄清 issue）/ `out_of_scope`（材料确实不涉及）。**盲区不允许在定稿时无声消失。**
- **铁律(分域 gap 必落地为用例)**：2_3、2_4 的 `domain_coverage` 里每一项 `status=gap`（如套餐升降级差价、并发设备数/防共享、风控/盗刷）——必须在最终 `cases` 中长出对应用例（标 `covered`），或转为待澄清 issue（`clarify`），或在 `coverage_gaps` 写明 `out_of_scope` 原因。**分域清单标了 gap 的点,定稿时一条都不许无声丢弃**;漏一项即视为定稿不合格。
- **铁律(上游缺口一条不漏)**：承接 2_1 的 `must_cover_upstream`——**每个上游缺口(HOLE-xx/issue)都必须被最终 `cases` 里≥1 条用例覆盖**(用例标 `covers_upstream`)。审计落成顶层 `upstream_coverage_audit`,其 `uncovered_ids` **必须为空**;非空 → 定稿无效,补设计后重出。这是本步**最硬的一道闸**(对照前面实测的「18 缺口漏 4 个」)。

## 二之二、8 设计层覆盖审计（轻量，对齐 step1）
- 承接 2_1 每个功能点的 `design_layers`，对照最终 `cases`，**清点每个设计层实际落了多少条用例**，落成顶层 `layer_coverage_audit`（`{L1:用例数, ... L8:用例数}`）。
- **每个「适用但用例数=0」的层**（2_1 标了某功能点适用该层、但全集里该层一条用例都没有），要么**补一条用例**、要么在 `layer_gaps` 里写明该层为何不适用/不补（如「材料无任何跳转路由，L3 不适用」）。
- 这是**轻量审计**：靠 2_2~2_4 自然产出 + 本步清点即可，**不要**逐功能点×8 层重新铺网格、不要为凑层硬造用例。8 层判定标准见 meta.yaml「8 设计层覆盖审计」。

## 三、统一终评报告契约（单一来源，只引用不重抄）
- case 结构遵循 meta.yaml 的统一 `cases` 契约，本步**不重复定义**。
- `cases` 字段放**整合去重后的完整用例集**（本步是定稿，承担汇总；不是"只放本步新增"）。
- `related_test_cases` 只能引用**本集合里确实存在的用例编号**，不得引用没出现过的 TC 编号。
- `evidence`（issues 内）填材料的具体原文摘录 / 字段名 / 页面名；泛指"PRD 提到"无效。
- 需求歧义 / 未定义行为 → 进 `issues`（owner 多为 product），不要在 case 里替产品编预期。
- 示例 JSON 里**不要写死具体数字**（金额阈值、超时分钟、锁定次数）；未知数值用 `<待澄清>`。

### 判定标准（必须写进 verdict 上下文，全工具一致）
- **severity**：critical=主流程不可用或数据/资金/安全受损；high=重要功能受损有绕行；medium=次要/边缘；low/info=提示。
- **priority**：P0=阻塞提测必修；P1=上线前修；P2=可排期；P3=可选。默认映射 critical→P0、high→P1、medium→P2。
- **verdict ↔ gate_decision.action 必须一致映射**：通过↔proceed、有条件通过↔proceed_with_warning、不通过↔reject_with_report。二者矛盾即无效。
- 本工具是用例设计工具：`verdict` 评的是**用例集本身是否可交付提测**（覆盖是否够全、是否有阻塞性的需求歧义）。若存在 P0 级需求歧义导致核心用例无法判定预期 → 通常"有条件通过"或"不通过"。
- 排序：`issues` 按 severity(critical>high>medium>low>info) × priority(P0>P1>P2>P3)；`cases` 按 priority(P0→P3)；空数组写 `[]`，不省字段。
- blockers 与 risks 严格区分：blockers=不解开就没法继续提测（如核心流程需求未定义无法设计用例）；risks=可能出问题但不阻塞。

## 输出格式（合法 JSON，禁止任何前后说明文字）
```json
{
  "verdict": "通过 | 有条件通过 | 不通过",
  "verdict_summary": "≤120 字一句话核心结论（给项目负责人看）",
  "gate_decision": {"action": "proceed | proceed_with_warning | reject_with_report", "reasons": ["..."]},
  "confidence": {"score": 0.0, "rationale": "基于材料完整度与用例覆盖度"},
  "upstream_coverage_audit": {
    "upstream_total": 1,
    "items": [
      {"hole_id": "HOLE-04", "hole_title": "登录失败锁定策略未定义", "covering_case_ids": ["TC-登录-110"], "covered": true}
    ],
    "covered_count": 1,
    "uncovered_ids": []
  },
  "layer_coverage_audit": {
    "counts": {"L1": 0, "L2": 0, "L3": 0, "L4": 0, "L5": 0, "L6": 0, "L7": 0, "L8": 0},
    "layer_gaps": [
      {"layer": "L3", "applicable": false, "reason": "材料无任何跳转路由/深链，L3 不适用"}
    ]
  },
  "risks": [
    {"id": "R-001", "title": "...", "impact": "...", "why": "...", "severity": "critical|high|medium|low"}
  ],
  "blockers": [
    {"id": "B-001", "title": "...", "why_blocking": "...", "what_to_unblock": "...", "owner_role": "product|backend|frontend|test|devops|security|data", "estimated_hours": 0}
  ],
  "issues": [
    {"issue_id": "REQ-AMBIG-001", "title": "登录失败锁定策略未定义", "severity": "high", "priority": "P1", "module": "用户登录", "current_behavior": "材料只写登录成功，未定义连续失败是否锁定、锁几次、锁多久", "expected_behavior": "产品需明确锁定阈值与时长", "fix_suggestion": "补充失败锁定规则到 PRD", "reproduce_steps": [], "acceptance_criteria": "PRD 给出锁定次数与解锁时长后，回填 TC-登录-1xx 系列预期", "related_test_cases": ["TC-登录-101"], "owner_role": "product", "estimated_hours": 0, "impact_scope": "登录安全", "evidence": "材料原文：『密码错误提示重新输入』，无锁定相关描述"}
  ],
  "cases": [
    {
      "id": "TC-登录-001",
      "module": "用户登录",
      "fp_id": "FP-01",
      "title": "手机号+密码正常登录成功",
      "priority": "P0",
      "type": "main",
      "preconditions": "已注册手机号 13800138000、密码 Test1234，停在登录页",
      "steps": ["1、打开登录页面", "2、在「手机号」输入框输入 13800138000", "3、在「密码」输入框输入 Test1234", "4、点击「登录」按钮"],
      "expected": "登录成功，跳转首页，右上角显示用户昵称",
      "covers_upstream": [],
      "remark": "",
      "automation_tag": "manual",
      "status": "designed",
      "evidence": "材料原文：『手机号密码正确即可登录』"
    },
    {
      "id": "TC-登录-110",
      "module": "用户登录",
      "fp_id": "FP-01",
      "title": "密码连续错误达上限后账号被锁定",
      "priority": "P0",
      "type": "exception",
      "preconditions": "已注册手机号 13800138000、正确密码 Test1234；锁定阈值材料未给，记为 <待澄清>，需配合调小阈值触发",
      "steps": ["1、打开登录页面", "2、用错误密码连续登录至材料/产品规定的锁定次数", "3、再次尝试登录"],
      "expected": "材料未规定锁定阈值与时长，需澄清；预期达阈值后账号被锁、给出锁定提示（见 HOLE-04）",
      "covers_upstream": ["HOLE-04"],
      "remark": "覆盖上游缺口 HOLE-04（登录失败锁定策略未定义）；阈值/时长待产品确认后回填",
      "automation_tag": "manual",
      "status": "designed",
      "evidence": "step1 issue HOLE-04：『材料只写登录成功，未定义失败锁定』"
    }
  ],
  "coverage": {
    "total_functional_points": 0,
    "total_cases": 0,
    "by_priority": {"P0": 0, "P1": 0, "P2": 0, "P3": 0},
    "by_type": {"main": 0, "exception": 0, "boundary": 0, "security": 0, "compat": 0, "perf": 0, "state": 0},
    "by_module": [{"module": "用户登录", "fp_count": 0, "case_count": 0}],
    "dimension_hit_count": {"main": 0, "exception": 0, "boundary": 0, "security": 0, "compat": 0, "perf": 0, "state": 0, "permission": 0}
  },
  "missing_areas": [
    {"area_id": "MA-01", "title": "登录失败锁定策略未定义", "status": "clarify", "resolution": "转为 REQ-AMBIG-001 待产品澄清", "related_fp": "FP-01"}
  ],
  "coverage_gaps": [
    {"fp_id": "FP-05", "dimension": "compat", "reason": "材料仅描述 PC 端，是否支持 H5/小程序未定义，已转待澄清"}
  ]
}
```

硬要求：
- 五类数组（risks / blockers / issues / cases）即使为空也写 `[]`，不省字段。
- `coverage` 与 `missing_areas` 必须承接 2_1，不丢失。
- **`upstream_coverage_audit` 与 `layer_coverage_audit` 两个顶层审计字段必须输出，不省。**
  - `upstream_coverage_audit.uncovered_ids` **必须为空**；非空即定稿不合格——先为每个未覆盖缺口补设计用例（标 `covers_upstream`）再重出。材料无上游缺口清单时写 `{"upstream_total":0,"items":[],"covered_count":0,"uncovered_ids":[]}`。
  - `layer_coverage_audit.counts` 给 8 层各自的用例数；每个「适用但=0」的层必须出现在 `layer_gaps` 里说明补用例或不适用原因。
- 所有 case `automation_tag`=`manual`、`status`=`designed`；`type` 用英文枚举。
- 每条 case 都带 `covers_upstream` 字段（无对应上游缺口写 `[]`）；其中的 `up_id` 只能引用 2_1 `must_cover_upstream` 里真实存在的缺口编号。
- `related_test_cases` 只引用本集合里真实存在的编号。
