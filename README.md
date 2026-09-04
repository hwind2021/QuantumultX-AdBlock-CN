<div align="center">

# Quantumult X 去广告规则集（国内环境优化版）

**专为国内 iOS 用户整理的开屏广告拦截方案 · 分流 / 重写 / 脚本 / 每日自动更新**

[![每日自动更新](https://img.shields.io/badge/自动更新-每日%2004%3A00-brightgreen?style=flat-square)](../../actions)
[![规则校验](https://img.shields.io/badge/安全校验-已通过-blue?style=flat-square)](tools/verify.py)
[![License](https://img.shields.io/badge/license-MIT-lightgrey?style=flat-square)](LICENSE)

</div>

---

## 这个项目解决什么问题

国内 App 的开屏广告之所以难去，是因为它们不走浏览器、不走系统 DNS，而是由 App 内嵌的广告 SDK 直连自家服务器。常规的「改 Hosts」「装描述文件」对它基本无效。

本项目通过 **Quantumult X（圈 X）的网络层拦截能力**，在三个层面同时切断开屏广告链路：

| 拦截层 | 作用位置 | 效果 |
|---|---|---|
| **分流规则** | 域名解析 / 连接建立阶段 | 广告服务器根本连不上 |
| **重写规则** | HTTPS 解密后的请求内容 | 精准匹配广告接口，返回空广告 |
| **JS 脚本** | 响应体 JSON 结构 | 让 App「以为没有广告」，瞬间跳过而非干等超时 |

> **关键的第四步：阻断 HTTPDNS。**
> 头条系、阿里系、腾讯系 App 会用 HTTPDNS 绕过本地 DNS 去解析广告域名，
> 不拦这一层，前面三层的效果会大打折扣。本项目的 `BlockHttpDNS.list` 就是干这个的，**强烈建议一并订阅**。

---

## 与直接使用上游规则的区别

上游 `blackmatrix7/ios_rule_script` 是优秀的通用规则库，但它对**国内开屏广告 SDK** 的覆盖存在明显缺口。我们做过实测比对：

```
45 个开屏广告关键域名，上游命中 10 个，缺失 35 个（覆盖率 22%）
```

缺失的恰好是穿山甲、广点通、百青藤、快手磁力、阿里妈妈这些**真正投放开屏广告**的域名。

本项目的 `Splash-Killer.list` 就是为补齐这块而生的——手工整理、按 SDK 分类、标注拦截优先级，并与上游数据自动合并去重。

**我们做的四件事：**

1. **补齐开屏 SDK 域名**——覆盖 20+ 主流广告平台，含国内厂商
2. **双源交叉验证**——`blackmatrix7` × `anti-AD` 共识域名优先，降低误杀
3. **白名单保命**——支付、登录、推送、系统服务一律放行，规则可以放心常开
4. **每日自动重建**——上游更新后 4 小时内自动同步，并跑安全校验

---

## 快速开始

> 完整图文步骤见 [docs/01-快速上手.md](docs/01-快速上手.md)

### 第一步：订阅规则

打开 Quantumult X → 右下角「风车」→ **配置文件** → 右上角 `+` → 选择「**远程资源**」，分别添加：

**① 开屏广告专项（核心，必装）**
```
https://raw.githubusercontent.com/hwind2021/QuantumultX-AdBlock-CN/main/quantumultx/filter/Splash-Killer.list
```

**② HTTPDNS 拦截（必装，否则效果打折）**
```
https://raw.githubusercontent.com/hwind2021/QuantumultX-AdBlock-CN/main/quantumultx/filter/BlockHttpDNS.list
```

**③ 通用广告精简版（推荐）**
```
https://raw.githubusercontent.com/hwind2021/QuantumultX-AdBlock-CN/main/quantumultx/filter/AdBlock-Lite.list
```

**④ 开屏广告重写规则（需 MITM，效果最强）**
```
https://raw.githubusercontent.com/hwind2021/QuantumultX-AdBlock-CN/main/quantumultx/rewrite/AdBlock-Splash.conf
```

> 订阅类型分别选择「分流规则」和「重写规则」，策略全部保持默认的 `REJECT`。

### 第二步：安装 JS 脚本（可选但推荐）

把 [splash-killer.js](quantumultx/script/splash-killer.js) 保存到「脚本」目录，
然后订阅脚本型重写规则 [AdBlock-Script.conf](quantumultx/rewrite/AdBlock-Script.conf)。

脚本的作用是把 App 卡在开屏页等待超时的 1.5~5 秒，压缩成**瞬间跳过**。

### 第三步：开启 MITM（使用重写规则才需要）

「设置」→「MITM」→ 开启，并把 [MITM.list](quantumultx/mitm/MITM.list) 里的主机名加入 `hostname`。

详见 [docs/06-MITM与证书配置.md](docs/06-MITM与证书配置.md)。

---

## 产物清单

### 分流规则（`quantumultx/filter/`）

| 文件 | 规模 | 说明 | 建议 |
|---|---|---|---|
| `Splash-Killer.list` | ~470 条 | **开屏广告专项**，20+ 主流 SDK | ✅ 必装 |
| `BlockHttpDNS.list` | ~62 条 | 阻断 HTTPDNS 绕过 | ✅ 必装 |
| `AdBlock-Lite.list` | ~2.3 万条 | 开屏 + 双源共识通用广告 | ✅ 推荐 |
| `AdBlock-Full.list` | ~29 万条 | 全量合并去重，拦截率最高 | ⚠️ 高性能设备 |
| `AdBlock-Privacy.list` | ~4 万条 | 隐私追踪与统计上报 | 🔘 可选 |
| `AdBlock-AntiHijack.list` | ~228 条 | 运营商劫持与插播广告 | 🔘 可选 |

### 重写规则（`quantumultx/rewrite/`）

| 文件 | 规模 | 说明 |
|---|---|---|
| `AdBlock-Splash.conf` | ~112 条 | 开屏广告精准拦截（需 MITM） |
| `AdBlock-Feed.conf` | ~108 条 | 信息流 / Banner / 弹窗（需 MITM） |
| `AdBlock-Script.conf` | ~36 条 | 依赖 JS 脚本的高级拦截 |
| `AdBlock-All.conf` | ~256 条 | 以上全部合集 |

### JS 脚本（`quantumultx/script/`）

| 文件 | 说明 |
|---|---|
| `splash-killer.js` | 通用开屏广告拦截，清空响应中的广告内容，App 立即跳过 |
| `feed-killer.js` | 信息流广告逐条甄别剔除，保留正常内容 |

### 其他

| 文件 | 说明 |
|---|---|
| `mitm/MITM.list` | 重写规则依赖的主机名清单 |
| `conf/sample.conf` | 完整配置示例，可直接参考 |
| `task/task.conf` | 定时任务示例（自动更新规则与脚本） |

---

## 数据源

本项目不生产原始规则，而是做**筛选、合并、去重、校验与国内化增强**。所有上游源均处于活跃维护状态：

| 上游 | Stars | 更新频率 | 用途 |
|---|---|---|---|
| [blackmatrix7/ios_rule_script](https://github.com/blackmatrix7/ios_rule_script) | 27k+ | 每日 | 主力：分流与重写规则 |
| [privacy-protection-tools/anti-AD](https://github.com/privacy-protection-tools/anti-AD) | 10k+ | 每周 | 中文区高命中广告域名 |
| [ACL4SSR/ACL4SSR](https://github.com/ACL4SSR/ACL4SSR) | 6.6k | 每周 | 规则交叉验证参考 |
| [Cats-Team/AdRules](https://github.com/Cats-Team/AdRules) | 3.6k | 每日 | 补充源 |

每日 04:00（UTC+8）自动拉取上游 → 合并去重 → 安全校验 → 提交发布。

---

## 每日更新机制

仓库内置两个 GitHub Actions 工作流：

- **`daily-update.yml`** — 每日 04:00（UTC+8）拉取上游最新规则，重建全部产物，
  跑安全校验，校验通过才提交；失败则保留上一版并推送告警。
- **`build-check.yml`** — 每次 PR / Push 时验证构建链路可用，防止改坏工具链。

你也可以手动触发：仓库页面 → Actions → 选择工作流 → **Run workflow**。

---

## 目录结构

```
QuantumultX-AdBlock-CN/
├── README.md                    # 你正在看的这份
├── docs/                        # 使用教程（10 篇）
├── quantumultx/
│   ├── filter/                  # 分流规则（订阅用）
│   ├── rewrite/                 # 重写规则（需 MITM）
│   ├── script/                  # JS 去广告脚本
│   ├── mitm/                    # MITM 主机名清单
│   ├── task/                    # 定时任务示例
│   └── conf/                    # 完整配置示例
├── tools/
│   ├── build.py                 # 构建引擎
│   ├── verify.py                # 安全校验器
│   ├── push_via_api.py          # ★ API 推送工具（受限网络备用）
│   └── data/
│       ├── splash_sdk.conf      # ★ 开屏广告 SDK 精选清单（核心资产）
│       └── whitelist.txt        # 关键业务白名单
└── .github/workflows/           # 每日自动更新
```

---

## 文档

| 编号 | 文档 | 适合谁 |
|---|---|---|
| [01](docs/01-快速上手.md) | 快速上手：10 分钟配好 | 所有人 |
| [02](docs/02-开屏广告原理与拦截策略.md) | 开屏广告原理与拦截策略 | 想搞懂原理 |
| [03](docs/03-分流规则使用教程.md) | 分流规则详解与自定义 | 想调整规则 |
| [04](docs/04-重写规则使用教程.md) | 重写规则语法与调试 | 想精准拦截 |
| [05](docs/05-JS脚本使用教程.md) | JS 脚本开发与调试 | 想写自己的脚本 |
| [06](docs/06-MITM与证书配置.md) | MITM 与证书配置 | 首次开启 MITM |
| [07](docs/07-常见问题排查.md) | 常见问题排查手册 | 遇到问题解决不了 |
| [08](docs/08-规则订阅与每日更新.md) | 订阅管理与更新机制 | 想长期维护 |
| [09](docs/09-自托管与二次开发.md) | 自托管与二次开发 | 想自己改构建 |

---

## 免责声明

- 本项目仅供学习交流，规则数据全部来自公开开源项目。
- 请在遵守当地法律法规的前提下使用，请勿用于任何商业或违法用途。
- 广告是许多免费服务的主要收入来源。如果你认可某个 App 的价值，
  **建议将其加入白名单**或购买会员支持开发者。
- 使用本规则可能导致部分 App 功能异常，请自行判断风险。

---

## 许可证

[MIT](LICENSE) — 数据来源见上方「数据源」章节，各上游项目保留其自身许可。
