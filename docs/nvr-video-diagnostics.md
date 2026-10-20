# 录像机视频接入诊断

当前匹配的 Meari XTS/p2p=2 摄像机与 NVR 已提供实时和录像回放实体；其他 SDK 仍只有
诊断。NVR 1 号实时画面已有用户确认，3 号等待后可看；硬盘与 SD/TF 查询和回放尚待
实机验证。观看步骤见 [实时视频](meari-live-video.md) 与 [录像回放](meari-hdd-playback.md)。

以下按引入版本整理诊断动作，安装时使用 `v2.0.0b1` 或后续适用版本并重启 HA。
`2.0.0b1` 沿用这些动作，不新增视频验证结论。`inspect_video_session` 只检查
授权响应结构，`probe_video_stream` 只执行限时编码首帧探测，二者都不是播放成功的证明。

它复用本集成已连接的欧瑞博云端会话，按 HomeMate APK 的 `cmd=148` 请求
Meari 外部授权。结果仅包含字段是否存在和阶段，不返回用户信息、token、hostKey、
厂商原始 JSON 或可复用的授权 URL；授权数据不写入配置或诊断文件。
该动作不建立 Meari MQTT 会话，也不连接摄像机、不修改设备、不查询或删除硬盘录像。
欧瑞博厂商授权请求本身是否影响手机会话尚未有真实账号验证。

## 运行

在已加载该诊断构建的 HA 中，打开“开发者工具 → 动作”，执行：

```yaml
action: orvibo_smart_control.inspect_video_session
data: {}
```

只有一个欧瑞博配置项时不需要配置项 ID；有多个时提供 `entry_id`。
中国账号默认 `country_code: CN`、`phone_code: "86"`、`language: zh`。
其他地区可指定国家代码和电话区号。需要现有云连接；纯本地模式不会自动切换到云端。

- `external_session_received`：取得了 API 地址、用户标识和用户 token。仅证明会话响应
  满足结构检查，不能证明 token 被厂商接受、实时画面或硬盘回放可用。
- `external_session_incomplete`：响应缺少必需字段。
- `session_rejected`：欧瑞博拒绝请求；`orvibo_status` 是返回的数值状态。
- `cloud_connection_required`：没有已连接的欧瑞博云会话。
- `no_response`：请求超时或当前会话不可用。
- `select_one_entry`：需指定一个有效配置项。

可以将动作返回的脱敏 JSON 用于后续排查，无需提供账号密码、设备 IP 或原始授权响应。

## 厂商设备列表诊断（0.2.6b1）

会话动作返回 `stage: external_session_received` 后，可执行：

```yaml
action: orvibo_smart_control.inspect_video_devices
data: {}
```

此动作重新获取一次外部会话，使用内存中的 token 为设备列表请求签名，向欧瑞博
响应下发的 HTTPS API 发送一次只读查询。它不连接 MQTT 或摄像机，不修改设备或硬盘。
地区和 `entry_id` 参数与会话动作相同。token、设备标识、设备名称、密钥和原始响应
不会出现在动作返回值中，也不会写入集成配置或日志。

返回保留 `stage` 表示欧瑞博会话阶段；`vendor_stage` 表示 Meari 请求阶段：

- `vendor_devices_received`：返回成功码 `1001` 及已识别的设备分类；查看 `device_count`
  和 `category_counts`。空列表也是有效结果，不能推断用户没有设备。
- `vendor_request_rejected`：厂商返回非成功码，`vendor_result_code` 是有限范围的数字码。
- `vendor_http_error`：HTTP 状态不是 200；`vendor_http_status` 是状态码，不跟随重定向。
- `inventory_request_data_invalid`：会话中的 API 地址或签名必需字段不满足请求要求。
- `vendor_request_failed`、`vendor_response_too_large`、`invalid_device_response`、
  `unrecognized_inventory_shape`：网络、响应大小或结构未通过验证，不能宣称授权已被接受。

`devices` 按列表顺序编号，仅包含固定分类、连接字段是否存在及有限的原始能力码。
`inventory_shape` 区分 App 使用的顶层分类结构与 `result` 包装结构；不返回任意服务端键。
输出最多 512 条，超过时 `devices_truncated` 为 true，`device_count` 仍为总行数。

NVR 通道来自 App 后续的设备参数查询，不是简单的设备库存字段。因此设备列表动作返回
`channel_query_implemented: false`；需另用下文 `inspect_video_channels`。
`nct` 等能力码不能直接解释为实际在线通道数，
这一步也不能证明首帧或硬盘回放可用。请提供完整动作返回的脱敏 JSON 继续排查。

## NVR 通道诊断（0.2.6b2）

用户在 `0.2.6b1` 的真实返回中已确认厂商接受授权并返回 1 台 IPC 与 1 台
`nvr-neutral` 录像机，两者 `iotType=3`。SDK 对这类设备使用 XTS 认证，并从序列号
生成连接 UUID，因而 `uuid_present=false` 本身不阻止该路径。此结论没有验证媒体连接。

该动作从 `v0.2.6b2` 起提供，当前版本无需启用预发布；安装并重启后运行：

```yaml
action: orvibo_smart_control.inspect_video_channels
data: {}
```

此动作一次获取会话和设备列表，然后调用厂商 HTTPS 批量接口读取 NVR 的
`213,1015,67` 属性。它只读取通道云端快照，不连接 P2P、启动实时流或查询/删除硬盘片段。
使用原有 `entry_id` 和地区参数。返回保留已有的会话、设备列表诊断字段，并新增：

- `channel_query_implemented`：此动作已包含通道查询代码，不表示视频已实现。
- `channel_stage: nvr_channels_received`：厂商返回成功码及结果对象；逐项查看 `nvrs`，不能由顶层成功推断每台录像机都有通道数据。
- `nvr_count`、`queried_nvr_count`：识别出的录像机数量与实际查询的数量；最多查询 32 台，缺少序列号的设备不发送请求。
- `nvrs[].index`：对应本次 `devices` 的序号，避免输出设备标识。
- `maximum_channel_count`：213 的容量值，与返回的实际通道条目数量分开。
- `channel_count`、`channels[].channel_id`、可选 `state/type/capability_codes`：1015 的实际通道条目及有限能力码；未知值省略，最多输出 128 条通道。
- 每台录像机的 `channel_stage` 可为 `channel_state_received`、`channel_state_missing`、`invalid_channel_state` 或 `nvr_result_missing`，用于区分空列表、缺失和无效数据。
- `channel_request_rejected`、`channel_http_error`、`channel_request_failed` 等顶层阶段表示请求失败；只返回有限结果码/HTTP 状态，不输出错误原文。

云端快照可能滞后，原始 `state/type` 不直接等同于已验证在线或可播放。
`0.2.6b4` 的 `video_playback_implemented=true` 表示已提供实时播放代码；
`real_device_video_verified=false` 表示本动作没有验证 HA 的真实画面播放。
用户已提供本动作的真实成功结果：录像机容量与返回通道数均为 8；1、3 号 state=1、p2p=2，其余 state=0、p2p=0。
此结果仅证明通道云端查询成功，媒体仍需单独测试。
请求与静态依据见 [NVR 通道接口](meari-nvr-channel-protocol.md)。

## 指定通道媒体测试（0.2.6b3）

该动作从普通 Release `v0.2.6b3` 起提供；先测试最新列表中已发现的录像机 1 号通道：

```yaml
action: orvibo_smart_control.probe_video_stream
data:
  device_index: 2
  channel_id: 1
  timeout_seconds: 35
```

完成后再把 `channel_id` 改为 `3`，分别保留两次完整脱敏返回。
`device_index` 按每次重新取得的厂商库存编号；库存变化时先重新运行设备/通道诊断。
只接受 iotType=3、p2p=2 的 NVR（devTypeID=11）或 IPC（devTypeID=2，channel_id 必须为 0）。
一次只连接一个明确指定的通道，同一配置项已有探测时返回 `probe_in_progress`。

此动作使用新的云会话和库存，在独立进程内实际建立信令与 P2P/TURN 媒体连接，
发送选定通道的实时开始请求。它不获取硬盘目录，不保存画面，不写入用户凭据或原始响应。
媒体预算为 10–60 秒，默认 35 秒；云会话和库存获取另需时间，整个动作另有总时限。
超时或取消后父进程强制结束并回收子进程，避免遗留阻塞网络线程；集成卸载与 HA 关闭也会取消探测。

返回保留会话/库存字段，新增：

| 字段 | 含义 |
| --- | --- |
| `probe_stage` | 最后结果；`probe_stage_history` 依序记录尝试过的连接阶段，阶段出现本身不证明该阶段已成功 |
| `root_discovery` / `signaling_connect` / `signaling_registered` | 发现信令地址、尝试连接、完成注册 |
| `device_status` / `turn_allocate` / `sdp_offer` | 查询设备状态、申请中继、发送媒体协商 |
| `ice_connect` / `kcp_connect` / `receiving_video` | 尝试 ICE、发送 KCP/IVA 握手、发送实时开始请求并等待视频 |
| `video_keyframe_received` | 识别到 H.264/HEVC 参数集和关键 NAL 的基本头部、长度结构；不是完整解码校验 |
| `encoded_video_verified` | 上述编码结构探测成功；与 `decoded`、播放和历史回放分开 |
| `video_frames` / `video_bytes` / `codec` | 识别到的候选视频帧数、编码负载字节数和编解码名称 |
| `received_packets` | 收到的网络包总数，包含协商包，不能直接当视频帧数 |
| `keyframe_received` / `parameter_sets_received` | 编码关键帧与必要参数集是否通过基本结构检查 |
| `no_video_frame` / `device_unavailable` / `probe_timeout` | 开流后无合格首帧、设备状态不允许连接、达到时限；不能自动归因为密码错误 |
| `probe_failed` / `dependency_missing` / `invalid_context` | 连接或协议处理失败、依赖不可用、输入会话/设备参数不满足要求；不回显原始错误 |
| `last_probe_stage` / `worker_stopped` | 最近尝试的阶段、媒体子进程已结束并回收 |
| `probe_device_index` / `probe_channel_id` | 本次明确请求的设备序号与通道号 |

首帧动作仍返回 `decoded=false`、`real_device_video_verified=false`，因为该动作不执行解码。
`0.2.6b4` 起 `video_playback_implemented=true` 表示另外提供了实时摄像头播放出口。
不能从首帧结构探测成功推断硬盘片段可播放。

身份取自外部会话的 partnerId/userID；不套用其他品牌账号。
本轮模拟客户端版本为 `5.2.6.307a13`，发现协议版本沿用固定参考实现的 `15259`，设备兼容性仍待实测。
鉴权只尝试 App 默认 hostKey 模式；若已明确返回独立视频密码状态，则拒绝该模式。
App 也可能把视频密码状态存在手机本地，所以字段缺失不表示已证明没有视频密码。

## 当前证据与验证边界

用户目标是 HAOS 虚拟机/x86 Linux 宿主上的实时画面和录像机硬盘历史。
APK 的 Android ARM 原生视频库不能直接用于此环境。公开 MIT 项目
[Ha-CloudEdge-Meari](https://github.com/ajeanney/Ha-CloudEdge-Meari/tree/5bc9f97766c3d8076da8bb15dcb7b62ca7721127)
提供了可参考的 Python P2P 实时协议；其当前代码没有 NVR 硬盘索引、回放和定位实现。
已收到用户真实 NVR 1 号通道的 HEVC 关键帧及参数集（153,474 字节、144 个网络包）。
这确认了指定通道的编码接收；后续另有用户确认 1 号实时画面可看、3 号等待后可看，
仍未取得硬盘或 SD/TF 回放成功结果。

外部会话、库存及 NVR 通道查询已获得用户环境成功返回。
媒体首帧、解码/播放出口与普通硬盘/存储卡查询和按时间回放均已有代码。
真实录像列表、定位精度与播放流畅度仍需设备验证。
`stream_supported` 按集成已实现的视频路由声明：Meari 摄像机及 NVR 为 true，其他 SDK 为 false。
探测结果不会改变此声明或 `hardware_verified`，也不替代动态播放验证。


## 实时摄像头（0.2.6b4）

更新后进入录像机的设备页面，打开新增的 `camera` 通道实体。已有“云端”“Meari”是
诊断传感器，打开它们不会显示画面。当前会为云端报告 state=1、p2p=2，且通过 XTS
会话校验的通道生成实体；按 App 的 Device.uid 与 CameraInfo.deviceID 精确关联，
不会按名称、设备数量或旧库存序号猜测对应关系。没有实体时检查是否选中了录像机，
运行 `inspect_video_channels`，并查看 HA 中该集成 camera 平台是否正在重试。

`video_stage` 是当前连接阶段（idle/starting/connecting/transcoding/streaming等固定值）。
`streaming` 表示转码器已有输出，不单独证明浏览器成功显示。请反馈能否看到实际画面、
1/3号各自情况，以及关闭后重新打开是否恢复。当前无音频；硬盘与存储卡历史通过独立
回放实体和卡片提供，不在实时实体中切换设备时间轴。

实现与限制见 [实时视频](meari-live-video.md)。
