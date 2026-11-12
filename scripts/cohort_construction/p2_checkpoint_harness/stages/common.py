"""Shared pieces of the stage modules: the finish tool and stable event ids."""
from __future__ import annotations

import hashlib
import json

from ..tools import Tool


def finish_tool(*, label="本阶段"):
    def run(note=""):
        """Declare this turn's work complete."""
        return dict(finished=True, note=note)

    return Tool(
        name="finish",
        description=("%s结束。在你已经提交全部结果、并读过程序返回的核验问题之后再调用。"
                     % label),
        input_schema=dict(type="object", additionalProperties=False,
                          properties=dict(note=dict(type="string"))),
        run=run, terminal=True)


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def event_id(origin_source_ids, local_id):
    """A proposal id based on its cited origins, not its discovery assignment.

    The local id remains the agent's revision key within one reading task. It
    does not assert clinical identity; two source-attributed proposals can still
    be compared and grouped explicitly by the case-level stages.
    """
    return "EV-" + digest([sorted(origin_source_ids), local_id])[:16]


def unit_id(unit):
    return unit["unit_id"]