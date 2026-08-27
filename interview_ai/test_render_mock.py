"""
逻辑执行验证：用 mock 桩模拟 Windows API，真实跑一遍 gdiplus_render.present
============================================================================

为什么需要这个：
  Linux 沙盒没有 gdiplus.dll / user32.dll，无法 import gdiplus_render 原生运行。
  但我们可以用 unittest.mock 把 ctypes.windll.gdiplus / gdi32 / user32 全部桩掉，
  让 present() 的真实 Python 控制流完整执行一遍，验证：
    - 所有 GDI/GDI+ 句柄按正确顺序创建与释放
    - UpdateLayeredWindow 收到的参数正确（含 AC_SRC_ALPHA、尺寸、位置）
    - HBITMAP 在提交后被 DeleteObject（无资源泄漏）
    - 错误路径（GdipCreateBitmapFromScan0 失败）被正确返回

这抓得到纯静态检查抓不到的 bug：参数顺序、未定义变量、资源泄漏。
"""
import sys
import ctypes
import ctypes.wintypes as wt
from unittest.mock import MagicMock

# 中文 Windows 控制台默认 GBK，✅/❌ 会 UnicodeEncodeError（跑不起来）。
# 已经是 utf-8 就别再包一层：新 wrapper 会顶掉旧的，旧的被回收时连底下的
# buffer 一起关闭，之后所有 print 都 ValueError（多个测试同进程跑会踩到）
try:
    import io
    if (getattr(sys.stdout, "encoding", "") or "").lower().replace("-", "") != "utf8":
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                      line_buffering=True)
except Exception:
    pass

# ---- 补足 Linux ctypes.wintypes 缺失的 Windows 专属类型 ----
# gdiplus_render 用到了 ULONG_PTR / LPVOID / HWND / HBITMAP / COLORREF / STATUS / LPCWSTR 等。
# 在 Linux 上这些不一定存在，手动补齐后再 import 模块。
for _name, _typ in (
    ("ULONG_PTR", ctypes.c_void_p),
    ("LPVOID", ctypes.c_void_p),
    ("LPCWSTR", ctypes.c_wchar_p),
    ("HBITMAP", ctypes.c_void_p),
    ("HWND", ctypes.c_void_p),
    ("HDC", ctypes.c_void_p),
    ("HBRUSH", ctypes.c_void_p),
    ("COLORREF", ctypes.c_uint32),
    ("STATUS", ctypes.c_int),
    ("ARGB", ctypes.c_uint32),
):
    if not hasattr(wt, _name):
        setattr(wt, _name, _typ)
# DWORD / BOOL / INT / UINT / POINTER 等在 wintypes 中通常存在，无需补

# ---- 挂载假的 Windows DLL 句柄 ----
# gdiplus_render 模块顶层执行 "gdiplus = ctypes.windll.gdiplus" 等绑定，
# 所以 import 它之前必须先让 ctypes.windll.<dll> 都指向我们的桩。
# Linux 上没有 ctypes.windll，故手动挂载一个 MagicMock。
fake = MagicMock()
ctypes.windll = fake
fake.gdiplus = fake
fake.gdi32 = fake
fake.user32 = fake
fake.kernel32 = fake

import gdiplus_render as gr


def make_status_mock():
    """让每个 GDI+ 函数默认返回 0（Ok = 成功）。"""
    m = MagicMock(return_value=0)
    return m


def reset_all():
    """让每个 GDI+ / GDI 函数默认返回 0（Ok / TRUE = 成功），并用 out-param 桩填充句柄。"""
    fake.reset_mock()
    # 允许通过 .return_value / .side_effect 精细控制
    fake.GdiplusStartup.return_value = 0
    fake.GdipSetSmoothingMode.return_value = 0
    fake.GdipGraphicsClear.return_value = 0
    fake.GdipCreateSolidFill.return_value = 0
    fake.GdipFillRectangleI.return_value = 0
    fake.GdipCreateFont.return_value = 0
    fake.GdipDeleteFont.return_value = 0
    fake.GdipDrawString.return_value = 0
    fake.GdipDeleteBrush = MagicMock(return_value=0)
    fake.GdipDeleteGraphics.return_value = 0
    fake.GdipDisposeImage.return_value = 0
    fake.UpdateLayeredWindow.return_value = 1  # BOOL TRUE

    # GDI 句柄类函数：返回整数（ctypes 会把它们当作 HDC/HBITMAP，避免转换 MagicMock 报错）
    fake.GetDC.return_value = 0x1000
    fake.CreateCompatibleDC.return_value = 0x1001
    fake.SelectObject.return_value = 0x2000

    # --- out 参数桩：ctypes.byref 出来的 POINTER，赋值 .contents ---
    def make_out_handle(value):
        """返回一个 side_effect：把指针的 contents.value 设为 value，返回 0。"""
        def _eff(ptr, *rest):
            try:
                ptr.contents.value = value
            except Exception:
                pass
            return 0
        return _eff

    # GdiplusStartup(token, ...) → token.value = 1
    def startup_eff(token, inp, callback):
        try:
            token[0] = 1
        except Exception:
            pass
        return 0
    fake.GdiplusStartup.side_effect = startup_eff

    # GdipCreateBitmapFromScan0(w,h,s,pf,data,&bitmap)
    fake.GdipCreateBitmapFromScan0.side_effect = make_out_handle(0x10)
    # GdipGetImageGraphicsContext(bitmap, &g)
    fake.GdipGetImageGraphicsContext.side_effect = make_out_handle(0x20)
    # GdipCreateHBITMAPFromBitmap(bitmap, &hbitmap, bg)
    fake.GdipCreateHBITMAPFromBitmap.side_effect = make_out_handle(0x30)
    # GdipCreateFontFamilyFromName(name, NULL, &family)
    # （必须桩：present 里字体族创建失败会直接 return False）
    fake.GdipCreateFontFamilyFromName.side_effect = make_out_handle(0x40)
    fake.GdipDeleteFontFamily.return_value = 0


reset_all()
# 重置模块级 GDI+ token，迫使 present 走 _startup（会调用桩）
gr._token = None


def call_present():
    return gr.present(
        hwnd=0x100,
        width=300, height=200,
        lines=["def f():", "    return 1"],
        bg_color=(20, 22, 30), bg_alpha=165, text_color=(235, 235, 235),
        x=100, y=50,
        pad_x=18, pad_y=14, line_height=26, font_size=18, font_name="Consolas",
    )


print("=" * 66)
print("测试 1：正常路径 —— 全链路调用顺序与参数")
print("=" * 66)
reset_all()
rc = call_present()
print(f"  present 返回值: {rc}  （1=True=成功）")
assert rc == 1, "present 应返回 True"

calls = [n for n, _, _ in fake.method_calls]
gdi_calls = [n for n in calls if n.startswith("Gdip") or n == "UpdateLayeredWindow"]
print(f"  GDI+ 调用序列: {gdi_calls}")

assert fake.GdipCreateBitmapFromScan0.called, "应创建 ARGB Bitmap"
assert fake.GdipGraphicsClear.called, "应清空为透明"
assert fake.GdipFillRectangleI.called, "应填充背板"
assert fake.GdipDrawString.call_count == 2, f"两行文字应 DrawString x2，实际 {fake.GdipDrawString.call_count}"
assert fake.GdipCreateHBITMAPFromBitmap.called, "应生成 HBITMAP"
assert fake.UpdateLayeredWindow.called, "应提交给分层窗口"
print("  ✅ 正常路径断言全部通过")

print()
print("=" * 66)
print("测试 2：UpdateLayeredWindow 参数正确性")
print("=" * 66)

captured = {}

# ctypes 在把结构通过 byref 传给 mock 时，参数会变成 CArgObject（无 .x 属性）。
# 用 cast 把它还原为结构指针来读取字段。
def ulw_side_effect(hwnd, screen_dc, dst_pt, size, mem_dc, src_pt, cr_key, blend, flags):
    captured["hwnd"] = hwnd
    # dst_pt / size / blend 都是 CArgObject → 视为对应结构指针
    p_dst = ctypes.cast(dst_pt, ctypes.POINTER(gr._POINT)).contents
    p_size = ctypes.cast(size, ctypes.POINTER(gr._SIZE)).contents
    p_blend = ctypes.cast(blend, ctypes.POINTER(gr.BLENDFUNCTION)).contents
    captured["dst"] = (p_dst.x, p_dst.y)
    captured["size"] = (p_size.cx, p_size.cy)
    captured["blend"] = (p_blend.BlendOp, p_blend.SourceConstantAlpha, p_blend.AlphaFormat)
    return 1  # TRUE

fake.UpdateLayeredWindow.side_effect = ulw_side_effect

call_present()

hwnd_arg = captured["hwnd"]
dst = captured["dst"]
sz = captured["size"]
blend_op, src_alpha, alpha_fmt = captured["blend"]
print(f"  hwnd={hwnd_arg}, dst={dst}, size={sz}")
print(f"  blend: BlendOp={blend_op}, SrcAlpha={src_alpha}, AlphaFmt={alpha_fmt}")
assert hwnd_arg == 0x100
assert dst == (100, 50)
assert sz == (300, 200)
assert alpha_fmt == gr.AC_SRC_ALPHA, "必须用每像素 alpha，而非 colorkey"
assert src_alpha == 255
print("  ✅ 参数正确（含 AC_SRC_ALPHA、尺寸 300x200、位置 100,50）")

print()
print("=" * 66)
print("测试 3：资源生命周期 —— HBITMAP 提交后被 DeleteObject")
print("=" * 66)
reset_all()
call_present()
method_names = [n for n, _, _ in fake.method_calls]
ulw_idx = method_names.index("UpdateLayeredWindow")
del_idx = method_names.index("DeleteObject") if "DeleteObject" in method_names else None
assert del_idx is not None, "必须调用 gdi32.DeleteObject(hbitmap)"
assert del_idx > ulw_idx, "DeleteObject(HBITMAP) 必须在 UpdateLayeredWindow 之后（先提交再释放）"
print(f"  调用顺序: ... UpdateLayeredWindow[#{ulw_idx}] ... DeleteObject[#{del_idx}] ✅")
for pair in [("GdipCreateFont", "GdipDeleteFont"),
             ("GdipGetImageGraphicsContext", "GdipDeleteGraphics"),
             ("GdipCreateBitmapFromScan0", "GdipDisposeImage")]:
    c = method_names.count(pair[0])
    d = method_names.count(pair[1])
    assert c == d, f"{pair}: 创建 {c} 次 ≠ 释放 {d} 次"
    print(f"  {pair[0]} x{c} ↔ {pair[1]} x{d} ✅")
print("  ✅ 无 GDI 资源泄漏")

print()
print("=" * 66)
print("测试 4：错误路径 —— 创建 Bitmap 失败时应返回 False")
print("=" * 66)
reset_all()
fake.GdipCreateBitmapFromScan0.side_effect = lambda *a: 1  # 非 0 = 失败
rc = call_present()
print(f"  present 返回值: {rc}  （0=False=失败）")
assert rc == 0, "Bitmap 创建失败应返回 False"
print("  ✅ 错误路径正确返回 False")

print()
print("=" * 66)
print("测试 5：字体名写错 —— 退回内置等宽字体，仍然出图")
print("=" * 66)
reset_all()
gr._warned_fonts.clear()
tried_names = []


def family_eff(name, dummy, ptr):
    """第一个字体名（用户配的）失败，后面的候选成功。"""
    tried_names.append(name)
    if len(tried_names) == 1:
        return 1  # 非 0 = 失败
    try:
        ptr.contents.value = 0x40
    except Exception:
        pass
    return 0


fake.GdipCreateFontFamilyFromName.side_effect = family_eff
rc = gr.present(
    hwnd=0x100, width=300, height=200, lines=["x = 1"],
    bg_color=(20, 22, 30), bg_alpha=165, text_color=(235, 235, 235),
    x=0, y=0, pad_x=18, pad_y=14, line_height=26, font_size=18,
    font_name="NoSuchFontZZZ",
)
print(f"  依次尝试的字体: {tried_names}")
print(f"  present 返回值: {rc}")
assert rc == 1, "字体退化后仍应出图（否则用户只看到一块空背板，最难排查）"
assert tried_names[0] == "NoSuchFontZZZ" and len(tried_names) == 2
assert fake.GdipDrawString.called, "退回字体后仍应绘制文字"
print("  ✅ 字体不可用时退回候选字体并正常绘制")

print()
print("=" * 66)
print("测试 6：所有候选字体都失败 —— 返回 False 且不泄漏绘制表面")
print("=" * 66)
reset_all()
gr._warned_fonts.clear()
fake.GdipCreateFontFamilyFromName.side_effect = lambda *a: 1  # 全部失败
rc = call_present()
names = [n for n, _, _ in fake.method_calls]
print(f"  present 返回值: {rc}  （0=False=失败）")
assert rc == 0
assert names.count("GdipDeleteGraphics") == names.count("GdipGetImageGraphicsContext")
assert names.count("GdipDisposeImage") == names.count("GdipCreateBitmapFromScan0")
print("  ✅ 提前返回也释放了 Graphics / Bitmap（浮层每帧一次，漏一次就是每帧漏）")

print()
print("=" * 66)
print("测试 7：wrap_lines 软换行（注入字符宽度，不碰 GDI+）")
print("=" * 66)


def w_ascii(ch: str) -> float:
    """测试用字符宽度：CJK/全角算 2，其它算 1 —— 断言就能按"格"数。"""
    import unicodedata
    return 2.0 if unicodedata.east_asian_width(ch) in "WF" else 1.0


def wrap(lines, width):
    return gr.wrap_lines(lines, width, 18, "Consolas", width_fn=w_ascii)


# 放得下就原样返回，空行保留（不能被吞掉，否则段落全糊成一坨）
assert wrap(["abc", "", "de"], 10) == ["abc", "", "de"]
# max_width <= 0（窗口宽度还没算出来）不能死循环，原样返回
assert wrap(["abcdefghij"], 0) == ["abcdefghij"]

# 英文按空格断，单词不被劈开
assert wrap(["hello world foo"], 8) == ["hello", "world", "foo"]
# 没有可断点 → 硬断，且一个字符都不能丢
hard = wrap(["a" * 20], 6)
print(f"  硬断: {hard}")
assert hard == ["aaaaaa", "aaaaaa", "aaaaaa", "aa"] and "".join(hard) == "a" * 20

# 中文可以逐字断（一个汉字占两格）
assert wrap(["中文测试"], 5) == ["中文", "测试"]
# 中英混排按宽度算而不是字符数：4 个汉字 = 8 格，不能塞进 6 格一行
assert wrap(["中文测试ab"], 6) == ["中文测", "试ab"]
# 中文排版禁则：句读不能落到行首（"，" 该跟着上一行走，宁可少放一个字）
punct = wrap(["中文，测试。收尾"], 6)
print(f"  标点禁则: {punct}")
assert not any(ln[0] in "，。" for ln in punct if ln), punct

# 代码折行沿用原缩进，视觉上还对得齐
code = wrap(["    x = aaaaaaaaaa"], 10)
print(f"  代码折行: {code}")
assert code == ["    x =", "    aaaaaa", "    aaaa"], code
assert all(ln.startswith("    ") for ln in code[1:]), "续行必须保留缩进"

# 缩进本身超过半屏时不再沿用，否则续行剩不下几个字；也不能因此吐出空行
deep = wrap([" " * 8 + "abcdefgh"], 10)
print(f"  超深缩进: {deep}")
assert deep == ["        ab", "cdefgh"], deep

# Tab 先展开成 4 空格（GDI+ 对 \t 的处理不可控）
assert wrap(["\tab"], 20) == ["    ab"]
print("  ✅ 软换行：空格/中文断点、硬断兜底、缩进沿用、Tab 展开")

print()
print("=" * 66)
print("测试 8：char_width —— 测不到时退回估算，且结果被缓存")
print("=" * 66)
gr._char_w.clear()
gr._measure_ctx.clear()
# mock 桩不会填 MeasureString 的 boundingBox（宽度恒为 0）→ 应退回估算
w_cjk = gr.char_width("中", 20, "Consolas")
w_lat = gr.char_width("a", 20, "Consolas")
print(f"  '中'={w_cjk}  'a'={w_lat}")
assert w_cjk == 20.0, "全角字符按一个整宽估"
assert 0 < w_lat < w_cjk, "等宽字体的半角字符应窄于全角"
assert ("Consolas", 20, "中") in gr._char_w, "量过的字符应进缓存"
before = len(fake.method_calls)
gr.char_width("中", 20, "Consolas")
assert len(fake.method_calls) == before, "命中缓存不该再调用 GDI+"
print("  ✅ 估算退路可用，缓存命中后不再碰 GDI+")

print()
print("=" * 66)
print("全部逻辑验证通过 ✅")
print("=" * 66)
