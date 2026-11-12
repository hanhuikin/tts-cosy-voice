"""下载大型 wheel 到本地, 支持 Range 断点续传。

用途: PyTorch CUDA wheel (3+ GiB) 从官方源速度较慢且单次命令可能超时,
      先分批下到本地, 再用 `uv pip install <local wheel>` 离线安装。

用法:
    python scripts/fetch_wheel.py <url> <out_path>     # 中断后重跑同一命令继续
"""
import sys
from pathlib import Path

import requests


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: python scripts/fetch_wheel.py <url> <out_path>")
        return 2
    url, out = sys.argv[1], Path(sys.argv[2])
    out.parent.mkdir(parents=True, exist_ok=True)
    head = requests.head(url, allow_redirects=True, timeout=30)
    head.raise_for_status()
    total = int(head.headers["Content-Length"])
    have = out.stat().st_size if out.exists() else 0
    if have == total:
        print(f"[done] already complete: {out} ({total / 1024**3:.2f} GiB)", flush=True)
        return 0
    if have > total:
        print("[reset] local file larger than remote, restarting", flush=True)
        out.write_bytes(b"")  # 截断而非删除, 避免批量删除保护
        have = 0
    headers = {"Range": f"bytes={have}-"} if have else {}
    print(f"[resume] {have / 1024**3:.2f}/{total / 1024**3:.2f} GiB -> {out}", flush=True)
    with requests.get(url, headers=headers, stream=True, timeout=(20, 120)) as r:
        r.raise_for_status()
        mode = "ab" if (r.status_code == 206 and headers) else "wb"
        if mode == "wb":
            have = 0
        next_report = have + 200 * 1024**2
        with open(out, mode) as f:
            for chunk in r.iter_content(4 * 1024**2):
                if not chunk:
                    continue
                f.write(chunk)
                have += len(chunk)
                if have >= next_report:
                    print(f"  {have / 1024**3:.2f}/{total / 1024**3:.2f} GiB", flush=True)
                    next_report = have + 200 * 1024**2
    size = out.stat().st_size
    print(f"[final] {size} / {total} bytes", flush=True)
    return 0 if size == total else 1


if __name__ == "__main__":
    sys.exit(main())
