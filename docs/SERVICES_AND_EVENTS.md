# 服务与事件参考

所有服务均位于 `orvibo_smart_control` 域。存在多个配置项时应显式传入 `entry_id`，避免
把请求发送给错误的家庭。可以在“设置 -> 设备与服务 -> ORVIBO Smart Control”中找到
配置项 ID。

本文覆盖 `2.0.0b1`。新增的原生调光和 RF 窗帘使用 HA
标准实体动作，没有新增集成专用服务；完整支持条件见 [设备支持矩阵](DEVICE_SUPPORT.md)。

## 灯光与窗帘标准动作

原生调光灯使用 `light.turn_on` 的 `brightness`（HA 0–255），由路由转换为设备刻度；
亮度为 0 执行关闭，调用者不应自行填入原生 `value2`。

明确为 RF 的 34/35 窗帘只支持 `cover.open_cover`、`cover.close_cover`、
`cover.stop_cover`。它们没有位置滑块，`cover.set_cover_position` 不受支持；
位置与关闭状态保持未知。非 RF 窗帘继续按既有能力提供位置操作。
静态协议和数值约定见 [2.0 协议重构](protocol-profiles-2.0.md)。

## 服务总览

| 服务                   | 用途                           | 响应                                  |
| ---------------------- | ------------------------------ | ------------------------------------- |
| `refresh_devices`      | 立即刷新指定配置项的设备快照   | 无                                    |
| `set_lock_user_name`   | 设置或清除门锁用户编号的显示名 | 无                                    |
| `fetch_video`          | 拉取并归档一个门锁事件录像     | `ok`、`media_id` 或 `error`           |
| `list_events`          | 查询已归档的门锁图片/录像      | `events`                              |
| `cleanup_history`      | 按时间或条数清理门锁媒体       | `removed`                             |
| `grant_temp_password`  | 创建门锁临时授权               | 新授权元数据；仅本次响应含 `password` |
| `revoke_temp_password` | 删除指定临时授权               | `ok`、`authorized_id` 或 `error`      |
| `list_temp_passwords`  | 读取服务器端授权元数据         | 按设备 ID 分组；不含密码              |
| `inspect_video_session` | 检查 Meari 外部授权响应      | 字段存在状态和固定阶段                |
| `inspect_video_devices` | 查询厂商设备列表            | 数量、分类和有限能力码                |
| `inspect_video_channels` | 查询 NVR 通道云端快照      | 容量、实际通道和有限状态码            |
| `probe_video_stream`  | 限时连接指定通道并检查编码首帧 | 阶段、帧/字节计数；不执行解码         |
| `search_hdd_recordings` | 查询硬盘/存储卡日期或片段   | `stage`、`recording_days` 或 `fragments` |
| `start_hdd_playback` | 选择回放起始时间               | `playback_selected`，不表示已收到录像 |
| `stop_hdd_playback` | 关闭对应回放连接                | `playback_stopped` 或失败阶段         |

带响应的服务使用 Home Assistant 的可选响应机制。自动化需要读取结果时，设置
`response_variable`。

## 视频诊断与录像回放

`inspect_video_session`、`inspect_video_devices`、`inspect_video_channels` 接受可选
`entry_id`、`country_code`（默认 `CN`）、`phone_code`（默认 `"86"`）及 `language`
（默认 `zh`）。多配置项时需选择一个有效配置项，且必须有云连接。
`probe_video_stream` 还要求 `device_index` 与 `channel_id`，可设置
`timeout_seconds`（10–60，默认 35）。索引取自最新库存，不作为持久设备身份。
完整阶段与限制见 [录像机诊断指南](nvr-video-diagnostics.md)。

回放动作通过 `entity_id` 选择本集成的硬盘或存储卡回放 camera，无需另填 `entry_id`：

| 动作 | 参数 |
| --- | --- |
| `search_hdd_recordings` | `entity_id`、`date`；`operation=month/day`（默认 day），日期分别为 `YYYYMM` / `YYYYMMDD` |
| `start_hdd_playback` | `entity_id`、`date=YYYYMMDDHHmmss`；选择后打开对应 camera 才开始建连 |
| `stop_hdd_playback` | `entity_id`；停止回放与转码，实时实体独立 |

日期按设备日历填写，不转换 HA 时区。硬盘与 SD/TF 查询/回放已实现，尚待实机验证；
诊断成功、ACK 或 `playback_selected` 均不证明实际播放。卡片与操作示例见
[录像回放](meari-hdd-playback.md)，实时画面见 [Meari 实时摄像头](meari-live-video.md)。

## 刷新与用户映射

### `refresh_devices`

`entry_id` 是必填字段；该服务只刷新一个配置项。

```yaml
action: orvibo_smart_control.refresh_devices
data:
  entry_id: 0123456789abcdef
```

### `set_lock_user_name`

| 字段        | 必填 | 说明                           |
| ----------- | ---- | ------------------------------ |
| `entry_id`  | 否   | 多配置项环境建议填写           |
| `device_id` | 是   | 门锁设备 ID                    |
| `user_id`   | 是   | 推送中的门锁用户编号           |
| `name`      | 是   | 显示名称；空字符串用于清除映射 |

```yaml
action: orvibo_smart_control.set_lock_user_name
data:
  device_id: w-example-door-lock-id
  user_id: "2"
  name: 张三
```

映射只改变 Home Assistant 中的显示和事件字段，不会修改门锁内部用户。

长期保存姓名请在集成选项“门锁用户映射”填写，每行一项：`用户ID=姓名`。
多把锁可以使用 `设备ID/用户ID=姓名`；不同开锁方式可能复用编号，可使用
`设备ID/fingerprint/用户ID=姓名`、`设备ID/password/用户ID=姓名` 等更精确的映射。
匹配顺序为设备及方式、方式、设备、通用用户编号；编号 `0` 有效。服务修改仅对当前运行期
生效且按设备隔离；重载后以集成选项为准。名字缺失时显示用户编号，内开未上报身份时显示人员未知。

## 媒体归档

### `fetch_video`

`device_id` 和 `object_key` 必填。`object_key` 必须是事件提供的相对对象键；完整 URL、
带查询串的值、路径穿越和反斜杠会被拒绝。

```yaml
action: orvibo_smart_control.fetch_video
data:
  device_id: w-example-door-lock-id
  object_key: /REDACTED/videoPicklockEvent/picklockEvent_1700000000.h264
response_variable: video_result
```

成功响应只公开 Home Assistant 媒体引用：

```yaml
ok: true
media_id: media-source://orvibo_smart_control/archive/...
```

服务不会返回 Home Assistant 主机上的绝对路径。录像先保存为 H.264；环境中存在 ffmpeg
时会以流复制方式转封装为 MP4。

### `list_events`

| 字段        | 默认           | 限制                       |
| ----------- | -------------- | -------------------------- |
| `entry_id`  | 全部匹配配置项 | 可选                       |
| `device_id` | 全部门锁       | 必须属于目标配置项且为门锁 |
| `limit`     | `100`          | `1..500`                   |
| `media_type` | 全部          | 可选 `image` / `video`，先筛选再限制条数 |

响应中的每条记录只可能包含 `device_id`、`kind`、`time`、`type` 和 `media_id`。

```yaml
action: orvibo_smart_control.list_events
data:
  device_id: w-example-door-lock-id
  limit: 50
response_variable: lock_history
```

### `cleanup_history`

| 字段          | 默认           | 限制                            |
| ------------- | -------------- | ------------------------------- |
| `keep_days`   | `7`            | `0..3650`；`0` 表示不保留旧记录 |
| `device_id`   | 全部门锁       | 可选                            |
| `max_entries` | 不按条数裁剪   | `1..10000`，按设备保留最新记录  |
| `entry_id`    | 全部匹配配置项 | 可选                            |

```yaml
action: orvibo_smart_control.cleanup_history
data:
  keep_days: 14
  max_entries: 500
response_variable: cleanup_result
```

集成启动时会执行一次默认清理，之后每 7 天运行一次。录像默认保留 7 天，抓拍按每把锁的
数量保留，默认最近 30 次；集成选项“门锁抓拍保留数量”支持 `1..500`。自动按天清理
不会删除保留范围内的抓拍。手动调用 `cleanup_history` 仍可显式删除抓拍。

## 临时密码

### `grant_temp_password`

| 字段         | 默认       | 约束                               |
| ------------ | ---------- | ---------------------------------- |
| `entry_id`   | 自动选择   | 多配置项时建议填写                 |
| `device_id`  | 第一把门锁 | 必须属于目标配置项                 |
| `type`       | `2`        | `1` 限时，`2` 临时                 |
| `minutes`    | `1440`     | `1..525600`                        |
| `number`     | `1`        | `0..100`，`0` 表示不限次数         |
| `name`       | 自动生成   | 最多 64 个字符                     |
| `phone`      | 空         | 可选，6 到 20 位数字，可带前导 `+` |
| `start_time` | 当前时间   | 可选 Unix 秒时间戳                 |
| `end_time`   | 由时长计算 | 可选，必须晚于 `start_time`        |

服务器最多允许 4 个活动授权。服务响应会返回 `authorized_id`、有效期、次数等元数据，
并且只在创建成功的本次响应中返回 6 位 `password`：

```yaml
action: orvibo_smart_control.grant_temp_password
data:
  device_id: w-example-door-lock-id
  type: 2
  minutes: 30
  number: 1
  name: 访客
response_variable: temp_password
```

不要把 `temp_password.password` 写入日志、Recorder 可见实体、蓝图跟踪或公开通知。集成
每 6 小时检查一次本地已知授权，并尝试撤销已过期或次数用尽的记录。

### `revoke_temp_password`

`device_id` 和正整数 `authorized_id` 必填。

```yaml
action: orvibo_smart_control.revoke_temp_password
data:
  device_id: w-example-door-lock-id
  authorized_id: 101
response_variable: revoke_result
```

### `list_temp_passwords`

`device_id` 可选。结果按设备 ID 分组，包含授权 ID、有效期、次数、名称、电话和过期状态
等服务器元数据，但永远不返回密码明文。

```yaml
action: orvibo_smart_control.list_temp_passwords
data:
  device_id: w-example-door-lock-id
response_variable: authorizations
```

## 门锁事件

### `orvibo_smart_control_lock_event`

门锁状态变化、开锁、告警、门铃和文本消息会发布到此事件。基础字段如下；没有值的可选
字段可能省略或为 `null`。

| 字段                            | 说明                                                        |
| ------------------------------- | ----------------------------------------------------------- |
| `device_id`、`uid`              | 事件所属门锁标识                                            |
| `source`                        | 原始实时通道，通常为 `ssl`；保留 `lan` 枚举用于统一事件契约 |
| `locked`、`door_open`           | 锁舌和门磁状态                                              |
| `inside_locked`、`child_locked` | 室内反锁和童锁状态                                          |
| `leave_home_armed`              | 离家防护状态                                                |
| `kind`                          | 归一化事件类别                                              |
| `time`                          | 设备事件时间戳                                              |

常见 `kind` 包括 `unlock`、`error_unlock`、`picklock`、`door_unclose`、
`leave_home`、`ring` 和 `message`。开锁事件还可能包含：

| 字段                                                    | 说明                                        |
| ------------------------------------------------------- | ------------------------------------------- |
| `unlock_type`                                           | `fingerprint`、`password`、`face` 或 `card` |
| `unlock_method`                                         | 规范化方式：`fingerprint/password/card/key/face/temporary_password`，未知为 null |
| `open_direction`                                        | `inside`（内开）/ `outside`（外开） |
| `unlock_user_id`                                        | 门锁用户编号                                |
| `unlock_user_name`                                      | 配置映射后的名称                            |
| `opened_by_user_id`、`opened_by_type`、`opened_by_name` | 最近一次开锁与随后开门的关联结果            |

新版 `doorLock/unlockEvent` 的 `userId` 与旧版 `unlock` 的 `userID` 均可读取，表示锁侧
授权编号。按四类凭据规则，密码、指纹、钥匙、卡片（含临时密码、胁迫凭据）判外开；
其他开锁方式（包括人脸、未识别或未上报方式）统一判内开。只有门磁打开、没有匹配到
四类外开凭据时也判内开，且不沿用上次外开人员。这是集成的方向推断规则；
内开不会据外开用户映射猜测人员。反锁状态不用于判断方向。开锁与开门的关联
窗口为 30 秒，一次开锁仅归属一次开门，关闭后再次打开不会复用上一个人。
较旧的开锁事件迟到时带 `historical: true`，其抓拍仍归档，但不更新最近开门状态和人员。

带媒体的事件可能附带短期签名 URL：`media_url`、`pic_media_url`、
`doorbell_media_url`。URL 中包含临时访问凭据，不应持久化或提交到 Issue。媒体在后台归档
完成后可通过服务或媒体浏览器访问。

自动化示例：

```yaml
triggers:
  - trigger: event
    event_type: orvibo_smart_control_lock_event
    event_data:
      kind: unlock
conditions:
  - condition: template
    value_template: "{{ trigger.event.data.unlock_user_id == 2 }}"
actions:
  - action: notify.mobile_app_phone
    data:
      title: 开门通知
      message: >-
        {{ trigger.event.data.unlock_user_name
           | default('用户' ~ trigger.event.data.unlock_user_id) }} 已开门
```

### `orvibo_smart_control_temp_password_event`

创建临时授权成功后发布。事件包含 `device_id` 和不敏感的授权元数据，明确不包含
`password`。需要新密码时只能读取 `grant_temp_password` 的当前服务响应。

## 内置门锁卡片

集成在前端注册两个卡片：

- `custom:orvibo-smart-control-door-lock-card`：门锁状态、自动保存的历史截图和临时密码管理；
- `custom:orvibo-smart-control-temp-password-card`：只显示临时密码的创建、授权列表和撤销操作。

专用临时密码卡片配置：

```yaml
type: custom:orvibo-smart-control-temp-password-card
device_id: w-example-door-lock-id
```

总览卡片配置：

```yaml
type: custom:orvibo-smart-control-door-lock-card
device_id: w-example-door-lock-id
history_limit: 24
```

`history_limit` 可选，用于设置卡片展示的历史截图数量（`1..500`，默认 `30`）。

门锁事件截图会自动保存到 Home Assistant 的 `config/media/orvibo_smart_control/<设备>/` 目录，
默认保留最近 30 次，可在集成选项调整。卡片中的“历史截图（自动保存）”区域可刷新列表
并点击缩略图查看大图；只查询抓拍，录像不会占用图片名额。抓拍保存成功后才淘汰旧图，
同秒不同图片分别保存，重载后从本地恢复最近画面。也可以直接调用 `list_events`
并传 `media_type: image` 获取每张截图的 `media_id`。

不填写 `device_id` 时卡片会尝试选择实体最完整的一把门锁；多配置项或多门锁环境应明确
配置。卡片调用的仍是上文服务，权限取决于当前 Home Assistant 用户。授权列表不会返回
或重复显示密码明文；新密码只在创建成功后显示一次。不要把门锁仪表盘匿名暴露到公网。
