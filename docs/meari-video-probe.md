# XTS 指定通道编码首帧探测

适配依据为用户提供的 HomeMate 5.2.6.307 APK，SHA256
`6ea3f3eff41ad4657ca437d10c79022949a1f080af857a8800dc6b6819abac8c`，
以及固定 MIT 参考 [Ha-CloudEdge-Meari](https://github.com/ajeanney/Ha-CloudEdge-Meari/tree/5bc9f97766c3d8076da8bb15dcb7b62ca7721127)。
vendor 的归属、许可和改动范围见
[ORIGIN.md](../custom_components/orvibo_smart_control/meari_transport/vendor/ORIGIN.md) 与
[vendor LICENSE](../custom_components/orvibo_smart_control/meari_transport/vendor/LICENSE)。

## APK 静态依据

- `SdkUtils.java:487–506,536–590`：iotType=3 使用 XTS/factory10，UUID 从 SN 派生；
  默认 username=admin、password=hostKey、auth=0；NVR devTypeID=11 对应 devicetype=3。
- `MeariIotController.java:177–215`：brand=外部会话 partnerId，clientid=userID，c=countryCode，
  v=宿主 App 版本加 Android 系统版本；不能套用公开项目的品牌账号常量。
- native `libmrplayer.so` SHA256
  `4ef7e16fba2a5d043dfe98b2b61b159778f1eaf695aa1f27d8de8e933fc6d857`：
  ELF `0x1e6b2c` 将连接输入复制到 ctx+0x128，因此 ctx+0x718=auth，
  ctx+0x71c=(protocolv>1)。ELF `0x1dd168` 据此选择完整/前16字节密码与 licenceId。
  可变参数调用的 licenceId 需结合汇编栈参数确认，不能依赖反编译 C 的缺失参数。
- ELF `0x1dd80c`、`0x1de0a8`、`0x1deb14`：实时开始、停止、心跳分别为
  `0x11ff`、`0x12ff`、`0x888e`，param=8、包长60。
  鉴权函数最终将头部偏移4写为 network-order 1；初始化时的0并非最终线上值。
- 实时包偏移0x34为低24位通道的 little-endian uint32，0x38为视频质量，
  0x39为camera-tags，0x3a为reserve。单通道普通预览本轮使用tags=0、quality=0；
  SDP里的channel=0属于传输信令，不取代实际NVR媒体通道。

本版只尝试默认 hostKey 模式，p2p=2 时签名为以下字段用 `|` 连接后取小写 MD5：

```text
admin | hostKey前16字节 | 十进制magic | seq | cmd | 8 | meari.p2p.ppcs | licenceId
```

SN为9字符时 UUID 前补7个0、licenceId前补11个0；其他有效SN的UUID去前4字符，licenceId保留。
有独立视频密码时的 auth=1 路径未开放；不依据密码长度猜测、不循环尝试鉴权组合。

## 生命周期与结果边界

父进程重新获取外部会话和库存，选择一个明确设备和通道，经私有 stdin 将有限参数送入
`python -I -u -B worker.py`。命令行只包含脚本及安装依赖的目录，不包含凭据。
worker 复用信令、TURN、ICE、KCP、VVP 和编码帧 primitives；不导入 HA、完整品牌登录、
MQTT 或参考引擎，不启动保活/唤醒/relay-probe线程，也不创建其他进程。

进程输出固定 JSON 标量，并在 HA 边界再次过滤；日志、stderr和原始协议内容不会作为诊断返回。
超时、取消及异常均 kill/wait，包括阻塞DNS；完整输出、单行、候选数量、网络字节、帧大小和KCP队列有限制。
HTTP清单响应在开始媒体连接前释放。同一配置项只允许一次探测，HA停止或配置项卸载会等待清理。

`encoded_video_verified` 只确认编码帧封装、NAL基本头部/长度、必要参数集与随机访问帧类型；
H.264支持IDR，HEVC支持IRAP16–21。它不是完整RBSP/参数关联校验，更不是解码成功。
首帧动作自身不解码。0.2.6b4 新增独立的连续收流与 HA 播放出口，见 [实时视频](meari-live-video.md)；
硬盘与 SD/TF 日期/片段查询和回放已另行实现，尚待实机验证；见
[录像回放](meari-hdd-playback.md)。这些能力不属于首帧探测动作。
客户端 `5.2.6.307a13` 的Android后缀是模拟选择，root发现协议版本15259来自参考实现，已在用户的 1 号通道收到 HEVC 关键帧；其他设备兼容性未据此确认。

用户真实证据：NVR index=2/channel=1，HEVC，153474字节，144包，参数集与关键帧齐全；
encoded_video_verified=true、decoded=false，探测结束已停止worker。它不等于 HA 连续播放成功。

后续用户已确认 NVR 1 号实时画面可看、3 号等待后可看，记录见实时视频指南。
这一独立反馈不改变首帧动作的 `decoded=false`，也不能证明硬盘回放或其他型号兼容。
