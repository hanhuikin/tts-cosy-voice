"""音色参考音频: 导出 / 导入 manifest。

仓库把 server/data/voices/ 的参考音频一起版本化 (约 3MB), 但音色元数据
(名称/参考文本/时长) 存在 SQLite 里。本脚本负责两端对账:

    # 从当前数据库导出 manifest (新增或改名音色后执行, 然后提交)
    .venv/Scripts/python.exe scripts/import_voices.py --export

    # 新克隆/换机器后, 按 manifest + 音频文件重建音色库
    .venv/Scripts/python.exe scripts/import_voices.py

已存在的 id 跳过, 音频缺失的条目单独报告, 不会删除任何现有音色。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DATA = REPO / "server" / "data"
VOICES = DATA / "voices"
DB = DATA / "db.sqlite"
MANIFEST = VOICES / "manifest.json"

DDL = """
CREATE TABLE IF NOT EXISTS voices (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    prompt_text TEXT NOT NULL DEFAULT '',
    file TEXT NOT NULL,
    duration REAL NOT NULL DEFAULT 0,
    sample_rate INTEGER NOT NULL DEFAULT 16000,
    created_at TEXT NOT NULL
)
"""

# 与 API 返回保持一致: 不把服务器绝对路径写进仓库
FIELDS = ("id", "name", "prompt_text", "duration", "sample_rate", "created_at")


def export_manifest() -> int:
    if not DB.exists():
        print(f"[export] 数据库不存在: {DB}")
        return 1
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    rows = con.execute("SELECT * FROM voices ORDER BY created_at").fetchall()
    con.close()
    items = []
    missing = []
    for r in rows:
        fname = Path(r["file"]).name
        if not (VOICES / fname).exists():
            missing.append(fname)
            continue
        items.append({**{k: r[k] for k in FIELDS}, "file": fname})
    MANIFEST.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[export] 写出 {len(items)} 条 -> {MANIFEST.relative_to(REPO)}")
    if missing:
        print(f"[export] 注意: 以下音频缺失, 未写入 manifest: {', '.join(missing)}")
    return 0


def import_manifest(dry_run: bool = False) -> int:
    if not MANIFEST.exists():
        print(f"[import] 找不到 {MANIFEST.relative_to(REPO)}")
        return 1
    items = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if not DB.parent.exists():
        print(f"[import] 数据目录不存在: {DB.parent}")
        return 1
    con = sqlite3.connect(DB)
    con.execute(DDL)
    existing = {r[0] for r in con.execute("SELECT id FROM voices")}
    added = skipped = missing = 0
    for it in items:
        vid = it.get("id")
        fname = it.get("file") or f"{vid}.wav"
        path = VOICES / fname
        if not vid or not path.exists():
            missing += 1
            print(f"[import] 跳过(缺文件): {vid} {fname}")
            continue
        if vid in existing:
            skipped += 1
            continue
        print(f"[import] {'将导入' if dry_run else '导入'}: {vid} {it.get('name')} ({path.stat().st_size // 1024}KB)")
        if not dry_run:
            con.execute(
                "INSERT INTO voices(id,name,prompt_text,file,duration,sample_rate,created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (vid, it.get("name") or vid, it.get("prompt_text") or "", str(path),
                 float(it.get("duration") or 0), int(it.get("sample_rate") or 16000),
                 it.get("created_at") or ""),
            )
        added += 1
    if not dry_run:
        con.commit()
    con.close()
    print(f"[import] 新增 {added}, 已存在跳过 {skipped}, 缺文件 {missing}"
          + (" (dry-run)" if dry_run else ""))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--export", action="store_true", help="从数据库导出 manifest.json")
    ap.add_argument("--dry-run", action="store_true", help="只显示将导入的音色, 不写库")
    args = ap.parse_args()
    return export_manifest() if args.export else import_manifest(args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
