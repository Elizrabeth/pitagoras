import os
from pathlib import Path

# Everything stays in the Pitagoras folder: downloaded models (Whisper, voice-model helpers) go to
# models\huggingface instead of C:\Users\<you>\.cache\huggingface.
_here = Path(__file__).resolve().parent
os.environ.setdefault("HF_HOME", str(_here / "models" / "huggingface"))
os.environ.setdefault("TORCH_HOME", str(_here / "models" / "torch"))
os.environ.setdefault("PIP_NO_CACHE_DIR", "1")

from pitagoras.main import main  # noqa: E402

if __name__ == "__main__":
    main()
