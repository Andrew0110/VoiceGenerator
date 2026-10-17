const $ = (selector, root = document) => root.querySelector(selector);
const state = { voices: [], jobs: [], installed: false, activeId: null, submitting: false };
const languageName = { uk: 'Українська', en: 'English' };
let toastTimer;

async function api(path, options = {}) {
  const response = await fetch(path, options);
  if (!response.ok) {
    let message = 'Не вдалося виконати дію. Спробуй ще раз.';
    try {
      const body = await response.json();
      if (typeof body.detail === 'string') message = body.detail;
      else if (Array.isArray(body.detail)) message = body.detail[0]?.msg?.replace('Value error, ', '') || message;
    } catch (_) { /* Preserve a readable message for a non-JSON server error. */ }
    throw new Error(message);
  }
  return response.json();
}
function json(method, body) { return { method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }; }
function toast(message) {
  clearTimeout(toastTimer);
  $('#toast').textContent = message;
  $('#toast').hidden = false;
  toastTimer = setTimeout(() => { $('#toast').hidden = true; }, 6000);
}
function element(tag, className, text) {
  const item = document.createElement(tag);
  if (className) item.className = className;
  if (text !== undefined) item.textContent = text;
  return item;
}
function action(label, handler, className = 'text-button') {
  const button = element('button', className, label);
  button.type = 'button';
  button.addEventListener('click', async () => {
    button.disabled = true;
    try { await handler(); } catch (error) { toast(error.message); }
    finally { button.disabled = false; }
  });
  return button;
}
function showView(view) {
  for (const item of document.querySelectorAll('.view')) item.hidden = item.id !== `view-${view}`;
  for (const item of document.querySelectorAll('.nav-item')) item.classList.toggle('active', item.dataset.view === view);
  $('#breadcrumb').textContent = `Студія / ${{ studio: 'Озвучення', editor: 'Аудіоредактор', voices: 'Мої голоси', history: 'Мої аудіо' }[view]}`;
  location.hash = view;
  window.dispatchEvent(new CustomEvent('studio-view', { detail: view }));
}
function updateButton() {
  $('#char-count').textContent = $('#text').value.length.toLocaleString('uk-UA');
  $('#generate').disabled = !state.installed || !$('#text').value.trim() || state.submitting;
}
function voiceHint() {
  const voice = state.voices.find(item => item.id === $('#voice-select').value);
  $('#voice-hint').textContent = voice
    ? `Зразок ${voice.duration.toFixed(1)} с · ${languageName[voice.language]}. Голос збережений на цьому Mac.`
    : 'Базовий голос може відрізнятися між генераціями. Додай зразок для власного голосу.';
}
function updateAudioSettings() {
  const speed = Number($('#speed').value);
  const label = `${speed.toLocaleString('uk-UA', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}×`;
  $('#speed-value').textContent = label;
  $('#speed').setAttribute('aria-valuetext', label);
  const format = $('#output-format').value;
  const mp3_bitrate = Number($('#mp3-bitrate').value);
  $('#mp3-settings').hidden = format !== 'mp3';
  $('#mp3-bitrate').disabled = format !== 'mp3';
  $('#file-info').textContent = `↳ ${format.toUpperCase()} · 24 кГц${format === 'mp3' ? ` · ${mp3_bitrate} кбіт/с` : ''}`;
  localStorage.setItem('holos-audio-settings', JSON.stringify({ speed, output_format: format, mp3_bitrate }));
}
function openVoiceDialog() {
  $('#voice-error').hidden = true;
  updateReadingText();
  $('#voice-dialog').showModal();
}

const readingTexts = {
  uk: 'Сьогодні я говорю спокійно й у звичному темпі. За вікном тихо, і можна нікуди не поспішати. Цікаво, яким буде завтрашній день?',
  en: 'Today I am speaking calmly, at my usual pace. It is quiet outside, and there is no need to hurry. I wonder what tomorrow will bring.',
};
let recordedSample = null;
let samplePreviewUrl = null;
let voiceSaving = false;
let recordingPhase = 'idle';
let recordingSeconds = 0;

function updateReadingText() {
  $('#reading-text').textContent = readingTexts[$('#voice-language').value];
}
function voiceError(message) {
  $('#voice-error').textContent = message;
  $('#voice-error').hidden = false;
}
function clearVoiceSample() {
  recordedSample = null;
  const player = $('#voice-preview');
  player.pause();
  player.removeAttribute('src');
  player.load();
  player.hidden = true;
  if (samplePreviewUrl) URL.revokeObjectURL(samplePreviewUrl);
  samplePreviewUrl = null;
}
function previewVoiceSample(file) {
  samplePreviewUrl = URL.createObjectURL(file);
  $('#voice-preview').src = samplePreviewUrl;
  $('#voice-preview').hidden = false;
}
function recordingState({ phase, seconds }) {
  if (seconds !== undefined) recordingSeconds = seconds;
  if (phase === 'recording' && recordingPhase !== phase) {
    clearVoiceSample();
    $('#voice-file').value = '';
    $('#voice-error').hidden = true;
  }
  recordingPhase = phase;
  const busy = ['requesting', 'recording', 'stopping'].includes(phase);
  $('#record-voice').disabled = busy || voiceSaving;
  $('#record-voice').textContent = recordedSample ? '● Записати ще раз' : '● Записати з мікрофона';
  $('#stop-recording').hidden = phase !== 'recording';
  $('#save-voice').disabled = busy || voiceSaving;
  $('#voice-file').disabled = busy || voiceSaving;
  $('#recording-time').textContent = durationLabel(Math.floor(recordingSeconds));
  $('#recording-time').classList.toggle('is-recording', phase === 'recording');
  $('#recording-status').textContent = {
    idle: 'Мікрофон увімкнеться після натискання кнопки й дозволу браузера.',
    requesting: 'Очікуємо дозвіл на мікрофон…',
    recording: 'Триває запис. Говори природно, а потім натисни «Зупинити».',
    stopping: 'Підготовка запису до прослуховування…',
    ready: 'Запис готовий. Прослухай його та перевір текст перед збереженням.',
  }[phase];
}
const voiceRecorder = new VoiceRecorder({
  onState: recordingState,
  onError: voiceError,
  onReady: blob => {
    clearVoiceSample();
    const extension = blob.type.includes('mp4') ? 'm4a' : blob.type.includes('ogg') ? 'ogg' : 'webm';
    recordedSample = new File([blob], `microphone.${extension}`, { type: blob.type });
    previewVoiceSample(recordedSample);
    $('#record-voice').textContent = '● Записати ще раз';
  },
});
function emptyState(title, description, addVoice = false) {
  const box = element('div', 'empty-state');
  box.append(element('span', 'empty-icon', addVoice ? '◎' : 'ııı'), element('strong', '', title), element('p', '', description));
  if (addVoice) box.append(action('＋ Додати перший голос', openVoiceDialog, 'secondary'));
  return box;
}
function audioPlayer(url) {
  const player = element('audio');
  player.controls = true;
  player.preload = 'none';
  player.src = url;
  return player;
}
function dateLabel(iso) {
  return new Intl.DateTimeFormat('uk-UA', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' }).format(new Date(iso));
}
function durationLabel(seconds) {
  const total = Math.round(seconds || 0);
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, '0')}`;
}
function makeJobCard(job) {
  const card = element('article', 'history-card');
  card.dataset.job = job.id;
  const top = element('div', 'history-card-top');
  const text = element('div', 'history-text');
  const meta = element('div', 'history-meta');
  meta.append(element('span', '', job.voice_name));
  if (job.kind !== 'mix') meta.append(element('span', '', languageName[job.language]));
  meta.append(element('span', '', dateLabel(job.created_at)));
  if (job.duration) meta.append(element('span', '', durationLabel(job.duration)));
  const formatLabel = `${(job.output_format || 'wav').toUpperCase()}${job.output_format === 'mp3' ? ` · ${job.mp3_bitrate || 128} кбіт/с` : ''}`;
  meta.append(element('span', '', `${formatLabel} · ${job.kind === 'mix' ? '48 кГц · стерео' : `${(job.speed || 1).toLocaleString('uk-UA')}×`}`));
  const title = element('h3', '', job.name || job.text);
  title.title = job.name || job.text;
  text.append(title, meta);
  const actions = element('div', 'history-actions');
  if (job.status === 'completed') {
    const format = job.output_format || 'wav';
    const download = element('a', '', `↓ ${format.toUpperCase()}`);
    download.href = `/api/generations/${job.id}/audio`;
    download.download = `holos-${job.id.slice(0, 8)}.${format}`;
    actions.append(download);
    actions.append(action('Редагувати', () => window.audioEditor?.openGeneration(job)));
  }
  if (['failed', 'completed'].includes(job.status)) {
    const remove = action('×', async () => {
      if (!confirm('Видалити цей запис та його аудіофайл?')) return;
      await api(`/api/generations/${job.id}`, { method: 'DELETE' });
      if (state.activeId === job.id) { state.activeId = null; $('#latest-result').hidden = true; }
      await refresh();
    }, 'icon-button');
    remove.setAttribute('aria-label', 'Видалити запис');
    actions.append(remove);
  }
  top.append(element('span', 'sound-icon', job.status === 'failed' ? '!' : 'ııı'), text, actions);
  card.append(top);
  if (job.status === 'completed') card.append(audioPlayer(`/api/generations/${job.id}/audio?v=${encodeURIComponent(job.finished_at || job.created_at)}`));
  else {
    card.append(element('p', `job-message ${job.status === 'failed' ? 'failed' : ''}`, job.message));
    if (job.status !== 'failed') {
      const track = element('progress', 'progress-track');
      track.max = 100;
      track.value = job.progress;
      track.setAttribute('aria-label', 'Прогрес генерації');
      card.append(track);
    } else {
      card.append(action('Повернути текст у редактор →', () => {
        $('#text').value = job.text;
        $('#language').value = job.language;
        $('#quality').value = job.steps;
        $('#speed').value = job.speed || 1;
        $('#output-format').value = job.output_format || 'wav';
        $('#mp3-bitrate').value = job.mp3_bitrate || 128;
        updateAudioSettings();
        $('#voice-select').value = state.voices.some(v => v.id === job.voice_id) ? job.voice_id : '';
        voiceHint(); updateButton(); showView('studio');
      }));
    }
  }
  return card;
}
function renderJobs() {
  const query = $('#history-search').value.trim().toLocaleLowerCase();
  const filtered = state.jobs.filter(job => `${job.name || ''} ${job.text} ${job.voice_name}`.toLocaleLowerCase().includes(query));
  const recent = $('#recent-list');
  const history = $('#history-list');
  // Keep existing players when polling unchanged records so playback is uninterrupted.
  for (const [target, jobs, empty] of [[recent, state.jobs.slice(0, 3), 'Тут з’явиться твоє перше аудіо'], [history, filtered, query ? 'Нічого не знайдено' : 'Поки що немає озвучень']]) {
    const signatures = JSON.stringify(jobs);
    if (target.dataset.signature === signatures && target.dataset.query === query) continue;
    const existing = new Map([...target.querySelectorAll('[data-job]')].map(node => [node.dataset.job, node]));
    const nodes = jobs.map(job => {
      const signature = JSON.stringify(job);
      const previous = existing.get(job.id);
      if (previous?.dataset.signature === signature) return previous;
      const card = makeJobCard(job);
      card.dataset.signature = signature;
      return card;
    });
    target.replaceChildren(...(nodes.length ? nodes : [emptyState(empty, query ? 'Спробуй інші слова.' : 'Додай текст і створи аудіо — воно залишиться під рукою.')]));
    target.dataset.signature = signatures;
    target.dataset.query = query;
  }
  $('#history-count').textContent = state.jobs.length;
  renderActive();
}
function renderActive() {
  const active = state.jobs.find(job => job.id === state.activeId);
  const target = $('#latest-result');
  if (!active) return;
  const signature = JSON.stringify(active);
  if (target.dataset.signature === signature) return;
  target.hidden = false;
  target.replaceChildren(element('h2', '', active.status === 'completed' ? 'Твоє аудіо готове.' : active.status === 'failed' ? 'Генерацію не завершено' : 'Текст набуває голосу…'), makeJobCard(active));
  target.dataset.signature = signature;
}
function renderVoices() {
  const selected = $('#voice-select').value;
  $('#voice-select').replaceChildren(new Option('Голос моделі', ''), ...state.voices.map(v => new Option(v.name, v.id)));
  $('#voice-select').value = state.voices.some(v => v.id === selected) ? selected : '';
  voiceHint();
  $('#voice-count').textContent = state.voices.length;
  const nodes = state.voices.map(voice => {
    const card = element('article', 'voice-card card');
    const avatar = element('div', 'voice-avatar', voice.name[0].toLocaleUpperCase());
    card.append(avatar, element('h3', '', voice.name), element('p', '', `${languageName[voice.language]} · зразок ${voice.duration.toFixed(1)} с`), audioPlayer(`/api/voices/${voice.id}/audio`));
    const actions = element('div', 'voice-card-actions');
    actions.append(action('Використати ↗', () => {
      $('#voice-select').value = voice.id;
      $('#language').value = voice.language;
      voiceHint(); showView('studio'); $('#text').focus();
    }), action('Назва', async () => {
      const name = prompt('Нова назва голосу:', voice.name);
      if (!name?.trim()) return;
      await api(`/api/voices/${voice.id}`, json('PATCH', { name: name.trim() }));
      await refresh({ voices: true });
    }), action('Видалити', async () => {
      if (!confirm(`Видалити голос «${voice.name}» і його зразок? Створені аудіофайли залишаться в розділі «Мої аудіо».`)) return;
      await api(`/api/voices/${voice.id}`, { method: 'DELETE' });
      await refresh({ voices: true });
    }, 'danger'));
    card.append(actions);
    return card;
  });
  $('#voices-list').replaceChildren(...(nodes.length ? nodes : [emptyState('Кожен голос має свою історію.', 'Додай короткий запис, щоб створювати озвучення цим голосом.', true)]));
}
let refreshPromise = null;
let refreshTimer = null;
let connectionFailed = false;
const allResources = { status: true, voices: true, jobs: true };

function scheduleRefresh() {
  clearTimeout(refreshTimer);
  refreshTimer = null;
  if (document.hidden) return;
  if (connectionFailed) {
    refreshTimer = setTimeout(() => refresh(allResources), 5000);
  } else if (state.jobs.some(job => ['queued', 'running'].includes(job.status))) {
    refreshTimer = setTimeout(() => refresh(), 2000);
  }
}

function refresh({ status = false, voices = false, jobs = false } = { jobs: true }) {
  // A refresh after a mutation must read data after any earlier request finishes.
  if (refreshPromise) return refreshPromise.then(() => refresh({ status, voices, jobs }));
  clearTimeout(refreshTimer);
  refreshTimer = null;
  refreshPromise = (async () => {
    try {
      const [newStatus, newVoices, newJobs] = await Promise.all([
        status || connectionFailed ? api('/api/status') : null,
        voices ? api('/api/voices') : null,
        jobs ? api('/api/generations') : null,
      ]);
      if (newStatus) {
        state.installed = newStatus.installed;
        $('#setup-notice').hidden = newStatus.installed;
      }
      if (newVoices) {
        state.voices = newVoices;
        renderVoices();
      }
      if (newJobs) {
        state.jobs = newJobs;
        window.dispatchEvent(new CustomEvent('studio-jobs'));
        renderJobs();
      }
      connectionFailed = false;
      $('#connection-notice').hidden = true;
      updateButton();
    } catch (error) {
      connectionFailed = true;
      $('#connection-notice').hidden = false;
      state.installed = false;
      updateButton();
    } finally {
      refreshPromise = null;
      scheduleRefresh();
    }
  })();
  return refreshPromise;
}

document.addEventListener('visibilitychange', () => {
  if (document.hidden) {
    clearTimeout(refreshTimer);
    refreshTimer = null;
  } else {
    refresh(allResources);
  }
});

document.querySelectorAll('[data-view]').forEach(button => button.addEventListener('click', () => showView(button.dataset.view)));
document.querySelectorAll('[data-add-voice]').forEach(button => button.addEventListener('click', openVoiceDialog));
$('#close-dialog').addEventListener('click', () => $('#voice-dialog').close());
$('#cancel-dialog').addEventListener('click', () => $('#voice-dialog').close());
$('#voice-dialog').addEventListener('close', () => {
  voiceRecorder.cancel();
  clearVoiceSample();
  $('#voice-file').value = '';
});
window.addEventListener('pagehide', () => { voiceRecorder.cancel(); clearVoiceSample(); });
$('#record-voice').addEventListener('click', () => { $('#voice-error').hidden = true; voiceRecorder.start(); });
$('#stop-recording').addEventListener('click', () => voiceRecorder.stop());
$('#voice-language').addEventListener('change', updateReadingText);
$('#use-reading-text').addEventListener('click', () => {
  $('#voice-transcript').value = readingTexts[$('#voice-language').value];
});
$('#voice-file').addEventListener('change', () => {
  voiceRecorder.cancel();
  clearVoiceSample();
  const file = $('#voice-file').files[0];
  if (file) {
    previewVoiceSample(file);
    $('#recording-status').textContent = 'Обрано аудіофайл. Прослухай його та перевір текст перед збереженням.';
  }
});
$('#text').addEventListener('input', () => { updateButton(); localStorage.setItem('holos-draft', $('#text').value); });
$('#voice-select').addEventListener('change', voiceHint);
$('#speed').addEventListener('input', updateAudioSettings);
$('#output-format').addEventListener('change', updateAudioSettings);
$('#mp3-bitrate').addEventListener('change', updateAudioSettings);
$('#reset-speed').addEventListener('click', () => { $('#speed').value = 1; updateAudioSettings(); });
$('#history-search').addEventListener('input', renderJobs);
$('#example').addEventListener('click', () => {
  $('#text').value = $('#language').value === 'uk'
    ? 'Привіт! Це моя локальна аудіостудія. Тут слова знаходять своє звучання, а кожна історія отримує власний голос.'
    : 'Hello! Welcome to my local audio studio. Here, words find their sound, and every story gets a voice of its own.';
  localStorage.setItem('holos-draft', $('#text').value); updateButton(); $('#text').focus();
});
$('#generate').addEventListener('click', async () => {
  state.submitting = true; updateButton();
  try {
    const job = await api('/api/generations', json('POST', {
      text: $('#text').value, voice_id: $('#voice-select').value || null,
      language: $('#language').value, steps: Number($('#quality').value),
      speed: Number($('#speed').value), output_format: $('#output-format').value,
      mp3_bitrate: Number($('#mp3-bitrate').value),
    }));
    state.activeId = job.id;
    state.jobs.unshift(job); renderJobs();
    toast('Текст додано до черги генерації.');
    await refresh();
  } catch (error) { toast(error.message); }
  finally { state.submitting = false; updateButton(); }
});
$('#voice-form').addEventListener('submit', async event => {
  event.preventDefault();
  const button = $('#save-voice');
  $('#voice-error').hidden = true;
  if (['requesting', 'recording', 'stopping'].includes(recordingPhase)) {
    voiceError('Спочатку зупини запис із мікрофона.'); return;
  }
  const file = recordedSample || $('#voice-file').files[0];
  if (!file) { voiceError('Завантаж аудіофайл або запиши голос із мікрофона.'); return; }
  if (file.size > 64 * 1024 * 1024) { voiceError('Максимальний розмір запису — 64 МБ.'); return; }
  const form = new FormData(event.target);
  form.set('sample', file);
  voiceSaving = true;
  recordingState({ phase: recordingPhase });
  button.disabled = true; button.textContent = 'Збереження…';
  try {
    const voice = await api('/api/voices', { method: 'POST', body: form });
    $('#voice-dialog').close(); event.target.reset();
    await refresh({ voices: true });
    $('#voice-select').value = voice.id;
    $('#language').value = voice.language;
    voiceHint(); toast('Голос збережено. Можна створювати озвучення.');
  } catch (error) {
    $('#voice-error').textContent = error.message; $('#voice-error').hidden = false;
  } finally {
    voiceSaving = false;
    recordingState({ phase: recordingPhase });
    button.textContent = 'Зберегти голос ↗';
  }
});
$('#text').value = (localStorage.getItem('holos-draft') || '').slice(0, 3000);
try {
  const settings = JSON.parse(localStorage.getItem('holos-audio-settings') || '{}');
  if (typeof settings.speed === 'number' && settings.speed >= 0.7 && settings.speed <= 1.3) $('#speed').value = settings.speed;
  if (['wav', 'mp3', 'flac'].includes(settings.output_format)) $('#output-format').value = settings.output_format;
  if ([64, 96, 128].includes(settings.mp3_bitrate)) $('#mp3-bitrate').value = settings.mp3_bitrate;
} catch (_) { /* Invalid saved preferences fall back to normal speed and WAV. */ }
updateAudioSettings();
showView(['studio', 'editor', 'voices', 'history'].includes(location.hash.slice(1)) ? location.hash.slice(1) : 'studio');
window.addEventListener('hashchange', () => {
  const view = location.hash.slice(1);
  if (['studio', 'editor', 'voices', 'history'].includes(view)) showView(view);
});
renderVoices();
refresh(allResources);
