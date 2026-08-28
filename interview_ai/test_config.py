"""
config.py 的纯函数单测（任何平台可跑，不碰 Win32、不读真实配置）
================================================================

这里只测那几组「配错了也不该崩」的解析函数：

  · parse_hotkey / format_hotkey —— 热键字符串 ⇄ (modifiers, vk)
  · _color / _int                —— YAML 里的 [r, g, b] 与整数项
  · input_mode / image 段        —— 输入模式（截图直发 ⇄ 本地 OCR）与截图编码参数

它们的输入全部来自用户手写的 config.yaml，是最容易写错的地方；
错值一路传下去会在 RegisterHotKey / GDI+ 的 ctypes 层报莫名其妙的错，
所以解析层必须自己挡住，并且有测试盯着。
"""
import io
import sys

if sys.platform == "win32":
    # 中文 Windows 控制台默认 GBK，✅/❌ 会直接 UnicodeEncodeError。
    # 已经是 utf-8 就别再包一层：新 wrapper 顶掉旧的，旧的被回收时连底下的
    # buffer 一起关闭，之后所有 print 都 ValueError（同进程跑多个测试会踩到）
    if (getattr(sys.stdout, "encoding", "") or "").lower().replace("-", "") != "utf8":
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from config import (  # noqa: E402
    MOD_ALT, MOD_CTRL, MOD_SHIFT, MOD_WIN,
    Config, _color, _int, format_hotkey, parse_hotkey,
)

CA = MOD_CTRL | MOD_ALT
_fails: list[str] = []


def eq(label, got, want):
    ok = got == want
    print(f"  {'✅' if ok else '❌'} {label}: {got!r}" + ("" if ok else f" ≠ {want!r}"))
    if not ok:
        _fails.append(label)


def main():
    print("=" * 66)
    print("一、parse_hotkey 正常输入")
    print("=" * 66)
    for s, want in [
        ("Ctrl+Alt+Q", (CA, 0x51)),
        ("ctrl+alt+q", (CA, 0x51)),            # 大小写随意
        (" CTRL + ALT + Q ", (CA, 0x51)),      # 空格随意
        ("Ctrl＋Alt＋Q", (CA, 0x51)),           # 中文输入法的全角加号
        ("Ctrl+Shift+F5", (MOD_CTRL | MOD_SHIFT, 0x74)),
        ("Ctrl+F24", (MOD_CTRL, 0x87)),
        ("Win+Alt+Space", (MOD_WIN | MOD_ALT, 0x20)),
        ("Ctrl+Alt+Up", (CA, 0x26)),
        ("Control+Alt+Esc", (CA, 0x1B)),
        ("Ctrl+Alt+3", (CA, 0x33)),
        ("Ctrl+Alt+=", (CA, 0xBB)),
        ("Ctrl+Alt+plus", (CA, 0xBB)),
        ("Ctrl+Alt+[", (CA, 0xDB)),
    ]:
        eq(f"parse {s!r}", parse_hotkey(s), want)

    print()
    print("=" * 66)
    print("二、parse_hotkey 非法输入必须返回 None（不能抛异常）")
    print("=" * 66)
    # "Q" / "Ctrl+Alt" 也算非法：裸键会在全局吞掉这个按键，连字都打不出来，
    # 属于配错了就没法收场的坑，宁可退回默认值。
    for s in ["", "   ", "Q", "Ctrl", "Ctrl+Alt", "Ctrl+Alt+QQ", "Ctrl+Alt+Q+W",
              "Ctrl+Alt+F0", "Ctrl+Alt+F25", "Ctrl+Alt+爪", None, 123, ["Ctrl", "Q"]]:
        eq(f"parse {s!r} → None", parse_hotkey(s), None)

    print()
    print("=" * 66)
    print("三、format_hotkey（启动时打印「实际生效的键」用它）")
    print("=" * 66)
    eq("format 字母键", format_hotkey(CA, 0x4D), "Ctrl+Alt+M")
    eq("format 功能键", format_hotkey(MOD_CTRL, 0x70), "Ctrl+F1")
    eq("format 命名键", format_hotkey(CA, 0x26), "Ctrl+Alt+Up")
    eq("format 全修饰键", format_hotkey(CA | MOD_SHIFT | MOD_WIN, 0x70),
       "Ctrl+Alt+Shift+Win+F1")
    for s in ["Ctrl+Alt+Q", "Ctrl+Shift+F5", "Ctrl+Alt+Up", "Ctrl+Alt+="]:
        eq(f"往返一致 {s!r}", parse_hotkey(format_hotkey(*parse_hotkey(s))), parse_hotkey(s))

    print()
    print("=" * 66)
    print("四、Config 里的默认热键必须都能被自己解析")
    print("=" * 66)
    d = Config()
    for name in ("solve", "toggle", "clear", "quit", "monitor", "append", "dock"):
        raw = getattr(d, f"hotkey_{name}")
        eq(f"默认 hotkey_{name} = {raw}", parse_hotkey(raw) is not None, True)

    print()
    print("=" * 66)
    print("五、_color：写错退回默认值，分量夹到 0-255")
    print("=" * 66)
    DEF = (20, 22, 30)
    eq("正常值", _color([10, 200, 30], DEF), (10, 200, 30))
    eq("元组也认", _color((1, 2, 3), DEF), (1, 2, 3))
    eq("越界分量夹住", _color([300, -5, 12.7], DEF), (255, 0, 12))
    eq("长度不对 → 默认", _color([10, 20], DEF), DEF)
    eq("字符串 → 默认", _color("white", DEF), DEF)
    eq("None → 默认", _color(None, DEF), DEF)
    eq("非数字 → 默认", _color(["a", "b", "c"], DEF), DEF)

    print()
    print("=" * 66)
    print("六、_int：越界夹住、写错退回默认（字号 / 行高 用它）")
    print("=" * 66)
    eq("正常值", _int(24, 18, 8, 96), 24)
    eq("字符串数字也认", _int("24", 18, 8, 96), 24)
    eq("超上限夹住", _int(500, 18, 8, 96), 96)
    eq("低于下限夹住", _int(1, 18, 8, 96), 8)
    eq("None → 默认", _int(None, 18, 8, 96), 18)
    eq("None → 默认可以是 None", _int(None, None, 8, 96), None)
    eq("写错 → 默认", _int("大一点", 18, 8, 96), 18)

    print()
    print("=" * 66)
    print("七、load_config 读浮层字体项（写错不崩、越界夹住）")
    print("=" * 66)
    import os                       # noqa: E402  局部 import，前面几节不需要
    import tempfile                 # noqa: E402
    from config import load_config   # noqa: E402

    def load(yaml_text):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "config.yaml")
        with open(p, "w", encoding="utf-8") as f:
            f.write(yaml_text)
        return load_config(p)

    c = load("overlay:\n  font_size: 28\n  line_height: 40\n  font_name: 微软雅黑\n")
    eq("font_size", c.overlay_font_size, 28)
    eq("line_height", c.overlay_line_height, 40)
    eq("font_name", c.overlay_font_name, "微软雅黑")

    c = load("overlay:\n  font_size: 999\n  line_height: 不填数字\n  font_name: '  '\n")
    eq("font_size 越界夹住", c.overlay_font_size, 96)
    eq("line_height 写错 → None（跟着字号走）", c.overlay_line_height, None)
    eq("font_name 空白 → 默认", c.overlay_font_name, Config().overlay_font_name)

    c = load("overlay: {}\n")
    eq("没写就是默认字号", c.overlay_font_size, Config().overlay_font_size)
    eq("没写行高就是 None", c.overlay_line_height, None)
    eq("软换行默认开", c.overlay_wrap, True)

    eq("wrap: false 能关掉软换行", load("overlay:\n  wrap: false\n").overlay_wrap, False)

    print()
    print("=" * 66)
    print("八、load_config 读浮层尺寸（写错要退回默认，不能一路传到 CreateWindowEx）")
    print("=" * 66)
    eq("正常尺寸", load("overlay:\n  size: [640, 480]\n").overlay_size, (640, 480))
    eq("太小的尺寸夹到下限", load("overlay:\n  size: [10, 10]\n").overlay_size, (240, 120))
    eq("写成一个数 → 默认", load("overlay:\n  size: 800\n").overlay_size,
       Config().overlay_size)
    eq("写成三个数 → 默认", load("overlay:\n  size: [1, 2, 3]\n").overlay_size,
       Config().overlay_size)
    eq("写成文字 → 默认", load("overlay:\n  size: [宽, 高]\n").overlay_size,
       Config().overlay_size)
    eq("没写 → 默认", load("overlay: {}\n").overlay_size, Config().overlay_size)

    print()
    print("=" * 66)
    print("九、尺寸热键：默认键 + 能被 config 覆盖")
    print("=" * 66)
    d = Config()
    eq("默认宽度键带 Shift（与移动的方向键区分）",
       parse_hotkey(d.hotkey_width_up), (MOD_CTRL | MOD_ALT | MOD_SHIFT, 0x27))
    eq("默认高度键", parse_hotkey(d.hotkey_height_up),
       (MOD_CTRL | MOD_ALT | MOD_SHIFT, 0x28))
    eq("hotkeys.width_up 能被 config 覆盖",
       load("hotkeys:\n  width_up: Ctrl+Alt+F9\n").hotkey_width_up, "Ctrl+Alt+F9")

    print()
    print("=" * 66)
    print("十、hotkey_candidates：回退链只对「还在用默认键」的项生效")
    print("=" * 66)
    from config import hotkey_candidates   # noqa: E402

    CAS = MOD_CTRL | MOD_ALT | MOD_SHIFT
    FB = ("Ctrl+Alt+Shift+H", "Ctrl+Alt+B")
    eq("用默认键 → 带上回退候选",
       hotkey_candidates("Ctrl+Alt+Left", "Ctrl+Alt+Left", FB),
       [(CA, 0x25), (CAS, 0x48), (CA, 0x42)])
    eq("大小写/空格不同也算「还在用默认键」",
       hotkey_candidates(" ctrl + alt + left ", "Ctrl+Alt+Left", FB),
       [(CA, 0x25), (CAS, 0x48), (CA, 0x42)])
    eq("用户显式换了键 → 只试他写的那一个（不擅自换键）",
       hotkey_candidates("Ctrl+Alt+F9", "Ctrl+Alt+Left", FB), [(CA, 0x78)])
    eq("写错 → 退回默认，并按「在用默认键」带上回退链",
       hotkey_candidates("Ctrl+Alt+方向键", "Ctrl+Alt+Left", FB),
       [(CA, 0x25), (CAS, 0x48), (CA, 0x42)])
    eq("回退候选写坏/重复的跳过",
       hotkey_candidates("Ctrl+Alt+Left", "Ctrl+Alt+Left",
                         ("裸键", "Ctrl+Alt+Left", "Ctrl+Alt+B")),
       [(CA, 0x25), (CA, 0x42)])
    eq("没有回退候选就只有它自己",
       hotkey_candidates("Ctrl+Alt+Q", "Ctrl+Alt+Q"), [(CA, 0x51)])

    d = Config()
    eq("默认移动键是 Ctrl+Alt+方向键", parse_hotkey(d.hotkey_move_left), (CA, 0x25))
    eq("移动键与尺寸键不撞（尺寸那组带 Shift）",
       parse_hotkey(d.hotkey_move_left) != parse_hotkey(d.hotkey_width_down), True)
    names = ["solve", "toggle", "clear", "quit", "monitor", "append", "dock",
             "input_mode",
             "font_up", "font_down", "alpha_down", "alpha_up",
             "width_down", "width_up", "height_down", "height_up",
             "move_left", "move_right", "move_up", "move_down"]
    combos = [parse_hotkey(getattr(d, f"hotkey_{n}")) for n in names]
    eq("20 个默认热键两两不重复（否则后注册的必然失败）",
       len(set(combos)), len(names))
    eq("每个默认热键都解析得出来", all(c for c in combos), True)

    print()
    print("=" * 66)
    print("十一、输入模式：默认截图直发，OCR 一行配置切回来")
    print("=" * 66)
    from config import INPUT_MODES, IMAGE_FORMATS   # noqa: E402

    eq("默认就是截图直发（image）", Config().input_mode, "image")
    eq("只有两种模式，没有「都发」这一档", INPUT_MODES, ("image", "ocr"))
    eq("input_mode: ocr 一行切回本地 OCR",
       load("input_mode: ocr\n").input_mode, "ocr")
    # 手误不该让整个程序起不来 —— 按下热键才发现配错了更糟
    eq("写错 → 退回默认 image（打印一行提示，不抛异常）",
       load("input_mode: iamge\n").input_mode, "image")
    eq("留空 → 默认 image", load("input_mode:\n").input_mode, "image")

    print()
    print("=" * 66)
    print("十二、image 段：截图编码参数（越界夹住、写错退默认）")
    print("=" * 66)
    c = load("image:\n  max_side: 1200\n  format: png\n  quality: 95\n")
    eq("max_side", c.image_max_side, 1200)
    eq("format", c.image_format, "png")
    eq("quality", c.image_quality, 95)
    eq("format 写错 → 默认 webp",
       load("image:\n  format: bmp\n").image_format, "webp")
    eq("max_side 越界夹住", load("image:\n  max_side: 99999\n").image_max_side, 4096)
    eq("quality 越界夹住", load("image:\n  quality: 5\n").image_quality, 30)
    # 写了 `image:` 却什么都不填，yaml 给的是 None —— 直接 .get 会 AttributeError
    eq("空段等同于没写（不能崩）", load("image:\n").image_max_side,
       Config().image_max_side)
    eq("所有支持的格式都在编码表的键里", set(IMAGE_FORMATS),
       {"webp", "png", "jpeg", "jpg"})
    eq("输入模式切换热键默认 Ctrl+Alt+O",
       parse_hotkey(Config().hotkey_input_mode), (CA, 0x4F))
    eq("hotkeys.input_mode 能被 config 覆盖",
       load("hotkeys:\n  input_mode: Ctrl+Alt+F8\n").hotkey_input_mode, "Ctrl+Alt+F8")

    print()
    print("=" * 66)
    print("结论:", "全部通过 ✅" if not _fails else f"失败 {len(_fails)} 项 ❌ {_fails}")
    print("=" * 66)
    sys.exit(0 if not _fails else 1)


if __name__ == "__main__":
    main()
