# MixPad 动态文字播报与音频调查

`2.0.0b10` 增加 `orvibo_smart_control.speak`，通过专用场景让 MixPad 播报每次传入的文字。
自定义音频文件和 HTTP(S) 音频 URL 仍未建立可用协议；媒体实体不声明 `PLAY_MEDIA`，
也不能作为 HA `tts.speak` 的播放目标。

## 准备专用场景

1. 在智家365 App 的“智能/场景 → 新建场景 → 添加执行动作”中选择目标 MixPad。
2. 只添加一条“语音播报”动作，填入测试文字。场景中不能包含灯光、音乐或其他动作。
3. 将场景命名为 `HA语音播报`，确保同一家庭没有其他同名场景，保存后手动执行一次。
4. 确认面板能念出文字，再在 HA 使用默认结合模式或纯云端模式调用下述服务。

用户已经确认 MixPad 7 Ultra / 固件 `5.4.5.500` 的官方 App 场景能发声。
本版 HA 动态文字请求已通过离线测试，仍需用户实机验证。

## 从 HA 调用

更新集成到 `v2.0.0b10` 后重启 Home Assistant。在“开发者工具 → 动作”执行：

```yaml
action: orvibo_smart_control.speak
data:
  scene_name: HA语音播报
  message: 欢迎回家
```

`scene_name` 必须与 App 中保存的名称完全一致。多个 ORVIBO 集成配置项时，
在 `data` 中再填写所需配置项的 `entry_id`，也可在界面通过配置项选择器选择。
可以在自动化的 `message` 中使用 HA 模板，但渲染结果必须是有效文字。

文字限制沿用 App 普通播报编辑器：非空、最多 300 个 UTF-16 单元，支持
`U+4E00..U+9FA5` 中文、英文字母、数字、空白和标点 `，。！？,.!?`。
不支持表情、SSML 或其他字符，超长或不符合规则时会报错，不自动截断。

每次调用会先读取最新场景配置，再保存新文字，收到匹配的云端成功响应后执行该场景。
App 中会保留最后一次播报文字。不要在调用期间从 App 编辑或手动执行此专用场景；
集成内部的并发调用会串行处理，无法锁住 App 或其他客户端的编辑。
不自动创建或删除场景，也不恢复先前文字或音乐播放状态。

如需读取服务响应，可添加 `response_variable`。成功的 `stage: speech_scene_executed`
表示云端确认了场景触发，不能证明面板已经发声。读取失败时不写入；更新未获确认时
不触发场景，但云端文字可能已保存；执行未获确认时，文字保留且场景可能已经执行。
集成不会自动重试这些写入。执行超时后先检查面板，再决定是否手动重试。
播报正文不写入集成日志或服务返回值；HA 自身的自动化追踪和 App 场景仍可能保存文字。

## 目标与证据范围

- 智家 365 APK `com.orvibo.homemate`，版本 `5.2.6.307`。
- APK SHA256：`6ea3f3eff41ad4657ca437d10c79022949a1f080af857a8800dc6b6819abac8c`。
- 用户面板只读发现返回型号 `MixPadG7U`、软件版本 `5.4.5.500`。
- 用户已确认这台设备在 App 的场景执行动作选择中有“语音播报”选项，并确认
  手动执行仅含一条语音动作的测试场景后，面板能正常念出文字。这是官方场景路径
  的真机反馈，尚未验证 HA 发送的请求或任意音频 URL。
- APK 仅做静态分析，没有执行。面板仅做发现、握手与有限服务检查，没有登录、
  播放、修改设置、创建场景或升级。

## 歌曲控制没有建立任意音频 URL 格式

Java 音乐请求使用 `cmd=289`。Flutter 桥将 `musicInfo`、`curMusic` 和 `musicArr`
原样转发，实际内容需继续追溯 Dart 模型，不能仅凭字段名构造 URL 请求。

补充离线 Blutter 分析匹配 Dart `3.9.2`、arm64 compressed pointers，
工具固定 commit `4a60ac648bf448c5a7596437243bcd0b9376fdf0`。
`libapp.so` SHA256 为 `88e70f38c571382247223efd02f396803bd874e974c59efb2a24a7d46f3b93ec`。

| 路径 | 恢复的序列化 | 结论 |
| --- | --- | --- |
| `playAndInsertSong`，`0x112e080` | `musicInfo=HMMusicSearchItem.toJson()`，19 项 | 歌曲 ID 与元数据；无音频 URL/路径字段 |
| `playAssignSong`，`0x1127e44` | `curMusic?.toJson()`，`musicArr.map(toJson)` | 动态 selector 已从快照确认；没有独立 URL 输入 |
| `playMusicType`，`0x12e6bf8` | `HmMusicProperty.toJson()`，16 项 | 歌曲 ID、状态和封面；无音频 URL/路径字段 |

`picUrl`、`imageUrl` 是封面信息。`MixPadLocal=10000` 是设备返回的本地歌曲列表来源，
没有证据说明它接受任意文件路径或 HTTP 地址。上述分析不能证明固件完全不支持 URL，
但不足以实现 `{url: ...}`、把 URL 塞入 `musicId` 或将封面字段当作播放源。

Java 桥 REA Evidence：
`ev_4c06e36504ac42a4f3c38c20b0b9a9d6b5b9cdefc5c781f87777a65732d5460b`、
`ev_297fca4f27b7a3e8d9fdf2b4d8bb30fce62619951a9107e46e240360266e84cf`。
Dart 地址来自独立 Blutter 输出，不另造 REA Evidence ID。

## 已实现的场景文字协议

原生 `MixPadVoiceAnnouncementsFragment.t7` 建立 `command="tts play"`、
`actionName=name=文字` 的 Action，随后通过 Intent 返回给场景编辑流程。
保留这些值的 SceneBind 经 `core.c.D → util.d3.f → util.d3.g` 序列化后，
使用 `cmd=194` 保存 `sceneBindList[].order` 和 `sceneBindList[].actionName`。

这是场景绑定保存请求，没有确认即时执行单个文本 Action 的接口。
普通 `cmd=15` 控制方法不序列化该文字字段，不能直接搬用上述内容。
Activity 返回值到 SceneBind 的所有中间 UI 分支尚未完整追踪。
其嵌套 `action` 通道也已追踪：物模型执行方法可构造 `{name,arg?,value?}`，
另有传入 JSONObject 的通用通道；尚未建立与 `tts play` 文字的实际调用关联。
通道可携带 JSON 本身不足以确定 TTS 格式。Evidence：
`ev_079631bebc678dffba16370650280e04cd6fefb5e58c257bd4a56798df2d3ca6`、
`ev_7a9330db6db76bbad9e21b2956ae8c65a8d94d7e7ecb1a5844e2a369b31a5781`。

已保存场景执行由 `core.c.R0` 构造 `cmd=197`，携带 `sceneNo`、`userName`、
`triggerType`，本地数据库有有效 `sceneId` 时一并发送。该请求按编号触发既有场景，
没有携带可替换的播报文字。REA Evidence：
`ev_eaee555f08a7da85e6f7dc302a6a47ef7c5433a73dff2e2e0f1d6a391dc12cfb`。
`smartscene.manager.j.x` 的手动执行分支传入 `triggerType=0`，Evidence：
`ev_474774c4d094408dd2684f22c805cb33c005ea828f870c8d46c723efcb26fe3f`。

修改既有场景动作由 `core.c.l2` 使用 `cmd=195`、`sceneNo` 与
`sceneBindList`，动作序列化包括 `sceneBindId`。它会更新已存场景内容，不是临时播放。
Evidence：`ev_a7d61d1efcd9a606b550f216f2bebca8caa14260091cc3fecb3229c65aada0f8`。
用户明确选择此专用场景方案后，b10 实现 `cmd=195 → cmd=197`，仅更新指定场景
的唯一普通 `tts play` 绑定，使用匹配的 `serial/uniSerial` 确认每一步。
不接受混有其他活动动作的场景；缺失、重复或不完整的场景数据会拒绝执行。
`SceneBind` 通过 Gson 读取；普通绑定的整数标记及 Action 整数字段缺省为零，
`Action` 无参构造器将 `themeId` 初始化为空字符串。写入使用与官方 `d3.g` 相同的
字段和缺省值，只改变播报文字。构造器与字段类型 Evidence：
`ev_327cae4c592f14889c4e4d81174baa4ff4605487146c8e539b78768f181e65ad`、
`ev_246a86795c875836b7b816629c9badcf142ab4502980796831a797960e425d48`、
`ev_bd74b757c9595c9831491ea083f2e2804de6f8ddff3ec354885682eab95dd1a4`、
`ev_ba00ae86158ea61a6475617e90331c1f0e7cba7f4c22be202957a8ff131e4ff9`。

普通编辑器 `k7` 设置 `InputFilter.LengthFilter(300)`，`u7` 的计数为 `/300`
（个性化语句类型 3 为 `/20`，不用于本功能），
`Z6` 使用 Java `String.length()`；`f7` 使用 `util.m2.g` 的字符正则验证。
REA Evidence：`ev_c6beb5b571ddd292ebd271c4174a2cd623ea5a6b0e776723e1f84460f76c82fd`、
`ev_22a6374537d56a4c7a4e0d58f5274438cb96b114affd323447d710335e7588bf`、
`ev_c5118a2ce625d5aab10d2b65eac85916e2aacc24aa36d6ebe3940088245fe2fc`、
`ev_9b11501eb960f4caf87b19a7d2924a4338d3cb99109967b2066d6dcf07cdb738`。
字符正则另由 APK DEX 静态常量交叉核对为
`[0-9a-zA-Z\u4e00-\u9fa5\s，。！？,.!?]+`；这是独立只读解析，不另造 REA Evidence。

REA Evidence：
`ev_0f77acc816696864939f878534ee81a3157e8167e3a772f3bfd779098aac84f5`、
`ev_db3e111e534a83a56889baec893498c0f026b0ac0ba3304bb709b54707a49c4d`、
`ev_eab364e15176794fb2d239872e64f367fe8b63a5da6ca442c951decb7b871cda`、
`ev_2c16a913b121417457d5b81e0d0692f54401c8e0005b329c4835d2b3362c3887`。

## 其他入口与剩余问题

APK 的语音网页入口指向 `/commercial/voice`、`/commercial/intercom` 和
`/commercial/broadcast`。从公开域名发现取得当前中国区主机后，已静态读取这三页的
对应脚本；页面组件展示对讲与广播说明、插图和提示，`methods` 为空，未建立自定义
音频或文字发送格式。REA 已解析这三个脚本，但未恢复其 Webpack 模块，其他脚本未纳入
该次分析；不能将此当作整个网站不存在接口的证明。

针对面板的有限端口与 SSDP/mDNS 发现也未建立 DLNA、AirPlay 或 Google Cast 接收路径。
这些结果受已选端口、发现超时及网络过滤影响，不能扩大成设备完全不支持的结论。

剩余问题是自定义音频 URL/文件的接收格式，以及本版 HA 场景更新和执行的实机结果。
目前的文字服务不能替代音频文件播放，不新增对应媒体能力标志。
