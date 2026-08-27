"""
配置（带合理默认值，可被 config.yaml 覆盖）
"""
import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
import yaml


class DeliveryMode(Enum):
    OVERLAY = "overlay"     # 隐形浮层（主交付，照着抄）
    CLIPBOARD = "clipboard" # 剪贴板（辅助/兼容）


@dataclass
class Config:
    # --- 截屏 ---
    region: tuple[int, int, int, int] | None = None  # (left, top, right, bottom)，None=全屏
    capture_backend: str = "mss"                     # mss | wgc
    monitor: int = 1                                 # 截图显示器：1=主屏 2=副屏… 0=全部合并

    # --- OCR ---
    ocr_lang: str = "ch"             # PaddleOCR 语言代码（ch=中英文混合）
    ocr_cpu_threads: int = 4         # CPU 推理线程数（过大 OCR 时鼠标会卡）

    # --- 解答 ---
    answer_mode: str = "api"         # api | none
    api_key: str = ""                # Claude API Key
    api_url: str = ""                # API 端点 URL
    api_model: str = ""              # 模型名称
    # 让模型别做深度思考：答题要的是「马上出字、边看边抄」，思考链再漂亮也
    # 是干等（实测首个正文字 21s → 6s）。False=不干预，模型爱想多久想多久
    api_no_thinking: bool = True

    # --- 投递（核心：默认浮层） ---
    delivery: DeliveryMode = DeliveryMode.OVERLAY

    # --- 浮层外观 ---
    overlay_size: tuple[int, int] = (820, 900)
    overlay_bg_alpha: int = 165       # 背板不透明度 0-255
    overlay_bg_color: tuple[int, int, int] = (20, 22, 30)
    overlay_text_color: tuple[int, int, int] = (235, 235, 235)
    overlay_font_size: int = 18               # 字号（像素）
    overlay_line_height: int | None = None    # 行高，None=按字号自动推算
    overlay_font_name: str = "Consolas"       # 等宽字体，找不到会退回系统默认
    overlay_wrap: bool = True                 # 长行软换行（False=超出宽度就裁掉）
    window_fake_title: str = "Windows Defender SmartScreen"  # 伪装标题

    # --- 浮层位置 ---
    # pos 显式钉死位置（(x, y) 屏幕坐标）；None=按截图屏右上角摆放
    overlay_pos: tuple[int, int] | None = None
    # 记住手动拖动/停靠后的位置，下次启动沿用（overlay_pos 已钉死时不生效）
    overlay_remember_pos: bool = True

    # --- 隐蔽 ---
    fake_process_name: str = "msbuild.exe"   # build.bat 打包时用的伪装名
    no_focus_steal: bool = True              # 弹出时不抢焦点

    # --- 热键（RegisterHotKey 的 修饰键 + 单键，见 parse_hotkey）---
    hotkey_solve: str = "Ctrl+Alt+Q"
    hotkey_toggle: str = "Ctrl+Alt+V"
    hotkey_clear: str = "Ctrl+Alt+C"
    hotkey_quit: str = "Ctrl+Alt+X"
    hotkey_monitor: str = "Ctrl+Alt+M"
    hotkey_append: str = "Ctrl+Alt+A"
    hotkey_dock: str = "Ctrl+Alt+W"
    # 观感微调：字号 +/-，背板透明度 [/]（现场调一下就好，不必改配置重启）
    hotkey_font_up: str = "Ctrl+Alt+="
    hotkey_font_down: str = "Ctrl+Alt+-"
    hotkey_alpha_down: str = "Ctrl+Alt+["
    hotkey_alpha_up: str = "Ctrl+Alt+]"
    # 尺寸微调：加 Shift 与「移动浮层」的方向键区分开（Ctrl+Alt+方向键留给移动）
    hotkey_width_down: str = "Ctrl+Alt+Shift+Left"
    hotkey_width_up: str = "Ctrl+Alt+Shift+Right"
    hotkey_height_down: str = "Ctrl+Alt+Shift+Up"
    hotkey_height_up: str = "Ctrl+Alt+Shift+Down"
    # 键盘微调位置（纯键盘也能摆浮层）。Intel 显卡驱动默认拿 Ctrl+Alt+方向键
    # 做屏幕旋转、IntelliJ 系也占，所以这四个键的回退链在 main.py 里备好了
    hotkey_move_left: str = "Ctrl+Alt+Left"
    hotkey_move_right: str = "Ctrl+Alt+Right"
    hotkey_move_up: str = "Ctrl+Alt+Up"
    hotkey_move_down: str = "Ctrl+Alt+Down"


# ---------------------------------------------------------------------------
# 热键字符串 ⇄ (modifiers, virtual-key)
# ---------------------------------------------------------------------------
# RegisterHotKey 的修饰键位（winuser.h）
MOD_ALT, MOD_CTRL, MOD_SHIFT, MOD_WIN = 0x0001, 0x0002, 0x0004, 0x0008

_MODS = {
    "ctrl": MOD_CTRL, "control": MOD_CTRL, "ctl": MOD_CTRL,
    "alt": MOD_ALT, "shift": MOD_SHIFT,
    "win": MOD_WIN, "super": MOD_WIN, "meta": MOD_WIN, "cmd": MOD_WIN,
}
_MOD_ORDER = [(MOD_CTRL, "Ctrl"), (MOD_ALT, "Alt"), (MOD_SHIFT, "Shift"), (MOD_WIN, "Win")]

# 常用非字母键的虚拟键码（winuser.h 的 VK_*）
_VKS = {
    "space": 0x20, "enter": 0x0D, "return": 0x0D, "tab": 0x09,
    "esc": 0x1B, "escape": 0x1B, "backspace": 0x08,
    "delete": 0x2E, "del": 0x2E, "insert": 0x2D, "ins": 0x2D,
    "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "-": 0xBD, "minus": 0xBD, "=": 0xBB, "plus": 0xBB, "equal": 0xBB,
    "[": 0xDB, "]": 0xDD, ";": 0xBA, "'": 0xDE, "`": 0xC0,
    ",": 0xBC, ".": 0xBE, "/": 0xBF, "\\": 0xDC,
}
_VK_TO_NAME = {
    0x20: "Space", 0x0D: "Enter", 0x09: "Tab", 0x1B: "Esc", 0x08: "Backspace",
    0x2E: "Delete", 0x2D: "Insert", 0x24: "Home", 0x23: "End",
    0x21: "PageUp", 0x22: "PageDown",
    0x26: "Up", 0x28: "Down", 0x25: "Left", 0x27: "Right",
    0xBD: "-", 0xBB: "=", 0xDB: "[", 0xDD: "]", 0xBA: ";", 0xDE: "'",
    0xC0: "`", 0xBC: ",", 0xBE: ".", 0xBF: "/", 0xDC: "\\",
}


def parse_hotkey(s: str) -> tuple[int, int] | None:
    """`"Ctrl+Alt+Q"` → `(MOD_CTRL|MOD_ALT, 0x51)`；解析不了返回 None。

    支持 Ctrl/Alt/Shift/Win（别名 Control、Super、Cmd…）+ 一个主键：
    字母、数字、F1-F24、方向键等命名键、常见符号键。大小写与空格随意。

    刻意要求至少带一个修饰键：RegisterHotKey 允许注册裸键，但那会在全局
    吞掉这个按键，打字都打不出来 —— 属于配错了就没法收场的那种坑。
    """
    if not isinstance(s, str) or not s.strip():
        return None
    parts = [p.strip().lower() for p in s.replace("＋", "+").split("+") if p.strip()]
    if not parts:
        return None
    mods, key = 0, None
    for p in parts:
        if p in _MODS:
            mods |= _MODS[p]
        elif key is None:
            key = p
        else:
            return None          # 出现第二个主键，写错了
    if key is None or mods == 0:
        return None
    if len(key) == 1 and key.isascii() and key.isalnum():
        return mods, ord(key.upper())
    if key in _VKS:
        return mods, _VKS[key]
    if key.startswith("f") and key[1:].isdigit() and 1 <= int(key[1:]) <= 24:
        return mods, 0x70 + int(key[1:]) - 1   # VK_F1 = 0x70
    return None


def format_hotkey(mods: int, vk: int) -> str:
    """(modifiers, vk) → `"Ctrl+Alt+Q"`，用于打印「实际生效的键」。"""
    names = [n for bit, n in _MOD_ORDER if mods & bit]
    if vk in _VK_TO_NAME:
        names.append(_VK_TO_NAME[vk])
    elif 0x70 <= vk <= 0x87:
        names.append(f"F{vk - 0x70 + 1}")
    else:
        names.append(chr(vk))
    return "+".join(names)


def hotkey_candidates(raw: str, default: str,
                      fallbacks: tuple[str, ...] = ()) -> list[tuple[int, int]]:
    """按尝试顺序给出要注册的 (modifiers, vk) 列表，第一个是「本来想要的那个」。

    只有**还在用默认键**时才带上回退候选：Ctrl+Alt+方向键 被 Intel 显卡驱动
    （屏幕旋转）占用、M/A/W 被微信/QQ 占用都是常态，默认键换一个总比功能没了好。
    但用户显式写了什么就只注册什么 —— 悄悄换成别的键，是「我配的键怎么变了」
    这类最难排查的问题的来源。

    raw 解析不了 → 退回 default（并按「在用默认键」处理，带上回退链）；
    解析不了或与已有候选重复的 fallback 直接跳过。

    「是不是默认键」比的是解析后的 (mod, vk)，不是字符串：
    `Alt+Ctrl+left` 和 `Ctrl+Alt+Left` 是同一个键，用户那么写也算在用默认键。
    """
    combo = parse_hotkey(raw)
    if combo is None:
        combo = parse_hotkey(default)
    if combo is None:                       # default 也写坏了（只会是代码 bug）
        return []
    out = [combo]
    if combo == parse_hotkey(default):
        for fb in fallbacks:
            c = parse_hotkey(fb)
            if c and c not in out:
                out.append(c)
    return out


def _load_api_from_keyfile(cfg: Config, config_path: str) -> Config:
    """从 aiKey.txt 自动加载 API 配置（exe 旁 / config.yaml 同级 / 项目根目录）。"""
    import sys
    search_dirs = []
    # 打包成 exe 后：exe 所在目录
    if getattr(sys, "frozen", False):
        search_dirs.append(Path(sys.executable).parent)
    # config.yaml / 源码所在目录及其上级
    search_dirs.extend([
        Path(config_path).parent,
        Path(config_path).parent.parent,
        Path(__file__).parent,
        Path(__file__).parent.parent,
    ])
    for d in search_dirs:
        keyfile = d / "aiKey.txt"
        if keyfile.exists():
            try:
                content = keyfile.read_text(encoding="utf-8")
                for line in content.splitlines():
                    line = line.strip()
                    if line.startswith("apiKey="):
                        cfg.api_key = line[len("apiKey="):]
                    elif line.startswith("url="):
                        cfg.api_url = line[len("url="):]
                    elif line.startswith("模型：") or line.startswith("模型:"):
                        cfg.api_model = line.split("：", 1)[-1].split(":", 1)[-1].strip()
                return cfg
            except Exception:
                pass
    return cfg


def _color(v, default: tuple[int, int, int]) -> tuple[int, int, int]:
    """把 YAML 里的 [r, g, b] 转成合法颜色元组；写错就退回默认值。

    颜色最终要进 GDI+ 的 ARGB 位运算，非 int / 长度不对会在 ctypes 里报
    莫名其妙的错，所以在这里挡掉，而不是让它一路传到渲染层。
    """
    if not isinstance(v, (list, tuple)) or len(v) != 3:
        return default
    try:
        return tuple(max(0, min(255, int(c))) for c in v)  # type: ignore[return-value]
    except (TypeError, ValueError):
        return default


def _int(v, default, lo: int, hi: int):
    """YAML 里的整数项：写错/留空退回 default，越界夹到 [lo, hi]。"""
    if v is None:
        return default
    try:
        return max(lo, min(hi, int(v)))
    except (TypeError, ValueError):
        return default


def load_config(path: str = "config.yaml") -> Config:
    cfg = Config()
    p = Path(path)
    if not p.exists():
        # 即使没有 config.yaml，也尝试从 aiKey.txt 加载 API 配置
        cfg = _load_api_from_keyfile(cfg, path)
        return cfg
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception:
        return cfg

    def get(key, default):
        return data.get(key, default)

    cfg.region = get("region", cfg.region)
    cfg.capture_backend = get("capture_backend", cfg.capture_backend)
    cfg.monitor = int(get("monitor", cfg.monitor))
    cfg.ocr_lang = get("ocr_lang", cfg.ocr_lang)
    cfg.ocr_cpu_threads = int(get("ocr_cpu_threads", cfg.ocr_cpu_threads))
    cfg.answer_mode = get("answer_mode", cfg.answer_mode)

    # API 配置（优先从 config.yaml 读，兜底从 aiKey.txt 读）
    api = data.get("api", {})
    cfg.api_key = api.get("key", cfg.api_key)
    cfg.api_url = api.get("url", cfg.api_url)
    cfg.api_model = api.get("model", cfg.api_model)
    cfg.api_no_thinking = bool(api.get("no_thinking", cfg.api_no_thinking))
    if not cfg.api_key:
        cfg = _load_api_from_keyfile(cfg, path)

    delivery = get("delivery", "overlay")
    cfg.delivery = DeliveryMode(delivery) if delivery else cfg.delivery

    ov = data.get("overlay", {})
    # size 写错（不是两个正整数）就退回默认：这个值会一路传到 CreateWindowEx，
    # 在那里报错比在这里退回默认难查得多。下限跟 overlay.MIN_W/MIN_H 一致
    size = ov.get("size")
    if isinstance(size, (list, tuple)) and len(size) == 2:
        try:
            cfg.overlay_size = (max(240, int(size[0])), max(120, int(size[1])))
        except (TypeError, ValueError):
            print(f"[config] overlay.size={size!r} 无法解析，用默认 {cfg.overlay_size}")
    elif size is not None:
        print(f"[config] overlay.size 应写成 [宽, 高]，用默认 {cfg.overlay_size}")
    cfg.overlay_bg_alpha = max(0, min(255, int(ov.get("bg_alpha", cfg.overlay_bg_alpha))))
    cfg.overlay_bg_color = _color(ov.get("bg_color"), cfg.overlay_bg_color)
    cfg.overlay_text_color = _color(ov.get("text_color"), cfg.overlay_text_color)
    cfg.overlay_font_size = _int(ov.get("font_size"), cfg.overlay_font_size, 8, 96)
    # 行高允许留空/写 0 → 跟着字号自动走（overlay.set_font 里推算）
    cfg.overlay_line_height = _int(ov.get("line_height"), None, 8, 200)
    name = ov.get("font_name")
    cfg.overlay_font_name = name.strip() if isinstance(name, str) and name.strip() \
        else cfg.overlay_font_name
    cfg.overlay_wrap = bool(ov.get("wrap", cfg.overlay_wrap))
    cfg.window_fake_title = ov.get("fake_title", cfg.window_fake_title)
    pos = ov.get("pos", cfg.overlay_pos)
    # pos 写错（不是两个数）就当没配，别让浮层开到奇怪的地方
    cfg.overlay_pos = tuple(pos) if isinstance(pos, (list, tuple)) and len(pos) == 2 else None
    cfg.overlay_remember_pos = bool(ov.get("remember_pos", cfg.overlay_remember_pos))

    stealth = data.get("stealth", {})
    cfg.fake_process_name = stealth.get("fake_process_name", cfg.fake_process_name)
    cfg.no_focus_steal = stealth.get("no_focus_steal", cfg.no_focus_steal)

    hk = data.get("hotkeys", {})
    for k in ("solve", "toggle", "clear", "quit", "monitor", "append", "dock",
              "font_up", "font_down", "alpha_down", "alpha_up",
              "width_down", "width_up", "height_down", "height_up",
              "move_left", "move_right", "move_up", "move_down"):
        v = hk.get(k)
        if isinstance(v, str) and v.strip():
            setattr(cfg, f"hotkey_{k}", v.strip())

    return cfg


if __name__ == "__main__":
    print(load_config())
