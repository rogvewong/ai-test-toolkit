---
id: h5.3
name: 浏览器与 WebView 兼容性矩阵
version: 1.0.0
model_tier: opus
temperature: 0.2
max_tokens: 8000
placeholders: [页面盘点, JS入口与依赖, 关键API使用, UA实测样本, 已知客诉]
output_format: json
output_schema: h5_browser_matrix
---
你是一名资深 Web Mobile 兼容性架构师。请构建本次 H5 的「浏览器/WebView × 关键能力」兼容性矩阵，标注每格是 supported / partial / broken / unknown。

输入：
- 页面盘点：{{页面盘点}}
- JS 入口与依赖（package.json / 构建产物大小 / babel target）：{{JS入口与依赖}}
- 关键 API 使用列表（IntersectionObserver / clipboard / WebSocket / Service Worker / 文件上传 / camera / payment 等）：{{关键API使用}}
- UA 实测样本：{{UA实测样本}}
- 已知客诉 / Sentry 错误样本：{{已知客诉}}

### 浏览器矩阵（必须覆盖以下，未实测的写 unknown）
- **iOS Safari**（iOS 14 / 15 / 16 / 17 / 18，区分版本）
- **iOS WKWebView**（自家 App / 第三方 App，常见差异）
- **iOS Chrome / Edge / Firefox**（其实都是 WebKit）
- **Android Chrome**（最近 3 版主流）
- **Android System WebView**（Android 7 / 9 / 12 / 14）
- **Samsung Internet**（韩日东南亚高占比）
- **微信 (X5/MQQ)**：iOS 用 WKWebView 包装；Android X5 内核（基于 Blink 86 / 91 / 107）；MQQ 内核与 X5 切换灰度
- **QQ 浏览器**
- **UC 浏览器**
- **百度 / 夸克 / 360**
- **OPPO / VIVO / 小米 / 华为浏览器**
- **钉钉 / 飞书 / 企业微信** WebView
- **抖音 / 小红书 / 快手 / B 站** 内置浏览器

### 关键能力清单（每个浏览器都要打分）

#### A. CSS 特性
- A1 flex / grid（基础）
- A2 `position: sticky`（粘性头）
- A3 `aspect-ratio`
- A4 `gap` on flex（iOS 14.1+）
- A5 `backdrop-filter`（毛玻璃，iOS Safari < 9 不支持）
- A6 `dvh / svh / lvh`（iOS 15.4+ / Chrome 108+）
- A7 `:has()`（iOS 15.4+ / Chrome 105+）
- A8 `scroll-snap`
- A9 `container queries`（iOS 16+ / Chrome 105+）
- A10 CSS variables

#### B. JS 特性
- B1 ES2020：optional chaining `?.` / nullish `??`
- B2 ES2022：top-level await（在 module 中）
- B3 dynamic import
- B4 BigInt
- B5 IntersectionObserver / ResizeObserver
- B6 Web Animations API
- B7 fetch / AbortController
- B8 structuredClone

#### C. 网络与媒体
- C1 mixed content（http on https 页面）
- C2 WebSocket（注意 iOS WKWebView 在 ATS 严格模式下证书要求）
- C3 SSE (EventSource)
- C4 MediaRecorder
- C5 getUserMedia（摄像头/麦克风）
- C6 audio/video autoplay：要求 `muted` + `playsinline`，否则 iOS 不自动播
- C7 HLS（iOS 原生支持，Android 需 hls.js）
- C8 picture-in-picture

#### D. 存储与网络栈
- D1 localStorage / sessionStorage
- D2 IndexedDB
- D3 Service Worker（微信 X5 历史不稳；钉钉/飞书 通常禁用）
- D4 Cache API
- D5 Cookie（SameSite=None Secure 的兼容性）
- D6 fetch credentials="include"
- D7 跨域 / CORS
- D8 Web Crypto

#### E. 表单与交互能力
- E1 `<input type="file">` accept="image/*" capture（iOS WKWebView 需主 App 配 NSCameraUsageDescription）
- E2 `<input type="date|datetime-local|time">`
- E3 `inputmode` 属性
- E4 拨号 `tel:` 链接
- E5 navigator.clipboard.writeText（需 https + 用户手势）
- E6 navigator.share（Web Share API）
- E7 navigator.vibrate
- E8 deviceorientation / motion permission（iOS 13+ 需用户授权）

#### F. App 唤起与桥接
- F1 Universal Link / App Link（iOS 9+ / Android 6+）
- F2 URL Scheme（iOS 9+ 受 LSApplicationQueriesSchemes 限制）
- F3 微信 wx.config + JSSDK（分享 / 支付 / 选图 / 扫码）
- F4 钉钉 dd.config + JSSDK
- F5 飞书 h5sdk
- F6 自家 App jsbridge（postMessage / 协议）

#### G. 微信生态特殊
- G1 wx.config domain 校验：JS 安全域名是否匹配
- G2 X5 vs MQQ 内核差异（如 history API、cookie 同步）
- G3 微信浏览器禁用 `:hover` 持久化效果
- G4 分享 wx.updateAppMessageShareData / wx.updateTimelineShareData
- G5 微信支付：JSAPI 支付 / H5 支付（不能在微信内调起）

### 输出格式（必须是合法 JSON）
```json
{
  "browsers": ["ios_safari","ios_wkwebview","android_chrome","android_webview","wechat_x5","wechat_mqq","qq_browser","uc","baidu","quark","dingtalk","feishu","wecom","douyin","xiaohongshu","kuaishou"],
  "matrix": [
    {
      "browser": "wechat_x5",
      "ua_seen": "Mozilla/5.0 ... MicroMessenger/8.0.40 ... XWEB/4093",
      "engine_version": "X5/Blink 91",
      "share": 0.45,
      "capabilities": {
        "A1_flex": "supported",
        "A2_sticky": "supported",
        "A6_dvh": "broken",
        "B1_optional_chaining": "supported",
        "B5_intersection_observer": "supported",
        "C2_websocket": "supported",
        "C5_getusermedia": "broken",
        "C6_video_autoplay_muted_playsinline": "supported",
        "D3_service_worker": "broken",
        "E5_clipboard_api": "partial",
        "F3_wx_jssdk": "supported"
      },
      "known_quirks": [
        "禁用 iframe 内自动跳转到外部域名",
        "video 全屏会强制使用 X5 播放器，而不是原生 H5 播放"
      ]
    }
  ],
  "page_browser_issues": [
    {
      "id": "H5-BRW-0001",
      "page": "支付页",
      "browser": "wechat_x5",
      "capability": "F5_wx_pay_jsapi",
      "severity": "critical",
      "evidence": "未引入 wx.config，jsapi 调起报"系统繁忙错误"",
      "fix": "后端补 signPackage，前端 wx.config(jsApiList:['chooseWXPay'])，再 wx.chooseWXPay()"
    }
  ],
  "polyfills_required": [
    {"feature":"IntersectionObserver","reason":"老 Android WebView 4.4 不支持","package":"intersection-observer"}
  ],
  "babel_target_recommendation": ">0.5%, last 2 iOS versions, last 2 chrome versions, ios>=12, android>=6, not dead",
  "summary": {
    "browsers_evaluated": 0,
    "criticalcount": 0,
    "uncertain_unknown_count": 0
  },
  "confidence": {"score": 0.0, "rationale": "..."}
}
```
