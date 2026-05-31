你在做**一对** UI 比对:附图里 `role=设计稿` 是目标设计,`role=实拍` 是实际界面。把这一对**仔细比对**,找出实拍偏离设计的**每一处**差异。

对照维度:布局/间距/对齐、字号字重、颜色(主色/背景/文字)、圆角阴影、图标尺寸、元素**缺失/多余**、文案是否一致、组件状态(空态/加载/错误)。可感知差异(错位>4px / 文案错 / 颜色明显不对)不要标 cosmetic。**绝不在设计稿图上标问题**,所有 bbox 都标在实拍图上。

只输出一个合法 JSON(无多余文字):
```json
{
  "issues": [
    {"title":"一句话点明差异","severity":"critical|high|medium|low|info","priority":"P0|P1|P2|P3",
     "current_behavior":"实拍里实际什么样","expected_behavior":"设计稿要求什么样",
     "viewport_filename":"<抄实拍 caption 里 viewport_filename= 后那段>","bbox":[x,y,w,h],
     "fix_suggestion":"具体怎么改"}
  ]
}
```
没有差异就返回 `{"issues":[]}`。真实差异一条都不能漏,但不要硬凑。若没有 `role=设计稿`(该实拍无对应设计帧),返回 `{"issues":[{"title":"该界面无对应设计稿帧,未能比对","severity":"info","viewport_filename":"...","bbox":[0,0,0,0]}]}`。
