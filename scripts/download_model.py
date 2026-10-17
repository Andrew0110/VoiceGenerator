"""One-time model download. No user text or voice recording is sent anywhere."""
import json
import hashlib
import os
import shutil
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from voice_studio.config import DATA, MODEL, MODEL_ID, MODEL_REVISION

# Only this setup process has access to Hugging Face.
os.environ.pop("HF_HUB_OFFLINE", None)
os.environ.pop("TRANSFORMERS_OFFLINE", None)
os.environ["HF_XET_CACHE"] = str(DATA / "cache" / "xet")
os.environ["HF_HUB_DISABLE_XET"] = "1"

from huggingface_hub import hf_hub_download

MODEL.mkdir(parents=True, exist_ok=True)
print("Завантаження OmniVoice (~1,64 ГБ). Наступні запуски працюватимуть офлайн.", flush=True)
for name in ["config.json", "tokenizer.json", "tokenizer_config.json", "chat_template.jinja",
             "audio_tokenizer/config.json", "audio_tokenizer/preprocessor_config.json"]:
    hf_hub_download(repo_id=MODEL_ID, revision=MODEL_REVISION, filename=name, local_dir=MODEL)

# The weights are pinned to these upstream LFS checksums. Range downloads avoid
# the Xet transport and can resume completed pieces after an interrupted setup.
weights = {
    "model.safetensors": (1225192351, "5768f3f1d11ee8b3ec31fe906e5d6f5934fdc397e3e5b818de85b70b0e1a2e7e"),
    "audio_tokenizer/model.safetensors": (402864930, "8ef745bfbabeb3bd9ebbdc69e7b6a05e43e191d0208dabfcf7adc42ca89c6580"),
}
CHUNK = 16 * 1024 * 1024


def checksum(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(8 * 1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def download_piece(name, size, start, destination):
    end = min(start + CHUNK, size) - 1
    expected = end - start + 1
    if destination.is_file() and destination.stat().st_size == expected:
        return
    url = f"https://huggingface.co/{MODEL_ID}/resolve/{MODEL_REVISION}/{name}?download=true&part={start}"
    temporary = destination.with_suffix(".tmp")
    for attempt in range(4):
        try:
            request = urllib.request.Request(url, headers={"Range": f"bytes={start}-{end}"})
            with urllib.request.urlopen(request, timeout=60) as response:
                if response.status != 206 or response.headers.get("Content-Range") != f"bytes {start}-{end}/{size}":
                    raise RuntimeError("Сервер повернув неочікуваний фрагмент моделі")
                with temporary.open("wb") as stream:
                    received = 0
                    while block := response.read(1024 * 1024):
                        received += len(block)
                        if received > expected:
                            raise RuntimeError("Некоректний розмір фрагмента")
                        stream.write(block)
            if temporary.stat().st_size != expected:
                raise RuntimeError("Завантаження фрагмента не завершене")
            temporary.replace(destination)
            return
        except Exception:
            temporary.unlink(missing_ok=True)
            if attempt == 3:
                raise
            time.sleep(attempt + 1)


for name, (size, digest) in weights.items():
    target = MODEL / name
    if target.is_file() and target.stat().st_size == size and checksum(target) == digest:
        print(f"Перевірено: {name}", flush=True)
        continue
    pieces = MODEL / ".cache" / "parts" / digest
    pieces.mkdir(parents=True, exist_ok=True)
    # Preserve sequential HTTP prefixes left by a previous huggingface_hub run.
    candidates = list((MODEL / ".cache" / "huggingface" / "download" / Path(name).parent).glob(f"*.{digest}.*.incomplete"))
    if candidates:
        prefix = max(candidates, key=lambda p: p.stat().st_size)
        with prefix.open("rb") as stream:
            for start in range(0, prefix.stat().st_size // CHUNK * CHUNK, CHUNK):
                part = pieces / str(start)
                content = stream.read(CHUNK)
                if not part.exists():
                    part.write_bytes(content)
    tasks = [(start, pieces / str(start)) for start in range(0, size, CHUNK)]
    completed = 0
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = [pool.submit(download_piece, name, size, start, part) for start, part in tasks]
        for future in as_completed(futures):
            future.result()
            completed += 1
            if completed % 4 == 0 or completed == len(tasks):
                print(f"{name}: {completed}/{len(tasks)} фрагментів", flush=True)
    temporary = target.with_suffix(".assembling")
    with temporary.open("wb") as stream:
        for _, part in tasks:
            with part.open("rb") as piece:
                shutil.copyfileobj(piece, stream)
    if temporary.stat().st_size != size or checksum(temporary) != digest:
        temporary.unlink(missing_ok=True)
        shutil.rmtree(pieces)
        raise RuntimeError("Контрольна сума моделі не збігається. Повторіть налаштування.")
    temporary.replace(target)
    shutil.rmtree(pieces)
    for prefix in candidates:
        prefix.unlink(missing_ok=True)
required = ["config.json", "model.safetensors", "tokenizer.json", "tokenizer_config.json",
            "audio_tokenizer/config.json", "audio_tokenizer/model.safetensors"]
for filename in required:
    if not (MODEL / filename).is_file() or (MODEL / filename).stat().st_size == 0:
        raise RuntimeError(f"Відсутній файл моделі: {filename}")
manifest = {"model": MODEL_ID, "revision": MODEL_REVISION,
            "files": {name: (MODEL / name).stat().st_size for name in required}}
temporary = MODEL / ".ready.tmp"
temporary.write_text(json.dumps(manifest, indent=2))
temporary.replace(MODEL / ".ready.json")
print("Модель готова. Відкрийте start.command.", flush=True)
