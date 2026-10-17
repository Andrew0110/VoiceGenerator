"""Decode uploaded and browser-recorded voice samples locally, without time trimming."""
import numpy as np
import soundfile as sf

from .audio_processing import run_ffmpeg

MAX_UPLOAD = 64 * 1024 * 1024
MAX_DECODED = 256 * 1024 * 1024
MAX_TRANSCRIPT = 20000


def import_voice(source, destination):
    temporary = destination.with_suffix(".partial.wav")
    try:
        # Accept only audio containers; uploaded playlists cannot open network URLs.
        run_ffmpeg([
            "-y", "-protocol_whitelist", "file,pipe",
            "-format_whitelist", "wav,flac,aiff,mp3,matroska,webm,ogg,mov",
            "-i", str(source), "-map", "0:a:0", "-vn",
            "-ac", "1", "-ar", "24000", "-c:a", "pcm_s16le",
            "-fs", str(MAX_DECODED + 65536), str(temporary),
        ])
        # Reject oversized decoded data instead of silently keeping a truncated recording.
        if temporary.stat().st_size >= MAX_DECODED:
            raise ValueError("Запис завеликий після розпакування. Виберіть менший файл.")
        with sf.SoundFile(temporary) as recording:
            duration = recording.frames / recording.samplerate
            if duration < 3:
                raise ValueError("Потрібно щонайменше 3 секунди мовлення.")
            energy = 0.0
            for block in recording.blocks(blocksize=65536, dtype="float32"):
                if not np.isfinite(block).all():
                    raise ValueError("Запис містить некоректні аудіодані.")
                energy += np.sum(np.square(block, dtype=np.float64))
            if np.sqrt(energy / recording.frames) < .001:
                raise ValueError("Запис надто тихий або містить лише тишу.")
        temporary.replace(destination)
        return duration
    finally:
        temporary.unlink(missing_ok=True)
