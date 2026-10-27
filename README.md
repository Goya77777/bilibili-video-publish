# bilibili-video-publish

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.9%2B-blue.svg)](https://www.python.org/)

> 仓库：<https://github.com/Goya77777/bilibili-video-publish>

批量把本地目录 / Windows 网络共享里的视频**投稿到 B站三连创意中心绑定的真实 UP 账号**，并把「投稿时间 / BVID / 原始文件名 / 品牌 / 项目」**回填到腾讯文档表格**。

一条可复跑的流水线，每一步都落盘，中断可续跑：

```
scan → plan → dedupe → manifest → upload → record → tdocs_fill → verify
```

- **纯 Python 标准库**，无第三方依赖
- 与具体项目无关：账号 / MID / 分区 / 标签 / 标题 / 目标表全部走配置
- 查重键是 **MID + MD5**（换文件名重投也能拦住）
- 每次投稿前强制校验收件人必须确认的三个维度：**运营组 / 品牌 / 项目**

## 为什么值得用

| 坑 | 这里怎么处理 |
|---|---|
| 直读 UNC 路径上传会 `UnauthorizedAccessException` | 先复制到本地暂存再 PUT |
| 中文经过 PowerShell 解码变成乱码 | HTTP 全走 Python `urllib`；`.ps1` 只写纯 ASCII |
| access_token 24h 过期 | 自动用 refresh_token 刷新，不是「授权失效」 |
| 平台处理慢，误判为失败反复重传 | 靠 `run.json` 进度落盘续跑，重跑即续传，零重传 |
| 腾讯文档连接器「显示已连接但工具调不出来」 | 改走 `mcporter` CLI，绕开会话工具清单 |
| 目标表列序和我们不一样，导致串列 | **按表头名映射列**，不按位置 |
| 绝对化用语误伤正常文案 | `最(?!后\|初\|近\|终)`、`第一(?![个段次天年期集章句页行])` |

## 安装

这是一个 **WorkBuddy skill**（本质是「一个目录：`SKILL.md` + `scripts/`」），所以安装 = **把仓库放到 skills 目录下**。

技能目录：`~/.workbuddy/skills/<技能名>/`
Windows 对应：`%USERPROFILE%\.workbuddy\skills\<技能名>\`

### 方式一：git clone（推荐，后续能 `git pull` 更新）

```bash
# macOS / Linux
git clone https://github.com/Goya77777/bilibili-video-publish.git \
  ~/.workbuddy/skills/bilibili-video-publish
```

```powershell
# Windows PowerShell
git clone https://github.com/Goya77777/bilibili-video-publish.git `
  "$env:USERPROFILE\.workbuddy\skills\bilibili-video-publish"
```

### 方式二：npx degit（npm 系命令，不要 git 历史）

```bash
npx degit Goya77777/bilibili-video-publish "$HOME/.workbuddy/skills/bilibili-video-publish"
```

### 方式三：下载 ZIP

网页右上 **Code → Download ZIP** → 解压后把文件夹**改名**为 `bilibili-video-publish`，放进 `~/.workbuddy/skills/`。

> 三种方式装出来的目录结构必须一致：`bilibili-video-publish/SKILL.md` 直接在 skills 下，**不要多套一层**
> （`skills/bilibili-video-publish/bilibili-video-publish/SKILL.md` 是装错了，技能不会被识别）。

### 装完还要做三件事（光拷文件不够）

```bash
cd ~/.workbuddy/skills/bilibili-video-publish
cp assets/groups.sample.json assets/groups.json    # 1) 填你自己的腾讯文档 fileId / sheetId
cp assets/config.sample.json mybatch.json          # 2) 建批次配置
```

3）授权腾讯文档（走官方 `tencent-docs` skill 的 `setup.sh`，输出 `READY` 即可）：

```bash
bash "<官方 tencent-docs skill 目录>/setup.sh" tdoc_check_and_start_auth
```

### 验证装好了

```bash
cd ~/.workbuddy/skills/bilibili-video-publish/scripts
python bili_publish.py --help      # 能看到 scan/plan/titles/... 说明脚本可用
python bili_publish.py groups      # 能列出你填的运营组，说明配置读到了
```

依赖：**Python 3.9+**（纯标准库，无需 pip install）；Windows 上投稿环节还需要 PowerShell 5.1+。

### 能不能用 npm 安装？

| 方式 | 可用 | 说明 |
|---|---|---|
| `npm install bilibili-video-publish` | ❌ | **没有发布到 npm registry**，也不打算发 |
| `npx degit Goya77777/... <目标目录>` | ✅ | npm 系工具，直接从 GitHub 拉文件，**无需发布**，就是上面的「方式二」 |
| `npm install github:Goya77777/...` | ⚠️ 别用 | 只会装进 `node_modules/`，**不会注册成 skill**，等于没装 |
| 发到 npm + `postinstall` 自动拷进 skills 目录 | ⚠️ 不建议 | 往全局应用目录写文件，容易踩权限和多版本问题，安装行为也不可预期 |

为什么不做成 npm 包：这个 skill 是**给智能体读的目录 + Python 脚本**，不是能被 `require()` 的 JS 库。
npm 安装的价值在于「拉依赖 + 注册 `bin`」，这里两者都不需要——`npx degit` 已经是最接近 npm 体验的装法了。

## 快速开始

```bash
cd scripts

# 1) 复制并改配置
cp ../assets/config.sample.json mybatch.json
cp ../assets/groups.sample.json ../assets/groups.json   # 填入你自己的腾讯文档 fileId / sheetId

# 2) 扫描 + 导出待填标题表（发给人填，或自己拟完先给对方确认）
python bili_publish.py scan   --config mybatch.json
python bili_publish.py titles --config mybatch.json
python bili_publish.py titles --config mybatch.json --import mybatch_titles.csv

# 3) 校验，不产生任何投稿
python bili_publish.py plan --config mybatch.json      # 标题/大小/必填项 不通过就停

# 4) 一把梭：查重 → 清单 → 上传 → 导出记录
python bili_publish.py all --config mybatch.json

# 5) 独立复核每个 BVID
python bili_publish.py verify --config mybatch.json

# 随时看进度
python bili_publish.py status --config mybatch.json
```

回填腾讯文档：

```bash
python tdocs_fill.py --record mybatch_record.json --group 运营组名 --dry-run   # 预演
python tdocs_fill.py --record mybatch_record.json --group 运营组名             # 写入 + 读回校验
```

## 前置条件

| 项 | 说明 |
|---|---|
| 三连凭据 | `%LOCALAPPDATA%\bilibili-sanlian-ads\credentials.json`，内含目标账户的 `refresh_token` |
| 目标 MID | 必须是该账户下**已授权**的 UP，脚本会调 `enterprise/mid` 实时校验 |
| 腾讯文档 | 官方 `tencent-docs` skill + `mcporter`（`bash setup.sh tdoc_check_and_start_auth` 输出 `READY`） |
| PowerShell | 上传环节调用 `scripts/upload_bound_mid_archives.ps1` |

## 目录

```
├── SKILL.md                       # 给智能体看的完整作业流程
├── scripts/
│   ├── bili_publish.py            # 主 CLI
│   ├── tdocs_fill.py              # 腾讯文档写入（mcporter 通道，按表头名映射 + 读回校验）
│   ├── upload_bound_mid_archives.ps1
│   ├── sync.sh                    # 一条命令提交并推送到本仓库
│   └── proxy_bridge.py            # 本地 HTTP→SOCKS5 桥（SOCKS-only 机器首次授权用）
├── assets/
│   ├── config.sample.json
│   └── groups.sample.json
└── references/
    ├── config.md                  # 配置字段逐项说明
    └── troubleshooting.md         # 报错对照表（含腾讯文档三类坑）
```

## 标题怎么定

工具**不生成标题**：`config.titles` 是唯一来源，只做红线校验。所以流程上必须先定「谁给标题」：

| 路径 | 做法 |
|---|---|
| 客户/使用者给 | `titles` 导出表 → 对方填 → `--import` 写回 |
| 自己拟 | 拟完**先给人看**，确认后才写进 `config.titles` |

```bash
python bili_publish.py titles --config mybatch.json                       # 导出（自动带出已填的）
python bili_publish.py titles --config mybatch.json --import x_titles.csv # 写回
```

导出表同时是**人工复核表**：投稿前扫一眼就能分清哪条是客户定的、哪条是自己拟的。
文件名里带 `_标题1 / _标题2` 这类编号，通常说明客户已有现成文案——优先用客户的，别自己拟。

## 标题红线

`plan` 会拦下这些（广告法）：

- 「最」——`最后 / 最初 / 最近 / 最终` 放行；「最好 / 最强」拦下
- 「第一」——`第一个 / 第一段 / 第一期` 放行；「行业第一 / 第一品牌 / 销量第一」拦下
- 「无敌 / 天花板 / 顶级 / 全网 / 史上最 / 绝对 / 必看」
- 标题 ≤ 80 字、同批次不重复

## 更新与同步

本地改完直接：

```bash
./scripts/sync.sh "改了什么"
```

它做 `git add -A → commit → pull --rebase → push`。

**首次 push** 需要授权。若机器只有 SOCKS5 代理，GCM（.NET）不认 socks5，先用桥过一次：

```bash
python scripts/proxy_bridge.py --push      # 起本地 HTTP→SOCKS5 桥并 push，弹窗登录一次
git config --local http.proxy socks5://127.0.0.1:10808   # 授权成功后改回直连 SOCKS
```

之后 `./scripts/sync.sh` 直接可用。各类代理/鉴权坑见 [references/troubleshooting.md](references/troubleshooting.md)。

## License

MIT
