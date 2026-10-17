/* The microphone is acquired only after Start, and released on every exit path. */
globalThis.VoiceRecorder = class VoiceRecorder {
  constructor({ onState, onReady, onError, maxBytes = 64 * 1024 * 1024, minSeconds = 3, maxSeconds = Infinity }) {
    Object.assign(this, { onState, onReady, onError, maxBytes, minSeconds, maxSeconds });
    this.version = 0;
    this.phase = 'idle';
  }

  emit(phase) {
    this.phase = phase;
    this.onState({ phase, seconds: this.started ? (performance.now() - this.started) / 1000 : 0 });
  }

  release() {
    clearInterval(this.timer);
    this.timer = null;
    if (this.stream) this.stream.getTracks().forEach(track => track.stop());
    this.stream = null;
    this.recorder = null;
  }

  cancel() {
    this.version += 1;
    if (this.recorder) {
      this.recorder.ondataavailable = null;
      this.recorder.onstop = null;
      this.recorder.onerror = null;
      if (this.recorder.state !== 'inactive') this.recorder.stop();
    }
    this.release();
    this.started = null;
    this.emit('idle');
  }

  async start() {
    if (['requesting', 'recording', 'stopping'].includes(this.phase)) return;
    this.cancel();
    const version = this.version;
    if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === 'undefined') {
      this.onError('Цей браузер не підтримує запис. Відкрий студію в актуальному Chrome або Safari чи завантаж аудіофайл.');
      return;
    }
    this.emit('requesting');
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true, video: false });
      // A permission dialog may resolve after the user closed our form.
      if (version !== this.version) {
        stream.getTracks().forEach(track => track.stop());
        return;
      }
      this.stream = stream;
      const mimeType = ['audio/webm;codecs=opus', 'audio/mp4', 'audio/ogg;codecs=opus']
        .find(type => MediaRecorder.isTypeSupported(type));
      const recorder = new MediaRecorder(stream, mimeType ? { mimeType } : {});
      this.recorder = recorder;
      const chunks = [];
      let bytes = 0;
      const fail = message => {
        if (version !== this.version) return;
        this.cancel();
        this.onError(message);
      };
      recorder.ondataavailable = event => {
        if (version !== this.version || !event.data.size) return;
        bytes += event.data.size;
        if (bytes > this.maxBytes) {
          fail('Запис перевищив 64 МБ. Запиши коротший фрагмент.');
          return;
        }
        chunks.push(event.data);
      };
      recorder.onerror = () => fail('Запис перервано. Перевір мікрофон і спробуй ще раз.');
      recorder.onstop = () => {
        if (version !== this.version) return;
        const seconds = (performance.now() - this.started) / 1000;
        const blob = new Blob(chunks, { type: recorder.mimeType || chunks[0]?.type || 'audio/webm' });
        this.release();
        if (!blob.size || seconds < this.minSeconds) {
          this.emit('idle');
          this.onError(this.minSeconds === 3 ? 'Запиши щонайменше 3 секунди мовлення.' : 'Запис надто короткий. Спробуй ще раз.');
          return;
        }
        this.emit('ready');
        this.onReady(blob);
      };
      recorder.start(1000);
      this.started = performance.now();
      this.emit('recording');
      this.timer = setInterval(() => {
        if ((performance.now() - this.started) / 1000 >= this.maxSeconds) this.stop();
        else this.emit('recording');
      }, 200);
    } catch (error) {
      if (version !== this.version) return;
      this.cancel();
      const message = error.name === 'NotAllowedError'
        ? 'Доступ до мікрофона не надано. Дозволь його в налаштуваннях браузера або завантаж аудіофайл.'
        : error.name === 'NotFoundError'
          ? 'Мікрофон не знайдено. Підключи його або завантаж аудіофайл.'
          : 'Не вдалося почати запис. Перевір, чи мікрофон доступний у браузері.';
      this.onError(message);
    }
  }

  stop() {
    if (this.phase !== 'recording') return;
    clearInterval(this.timer);
    this.emit('stopping');
    this.recorder.stop();
    // Stop capture immediately; the final encoded data arrives in onstop.
    if (this.stream) this.stream.getTracks().forEach(track => track.stop());
  }
};
