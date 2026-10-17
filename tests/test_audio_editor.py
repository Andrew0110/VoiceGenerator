import io

import numpy as np
import pytest
import soundfile as sf
from fastapi.testclient import TestClient

from voice_studio.app import create_app
from voice_studio.audio_editor import EditorTrack, render_mix
from voice_studio.storage import identifier
from test_studio import FakeEngine, wait_job


def audio_bytes(values=None, sample_rate=24000, output_format='WAV'):
    if values is None:
        values = np.tile([.2, -.2], sample_rate // 2)
    stream = io.BytesIO()
    sf.write(stream, values, sample_rate, format=output_format, subtype='PCM_16')
    return stream.getvalue()


def upload(client, values=None, sample_rate=24000):
    response = client.post('/api/editor/sources', files={
        'sample': ('music.wav', audio_bytes(values, sample_rate), 'audio/wav'),
    })
    assert response.status_code == 201, response.text
    return response.json()


def spec(source, **settings):
    return {'kind': 'upload', 'id': source['id'], **settings}


def read(response):
    assert response.status_code in (200, 201), response.text
    return sf.read(io.BytesIO(response.content), always_2d=True)


def test_two_sources_offset_stereo_and_fades_match_preview_and_export(tmp_path):
    with TestClient(create_app(tmp_path, FakeEngine(False))) as client:
        first = upload(client, np.tile([.2, -.1], (24000, 1)))
        second = upload(client, np.tile([.1, .3], (48000, 1)), sample_rate=48000)
        assert first['duration'] == 1 and len(first['peaks']) == 180
        tracks = [spec(first, fade_in=.25, fade_out=.25), spec(second, start=.5)]
        preview, rate = read(client.post('/api/editor/preview', json={'tracks': tracks}))
        assert rate == 48000 and preview.shape == (72000, 2)
        assert np.max(np.abs(preview[0])) < 1e-4
        np.testing.assert_allclose(preview[18000], [.2, -.1], atol=.001)
        np.testing.assert_allclose(preview[30000], [.3, .2], atol=.001)
        np.testing.assert_allclose(preview[60000], [.1, .3], atol=.001)
        result = client.post('/api/editor/mixes', json={'tracks': tracks, 'name': 'Перший мікс'})
        assert result.status_code == 201, result.text
        job = result.json()
        assert job['duration'] == 1.5 and job['kind'] == 'mix' and job['name'] == 'Перший мікс'
        actual, _ = read(client.get(f"/api/generations/{job['id']}/audio"))
        np.testing.assert_array_equal(actual, preview)
        assert len(client.get('/api/generations').json()) == 1
        assert not list(tmp_path.glob('preview-*'))
        assert not list((tmp_path / 'outputs').glob('editor-save-*'))


def test_overlap_gain_prevents_clipping_preserves_balance_and_silent_offsets(tmp_path):
    with TestClient(create_app(tmp_path, FakeEngine())) as client:
        source = upload(client, np.tile([.8, .4], (24000, 1)))
        audio, _ = read(client.post('/api/editor/preview', json={
            'tracks': [spec(source, start=.5), spec(source, start=.5)],
        }))
        assert not audio[:24000].any()
        assert np.max(audio) < 1
        np.testing.assert_allclose(audio[40000], [1, .5], atol=.001)


@pytest.mark.parametrize('output_format', ['wav', 'flac', 'mp3'])
def test_generated_source_copy_and_replace_preserve_identity_and_format(tmp_path, output_format):
    with TestClient(create_app(tmp_path, FakeEngine())) as client:
        initial = client.post('/api/generations', json={'text': 'Оригінал', 'output_format': output_format}).json()
        initial = wait_job(client, initial['id'])
        original = client.get(f"/api/generations/{initial['id']}/audio").content
        description = client.get(f"/api/editor/sources/generation/{initial['id']}")
        assert description.status_code == 200 and len(description.json()['peaks']) == 180
        source = upload(client)
        tracks = [dict(kind='generation', id=initial['id']), spec(source, start=1)]
        copy = client.post('/api/editor/mixes', json={'tracks': tracks, 'name': 'Копія', 'output_format': output_format})
        assert copy.status_code == 201, copy.text
        assert copy.json()['id'] != initial['id']
        assert client.get(f"/api/generations/{initial['id']}/audio").content == original
        replaced = client.post('/api/editor/mixes', json={
            'tracks': tracks, 'replace_id': initial['id'], 'name': 'Заміна', 'output_format': 'flac',
        })
        assert replaced.status_code == 201, replaced.text
        row = replaced.json()
        assert row['id'] == initial['id'] and row['output_format'] == output_format
        assert row['duration'] == 2 and row['kind'] == 'mix' and row['name'] == 'Заміна'
        assert row['created_at'] == initial['created_at']
        assert len(client.get('/api/generations').json()) == 2
        audio, rate = read(client.get(f"/api/generations/{initial['id']}/audio"))
        assert rate == 48000 and audio.shape == (96000, 2)
        assert client.delete(f"/api/generations/{row['id']}").status_code == 200


@pytest.mark.parametrize('overrides', [
    {'start': -.1}, {'start': 601}, {'start': 'NaN'}, {'fade_in': 'Infinity'},
    {'fade_in': -1}, {'fade_out': 2}, {'kind': 'file'}, {'id': '../secret'},
])
def test_invalid_tracks_leave_original_untouched(tmp_path, overrides):
    with TestClient(create_app(tmp_path, FakeEngine())) as client:
        source = upload(client)
        response = client.post('/api/editor/mixes', json={'tracks': [spec(source, **overrides)]})
        assert response.status_code == 422, response.text
        assert client.get('/api/generations').json() == []
        assert not list((tmp_path / 'outputs').iterdir())


def test_bad_missing_sources_empty_mix_and_unrelated_replacement(tmp_path):
    with TestClient(create_app(tmp_path, FakeEngine())) as client:
        response = client.post('/api/editor/sources', files={'sample': ('bad.wav', b'garbage')})
        assert response.status_code == 422
        assert not list((tmp_path / 'editor_sources').iterdir())
        source = upload(client)
        assert client.post('/api/editor/mixes', json={'tracks': []}).status_code == 422
        assert client.post('/api/editor/preview', json={'tracks': [spec({'id': identifier()})]}).status_code == 404
        assert client.post('/api/editor/mixes', json={'tracks': [spec(source)], 'name': '   '}).status_code == 422
        initial = client.post('/api/generations', json={'text': 'Оригінал'}).json()
        wait_job(client, initial['id'])
        assert client.post('/api/editor/mixes', json={
            'tracks': [spec(source)], 'replace_id': initial['id'],
        }).status_code == 422
        assert client.delete(f"/api/editor/sources/{source['id']}").status_code == 200
        assert client.get(f"/api/editor/sources/upload/{source['id']}").status_code == 404
        assert not list((tmp_path / 'editor_sources').iterdir())


def test_failed_encoding_keeps_original_audio_and_metadata(tmp_path, monkeypatch):
    with TestClient(create_app(tmp_path, FakeEngine())) as client:
        initial = client.post('/api/generations', json={'text': 'Оригінал'}).json()
        job = wait_job(client, initial['id'])
        url = f"/api/generations/{job['id']}"
        original = client.get(url + '/audio').content
        def fail(*_):
            raise RuntimeError('Encoder failed')
        monkeypatch.setattr('voice_studio.audio_editor.export_audio', fail)
        response = client.post('/api/editor/mixes', json={
            'tracks': [dict(kind='generation', id=job['id'])], 'replace_id': job['id'],
        })
        assert response.status_code == 422
        assert client.get(url).json() == job
        assert client.get(url + '/audio').content == original
        assert len(list((tmp_path / 'outputs').iterdir())) == 1


def test_database_failure_rolls_back_replacement_file(tmp_path, monkeypatch):
    from contextlib import contextmanager
    with TestClient(create_app(tmp_path, FakeEngine()), raise_server_exceptions=False) as client:
        initial = client.post('/api/generations', json={'text': 'Оригінал'}).json()
        job = wait_job(client, initial['id'])
        url = f"/api/generations/{job['id']}"
        original = client.get(url + '/audio').content
        store = client.app.state.store
        connect = store.connect
        class FailingDB:
            def execute(self, statement, *args):
                if statement.startswith('UPDATE jobs SET id') or statement.startswith('UPDATE jobs SET name'):
                    raise RuntimeError('Database write failed')
                return self.db.execute(statement, *args)
        @contextmanager
        def failing_connect():
            with connect() as db:
                wrapper = FailingDB(); wrapper.db = db
                yield wrapper
        monkeypatch.setattr(store, 'connect', failing_connect)
        response = client.post('/api/editor/mixes', json={
            'tracks': [dict(kind='generation', id=job['id'], start=1)], 'replace_id': job['id'],
        })
        assert response.status_code == 500
        assert client.get(url).json() == job
        assert client.get(url + '/audio').content == original


def test_short_silent_imports_and_browser_recorded_containers_are_supported(tmp_path):
    from voice_studio.audio_processing import run_ffmpeg
    wav_path = tmp_path / 'short.wav'
    wav_path.write_bytes(audio_bytes(np.zeros(2400)))
    webm = tmp_path / 'microphone.webm'
    run_ffmpeg(['-y', '-i', str(wav_path), '-c:a', 'libopus', str(webm)])
    with TestClient(create_app(tmp_path, FakeEngine())) as client:
        short = upload(client, np.zeros(2400))
        assert short['duration'] == .1
        recorded = client.post('/api/editor/sources', files={'sample': ('microphone.webm', webm.read_bytes(), 'audio/webm')})
        assert recorded.status_code == 201, recorded.text
        assert .09 < recorded.json()['duration'] < .11


def test_long_import_is_rejected_instead_of_truncated(tmp_path, monkeypatch):
    monkeypatch.setattr('voice_studio.audio_editor.MAX_SOURCE_SECONDS', 1)
    # decode_source's explicit limit can be exercised without a ten-minute fixture.
    from voice_studio.audio_editor import decode_source
    source = tmp_path / 'long.wav'
    source.write_bytes(audio_bytes(np.zeros(48000)))
    destination = tmp_path / 'result.wav'
    with pytest.raises(ValueError, match='не більше'):
        decode_source(source, destination, max_seconds=1)
    assert not destination.exists()


def test_multiple_source_sample_rates_are_converted_without_changing_timing(tmp_path):
    sources = []
    for index, rate in enumerate([22050, 44100]):
        path = tmp_path / f'{index}.wav'
        sf.write(path, np.full(rate, .1), rate)
        sources.append((EditorTrack(kind='upload', id=identifier(), start=index), path))
    destination = tmp_path / 'out.wav'
    assert render_mix(sources, destination) == 2
    audio, rate = sf.read(destination, always_2d=True)
    assert rate == 48000 and audio.shape == (96000, 2)


def test_trim_uses_source_times_and_fades_apply_to_retained_fragment(tmp_path):
    with TestClient(create_app(tmp_path, FakeEngine(False))) as client:
        # Each half-second has a distinct level, so wrong source offsets are audible.
        values = np.repeat([[.1, -.05], [.2, -.1], [.3, -.15]], 24000, axis=0)
        source = upload(client, values, sample_rate=48000)
        tracks = [spec(source, trim_start=.5, trim_end=1, start=.25, fade_in=.1, fade_out=.1, volume=2)]
        preview, rate = read(client.post('/api/editor/preview', json={'tracks': tracks}))
        assert rate == 48000 and preview.shape == (36000, 2)
        assert not preview[:12000].any()
        assert np.max(np.abs(preview[12000])) < 1e-4
        np.testing.assert_allclose(preview[24000], [.4, -.2], atol=.001)
        assert np.max(np.abs(preview[-1])) < 1e-4
        result = client.post('/api/editor/mixes', json={'tracks': tracks, 'name': 'Обрізано й посилено'})
        assert result.status_code == 201, result.text
        assert result.json()['duration'] == .75
        actual, _ = read(client.get(f"/api/generations/{result.json()['id']}/audio"))
        np.testing.assert_array_equal(actual, preview)
        # Trimming only affects the rendered output, never the imported source.
        original = client.get(f"/api/editor/sources/upload/{source['id']}").json()
        assert original['duration'] == 1.5


@pytest.mark.parametrize('volume', [0, .5, 1, 2, 4])
def test_track_volume_mutes_attenuates_and_amplifies_both_channels(tmp_path, volume):
    with TestClient(create_app(tmp_path, FakeEngine(False))) as client:
        source = upload(client, np.tile([.15, -.05], (24000, 1)))
        actual, _ = read(client.post('/api/editor/preview', json={'tracks': [spec(source, volume=volume)]}))
        np.testing.assert_allclose(actual[20000], [.15 * volume, -.05 * volume], atol=.001)
        if volume == 0:
            assert not actual.any()


@pytest.mark.parametrize('settings', [
    {'trim_start': -.1}, {'trim_start': 1}, {'trim_start': .8, 'trim_end': .5},
    {'trim_end': 0}, {'trim_end': 1.1}, {'trim_start': 'NaN'}, {'trim_end': 'Infinity'},
    {'volume': -.1}, {'volume': 4.01}, {'volume': 'NaN'}, {'volume': 'Infinity'},
    {'trim_start': .5, 'trim_end': .75, 'fade_in': .3},
])
def test_invalid_trim_and_volume_do_not_create_outputs(tmp_path, settings):
    with TestClient(create_app(tmp_path, FakeEngine(False))) as client:
        source = upload(client)
        response = client.post('/api/editor/mixes', json={'tracks': [spec(source, **settings)]})
        assert response.status_code == 422, response.text
        assert not list((tmp_path / 'outputs').iterdir())
        assert client.get('/api/generations').json() == []


def test_trim_across_render_blocks_and_subsample_rounding(tmp_path):
    source = tmp_path / 'source.wav'
    values = np.column_stack((np.linspace(-.3, .3, 4 * 48000), np.full(4 * 48000, .1)))
    sf.write(source, values, 48000, subtype='FLOAT')
    target = tmp_path / 'trimmed.wav'
    track = EditorTrack(kind='upload', id=identifier(), trim_start=1, trim_end=3)
    assert render_mix([(track, source)], target) == 2
    actual, _ = sf.read(target, always_2d=True)
    np.testing.assert_allclose(actual, values[48000:144000], atol=4e-5)
    # A computed fade duration can differ by a floating-point epsilon from the
    # retained duration. Render using integer audio frames for both boundaries.
    track = EditorTrack(kind='upload', id=identifier(), trim_start=.1, trim_end=.8, fade_in=.8 - .1)
    assert render_mix([(track, source)], target) == .7


def test_automatic_loop_follows_main_track_with_trim_offset_and_partial_last_cycle(tmp_path):
    with TestClient(create_app(tmp_path, FakeEngine(False))) as client:
        anchor = upload(client, np.zeros((64800, 2)), sample_rate=48000)  # 1.35 seconds
        values = np.column_stack((np.linspace(-.2, .2, 14400), np.full(14400, .1)))
        source = upload(client, values, sample_rate=48000)
        tracks = [spec(anchor), spec(source, loop=True, trim_start=.1, trim_end=.2, start=.3, volume=.5)]
        actual, rate = read(client.post('/api/editor/preview', json={'tracks': tracks}))
        assert rate == 48000 and actual.shape == (64800, 2)
        assert not actual[:14400].any()
        expected = values[4800 + np.arange(50400) % 4800] * .5
        np.testing.assert_allclose(actual[14400:], expected, atol=5e-5)
        saved = client.post('/api/editor/mixes', json={'tracks': tracks, 'name': 'Циклічний фон'})
        assert saved.status_code == 201, saved.text
        assert saved.json()['duration'] == 1.35
        exported, _ = read(client.get(f"/api/generations/{saved.json()['id']}/audio"))
        np.testing.assert_array_equal(exported, actual)


def test_long_automatic_loop_does_not_extend_shorter_main_track(tmp_path):
    with TestClient(create_app(tmp_path, FakeEngine(False))) as client:
        anchor = upload(client, np.zeros((12000, 2)), sample_rate=48000)
        source = upload(client, np.full((48000, 2), .1), sample_rate=48000)
        actual, _ = read(client.post('/api/editor/preview', json={'tracks': [spec(anchor), spec(source, loop=True)]}))
        assert actual.shape == (12000, 2)
        np.testing.assert_allclose(actual, .1, atol=5e-5)


def test_explicit_loop_duration_fades_once_and_repeats_across_block_boundaries(tmp_path):
    with TestClient(create_app(tmp_path, FakeEngine(False))) as client:
        # Longer than one processing block, so this also exercises wraparound reads.
        values = np.tile([.2, -.1], (72000, 1))
        source = upload(client, values, sample_rate=48000)
        tracks = [spec(source, loop=True, loop_duration=3.75, fade_in=.25, fade_out=.5)]
        actual, _ = read(client.post('/api/editor/preview', json={'tracks': tracks}))
        assert actual.shape == (180000, 2)
        assert not actual[0].any() and not actual[-1].any()
        for frame in [20000, 65536, 71999, 72000, 131072, 144000]:
            np.testing.assert_allclose(actual[frame], [.2, -.1], atol=.001)
        np.testing.assert_allclose(actual[168000], [.1, -.05], atol=.001)


def test_all_automatic_loops_use_longest_natural_cycle_as_fallback(tmp_path):
    with TestClient(create_app(tmp_path, FakeEngine(False))) as client:
        short = upload(client, np.full((12000, 2), .1), sample_rate=48000)
        long = upload(client, np.full((48000, 2), .1), sample_rate=48000)
        actual, _ = read(client.post('/api/editor/preview', json={'tracks': [spec(short, loop=True), spec(long, loop=True)]}))
        assert actual.shape == (48000, 2)
        np.testing.assert_allclose(actual, .2, atol=.001)


def test_many_tracks_have_no_fixed_count_limit_and_decode_duplicate_source_once(tmp_path, monkeypatch):
    from voice_studio import audio_editor
    decode = audio_editor.decode_source
    calls = []
    def counting_decode(*args, **kwargs):
        calls.append(args[0])
        return decode(*args, **kwargs)
    with TestClient(create_app(tmp_path, FakeEngine(False))) as client:
        source = upload(client, np.full((4800, 2), .01), sample_rate=48000)
        monkeypatch.setattr(audio_editor, 'decode_source', counting_decode)
        response = client.post('/api/editor/mixes', json={'tracks': [spec(source)] * 80, 'name': '80 доріжок'})
        assert response.status_code == 201, response.text
        assert len(calls) == 1
        actual, _ = read(client.get(f"/api/generations/{response.json()['id']}/audio"))
        assert actual.shape == (4800, 2)
        np.testing.assert_allclose(actual[1000], [.8, .8], atol=.003)


@pytest.mark.parametrize('settings', [
    {'loop': True, 'loop_duration': 0}, {'loop': True, 'loop_duration': -1},
    {'loop': True, 'loop_duration': 'NaN'}, {'loop': True, 'loop_duration': 'Infinity'},
    {'loop': True, 'loop_duration': 1201}, {'loop': True, 'loop_duration': 1200, 'start': 1},
    {'loop': True, 'loop_duration': .5, 'fade_out': .6},
])
def test_bad_loop_durations_never_publish_output(tmp_path, settings):
    with TestClient(create_app(tmp_path, FakeEngine(False))) as client:
        source = upload(client)
        response = client.post('/api/editor/mixes', json={'tracks': [spec(source, **settings)]})
        assert response.status_code == 422, response.text
        assert not list((tmp_path / 'outputs').iterdir())


def test_one_sample_cycle_repeats_without_per_sample_io(tmp_path):
    with TestClient(create_app(tmp_path, FakeEngine(False))) as client:
        anchor = upload(client, np.zeros((9600, 2)), sample_rate=48000)
        source = upload(client, np.array([[.1, -.2]]), sample_rate=48000)
        actual, _ = read(client.post('/api/editor/preview', json={'tracks': [spec(anchor), spec(source, loop=True)]}))
        assert actual.shape == (9600, 2)
        np.testing.assert_allclose(actual, np.tile([.1, -.2], (9600, 1)), atol=.001)


def test_automatic_loop_after_other_audio_plays_one_cycle(tmp_path):
    with TestClient(create_app(tmp_path, FakeEngine(False))) as client:
        source = upload(client, np.tile([.1, -.1], (2400, 1)), sample_rate=48000)
        actual, _ = read(client.post('/api/editor/preview', json={'tracks': [spec(source), spec(source, loop=True, start=.1)]}))
        assert actual.shape == (7200, 2)
        assert not actual[2400:4800].any()
        np.testing.assert_allclose(actual[4800:], np.tile([.1, -.1], (2400, 1)), atol=.001)


@pytest.mark.parametrize('bitrate', [None, 64, 96, 128])
def test_editor_mp3_quality_is_encoded_persisted_and_can_change_on_replace(tmp_path, bitrate):
    import re
    import subprocess
    import imageio_ffmpeg

    expected = 64 if bitrate is None else bitrate
    def encoded_bitrate(key, quality):
        recording = tmp_path / 'outputs' / f'{key}.mp3'
        probe = subprocess.run([
            imageio_ffmpeg.get_ffmpeg_exe(), '-hide_banner', '-i', str(recording),
            '-t', '0', '-f', 'null', '-',
        ], capture_output=True, text=True, check=True)
        assert re.search(rf'Audio: mp3[^\n]*\b{quality} kb/s', probe.stderr)
        assert sf.info(recording).channels == 2 and sf.info(recording).samplerate == 48000
        assert sf.info(recording).duration == 1

    with TestClient(create_app(tmp_path, FakeEngine(False))) as client:
        source = upload(client)
        payload = {'tracks': [spec(source)], 'output_format': 'mp3', 'name': 'Якість міксу'}
        if bitrate is not None:
            payload['mp3_bitrate'] = bitrate
        response = client.post('/api/editor/mixes', json=payload)
        assert response.status_code == 201, response.text
        job = response.json()
        assert job['mp3_bitrate'] == expected
        encoded_bitrate(job['id'], expected)
        assert client.get('/api/generations').json()[0]['mp3_bitrate'] == expected
        changed_quality = {64: 96, 96: 128, 128: 64}[expected]
        response = client.post('/api/editor/mixes', json={
            'tracks': [{'kind': 'generation', 'id': job['id']}], 'replace_id': job['id'],
            'output_format': 'flac', 'mp3_bitrate': changed_quality, 'name': 'Інша якість',
        })
        assert response.status_code == 201, response.text
        assert response.json()['id'] == job['id']
        assert response.json()['output_format'] == 'mp3'
        assert response.json()['mp3_bitrate'] == changed_quality
        encoded_bitrate(job['id'], changed_quality)
    with TestClient(create_app(tmp_path, FakeEngine(False))) as client:
        assert client.get(f"/api/generations/{job['id']}").json()['mp3_bitrate'] == changed_quality


@pytest.mark.parametrize('bitrate', [0, 32, 192, 'NaN', None])
def test_editor_rejects_unsupported_mp3_quality(tmp_path, bitrate):
    with TestClient(create_app(tmp_path, FakeEngine(False))) as client:
        source = upload(client)
        response = client.post('/api/editor/mixes', json={
            'tracks': [spec(source)], 'output_format': 'mp3', 'mp3_bitrate': bitrate,
        })
        assert response.status_code == 422, response.text
        assert client.get('/api/generations').json() == []
        assert not list((tmp_path / 'outputs').iterdir())
