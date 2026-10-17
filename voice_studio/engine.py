from pathlib import Path

from .audio_render import render_script
from .config import LANGUAGES, MODEL
from .speech_script import Speech, parse_script, split_text


class LocalEngine:
    """Access only from the single inference worker: MLX models are not shared across workers."""
    def __init__(self, model_path: Path = MODEL):
        self.model_path = model_path
        self.model = None
        self.reference_cache = {}

    def installed(self):
        return (self.model_path / ".ready.json").is_file()

    def generate(self, text, language, steps, reference, transcript, destination, report, speed=1.0, mp3_bitrate=64):
        segments = parse_script(text)
        if not any(isinstance(segment, Speech) for segment in segments):
            return render_script(segments, None, destination, 24000, report, speed=speed, mp3_bitrate=mp3_bitrate)
        import mlx.core as mx
        import numpy as np

        if not self.installed():
            raise RuntimeError("Модель ще не завантажена. Запустіть setup.command у папці застосунку.")
        if self.model is None:
            report(0, "Завантаження моделі в пам’ять…")
            from mlx_audio.tts.utils import load_model
            self.model = load_model(str(self.model_path))
            if self.model.audio_tokenizer is None or self.model.text_tokenizer is None:
                self.model = None
                raise RuntimeError("Не вдалося завантажити токенізатор моделі. Повторіть налаштування.")
        ref_tokens = None
        if reference:
            key = str(reference)
            if key not in self.reference_cache:
                report(3, "Підготовка обраного голосу…")
                from mlx_audio.tts.models.omnivoice.utils import create_voice_clone_prompt
                # Providing the full transcript disables the library's automatic long-audio trim.
                tokens = create_voice_clone_prompt(key, tokenizer=self.model.audio_tokenizer, ref_text=transcript)
                mx.eval(tokens)
                # A small cache avoids keeping every imported voice in GPU memory.
                if len(self.reference_cache) >= 4:
                    self.reference_cache.pop(next(iter(self.reference_cache)))
                self.reference_cache[key] = tokens
            ref_tokens = self.reference_cache[key]
        sample_rate = self.model.sample_rate

        def synthesize(text):
            try:
                for result in self.model.generate(
                    text=text, language=LANGUAGES[language], num_steps=steps,
                    ref_tokens=ref_tokens, ref_text=transcript or None,
                ):
                    if result.sample_rate != sample_rate:
                        raise RuntimeError("Модель повернула неочікувану частоту аудіо.")
                    yield np.asarray(result.audio.astype(mx.float32))
            finally:
                mx.clear_cache()

        return render_script(segments, synthesize, destination, sample_rate, report, speed=speed, mp3_bitrate=mp3_bitrate)
