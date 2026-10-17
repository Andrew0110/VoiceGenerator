import io
import sqlite3
import re
import subprocess

import imageio_ffmpeg
import numpy as np
import pytest
import soundfile as sf
from fastapi.testclient import TestClient

from voice_studio.app import create_app
from voice_studio.audio_processing import MIME_TYPES, change_tempo
from voice_studio.audio_render import render_script
from voice_studio.engine import LocalEngine
from voice_studio.speech_script import parse_script
from voice_studio.storage import Storage, identifier, now
from test_studio import FakeEngine, wait_job


def tone(seconds=1):
    return (.25 * np.sin(2 * np.pi * 440 * np.arange(round(24000 * seconds)) / 24000)).astype(np.float32)


@pytest.mark.parametrize("speed", [.7, 1, 1.3])
@pytest.mark.parametrize("output_format", ["wav", "flac", "mp3"])
def test_real_codecs_keep_breaks_and_change_only_speech(tmp_path, speed, output_format):
    output = tmp_path / f"result.{output_format}"
    speech = tone()
    text = '<break time="250ms"/>До<break time="2s"/>Після<break time="500ms"/>'
    duration = render_script(parse_script(text), lambda _: [speech], output, 24000, lambda *_: None, speed=speed)
    audio, sr = sf.read(output)
    speech_frames = round(24000 / speed)
    assert sr == 24000
    assert len(audio) == 2 * speech_frames + 66000
    assert duration == len(audio) / sr
    middle_start = 6000 + speech_frames
    if output_format != "mp3":
        assert not audio[:6000].any()
        assert not audio[middle_start:middle_start + 48000].any()
        assert not audio[-12000:].any()
    else:
        # Lossy MP3 may ring at boundaries; check silence away from those edges.
        assert np.max(np.abs(audio[middle_start + 2400:middle_start + 45600])) < 1e-4
    assert np.max(np.abs(audio[6000:middle_start])) > .1
    assert not list(tmp_path.glob("*.partial.*"))


@pytest.mark.parametrize("speed", [.7, 1.3])
def test_tempo_preserves_voice_pitch(speed):
    adjusted = change_tempo(tone(2), 24000, speed)
    assert len(adjusted) == round(48000 / speed)
    # Pitch remains 440 Hz even though the waveform duration changes.
    sample = adjusted[2400:-2400]
    spectrum = np.abs(np.fft.rfft(sample))
    peak = np.argmax(spectrum) * 24000 / len(sample)
    assert abs(peak - 440) < 2


@pytest.mark.parametrize("output_format", ["wav", "flac", "mp3"])
def test_settings_survive_api_history_download_and_delete(tmp_path, output_format):
    with TestClient(create_app(tmp_path, FakeEngine())) as client:
        response = client.post("/api/generations", json={
            "text": 'До<break time="2s"/>Після', "speed": .8, "output_format": output_format,
        })
        assert response.status_code == 202
        job = wait_job(client, response.json()["id"])
        assert job["status"] == "completed", job["message"]
        assert job["speed"] == .8 and job["output_format"] == output_format
        assert job["duration"] == 4.5  # 2 / .8 seconds of speech + 2 seconds of silence.
        download = client.get(f"/api/generations/{job['id']}/audio")
        assert download.headers["content-type"] == MIME_TYPES[output_format]
        assert f".{output_format}" in download.headers["content-disposition"]
        recording = sf.info(io.BytesIO(download.content))
        assert recording.format == output_format.upper()
        assert client.get("/api/generations").json()[0]["speed"] == .8
        assert client.delete(f"/api/generations/{job['id']}").status_code == 200
        assert not list((tmp_path / "outputs").iterdir())


@pytest.mark.parametrize("payload", [
    {"speed": .69}, {"speed": 1.31}, {"speed": "NaN"}, {"speed": "Infinity"},
    {"output_format": "ogg"}, {"output_format": "../wav"},
    {"mp3_bitrate": 32}, {"mp3_bitrate": 192},
])
def test_invalid_settings_never_enter_queue(tmp_path, payload):
    with TestClient(create_app(tmp_path, FakeEngine())) as client:
        assert client.post("/api/generations", json={"text": "Текст", **payload}).status_code == 422
        assert client.get("/api/generations").json() == []


@pytest.mark.parametrize("output_format", ["wav", "flac", "mp3"])
def test_silence_only_does_not_change_duration_or_load_model(tmp_path, output_format):
    engine = LocalEngine(tmp_path / "absent")
    output = tmp_path / f"silence.{output_format}"
    assert engine.generate('<break time="2s"/>', "uk", 32, None, None, output, lambda *_: None, speed=1.3) == 2
    assert sf.info(output).duration == 2
    assert engine.model is None


def test_existing_database_and_wav_remain_usable(tmp_path):
    key = identifier()
    with sqlite3.connect(tmp_path / "studio.sqlite3") as db:
        db.execute("""CREATE TABLE jobs (
            id TEXT PRIMARY KEY, text TEXT NOT NULL, voice_id TEXT, voice_name TEXT NOT NULL,
            language TEXT NOT NULL, steps INTEGER NOT NULL, status TEXT NOT NULL,
            progress INTEGER NOT NULL DEFAULT 0, message TEXT NOT NULL DEFAULT '', duration REAL,
            created_at TEXT NOT NULL, finished_at TEXT)""")
        db.execute("INSERT INTO jobs (id,text,voice_name,language,steps,status,created_at) VALUES (?,?,?,?,?,?,?)",
                   (key, "Старий запис", "Голос моделі", "uk", 32, "completed", now()))
    store = Storage(tmp_path)
    sf.write(tmp_path / "outputs" / f"{key}.wav", tone(), 24000)
    # A second startup must be safe and keep all historical settings.
    with TestClient(create_app(tmp_path, FakeEngine())) as client:
        job = client.get(f"/api/generations/{key}").json()
        assert job["output_format"] == "wav" and job["speed"] == 1
        assert job["mp3_bitrate"] == 128
        assert client.get(f"/api/generations/{key}/audio").content[:4] == b"RIFF"
        assert client.delete(f"/api/generations/{key}").status_code == 200
    assert store.all("jobs") == []


def test_export_failure_does_not_publish_or_leave_partial_files(tmp_path, monkeypatch):
    def fail(*_, **__):
        raise RuntimeError("Encoder failed")

    monkeypatch.setattr("voice_studio.audio_processing.run_ffmpeg", fail)
    output = tmp_path / "failed.mp3"
    output.write_bytes(b"previous output")
    with pytest.raises(RuntimeError, match="Encoder failed"):
        render_script(parse_script("Текст"), lambda _: [tone()], output, 24000, lambda *_: None)
    assert output.read_bytes() == b"previous output"
    assert not list(tmp_path.glob("*.partial.*"))


def test_automatic_gaps_and_zero_breaks_keep_their_duration(tmp_path):
    output = tmp_path / "gaps.wav"
    script = 'Слово ' * 60 + '<break time="0s"/>Кінець'
    segments = parse_script(script)
    duration = render_script(segments, lambda _: [tone()], output, 24000, lambda *_: None, speed=1.25)
    from voice_studio.speech_script import Speech
    speech_count = sum(isinstance(segment, Speech) for segment in segments)
    assert duration == speech_count / 1.25 + (speech_count - 2) * .18


@pytest.mark.parametrize("bitrate", [None, 64, 96, 128])
def test_mp3_bitrate_is_encoded_and_persisted_with_unchanged_pauses(tmp_path, bitrate):
    expected = 64 if bitrate is None else bitrate
    payload = {"text": 'До<break time="2s"/>Після', "output_format": "mp3", "speed": .8}
    if bitrate is not None:
        payload["mp3_bitrate"] = bitrate
    with TestClient(create_app(tmp_path, FakeEngine())) as client:
        response = client.post("/api/generations", json=payload)
        assert response.status_code == 202
        job = wait_job(client, response.json()["id"])
        assert job["status"] == "completed", job["message"]
        assert job["mp3_bitrate"] == expected
        assert job["duration"] == 4.5
        assert client.get("/api/generations").json()[0]["mp3_bitrate"] == expected
    recording = tmp_path / "outputs" / f"{job['id']}.mp3"
    probe = subprocess.run([
        imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-i", str(recording), "-t", "0", "-f", "null", "-",
    ], capture_output=True, text=True, check=True)
    assert re.search(rf"Audio: mp3[^\n]*\b{expected} kb/s", probe.stderr)
    audio, sr = sf.read(recording)
    assert len(audio) / sr == 4.5
    assert np.max(np.abs(audio[36000:72000])) < 1e-4  # Inside the unchanged 2-second pause.
    with TestClient(create_app(tmp_path, FakeEngine())) as client:
        assert client.get(f"/api/generations/{job['id']}").json()["mp3_bitrate"] == expected
