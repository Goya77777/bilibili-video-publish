#!/usr/bin/env node
/**
 * bilibili-video-publish 安装器
 *
 * 用法：
 *   npx github:Goya77777/bilibili-video-publish install
 *   npx github:Goya77777/bilibili-video-publish            # 默认等同 install
 *   node bin/install.js --dest /some/tmp/dir               # 装到指定目录（自测用）
 *
 * 行为（幂等，可当升级用）：
 *   1. 把 skill 文件拷到 ~/.workbuddy/skills/bilibili-video-publish
 *   2. 已存在则只更新代码，**不动** assets/groups.json 与 mybatch.json
 *   3. 缺的配置骨架从 sample 生成
 *   4. 自检 python 与 CLI，最后打印还差哪几步（腾讯文档授权必须人工）
 *
 * 绝不删除任何文件；不覆盖已有配置。
 */
"use strict";

const fs = require("fs");
const path = require("path");
const os = require("os");
const { execFileSync } = require("child_process");

const PKG_ROOT = path.resolve(__dirname, "..");
const SKILL_NAME = "bilibili-video-publish";

// 装的时候不带的：仓库/包管理/构建产物
const SKIP_TOP = new Set([
  ".git", ".github", "node_modules", "bin", "package.json", "package-lock.json",
  "yarn.lock", ".gitattributes", ".npmignore",
]);
// 按**文件名**硬跳过：这两个是本机真实配置（含内部文档 ID），
// 只要源目录里有（开发者本机就有），无论目标存不存在都不能拷。
// 用 basename 而不是相对路径 —— 防止哪天目录结构变了、或嵌套目录里出现同名文件时漏网。
const SKIP_BASENAME = new Set(["groups.json", "mybatch.json"]);

function log(s = "") {
  process.stdout.write(s + "\n");
}

function parseArgs(argv) {
  const out = { dest: null, cmd: "install" };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === "--dest") out.dest = argv[++i];
    else if (a === "--help" || a === "-h") out.cmd = "help";
    else if (!a.startsWith("-")) out.cmd = a;
  }
  return out;
}

function exists(p) {
  try { fs.accessSync(p); return true; } catch { return false; }
}

function findPython() {
  const candidates = process.platform === "win32"
    ? [["py", ["-3"]], ["python", []], ["python3", []]]
    : [["python3", []], ["python", []]];
  for (const [cmd, pre] of candidates) {
    try {
      const v = execFileSync(cmd, [...pre, "--version"], { encoding: "utf8", stdio: ["ignore", "pipe", "ignore"] });
      if (/Python\s+3\.\d+/.test(v)) return { cmd, pre, version: v.trim() };
    } catch { /* 继续试下一个 */ }
  }
  return null;
}

function copyTree(src, dest, relBase = "") {
  let copied = 0;
  const entries = fs.readdirSync(src, { withFileTypes: true });
  for (const e of entries) {
    const rel = path.join(relBase, e.name);
    if (!relBase && SKIP_TOP.has(e.name)) continue;
    if (e.name === "__pycache__") continue;
    if (SKIP_BASENAME.has(e.name)) continue;

    const from = path.join(src, e.name);
    const to = path.join(dest, rel);
    if (e.isDirectory()) {
      fs.mkdirSync(to, { recursive: true });
      copied += copyTree(from, dest, rel);
    } else {
      fs.mkdirSync(path.dirname(to), { recursive: true });
      fs.copyFileSync(from, to);
      copied++;
    }
  }
  return copied;
}

function main() {
  const args = parseArgs(process.argv.slice(2));
  if (args.cmd === "help") {
    log("用法：npx github:Goya77777/bilibili-video-publish [install] [--dest <目录>]");
    return 0;
  }

  const target = args.dest
    ? path.resolve(args.dest)
    : path.join(os.homedir(), ".workbuddy", "skills", SKILL_NAME);
  const fresh = !exists(path.join(target, "SKILL.md"));

  log("");
  log(`安装 ${SKILL_NAME} → ${target}`);
  log(fresh ? "  模式：全新安装" : "  模式：更新（已存在，只覆盖代码，配置不动）");
  log("");

  fs.mkdirSync(target, { recursive: true });
  const copied = copyTree(PKG_ROOT, target);
  log(`  ✔ 已拷贝 ${copied} 个文件`);

  // 配置骨架（只在缺失时生成）
  const groupsFile = path.join(target, "assets", "groups.json");
  const groupsSample = path.join(target, "assets", "groups.sample.json");
  if (!exists(groupsFile) && exists(groupsSample)) {
    fs.copyFileSync(groupsSample, groupsFile);
    log("  ✔ 已生成 assets/groups.json（从 sample，需要你填自己的 fileId/sheetId）");
  } else if (exists(groupsFile)) {
    log("  · assets/groups.json 已存在，保留原样");
  }

  const batchFile = path.join(target, "mybatch.json");
  const cfgSample = path.join(target, "assets", "config.sample.json");
  if (!exists(batchFile) && exists(cfgSample)) {
    fs.copyFileSync(cfgSample, batchFile);
    log("  ✔ 已生成 mybatch.json（配置骨架，需要你改成这一批的素材目录/账号/MID）");
  } else if (exists(batchFile)) {
    log("  · mybatch.json 已存在，保留原样");
  }

  // npm 打包会剔除 .gitignore，这里补一层最小保护：别让本机配置被 git 提交上去
  const gi = path.join(target, ".gitignore");
  if (!exists(gi)) {
    fs.writeFileSync(gi, [
      "# 本机真实配置（含内部文档 ID），不要提交",
      "assets/groups.json",
      "mybatch.json",
      "*_record.json",
      "*_record.csv",
      "*_record.tsv",
      "*_run.json",
      "__pycache__/",
      "",
    ].join("\n"), "utf8");
    log("  ✔ 已补 .gitignore（防止 groups.json / mybatch.json 被提交）");
  }

  // 自检
  log("");
  const py = findPython();
  if (!py) {
    log("  ⚠ 没找到 Python 3（脚本本身需要 Python 3.9+，纯标准库）");
  } else {
    log(`  ✔ 找到 ${py.version}（${py.cmd}）`);
    try {
      const help = execFileSync(py.cmd, [...py.pre, path.join("scripts", "bili_publish.py"), "--help"],
        { cwd: target, encoding: "utf8", stdio: ["ignore", "pipe", "ignore"] });
      const ok = /scan/.test(help) && /titles/.test(help);
      log(ok ? "  ✔ CLI 自检通过（scan/plan/titles 等命令可用）" : "  ⚠ CLI 自检输出异常");
    } catch (err) {
      log("  ⚠ CLI 自检失败：" + String(err.message || err).split("\n")[0]);
    }
  }

  // 下一步
  log("");
  log("接下来还差三步（工具替不了，必须你来做）：");
  log("");
  log("  1) 填运营组 → 投稿记录表 的对应关系");
  log(`     ${path.join(target, "assets", "groups.json")}`);
  log("     从 groups.sample.json 抄结构，fileId 取链接里 /sheet/ 后面那段，sheetId 取 ?tab= 后面那段");
  log("");
  log("  2) 填批次配置");
  log(`     ${path.join(target, "mybatch.json")}`);
  log("     sources.dir / job.accountId / job.mid / group / brand / project / titles");
  log("");
  log("  3) 授权腾讯文档（回填那一步才需要）");
  log('     bash "<官方 tencent-docs skill 目录>/setup.sh" tdoc_check_and_start_auth');
  log("     输出 READY 即可；首次会经历一次浏览器授权");
  log("");
  log("然后就可以跑了：");
  log(`  cd ${target}`);
  log("  python scripts/bili_publish.py scan   --config mybatch.json");
  log("  python scripts/bili_publish.py titles --config mybatch.json");
  log("  python scripts/bili_publish.py plan   --config mybatch.json");
  log("");
  log(fresh ? "安装完成。" : "更新完成（配置未被改动）。");
  return 0;
}

process.exitCode = (() => {
  try { return main(); } catch (e) { log("安装失败：" + (e && e.message ? e.message : e)); return 1; }
})();
