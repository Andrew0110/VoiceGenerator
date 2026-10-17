import io
import threading
import time

import numpy as np
import pytest
import soundfile as sf
from fastapi.testclient import TestClient

from voice_studio.app import create_app
from voice_studio.audio_render import render_script
from voice_studio.engine import split_text
from voice_studio.speech_script import parse_script
from voice_studio.storage import Storage, identifier, now


class FakeEngine:
    model = None

    def __init__(self, ready=True, hold=None):
        self.ready = ready
        self.hold = hold
        self.calls = []

    def installed(self):
        return self.ready

    def generate(self, text, language, steps, reference, transcript, destination, report, speed=1.0, mp3_bitrate=64):
        self.calls.append((text, language, reference, transcript))
        report(20, "Озвучення…")
        if self.hold:
            self.hold.wait(timeout=5)
        return render_script(parse_script(text), lambda _: [np.sin(np.arange(24000) * 0.02) * .1],
                             destination, 24000, report, speed=speed, mp3_bitrate=mp3_bitrate)


def wav(seconds=5, silent=False):
    buffer = io.BytesIO()
    values = np.zeros(int(seconds * 24000)) if silent else np.sin(np.arange(int(seconds * 24000)) * .03) * .1
    sf.write(buffer, values, 24000, format="WAV", subtype="PCM_16")
    return buffer.getvalue()


def add_voice(client, **overrides):
    data = {"name": "Тестовий голос", "transcript": "Привіт, це тестовий запис.", "language": "uk"}
    data.update(overrides)
    return client.post("/api/voices", data=data, files={"sample": ("sample.wav", wav(), "audio/wav")})


def wait_job(client, key):
    for _ in range(100):
        row = client.get(f"/api/generations/{key}").json()
        if row["status"] in ("completed", "failed"):
            return row
        time.sleep(.01)
    pytest.fail("Job did not finish")


def test_voice_lifecycle_generation_and_history(tmp_path):
    engine = FakeEngine()
    with TestClient(create_app(tmp_path, engine)) as client:
        voice = add_voice(client).json()
        assert voice["duration"] == 5
        assert client.get(f"/api/voices/{voice['id']}/audio").headers["content-type"] == "audio/wav"
        assert client.patch(f"/api/voices/{voice['id']}", json={"name": "Новий голос"}).status_code == 200
        response = client.post("/api/generations", json={"text": "Новий текст.", "voice_id": voice["id"], "language": "en", "steps": 16})
        assert response.status_code == 202
        job = wait_job(client, response.json()["id"])
        assert job["status"] == "completed"
        assert job["voice_name"] == "Новий голос"
        assert job["progress"] == 100
        assert engine.calls[0][2] == tmp_path / "voices" / f"{voice['id']}.wav"
        assert engine.calls[0][3] == voice["transcript"]
        assert client.get(f"/api/generations/{job['id']}/audio").content[:4] == b"RIFF"
        assert client.delete(f"/api/voices/{voice['id']}").status_code == 200
        assert len(client.get("/api/generations").json()) == 1
        assert not (tmp_path / "voices" / f"{voice['id']}.wav").exists()
        assert client.get(f"/api/generations/{job['id']}/audio").status_code == 200
        assert client.delete(f"/api/generations/{job['id']}").status_code == 200
        assert not list((tmp_path / "outputs").glob("*.wav"))


@pytest.mark.parametrize("payload", [{"text": " "}, {"text": "Hi", "language": "zz"}, {"text": "Hi", "steps": 1}, {"text": "x" * 3001}])
def test_bad_generation_parameters(tmp_path, payload):
    with TestClient(create_app(tmp_path, FakeEngine())) as client:
        assert client.post("/api/generations", json=payload).status_code == 422
        assert client.get("/api/generations").json() == []


@pytest.mark.parametrize("sample", [b"not an audio file", wav(1), wav(5, silent=True)])
def test_reject_bad_voice_samples(tmp_path, sample):
    with TestClient(create_app(tmp_path, FakeEngine())) as client:
        response = client.post("/api/voices", data={"name": "Name", "transcript": "Transcript", "language": "uk"}, files={"sample": ("sample.wav", sample)})
        assert response.status_code == 422
        assert client.get("/api/voices").json() == []
        assert not list((tmp_path / "voices").glob("*.wav"))


def test_missing_model_and_unknown_voice(tmp_path):
    with TestClient(create_app(tmp_path, FakeEngine(False))) as client:
        assert client.post("/api/generations", json={"text": "Hello"}).status_code == 409
    with TestClient(create_app(tmp_path, FakeEngine())) as client:
        assert client.post("/api/generations", json={"text": "Hello", "voice_id": identifier()}).status_code == 404


def test_protect_active_voice_and_bound_queue(tmp_path):
    hold = threading.Event()
    with TestClient(create_app(tmp_path, FakeEngine(hold=hold))) as client:
        try:
            voice = add_voice(client).json()
            payload = {"text": "Привіт", "voice_id": voice["id"]}
            for _ in range(5):
                assert client.post("/api/generations", json=payload).status_code == 202
            assert client.post("/api/generations", json=payload).status_code == 429
            assert client.delete(f"/api/voices/{voice['id']}").status_code == 409
        finally:
            hold.set()


def test_failure_is_recorded(tmp_path):
    class BrokenEngine(FakeEngine):
        def generate(self, *args, **kwargs):
            raise RuntimeError("Test model error")
    with TestClient(create_app(tmp_path, BrokenEngine())) as client:
        job = client.post("/api/generations", json={"text": "Hello"}).json()
        completed = wait_job(client, job["id"])
        assert completed["status"] == "failed"
        assert "Test model error" in completed["message"]
        assert client.get(f"/api/generations/{job['id']}/audio").status_code == 409


def test_restart_marks_interrupted_jobs(tmp_path):
    store = Storage(tmp_path)
    key = identifier()
    store.insert("jobs", dict(id=key, text="Hello", voice_id=None, voice_name="Model", language="en", steps=32, status="running", created_at=now()))
    with TestClient(create_app(tmp_path, FakeEngine())) as client:
        assert client.get(f"/api/generations/{key}").json()["status"] == "failed"


def test_reject_cross_site_mutations_and_bad_hosts(tmp_path):
    with TestClient(create_app(tmp_path, FakeEngine())) as client:
        assert client.post("/api/generations", json={"text": "Hello"}, headers={"Origin": "https://example.com"}).status_code == 403
        assert client.get("/api/voices", headers={"Host": "evil.example"}).status_code == 400
        assert client.get("/api/voices/../../etc/passwd/audio").status_code == 404


def test_chunking_preserves_all_words():
    text = "Привіт! " + "Це довгий український текст. " * 100
    parts = split_text(text)
    assert " ".join(parts).split() == text.split()
    assert all(0 < len(part) <= 180 for part in parts)
    assert all(len(part) <= 180 for part in split_text("x" * 1000))
    assert split_text("x" * 180) == ["x" * 180]
