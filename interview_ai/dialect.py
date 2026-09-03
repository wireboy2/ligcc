"""
方言（Dialect）层：把「线上 API 的格式差异」收进一个模块。

当前项目只支持 Anthropic Messages 格式，第三方中转站多半也兼容它。
但 DeepSeek 官网、智谱 GLM、Moonshot Kimi 等只提供 OpenAI 的
`/v1/chat/completions`，图片块、事件名、思考字段的写法都不一样。

这一层只做两件事：
  1. 同一份「题目 → 请求体」的拼接逻辑，按方言写出两份
  2. 同一份「SSE 字节流 → 正文」的消费逻辑，按方言读两份

上层（main.py 的流式收集、节流、断流保留、重试、非流式兜底）全部
走方言提供的方法，不动手分辨是哪家在说话。

内置方言
---------
ANTHROPIC  : 官方格式，`/v1/messages`，SSE 事件名 `content_block_delta` 等
OPENAI     : OpenAI 兼容格式，`/v1/chat/completions`，delta 在 `choices[0]`

自动选择
---------
`resolve_dialect(cfg)` 默认 `auto`：
  - url 命中 `re.compile(r"/messages?$")` 且 req_headers 含
    `anthropic-version` → anthropic（这是现有中转站的写法）
  - 其它 → openai
写死 `anthropic` / `openai` 可覆盖自动判断。
"""
import copy
import re
from dataclasses import dataclass
from typing import Any, Callable

from imaging import Shot


# ---------------------------------------------------------------------------
# 方言接口
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Dialect:
    """一份「端点规格」：给 provider 用它的字段来拼请求、读响应。"""
    # HTTP
    path: str                        # 例如 "/v1/messages" 或 "/v1/chat/completions"
    headers: dict                    # 除 Authorization 之外要带的固定头
    # 请求体（返回完整 payload dict，上层只管 set 头）
    build_messages: Callable[
        [Any, str, bool, dict],
        dict
    ]  # content（str|list） → 完整请求体 dict（model/max_tokens/stream/messages/…）
    # 图片块（仅 anthropic 用；openai 在 build_messages 内就地转换，默认 None）
    pic_block: Callable[[Shot], dict] | None = None
    # 响应流
    text_delta: Callable[[dict], str | None] = None  # type: ignore[assignment]
    thinking_delta: Callable[[dict], str | None] = None  # type: ignore[assignment]
    is_stop: Callable[[dict], bool] = None  # type: ignore[assignment]
    # 响应体（非流式兜底）
    try_parse: Callable[[dict], str | None] = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Anthropic 方言（默认，保持现有行为）
# ---------------------------------------------------------------------------
def _anthropic_pic_block(shot: Shot) -> dict:
    return shot.to_block()


def _anthropic_build_messages(
    content: Any, model: str, thinking_disabled: bool, extra: dict
) -> dict:
    """Anthropic 的 system prompt 位置独特（顶层），其它都塞 user 消息。
    返回完整 payload dict。"""
    payload = {
        "model": model,
        "max_tokens": 4096,
        "stream": True,
        "messages": [{"role": "user", "content": content}],
    }
    kwargs: dict = {}
    if thinking_disabled:
        kwargs["thinking"] = {"type": "disabled"}
    # extra 放在顶层，Anthropic 目前只认 model/max_tokens/stream/thinking
    kwargs.update(extra)
    payload.update(kwargs)
    return payload


def _anthropic_text_delta(ev: dict) -> str | None:
    if ev.get("type") != "content_block_delta":
        return None
    d = ev.get("delta") or {}
    t = d.get("text")
    return t if isinstance(t, str) else None


def _anthropic_thinking_delta(ev: dict) -> str | None:
    if ev.get("type") != "content_block_delta":
        return None
    d = ev.get("delta") or {}
    t = d.get("thinking")
    return t if isinstance(t, str) else None


def _anthropic_is_stop(ev: dict) -> bool:
    if ev.get("type") == "message_stop":
        return True
    if ev.get("type") == "message_delta":
        return bool((ev.get("delta") or {}).get("stop_reason"))
    return False


def _anthropic_try_parse(body: dict) -> str | None:
    """非流式兜底：Anthropic 的 content 是块列表，正文在 type==text 的块里。"""
    blocks = body.get("content") if isinstance(body, dict) else None
    if isinstance(blocks, list):
        texts = [b.get("text", "") for b in blocks
                 if isinstance(b, dict) and b.get("type") == "text"]
        joined = "\n".join(t for t in texts if t)
        if joined:
            return joined
    return None


ANTHROPIC = Dialect(
    path="/v1/messages",
    headers={"anthropic-version": "2023-06-01"},
    pic_block=_anthropic_pic_block,
    build_messages=_anthropic_build_messages,
    text_delta=_anthropic_text_delta,
    thinking_delta=_anthropic_thinking_delta,
    is_stop=_anthropic_is_stop,
    try_parse=_anthropic_try_parse,
)


# ---------------------------------------------------------------------------
# OpenAI 方言
# ---------------------------------------------------------------------------
# 用 lambda 避免在模块顶层 import Shot（避免循环），反正初始化时已经 import 进来了
_OPENAI_PIC_RE = re.compile(r"^image/(webp|png|jpeg|jpg)$")


def _openai_pic_block(shot: Shot) -> dict:
    """图片块转成 OpenAI 的 image_url:data URI。"""
    mt = shot.media_type
    if not _OPENAI_PIC_RE.match(mt):
        # 不认识的就退回 png base64（端点至少能看见一张图，虽然糊）
        print(f"[dialect] 不认识 media_type {mt!r}，按 image/png 发")
        mt = "image/png"
    return {
        "type": "image_url",
        "image_url": {"url": f"data:{mt};base64,{shot.b64}"},
    }


def _openai_build_messages(
    content: Any, model: str, thinking_disabled: bool, extra: dict
) -> dict:
    """OpenAI 的 messages 就是平铺列表，无 system 顶层字段（除非用 Responses API，
    这里统一按 Chat Completions 处理）。
    关思考的字段各家不同，不在这里猜测，交给调用方通过 extra_body 传。"""
    sys_text = ""
    if isinstance(content, str):
        user_content: Any = content
    else:
        # 图片模式：content 已经是 Anthropic 格式的 blocks 列表，按顺序重排成
        # OpenAI 的 messages[user].content 数组（text + image_url 交替）。
        # Anthropic 顺序是 [img1, text(prompt), img2, text(prompt) ...]，
        # 但我们 image_content() 已经把提示词单独放在最后一块 —— 所以这里直接把
        # 除了最后一块之外的所有块当作图片内容，最后一块当 user text。
        blocks = list(content)
        if blocks and blocks[-1].get("type") == "text":
            user_content = blocks[:-1]
            sys_text = blocks[-1].get("text", "")
        else:
            user_content = blocks
        # 把 Anthropic 的 {"type":"image","source":...} 转成 image_url
        out: list[dict] = []
        for b in user_content:
            if b.get("type") == "image" and "source" in b:
                out.append(_openai_pic_block(Shot(
                    b64=b["source"]["data"],
                    media_type=b["source"]["media_type"],
                    width=0, height=0, nbytes=0,
                    fingerprint="", scaled=False,
                )))
            elif b.get("type") == "text":
                out.append({"type": "text", "text": b.get("text", "")})
        user_content = out
    msgs: list[dict] = []
    if sys_text.strip():
        msgs.append({"role": "system", "content": sys_text})
    msgs.append({"role": "user", "content": user_content})
    # extra 透传（thinking / reasoning_effort / enable_thinking 等）
    kwargs: dict = {"max_tokens": 4096, "stream": True}
    kwargs.update(extra)
    return {
        "model": model,
        "messages": msgs,
        **kwargs,
    }


def _openai_text_delta(ev: dict) -> str | None:
    """OpenAI 流式事件：正文在 choices[0].delta.content。"""
    choices = ev.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    d = choices[0].get("delta") if isinstance(choices[0], dict) else None
    if not isinstance(d, dict):
        return None
    t = d.get("content")
    return t if isinstance(t, str) else None


def _openai_thinking_delta(ev: dict) -> str | None:
    """OpenAI 流式事件：思考在 choices[0].delta.reasoning_content（DeepSeek/Kimi）
    或 choices[0].delta.reasoning（GLM）。"""
    choices = ev.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    d = choices[0].get("delta") if isinstance(choices[0], dict) else None
    if not isinstance(d, dict):
        return None
    for key in ("reasoning_content", "reasoning"):
        t = d.get(key)
        if isinstance(t, str) and t:
            return t
    return None


def _openai_is_stop(ev: dict) -> bool:
    """OpenAI 流式收尾：finish_reason == "stop"（或 "tool_calls" 等）。"""
    choices = ev.get("choices")
    if not isinstance(choices, list) or not choices:
        return False
    fr = choices[0].get("finish_reason")
    return fr is not None and fr != ""


def _openai_try_parse(body: dict) -> str | None:
    """OpenAI 非流式：choices[0].message.content。"""
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    msg = choices[0].get("message")
    if isinstance(msg, dict):
        c = msg.get("content")
        if isinstance(c, str) and c.strip():
            return c
    return None


OPENAI = Dialect(
    path="/v1/chat/completions",
    headers={},
    pic_block=_openai_pic_block,
    build_messages=_openai_build_messages,
    text_delta=_openai_text_delta,
    thinking_delta=_openai_thinking_delta,
    is_stop=_openai_is_stop,
    try_parse=_openai_try_parse,
)


# ---------------------------------------------------------------------------
# 自动选择
# ---------------------------------------------------------------------------
# url 末尾是 /messages 或 /messages/ 就算 Anthropic 路由（DeepSeek 的
# /anthropic/v1/messages 也能命中）；Anthropic SDK 会带上 anthropic-version 头，
# 有这个头也算。其它都走 OpenAI。
_ANTHROPIC_URL_RE = re.compile(r"/messages/?$", re.IGNORECASE)
_ANTHROPIC_HEADER = re.compile(r"anthropic-version", re.IGNORECASE)


def _guess_dialect_from(cfg_url: str, cfg_headers: dict | None) -> Dialect:
    if _ANTHROPIC_URL_RE.search(cfg_url):
        return ANTHROPIC
    if cfg_headers and any(_ANTHROPIC_HEADER.search(k) for k in cfg_headers):
        return ANTHROPIC
    # 没命中任何 anthropic 特征就退回 ANTHROPIC：保持现有代码「默认Anthropic」
    # 的向后兼容。想切 OpenAI 需要显式写 api.format: openai
    return ANTHROPIC


def resolve_dialect(api_format: str, api_url: str,
                    api_extra_body: dict | None = None) -> Dialect:
    """按配置决定用哪个方言，并透传 extra_body。

    :param api_format: "auto" / "anthropic" / "openai"
    :param api_url: 用来做自动判断的依据
    :param api_extra_body: 用户额外塞的顶层字段（thinking / reasoning_effort 等）
    :return: 一份可变的 Dialect（调用方可以往里塞 extra_body，不污染预置方言）
    """
    fmt = (api_format or "auto").strip().lower()
    if fmt == "anthropic":
        base = ANTHROPIC
    elif fmt == "openai":
        base = OPENAI
    else:
        base = _guess_dialect_from(api_url, None)
    out = copy.copy(base)
    if api_extra_body:
        out = copy.copy(out)
        out.headers = {**out.headers, **api_extra_body.pop("headers", {})}
        # build_messages 拿到的 extra 字典会被 merge 到请求体顶层
        # 这里不做预处理，留给 build_messages 自己处理
    return out


if __name__ == "__main__":
    # 自测：两个方言各自的请求形状
    from imaging import Shot
    s = Shot(b64="dGVzdA==", media_type="image/webp", width=100, height=100,
             nbytes=100, fingerprint="a", scaled=False)
    print("=== ANTHROPIC ===")
    print(ANTHROPIC.pic_block(s))
    print(ANTHROPIC.build_messages("题目", "claude-sonnet-4-5", True, {}))
    print(ANTHROPIC.text_delta({"type": "content_block_delta",
                                "delta": {"text": "hi"}}))
    print("=== OPENAI ===")
    print(OPENAI.pic_block(s))
    print(OPENAI.build_messages("题目", "gpt-4o", False, {}))
    print(OPENAI.build_messages(
        [{"type": "text", "text": "【第 1 张截图】"},
         {"type": "image", "source": {"type": "base64",
                                      "media_type": "image/webp",
                                      "data": "dGVzdA=="}}],
        "glm-4.6v", False, {}))
    print(resolve_dialect("auto", "https://example.com/v1/messages"))
    print(resolve_dialect("auto", "https://api.deepseek.com/v1/chat/completions"))
    print(resolve_dialect("openai", "https://any.url"))
