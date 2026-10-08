"""One-shot preview only: intentionally no deploy, archive, delete, or execute command."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
from .intake import decode, read_bytes
from .preview import build_preview
from .render import render_report
from .review import build_review_model
from .review_render import render_review


def output_directory(output, protected):
    p = Path(output).resolve()
    protected = [Path(root).resolve() for root in protected]
    if any(p == root or p.is_relative_to(root) or root.is_relative_to(p) for root in protected):
        raise ValueError("预览输出必须与中转和项目目录分离")
    p.mkdir(parents=True, exist_ok=True)
    return p


def atomic_output(path: Path, data: bytes):
    fd, temp = tempfile.mkstemp(prefix=".preview-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def load_json(path):
    p = Path(path).absolute()
    return decode(read_bytes(p.parent, p.name))


def main(argv=None):
    parser = argparse.ArgumentParser(description="木梨独立素材分类：生成只读预览与本地确认页面")
    parser.add_argument("--staging", required=True)
    parser.add_argument("--projects", required=True)
    parser.add_argument("--runtime-snapshot", help="120 秒内采集的最小运行状态；缺失时不放行")
    parser.add_argument("--bindings", help="已人工确认的设备/时间段映射 JSON；不包含 AI 自动确认")
    parser.add_argument("--output", required=True, help="独立的本地预览输出目录")
    args = parser.parse_args(argv)
    staging, projects = Path(args.staging).resolve(strict=True), Path(args.projects).resolve(strict=True)
    output = output_directory(args.output, [staging, projects])
    snapshot = load_json(args.runtime_snapshot) if args.runtime_snapshot else None
    bindings = load_json(args.bindings).get("bindings", []) if args.bindings else []
    if not isinstance(bindings, list) or any(not isinstance(r, dict) for r in bindings):
        parser.error("bindings 必须是对象列表")
    report = build_preview(staging, projects, snapshot, bindings)
    model = build_review_model(report)
    artifacts = {
        "分类预览.json": (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode(),
        "分类预览.html": render_report(report).encode(),
        "确认模型.json": (json.dumps(model, ensure_ascii=False, indent=2) + "\n").encode(),
        "拍摄段确认.html": render_review(model, project_root_label=str(projects)).encode(),
    }
    for name, content in artifacts.items():
        atomic_output(output / name, content)
    print(json.dumps({"mode": report["mode"], "summary": report["summary"], "project_count": report["project_count"], "output": str(output)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
