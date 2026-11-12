"""Conservative token estimation, used to decide compaction before a request.

Provider usage is the only authoritative count. This module prices a surface
*before* the request so the context policy can decide without a network round
trip. Every receipt it produces says ``estimated`` so the two never get
conflated.

Calibration comes from this project's own measurements, not a guess: over 774
frozen provider responses the median was 2.37 characters per token, and the
worst-case Chinese-text mixture reached about 1.0 token per character. The
estimator therefore charges one token per CJK character and 2.8 characters per
token for everything else, which is deliberately pessimistic.
"""
from __future__ import annotations

import json
import math

# Ranges that cost roughly one token per character in the models used here.
_CJK_RANGES = (
    (0x3000, 0x303F),   # CJK punctuation
    (0x3040, 0x30FF),   # kana
    (0x3400, 0x4DBF),   # CJK extension A
    (0x4E00, 0x9FFF),   # CJK unified ideographs
    (0xF900, 0xFAFF),   # compatibility ideographs
    (0xFF00, 0xFFEF),   # fullwidth forms
)
ASCII_CHARS_PER_TOKEN = 2.8
MESSAGE_OVERHEAD = 4


def _is_wide(character: str) -> bool:
    code = ord(character)
    for low, high in _CJK_RANGES:
        if low <= code <= high:
            return True
    return False


def estimate_text(text: str) -> int:
    """Estimate tokens for one string. Never negative, never zero for input."""
    if not isinstance(text, str) or not text:
        return 0
    wide = 0
    for character in text:
        if _is_wide(character):
            wide += 1
    narrow = len(text) - wide
    return wide + int(math.ceil(narrow / ASCII_CHARS_PER_TOKEN))


def estimate_messages(messages) -> int:
    """Estimate the prompt footprint of a projected message list."""
    total = 0
    for message in messages or ():
        total += MESSAGE_OVERHEAD
        content = message.get("content")
        if isinstance(content, str):
            total += estimate_text(content)
        elif content is not None:
            total += estimate_text(str(content))
        calls = message.get("tool_calls")
        if calls:
            total += estimate_text(json.dumps(calls, ensure_ascii=False, default=str))
    return total


def estimate_tools(tools) -> int:
    """Tool schemas are sent with every request; leaving them out undercounts.

    In the frozen batch this omission was not academic: a catalog plus twenty
    verbatim blocks were priced as "text only" while the schema block rode along
    uncounted.
    """
    if not tools:
        return 0
    return estimate_text(json.dumps(list(tools), ensure_ascii=False, default=str))


class TokenMeter:
    """Prices a surface and remembers the provider count that last disagreed.

    ``measure`` is what compaction calls. ``record_usage`` keeps the most recent
    provider-reported prompt size beside the estimate so a systematic drift is
    visible in the run record rather than silently absorbed.
    """

    def __init__(self, *, chars_per_token: float = ASCII_CHARS_PER_TOKEN):
        self.chars_per_token = float(chars_per_token)
        self.last_estimate = None
        self.last_provider_prompt_tokens = None
        self.samples = []

    def measure(self, messages, *, tools=None) -> int:
        self.last_estimate = estimate_messages(messages) + estimate_tools(tools)
        return self.last_estimate

    def measure_text(self, text: str) -> int:
        return estimate_text(text)

    def record_usage(self, usage) -> None:
        if not isinstance(usage, dict):
            return
        prompt = usage.get("prompt_tokens")
        if prompt is None:
            prompt = usage.get("input_tokens")
        if type(prompt) is not int:
            return
        self.last_provider_prompt_tokens = prompt
        if self.last_estimate:
            self.samples.append(dict(
                estimated=self.last_estimate,
                provider=prompt,
                ratio=round(prompt / self.last_estimate, 4)))