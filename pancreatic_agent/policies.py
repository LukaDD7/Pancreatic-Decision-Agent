"""Policy adapters. They receive the visible snapshot, never the replay environment."""

import json
import os

import openai
from openai import OpenAI

from .state_machine import OUTPUT_SCHEMA


PROMPT_VERSION = "clinical-chart-predecision-v0.2"
SYSTEM_PROMPT = """你参与离线临床决策研究，不执行任何治疗。
阅读当前时点可见的病历、完整检查报告及检验结果，独立判断下一步临床处理，是否可以进入根治性切除，或还需检查、讨论及其他处理。
不知道真实医生的选择及后续结果；不要猜测数据集结局。影像报告中的诊断措辞是报告证据，不自动等于病理确认。
资料中未记录不等于阴性或正常。自行识别资料中的问题、矛盾与不确定性；没有研究者预先提供的 gap 或关键发现清单。
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
        {"role": "system", "content": SYSTEM_PROMPT + "\n输出 JSON schema：\n" + json.dumps(OUTPUT_SCHEMA, ensure_ascii=False)},
        {"role": "user", "content": render_chart(visible_state)},
    ]


def render_chart(state):
    """Present source text verbatim; formatting never selects findings or lab rows."""
    lines = [
        "# 当前决策时点的病历资料", f"病例别名：{state['case_id']}",
        f"决策步骤：{state['step']}", f"当前时点：{state['as_of'] or '精确时间未核实'}",
        f"所评价的不可逆边界：{state['irreversible_boundary']}",
        "请阅读以下资料，独立决定此时的下一步处理，并按输出格式作答。", "",
    ]
    for record in state["records"]:
        lines.extend([
            f"## [{record['source_id']}] {record['title']}",
            f"{record['time_label']}：{record['display_time'] or '未记录'}", "",
            record["body"], "",
        ])
    if state["agent_previous_decisions"]:
        lines.extend(["## 你在此前步骤的判断", json.dumps(state["agent_previous_decisions"], ensure_ascii=False), ""])
    return "\n".join(lines)


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
