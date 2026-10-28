# 文档索引

本文档对应 `2.0` 开发分支，当前发布版本为 `2.0.0b4`，协议重构基线为 `0.2.6b17`。
`2.0.0b0` 开启新的主版本线，包含原生调光与 RF 窗帘协议重构。
`2.0.0b1` 修复摄像机/NVR 选择页与诊断中的视频支持标记。
`2.0.0b2` 增加独立开门记录，内开只显示方向和时间。
`2.0.0b3` 修复摄像机首次连接与断线立即重试，并增加本地视频桥 503 原因诊断。
`2.0.0b4` 降低摄像机视频帧解析的 CPU 开销，保持原有帧边界和输出字节。
当前实现以代码和 [Unreleased](../CHANGELOG.md#unreleased) 为准，历史版本文档保留发布时的行为和测试结果。

## 安装与使用

| 文档 | 内容 |
| --- | --- |
| [项目 README](../README.md) | 安装、配置、通道策略、门锁和故障定位 |
| [设备支持矩阵](DEVICE_SUPPORT.md) | 型号、平台、通道、静态支持与真机证据边界 |
| [升级与迁移](MIGRATION.md) | 同域升级、2.0 试用、其他集成迁移与回退 |
| [服务与事件](SERVICES_AND_EVENTS.md) | 16 个服务、标准实体动作、门锁事件与卡片 |
| [门锁方向、开门记录与抓拍](lock-opening-and-snapshots.md) | 方向规则、姓名映射、记录与媒体保存及卡片交互 |
| [Meari 实时视频](meari-live-video.md) | 摄像机/NVR 实时实体、资源与生命周期 |
| [录像回放](meari-hdd-playback.md) | 硬盘及 SD/TF 查询、按时间播放和停止 |
| [视频诊断](nvr-video-diagnostics.md) | 外部会话、设备、通道与编码首帧的诊断动作 |

## 架构、协议与贡献

| 文档 | 内容 |
| --- | --- |
| [运行时架构](../FUSION_DESIGN.md) | 库存、协议档案、路由、状态优先级与生命周期 |
| [2.0 协议重构](protocol-profiles-2.0.md) | 原生调光刻度、模式保留、RF 窗帘及验证记录 |
| [readtable 加载依据](READTABLE_REVERSE_ENGINEERING.md) | 联表、属性时间、局部补丁与身份元数据 |
| [云控制失效审计](CONTROL_RECOVERY_AUDIT.md) | 2026-10-08 的连接恢复修复及当时验证 |
| [XTS 编码首帧](meari-video-probe.md) | 鉴权、封包、超时回收与编码验证边界 |
| [NVR 通道快照协议](meari-nvr-channel-protocol.md) | 云端批量接口、容量与实际通道的区别 |
| [普通录像协议](meari-hdd-protocol.md) | 月/日查询、原生命令、媒体门控与结束标记 |
| [贡献指南](../CONTRIBUTING.md) | Python 3.12、Node 22、证据采集、测试及文档同步 |
| [安全策略](../SECURITY.md) | 凭据、网络、媒体、诊断与漏洞报告 |
| [维护指令](../AGENTS.md) | 开发文档与发布版本政策 |
| [Meari vendor 来源](../custom_components/orvibo_smart_control/meari_transport/vendor/ORIGIN.md) | 固定来源、MIT 许可和本地改动边界 |
| [项目许可证](../LICENSE) | 使用与再分发条款 |

## 历史发布记录

[CHANGELOG](../CHANGELOG.md) 汇总所有版本。以下文件是对应版本的发布快照，
其中“尚未实现”、安装版本和测试数量描述的是当时状态；当前使用请以上面的指南为准。

| 版本 | 主要内容 |
| --- | --- |
| [2.0.0b4](releases/2.0.0b4.md) | 摄像机视频帧扫描 CPU 优化与字节一致性验证 |
| [2.0.0b3](releases/2.0.0b3.md) | 摄像机首次连接恢复、断线重试与 503 诊断 |
| [2.0.0b2](releases/2.0.0b2.md) | 独立开门记录、持久化与内开匿名显示 |
| [2.0.0b1](releases/2.0.0b1.md) | 摄像机/NVR 视频支持显示与诊断一致性 |
| [2.0.0b0](releases/2.0.0b0.md) | 2.0 协议重构、原生调光刻度与 RF 窗帘 |
| [0.2.6b17](releases/0.2.6b17.md) | 抓拍下载超时、重试与图片复用 |
| [0.2.6b16](releases/0.2.6b16.md) | Lovelace 刷新竞态恢复 |
| [0.2.6b15](releases/0.2.6b15.md) | 卡片资源加载时序 |
| [0.2.6b14](releases/0.2.6b14.md) | 资源地址更新与独立媒体源 |
| [0.2.6b13](releases/0.2.6b13.md) | 门锁总览卡片布局与抓拍预览 |
| [0.2.6b12](releases/0.2.6b12.md) | 旧式门锁电量与完整诊断 |
| [0.2.6b11](releases/0.2.6b11.md) | 电量来源和主锁/反锁状态 |
| [0.2.6b10](releases/0.2.6b10.md) | 四类凭据的开门方向规则 |
| [0.2.6b9](releases/0.2.6b9.md) | 开门归属、抓拍保存及姓名隔离 |
| [0.2.6b8](releases/0.2.6b8.md) | IPC SD/TF 存储卡回放 |
| [0.2.6b7](releases/0.2.6b7.md) | 回放卡片紧凑布局 |
| [0.2.6b6](releases/0.2.6b6.md) | 回放卡片资源注册 |
| [0.2.6b5](releases/0.2.6b5.md) | NVR 硬盘查询与按时间回放 |
| [0.2.6b4](releases/0.2.6b4.md) | Meari 实时 camera 实体 |
| [0.2.6b3](releases/0.2.6b3.md) | 指定通道的编码首帧探测 |
| [0.2.6b2](releases/0.2.6b2.md) | NVR 通道云端诊断 |
| [0.2.6b1](releases/0.2.6b1.md) | 厂商设备列表诊断 |
| [0.2.6b0](releases/0.2.6b0.md) | 外部会话诊断的初始版本 |

`bN` 用于区分临时诊断版本。当前发布政策统一使用普通 GitHub Release / Latest，
`Pre-release=false`；早期记录中的预发布安装说明只作为历史保留。
