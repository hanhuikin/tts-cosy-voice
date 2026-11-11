"""Pydantic 请求/响应模型。"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


class TTSRequest(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    model: Literal["cosyvoice3", "cosyvoice2"] = "cosyvoice3"
    mode: Literal["sft", "zero_shot", "cross_lingual", "instruct"] = "sft"
    spk_id: Optional[str] = None
    voice_id: Optional[str] = None
    prompt_text: Optional[str] = None
    instruct_text: Optional[str] = None
    speed: float = Field(default=1.0, ge=0.5, le=2.0)
    stream: bool = False


class OpenAISpeechRequest(BaseModel):
    input: str = Field(min_length=1, max_length=2000)
    voice: str = Field(min_length=1)  # 内置说话人名 或 "voice:<voice_id>" 或 "v:<id>"
    model: Literal["cosyvoice3", "cosyvoice2"] = "cosyvoice3"
    speed: float = Field(default=1.0, ge=0.5, le=2.0)
    response_format: Literal["wav"] = "wav"


class ModelSwitchRequest(BaseModel):
    model: Literal["cosyvoice3", "cosyvoice2"]
