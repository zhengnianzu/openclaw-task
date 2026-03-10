#!/usr/bin/env python
"""
论文阅读分析 - 快速启动脚本

用法：
  python run_paper_analysis.py
"""

import asyncio
import sys
from pathlib import Path

# 添加当前目录到路径
sys.path.insert(0, str(Path(__file__).parent))

from openclaw_automation import main


async def run_paper_analysis():
    """运行论文分析任务"""
    print("=" * 70)
    print("📚 论文阅读分析系统")
    print("=" * 70)
    print()
    print("任务：分析单容水箱液位控制实验相关文档")
    print("Agent：paper_reader (学术论文分析助手)")
    print()
    print("文档列表：")
    print("  1. 单容水箱液位控制实验_报告参考文献.pdf")
    print("  2. 多功能过程控制实验平台用户手册.pdf")
    print()
    print("=" * 70)
    print()

    # 配置文件路径
    config_file = Path(__file__).parent / "config_paper_reader.json"

    if not config_file.exists():
        print(f"❌ 错误：配置文件不存在")
        print(f"   路径：{config_file}")
        return

    try:
        # 运行分析
        print("🚀 开始执行分析任务...\n")
        await main(config_file=str(config_file))

        print("\n" + "=" * 70)
        print("✅ 分析完成！")
        print("=" * 70)
        print()
        print("📄 查看结果：")
        print("   execution_report.txt")
        print()
        print("💡 提示：")
        print("   - 报告包含详细的论文分析结果")
        print("   - 可以修改 config_paper_reader.json 自定义查询")
        print("   - 查看 PAPER_READER_GUIDE.md 了解更多用法")
        print()

    except Exception as e:
        print(f"\n❌ 执行失败：{e}")
        print()
        print("💡 故障排查：")
        print("   1. 确保 OpenClaw 正在运行")
        print("   2. 检查 PDF 文件是否在正确位置")
        print("   3. 查看 PAPER_READER_GUIDE.md 获取帮助")
        print()


if __name__ == "__main__":
    asyncio.run(run_paper_analysis())
