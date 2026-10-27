# 常见报错与处理

## 投稿 / 上传

### `status = -1` + `获取cid失败`（或其它 reason）
**硬失败**。稿件库里也查不到，这条记录等于没投。
处理：**不要只重跑 verify**，要重新上传建稿（清掉该条在 `run.json` 里的 `uploaded_at` / `process_id` 后重跑 `upload`，或把该文件单独做一份新清单）。
→ 若整批都这样，先怀疑平台侧拥堵，隔十几分钟再试。

### 长时间停在 `status = 0` + `上传中`
**正常处理中，不是失败**。B站处理速度波动很大：同一批量下午可能 25 分钟跑完，晚上单条就要 10+ 分钟。
处理：等。`MaxPollMinutes` 留足余量（建议 90）；靠 `run.json` 的进度落盘续跑，别反复重传。
注意 `status = 0` + `待审核` 也仍是未完成。

### `upload` exit ≠ 0，但大部分已成功
正常。脚本在「通过数 ≠ 总数」时故意返回非 0。
处理：看 `run.json` 里哪些没完成，修好后**同命令重跑**（会跳过已完成/已上传的，只补剩下的）。

### `Title does not contain book name: ...`
清单里 `require_book_in_title` 被打开了，而该条 `book` 字段为空或不是书名。
处理：把它设为 `false`（本工具生成清单时恒为 false）。标题合规性交给 `plan`。

### `No signed URL returned` / `Signed URL rejected`
签名 URL 拿不到：多半是账户对不上，或该 MD5 已被占用。
处理：先跑 `dedupe` 确认这条是否已在稿件库；再看 `accountId` 是不是目标账户。

### `UnauthorizedAccessException`
直接拿 UNC 路径去 PUT 会触发。
处理：上传脚本已内置「先复制到本地暂存再 PUT」，若报这个说明暂存目录不可写——换 `outDir` 到本地盘。

## 凭据

### `token refresh failed`
`refresh_token` 失效。只有两种情况：① 在 B站后台手动取消了授权；② 超过 30 天没刷新。
处理：让用户重新授权，**不要**反复重试。
提醒口径：`access_token` 24h 过期**不是**授权失效，脚本会自动刷新，别拿这个吓用户。

### `没有一条凭据通过 MID 实时校验`
凭据文件里没有该 `accountId` 的记录，或记录里的 MID 列表不含目标 `mid`。
处理：核对 `accountId` / `mid` 是否配对；必要时换凭据文件。

## 腾讯文档

### `mcp__tencent-docs__*` 工具调不出来（显示 connected 但 not found）
**根因（2026-10-26 查实）**：连接器本身是好的 —— 日志里 `[MCP-AuthDiag] connection-ready {"tools":226}` 明确拿到 226 个工具；`enabledPluginIds` 里也有它、`degraded: []`。
问题在 **sidecar 启动时的工具清单**：会话是按 `--tools=<白名单> --mcp-config=<...>` 拉起子进程的，清单在会话创建那一刻定死，**中途连上的 MCP 不会补进已在运行的会话**。所以 `reconnect_plugin` 返回 connected 也没用。

**解法：改用 mcporter 通道**（不依赖会话工具清单）

```bash
bash "<官方 tencent-docs skill>/setup.sh" tdoc_check_and_start_auth     # READY 即可用
mcporter call --server tencent-docs --tool "sheet.get_sheet_info" --args '{"file_id":"..."}'
```

工具名带点，**必须** `--server` / `--tool` 分开传；写成 `tencent-docs.sheet.get_sheet_info` 会被点号切错，报 `工具: "sheet" 没有注册`。

用 `mcporter list tencent-docs` 看已注册工具名，`mcporter list tencent-docs --schema` 看参数（签名是 TS 风格）。

### 写入后表里列对不上 / 串列
**根因**：目标表列序和我们的记录列序不一致（实测目标表是 `BVID / 投稿时间 / 文件名称 / 品牌 / 项目`，和我们 `投稿时间 / bvid / 原始文件名称 / …` 不同），而且表头叫「文件名称」不叫「原始文件名称」。
**正解**：`tdocs_fill.py` 先读第 0 行表头，**按表头名映射列**，不按位置。表头名差异大时在该组 `columns` 里写显式映射。

### 写进了别的组的子表
**根因**：一个工作簿里一组一个子表，`?tab=` 是子表 id 不是组名（`<SHEET_ID>` 其实是**运营X组**，运营X组是 `<SHEET_ID>`）。
**正解**：`groups.json` 里逐个组登记 `sheetId`，写之前用 `sheet.get_sheet_info` 打印 `sheet_name` 核对。

### 重连返回 connected，但工具**仍然** `not found`
见上面第一条 —— 这是**工具清单不刷新**导致的正常现象，不是授权问题。**不要反复重连**，直接切 mcporter 通道。

### 写入成功但表里没数据 / 写到了别的子表
`tab` / `sheet_id` 传错，或 `file_id` 与 `tab` 不同源。
处理：先 `get_sheet_info` 拿到真实子表清单，再写；多子表文档**必须显式指定 tab**。

### 行数不够导致写入被截断
腾讯表有固定行数上限。
处理：先 `insert_dimension`（`dimension: "row"`，`count` = 需要的行数），再写值。

### 写完读回发现串行 / 错位
九成是 `startRow` 算错了（把表头算成数据行，或忽略中间空行）。
处理：按 A 列**最后一个非空行 + 1** 重新算；重写前用 `clear_range_cells` 清掉错误范围。

## 标题校验

### `标题含禁用词「最」/「第一」`
正则已放行正常用法：`最后 / 最初 / 最近 / 最终`、`第一个 / 第一段 / 第一天 / 第一期`……
被拦下说明是「最好 / 最强 / 行业第一 / 第一品牌 / 第一选择」这类**绝对化宣称**。
处理：改标题，别去改校验规则。

### `标题超过 80 字`
B站稿件标题上限。截短，保留核心利益点。

### `标题重复`
同一批次内两条同名。给其中一条换个切入角度（同集不同角度，别做同义改写，否则素材标题太像）。


## 推送到 GitHub（代理 / 鉴权）

### `CONNECT tunnel failed, response 502` / `schannel: server closed abruptly`
环境里被注入了 `HTTP_PROXY=http://127.0.0.1:<某端口>`（IDE / 沙箱 / 终端工具常见），那个端口连不通 GitHub。
排查：`env | grep -i proxy`；`netstat -ano | findstr LISTENING` 看本机真正在听的代理端口。
处理：`env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY git push`，或给仓库配 `http.proxy`。

### 直连 github.com 超时
国内网络常见。本机若有 SOCKS5 代理（如 v2rayN 的 10808），给仓库配上：

```bash
git config --local http.proxy socks5://127.0.0.1:10808
```

### `ServicePointManager 不支持具有 socks5 方案的代理`
**Git Credential Manager 是 .NET 实现，只支持 HTTP 代理，完全不认 socks5**；而 git 自己的传输层认 socks5h/socks5。两者需求冲突。
处理（只在「还没有缓存凭据」的那一次需要）：

```bash
python scripts/proxy_bridge.py --push      # 起本地 HTTP→SOCKS5 桥，再 push
```

授权成功、凭据被缓存之后，改回 `http.proxy=socks5://...` 即可，后续 push 不再需要桥。

### `could not read Password ... terminal prompts disabled` / `/dev/tty: No such device`
非交互 shell（智能体、CI、无 TTY）里 GCM 无法弹窗，git 退回读 TTY。
处理：用 `proxy_bridge.py --push`（脚本内设了 `GCM_INTERACTIVE=always`），或让用户在自己的终端里执行一次 push。

### `Permission to <owner>/<repo>.git denied to <另一个账号>`
凭据管理器里缓存的是**别的 GitHub 账号**，GCM 直接复用、压根不弹窗。
处理：`GIT_TERMINAL_PROMPT=1 GCM_INTERACTIVE=always`，并指定用户名让旧凭据匹配不上：

```bash
git config --local credential.https://github.com.username <你的账号>
```

或 `git credential-manager github logout` 后再推。

### 403 `Resource not accessible by integration`（用 GitHub 连接器建仓时）
GitHub App 没有 administration 权限，**建不了仓库**。
处理：让用户在网页上建空仓库（不要勾 README/gitignore/license），再用 git 推。
