#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
bili_publish.py —— 通用 B站三连（创意中心）批量投稿 + 投稿记录导出 CLI

用途：把「扫描素材 → 校验清单 → 查重 → 上传建稿 → 等审核 → 导出投稿记录」做成
一条可复跑、可中断续跑的流水线。**与具体项目无关**，所有账号/分区/表结构都来自 config。

设计约束（都是踩过的坑，别改）
1. 所有 HTTP 走 urllib（Python 直连），中文不经过 PowerShell 解码 —— 免疫 GBK 乱码。
2. 上传交给 scripts/upload_bound_mid_archives.ps1（本地暂存 + 令牌自动刷新 +
   进度原子落盘 → 中断后同命令重跑即续跑，零重传）。.ps1 内**只写纯 ASCII**。
3. 每一步产出都落盘（<batch>_*.json），任何一步都能单独重跑。
4. 标题不自动编：由 config.titles 提供，本工具只做校验（长度/重复/禁用词）。

命令
    scan     扫描素材目录 → <batch>_inventory.json
    plan     校验标题与清单 → <batch>_plan.json
    dedupe   MID + MD5 查重（跳过已有的） → <batch>_dedupe.json
    manifest 生成上传清单 → <batch>_manifest.json
    upload   调用上传脚本（可续跑） → <batch>_run.json
    record   导出投稿记录 → <batch>_record.json / .csv（可选腾讯文档 cells）
    verify   复核每个 BVID（稿件库 + 公开接口） → <batch>_verify.txt
    all      依次跑 dedupe → manifest → upload → record
    status   查看各步产出状态
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
UPLOADER = os.path.join(HERE, "upload_bound_mid_archives.ps1")
POWERSHELL = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"

MAPI_BASE = "https://cm.bilibili.com/takumi/api"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
TZ_CN = timezone(timedelta(hours=8))

# 广告法绝对化用语红线。用正则而非纯子串：否则「最后/最初/最近/最终」这类时间副词、
# 「第一个/第一段」这类序数会被误判（真踩过）。
BANNED_WORDS = ["无敌", "天花板", "顶级", "全网", "史上最", "绝对", "必看"]
BANNED_PATTERNS = [
    (re.compile(r"最(?!后|初|近|终)"), "最（绝对化用语）"),
    (re.compile(r"第一(?![个段次天年期集章句页行])"), "第一（绝对化用语）"),
]

DEFAULT_RECORD_COLUMNS = ["投稿时间", "bvid", "原始文件名称", "品牌", "项目"]
OPTIONAL_RECORD_COLUMNS = ["运营组", "标题", "批次", "大小MB", "审核状态", "账号MID"]

GROUPS_PATH = os.path.join(HERE, "..", "assets", "groups.json")


def load_groups() -> dict:
    """运营组 → 投稿记录表。文件不存在就返回空表。"""
    try:
        return read_json(GROUPS_PATH)
    except Exception:
        return {}


# ------------------------------------------------------------------ 基础

def log(msg: str) -> None:
    print(msg, flush=True)


def now_iso() -> str:
    return datetime.now(TZ_CN).isoformat()


def read_json(path: str):
    with io.open(path, "r", encoding="utf-8-sig") as f:
        raw = f.read()
    # 取 "{" 和 "[" 中**先出现**者：有些产出是数组（以 [ 开头），
    # 一味优先找 "{" 会把数组里第一个对象当起点，解析失败。
    cands = [i for i in (raw.find("{"), raw.find("[")) if i >= 0]
    return json.loads(raw[min(cands):] if cands else raw)


def write_json(path: str, value) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = "%s.tmp.%d" % (path, os.getpid())
    with io.open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def md5_of(path: str, chunk: int = 8 << 20) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def find_banned(text: str):
    hits = []
    for w in BANNED_WORDS:
        if w in text:
            hits.append((w, w))
    for rx, why in BANNED_PATTERNS:
        m = rx.search(text)
        if m:
            hits.append((m.group(0), why))
    return hits


def fmt_time(value: str, fmt: str) -> str:
    """把 run.json 里的 ISO 时间转成本地可读字符串；解析失败就原样返回。"""
    s = str(value or "")
    if not s:
        return ""
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TZ_CN)
        return dt.astimezone(TZ_CN).strftime(fmt)
    except Exception:
        return s


# ------------------------------------------------------------------ 配置

class Batch:
    def __init__(self, path: str, group_override: str = None):
        self.path = os.path.abspath(path)
        cfg = read_json(path)
        self.raw = cfg
        self.tag = cfg.get("batch") or os.path.splitext(os.path.basename(path))[0]
        self.out = cfg.get("outDir") or os.path.dirname(self.path)
        os.makedirs(self.out, exist_ok=True)
        self.sources = cfg["sources"]
        self.job = cfg["job"]
        self.titles = cfg.get("titles") or {}
        self.record = cfg.get("record") or {}
        # 回填哪个表由「运营组」决定：命令行 --group > 配置 group
        self.group = (group_override or cfg.get("group") or "").strip()
        self.brand = (cfg.get("brand") or "").strip()
        self.project = (cfg.get("project") or "").strip()
        # 表定位优先级：config.tdocs（显式指定）> 运营组登记表
        tdocs = cfg.get("tdocs") or {}
        if not tdocs.get("fileId") and self.group:
            entry = load_groups().get(self.group) or {}
            tdocs = entry.get("tdocs") or {}
        self.tdocs = tdocs
        self.limit_mb = float(cfg.get("limitMB") or 600)
        self.creds = cfg.get("credentials") or os.path.expandvars(
            r"%LOCALAPPDATA%\bilibili-sanlian-ads\credentials.json")

    def p(self, name: str) -> str:
        return os.path.join(self.out, "%s_%s" % (self.tag, name))

    def require(self, filename: str, command: str):
        path = self.p(filename)
        if not os.path.exists(path):
            log("前置产出缺失：%s" % path)
            log("先跑：python bili_publish.py %s --config <配置>" % command)
            log("（用 status 命令可以看每一步的产出状态）")
            raise SystemExit(2)
        return read_json(path)


# ------------------------------------------------------------------ 三连 API

class Mapi:
    """三连（cm.bilibili.com）开放接口：令牌刷新 + MID 校验 + 稿件库查询。"""

    def __init__(self, auth_path: str, account_id: str, mid: str):
        self.auth_path = auth_path
        self.account_id = str(account_id)
        self.mid = str(mid)
        self.store = read_json(auth_path)
        self.headers = None

    def _account_ids(self, rec) -> list:
        ids = []
        for i in rec.get("selected_account_ids") or []:
            if i not in (None, ""):
                ids.append(str(i))
        for a in rec.get("advertiser_accounts") or []:
            for name in ("account_id", "id", "advertiser_id"):
                if a.get(name) not in (None, ""):
                    ids.append(str(a[name]))
        return sorted(set(ids))

    def _refresh(self, rec) -> None:
        data = None
        for payload in (
            {"grant_type": "refresh_token", "refresh_token": str(rec.get("refresh_token"))},
            {"grant_type": "refresh_token", "refresh_token": str(rec.get("refresh_token")),
             "client_id": str(rec.get("client_id")), "client_secret": str(rec.get("client_secret"))},
        ):
            try:
                body = urllib.parse.urlencode(payload).encode()
                req = urllib.request.Request(
                    MAPI_BASE + "/open_api/oauth2/token", data=body, method="POST",
                    headers={"Content-Type": "application/x-www-form-urlencoded",
                             "X-Call-Source": "sanlian-skills"})
                with urllib.request.urlopen(req, timeout=60) as r:
                    resp = json.loads(r.read().decode("utf-8"))
                if str(resp.get("code")) in ("0", "200") and (resp.get("data") or {}).get("access_token"):
                    data = resp["data"]
                    break
            except Exception:
                continue
        if not data:
            raise RuntimeError("token refresh failed for account %s（refresh_token 可能已过期或授权被取消）"
                               % self.account_id)
        now = datetime.now(timezone.utc)
        rec["access_token"] = data["access_token"]
        if data.get("refresh_token"):
            rec["refresh_token"] = data["refresh_token"]
        rec["access_token_expires_at"] = (now + timedelta(seconds=float(data["expires_in"]))).isoformat()
        rec["updated_at"] = now.isoformat()
        self.store["updated_at"] = now.isoformat()
        write_json(self.auth_path, self.store)
        log("  token refreshed, expires %s" % rec["access_token_expires_at"])

    def _get(self, path: str, params: dict, retries: int = 3):
        uri = MAPI_BASE + path + "?" + urllib.parse.urlencode(params)
        last = None
        for attempt in range(retries + 1):
            try:
                req = urllib.request.Request(uri, headers=self.headers)
                with urllib.request.urlopen(req, timeout=60) as r:
                    return json.loads(r.read().decode("utf-8"))
            except Exception as e:
                last = e
                if attempt >= retries:
                    raise
                time.sleep([3, 5, 10][attempt])
        raise last

    def connect(self) -> None:
        cands = [r for r in (self.store.get("records") or []) if self.account_id in self._account_ids(r)]
        if not cands:
            raise RuntimeError("凭据里没有账户 %s 的记录：%s" % (self.account_id, self.auth_path))
        cands.sort(key=lambda r: str(r.get("access_token_expires_at") or ""), reverse=True)
        for rec in cands:
            exp = None
            try:
                exp = datetime.fromisoformat(str(rec.get("access_token_expires_at")).replace("Z", "+00:00"))
            except Exception:
                exp = None
            if exp is None or exp <= datetime.now(timezone.utc) + timedelta(minutes=2):
                self._refresh(rec)
            self.headers = {"Authorization": "Bearer %s" % rec["access_token"],
                            "X-Call-Source": "sanlian-skills"}
            try:
                resp = self._get("/open_api/launch/cpc/meta_data/enterprise/mid", {"account_id": self.account_id})
            except Exception as e:
                log("  candidate rejected by live check: %s" % e)
                continue
            if any(str(x.get("mid")) == self.mid for x in (resp.get("result") or [])):
                log("  授权 OK（mid=%s 已实时校验）" % self.mid)
                return
        raise RuntimeError("没有一条凭据通过 MID 实时校验（目标 mid=%s）" % self.mid)

    def archive_by_md5(self, md5: str, arc_type: int = 1):
        resp = self._get("/open_api/launch/cpc/meta_data/v3/archive/get_archive", {
            "account_id": self.account_id, "arc_type": arc_type, "mid": self.mid,
            "video_md5": md5, "page": 1, "size": 10, "need_material_tag": 1})
        for a in (resp.get("result") or {}).get("data") or []:
            if str(a.get("mid")) == self.mid and str(a.get("video_md5")).lower() == md5.lower():
                return a
        return None


def public_info(bvid: str):
    """B站公开接口：返回 (code, title, owner_mid)。用于独立复核稿件真的公开可见。"""
    url = "https://api.bilibili.com/x/web-interface/view?bvid=" + bvid
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                               "Referer": "https://www.bilibili.com/"})
    with urllib.request.urlopen(req, timeout=45) as r:
        j = json.loads(r.read().decode("utf-8"))
    d = j.get("data") or {}
    return j.get("code"), d.get("title"), str((d.get("owner") or {}).get("mid"))


# ------------------------------------------------------------------ 步骤

def scan_items(batch: Batch):
    items, seen = [], set()
    for src in batch.sources:
        d = src["dir"]
        if not os.path.isdir(d):
            raise RuntimeError("素材目录不存在：%s" % d)
        exts = tuple(src.get("exts") or [".mp4"])
        recursive = bool(src.get("recursive"))
        walker = os.walk(d) if recursive else [(d, [], os.listdir(d))]
        for root, _dirs, files in walker:
            for name in files:
                if not name.lower().endswith(exts):
                    continue
                path = os.path.join(root, name)
                if path in seen:
                    continue
                seen.add(path)
                items.append({"name": name, "path": path, "bytes": os.path.getsize(path),
                              "source": d})
    return sorted(items, key=lambda x: x["path"])


def cmd_scan(batch: Batch) -> int:
    items = scan_items(batch)
    write_json(batch.p("inventory.json"), {"batch": batch.tag, "generatedAt": now_iso(), "items": items})
    total_mb = sum(i["bytes"] for i in items) / 1024 / 1024
    log("扫描到 %d 个文件，合计 %.1f MB" % (len(items), total_mb))
    big = [i for i in items if i["bytes"] / 1024 / 1024 > batch.limit_mb]
    for i in items:
        flag = "  ← 超过 %.0f MB" % batch.limit_mb if i in big else ""
        log("  %-58s %7.1f MB%s" % (i["name"][:58], i["bytes"] / 1024 / 1024, flag))
    if big:
        log("")
        log("注意：%d 个文件超过 %.0f MB，B站投稿可能被拒，先确认。" % (len(big), batch.limit_mb))
    log("清单：%s" % batch.p("inventory.json"))
    return 0


def cmd_plan(batch: Batch) -> int:
    inv = batch.require("inventory.json", "scan")
    problems, planned, seen_titles = [], [], {}
    # 三个必填项，必须在投稿之前确定（不是等写表时才发现缺）
    if not batch.group:
        problems.append("缺「运营组」：先问使用者是运营几组（决定回填哪张表），写进 config.group")
    elif not batch.tdocs.get("fileId"):
        problems.append("运营组「%s」没有登记对应的投稿记录表："
                        "把该组腾讯文档链接补进 assets/groups.json，或在 config.tdocs 里显式指定"
                        % batch.group)
    if not batch.brand:
        problems.append("缺「品牌」：投稿前必须和使用者确认，写进 config.brand")
    if not batch.project:
        problems.append("缺「项目」：投稿前必须和使用者确认，写进 config.project")
    for it in inv["items"]:
        title = batch.titles.get(it["name"])
        rec = dict(it)
        rec["title"] = title
        if not title:
            problems.append("缺标题（跑 `titles` 导出待填表，或直接在 config.titles 里补）：%s" % it["name"])
        else:
            if len(title) > 80:
                problems.append("标题超过 80 字：%s" % title)
            if title in seen_titles:
                problems.append("标题重复：%s（%s / %s）" % (title, seen_titles[title], it["name"]))
            seen_titles[title] = it["name"]
            for w, why in find_banned(title):
                problems.append("标题含禁用词「%s」（%s）：%s" % (w, why, title))
        mb = it["bytes"] / 1024 / 1024
        if mb > batch.limit_mb:
            problems.append("文件超过 %.0f MB：%s（%.1f MB）" % (batch.limit_mb, it["name"], mb))
        planned.append(rec)

    write_json(batch.p("plan.json"), {"batch": batch.tag, "generatedAt": now_iso(),
                                      "items": planned, "problems": problems})
    log("")
    for rec in planned:
        log("  %-56s %s" % (rec["name"][:56], (rec["title"] or "（缺标题）")[:40]))
    if problems:
        log("")
        log("发现 %d 个问题：" % len(problems))
        for p in problems:
            log("  ! " + p)
        log("先修好 plan 再往下走（不要把不合规的标题传上去）。")
        return 2
    log("")
    log("plan OK：%d 条标题全部通过。落盘：%s" % (len(planned), batch.p("plan.json")))
    return 0


def cmd_titles(batch: Batch, import_path: str = None) -> int:
    """标题清单：导出「文件名 → 待填标题」表给人填；填好后 --import 写回 config.titles。"""
    inv = batch.require("inventory.json", "scan")

    # ---------- 导入模式：把填好的表写回 config.titles ----------
    if import_path:
        path = os.path.abspath(import_path)
        if not os.path.exists(path):
            log("找不到填好的表：%s" % path)
            return 2
        delim = "\t" if path.lower().endswith(".tsv") else ","
        mapping, blank = {}, []
        with io.open(path, "r", encoding="utf-8-sig", newline="") as f:
            rd = csv.reader(f, delimiter=delim)
            header = next(rd, None)
            if not header:
                log("表是空的：%s" % path)
                return 2
            for row in rd:
                if not row or not (row[0] or "").strip():
                    continue
                name = row[0].strip()
                title = (row[1].strip() if len(row) > 1 else "")
                if title:
                    mapping[name] = title
                else:
                    blank.append(name)

        names = {i["name"] for i in inv["items"]}
        unknown = [n for n in mapping if n not in names]
        cfg = read_json(batch.path)
        old = cfg.get("titles") or {}
        old.update(mapping)
        cfg["titles"] = old
        write_json(batch.path, cfg)
        log("已写回 config.titles：新增/更新 %d 条 → %s" % (len(mapping), batch.path))
        if unknown:
            log("⚠️ 表里有 %d 个文件名不在本批素材里（检查是否改了空格/扩展名）：" % len(unknown))
            for n in unknown:
                log("  ! " + n)
        missing = sorted(names - set(old))
        if missing:
            log("⚠️ 还有 %d 条没有标题（`titles` 再导出会只剩这些）：" % len(missing))
            for n in missing:
                log("  ! " + n)
        else:
            log("✔ 全部素材都有标题了，可以跑 plan")
        return 0 if not (missing or unknown) else 2

    # ---------- 导出模式：给一张待填表 ----------
    rows = []
    for it in inv["items"]:
        rows.append({"原始文件名称": it["name"], "标题": batch.titles.get(it["name"], "")})
    filled = sum(1 for r in rows if r["标题"])
    blank = len(rows) - filled

    cols = ["原始文件名称", "标题"]
    csv_path = batch.p("titles.csv")
    with io.open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow(r)
    tsv_path = batch.p("titles.tsv")
    with io.open(tsv_path, "w", encoding="utf-8", newline="") as f:
        f.write("\t".join(cols) + "\n")
        for r in rows:
            f.write("\t".join(r[c] for c in cols) + "\n")

    log("标题清单：%d 条（已填 %d / 待填 %d）" % (len(rows), filled, blank))
    log("  CSV %s" % csv_path)
    log("  TSV %s" % tsv_path)
    log("")
    log("  %-6s %-52s %s" % ("状态", "原始文件名称", "标题"))
    for r in rows:
        log("  %-6s %-52s %s" % ("已填" if r["标题"] else "待填",
                                 r["原始文件名称"][:52], r["标题"][:36]))
    log("")
    log("填好后写回配置：")
    log("  python bili_publish.py titles --config <配置> --import %s" % csv_path)
    return 0


def cmd_dedupe(batch: Batch) -> int:
    plan = batch.require("plan.json", "plan")
    mapi = Mapi(batch.creds, batch.job["accountId"], batch.job["mid"])
    mapi.connect()
    rows = []
    for rec in plan["items"]:
        log("  %-56s MD5 ..." % rec["name"][:56])
        md5 = md5_of(rec["path"])
        hit = mapi.archive_by_md5(md5)
        row = dict(rec)
        row["md5"] = md5
        row["existing"] = bool(hit)
        row["existingBvid"] = (hit or {}).get("bvid")
        rows.append(row)
        time.sleep(0.3)
    new = [r for r in rows if not r["existing"]]
    write_json(batch.p("dedupe.json"), {"batch": batch.tag, "generatedAt": now_iso(),
                                        "accountId": batch.job["accountId"], "mid": batch.job["mid"],
                                        "items": rows})
    log("")
    log("查重完成：新增 %d / 已存在 %d（已存在的不会重复投稿）" % (len(new), len(rows) - len(new)))
    for r in rows:
        if r["existing"]:
            log("  已存在 %-54s %s" % (r["name"][:54], r["existingBvid"]))
    return 0


def cmd_manifest(batch: Batch) -> int:
    dd = batch.require("dedupe.json", "dedupe")
    items = [r for r in dd["items"] if not r["existing"]]
    if not items:
        log("没有新文件需要上传（全部已存在）。")
        return 0
    manifest = {
        "account_id": str(batch.job["accountId"]),
        "mid": str(batch.job["mid"]),
        "bili_category_one": int(batch.job["categoryOne"]),
        "bili_category_two": int(batch.job["categoryTwo"]),
        "arc_tags": list(batch.job["tags"]),
        # 恒为 false：上传脚本的这条校验是**全局**的，会对每条都要求 book 出现在标题里。
        # 这里不启用，标题合规性已由 plan 逐条校验。
        "require_book_in_title": False,
        "items": [{"book": r.get("book") or "", "path": r["path"], "title": r["title"]} for r in items],
    }
    write_json(batch.p("manifest.json"), manifest)
    log("上传清单：%d 条 → %s" % (len(items), batch.p("manifest.json")))
    return 0


def cmd_upload(batch: Batch) -> int:
    mpath = batch.p("manifest.json")
    if not os.path.exists(mpath):
        log("没有 manifest（可能全部已存在），跳过上传。")
        return 0
    runpath = batch.p("run.json")
    stage = os.path.join(batch.out, "stage_%s" % batch.tag)
    log("调用上传脚本（中断后重跑本命令即续跑，不会重传）...")
    log("  清单 %s" % mpath)
    log("  记录 %s" % runpath)
    p = subprocess.run([POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", UPLOADER,
                        "-ManifestPath", mpath, "-OutputPath", runpath, "-AuthPath", batch.creds,
                        "-StageDir", stage,
                        "-MaxPollMinutes", str(batch.job.get("maxPollMinutes", 90)),
                        "-PollIntervalSeconds", str(batch.job.get("pollIntervalSeconds", 20))],
                       capture_output=True)
    out = p.stdout.decode("utf-8", "replace")
    err = p.stderr.decode("utf-8", "replace")
    log(out[-6000:])
    if p.returncode != 0:
        log(err[-2000:])
        log("上传未通过完成门禁（exit=%d）。run.json 已保留，修好后重跑本命令即可续跑。" % p.returncode)
        return p.returncode
    return 0


def build_records(batch: Batch, run: dict):
    cols = batch.record.get("columns") or DEFAULT_RECORD_COLUMNS
    fmt = batch.record.get("timeFormat") or "%Y-%m-%d %H:%M:%S"
    recs, skipped = [], []
    for item in run["results"]:
        ok = str(item.get("status")) == "1" and item.get("bvid")
        if not ok:
            skipped.append({"name": item["name"], "status": item.get("status"),
                            "message": item.get("status_message") or item.get("create_error_msg") or ""})
            continue
        row = {
            "投稿时间": fmt_time(item.get("uploaded_at"), fmt),
            "bvid": item.get("bvid"),
            "原始文件名称": item.get("name"),
            "品牌": batch.brand,
            "项目": batch.project,
            "运营组": batch.group,
            "标题": item.get("title"),
            "批次": batch.tag,
            "大小MB": round((item.get("bytes") or 0) / 1024 / 1024, 1),
            "审核状态": item.get("status_message") or "",
            "账号MID": batch.job["mid"],
        }
        recs.append({c: row.get(c, "") for c in cols})
    return cols, recs, skipped


def cmd_record(batch: Batch) -> int:
    run = batch.require("run.json", "upload")
    cols, recs, skipped = build_records(batch, run)
    write_json(batch.p("record.json"), {"batch": batch.tag, "generatedAt": now_iso(),
                                        "columns": cols, "rows": recs, "skipped": skipped})

    # CSV：UTF-8 带 BOM，Excel/WPS 直接双击不乱码
    csv_path = batch.p("record.csv")
    with io.open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in recs:
            w.writerow(r)

    # TSV：MCP 不可用时的兜底——整块复制粘贴进表格（从第一个空行 A 列开始）
    tsv_path = batch.p("record.tsv")
    with io.open(tsv_path, "w", encoding="utf-8", newline="") as f:
        f.write("\t".join(cols) + "\n")
        for r in recs:
            f.write("\t".join(str(r[c]) for c in cols) + "\n")

    log("投稿记录：%d 条（跳过 %d 条未完成）" % (len(recs), len(skipped)))
    log("  列：%s" % " / ".join(cols))
    log("  JSON %s" % batch.p("record.json"))
    log("  CSV  %s" % csv_path)
    log("  TSV  %s（可直接整块粘贴进腾讯表）" % tsv_path)
    if skipped:
        log("")
        log("未完成（未写入记录，需重跑 upload 补齐）：")
        for s in skipped:
            log("  - %-46s status=%s %s" % (s["name"][:46], s["status"], s["message"]))

    # 腾讯文档：生成写入载荷，供 MCP 的 sheet.set_range_value 一次写入。
    # 同时给两种形态，便于适配不同接口签名：
    #   values —— 行优先二维数组（主流 set_range_value 用这个）
    #   cells  —— A1 地址 → {value} 映射（map 风格接口用这个）
    start = batch.record.get("tdocsStartRow")
    if start:
        start = int(start)
        letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        cells, values_grid, rn = {}, [], start
        for r in recs:
            grid_row = []
            for i, c in enumerate(cols):
                cells["%s%d" % (letters[i], rn)] = {"value": r[c]}
                grid_row.append(r[c])
            values_grid.append(grid_row)
            rn += 1
        payload = {
            "file_id": batch.tdocs.get("fileId") or "",
            "tab": batch.tdocs.get("tab") or "",
            "range": "%s%d:%s%d" % (letters[0], start, letters[len(cols) - 1], rn - 1),
            "startRow": start,
            "columns": cols,
            "values": values_grid,
            "cells": cells,
        }
        write_json(batch.p("tdocs_cells.json"), payload)
        log("  腾讯文档载荷 %s（目标 %s，%d 行 × %d 列）"
            % (batch.p("tdocs_cells.json"), payload["range"], len(values_grid), len(cols)))
    else:
        log("")
        log("提示：config.record.tdocsStartRow 未设置，未生成 A1 载荷。")
        log("      回填腾讯文档用 tdocs_fill.py 即可（它自己读表头、自动从表头下一行开始写），不需要这个配置。")
    return 0


def cmd_verify(batch: Batch) -> int:
    run = batch.require("run.json", "upload")
    mapi = Mapi(batch.creds, batch.job["accountId"], batch.job["mid"])
    mapi.connect()
    lines, ok_all = [], True
    for item in run["results"]:
        name, md5 = item["name"], item.get("md5")
        if str(item.get("status")) != "1":
            lines.append("SKIP  %-46s status=%s" % (name[:46], item.get("status")))
            ok_all = False
            continue
        hit = mapi.archive_by_md5(md5) if md5 else None
        bvid = (hit or {}).get("bvid") or item.get("bvid")
        code, ptitle, pmid = (public_info(bvid) if bvid else (None, None, None))
        bad = []
        if not hit:
            bad.append("稿件库未命中")
        elif hit.get("title") != item.get("title"):
            bad.append("稿件库标题不一致")
        if code != 0:
            bad.append("公开接口 code=%s" % code)
        elif pmid != str(batch.job["mid"]):
            bad.append("公开 owner.mid=%s" % pmid)
        elif ptitle != item.get("title"):
            bad.append("公开标题不一致")
        lines.append("%-5s %-46s %-14s %s" % ("OK" if not bad else "FAIL", name[:46], bvid or "-",
                                              "; ".join(bad)))
        if bad:
            ok_all = False
        time.sleep(0.2)
    text = "\n".join(["复核 %d 条  ALL_OK=%s" % (len(run["results"]), ok_all), ""] + lines)
    with io.open(batch.p("verify.txt"), "w", encoding="utf-8", newline="\n") as f:
        f.write(text + "\n")
    log(text)
    return 0 if ok_all else 3


def cmd_status(batch: Batch) -> int:
    steps = [("inventory.json", "扫描素材"), ("plan.json", "校验标题"), ("dedupe.json", "查重"),
             ("manifest.json", "上传清单"), ("run.json", "投稿运行记录"),
             ("record.json", "投稿记录"), ("verify.txt", "复核报告")]
    log("批次 %s" % batch.tag)
    for fn, desc in steps:
        p = batch.p(fn)
        log("  %-16s %-8s %s" % (fn, "已生成" if os.path.exists(p) else "未生成", desc))
        if fn == "run.json" and os.path.exists(p):
            r = read_json(p)
            n = len(r.get("results") or [])
            passed = sum(1 for x in r["results"]
                         if str(x.get("status")) == "1" and x.get("bvid"))
            log("                   %d 条 / 成功 %d / completed_at=%s" % (n, passed, r.get("completed_at")))
    return 0


def cmd_groups(batch: Batch) -> int:
    """列出已登记的「运营组 → 投稿记录表」。新组要先补进 assets/groups.json。"""
    groups = load_groups()
    groups = {k: v for k, v in groups.items() if not k.startswith("_")}
    if not groups:
        log("还没有登记任何运营组。")
        log("维护文件：%s" % os.path.abspath(GROUPS_PATH))
        return 0
    log("已登记 %d 个运营组（维护文件 %s）：" % (len(groups), os.path.abspath(GROUPS_PATH)))
    for name, v in groups.items():
        t = v.get("tdocs") or {}
        log("  %-10s %s" % (name, t.get("url") or "（未填链接）"))
        log("  %-10s fileId=%s tab=%s  %s" % ("", t.get("fileId"), t.get("tab"), v.get("note") or ""))
    return 0


# ------------------------------------------------------------------ CLI

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="通用 B站三连批量投稿 + 投稿记录导出")
    ap.add_argument("command", choices=["scan", "plan", "titles", "dedupe", "manifest", "upload",
                                        "record", "verify", "all", "status", "groups"])
    ap.add_argument("--config", default=None, help="批次配置 JSON（见 references/config.md）；groups 命令可省略")
    ap.add_argument("--group", default=None,
                    help="运营组（覆盖 config.group）；决定回填哪张投稿记录表")
    ap.add_argument("--import", dest="import_path", default=None,
                    help="titles 用：读入填好的 CSV/TSV，写回 config.titles")
    ap.add_argument("--start-row", type=int, default=None,
                    help="record 用：腾讯文档第一条数据行（覆盖 config.record.tdocsStartRow）")
    args = ap.parse_args(argv)

    # groups 只是查登记表，不需要批次配置
    if args.command == "groups":
        return cmd_groups(None)
    if not args.config:
        ap.error("--config 是必填的（groups 命令除外）")

    batch = Batch(args.config, args.group)
    if args.start_row:
        batch.record["tdocsStartRow"] = args.start_row

    if args.command == "titles":
        return cmd_titles(batch, args.import_path)

    steps = {"scan": cmd_scan, "plan": cmd_plan, "dedupe": cmd_dedupe, "manifest": cmd_manifest,
             "upload": cmd_upload, "record": cmd_record, "verify": cmd_verify, "status": cmd_status,
             "groups": cmd_groups}
    if args.command == "all":
        for name in ("dedupe", "manifest", "upload", "record"):
            log("")
            log("===== %s =====" % name)
            rc = steps[name](batch)
            if rc:
                return rc
        return 0
    return steps[args.command](batch)


if __name__ == "__main__":
    sys.exit(main())
