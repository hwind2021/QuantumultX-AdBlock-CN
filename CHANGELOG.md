# 更新记录

## 2026-09-04 19:33:57

自动同步上游规则。主要产物规模：

- `AdBlock-AntiHijack.list`：228 条
- `AdBlock-Full.list`：291922 条
- `AdBlock-Lite.list`：22802 条
- `AdBlock-Privacy.list`：39918 条
- `BlockHttpDNS.list`：62 条
- `Splash-Killer.list`：467 条
- `AdBlock-All.conf`：256 条
- `AdBlock-Feed.conf`：108 条
- `AdBlock-Script.conf`：36 条
- `AdBlock-Splash.conf`：112 条


本文件由 GitHub Actions 每日自动追加。每次更新包含各产物的规则数量变化。

---

## 2026-09-04

项目初始化。基于 `blackmatrix7/ios_rule_script`、`anti-AD` 等活跃上游源，
构建面向国内环境的 Quantumult X 去广告规则集。

核心工作：

- 整理开屏广告 SDK 域名精选清单，覆盖穿山甲、广点通、百青藤、快手磁力、
  阿里妈妈、AdMob、AppLovin、Unity Ads、ironSource 等 20+ 平台。
  经比对，上游对这批关键域名的覆盖率仅 22%（45 个命中 10 个），
  本项目予以补齐。
- 建立双源交叉验证机制：仅保留 blackmatrix7 与 anti-AD 共识域名进入精简版，
  显著降低误杀风险。
- 建立关键业务白名单，覆盖 Apple 系统服务、支付、通讯、主流 App 核心业务域。
- 实现规则压缩算法，通过后缀覆盖剪枝去除冗余条目。
- 实现安全校验器，在每日自动更新时把关，误杀关键域名即阻断提交。
- 编写通用开屏广告拦截脚本与信息流广告清理脚本，
  解决「广告拦住了但仍需等待 SDK 超时」的体验问题。
