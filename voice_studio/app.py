import asyncio
import logging
import re
import shutil
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal

import soundfile as sf
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.concurrency import run_in_threadpool
from starlette.background import BackgroundTask

from .config import DATA, LANGUAGES, ROOT
from .audio_processing import MAX_SPEED, MIN_SPEED, MIME_TYPES
from .audio_editor import MAX_TIMELINE_SECONDS, MixRequest, decode_source, render_mix, waveform
from .engine import LocalEngine
from .speech_script import Speech, parse_script
from .storage import Storage, identifier, now
from .voice_import import MAX_TRANSCRIPT, MAX_UPLOAD, import_voice

ID_PATTERN = re.compile(r"^[a-f0-9]{32}$")


class GenerateRequest(BaseModel):
    text: str = Field(min_length=1, max_length=3000)
    voice_id: str | None = None
    language: str = "uk"
    steps: int = Field(default=32)
    output_format: Literal["mp3", "flac", "wav"] = "wav"
    speed: float = Field(default=1.0, ge=MIN_SPEED, le=MAX_SPEED, allow_inf_nan=False)
    mp3_bitrate: Literal[64, 96, 128] = 64

    @field_validator("text")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("Введіть текст для озвучення.")
        parse_script(value)
        return value.strip()

    @field_validator("language")
    @classmethod
    def language_supported(cls, value):
        if value not in LANGUAGES:
            raise ValueError("Оберіть українську або англійську мову.")
        return value

    @field_validator("steps")
    @classmethod
    def quality_supported(cls, value):
        if value not in (16, 32, 48):
            raise ValueError("Невідомий режим якості.")
        return value


class RenameRequest(BaseModel):
    name: str = Field(min_length=1, max_length=60)


def create_app(root: Path = DATA, engine=None):
    store = Storage(root)
    engine = engine or LocalEngine()
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="voice-inference")
    submit_lock = asyncio.Lock()

    @asynccontextmanager
    async def lifespan(app):
        store.interrupt_jobs()
        yield
        executor.shutdown(wait=True)

    app = FastAPI(title="Голос — локальна студія", lifespan=lifespan)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"])
    app.state.store = store
    app.state.engine = engine

    @app.middleware("http")
    async def local_requests(request: Request, call_next):
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            expected = f"{request.url.scheme}://{request.headers.get('host')}"
            if (origin and origin != expected) or request.headers.get("sec-fetch-site") == "cross-site":
                return JSONResponse({"detail": "Запит дозволений лише з локального застосунку."}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; media-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        return response

    def find(table, key):
        if not ID_PATTERN.fullmatch(key):
            raise HTTPException(404, "Запис не знайдено.")
        row = store.get(table, key)
        if row is None:
            raise HTTPException(404, "Запис не знайдено.")
        return row

    def audio(path):
        if not path.is_file():
            raise HTTPException(404, "Аудіофайл не знайдено.")
        return FileResponse(path, media_type=MIME_TYPES[path.suffix.lstrip(".")], filename=path.name, content_disposition_type="inline")

    def run_job(job, voice):
        key = job["id"]
        def report(progress, message):
            store.update_job(key, progress=progress, message=message)
        try:
            store.update_job(key, status="running", message="Підготовка до генерації…")
            duration = engine.generate(
                job["text"], job["language"], job["steps"],
                root / "voices" / f"{voice['id']}.wav" if voice else None,
                voice["transcript"] if voice else None,
                root / "outputs" / f"{key}.{job['output_format']}", report, speed=job["speed"],
                mp3_bitrate=job["mp3_bitrate"],
            )
            store.update_job(key, status="completed", progress=100, message="Аудіо готове", duration=duration, finished_at=now())
        except Exception as exc:
            logging.exception("Generation failed: %s", key)
            store.update_job(key, status="failed", message=f"Не вдалося створити аудіо: {str(exc)[:350]}", finished_at=now())

    @app.get("/api/status")
    def status():
        return {"model": "OmniVoice", "installed": engine.installed(), "loaded": engine.model is not None,
                "offline": True, "languages": list(LANGUAGES)}

    @app.get("/api/voices")
    def voices():
        return store.all("voices")

    @app.post("/api/voices", status_code=201)
    async def add_voice(name: str = Form(...), transcript: str = Form(...),
                        language: str = Form("uk"), sample: UploadFile = File(...)):
        name, transcript = name.strip(), transcript.strip()
        if not 1 <= len(name) <= 60 or not 1 <= len(transcript) <= MAX_TRANSCRIPT or language not in LANGUAGES:
            raise HTTPException(422, "Заповніть назву, мову та точний текст запису.")
        key = identifier()
        path = root / "voices" / f"{key}.wav"
        try:
            with TemporaryDirectory(prefix="voice-upload-", dir=root) as temporary:
                source = Path(temporary) / "sample"
                size = 0
                with source.open("wb") as uploaded:
                    while chunk := await sample.read(1024 * 1024):
                        size += len(chunk)
                        if size > MAX_UPLOAD:
                            raise HTTPException(413, "Максимальний розмір запису — 64 МБ.")
                        uploaded.write(chunk)
                try:
                    duration = await run_in_threadpool(import_voice, source, path)
                except (ValueError, RuntimeError, OSError, sf.LibsndfileError) as exc:
                    raise HTTPException(422, f"Не вдалося прочитати голос. Використайте аудіофайл або запис із мікрофона. {exc}") from exc
        finally:
            await sample.close()
        row = dict(id=key, name=name, transcript=transcript, language=language, duration=duration, created_at=now())
        try:
            store.insert("voices", row)
        except Exception:
            path.unlink(missing_ok=True)
            raise
        return row

    @app.get("/api/voices/{key}/audio")
    def voice_audio(key: str):
        find("voices", key)
        return audio(root / "voices" / f"{key}.wav")

    @app.patch("/api/voices/{key}")
    def rename_voice(key: str, body: RenameRequest):
        find("voices", key)
        if not body.name.strip():
            raise HTTPException(422, "Введіть назву голосу.")
        with store.connect() as db:
            db.execute("UPDATE voices SET name=? WHERE id=?", (body.name.strip(), key))
        return find("voices", key)

    @app.delete("/api/voices/{key}")
    async def delete_voice(key: str):
        async with submit_lock:
            find("voices", key)
            with store.connect() as db:
                if db.execute("SELECT 1 FROM jobs WHERE voice_id=? AND status IN ('queued','running')", (key,)).fetchone():
                    raise HTTPException(409, "Дочекайтеся завершення генерації цим голосом.")
                db.execute("DELETE FROM voices WHERE id=?", (key,))
            (root / "voices" / f"{key}.wav").unlink(missing_ok=True)
        return {"ok": True}

    @app.post("/api/generations", status_code=202)
    async def generate(body: GenerateRequest):
        if not engine.installed() and any(isinstance(segment, Speech) for segment in parse_script(body.text)):
            raise HTTPException(409, "Спочатку завантажте модель за допомогою setup.command.")
        async with submit_lock:
            voice = find("voices", body.voice_id) if body.voice_id else None
            with store.connect() as db:
                count = db.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('queued','running')").fetchone()[0]
            if count >= 5:
                raise HTTPException(429, "У черзі вже 5 записів. Дочекайтеся завершення одного з них.")
            row = dict(id=identifier(), text=body.text, voice_id=body.voice_id,
                       voice_name=voice["name"] if voice else "Голос моделі", language=body.language,
                       steps=body.steps, output_format=body.output_format, speed=body.speed, mp3_bitrate=body.mp3_bitrate,
                       status="queued", progress=0, message="У черзі", created_at=now())
            store.insert("jobs", row)
            executor.submit(run_job, row, voice)
        return row

    @app.get("/api/generations")
    def generations():
        return store.all("jobs")

    @app.get("/api/generations/{key}")
    def generation(key: str):
        return find("jobs", key)

    @app.get("/api/generations/{key}/audio")
    def generation_audio(key: str):
        row = find("jobs", key)
        if row["status"] != "completed":
            raise HTTPException(409, "Аудіо ще не готове.")
        return audio(root / "outputs" / f"{key}.{row['output_format']}")

    @app.delete("/api/generations/{key}")
    async def delete_generation(key: str):
        async with submit_lock:
            row = find("jobs", key)
            if row["status"] in ("queued", "running"):
                raise HTTPException(409, "Дочекайтеся завершення генерації.")
            with store.connect() as db:
                db.execute("DELETE FROM jobs WHERE id=?", (key,))
            (root / "outputs" / f"{key}.{row['output_format']}").unlink(missing_ok=True)
        return {"ok": True}

    def editor_source(kind, key):
        if kind == "generation":
            row = find("jobs", key)
            if row["status"] != "completed":
                raise HTTPException(409, "Оберіть завершений запис.")
            path = root / "outputs" / f"{key}.{row['output_format']}"
        elif kind == "upload":
            row = find("editor_sources", key)
            path = root / "editor_sources" / f"{key}.wav"
        else:
            raise HTTPException(404, "Джерело не знайдено.")
        if not path.is_file():
            raise HTTPException(404, "Аудіофайл не знайдено.")
        return row, path

    @app.post("/api/editor/sources", status_code=201)
    async def upload_editor_source(sample: UploadFile = File(...)):
        key = identifier()
        destination = root / "editor_sources" / f"{key}.wav"
        try:
            with TemporaryDirectory(prefix="editor-upload-", dir=root) as temporary:
                source = Path(temporary) / "sample"
                size = 0
                with source.open("wb") as output:
                    while chunk := await sample.read(1024 * 1024):
                        size += len(chunk)
                        if size > MAX_UPLOAD:
                            raise HTTPException(413, "Максимальний розмір файлу — 64 МБ.")
                        output.write(chunk)
                duration = await run_in_threadpool(decode_source, source, destination)
            row = dict(id=key, name=(Path(sample.filename or "Запис із мікрофона").name[:120]),
                       duration=duration, created_at=now())
            store.insert("editor_sources", row)
            return {**row, "peaks": await run_in_threadpool(lambda: waveform(destination)["peaks"])}
        except (ValueError, RuntimeError, OSError, sf.LibsndfileError) as exc:
            destination.unlink(missing_ok=True)
            with store.connect() as db:
                db.execute("DELETE FROM editor_sources WHERE id=?", (key,))
            raise HTTPException(422, f"Не вдалося прочитати доріжку. {exc}") from exc
        except Exception:
            destination.unlink(missing_ok=True)
            with store.connect() as db:
                db.execute("DELETE FROM editor_sources WHERE id=?", (key,))
            raise
        finally:
            await sample.close()

    @app.get("/api/editor/sources/{kind}/{key}")
    async def describe_editor_source(kind: str, key: str):
        async with submit_lock:
            row, source = editor_source(kind, key)
            def describe():
                with TemporaryDirectory(prefix="waveform-", dir=root) as temporary:
                    decoded = Path(temporary) / "audio.wav"
                    decode_source(source, decoded, MAX_TIMELINE_SECONDS if kind == "generation" else 600)
                    return waveform(decoded)
            try:
                return {"id": key, "name": row.get("name") or row.get("text") or "Доріжка",
                        **await run_in_threadpool(describe)}
            except (ValueError, RuntimeError, OSError, sf.LibsndfileError) as exc:
                raise HTTPException(422, str(exc)) from exc

    @app.delete("/api/editor/sources/{key}")
    async def delete_editor_source(key: str):
        async with submit_lock:
            find("editor_sources", key)
            with store.connect() as db:
                db.execute("DELETE FROM editor_sources WHERE id=?", (key,))
            (root / "editor_sources" / f"{key}.wav").unlink(missing_ok=True)
        return {"ok": True}

    def mix_inputs(body):
        return [(track, editor_source(track.kind, track.id)[1]) for track in body.tracks]

    @app.post("/api/editor/preview")
    async def preview_mix(body: MixRequest):
        async with submit_lock:
            sources = mix_inputs(body)
            directory = TemporaryDirectory(prefix="preview-", dir=root)
            destination = Path(directory.name) / "preview.wav"
            try:
                await run_in_threadpool(render_mix, sources, destination)
            except (ValueError, RuntimeError, OSError, sf.LibsndfileError) as exc:
                directory.cleanup()
                raise HTTPException(422, str(exc)) from exc
            except Exception:
                directory.cleanup()
                raise
        return FileResponse(destination, media_type="audio/wav", background=BackgroundTask(directory.cleanup))

    @app.post("/api/editor/mixes", status_code=201)
    async def save_mix(body: MixRequest):
        if not body.name.strip():
            raise HTTPException(422, "Введіть назву міксу.")
        async with submit_lock:
            sources = mix_inputs(body)
            previous = find("jobs", body.replace_id) if body.replace_id else None
            if previous and previous["status"] != "completed":
                raise HTTPException(409, "Замінити можна лише завершений запис.")
            if previous and not any(track.kind == "generation" and track.id == previous["id"] for track in body.tracks):
                raise HTTPException(422, "Для заміни оберіть одну з початкових доріжок.")
            key = previous["id"] if previous else identifier()
            output_format = previous["output_format"] if previous else body.output_format
            destination = root / "outputs" / f"{key}.{output_format}"
            with TemporaryDirectory(prefix="editor-save-", dir=root / "outputs") as temporary:
                rendered = Path(temporary) / f"mix.{output_format}"
                try:
                    duration = await run_in_threadpool(render_mix, sources, rendered, body.mp3_bitrate)
                except (ValueError, RuntimeError, OSError, sf.LibsndfileError) as exc:
                    raise HTTPException(422, str(exc)) from exc
                row = dict(id=key, name=body.name.strip(), kind="mix", text=body.name.strip(),
                           voice_id=None, voice_name="Аудіоредактор", language="uk", steps=32,
                           output_format=output_format, speed=1.0, mp3_bitrate=body.mp3_bitrate, status="completed", progress=100,
                           message="Мікс збережено", duration=duration,
                           created_at=previous["created_at"] if previous else now(), finished_at=now())
                backup = Path(temporary) / "original"
                if previous:
                    await run_in_threadpool(shutil.copyfile, destination, backup)
                try:
                    rendered.replace(destination)
                    if previous:
                        with store.connect() as db:
                            db.execute("UPDATE jobs SET " + ", ".join(f"{field}=?" for field in row if field != "id")
                                       + " WHERE id=?", (*[value for field, value in row.items() if field != "id"], key))
                    else:
                        store.insert("jobs", row)
                except Exception:
                    if previous:
                        backup.replace(destination)
                    else:
                        destination.unlink(missing_ok=True)
                    raise
            return find("jobs", key)

    app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")

    @app.get("/")
    def index():
        return FileResponse(ROOT / "static" / "index.html")

    return app
