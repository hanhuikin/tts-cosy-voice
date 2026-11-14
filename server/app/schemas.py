"""Pydantic 请求/响应模型。"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

from .model_registry import MODEL_KEYS

# 新增模型时: 在 model_registry.py 注册后, 同步扩展下面的 Literal (断言会提醒)
ModelKey = Literal[
    "cosyvoice3", "cosyvoice2",
    "indextts2", "indextts25",
    "qwen3tts", "qwen3tts_base", "qwen3tts_design",
]
assert set(ModelKey.__args__) == set(MODEL_KEYS), \
    "schemas.ModelKey 与 model_registry.MODEL_KEYS 不一致, 请同步更新"


class TTSRequest(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    model: ModelKey = "cosyvoice3"
    mode: Literal["sft", "zero_shot", "cross_lingual", "instruct"] = "sft"
    spk_id: Optional[str] = None
    voice_id: Optional[str] = None
    prompt_text: Optional[str] = None
    instruct_text: Optional[str] = None
    speed: float = Field(default=1.0, ge=0.5, le=2.0)
    stream: bool = False
    bgm_file: Optional[str] = None   # 背景音乐文件名 (data/bgm 下, 仅文件名; 空=不混音)
    bgm_volume: float = Field(default=0.18, ge=0.0, le=1.0)   # BGM 基础音量
    bgm_duck: bool = True            # 自动闪避: 说话时自动压低 BGM


class OpenAISpeechRequest(BaseModel):
    input: str = Field(min_length=1, max_length=2000)
    voice: str = Field(min_length=1)  # 内置说话人名 或 "voice:<voice_id>" 或 "v:<id>"
    model: ModelKey = "cosyvoice3"
    speed: float = Field(default=1.0, ge=0.5, le=2.0)
    response_format: Literal["wav"] = "wav"


class ModelSwitchRequest(BaseModel):
    model: ModelKey
