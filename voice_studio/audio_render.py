"""Adjust speech tempo, insert unchanged pauses, then export the assembled audio."""
from decimal import ROUND_HALF_UP

import numpy as np
import soundfile as sf

from .audio_processing import MAX_SPEED, MIN_SPEED, change_tempo, export_audio
from .speech_script import Silence, Speech


def render_script(segments, synthesize, destination, sample_rate, report, speed=1.0, mp3_bitrate=64):
    if not MIN_SPEED <= speed <= MAX_SPEED:
        raise ValueError("Швидкість мовлення має бути від 0,7× до 1,3×.")
    temporary = destination.with_suffix(".partial.wav")
    frames = 0
    speech_count = sum(isinstance(segment, Speech) for segment in segments)
    speech_index = 0
    # Stream pauses in one-second blocks, including long pauses without large allocations.
    zero_block = np.zeros(sample_rate, dtype=np.float32)
    try:
        with sf.SoundFile(temporary, "w", samplerate=sample_rate, channels=1, subtype="PCM_16") as output:
            def write_silence(count):
                nonlocal frames
                while count:
                    size = min(count, len(zero_block))
                    output.write(zero_block[:size])
                    frames += size
                    count -= size

            for index, segment in enumerate(segments):
                progress = int(index / len(segments) * 95)
                if isinstance(segment, Silence):
                    report(progress, f"Додавання паузи {segment.seconds} с…")
                    samples = int((segment.seconds * sample_rate).to_integral_value(rounding=ROUND_HALF_UP))
                    write_silence(samples)
                    continue
                speech_index += 1
                report(progress, f"Озвучення фрагмента {speech_index} із {speech_count}…")
                produced = False
                for audio in synthesize(segment.text):
                    audio = np.asarray(audio, dtype=np.float32).reshape(-1)
                    if not audio.size or not np.isfinite(audio).all() or np.max(np.abs(audio)) < 1e-5:
                        raise RuntimeError("Модель повернула порожнє аудіо. Спробуйте інший текст або зразок голосу.")
                    audio = change_tempo(np.clip(audio, -1, 1), sample_rate, speed)
                    output.write(np.clip(audio, -1, 1))
                    frames += audio.size
                    produced = True
                if not produced:
                    raise RuntimeError("Модель не створила аудіо.")
                # Explicit breaks replace the automatic gap, including a zero-length break.
                if index + 1 < len(segments) and isinstance(segments[index + 1], Speech):
                    write_silence(round(sample_rate * .18))
        report(96, "Збереження аудіофайлу…")
        export_audio(temporary, destination, mp3_bitrate=mp3_bitrate)
        return frames / sample_rate
    finally:
        temporary.unlink(missing_ok=True)
