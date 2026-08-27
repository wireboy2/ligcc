"""
测试统一入口：一条命令跑完所有能自动跑的测试
============================================================================

    python run_tests.py            # 跑全部（按平台自动跳过跑不了的）
    python run_tests.py -q         # 只在失败时打细节，末尾给汇总
    python run_tests.py config     # 只跑名字里含 "config" 的

为什么是自己写的 runner 而不是 pytest：
  这些测试早就是「自己 print ✅/❌ + 用退出码表态」的独立脚本，改成 pytest
  要么把断言全重写一遍，要么加一层 pytest 只为了 collect —— 都不划算，
  还得多一个依赖（这个项目的卖点之一是 pip install 之后不用装别的）。
  这里要的只是：一条命令、按平台跳过、末尾一张汇总表、退出码正确。

为什么用子进程而不是 import 进来跑：
  · 每个测试的 main() 都 sys.exit()，同进程跑要满地捕 SystemExit；
  · 它们会改全局状态（sys.stdout 包装、overlay.STATE_FILE 指到临时目录、
    PADDLE_PDX_CACHE_HOME），彼此污染的调试成本远高于 fork 一个进程的开销；
  · 退出码本来就是它们的表态方式，子进程正好原样拿到。
"""
import argparse
import os
import subprocess
import sys
import time

# 汇总表里有 ✅/❌，GBK 控制台会 UnicodeEncodeError（和各测试同样处理）。
# 已经是 utf-8 就别再包一层（重复包会关掉底下的 buffer）
try:
    import io
    if (getattr(sys.stdout, "encoding", "") or "").lower().replace("-", "") != "utf8":
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                      line_buffering=True)
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))

# (脚本名, 运行要求, 一句话说明)
#   any     = 任何平台都能跑（Win32 全被 mock 掉了）
#   windows = 需要 Windows（import 就会碰 ctypes.windll，但不用真桌面）
#   desktop = 还需要真实桌面会话（建真窗口、装钩子）
SUITES = [
    ("test_validate.py", "any", "静态架构约束（语法 + 跨模块引用 + 隐蔽性约束）"),
    ("test_config.py", "any", "config 解析：热键字符串、颜色、尺寸"),
    ("test_render_mock.py", "any", "渲染逻辑：mock Win32，句柄生命周期与折行"),
    ("test_stream.py", "windows", "流式作答：SSE 解析、节流投递、断流保留半页"),
    ("test_move.py", "desktop", "浮层交互：拖动/停靠/翻页/尺寸/状态反馈/自动退出"),
]

# 要人眼确认的，不进自动跑：共享画面里到底看不看得见，只能自己开会议看
MANUAL = [
    ("verify_stealth.py", "隐蔽性人工验证：开着共享跑，确认共享画面里看不到浮层"),
    ("overlay.py", "浮层目视自测：可手动试 Ctrl+拖动、滚轮翻页"),
]


def run(script: str, quiet: bool) -> tuple[int, float]:
    """跑一个测试脚本，返回 (退出码, 耗时秒)。"""
    t0 = time.time()
    # 子进程也用 utf-8：-q 模式要把它的输出捕回来解码，让它直接吐 utf-8
    # 最省事（各测试自己也会包一层，这里设了那层就成了空操作）
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    # -u：不缓冲，子进程的 print 与本进程的分节标题不会错位
    cmd = [sys.executable, "-u", script]
    if quiet:
        p = subprocess.run(cmd, cwd=HERE, env=env, capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        if p.returncode != 0:
            print(p.stdout, end="")
            print(p.stderr, end="", file=sys.stderr)
        return p.returncode, time.time() - t0
    p = subprocess.run(cmd, cwd=HERE, env=env)
    return p.returncode, time.time() - t0


def main() -> int:
    ap = argparse.ArgumentParser(description="跑本项目所有能自动跑的测试")
    ap.add_argument("filter", nargs="?", default="",
                    help="只跑名字里含这个词的测试（如 config）")
    ap.add_argument("-q", "--quiet", action="store_true",
                    help="只在失败时打出细节")
    args = ap.parse_args()

    win = sys.platform == "win32"
    results: list[tuple[str, str, float]] = []   # (脚本, 结论, 耗时)
    for script, needs, desc in SUITES:
        if args.filter and args.filter.lower() not in script.lower():
            continue
        if needs != "any" and not win:
            # 非 Windows 上不是失败，是跑不了：CI 的 ubuntu runner 走这条
            why = "需要 Windows 桌面会话" if needs == "desktop" else "需要 Windows"
            results.append((script, f"跳过（{why}）", 0.0))
            continue
        if not args.quiet:
            print()
            print("#" * 74)
            print(f"# {script}  —— {desc}")
            print("#" * 74)
        code, sec = run(script, args.quiet)
        results.append((script, "通过" if code == 0 else f"失败（退出码 {code}）", sec))

    print()
    print("=" * 74)
    print("汇总")
    print("=" * 74)
    width = max((len(s) for s, _, _ in results), default=0)
    failed = 0
    for script, verdict, sec in results:
        mark = {"通过": "✅"}.get(verdict, "⏭ " if verdict.startswith("跳过") else "❌")
        if verdict.startswith("失败"):
            failed += 1
        print(f"  {mark} {script.ljust(width)}  {verdict}"
              + (f"  {sec:.1f}s" if sec else ""))
    if not results:
        print(f"  没有匹配 {args.filter!r} 的测试")
        return 1
    print()
    for script, desc in MANUAL:
        print(f"  ℹ 手动：python {script}  —— {desc}")
    print()
    print("=" * 74)
    print("结论:", "全部通过 ✅" if failed == 0 else f"{failed} 个测试失败 ❌")
    print("=" * 74)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
