/* Dynamic local tracks, loop-aware timeline, and server-rendered preview/export. */
(() => {
  const tracks = [];
  const cards = [];
  const player = $('#editor-player');
  let busy = false, recordingSlot = null, previewUrl = null, revision = 0, animation = null;
  let extent = 10, drag = null, choicesSignature = null;
  const versions = [];
  const activeRecording = () => ['requesting', 'recording', 'stopping'].includes(recorder.phase);
  const error = message => {
    $('#editor-error').textContent = message;
    $('#editor-error').hidden = !message;
    for (const card of cards) if (card) {
      card.querySelector('[data-dialog-error]').textContent = message;
      card.querySelector('[data-dialog-error]').hidden = !message;
    }
  };
  const seconds = value => `${Number(value).toLocaleString('uk-UA', { maximumFractionDigits: 2 })} с`;
  const clipDuration = track => (track.trim_end ?? track.duration) - (track.trim_start || 0);
  const trackSettings = ({ kind, id, start, fade_in, fade_out, trim_start, trim_end, volume, loop, loop_duration }) =>
    ({ kind, id, start, fade_in, fade_out, trim_start, trim_end, volume, loop, loop_duration });
  const naturalEnd = track => track.start + (track.loop && track.loop_duration != null ? track.loop_duration : clipDuration(track));
  function loopEnd() {
    const loaded = tracks.filter(Boolean);
    const anchors = loaded.filter(track => !track.loop || track.loop_duration != null);
    let end = (anchors.length ? anchors : loaded).reduce((value, track) => Math.max(value, naturalEnd(track)), 0);
    const base = end;
    for (const track of loaded) if (track.loop && track.loop_duration == null && track.start >= base) end = Math.max(end, naturalEnd(track));
    return end;
  }
  const playbackDuration = track => track.loop ? (track.loop_duration ?? Math.max(0, loopEnd() - track.start)) : clipDuration(track);
  const maxStart = track => Math.max(0, extent - (track.loop && track.loop_duration == null ? clipDuration(track) : playbackDuration(track)));
  const restoredSeconds = value => Number.isFinite(Number(value)) && Number(value) >= 0 && Number.isSafeInteger(Math.round(Number(value) * 48000)) ? Number(value) : 0;
  const totalDuration = () => tracks.reduce((end, track) => Math.max(end, track ? track.start + playbackDuration(track) : 0), 0);
  const rowNumber = index => cards.slice(0, index + 1).filter(Boolean).length;
  const body = () => ({
    tracks: tracks.filter(Boolean).map(trackSettings),
    name: $('#editor-name').value.trim(), output_format: $('#editor-format').value,
    mp3_bitrate: Number($('#editor-mp3-bitrate').value),
    replace_id: $('#editor-save-mode').value || null,
  });

  function stopPlayback() {
    player.pause();
    if (player.src) player.currentTime = 0;
    cancelAnimationFrame(animation);
    updatePlayhead();
  }
  function changed() {
    revision += 1;
    stopPlayback();
    player.removeAttribute('src');
    player.load();
    player.hidden = true;
    if (previewUrl) URL.revokeObjectURL(previewUrl);
    previewUrl = null;
    $('#editor-result').hidden = true;
    $('#editor-preview-status').textContent = tracks.some(Boolean) ? 'Прослухай розташування доріжок і переходи.' : 'Додай хоча б одну доріжку.';
    persist();
    updateControls();
  }
  function persist() {
    try {
      localStorage.setItem('holos-editor-draft', JSON.stringify({
        tracks: tracks.filter((_, index) => cards[index]).map(track => track && trackSettings(track)),
        name: $('#editor-name').value,
        output_format: $('#editor-format').value, mp3_bitrate: Number($('#editor-mp3-bitrate').value),
      }));
    } catch (_) { /* Editing remains available when browser storage is full. */ }
  }
  function updateControls() {
    const locked = busy || activeRecording();
    const loaded = tracks.some(Boolean);
    $('#editor-preview').disabled = locked || !loaded;
    $('#editor-preview').textContent = player.paused ? '▶ Прослухати мікс' : 'Ⅱ Пауза';
    $('#editor-save').disabled = locked || !loaded;
    $('#editor-stop').disabled = busy || !previewUrl;
    $('#editor-zoom').disabled = busy;
    $('#editor-add-track').disabled = locked;
    for (const card of cards) {
      if (!card) continue;
      for (const input of card.querySelectorAll('input, select, button')) input.disabled = locked;
      const stop = card.querySelector('[data-stop]');
      stop.hidden = !(recorder.phase === 'recording' && Number(card.dataset.slot) === recordingSlot);
      stop.disabled = false;
      card.querySelector('[data-edit]').disabled = locked || !tracks[Number(card.dataset.slot)];
      card.querySelector('[data-loop-duration]').disabled = locked || !tracks[Number(card.dataset.slot)]?.loop;
    }
    for (const control of $('#editor-save-form').querySelectorAll('input, select')) control.disabled = locked;
    updateSaveMode();
    updatePlayhead();
  }
  function updateExportSettings() {
    const isMP3 = $('#editor-format').value === 'mp3';
    $('#editor-mp3-settings').hidden = !isMP3;
    $('#editor-mp3-bitrate').disabled = busy || activeRecording() || !isMP3;
  }
  function updateSaveMode(loadTarget = false) {
    const select = $('#editor-save-mode');
    const value = select.value;
    const originals = [...new Map(tracks.filter(track => track?.kind === 'generation')
      .map(track => [track.id, track])).values()];
    select.replaceChildren(new Option('Окремий новий трек', ''), ...originals.map(track => new Option(`Замінити: ${track.name}`, track.id)));
    select.value = originals.some(track => track.id === value) ? value : '';
    const target = state.jobs.find(job => job.id === select.value);
    $('#editor-format').disabled = busy || activeRecording() || Boolean(select.value);
    if (target) $('#editor-format').value = target.output_format;
    if (loadTarget && target?.output_format === 'mp3') $('#editor-mp3-bitrate').value = target.mp3_bitrate || 128;
    updateExportSettings();
    $('#editor-save-hint').textContent = select.value
      ? 'Обраний початковий трек у розділі «Мої аудіо» буде замінено цим міксом. Його формат збережеться.'
      : 'Новий трек з’явиться в розділі «Мої аудіо». Початкові записи збережуться.';
    $('#editor-save').textContent = busy ? 'Обробка…' : select.value ? 'Замінити початковий трек ↗' : 'Зберегти новий трек ↗';
  }
  function refreshChoices() {
    const signature = JSON.stringify(state.jobs.filter(job => job.status === 'completed'));
    if (signature === choicesSignature) return;
    choicesSignature = signature;
    for (const card of cards) {
      if (!card) continue;
      const select = card.querySelector('[data-history]');
      const current = select.value;
      select.replaceChildren(new Option('Обери готовий запис…', ''), ...state.jobs.filter(job => job.status === 'completed')
        .map(job => new Option(`${job.name || job.text.slice(0, 65)} · ${durationLabel(job.duration)}`, job.id)));
      select.value = [...select.options].some(option => option.value === current) ? current : '';
    }
    updateSaveMode();
  }
  function updateCard(index) {
    const track = tracks[index], card = cards[index];
    if (!card) return;
    const number = rowNumber(index);
    card.querySelector('[data-row-title]').textContent = `Доріжка ${number}`;
    card.querySelector('[data-dialog-title]').textContent = `Доріжка ${number} · монтаж і звук`;
    card.querySelector('[data-remove]').setAttribute('aria-label', `Видалити доріжку ${number}`);
    card.querySelector('[data-edit]').setAttribute('aria-label', `Редагувати доріжку ${number}`);
    card.querySelector('[data-name]').textContent = track ? track.name : 'Аудіо ще не додано';
    card.querySelector('[data-source-info]').textContent = track
      ? `${track.kind === 'generation' ? 'Мої аудіо' : 'Файл / мікрофон'} · ${seconds(clipDuration(track))}${clipDuration(track) < track.duration ? ` із ${seconds(track.duration)}` : ''}`
      : 'З моїх аудіо, з файлу або просто зараз із мікрофона.';
    for (const field of ['start', 'fade_in', 'fade_out', 'trim_start', 'trim_end', 'volume']) {
      const input = card.querySelector(`[data-field="${field}"]`);
      input.value = track ? (field === 'volume' ? Math.round(track.volume * 100) : track[field]) : 0;
      if (field === 'start') input.removeAttribute('max');
      else input.max = field === 'volume' ? 400 : field.startsWith('trim_') ? track?.duration || 0 : track ? playbackDuration(track) : 0;
    }
    card.querySelector('[data-volume]').value = Math.round((track?.volume ?? 1) * 100);
    card.querySelector('[data-edit-summary]').textContent = track ? `${seconds(playbackDuration(track))} · ${Math.round(track.volume * 100)}%${track.loop ? ' · ↻ повторення' : ''}` : '';
    card.querySelector('[data-volume]').setAttribute('aria-label', `Гучність доріжки ${number}`);
    card.querySelector('[data-loop]').checked = Boolean(track?.loop);
    card.querySelector('[data-loop-duration]').value = track?.loop_duration ?? '';
    card.querySelector('[data-loop-duration]').disabled = busy || activeRecording() || !track?.loop;
    card.querySelector('[data-loop-hint]').textContent = track?.loop
      ? `Порожнє поле — до кінця спільної шкали. Зараз: ${seconds(playbackDuration(track))}. Переходи діють на весь час повторення.`
      : 'Повторювати обраний фрагмент. За замовчуванням — до кінця спільної шкали.';
    card.querySelector('[data-history]').value = track?.kind === 'generation' ? track.id : '';
  }
  async function discardUpload(track) {
    if (track?.kind === 'upload' && !tracks.some(other => other?.id === track.id)) {
      try { await api(`/api/editor/sources/${track.id}`, { method: 'DELETE' }); }
      catch (_) { /* A missing temporary source must not undo the new selection. */ }
    }
  }
  async function setTrack(index, source, kind) {
    const previous = tracks[index];
    tracks[index] = { ...source, kind, start: 0, fade_in: 0, fade_out: 0, trim_start: 0, trim_end: source.duration, volume: 1, loop: false, loop_duration: null };
    changed(); updateCard(index); renderTimeline();
    await discardUpload(previous);
  }
  async function loadGeneration(index, id) {
    if (!id || busy || activeRecording()) return;
    const version = ++versions[index];
    busy = true; error(''); updateControls();
    try {
      const source = await api(`/api/editor/sources/generation/${id}`);
      if (version === versions[index]) await setTrack(index, source, 'generation');
    } catch (exception) { error(exception.message); updateCard(index); }
    finally { busy = false; updateControls(); }
  }
  async function upload(index, file) {
    if (!file || busy) return;
    const version = ++versions[index];
    busy = true; error(''); updateControls();
    const form = new FormData(); form.set('sample', file);
    try {
      const source = await api('/api/editor/sources', { method: 'POST', body: form });
      if (version === versions[index]) await setTrack(index, source, 'upload');
      else await discardUpload({ ...source, kind: 'upload' });
    } catch (exception) { error(exception.message); }
    finally { busy = false; updateControls(); }
  }

  const recorder = new VoiceRecorder({
    minSeconds: .1, maxBytes: Infinity,
    onState: ({ phase, seconds: elapsed }) => {
      if (recordingSlot !== null) {
        const status = cards[recordingSlot].querySelector('[data-record-status]');
        status.textContent = phase === 'recording' ? `● Запис ${durationLabel(elapsed)}`
          : phase === 'requesting' ? 'Очікуємо дозвіл на мікрофон…'
            : phase === 'stopping' ? 'Підготовка запису…' : '';
      }
      updateControls();
    },
    onError: message => { error(message); recordingSlot = null; updateControls(); },
    onReady: blob => {
      const index = recordingSlot;
      recordingSlot = null;
      if (index === null) return;
      const extension = blob.type.includes('mp4') ? 'm4a' : blob.type.includes('ogg') ? 'ogg' : 'webm';
      upload(index, new File([blob], `Мікрофон-${new Date().toISOString().slice(11, 19).replaceAll(':', '-')}.${extension}`, { type: blob.type }));
    },
  });

  function createCard(index) {
    const card = element('section', 'card editor-source');
    card.dataset.slot = index;
    card.innerHTML = `
      <div class="track-row-main">
        <h2 data-row-title>Доріжка ${index + 1}</h2>
        <h3 data-name>Аудіо ще не додано</h3><p class="field-hint" data-source-info>З моїх аудіо, з файлу або просто зараз із мікрофона.</p>
        <p class="track-row-summary" data-edit-summary></p>
        <div class="track-source-fields"><div><label for="editor-history-${index}">Збережені аудіо</label><select id="editor-history-${index}" data-history><option value="">Обери готовий запис…</option></select></div>
        <div><label for="editor-file-${index}">Додати з файлу</label><input id="editor-file-${index}" data-file type="file" accept=".wav,.flac,.aiff,.aif,.mp3,.m4a,.mp4,.webm,.ogg"></div></div>
        <div class="editor-record"><button class="secondary" data-record type="button">● Записати</button><button class="secondary recording-stop" data-stop type="button" hidden>■ Зупинити</button><span class="field-hint" role="status" data-record-status></span></div>
      </div>
      <div class="track-row-actions">
        <button class="track-icon" data-remove type="button" aria-label="Видалити доріжку ${index + 1}" title="Видалити доріжку"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7M14 10v7"/></svg></button>
        <button class="track-icon" data-edit type="button" aria-label="Редагувати доріжку ${index + 1}" title="Монтаж і звук" disabled><svg viewBox="0 0 24 24" aria-hidden="true"><path d="m15 4 5 5M4 20l5-1L21 7l-5-5L4 14z"/></svg></button>
      </div>
      <dialog class="track-dialog" aria-labelledby="track-dialog-title-${index}">
        <div class="dialog-heading"><h2 id="track-dialog-title-${index}" data-dialog-title>Доріжка ${index + 1} · монтаж і звук</h2><button class="icon-button" data-close-edit type="button" aria-label="Закрити опції">×</button></div>
        <section class="track-loop"><label class="loop-checkbox"><input id="editor-loop-${index}" data-loop type="checkbox"> Циклічне програвання</label>
          <label for="editor-loop-duration-${index}">Тривалість повторення, с</label><input id="editor-loop-duration-${index}" data-loop-duration type="number" min="0" step="any" placeholder="До кінця спільної шкали" disabled>
          <p class="field-hint" data-loop-hint>Повторювати обраний фрагмент до кінця спільної шкали.</p>
        </section>
        <div class="track-trim"><div class="card-top"><h4>Обрізання</h4><button class="text-button" data-reset-trim type="button">Увесь трек ↺</button></div>
          <div class="trim-fields"><div><label for="editor-trim-start-${index}">Залишити від, с</label><input id="editor-trim-start-${index}" data-field="trim_start" type="number" min="0" step="any" value="0"></div>
          <div><label for="editor-trim-end-${index}">До, с</label><input id="editor-trim-end-${index}" data-field="trim_end" type="number" min="0" step="any" value="0"></div></div>
          <p class="field-hint">Час у початковому файлі. Повторюється лише обраний фрагмент.</p>
        </div>
        <div class="track-effects">
          <div><label for="editor-start-${index}">Початок, с</label><input id="editor-start-${index}" data-field="start" type="number" min="0" step="any" value="0"></div>
          <div><label for="editor-in-${index}">Наростання, с</label><input id="editor-in-${index}" data-field="fade_in" type="number" min="0" step="any" value="0"></div>
          <div><label for="editor-out-${index}">Затухання, с</label><input id="editor-out-${index}" data-field="fade_out" type="number" min="0" step="any" value="0"></div>
        </div><p class="field-hint">Переходи діють на початку й наприкінці доріжки, без повторення на кожному циклі.</p>
        <div class="track-volume"><label for="editor-volume-${index}">Гучність, %</label><div class="volume-fields"><input id="editor-volume-range-${index}" data-volume type="range" min="0" max="400" step="1" value="100" aria-label="Гучність доріжки ${index + 1}"><input id="editor-volume-${index}" data-field="volume" type="number" min="0" max="400" step="1" value="100"></div>
        <div class="speed-scale"><span>0% · тиша</span><button class="text-button" data-reset-volume type="button">100% ↺</button><span>400% · гучніше</span></div></div>
        <p class="form-error" data-dialog-error role="alert" hidden></p>
        <div class="dialog-actions"><span class="field-hint">Зміни застосовуються одразу.</span><button class="primary" data-done-edit type="button">Готово</button></div>
      </dialog>`;
    card.querySelector('[data-history]').addEventListener('change', event => {
      if (event.target.value) loadGeneration(index, event.target.value);
      else updateCard(index);
    });
    card.querySelector('[data-file]').addEventListener('change', event => {
      const file = event.target.files[0]; event.target.value = ''; upload(index, file);
    });
    card.querySelector('[data-record]').addEventListener('click', () => {
      stopPlayback(); error(''); recordingSlot = index; recorder.start();
    });
    card.querySelector('[data-stop]').addEventListener('click', () => recorder.stop());
    card.querySelector('[data-edit]').addEventListener('click', () => {
      if (!tracks[index] || busy || activeRecording()) return;
      stopPlayback(); error(''); updateCard(index); card.querySelector('dialog').showModal();
    });
    for (const button of card.querySelectorAll('[data-close-edit], [data-done-edit]')) button.addEventListener('click', () => card.querySelector('dialog').close());
    card.querySelector('[data-remove]').addEventListener('click', async () => {
      const previous = tracks[index]; tracks[index] = null; versions[index] += 1;
      card.querySelector('dialog').close(); card.remove(); cards[index] = null;
      changed(); renderTimeline(); await discardUpload(previous);
    });
    card.querySelector('[data-loop]').addEventListener('change', event => {
      const track = tracks[index]; if (!track) return;
      track.loop = event.target.checked;
      track.fade_in = Math.min(track.fade_in, playbackDuration(track));
      track.fade_out = Math.min(track.fade_out, playbackDuration(track));
      error(''); changed(); renderTimeline();
    });
    card.querySelector('[data-loop-duration]').addEventListener('change', event => {
      const track = tracks[index]; if (!track) return;
      const value = event.target.value.trim() === '' ? null : Number(event.target.value);
      if (value !== null && (!Number.isFinite(value) || !Number.isSafeInteger(Math.round(value * 48000)) || Math.round(value * 48000) < 1)) {
        error('Задай додатну тривалість повторення або залиш поле порожнім.'); updateCard(index); return;
      }
      track.loop_duration = value;
      track.fade_in = Math.min(track.fade_in, playbackDuration(track)); track.fade_out = Math.min(track.fade_out, playbackDuration(track));
      error(''); changed(); renderTimeline();
    });
    card.querySelectorAll('[data-field]').forEach(input => input.addEventListener('change', () => {
      const value = Number(input.value), track = tracks[index];
      if (!track) return;
      const maximum = input.max === '' ? Infinity : Number(input.max);
      if (!input.value.trim() || !Number.isFinite(value) || !Number.isSafeInteger(Math.round(value * 48000)) || value < 0 || value > maximum) {
        error(Number.isFinite(maximum) ? `Введи число від 0 до ${input.max}.` : 'Введи коректний невід’ємний час у секундах.'); updateCard(index); return;
      }
      const field = input.dataset.field;
      const first = field === 'trim_start' ? value : track.trim_start;
      const last = field === 'trim_end' ? value : track.trim_end;
      if (first >= last || Math.round(first * 48000) >= Math.round(last * 48000)) {
        error('«Від» має бути меншим за «До». Залиши щонайменше один аудіосемпл.'); updateCard(index); return;
      }
      track[field] = field === 'volume' ? value / 100 : value;
      if (field.startsWith('trim_')) {
        track.fade_in = Math.min(track.fade_in, playbackDuration(track));
        track.fade_out = Math.min(track.fade_out, playbackDuration(track));
      }
      error(''); changed(); updateCard(index); renderTimeline();
    }));
    card.querySelector('[data-volume]').addEventListener('input', event => {
      if (!tracks[index]) return;
      tracks[index].volume = Number(event.target.value) / 100;
      error(''); changed(); updateCard(index); renderTimeline();
    });
    card.querySelector('[data-reset-volume]').addEventListener('click', () => {
      if (!tracks[index]) return;
      tracks[index].volume = 1; error(''); changed(); updateCard(index); renderTimeline();
    });
    card.querySelector('[data-reset-trim]').addEventListener('click', () => {
      const track = tracks[index];
      if (!track) return;
      track.trim_start = 0; track.trim_end = track.duration;
      error(''); changed(); updateCard(index); renderTimeline();
    });
    return card;
  }

  function drawWaveform(canvas, track) {
    canvas.width = Math.min(1600, Math.max(180, canvas.clientWidth * devicePixelRatio));
    canvas.height = 88;
    const context = canvas.getContext('2d');
    context.fillStyle = '#62816b';
    const originalPeaks = track.peaks || [];
    const duration = playbackDuration(track), period = clipDuration(track);
    const count = Math.max(180, Math.min(800, Math.round(canvas.clientWidth / 3)));
    const peaks = Array.from({ length: originalPeaks.length ? count : 0 }, (_, index) => {
      const local = index / count * duration;
      const time = track.loop ? local % period : local;
      return originalPeaks[Math.min(originalPeaks.length - 1,
        Math.round((track.trim_start + time) / track.duration * (originalPeaks.length - 1)))];
    });
    for (let i = 0; i < peaks.length; i += 1) {
      const time = i / Math.max(1, peaks.length - 1) * duration;
      const gainIn = track.fade_in ? Math.min(1, time / track.fade_in) : 1;
      const gainOut = track.fade_out ? Math.min(1, (duration - time) / track.fade_out) : 1;
      const height = Math.min(86, Math.max(1, peaks[i] * gainIn * gainOut * track.volume * 82));
      context.fillRect(i / peaks.length * canvas.width, (88 - height) / 2, Math.max(1, canvas.width / peaks.length - 1), height);
    }
  }
  function positionClip(clip, track) {
    const duration = playbackDuration(track);
    clip.style.left = `${track.start / extent * 100}%`;
    clip.style.width = `${duration / extent * 100}%`;
    clip.setAttribute('aria-valuenow', track.start.toFixed(2));
    clip.setAttribute('aria-valuetext', `Початок ${seconds(track.start)}, тривалість ${seconds(duration)}`);
    clip.querySelector('.clip-label').textContent = `${track.loop ? '↻ ' : ''}${track.name} · ${seconds(track.start)}`;
    clip.querySelector('.fade-in-shade').style.width = `${track.fade_in / duration * 100}%`;
    clip.querySelector('.fade-out-shade').style.width = `${track.fade_out / duration * 100}%`;
  }
  function renderTimeline() {
    let clamped = false;
    for (const track of tracks) if (track) {
      const duration = playbackDuration(track);
      if (track.fade_in > duration || track.fade_out > duration) clamped = true;
      track.fade_in = Math.min(track.fade_in, duration); track.fade_out = Math.min(track.fade_out, duration);
    }
    if (clamped) persist();
    if (!drag) extent = Math.max(10, Math.ceil(totalDuration() + Math.max(10, totalDuration() * .15)));
    const width = Math.max(540, $('#editor-timeline-scroll').clientWidth) * Number($('#editor-zoom').value);
    $('#editor-timeline').style.width = `${width}px`;
    const ruler = $('#editor-ruler');
    ruler.replaceChildren();
    for (let tick = 0; tick <= 10; tick += 1) {
      const mark = element('span', '', seconds(extent * tick / 10));
      mark.style.left = `${tick * 10}%`;
      ruler.append(mark);
    }
    $('#editor-lanes').replaceChildren();
    tracks.forEach((track, index) => {
      if (!cards[index]) return;
      updateCard(index);
      const number = rowNumber(index);
      const lane = element('div', 'timeline-lane'); lane.dataset.lane = index; $('#editor-lanes').append(lane);
      if (!track) { lane.append(element('span', 'lane-placeholder', `Доріжка ${number} · додай аудіо вище`)); return; }
      const clip = element('div', `timeline-clip track-${(number - 1) % 4}${track.loop ? ' is-looping' : ''}`);
      clip.tabIndex = 0;
      clip.setAttribute('role', 'slider');
      clip.setAttribute('aria-label', `Початок доріжки ${number}: ${track.name}`);
      clip.setAttribute('aria-valuemin', '0'); clip.setAttribute('aria-valuemax', String(maxStart(track)));
      clip.setAttribute('aria-orientation', 'horizontal');
      clip.append(element('span', 'clip-label'), element('canvas', 'clip-waveform'), element('span', 'fade-in-shade'), element('span', 'fade-out-shade'));
      lane.append(clip); positionClip(clip, track); drawWaveform(clip.querySelector('canvas'), track);
      clip.addEventListener('pointerdown', event => {
        if (busy || activeRecording() || event.button !== 0) return;
        event.preventDefault(); clip.focus(); changed();
        drag = { index, x: event.clientX, start: track.start, width: lane.clientWidth };
        clip.setPointerCapture(event.pointerId); clip.classList.add('dragging');
      });
      clip.addEventListener('pointermove', event => {
        if (!drag || drag.index !== index) return;
        track.start = Math.round(Math.max(0, Math.min(maxStart(track), extent - (track.loop && track.loop_duration == null ? clipDuration(track) : playbackDuration(track)),
          drag.start + (event.clientX - drag.x) / drag.width * extent)) * 100) / 100;
        positionClip(clip, track); updateCard(index); updatePlayhead();
      });
      const release = () => {
        if (!drag || drag.index !== index) return;
        drag = null; clip.classList.remove('dragging'); persist(); renderTimeline();
      };
      clip.addEventListener('pointerup', release);
      clip.addEventListener('pointercancel', release);
      clip.addEventListener('lostpointercapture', release);
      clip.addEventListener('keydown', event => {
        if (busy || activeRecording() || !['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
        event.preventDefault();
        track.start = event.key === 'Home' ? 0 : event.key === 'End' ? maxStart(track)
          : Math.round(Math.max(0, Math.min(maxStart(track), track.start + (event.key === 'ArrowLeft' ? -1 : 1) * (event.shiftKey ? 1 : .1))) * 100) / 100;
        changed(); updateCard(index); renderTimeline(); $(`[data-lane="${index}"] .timeline-clip`).focus();
      });
    });
    updatePlayhead();
  }
  function updatePlayhead() {
    const time = Number.isFinite(player.currentTime) ? player.currentTime : 0;
    $('#editor-duration').textContent = `${durationLabel(time)} / ${durationLabel(totalDuration())}`;
    $('#editor-playhead').hidden = !previewUrl;
    $('#editor-playhead').style.left = `${Math.min(100, time / extent * 100)}%`;
  }
  function animatePlayhead() {
    updatePlayhead();
    if (!player.paused) animation = requestAnimationFrame(animatePlayhead);
  }
  $('#editor-preview').addEventListener('click', async () => {
    if (!player.paused) { player.pause(); return; }
    if (previewUrl) { try { await player.play(); } catch (exception) { error(exception.message); } return; }
    busy = true; error(''); updateControls();
    $('#editor-preview-status').textContent = 'Готуємо мікс для прослуховування…';
    const currentRevision = revision;
    try {
      const response = await fetch('/api/editor/preview', json('POST', { ...body(), name: 'Прослуховування' }));
      if (!response.ok) {
        let message = 'Не вдалося підготувати мікс.';
        try { const result = await response.json(); if (typeof result.detail === 'string') message = result.detail; } catch (_) { /* Use fallback. */ }
        throw new Error(message);
      }
      const blob = await response.blob();
      if (currentRevision !== revision || $('#view-editor').hidden) return;
      previewUrl = URL.createObjectURL(blob); player.src = previewUrl; player.hidden = false;
      $('#editor-preview-status').textContent = 'Готовий мікс з усіма переходами. Можна перемотувати нижче.';
      await player.play();
    } catch (exception) { error(exception.message); $('#editor-preview-status').textContent = 'Спробуй прослухати мікс ще раз.'; }
    finally { busy = false; updateControls(); }
  });
  $('#editor-stop').addEventListener('click', stopPlayback);
  player.addEventListener('play', () => { updateControls(); animatePlayhead(); });
  player.addEventListener('pause', () => { cancelAnimationFrame(animation); updateControls(); });
  player.addEventListener('timeupdate', updatePlayhead);
  $('#editor-zoom').addEventListener('input', renderTimeline);
  $('#editor-save-mode').addEventListener('change', () => { updateSaveMode(true); persist(); });
  $('#editor-format').addEventListener('change', () => { updateExportSettings(); persist(); });
  $('#editor-mp3-bitrate').addEventListener('change', persist);
  $('#editor-name').addEventListener('input', persist);
  $('#editor-save-form').addEventListener('submit', async event => {
    event.preventDefault();
    if (busy || activeRecording() || !tracks.some(Boolean)) return;
    busy = true; error(''); stopPlayback(); updateControls();
    const payload = body();
    try {
      const result = await api('/api/editor/mixes', json('POST', payload));
      if (payload.replace_id) {
        const old = [...tracks];
        const source = await api(`/api/editor/sources/generation/${result.id}`);
        for (const card of cards) card?.remove();
        tracks.length = 0; cards.length = 0; versions.length = 0;
        addTrack(false);
        refreshChoices();
        tracks[0] = { ...source, kind: 'generation', start: 0, fade_in: 0, fade_out: 0, trim_start: 0, trim_end: source.duration, volume: 1, loop: false, loop_duration: null };
        changed(); renderTimeline();
        for (const track of old) await discardUpload(track);
        $('#editor-save-mode').value = '';
      }
      await refresh();
      const target = $('#editor-result');
      target.hidden = false;
      target.replaceChildren(element('h2', '', payload.replace_id ? 'Початковий трек замінено.' : 'Новий трек збережено.'), makeJobCard(result));
      toast(payload.replace_id ? 'Мікс збережено замість обраного треку.' : 'Новий мікс збережено в розділі «Мої аудіо».');
    } catch (exception) { error(exception.message); }
    finally { busy = false; updateControls(); }
  });
  window.addEventListener('studio-jobs', refreshChoices);
  window.addEventListener('studio-view', event => {
    if (event.detail !== 'editor') {
      for (const card of cards) card?.querySelector('dialog').close();
      recorder.cancel(); recordingSlot = null; stopPlayback(); revision += 1;
    } else requestAnimationFrame(renderTimeline);
  });
  window.addEventListener('pagehide', () => {
    recorder.cancel(); stopPlayback();
    if (previewUrl) URL.revokeObjectURL(previewUrl);
  });
  new ResizeObserver(() => { if (!drag && !$('#view-editor').hidden) renderTimeline(); }).observe($('#editor-timeline-scroll'));
  function addTrack(save = true) {
    const index = tracks.length;
    tracks.push(null); versions.push(0);
    const card = createCard(index); cards.push(card); $('#editor-tracks').append(card);
    choicesSignature = null;
    if (save) { refreshChoices(); changed(); renderTimeline(); updateControls(); }
    return index;
  }
  $('#editor-add-track').addEventListener('click', () => {
    const index = addTrack(); cards[index].querySelector('[data-history]').focus();
  });
  window.audioEditor = {
    openGeneration: async job => {
      if (busy || activeRecording()) { toast('Спочатку заверши поточну дію в аудіоредакторі.'); return; }
      let index = tracks.findIndex((track, slot) => cards[slot] && !track);
      showView('editor');
      if (index === -1) index = addTrack();
      await loadGeneration(index, job.id);
    },
  };
  let initialDraft;
  try { initialDraft = JSON.parse(localStorage.getItem('holos-editor-draft') || 'null'); } catch (_) { /* Start with one empty row. */ }
  if (Array.isArray(initialDraft?.tracks) && !initialDraft.tracks.some(Boolean)) initialDraft.tracks = [null];
  const count = Array.isArray(initialDraft?.tracks) ? initialDraft.tracks.length : 1;
  for (let index = 0; index < count; index += 1) addTrack(false);
  refreshChoices(); renderTimeline(); updateControls();
  (async () => {
    const draft = initialDraft;
    if (!Array.isArray(draft?.tracks)) return;
    busy = true; updateControls();
    try {
      for (let index = 0; index < draft.tracks.length; index += 1) {
        const saved = draft.tracks[index];
        if (!saved || !['generation', 'upload'].includes(saved.kind) || !/^[a-f0-9]{32}$/.test(saved.id)) continue;
        try {
          const source = await api(`/api/editor/sources/${saved.kind}/${saved.id}`);
          let trimStart = Math.max(0, Math.min(source.duration, Number(saved.trim_start) || 0));
          let trimEnd = saved.trim_end == null ? source.duration : Math.max(0, Math.min(source.duration, Number(saved.trim_end) || 0));
          if (Math.round(trimStart * 48000) >= Math.round(trimEnd * 48000)) { trimStart = 0; trimEnd = source.duration; }
          tracks[index] = { ...source, kind: saved.kind,
            trim_start: trimStart, trim_end: trimEnd,
            volume: typeof saved.volume === 'number' ? Math.max(0, Math.min(4, saved.volume)) : 1,
            loop: saved.loop === true,
            loop_duration: restoredSeconds(saved.loop_duration) || null,
            start: restoredSeconds(saved.start),
            fade_in: restoredSeconds(saved.fade_in),
            fade_out: restoredSeconds(saved.fade_out) };
          updateCard(index);
        } catch (_) { error('Одне з джерел чернетки недоступне. Додай аудіо знову.'); }
      }
      $('#editor-name').value = String(draft.name || 'Мій мікс').slice(0, 60);
      if (['wav', 'mp3', 'flac'].includes(draft.output_format)) $('#editor-format').value = draft.output_format;
      if ([64, 96, 128].includes(draft.mp3_bitrate)) $('#editor-mp3-bitrate').value = draft.mp3_bitrate;
      changed(); renderTimeline();
    } finally { busy = false; updateControls(); }
  })();
})();
