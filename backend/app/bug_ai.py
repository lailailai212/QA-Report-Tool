# -*- coding: utf-8 -*-
"""Generate structured Bug Description via OpenAI-compatible chat API (DeepSeek)."""
from __future__ import annotations

import re
from typing import Any

import requests

from .config import settings

REQUIRED_SECTIONS = (
    "【前置条件】",
    "【复现步骤】",
    "【预期结果】",
    "【实际结果】",
    "【测试数据】",
)

SYSTEM_PROMPT = """你是资深 QA，根据用户一句话的缺陷描述，生成可直接粘贴到飞书 Bug Description 的中文文本。

硬性要求：
1. 只输出正文，不要 Markdown 代码块、不要开场白/结束语。
2. 必须且仅使用下列五个标题（含全角括号），顺序固定；每个标题必须单独占一行，标题前必须换行（不能接在上一句或列表项后面）：
【前置条件】
【复现步骤】
【预期结果】
【实际结果】
【测试数据】
3. 每个标题的下一行开始写内容；复现步骤、预期结果、实际结果用有序列表（1. 2. 3.）。章节之间空一行。
4. 信息不足时用合理占位（如「待补充」），不要编造与描述无关的业务细节。
5. 实际结果应体现缺陷现象；预期结果写正确行为。

正确示例片段：
【前置条件】
1. 已登录系统。

【复现步骤】
1. 打开页面。
"""


class BugAiError(RuntimeError):
    pass


def _extract_message_text(data: dict[str, Any]) -> str:
    choices = data.get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        raise BugAiError(f"AI 返回无 choices: {str(data)[:300]}")
    msg = choices[0].get("message") or {}
    if not isinstance(msg, dict):
        raise BugAiError("AI 返回 message 格式异常")
    content = msg.get("content")
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
            elif isinstance(block, str):
                parts.append(block)
        content = "".join(parts)
    text = str(content or "").strip()
    # Some models wrap thinking; strip common fences
    if text.startswith("```"):
        text = re.sub(r"^```(?:\w+)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()
    if not text:
        raise BugAiError("AI 返回空内容")
    return text


def normalize_section_headers(text: str) -> str:
    """Force 【章节】 titles onto their own lines (shared by AI + Feishu create)."""
    cleaned = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    for section in REQUIRED_SECTIONS:
        pat = re.escape(section)
        cleaned = re.sub(rf"(?<!\n)[ \t]*({pat})", r"\n\n\1", cleaned)
        cleaned = re.sub(rf"({pat})[ \t]*([^\n])", r"\1\n\2", cleaned)
    cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned


def _normalize_description(text: str) -> str:
    cleaned = normalize_section_headers(text)
    missing = [s for s in REQUIRED_SECTIONS if s not in cleaned]
    if missing:
        raise BugAiError("AI 输出缺少章节：" + "、".join(missing))
    return cleaned


def generate_bug_description(
    prompt: str,
    *,
    summary: str = "",
) -> dict[str, str]:
    api_key = (settings.ai_api_key or "").strip()
    if not api_key:
        raise BugAiError("未配置 DEEPSEEK_API_KEY / AI_API_KEY，无法生成 Description")

    user_bits = [f"缺陷一句话描述：{(prompt or '').strip()}"]
    if (summary or "").strip():
        user_bits.append(f"可选标题（Summary）：{summary.strip()}")
    user_bits.append("请按固定五个章节输出。")

    base = (settings.ai_base_url or "").rstrip("/")
    url = f"{base}/chat/completions"
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": "\n".join(user_bits)},
    ]
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    body: dict[str, Any] = {
        "model": settings.ai_model,
        "messages": messages,
        "temperature": 0.3,
    }
    try:
        resp = requests.post(url, headers=headers, json=body, timeout=90)
    except requests.RequestException as exc:
        raise BugAiError(f"调用 AI 接口失败: {exc}") from exc

    if resp.status_code >= 400:
        raise BugAiError(
            f"AI 接口 HTTP {resp.status_code}: {(resp.text or '')[:400]}"
        )
    try:
        data = resp.json()
    except ValueError as exc:
        raise BugAiError(f"AI 返回非 JSON: {resp.text[:200]!r}") from exc
    if not isinstance(data, dict):
        raise BugAiError("AI 返回根节点非 object")

    if data.get("error"):
        err = data.get("error")
        raise BugAiError(f"AI 业务错误: {err}")

    text = _normalize_description(_extract_message_text(data))
    return {
        "description": text,
        "model": settings.ai_model,
    }
