# 门锁方向、开锁人及抓拍

本轮依据 HomeMate 5.2.6.307（APK SHA-256
`6ea3f3eff41ad4657ca437d10c79022949a1f080af857a8800dc6b6819abac8c`）
已有反编译产物核对，未执行 APK，也未向门锁发送新命令。下列源路径相对于逆向包
`java/sources/`；这些是静态观察，不代表所有固件已实测。

| 静态证据 | 集成处理 |
| --- | --- |
| `com/orvibo/homemate/model/bind/scene/thing/a.java:24`：`doorLock/unlockEvent`，`userId`，password/card/fingerprint/temppassword/face | 兼容新事件并规范化开锁方式 |
| `com/orvibo/homemate/core/g.java:2947`、`:2953`：`unlock`，`userID`，`picture_url` | 兼容旧事件，保留抓拍地址 |
| 同文件 `:2960`、`device/smartlock/auth/AuthInfoFragment.java:478` | 用户编号按锁侧授权编号处理，不当作登录账号 |
| `com/orvibo/homemate/util/f3.java:158`、`:197` | 旧独立记录枚举：1 指纹、2 密码、3 卡、4 临时密码、5 钥匙 |
| `com/orvibo/homemate/device/smartlock/ble/status/f.java:105`、`LockSafetyProblem.java:287` | 旧独立记录 20 明确对应门内开锁；只在 `value2=65535` 的旧记录中解析 |
| `com/orvibo/homemate/model/bind/scene/thing/a.java:39` | `insideLockState` 只表示反锁，不表示内开 |
| `com/orvibo/homemate/device/smartlock/LockKeyUnlockAlarmSettingActivity.java:115` | `manual_unlock_alarm` 属于钥匙报警，不能因 manual 字样认定内开 |

当前集成按用户指定规则判断：密码（含临时密码）、指纹、钥匙和卡片判外开；其余
开锁方式（含人脸及未知方式）、只有门磁变化而未关联四类凭据的开门统一判内开。
内开不继承任何上次外开用户，显示人员未知。这是方向推断规则，不代表固件明确上报内开。
不会把新物模型的数字 `type=20` 套用旧枚举；也不把旧打包 `value4` 当成直接枚举。

姓名由集成选项映射提供，支持设备及凭据方式隔离；锁侧没有上报身份时不能恢复开门人。
详见 [服务与事件参考](SERVICES_AND_EVENTS.md)。

抓拍使用既有 COS 凭据和对象键：立即尝试，失败后重试并重新签名。下载完成后原子保存，
按设备裁剪为最近配置数量（默认 30）；保留中的图片不受自动 7 天录像清理影响。
同秒不同对象分开归档，旧事件迟到不会替换当前较新的画面。重载从本地恢复，卸载会取消
下载任务并等待正在进行的磁盘操作完成。

回归覆盖：两种事件字段、管理员 0、凭据方向优先、内开清除身份、过期及重复事件、一次
开锁仅归属一次开门、设备及方式姓名隔离、同秒抓拍、下载重试、并发保存、重载恢复、
数量裁剪、图片筛选和摄像头更新驱动卡片刷新。测试不代替门锁实机验证。

## 总览卡片

总览卡片集中显示主锁、门磁与反锁状态，电池优先展示有有效百分比的来源，0% 仍有效。
最近开门区展示开门人、内外开方向、开锁方式与时间；内开人员未知时不会显示上次外开姓名。

猫眼抓拍区默认展示最近 30 张，最新一张单独预览，其余图片可横向滑动查看。
点击图片打开大图，可用上一张、下一张按钮或键盘左右键翻图，Escape 关闭并恢复图片焦点。
图片下载最多并发 2 张，每次请求（含重试时重新解析地址和读取图片内容）最多等待 12 秒，
失败或一直挂起时取消请求并自动重试一次；仍失败会显示“图片加载失败，请刷新抓拍重试”。
已下载图片在卡片内复用，最新预览、缩略图和大图共享同一份图片，状态刷新不会重复下载。
右上角刷新按钮可重新尝试失败图片，刷新失败时保留已显示的抓拍；切换配置或移除卡片时
会取消未完成请求并释放图片缓存。
`history_limit` 只控制卡片显示数量，磁盘保留数量仍在集成选项“门锁抓拍保留数量”中设置。

从 `0.2.6b14` 起，图片与录像通过集成的独立媒体源读取：
`media-source://orvibo_smart_control/archive/<设备>/<文件>`。媒体接口沿用 Home Assistant
鉴权与签名，现有归档仍保存在 `config/media/orvibo_smart_control/`，无需配置 `media_dirs`
或搬动文件。卡片解析图片时使用 HA 的 `media_content_id` 参数。

卡片跟随 Home Assistant 深浅色主题，适配窄列与手机。升级并重启后，集成会在 HA 启动
完成时再次核对资源地址并原地更新；保留已有资源条目与卡片配置。浏览器仍加载旧脚本时
可强制刷新页面。独立临时密码卡片继续使用原有自定义类型和服务。
