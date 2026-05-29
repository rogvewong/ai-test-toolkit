---
id: h5.4
name: 触摸、手势、表单、滚动审计
version: 1.0.0
model_tier: sonnet
temperature: 0.2
max_tokens: 8000
placeholders: [页面盘点, 关键交互组件, 表单清单, 已知客诉]
output_format: json
output_schema: h5_interaction
---
你是一名资深 H5 交互工程师。请按以下 6 大维度逐页审计触摸、手势、表单、滚动、媒体唤起的适配质量。

输入：
- 页面盘点：{{页面盘点}}
- 关键交互组件（轮播 / 抽屉 / 选择器 / 弹窗 / 自定义键盘 / map / video / signature）：{{关键交互组件}}
- 表单清单（每个表单的 input 类型、validation、提交方式）：{{表单清单}}
- 已知客诉 / 客户反馈：{{已知客诉}}

### 1. 触摸目标 (tap target)
- iOS Human Interface 推荐 ≥ 44×44pt；Android Material 推荐 ≥ 48×48dp
- 实际目标尺寸不应仅依赖图标的视觉尺寸，而是含 padding 后的可点击区
- 相邻按钮间距 ≥ 8px，避免误触
- 仅图标按钮必须有可读 `aria-label`
- 链接和按钮的 hover 状态在 mobile 上不持久（iOS 双 tap 才触发 click 是因为 hover 锁定）

### 2. 手势冲突
- 横向轮播（swipe）与纵向滚动（scroll）的方向锁
- 抽屉 / drawer 拉手 vs 内容滚动
- pull-to-refresh：iOS Safari 顶部回弹会触发刷新（如果 body 滚动顶部），需用 `overscroll-behavior: contain` 或固定 body
- 长按选择文字 vs 长按上下文菜单（图片保存 / 复制图）
- 双击放大（地图 / 图片）vs 业务双击事件
- 捏合缩放（用户体验上需禁用）：viewport meta 的 user-scalable=no（但破坏无障碍）；或在容器上 `touch-action: pan-x pan-y`

### 3. 表单与键盘
对**每一个 input** 必须检查以下项：

- **type / inputmode 准确**：
  - 手机号：`type="tel" inputmode="numeric"`
  - 身份证：`inputmode="numeric"` + 自定义校验
  - 数字金额：`inputmode="decimal"`
  - 邮箱：`type="email"`
  - URL：`type="url"`
  - 搜索：`type="search"`
  - 密码：`type="password"`，注意微信内置浏览器对密码框可能弹"使用微信密码"提示
  - 多行：`<textarea>` 而非多个 input

- **autocomplete**：
  - 登录页：username / current-password / new-password
  - 收货地址：street-address / postal-code / tel
  - 信用卡：cc-number / cc-exp / cc-csc / cc-name
  - 一次性密码：one-time-code（iOS 12+ 自动从短信抓 OTP）
  - autocorrect="off" / autocapitalize="off" / spellcheck="false" 适合用户名 / 验证码

- **iOS 自动放大防御**：所有 input 字号 ≥ 16px

- **键盘弹起遮挡**：
  - 当前聚焦输入是否被键盘遮挡
  - 推荐：focus 时调 `el.scrollIntoView({block:'center', behavior:'smooth'})`
  - 或使用 `visualViewport.height` 动态设置容器 padding-bottom
  - 安卓键盘 resize viewport（Chrome 可加 `interactive-widget=resizes-content`）；iOS 不会 resize

- **键盘"完成 / 下一项"**：
  - 多 input 表单的 `enterkeyhint`：done / go / next / search / send
  - tabindex 顺序合理

- **clear 按钮**：iOS 默认有，Android 需要自己实现 ×

- **Number 输入特殊**：`type="number"` 在 iOS 会出现上下箭头但 Android 没；建议改用 `inputmode="numeric"` + `pattern="\d*"`

- **日期 / 时间**：原生 `type="date"` 在不同浏览器交互差异大，关键场景建议自研选择器

- **粘贴 / 复制**：
  - 验证码框是否支持 `autocomplete="one-time-code"`（iOS）
  - 长内容粘贴是否触发 maxlength 截断

### 4. 滚动
- **滚动惯性**（iOS 5+）：`-webkit-overflow-scrolling: touch`（iOS 13- 兼容）；现代用 `overflow-y: auto` 默认有
- **嵌套滚动**：弹窗 / 列表内嵌套 scroller，是否触发祖先的滚动联动；建议 `overscroll-behavior: contain`
- **弹窗内滚动锁 body**：弹窗打开时 body 不应滚动，关闭后位置恢复
- **scroll-snap**：轮播 / 时间线（兼容性见矩阵）
- **滚动位置恢复**：浏览器返回时 / tab 切换 / 后台回前台
- **scroll restoration**：路由返回时顶部位置 vs 上次位置

### 5. 媒体与系统能力调用
- **拍照 / 相册**：`<input type="file" accept="image/*" capture="environment">` (camera) 或 capture="user" (front cam)；多选 `multiple`
- **相册选择**：iOS WKWebView 在自家 App 内默认禁用，需 App 配 NSPhotoLibraryUsageDescription
- **拨号**：`<a href="tel:13800001234">`
- **短信**：`sms:13800001234?body=...`
- **复制到剪贴板**：navigator.clipboard.writeText（https + 用户手势）；旧浏览器 fallback execCommand('copy')
- **分享**：navigator.share（H5 原生分享 API）；微信内必须用 wx.updateAppMessageShareData
- **下载文件**：`<a download>` 在 iOS Safari 不一定有效；建议后端 Content-Disposition

### 6. 唤起 App
- **Universal Link**（iOS）：apple-app-site-association 配置；用户点击立刻跳转，不弹"打开"对话框
- **App Link**（Android）：assetlinks.json
- **URL Scheme fallback**：myapp://path —— 注意 iOS 9+ 必须列入 LSApplicationQueriesSchemes 才能用
- **微信内禁止 scheme 唤起**：用户必须先点"在浏览器打开"
- **iframe scheme trick**：旧手段，现代浏览器多禁用
- **降级**：未安装 App 时 fallback 到下载页

### 输出格式（必须是合法 JSON）
```json
{
  "pages": [
    {
      "page_id": "H5-SCP-0010",
      "tap_targets": {
        "min_size_ok": false,
        "violations": [{"selector":".carousel .dot","actual":"12px","expected":"≥44px","severity":"high"}]
      },
      "gesture_conflicts": [
        {"area":"商品图轮播","conflict":"horizontal swipe vs page scroll","severity":"medium","fix":"touch-action: pan-y on container"}
      ],
      "forms": [
        {
          "form": "login",
          "inputs": [
            {
              "selector": "input[name=phone]",
              "type": {"current":"text","recommended":"tel"},
              "inputmode": {"current":"none","recommended":"numeric"},
              "autocomplete": {"current":"off","recommended":"username"},
              "ios_zoom_safe": false,
              "issues": [
                {"id":"H5-INT-0001","severity":"high","title":"字号 13px 触发 iOS 缩放","fix":"改 16px"},
                {"id":"H5-INT-0002","severity":"medium","title":"键盘弹起遮挡按钮","fix":"focus 时 scrollIntoView({block:'center'})"}
              ]
            }
          ],
          "submit_method": "fetch POST",
          "double_submit_protected": false
        }
      ],
      "scroll": {
        "nested_scroll_isolated": true,
        "popup_locks_body": false,
        "scroll_restoration_ok": "unknown"
      },
      "media_capabilities": {
        "file_upload": "supported",
        "camera_capture": "untested",
        "clipboard_write": "broken_in_wechat_old"
      },
      "app_open": {
        "method": "universal_link",
        "fallback": "down_app",
        "wechat_compat": "needs_user_browser_open_hint"
      }
    }
  ],
  "issues": [
    {"id":"H5-INT-0010","severity":"critical","page":"支付页","title":"键盘遮挡支付按钮且不能滚动","fix":"focus 时 scrollIntoView + visualViewport 监听","fix_effort_hours":2}
  ],
  "summary": {"total_issues":0,"by_severity":{"critical":0,"high":0,"medium":0,"low":0}},
  "confidence": {"score": 0.0, "rationale": "..."}
}
```
