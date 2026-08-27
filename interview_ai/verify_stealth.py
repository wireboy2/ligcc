"""
验证脚本：隐形浮层的两大核心技术前提
运行环境：Windows 10 1803+ / Windows 11，需管理员权限（部分 API）

验证项：
  1. SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE=0x11) 是否真的让窗口
     从桌面复制（Desktop Duplication / DXGI）管线中消失
     —— 这是腾讯会议、Zoom、Teams、OBS 共享屏幕的底层机制
  2. Layered Window (WS_EX_LAYERED) + 透明色 = 点击穿透到下层窗口
     —— 浮层不拦截鼠标，你照常操作 IDE

用法：
  python verify_stealth.py            # 全量验证 + 弹出测试浮层
  python verify_stealth.py --no-gui   # 仅检查 API 可用性与系统版本
"""
import sys
import ctypes
import platform
import argparse
from ctypes import wintypes

# ---------------------------------------------------------------------------
# 1) Win32 绑定
# ---------------------------------------------------------------------------
user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

# 常量
GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_TOPMOST = 0x00000008
WS_POPUP = 0x80000000
LWA_COLORKEY = 0x00000001
LWA_ALPHA = 0x00000002

WDA_NONE = 0x00000000
WDA_MONITOR = 0x00000001
WDA_EXCLUDEFROMCAPTURE = 0x00000011  # Windows 10 1803 (17134) 起可用

# 函数原型
user32.SetWindowDisplayAffinity.argtypes = [wintypes.HWND, wintypes.DWORD]
user32.SetWindowDisplayAffinity.restype = wintypes.BOOL
user32.SetLayeredWindowAttributes.argtypes = [
    wintypes.HWND, wintypes.COLORREF, wintypes.BYTE, wintypes.DWORD
]
user32.SetLayeredWindowAttributes.restype = wintypes.BOOL
user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
user32.SetWindowLongW.restype = ctypes.c_long
user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetWindowLongW.restype = ctypes.c_long
user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
user32.RegisterHotKey.restype = wintypes.BOOL


def check_system_support():
    """检查操作系统版本是否支持 WDA_EXCLUDEFROMCAPTURE。"""
    print("=" * 60)
    print("[系统环境]")
    print(f"  OS: {platform.system()} {platform.release()} ({platform.version()})")
    print(f"  Arch: {platform.machine()}")
    print(f"  Python: {sys.version.split()[0]}")

    ver = sys.getwindowsversion()
    build = ver.build
    # 1803 = build 17134
    supported = build >= 17134
    print(f"  Windows build: {build}")
    print(f"  WDA_EXCLUDEFROMCAPTURE 支持: {'✅ 是 (>=17134)' if supported else '❌ 否，需要 Windows 10 1803+'}")

    # 实际查询 DLL 中符号是否存在（比版本号更可靠）
    try:
        func = getattr(user32, "SetWindowDisplayAffinity")
        print(f"  user32.SetWindowDisplayAffinity 导出: ✅ 存在")
    except AttributeError:
        print(f"  user32.SetWindowDisplayAffinity 导出: ❌ 不存在")
        supported = False

    print("=" * 60)
    return supported


# ---------------------------------------------------------------------------
# 2) 创建一个最小测试浮层，验证两个特性
# ---------------------------------------------------------------------------
WINDOW_CLASS = "StealthOverlayTestCls"
TRANSPARENT_COLOR = 0x00010101  # 一个几乎不会用到的深紫色作为透明色键


def make_window():
    """创建一个 WS_EX_LAYERED + 透明色键的弹出窗口，返回 hwnd。"""
    hInstance = kernel32.GetModuleHandleW(None)

    # 注册窗口类
    class_name = ctypes.create_unicode_buffer(WINDOW_CLASS)
    wndclass = wintypes.WNDCLASSEX()
    wndclass.cbSize = ctypes.sizeof(wintypes.WNDCLASSEX)
    wndclass.lpfnWndProc = ctypes.WINFUNCTYPE(
        wintypes.LPARAM, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
    )(lambda hwnd, msg, wp, lp: user32.DefWindowProcW(hwnd, msg, wp, lp))
    wndclass.hInstance = hInstance
    wndclass.lpszClassName = class_name
    user32.RegisterClassExW(ctypes.byref(wndclass))

    # 创建窗口：WS_POPUP，扩展样式在 create 时即设 WS_EX_LAYERED（不可事后加）
    hwnd = user32.CreateWindowExW(
        WS_EX_LAYERED | WS_EX_TOOLWINDOW | WS_EX_TOPMOST,
        class_name,
        "StealthTest",
        WS_POPUP,
        100, 100, 480, 300,
        None, None, hInstance, None
    )
    if not hwnd:
        raise RuntimeError("CreateWindowExW 失败，错误码: %d" % kernel32.GetLastError())
    return hwnd


def apply_stealth(hwnd):
    """对窗口施加三项隐蔽属性，返回每项的成功/失败。"""
    results = {}

    # (a) 设为层叠窗口的透明色键 —— 该色及其下方像素完全穿透点击
    ok_alpha = user32.SetLayeredWindowAttributes(hwnd, TRANSPARENT_COLOR, 255, LWA_COLORKEY)
    results["LWA_COLORKEY(点击穿透)"] = bool(ok_alpha)

    # (b) 排除出屏幕捕获管线 —— 这是对抗腾讯会议的核心
    ok_aff = user32.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE)
    results["WDA_EXCLUDEFROMCAPTURE(屏幕共享不可见)"] = bool(ok_aff)

    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-gui", action="store_true", help="只检查 API 可用性，不弹窗")
    args = ap.parse_args()

    supported = check_system_support()

    if args.no_gui:
        print("\n[--no-gui] 跳过创建测试浮层。")
        return 0 if supported else 1

    if not supported:
        print("\n⚠ 当前系统不支持 WDA_EXCLUDEFROMCAPTURE，无法验证，退出。")
        return 1

    print("\n[创建测试浮层]")
    hwnd = make_window()
    print(f"  HWND = {hwnd}")

    # 先验证"未设 affinity"时窗口正常显示
    user32.ShowWindow(hwnd, 5)  # SW_SHOW
    user32.UpdateWindow(hwnd)

    results = apply_stealth(hwnd)
    print("\n[隐蔽属性施加结果]")
    for k, v in results.items():
        print(f"  {k}: {'✅ 成功' if v else '❌ 失败'}")

    print("\n" + "=" * 60)
    print("请自行验证：")
    print("  1) 打开 腾讯会议/Zoom/OBS → 开始屏幕共享/录屏")
    print("  2) 观察共享画面中是否还能看到这个测试窗口")
    print("  3) 若看不到 → WDA_EXCLUDEFROMCAPTURE 生效 ✅")
    print("  4) 用鼠标点击窗口的透明区域，应能穿透到下方程序")
    print("  按 Ctrl+Alt+X 退出验证")
    print("=" * 60)

    # 注册一个退出热键，避免靠点击关闭按钮
    user32.RegisterHotKey(None, 1, 0x2 | 0x1, 0x58)  # MOD_ALT|MOD_CTRL, 'X'

    # 消息循环（简化：仅等待热键）
    msg = wintypes.MSG()
    while True:
        if user32.GetMessageW(ctypes.byref(msg), None, 0, 0) <= 0:
            break
        if msg.message == 0x0312:  # WM_HOTKEY
            print("收到退出热键，销毁窗口。")
            break
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))

    user32.DestroyWindow(hwnd)
    return 0


if __name__ == "__main__":
    sys.exit(main())
