import builtins
import sys
from decimal import Decimal
from types import ModuleType, SimpleNamespace

import numpy as np
import pytest
import soundfile as sf
from fastapi.testclient import TestClient

from voice_studio.app import create_app
from voice_studio.audio_render import render_script
from voice_studio.engine import LocalEngine
from voice_studio.speech_script import Silence, Speech, parse_script


@pytest.mark.parametrize("tag, seconds", [
    ('<break time="10.0s" />', "10"),
    ("<break time='500ms'/>", ".5"),
    ('<break  time = "0.125s"/>', ".125"),
    ('<break time="0s" />', "0"),
])
def test_parse_explicit_break(tag, seconds):
    assert parse_script(f"До.{tag}Після.") == [Speech("До."), Silence(Decimal(seconds)), Speech("Після.")]


@pytest.mark.parametrize("tag", [
    '<break time="-1s" />', '<break time="nans" />', '<break time="1m" />',
    '<break time="2s">', '<break />', '<break time="1s" strength="weak" />',
    '<break time="1s"', '</break>', '<break time="3601s" />',
    '<break time="2000s" /><break time="2000s" />', '<break time="0s" />',
])
def test_invalid_break_rejected_before_queue(tmp_path, tag):
    class NoModel:
        model = None

        def installed(self):
            return False

    with TestClient(create_app(tmp_path, NoModel())) as client:
        response = client.post("/api/generations", json={"text": tag})
        assert response.status_code == 422
        assert client.get("/api/generations").json() == []


def test_real_engine_sends_only_speech_to_model_and_inserts_exact_ten_seconds(tmp_path, monkeypatch):
    # Exercise LocalEngine's actual assembly path without requiring Metal in the test runner.
    mlx = ModuleType("mlx")
    core = ModuleType("mlx.core")
    core.float32 = np.float32
    core.clear_cache = lambda: None
    mlx.core = core
    monkeypatch.setitem(sys.modules, "mlx", mlx)
    monkeypatch.setitem(sys.modules, "mlx.core", core)
    calls = []

    class Model:
        sample_rate = 24000

        def generate(self, **kwargs):
            calls.append(kwargs["text"])
            yield SimpleNamespace(audio=np.full(24000, .25), sample_rate=24000)

    engine = LocalEngine(tmp_path)
    engine.model = Model()
    monkeypatch.setattr(engine, "installed", lambda: True)
    output = tmp_path / "explicit.wav"
    duration = engine.generate('Початок.<break time="10.0s" />Продовження.', "uk", 32, None, None, output, lambda *_: None)
    audio, sample_rate = sf.read(output, dtype="int16")
    assert calls == ["Початок.", "Продовження."]
    assert sample_rate == 24000 and duration == 12
    assert len(audio) == 288000
    assert (audio[:24000] == 8192).all()
    assert (audio[24000:264000] == 0).all()
    assert (audio[264000:] == 8192).all()


def test_leading_trailing_and_consecutive_breaks(tmp_path):
    script = '<break time="250ms" />Текст<break time="0.5s" /><break time="750ms" />'
    output = tmp_path / "edges.wav"
    render_script(parse_script(script), lambda _: [np.full(24000, .25)], output, 24000, lambda *_: None)
    audio, _ = sf.read(output, dtype="int16")
    assert len(audio) == 60000  # .25 + 1 + .5 + .75 seconds; no implicit gaps.
    assert (audio[:6000] == 0).all()
    assert (audio[6000:30000] == 8192).all()
    assert (audio[30000:] == 0).all()


def test_zero_break_suppresses_automatic_gap(tmp_path):
    output = tmp_path / "zero.wav"
    render_script(parse_script('Перше.<break time="0s" />Друге.'), lambda _: [np.full(100, .25)], output, 24000, lambda *_: None)
    audio, _ = sf.read(output, dtype="int16")
    assert len(audio) == 200 and (audio == 8192).all()


def test_automatic_gap_still_applies_between_ordinary_chunks(tmp_path):
    segments = parse_script("Слово " * 60)
    assert len(segments) > 1
    output = tmp_path / "chunks.wav"
    render_script(segments, lambda _: [np.full(100, .25)], output, 24000, lambda *_: None)
    audio, _ = sf.read(output, dtype="int16")
    assert len(audio) == 100 * len(segments) + 4320 * (len(segments) - 1)


def test_pause_only_uses_no_model_and_can_run_without_weights(tmp_path, monkeypatch):
    original_import = builtins.__import__

    def no_mlx(name, *args, **kwargs):
        assert not name.startswith("mlx"), "A silence-only script must not import the inference runtime"
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_mlx)
    engine = LocalEngine(tmp_path / "missing-model")
    output = tmp_path / "silence.wav"
    assert engine.generate('<break time="10.0s" />', "uk", 32, None, None, output, lambda *_: None) == 10
    audio, sr = sf.read(output, dtype="int16")
    assert sr == 24000 and len(audio) == 240000 and not audio.any()
    assert engine.model is None


def test_failed_synthesis_leaves_existing_output_intact(tmp_path):
    output = tmp_path / "failure.wav"
    output.write_bytes(b"previous output")
    with pytest.raises(RuntimeError, match="порожнє аудіо"):
        render_script(parse_script('Текст<break time="10s"/>'), lambda _: [np.zeros(100)], output, 24000, lambda *_: None)
    assert output.read_bytes() == b"previous output"
    assert not output.with_suffix(".partial.wav").exists()
