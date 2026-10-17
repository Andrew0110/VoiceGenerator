"""Parse explicit pauses before any text is sent to the speech model."""
import re
from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class Speech:
    text: str


@dataclass(frozen=True)
class Silence:
    seconds: Decimal


BREAK_START = re.compile(r"<\s*/?\s*break\b", re.IGNORECASE)
BREAK_TAG = re.compile(
    r'''<break\s+time\s*=\s*(["'])(\d+(?:\.\d+)?)(ms|s)\1\s*/>''',
    re.IGNORECASE,
)
MAX_SILENCE_SECONDS = Decimal("3600")


def split_text(text, limit=180):
    """Bound inference memory, preferring punctuation and word boundaries."""
    sentences = re.split(r"(?<=[.!?;:])\s+|\n+", text.strip())
    chunks, current = [], ""
    for sentence in sentences:
        for word in sentence.split():
            while len(word) > limit:
                if current:
                    chunks.append(current)
                    current = ""
                chunks.append(word[:limit])
                word = word[limit:]
            if current and len(current) + len(word) + 1 > limit:
                chunks.append(current)
                current = word
            else:
                current = f"{current} {word}".strip()
        if len(current) >= limit // 2:
            chunks.append(current)
            current = ""
    if current:
        chunks.append(current)
    return chunks


def parse_script(text):
    segments = []
    offset = 0
    total_silence = Decimal(0)
    for start in BREAK_START.finditer(text):
        tag = BREAK_TAG.match(text, start.start())
        if tag is None:
            raise ValueError('Некоректний тег паузи. Приклад: <break time="10.0s" /> або <break time="500ms" />.')
        segments.extend(Speech(chunk) for chunk in split_text(text[offset:start.start()]))
        seconds = Decimal(tag[2]) / (1000 if tag[3].lower() == "ms" else 1)
        # Compare before adding: do not round away very large values in Decimal's context.
        if seconds > MAX_SILENCE_SECONDS or total_silence + seconds > MAX_SILENCE_SECONDS:
            raise ValueError("Сумарна тривалість пауз в одному записі не може перевищувати 1 годину.")
        total_silence += seconds
        segments.append(Silence(seconds))
        offset = tag.end()
    segments.extend(Speech(chunk) for chunk in split_text(text[offset:]))
    if not any(isinstance(segment, Speech) or segment.seconds > 0 for segment in segments):
        raise ValueError("Введіть текст для озвучення або тег паузи з додатною тривалістю.")
    return segments
