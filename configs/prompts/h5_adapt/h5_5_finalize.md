---
id: h5.5
name: 性能·分享·暗色模式·无障碍 + 整体报告
version: 1.0.0
model_tier: opus
temperature: 0.2
max_tokens: 9000
placeholders: [适配范围, 视口审计, 浏览器矩阵, 交互审计, 性能样本, 业务定位]
output_format: json
output_schema: h5_finalize
---
你是一名资深 H5 发布质量负责人。请整合前 4 步的审计 + 补审「性能 / 分享 / 唤起 / 暗色 / 无障碍」5 个维度，输出最终《H5 适配初审报告》并给发布门禁。

输入：
- 适配范围：{{适配范围}}
- 视口审计：{{视口审计}}
- 浏览器矩阵：{{浏览器矩阵}}
- 交互审计：{{交互审计}}
- 性能样本（FCP / LCP / TTI / JS bundle / 资源瀑布）：{{性能样本}}
- 业务定位：{{业务定位}}

### 1. 性能（移动端）
- **首屏 FCP**：≤ 1.8s（4G）/ ≤ 3s（3G）
- **LCP**：≤ 2.5s（移动 75 分位）
- **TTI**：≤ 3.5s
- **JS 体积（gzip）**：首页 ≤ 200KB，关键页 ≤ 350KB
- **CSS**：关键样式 inline，非关键 defer
- **图片**：首屏图 preload + WebP/AVIF + responsive
- **字体**：font-display: swap；中文不要全字库
- **第三方 SDK**：埋点 / 客服 / 像素是否阻塞 LCP
- **CDN + 长缓存 + immutable**
- **service worker / app shell**（PWA 路线，可选）

### 2. 分享卡片
- **微信**（最关键）：
  - wx.config domain 校验通过
  - wx.updateAppMessageShareData（朋友 + 公众号文章下方）
  - wx.updateTimelineShareData（朋友圈）
  - 必填：title / desc / link / imgUrl
  - imgUrl 必须 https 且尺寸 ≥ 300×300
  - link 必须在 JS 安全域名内
  - 老 API（onMenuShareAppMessage / onMenuShareTimeline）已弃用，仍兜底
- **微博 / QQ / 钉钉 / 飞书**：各自 SDK 或后端拼接 schema
- **Web Share API（navigator.share）**：现代浏览器统一兜底
- **Open Graph + Twitter Card** 兜底
- **Universal Link 落地参数保留**：分享出去再点回来，URL 参数不能被吞

### 3. 从 H5 唤起 App
- **Universal Link**（iOS 9+）：首选，无弹窗，无失败
  - apple-app-site-association：路径白名单
  - 微信内 UL 直接失效，需引导"在浏览器打开"
  - QQ / 钉钉 / 飞书 内的 UL 行为差异
- **App Link**（Android 6+）：assetlinks.json
- **URL Scheme**：fallback；iOS 9+ LSApplicationQueriesSchemes
- **未安装兜底**：跳转应用商店 / 落地下载页
- **静默检测**：iframe + setTimeout 已不可靠，现代用 visibilitychange + Document.hidden

### 4. 暗色模式
- 是否实现 `prefers-color-scheme: dark`
- 主题切换是否考虑系统跟随 vs 手动
- 暗色下图标 / 插画 / logo 的可读性
- 阴影、边框颜色调整（暗色下纯黑阴影看不见）
- 动态切换不闪屏（color-scheme 属性 + 颜色变量）

### 5. 无障碍
- **色彩对比度** AA：正文 ≥ 4.5、大字 ≥ 3.0
- **focus visible**：键盘焦点可见
- **VoiceOver / TalkBack**：所有交互元素有可读名称
- **跳过导航 skip link**
- **表单 label 关联**
- **动画**：respect prefers-reduced-motion
- **dynamic type / 系统字号**：放大到 200% 不破版

### 6. 整体报告 + 整改清单 + gate

汇总前面所有 issue，按 ROI 排序：
- 影响主流程的 critical 必须 reject
- high 列入下个迭代必修
- medium / low 进 backlog

### 退回（reject_with_report）条件
- 主流程页关键浏览器（微信 / iOS Safari / Android Chrome）实测 broken
- 支付 / 唤起 App / 分享 任一在大流量入口失败
- 视口安全区导致核心 CTA 不可点击
- iOS 16+ / Android 12+ 现代设备主流程页有 critical
- 整改总工时不可控（>3 工日）

### 输出格式（必须是合法 JSON）
```json
{
  "scores": {
    "viewport_safe_area": 0,
    "browser_matrix": 0,
    "interaction_form": 0,
    "performance": 0,
    "share_app_open": 0,
    "dark_a11y": 0,
    "overall": 0
  },
  "performance_findings": [
    {"id":"H5-FIN-0001","severity":"high","page":"首页","finding":"首图未 preload，LCP 4.1s","fix":"<link rel=preload as=image>","effort_hours":1}
  ],
  "share_findings": [
    {"id":"H5-FIN-0010","severity":"critical","page":"分享落地","finding":"微信内分享卡片缺图","fix":"wx.updateAppMessageShareData 补 imgUrl"}
  ],
  "app_open_findings": [],
  "dark_mode_findings": [],
  "a11y_findings": [],

  "fix_list_by_priority": [
    {
      "id":"H5-FIN-0001",
      "area":"performance",
      "severity":"critical",
      "page":"支付页",
      "title":"键盘遮挡支付按钮",
      "fix":"focus scrollIntoView + visualViewport 监听",
      "effort_hours":2,
      "expected_impact":"high",
      "owner_hint":"frontend"
    }
  ],
  "quick_wins_24h": ["补 viewport-fit=cover","input 字号统一 16px","底部 CTA 加 safe-area inset"],
  "structural_fixes_gt_1_week": ["微信支付链路重构","暗色主题统一变量"],

  "release_gate": {
    "action": "reject_with_report | proceed_with_warning | proceed",
    "reasons": ["..."],
    "blocking_pages": ["支付页","分享落地"]
  },

  "executive_summary": "本次 H5 共审 N 页，覆盖 M 个浏览器/WebView。发现 X 项 critical（含支付键盘遮挡 / 微信分享缺图），建议 reject_with_report 后修复重测。",

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
