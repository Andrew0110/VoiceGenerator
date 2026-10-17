"""Bounded stereo decoding and block-wise timeline mixing, independent of the model."""
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np
import soundfile as sf
from pydantic import BaseModel, Field
from typing import Literal

from .audio_processing import export_audio, run_ffmpeg

SAMPLE_RATE = 48000
MAX_SOURCE_SECONDS = 600
MAX_TIMELINE_SECONDS = 1200


def waveform(path: Path):
    with sf.SoundFile(path) as audio:
        peaks = []
        for left, right in zip(np.linspace(0, audio.frames, 181, dtype=int)[:-1],
                               np.linspace(0, audio.frames, 181, dtype=int)[1:]):
            audio.seek(left)
            block = audio.read(max(1, right - left), dtype="float32", always_2d=True)
            peaks.append(round(float(np.max(np.abs(block))) if block.size else 0, 4))
        return {"duration": audio.frames / audio.samplerate, "peaks": peaks}


class EditorTrack(BaseModel):
    kind: Literal["generation", "upload"]
    id: str = Field(pattern=r"^[a-f0-9]{32}$")
    start: float = Field(default=0, ge=0, le=600, allow_inf_nan=False)
    fade_in: float = Field(default=0, ge=0, le=600, allow_inf_nan=False)
    fade_out: float = Field(default=0, ge=0, le=600, allow_inf_nan=False)
    trim_start: float = Field(default=0, ge=0, le=MAX_TIMELINE_SECONDS, allow_inf_nan=False)
    trim_end: float | None = Field(default=None, ge=0, le=MAX_TIMELINE_SECONDS, allow_inf_nan=False)
    volume: float = Field(default=1, ge=0, le=4, allow_inf_nan=False)
    loop: bool = False
    loop_duration: float | None = Field(default=None, gt=0, le=MAX_TIMELINE_SECONDS, allow_inf_nan=False)


class MixRequest(BaseModel):
    tracks: list[EditorTrack] = Field(min_length=1)
    name: str = Field(default="Мій мікс", min_length=1, max_length=60)
    output_format: Literal["wav", "mp3", "flac"] = "wav"
    mp3_bitrate: Literal[64, 96, 128] = 64
    replace_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")


def decode_source(source: Path, destination: Path, max_seconds=MAX_SOURCE_SECONDS):
    """Reject long inputs rather than silently truncating them; keep stereo channels."""
    try:
        run_ffmpeg([
            "-y", "-protocol_whitelist", "file,pipe",
            "-format_whitelist", "wav,flac,aiff,mp3,matroska,webm,ogg,mov",
            "-i", str(source), "-map", "0:a:0", "-vn", "-t", str(max_seconds + .1),
            "-ac", "2", "-ar", str(SAMPLE_RATE), "-c:a", "pcm_f32le", str(destination),
        ])
        with sf.SoundFile(destination) as audio:
            duration = audio.frames / audio.samplerate
            if not 0 < duration <= max_seconds:
                raise ValueError(f"Доріжка має бути непорожньою та тривати не більше {max_seconds // 60} хвилин.")
            for block in audio.blocks(blocksize=65536, dtype="float32"):
                if not np.isfinite(block).all():
                    raise ValueError("Аудіофайл містить некоректні дані.")
        return duration
    except Exception:
        destination.unlink(missing_ok=True)
        raise


def render_mix(tracks, destination: Path, mp3_bitrate=128):
    """Mix any number of tracks with bounded buffers and only two open audio files."""
    with TemporaryDirectory(prefix="mix-", dir=destination.parent) as directory:
        directory = Path(directory)
        prepared, decoded_sources = [], {}
        for track, source in tracks:
            # Reusing an audio file across rows does not decode or store it repeatedly.
            if source not in decoded_sources:
                decoded = directory / f"source-{len(decoded_sources)}.wav"
                decode_source(source, decoded, MAX_TIMELINE_SECONDS if track.kind == "generation" else MAX_SOURCE_SECONDS)
                decoded_sources[source] = decoded
            decoded = decoded_sources[source]
            with sf.SoundFile(decoded) as recording:
                first = round(track.trim_start * SAMPLE_RATE)
                last = recording.frames if track.trim_end is None else round(track.trim_end * SAMPLE_RATE)
                if not 0 <= first < last <= recording.frames:
                    raise ValueError("Оберіть непорожній фрагмент у межах початкової доріжки: «Від» має бути меншим за «До».")
            prepared.append(dict(track=track, path=decoded, first=first, length=last - first,
                                 start=round(track.start * SAMPLE_RATE)))

        natural_end = max(item["start"] + item["length"] for item in prepared)
        anchors = [item["start"] + (round(item["track"].loop_duration * SAMPLE_RATE)
                   if item["track"].loop and item["track"].loop_duration is not None else item["length"])
                   for item in prepared if not item["track"].loop or item["track"].loop_duration is not None]
        end = max(anchors, default=natural_end)
        # An automatic loop placed beyond all other audio still plays one full cycle.
        end = max(end, max((item["start"] + item["length"] for item in prepared
                  if item["track"].loop and item["track"].loop_duration is None and item["start"] >= end), default=end))
        for item in prepared:
            track = item["track"]
            item["playback"] = (round(track.loop_duration * SAMPLE_RATE) if track.loop_duration is not None
                                else end - item["start"]) if track.loop else item["length"]
            if item["playback"] < 1:
                raise ValueError("Тривалість повторення має бути щонайменше один аудіосемпл.")
            if max(round(track.fade_in * SAMPLE_RATE), round(track.fade_out * SAMPLE_RATE)) > item["playback"]:
                raise ValueError("Наростання та затухання не можуть бути довшими за тривалість доріжки на шкалі.")
        frames = max(item["start"] + item["playback"] for item in prepared)
        if frames > MAX_TIMELINE_SECONDS * SAMPLE_RATE:
            raise ValueError("Тривалість міксу не може перевищувати 20 хвилин.")

        raw = directory / "mix.wav"
        zero = np.zeros((65536, 2), dtype=np.float32)
        with sf.SoundFile(raw, "w", samplerate=SAMPLE_RATE, channels=2, subtype="FLOAT") as output:
            for position in range(0, frames, len(zero)):
                output.write(zero[:min(len(zero), frames - position)])
        # Accumulate one track at a time on disk, keeping file descriptors independent
        # of the number of tracks. Short loops use vector indexing instead of many seeks.
        with sf.SoundFile(raw, "r+") as mixed:
            for item in prepared:
                track, first, length, start, playback = (item[key] for key in ("track", "first", "length", "start", "playback"))
                if track.volume == 0:
                    continue
                with sf.SoundFile(item["path"]) as recording:
                    cycle = None
                    if track.loop and length <= 65536:
                        recording.seek(first)
                        cycle = recording.read(length, dtype="float32", always_2d=True)
                    for local in range(0, playback, 65536):
                        size = min(65536, playback - local)
                        indices = np.arange(local, local + size)
                        if cycle is not None:
                            block = cycle[indices % length]
                        else:
                            offset = local % length if track.loop else local
                            recording.seek(first + offset)
                            block = recording.read(min(size, length - offset), dtype="float32", always_2d=True)
                            if len(block) < size:
                                recording.seek(first)
                                block = np.concatenate((block, recording.read(size - len(block), dtype="float32", always_2d=True)))
                        gain = np.full(size, track.volume, dtype=np.float32)
                        fade_in, fade_out = round(track.fade_in * SAMPLE_RATE), round(track.fade_out * SAMPLE_RATE)
                        if fade_in:
                            gain *= np.minimum(1, indices / max(1, fade_in - 1))
                        if fade_out:
                            gain *= np.minimum(1, (playback - 1 - indices) / max(1, fade_out - 1))
                        mixed.seek(start + local)
                        previous = mixed.read(size, dtype="float32", always_2d=True)
                        mixed.seek(start + local)
                        mixed.write(previous + block * gain[:, None])
        peak = 1.0
        with sf.SoundFile(raw) as recording:
            for block in recording.blocks(blocksize=65536, dtype="float32"):
                peak = max(peak, float(np.max(np.abs(block))))
        master = directory / "master.wav"
        with sf.SoundFile(raw) as recording, sf.SoundFile(
            master, "w", samplerate=SAMPLE_RATE, channels=2, subtype="PCM_16"
        ) as output:
            for block in recording.blocks(blocksize=65536, dtype="float32", always_2d=True):
                output.write(block / peak)
        export_audio(master, destination, mp3_bitrate)
        return frames / SAMPLE_RATE
