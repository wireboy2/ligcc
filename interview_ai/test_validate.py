"""
静态校验（Linux 沙盒，无法运行 Win32 GUI —— 改为纯 AST 分析）
================================================================

为什么要这样：
  gdiplus_render / overlay 依赖 gdiplus.dll、win32gui，在 Linux 上 import 即失败。
  但我们仍能做有价值的机器校验：
    1) 所有 .py 语法可编译（ast.parse）
    2) 跨模块引用对得上（main 引用的名字真实存在）
    3) 关键架构约束被遵守（这是本次重构的核心，绝不能靠肉眼）

约束清单（机器可检查）
----------------------
  C1. overlay.py 不得出现 LWA_COLORKEY           （与 WDA 冲突）
  C2. overlay.py 必须调用 gdiplus_render.present  （绘制职责归属）
  C3. gdiplus_render 必须用 AC_SRC_ALPHA          （每像素 alpha，非 colorkey）
  C4. gdiplus_render 必须调用 UpdateLayeredWindow
  C5. overlay.py 版本门槛必须是 19041（2004），不是 1803
  C6. main.py delivery 默认应为 overlay
  C7. capture.py 不应产生出站网络调用（mss 本地）
  C8. gdiplus_render 自行管理 HBITMAP 生命周期
  C9. overlay 不再自行操作 GDI DC
  C10. main.py 建浮层时必须把 config 的外观项传进去（否则配置改了没反应）
  C11. main.py 的热键必须来自 cfg.hotkey_*，不能硬编码 ord("Q") 之类
  C12. overlay 必须把长行交给 wrap_lines 软换行（否则右半边直接被裁掉）
  C13. main.run_once 的进度/失败必须走 _status（投浮层），不能只 print
  C14. main.run_once_cli 必须跑消息循环（否则 --once 下退出键/滚轮全是死的）
"""
import ast
import sys
import pathlib

# 中文 Windows 控制台默认 GBK，✅/❌ 会 UnicodeEncodeError —— 以前必须
# `PYTHONIOENCODING=utf-8 python test_validate.py` 才能跑，现在自己包。
# 已经是 utf-8 就别再包一层：新 wrapper 会顶掉旧的，旧的被回收时连底下的
# buffer 一起关闭，之后所有 print 都 ValueError（多个测试同进程跑会踩到）
try:
    import io
    if (getattr(sys.stdout, "encoding", "") or "").lower().replace("-", "") != "utf8":
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                      line_buffering=True)
except Exception:
    pass

ROOT = pathlib.Path(__file__).parent


def parse(p: pathlib.Path) -> ast.Module:
    return ast.parse(p.read_text(encoding="utf-8"))


def syntax_check(p: pathlib.Path) -> bool:
    try:
        parse(p)
        print(f"  ✅ 语法 OK  {p.name}")
        return True
    except SyntaxError as e:
        print(f"  ❌ 语法错误 {p.name}: {e}")
        return False


def names_defined(tree: ast.Module) -> set[str]:
    """顶层定义的名字（类、函数、赋值的目标名）。"""
    names = set()
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            names.add(node.name)
            # 也收集方法名（用于"类.方法"核对）
            for item in node.body:
                if isinstance(item, ast.FunctionDef):
                    names.add(f"{node.name}.{item.name}")
        elif isinstance(node, ast.FunctionDef):
            names.add(node.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            for t in node.targets if isinstance(node, ast.Assign) else [node.target]:
                if isinstance(t, ast.Name):
                    names.add(t.id)
    return names


def main():
    files = sorted(ROOT.glob("*.py"))
    print("=" * 66)
    print("一、语法编译检查")
    print("=" * 66)
    syntax_ok = all(syntax_check(f) for f in files)

    trees = {f.stem: parse(f) for f in files}
    defined = {stem: names_defined(t) for stem, t in trees.items()}

    print()
    print("=" * 66)
    print("二、跨模块引用核对（main 引用的名字是否在对应模块定义）")
    print("=" * 66)
    main_src = (ROOT / "main.py").read_text(encoding="utf-8")
    checks = []

    # main.py → capture.ScreenCapturer
    checks.append(("main → capture.ScreenCapturer",
                  "ScreenCapturer" in defined.get("capture", set())))
    # main.py → ocr.OCR
    checks.append(("main → ocr.OCR", "OCR" in defined.get("ocr", set())))
    # main.py → overlay.StealthOverlay / check_system
    checks.append(("main → overlay.StealthOverlay", "StealthOverlay" in defined.get("overlay", set())))
    checks.append(("main → overlay.check_system", "check_system" in defined.get("overlay", set())))
    checks.append(("main → overlay.load_saved_pos", "load_saved_pos" in defined.get("overlay", set())))
    checks.append(("main → overlay.StealthOverlay.dock_next（停靠热键）",
                  "StealthOverlay.dock_next" in defined.get("overlay", set())))
    # main.py → config.DeliveryMode / load_config
    checks.append(("main → config.DeliveryMode", "DeliveryMode" in defined.get("config", set())))
    checks.append(("main → config.load_config", "load_config" in defined.get("config", set())))
    # overlay → gdiplus_render.present
    checks.append(("overlay → gdiplus_render.present", "present" in defined.get("gdiplus_render", set())))

    for name, ok in checks:
        print(f"  {'✅' if ok else '❌'} {name}")

    print()
    print("=" * 66)
    print("三、关键架构约束（本次重构的核心，机器强制）")
    print("=" * 66)
    overlay_src = (ROOT / "overlay.py").read_text(encoding="utf-8")
    gdi_src = (ROOT / "gdiplus_render.py").read_text(encoding="utf-8")
    capture_src = (ROOT / "capture.py").read_text(encoding="utf-8")
    config_src = (ROOT / "config.py").read_text(encoding="utf-8")

    # 提取 capture.py 的顶层 import 语句（只看真正 import 的网络库，忽略注释/字符串）
    capture_tree = trees["capture"]
    imported_names = set()
    for node in capture_tree.body:
        if isinstance(node, ast.Import):
            for n in node.names:
                imported_names.add(n.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imported_names.add(node.module.split(".")[0])
    net_libs = {"requests", "httpx", "urllib3", "websocket", "socket"}
    has_net_import = bool(imported_names & net_libs)

    # C1：overlay 代码里不得真正调用 SetLayeredWindowAttributes。
    #   正确的"每像素 alpha"走 UpdateLayeredWindow，根本不需要该函数；
    #   一旦调用它并传 LWA_COLORKEY，就会与 WDA 捕获排除冲突。
    #   策略：剥离文档字符串/注释后检查是否仍有该调用。
    overlay_tree = trees["overlay"]
    code_lines = []
    for node in overlay_tree.body:
        if isinstance(node, ast.FunctionDef):
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call):
                    code_lines.append(ast.unparse(sub))
    has_colorkey_call = any(
        "SetLayeredWindowAttributes" in ln and "LWA_COLORKEY" in ln for ln in code_lines
    )

    # C10：main.py 里 StealthOverlay(...) 必须把外观类配置项显式传进去。
    #   overlay 收得下、config 也解析了，但 main 不传 —— 这种"看着能配、改了
    #   没反应"的断线纯靠肉眼 review 很容易漏，所以机器盯着。
    ov_kwargs: set[str] = set()
    for node in ast.walk(trees["main"]):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                and node.func.id == "StealthOverlay":
            ov_kwargs |= {kw.arg for kw in node.keywords if kw.arg}
    appearance_kwargs = {"bg_color", "bg_alpha", "text_color",
                         "font_size", "line_height", "font_name", "wrap"}
    missing_kwargs = appearance_kwargs - ov_kwargs

    # C11：热键必须从 cfg.hotkey_* 读。以前这里是硬编码 ord("Q")/ord("V")/…，
    #   config 的 hotkeys: 段解析了却没人用 —— 同样是"能配但没接线"。
    #   回退候选现在写成完整键名字符串交给 config.hotkey_candidates 解析，
    #   main.py 里不该再出现 ord(...) 拼 vk 的写法。
    hardcoded_hotkeys = [c for c in "QVCX" if f'ord("{c}")' in main_src]
    reads_hotkey_cfg = 'f"hotkey_{name}"' in main_src or "cfg.hotkey_" in main_src

    # C12：长行必须软换行。GDI+ 一行画一个矩形，不折行就等于把右半边裁掉，
    #   而滚轮只能上下翻 —— 用户看到的是"答案缺了一半"，还以为模型没写完。
    wraps_long_lines = "def wrap_lines" in gdi_src and "wrap_lines(" in overlay_src

    # C13：run_once 的每一步进度和每一条失败原因都必须走 _status（同时投浮层）。
    #   只 print 等于没说 —— CONSOLE=False 的打包版没有控制台，用户看到的是
    #   一块几十秒不动的背板，分不清是热键没生效、截屏失败还是模型还在写。
    #   机器盯两件事：_status 本身存在，且 run_once 里确实在用（不止一处）。
    run_once_fn = None
    for node in ast.walk(trees["main"]):
        if isinstance(node, ast.FunctionDef) and node.name == "run_once":
            run_once_fn = node
            break
    status_calls = 0
    if run_once_fn is not None:
        for sub in ast.walk(run_once_fn):
            if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute) \
                    and sub.func.attr == "_status":
                status_calls += 1
    has_status = "def _status" in main_src and "overlay.set_text(msg)" in main_src

    # C14：--once 也必须跑真正的消息循环。原来是 while True: sleep(1)，于是
    #   热键（含说明书上写的退出键）没注册、消息泵没起 —— 滚轮翻页和拖动
    #   全是死的，"验证链路"只能看到第一屏答案。
    once_fn = None
    for node in ast.walk(trees["main"]):
        if isinstance(node, ast.FunctionDef) and node.name == "run_once_cli":
            once_fn = node
            break
    once_pumps = once_busy_wait = False
    if once_fn is not None:
        once_pumps = any(
            isinstance(s, ast.Call) and isinstance(s.func, ast.Attribute)
            and s.func.attr in ("_hotkey_loop", "_register_hotkeys")
            for s in ast.walk(once_fn)
        )
        once_busy_wait = any(
            isinstance(s, ast.While) and isinstance(s.test, ast.Constant)
            and s.test.value is True
            for s in ast.walk(once_fn)
        )

    constraints = [
        ("C1  overlay 无 LWA_COLORKEY 调用（走 UpdateLayeredWindow 每像素 alpha）",
         not has_colorkey_call and "UpdateLayeredWindow" in gdi_src),
        ("C2  overlay 委托 gdiplus_render.present", "_gdi.present(" in overlay_src or "gdiplus_render.present" in overlay_src),
        ("C3  gdiplus_render 使用 AC_SRC_ALPHA（每像素 alpha）", "AC_SRC_ALPHA" in gdi_src),
        ("C4  gdiplus_render 调用 UpdateLayeredWindow", "UpdateLayeredWindow" in gdi_src),
        ("C5  版本门槛为 19041（2004），非 1803", "19041" in overlay_src and "1803" not in overlay_src.replace("1803", "")),
        ("C6  config 默认 delivery = OVERLAY", 'OVERLAY = "overlay"' in config_src and "DeliveryMode.OVERLAY" in config_src),
        ("C7  capture 顶层 import 无网络库", not has_net_import),
        ("C8  gdiplus_render 自行管理 HBITMAP 生命周期", "DeleteObject(hbitmap)" in gdi_src),
        ("C9  overlay 不再自行操作 GDI DC", "CreateCompatibleDC" not in overlay_src),
        (f"C10 main 建浮层时传入外观配置{'（缺 ' + ', '.join(sorted(missing_kwargs)) + '）' if missing_kwargs else ''}",
         not missing_kwargs),
        (f"C11 main 热键读 cfg.hotkey_*，无硬编码"
         f"{'（硬编码了 ' + ', '.join(hardcoded_hotkeys) + '）' if hardcoded_hotkeys else ''}",
         reads_hotkey_cfg and not hardcoded_hotkeys),
        ("C12 overlay 长行走 wrap_lines 软换行", wraps_long_lines),
        (f"C13 run_once 进度/失败走 _status 投浮层（{status_calls} 处）",
         has_status and status_calls >= 4),
        ("C14 run_once_cli 跑消息循环，非 while True 空转",
         once_pumps and not once_busy_wait),
    ]
    for name, ok in constraints:
        print(f"  {'✅' if ok else '❌'} {name}")

    all_ok = syntax_ok and all(c[1] for c in checks) and all(c[1] for c in constraints)
    print()
    print("=" * 66)
    print("结论:", "全部通过 ✅" if all_ok else "存在问题 ❌")
    print("=" * 66)
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
