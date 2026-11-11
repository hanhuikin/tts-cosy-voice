"""CosyVoice3 流式语速补丁 (运行时 monkey-patch, 不修改上游文件)。

上游限制: 语速仅非流式推理支持 (token2wav 内 assert), 流式分支不透传 speed。
实现: 重写 CosyVoice3Model.token2wav —— 流式模式下对每个新增 mel 分块
独立做时间插值 (mel 均匀缩放, 音高不变), 经 hift 缓存累积后整体一致;
tts 包装器把语速透传给流式分支。GPU 推理被服务层队列串行化,
在 self 上暂存当前语速是安全的。

注意: CosyVoice2 的流式重叠加窗缓存与逐块缩放不兼容, v2 流式保持语速=1。
"""
import torch
import torch.nn.functional as F

from cosyvoice.cli.model import CosyVoice2Model, CosyVoice3Model


def _token2wav_v3_speed(self, token, prompt_token, prompt_feat, embedding, token_offset,
                        uuid, stream=False, finalize=False, speed=1.0):
    """与上游 CosyVoice3Model.token2wav 相同, 差异: 支持流式语速 (逐块 mel 插值)。"""
    # 流式分支调用不显式传 speed -> 读取 tts 包装器暂存的当前语速
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
        # [补丁] 入缓存前先缩放新分块, 缓存累积的即为统一缩放后的 mel
        if speed != 1.0 and tts_mel.shape[2] > 1:
            tts_mel = F.interpolate(tts_mel, size=max(1, int(tts_mel.shape[2] / speed)), mode="linear")
        # append mel cache
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
    """包装 tts: 流式时把语速暂存到 self, 供 token2wav 读取。

    上游 tts 是生成器函数, 调用仅创建生成器不执行函数体;
    因此必须在生成器耗尽后 (yield from 的 finally) 才能重置 _cur_speed。
    """
    self._cur_speed = speed if stream else 1.0
    gen = _orig_tts(self, *args, stream=stream, speed=speed, **kwargs)

    def wrapper():
        try:
            yield from gen
        finally:
            self._cur_speed = 1.0

    return wrapper()


def apply() -> None:
    """打补丁 (幂等)。v3 流式语速可用; v2 维持上游行为。"""
    CosyVoice2Model.tts = _tts_speed_aware
    CosyVoice3Model.token2wav = _token2wav_v3_speed
