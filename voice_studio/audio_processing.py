"""Local tempo adjustment and export; explicit pauses never pass through tempo filters."""
import subprocess

import imageio_ffmpeg
import numpy as np

MIME_TYPES = {"wav": "audio/wav", "flac": "audio/flac", "mp3": "audio/mpeg"}
MIN_SPEED = 0.7
MAX_SPEED = 1.3
MP3_BITRATES = (64, 96, 128)


def run_ffmpeg(arguments, audio=None, *, timeout=120):
    try:
        result = subprocess.run(
            [imageio_ffmpeg.get_ffmpeg_exe(), "-nostdin", "-hide_banner", "-loglevel", "error", *arguments],
            input=audio, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, check=True,
        )
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        raise RuntimeError("Не вдалося обробити аудіо. Перевірте встановлення залежностей через setup.command.") from exc
    return result.stdout


def change_tempo(audio, sample_rate, speed):
    if speed == 1:
        return audio
    target_frames = max(1, round(len(audio) / speed))
    # atempo preserves pitch. Normalize its small boundary rounding to the requested duration.
    content = run_ffmpeg([
        "-f", "f32le", "-ar", str(sample_rate), "-ac", "1", "-i", "pipe:0",
        "-af", f"atempo={speed},apad=whole_len={target_frames},atrim=end_sample={target_frames}",
        "-f", "f32le", "-acodec", "pcm_f32le", "pipe:1",
    ], np.asarray(audio, dtype="<f4").tobytes())
    adjusted = np.frombuffer(content, dtype="<f4")
    if len(adjusted) != target_frames or not np.isfinite(adjusted).all():
        raise RuntimeError("Не вдалося змінити швидкість мовлення.")
    return adjusted


def export_audio(source, destination, mp3_bitrate=128, *, timeout=120):
    output_format = destination.suffix.lstrip(".").lower()
    if output_format not in MIME_TYPES:
        raise ValueError("Оберіть WAV, FLAC або MP3.")
    if output_format == "mp3" and mp3_bitrate not in MP3_BITRATES:
        raise ValueError("Оберіть бітрейт MP3: 64, 96 або 128 кбіт/с.")
    if output_format == "wav":
        source.replace(destination)
        return
    temporary = destination.with_suffix(f".partial.{output_format}")
    try:
        codec = ["-c:a", "flac"] if output_format == "flac" else ["-c:a", "libmp3lame", "-b:a", f"{mp3_bitrate}k"]
        # Both renderers supply PCM16 WAV. Explicit decoding prevents periodic PCM
        # patterns from being incorrectly detected as MPEG audio inside the WAV.
        run_ffmpeg(["-y", "-f", "wav", "-c:a", "pcm_s16le", "-i", str(source),
                    "-map", "0:a:0", *codec, "-f", output_format, str(temporary)], timeout=timeout)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
