"""Check a local association plan and print JSON; no filesystem writes."""
import argparse
import json
import sys
from .cli import load_json
from .review import ReviewError, compile_plan


def main(argv=None):
    parser = argparse.ArgumentParser(description="检查本地项目归属计划，不执行任何素材操作")
    parser.add_argument("--model", required=True)
    parser.add_argument("--decisions", required=True)
    args = parser.parse_args(argv)
    try:
        result = compile_plan(load_json(args.model), load_json(args.decisions))
    except (ReviewError, ValueError, TypeError, KeyError, OSError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
