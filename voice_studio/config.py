import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get("VOICE_STUDIO_DATA", ROOT / "data")).resolve()
MODEL = DATA / "models" / "omnivoice"
MODEL_ID = "mlx-community/OmniVoice-bf16"
MODEL_REVISION = "8fb0b754cad788aaefec690cd55c207e8a628f85"
LANGUAGES = {"uk": "ukrainian", "en": "english"}

# Inference uses only local files. The separate download script enables networking.
os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"
os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
os.environ.setdefault("HF_HOME", str(DATA / "cache" / "huggingface"))
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
