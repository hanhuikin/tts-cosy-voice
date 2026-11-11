/* CosyVoice 控制台 — Vue3 应用 */
const { createApp, reactive, ref, computed, onMounted, nextTick, watch } = Vue;

/* ================= 全局播放器 =================
   两种模式:
   - stream: WebAudio 边收边播 (流式合成), 结束后转 audio 元素模式
   - file:   audio 元素直接播放 (历史/试听)
   波形: peaks 数组 + RAF 重绘 */
class Player {
  PREBUFFER = 2.5;  // 流式起播预缓冲秒数
  constructor(vm) {
    this.vm = vm;                 // Vue 实例, 用于更新 reactive 状态
    this.audio = new Audio();
    this.audio.addEventListener('timeupdate', () => {
      if (this.mode === 'file') {
        this.vm.player.position = this.audio.currentTime;
        this.vm.player.playing = !this.audio.paused && !this.audio.ended;
        this.draw();  // 按需重绘进度波形 (timeupdate ~4Hz)
      }
    });
    this.audio.addEventListener('ended', () => { this.vm.player.playing = false; });
    this.reset();
  }
  reset() {
    if (this.ctx) { try { this.ctx.close(); } catch (e) {} this.ctx = null; }
    if (this.blobUrl) { URL.revokeObjectURL(this.blobUrl); this.blobUrl = null; }
    this.audio.pause(); this.audio.removeAttribute('src');
    this.mode = null; this.aborter = null;
    this.parts = []; this.header = null; this.sampleRate = 24000;
    this.playedSamples = 0; this.totalSamples = 0;
    this.peaks = []; this.rafId = null;
    Object.assign(this.vm.player, {
      active: false, playing: false, streaming: false, title: '', taskId: null,
      duration: 0, position: 0, firstMs: null, sampleRate: null,
    });
  }
  /* ---------- 流式 ---------- */
  async startStream(resp, title, taskId) {
    this.reset();
    this.mode = 'stream';
    Object.assign(this.vm.player, { active: true, streaming: true, title, taskId });
    const reader = resp.body.getReader();
    this.aborter = reader;
    let buf = new Uint8Array(0);   // 未解析字节 (header + 对齐残余)
    let headerParsed = false;
    let gotAudio = false;
    const t0 = performance.now();

    const ensureCtx = () => {
      if (!this.ctx) {
        this.ctx = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: this.sampleRate });
        // 预缓冲: 起播前先攒 PREBUFFER 秒音频, 生成速度波动时不至于断流
        this.nextTime = this.ctx.currentTime + this.PREBUFFER;
        this.startTime = this.nextTime;
        this.vm.player.sampleRate = this.sampleRate;
      }
    };
    const pushPcm = (u8) => {
      this.parts.push(u8);
      const samples = u8.byteLength >> 1;
      ensureCtx();
      const audioBuf = this.ctx.createBuffer(1, samples, this.ctx.sampleRate);
      const ch = audioBuf.getChannelData(0);
      const dv = new DataView(u8.buffer, u8.byteOffset, u8.byteLength);
      for (let i = 0; i < samples; i++) ch[i] = dv.getInt16(i * 2, true) / 32768;
      const src = this.ctx.createBufferSource();
      src.buffer = audioBuf;
      src.connect(this.ctx.destination);
      // 永远在"缓冲水位线"之后排队, 保证至少有 PREBUFFER 秒的播放余量
      this.nextTime = Math.max(this.nextTime, this.ctx.currentTime + this.PREBUFFER);
      src.start(this.nextTime);
      this.nextTime += audioBuf.duration;
      this.totalSamples += samples;
      if (!gotAudio) {
        gotAudio = true;
        this.vm.player.firstMs = Math.round(performance.now() - t0);  // 首个音频分块到达
      }
      // 波形峰值 (每块最多 12 个采样点)
      const step = Math.max(1, Math.floor(samples / 12));
      const i16 = new Int16Array(u8.buffer, u8.byteOffset, samples);
      for (let s = 0; s < samples; s += step) {
        let peak = 0;
        for (let k = s; k < Math.min(s + step, samples); k++) peak = Math.max(peak, Math.abs(i16[k]));
        this.peaks.push(peak / 32768);
      }
      this.vm.player.duration = this.totalSamples / this.sampleRate;
    };

    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        const merged = new Uint8Array(buf.length + value.length);
        merged.set(buf); merged.set(value, buf.length);
        buf = merged;
        if (!headerParsed) {
          if (buf.length < 44) continue;
          this.header = buf.slice(0, 44);
          this.sampleRate = new DataView(this.header.buffer).getUint32(24, true);
          buf = buf.slice(44);
          headerParsed = true;
          this.startRaf();
        }
        // 网络分帧可能在任意字节处切割, 只消费 2 字节对齐前缀, 残余留给下一轮
        const evenLen = buf.length - (buf.length % 2);
        if (evenLen > 0) {
          pushPcm(buf.slice(0, evenLen));
          buf = buf.slice(evenLen);
        }
      }
      if (headerParsed && this.totalSamples > 0) this.finishStream();
      else this.vm.toast('流式响应异常: 未收到音频数据', 'error');
    } catch (e) {
      if (e.name !== 'AbortError') this.vm.toast('流式播放中断: ' + e.message, 'error');
    }
  }
  finishStream() {
    // 合成完整 WAV blob (修正 RIFF 长度字段), 切换到 audio 元素模式以支持拖动进度
    const dataLen = this.parts.reduce((n, p) => n + p.byteLength, 0);
    const full = new Uint8Array(44 + dataLen);
    full.set(this.header, 0);
    const dv = new DataView(full.buffer);
    dv.setUint32(4, 36 + dataLen, true);
    dv.setUint32(40, dataLen, true);
    let off = 44;
    for (const p of this.parts) { full.set(p, off); off += p.byteLength; }
    const blob = new Blob([full], { type: 'audio/wav' });
    this.blobUrl = URL.createObjectURL(blob);
    this.audio.src = this.blobUrl;
    this.mode = 'file';
    this.vm.player.streaming = false;
    this.stopRaf();
    this.draw();  // 最终帧落盘
  }
  abortStream() {
    if (this.aborter) { try { this.aborter.cancel(); } catch (e) {} }
    this.reset();
  }
  /* ---------- 文件 ---------- */
  playFile(url, title, taskId) {
    this.reset();
    this.mode = 'file';
    Object.assign(this.vm.player, { active: true, title, taskId });
    this.audio.src = url;
    this.audio.play().then(() => { this.vm.player.playing = true; }).catch(() => {});
    this.audio.addEventListener('loadedmetadata', () => {
      this.vm.player.duration = this.audio.duration;
      this.decodePeaks(url).then(() => this.draw());
    }, { once: true });
  }
  async decodePeaks(url) {
    try {
      const ab = await (await fetch(url)).arrayBuffer();
      const ctx = new (window.AudioContext || window.webkitAudioContext)();
      const buf = await ctx.decodeAudioData(ab);
      ctx.close();
      this.sampleRate = buf.sampleRate;
      this.vm.player.sampleRate = buf.sampleRate;
      const ch = buf.getChannelData(0);
      const buckets = Math.min(220, Math.max(60, Math.floor(ch.length / 400)));
      const size = Math.max(1, Math.floor(ch.length / buckets));
      this.peaks = [];
      for (let b = 0; b < buckets; b++) {
        let peak = 0;
        for (let i = b * size; i < Math.min((b + 1) * size, ch.length); i += 4) peak = Math.max(peak, Math.abs(ch[i]));
        this.peaks.push(peak);
      }
    } catch (e) { /* 波形解码失败不影响播放 */ }
  }
  /* ---------- 控制 ---------- */
  togglePlay() {
    if (this.mode === 'stream') {
      if (!this.ctx) return;
      if (this.ctx.state === 'running') { this.ctx.suspend(); this.vm.player.playing = false; }
      else { this.ctx.resume(); this.vm.player.playing = true; }
    } else if (this.audio.src) {
      if (this.audio.paused) { this.audio.play(); this.vm.player.playing = true; }
      else { this.audio.pause(); this.vm.player.playing = false; }
    }
  }
  get position() {
    if (this.mode === 'stream' && this.ctx) return Math.max(0, this.ctx.currentTime - this.startTime);
    return this.audio.currentTime || 0;
  }
  startRaf() {
    const loop = () => {
      if (!this.vm.player.active) { this.stopRaf(); return; }
      if (this.mode === 'stream') {
        this.vm.player.position = this.position;
        this.vm.player.playing = !!this.ctx && this.ctx.state === 'running';
        if (this.ctx) {
          this.vm.player.buffered = Math.max(0, (this.nextTime || 0) - this.ctx.currentTime);
        }
      }
      this.draw();
      this.rafId = requestAnimationFrame(loop);
    };
    if (!this.rafId) loop();
  }
  stopRaf() { if (this.rafId) { cancelAnimationFrame(this.rafId); this.rafId = null; } }
  draw() {
    const canvas = this.vm.$refs.waveCanvas;
    if (!canvas) return;
    const dpr = window.devicePixelRatio || 1;
    const w = canvas.clientWidth, h = canvas.clientHeight;
    if (canvas.width !== w * dpr) { canvas.width = w * dpr; canvas.height = h * dpr; }
    const g = canvas.getContext('2d');
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.clearRect(0, 0, w, h);
    const peaks = this.peaks;
    if (!peaks.length) return;
    const total = Math.max(this.totalSamples, 1);
    const progress = this.mode === 'stream'
      ? Math.min(1, (this.vm.player.position * (this.sampleRate || 24000)) / total)
      : (this.audio.duration ? this.audio.currentTime / this.audio.duration : 0);
    const n = peaks.length, barW = Math.max(2, w / n - 1);
    for (let i = 0; i < n; i++) {
      const x = (i / n) * w;
      const amp = Math.max(0.06, Math.min(1, peaks[i]));
      const bh = amp * (h - 6);
      const played = (i / n) <= progress;
      if (played) {
        const grad = g.createLinearGradient(x, 0, x, h);
        grad.addColorStop(0, '#4f8cff'); grad.addColorStop(1, '#22d3ee');
        g.fillStyle = grad;
      } else {
        g.fillStyle = '#3a465e';
      }
      g.beginPath();
      const r = Math.min(barW / 2, 2);
      g.roundRect(x, (h - bh) / 2, barW, bh, r);
      g.fill();
    }
  }
}

/* ================= Vue 应用 ================= */
createApp({
  setup() {
    // ---------- 状态 ----------
    const view = ref('workbench');
    const views = [
      { key: 'workbench', icon: '🎬', name: '合成工作台' },
      { key: 'voices', icon: '🧬', name: '音色库' },
      { key: 'history', icon: '🕘', name: '历史记录' },
      { key: 'system', icon: '⚙️', name: '系统状态' },
    ];
    const health = reactive({
      state: 'idle', model: null, model_label: null, sample_rate: null,
      speakers: [], queue_len: 0, queue_max: 16, last_infer_ms: null, error: null,
      gpu: null, voices_count: 0, models: [],
    });
    const form = reactive({
      text: '', model: 'cosyvoice3', mode: 'sft', spk_id: '', voice_id: '',
      prompt_text: '', instruct_text: '', speed: 1.0, stream: false, lang: '',
    });
    const busy = ref(false);
    const busyText = ref('合成中…');
    const workbenchError = ref('');
    const lastResult = ref(null);
    const voices = ref([]);
    const tasks = ref([]);
    const taskFilter = reactive({ mode: null });
    const toasts = ref([]);
    const banner = ref(null);
    const upload = reactive({
      name: '', prompt_text: '', file: null, duration: null, sr: null,
      busy: false, checkError: '', peaks: [],
    });
    const uploadDrag = ref(false);
    const playerState = reactive({
      active: false, playing: false, streaming: false, title: '', taskId: null,
      duration: 0, position: 0, firstMs: null, sampleRate: null, buffered: 0,
    });

    const modeDefs = [
      { key: 'sft', icon: '🎙', name: '预置音色' },
      { key: 'zero_shot', icon: '🧬', name: '声音克隆' },
      { key: 'cross_lingual', icon: '🌐', name: '跨语言' },
      { key: 'instruct', icon: '💬', name: '指令控制' },
    ];
    const langs = [
      { code: 'zh', name: '中文' },
      { code: 'en', name: '英语' },
      { code: 'ja', name: '日语' },
      { code: 'ko', name: '韩语' },
      { code: 'yue', name: '粤语' },
      { code: 'de', name: '德语' },
      { code: 'fr', name: '法语' },
      { code: 'es', name: '西班牙语' },
      { code: 'ru', name: '俄语' },
    ];
    const sampleTexts = [
      { label: '中文问候', text: '你好，我是通义生成式语音大模型，请问有什么可以帮您的吗？' },
      { label: '英文', text: 'The quick brown fox jumps over the lazy dog. Speech synthesis has come a long way!' },
      { label: '情感', text: '收到好友从远方寄来的生日礼物，那份意外的惊喜与深深的祝福让我心中充满了甜蜜的快乐，笑容如花儿般绽放。' },
      { label: '绕口令', text: '八百标兵奔北坡，北坡炮兵并排跑，炮兵怕把标兵碰，标兵怕碰炮兵炮。' },
    ];

    let player = null;
    let es = null;

    // ---------- 工具 ----------
    function toast(text, type = 'info', ms = 4000) {
      const id = Date.now() + Math.random();
      toasts.value.push({ id, text, type });
      setTimeout(() => { toasts.value = toasts.value.filter(t => t.id !== id); }, ms);
    }
    const fmt = (s) => {
      if (!s && s !== 0) return '0:00';
      const m = Math.floor(s / 60), ss = (s % 60).toFixed(1);
      return `${m}:${ss.padStart(4, '0')}`;
    };
    const modeName = (k) => (modeDefs.find(m => m.key === k) || {}).name || k;
    const modelLabel = (k) => k === 'cosyvoice3' ? 'CosyVoice 3.0' : 'CosyVoice 2.0';
    // 历史显示: 剥离开头语言标签 (它是控制标记, 不属于朗读内容)
    const displayTaskText = (t) => (t || '').replace(/^(?:<\|[a-z]{2}\|>\s*)+/, '');
    const voiceOrSpk = (t) => {
      if (t.spk_id) return t.spk_id;
      if (t.voice_id) { const v = voices.value.find(v => v.id === t.voice_id); return v ? v.name : '音色 ' + t.voice_id; }
      return '';
    };
    const stateClass = computed(() => health.state);
    const port = computed(() => window.location.port || (window.location.protocol === 'https:' ? '443' : '80'));
    const stateText = computed(() => ({
      idle: '未加载模型', loading: '模型加载中…', ready: '就绪', error: '加载失败',
    }[health.state] || health.state));
    const gpuText = computed(() => (health.gpu && health.gpu.vram_total_gb)
      ? `${health.gpu.vram_used_gb}/${health.gpu.vram_total_gb}G` : (health.gpu ? health.gpu.name : '…'));
    const vramPct = computed(() => (health.gpu && health.gpu.vram_total_gb)
      ? Math.round(health.gpu.vram_used_gb / health.gpu.vram_total_gb * 100) : 0);
    const availableModels = computed(() => health.models.filter(m => m.available));
    // 当前模型没有内置说话人时隐藏「预置音色」Tab (避免死入口)
    const visibleModeDefs = computed(() => health.speakers.length
      ? modeDefs
      : modeDefs.filter(m => m.key !== 'sft'));
    const currentDefaults = computed(() => {
      const m = health.models.find(m => m.key === form.model);
      return (m && m.default_instructs) || [];
    });
    const canSynth = computed(() => !busy.value && health.state === 'ready' && form.text.trim() && (
      form.mode === 'sft' ? !!form.spk_id : !!form.voice_id && (form.mode !== 'zero_shot' || form.prompt_text.trim())
    ));
    // 合成按钮禁用原因 (显示给用户)
    const synthBlocker = computed(() => {
      if (health.state === 'loading') return '模型加载中…';
      if (health.state !== 'ready') return '模型未就绪';
      if (!form.text.trim()) return '请输入合成文本';
      if (form.mode === 'sft') {
        if (!health.speakers.length) return '当前模型没有内置说话人，请改用「声音克隆」';
        if (!form.spk_id) return '请选择说话人';
      } else {
        if (!form.voice_id) return '请选择克隆音色';
        if (form.mode === 'zero_shot' && !form.prompt_text.trim()) return '请填写参考音频的文字内容';
      }
      return '';
    });
    const canUpload = computed(() => !upload.busy && upload.file && upload.duration >= 1 && upload.duration <= 30
      && upload.name.trim() && upload.prompt_text.trim());
    const filteredTasks = computed(() => taskFilter.mode
      ? tasks.value.filter(t => t.mode === taskFilter.mode) : tasks.value);
    const currentVoice = computed(() => voices.value.find(v => v.id === form.voice_id));
    // 选中克隆音色后自动回填参考文本
    watch(() => form.voice_id, (vid) => {
      const v = voices.value.find(x => x.id === vid);
      if (v && v.prompt_text) form.prompt_text = v.prompt_text;
    });

    function updateBanner() {
      if (health.state === 'loading') {
        banner.value = { type: '', icon: '⏳', text: `正在加载 ${health.model_label || ''} 模型，首次加载约需 1~2 分钟…`, busy: true };
      } else if (health.state === 'error') {
        banner.value = { type: 'error', icon: '⚠️', text: '模型加载失败：' + (health.error || '未知错误'), closable: true };
      } else if (health.state === 'idle') {
        banner.value = { type: 'warn', icon: '📦', text: '未加载任何模型，请先运行 scripts/download_models.py 下载权重', closable: true };
      } else {
        banner.value = null;
      }
    }
    function dismissBanner() { banner.value = null; }

    // ---------- API ----------
    async function api(path, opts) {
      const resp = await fetch(path, opts);
      if (!resp.ok) {
        let msg = `HTTP ${resp.status}`;
        try { const j = await resp.json(); msg = j.detail || msg; } catch (e) {}
        throw new Error(msg);
      }
      return resp;
    }
    async function fetchHealth() {
      try {
        const st = await (await api('/api/health')).json();
        applyHealth(st);
      } catch (e) { /* 服务未起时静默 */ }
    }
    function applyHealth(st) {
      const prev = health.state;
      Object.assign(health, st);
      if (prev !== 'ready' && st.state === 'ready') toast(`${st.model_label} 已就绪`, 'ok');
      if (prev !== 'error' && st.state === 'error') toast('模型加载失败', 'error');
      if (st.models && st.models.length && !st.models.find(m => m.key === form.model)) {
        const firstAvail = st.models.find(m => m.available);
        if (firstAvail) form.model = firstAvail.key;
      }
      // 当前模型没有内置说话人时, 从「预置音色」自动切到「声音克隆」
      if (health.state === 'ready' && form.mode === 'sft' && health.speakers.length === 0) {
        form.mode = 'zero_shot';
      }
      updateBanner();
    }
    async function loadVoices() {
      voices.value = await (await api('/api/voices')).json();
    }
    async function loadTasks() {
      const j = await (await api('/api/tasks?limit=200')).json();
      tasks.value = j.tasks;
    }

    // ---------- SSE ----------
    function connectSSE() {
      es = new EventSource('/api/events');
      es.onmessage = (e) => {
        try {
          const d = JSON.parse(e.data);
          if (d.type === 'engine') {
            applyHealth({ ...health, state: d.state, model: d.model,
                          model_label: d.label ?? health.model_label,
                          sample_rate: d.sample_rate ?? health.sample_rate,
                          speakers: d.speakers ?? health.speakers, error: d.error });
            if (d.state === 'ready') { loadTasks(); }
          } else if (d.type === 'queue') {
            health.queue_len = d.queue_len;
          }
        } catch (err) {}
      };
      es.onerror = () => { /* 断线后 EventSource 自动重连 */ };
    }

    // ---------- 工作台 ----------
    function applySample(s) { form.text = s.text; }
    // 跨语言: 选择语言仅记录状态, 合成请求时自动在文本前拼接标签; 再次点击同一语言取消
    function pickLang(code) {
      form.lang = form.lang === code ? '' : code;
    }
    function switchMode(key) { form.mode = key; }
    function toggleStream() { form.stream = !form.stream; }
    function goVoices() { switchView('voices'); }
    function switchView(key) {
      view.value = key;
      if (key === 'voices') loadVoices();
      if (key === 'history') loadTasks();
    }

    async function synthesize() {
      if (!canSynth.value) return;
      // 跨语言选中了语言: 请求时自动前置语言标签 (不进入文本框, 模型也不会读出它)
      const text = (form.mode === 'cross_lingual' && form.lang)
        ? `<|${form.lang}|>` + form.text : form.text;
      const payload = {
        text, model: form.model, mode: form.mode, speed: form.speed, stream: form.stream,
      };
      if (form.mode === 'sft') payload.spk_id = form.spk_id;
      else payload.voice_id = form.voice_id;
      if (form.mode === 'zero_shot') payload.prompt_text = form.prompt_text;
      if (form.mode === 'instruct') payload.instruct_text = form.instruct_text;

      busy.value = true;
      workbenchError.value = '';
      lastResult.value = null;
      busyText.value = form.stream ? '流式合成中…' : '合成中…';
      try {
        const resp = await api('/api/tts', {
          method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
        });
        const taskId = resp.headers.get('X-Task-Id');
        if (form.stream) {
          await player.startStream(resp, (payload.spk_id ? payload.spk_id : voiceName(payload.voice_id)) + ' · 流式', taskId);
          lastResult.value = {
            id: taskId, mode: form.mode, model: form.model,
            firstMs: player.vm.player.firstMs,
            duration: player.vm.player.duration,
          };
        } else {
          const blob = await resp.blob();
          const durMs = resp.headers.get('X-Duration-Ms');
          const inferMs = resp.headers.get('X-Infer-Ms');
          player.playFile(URL.createObjectURL(blob), (payload.spk_id ? payload.spk_id : voiceName(payload.voice_id)), taskId);
          player.vm.player.taskId = taskId;
          lastResult.value = {
            id: taskId, mode: form.mode, model: form.model,
            duration: durMs ? durMs / 1000 : null, inferMs: inferMs ? +inferMs : null,
          };
          player.vm.player.duration = durMs ? durMs / 1000 : 0;
        }
        toast('合成完成', 'ok');
        loadTasks();
      } catch (e) {
        workbenchError.value = e.message;
        toast('合成失败: ' + e.message, 'error', 6000);
      } finally {
        busy.value = false;
        busyText.value = '合成中…';
      }
    }
    const voiceName = (vid) => {
      const v = voices.value.find(v => v.id === vid);
      return v ? v.name : (vid ? '音色 ' + vid : '');
    };

    function previewSpeaker() {
      synthesizePreview({ ...form, text: '你好呀，这是我内置音色的试听效果。' });
    }
    function previewVoice(vid) {
      const v = voices.value.find(v => v.id === (vid || form.voice_id));
      if (!v) return;
      player.playFile(`/api/voices/${v.id}/prompt`, '参考音频 · ' + v.name, null);
    }
    async function synthesizePreview(payload) {
      busy.value = true;
      try {
        const resp = await api('/api/tts', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ ...payload, stream: false, speed: 1.0 }),
        });
        const taskId = resp.headers.get('X-Task-Id');
        player.playFile(URL.createObjectURL(await resp.blob()), '试听', taskId);
        loadTasks();
      } catch (e) { toast('试听失败: ' + e.message, 'error'); }
      finally { busy.value = false; }
    }

    function reloadParams(t) {
      // 回填时剥离开头语言标签, 恢复为语言选择状态
      const m = (t.text || '').match(/^(?:<\|([a-z]{2})\|>\s*)/);
      form.text = displayTaskText(t.text); form.model = t.model; form.mode = t.mode;
      form.lang = (t.mode === 'cross_lingual' && m && m[1]) ? m[1] : '';
      form.spk_id = t.spk_id || ''; form.voice_id = t.voice_id || '';
      form.prompt_text = t.voice_id ? (currentVoice.value ? currentVoice.value.prompt_text : '') : '';
      form.instruct_text = t.instruct_text || ''; form.speed = t.speed || 1.0;
      view.value = 'workbench';
      toast('参数已回填到工作台');
    }
    function downloadTask(taskId) {
      if (!taskId) return toast('试听音频不支持下载', 'error');
      const a = document.createElement('a');
      a.href = `/api/tasks/${taskId}/audio`;
      a.download = `${taskId}.wav`;
      a.click();
    }
    async function deleteTask(t) {
      if (!confirm(`删除这条合成记录？`)) return;
      await api(`/api/tasks/${t.id}`, { method: 'DELETE' });
      tasks.value = tasks.value.filter(x => x.id !== t.id);
      if (playerState.taskId === t.id) player.reset();
    }
    function stopPlayer() { if (player.vm.player.streaming) player.abortStream(); else player.reset(); }
    function playTask(t) {
      player.playFile(`/api/tasks/${t.id}/audio`, t.text.slice(0, 24) + '…', t.id);
    }

    // ---------- 音色库 ----------
    async function onFilePick(e) {
      const f = e.target.files[0];
      if (f) await inspectFile(f);
      e.target.value = '';
    }
    async function onDrop(e) {
      uploadDrag.value = false;
      const f = e.dataTransfer.files[0];
      if (f) await inspectFile(f);
    }
    async function inspectFile(f) {
      upload.checkError = ''; upload.duration = null; upload.sr = null; upload.peaks = [];
      const ext = f.name.split('.').pop().toLowerCase();
      if (!['wav', 'mp3', 'flac', 'm4a', 'ogg', 'opus'].includes(ext)) {
        upload.checkError = `不支持的格式 .${ext}，请使用 wav / mp3 / flac / m4a`;
        return;
      }
      upload.file = f;
      try {
        const ab = await f.arrayBuffer();
        const ctx = new (window.AudioContext || window.webkitAudioContext)();
        const buf = await ctx.decodeAudioData(ab);
        upload.duration = buf.duration;
        upload.sr = buf.sampleRate;
        ctx.close();
        // 迷你波形预览
        const ch = buf.getChannelData(0);
        const buckets = 120, size = Math.max(1, Math.floor(ch.length / buckets));
        upload.peaks = [];
        for (let b = 0; b < buckets; b++) {
          let peak = 0;
          for (let i = b * size; i < Math.min((b + 1) * size, ch.length); i += 4) peak = Math.max(peak, Math.abs(ch[i]));
          upload.peaks.push(peak);
        }
        nextTick(drawDzWave);
      } catch (e) {
        upload.checkError = '无法解码该音频文件，请换一个文件试试';
      }
    }
    function drawDzWave() {
      const c = document.querySelector('.dz-wave');
      if (!c) return;
      const g = c.getContext('2d');
      const w = c.clientWidth, h = c.clientHeight;
      c.width = w; c.height = h;
      g.clearRect(0, 0, w, h);
      const n = upload.peaks.length;
      if (!n) return;
      g.fillStyle = '#4f8cff';
      upload.peaks.forEach((p, i) => {
        const bh = Math.max(2, p * (h - 4));
        g.fillRect((i / n) * w, (h - bh) / 2, Math.max(2, w / n - 1), bh);
      });
    }
    async function uploadVoice() {
      if (!canUpload.value) return;
      upload.busy = true;
      try {
        const fd = new FormData();
        fd.append('name', upload.name.trim());
        fd.append('prompt_text', upload.prompt_text.trim());
        fd.append('file', upload.file);
        const v = await (await api('/api/voices', { method: 'POST', body: fd })).json();
        toast(`音色「${v.name}」已保存`, 'ok');
        upload.name = ''; upload.prompt_text = ''; upload.file = null;
        upload.duration = null; upload.peaks = [];
        await loadVoices();
        form.voice_id = v.id;
        if (form.mode === 'sft') form.mode = 'zero_shot';
        form.prompt_text = v.prompt_text;
        view.value = 'workbench';
      } catch (e) {
        upload.checkError = e.message;
        toast('保存失败: ' + e.message, 'error');
      } finally { upload.busy = false; }
    }
    // ---------- 音色编辑 ----------
    const editVoice = reactive({ id: null, name: '', prompt_text: '' });
    function startVoiceEdit(v) {
      editVoice.id = v.id;
      editVoice.name = v.name;
      editVoice.prompt_text = v.prompt_text;
    }
    function cancelVoiceEdit() { editVoice.id = null; }
    async function saveVoiceEdit() {
      if (!editVoice.name.trim()) { toast('名称不能为空', 'error'); return; }
      if (!editVoice.prompt_text.trim()) { toast('参考文本不能为空', 'error'); return; }
      try {
        const updated = await (await api(`/api/voices/${editVoice.id}`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name: editVoice.name.trim(), prompt_text: editVoice.prompt_text.trim() }),
        })).json();
        const i = voices.value.findIndex(x => x.id === updated.id);
        if (i >= 0) voices.value[i] = updated;
        editVoice.id = null;
        toast('音色已更新', 'ok');
      } catch (e) {
        toast('保存失败: ' + e.message, 'error');
      }
    }
    function useVoice(v) {
      form.voice_id = v.id;
      form.prompt_text = v.prompt_text;
      if (form.mode === 'sft') form.mode = 'zero_shot';
      view.value = 'workbench';
      toast(`已选择音色「${v.name}」`);
    }
    async function deleteVoice(v) {
      if (!confirm(`删除音色「${v.name}」？`)) return;
      await api(`/api/voices/${v.id}`, { method: 'DELETE' });
      voices.value = voices.value.filter(x => x.id !== v.id);
      if (form.voice_id === v.id) form.voice_id = '';
      await fetchHealth();
    }

    // ---------- 系统 ----------
    async function switchModel(key) {
      try {
        await api('/api/admin/model', {
          method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ model: key }),
        });
        updateBanner();
        toast('开始加载模型…', 'info');
      } catch (e) { toast('切换失败: ' + e.message, 'error'); }
    }
    const modelTagClass = (m) => m.loaded ? 'ok' : (m.available ? '' : 'warn');
    const modelTagText = (m) => m.loaded ? '已加载' : (m.available ? '已下载' : '未下载');

    // ---------- 快捷键 / 生命周期 ----------
    function onKeydown(e) {
      if ((e.ctrlKey || e.metaKey) && e.key === 'Enter' && view.value === 'workbench') { synthesize(); }
      else if (e.code === 'Space' && playerState.active && !['INPUT', 'TEXTAREA', 'SELECT'].includes(e.target.tagName)) {
        e.preventDefault(); player.togglePlay();
      }
    }
    onMounted(() => {
      // Player 通过 vm.$refs.waveCanvas 取画布; 单页内用全局查询即可
      const vm = {
        player: playerState,
        toast,
        get $refs() { return { waveCanvas: document.querySelector('.pb-wave') }; },
      };
      player = new Player(vm);
      fetchHealth();
      loadVoices();
      connectSSE();
      setInterval(fetchHealth, 8000);
      window.addEventListener('keydown', onKeydown);
      window.addEventListener('resize', () => { if (playerState.active) player.draw(); });
    });

    const appVm = {
      view, views, health, form, busy, busyText, workbenchError, lastResult,
      voices, tasks, taskFilter, toasts, banner, upload, uploadDrag, player: playerState,
      modeDefs, langs, sampleTexts,
      stateClass, stateText, gpuText, vramPct, port, availableModels, visibleModeDefs, currentDefaults,
      canSynth, synthBlocker, canUpload, filteredTasks, currentVoice,
      fmt, modeName, modelLabel, voiceOrSpk, displayTaskText,
      toast, dismissBanner, switchView, applySample, pickLang, switchMode, toggleStream, goVoices,
      synthesize, previewSpeaker, previewVoice, reloadParams, downloadTask, deleteTask, stopPlayer, playTask,
      onFilePick, onDrop, uploadVoice, useVoice, deleteVoice,
      editVoice, startVoiceEdit, cancelVoiceEdit, saveVoiceEdit,
      switchModel, modelTagClass, modelTagText,
    };
    return appVm;
  },
}).mount('#app');
