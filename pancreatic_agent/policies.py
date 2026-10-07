"""Policy adapters. They receive the visible snapshot, never the replay environment."""

import json
import os

import openai
from openai import OpenAI

from .state_machine import OUTPUT_SCHEMA


PROMPT_VERSION = "predecision-blinded-v0.1"
SYSTEM_PROMPT = """你参与离线临床决策研究，不执行任何治疗。
只根据提供的当前证据，独立评价是否有足够依据跨过所定义的不可逆根治步骤，或应先取得什么证据。
不知道真实医生的选择及后续结果；不要猜测数据集结局。影像报告中的诊断措辞是报告证据，不自动等于病理确认。
unknown 不等于阴性，疑似不等于确诊，未提及不等于正常。保留冲突、时间代理和未解决的病种问题。
选择恰好一个 canonical_action。诊断性腹腔镜或活检是取证，不是根治切除；CONTINUE_NO_NEW_STAGING 表示不再增加分期而继续所评价的根治路径。
PAUSE 必须指定当前存在的 gap 和目标部位；不能只因缺某项检查就机械补齐套餐。
目标部位优先使用 PERITONEUM_OMENTUM、LIVER、LUNG_PLEURA、PANCREAS、SYSTEMIC。
每个实质性判断引用当前证据中的 source_id。只输出严格符合所给 schema 的 JSON；不输出 Markdown、思维链或额外字段。
只提供简短、可核查的理由。最多两次追加取证，step=2 时应输出终止动作；置信度不代表已验证的可靠性。
"""


class GenerationError(ValueError):
    def __init__(self, reason, raw_output=None):
        super().__init__(reason)
        self.raw_output = raw_output


def build_messages(visible_state):
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps({
            "task": "根据当前证据独立判断下一步，不预告真实医生决策。",
            "decision_state": visible_state,
            "output_schema": OUTPUT_SCHEMA,
        }, ensure_ascii=False)},
    ]


class ScriptedPolicy:
    """Deterministic controller test; supplied outputs are not clinical labels."""
    def __init__(self, outputs):
        self.outputs = iter(outputs)

    def decide(self, state):
        value = next(self.outputs)
        return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


class OpenAICompatiblePolicy:
    def __init__(self, model="kimi-k3", temperature=1.0, max_tokens=3000, timeout=60.0, seed=None):
        key = os.environ.get("BOYU_API_KEY")
        if not key:
            raise ValueError("BOYU_API_KEY is not configured")
        self.client = OpenAI(api_key=key, base_url="https://apicz.boyuerichdata.com/v1/", timeout=timeout, max_retries=5)
        self.model, self.temperature, self.max_tokens, self.seed = model, temperature, max_tokens, seed

    def decide(self, state):
        self.last_metadata = None
        params = {"model": self.model, "messages": build_messages(state), "temperature": self.temperature, "max_tokens": self.max_tokens}
        if self.seed is not None:
            params["seed"] = self.seed
        response = self.client.chat.completions.create(**params)
        choice = response.choices[0] if response.choices else None
        self.last_metadata = {
            "requested_model": self.model, "returned_model": response.model,
            "finish_reason": choice.finish_reason if choice else None,
            "usage": response.usage.model_dump() if response.usage else None,
            "request_id": getattr(response, "_request_id", None), "sdk_version": openai.__version__,
        }
        if not response.choices or not response.choices[0].message.content:
            raise GenerationError("Model returned no final answer text")
        if response.choices[0].finish_reason == "length":
            raise GenerationError("Model output truncated; not repaired into a clinical decision", response.choices[0].message.content)
        # Preserve final raw output; provider reasoning_content is not collected.
        return response.choices[0].message.content

    def close(self):
        self.client.close()
