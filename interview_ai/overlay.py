"""
隐形浮层（答案的主交付方式）
=================================================================

【为什么不是剪贴板？】
代码题不可能一次性粘贴——你要"照着抄"。所以答案渲染到一个始终置顶、
半透明的浮层上，你一边看一边敲。剪贴板模式降级为可选。

【核心机制：为什么共享屏幕里看不到，但你本机看得见】
------------------------------------------------------------------
这是整套方案里最关键的一环。原理来自微软官方文档与实测：

  · SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE)
      → DWM（桌面窗口管理器）在合成时，把这个窗口送向**物理显示器**，
        但在交给**捕获管线**（WGC / DXGI Desktop Duplication）的帧里把它剔除。
      → 因此：你的显示器正常显示；腾讯会议/Zoom/Teams/OBS/录屏抓到的是
        一个"透明空洞"，看不到浮层内容。
      → 腾讯会议自身的共享窗口就是靠同一机制过滤掉边框和绿框的（接收端透明）。

  · WS_EX_LAYERED + UpdateLayeredWindow（每像素 Alpha，Per-Pixel Alpha）
      → 背景 alpha = 0 → 真透明（桌面透过来，且鼠标可穿透到下层 IDE）
      → 文字像素 alpha = 255 → 完全不透明、清晰可读
      → 不设置 LWA_COLORKEY，避免与 WDA 冲突（见下方"踩坑"）

  · WS_EX_TRANSPARENT
      → 窗口整体不参与鼠标命中测试，点击直接穿透到下层窗口

【⚠️ 关键踩坑：LWA_COLORKEY 与 WDA 不能混用】
------------------------------------------------------------------
实测结论（CSDN 多方验证 + 微软文档）：
  若在 WS_EX_LAYERED 窗口上同时调用 SetLayeredWindowAttributes(LWA_COLORKEY)，
  再设 WDA_EXCLUDEFROMCAPTURE，DWM 合成会冲突 —— 颜色键区域可能变成**纯黑**
  而非透明，且捕获排除行为不稳定。
权威建议：**"防止捕获"和"华丽半透明渐变"不要同时用同一套技术实现**；
若必须两者都要，则用 Per-Pixel Alpha（UpdateLayeredWindow）代替 COLORKEY。

本实现因此选择 UpdateLayeredWindow + 每像素 alpha，弃用 LWA_COLORKEY。

【移动浮层：为什么不能用普通拖动，怎么解决】
------------------------------------------------------------------
窗口带 WS_EX_TRANSPARENT（不参与命中测试），系统根本不会把鼠标消息投给它，
所以「按住标题栏拖」这条常规路径不存在 —— 想拿掉 TRANSPARENT 换取可拖动，
代价是浮层挡住的那块区域再也点不到下层 IDE，得不偿失。
本实现沿用滚轮翻页那套办法：WH_MOUSE_LL 全局钩子 + 自己做命中测试。
  · 浮层内 **Ctrl+左键拖动** 或 **中键拖动** → 移动浮层（事件被钩子吃掉，
    不会漏给下层浏览器/IDE）；不按 Ctrl 的普通左键照旧穿透。
  · 拖动过程只调 SetWindowPos（DWM 保留已提交的像素，不重走 GDI+ 绘制）。
  · 位置自动夹在虚拟桌面内（至少留 MIN_VISIBLE 像素可见），拖不丢；
    remember_pos=True 时落盘 history/overlay_state.json（位置和你调过的字号/
    透明度一起记），下次启动沿用。

【长行：软换行而不是裁切】
------------------------------------------------------------------
GDI+ 是「一行一个矩形」画出来的，超出矩形的部分直接没了 —— 长代码行右半边
会凭空消失，而滚轮只能上下翻，看不到被裁的部分。所以 set_text 收到的原始行
先经 gdiplus_render.wrap_lines 按**实际字符宽度**折成显示行（中英文混排也准），
续行沿用原缩进，代码折了还对得齐。wrap=False 可以退回旧的裁切行为。

【系统要求】
  · Windows 10 2004 (build 19041) 或更高
    ⚠️ 注意：WDA_EXCLUDEFROMCAPTURE 是 2004 引入的，不是 1803。
       1803 引入的是 Windows.Graphics.Capture API 本身。
       在 <19041 的系统上调用会失败（GetLastError=ERROR_INVALID_PARAMETER），
       本类会 fallback 到 WDA_MONITOR（窗口在捕获中变纯黑 —— 仍能藏内容，但会留黑块）。
  · DWM 正在合成桌面（默认即满足；禁用 DWM 时 WDA 整体不生效）

【安全边界（务必阅读）】
  · 屏幕采集层（WGC / DXGI Desktop Duplication）已被广泛验证可被此机制排除；
  · 但**任何"你本机屏幕上可见"的元素都无法对抗：
      - AI 监考（视线追踪、行为建模、第二机位摄像头）
      - 内核级反作弊 / DRM
      - 硬件采集卡、手机拍照（"模拟漏洞"）
    → 这些是应用层无法解决的，本工具不承诺 100% 不可检测。
  · 请仅用于合法场景：个人学习、自有系统自动化测试、辅助技术研究。

依赖：pywin32 (win32gui, win32con, win32api)
仅 Windows 可用。
"""
import sys
import os
import json
import time
import ctypes
import ctypes.wintypes as wt
import threading

try:
    import win32gui
    import win32con
    import win32api
except ImportError:  # pragma: no cover - Windows-only
    win32gui = win32con = win32api = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
WDA_NONE = 0x00000000
WDA_MONITOR = 0x00000001
WDA_EXCLUDEFROMCAPTURE = 0x00000011  # Windows 10 2004 (build 19041)+
MIN_BUILD_FOR_EXCLUDE = 19041        # 低于此版本只能 fallback 到 WDA_MONITOR

# 低级鼠标钩子相关的消息 / 虚拟键码（直接写常量，不依赖 win32con）
WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_MBUTTONDOWN = 0x0207
WM_MBUTTONUP = 0x0208
WM_MOUSEWHEEL = 0x020A
VK_LBUTTON, VK_MBUTTON, VK_CONTROL = 0x01, 0x04, 0x11

# 拖动边界：窗口与某块显示器至少要有这么多像素的重叠，防止"拖丢"
MIN_VISIBLE = 80
MONITOR_DEFAULTTONEAREST = 2

# 记住上次的浮层状态（位置 / 字号 / 透明度）：exe 同目录 / 项目根目录下 history/
if getattr(sys, "frozen", False):
    _ROOT = os.path.dirname(sys.executable)
else:
    _ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_FILE = os.path.join(_ROOT, "history", "overlay_state.json")
# 老版本只存位置，文件名也不一样；读得到就顺带迁移过来（写只写新文件）
LEGACY_POS_FILE = os.path.join(_ROOT, "history", "overlay_pos.json")


def load_state() -> dict:
    """读取记住的浮层状态；文件不存在/损坏返回 {}（调用方全用默认值）。"""
    for path in (STATE_FILE, LEGACY_POS_FILE):
        try:
            with open(path, "r", encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, ValueError):
            continue
        if isinstance(d, dict):
            return d
    return {}


def save_state(**fields) -> bool:
    """把给到的字段并进 overlay_state.json（没给的字段保持原样）。

    合并而不是整体覆盖：位置是拖动时高频写的，字号/透明度是热键偶尔改的，
    两边各写各的字段，不能互相清掉。写失败只返回 False，不影响使用。
    """
    data = load_state()
    data.update({k: v for k, v in fields.items() if v is not None})
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        return True
    except OSError:
        return False


def load_saved_pos() -> tuple[int, int] | None:
    """上次记住的浮层位置；没记过/损坏返回 None（调用方退回默认位置）。"""
    d = load_state()
    try:
        return int(d["x"]), int(d["y"])
    except (KeyError, TypeError, ValueError):
        return None


def save_pos(x: int, y: int) -> bool:
    """记住浮层位置（下次启动沿用）。"""
    return save_state(x=int(x), y=int(y))


def load_saved_size() -> tuple[int, int] | None:
    """上次记住的浮层尺寸；没记过/损坏返回 None（调用方退回配置里的尺寸）。"""
    d = load_state()
    try:
        return int(d["w"]), int(d["h"])
    except (KeyError, TypeError, ValueError):
        return None


# gdiplus_render.present 负责"绘制 + UpdateLayeredWindow 提交"全流程，
# 并统一管理 HBITMAP 生命周期，本模块不再直接操作 DC / BLENDFUNCTION。
import gdiplus_render as _gdi

gdi32 = ctypes.windll.gdi32
user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

user32.SetWindowDisplayAffinity.argtypes = [wt.HWND, wt.DWORD]
user32.SetWindowDisplayAffinity.restype = wt.BOOL


# ---------------------------------------------------------------------------
# 主类
# ---------------------------------------------------------------------------
class StealthOverlay:
    """
    半透明文字浮层，对屏幕共享/录屏默认不可见。

    典型用法
    --------
        ov = StealthOverlay(title="System Update")   # 伪装标题
        ov.show()
        ov.set_text("def solve():\\n    return 42")
        ov.install_mouse_hook()   # 滚轮翻页 + Ctrl/中键拖动移动（需消息循环）
        # ... 用热键控制显隐 ...
        ov.destroy()
    """

    # 样式：分层 + 工具窗口(不进 Alt-Tab) + 置顶 + 鼠标穿透
    EX_STYLE = (
        win32con.WS_EX_LAYERED
        | win32con.WS_EX_TOOLWINDOW
        | win32con.WS_EX_TOPMOST
        | win32con.WS_EX_TRANSPARENT        # 点击穿透（不参与命中测试）
        | win32con.WS_EX_NOACTIVATE         # 不抢焦点
    )
    STYLE = win32con.WS_POPUP               # 无边框

    DEFAULT_W, DEFAULT_H = 820, 900
    PAD_X, PAD_Y = 18, 14
    LINE_HEIGHT = 26
    FONT_SIZE = 18
    FONT_NAME = "Consolas"
    DOCK_MARGIN = 40                        # 停靠到屏幕角落时留的边距
    # 行高 / 字号 的合理区间：字号太小看不清，太大一屏放不下几行；
    # 行高必须 > 字号，否则相邻两行会咬在一起
    MIN_FONT_SIZE, MAX_FONT_SIZE = 8, 96
    LINE_SPACING = 1.45                     # 只给字号时按这个比例推行高（26/18）
    # 尺寸下限：再小就放不下「一行正文 + 一行页码」，等于白留一块背板
    MIN_W, MIN_H = 240, 120

    def __init__(
        self,
        title: str = "Windows Defender SmartScreen",  # 任务栏伪装标题
        width: int = DEFAULT_W,
        height: int = DEFAULT_H,
        x: int | None = None,
        y: int | None = None,
        bg_color: tuple[int, int, int] = (20, 22, 30),   # 背板 RGB
        bg_alpha: int = 165,                              # 背板不透明度 0-255
        text_color: tuple[int, int, int] = (235, 235, 235),
        remember_pos: bool = False,                       # 拖动后记住位置
        font_size: int = FONT_SIZE,
        line_height: int | None = None,   # None=按字号 × LINE_SPACING 自动推算
        font_name: str = FONT_NAME,
        wrap: bool = True,                # 长行按浮层宽度软换行（关掉=超宽即裁切）
        pad_x: int = PAD_X,
        pad_y: int = PAD_Y,
    ):
        self.title = title
        # 尺寸下限在这里就夹一次：config 里写了 size: [10, 10] 也不该造出
        # 一块什么都放不下的背板（上限要知道在哪块屏上，留给 resize 处理）
        self.width, self.height = max(self.MIN_W, int(width)), max(self.MIN_H, int(height))
        self.bg_color = bg_color
        self.bg_alpha = max(0, min(255, bg_alpha))
        self.text_color = text_color
        self.remember_pos = remember_pos
        self.font_name = font_name or self.FONT_NAME
        self.wrap = bool(wrap)
        self.pad_x, self.pad_y = max(0, pad_x), max(0, pad_y)
        # _raw_lines 是 set_text 收到的原始行，_lines 是折行后真正画出来的行；
        # 改字号/字体/宽度都要拿原始行重折一遍，所以两份都得留。
        self._lock = threading.Lock()
        self._raw_lines: list[str] = []
        self._lines: list[str] = []
        # 用户显式指定的行高（None=跟着字号走），改字号时才知道要不要重算
        self._line_height_fixed = int(line_height) if line_height else None
        self.set_font(size=font_size, render=False)

        # 默认放右上角（避开摄像头常见位置）
        if x is None or y is None:
            sx = win32api.GetSystemMetrics(win32con.SM_CXSCREEN)
            x = sx - self.width - 40
            y = 40
        # 夹一次：记住的位置可能来自已拔掉的副屏，别让浮层开在桌面外
        self.x, self.y = self._clamp(x, y)

        self._hwnd = None
        self._visible = False
        self._affinity_mode = WDA_NONE
        self._class_name = "StealthOverlayCls_" + str(id(self))
        self._scroll_offset = 0       # 滚动偏移（行数）
        self._mouse_hook = None       # WH_MOUSE_LL 钩子句柄
        self._mouse_proc = None       # 钩子回调引用（防 GC）
        self._drag = None             # 拖动中：(光标到窗口左上角的 dx, dy, 起始按键消息)
        self._dock_idx = -1           # dock_next 的循环下标
        self._last_wheel = {"t": 0.0, "delta": 0}   # 滚轮去抖（见 _handle_wheel）
        self._last_pos_save = 0.0     # 位置落盘限流
        self._pos_dirty = False

        self._create_window()
        self._apply_affinity()

    # ================================================================== 创建
    def _wnd_proc(self, hwnd, msg, wparam, lparam):
        # 我们不需要处理绘制消息：所有绘制都通过 UpdateLayeredWindow 主动推送。
        # WM_NCHITTEST 交由 WS_EX_TRANSPARENT 处理（返回 HTTRANSPARENT）。
        return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)

    def _create_window(self):
        wc = win32gui.WNDCLASS()
        wc.hInstance = win32api.GetModuleHandle(None)
        wc.lpszClassName = self._class_name
        wc.lpfnWndProc = self._wnd_proc
        wc.hCursor = win32gui.LoadCursor(0, win32con.IDC_ARROW)
        wc.style = win32con.CS_HREDRAW | win32con.CS_VREDRAW
        win32gui.RegisterClass(wc)

        self._hwnd = win32gui.CreateWindowEx(
            self.EX_STYLE, self._class_name, self.title, self.STYLE,
            self.x, self.y, self.width, self.height,
            0, 0, wc.hInstance, None,
        )
        if not self._hwnd:
            raise RuntimeError("创建浮层窗口失败 (CreateWindowEx 返回 0)")

    # ========================================================== 捕获排除（核心）
    def _apply_affinity(self):
        """
        设置显示亲和性。优先 WDA_EXCLUDEFROMCAPTURE，失败则 fallback WDA_MONITOR。

        调用时机很重要：必须在窗口创建之后、首次大量绘制之前设置。
        对**已有**的 WS_EX_LAYERED 窗口，WDA 仍可事后设置（实测有效），
        但设置后**不要再调用 SetLayeredWindowAttributes(LWA_COLORKEY)**。
        """
        if not self._hwnd:
            return

        # 优先尝试 EXCLUDEFROMCAPTURE
        ok = user32.SetWindowDisplayAffinity(
            self._hwnd, WDA_EXCLUDEFROMCAPTURE
        )
        if ok:
            self._affinity_mode = WDA_EXCLUDEFROMCAPTURE
            return

        # 失败 → 可能是 <19041 系统，或 DWM 未合成。退回 WDA_MONITOR：
        # 内容仍只对物理显示器可见，捕获侧会显示纯黑块（内容不泄露，但会留痕迹）。
        err = kernel32.GetLastError()
        ok2 = user32.SetWindowDisplayAffinity(self._hwnd, WDA_MONITOR)
        if ok2:
            self._affinity_mode = WDA_MONITOR
            print(
                f"[overlay] WDA_EXCLUDEFROMCAPTURE 失败(GetLastError={err})，"
                f"已 fallback 到 WDA_MONITOR（捕获侧会留纯黑块，仍不泄露内容）。"
                f"建议升级到 Windows 10 2004(build {MIN_BUILD_FOR_EXCLUDE}) 以上。"
            )
        else:
            print(
                f"[overlay] SetWindowDisplayAffinity 完全失败 "
                f"(err={err})。浮层内容可能被捕获，请勿在敏感场景使用。"
            )

    @property
    def affinity_mode(self) -> str:
        return {
            WDA_EXCLUDEFROMCAPTURE: "WDA_EXCLUDEFROMCAPTURE (完全排除，无痕迹)",
            WDA_MONITOR: "WDA_MONITOR (捕获侧留纯黑块)",
            WDA_NONE: "未启用 (内容可能被捕获!)",
        }.get(self._affinity_mode, "未知")

    # ========================================================== 绘制（每像素 Alpha）
    def _render(self):
        """
        把当前 _lines 的「滚动窗口」绘制成一帧 ARGB，通过 gdiplus_render.present
        提交给分层窗口。内容超可视区时切当前页，末行显示进度提示。
        """
        if not self._hwnd:
            return
        with self._lock:
            lines = list(self._lines)

        visible = self.visible_lines()
        if len(lines) > visible:
            # 留最后一行做进度条
            body = lines[self._scroll_offset : self._scroll_offset + visible - 1]
            last = self._scroll_offset + len(body)
            body.append(
                f"┈ 第 {self._scroll_offset + 1}-{last} / {len(lines)} 行 · "
                f"滚轮翻页 · Ctrl+拖动 移动 ┈"
            )
        else:
            body = lines

        try:
            ok = _gdi.present(
                self._hwnd, self.width, self.height, body,
                self.bg_color, self.bg_alpha, self.text_color,
                self.x, self.y,
                self.pad_x, self.pad_y, self.line_height,
                self.font_size, self.font_name,
            )
        except Exception as e:
            print(f"[overlay] 渲染异常: {e}")
            return
        if not ok:
            print(f"[overlay] UpdateLayeredWindow 提交失败 err={kernel32.GetLastError()}")

    # ========================================================== 对外 API
    def set_font(self, size: int | None = None, line_height: int | None = None,
                 name: str | None = None, render: bool = True):
        """改字号 / 行高 / 字体，并按需重绘。

        line_height 省略时：用户显式配过就沿用，否则按字号 × LINE_SPACING 推算，
        这样只调字号也不会把行挤在一起。行高至少比字号大 2px —— GDI+ 是把每行
        画在自己的矩形里的，行高不够两行就会咬在一起（见 gdiplus_render.present）。

        字号/字体一变，一行能放多少字也变了，所以要拿原始行重折一遍。
        """
        if size is not None:
            self.font_size = max(self.MIN_FONT_SIZE, min(self.MAX_FONT_SIZE, int(size)))
        if name:
            self.font_name = name
        if line_height:
            self._line_height_fixed = int(line_height)
        lh = self._line_height_fixed or round(self.font_size * self.LINE_SPACING)
        self.line_height = max(self.font_size + 2, lh)
        if size is not None or name:
            with self._lock:
                self._rewrap()
        if render:
            self._render()

    def visible_lines(self) -> int:
        """当前高度 / 行高下能完整显示多少行（翻页、进度条都按它算）。"""
        return max(1, (self.height - 2 * self.pad_y) // self.line_height)

    def bump_font(self, delta: int) -> int:
        """字号 ±delta（热键用），重折行并重绘，返回新字号。

        行高被用户钉死过就不动它（他要的就是那个行距）；否则跟着字号按比例走。
        """
        self.set_font(size=self.font_size + delta)
        self._remember_look()
        return self.font_size

    def bump_alpha(self, delta: int) -> int:
        """背板不透明度 ±delta（热键用），夹在 [0,255]，返回新值。

        背板越透越隐蔽、越不干扰下层，但字也越难读 —— 这个平衡只能现场调，
        所以给热键而不是只给配置文件。
        """
        self.bg_alpha = max(0, min(255, self.bg_alpha + delta))
        self._render()
        self._remember_look()
        return self.bg_alpha

    def _remember_look(self):
        """把字号/透明度记进 overlay_state.json（remember_pos 关了就不记）。"""
        if self.remember_pos:
            save_state(font_size=self.font_size, bg_alpha=self.bg_alpha)

    def _rewrap(self):
        """把 _raw_lines 按当前宽度/字体折成 _lines（调用方须持有 _lock）。

        折行交给 gdiplus_render.wrap_lines（那边有字符宽度缓存），
        它挂了也不能让浮层空着 —— 退回原样显示，至少还看得见左半边。
        """
        raw = self._raw_lines
        if not self.wrap:
            self._lines = list(raw) or [""]
            return
        try:
            self._lines = _gdi.wrap_lines(raw, self.width - 2 * self.pad_x,
                                          self.font_size, self.font_name) or [""]
        except Exception as e:
            print(f"[overlay] 折行失败，按原样显示: {e}")
            self._lines = list(raw) or [""]

    def set_text(self, text: str, keep_scroll: bool = False):
        """更新浮层内容（线程安全，可被识别线程直接调用）。

        :param keep_scroll: 默认 False = 新内容回到顶部（新答案就该从头看）。
                            流式作答必须传 True：每来一块增量都归零，用户翻到
                            中间照着抄的位置会被反复拽回开头。内容变短时偏移
                            夹回范围内，不会停在一屏空白上。
        """
        with self._lock:
            self._raw_lines = text.splitlines() or [""]
            self._rewrap()
            total = len(self._lines)
        if keep_scroll:
            max_off = max(0, total - self.visible_lines())
            self._scroll_offset = max(0, min(self._scroll_offset, max_off))
        else:
            self._scroll_offset = 0
        try:
            self._render()
        except Exception as e:
            print(f"[overlay] set_text 异常: {e}")

    def hit(self, x: int, y: int) -> bool:
        """屏幕坐标是否落在浮层矩形内（滚轮命中测试用）。"""
        return (self.x <= x < self.x + self.width
                and self.y <= y < self.y + self.height)

    def scroll(self, lines: int):
        """滚动内容（正数=向后看，负数=向前看），自动夹到 [0, max]。"""
        with self._lock:
            total = len(self._lines)
        visible = self.visible_lines()
        max_off = max(0, total - visible)
        new_off = max(0, min(self._scroll_offset + lines, max_off))
        if new_off != self._scroll_offset:
            self._scroll_offset = new_off
            self._render()

    # ========================================================== 鼠标事件判定（可单测）
    def _handle_mouse(self, msg: int, x: int, y: int, delta: int = 0,
                      ctrl_down: bool | None = None,
                      button_down: bool | None = None) -> bool:
        """
        处理一个低级鼠标事件，返回 True 表示「浮层已消费，钩子应拦截该事件」。

        故意与钩子解耦成独立方法：翻页/拖动的判定逻辑可以用合成事件直接驱动
        （见 test_move.py），不必真的动鼠标。

        :param msg:         WM_* 鼠标消息
        :param x, y:        光标屏幕坐标
        :param delta:       仅 WM_MOUSEWHEEL 用，有符号滚轮增量（±120/格）
        :param ctrl_down:   Ctrl 是否按下；None=现场查 GetAsyncKeyState
        :param button_down: 拖动键是否仍按住；None=现场查（丢失抬起事件时解卡用）
        """
        if not self._hwnd:
            return False

        if msg == WM_MOUSEWHEEL:
            return self._handle_wheel(x, y, delta)

        # --- 起手：浮层内 Ctrl+左键 或 中键 → 进入拖动 ---
        if msg in (WM_LBUTTONDOWN, WM_MBUTTONDOWN):
            if self._drag or not (self._visible and self.hit(x, y)):
                return False
            if msg == WM_LBUTTONDOWN:
                held = self._ctrl_down() if ctrl_down is None else ctrl_down
                if not held:
                    return False   # 不带 Ctrl 的普通左键：照旧穿透到下层 IDE
            self._drag = (x - self.x, y - self.y, msg)
            return True

        # --- 拖动中：跟随光标 ---
        if self._drag and msg == WM_MOUSEMOVE:
            dx, dy, btn = self._drag
            still = self._button_down(btn) if button_down is None else button_down
            if not still:
                # 抬起事件没到（切窗口 / 被别的钩子吃掉）→ 结束拖动，别卡住
                self._end_drag()
                return False
            self.move(x - dx, y - dy, save=False)
            return True

        # --- 收手：必须是起手那个键的抬起 ---
        if self._drag and msg in (WM_LBUTTONUP, WM_MBUTTONUP):
            btn = self._drag[2]
            if msg != (WM_LBUTTONUP if btn == WM_LBUTTONDOWN else WM_MBUTTONUP):
                return False
            self._end_drag()
            return True

        return False

    def _handle_wheel(self, x: int, y: int, delta: int) -> bool:
        """光标在浮层内滚滚轮 → 翻页并吃掉事件；否则不干预。"""
        if not (self._visible and self.hit(x, y)):
            return False
        # 去抖：本机若有第三方钩子（鼠标增强/录屏软件）会把滚轮事件重复回放
        # （实测同 delta 紧邻出现多份），30ms 内的完全重复丢弃。
        # 真实滚轮/触控板两格间隔远大于 30ms，不受影响。
        now = time.time()
        if delta == self._last_wheel["delta"] and now - self._last_wheel["t"] < 0.03:
            return True
        self._last_wheel.update(t=now, delta=delta)
        steps = int(round(-delta / 40.0))  # 每 40 滚 1 行
        if steps:
            self.scroll(steps)
        return True

    def _end_drag(self):
        self._drag = None
        if self.remember_pos:
            self._remember(force=True)

    @property
    def dragging(self) -> bool:
        return self._drag is not None

    @staticmethod
    def _ctrl_down() -> bool:
        return bool(user32.GetAsyncKeyState(VK_CONTROL) & 0x8000)

    @staticmethod
    def _button_down(down_msg: int) -> bool:
        vk = VK_LBUTTON if down_msg == WM_LBUTTONDOWN else VK_MBUTTON
        return bool(user32.GetAsyncKeyState(vk) & 0x8000)

    # ========================================================== 低级鼠标钩子
    def install_mouse_hook(self) -> bool:
        """
        安装 WH_MOUSE_LL 全局钩子，让这个「点击穿透」的浮层依然可被操作：
          · 光标在浮层内滚滚轮        → 翻页
          · 浮层内 Ctrl+左键 / 中键拖动 → 移动浮层

        浮层是 WS_EX_TRANSPARENT（不参与命中测试）收不到普通鼠标消息，
        只能用低级钩子 + 自己做命中测试；被消费的事件返回 1 拦截，避免下层
        浏览器/IDE 跟着滚动或响应点击。钩子回调在「安装线程」的消息循环里
        分发，调用方线程必须正在跑 GetMessage 循环（main.py 主线程满足）。
        """
        if self._mouse_hook:
            return True
        u = ctypes.windll.user32

        WH_MOUSE_LL = 14

        class MSLLHOOKSTRUCT(ctypes.Structure):
            _fields_ = [("pt", wt.POINT), ("mouseData", ctypes.c_ulong),
                        ("flags", ctypes.c_ulong), ("time", ctypes.c_ulong),
                        ("dwExtraInfo", ctypes.c_size_t)]

        LRESULT = ctypes.c_ssize_t
        HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, ctypes.c_size_t,
                                      ctypes.POINTER(MSLLHOOKSTRUCT))
        u.CallNextHookEx.restype = LRESULT

        def _proc(nCode, wParam, lParam):
            try:
                if nCode == 0 and self._hwnd:
                    info = lParam.contents
                    # WM_MOUSEWHEEL 时 mouseData 高位字 = 有符号增量
                    # （标准滚轮 ±120/格，触控板 ±40/轻扫）；其它消息该值不用
                    delta = ctypes.c_short((info.mouseData >> 16) & 0xFFFF).value
                    if self._handle_mouse(int(wParam), info.pt.x, info.pt.y, delta):
                        return 1  # 吃掉，避免下层窗口同步响应
            except Exception:
                pass
            return u.CallNextHookEx(None, nCode, wParam, lParam)

        self._mouse_proc = HOOKPROC(_proc)  # 保存引用，防 GC 导致回调失效
        self._mouse_hook = u.SetWindowsHookExW(WH_MOUSE_LL, self._mouse_proc, None, 0)
        return bool(self._mouse_hook)

    def uninstall_mouse_hook(self):
        """卸载鼠标钩子（destroy 会自动调用）。"""
        if self._mouse_hook:
            try:
                ctypes.windll.user32.UnhookWindowsHookEx(self._mouse_hook)
            except Exception:
                pass
            self._mouse_hook = None

    # 旧名保留：现在同一个钩子同时负责翻页与拖动
    def install_wheel_scroll(self) -> bool:
        return self.install_mouse_hook()

    def uninstall_wheel_scroll(self):
        self.uninstall_mouse_hook()

    def show(self):
        """显示并置顶（不抢焦点，不激活）。"""
        if not self._hwnd:
            return
        win32gui.ShowWindow(self._hwnd, win32con.SW_SHOWNOACTIVATE)
        win32gui.SetWindowPos(
            self._hwnd, win32con.HWND_TOPMOST,
            self.x, self.y, self.width, self.height,
            win32con.SWP_NOACTIVATE | win32con.SWP_SHOWWINDOW,
        )
        self._render()
        self._visible = True

    def hide(self):
        if self._hwnd:
            win32gui.ShowWindow(self._hwnd, win32con.SW_HIDE)
        self._visible = False

    def toggle(self):
        self.hide() if self._visible else self.show()

    # ========================================================== 位置与移动
    @staticmethod
    def _monitor_rects() -> list[tuple[int, int, int, int]]:
        """
        所有显示器的矩形 [(l, t, r, b), ...]（含任务栏区域）。

        为什么不用 SM_CXVIRTUALSCREEN 那个外接矩形：多屏分辨率/缩放不一致时，
        外接矩形里存在「不属于任何显示器」的空洞（本机实测：主屏 125% 缩放后
        逻辑宽 1536，副屏却从 1920 开始 → 1536~1920 是空洞），
        窗口挪进空洞等于消失，光看外接矩形夹不住。
        """
        try:
            rects = [tuple(win32api.GetMonitorInfo(h)["Monitor"])
                     for h, _hdc, _r in win32api.EnumDisplayMonitors()]
            if rects:
                return rects
        except Exception:
            pass
        return [(0, 0,
                 win32api.GetSystemMetrics(win32con.SM_CXSCREEN),
                 win32api.GetSystemMetrics(win32con.SM_CYSCREEN))]

    def _clamp(self, x: int, y: int) -> tuple[int, int]:
        """
        保证窗口与某块显示器横竖都有 MIN_VISIBLE 以上重叠，否则拉回最近的那块屏。

        允许贴边只露一条（挡得少、看得见就行），但不允许整块挪出屏幕 ——
        浮层没有任务栏图标也不进 Alt-Tab，真拖丢了只能重启程序。
        """
        x, y = int(x), int(y)
        mons = self._monitor_rects()

        def visible_on(rc) -> bool:
            l, t, r, b = rc
            return (min(x + self.width, r) - max(x, l) >= MIN_VISIBLE
                    and min(y + self.height, b) - max(y, t) >= MIN_VISIBLE)

        if any(visible_on(rc) for rc in mons):
            return x, y

        # 掉到显示器之外（含多屏空洞）→ 夹进中心距离最近的那块屏
        cx, cy = x + self.width // 2, y + self.height // 2
        l, t, r, b = min(mons, key=lambda rc: ((cx - (rc[0] + rc[2]) // 2) ** 2
                                               + (cy - (rc[1] + rc[3]) // 2) ** 2))
        x = max(l + MIN_VISIBLE - self.width, min(x, r - MIN_VISIBLE))
        y = max(t, min(y, b - MIN_VISIBLE))
        return x, y

    def _current_work_area(self) -> tuple[int, int, int, int]:
        """浮层当前所在显示器的工作区 (l, t, r, b)（已排除任务栏）。"""
        try:
            hmon = win32api.MonitorFromPoint(
                (self.x + self.width // 2, self.y + self.height // 2),
                MONITOR_DEFAULTTONEAREST)
            return tuple(win32api.GetMonitorInfo(hmon)["Work"])
        except Exception:
            return (0, 0,
                    win32api.GetSystemMetrics(win32con.SM_CXSCREEN),
                    win32api.GetSystemMetrics(win32con.SM_CYSCREEN))

    def move(self, x: int, y: int, save: bool | None = None):
        """
        移动浮层到屏幕坐标 (x, y)（自动夹取到桌面内；隐藏状态下也生效）。

        分层窗口用 SetWindowPos 移动即可 —— DWM 保留 UpdateLayeredWindow
        已提交的像素，内容跟着走，不必重走一遍 GDI+ 绘制（拖动时每次
        鼠标移动都重绘会明显拖累）。

        :param save: 是否记住这个位置；None=按 remember_pos 决定
        """
        x, y = self._clamp(x, y)
        moved = (x, y) != (self.x, self.y)
        self.x, self.y = x, y
        if moved and self._hwnd:
            win32gui.SetWindowPos(
                self._hwnd, win32con.HWND_TOPMOST, x, y,
                self.width, self.height, win32con.SWP_NOACTIVATE,
            )
        if save or (save is None and self.remember_pos):
            # 显式 save=True 立刻落盘；跟着 remember_pos 走的（如键盘微调
            # 长按自动重复）走限流，避免高频写文件
            self._remember(force=bool(save))

    def resize(self, width: int, height: int, save: bool | None = None) -> tuple[int, int]:
        """改浮层尺寸（像素），返回夹取后的实际 (宽, 高)。

        为什么不只是 SetWindowPos：
          · 宽度变了 → 一行能放多少字也变了，长行要按新宽度重折（否则要么
            右边一截空白，要么又被裁掉，等于折行白做）；
          · 高度变了 → 可视行数变了，原来的滚动偏移可能越界，会停在一屏空白上，
            所以要把偏移收回 [0, 总行数 - 可视行数]；
          · 变大可能把窗口顶出桌面 → 位置要重夹一次。
        上限取当前显示器**工作区**：比屏幕还大的浮层没法用，也没法再拖回来。

        :param save: 是否记住这个尺寸；None=按 remember_pos 决定
        """
        l, t, r, b = self._current_work_area()
        w = max(self.MIN_W, min(int(width), max(self.MIN_W, r - l)))
        h = max(self.MIN_H, min(int(height), max(self.MIN_H, b - t)))
        if (w, h) == (self.width, self.height):
            return w, h

        rewrap = w != self.width
        self.width, self.height = w, h
        with self._lock:
            if rewrap:
                self._rewrap()
            total = len(self._lines)
        self._scroll_offset = max(0, min(self._scroll_offset,
                                         max(0, total - self.visible_lines())))
        self.x, self.y = self._clamp(self.x, self.y)
        if self._hwnd:
            # 先用 SetWindowPos 定尺寸：隐藏状态下 _render 不一定跑得到，
            # 而 show() 会按 self.width/height 摆位，两边要一致
            win32gui.SetWindowPos(
                self._hwnd, win32con.HWND_TOPMOST, self.x, self.y, w, h,
                win32con.SWP_NOACTIVATE,
            )
        self._render()
        if save or (save is None and self.remember_pos):
            save_state(w=w, h=h, x=self.x, y=self.y)
        return w, h

    def bump_size(self, dw: int, dh: int) -> tuple[int, int]:
        """尺寸 ±（热键用），返回新的 (宽, 高)。"""
        return self.resize(self.width + dw, self.height + dh)

    def dock_next(self) -> str:
        """
        循环停靠到当前显示器的 右上→右下→左下→左上→居中（热键用）。

        兜底手段：万一浮层被拖到别扭的位置、或副屏被拔掉，一个键就能拉回来。
        :return: 停靠位置名称（供调用方打印）
        """
        left, top, right, bottom = self._current_work_area()
        m = self.DOCK_MARGIN
        slots = [
            ("右上", right - self.width - m, top + m),
            ("右下", right - self.width - m, bottom - self.height - m),
            ("左下", left + m, bottom - self.height - m),
            ("左上", left + m, top + m),
            ("居中", left + (right - left - self.width) // 2,
                     top + (bottom - top - self.height) // 2),
        ]
        self._dock_idx = (self._dock_idx + 1) % len(slots)
        name, x, y = slots[self._dock_idx]
        self.move(x, y, save=True)
        return name

    def _remember(self, force: bool = False):
        """把当前位置落盘（限流：拖动中最多每 0.5s 写一次，destroy 时补写）。"""
        self._pos_dirty = True
        now = time.time()
        if not force and now - self._last_pos_save < 0.5:
            return
        if save_pos(self.x, self.y):
            self._last_pos_save = now
            self._pos_dirty = False

    def destroy(self):
        self.uninstall_mouse_hook()
        if self.remember_pos and self._pos_dirty:
            save_pos(self.x, self.y)
        if self._hwnd:
            try:
                win32gui.DestroyWindow(self._hwnd)
            except Exception:
                pass
            self._hwnd = None

    def __del__(self):
        try:
            self.destroy()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 便捷函数：系统版本检查
# ---------------------------------------------------------------------------
def check_system() -> dict:
    """返回当前系统对 WDA_EXCLUDEFROMCAPTURE 的支持情况。"""
    try:
        ver = sys.getwindowsversion()
        build = ver.build
    except Exception:
        return {"supported": False, "reason": "非 Windows 系统"}

    supported = build >= MIN_BUILD_FOR_EXCLUDE
    return {
        "build": build,
        "supported": supported,
        "mode": "WDA_EXCLUDEFROMCAPTURE" if supported else "需 fallback WDA_MONITOR",
        "reason": (
            f"Windows build {build}，{'满足' if supported else '低于'}要求 {MIN_BUILD_FOR_EXCLUDE}"
        ),
    }


# ---------------------------------------------------------------------------
# 自测
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys
    info = check_system()
    print("[系统检查]", info)
    if not info["supported"]:
        print("⚠ 当前系统无法使用 WDA_EXCLUDEFROMCAPTURE，将 fallback 到 WDA_MONITOR。")

    # GUI 线程需要消息循环；这里用 win32gui.PumpMessages 简化
    ov = StealthOverlay(title="Windows Defender SmartScreen")
    print("亲和性模式:", ov.affinity_mode)
    ov.set_text(
        "def two_sum(nums, target):\n"
        "    seen = {}\n"
        "    for i, n in enumerate(nums):\n"
        "        if target - n in seen:\n"
        "            return [seen[target - n], i]\n"
        "        seen[n] = i\n"
        "    return []"
    )
    ov.show()
    hooked = ov.install_mouse_hook()
    print("浮层已显示（半透明背板 + 每像素 alpha，点击可穿透到下层 IDE）。")
    print(f"鼠标钩子: {'已启用' if hooked else '安装失败（无法翻页/拖动）'}")
    print("请自行验证：")
    print("  1) 打开 腾讯会议 → 共享屏幕；或打开 OBS/录屏")
    print("  2) 在你本机显示器上应能看到浮层；")
    print("  3) 在共享/录制画面中应看不到（透明空洞）")
    print("  4) 在浮层上按住 Ctrl 拖动（或按住中键拖动）→ 浮层跟着走；")
    print("     不按 Ctrl 的普通点击仍然穿透到下层窗口")
    print("  按 Ctrl+C 退出。")

    try:
        import pythoncom
        pythoncom.CoInitialize()
    except Exception:
        pass

    try:
        # 低级鼠标钩子的回调经本线程的消息队列分发，必须持续泵消息，
        # 否则滚轮/拖动都不会触发（且会拖慢整个系统的鼠标事件）
        while True:
            win32gui.PumpWaitingMessages()
            time.sleep(0.01)
    except KeyboardInterrupt:
        ov.destroy()
        print("已销毁浮层。")
