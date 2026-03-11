#!/usr/bin/env python3
"""
根据 map.json 的目标路径（value）清除已复制的文件/目录

用法：
  python cleanup_map.py <map.json路径> [--dry-run]

示例：
  python cleanup_map.py "周子敬_资深财务会计师 _ 审计主管/MAP_Linux.json"
  python cleanup_map.py "周子敬_资深财务会计师 _ 审计主管/MAP_Linux.json" --dry-run
"""

import argparse
import json
import shutil
import sys
from pathlib import Path


def cleanup(map_file: str, dry_run: bool = False) -> None:
    map_path = Path(map_file)
    if not map_path.exists():
        print(f"✗ map 文件不存在: {map_path}")
        sys.exit(1)

    mapping: dict = json.loads(map_path.read_text(encoding="utf-8"))
    print(f"📄 读取 map 文件: {map_path}，共 {len(mapping)} 条映射")
    if dry_run:
        print("🔍 DRY-RUN 模式，仅预览，不执行删除\n")

    deleted = skipped = missing = 0

    for src_str, dst_str in mapping.items():
        dst = Path(dst_str).expanduser()
        if not dst.exists():
            print(f"  - 不存在，跳过: {dst}")
            missing += 1
            continue

        if dry_run:
            kind = "目录" if dst.is_dir() else "文件"
            print(f"  [预览] 删除{kind}: {dst}")
            skipped += 1
        else:
            try:
                if dst.is_dir():
                    shutil.rmtree(dst)
                else:
                    dst.unlink()
                print(f"  ✓ 已删除: {dst}")
                deleted += 1
            except Exception as e:
                print(f"  ✗ 删除失败: {dst}  ({e})")
                skipped += 1

    print()
    if dry_run:
        print(f"预览完成：{skipped} 个待删除，{missing} 个不存在")
    else:
        print(f"清理完成：{deleted} 个已删除，{skipped} 个失败，{missing} 个不存在")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="根据 map.json 清除已复制的目标文件/目录")
    parser.add_argument("map_file", help="map.json 文件路径")
    parser.add_argument("--dry-run", action="store_true", help="预览模式，不实际删除")
    args = parser.parse_args()

    cleanup(args.map_file, dry_run=args.dry_run)
