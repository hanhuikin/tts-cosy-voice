"""独立验证 v3 流式语速补丁: 加载模型 -> 流式推理 1.0x vs 1.5x -> 对比时长。"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "CosyVoice"))
sys.path.insert(0, str(REPO / "CosyVoice" / "third_party" / "Matcha-TTS"))

from cosyvoice.cli.cosyvoice import AutoModel
from cosyvoice.cli.model import CosyVoice2Model, CosyVoice3Model
import torch.nn.functional as F
import torch


def _token2wav_v3_speed(self, token, prompt_token, prompt_feat, embedding, token_offset,
                        uuid, stream=False, finalize=False, speed=1.0):
    if speed == 1.0 and getattr(self, "_cur_speed", 1.0) != 1.0:
        speed = self._cur_speed
    with torch.cuda.amp.autocast(self.fp16):
        tts_mel, _ = self.flow.inference(token=token.to(self.device, dtype=torch.int32),
                                         token_len=torch.tensor([token.shape[1]], dtype=torch.int32).to(self.device),
                                         prompt_token=prompt_token.to(self.device),
                                         prompt_token_len=torch.tensor([prompt_token.shape[1]], dtype=torch.int32).to(self.device),
                                         prompt_feat=prompt_feat.to(self.device),
                                         prompt_feat_len=torch.tensor([prompt_feat.shape[1]], dtype=torch.int32).to(self.device),
                                         embedding=embedding.to(self.device),
                                         streaming=stream,
                                         finalize=finalize)
        tts_mel = tts_mel[:, :, token_offset * self.flow.token_mel_ratio:]
        if speed != 1.0 and tts_mel.shape[2] > 1:
            print(f"    [scale] chunk mel {tts_mel.shape[2]} -> {max(1, int(tts_mel.shape[2] / speed))} (speed={speed}, stream={stream}, finalize={finalize})")
            tts_mel = F.interpolate(tts_mel, size=max(1, int(tts_mel.shape[2] / speed)), mode="linear")
        if self.hift_cache_dict[uuid] is not None:
            hift_cache_mel = self.hift_cache_dict[uuid]["mel"]
            tts_mel = torch.concat([hift_cache_mel, tts_mel], dim=2)
            self.hift_cache_dict[uuid]["mel"] = tts_mel
        else:
            self.hift_cache_dict[uuid] = {"mel": tts_mel, "speech_offset": 0}
        tts_speech, _ = self.hift.inference(speech_feat=tts_mel, finalize=finalize)
        tts_speech = tts_speech[:, self.hift_cache_dict[uuid]["speech_offset"]:]
        self.hift_cache_dict[uuid]["speech_offset"] += tts_speech.shape[1]
    return tts_speech


_orig_tts = CosyVoice2Model.tts


def _tts_speed_aware(self, *args, stream=False, speed=1.0, **kwargs):
    self._cur_speed = speed if stream else 1.0
    print(f"    [tts] stream={stream} speed={speed} -> _cur_speed={self._cur_speed}")
    gen = _orig_tts(self, *args, stream=stream, speed=speed, **kwargs)

    def wrapper():
        try:
            yield from gen
        finally:
            self._cur_speed = 1.0

    return wrapper()


CosyVoice2Model.tts = _tts_speed_aware
CosyVoice3Model.token2wav = _token2wav_v3_speed

m = AutoModel(model_dir=str(REPO / "CosyVoice" / "pretrained_models" / "Fun-CosyVoice3-0.5B"), fp16=True)
prompt = str(REPO / "CosyVoice" / "asset" / "zero_shot_prompt.wav")
text = "流式变速测试，这段话会以一点五倍语速生成。"

for speed in (1.0, 1.5):
    total = 0
    chunks = 0
    for j in m.inference_zero_shot(f"You are a helpful assistant.<|endofprompt|>希望你以后能够做的比我还好呦。".join([""]) and text or text,
                                   "You are a helpful assistant.<|endofprompt|>希望你以后能够做的比我还好呦。",
                                   prompt, stream=True, speed=speed):
        n = j["tts_speech"].shape[1]
        total += n
        chunks += 1
    print(f"speed={speed}: {chunks} chunks, {total} samples = {total / m.sample_rate:.2f}s")
