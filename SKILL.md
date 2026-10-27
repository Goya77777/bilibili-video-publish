---
name: bilibili-video-publish
description: 通用「视频批量投稿 + 投稿记录回填」流程。把本地目录或 Windows 网络共享里的视频批量投到 B站三连创意中心绑定的真实 UP 账号（指定 MID），再按「投稿时间 / bvid / 原始文件名称」回填腾讯文档投稿记录表。覆盖扫描校验、MID+MD5 查重、断点续传上传、审核轮询、BVID 双重复核（稿件库 + 公开接口）、腾讯文档写入与读回校验。用户提到投稿、上传视频到B站、批量投三连、投稿记录表、回填腾讯文档时使用。
---

# B站视频批量投稿 + 投稿记录回填

一条可复跑的流水线：**扫描 → 校验 → 查重 → 上传建稿 → 等审核 → 导出记录 → 回填腾讯文档 → 复核**。
与具体项目无关，账号/分区/标签/表结构全部来自一份 JSON 配置。

## 什么时候用 / 不用

**用**：有一批（或一个大目录里的）本地 / UNC 共享视频，要投到某个**已授权的真实 UP（MID）**，并且要留一份可对账的投稿记录。

**不用**：
- 投到自己的普通个人账号（不是三连创意中心的「绑定 UP」）——走人工后台上传更简单。
- 只查稿件、不下发投稿 —— 直接查稿件库接口即可，别跑这条流水线。

## 前置条件

| 项 | 说明 |
|---|---|
| Python 3.9+ | 建议用 WorkBuddy 自带解释器；纯标准库，无第三方依赖 |
| 三连凭据 | `%LOCALAPPDATA%\bilibili-sanlian-ads\credentials.json`，内含目标账户的 `refresh_token` |
| 目标 MID | 必须是该账户下**已授权**的 UP；脚本会调 `enterprise/mid` 实时校验，对不上直接拒跑 |
| 腾讯文档 | 连接器 `tencent-docs` 已连接；或退化为导出 CSV 手动粘贴 |
| PowerShell | 上传环节调用 `scripts/upload_bound_mid_archives.ps1`（Windows） |

凭据过期只需 `refresh_token` 有效（30 天内刷新过）。**access_token 只有 24h，过期不算授权失效**，脚本会自动刷新，不用提前提醒用户。

## 开工前必须先问清四件事（缺一不可）

**在写配置、更在上传之前**，逐条向使用者确认，得到明确答复后再动手。前三项缺失时 `plan` 会直接拒绝放行；第四项靠这条流程约束（工具管不到）。

| 必填 | 为什么要问 | 落到配置 |
|---|---|---|
| **运营组** | 决定回填**哪一张**投稿记录表（不同组表不同） | `group`（或命令行 `--group`） |
| **品牌** | 投稿记录的必填字段 | `brand` |
| **项目** | 投稿记录的必填字段 | `project` |
| **标题由谁定** | 决定标题是**客户给的**还是**我拟的**；我拟的必须先在聊天里给你看过 | `titles`（见下节） |

```
# 确认「运营组」后，先看这个组登记过哪张表
python bili_publish.py groups
```

登记表是 `assets/groups.json`（`运营组 → 腾讯文档 fileId/tab`）。
**这个组没登记过**时：先问使用者要该组投稿记录表的链接，把 `fileId`/`tab` 补进 `groups.json`，再往下走。

> 品牌 / 项目不要从文件名或目录名去猜——猜错了整张记录表的维度就是错的，问一句只要几秒。

## 标题怎么来（两条路，都得先说清）

**工具永远不自己编标题**：`config.titles` 是唯一来源，`plan` 只做校验（长度/重复/绝对化用语）。
所以标题必须有一个明确的来源约定，别默认。

| 路径 | 怎么走 |
|---|---|
| **A. 客户/使用者给现成标题** | 让对方按 `titles` 导出的表填，或直接把「文件名 → 标题」贴过来 |
| **B. 我（agent）拟** | 拟完**先在聊天里列给使用者看**，明确说「这几条是我拟的，确认没问题我再写进配置」，得到确认后才落地 |

```bash
# 导出一张「文件名 → 待填标题」的表（CSV/TSV，用 Excel/WPS 填）
python bili_publish.py titles --config mybatch.json

# 填好后写回 config.titles（会保留配置里其他键）
python bili_publish.py titles --config mybatch.json --import mybatch_titles.csv
```

`titles` 会自动带出已填的标题，所以它同时是一张**人工复核表**——投稿前扫一眼就能发现「哪条是客户定的、哪条是我拟的」。
若素材文件名里已经带了标题编号（如 `_标题1 / _标题2`），那通常说明客户已有现成文案，**优先走 A，别自己拟**。

## 五分钟上手

```bash
cd <skill>/scripts

# 1) 复制配置模板，改成这一批
copy config.sample.json mybatch.json

# 2) 扫描 + 导出待填标题表（把 CSV 发给使用者填，或自己拟完先给对方确认）
python bili_publish.py scan   --config mybatch.json
python bili_publish.py titles --config mybatch.json
python bili_publish.py titles --config mybatch.json --import mybatch_titles.csv

# 3) 校验（标题/大小/运营组/品牌/项目，这一步不产生任何投稿）
python bili_publish.py plan   --config mybatch.json

# 4) 一把梭：查重 → 清单 → 上传 → 导出记录
python bili_publish.py all    --config mybatch.json

# 5) 独立复核（可随时跑）
python bili_publish.py verify --config mybatch.json

# 随时看进度
python bili_publish.py status --config mybatch.json
```

每一步产出都落盘 `outDir/<batch>_*.json`，**任何一步中断都能单独重跑**；`upload` 重跑是续跑，不会重传已传的文件。

## 六步流程

| 步 | 命令 | 做什么 | 产出 |
|---|---|---|---|
| 1 | `scan` | 遍历素材目录，只收视频；超 `limitMB`（默认 600）会告警 | `<batch>_inventory.json` |
| 1.5 | `titles` | 导出「文件名 → 待填标题」表 / 填好后 `--import` 写回 `config.titles` | `<batch>_titles.csv` / `.tsv` |
| 2 | `plan` | **校验运营组/品牌/项目三个必填项**；逐条校验标题：缺失 / 超 80 字 / 重复 / 含绝对化用语；文件超限也拦 | `<batch>_plan.json` |
| 3 | `dedupe` | 算 MD5，按 **MID + MD5** 查稿件库；已有的不会再投 | `<batch>_dedupe.json` |
| 4 | `manifest` → `upload` | 生成上传清单，交 PowerShell 上传+建稿+轮询审核 | `<batch>_manifest.json`、`<batch>_run.json` |
| 5 | `record` | 导出投稿记录（必备：投稿时间 / bvid / 原始文件名称 / **品牌** / **项目**） | `<batch>_record.json` / `.csv` / `_tdocs_cells.json` |
| 6 | `verify` | 逐条复核：稿件库命中 + 标题一致 + 公开接口 `code=0` 且 `owner.mid` 匹配 | `<batch>_verify.txt` |

### 关键设计（别改）

1. **HTTP 全走 Python `urllib`**。中文不经过 PowerShell 解码，规避 GBK 乱码这一整类坑。
2. **上传用 `.ps1`，且只写纯 ASCII**。脚本做三件事：① 先复制到本地暂存再 PUT（直读 UNC 路径会抛 `UnauthorizedAccessException`）；② 令牌过期自动刷新；③ 每条进度原子落盘。
3. **查重键是 MID + MD5**，不是文件名、也不是标题。同一条视频换个文件名重投，会被正确拦下。
4. **`require_book_in_title` 恒为 false**。上传脚本里这个校验是**全局**的，会对每条都要求清单里的 `book` 出现在标题里；留空或非书名时一开就整批报错。标题合规性由 `plan` 负责。
5. **不自动编标题**。标题是人写的（或我拟、但你确认过的），工具只做红线校验；`plan` 不通过就停，别硬传。

## 回填腾讯文档（本流程的额外一步）

### 用哪条通道：mcporter，不是 MCP 工具

腾讯文档连接器是远端 MCP（`https://docs.qq.com/openapi/mcp`）。它在 WorkBuddy 里**经常「显示 connected 但工具没注入当前会话」**（`not found in the deferred tools index`，重连也治不好）。
**可靠通道是 `mcporter`（MCP 命令行客户端）**，一次性配好：

```bash
bash "<官方 tencent-docs skill 目录>/setup.sh" tdoc_check_and_start_auth
# 输出 READY 就绪；AUTH_REQUIRED:<url> 则把链接给用户授权；ERROR:expired 需重新取 Token
```

调用要注意：**工具名里带点**，必须 `--server` / `--tool` 分开传：

```bash
mcporter call --server tencent-docs --tool "sheet.get_sheet_info" \
  --args '{"file_id":"xxxx"}'
# ❌ 别写成 tencent-docs.sheet.get_sheet_info —— 点号会被切成 server/第一个 tool
```

### 目标子表由「运营组」决定

`assets/groups.json` 维护 `运营组 → fileId / sheetId`。**同一个工作簿里往往一组一个子表**，所以 fileId 一样、sheetId 不同——`?tab=` 后面那段就是 sheetId，别拿它当组名猜。

已登记 7 个组（运营一 / 二 / 三 / 五 / 六 / 七 / 十一组）。新组第一次用，先问用户要链接补进来。

### 一条命令完成写入 + 校验

```bash
python tdocs_fill.py --record <batch>_record.json --group 运营X组
python tdocs_fill.py --record <batch>_record.json --group 运营X组 --dry-run   # 只看不改
```

它做四件事：

1. 读目标子表**第 0 行表头**；
2. **按表头名映射列**（不按位置！目标表列序常常和我们记录列序不同，例如目标表是 `BVID / 投稿时间 / 文件名称 / 品牌 / 项目`）；
3. 从表头下一行开始写（行/列 **0-based**）；
4. **读回逐格比对**，不一致就返回非 0。

表头名不一致时，在该组下加 `"columns": {"原始文件名称": "素材文件名"}` 做显式映射。

### 兜底

`mcporter` 不可用（未安装 / Token 过期）时：

1. 用 `<batch>_record.tsv` 交付给用户粘贴（点开第一个空行 A 列，整块粘，天然按列落位）；
2. 明确告知「腾讯文档未写入，已给 TSV」——**不要静默跳过**。

## 铁律

- **标题必须先落实来源**：要么使用者给的，要么我拟且经使用者确认；不许「默默拟好直接投」。
- **投稿前必须跑 `plan`**，标题有红线问题就不传。绝对化用语的正则已处理「最后/最初/最近/最终」「第一个/第一段」这类**正常用法**，别为了绕校验去改标题。
- **不要按标题判断重复**，一律 MID + MD5。
- **`verify` 是双重复核**：稿件库有记录 ≠ 公开可见。两边都过才算成功。
- **状态 `-1` 是硬失败**（如 `获取cid失败`），稿件库查不到，必须重新上传建稿；`0` + `上传中` 只是没跑完，别急着重传。
- **别把 `outDir` 指向会被自动清理的临时目录**，`run.json` 是续跑的唯一依据。

## 目录

```
bilibili-video-publish/
├── SKILL.md
├── scripts/
│   ├── bili_publish.py                    # 主 CLI（scan/plan/dedupe/manifest/upload/record/verify/status/groups）
│   ├── tdocs_fill.py                      # 腾讯文档写入（走 mcporter；按表头名映射列 + 读回校验）
│   └── upload_bound_mid_archives.ps1      # 加固版上传脚本（纯 ASCII，勿加中文字面量）
├── assets/
│   ├── config.sample.json                 # 配置模板
│   └── groups.json                        # 运营组 → fileId/sheetId 登记表
└── references/
    ├── config.md                          # 配置字段逐项说明
    └── troubleshooting.md                 # 常见报错与处理
```

## 输出给用户的格式

交付时给出：**条数（成功/跳过）→ 回填行范围 → 验收证据（`verify` 摘要 + 逐格比对结论）→ 未完成的条目及原因**。
不要只说「已完成」。
