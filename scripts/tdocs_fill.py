#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
tdocs_fill.py —— 把 bili_publish.py record 产出的投稿记录写进腾讯文档表格（并按表头名映射列）

为什么不用 MCP 工具直调
    腾讯文档连接器是远端 MCP（https://docs.qq.com/openapi/mcp）。在 WorkBuddy 里它常常
    「显示 connected，但工具没注入当前会话」（`not found in the deferred tools index`），
    这时智能体无法直接调用。**可靠通道是 mcporter**（MCP 命令行客户端）：
        mcporter call --server tencent-docs --tool "sheet.set_range_value" --args '{...}'
    注意工具名里带点（sheet.get_sheet_info），所以必须用 --server / --tool 分开传，
    不能写成 tencent-docs.sheet.get_sheet_info（会被点号切错）。

用法
    python tdocs_fill.py --record <batch>_record.json --group 运营X组 [--dry-run]
    python tdocs_fill.py --record ... --file-id X --sheet-id Y     # 不走 groups.json

关键规则（都是踩出来的）
    1. **按表头名映射列，不按位置**：目标表的列序可能和我们的记录列序不同。
    2. 行/列索引 **0-based**；表头通常占第 0 行。
    3. 写完必须读回逐格比对，不能只信「写入成功」。
    4. 目标子表由「运营组」决定（一个工作簿里往往一组一个子表）。
"""

from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
GROUPS_PATH = os.path.join(HERE, "..", "assets", "groups.json")

# 记录列名 → 目标表头候选名（按优先级）。命中任意一个即可。
HEADER_ALIASES = {
    "bvid": ["BVID", "bvid", "稿件ID", "稿件号"],
    "投稿时间": ["投稿时间", "提交时间", "投稿日期", "时间"],
    "原始文件名称": ["文件名称", "原始文件名称", "文件名", "素材名称", "素材文件"],
    "品牌": ["品牌"],
    "项目": ["项目"],
}


def log(m):
    print(m, flush=True)


def read_json(p):
    with io.open(p, "r", encoding="utf-8-sig") as f:
        raw = f.read()
    cands = [i for i in (raw.find("{"), raw.find("[")) if i >= 0]
    return json.loads(raw[min(cands):] if cands else raw)


def find_mcporter() -> str:
    for name in ("mcporter.cmd", "mcporter"):
        p = shutil.which(name)
        if p:
            return p
    # WorkBuddy 自带的 node 目录
    for root, _dirs, files in os.walk(r"C:\Users\%s\.workbuddy\binaries\node" % os.environ.get("USERNAME", "")):
        for f in files:
            if f.lower() in ("mcporter.cmd", "mcporter"):
                return os.path.join(root, f)
    raise SystemExit("找不到 mcporter：先跑官方 tencent-docs skill 的 `bash setup.sh tdoc_check_and_start_auth`")


class Tdocs:
    def __init__(self, binary: str, workdir: str = None):
        self.bin = binary
        self.cwd = workdir or os.getcwd()

    def call(self, tool: str, args: dict, timeout: int = 120):
        p = subprocess.run([self.bin, "call", "--server", "tencent-docs", "--tool", tool,
                            "--args", json.dumps(args, ensure_ascii=False), "--output", "json"],
                           capture_output=True, cwd=self.cwd)
        out = p.stdout.decode("utf-8", "replace").strip()
        err = p.stderr.decode("utf-8", "replace").strip()
        if not out:
            raise RuntimeError("mcporter 无输出 tool=%s\n%s" % (tool, err[-800:]))
        try:
            return json.loads(out)
        except Exception:
            raise RuntimeError("mcporter 输出不是 JSON tool=%s\n%s\n%s" % (tool, out[:600], err[-400:]))


def read_header(td: Tdocs, file_id: str, sheet_id: str) -> dict:
    d = td.call("sheet.get_cell_data", {"file_id": file_id, "sheet_id": sheet_id,
                                        "start_row": 0, "end_row": 0,
                                        "start_col": 0, "end_col": 40})
    head = {}
    for c in d.get("cells") or []:
        name = (c.get("string_value") or "").strip()
        if name:
            head[name] = int(c["col"])
    return head


def resolve_columns(head: dict, record_columns: list, override: dict = None) -> dict:
    """返回 {记录列名: 目标列索引}。按表头名匹配，不按位置。"""
    mapping, unmatched = {}, []
    for col in record_columns:
        target = None
        if override and col in override:
            target = head.get(override[col])
        if target is None:
            for cand in HEADER_ALIASES.get(col, [col]):
                if cand in head:
                    target = head[cand]
                    break
        # 宽容兜底：表头包含该列名的子串
        if target is None:
            for hname, idx in head.items():
                if col in hname or hname in col:
                    target = idx
                    break
        if target is None:
            unmatched.append(col)
        else:
            mapping[col] = target
    return mapping, unmatched


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="把投稿记录写入腾讯文档表格（mcporter 通道）")
    ap.add_argument("--record", required=True, help="<batch>_record.json")
    ap.add_argument("--group", help="运营组（从 assets/groups.json 解析 fileId/sheetId）")
    ap.add_argument("--file-id", help="直接指定 fileId（覆盖 --group）")
    ap.add_argument("--sheet-id", help="直接指定子表 id（覆盖 --group）")
    ap.add_argument("--groups", default=GROUPS_PATH, help="运营组登记表路径")
    ap.add_argument("--mcporter", default=None, help="mcporter 可执行文件路径")
    ap.add_argument("--start-row", type=int, default=None,
                    help="0-based 起始行；不填=自动(表头下一行)")
    ap.add_argument("--dry-run", action="store_true", help="只打印将要写入的内容，不落盘")
    args = ap.parse_args(argv)

    rec = read_json(args.record)
    rows, cols = rec["rows"], rec["columns"]

    file_id, sheet_id = args.file_id, args.sheet_id
    if not (file_id and sheet_id):
        groups = read_json(args.groups)
        entry = groups.get(args.group or "") or {}
        td = entry.get("tdocs") or {}
        file_id = file_id or td.get("fileId")
        sheet_id = sheet_id or td.get("sheetId") or td.get("tab")
        if not (file_id and sheet_id):
            log("解析不到目标表：给了 --group=%r，但登记表里没有它的 fileId/sheetId" % args.group)
            log("维护文件：%s" % os.path.abspath(args.groups))
            log("已登记：%s" % ", ".join(k for k in groups if not k.startswith("_")))
            return 2

    td = Tdocs(args.mcporter or find_mcporter())
    head = read_header(td, file_id, sheet_id)
    if not head:
        log("读不到表头：fileId=%s sheetId=%s（子表 id 对吗？）" % (file_id, sheet_id))
        return 2
    log("目标表 fileId=%s sheetId=%s" % (file_id, sheet_id))
    log("表头：%s" % head)

    override = (rec.get("tdocsColumnMap") or {})
    mapping, unmatched = resolve_columns(head, cols, override)
    log("列映射：%s" % {k: list(head)[list(head.values()).index(v)] if v in head.values() else v
                     for k, v in mapping.items()})
    if unmatched:
        log("")
        log("目标表里没有对应列：%s" % unmatched)
        log("→ 停手。要么在表里补列，要么在 groups.json 该组下加 \"columns\": {\"记录列\": \"表头名\"} 做映射。")
        return 2

    start = args.start_row
    if start is None:
        start = max(head.values()) * 0 + 1      # 表头在第 0 行 → 数据从 1 开始

    values = []
    for i, r in enumerate(rows):
        for col, cid in mapping.items():
            values.append({"row": start + i, "col": cid,
                           "value_type": "STRING", "string_value": str(r.get(col, "") or "")})
    log("")
    log("将写入 %d 行 × %d 列 = %d 个单元格（0-based 行 %d..%d）"
        % (len(rows), len(mapping), len(values), start, start + len(rows) - 1))
    log("首行：%s" % [values[j]["string_value"] for j in range(len(mapping))])
    if args.dry_run:
        log("（dry-run，未写入）")
        return 0

    res = td.call("sheet.set_range_value", {"file_id": file_id, "sheet_id": sheet_id, "values": values})
    if res.get("error"):
        log("写入失败：%s" % res.get("error"))
        return 3
    log("写入完成，开始读回校验 ...")

    got = td.call("sheet.get_cell_data", {"file_id": file_id, "sheet_id": sheet_id,
                                          "start_row": start,
                                          "end_row": start + len(rows) - 1,
                                          "start_col": 0,
                                          "end_col": max(head.values()) if head else 20})
    grid = {}
    for c in got.get("cells") or []:
        grid[(int(c["row"]), int(c["col"]))] = (c.get("string_value") or "")

    bad_rows = []
    for i, r in enumerate(rows):
        bad = []
        for col, cid in mapping.items():
            want = str(r.get(col, "") or "")
            g = grid.get((start + i, cid))
            if g != want:
                bad.append("%s: got=%r want=%r" % (col, g, want))
        if bad:
            bad_rows.append("行%d %s" % (start + i, "; ".join(bad)))

    log("")
    if bad_rows:
        log("校验失败 %d 行：" % len(bad_rows))
        for b in bad_rows[:20]:
            log("  " + b)
        return 4
    log("校验通过：%d 行 × %d 列 全部与记录一致（读回逐格比对）" % (len(rows), len(mapping)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
