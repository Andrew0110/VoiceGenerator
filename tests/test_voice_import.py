import io
import sys
from types import ModuleType, SimpleNamespace

import numpy as np
import pytest
import soundfile as sf
from fastapi.testclient import TestClient

from voice_studio.app import create_app
from voice_studio.audio_processing import run_ffmpeg
from voice_studio.engine import LocalEngine
from test_studio import FakeEngine, wav


@pytest.mark.parametrize("seconds", [16, 30, 90])
def test_long_samples_and_full_transcripts_are_kept(tmp_path, seconds):
    transcript = "Це повний текст мого довгого запису. " * 100
    with TestClient(create_app(tmp_path, FakeEngine())) as client:
        response = client.post("/api/voices", data={"name": "Мій голос", "language": "uk", "transcript": transcript},
                               files={"sample": ("long.wav", wav(seconds), "audio/wav")})
        assert response.status_code == 201, response.text
        voice = response.json()
        assert voice["duration"] == seconds
        assert voice["transcript"] == transcript.strip()
        audio = client.get(f"/api/voices/{voice['id']}/audio")
        assert sf.info(io.BytesIO(audio.content)).duration == seconds
        assert not list(tmp_path.glob("voice-upload-*"))
        assert not list((tmp_path / "voices").glob("*.partial.*"))


@pytest.mark.parametrize("container,codec,extension,mime", [
    ("webm", "libopus", "webm", "audio/webm;codecs=opus"),
    ("ogg", "libopus", "ogg", "audio/ogg;codecs=opus"),
    ("ipod", "aac", "m4a", "audio/mp4"),
    ("mp3", "libmp3lame", "mp3", "audio/mpeg"),
    ("flac", "flac", "flac", "audio/flac"),
    ("aiff", "pcm_s16be", "aiff", "audio/aiff"),
])
def test_real_browser_recordings_and_uploaded_codecs(tmp_path, container, codec, extension, mime):
    source = tmp_path / "source.wav"
    source.write_bytes(wav(25))
    encoded = tmp_path / f"sample.{extension}"
    run_ffmpeg(["-y", "-i", str(source), "-c:a", codec, "-f", container, str(encoded)])
    with TestClient(create_app(tmp_path / "studio", FakeEngine())) as client:
        response = client.post("/api/voices", data={"name": "Запис", "language": "uk", "transcript": "Текст запису."},
                               files={"sample": (encoded.name, encoded.read_bytes(), mime)})
        assert response.status_code == 201, response.text
        voice = response.json()
        assert abs(voice["duration"] - 25) < .1
        recording = sf.info(tmp_path / "studio" / "voices" / f"{voice['id']}.wav")
        assert recording.samplerate == 24000 and recording.channels == 1


def test_upload_size_limit_and_failed_decode_cleanup(tmp_path, monkeypatch):
    monkeypatch.setattr("voice_studio.app.MAX_UPLOAD", 100)
    with TestClient(create_app(tmp_path, FakeEngine())) as client:
        for content, expected in [(b"x" * 101, 413), (b"invalid", 422)]:
            response = client.post("/api/voices", data={"name": "Запис", "transcript": "Текст."},
                                   files={"sample": ("sample.webm", content, "audio/webm")})
            assert response.status_code == expected
            assert not list(tmp_path.glob("voice-upload-*"))
            assert not list((tmp_path / "voices").iterdir())


def test_decoded_size_limit_rejects_instead_of_truncating(tmp_path, monkeypatch):
    monkeypatch.setattr("voice_studio.voice_import.MAX_DECODED", 200000)
    with TestClient(create_app(tmp_path, FakeEngine())) as client:
        response = client.post("/api/voices", data={"name": "Запис", "transcript": "Текст."},
                               files={"sample": ("sample.wav", wav(10), "audio/wav")})
        assert response.status_code == 422
        assert "завеликий" in response.json()["detail"]
        assert not list((tmp_path / "voices").iterdir())


def test_long_reference_preparation_receives_full_transcript(tmp_path, monkeypatch):
    mlx = ModuleType("mlx")
    core = ModuleType("mlx.core")
    core.float32 = np.float32
    core.eval = lambda *_: None
    core.clear_cache = lambda: None
    mlx.core = core
    utils = ModuleType("mlx_audio.tts.models.omnivoice.utils")
    captured = []

    def prepare(*args, **kwargs):
        captured.append((args, kwargs))
        return np.ones(600)

    utils.create_voice_clone_prompt = prepare
    monkeypatch.setitem(sys.modules, "mlx", mlx)
    monkeypatch.setitem(sys.modules, "mlx.core", core)
    monkeypatch.setitem(sys.modules, "mlx_audio.tts.models.omnivoice.utils", utils)

    class Model:
        sample_rate = 24000
        audio_tokenizer = object()

        def generate(self, **kwargs):
            yield SimpleNamespace(audio=np.full(24000, .25), sample_rate=24000)

    transcript = "Повний текст зразка. " * 100
    engine = LocalEngine(tmp_path)
    engine.model = Model()
    monkeypatch.setattr(engine, "installed", lambda: True)
    engine.generate("Перевірка.", "uk", 16, tmp_path / "long.wav", transcript, tmp_path / "result.wav", lambda *_: None)
    assert captured[0][1]["ref_text"] == transcript
    assert "max_duration_s" not in captured[0][1]
