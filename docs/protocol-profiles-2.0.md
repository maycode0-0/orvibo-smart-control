# 2.0 协议 profile 重构

此分支基于 HomeMate 5.2.6.307 的 Java 反编译和 ARM64 原生伪代码，把已确认的
设备身份、控制编码与状态解码接入现有 HA 集成。逆向证据均为静态分析，尚未用
本分支连接真实设备验证；不能据此声称所有型号或固件兼容。

## 已实现的范围

| 设备条件 | 控制与状态语义 |
| --- | --- |
| 普通 `deviceType=0` 调光灯 | 百分比 p 编码为 `(p*253+50)//100+2`，亮度写 `value2`；回读使用 `(level*100-72)//253` 并限制到 1..100 |
| `deviceType=116` 调光灯 | 亮度编码使用 0..120 刻度；注册 light 实体，支持 SSL 和 LAN 路由 |
| 类型 0/116 中的 `VirtualModelidForMixpadD` | 编码优先使用 0..255；回读按原生函数顺序先检查 116，再检查虚拟型号 |
| 明确标记 `isRFCurtain` 的类型 34/35 窗帘 | 开/关/停发送 `order="rf control"`，`value1=380000/380001/380002`；不提供位置滑块，位置和关闭状态未知 |

库存保留 `isRFCurtain`、`combinedCode`，并沿用既有的 `deviceFlag`、`appDeviceId`、
model 和类型信息。元数据用于选取具体协议，不能仅凭设备名称下发猜测命令。
其他简单灯、窗帘、RGB、空调及 IR 类型的静态候选分支不等于本次新增支持。
本次静态证据新增的 116、虚拟调光和 RF 34/35 分支允许控制，但
`hardware_verified` 保持为 false，不继承同类别其他设备的实机验证结论。

## 状态与传输约定

### MixPad 背景音乐（b8 引入，b9 修正命令号）

类型 `128` 的音乐主机通过云端 SSL `cmd=289` 控制，外层使用主机 `uid`，
`data.type=2`、`order="song control"`。`value1` 分别为暂停 `0`、
播放 `1`（`value2=2`）、音量 `2`（`value2=0..100`）、切换静音 `3`、
上一首 `6`、下一首 `7`、跳转 `11`（`value2` 毫秒）。HA 实体边界转换音量和秒数。

初始查询同样使用 `cmd=289`，`data.type=13`，属性为
`music_current_song,music_play_status,music_play_mode,volume,mute`。
响应 `data.statusList` 按 `deviceId/property/value` 进入同一状态分发器；
`music_play_status=1` 为播放，`0` 为暂停；官方 App 将 `mute=0` 视为静音。
当前歌曲 JSON 的 `position` 与嵌套 `music.duration` 均从毫秒转换为秒。

静态依据为智家 365 `5.2.6.307`，APK SHA-256 为
`6ea3f3eff41ad4657ca437d10c79022949a1f080af857a8800dc6b6819abac8c`。
REA/JADX Evidence：动作类 `ev_71cef0f61345c3c970ff0d48a76be9fc34c178cc92176d873fe50b3edf350db8`，
请求类 `ev_27133f7edbe447ea6e3f2961495fa23089f160586ebcf2cb8e6bee5650cd29ca`，
控制器 `ev_edb0065acbb5f39e91d20dbc8df915a32145406e2ef4013182710a57b3de79b4`。
这些证据不包含运行时观察；`hardware_verified=false`。未发现支持同一命令的 LAN
证据，因此纯 LAN 模式不提供音乐控制；类型 `114/45` 网关不会额外创建媒体实体。

b9 进一步核对 `com.orvibo.homemate.model.device.music.a` 的构造器，确认其
`this.cmd = 289`，Evidence 为
`ev_2832cda086a5312d47280a83c67bfbfaab9deb415dd580615cac07f2c70dbdf3`。
父类 `com.orvibo.homemate.model.s.getCommand(JSONObject, RequestConfig)`
将该 `this.cmd` 传入通用请求封装，Evidence 为
`ev_d9e2c192edb2322f5e3b5ec9021201a09076d5aca62954c442e34579ead03b6d`。
最终 `com.orvibo.homemate.core.c.Z0(...)` 将该命令号写入 JSON 的 `cmd`，
随后序列化并加密封包，Evidence 为
`ev_cf4dfaca03d2809da4d7c90806b78a4e0eed6d1291e035e163828c73dfd42022`。
恢复播放 `a.U(uid)` 的正文为 `type=2/value1=1/value2=2`，无 `delayTime`，
Evidence 为 `ev_fbb271391886c08aa4d3ab806b62227c4c15c158f9fa21390b432a301c398a64`。
此前 b8 将正文套入普通设备 `cmd=15` 是实现错误；b9 同时修正发送、查询和接收分发。
云端非零错误状态不进入音乐状态解析；发送失败通过媒体实体抛出 HA 可见错误。
本轮只有用户失败反馈、静态证据和离线回归，不包含修复后真机成功验证。

### 调光与 RF

- 原生调光灯的状态 `brightness` 始终保存协议刻度。HA 的 0..255 输入在路由中
  转换，实体输出再解码；属性百分比、库存初始化、云刷新、SSL/LAN 推送和超时
  乐观状态采用相同约定。
- HA 亮度 0 按关闭处理；正亮度至少编码为 1%（普通调光原生 level 最低为 5）。
  百分比取整会产生正常的刻度量化，不承诺 HA 每一个数值都能精确往返。
- 保留已知 `value3/value4`，忽略缺失或负哨兵值。虚拟 MixPad 是例外，其控制
  `value4` 按原生转换固定使用 0。局部推送不擦除此前有效的亮度或模式字段。
- 控制路由与报文生成分别负责语义和封包；SSL 与 LAN 复用同一生成器。
  原有非 RF 窗帘命令及卷帘停止参数保留。
- cmd15 请求结果和 cmd42 状态报告继续走现有独立链路。没有状态确认时使用
  已标记来源的乐观状态，不把它当作实机验证结果。

## 可追溯证据

原始报告位于本机分析仓库的
`D:/own/SmartHome365/reverse/homemate/reports/ha-device-control.md`。
以下位置相对该仓库的 `reverse/homemate/`，地址为 Ghidra ARM64 地址
（镜像基址 0x100000）：

| 行为 | 证据位置 |
| --- | --- |
| 亮度编码、保留字段、虚拟型号分支 | `native-analysis/pseudocode-arm64/libpvconvert.so/functions/00138158.c`，`dimmerProperty2value` |
| 亮度回读及分支优先级 | 同目录 `00138438.c`，`getBrightPercentByBright` |
| RF 窗帘开关/停止与类型方向差异 | 同目录 `00135bd4.c`，`curtainProperty2value` |
| 窗帘属性回读 | 同目录 `00135f30.c`，`curtainSetPropertyWithStatus` |
| Java 调光/窗帘控制交叉依据 | `java/sources/com/orvibo/homemate/model/control/a.java` |
| 普通调光反向整数公式 | `java/sources/com/orvibo/homemate/util/b3.java` |

`protocol_profiles.py` 是纯协议转换层，不执行 I/O。`parsers/light.py` 负责原生灯
的状态补丁，库存和实时分发器共享它。新增型号时应补充脱敏库存、cmd15/cmd42
样本以及对应回归用例，尤其不能把类型 3/109/73 的窗帘方向或空调位域分支泛化。

## 验证

`tests/test_native_profile_integration.py` 覆盖实体 → 路由 → SSL/LAN 报文、推送回读、
属性型云刷新、缓存再次开灯、零亮度、模式字段保留和 RF 能力展示。
`test_protocol_profiles.py` 覆盖原生公式与边界，既有 packet、库存、路由和解析测试
继续校验兼容性。测试不连接真实账号、网关或设备。

运行命令：`python -m unittest discover -s tests`（Python 3.12，依赖见 CI workflow）。

`2.0.0b0` 协议重构时的本地验证：共运行 812 项测试，810 项通过，结果 `OK (skipped=2)`；
2 项视频测试因缺少 FFmpeg 跳过。184 个 Python 文件通过 AST 语法检查，
`git diff --check` 通过；新增协议模块和新增测试通过 Ruff 检查。
全库 Ruff 仍有既有问题，本次没有进行无关的全库格式化。

当前以 `2.0` 为基础，发布版本为 `2.0.0b9`，协议重构基线为 `0.2.6b17`。
`b8` 增加 MixPad 音乐实体，`b9` 修正音乐命令号；历史测试数量保留发布时的记录，本版结果见
[发布说明](releases/2.0.0b9.md) 与 [CHANGELOG](../CHANGELOG.md)。
[迁移指南](MIGRATION.md) 说明升级与回退，[文档索引](README.md) 汇总其他协议资料。
