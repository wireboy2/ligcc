"""
浮层移动功能验证（Windows 真机运行，不需要真的动鼠标）
============================================================================

为什么能这么测：
  拖动的判定逻辑被抽成了 StealthOverlay._handle_mouse(msg, x, y, ...)，
  钩子回调只是它的一层壳。所以这里直接喂合成的 WM_LBUTTONDOWN/MOUSEMOVE/
  LBUTTONUP 序列，再用 win32gui.GetWindowRect 读**真实窗口位置**核对 ——
  既覆盖了判定逻辑，也覆盖了 SetWindowPos 真的把窗口搬走了。

覆盖点：
  1. move() 真的移动窗口（隐藏状态下也生效）
  2. Ctrl+左键拖动 → 窗口跟随，事件被吃掉（不漏给下层）
  3. 不按 Ctrl 的左键 / 浮层外的点击 → 不拦截（保住点击穿透）
  4. 中键拖动 → 同样可移动
  5. 抬起事件丢失 → 拖动自动解卡，不会粘住鼠标
  6. 边界夹取：拖不出桌面（至少留 MIN_VISIBLE 可见）
  7. dock_next 循环停靠，位置都在显示器工作区内
  8. 位置记忆 save_pos/load_saved_pos 往返
  9. 滚轮：浮层内翻页并拦截；隐藏时不拦截（不抢下层页面的滚动）
 10. resize：真的改窗口尺寸、宽度变了重折长行、高度变了收回滚动偏移、
     下限/工作区夹取、尺寸落盘
 11. 状态反馈：进度/错误进浮层、提示里用实际生效的热键、模型缓存检测
 12. --duration 到点自动退出

运行：python test_move.py    （需要桌面会话 + pywin32）
"""
import os
import sys
import tempfile

# 控制台默认 GBK，✅/❌ 会 UnicodeEncodeError（与 main.py 同样处理）。
# 已经是 utf-8 就别再包一层（重复包会关掉底下的 buffer，见 main.py 同处注释）
try:
    import io
    if (getattr(sys.stdout, "encoding", "") or "").lower().replace("-", "") != "utf8":
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                      line_buffering=True)
except Exception:
    pass

import win32gui

import overlay as ov_mod
from overlay import (StealthOverlay, MIN_VISIBLE, WM_LBUTTONDOWN, WM_LBUTTONUP,
                     WM_MBUTTONDOWN, WM_MBUTTONUP, WM_MOUSEMOVE, WM_MOUSEWHEEL)

FAILED = []


def check(name: str, cond: bool, extra: str = ""):
    print(f"  {'✅' if cond else '❌'} {name}{('  ' + extra) if extra else ''}")
    if not cond:
        FAILED.append(name)


def rect(ov) -> tuple[int, int]:
    """真实窗口左上角（问系统，不是问对象自己的字段）。"""
    l, t, _r, _b = win32gui.GetWindowRect(ov._hwnd)
    return l, t


def wh(ov) -> tuple[int, int]:
    """真实窗口尺寸（同上，resize 要核对系统那边真的变了）。"""
    l, t, r, b = win32gui.GetWindowRect(ov._hwnd)
    return r - l, b - t


def section(title: str):
    print()
    print("=" * 70)
    print(title)
    print("=" * 70)


def _check_status_feedback():
    """App._status / _key 的行为（不建真 App：那会拉起 OCR、写 history）。

    只需要 self 上的 cfg / overlay / _hotkey_labels 三样，所以直接拿
    unbound 方法配一个假 self 调 —— 验证的是「进度和错误确实被送到
    set_text + show」，而这正是状态反馈的全部要求。
    """
    from types import SimpleNamespace
    from config import DeliveryMode
    from main import App
    import ocr as ocr_mod

    class _FakeOverlay:
        def __init__(self):
            self.text, self.shown = None, False

        def set_text(self, t):
            self.text = t

        def show(self):
            self.shown = True

    fake_ov = _FakeOverlay()
    me = SimpleNamespace(
        cfg=SimpleNamespace(delivery=DeliveryMode.OVERLAY, hotkey_monitor="Ctrl+Alt+M"),
        overlay=fake_ov,
        _hotkey_labels={"monitor": "Ctrl+Alt+N"},
    )
    App._status(me, "识别中…", "ocr")
    check("进度文本进了浮层", fake_ov.text == "识别中…", fake_ov.text)
    check("并且把浮层显示出来（之前可能是隐藏的）", fake_ov.shown)
    App._status(me, "截屏失败", "capture")
    check("错误也走同一条路（不再只 print）", fake_ov.text == "截屏失败")

    # 提示语里的按键要用实际生效的键：默认键被占用换过键时硬写就是错的
    check("_key 用实际注册成功的键", App._key(me, "monitor") == "Ctrl+Alt+N")
    me._hotkey_labels = {}
    check("没注册过（--once）退回配置值", App._key(me, "monitor") == "Ctrl+Alt+M")

    # clipboard 模式没有浮层，_status 不能炸
    clip = SimpleNamespace(cfg=SimpleNamespace(delivery=DeliveryMode.CLIPBOARD),
                           overlay=None, _hotkey_labels={})
    try:
        App._status(clip, "剪贴板模式只打印", "deliver")
        ok = True
    except Exception as e:
        ok = False
        print(f"    异常: {e}")
    check("clipboard 模式退化成只打印（不抛异常）", ok)

    # 模型缓存检测：这台机器上模型早就下好了，应当为 True
    check("models_cached 认出已下载的模型缓存", ocr_mod.models_cached() is True,
          os.environ.get("PADDLE_PDX_CACHE_HOME", "(未设置)"))
    old = os.environ.get("PADDLE_PDX_CACHE_HOME")
    os.environ["PADDLE_PDX_CACHE_HOME"] = tempfile.mkdtemp(prefix="empty_cache_")
    check("空缓存目录 → 提示要下 200MB", ocr_mod.models_cached() is False)
    if old is not None:
        os.environ["PADDLE_PDX_CACHE_HOME"] = old
    check("OCR 未加载时 loaded=False（懒加载）",
          ocr_mod.OCR(cpu_threads=1).loaded is False)


def _check_auto_quit():
    """--duration 到点自动退。

    同样不建真 App：_arm_auto_quit 只用到 self._running 和 self._quit，
    拿假 self 调就能验证「该开线程时开、该闭嘴时闭嘴」。
    """
    import time
    from types import SimpleNamespace
    from main import App

    calls = []
    me = SimpleNamespace(_running=True, _quit=lambda: calls.append(1))
    App._arm_auto_quit(me, None)
    App._arm_auto_quit(me, 0)
    time.sleep(0.3)
    check("--duration 没给/为 0 → 不装定时器", calls == [], repr(calls))

    App._arm_auto_quit(me, 0.2)
    time.sleep(0.6)
    check("到点调 _quit（投 WM_QUIT，浮层照常 destroy）", calls == [1], repr(calls))

    # 已经在退出了（用户按了退出键）就别再喊一次
    calls.clear()
    gone = SimpleNamespace(_running=False, _quit=lambda: calls.append(1))
    App._arm_auto_quit(gone, 0.2)
    time.sleep(0.5)
    check("已经在退出了就不重复触发", calls == [], repr(calls))


def main():
    # 状态记忆改到临时文件，别污染 history/overlay_state.json
    tmpdir = tempfile.mkdtemp(prefix="ov_pos_")
    ov_mod.STATE_FILE = os.path.join(tmpdir, "overlay_state.json")
    ov_mod.LEGACY_POS_FILE = os.path.join(tmpdir, "overlay_pos.json")

    ov = StealthOverlay(title="Windows Defender SmartScreen",
                        width=500, height=400, x=300, y=200,
                        remember_pos=True)
    try:
        ov.set_text("\n".join(f"line {i}" for i in range(60)))
        ov.show()

        section("一、move() 是否真的搬动了窗口")
        ov.move(400, 300, save=False)
        check("move(400,300) → GetWindowRect 一致", rect(ov) == (400, 300), str(rect(ov)))
        ov.hide()
        ov.move(420, 320, save=False)
        check("隐藏状态下 move 也生效（旧实现会静默忽略）",
              rect(ov) == (420, 320) and (ov.x, ov.y) == (420, 320), str(rect(ov)))
        ov.show()
        ov.move(400, 300, save=False)

        section("二、Ctrl+左键拖动")
        eaten_down = ov._handle_mouse(WM_LBUTTONDOWN, 450, 350, ctrl_down=True)
        check("浮层内 Ctrl+左键按下 → 进入拖动并拦截事件",
              eaten_down and ov.dragging)
        eaten_move = ov._handle_mouse(WM_MOUSEMOVE, 550, 420, button_down=True)
        check("移动 +100/+70 → 窗口同步跟随（按下点相对偏移不变）",
              eaten_move and rect(ov) == (500, 370), str(rect(ov)))
        eaten_up = ov._handle_mouse(WM_LBUTTONUP, 550, 420)
        check("左键抬起 → 结束拖动", eaten_up and not ov.dragging)
        check("松手后位置已落盘", ov_mod.load_saved_pos() == (500, 370),
              str(ov_mod.load_saved_pos()))

        section("三、不该拦截的情况（保住点击穿透）")
        before = rect(ov)
        check("浮层内不按 Ctrl 的左键 → 不拦截、不移动",
              ov._handle_mouse(WM_LBUTTONDOWN, 550, 400, ctrl_down=False) is False
              and not ov.dragging and rect(ov) == before)
        check("浮层外 Ctrl+左键 → 不拦截",
              ov._handle_mouse(WM_LBUTTONDOWN, 50, 50, ctrl_down=True) is False
              and not ov.dragging)
        check("非拖动状态下的鼠标移动 → 不拦截",
              ov._handle_mouse(WM_MOUSEMOVE, 550, 420) is False)

        section("四、中键拖动")
        ov.move(400, 300, save=False)
        check("浮层内中键按下 → 进入拖动",
              ov._handle_mouse(WM_MBUTTONDOWN, 450, 350) and ov.dragging)
        ov._handle_mouse(WM_MOUSEMOVE, 500, 400, button_down=True)
        check("中键拖动 → 窗口跟随", rect(ov) == (450, 350), str(rect(ov)))
        check("左键抬起不该结束中键拖动（按键要对得上）",
              ov._handle_mouse(WM_LBUTTONUP, 500, 400) is False and ov.dragging)
        check("中键抬起 → 结束拖动",
              ov._handle_mouse(WM_MBUTTONUP, 500, 400) and not ov.dragging)

        section("五、抬起事件丢失时自动解卡")
        ov._handle_mouse(WM_LBUTTONDOWN, 470, 370, ctrl_down=True)
        check("拖动中检测到按键其实已松开 → 结束拖动且不再拦截",
              ov._handle_mouse(WM_MOUSEMOVE, 600, 500, button_down=False) is False
              and not ov.dragging)

        section("六、边界夹取（拖不丢）")
        mons = ov._monitor_rects()
        for m in mons:
            print(f"  显示器: {m}")

        def visible_somewhere() -> bool:
            """窗口是否与某块显示器有 MIN_VISIBLE 以上重叠（横竖都要）。"""
            x, y = rect(ov)
            return any(min(x + ov.width, r) - max(x, l) >= MIN_VISIBLE
                       and min(y + ov.height, b) - max(y, t) >= MIN_VISIBLE
                       for l, t, r, b in mons)

        for tx, ty, desc in [(99999, 99999, "右下天边"),
                             (-99999, -99999, "左上天边"),
                             (99999, -99999, "右上天边"),
                             (-99999, 99999, "左下天边")]:
            ov.move(tx, ty, save=False)
            check(f"拖到{desc} → 仍有 {MIN_VISIBLE}px 露在某块屏上",
                  visible_somewhere(), str(rect(ov)))

        # 多屏分辨率/缩放不一致时，外接矩形内部会有"不属于任何显示器"的空洞。
        # 扫一遍找出这样的坐标（本机有 125% 主屏 + 100% 副屏，确实存在），
        # 验证按外接矩形夹取会漏掉的这类位置也被拉回来了。
        def raw_visible(x, y) -> bool:
            return any(min(x + ov.width, r) - max(x, l) >= MIN_VISIBLE
                       and min(y + ov.height, b) - max(y, t) >= MIN_VISIBLE
                       for l, t, r, b in mons)

        bl = min(m[0] for m in mons); bt = min(m[1] for m in mons)
        br = max(m[2] for m in mons); bb = max(m[3] for m in mons)
        hole = next(((x, y) for y in range(bt, bb, 40) for x in range(bl, br, 40)
                     if not raw_visible(x, y)), None)
        if hole:
            ov.move(*hole, save=False)
            check(f"拖进多屏空洞 {hole}（在外接矩形内却不在任何屏上）→ 被拉回屏内",
                  visible_somewhere(), str(rect(ov)))
        else:
            print("  ⏭ 本机显示器布局没有空洞，跳过空洞用例")

        section("七、dock_next 循环停靠")
        seen = []
        for _ in range(6):
            name = ov.dock_next()
            l, t, r, b = ov._current_work_area()
            inside = (l - 1 <= ov.x and ov.y >= t - 1
                      and ov.x + ov.width <= r + 1 and ov.y + ov.height <= b + 1)
            seen.append(name)
            check(f"停靠「{name}」落在工作区内", inside, f"({ov.x},{ov.y})")
        check("五个位置循环（第 6 次回到第 1 个）",
              seen[:5] == ["右上", "右下", "左下", "左上", "居中"] and seen[5] == seen[0],
              str(seen))

        section("八、状态记忆往返（位置 + 字号/透明度）")
        ov_mod.save_pos(1234, 567)
        check("save_pos → load_saved_pos 往返一致",
              ov_mod.load_saved_pos() == (1234, 567))
        ov_mod.save_state(font_size=22, bg_alpha=200)
        st = ov_mod.load_state()
        check("字号/透明度写入后位置字段没被清掉（合并写而不是整体覆盖）",
              (st.get("x"), st.get("y"), st.get("font_size"), st.get("bg_alpha"))
              == (1234, 567, 22, 200), str(st))
        bad = os.path.join(tmpdir, "broken.json")
        with open(bad, "w", encoding="utf-8") as f:
            f.write("{not json")
        old_s, old_l = ov_mod.STATE_FILE, ov_mod.LEGACY_POS_FILE
        ov_mod.STATE_FILE = ov_mod.LEGACY_POS_FILE = bad
        check("文件损坏 → 返回 None（调用方退回默认位置）",
              ov_mod.load_saved_pos() is None)
        # 老版本只有 overlay_pos.json：读得到就迁移过来，位置不能丢
        legacy = os.path.join(tmpdir, "legacy.json")
        with open(legacy, "w", encoding="utf-8") as f:
            f.write('{"x": 11, "y": 22}')
        ov_mod.STATE_FILE, ov_mod.LEGACY_POS_FILE = os.path.join(tmpdir, "none.json"), legacy
        check("只有旧 overlay_pos.json 时仍能读出位置（升级不丢）",
              ov_mod.load_saved_pos() == (11, 22))
        ov_mod.STATE_FILE, ov_mod.LEGACY_POS_FILE = old_s, old_l

        section("八之二、观感热键：字号 / 透明度")
        n_before, size_before = len(ov._lines), ov.font_size
        check("bump_font(+4) → 字号变大、行高跟着变、长行重折",
              ov.bump_font(4) == size_before + 4 and ov.line_height > size_before + 4)
        check("bump_font(-4) → 回到原字号", ov.bump_font(-4) == size_before)
        check("折行结果随字号来回变（行数复原）", len(ov._lines) == n_before,
              f"{n_before} → {len(ov._lines)}")
        check("字号夹在 [MIN,MAX] 内（连按不会调成 0 或天文数字）",
              ov.bump_font(-999) == ov.MIN_FONT_SIZE
              and ov.bump_font(999) == ov.MAX_FONT_SIZE)
        ov.set_font(size=size_before)
        a0 = ov.bg_alpha
        check("bump_alpha(+15) / (-15) 往返", ov.bump_alpha(15) == min(255, a0 + 15)
              and ov.bump_alpha(-15) == a0)
        check("不透明度夹在 [0,255]",
              ov.bump_alpha(-999) == 0 and ov.bump_alpha(999) == 255)
        ov.bump_alpha(a0 - ov.bg_alpha)
        check("调过的字号/透明度已落盘（下次启动沿用）",
              ov_mod.load_state().get("bg_alpha") == a0
              and ov_mod.load_state().get("font_size") == size_before,
              str(ov_mod.load_state()))

        section("八之三、resize：尺寸热键")
        ov.move(400, 300, save=False)
        ov.set_text("\n".join(f"line {i}" for i in range(60)))
        w0, h0 = ov.width, ov.height
        vis0 = ov.visible_lines()
        w, h = ov.resize(w0 + 100, h0 + 100, save=False)
        check("resize 改到的尺寸 = GetWindowRect 的尺寸", (w, h) == wh(ov), f"{wh(ov)}")
        check("变高 → 一屏能放更多行", ov.visible_lines() > vis0,
              f"{vis0} → {ov.visible_lines()}")
        ov.resize(w0, h0, save=False)
        check("缩回去 → 一屏行数复原", ov.visible_lines() == vis0 and wh(ov) == (w0, h0))
        check("bump_size 走同一条路", ov.bump_size(-60, 0)[0] == w0 - 60)
        ov.bump_size(+60, 0)

        # 宽度变了必须重折长行：不重折就等于右边一截空白（或又被裁掉）
        ov.set_text("咖啡" * 200)
        n_wide = len(ov._lines)
        ov.resize(w0 // 2, h0, save=False)
        n_narrow = len(ov._lines)
        check("变窄 → 长行按新宽度重折（行数变多）", n_narrow > n_wide,
              f"{n_wide} → {n_narrow}")
        ov.resize(w0, h0, save=False)
        check("变回原宽 → 折行行数复原", len(ov._lines) == n_wide)

        # 高度变小时，原来的滚动偏移可能越界 → 会停在一屏空白上
        ov.set_text("\n".join(f"line {i}" for i in range(60)))
        ov.scroll(999)
        off_before = ov._scroll_offset
        ov.resize(w0, h0 + 400, save=False)
        check("变高后滚动偏移被收回（不会停在一屏空白上）",
              ov._scroll_offset <= max(0, len(ov._lines) - ov.visible_lines())
              and ov._scroll_offset < off_before,
              f"{off_before} → {ov._scroll_offset}")
        ov.resize(w0, h0, save=False)

        check("尺寸夹在下限以上（连按变小不会缩成 0）",
              ov.resize(1, 1, save=False) == (ov.MIN_W, ov.MIN_H), str(wh(ov)))
        big = ov.resize(99999, 99999, save=False)
        wl, wt_, wr, wb = ov._current_work_area()
        check("尺寸夹在显示器工作区内（拖大了还得看得见）",
              big == wh(ov) and big[0] <= wr - wl and big[1] <= wb - wt_, str(big))
        ov.resize(w0, h0, save=True)
        check("尺寸落盘（下次启动沿用）", ov_mod.load_saved_size() == (w0, h0),
              str(ov_mod.load_saved_size()))
        check("写尺寸没清掉位置字段",
              ov_mod.load_saved_pos() == (ov.x, ov.y), str(ov_mod.load_state()))

        section("九、滚轮翻页（与拖动共用同一个钩子）")
        ov.move(400, 300, save=False)
        ov._scroll_offset = 0
        check("浮层内向下滚 → 翻页并拦截",
              ov._handle_mouse(WM_MOUSEWHEEL, 450, 350, delta=-120)
              and ov._scroll_offset > 0, f"offset={ov._scroll_offset}")
        check("浮层外滚轮 → 不拦截",
              ov._handle_mouse(WM_MOUSEWHEEL, 50, 50, delta=-120) is False)
        ov.hide()
        check("浮层隐藏时滚轮不拦截（不抢下层页面的滚动）",
              ov._handle_mouse(WM_MOUSEWHEEL, 450, 350, delta=-120) is False)
        ov.show()

        section("十、钩子安装 / 卸载（真机装一次，立刻卸掉）")
        hooked = ov.install_mouse_hook()
        check("install_mouse_hook 返回有效句柄", hooked and bool(ov._mouse_hook))
        check("重复安装是幂等的", ov.install_mouse_hook())
        for _ in range(5):   # 低级钩子要求安装线程持续泵消息
            win32gui.PumpWaitingMessages()
        ov.uninstall_mouse_hook()
        check("uninstall_mouse_hook 清掉句柄", ov._mouse_hook is None)
        check("旧名 install_wheel_scroll 仍可用（main.py 兼容）",
              ov.install_wheel_scroll() and bool(ov._mouse_hook))
        ov.uninstall_wheel_scroll()
        print("  ℹ 真实的「按住 Ctrl 拖动」需要人手操作：python overlay.py 自测")
    finally:
        ov.destroy()

    section("十一、状态反馈：进度/错误要落到浮层")
    _check_status_feedback()

    section("十二、--duration 到点自动退出")
    _check_auto_quit()

    print()
    print("=" * 70)
    if FAILED:
        print(f"存在问题 ❌  失败 {len(FAILED)} 项：")
        for f in FAILED:
            print(f"  - {f}")
    else:
        print("全部通过 ✅")
    print("=" * 70)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
