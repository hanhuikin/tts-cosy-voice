"""重新转写指定音频 (whisper small, 指定语言), 打印结果供人工校对。"""
import sys

import librosa
import numpy as np
import whisper

path, lang = sys.argv[1], sys.argv[2]
audio, _ = librosa.load(path, sr=16000, mono=True)
model = whisper.load_model("small")
result = model.transcribe(np.asarray(audio), language=lang)
print("TEXT:", " ".join(s["text"].strip() for s in result["segments"]))
