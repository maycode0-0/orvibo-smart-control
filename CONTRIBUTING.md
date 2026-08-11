# 为 ORVIBO Smart Control 贡献

本项目接受设备档案、协议修复、Home Assistant 实体、LAN 稳定性、门锁功能、测试和文档
改进。每个提交应保持范围清晰，并用自动化测试或可复核的真机证据说明行为。

## 先确定变更类型

| 变更                          | 最小证据                                    | 通常修改的位置                                 |
| ----------------------------- | ------------------------------------------- | ---------------------------------------------- |
| 已有类别的新商品型号          | 官方型号字段、初始状态、真机回归            | `device_types.py`、型号目录、设备矩阵、测试    |
| 新设备类别                    | 发现记录、全部状态推送、每项控制请求/响应   | 分类、能力、解析器、控制、平台、测试           |
| LAN 连接或协议问题            | 脱敏帧形态、网关型号、失败阶段              | `lan/`、协议/连接测试                          |
| 云端登录或推送问题            | 云区、错误码、脱敏响应形态                  | `https_client.py`、`ssl_client.py`、关联测试   |
| 门锁事件/媒体/授权            | 事件类别、对象键形态、预期公开字段          | `lock_*`、`temp_*`、服务和安全测试             |
| Home Assistant 平台或配置问题 | 实体平台、配置步骤、状态/服务调用、复现步骤 | 平台文件、coordinator、config flow、翻译、测试 |
| 文档或项目元数据              | 对应代码事实、受影响链接                    | 文档、manifest、HACS/CI                        |

不要把协议重构、全仓格式化和一个新型号混在同一 PR。危险动作设备的控制实现必须比只读
识别更保守；证据不足时可以先合入诊断 fixture 或登记支持，不应启用猜测命令。

## 开发环境

建议使用 Python 3.11。项目的大部分测试通过轻量 stub 加载模块，不需要安装完整
Home Assistant。

使用 `uv` 运行与当前验证一致的隔离环境：

```powershell
uv run --isolated --python 3.11 `
  --with "aiohttp>=3.9,<4" `
  --with "cryptography>=41,<46" `
  --with "voluptuous>=0.13,<1" `
  python -B -m unittest discover -s tests -q
```

也可以在专用虚拟环境安装最小依赖：

```bash
python -m pip install "aiohttp>=3.9,<4" "cryptography>=41,<46" "voluptuous>=0.13,<1"
python -B -m unittest discover -s tests -q
```

从远端默认分支创建主题分支。分支名建议使用 `feat/`、`fix/`、`docs/` 或 `test/` 前缀。
不要提交真实账号数据、审计输出、IDE 缓存或 Home Assistant 配置目录。

## 理解调用链

开始修改前先阅读 [运行时架构](FUSION_DESIGN.md)。核心路径可以压缩为：

```text
REST inventory -> normalize -> classify -> capability -> HA platform
LAN/SSL push -> device match -> parser -> StateStore -> entity update
HA action -> control route -> transport selection -> LAN or SSL -> state reconciliation
cloud lock packet -> event/media/password managers -> HA event/service/media
```

仓库包含 `.codegraph/` 索引时，优先使用 `codegraph explore` 或 `codegraph node` 定位符号和
调用者。结构性改动完成后运行：

```bash
codegraph sync .
```

不要手工编辑 `.codegraph` 的生成内容。

## 代码边界

### 分类与平台注册

设备支持有两个相邻但不同的层次：

1. `device_types.py` 根据原始类型、子类型、classId、状态类型和型号得到
   `DeviceProfile`。
2. `capabilities.py` 根据 profile 和 type 决定 HA platforms、可用控制 channel、
   `status_only` 与 `cloud_only`。

新增设备时还要检查 `const.py`、`protocol.py` 和对应平台的兼容映射。分类正确但平台映射
缺失，会形成“已识别但没有实体”；平台映射过宽，则可能把未知设备错误注册为可控设备。

`hardware_verified` 是类别级证据。给同类别增加型号不会自动证明新硬件已经验证。官方目录
命中也只代表产品名称可识别。

### 状态解析

解析器位于 `parsers/`，应是可独立测试的纯转换函数。实现必须满足：

- 接受缺失字段、字符串数字和未知附加字段；
- 不修改输入对象；
- 只返回当前报文明示的 `StatePatch`，不重置未出现的字段；
- 统一 Home Assistant 侧的布尔语义、范围和单位；
- 对越界或畸形数值选择拒绝或钳制，并用测试固定选择；
- LAN 与 SSL 对同一设备复用同一解析语义。

新增类别后在 `parsers/__init__.py` 注册，并覆盖正常值、边界值、缺失值、错误类型和部分
更新。

### 控制与传输

设备控制语义放在 `control.py` 和 `control_router.py`。`control_executor.py` 负责选择通道、
单次云回退和乐观状态；`lan/control_adapter.py` 只把规范调用翻译成网关 payload。

新增控制时说明：

- 开关是 active-low 还是 active-high；
- 数值范围、单位、枚举和取整规则；
- 请求命令、响应命令以及可靠的 correlation key；
- LAN 与云 payload 是否存在差异；
- 超时后允许的乐观状态；
- 是否可能导致门锁、加热、消毒、电机等危险动作。

控制测试必须断言方法名、位置/关键字参数和精确 payload。只断言“返回 True”不能证明没有
发错设备或反转动作。LAN 失败测试还必须断言云端只调用一次。

### 门锁、媒体和服务

门锁 `107/522` 是云专属只读设备。普通实体控制不能绕过能力表向其发送命令。临时密码、
媒体和历史通过专用 manager 与服务处理器完成。

公共事件或服务响应不得包含：

- 密码和密码摘要；
- COS credential 或完整签名细节之外的内部 token；
- Home Assistant 主机绝对路径；
- 未验证的任意对象键或 URL；
- 属于另一个配置项的设备数据。

修改公开服务时同步更新 `services.yaml`、[服务与事件参考](docs/SERVICES_AND_EVENTS.md)和服务
测试。用户可见字符串同步更新 `strings.json`、英文和简体中文翻译。

## 新设备证据包

### 身份与拓扑

请记录并脱敏以下事实：

- 商品名称、准确型号、固件/硬件版本；
- 中国区或国际区；
- Wi-Fi、Zigbee、蓝牙或其他接入方式；
- 是否依赖网关以及网关商品型号；
- `deviceType`、`subDeviceType`、`classId`、`statusType`、`ui.model`、`model`；
- App 中可见的全部能力。

### 状态样本

至少收集设备初次出现时的 readtable 记录，以及每个物理状态变化对应的实时推送。多字段
设备需要提供单字段变化，证明解析不会清空其他状态。还应记录离线/恢复、最小/最大值和单位。

### 控制样本

每项能力验证正向、反向和边界操作。例如窗帘应包含开、关、停止和中间位置；调光设备应
包含最小、中间、最大值；暖通设备应覆盖所有模式与风速。一次完整证据包含：

```text
HA intent -> selected transport -> request -> acknowledgement -> physical result -> state push
```

只有服务端返回成功而没有实物动作和状态确认，不算真机验证。

## 诊断工具

### 单家庭探针

`tests/orvibo_probe.py` 可以列设备、监听实时包或在限定时间内收集门锁事件：

```text
python tests/orvibo_probe.py <用户名> <密码> list
python tests/orvibo_probe.py <用户名> <密码> listen
python tests/orvibo_probe.py <用户名> <密码> lock <家庭索引> <时长秒>
```

探针的自动掩码不保证覆盖家庭 ID、设备 ID、UID 等所有关联标识。输出 JSONL 只能留在本地，
提交前必须再次人工最小化和脱敏。

### 多家庭覆盖审计

账号拥有大量家庭时可运行：

```powershell
$env:ORVIBO_USERNAME = "账号"
$env:ORVIBO_PASSWORD = "密码"
python tools/bulk_readtable_audit.py --cloud auto --max-families 3
```

先用少量家庭确认云区和输出，再移除 `--max-families`。脚本按家庭写 checkpoint；使用同一
`--output-dir` 重跑即可续传。主要产物：

| 文件                       | 用途                       | 是否提交 |
| -------------------------- | -------------------------- | -------- |
| `report.json`              | 协议特征与支持状态汇总     | 否       |
| `unsupported.csv`          | 登记、映射不一致和解析缺口 | 否       |
| `unsupported-enriched.csv` | 叠加官方产品目录名称       | 否       |
| `state.json`               | 本地续传状态               | 否       |

状态值包括 `supported_verified`、`supported_unverified`、`hidden`、`recognized_only`、
`platform_mismatch`、`registration_only` 和 `parser_gap`。空家庭记录为 `empty`，不算失败。

已有报告可以离线补全官方名称：

```bash
python tools/bulk_readtable_audit.py --enrich-existing --output-dir audit-output
```

这些目录和 `tools/device_catalog.json` 已加入 `.gitignore`。Issue/PR 只能摘取人工审核后的
最小片段，不能上传整份家庭审计。

### 生成公开型号目录

取得新的官方 `device_catalog` 后运行：

```bash
python tools/generate_known_device_catalog.py \
  tools/device_catalog.json \
  custom_components/orvibo_smart_control/known_device_catalog.json
```

生成文件只应包含公开 `model`、产品名称和内部型号。家庭审计不能覆盖官方名称，目录命中也
不能自动设置真机验证标记。

## 测试要求

先运行最接近改动的测试，再跑全量。常见映射如下：

| 改动                | 定向测试                                                                                 |
| ------------------- | ---------------------------------------------------------------------------------------- |
| 设备分类/能力       | `test_device_profiles.py`、`test_capabilities.py`                                        |
| readtable 归一化    | `test_protocol.py`                                                                       |
| 状态字段            | `test_state_parsers.py` 或设备专用测试                                                   |
| 控制语义            | `test_control.py`、`test_control_router.py`                                              |
| transport 选择      | `test_control_transport.py`、`test_control_executor.py`、`test_coordinator_transport.py` |
| 通用配置/运行时选项 | `test_config_flow_reauth.py`、`test_runtime_options.py`                                  |
| 设备传输诊断实体    | `test_transport_sensor.py`、`test_capabilities.py`、`test_control_transport.py`          |
| 二进制帧/LAN 会话   | `test_binary_protocol.py`、`test_lan_packet.py`、`test_lan_gateway_connection.py`        |
| ID 匹配/实时分发    | `test_status_dispatcher.py`                                                              |
| 门锁事件/媒体/密码  | `test_lock_manager.py`、`test_lock_media_manager.py`、`test_temp_password_manager.py`    |
| 服务公开边界        | `test_service_handlers.py`、`test_video_archive.py`                                      |
| 项目命名            | `test_project_identity.py`、身份检查脚本                                                 |

提交前至少执行：

```bash
python -B -m unittest discover -s tests -q
python -B -m compileall -q custom_components tests tools
node --check custom_components/orvibo_smart_control/www/orvibo-smart-control-door-lock-card.js
git diff --check
```

结构性改动还应运行 `codegraph sync .`。CI 会进一步执行 HACS validation 和 Home Assistant
hassfest。测试 fixture 必须最小化、稳定且不可逆脱敏。

## 文档与版本

用户可见变化按职责更新：

| 改动                 | 文档                                                    |
| -------------------- | ------------------------------------------------------- |
| 新型号或能力         | `docs/DEVICE_SUPPORT.md`                                |
| 服务、事件或卡片     | `docs/SERVICES_AND_EVENTS.md`、`services.yaml`          |
| 传输或状态语义       | `FUSION_DESIGN.md`、README 通道摘要                     |
| 跨域或配置变化       | `docs/MIGRATION.md`                                     |
| 凭据、媒体或网络边界 | `SECURITY.md`                                           |
| 通用配置入口或默认值 | README 配置表、三份 config flow 翻译、相关架构/安全文档 |
| 任意用户可见变化     | `CHANGELOG.md` 的 `Unreleased`                          |

不要在普通 PR 中静默修改 manifest 版本，也不要重写已经发布的 CHANGELOG 历史。文档中的
项目名、域名、仓库 owner 和卡片名必须在 manifest、HACS 元数据、代码和文档中保持一致。

合并到 `main` 后，`.github/workflows/orvibo-smart-control-validate.yml` 会比较当前提交与
上一个提交的 `manifest.json` 版本值。只有版本实际变化且单元测试、HACS validation、
hassfest 全部通过时，CI 才会自动创建 `v<version>` 标签、构建
`orvibo-smart-control.zip` 并发布 GitHub Release。只修改 manifest 的其他字段、普通代码提交
或 PR 不会自动发布；版本号必须使用稳定的 `0.x.y` 格式。

## 脱敏清单

提交前删除或替换：账号、邮箱、手机号、密码及摘要、token、cookie、session/dynamic key、
家庭/用户/设备/状态 ID、UID、extAddr、MAC、IP、家庭/房间名称、门锁授权和密码、COS 临时
凭据、签名 URL、对象私人路径、HA token、绝对配置路径与备份内容。

建议使用：

```json
{
  "familyId": "REDACTED_FAMILY_A",
  "deviceId": "w-REDACTED_DEVICE_A",
  "uid": "REDACTED_UID_A",
  "extAddr": "REDACTED_EXT_ADDR_A"
}
```

同一样本中保持占位符关系一致。不要使用另一个随机但逼真的 32 位值替换真实 ID。
更多安全边界见 [SECURITY.md](SECURITY.md)。

## PR 完成条件

一个可审阅的设备或协议 PR 应同时具备：

- 问题和预期行为的简短说明；
- 设备/环境矩阵和真机结果；
- 分类、解析、控制或平台的最小代码修改；
- 自动化测试及执行命令；
- 脱敏后的最小证据；
- 对应文档和 `Unreleased` 记录；
- 无项目身份漂移、无秘密、无无关格式化。

PR 描述可使用以下骨架：

```markdown
## 问题与范围

## 设备和环境

- 商品/固件：
- 云区/接入方式/网关：
- 协议分类字段：

## 证据

- 初始快照：
- 状态推送：
- 控制请求、回执和实物结果：
- 范围、单位和特殊语义：

## 验证

- 定向测试：
- 全量测试：
- 真机回归：

## 脱敏确认

- [ ] 不含账号、凭据、密码摘要或 token
- [ ] 不含真实家庭/设备/网络标识
- [ ] 不含私人媒体 URL、对象键或主机路径
- [ ] 嵌套 JSON、图片和视频已人工检查
```

维护者可以接受只读识别和诊断改进，同时暂缓证据不足的控制功能。对未知设备保持不动作，是项目的明确兼容策略。
