# Agent instructions

## Development branch and documentation

- `2.0` 是协议重构开发分支；用户指定以 `2.0` 为基础版本，当前发布为 `2.0.0b7`。
  未发布变更写入 `CHANGELOG.md` 的 `Unreleased`，不得回写历史发布结果。
- 修改协议档案时同步 `docs/protocol-profiles-2.0.md`、`docs/DEVICE_SUPPORT.md`、
  `FUSION_DESIGN.md` 和相关使用说明；新增文档加入 `docs/README.md` 索引。
- 静态逆向、离线测试与真机验证分别记录。新增 116、虚拟 MixPad 调光和 RF 34/35
  分支的 `hardware_verified` 仍为 false，不能因同类别已有验证而提升证据等级。

## Release version policy

- 默认发布的是临时诊断版本，版本号使用 `<major>.<minor>.<patch>bN`。
  当前版本线从 `2.0.0b0` 开始，下一次临时发布使用 `2.0.0b8`；不再沿用 `0.2.6bN`。
  这里的“发布”“发版”“更新版本”“发布最新版本”等普通说法，均按临时
  版本处理。
- 只有用户明确说“发布正式版”时，才使用不带 `bN` 后缀的稳定版本，例如 `2.0.0`。
  不能从“最新”“可安装”“HACS 更新”或测试完成等表述推断用户要正式版；有歧义时仍
  使用 bN 临时版本。讨论、引用或举例“发布正式版”不构成实际发布指令。
- 临时版本和正式版本都发布为 GitHub 普通 Release / Latest，`Pre-release` 必须为
  `false`，这样 HACS 能识别更新。`bN` 仅用于区分临时诊断版本，不表示 GitHub 的
  Pre-release 状态。
- 发布前同步更新 `manifest.json`、`README.md`、`CHANGELOG.md` 和对应的
  `docs/releases/<version>.md`。版本标签、Release 名称和安装包版本必须完全一致。
- 不得移动或覆盖已经发布的版本标签、Release 或安装包。要发布新代码，先递增版本号；
  临时版本递增 `bN`，正式版去掉当前 `bN` 后缀或使用用户指定的稳定版本号。

## User-facing release steps

1. 运行适合改动范围的测试，并记录实际结果。
2. 提交并推送后，等待 `unit-tests`、HACS validation 和 hassfest 全部成功。
3. 核验 GitHub Release 是普通 Release / Latest，且下载 ZIP 与发布提交一致。
4. 向用户说明实际版本、安装方式、测试结果和仍需真机验证的部分。
