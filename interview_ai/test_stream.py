"""
流式作答（SSE）单测：解析、节流投递、中断保留
============================================================================

覆盖的都是「网络一抖就踩到、但手工几乎没法复现」的分支：

  · parse_sse_lines     —— 代理插的心跳/注释/[DONE]/坏 JSON 不能带走答案
  · collect_sse_answer  —— 正文累积、thinking 不进答案、中途断开保留半页
  · StreamSink          —— 上千个增量块必须节流，否则重绘把 CPU 烧光
  · _call_api           —— payload 带 stream + 关深度思考；端点不认这个字段
                           就去掉重来；端点无视 stream 直接吐整个 JSON 也要能读
  · 编程/算法题         —— OCR 关键词开思考、image 先判题型再思考、
                           Java/Scanner 作答要求进提示词
  · _answer_with_retry  —— 已流出半页时**不能**重试（会把用户正抄的内容推翻）
  · image_content       —— 图片模式的请求形状：图在前提示词在后，且**不带**
                           任何 OCR 文本（两种输入模式互斥）
  · App 接线            —— 增量投浮层时必须保住滚动位置

全程不出网、不建窗口：喂假的字节行流 + 假时钟 + 假响应对象。
需要 Windows 只是因为 import main 会连带 import overlay（ctypes.windll）。
"""
import io
import json
import sys
from types import SimpleNamespace

if sys.platform == "win32":
    # 中文 Windows 控制台默认 GBK，✅/❌ 会直接 UnicodeEncodeError。
    # 已经是 utf-8 就别再包一层（重复包会关掉底下的 buffer）
    if (getattr(sys.stdout, "encoding", "") or "").lower().replace("-", "") != "utf8":
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                      errors="replace")

from config import DeliveryMode                                      # noqa: E402
from dialect import ANTHROPIC, OPENAI, THINKING_BUDGET               # noqa: E402
from main import (AnswerProvider, ApiError, App, StreamInterrupted,   # noqa: E402
                  StreamSink, collect_sse_answer, looks_like_coding,
                  parse_sse_lines)

_fails: list[str] = []


def eq(label, got, want):
    ok = got == want
    print(f"  {'✅' if ok else '❌'} {label}: {got!r}" + ("" if ok else f" ≠ {want!r}"))
    if not ok:
        _fails.append(label)


def check(label, ok, extra=""):
    print(f"  {'✅' if ok else '❌'} {label}" + (f"  （{extra}）" if extra else ""))
    if not ok:
        _fails.append(label)


def section(title):
    print()
    print("=" * 70)
    print(title)
    print("=" * 70)


def sse(*events: dict) -> list[bytes]:
    """把事件对象编成 SSE 字节行 —— 带 event: 行和空行，跟真流一样。"""
    out = []
    for ev in events:
        out.append(f"event: {ev.get('type', 'x')}\n".encode())
        out.append(("data: " + json.dumps(ev, ensure_ascii=False) + "\n").encode())
        out.append(b"\n")
    return out


def sse_done(*events: dict) -> list[bytes]:
    """一轮**正常收尾**的流：事件 + message_stop。

    单独一个 helper 是因为「有没有收尾事件」现在是判断答案完整与否的依据，
    happy path 必须带上；故意不带的用例直接用 sse()（见三之二）。
    """
    return sse(*events, {"type": "message_stop"})


def sse_openai(*deltas: str, finish: bool = True) -> list[bytes]:
    """OpenAI 风格的流式字节：choices[0].delta.content + finish_reason。"""
    out = []
    for d in deltas:
        ev = {"id": "evt", "object": "chat.completion.chunk",
              "created": 1, "model": "m",
              "choices": [{"index": 0, "delta": {"content": d},
                           "finish_reason": None}]}
        out.append(("data: " + json.dumps(ev, ensure_ascii=False) + "\n").encode())
        out.append(b"\n")
    if finish:
        stop = {"id": "evt", "object": "chat.completion.chunk",
                "created": 1, "model": "m",
                "choices": [{"index": 0, "delta": {},
                             "finish_reason": "stop"}]}
        out.append(("data: " + json.dumps(stop, ensure_ascii=False) + "\n").encode())
        out.append(b"\n")
    return out


def delta(text=None, thinking=None) -> dict:
    """一个 content_block_delta 事件（正文块 或 思考块）。"""
    return {"type": "content_block_delta", "index": 0,
            "delta": {"text": text} if text is not None else {"thinking": thinking}}


class FakeResp:
    """假响应：够 _call_api 用的最小面 —— readline / read / 迭代 / close。"""

    def __init__(self, lines):
        self._lines = list(lines)
        self.closed = False

    def readline(self):
        return self._lines.pop(0) if self._lines else b""

    def read(self):
        rest, self._lines = b"".join(self._lines), []
        return rest

    def __iter__(self):
        while self._lines:
            yield self._lines.pop(0)

    def close(self):
        self.closed = True


def _fake_open_factory(sink: list, lines_for):
    """造一个记录 payload 的假 _open：第 n 次调用用 lines_for(n) 给字节行。"""

    def fake_open(payload, dialect=None, timeout=None):
        sink.append(dict(payload))
        return FakeResp(lines_for(len(sink)))

    return fake_open


def t_parse():
    section("一、parse_sse_lines：只认 data: 行，噪音一律跳过")
    lines = [
        b"event: message_start\n",                  # event: 行不能再数一遍
        b'data: {"type":"message_start"}\n',
        b"\n",                                      # 事件之间的空行
        b": keep-alive\n",                          # 代理的注释心跳
        b'data: {"type":"ping"}\n',
        b"data: \n",                                # 空 data
        b"data: {\xe5\x9d\x8f\xe4\xba\x86\n",       # 坏 JSON（只跳这一行）
        'data: ["不是对象"]\n'.encode(),             # 合法 JSON 但不是事件对象
        b'data: {"type":"message_stop"}\n',
    ]
    eq("只剩下真事件", [ev.get("type") for ev in parse_sse_lines(lines)],
       ["message_start", "ping", "message_stop"])
    eq("str 行也认（不只 bytes）",
       [ev["type"] for ev in parse_sse_lines(['data: {"type":"ping"}'])], ["ping"])
    # OpenAI 风格的 [DONE] 折成 message_stop：不折的话，用这种写法的端点
    # 每次都会被判成「没收到收尾事件 = 断流」，好答案全被标上连接中断
    eq("data: [DONE] 当成正常收尾",
       [ev["type"] for ev in parse_sse_lines([b"data: [DONE]\n"])], ["message_stop"])


def t_collect():
    section("二、collect_sse_answer：正文累积，thinking 不进答案")
    seen = []
    text, think = collect_sse_answer(
        sse({"type": "message_start"},
            delta(thinking="先想想"), delta(thinking="再想想"),
            delta("def f():"), delta("\n    return 42"),
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"}},
            {"type": "message_stop"}),
        seen.append)
    eq("答案里只有正文", text, "def f():\n    return 42")
    eq("thinking 只计字数、不进答案", think, 6)
    eq("每块正文回调一次，参数是累积到此刻的全文", seen,
       ["def f():", "def f():\n    return 42"])
    check("回调给的每一版都是最终文本的前缀（不会前后跳）",
          all(text.startswith(s) for s in seen))
    eq("空流不算错，交给上层判断", collect_sse_answer(sse({"type": "message_stop"})),
       ("", 0))
    # 有的端点只发 message_delta（带 stop_reason）就收尾，不发 message_stop
    eq("只有 message_delta 收尾也算正常结束",
       collect_sse_answer(sse(delta("答案"),
                             {"type": "message_delta",
                              "delta": {"stop_reason": "end_turn"}})),
       ("答案", 0))


def _broken(after: int):
    """吐 after 块正文后连接断掉（模拟代理掐线 / SSL 中断）。"""
    for i in range(after):
        yield ("data: " + json.dumps(delta(f"第{i}块"), ensure_ascii=False)).encode()
    raise ConnectionResetError("远端主机强迫关闭了一个现有的连接")


def t_interrupt():
    section("三、中途断流：保留已收到的半页，不当成「什么都没拿到」")
    try:
        collect_sse_answer(_broken(3))
        check("断流要抛 StreamInterrupted", False, "什么都没抛")
    except StreamInterrupted as e:
        eq("partial 里是断线前收到的全部正文", e.partial, "第0块第1块第2块")
        check("cause 留着原始异常", isinstance(e.cause, ConnectionResetError))
    except Exception as e:
        check("断流要抛 StreamInterrupted", False, f"抛的是 {type(e).__name__}")

    # 一个字都没收到 → 原样抛出。这才是「该重试」的情形，不能混进来
    try:
        collect_sse_answer(_broken(0))
        check("一个字都没收到时原样抛出", False, "什么都没抛")
    except StreamInterrupted:
        check("一个字都没收到时原样抛出（不该包成 StreamInterrupted）", False)
    except ConnectionResetError:
        check("一个字都没收到时原样抛出 ConnectionResetError", True)

    # 服务端在流里报错（overloaded 之类）同理：半页也要留住
    try:
        collect_sse_answer(sse(delta("半句"),
                               {"type": "error",
                                "error": {"type": "overloaded_error", "message": "忙"}}))
        check("error 事件要抛出来", False, "什么都没抛")
    except StreamInterrupted as e:
        eq("error 事件也保留半页", e.partial, "半句")
        check("错误原因带上了服务端的说法", "overloaded_error" in str(e), str(e))

    section("三之二、流「干净地」提前结束：也算断流，不能当完整答案交出去")
    # 实测过一次：这个代理答到 135 字就 EOF —— 没有异常、没有收尾事件。
    # 不检查收尾事件的话，用户拿到的是半句话，而且没有任何提示
    try:
        collect_sse_answer(sse(delta("答到一半就"), delta("没了")))
        check("缺收尾事件 → 当断流抛出", False, "静悄悄当成完整答案返回了")
    except StreamInterrupted as e:
        eq("半截答案留着", e.partial, "答到一半就没了")
        check("原因说清楚是提前结束", "提前结束" in str(e), str(e))


def t_callback_safety():
    section("四、投递回调自己抛异常，不能把答案带走")

    def boom(_):
        raise RuntimeError("浮层没了")

    text, _ = collect_sse_answer(sse_done(delta("a"), delta("b")), boom)
    eq("答案照样完整（改成收完再显示）", text, "ab")


def t_sink():
    section("五、StreamSink：按时间节流，首块不等")
    now = [1000.0]
    shown = []
    sink = StreamSink(shown.append, interval=0.1, clock=lambda: now[0])
    sink("a")
    eq("首块立刻投（用户的体感就是首字什么时候出现）", shown, ["a"])
    now[0] += 0.05
    sink("ab")
    eq("间隔没到就不投（省掉整篇重折行 + 重绘）", shown, ["a"])
    now[0] += 0.06
    sink("abc")
    eq("过了间隔投最新的全文（跳过的中间态不用补）", shown, ["a", "abc"])
    check("delivered 记着确实投过内容", sink.delivered is True)

    def bad_show(_):
        raise RuntimeError("UpdateLayeredWindow 失败")

    bad = StreamSink(bad_show, interval=0)
    bad("x")
    check("浮层刷新失败被吞掉，不会带走整轮作答", bad.delivered is False)


def t_call_api():
    section("六、_call_api：payload 带 stream + 关思考，读回流式正文")
    sent = []

    def fake_open(payload, dialect=None, timeout=None):
        sent.append(dict(payload))
        return FakeResp(sse_done(delta("hi"), delta(" there")))

    prov = AnswerProvider(api_key="k", api_url="http://x/v1/messages", model="m")
    prov._open = fake_open
    eq("正文拼回来了", prov._call_api("题目"), "hi there")
    check("payload 里 stream=True", sent[-1].get("stream") is True)
    eq("payload 里关掉深度思考", sent[-1].get("thinking"), {"type": "disabled"})

    prov2 = AnswerProvider(api_key="k", api_url="u", model="m", no_thinking=False)
    prov2._open = fake_open
    prov2._call_api("题目")
    check("no_thinking=False 就不干预（爱想多久想多久）", "thinking" not in sent[-1])

    # 端点不认 thinking 字段 → 去掉重来一次，并记住本次运行不再白试
    calls = []

    def picky_open(payload, dialect=None, timeout=None):
        calls.append(dict(payload))
        if "thinking" in payload:
            raise ApiError(400, '{"error":{"message":"thinking: unsupported parameter"}}')
        return FakeResp(sse_done(delta("ok")))

    prov3 = AnswerProvider(api_key="k", api_url="u", model="m")
    prov3._open = picky_open
    eq("去掉字段重来一次就拿到答案", prov3._call_api("题目"), "ok")
    eq("一共发了两次", len(calls), 2)
    prov3._call_api("下一题")
    eq("记住了端点的脾气，之后不再白试", len(calls), 3)
    check("后续请求确实不带这个字段", "thinking" not in calls[-1])

    # 与 thinking 无关的 400（模型名写错、余额不足）必须原样抛给重试逻辑
    prov4 = AnswerProvider(api_key="k", api_url="u", model="m")
    prov4._open = lambda payload, dialect=None, timeout=None: (
        (_ for _ in ()).throw(ApiError(400, "model not found")))
    try:
        prov4._call_api("题目")
        check("无关的 400 原样抛出", False, "被吞了")
    except ApiError as e:
        check("无关的 400 原样抛出，且带上响应体", "model not found" in str(e), str(e))

    # 端点无视 stream:true、直接吐整个 JSON 响应 → 按非流式读
    prov5 = AnswerProvider(api_key="k", api_url="u", model="m")
    body = json.dumps({"content": [{"type": "thinking", "thinking": "…"},
                                   {"type": "text", "text": "整段答案"}]},
                      ensure_ascii=False).encode()
    resp5 = FakeResp([body])
    prov5._open = lambda payload, dialect=None, timeout=None: resp5
    eq("非 SSE 响应也能读出正文", prov5._call_api("题目"), "整段答案")
    check("响应对象被关掉（不漏 socket）", resp5.closed is True)

    # 空流当失败抛出，否则浮层会永远停在「AI 作答中…」
    prov6 = AnswerProvider(api_key="k", api_url="u", model="m")
    prov6._open = lambda payload, dialect=None, timeout=None: FakeResp(
        sse({"type": "message_stop"}))
    try:
        prov6._call_api("题目")
        check("一个 text 增量都没有 → 当失败抛出", False, "静悄悄返回了空")
    except RuntimeError:
        check("一个 text 增量都没有 → 当失败抛出（上层会重试）", True)

    # 流答到一半就 EOF：StreamInterrupted 要穿到上层（那里负责加「连接中断」），
    # 并且响应对象照样关掉 —— 异常路径最容易漏 socket
    prov7 = AnswerProvider(api_key="k", api_url="u", model="m")
    resp7 = FakeResp(sse(delta("半截")))
    prov7._open = lambda payload, dialect=None, timeout=None: resp7
    try:
        prov7._call_api("题目")
        check("提前结束 → 抛 StreamInterrupted", False, "当成完整答案返回了")
    except StreamInterrupted as e:
        eq("半截答案带上来了", e.partial, "半截")
    check("异常路径也关掉了响应对象", resp7.closed is True)


def t_retry():
    section("七、重试语义：断流保留半页不重试，别的错误照旧重试 3 次")
    prov = AnswerProvider(api_key="k", api_url="u", model="m")
    tries = []

    def half(question, on_text=None, thinking=None, timeout=None):
        tries.append(1)
        raise StreamInterrupted("已经写了一半", ConnectionResetError("掐线"))

    prov._call_api = half
    out = prov._answer_with_retry("题目")
    check("保留了已经流出来的正文", out.startswith("已经写了一半"), out)
    check("末尾标一行「连接中断」", "连接中断，已保留 6 字" in out, out)
    eq("只调一次 —— 重试会把用户正抄的半页推翻重写", len(tries), 1)

    # 其它异常仍然重试 3 次（把 sleep 掉包，别真等 6 秒）
    import main as main_mod
    slept, real_sleep = [], main_mod.time.sleep
    main_mod.time.sleep = slept.append
    try:
        tries.clear()

        def boom(question, on_text=None, thinking=None, timeout=None):
            tries.append(1)
            raise ConnectionResetError("代理 403")

        prov._call_api = boom
        out = prov._answer_with_retry("题目")
    finally:
        main_mod.time.sleep = real_sleep
    eq("重试 3 次", len(tries), 3)
    eq("退避 2s、4s", slept, [2, 4])
    check("最后给出失败说明，题目还留着（不至于白识别一遍）",
          "【API 调用失败】" in out and "题目" in out)


def _shot(tag: str = "AAA", media: str = "image/webp"):
    """一张假截图（imaging.Shot 的最小可用实例，不碰 opencv）。"""
    from imaging import Shot
    return Shot(b64=tag, media_type=media, width=1568, height=882,
                nbytes=120 * 1024, fingerprint=tag, scaled=True)


def t_image_mode():
    section("八、图片模式：图在前、提示词在后，且绝不夹带 OCR 文本")
    # coding_thinking=False：题型判断单独在第十一节测，这里只看请求形状
    prov = AnswerProvider(api_key="k", api_url="u", model="m",
                          coding_thinking=False)

    one = prov.image_content([_shot("IMG1")])
    eq("单图两块：图 + 提示词", [b["type"] for b in one], ["image", "text"])
    eq("图片块是 base64 source", one[0]["source"]["type"], "base64")
    eq("media_type 跟着编码格式走", one[0]["source"]["media_type"], "image/webp")
    eq("图片数据就是编好的 base64", one[0]["source"]["data"], "IMG1")
    check("提示词在最后一块（官方建议单图这么放效果最好）",
          one[-1]["type"] == "text" and "答案" in one[-1]["text"])
    check("单图提示词要求忽略界面元素（地址栏/行号/状态栏那些噪音）",
          "行号" in one[-1]["text"] and "地址栏" in one[-1]["text"])

    two = prov.image_content([_shot("IMG1"), _shot("IMG2")])
    eq("多图：每张前面加一行【第 N 张】标签",
       [b["type"] for b in two], ["text", "image", "text", "image", "text"])
    eq("标签认得出第几张", two[0]["text"], "【第 1 张截图】")
    check("多图提示词说明是同一道题的不同部分（否则会被当成两道题）",
          "同一道题目" in two[-1]["text"])

    # 互斥的机器化确认：整个请求里不能出现任何「识别/OCR 文本」的痕迹
    texts = " ".join(b.get("text", "") for b in two if b["type"] == "text")
    check("图片请求里没有 OCR 识别文本（两种输入模式互斥）",
          "OCR" not in texts and "识别结果" not in texts)

    # 真正发出去的 payload 形状：content 必须是 blocks 列表，不能被拼成字符串
    sent = []

    def fake_open(payload, dialect=None, timeout=None):
        sent.append(payload)
        return FakeResp(sse_done(delta("答案")))

    prov._open = fake_open
    eq("图片模式也能正常拿到答案", prov.answer_from_shots([_shot("IMG1")]), "答案")
    content = sent[-1]["messages"][0]["content"]
    check("content 是 content blocks 列表（不是拼成一坨字符串）",
          isinstance(content, list))
    eq("列表里正好一张图", sum(1 for b in content if b["type"] == "image"), 1)
    check("图片模式同样关掉深度思考（答题要的是首字快）",
          sent[-1].get("thinking") == {"type": "disabled"})

    eq("一张图都没有 → 不发请求，返回空", prov.answer_from_shots([]), "")
    eq("None 也不算一张图", prov.answer_from_shots([None]), "")
    eq("没发请求（列表为空时压根不该出网）", len(sent), 1)

    # 图片模式失败时没有识别文本可回显，至少要说清「采了几张、没送出去」
    import main as main_mod
    real_sleep = main_mod.time.sleep
    main_mod.time.sleep = lambda *_: None
    try:
        prov._call_api = lambda c, on_text=None, thinking=None, timeout=None: (
            (_ for _ in ()).throw(ConnectionResetError("代理 403")))
        out = prov.answer_from_shots([_shot("IMG1"), _shot("IMG2")])
    finally:
        main_mod.time.sleep = real_sleep
    check("失败说明里带上「采了几张截图」", "2 张截图" in out, out)
    check("末尾是失败原因", "【API 调用失败】" in out, out)
    check("失败说明里不会回显 base64（那是几十万字符）", "IMG1" not in out)

    # 存档整理：图片模式本地没有题面，得让模型从截图里读出来
    refine = prov.image_content([_shot("IMG1")])[:-1]
    check("整理请求复用同一批图片块（末尾换成整理提示词）",
          len(refine) == 1 and refine[0]["type"] == "image")


class _FakeOverlay:
    def __init__(self):
        self.text, self.keep, self.shown = None, None, 0

    def set_text(self, text, keep_scroll=False):
        self.text, self.keep = text, keep_scroll

    def show(self):
        self.shown += 1


def t_app_wiring():
    section("九、App 接线：增量进浮层，收尾不把滚动位置归零")
    # 不建真 App（那会拉起 OCR、写 history）：这两个方法只用到 cfg 和 overlay
    ov = _FakeOverlay()
    me = SimpleNamespace(cfg=SimpleNamespace(delivery=DeliveryMode.OVERLAY), overlay=ov)

    sink = App._stream_sink(me)
    sink("半句")
    eq("增量投进了浮层", ov.text, "半句")
    check("增量必须 keep_scroll —— 否则每来一块都把正抄的位置拽回开头",
          ov.keep is True)
    check("并且把浮层显示出来（之前可能是隐藏的）", ov.shown >= 1)

    App._deliver(me, "完整答案", streamed=True)
    eq("收尾投的是完整答案", ov.text, "完整答案")
    check("流式收尾同样保住滚动位置", ov.keep is True)

    App._deliver(me, "另一道题的答案")
    check("非流式投递回到顶部（新答案就该从头看）", ov.keep is False)

    clip = SimpleNamespace(cfg=SimpleNamespace(delivery=DeliveryMode.CLIPBOARD),
                           overlay=None)
    check("剪贴板模式没有流式口（半段代码粘出去更糟）",
          App._stream_sink(clip) is None)


def t_openai_dialect():
    section("十、OpenAI 方言：流式解析 + 图片块转换 + 非流式兜底")
    sent = []

    def fake_open(payload, dialect=None, timeout=None):
        sent.append(dict(payload))
        return FakeResp(sse_openai("hi", " there", finish=True))

    prov = AnswerProvider(api_key="k", api_url="https://api.example.com/v1", model="gpt-4o",
                          dialect=OPENAI)
    prov._open = fake_open
    eq("openai 流式正文拼回来", prov._call_api("题目"), "hi there")
    req = sent[-1]
    check("payload 里 stream=True", req.get("stream") is True)
    check("payload 里 messages 是 list", isinstance(req.get("messages"), list))
    check("openai 格式不自动塞 thinking 字段（让调用方通过 extra_body 传）",
          "thinking" not in req)
    # 图片块：anthropic source → openai image_url
    from imaging import Shot
    shot = Shot(b64="dGVzdA==", media_type="image/webp", width=100, height=100,
                nbytes=100, fingerprint="a", scaled=False)
    req2 = prov._resolve_dialect().build_messages(
        [{"type": "image", "source": {"type": "base64",
                                      "media_type": "image/webp",
                                      "data": "dGVzdA=="}},
         {"type": "text", "text": "提示词"}],
        "glm-4.6v", "off", {})
    # openai build_messages 把图片前缀的文本放 system，图片放 user
    user_content = req2["messages"][-1]["content"]
    eq("图片块转成 image_url", user_content[0]["type"], "image_url")
    eq("data URI 包含 base64 数据",
       user_content[0]["image_url"]["url"],
       "data:image/webp;base64,dGVzdA==")
    # 提示词进了 system 消息（OpenAI 格式下图片块前的文字被当作 system prompt）
    sys_text = req2["messages"][0].get("content", "")
    eq("提示词在 system 消息里", sys_text, "提示词")

    # 非流式兜底（用 OpenAI 方言，对应「端点无视 stream」的真实场景）
    prov3 = AnswerProvider(api_key="k", api_url="u", model="m", dialect=OPENAI)
    body = json.dumps({"choices": [{"message": {"content": "整段答案"},
                                   "finish_reason": "stop"}]}).encode()
    resp3 = FakeResp([body])
    prov3._open = lambda payload, dialect=None, timeout=None: resp3
    eq("非 SSE 响应也能读出正文", prov3._call_api("题目"), "整段答案")
    check("响应对象被关掉（不漏 socket）", resp3.closed is True)

    # 空流当失败抛出
    prov4 = AnswerProvider(api_key="k", api_url="u", model="m")
    prov4._open = lambda payload, dialect=None, timeout=None: FakeResp(
        sse_openai(finish=True))
    try:
        prov4._call_api("题目")
        check("一个 text 增量都没有 → 当失败抛出", False, "静悄悄返回了空")
    except RuntimeError:
        check("一个 text 增量都没有 → 当失败抛出（上层会重试）", True)


def t_coding_thinking():
    section("十一、编程/算法题：OCR 关键词开思考、image 先判题型、Java/Scanner 要求")

    # ---- looks_like_coding：强信号一个就够，弱信号要凑两个 ----
    check("LeetCode 页面（强信号）",
          looks_like_coding("c9.回文数-力扣(LeetCode) 给你一个整数 x"))
    check("OJ 输入/输出描述（强信号）",
          looks_like_coding("输入描述：第一行三个整数 a,b,s\n输出描述：输出最少花费"))
    check("OJ 时间/内存限制（强信号）",
          looks_like_coding("时间限制：C/C++语言 1000MS 内存限制：524288KB"))
    check("「请实现」（强信号）",
          looks_like_coding("请实现核心方法 topK，返回出现频率最高的 k 个元素"))
    check("数组 + 示例（两个弱信号）",
          looks_like_coding("给定一个整数数组 nums 和一个整数目标值 target。"
                            "示例 1：输入 nums = [2,7,11,15]"))
    check("单个弱信号不算（概念单选题）",
          not looks_like_coding("在 ArrayList 的中间位置插入一个元素，"
                                "平均时间复杂度是？"))
    check("普通阅读理解不算",
          not looks_like_coding("过度工作和压力都会不可避免地导致失眠症。"
                                "森达公司的所有管理人员都有压力。"))
    check("空文本不算", not looks_like_coding(""))

    # ---- OCR 路径：判成编程题 → thinking enabled + max_tokens 抬到预算之上 ----
    sent = []
    prov = AnswerProvider(api_key="k", api_url="u", model="m")
    prov._open = _fake_open_factory(
        sent, lambda n: sse_done(delta("思路…"), delta("Java 代码…")))
    out = prov.answer_from_passes(
        ["1. 两数之和 给定一个整数数组 nums 和一个整数目标值 target。"
         "示例 1：输入 nums = [2,7,11,15]，输出 [0,1]。"])
    eq("答案正常返回", out, "思路…Java 代码…")
    body = sent[-1]
    eq("编程题 → 请求显式开启思考",
       body.get("thinking"), {"type": "enabled", "budget_tokens": THINKING_BUDGET})
    check("max_tokens 抬到思考预算之上（Anthropic 的硬性要求）",
          body.get("max_tokens", 0) >= THINKING_BUDGET + 4096,
          f"max_tokens={body.get('max_tokens')}")
    user_text = body["messages"][0]["content"]
    check("OCR 题面后追加了作答要求", isinstance(user_text, str))
    check("要求里指明 Java 版本 + Main 类",
          "Java 版本" in user_text and "public class Main" in user_text)
    check("多输入输出要求用 Scanner 传统 IO、禁快速读写",
          "java.util.Scanner" in user_text
          and "BufferedReader" in user_text and "StreamTokenizer" in user_text)
    check("要求是条件式的（非编程题可忽略）", "如果不是编程题" in user_text)

    # ---- OCR 路径：非编程题 → 思考模式跟随 no_thinking 配置，但要求仍带上 ----
    sent.clear()
    prov.answer_from_passes(
        ["过度工作和压力都会不可避免地导致失眠症。请根据材料判断以下结论的正误。"])
    eq("非编程题保持原有关思考的默认行为",
       sent[-1].get("thinking"), {"type": "disabled"})
    check("非编程题的请求里也带条件式作答要求",
          "java.util.Scanner" in sent[-1]["messages"][0]["content"])

    # ---- 端点不认 thinking 字段 → 编程题也退回 default，不炸 ----
    prov2 = AnswerProvider(api_key="k", api_url="u", model="m")
    calls = []
    prov2._open = _fake_open_factory(calls, lambda n: sse_done(delta("答案")))
    prov2._open_original = prov2._open

    def picky(payload, dialect=None, timeout=None):
        if "thinking" in payload:
            raise ApiError(400, "thinking: unsupported parameter")
        return prov2._open_original(payload, dialect, timeout)

    prov2._open = picky
    prov2.answer_from_passes(["给定一个整数数组 nums，请实现两数之和。"])
    check("编程题撞上不认 thinking 的端点 → 去掉字段照常出答案",
          "thinking" not in calls[-1])

    # ---- image 路径：先发一次小请求判题型，判成编程题才开思考 ----
    calls.clear()

    def lines_for(n):
        return sse_done(delta("是")) if n == 1 else sse_done(delta("答案"))

    prov3 = AnswerProvider(api_key="k", api_url="u", model="m")
    prov3._open = _fake_open_factory(calls, lines_for)
    eq("判成编程题 → 照常拿到答案", prov3.answer_from_shots([_shot("IMG1")]), "答案")
    eq("共两次请求（先判题型后解答）", len(calls), 2)
    eq("解答请求开了思考",
       calls[-1].get("thinking"), {"type": "enabled", "budget_tokens": THINKING_BUDGET})
    eq("判断请求自己不思考", calls[0].get("thinking"), {"type": "disabled"})
    eq("判断请求 max_tokens 掐到 16", calls[0].get("max_tokens"), 16)
    classify_text = " ".join(b.get("text", "") for b in
                             calls[0]["messages"][0]["content"] if b["type"] == "text")
    check("判断请求带的是题型判断提示词（不是解答提示词）",
          "严格只回答一个字" in classify_text and "java.util.Scanner" not in classify_text)

    # 判成非编程题 → 解答请求关思考（no_thinking 默认行为不变）
    calls.clear()

    def lines_for_no(n):
        return sse_done(delta("否")) if n == 1 else sse_done(delta("答案"))

    prov4 = AnswerProvider(api_key="k", api_url="u", model="m")
    prov4._open = _fake_open_factory(calls, lines_for_no)
    prov4.answer_from_shots([_shot("IMG1")])
    eq("非编程题 → 解答请求关思考", calls[-1].get("thinking"), {"type": "disabled"})
    check("image 解答提示词里同样带 Java/Scanner 要求",
          any("java.util.Scanner" in b.get("text", "")
              for b in calls[-1]["messages"][0]["content"] if b["type"] == "text"))

    # 判断挂了 → 当非编程题，解答照常（辅助步骤不能变成故障）
    calls.clear()

    def flaky(payload, dialect=None, timeout=None):
        calls.append(dict(payload))
        if len(calls) == 1:
            raise ConnectionResetError("判断请求挂了")
        return FakeResp(sse_done(delta("答案")))

    prov5 = AnswerProvider(api_key="k", api_url="u", model="m")
    prov5._open = flaky
    eq("判断失败不影响解答", prov5.answer_from_shots([_shot("IMG1")]), "答案")
    eq("解答请求保持关思考", calls[-1].get("thinking"), {"type": "disabled"})

    # coding_thinking=False → image 模式直接解答，不判断题型
    calls.clear()
    prov6 = AnswerProvider(api_key="k", api_url="u", model="m",
                           coding_thinking=False)
    prov6._open = _fake_open_factory(calls, lambda n: sse_done(delta("答案")))
    prov6.answer_from_shots([_shot("IMG1")])
    eq("开关关掉 → 只发一次解答请求", len(calls), 1)

    # ---- Anthropic 方言 thinking 三态 ----
    eq("off → disabled", ANTHROPIC.build_messages("x", "m", "off", {})["thinking"],
       {"type": "disabled"})
    eq("on → enabled + 预算",
       ANTHROPIC.build_messages("x", "m", "on", {})["thinking"],
       {"type": "enabled", "budget_tokens": THINKING_BUDGET})
    check("default → 不发字段", "thinking" not in ANTHROPIC.build_messages("x", "m", "default", {}))
    check("OpenAI 方言不自动塞思考字段（由 extra_body 控制）",
          "thinking" not in OPENAI.build_messages("x", "m", "on", {}))


def main():
    print("=" * 70)
    print("流式作答（SSE）单测：解析 / 节流 / 中断保留 / 关深度思考 / 图片模式 / 编程题思考")
    print("=" * 70)
    for t in (t_parse, t_collect, t_interrupt, t_callback_safety, t_sink,
              t_call_api, t_retry, t_image_mode, t_app_wiring, t_openai_dialect,
              t_coding_thinking):
        t()
    print()
    print("=" * 70)
    print("结论:", "全部通过 ✅" if not _fails else f"失败 {len(_fails)} 项 ❌ {_fails}")
    print("=" * 70)
    sys.exit(0 if not _fails else 1)


if __name__ == "__main__":
    main()
