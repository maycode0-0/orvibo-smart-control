# 设备加载与状态合并的逆向依据

最初的库存加载改动及 `2.0` 协议重构均依据欧瑞博智家 APK `5.2.6.307` 的静态代码。
本文分别保留原始回归记录与当前实现边界。APK SHA-256：
`6ea3f3eff41ad4657ca437d10c79022949a1f080af857a8800dc6b6819abac8c`。
没有运行 APK、调用欧瑞博服务器或连接真实设备；测试使用合成数据和传输 mock。

## APK 中确认的契约

- `model/SyncTableData.java:30,131,162`：设备加载实际使用 HTTPS
  `v2/cmd/app/readtable`；`data.tableNameList` 给出表名，记录在 `data[表名]`。
- `bo/device/DevicePropertyStatus.java:16`、`core/f.java:1786`：属性状态可独立放在
  `devicePropertyStatus` 表，字段为 `deviceId/property/value`。
- `data/e0.java:675`、`dao/p0.java:58`：属性关联键为 `(deviceId, property)`，
  `devicePropertyStatusId` 不能替代业务设备 ID。
- `core/g.java:1971,2034`：`deviceStatus.properties` 是另一种属性来源。
- `bo/BaseBo.java:84`：`updateTimeSec` 转毫秒后参与比较；
  `dao/p0.java:64` 的批量写入接受未提供时间或不早于旧记录的值。
- `core/c.java:3646` 构造普通控制 `cmd=15`；`core/g.java:2185,3434` 分派 `cmd=42`
  状态报告。控制 ACK 本身不能证明设备已到达目标状态。

这些路径及行号对应本次反编译导出，不是厂商公开接口或原始源代码行号。

## 集成实现

`protocol.parse_readtable_devices` 统一处理数组表和按 ID 索引的字典表，保留
房间、网关、状态别名、子类型、界面模型及设备自带的旧式状态值。HTTPS 包装器和
主页后备解析复用此路径；未知类型保留为库存记录，控制能力仍由现有能力表决定。

`2.0` 额外保留 `isRFCurtain` → `is_rf_curtain`、`combinedCode` / `combineCode` →
`combined_code`，沿用 `appDeviceId`、`deviceFlag`、型号与类型字段。它们是协议选择的
身份依据，不按名称推断。类型 116 进入 light 映射；RF 34/35 只启用开、关、停。

属性先合并设备和设备状态中的 `properties`，再读取独立属性记录。复合 JSON
字符串转换为对象/数组，普通字符串、数字、布尔和列表保持类型。递归合并保留
同一属性内未更新的成员；输入对象不被修改。

独立记录的数字时间统一为毫秒；较旧记录不覆盖较新属性。没有时间或时间相等时，
本实现采用到达顺序，独立记录排在内嵌属性之后。删除记录会移除本次快照中的对应
属性，并阻止随后较旧的记录把它重新引入。这是集成的明确快照策略；APK 的批量
SQL 实际写死 `delFlag=0`，不能把该行为直接用于 HA 删除处理。

LAN/SSL 分派保留解析器所需的当前状态视图，只将本次赋值的字段交给 `StateStore`。
明确上报的同值仍更新保护时间；未上报的亮度、色温等不会随开关推送进入保护期。
普通、开关及晾衣机的部分更新保留缺省字段。

## 最初加载改动的验证记录

新增 `tests/test_readtable_loading.py` 的 16 项离线回归，覆盖两种库存形式、
房间/状态联表、独立属性、JSON 值、数字时间、删除标记、网关容错、初始灯状态及
LAN/SSL 字段保护。Python 3.11 完整测试 477 项通过。

上述 477 项是当时 Python 3.11 的结果，不是当前分支的全量测试数量。

## 2.0 状态约定与剩余边界

原生类型 0/116 及其中虚拟 MixPad 调光器的初始化、周期云刷新和 LAN/SSL 推送
共享 `parse_native_dimmer`；内部亮度始终为协议刻度。属性百分比输入先编码，HA
实体输出再解码，避免快照与推送混用单位。局部缺失或 `-1` 不抹去有效模式与亮度。

新增协议的公式、控制 payload 及静态证据见 [2.0 协议重构](protocol-profiles-2.0.md)。
当前 Python 3.12 离线验证运行 812 项，810 项通过、2 项因缺少 FFmpeg 跳过，
不代表新增分支已通过真机验证。其余型号亮度、窗帘方向/停止和空调位域仍需按型号确认。

独立属性删除目前作用于解析出的快照；跨快照缓存失效、
属性叶子级来源保护和非灯具的周期云状态解析还需进一步完善。全新安装/重载的库存
加载仍依赖云端，不能据此宣称任意网关可以完全离线部署。
