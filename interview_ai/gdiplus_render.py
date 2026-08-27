"""
gdiplus_render
==================================================================
为 StealthOverlay 绘制一帧带**每像素 alpha** 的 ARGB 内容，并提交给分层窗口。

设计目标
--------
StealthOverlay 用的是 WS_EX_LAYERED + UpdateLayeredWindow(AC_SRC_ALPHA)，
即"每像素 alpha"：每个像素自带 0-255 透明度。
  · 背景像素：A = bg_alpha（如 165）→ 半透明，桌面隐约透出，且可点击穿透
  · 文字像素：A = 255              → 完全不透明、锐利清晰
  · 窗口外区域：A = 0              → 完全透明 + 鼠标穿透

为什么不用 LWA_COLORKEY
----------------------
SetLayeredWindowAttributes(LWA_COLORKEY) 会让某个纯色"透明"，但它与
SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE) 混用时，DWM 合成行为冲突，
颜色键区域可能变成纯黑而非透明，捕获排除也不稳定。
权威建议：要"捕获排除"就不要用 COLORKEY，改用 UpdateLayeredWindow 的每像素 alpha。

实现说明
--------
  · GDI+（gdiplus.dll）原生支持 ARGB Bitmap，画完用
    GdipCreateHBITMAPFromBitmap 取出带 alpha 的 HBITMAP；
  · UpdateLayeredWindow 从 GDI DC 读取该 HBITMAP 的 alpha 通道提交给 DWM。
  · 所有 GDI / GDI+ 句柄在本函数内成对创建与释放，避免泄漏。

依赖：仅 pywin32（ctypes 绑定系统 gdiplus.dll / gdi32.dll，无需额外 pip 包）。
"""
import ctypes
import ctypes.wintypes as wt
import unicodedata

# ---------------------------------------------------------------------------
# GDI+ / GDI 绑定
# ---------------------------------------------------------------------------
gdiplus = ctypes.windll.gdiplus
gdi32 = ctypes.windll.gdi32
user32 = ctypes.windll.user32

PixelFormat32bppARGB = 0x26200A

# wintypes 中缺失的类型别名（Python 3.13 已移除 ULONG_PTR，且无 STATUS/ARGB）
ULONG_PTR = ctypes.c_size_t   # 指针宽度无符号整数
STATUS = ctypes.c_int         # GDI+ Status 枚举（32 位）
ARGB = ctypes.c_ulong         # 0xAARRGGBB 32 位颜色值


class GdiplusStartupInput(ctypes.Structure):
    _fields_ = [
        ("GdiplusVersion", wt.UINT),
        ("DebugEventCallback", wt.LPVOID),
        ("SuppressBackgroundThread", wt.BOOL),
        ("SuppressExternalCodecs", wt.BOOL),
    ]


class RectF(ctypes.Structure):
    _fields_ = [
        ("X", ctypes.c_float), ("Y", ctypes.c_float),
        ("Width", ctypes.c_float), ("Height", ctypes.c_float),
    ]


# --- 函数原型 ---
GdiplusStartup = gdiplus.GdiplusStartup
GdiplusStartup.argtypes = [
    ctypes.POINTER(ULONG_PTR), ctypes.POINTER(GdiplusStartupInput), ctypes.POINTER(wt.LPVOID),
]
GdiplusStartup.restype = STATUS

GdipCreateBitmapFromScan0 = gdiplus.GdipCreateBitmapFromScan0
GdipCreateBitmapFromScan0.argtypes = [
    wt.INT, wt.INT, wt.INT, wt.INT, wt.LPVOID, ctypes.POINTER(wt.LPVOID),
]
GdipCreateBitmapFromScan0.restype = STATUS

GdipGetImageGraphicsContext = gdiplus.GdipGetImageGraphicsContext
GdipGetImageGraphicsContext.argtypes = [wt.LPVOID, ctypes.POINTER(wt.LPVOID)]
GdipGetImageGraphicsContext.restype = STATUS

GdipSetSmoothingMode = gdiplus.GdipSetSmoothingMode
GdipSetSmoothingMode.argtypes = [wt.LPVOID, wt.INT]
GdipSetSmoothingMode.restype = STATUS

GdipGraphicsClear = gdiplus.GdipGraphicsClear
GdipGraphicsClear.argtypes = [wt.LPVOID, ARGB]
GdipGraphicsClear.restype = STATUS

GdipCreateSolidFill = gdiplus.GdipCreateSolidFill
GdipCreateSolidFill.argtypes = [ARGB, ctypes.POINTER(wt.LPVOID)]
GdipCreateSolidFill.restype = STATUS

GdipDeleteBrush = gdiplus.GdipDeleteBrush
GdipDeleteBrush.argtypes = [wt.LPVOID]
GdipDeleteBrush.restype = STATUS

GdipFillRectangleI = gdiplus.GdipFillRectangleI
GdipFillRectangleI.argtypes = [wt.LPVOID, wt.LPVOID, wt.INT, wt.INT, wt.INT, wt.INT]
GdipFillRectangleI.restype = STATUS

GdipCreateFontFamilyFromName = gdiplus.GdipCreateFontFamilyFromName
# 签名: (WCHAR *name, GpFontCollection *collection /*NULL=系统集*/, GpFontFamily **out)
GdipCreateFontFamilyFromName.argtypes = [wt.LPCWSTR, wt.LPVOID, ctypes.POINTER(wt.LPVOID)]
GdipCreateFontFamilyFromName.restype = STATUS

GdipDeleteFontFamily = gdiplus.GdipDeleteFontFamily
GdipDeleteFontFamily.argtypes = [wt.LPVOID]
GdipDeleteFontFamily.restype = STATUS

GdipCreateFont = gdiplus.GdipCreateFont
GdipCreateFont.argtypes = [
    wt.LPVOID, ctypes.c_float, wt.INT, wt.INT, ctypes.POINTER(wt.LPVOID),
]
GdipCreateFont.restype = STATUS

GdipDeleteFont = gdiplus.GdipDeleteFont
GdipDeleteFont.argtypes = [wt.LPVOID]
GdipDeleteFont.restype = STATUS

GdipDrawString = gdiplus.GdipDrawString
GdipDrawString.argtypes = [
    wt.LPVOID, wt.LPCWSTR, wt.INT, wt.LPVOID, ctypes.POINTER(RectF), wt.LPVOID, wt.LPVOID,
]
GdipDrawString.restype = STATUS

GdipMeasureString = gdiplus.GdipMeasureString
# 签名: (graphics, str, len, font, &layoutRect, format, &boundingBox,
#        *codepointsFitted, *linesFilled)
GdipMeasureString.argtypes = [
    wt.LPVOID, wt.LPCWSTR, wt.INT, wt.LPVOID, ctypes.POINTER(RectF), wt.LPVOID,
    ctypes.POINTER(RectF), ctypes.POINTER(wt.INT), ctypes.POINTER(wt.INT),
]
GdipMeasureString.restype = STATUS

GdipCreateHBITMAPFromBitmap = gdiplus.GdipCreateHBITMAPFromBitmap
GdipCreateHBITMAPFromBitmap.argtypes = [wt.LPVOID, ctypes.POINTER(wt.HBITMAP), ARGB]
GdipCreateHBITMAPFromBitmap.restype = STATUS

GdipDeleteGraphics = gdiplus.GdipDeleteGraphics
GdipDeleteGraphics.argtypes = [wt.LPVOID]
GdipDeleteGraphics.restype = STATUS

GdipDisposeImage = gdiplus.GdipDisposeImage
GdipDisposeImage.argtypes = [wt.LPVOID]
GdipDisposeImage.restype = STATUS

# UpdateLayeredWindow
class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

class _SIZE(ctypes.Structure):
    _fields_ = [("cx", ctypes.c_long), ("cy", ctypes.c_long)]

class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [
        ("BlendOp", ctypes.c_ubyte),
        ("BlendFlags", ctypes.c_ubyte),
        ("SourceConstantAlpha", ctypes.c_ubyte),
        ("AlphaFormat", ctypes.c_ubyte),
    ]

AC_SRC_OVER = 0x00
AC_SRC_ALPHA = 0x01

user32.UpdateLayeredWindow.argtypes = [
    wt.HWND, wt.HDC, ctypes.POINTER(_POINT), ctypes.POINTER(_SIZE),
    wt.HDC, ctypes.POINTER(_POINT), wt.COLORREF,
    ctypes.POINTER(BLENDFUNCTION), wt.DWORD,
]
user32.UpdateLayeredWindow.restype = wt.BOOL


_token = None


def _startup():
    """进程内初始化 GDI+（幂等）。"""
    global _token
    if _token is not None:
        return
    inp = GdiplusStartupInput()
    inp.GdiplusVersion = 1
    token = ULONG_PTR()
    if GdiplusStartup(ctypes.byref(token), ctypes.byref(inp), None) != 0:
        raise RuntimeError("GdiplusStartup 失败")
    _token = token


def _argb(a: int, r: int, g: int, b: int) -> int:
    return ((a & 0xFF) << 24) | ((r & 0xFF) << 16) | ((g & 0xFF) << 8) | (b & 0xFF)


# ---------------------------------------------------------------------------
# 对外 API
# 已提醒过的字体名（同一个错拼的字体只在控制台说一次，别每帧刷屏）
_warned_fonts: set[str] = set()


def _cleanup_surface(g, bitmap):
    """错误路径提前返回时释放绘制表面（正常路径在函数末尾统一释放）。

    每次 present 都是一帧，这里漏一次就是每帧漏一次 —— 浮层刷得很勤，
    很快就能把 GDI+ 对象攒到出问题。
    """
    GdipDeleteGraphics(g)
    GdipDisposeImage(bitmap)


# ---------------------------------------------------------------------------
# 文本测量与软换行
# ---------------------------------------------------------------------------
# 每个 (字体, 字号) 一套测量用的 1x1 Bitmap + Graphics + Font，长期复用；
# 字符宽度再按 (字体, 字号, 字符) 缓存 —— 一屏中文也就几百个不同字，
# 量一次之后折行就是纯加法，翻页/流式刷新都不会再碰 GDI+。
_measure_ctx: dict[tuple[str, int], tuple] = {}
_char_w: dict[tuple[str, int, str], float] = {}

# 允许在这些字符「之后」断行：空格、中文标点、以及代码里读起来自然的分隔符。
# 不含英文字母/数字，所以英文单词不会被劈开（劈不动就退回硬断）。
_BREAK_AFTER = set(" \t，。、；：？！）】》」』,.;:?!)]}>-+*/&|=")
# 这些字符不能落在行首（中文排版禁则）：句读和收尾括号跑到下一行开头很扎眼
_NO_LINE_START = set("，。、；：？！）】》」』,.;:?!)]}%")


def _measure_env(font_name: str, font_size: int):
    """拿到（或建好）某字体+字号的测量上下文，失败返回 None（调用方退回估算）。"""
    key = (font_name, int(font_size))
    if key in _measure_ctx:
        return _measure_ctx[key]
    try:
        _startup()
        bmp = wt.LPVOID()
        if GdipCreateBitmapFromScan0(1, 1, 0, PixelFormat32bppARGB, None,
                                     ctypes.byref(bmp)) != 0:
            return None
        g = wt.LPVOID()
        if GdipGetImageGraphicsContext(bmp, ctypes.byref(g)) != 0:
            GdipDisposeImage(bmp)
            return None
        family = wt.LPVOID()
        for cand in (font_name, "Consolas", "Courier New"):
            if GdipCreateFontFamilyFromName(cand, None, ctypes.byref(family)) == 0:
                break
        else:
            _cleanup_surface(g, bmp)
            return None
        font = wt.LPVOID()
        if GdipCreateFont(family, float(font_size), 0, 2, ctypes.byref(font)) != 0:
            GdipDeleteFontFamily(family)
            _cleanup_surface(g, bmp)
            return None
        _measure_ctx[key] = (bmp, g, family, font)
        return _measure_ctx[key]
    except Exception:
        return None


def _measure(env, text: str) -> float | None:
    """量一段文字的宽度（像素）。"""
    _bmp, g, _family, font = env
    layout = RectF(0.0, 0.0, 1e6, 1e6)
    box = RectF()
    fitted, filled = wt.INT(), wt.INT()
    if GdipMeasureString(g, text, -1, font, ctypes.byref(layout), None,
                         ctypes.byref(box), ctypes.byref(fitted),
                         ctypes.byref(filled)) != 0:
        return None
    return float(box.Width)


def _estimate_char_width(ch: str, font_size: int) -> float:
    """没有 GDI+ 时的退路：CJK/全角算一个整宽，其它按等宽字体的 0.6em 估。"""
    return font_size * (1.0 if unicodedata.east_asian_width(ch) in "WF" else 0.6)


def char_width(ch: str, font_size: int, font_name: str) -> float:
    """单个字符的推进宽度（像素），带缓存。

    直接量单字符会把 GDI+ MeasureString 两侧的额外留白（约 1/6 em）算进去，
    折行就会偏窄一大截。量 11 个再减 1 个除以 10，留白正好抵掉。
    """
    key = (font_name, int(font_size), ch)
    hit = _char_w.get(key)
    if hit is not None:
        return hit
    w = None
    env = _measure_env(font_name, font_size)
    if env is not None:
        w11, w1 = _measure(env, ch * 11), _measure(env, ch)
        if w11 is not None and w1 is not None and w11 > w1:
            w = (w11 - w1) / 10.0
    if w is None or w <= 0:
        w = _estimate_char_width(ch, font_size)
    _char_w[key] = w
    return w


def wrap_lines(lines: list[str], max_width: float, font_size: int, font_name: str,
               width_fn=None) -> list[str]:
    """按可用像素宽度软换行，返回新的行列表。

    - 折行按**实际字符宽度**算，中英文混排不会算错（一个汉字≈两个字母宽）；
    - 续行沿用原行的缩进，代码折行后仍然对得起来；
    - 尽量在空格/标点后断（英文单词不劈开），实在断不动就硬断；
    - 句读、收尾括号不会被甩到行首（中文排版禁则）；
    - Tab 先展开成 4 空格：GDI+ DrawString 对 \\t 的处理不可控。

    width_fn 只为测试注入（给定字符 → 宽度），正常走带缓存的 char_width。
    """
    wf = width_fn or (lambda ch: char_width(ch, font_size, font_name))

    def width_of(s: str) -> float:
        return sum(wf(c) for c in s)

    out: list[str] = []
    for raw in lines:
        line = raw.expandtabs(4)
        if not line:
            out.append("")
            continue
        if max_width <= 0 or width_of(line) <= max_width:
            out.append(line)
            continue

        indent = line[: len(line) - len(line.lstrip())]
        # 缩进本身占掉半屏时就不再沿用，否则续行剩不下几个字
        cont = indent if width_of(indent) <= max_width * 0.5 else ""
        i, first = 0, True
        while i < len(line):
            prefix = "" if first else cont
            avail = max_width - width_of(prefix)
            if avail <= 0:                     # 窗口窄到放不下缩进：整行原样吐出
                out.append(line[i:])
                break
            # 贪心吃字符，记住最后一个可断点
            j, w, brk = i, 0.0, -1
            while j < len(line):
                cw = wf(line[j])
                if w + cw > avail and j > i:
                    break
                w += cw
                j += 1
                if j < len(line) and line[j] not in _NO_LINE_START and (
                        line[j - 1] in _BREAK_AFTER
                        or unicodedata.east_asian_width(line[j - 1]) in "WF"
                        or unicodedata.east_asian_width(line[j]) in "WF"):
                    brk = j
            if j >= len(line):
                out.append(prefix + line[i:])
                break
            cut = brk if brk > i else j        # 没有可断点就硬断
            if cut < len(line) and line[cut] in _NO_LINE_START and cut - 1 > i:
                cut -= 1                       # 硬断也别把标点甩到行首
            seg = (prefix + line[i:cut]).rstrip()
            if not seg and brk > i:
                # 断点只切出一串空白（行首深缩进时会这样）→ 改硬断，别吐空行
                cut = j
                seg = (prefix + line[i:cut]).rstrip()
            out.append(seg)
            i = cut
            while i < len(line) and line[i] == " ":   # 断在空格处：吞掉行首空格
                i += 1
            first = False
    return out


# ---------------------------------------------------------------------------
def present(
    hwnd: int,
    width: int,
    height: int,
    lines: list[str],
    bg_color: tuple[int, int, int],
    bg_alpha: int,
    text_color: tuple[int, int, int],
    x: int,
    y: int,
    pad_x: int,
    pad_y: int,
    line_height: int,
    font_size: int,
    font_name: str,
) -> bool:
    """
    绘制一帧并立即提交给 hwnd（WS_EX_LAYERED 窗口）。

    流程：ARGB Bitmap → GDI+ 绘制 → HBITMAP → UpdateLayeredWindow。
    所有 GDI/GDI+ 句柄在函数返回前释放（HBITMAP 在 UpdateLayeredWindow
    读完后立即 DeleteObject，因为该函数会拷贝像素数据）。

    返回 True 表示 UpdateLayeredWindow 成功。
    """
    _startup()

    # 1) ARGB 绘制表面
    bitmap = wt.LPVOID()
    if GdipCreateBitmapFromScan0(width, height, 0, PixelFormat32bppARGB, None, ctypes.byref(bitmap)) != 0:
        return False

    g = wt.LPVOID()
    GdipGetImageGraphicsContext(bitmap, ctypes.byref(g))
    GdipSetSmoothingMode(g, 4)  # AntiAlias

    # 2) 整块清成完全透明（窗口外区域保持穿透）
    GdipGraphicsClear(g, _argb(0, 0, 0, 0))

    # 3) 背板：半透明填充
    br = wt.LPVOID()
    GdipCreateSolidFill(_argb(bg_alpha, *bg_color), ctypes.byref(br))
    GdipFillRectangleI(g, br, 0, 0, width, height)
    GdipDeleteBrush(br)

    # 4) 文字：不透明
    # GdipCreateFont 第一个参数是 GpFontFamily*（对象指针），不能直接传
    # 字体名字符串——必须先 GdipCreateFontFamilyFromName 创建族对象，
    # 否则 font 句柄无效，GdipDrawString 静默失败（只剩背板无文字）。
    #
    # 字体名来自 config，用户完全可能写错或写个没装的字体。那时直接放弃
    # 会得到「一块什么都没有的背板」——最难排查的一种表现，所以退回内置
    # 等宽字体，并把这次退化打印出来（每个字体名只提醒一次）。
    family = wt.LPVOID()
    used_name = font_name
    for cand in (font_name, "Consolas", "Courier New"):
        if GdipCreateFontFamilyFromName(cand, None, ctypes.byref(family)) == 0:
            used_name = cand
            break
    else:
        _cleanup_surface(g, bitmap)
        return False
    if used_name != font_name and font_name not in _warned_fonts:
        _warned_fonts.add(font_name)
        print(f"[render] 字体 {font_name!r} 不可用，已退回 {used_name!r}")
    font = wt.LPVOID()
    # UnitPixel(2)：字号按像素计，与 line_height 语义一致
    if GdipCreateFont(family, float(font_size), 0, 2, ctypes.byref(font)) != 0:
        GdipDeleteFontFamily(family)
        _cleanup_surface(g, bitmap)
        return False
    tb = wt.LPVOID()
    GdipCreateSolidFill(_argb(255, *text_color), ctypes.byref(tb))

    for i, line in enumerate(lines):
        top = pad_y + i * line_height
        if top + line_height > height:
            break
        rf = RectF(float(pad_x), float(top), float(width - 2 * pad_x), float(line_height))
        GdipDrawString(g, line, -1, font, ctypes.byref(rf), None, tb)

    GdipDeleteBrush(tb)
    GdipDeleteFont(font)
    GdipDeleteFontFamily(family)

    # 5) ARGB Bitmap → 带 alpha 的 HBITMAP
    hbitmap = wt.HBITMAP()
    GdipCreateHBITMAPFromBitmap(bitmap, ctypes.byref(hbitmap), _argb(0, 0, 0, 0))

    # 6) 准备 GDI DC（GetDC/ReleaseDC 属于 user32，不是 gdi32）
    screen_dc = user32.GetDC(0)
    mem_dc = gdi32.CreateCompatibleDC(screen_dc)
    gdi32.SelectObject(mem_dc, hbitmap)

    # 7) 提交给分层窗口
    src_pt = _POINT(0, 0)
    win_size = _SIZE(width, height)
    dst_pt = _POINT(x, y)
    blend = BLENDFUNCTION()
    blend.BlendOp = AC_SRC_OVER
    blend.SourceConstantAlpha = 255
    blend.AlphaFormat = AC_SRC_ALPHA

    ok = user32.UpdateLayeredWindow(
        hwnd, screen_dc, ctypes.byref(dst_pt), ctypes.byref(win_size),
        mem_dc, ctypes.byref(src_pt), 0, ctypes.byref(blend), 0,
    )

    # 8) 清理（顺序重要）
    gdi32.DeleteObject(hbitmap)          # UpdateLayeredWindow 已拷贝，可立即释放
    gdi32.DeleteDC(mem_dc)
    user32.ReleaseDC(0, screen_dc)
    GdipDeleteGraphics(g)
    GdipDisposeImage(bitmap)

    return bool(ok)
