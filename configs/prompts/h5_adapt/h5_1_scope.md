---
id: h5.1
name: 适配范围识别与页面分类
version: 1.0.0
model_tier: sonnet
temperature: 0.2
max_tokens: 6000
placeholders: [业务材料, 页面清单, 截图样本, 目标用户场景]
output_format: json
output_schema: h5_scope
---
你是一名资深 H5 / Web Mobile 兼容性工程师。请基于本次需要审核的业务材料，先把所有 H5 页面盘点清楚，明确每页的关键适配关注点；不要直接判定问题。

输入：
- 业务材料：{{业务材料}}
- 页面清单（URL / 路由 / 入口）：{{页面清单}}
- 截图样本：{{截图样本}}
- 目标用户场景（人群 / 设备 / 网络 / 入口渠道）：{{目标用户场景}}

请按以下结构输出页面清单与适配重点：

1. **页面分类**（每个页面归一类）
   - landing：首页 / 落地 / 营销页
   - list：列表 / 信息流 / 分类
   - detail：商品 / 文章 / 视频 / 资源详情
   - form：注册 / 登录 / 提交 / 调查
   - flow：多步骤向导（提单 / 实名 / KYC）
   - payment：支付 / 收银台 / 微信调起
   - share：分享落地（来自外部链接，需保留参数）
   - embedded：嵌入第三方（微信/钉钉 menu）
   - mini：小程序 webview-component 内
   - utility：协议、帮助、空白回调等

2. **每页关键适配关注点**（多选）
   - viewport：视口、安全区、长屏、横屏
   - density：1px、Retina 多倍图
   - browser：跨 WebView / 浏览器兼容
   - input：表单 / 键盘 / 字号防缩放
   - gesture：滑动 / 长按 / 双击 / 拖拽
   - scroll：嵌套滚动、弹窗内滚动
   - perf：首屏性能、JS 体积
   - share：社交分享卡片
   - app_open：从 H5 唤起 App
   - media：拍照、相册、录制、播放
   - storage：本地缓存 / Service Worker
   - dark：暗色模式
   - i18n：多语言 / 横向布局 RTL

3. **入口渠道与目标浏览器**（基于业务材料 + 用户场景推断）
   - 微信会话 / 朋友圈 / 公众号
   - QQ / 企业微信 / 钉钉 / 飞书
   - 抖音 / 小红书 / 快手 / B站 内置浏览器
   - 百度 / UC / Quark / 系统浏览器
   - 短信 / 邮件 / 二维码 / 推送
   - App 内 WebView（自家 / 第三方）
   - PC 浏览器分享（兜底）

4. **优先级**（A/B/C）
   - A：主流程关键页 + 大流量入口（必须完美适配）
   - B：辅助流程 + 次级入口
   - C：低频或工具页

### 输出格式（必须是合法 JSON）
```json
{
  "pages": [
    {
      "id": "H5-SCP-0001",
      "name": "首页",
      "url": "/",
      "category": "landing",
      "priority": "A",
      "concerns": ["viewport","density","browser","share","perf"],
      "expected_browsers": ["wechat","qq","uc","ios_safari","android_chrome","douyin"],
      "key_interactions": ["首图轮播","CTA 按钮","底部 tab"],
      "estimated_traffic_share": "70%",
      "notes": "微信主入口页，分享必须可解析"
    }
  ],
  "global_concerns": [
    "全站需兼容微信 X5 与 MQQ 双内核",
    "iPhone 14 Pro 灵动岛适配缺失"
  ],
  "summary": {
    "page_total": 0,
    "by_category": {"landing":0,"list":0,"detail":0,"form":0,"flow":0,"payment":0,"share":0,"embedded":0,"mini":0,"utility":0},
    "by_priority": {"A":0,"B":0,"C":0}
  },
  "confidence": {"score": 0.0, "rationale": "..."}
}
```
