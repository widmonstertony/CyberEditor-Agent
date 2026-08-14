"""Exercise the third-party APIs CyberEditor uses without downloading models."""

from __future__ import annotations

import json

import cv2
import librosa
import numpy as np
import requests
import torch
import whisper


def main() -> None:
    tensor = torch.eye(2, dtype=torch.float32)
    if not torch.equal(tensor @ tensor, tensor):
        raise RuntimeError("PyTorch CPU tensor smoke test failed")

    frame = np.zeros((8, 8, 3), dtype=np.uint8)
    encoded, payload = cv2.imencode(".png", frame)
    if not encoded or payload.size == 0:
        raise RuntimeError("OpenCV image encoding smoke test failed")

    audio = np.zeros(22050, dtype=np.float32)
    rms = librosa.feature.rms(y=audio)
    if rms.ndim != 2 or rms.shape[0] != 1:
        raise RuntimeError("librosa RMS smoke test returned an unexpected shape")

    models = whisper.available_models()
    if "tiny" not in models:
        raise RuntimeError("OpenAI Whisper model catalog is unavailable")

    print(
        json.dumps(
            {
                "torch": str(torch.__version__),
                "opencv": str(cv2.__version__),
                "librosa": str(librosa.__version__),
                "requests": str(requests.__version__),
                "whisper_models": len(models),
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
