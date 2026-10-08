# 开关失效排查记录

排查日期：2026-10-08。

用户反馈：在 `smart-home-dashboard` 控制设备开关后，本集成部分开关无法继续控制，界面没有报错，重启后恢复。

## 结论与证据范围

本集成云端 TLS 控制链路存在可通过自动化测试复现的稳定性缺陷：断线后保留错误的连接标志，以及发送失败被吞掉。这些缺陷能形成“开关没有动作、界面却没有报错、重启恢复”的故障路径。相关代码已经修复。

此次没有连接实际 Home Assistant 或物理设备，也没有取得故障时的运行日志。因此，尚不能证明 dashboard 的开关操作直接造成了云端断线，也不能断定所有失效设备都受同一原因影响。

## 两个项目的调用关系

dashboard 的 `src/features/devices/device-controls.ts` 将灯或开关的电源操作映射为 `turn_on` / `turn_off`。`src/adapters/homeAssistantAdapter.ts` 通过 Home Assistant WebSocket 发送 `call_service`，指定实体所属 domain 和 `entity_id`，并将服务调用排队。这里没有直接登录 ORVIBO 云端，也没有独立的 ORVIBO 设备控制会话。

Home Assistant 收到服务请求后，执行当前集成的实体方法，再经 coordinator、`ControlExecutor` 选择 LAN 或云端 TLS。dashboard 与 HA 内置页面操作同一实体时，会使用同一个集成控制链路。

LAN 不可用时回退到云端，云专属设备也使用云端，所以云端连接异常可以只影响部分设备。

## 已复现并修复的缺陷

| 缺陷 | 原实现行为 | 修复后行为 |
| --- | --- | --- |
| TLS reader 断开后保留 `connected=True` | `_listen_loop()` 遇到 EOF 或网络错误退出；`_reconnect()` 因标志仍为 True 直接返回，实际没有重新登录 | 意外退出立即清除连接标志、释放待响应请求，然后重新建立会话 |
| reader 丢失导致放弃恢复 | reader 为 None 时直接退出重连循环 | 无 reader 时仍尝试建立新的连接 |
| 临时清理被当作永久关闭 | `_disconnect()` 设置 `_closed=True`，首次重新登录失败后后续重试可能停止 | 临时清理保留重试能力；集成卸载使用永久关闭 |
| 发送失败被吞掉 | `_send_packet()` 捕获异常后返回，`send_control_*()` 继续返回 True，控制层可能写入乐观状态 | 向调用方抛出 `ConnectionError`，阻止假成功；网络错误同时释放请求并关闭失效 stream |
| writer 缺失时丢弃当前控制 | 发包代码只进行重连，没有发送原指令，上层仍返回成功 | 缺失或关闭的 writer 明确失败，不自动重放结果未知的指令 |
| 发送等待无超时 | `writer.drain()` 长时间不返回时，控制调用持续占用，dashboard 后续排队操作也受影响 | 单次发包等待最多 10 秒，超时释放故障链路并向上报错 |

修改主要位于 `custom_components/orvibo_smart_control/ssl_client.py`，回归测试位于 `tests/test_ssl_client_recovery.py`。

## 验证结果

新增 9 项测试覆盖 EOF、网络错误、缺失 reader / writer、首次登录失败后的继续重试、发送阻塞、正常开关报文、显式关闭以及重试上限。

修复前，断线、发送失败、writer 丢失和发送阻塞的测试未通过；修复后全部通过。

全量验证命令：

```powershell
python -B -m unittest discover -s tests -q
```

结果：运行 391 项测试，390 项通过，1 项跳过；没有失败。

## 真机确认步骤

1. 将修复后的集成代码更新到 Home Assistant，并重载集成或重启一次以加载代码。
2. 依次从 HA 内置页面和 dashboard 控制同一个开关，确认两边状态更新、设备实际动作一致。
3. 如果再次失效，先查看该设备的 `transport_path` 诊断实体是 `lan` 还是 `cloud`，保留故障时间附近集成的日志。
4. 云端断线后应出现重新连接和登录，而非仅“重连成功”但没有新登录；发送失败应向服务调用方报错。

正常发包但没有设备状态回执时仍沿用现有乐观更新策略，因此服务成功不等于物理动作已确认。若纯 LAN 设备也出现同样问题，需要结合网关运行日志继续定位，不能直接归因于本次云端缺陷。
