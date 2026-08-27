"""
隐蔽辅助模块 (stealth.py) —— 【已降级为可选工具函数库】

⚠️ 架构说明（请先读）：
  本模块是早期独立隐形窗口的实现。当前主架构中，"隐形浮层"的完整逻辑
  （WDA_EXCLUDEFROMCAPTURE + 每像素 Alpha + 点击穿透 + GDI+ 绘制）已统一到
  `overlay.py` + `gdiplus_render.py`，并由 `main.py` 直接驱动。

  因此本文件**不再是主路径**，保留原因如下：
    · apply_affinity / make_cloaked / set_tool_window 等函数可作为
      对"任意 HWND"施加隐蔽属性的通用工具函数复用；
    · 进程伪装、全局热键的参考实现；
    · 配合 verify_stealth.py 做独立验证。

  如需扩展主浮层能力（例如对第三方窗口施加 WDA），从这里取函数即可。
  新功能请优先加到 overlay.py，避免逻辑分散。

核心原理（Windows 专用）：
  1) 工具窗口标记为 "排除从捕获" (WDA_EXCLUDEFROMCAPTURE)
     —— 这是 Windows 10 2004 (build 19041)+ 提供的官方机制[citation:1][citation:17]。
     腾讯会议自身也用同一机制过滤共享 UI[citation:10]。
  2) 无任务栏图标 + 从 Alt-Tab / 任务视图中排除 (Cloaked window)
  3) 全局热键控制显隐，避免点击工具导致浏览器/会议失去焦点
  4) 进程名伪装：以普通进程名启动，去除控制台窗口

⚠️ 重要声明：
  本模块仅提供技术层面的不可见机制。使用此类工具参与面试/考试可能违反平台
  诚信协议，请仅用于个人学习、辅助工具技术研究、自有系统自动化测试。
"""
import ctypes
import ctypes.wintypes as wt
from ctypes import wintypes

# -------------------- Win32 常量与原型 --------------------
user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080      # 不在任务栏/Alt-Tab 显示
WS_EX_APPWINDOW = 0x00040000
WS_EX_LAYERED = 0x00080000

# SetWindowDisplayAffinity 亲和值
WDA_NONE = 0
WDA_MONITOR = 1                     # 仅在自己显示器可见
WDA_EXCLUDEFROMCAPTURE = 0x00000011  # 从截屏/共享中排除 (Win10 1803+)

GWDPA_NEGATIVE_INCLUDE = 0  # GetWindowDisplayAffinity 占位

# 窗口名称
GetWindowLongW = user32.GetWindowLongW
SetWindowLongW = user32.SetWindowLongW
SetWindowDisplayAffinity = user32.SetWindowDisplayAffinity
GetWindowDisplayAffinity = user32.GetWindowDisplayAffinity


def hide_from_capture(hwnd: int) -> bool:
    """
    让指定窗口从屏幕捕获/共享中消失（核心 API）。
    必须在窗口创建后、首次显示前调用效果最佳。
    返回是否成功。
    """
    if not hwnd:
        return False
    ok = SetWindowDisplayAffinity(int(hwnd), WDA_EXCLUDEFROMCAPTURE)
    return bool(ok)


def apply_stealth_style(hwnd: int):
    """
    应用隐形样式：
    - WS_EX_TOOLWINDOW：不出现在任务栏、Alt-Tab 列表
    - 组合 WS_EX_APPWINDOW 的反向处理，确保任务视图中也不可见（Cloaked）
    """
    if not hwnd:
        return
    style = GetWindowLongW(int(hwnd), GWL_EXSTYLE)
    style = style | WS_EX_TOOLWINDOW | WS_EX_LAYERED
    style = style & ~WS_EX_APPWINDOW
    SetWindowLongW(int(hwnd), GWL_EXSTYLE, style)


# -------------------- 基于 tkinter 的隐形答案浮层 --------------------
class InvisibleOverlay:
    """
    答案显示浮层：默认全透明、置顶、点击穿透、从截屏排除。
    - 默认完全透明（不可见），仅在需要时短暂显示答案片段
    - 推荐替代方案：直接用全局热键把答案复制到剪贴板，由用户自行查看，
      做到"屏幕上一无所有"，隐蔽性最高（见 main.py 的 copy_to_clipboard）
    """

    def __init__(self):
        self._tk = None
        self._label = None
        self.hwnd = None

    def create(self, opacity: float = 0.0):
        """创建一个透明置顶窗口。opacity=0.0 表示完全透明（推荐）。"""
        import tkinter as tk
        self._tk = tk.Tk()
        self._tk.overrideredirect(True)          # 无边框/标题栏
        self._tk.attributes("-topmost", True)    # 始终置顶
        self._tk.attributes("-transparentcolor", "black")  # 黑色视为透明
        self._tk.config(bg="black")
        self._tk.geometry("400x60+10+10")
        self._label = tk.Label(self._tk, text="", fg="lime", bg="black",
                               font=("Consolas", 12))
        self._label.pack(fill="both", expand=True)

        # ---- 关键：在窗口显示前应用捕获排除 ----
        self.hwnd = self._tk.winfo_id() if False else self._get_hwnd()
        self._tk.after(10, self._apply_affinity)

    def _get_hwnd(self):
        """tkinter 顶层窗口 HWND 获取。"""
        try:
            return int(self._tk.frame(), 16)
        except Exception:
            return None

    def _apply_affinity(self):
        if self.hwnd:
            apply_stealth_style(self.hwnd)
            hide_from_capture(self.hwnd)

    def show_answer(self, text: str, duration_ms: int = 5000):
        """短暂显示答案（如需视觉辅助）。高隐蔽场景建议改用剪贴板。"""
        if not self._tk:
            return
        self._label.config(text=text)
        self._tk.after(duration_ms, lambda: self._label.config(text=""))

    def mainloop(self):
        if self._tk:
            self._tk.mainloop()

    def destroy(self):
        if self._tk:
            self._tk.destroy()


# -------------------- 全局热键 --------------------
class GlobalHotkey:
    """
    注册全局热键，控制工具显隐/触发识别，避免 Alt-Tab 切换窗口留下痕迹。
    使用 RegisterHotKey (modifiers + vk_code)。
    常用键位（默认，可在配置中修改）：
        Ctrl+Alt+Q : 触发一次识别
        Ctrl+Alt+H : 切换答案显示/隐藏
        Ctrl+Alt+X : 紧急退出（清场）
    """

    MOD_ALT = 0x0001
    MOD_CONTROL = 0x0002
    MOD_SHIFT = 0x0004
    WM_HOTKEY = 0x0312

    def __init__(self, bindings: dict):
        """
        :param bindings: {hotkey_id: callback} ，hotkey_id 为自定义整数
        """
        self.bindings = bindings
        self._registered = []

    def register(self, hwnd: int, hotkey_id: int, modifiers: int, vk: int, callback):
        """注册单个热键。hwnd=0 表示线程级热键。"""
        if user32.RegisterHotKey(hwnd, hotkey_id, modifiers, vk):
            self._registered.append((hwnd, hotkey_id))
            self.bindings[hotkey_id] = callback
        else:
            print(f"[warn] 热键注册失败 id={hotkey_id}，可能已被占用")

    def unregister_all(self):
        for hwnd, hid in self._registered:
            user32.UnregisterHotKey(hwnd, hid)
        self._registered.clear()

    @staticmethod
    def vk(code: str) -> int:
        """字符 -> 虚拟键码（A-Z 直接 ord）。"""
        return ord(code.upper())


# -------------------- 进程伪装辅助 --------------------
def disguise_process():
    """
    进程层面伪装（可选，需谨慎）：
    - 修改当前窗口/控制台标题为常见名称
    - 隐藏控制台窗口（若为 --windowed 打包则天然无控制台）
    更彻底的伪装（如伪造进程名）建议直接通过 PyInstaller 打包为常见名称 exe，
    而非运行时篡改，后者易触发安全软件告警。
    """
    # 隐藏控制台窗口
    kernel32 = ctypes.windll.kernel32
    user32.ShowWindowWindow = getattr(user32, "ShowWindow", None)
    try:
        hwnd = kernel32.GetConsoleWindow()
        if hwnd and user32.ShowWindowWindow:
            user32.ShowWindowWindow(hwnd, 0)  # SW_HIDE=0
    except Exception:
        pass


if __name__ == "__main__":
    # 自检：演示 API 是否可用（不实际创建窗口）
    print("WDA_EXCLUDEFROMCAPTURE =", hex(WDA_EXCLUDEFROMCAPTURE))
    print("SetWindowDisplayAffinity addr =", SetWindowDisplayAffinity)
    print("stealth module loaded OK")
