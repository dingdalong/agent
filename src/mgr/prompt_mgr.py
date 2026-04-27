from __future__ import annotations
from typing import TYPE_CHECKING

import datetime
import os
from pathlib import Path
from dataclasses import dataclass, field

if TYPE_CHECKING:
    from src.agent import Agent

@dataclass
class PromptMgr:
    agent: Agent
    model: str
    workdir: Path

    """
    从独立的部分组装系统提示。
    这里的设计原则是清晰性：
    每个部分有单一来源、单一职责。
    这使得提示更易于推理、更易于测试，也更易于随着智能体能力的增长而演进。
    """

    def _build_core(self) -> str:
        return (
            f"你是一个运行在 {self.workdir} 中的智能体。\n"
            "可以使用所提供的工具来完成用户的需求。\n"
            "在做假设之前务必先验证。不要凭空猜测行事。\n"
        )

    def _build_dynamic_context(self) -> str:
        lines = [
            f"当前时间：{datetime.date.today().isoformat()}",
            f"工作目录：{self.workdir}",
            f"llm模型：{self.model}",
            f"运行平台：{os.uname().sysname}",
        ]
        return "# 动态上下文\n" + "\n".join(lines)

    def build(self) -> list:
        """
        将所有部分组装成完整的系统提示。
        静态部分（1-5）与动态部分（6）通过 === 动态边界标记 === 标记分隔。
        在实际的缓存补全（CC）中，静态前缀会在多轮对话中被缓存，以节省提示词令牌。
        """
        sections = []
        core = self._build_core()
        if core:
            sections.append(core)

        sections.append("=== 动态边界标记 ===")
        dynamic = self._build_dynamic_context()
        if dynamic:
            sections.append(dynamic)

        return [{"role": "system", "content": "\n\n".join(sections)}]
