# 摄像机存储卡与录像机硬盘回放

`0.2.6b5` 新增硬盘录像日期/片段查询和按时间播放，`0.2.6b8` 扩展到巨目 2K 智能
摄像机 S1 的 SD/TF 存储卡录像。实时 NVR 通道 1、3 已获用户确认可看。
**硬盘和存储卡查询与回放尚待真实设备验证**。

## 打开回放

1. 在 HACS 更新到 `v2.0.0b1` 或后续适用版本，重启 Home Assistant，强制刷新浏览器。
2. 编辑仪表盘 → 添加卡片 → 搜索“欧瑞博录像回放”。也可添加手动卡片：

   ```yaml
   type: custom:orvibo-smart-control-nvr-card
   title: 欧瑞博录像回放
   ```

3. 选择设备 / 通道，如“S1 存储卡回放”或“录像机 通道 1 硬盘回放”，选择日期，点击“查询录像”。
   当月查询位于“更多选项 → 查询当月日期”，点击返回的日期可查询片段。
4. 点击片段播放。也可在“更多选项”中填写“指定回放时间”后点击“播放”。首次建立 P2P
   连接可能需要约一分钟。点击“停止回放”释放连接。

卡片默认使用紧凑布局和自动高度。片段列表最多占约三行，多余片段在列表内滚动。
播放后自动收起片段和更多选项；需要换片段时点击“录像片段”展开。播放器显示在
通道和日期下方，不会被长列表推到页面底部。

如果原仪表盘曾手动设定固定行数，更新后仍有大块空白，可在卡片“布局”中改为
自动高度，或在卡片 YAML 中加入：

```yaml
grid_options:
  columns: 12
  rows: auto
```

卡片可通过 `entity: camera.your_nvr_channel_1_hdd` 固定为一个回放实体；S1 可选择其“存储卡回放”实体。
`0.2.6b6` 会自动在存储模式的仪表盘资源列表中注册卡片，更新已有旧地址，并使用脚本
内容校验值刷新缓存。YAML 模式不修改用户配置，继续通过全局前端模块加载。

若出现 `Custom element doesn't exist: orvibo-smart-control-nvr-card`，这是卡片脚本
尚未注册。可在“设置 → 仪表盘 → 右上角菜单 → 资源”（需开启高级模式）添加：

```text
/orvibo_smart_control/www/orvibo-smart-control-nvr-card.js?v=2.0.0b1
```

类型选“JavaScript 模块”，保存后强制刷新。YAML 资源模式则在 `lovelace.resources`
配置中使用同一 URL 和 `type: module`。若仍无法加载，在同一个 HA 地址下直接打开
上述路径：应返回 JavaScript 源码；若为404，请检查实际安装版本及 HA 日志中
“欧瑞博卡片静态资源注册失败”的记录。
若没有回放实体，请检查集成设备选择中已勾选 S1 或录像机。S1 的通道 0 是内部协议编号，
卡片显示设备名称；NVR 实体随在线通道创建。从 b8 之前升级时，重启 HA 后才会增加 S1
回放实体。手动注册过旧版资源时，更新原条目，避免重复；正常自动注册使用脚本内容
哈希刷新缓存，上面的版本参数仅用于手动排障。

日期和时间采用**摄像机或录像机本身的日历时间**，不自动按 HA 时区换算。日列表最多展示
256 个有效片段，超过时会提示截断；仍可指定时间播放。

## 开发者工具中的操作

`orvibo_smart_control.search_hdd_recordings`：

```yaml
entity_id: camera.your_nvr_channel_1_hdd
operation: day
date: "20261015"
```

月查询使用 `operation: month` 和六位日期，例如 `"202610"`。成功响应
`stage: hdd_search_received`，带 `recording_days` 或 `fragments`。
连接失败的响应只含固定阶段；可将响应贴回用于诊断。

`orvibo_smart_control.start_hdd_playback`：

```yaml
entity_id: camera.your_nvr_channel_1_hdd
date: "20261015090000"
```

该操作选择回放位置；打开对应 camera 后才开始建连。`playback_selected` 只表示
已选时间，不能证明收到录像。停止使用 `orvibo_smart_control.stop_hdd_playback`，
只需相同的 `entity_id`。

## 当前范围

- 支持 Meari XTS/p2p=2、默认 hostKey 鉴权的 IPC（含 S1）普通存储卡录像和 NVR 普通硬盘录像；不含云存储或
  需要 record_id 的另一种录像模式。
- 每台 IPC 或 NVR 通道有独立实时/回放实体；查询也建立独立短连接。设备能否允许多路同时
  连接仍取决于设备。当前没有音频、暂停和倍速；重新选择时间会重新建连定位。
- 回放结束后不会自动从最初时间重播。请重新点击片段或选择新时间。
- 缩略图不启动录像播放；播放过程不自动保存视频文件。卸载或 HA 关闭会取消
  查询并清理协议、FFmpeg 和 HA Stream。
- 实时实体中的 `video_diagnostics` 提供固定阶段、连接次数、失败/超时计数和
  帧/字节统计；`streaming` 只说明已输出转码视频，不代替肉眼画面验证。

离线验证涵盖原生命令字节、隧道请求/回复序号、日历/片段解析、回放媒体类型及
结束标记、取消清理、实体切换和卡片交互。真实硬盘和存储卡录像的查询结果、播放流畅度与
定位精度需要用户测试。

相关资料：[实时视频](meari-live-video.md)、[诊断指南](nvr-video-diagnostics.md)、
[原生录像协议](meari-hdd-protocol.md)。`2.0` 的调光/RF 重构不改变这里的真机验证边界。
