---
id: h5.2
name: 视口、安全区、像素密度、长屏审计
version: 1.0.0
model_tier: opus
temperature: 0.2
max_tokens: 8000
placeholders: [页面盘点, HTML头部样本, 关键样式片段, 截图样本]
output_format: json
output_schema: h5_viewport
---
你是一名资深 H5 视觉与排版兼容性专家。请审计每个页面在「视口 + 安全区 + 像素密度 + 长屏 / 横竖屏 / 折叠屏」维度上的适配质量。

输入：
- 页面盘点：{{页面盘点}}
- HTML 头部样本（每页 `<head>` 内 viewport / theme-color / meta 标签）：{{HTML头部样本}}
- 关键样式片段（外层容器、根字号、安全区使用、媒体查询）：{{关键样式片段}}
- 截图样本（多机型）：{{截图样本}}

请按 7 大维度逐页判定（每条给 status: pass / fail / warn / unknown，附 evidence 与 fix）：

### 1. viewport meta 标签
检查项：
- 是否存在 `<meta name="viewport">`
- 是否含 `width=device-width`（必须）
- `initial-scale=1`（必须）
- `maximum-scale=1` + `user-scalable=no`：会破坏无障碍缩放，仅在地图/特殊页可接受，普通业务页 → warn
- `viewport-fit=cover`：使用 env(safe-area-inset-*) 时必须；缺则安全区无效 → fail
- `interactive-widget=resizes-content`（Chrome 108+ 软键盘行为）：可选 info

### 2. 安全区（Safe Area）
- 顶部状态栏 / 灵动岛 / 刘海：固定头部是否使用 `padding-top: env(safe-area-inset-top)` 或在 `viewport-fit=cover` 下提升
- 底部 home indicator：固定底部按钮 / tab bar 是否使用 `padding-bottom: env(safe-area-inset-bottom)`，至少 `max(env(safe-area-inset-bottom), 12px)`
- 横屏左右安全区：横屏时 `env(safe-area-inset-left/right)` 是否考虑
- 弹窗 / actionsheet 是否避开 home indicator
- 旧机型回退：`constant(safe-area-inset-*)`（iOS 11.0–11.2）— 现代项目可忽略，但若声明支持 iOS 11 必须并存

### 3. 像素密度 / Retina
- 1px hairline：使用 `0.5px` / `transform: scaleY(0.5)` / `box-shadow inset 0 -1px` / SVG 等任一合理方案
- 图片资源：是否提供 `srcset` + `sizes`（移动端建议 1x/2x/3x）
- 大图是否使用 WebP / AVIF + 旧浏览器 JPEG fallback
- LCP 大图是否使用 `<link rel="preload" as="image">`
- 图标：优先 SVG > webfont > PNG@3x；不能仅 PNG@1x
- 是否声明 `<meta name="theme-color">` 适配 Android Chrome 状态栏

### 4. 长屏 / 折叠屏 / 横竖屏
- `100vh` 在 iOS Safari 工具栏出现 / 收起时会跳动 → 应使用 `100dvh` / `100svh` / `100lvh`，或 JS 监听 `visualViewport.resize`
- iPhone X / 14 Pro / 15 Pro Max 顶部刘海与灵动岛差异：banner 区域是否预留
- 折叠屏（Galaxy Z Fold/Flip）：max-width 限制、关键信息是否被分屏切断
- 平板（iPad）：是否过度拉伸；表单输入区是否限宽
- 横竖屏切换：弹窗位置 / 输入聚焦 / 滚动位置是否保留

### 5. 字号与根尺寸
- 是否使用 rem + 1rem = 100px / 16px / 1px = 1px scheme（任意但必须一致）
- iOS 防自动放大：所有 `<input>/<textarea>` 字号 ≥ 16px，否则会触发 zoom-in
- 不要在 `html` 上禁用 `font-size`，影响 dynamic type 与无障碍

### 6. 横向溢出
- 是否存在水平滚动条（除非有意）
- 子元素超出 100vw 未声明 overflow
- 表格 / 长字符串（URL、订单号）是否 `word-break: break-all` 或 `overflow-x: auto`

### 7. flex / grid 兼容
- 老版微信 X5（基于 Blink 53–77）的 flex 旧语法回退（`-webkit-box`）— 一般可忽略，但若声明覆盖 < Android 5.0 必须 verify
- `gap` 在 flex 上的兼容性（iOS Safari < 14.1 不支持）
- `aspect-ratio` 兼容性
- 子元素 min-width:0 trick（防 flex 溢出）

### 输出格式（必须是合法 JSON）
```json
{
  "pages": [
    {
      "page_id": "H5-SCP-0001",
      "viewport_meta": {
        "status": "warn",
        "value": "width=device-width,initial-scale=1,user-scalable=no",
        "evidence": "缺 viewport-fit=cover，导致 env() 安全区不生效",
        "fix": "改为 width=device-width,initial-scale=1,viewport-fit=cover",
        "severity": "high"
      },
      "safe_area": {
        "top": {"status":"pass"},
        "bottom": {"status":"fail","evidence":"底部 cta 与 home indicator 重叠","fix":"padding-bottom: max(env(safe-area-inset-bottom), 12px)","severity":"critical"},
        "horizontal_landscape": {"status":"unknown"},
        "popup_avoids_indicator": {"status":"warn"}
      },
      "pixel_density": {
        "hairline_1px": {"status":"pass","evidence":"使用 transform scaleY(0.5)"},
        "responsive_images": {"status":"warn","evidence":"首图仅 1x","fix":"加 srcset + WebP","severity":"high"},
        "icon_strategy": {"status":"pass"},
        "theme_color_meta": {"status":"info"}
      },
      "long_screen": {
        "uses_dvh_or_svh": {"status":"fail","evidence":"使用 100vh 在 Safari 滑动时抖动","fix":"换成 100dvh","severity":"medium"},
        "notch_dynamic_island": {"status":"unknown"},
        "foldable": {"status":"unknown"},
        "tablet": {"status":"warn","evidence":"iPad 上表单宽度撑满","fix":"设 max-width:560px","severity":"low"},
        "orientation_swap": {"status":"unknown"}
      },
      "font_sizing": {
        "input_font_size_ge_16": {"status":"fail","evidence":".form input 字号 13px","fix":"改 16px","severity":"high"},
        "rem_scheme_consistent": {"status":"pass"}
      },
      "horizontal_overflow": {"status":"pass"},
      "flex_grid_compat": {"status":"pass"}
    }
  ],
  "issues": [
    {"id":"H5-VPT-0001","page":"首页","severity":"critical","title":"底部按钮被 home indicator 遮挡","fix":"加 padding-bottom: env(safe-area-inset-bottom)","fix_effort_hours":1}
  ],
  "summary": {
    "total_pages": 0,
    "critical": 0,
    "high": 0,
    "medium": 0
  },
  "confidence": {"score": 0.0, "rationale": "..."}
}
```
