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

本次本地验证：共运行 812 项测试，810 项通过，结果 `OK (skipped=2)`；
2 项视频测试因缺少 FFmpeg 跳过。184 个 Python 文件通过 AST 语法检查，
`git diff --check` 通过；新增协议模块和新增测试通过 Ruff 检查。
全库 Ruff 仍有既有问题，本次没有进行无关的全库格式化。

本次以 `2.0` 为基础，发布版本为 `2.0.0b0`，上一版本基线为 `0.2.6b17`。
[发布说明](releases/2.0.0b0.md) 与 [CHANGELOG](../CHANGELOG.md) 记录本次变化，
[迁移指南](MIGRATION.md) 说明升级与回退，[文档索引](README.md) 汇总其他协议资料。
