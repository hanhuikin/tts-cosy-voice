/* Voice Studio 控制台 — 按 design/prototype.html (v1.8) 实现的交互层, 接线真实 API。
   结构与原型一致: data-act 事件委托 + 全量 render 函数; 数据来自后端接口 (无任何 mock)。
   声音分组 → 后端 mode 的推导见 deriveRequest(): preset→sft, mine→zero_shot/cross_lingual, design→instruct。 */

/* ================= 工具 ================= */
const $=s=>document.querySelector(s);
const $$=s=>Array.from(document.querySelectorAll(s));
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const icon=(id,cls='ic')=>`<svg class="${cls}"><use href="#i-${id}"/></svg>`;
const fmt=s=>{s=Math.max(0,Math.floor(s||0));return Math.floor(s/60)+':'+String(s%60).padStart(2,'0')};
function toast(msg,warn,ms=3000){
  const el=document.createElement('div');el.className='toast'+(warn?' warn':'');
  el.innerHTML=icon(warn?'info':'check','ic sm')+'<span>'+esc(msg)+'</span>';
  $('#toasts').appendChild(el);
  setTimeout(()=>{el.style.opacity='0';el.style.transform='translateY(6px)';setTimeout(()=>el.remove(),230)},ms);
}
function relTime(iso){
  if(!iso)return '';
  const t=new Date(iso).getTime();if(isNaN(t))return String(iso);
  const s=Math.max(0,(Date.now()-t)/1000);
  if(s<60)return '刚刚';
  if(s<3600)return Math.floor(s/60)+' 分钟前';
  if(s<86400)return Math.floor(s/3600)+' 小时前';
  return Math.floor(s/86400)+' 天前';
}
const displayTaskText=t=>(t||'').replace(/^(?:<\|[a-z]{2}\|>\s*)+/,'');

/* ================= 全局播放器 =================
   两种模式: stream = WebAudio 边收边播; file = audio 元素播放。
   原型的 DOM 条形波形由 onTick() 驱动 (进度/时间/playing 态)。 */
const playerState={active:false,playing:false,streaming:false,title:'',taskId:null,
  duration:0,position:0,firstMs:null,sampleRate:null};
class Player{
  PREBUFFER=2.5;
  constructor(onTick){
    this.onTick=onTick||(()=>{});
    this.audio=new Audio();
    this.audio.addEventListener('timeupdate',()=>{
      if(this.mode==='file'){playerState.position=this.audio.currentTime;
        playerState.playing=!this.audio.paused&&!this.audio.ended;this.onTick();}
    });
    this.audio.addEventListener('ended',()=>{playerState.playing=false;this.onTick();});
    this.reset();
  }
  reset(){
    if(this.ctx){try{this.ctx.close()}catch(e){}this.ctx=null}
    if(this.blobUrl){staticWaveCache.delete(this.blobUrl);URL.revokeObjectURL(this.blobUrl);this.blobUrl=null}
    this.audio.pause();this.audio.removeAttribute('src');
    this.mode=null;this.aborter=null;this.parts=[];this.header=null;this.sampleRate=24000;
    this.playedSamples=0;this.totalSamples=0;this.peaks=[];this.rafId=null;this.nextTime=0;this.startTime=0;
    this.segments=[];this.pendHandoff=false;this.holdEnd=false;this.view=null;
    this.lineMarks=[];this.podEpId=null;this.lineMarkSr=24000;   // 逐句采样偏移(播客对话稿高亮对齐用)
    Object.assign(playerState,{active:false,playing:false,streaming:false,title:'',taskId:null,
      duration:0,position:0,firstMs:null,sampleRate:null});
    this.onTick();
  }
  /* 在用户手势的同一拍创建/唤醒 AudioContext: 等到首包(5~12s 后)再创建时,
     浏览器可能已判定激活过期而把 ctx 挂在 suspended —— 表现为波形不动、没有声音 */
  ensureCtx(){
    if(!this.ctx){
      this.ctx=new (window.AudioContext||window.webkitAudioContext)({sampleRate:this.sampleRate});
      this.nextTime=this.ctx.currentTime+this.PREBUFFER;this.startTime=this.nextTime;
      playerState.sampleRate=this.sampleRate;
    }
    if(this.ctx.state==='suspended')this.ctx.resume().catch(()=>{});
    return this.ctx;
  }
  async startStream(resp,title,taskId,extra){
    this.reset();this.mode='stream';this.view=state.view||null;
    if(extra){   // reset 会清空播客关联: 播客流式在 reset 后重新挂上 (标记数组按引用共享, 随流推进)
      if(extra.podEpId)this.podEpId=extra.podEpId;
      if(extra.lineMarks)this.lineMarks=extra.lineMarks;
    }
    Object.assign(playerState,{active:true,streaming:true,title,taskId});
    this.ensureCtx();   // 同步创建: 与点击手势同一拍, 规避自动播放策略
    const reader=resp.body.getReader();this.aborter=reader;
    let buf=new Uint8Array(0),headerParsed=false,gotAudio=false;
    const t0=performance.now();
    const pushPcm=u8=>{
      this.parts.push(u8);
      const samples=u8.byteLength>>1;
      const ctx=this.ensureCtx();
      const audioBuf=ctx.createBuffer(1,samples,ctx.sampleRate);
      const ch=audioBuf.getChannelData(0);
      const dv=new DataView(u8.buffer,u8.byteOffset,u8.byteLength);
      for(let i=0;i<samples;i++)ch[i]=dv.getInt16(i*2,true)/32768;
      const src=ctx.createBufferSource();src.buffer=audioBuf;src.connect(ctx.destination);
      if(!gotAudio){   // 首包才起算时间轴并启动 rAF, 等待首包的几秒不计入播放进度
        gotAudio=true;playerState.firstMs=Math.round(performance.now()-t0);
        this.startTime=ctx.currentTime+this.PREBUFFER;this.nextTime=this.startTime;this.startRaf();
      }
      this.nextTime=Math.max(this.nextTime,ctx.currentTime+this.PREBUFFER);
      this.segments.push({t0:this.nextTime,s0:this.totalSamples});   // 调度段表: audiblePos 据此精确换算可听进度
      src.start(this.nextTime);this.nextTime+=audioBuf.duration;
      this.totalSamples+=samples;
      const step=Math.max(1,Math.floor(samples/12));
      const i16=new Int16Array(u8.buffer,u8.byteOffset,samples);
      for(let s=0;s<samples;s+=step){let peak=0;
        for(let k=s;k<Math.min(s+step,samples);k++)peak=Math.max(peak,Math.abs(i16[k]));
        this.peaks.push(peak/32768);}
      playerState.duration=this.totalSamples/this.sampleRate;
    };
    try{
      while(true){
        const{done,value}=await reader.read();
        if(done)break;
        const merged=new Uint8Array(buf.length+value.length);
        merged.set(buf);merged.set(value,buf.length);buf=merged;
        if(!headerParsed){
          if(buf.length<44)continue;
          this.header=buf.slice(0,44);
          const rate=new DataView(this.header.buffer).getUint32(24,true);
          if(rate&&rate!==this.sampleRate){   // 与预建 ctx 采样率不一致且尚无音频: 重建
            this.sampleRate=rate;
            if(!gotAudio&&this.ctx){try{this.ctx.close()}catch(e){}this.ctx=null}
          }
          buf=buf.slice(44);headerParsed=true;
        }
        const evenLen=buf.length-(buf.length%2);
        if(evenLen>0){pushPcm(buf.slice(0,evenLen));buf=buf.slice(evenLen)}
      }
      if(headerParsed&&this.totalSamples>0){this.finishStream();return true}
      toast('流式响应异常: 未收到音频数据',true);this.reset();return false;
    }catch(e){
      if(e.name!=='AbortError'){console.error('[stream]',e);toast('流式播放中断: '+e.message,true,8000)}
      this.reset();return false;
    }
  }
  finishStream(){
    const dataLen=this.parts.reduce((n,p)=>n+p.byteLength,0);
    const full=new Uint8Array(44+dataLen);
    full.set(this.header,0);
    const dv=new DataView(full.buffer);
    dv.setUint32(4,36+dataLen,true);dv.setUint32(40,dataLen,true);
    let off=44;for(const p of this.parts){full.set(p,off);off+=p.byteLength}
    const blob=new Blob([full],{type:'audio/wav'});
    this.blobUrl=URL.createObjectURL(blob);
    this.audio.src=this.blobUrl;   // 成品 blob 先备好: 播完/重播/下载用, 不立即接管声音
    playerState.streaming=false;
    if(this.audibleRemaining()>0.15){   // 生成快于播放: 网络流收完但 WebAudio 队列还在播 → 保持流式播完再移交
      this.pendHandoff=true;
    }else{
      this.handoffToFile(true);
    }
    this.onTick();
  }
  /* 流式真正播完 → 切 file 模式(blob 就绪)。ended=true: 进度定格 100%, 再按播放键从头重播;
     关闭 AudioContext 后声音只由 audio 元素出, 避免双声源叠加 */
  handoffToFile(ended){
    if(this.ctx){try{this.ctx.close()}catch(e){}this.ctx=null}
    this.mode='file';this.pendHandoff=false;this.holdEnd=!!ended;
    playerState.streaming=false;
    if(ended){playerState.position=playerState.duration;playerState.playing=false}
    this.stopRaf();this.onTick();
  }
  audibleRemaining(){return this.mode==='stream'&&this.ctx?Math.max(0,this.totalSamples-this.audiblePos())/this.sampleRate:0}
  /* 逐句偏移标记([{s:采样偏移,idx:句序}]): 播客流式由读流剥标记推算, 成品来自服务端 lines_meta。
     sr = 标记的采样空间(服务端 24k)。file 模式必须用 lineMarkSr 换算 ——
     decodeAudioData 会把音频重采样到设备率, this.sampleRate 被污染后不能用于标记比较 */
  setLineMarks(marks,sr){this.lineMarkSr=sr||24000;this.lineMarks=(marks||[]).slice().sort((a,b)=>a.s-b.s)}
  /* 当前可听位置落在第几句(采样偏移对齐, 与耳朵同步); 无标记或不属于播客播放返回 -1。
     marks 是每句的起始采样点(第 0 句为 0): 已越过的起点数 - 1 = 正在播放的句序。
     流式播完移交时 audio 元素从未真正播放(currentTime=0), 按 holdEnd 定格最后一句 */
  currentLine(){
    const m=this.lineMarks;if(!m.length)return -1;
    if(this.mode==='file'&&this.holdEnd)return m.length-1;
    const posSamples=this.mode==='stream'
      ?this.audiblePos()
      :(this.audio.currentTime||0)*this.lineMarkSr;
    if(posSamples<=0)return -1;
    let n=0;
    for(const k of m){if(k.s<=posSamples)n++;else break}
    return n-1;
  }
  /* 精确的「正在听见」的采样位置: 按调度段表映射 ctx 时间, 并扣除输出延迟;
     断流重预缓冲插入的空窗不会让视觉进度超前耳朵 */
  audiblePos(){
    if(this.mode!=='stream'||!this.ctx)return 0;
    const t=this.ctx.currentTime-(this.ctx.outputLatency||this.ctx.baseLatency||0);
    let pos=0;
    for(const sg of this.segments){if(t<sg.t0)break;pos=sg.s0+(t-sg.t0)*this.sampleRate}
    return Math.min(Math.max(0,pos),this.totalSamples);
  }
  abortStream(){if(this.aborter){try{this.aborter.cancel()}catch(e){}}this.reset()}
  playFile(url,title,taskId){
    this.reset();this.mode='file';this.view=state.view||null;
    Object.assign(playerState,{active:true,title,taskId});
    this.audio.src=url;
    this.audio.play().then(()=>{playerState.playing=true;this.onTick()}).catch(()=>{});
    this.audio.addEventListener('loadedmetadata',()=>{
      playerState.duration=this.audio.duration;
      this.decodePeaks(url).then(()=>this.onTick());
    },{once:true});
  }
  async decodePeaks(url){
    const r=await decodeWavePeaks(url);
    if(!r)return;   /* 波形解码失败不影响播放 */
    this.sampleRate=r.sampleRate;playerState.sampleRate=r.sampleRate;this.peaks=r.peaks;
  }
  togglePlay(){
    if(this.mode==='stream'){
      if(!this.ctx)return;
      if(this.ctx.state==='running'){this.ctx.suspend();playerState.playing=false}
      else{this.ctx.resume();playerState.playing=true}
    }else if(this.audio.src){
      if(this.audio.paused){this.holdEnd=false;this.audio.play();playerState.playing=true}
      else{this.audio.pause();playerState.playing=false}
    }
    this.onTick();
  }
  startRaf(){
    const loop=()=>{
      if(!playerState.active){this.stopRaf();return}
      if(this.mode==='stream'&&this.ctx){   // ctx 创建前不得读 currentTime, 否则整条 rAF 链崩掉
        playerState.position=this.audiblePos()/this.sampleRate;
        playerState.playing=this.ctx.state==='running';
        if(this.pendHandoff&&this.audibleRemaining()<=0.05)this.handoffToFile(true);
      }
      this.onTick();
      this.rafId=requestAnimationFrame(loop);
    };
    if(!this.rafId)loop();
  }
  stopRaf(){if(this.rafId){cancelAnimationFrame(this.rafId);this.rafId=null}}
  /* 当前播放进度 → 波形渐变边界。stream 模式用可听位置(见 audiblePos, 与耳朵时序对齐);
     file 模式用 audio 元素进度, holdEnd(流式自然播完移交)定格 100% */
  draw(canvas){
    /* 三处波形(#wave/#pod-wave/#pod-stream-wave)同时在 DOM, 只画当前视图可见的那块 */
    canvas=canvas||[...document.querySelectorAll('.pb-wave-c')].find(c=>c.clientWidth>0&&c.clientHeight>0);
    if(!canvas)return;
    const total=Math.max(this.totalSamples,1);
    const progress=this.mode==='stream'
      ?Math.min(1,this.audiblePos()/total)
      :(this.holdEnd?1:(this.audio.duration?this.audio.currentTime/this.audio.duration:0));
    paintWave(canvas,this.peaks,progress);
  }
}
/* ---------- 波形绘制(Player 与静态成品共用) ----------
   canvas 峰值波形(移植自 main 分支): 已播放=蓝→青渐变圆角条, 未播放=暗灰;
   peaks 按显示宽度重采样为固定条数(长音频条形不糊), 未就绪时用确定性伪峰值占位 */
function paintWave(canvas,peaks,progress){
  const dpr=window.devicePixelRatio||1;
  const w=canvas.clientWidth,h=canvas.clientHeight;
  if(!w||!h)return;
  if(canvas.width!==Math.round(w*dpr)){canvas.width=Math.round(w*dpr);canvas.height=Math.round(h*dpr)}
  const g=canvas.getContext('2d');
  g.setTransform(dpr,0,0,dpr,0,0);
  g.clearRect(0,0,w,h);
  if(!peaks||!peaks.length){
    if(!Player.fakePeaks)Player.fakePeaks=Array.from({length:64},(_,i)=>.25+.45*Math.abs(Math.sin(i*1.7)+.3*Math.sin(i*.53)));
    peaks=Player.fakePeaks;
  }
  const n=Math.max(40,Math.min(160,Math.round(w/4)));
  let disp=peaks;
  if(peaks.length>n){
    disp=[];const size=peaks.length/n;
    for(let b=0;b<n;b++){let m=0;
      const a=Math.floor(b*size),z=Math.max(a+1,Math.floor((b+1)*size));
      for(let j=a;j<z;j++)m=Math.max(m,peaks[j]||0);
      disp.push(m);}
  }
  const barW=Math.max(2,w/n-1);
  for(let i=0;i<n;i++){
    const x=(i/n)*w;
    const amp=Math.max(.06,Math.min(1,disp[i]));
    const bh=amp*(h-6);
    const played=(i/n)<=progress;
    if(played){
      const grad=g.createLinearGradient(x,0,x,h);
      grad.addColorStop(0,'#4f8cff');grad.addColorStop(1,'#22d3ee');
      g.fillStyle=grad;
    }else{
      g.fillStyle=document.documentElement.dataset.theme==='light'?'#c9d3e4':'#3a465e';
    }
    g.beginPath();
    const r=Math.min(barW/2,2);
    g.roundRect(x,(h-bh)/2,barW,bh,r);
    g.fill();
  }
}
/* 成品(未在播放时)的静态波形: 按音频地址解码并缓存, 进度 0 全灰展示 */
const staticWaveCache=new Map();
function decodeWavePeaks(url){
  if(!staticWaveCache.has(url)){
    const p=(async()=>{try{
      const ab=await(await fetch(url)).arrayBuffer();
      // 按 WAV 头的真实采样率建解码上下文: decodeAudioData 会重采样到上下文率,
      // 不固定的话 peaks 的时间轴和 player.sampleRate 都会被设备率(44.1k/48k)污染
      let rate=0;try{rate=new DataView(ab).getUint32(24,true)}catch(e){}
      const ctx=new (window.AudioContext||window.webkitAudioContext)(rate?{sampleRate:rate}:{});
      const buf=await ctx.decodeAudioData(ab);ctx.close();
      const ch=buf.getChannelData(0);
      const buckets=Math.min(220,Math.max(60,Math.floor(ch.length/400)));
      const size=Math.max(1,Math.floor(ch.length/buckets));
      const peaks=[];
      for(let b=0;b<buckets;b++){let peak=0;
        for(let i=b*size;i<Math.min((b+1)*size,ch.length);i+=4)peak=Math.max(peak,Math.abs(ch[i]));
        peaks.push(peak);}
      return{peaks,sampleRate:buf.sampleRate};
    }catch(e){return null}})();
    staticWaveCache.set(url,p);
    if(staticWaveCache.size>24){const k=staticWaveCache.keys().next().value;staticWaveCache.delete(k)}
  }
  return staticWaveCache.get(url);
}
let staticWaveSeq=0;
async function drawStaticWave(canvas,url){
  const seq=++staticWaveSeq;
  const r=await decodeWavePeaks(url);
  if(seq!==staticWaveSeq)return;                                  // 已有更新的绘制请求
  if(!canvas.isConnected||!canvas.clientWidth)return;             // 画布已被重渲染替换
  if(player&&playerState.active&&player.view===state.view)return; // 播放已接管波形
  paintWave(canvas,r?r.peaks:null,0);
}
/* 当前视图的波形画什么: 播放中→player 实时进度; 否则若有成品音频→静态解码真实波形 */
function drawVisibleWave(){
  const canvas=[...document.querySelectorAll('.pb-wave-c')].find(c=>c.clientWidth>0&&c.clientHeight>0);
  if(!canvas)return;
  if(player&&playerState.active&&player.view===state.view){player.draw(canvas);return}
  const url=staticWaveUrl();
  if(url)drawStaticWave(canvas,url);
  else paintWave(canvas,null,0);
}
function staticWaveUrl(){
  if(state.view==='workbench')
    return state.result&&state.result.taskId?`/api/tasks/${state.result.taskId}/audio`:null;
  if(state.view==='podcast'){
    if(pod.outTab!=='final')return null;
    return podSynthDone()?`/api/podcast/episodes/${pod.episode.id}/audio`:null;
  }
  return null;
}
let player=null;
function onPlayerTick(){
  if(state.view==='workbench'&&state.result)syncPlayer();
  if(state.view==='podcast'&&(pod.outTab==='final'||pod.outTab==='stream'))updatePodWaveLive();
  /* 对话稿播放跟随: 当前句号变化才更新高亮/滚动 */
  if(state.view==='podcast'){
    const li=player&&player.podEpId&&player.podEpId===(pod.episode&&pod.episode.id)?player.currentLine():-1;
    if(li!==pod.playLine){pod.playLine=li;updatePodLineHighlight()}
  }
  drawVisibleWave();
}

/* ================= API ================= */
async function api(path,opts){
  const resp=await fetch(path,opts);
  if(!resp.ok){
    let msg=`HTTP ${resp.status}`;
    try{msg=(await resp.json()).detail||msg}catch(e){}
    throw new Error(msg);
  }
  return resp;
}

/* ================= 数据 ================= */
const health={state:'idle',model:null,model_label:null,sample_rate:null,speakers:[],
  queue_len:0,queue_max:16,last_infer_ms:null,error:null,gpu:null,voices_count:0,models:[]};

/* 后端模型能力 → 界面分组/高级项 (对应 spec 08 能力映射表) */
function capsOf(m){
  m=m||model();if(!m)return{};
  const modes=m.modes||[],spk=m.speaker_modes||[],ref=m.reference_modes||[];
  return{
    preset:spk.includes('sft'),
    mine:modes.includes('zero_shot'),
    design:m.instruct_kind==='design'&&modes.includes('instruct')&&!spk.includes('instruct'),
    lang:(m.lang_codes||[]).length>0,
    instruct:modes.includes('instruct'),
    stream:m.stream_mode==='native',
    cloneText:m.requires_prompt_text===true,
    crossLingual:modes.includes('cross_lingual'),
    refInstruct:ref.includes('instruct'),   /* 克隆音色 + 风格指令 (CosyVoice) */
    spkInstruct:spk.includes('instruct'),   /* 预置音色 + 风格指令 (Qwen3-TTS) */
  };
}
/* Qwen3-TTS 内置说话人显示名 (未加载模型时也能展示卡片; 加载后以 /api/health.speakers 为准) */
const QWEN3_KNOWN={aiden:['Aiden','英语男声 · 播报'],dylan:['Dylan','中文男声 · 沉稳'],eric:['Eric','英语男声 · 温和'],
  ono_anna:['Ono Anna','日语女声'],ryan:['Ryan','英语男声 · 活力'],serena:['Serena','中文女声 · 温柔'],
  sohee:['Sohee','韩语女声'],uncle_fu:['Uncle Fu','中文男声 · 低沉'],vivian:['Vivian','中文女声 · 明亮']};
const SAMPLES=[
  {label:'产品发布词',text:'大家好，欢迎来到今天的发布会。我们花了整整一年时间，把一件复杂的事情做得足够简单。现在，让我们一起看看它。'},
  {label:'播客开场',text:'欢迎收听新一期的节目。我是主播，今天想和你聊聊，为什么好的工具总是让人感觉不到它的存在。'},
  {label:'英文短句',text:'Welcome aboard. It is great to have you here. Let us get started.'}
];
const GROUPS=[['preset','预置音色'],['mine','我的音色'],['design','声音设计']];
const LANG_DEFS=[['zh','中文'],['en','English'],['ja','日本語'],['ko','한국어'],['yue','粤语'],
  ['de','Deutsch'],['fr','Français'],['es','Español'],['ru','Русский'],['it','Italiano'],['pt','Português'],['ar','العربية']];
const LANG_DEFAULT=['zh','en','ja','ko','yue'];
const SYS_MODE_NAMES={sft:'预置音色',zero_shot:'声音克隆',cross_lingual:'跨语种复刻',instruct:'指令控制'};
const TITLES={workbench:['创作台','把文字变成声音'],podcast:['播客工坊','多人对话一次成片'],
  voices:['我的音色','上传一次，处处复用'],library:['作品库','生成记录与参数复用'],
  system:['系统状态与设置','模型、运行环境与权重目录配置']};

const state={view:'workbench',model:'cosyvoice3',modelTouched:false,group:'preset',pick:null,
  text:'',designText:'',refText:'',speed:1,lang:'',stream:false,instruct:'',
  busy:false,previewBusy:false,genProg:0,result:null,menu:false,tasks:[],filter:'全部',bars:[],
  genTaskId:null,genAbort:null,genStopping:false};
state.bars=Array.from({length:40},(_,i)=>18+Math.round(Math.abs(Math.sin(i*1.7)*Math.cos(i*.43))*32));
const voices=[];
const vlib={file:null,name:'',prompt:'',busy:false,error:'',drag:false,peaks:[],
  editId:null,editName:'',editPrompt:'',confirmDel:null};

/* ---- 播客工坊状态 ---- */
const pod={
  src:'text',text:'',url:'',fileName:'',fileRaw:null,
  extractText:'',extractChars:0,extracting:false,sourceError:'',
  extracted:false,extractText0:'',extractDirty:false,
  lines:[],speakers:[
    {role:'host',name:'主播',desc:'',voice_id:''},
    {role:'guest',name:'嘉宾1',desc:'',voice_id:''},
  ],
  speed:1,genning:false,genRaw:'',editLine:-1,editText:'',
    outTab:'final',protocol:'rtmp',pushOn:false,pushUrl:'',pushing:false,pushStatus:'',playLine:-1,
  busyFinal:false,busyStream:false,busySince:0,progress:0,stopRequested:'',stopping:false,
  episode:null,episodes:[],confirmDel:null,detail:null,detailOpen:false,ffmpeg:true,
  confirmPushOff:false,
};
const llmCfg={base_url:'',model:'',api_key_masked:'',temperature:0.7,rounds_hint:'10~16',style_prompt:''};
const adopted=new Map();   // episode id -> 接管时的任务类型, 终态时补通知
let podPollTick=0;
const QUEUE_ACTIVE=['synthesizing','pushing'];

/* ---- 系统状态 ---- */
const sys={refreshing:false,doctorBusy:{},doctorResult:{},expanded:{},updated:'刚刚刷新',settings:false};
/* 05H 权重目录配置: 原型边界 — 浏览器本地演示, 服务端以启动环境变量为准 */
const SYS_DIR_ENV={cosyvoice3:'COSYVOICE_MODELS_DIR',cosyvoice2:'COSYVOICE_MODELS_DIR',
  indextts2:'INDEXTTS2_DIR',indextts25:'INDEXTTS25_DIR',
  qwen3tts:'QWEN3TTS_DIR',qwen3tts_base:'QWEN3TTS_BASE_DIR',qwen3tts_design:'QWEN3TTS_DESIGN_DIR'};
let sysDefaults={root:'',repo:'',pyIndextts:'',pyQwen3:'',dirs:{}};
let sysConfig=null;
function buildSysDefaults(){
  const models=health.models||[];
  const dirs={};models.forEach(m=>{dirs[m.key]=m.dir||''});
  const first=Object.values(dirs).find(Boolean)||'';
  const rt=k=>((models.find(m=>m.key===k)||{}).runtime||'');
  sysDefaults={root:first?first.replace(/[\\/][^\\/]+$/,''):'',
    repo:rt('indextts2')?'':'',pyIndextts:rt('indextts2'),pyQwen3:rt('qwen3tts'),dirs};
}
function loadSysConfig(){
  const d=JSON.parse(JSON.stringify(sysDefaults));
  try{const o=JSON.parse(localStorage.getItem('vs-sys-config')||'{}');
    ['root','repo','pyIndextts','pyQwen3'].forEach(k=>{if(o[k])d[k]=o[k]});
    if(o.dirs)Object.keys(d.dirs).forEach(k=>{if(o.dirs[k])d.dirs[k]=o.dirs[k]});
  }catch(_){}
  return d;
}

/* ================= 基础取值 ================= */
function model(){return (health.models||[]).find(m=>m.key===state.model)}
function caps(){return capsOf(model())}
function modelLabel(k){const m=(health.models||[]).find(x=>x.key===k);return m?m.label:(k||'—')}
const voiceName=vid=>{const v=voices.find(v=>v.id===vid);return v?v.name:(vid?'音色 '+vid:'')};
function presetSpeakers(){
  const m=model();if(!m)return[];
  if(state.model===health.model&&health.speakers.length)
    return health.speakers.map(id=>({id,name:(QWEN3_KNOWN[id]||[id])[0],meta:(QWEN3_KNOWN[id]||[id,id])[1]||'内置音色'}));
  if(state.model==='qwen3tts')
    return Object.entries(QWEN3_KNOWN).map(([id,[name,meta]])=>({id,name,meta}));
  return[];
}
function taskKind(t){
  if(t.mode==='sft')return'预置音色';
  if(t.mode==='instruct')return t.model==='qwen3tts_design'?'声音设计':(t.spk_id?'预置音色':'我的音色');
  return'我的音色';
}
function waveBars(n){return Array.from({length:n},(_,i)=>state.bars[i%state.bars.length])}
function peaksToBars(n,amp=44){
  const p=player&&player.peaks;
  if(!p||!p.length)return waveBars(n);
  const out=[];
  for(let i=0;i<n;i++){const v=p[Math.floor(i/n*p.length)]||0;
    out.push(Math.max(6,Math.round(Math.min(1,v)*amp)));}
  return out;
}
function spkVoiceLabel(s){return `${s.name||'未命名'} · ${voiceName(s.voice_id)||'未配置'}`}
function ensureSpeakerNames(){
  for(const s of pod.speakers){if(!(s.name||'').trim())s.name=s.role==='host'?'主播':`嘉宾${pod.speakers.filter(x=>x.role!=='host').indexOf(s)+1}`}
}
function speakersSummary(ep){
  const spks=Array.isArray(ep&&ep.speakers)&&ep.speakers.length?ep.speakers:null;
  if(!spks)return'—';
  return spks.map(s=>`${s.name||(s.role==='host'?'主播':'嘉宾')}（${voiceName(s.voice_id)||'未配置'}）`).join(' · ');
}
function podCharCount(){return pod.lines.reduce((n,l)=>n+(l.text||'').length,0)}
function podEstimate(){const sec=Math.round(podCharCount()/4.3/Math.max(.5,pod.speed));return `${Math.floor(sec/60)} 分 ${sec%60} 秒`}
function podSrcReady(){return (pod.src==='text'?pod.text:pod.extractText).trim().length>=10}
function podSynthDone(){return !!(pod.episode&&((pod.episode.duration||0)>0||pod.episode.status==='completed'))}
function podEpisodeTasks(){
  return pod.episodes.filter(e=>e.status&&e.status!=='draft')
    .slice().sort((a,b)=>((QUEUE_ACTIVE.includes(b.status)?1:0)-(QUEUE_ACTIVE.includes(a.status)?1:0))
      ||(a.created_at<b.created_at?1:-1));
}

/* ================= 主题 ================= */
function syncThemeIcon(){const d=document.documentElement.getAttribute('data-theme')==='dark';
  $('#theme-icon').innerHTML=`<use href="#i-${d?'sun':'moon'}"/>`}

/* ================= 模型菜单 / 能力提示 ================= */
function renderModelMenu(){
  const m=model();
  $('#model-name').textContent=m?m.label:'—';
  $('#model-btn').setAttribute('aria-expanded',state.menu);
  $('#model-menu').hidden=!state.menu;
  const ready=(health.models||[]).filter(x=>x.available).length;
  $('#model-menu').innerHTML=`<div class="menu-head">选择推理模型 · ${ready} 个权重就绪</div>`+(health.models||[]).map(mm=>`
   <button class="menu-item" data-act="model" data-key="${mm.key}" role="option" aria-selected="${mm.key===state.model}">
    <span class="dot" style="box-shadow:none;background:${mm.loaded?'var(--primary)':mm.available?'var(--ok)':'var(--danger)'}"></span>
    <span class="mi-main"><span class="mi-name">${esc(mm.label)}</span><span class="mi-note">${esc(capsNote(mm))}</span></span>
    ${mm.key===state.model?icon('check','ic sm mi-check'):''}</button>`).join('');
}
function capsNote(m){const c=capsOf(m);const parts=[];
  if(c.preset)parts.push('预置音色');if(c.mine)parts.push('声音克隆');if(c.design)parts.push('声音设计');
  return parts.join(' · ')||'—';}
function renderCapNotice(){
  const m=model();if(!m){$('#cap-text').textContent='正在读取模型能力…';return}
  const parts=[];const c=caps();
  if(c.preset)parts.push('预置音色');if(c.mine)parts.push('声音克隆');if(c.design)parts.push('声音设计');
  $('#cap-text').innerHTML=`<strong>${esc(m.label)}</strong> 当前可用：${parts.join(' · ')}。切换模型时，界面会自动只显示该模型支持的方式。`;
}

/* ================= 声音选择 ================= */
function renderTabs(){
  const c=caps();
  $('#group-tabs').innerHTML=GROUPS.filter(([k])=>c[k]).map(([k,n])=>
    `<button class="tab ${state.group===k?'active':''}" role="tab" aria-selected="${state.group===k}" data-act="group" data-key="${k}">${n}</button>`).join('');
}
function renderSounds(){
  const c=caps();let html='';
  if(state.group==='preset'){
    const spks=presetSpeakers();
    html=spks.map((s,i)=>`
     <button class="sound ${state.pick===s.id?'active':''}" data-act="pick" data-key="${s.id}">
      ${state.pick===s.id?`<span class="sound-check">${icon('check')}</span>`:''}
      <span class="sound-top"><span class="avatar a${i%5+1}">${esc((s.name||'?').slice(0,1))}</span><span><span class="sound-name">${esc(s.name)}</span><span class="sound-meta">${esc(s.meta)}</span></span></span>
      <span class="sound-meta" style="display:flex;align-items:center">${state.model===health.model?'内置音色':'未加载 · 生成时自动加载'}<span class="sound-play" data-act="preview" data-key="${esc(s.id)}" title="试听">${icon('play','ic sm')}</span></span>
     </button>`).join('')||'<div class="empty-inline">该模型没有内置说话人（或尚未加载）</div>';
  }else if(state.group==='mine'){
    html=voices.map((v,i)=>`
     <button class="sound ${state.pick===v.id?'active':''}" data-act="pick" data-key="${v.id}">
      ${state.pick===v.id?`<span class="sound-check">${icon('check')}</span>`:''}
      <span class="sound-top"><span class="avatar a${i%5+1}">${esc((v.name||'?').slice(0,1))}</span><span><span class="sound-name">${esc(v.name)}</span><span class="sound-meta">${esc(voiceMeta(v))}</span></span></span>
      <span class="sound-meta" style="display:flex;align-items:center">${v.prompt_text?'参考文本已填':'<span style="color:var(--warn)">缺参考文本</span>'}<span class="sound-play" data-act="preview" data-key="${esc(v.id)}" title="试听参考音频">${icon('play','ic sm')}</span></span>
     </button>`).join('')||'<div class="empty-inline">还没有克隆音色，去「我的音色」上传一段参考音频</div>';
  }else{
    html=`<button class="sound ${state.pick==='design'?'active':''}" data-act="pick" data-key="design" style="grid-column:1/-1;min-height:88px">
     ${state.pick==='design'?`<span class="sound-check">${icon('check')}</span>`:''}
     <span class="sound-top"><span class="avatar a5">${icon('spark','ic sm')}</span><span><span class="sound-name">用一句话描述一个新声音</span><span class="sound-meta">不需要上传音频，模型会按描述生成音色</span></span></span></button>`;
  }
  $('#sound-grid').innerHTML=html;
  renderExtra();
}
function voiceMeta(v){
  const parts=[];
  if(v.duration)parts.push(v.duration.toFixed(1)+'s');
  if(v.sample_rate)parts.push(v.sample_rate+' Hz');
  if(v.created_at)parts.push(relTime(v.created_at));
  return parts.join(' · ')||'克隆音色';
}
function renderExtra(){
  const m=model();if(!m){$('#extra-fields').innerHTML='';return}
  const c=caps();let html='';
  if(state.group==='design'&&state.pick==='design'){
    html+=`<div class="field"><label class="field-label" for="design-input">声音描述 <span class="opt">必填 · 描述性别、年龄、音调、情绪或使用场景</span></label>
     <textarea class="textarea" id="design-input" rows="2" style="min-height:76px" placeholder="例如：沉稳的中年男声，语速偏慢，适合纪录片旁白">${esc(state.designText)}</textarea>
     <div class="chips">${['沉稳的中年男声，适合纪录片旁白','活泼的年轻女声，适合短视频','温柔的睡前故事女声'].map(t=>`<button class="chip" data-act="design-preset" data-t="${esc(t)}">${esc(t.slice(0,10))}…</button>`).join('')}</div></div>`;
  }
  if(state.group==='mine'&&state.pick){
    const v=voices.find(x=>x.id===state.pick);
    html+=`<div class="field"><label class="field-label" for="ref-input">参考音频文字内容 <span class="opt">${c.cloneText?'必填 · 当前模型需要':'选填 · 当前模型可自动识别'}</span></label>
     <input class="input" id="ref-input" value="${esc(state.refText)}" placeholder="参考音频里说的那句话…">
     <div class="chips"><span class="chip-label">${v&&v.prompt_text?'已自动回填':'此音色尚未录入参考文本'}</span>${v&&!v.prompt_text?`<button class="chip" data-act="go-voices">去补录</button>`:''}</div></div>`;
  }
  $('#extra-fields').innerHTML=html;
}

/* ================= 高级设置 ================= */
function visibleLangs(){
  const codes=(model()&&model().lang_codes)||[];
  const defs=codes.length?LANG_DEFS.filter(l=>codes.includes(l[0])):LANG_DEFS.filter(l=>LANG_DEFAULT.includes(l[0]));
  return defs.length?defs:LANG_DEFS.slice(0,5);
}
function instructUsable(){
  const c=caps();
  if(!c.instruct||state.group==='design')return false;
  if(state.group==='preset')return c.spkInstruct;      /* 预置音色 + 风格指令 (qwen3tts) */
  if(state.group==='mine')return c.refInstruct;        /* 克隆音色 + 风格指令 (cosyvoice) */
  return false;
}
function renderAdv(){
  const m=model();if(!m){$('#adv-body').innerHTML='';return}
  const c=caps();const rows=[];
  rows.push(`<div class="adv-row"><div class="adv-name">语速<small>0.5× ~ 2.0×</small></div><div class="adv-ctl"><input type="range" id="speed-range" min="0.5" max="2" step="0.05" value="${state.speed}" aria-label="语速"><span class="range-val" id="speed-val">${state.speed.toFixed(2)}×</span></div></div>`);
  if(c.lang||c.crossLingual){
    const langs=visibleLangs();
    rows.push(`<div class="adv-row"><div class="adv-name">目标语言<small>${c.crossLingual?'跨语种复刻 · 选中后自动加语种标签':'选中后自动加语种标签'}</small></div><div class="adv-ctl">${langs.map(([code,n])=>`<button class="chip ${state.lang===code?'active':''}" data-act="lang" data-key="${code}">${n}</button>`).join('')}<button class="chip ghost" data-act="lang" data-key="">不限</button></div></div>`);
  }
  if(instructUsable()){
    rows.push(`<div class="adv-row"><div class="adv-name">风格指令<small>可留空</small></div><div class="adv-ctl"><input class="input" id="instruct-input" value="${esc(state.instruct)}" placeholder="例如：用四川话说这句话 / 用特别愤怒的语气"></div></div>`);
  }
  rows.push(`<div class="adv-row"><div class="adv-name">流式合成<small>${c.stream?'边生成边播放':'当前模型为整段生成后分块下发'}</small></div><div class="adv-ctl"><button class="toggle ${state.stream?'on':''}" data-act="stream" role="switch" aria-checked="${state.stream}" aria-label="流式合成" ${c.stream?'':'disabled style="opacity:.4;cursor:not-allowed"'}></button><span style="font-size:12px;color:var(--text-3)">${c.stream?(state.stream?'已开启 · 首包更快':'关闭 · 音质更稳'):'当前模型不支持边播边生成'}</span></div></div>`);
  $('#adv-body').innerHTML=rows.join('');
}

/* ================= 操作条 ================= */
/* 已选音色自带参考文本时自动回填 (刷新/切模型后 pick 恢复但 refText 为空) */
function ensureRefText(){
  if(state.group!=='mine'||state.refText.trim())return;
  const v=voices.find(x=>x.id===state.pick);
  if(v&&v.prompt_text)state.refText=v.prompt_text;
}
function blocker(){
  if(state.busy)return'正在生成…';
  if(health.state==='loading')return'模型加载中…';
  if(health.state==='error')return'模型加载失败，请到「系统状态与设置」查看';
  if(health.state==='idle')return'请先在「系统状态与设置」加载一个模型';
  if(!state.text.trim())return'先输入要合成的文本';
  if(!state.pick)return'选一个声音';
  const c=caps();
  if(state.group==='design'&&!state.designText.trim())return'描述一下你想要的声音';
  if(state.group==='preset'&&!presetSpeakers().length)return'当前模型没有内置说话人';
  if(state.group==='mine'&&c.cloneText&&!state.refText.trim())return'当前模型需要参考音频的文字内容';
  return'';
}
function renderAction(){
  ensureRefText();
  const b=blocker();const m=model();const c=caps();
  const dur=Math.max(1.2,Math.round(state.text.trim().length/5.2*10)/10);
  const langName=state.lang?(visibleLangs().find(l=>l[0]===state.lang)||['',''])[1]:'';
  $('#action-info').innerHTML=`<strong>${b?'':`预计 ${dur.toFixed(1)} 秒 · ${health.sample_rate||'—'} Hz WAV`}</strong><span class="${b?'warn-txt':''}">${b||`将使用 ${esc(m?m.label:'—')}${langName?' · '+esc(langName):''} · ${state.speed.toFixed(2)}× 语速`}</span>`;
  const btn=$('#gen-btn');btn.disabled=!!b;
  $('#gen-label').textContent=state.busy?'生成中…':(state.result?'重新生成':'生成语音');
}

/* ================= 右侧输出面板 (创作台) ================= */
function setPill(cls,text){const p=$('#out-pill');p.className='pill'+(cls?' '+cls:'');p.textContent=text}
function renderOutput(){
  if(state.view!=='workbench')return;
  if(state.busy){
    setPill('busy','生成中');
    $('#out-body').innerHTML=`<div class="result-card"><p class="result-title">${state.genStopping?'正在停止…':'正在合成…'}</p><div class="result-meta">${esc(model()?model().label:'')} · 排队与推理中</div><div class="progress-track"><div class="progress-fill" id="gen-progress" style="width:12%"></div></div><div class="times"><span>首次加载模型会稍慢</span><span>${state.genStopping?'推理将在分块边界停止':'<button class="chip" data-act="gen-stop" style="color:var(--danger)">■ 停止生成</button>'}</span></div></div>`;
    return;
  }
  if(!state.result){
    setPill('','待生成');
    const bars=waveBars(28).map(h=>`<i style="--h:${Math.max(6,h*.55)}px"></i>`).join('');
    $('#out-body').innerHTML=`<div class="out-empty"><div class="wave-static">${bars}</div><p>还没有生成结果</p><span>填好文本、选好声音，点「生成语音」</span></div>`;
    return;
  }
  setPill('ok','已完成');
  const r=state.result;
  $('#out-body').innerHTML=`<div class="result-card">
   <p class="result-title">${esc(r.title)}</p><div class="result-meta">${esc(r.model)} · ${esc(r.voice)} · ${esc(r.time)}</div>
   <canvas class="pb-wave-c" id="wave" style="height:44px"></canvas>
   <div class="times"><span id="t-cur">${fmt(playerState.position)}</span><span>${fmt(playerState.duration||r.dur)}</span></div>
   <div class="player-ctrl"><button class="play-main" data-act="play" aria-label="播放或暂停">${icon(playerState.playing?'pause':'play','ic sm')}</button>
    <button class="mini-btn" data-act="download" title="下载 WAV">${icon('down','ic sm')}</button>
    <button class="mini-btn" data-act="reuse" title="复用参数">${icon('copy','ic sm')}</button>
    <span style="flex:1"></span><span class="tag">${playerState.sampleRate?playerState.sampleRate+' Hz':(health.sample_rate||'—')+' Hz'}</span></div></div>`;
  syncPlayer();
}
function syncPlayer(){
  const r=state.result;if(!r||state.view!=='workbench')return;
  const cur=$('#t-cur');if(cur)cur.textContent=fmt(playerState.position);
  const playBtn=$('[data-act="play"]');if(playBtn)playBtn.innerHTML=icon(playerState.playing?'pause':'play','ic sm');
  drawVisibleWave();
}
function renderRecent(){
  const items=state.tasks.slice(0,4);
  $('#recent-list').innerHTML=items.map(t=>`<div class="recent" data-act="play-recent" data-key="${t.id}" title="点击播放">
   <span class="avatar a3" style="width:30px;height:30px;font-size:11px">${icon('play','ic sm')}</span>
   <span class="r-main"><span class="r-name">${esc(displayTaskText(t.text).slice(0,26)||'（空文本）')}</span>
   <span class="r-meta">${esc(modelLabel(t.model))} · ${esc(voiceOrSpk(t)||SYS_MODE_NAMES[t.mode]||t.mode)}</span></span>
   <span class="r-dur">${t.duration?t.duration.toFixed(1)+'s':'—'}</span></div>`).join('')
   ||'<div class="out-empty-sm">还没有生成记录</div>';
  $('#nav-count').textContent=state.tasks.length;
}
function voiceOrSpk(t){
  if(t.spk_id)return (QWEN3_KNOWN[t.spk_id]||[t.spk_id])[0];
  if(t.voice_id)return voiceName(t.voice_id);
  return'';
}

/* ================= 作品库 ================= */
function renderLibrary(){
  if(state.view!=='library')return;
  const tasks=state.tasks;
  const total=tasks.reduce((a,b)=>a+(b.duration||0),0);
  const week=tasks.filter(t=>{const d=new Date(t.created_at).getTime();
    return !isNaN(d)&&Date.now()-d<7*86400*1000}).length;
  const ready=(health.models||[]).filter(m=>m.available).length;
  $('#lib-stats').innerHTML=[['作品总数',String(tasks.length),'个'],['本周新增',String(week),'个'],
    ['累计时长',String(Math.round(total)),'秒'],['可用模型',String(ready),'个']]
    .map(([k,v,u])=>`<div class="stat"><div class="k">${k}</div><div class="v">${v} <small>${u}</small></div></div>`).join('');
  const filters=['全部','预置音色','我的音色','声音设计'];
  $('#lib-filters').innerHTML=filters.map(f=>`<button class="chip ${state.filter===f?'active':''}" data-act="filter" data-key="${f}">${f}</button>`).join('');
  const list=state.filter==='全部'?tasks:tasks.filter(t=>taskKind(t)===state.filter);
  $('#lib-list').innerHTML=list.length?list.map(t=>`<div class="list-row">
    <span class="avatar a4" style="width:34px;height:34px;font-size:12px">${icon('play','ic sm')}</span>
    <span class="lr-main"><span class="lr-title">${esc(displayTaskText(t.text).slice(0,32)||'（空文本）')}</span>
    <span class="lr-meta">${esc(modelLabel(t.model))} · ${esc(voiceOrSpk(t)||SYS_MODE_NAMES[t.mode]||t.mode)} · ${esc(relTime(t.created_at))}</span></span>
    <span class="tag ${taskKind(t)==='声音设计'?'acc':''}">${taskKind(t)}</span>
    <span class="r-dur" style="width:52px;text-align:right">${t.duration?t.duration.toFixed(1)+'s':'—'}</span>
    <button class="mini-btn" data-act="play-task" data-key="${t.id}" aria-label="播放">${icon('play','ic sm')}</button>
    <button class="mini-btn" data-act="download-task" data-key="${t.id}" aria-label="下载">${icon('down','ic sm')}</button></div>`).join('')
    :'<div class="out-empty" style="border:none"><p>这个分类下还没有作品</p><span>去创作台生成第一条吧</span></div>';
  $('#nav-count').textContent=tasks.length;
}

/* ================= 我的音色 (v1.8) ================= */
const VLIB_EXTS=['wav','mp3','flac','m4a','ogg','opus'];
async function vlibInspectFile(f){
  vlib.error='';
  const ext=(f.name.split('.').pop()||'').toLowerCase();
  if(!VLIB_EXTS.includes(ext)){
    vlib.file=null;
    vlib.error=`不支持的格式 .${ext}，请使用 wav / mp3 / flac / m4a / ogg / opus`;
    renderVoiceUpload();return;
  }
  vlib.file={name:f.name,raw:f};
  renderVoiceUpload();
  try{
    const ab=await f.arrayBuffer();
    const ctx=new (window.AudioContext||window.webkitAudioContext)();
    const buf=await ctx.decodeAudioData(ab);ctx.close();
    const dur=buf.duration,sr=buf.sampleRate;
    const ch=buf.getChannelData(0);
    const buckets=120,size=Math.max(1,Math.floor(ch.length/buckets));
    vlib.peaks=[];
    for(let b=0;b<buckets;b++){let peak=0;
      for(let i=b*size;i<Math.min((b+1)*size,ch.length);i+=4)peak=Math.max(peak,Math.abs(ch[i]));
      vlib.peaks.push(peak);}
    vlib.file={name:f.name,raw:f,duration:dur,sr,ok:dur>=1&&dur<=30,
      reason:dur>=1&&dur<=30?'':'时长需在 1~30 秒'};
  }catch(e){
    vlib.file=null;vlib.error='无法解码该音频文件，请换一个文件试试';
  }
  renderVoiceUpload();vlibUpdateSave();
}
function vlibCanSave(){return !vlib.busy&&!!vlib.file&&vlib.file.ok&&!!vlib.name.trim()&&!!vlib.prompt.trim()}
function vlibWhy(){
  if(vlib.busy)return'正在保存…';
  if(!vlib.file)return'先选择一段参考音频';
  if(!vlib.file.ok)return'音频不符合要求，请重新选择';
  if(!vlib.name.trim())return'请填写音色名称';
  if(!vlib.prompt.trim())return'请填写参考音频文字内容';
  return'名称与参考文本会随音色一起保存';
}
function vlibUpdateSave(){
  const b=$('#voice-save-btn');if(b)b.disabled=!vlibCanSave();
  const w=$('#voice-save-why');if(w)w.textContent=vlibWhy();
}
function renderVoiceUpload(){
  const el=$('#voice-upload-card');if(!el)return;
  const f=vlib.file;
  const fileHtml=f?`<div class="vlib-file" style="text-align:center"><b>${esc(f.name)}</b>
    <span>${f.duration!=null?f.duration.toFixed(1)+'s · '+f.sr+' Hz':'解析中…'}</span>
    ${f.ok!=null?`<span class="${f.ok?'ok':'bad'}" style="display:block;margin-top:3px">${f.ok?'✓ 可用':'✗ '+(f.reason||'不可用')}</span>`:''}</div>
    ${vlib.peaks.length?`<div class="vlib-wave">${vlib.peaks.map(h=>`<i style="--h:${Math.max(3,Math.round(h/32768*26))}px"></i>`).join('')}</div>`:''}`:'';
  const emptyHtml=`<div class="dz-icon">${icon('up','ic lg')}</div><div class="dz-title">点击或拖拽音频到此处</div>
   <div class="dz-hint">wav / mp3 / flac / m4a / ogg / opus · 建议 5~15s 干净单人语音<br>1~30 秒 · 单人声 · 上传后自动校验时长并生成波形</div>`;
  el.innerHTML=`<div class="vlib-card">
   <div class="vlib-cap">${icon('mic','ic sm')}<h3>上传新音色（3~30 秒参考音频）</h3><span class="spacer"></span><span class="tag">1~30 秒 · 单人声</span></div>
   <div class="vlib-grid">
    <div>
     <div class="vlib-drop ${vlib.drag?'drag':''} ${f&&f.ok===false?'invalid':''}" data-act="voice-pick" role="button" tabindex="0" aria-label="选择参考音频">
      <input type="file" id="voice-file-input" accept=".wav,.mp3,.flac,.m4a,.ogg,.opus" hidden>
      ${f?fileHtml:emptyHtml}
     </div>
     ${vlib.error?`<div class="vlib-err" style="margin-top:8px">${icon('info','ic sm')} ${esc(vlib.error)}</div>`:''}
    </div>
    <div class="vlib-form">
     <div class="vfield"><label for="voice-new-name">音色名称 <span class="req">*</span></label>
      <input class="input" id="voice-new-name" maxlength="50" value="${esc(vlib.name)}" placeholder="例如：小雅（温柔女声）"></div>
     <div class="vfield"><label for="voice-new-text">参考音频文字内容 <span class="req">*</span></label>
      <textarea class="textarea" id="voice-new-text" maxlength="500" placeholder="逐字填写音频里说的内容，克隆质量的关键">${esc(vlib.prompt)}</textarea></div>
     <div class="vlib-save">
      <button class="btn btn-primary" id="voice-save-btn" data-act="voice-save" ${vlibCanSave()?'':'disabled'}>${vlib.busy?'保存中…':'保存音色'}</button>
      <span class="why" id="voice-save-why">${esc(vlibWhy())}</span></div>
     <p class="hint">保存后会自动选中该音色、回填参考文本，并回到创作台。</p>
    </div>
   </div></div>`;
}
function renderVoiceCards(){
  const el=$('#voice-cards');if(!el)return;
  const cnt=$('#voice-count');if(cnt)cnt.textContent=voices.length+' 个音色';
  const note=$('#voice-list-note');
  if(note)note.textContent=`共 ${voices.length} 个 · ${voices.filter(v=>!v.prompt_text).length} 个缺参考文本`;
  if(!voices.length){el.innerHTML='<div class="vlib-empty">还没有克隆音色。上传一段 3~30 秒的参考音频开始克隆。</div>';return}
  el.innerHTML=voices.map((v,i)=>{
    if(vlib.editId===v.id)return`<div class="vcard"><div class="vc-edit">
     <label for="voice-edit-name">音色名称</label><input class="input" id="voice-edit-name" maxlength="50" value="${esc(vlib.editName)}">
     <label for="voice-edit-text">参考音频文字内容</label><textarea class="textarea" id="voice-edit-text" maxlength="500">${esc(vlib.editPrompt)}</textarea>
     <div class="vc-actions"><button class="btn btn-primary" data-act="voice-edit-save" data-key="${v.id}">保存</button>
      <button class="btn sec" data-act="voice-edit-cancel">取消</button></div></div></div>`;
    return`<div class="vcard"><div class="vc-top"><span class="avatar a${i%5+1}">${esc((v.name||'?').slice(0,1))}</span>
     <span><span class="vc-name">${esc(v.name)}</span><span class="vc-meta">${esc(v.duration?v.duration.toFixed(1)+'s · '+(v.sample_rate||'')+' Hz':'克隆音色')}</span></span>
     <span class="tag vc-badge">${Number(v.duration||0).toFixed(1)}s</span></div>
     <p class="vc-prompt">${v.prompt_text?esc(v.prompt_text):'<span style="color:var(--warn)">未填写参考文本，克隆质量会受影响</span>'}</p>
     <div class="mini-wave">${waveBars(24).map(h=>`<i style="--h:${Math.max(4,h*.7)}px"></i>`).join('')}</div>
     <div class="vc-actions">
      <button class="btn sec" data-act="voice-preview" data-key="${v.id}">${icon('play','ic sm')} 试听参考</button>
      <button class="btn sec" data-act="voice-use" data-key="${v.id}">用于合成</button>
      <button class="btn sec" data-act="voice-edit" data-key="${v.id}">${icon('set','ic sm')} 编辑</button>
      ${vlib.confirmDel===v.id?`<button class="btn sec danger" data-act="voice-del" data-key="${v.id}">确认删除</button>`
        :`<button class="btn sec danger" data-act="voice-del" data-key="${v.id}">删除</button>`}
     </div>
     <div class="vc-date">${esc(relTime(v.created_at))} · ${v.sample_rate||24000} Hz</div></div>`;
  }).join('');
}
function renderVoiceSide(){
  if(state.view!=='voices')return;
  $('#out-title').textContent='音色提交记录';
  setPill('',voices.length+' 条');
  $('#out-recent').hidden=true;
  $('#out-tip').textContent='点击任一条可试听参考音频；每次上传成功后都会在这里留下记录。';
  $('#out-body').innerHTML=`<div class="out-group"><div class="og-head"><span class="og-title">最近提交</span>
    <span class="og-count">${voices.length}</span><span class="spacer"></span>
    <button class="mini-btn" style="width:28px;height:28px" data-act="voice-refresh" title="刷新">${icon('refresh','ic sm')}</button></div>
   ${voices.length?voices.map(v=>`<div class="vside-row" data-act="voice-preview" data-key="${v.id}" title="点击试听参考音频">
    <span class="dot"></span><span class="vm"><b>${esc(v.name)}</b><span>参考 ${Number(v.duration||0).toFixed(1)}s · ${esc(relTime(v.created_at))}</span></span>
    <span style="color:var(--text-3)">›</span></div>`).join('')
    :'<div class="out-empty-sm">还没有音色提交记录。上传参考音频克隆后，这里会记录每次提交。</div>'}</div>`;
}
function renderVoiceLibrary(){renderVoiceUpload();renderVoiceCards()}
async function vlibSave(){
  if(!vlibCanSave())return;
  vlib.busy=true;vlibUpdateSave();
  try{
    const fd=new FormData();
    fd.append('name',vlib.name.trim());
    fd.append('prompt_text',vlib.prompt.trim());
    fd.append('file',vlib.file.raw);
    const v=await(await api('/api/voices',{method:'POST',body:fd})).json();
    voices.unshift(v);
    const savedName=v.name;
    vlib.file=null;vlib.name='';vlib.prompt='';vlib.busy=false;vlib.error='';vlib.peaks=[];
    if(caps().mine){state.group='mine';state.pick=v.id;state.refText=v.prompt_text||''}
    renderVoiceLibrary();renderVoiceSide();renderSounds();renderAction();renderTabs();
    switchView('workbench');
    toast(`音色「${savedName}」已保存并选中`);
  }catch(e){
    vlib.busy=false;vlib.error=e.message;renderVoiceUpload();vlibUpdateSave();
    toast('保存失败: '+e.message,true,5000);
  }
}

/* ================= 系统状态与设置 (05G / 05H) ================= */
function sysModes(k){const m=(health.models||[]).find(x=>x.key===k);
  return((m&&m.modes)||[]).map(x=>SYS_MODE_NAMES[x]||x).join(' / ')}
function sysDirFor(m){return (sysConfig&&sysConfig.dirs&&sysConfig.dirs[m.key])||m.dir||''}
function renderSystem(){
  if(state.view!=='system')return;
  const g=health.gpu||{};
  const pct=g.vram_total_gb?Math.round((g.vram_used_gb||0)/g.vram_total_gb*100):0;
  const models=health.models||[];
  const ready=models.filter(m=>m.available).length;
  $('#sys-updated').textContent=sys.updated;
  const custom=sysConfig?models.filter(m=>sysConfig.dirs[m.key]&&sysDefaults.dirs[m.key]&&sysConfig.dirs[m.key]!==sysDefaults.dirs[m.key]).length:0;
  $('#sys-ov-note').textContent=`权重根目录 ${esc(sysConfig?sysConfig.root:'—')}${custom?' · 已自定义 '+custom+' 项':''}`;
  $('#sys-stats').innerHTML=`
   <div class="stat"><div class="k">服务状态</div><div class="v" style="color:var(--ok)">${health.state==='ready'?'就绪':health.state==='loading'?'加载中':health.state==='error'?'异常':'未加载'}</div>
    <div class="s-kv"><span>服务端口</span><b>${esc(window.location.port||(window.location.protocol==='https:'?'443':'80'))}</b></div>
    <div class="s-kv"><span>独立运行环境</span><b>${new Set(models.map(m=>m.runtime).filter(r=>r&&r!=='in-process')).size} 套</b></div></div>
   <div class="stat"><div class="k">GPU / 显存</div><div class="s-model">${esc(g.name||'—')}</div>
    <div class="s-mid">${g.vram_total_gb?(g.vram_used_gb||0).toFixed(2)+' / '+g.vram_total_gb.toFixed(0)+' GB':'—'} <small>（${pct}%）</small></div>
    <div class="bar-track"><div class="bar-fill" style="width:${pct}%"></div></div></div>
   <div class="stat"><div class="k">推理队列</div><div class="v">${health.queue_len} <small>/ ${health.queue_max}</small></div>
    <div class="s-kv"><span>最近一次合成耗时</span><b>${health.last_infer_ms?health.last_infer_ms+' ms':'—'}</b></div>
    <div class="s-kv"><span>已就绪模型</span><b>${ready} / ${models.length}</b></div></div>
   <div class="stat"><div class="k">音色与输出</div><div class="v">${health.voices_count!=null?health.voices_count:voices.length} <small>个克隆音色</small></div>
    <div class="s-kv"><span>输出采样率</span><b>${health.sample_rate||'—'} Hz</b></div></div>`;
  $('#model-cards').innerHTML=models.map(m=>{
    const loaded=health.model===m.key&&health.state!=='idle';
    const available=m.available!==false,open=!!sys.expanded[m.key];
    const tag=loaded?'<span class="tag pri">已加载</span>':available?'<span class="tag">已就绪</span>':'<span class="tag err">未下载</span>';
    const dr=sys.doctorResult[m.key],busy=!!sys.doctorBusy[m.key];
    return`<div class="mcard">
     <div class="mc-top"><span class="dot" style="box-shadow:none;background:${loaded?'var(--primary)':available?'var(--ok)':'var(--danger)'}"></span><span class="mc-name" title="${esc(m.label)}">${esc(m.label)}</span>${tag}</div>
     <div class="mc-note">${esc(capsNote(m))}</div>
     <div class="sys-kv"><span class="k">推理模式</span><span class="v" title="${esc(sysModes(m.key))}">${esc(sysModes(m.key))}</span></div>
     <div class="sys-kv"><span class="k">权重目录</span><span class="v link ${open?'open':''}" data-act="sys-path" data-key="${m.key}" role="button" tabindex="0" aria-label="展开或收起完整权重路径" title="点击${open?'收起':'展开完整路径'}：${esc(sysDirFor(m))}">${esc(sysDirFor(m)||'—')}</span><button class="ss-edit" data-act="sys-settings" data-key="${m.key}" title="在设置中修改此模型的权重目录" aria-label="修改权重目录">${icon('set','ic sm')}</button></div>
     <div class="sys-kv"><span class="k">运行环境</span><span class="v" title="${esc(m.runtime||'')}">${m.runtime==='in-process'||!m.runtime?'随服务主环境':esc(m.runtime)}</span></div>
     <div class="sys-kv"><span class="k">流式</span><span class="v">${m.stream_mode==='buffered'?'整段生成后分块下发':'原生逐块推理'}</span></div>
     ${!available?`<div class="sys-hint">${esc(m.install_hint||'权重未下载，可运行 scripts/download_models.py')}</div>`:''}
     ${sysDoctorHtml(dr)}
     <div class="mc-foot">
      <button class="btn ${loaded?'':'btn-primary'}" data-act="sys-load" data-key="${m.key}" ${loaded||!available||health.state==='loading'?'disabled':''}>${loaded?'当前已加载':health.state==='loading'?'加载中…':'加载此模型'}</button>
      <button class="btn btn-ghost" data-act="sys-doctor-one" data-key="${m.key}" ${busy?'disabled':''}>${busy?'检测中…':'环境自检'}</button>
     </div></div>`;
  }).join('');
  renderSysMini();
}
function sysDoctorHtml(dr){
  if(!dr)return'';
  const line=(lb,ok,txt)=>`<div class="r"><span class="lb">${lb}</span><span class="${ok?'ok':'bad'}">${esc(txt)}</span></div>`;
  return`<div class="sys-doct">
   ${line('权重',dr.weights,dr.weights?'✓ 就绪':'✗ 缺失 '+((dr.missing_files||[]).join(', ')||'关键文件'))}
   ${dr.dependencies?line('推理依赖',dr.dependencies.ok,dr.dependencies.ok?'✓ 可导入':'✗ '+(dr.dependencies.detail||'不可用')):''}
   ${dr.repo?line('index-tts 源码',dr.repo.ok,dr.repo.ok?'✓ 已找到':'✗ '+(dr.repo.dir||'未找到')):''}
   ${dr.error?line('错误',false,dr.error):''}
  </div>`;
}
function renderSysMini(){
  const el=$('#sys-mini');if(!el)return;
  const g=health.gpu||{};
  el.innerHTML=`<div class="full"><span class="dot" style="box-shadow:none;background:${health.state==='ready'?'var(--ok)':health.state==='error'?'var(--danger)':'var(--primary)'};display:inline-block;width:7px;height:7px;border-radius:50%;margin-right:6px"></span><b>服务${health.state==='ready'?'就绪':health.state==='loading'?'加载中':'未加载'}</b></div>
   <div class="full">${esc((g.name||'GPU').replace('NVIDIA GeForce ',''))}</div>
   <div>显存 <b>${g.vram_total_gb?(g.vram_used_gb||0).toFixed(1)+' / '+g.vram_total_gb.toFixed(0)+' GB':'—'}</b></div>
   <div>排队 <b>${health.queue_len} / ${health.queue_max}</b></div>
   <div>输出 <b>${health.sample_rate||'—'} Hz</b></div>
   <div>端口 <b>${esc(window.location.port||'—')}</b></div>`;
}
async function sysRunDoctor(k){
  if(sys.doctorBusy[k])return;
  sys.doctorBusy[k]=true;renderSystem();
  try{sys.doctorResult[k]=await(await api(`/api/models/${k}/doctor`)).json();
    toast(((health.models.find(m=>m.key===k)||{}).label||k)+'：自检完成');}
  catch(e){sys.doctorResult[k]={error:e.message};toast('自检失败: '+e.message,true);}
  finally{sys.doctorBusy[k]=false;renderSystem();}
}
function sysDoctorAll(){
  (health.models||[]).forEach(async m=>{
    if(!sys.doctorResult[m.key]&&!sys.doctorBusy[m.key])sysRunDoctor(m.key);
  });
  toast('已开始对 '+(health.models||[]).length+' 个模型逐一自检');
}
async function sysRefresh(){
  if(sys.refreshing)return;
  sys.refreshing=true;sys.updated='刷新中…';
  const ic=$('#sys-refresh-icon');if(ic)ic.classList.add('spin');
  renderSystem();
  await fetchHealth();
  sys.refreshing=false;sys.updated='刚刚刷新';
  if(ic)ic.classList.remove('spin');
  renderSystem();toast('系统状态已刷新');
}
async function sysLoad(k){
  const m=(health.models||[]).find(x=>x.key===k);
  if(!m||health.model===k||health.state==='loading'||m.available===false)return;
  try{
    await api('/api/admin/model',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({model:k})});
    setModel(k,false);   // 界面即随新模型收敛; SSE 汇报 loading→ready 后状态自然更新
    renderSystem();
    toast('开始加载 '+m.label+'，首次加载约需 20~60 秒…',false,5000);
  }catch(e){toast('切换失败: '+e.message,true);}
}
/* ---- 05H 权重目录设置 (浏览器本地演示, 见弹窗内落地说明) ---- */
function renderSysSettings(){
  const row=(label,env,field,key,val,custom)=>`<div class="ss-row" data-row="${key||field}">
   <div class="ss-head"><b title="${esc(label)}">${esc(label)}</b><span class="ss-env" title="${esc(env)}">${esc(env)}</span><span class="spacer"></span>${custom?'<span class="tag pri">自定义</span>':''}</div>
   <input class="input ss-path" data-cfg="${field}" ${key?`data-key="${key}"`:''} value="${esc(val||'')}" spellcheck="false" aria-label="${esc(label)}目录"></div>`;
  const models=health.models||[];
  const modelRows=models.map(m=>row(m.label,SYS_DIR_ENV[m.key]||'—','dir',m.key,
    sysConfig.dirs[m.key],sysDefaults.dirs[m.key]&&sysConfig.dirs[m.key]!==sysDefaults.dirs[m.key])).join('');
  $('#sys-settings-body').innerHTML=`
   <div class="ss-section"><h4>全局 <span>1 项</span></h4>
    ${row('权重根目录','COSYVOICE_MODELS_DIR','root','',sysConfig.root,sysDefaults.root&&sysConfig.root!==sysDefaults.root)}</div>
   <div class="ss-section models"><h4>模型权重目录 <span>${models.length} 个 · 对应各模型卡显示</span></h4>${modelRows}</div>
   <div class="ss-section envs"><h4>独立运行环境 <span>外部 worker 解释器</span></h4>
    ${row('index-tts 源码','INDEXTTS_REPO','repo','',sysConfig.repo,sysDefaults.repo&&sysConfig.repo!==sysDefaults.repo)}
    ${row('IndexTTS 解释器','INDEXTTS_PYTHON','pyIndextts','',sysConfig.pyIndextts,sysDefaults.pyIndextts&&sysConfig.pyIndextts!==sysDefaults.pyIndextts)}
    ${row('Qwen3-TTS 解释器','QWEN3TTS_PYTHON','pyQwen3','',sysConfig.pyQwen3,sysDefaults.pyQwen3&&sysConfig.pyQwen3!==sysDefaults.pyQwen3)}</div>`;
}
function collectSysConfig(){
  $$('#sys-settings-body [data-cfg]').forEach(el=>{
    const f=el.dataset.cfg,k=el.dataset.key,v=el.value.trim();
    if(f==='dir'&&k)sysConfig.dirs[k]=v;
    else if(f in sysConfig)sysConfig[f]=v;
  });
}
function openSysSettings(key){
  if(!sysConfig)sysConfig=loadSysConfig();
  renderSysSettings();
  $('#sys-settings-modal').hidden=false;sys.settings=true;
  if(key){
    $$('#sys-settings-body .ss-row').forEach(r=>r.classList.toggle('focus',r.dataset.row===key));
    const el=$('#sys-settings-body input[data-key="'+key+'"]');
    if(el){el.focus();if(el.scrollIntoView)el.scrollIntoView({block:'nearest'})}
  }
}
function closeSysSettings(){$('#sys-settings-modal').hidden=true;sys.settings=false}
function saveSysConfig(){
  collectSysConfig();
  try{localStorage.setItem('vs-sys-config',JSON.stringify(sysConfig))}catch(_){}
  closeSysSettings();renderSystem();
  toast('权重目录配置已保存到浏览器本地，重启服务并设置对应环境变量后生效');
}
function resetSysConfig(){
  /* 只重置表单，不动内存配置；点「取消」即可撤销，点「保存」才写入 */
  $$('#sys-settings-body [data-cfg]').forEach(el=>{
    const f=el.dataset.cfg,k=el.dataset.key;
    el.value=(f==='dir'&&k?sysDefaults.dirs[k]:sysDefaults[f])||'';
  });
  $$('#sys-settings-body .ss-row').forEach(r=>{const t=r.querySelector('.tag.pri');if(t)t.remove()});
  toast('表单已恢复默认值，点「保存配置」生效');
}

/* ================= 播客工坊 ================= */
function renderPodStepper(){
  const flags=[podSrcReady(),pod.speakers.length>=2&&pod.speakers.every(s=>!!s.voice_id),pod.lines.length>0,podSynthDone()];
  const names=['内容源','主持阵容','对话稿','成品与发布'];
  const firstTodo=flags.findIndex(f=>!f);
  $('#pod-stepper').innerHTML=names.map((n,i)=>{
    const cls=flags[i]?'done':(i===firstTodo?'active':'');
    return`<span class="pstep ${cls}"><span class="pn">${flags[i]?'✓':i+1}</span><span>${n}</span></span>`+(i<3?'<span class="pstep-line"></span>':'')
  }).join('')+`<span style="flex:1"></span><span style="font-size:11px;color:var(--text-3)">${pod.lines.length} 轮 · 预计 ${podEstimate()}</span>`;
}
function renderPodSource(){
  const tabs=[['text','粘贴文本'],['file','上传文件'],['url','网页链接']];
  const prev=pod.extracted?(()=>{
    const over=pod.extractChars>20000;
    return`<div class="extract-prev">
     <div class="extract-prev-head"><span>正文预览 · 可编辑 · <b id="pod-extract-chars"${over?' class="over"':''}>${pod.extractChars}</b> 字</span>
      <button class="chip" data-act="pod-extract-reset" ${pod.extractDirty?'':'disabled'} title="放弃编辑，恢复为抓取的原始内容">还原</button></div>
     <textarea class="extract-prev-edit" id="pod-src-extract" spellcheck="false" placeholder="可直接编辑：删除广告、口播提示等不合适的内容，编辑结果就是成稿内容。">${esc(pod.extractText)}</textarea></div>`;
  })():'';
  let body='';
  if(pod.src==='text'){
    body=`<textarea class="textarea" id="pod-src-text" style="font-size:12.5px" placeholder="粘贴文章、会议记录、学习笔记…&#10;文章 / 文件 / 链接决定节目内容；可直接朗读，也可按主持阵容改写成多轮多人对话。">${esc(pod.text)}</textarea>`;
  }else if(pod.src==='file'){
    body=`<div class="dropzone" data-act="pod-file"><input type="file" id="pod-file-input" accept=".txt,.md,.pdf,.docx" hidden><b>${pod.fileName?esc(pod.fileName):'点击或拖拽文件到这里'}</b>${pod.fileName?(pod.extractChars?`已提取 ${pod.extractChars} 字正文`:(pod.extracting?'解析中…':'点击开始解析')):'支持 txt / md / pdf / docx · ≤ 20 MB'}</div>
     ${prev}
     ${pod.sourceError?`<div class="notice warn" style="margin-top:10px">${icon('info','ic sm')}<span>${esc(pod.sourceError)}</span></div>`:''}`;
  }else{
    body=`<div style="display:flex;gap:8px"><input class="input" id="pod-src-url" value="${esc(pod.url)}" placeholder="https:// 文章页面地址"><button class="btn" data-act="pod-url-extract" ${pod.extracting?'disabled':''}>${pod.extracting?'抓取中…':'抓取'}</button></div>
     ${prev}
     ${pod.sourceError?`<div class="notice warn" style="margin-top:10px">${icon('info','ic sm')}<span>${esc(pod.sourceError)}</span></div>`:''}`;
  }
  const counter=pod.src==='text'?`${pod.text.length} / 20000`:(pod.extractChars?`已提取 ${pod.extractChars} 字`:'待导入');
  $('#pod-source').innerHTML=`
   <div class="pcol-head"><span class="step">1</span><h3>内容源</h3><span class="spacer"></span><span class="tag" id="pod-src-counter">${esc(counter)}</span></div>
   <p class="pcol-sub">文章、文件或链接，决定节目讲什么</p>
   <div class="src-scroll">
    <div class="src-tabs">${tabs.map(([k,n])=>`<button class="${pod.src===k?'active':''}" data-act="pod-src" data-key="${k}">${n}</button>`).join('')}</div>
    ${body}
   </div>
   <div class="src-actions">
    <button class="btn btn-primary" data-act="pod-direct" title="直接朗读（单音色 · 无需大模型）" ${podSrcReady()&&!pod.genning?'':'disabled'}>${icon('play','ic sm')} 直接朗读</button>
    <button class="btn sec" data-act="pod-gen" title="由大模型改写为多人对话稿" ${podSrcReady()&&!pod.genning?'':'disabled'}>${icon('spark','ic sm')} ${pod.genning?'改写中…':'大模型改写'}</button>
   </div>
   ${!podSrcReady()?`<div class="notice warn" style="margin-top:8px">${icon('info','ic sm')}<span>内容源至少需要 10 个字才能成稿。</span></div>`:''}
   <div class="llm-strip" title="仅「由大模型改写为多人对话稿」需要；直接朗读不调用大模型">${icon('set','ic sm')}<span class="lm"><b>大模型配置</b> · ${esc(llmCfg.model||'未配置')} · ${esc(llmCfg.base_url||'未设置服务地址')}</span><button class="chip" data-act="pod-llm">配置 / 测试</button></div>`;
}
function renderPodScript(){
  const rows=pod.lines.map((l,i)=>{
    const isHost=l.role==='host';
    const editing=pod.editLine===i;
    const opt=`<select data-line="${i}" aria-label="切换该行说话人">${pod.speakers.map((s,j)=>`<option value="${j}" ${s.name===l.name?'selected':''}>${esc(spkVoiceLabel(s))}</option>`).join('')}</select>`;
    return`<div class="dlg ${isHost?'':'guest'} ${pod.playLine===i?'line-playing':''}" data-line="${i}">
     <span class="avatar a${isHost?1:2}" style="width:30px;height:30px;font-size:11px">${esc((l.name||'?').slice(0,1))}</span>
     <div class="dlg-body"><div class="dlg-who"><span class="tag ${isHost?'pri':'acc'}">${isHost?'主播':'嘉宾'}</span>${opt}<span class="spacer"></span>
      <button class="mini-btn" style="width:26px;height:26px" data-act="pod-line-edit" data-i="${i}" title="编辑">${icon('copy','ic sm')}</button>
      <button class="mini-btn" style="width:26px;height:26px" data-act="pod-line-del" data-i="${i}" title="删除">${icon('close','ic sm')}</button></div>
      ${editing?`<div class="dlg-edit"><textarea id="pod-line-edit-text">${esc(pod.editText)}</textarea><div style="display:flex;gap:6px;margin-top:6px"><button class="btn" style="min-height:30px;padding:4px 11px;font-size:12px" data-act="pod-line-save" data-i="${i}">保存</button><button class="btn sec" style="min-height:30px;padding:4px 11px;font-size:12px" data-act="pod-line-cancel">取消</button></div></div>`
        :`<div class="bubble" data-act="pod-line-edit" data-i="${i}">${esc(l.text)||'（空行，点击编辑）'}</div>`}</div></div>`;
  }).join('');
  const genning=pod.genning?`<div class="out-empty" style="padding:22px 12px"><div class="wave-static">${waveBars(20).map(h=>`<i style="--h:${Math.max(6,h*.5)}px"></i>`).join('')}</div><p>大模型改写中…</p><span id="pod-gen-raw" style="white-space:pre-wrap;display:block;max-height:180px;overflow:hidden;text-align:left">${esc(pod.genRaw.slice(-260))}</span></div>`:'';
  $('#pod-script-col').innerHTML=`
   <div class="pcol-head"><span class="step">3</span><h3>对话稿</h3><span class="spacer"></span><span class="tag pri">${pod.lines.length} 轮 · ${podEstimate()}</span></div>
   <p class="pcol-sub">逐轮可编辑；点击气泡改文字，下拉可换说话人与音色</p>
   ${pod.genning?genning:(pod.lines.length?`<div class="dlg-list">${rows}</div>`:`<div class="out-empty" style="padding:26px 12px"><div class="wave-static">${waveBars(20).map(h=>`<i style="--h:${Math.max(6,h*.5)}px"></i>`).join('')}</div><p>还没有对话稿</p><span>完成左侧内容源后点「直接朗读」或「大模型改写」</span></div>`)}
   ${pod.genning?'':`<button class="btn sec" style="width:100%;margin-top:8px" data-act="pod-line-add">${icon('plus','ic sm')} 插入一轮对话</button>
   <button class="btn sec" style="width:100%;margin-top:8px" data-act="pod-save">${icon('down','ic sm')} 保存剧集</button>`}`;
  updatePodLineHighlight();   // 重渲染后恢复当前播放轮的高亮
}
/* 对话稿播放跟随: 高亮当前播放轮, 并在其离开可视区时滚动跟随(每次句号变化才调用) */
function updatePodLineHighlight(){
  const rows=document.querySelectorAll('#pod-script-col .dlg-list .dlg');
  rows.forEach((el,i)=>el.classList.toggle('line-playing',i===pod.playLine));
  if(pod.playLine>=0&&rows[pod.playLine])rows[pod.playLine].scrollIntoView({block:'nearest',behavior:'smooth'});
}
function renderPodCast(){
  const c=caps();
  const cards=pod.speakers.map((s,i)=>{
    const guestIdx=pod.speakers.slice(0,i).filter(x=>x.role!=='host').length;
    return`<div class="spk-card"><div class="sc-head"><span class="tag ${s.role==='host'?'pri':''}">${s.role==='host'?'主播':`嘉宾 ${guestIdx+1}`}</span><b>${esc(s.name)||'未命名'}</b>
     ${s.role!=='host'?`<button class="mini-btn" style="width:26px;height:26px" data-act="pod-cast-del" data-i="${i}" title="移除嘉宾">${icon('close','ic sm')}</button>`:''}</div>
     <div class="spk-row"><input data-spk="${i}" data-field="name" value="${esc(s.name)}" placeholder="姓名"><select data-spk="${i}" data-field="voice" aria-label="音色">${voiceOptions(s.voice_id)}</select></div>
     <textarea data-spk="${i}" data-field="desc" placeholder="人设 / 简介（生成对话稿时供大模型参考）">${esc(s.desc)}</textarea></div>`;
  }).join('');
  $('#pod-cast-col').innerHTML=`
   <div class="pcol-head"><span class="step">2</span><h3>主持阵容</h3><span class="spacer"></span><span class="chip-label">主播 1 · 嘉宾 ${pod.speakers.filter(s=>s.role!=='host').length}</span></div>
   <p class="pcol-sub">姓名、音色与人设会一起交给大模型改写对话稿</p>
   ${cards}
   <button class="btn sec" style="width:100%" data-act="pod-cast-add">${icon('plus','ic sm')} 添加嘉宾</button>
   <div class="adv-row" style="margin-top:12px;padding-top:12px;border-top:1px solid var(--line)"><div class="adv-name" style="width:56px">语速</div><div class="adv-ctl"><input type="range" id="pod-speed" min="0.5" max="2" step="0.05" value="${pod.speed}"><span class="range-val">${pod.speed.toFixed(2)}×</span></div></div>
   ${c.mine?`<div class="notice" style="margin-top:12px">${icon('info','ic sm')}<span>切到不支持声音克隆的模型时，未配置音色的角色会在这里标出并提示更换。</span></div>`
     :`<div class="notice warn" style="margin-top:12px">${icon('info','ic sm')}<span>当前模型 ${esc(model()?model().label:'')} 不支持声音克隆，请先在顶栏切换到支持的模型。</span></div>`}`;
}
function voiceOptions(sel){
  return voices.map(v=>`<option value="${v.id}" ${v.id===sel?'selected':''}>${esc(v.name)}</option>`).join('')
    ||'<option value="">（暂无音色）</option>';
}
function renderPodOutput(){
  const tabs=[['final','成品合成'],['stream','流式播放'],['push','直播推流']];
  let body='';
  if(pod.outTab==='final'){
    const done=podSynthDone();
    const busy=pod.busyFinal;
    const pct=busy?Math.round(100*(pod.progress||0)/Math.max(1,pod.lines.length)):0;
    body=`<div class="push-grid"><div>
     <p style="font-size:13px;margin:0 0 4px"><strong>整段合成，音质最稳</strong></p>
     <p style="font-size:12px;color:var(--text-3);margin:0">逐句合成后拼成一条音轨，完成后自动归档为剧集，可试听与下载。</p>
     <div style="display:flex;gap:8px;margin-top:12px;flex-wrap:wrap"><button class="btn sec" data-act="pod-synth" ${pod.lines.length&&!busy&&!pod.busyStream?'':'disabled'}>${busy?`合成中 ${pod.progress||0}/${pod.lines.length}`:'合成播客成品'}</button>
      <button class="btn sec" data-act="pod-synth-stop" ${busy&&!pod.stopping?'':'disabled'}>${pod.stopping?'停止中…':'■ 停止'}</button>
      <button class="btn sec" data-act="pod-play" ${done?'':'disabled'}>${icon('play','ic sm')}试听</button>
      <button class="btn sec" data-act="pod-download" ${done?'':'disabled'}>${icon('down','ic sm')}下载 WAV</button></div>
     ${!pod.lines.length?`<div class="notice warn" style="margin-top:10px">${icon('info','ic sm')}<span>请先生成或载入对话稿，才能合成成品。</span></div>`:''}
    </div><div>
     <div class="result-card"><p class="result-title">${pod.episode?esc(pod.episode.title):'当前剧集尚未合成'}</p>
      <div class="result-meta">${pod.lines.length} 轮 · ${done&&pod.episode&&pod.episode.duration?pod.episode.duration.toFixed(1)+' 秒 · ':''}24 kHz WAV</div>
      <canvas class="pb-wave-c" id="pod-wave" style="height:44px"></canvas>
      ${busy?`<div class="progress-track"><div class="progress-fill" style="width:${pct}%"></div></div><div class="times"><span>正在合成第 ${pod.progress||0} / ${pod.lines.length} 句</span><span>请稍候</span></div>`
        :`<div class="times"><span id="pod-t-cur">${podPlayingEp()?fmt(playerState.position):'0:00'}</span><span>${done&&pod.episode&&pod.episode.duration?fmt(pod.episode.duration):'—'}</span></div>`}
     </div></div></div>`;
  }else if(pod.outTab==='stream'){
    const done=playerState.active&&!playerState.streaming&&playerState.duration>0;
    body=`<div class="push-grid"><div>
     <p style="font-size:13px;margin:0 0 4px"><strong>逐句合成，边生成边播放</strong></p>
     <p style="font-size:12px;color:var(--text-3);margin:0">2.5 秒预缓冲后开始播放，适合先听效果再决定是否导出成品；完成后同样自动保存。</p>
     <div style="display:flex;gap:8px;margin-top:12px;align-items:center;flex-wrap:wrap"><button class="btn btn-primary" data-act="pod-stream" ${pod.lines.length&&!pod.busyStream&&!pod.busyFinal?'':'disabled'}>${pod.busyStream?'流式合成中…':'流式合成并播放'}</button>
     <button class="btn sec" data-act="pod-stream-stop" ${pod.busyStream&&!pod.stopping?'':'disabled'}>${pod.stopping?'停止中…':'■ 停止'}</button>
     <span class="tag">2.5s 预缓冲</span><span class="tag">非流式成品仍可下载</span></div>
     ${pod.busyStream?`<div class="notice" style="margin-top:10px">${icon('info','ic sm')}<span id="pod-stream-notice-text">${playerState.firstMs?`正在播放 · 首包 ${playerState.firstMs} ms`
        :caps().stream?`正在合成首句，${esc(model()?model().label:'当前模型')} 逐块下发，首包到达即开始播放`
        :`等待首包…（${esc(model()?model().label:'当前模型')} 为整段缓冲型，首句合成完才开始下发）`}</span></div>`
       :done?`<div class="notice" style="margin-top:10px">${icon('info','ic sm')}<span>播放完成，成品已自动归档；可切到「成品合成」下载 WAV。</span></div>`:''}
    </div><div>
     <div class="result-card"><p class="result-title">${pod.busyStream?'流式播放中':(done?'流式播放完成 · 已归档':'尚未开始流式播放')}</p>
      <div class="result-meta" id="pod-stream-meta">${pod.lines.length} 轮 · 24 kHz WAV${playerState.firstMs?` · 首包 ${playerState.firstMs} ms`:''}${pod.busyStream&&!playerState.firstMs?' · 等待首包…':''}</div>
      <canvas class="pb-wave-c" id="pod-stream-wave" style="height:44px"></canvas>
      <div class="times"><span id="pod-stream-t-cur">${fmt(playerState.position)}</span><span id="pod-stream-t-dur">${pod.busyStream&&!playerState.duration?'合成中…':(playerState.duration?fmt(playerState.duration):'—')}</span></div>
     </div></div></div>`;
  }else{
    const protos=['rtmp','srt','rtsp','hls'];
    const ph={rtmp:'rtmp://服务器:1935/app/推流码',srt:'srt://服务器:9000?streamid=xxx',rtsp:'rtsp://服务器:8554/流名'}[pod.protocol];
    body=`<div style="display:flex;align-items:center;gap:10px;margin-bottom:12px;flex-wrap:wrap"><button class="toggle ${pod.pushOn?'on':''}" data-act="pod-push-toggle" role="switch" aria-checked="${pod.pushOn}" aria-label="推流开关"></button>
     <span style="font-size:12px;color:var(--text-2)">${pod.pushOn?'推流到流媒体服务器':'不推流 · 本地播放（与创作台一致）'}</span><span style="flex:1"></span><span class="tag">${pod.ffmpeg?icon('info','ic sm')+' ffmpeg 已就绪':'ffmpeg 未安装 · 推流不可用'}</span></div>
     ${pod.pushOn?`<div class="push-grid"><div>
      <div class="proto-chips">${protos.map(p=>`<button class="chip ${pod.protocol===p?'active':''}" data-act="pod-proto" data-key="${p}">${p.toUpperCase()}</button>`).join('')}</div>
      ${pod.protocol!=='hls'?`<input class="input" id="pod-push-url" value="${esc(pod.pushUrl)}" placeholder="${esc(ph)}" style="font-family:ui-monospace,monospace;font-size:12px">
       <p class="chip-label" style="margin-top:8px;line-height:1.5">需要一个接收端（如本机 SRS 或直播平台推流地址）；只在本机试听推荐 HLS。</p>`:`<div class="notice">${icon('info','ic sm')}<span>拉流地址：<code>${esc(window.location.origin)}/live/&lt;剧集ID&gt;.m3u8</code><br>用 VLC 等支持 HLS 的播放器打开；浏览器地址栏直接打开只会显示文本。</span></div>`}
      <div style="display:flex;gap:8px;margin-top:12px;flex-wrap:wrap"><button class="btn btn-primary" data-act="pod-push-start" ${pod.lines.length&&!pod.pushing&&pod.ffmpeg?'':'disabled'}>开始合成并推流</button>
       ${pod.pushing?'<button class="btn sec" data-act="pod-push-stop">停止推流</button>':''}</div>
     </div><div>
      <div class="push-status">${pod.pushing?'<span class="live-dot"></span>':'<span class="dot" style="box-shadow:none"></span>'}<span>${esc(pod.pushStatus||'尚未开始推流')}</span></div>
      ${pod.pushing&&pushInfo()?`<div class="progress-track"><div class="progress-fill" style="width:${pushPct()}%"></div></div><div class="times"><span>${pushInfo().lines_done}/${pushInfo().lines_total} 句 · ${fmt(pushInfo().elapsed_s||0)} 已推</span><span>${pushInfo().duration_s?'剩余 '+fmt(Math.max(0,pushInfo().duration_s-(pushInfo().elapsed_s||0))):''}</span></div>`:''}
      <div class="kv" style="margin-top:12px"><span class="k" style="width:88px">协议</span><span class="v">${pod.protocol.toUpperCase()}</span></div>
      <div class="kv"><span class="k" style="width:88px">实时状态</span><span class="v">${pod.pushing?'推流中':(pod.pushStatus||'空闲')}</span></div>
     </div></div>`:`<div class="out-empty" style="padding:22px"><p>推流未开启</p><span>打开左上开关后可选择 RTMP / SRT / RTSP / HLS</span></div>`}`;
  }
  $('#pod-output').innerHTML=`
   <div class="pcol-head"><span class="step">4</span><h3>成品与发布</h3><span class="spacer"></span><span class="chip-label">合成进度与推流状态在这里统一查看，不用再滚动长侧栏</span></div>
   <div class="out-tabs">${tabs.map(([k,n])=>`<button class="${pod.outTab===k?'active':''}" data-act="pod-out-tab" data-key="${k}">${n}</button>`).join('')}</div>
   ${body}`;
  drawVisibleWave();   // 新画布立即出波形: 播放中走实时进度, 未播放时解码成品静态波形
}
function pushInfo(){
  const pushing=pod.episodes.find(e=>e.status==='pushing'&&e.push);
  return pushing?pushing.push:(pod.episode&&pod.episode.status==='pushing'&&pod.episode.push?pod.episode.push:null);
}
function pushPct(){const p=pushInfo();if(!p)return 0;
  return Math.min(100,Math.round(100*(p.elapsed_s||0)/Math.max(1,p.duration_s||1)))}
function podPlayingEp(){
  return player&&player.mode==='file'&&playerState.active&&
    /\/api\/podcast\/episodes\/([^/]+)\/audio/.test(player.audio.src||'')&&
    RegExp.$1===(pod.episode&&pod.episode.id);
}
function updatePodWaveLive(){
  const cur=$('#pod-t-cur');if(cur)cur.textContent=fmt(playerState.position);
  const sc=$('#pod-stream-t-cur');if(sc)sc.textContent=fmt(playerState.position);
  const sd=$('#pod-stream-t-dur');
  if(sd)sd.textContent=playerState.streaming&&!playerState.duration?'合成中…':(playerState.duration?fmt(playerState.duration):'—');
  const sm=$('#pod-stream-meta');
  if(sm)sm.innerHTML=`${pod.lines.length} 轮 · 24 kHz WAV${playerState.firstMs?` · 首包 ${playerState.firstMs} ms`:''}${playerState.streaming&&!playerState.firstMs?' · 等待首包…':''}`;
  const nt=$('#pod-stream-notice-text');
  if(nt&&playerState.firstMs)nt.textContent=`正在播放 · 首包 ${playerState.firstMs} ms`;
  /* 波形由 player.draw() 统一绘制(onPlayerTick 每帧调用) */
}

/* POD_BADGE: draft 存稿 / busy 合成中·推流中 / ok 已完成 / err 失败 (v1.6 单色系) */
const POD_BADGE={draft:['存稿',''],synthesizing:['合成中','busy'],pushing:['推流中','busy'],
  completed:['已完成','ok'],failed:['失败','err'],stopped:['已停止','']};
function podStateColor(s){
  if(s==='synthesizing')return'var(--primary)';
  if(s==='pushing')return'var(--accent)';
  if(s==='failed')return'var(--danger)';
  return'var(--text-3)';
}
function podTaskText(e){
  if(e.status==='synthesizing')return`${e.progress||0}/${e.lines_count||pod.lines.length} 句`;
  if(e.status==='pushing'&&e.push){
    const base=e.push.mode==='live'?`${e.push.lines_done||0}/${e.push.lines_total||0} 句`:'成品直推';
    return e.push.duration_s?`${base} · ${fmt(e.push.elapsed_s||0)}/${fmt(e.push.duration_s)}`:base;
  }
  if(e.status==='completed')return e.duration?`时长 ${e.duration.toFixed(1)}s`:'';
  if(e.status==='failed')return (e.error||'未知错误').slice(0,50);
  return'';
}
function podTaskBar(e){
  const pct=e.status==='synthesizing'?Math.min(100,Math.round(100*(e.progress||0)/Math.max(1,e.lines_count||1)))
    :e.status==='pushing'&&e.push?Math.min(100,Math.round(100*(e.push.elapsed_s||0)/Math.max(1,e.push.duration_s||1))):0;
  return pct?`<div class="qbar"><div class="qbar-fill" style="width:${pct}%"></div></div>`:'';
}
/* 右侧面板: 任务队列 + 播客列表 (两个独立分组, 05B 边界不得合并) */
function renderPodEpisodes(){
  if(state.view!=='podcast')return;
  const tasks=podEpisodeTasks();
  const running=tasks.filter(t=>QUEUE_ACTIVE.includes(t.status)).length;
  $('#out-title').textContent='任务与剧集';
  setPill(running?'busy':'',running?running+' 进行中':pod.episodes.length+' 剧集');
  $('#out-recent').hidden=true;
  $('#out-tip').textContent='任务队列只跟踪已提交合成 / 推流的任务，点击看详情；播客列表包含全部剧集（含存稿），点击载入编辑器。';
  const rowBadge=e=>{
    const b=POD_BADGE[e.status]||['未知',''];
    const rowCls=b[1]==='busy'?'st-live':b[1]==='err'?'st-err':'';
    const tagCls=b[1]==='busy'?'live':b[1]==='ok'?'ok':b[1]==='err'?'err':'';
    return{b,rowCls,tagCls};
  };
  const queueHtml=tasks.length?tasks.map(e=>{
    const{b,rowCls,tagCls}=rowBadge(e);
    return`<div class="task-row ${rowCls}" data-act="pod-task-detail" data-key="${e.id}" title="点击查看任务详情">
     <span class="dot" style="box-shadow:none;background:${podStateColor(e.status)};margin-top:5px"></span>
     <span class="tm"><b>${esc(e.title)}</b><span>${esc(podTaskText(e)||'—')} · ${esc(relTime(e.created_at))}</span>${podTaskBar(e)}</span>
     <span class="tag ${tagCls}">${b[0]}</span>
     <span style="color:var(--text-3);align-self:center">›</span></div>`;
  }).join(''):'<div class="out-empty-sm">暂无任务。开始合成后，这里会实时更新进度。</div>';
  const listHtml=pod.episodes.length?pod.episodes.map(e=>{
    const{b,rowCls,tagCls}=rowBadge(e);
    return`<div class="ep-row ${rowCls}" data-act="pod-ep-load" data-key="${e.id}" title="点击载入到播客编辑器">
     <span class="dot" style="box-shadow:none;background:${podStateColor(e.status)}"></span>
     <span class="em"><b>${esc(e.title)}</b><span>${e.lines_count||'—'} 轮 · ${e.duration?e.duration.toFixed(1)+'s':'—'} · ${esc(relTime(e.created_at))}</span></span>
     <span class="tag ${tagCls}">${b[0]}</span>
     ${e.duration?`<button class="mini-btn" style="width:28px;height:28px" data-act="pod-ep-download" data-key="${e.id}" title="下载成品">${icon('down','ic sm')}</button>`:''}
     ${pod.confirmDel===e.id?`<button class="chip" style="color:var(--danger);border-color:var(--danger)" data-act="pod-ep-del" data-key="${e.id}">确认删除</button>`:`<button class="mini-btn" style="width:28px;height:28px" data-act="pod-ep-del" data-key="${e.id}" title="删除">${icon('close','ic sm')}</button>`}</div>`;
  }).join(''):'<div class="out-empty-sm">暂无播客剧集。生成对话稿并保存后，会出现在这里。</div>';
  $('#out-body').innerHTML=`
   <div class="out-group">
    <div class="og-head"><span class="og-title">任务队列</span>${running?`<span class="og-live">${running} 进行中</span>`:''}<span class="spacer"></span>
     <button class="mini-btn" style="width:28px;height:28px" data-act="pod-queue-refresh" title="刷新队列">${icon('refresh','ic sm')}</button></div>
    ${queueHtml}
   </div>
   <div class="out-group">
    <div class="og-head"><span class="og-title">播客列表</span><span class="og-count">${pod.episodes.length}</span><span class="spacer"></span>
     <button class="mini-btn" style="width:28px;height:28px" data-act="pod-list-refresh" title="刷新列表">${icon('refresh','ic sm')}</button></div>
    ${listHtml}
   </div>`;
}
/* 任务详情面板: 进度/协议/地址/耗时/错误/来源/轮数/语速/阵容/时间 + 5 个操作 */
function renderPodDetail(){
  const e=pod.detail;
  if(!e)return;
  const b=POD_BADGE[e.status]||['未知',''];
  let mid='';
  if(e.status==='synthesizing'){
    mid+=`<div class="qbar" style="height:6px"><div class="qbar-fill" style="width:${Math.round(100*(e.progress||0)/Math.max(1,e.lines_count||1))}%"></div></div>
     <p style="font-size:12px;color:var(--text-3);margin:7px 0 0">正在合成 ${e.progress||0} / ${e.lines_count||'?'} 句…（本页实时刷新，切走也不会中断）</p>`;
  }
  if(e.status==='pushing'&&e.push){
    mid+=`<div class="detail-grid">
     <span class="dk">推流协议</span><span class="dv">${(e.push.protocol||'').toUpperCase()} ${e.push.mode==='live'?'（实时合成推流）':'（成品直推）'}</span>
     <span class="dk">推流地址</span><span class="dv"><code>${esc(e.push.url||'—')}</code></span>
     <span class="dk">已推送</span><span class="dv">${fmt(e.push.elapsed_s||0)}${e.push.duration_s?' / '+fmt(e.push.duration_s):''} · ${e.push.lines_done||0}/${e.push.lines_total||0} 句</span>
     </div><div class="qbar" style="height:6px"><div class="qbar-fill" style="width:${e.push.duration_s?Math.round(100*(e.push.elapsed_s||0)/e.push.duration_s):0}%"></div></div>
     <button class="btn sec" style="margin-top:10px;color:var(--danger)" data-act="pod-detail-stop" data-key="${e.id}">停止推流</button>`;
  }
  if(e.status==='failed')mid+=`<div class="notice warn">${icon('info','ic sm')}<span>${esc(e.error||'合成失败')}</span></div>`;
  if(e.status==='completed'){
    mid+=`<div class="detail-grid"><span class="dk">成品时长</span><span class="dv">${e.duration?e.duration.toFixed(1)+'s':'—'}</span></div>
     <div style="display:flex;gap:8px;margin-bottom:6px"><button class="btn sec" data-act="pod-detail-play" data-key="${e.id}">${icon('play','ic sm')}试听</button><button class="btn sec" data-act="pod-detail-download" data-key="${e.id}">${icon('down','ic sm')}下载成品</button></div>`;
  }
  const srcNames={text:'粘贴文本',file:'上传文件',url:'网页 URL'};
  $('#pod-detail-body').innerHTML=`
   <div style="display:flex;align-items:center;gap:9px;margin-bottom:12px">
    <span class="tag ${b[1]==='busy'?'live':b[1]==='ok'?'ok':b[1]==='err'?'err':''}"><i class="tag-dot" style="background:${podStateColor(e.status)}"></i>${b[0]}</span>
    <b id="pod-detail-title" style="font-size:15px;flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(e.title)}</b>
    <button class="mini-btn" data-act="pod-detail-close" aria-label="关闭">${icon('close','ic sm')}</button></div>
   ${mid}
   <div class="detail-grid">
    <span class="dk">来源</span><span class="dv">${esc(srcNames[e.source_type]||e.source_type||'粘贴文本')}</span>
    <span class="dk">对话稿</span><span class="dv">${e.lines_count||(e.script?e.script.length:'—')} 轮 · 语速 ×${Number(e.speed||1).toFixed(2)}</span>
    <span class="dk">主持阵容</span><span class="dv">${esc(speakersSummary(e))}</span>
    <span class="dk">创建时间</span><span class="dv">${esc(relTime(e.created_at))}</span>
   </div>
   <div style="display:flex;gap:8px;margin-top:14px;flex-wrap:wrap">
    <button class="btn" data-act="pod-detail-load" data-key="${e.id}">载入编辑器</button>
    <button class="btn sec" style="color:var(--danger)" data-act="pod-detail-del" data-key="${e.id}">删除剧集</button></div>`;
}
function renderPod(){
  renderPodStepper();renderPodSource();renderPodScript();renderPodCast();renderPodOutput();
  const flags=[podSrcReady(),pod.speakers.length>=2&&pod.speakers.every(s=>!!s.voice_id),pod.lines.length>0,podSynthDone()];
  const cur=flags.findIndex(f=>!f);
  ['#pod-source','#pod-cast-col','#pod-script-col','#pod-output'].forEach((sel,i)=>{
    const el=$(sel);if(el)el.classList.toggle('stage-active',i===cur);
  });
}
function refreshPod(){renderPod();if(state.view==='podcast')renderPodEpisodes()}

/* ---- 播客: 内容源 ---- */
async function extractPodUrl(){
  if(!pod.url.trim()){toast('先填写网页链接',true);return}
  pod.extracting=true;pod.sourceError='';pod.extractText='';pod.extracted=false;renderPodSource();renderPodStepper();
  try{
    const j=await(await api('/api/podcast/source/extract',{
      method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url:pod.url})})).json();
    pod.extractText=j.text;pod.extractChars=j.chars;
    pod.extracted=true;pod.extractText0=j.text;pod.extractDirty=false;
    toast(`网页正文抓取完成，已提取 ${j.chars} 字`);
  }catch(e){pod.sourceError=e.message;}
  finally{pod.extracting=false;renderPodSource();renderPodStepper();}
}
async function extractPodFile(){
  pod.extracting=true;pod.sourceError='';pod.extracted=false;renderPodSource();
  try{
    const fd=new FormData();fd.append('file',pod.fileRaw);
    const j=await(await api('/api/podcast/source/extract',{method:'POST',body:fd})).json();
    pod.extractText=j.text;pod.extractChars=j.chars;
    pod.extracted=true;pod.extractText0=j.text;pod.extractDirty=false;
    toast(`已解析文件，提取 ${j.chars} 字正文`);
  }catch(e){pod.sourceError=e.message;pod.extractChars=0;}
  finally{pod.extracting=false;renderPodSource();renderPodStepper();}
}
/* ---- 播客: 成稿 (直接朗读 / 大模型改写) ---- */
function splitToLines(text,maxLen=120,minLen=15){
  const sentences=(text||'').replace(/\s+/g,' ').match(/[^。！？!?\n]+[。！？!?\n]?/g)||[];
  const lines=[];let buf='';
  for(let s of sentences.map(x=>x.trim()).filter(Boolean)){
    while(s.length>maxLen){
      let cut=s.lastIndexOf('，',maxLen);
      if(cut<minLen)cut=s.lastIndexOf('、',maxLen);
      if(cut<minLen)cut=maxLen;
      lines.push(s.slice(0,cut+1));s=s.slice(cut+1);
    }
    buf+=s;
    if(buf.length>=minLen){lines.push(buf);buf=''}
  }
  if(buf)lines.push(buf);
  return lines;
}
function directRead(){
  ensureSpeakerNames();
  const host=pod.speakers.find(s=>s.role==='host')||pod.speakers[0];
  const source=pod.src==='text'?pod.text:pod.extractText;
  const lines=splitToLines(source);
  if(!lines.length)return toast('内容源为空',true);
  pod.lines=lines.map(t=>({role:'host',name:host.name,text:t}));
  pod.editLine=-1;renderPod();toast(`已切分为 ${lines.length} 句朗读稿（${host.name} 单音色）`);
}
async function genScript(){
  const source=pod.src==='text'?pod.text:pod.extractText;
  if(source.trim().length<10)return toast('内容源太短',true);
  if(pod.speakers.length<2)return toast('至少需要 1 位主播 + 1 位嘉宾才能生成多人对话稿',true);
  ensureSpeakerNames();
  const speakersBrief=pod.speakers.map(s=>({role:s.role,name:(s.name||'').trim(),desc:(s.desc||'').trim()}));
  pod.genning=true;pod.genRaw='';pod.lines=[];renderPod();
  try{
    const resp=await fetch('/api/podcast/script',{
      method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({text:source,speakers:speakersBrief})});
    if(!resp.ok){
      let msg=`HTTP ${resp.status}`;
      try{msg=(await resp.json()).detail||msg}catch(e){}
      throw new Error(msg);
    }
    const reader=resp.body.getReader();const dec=new TextDecoder();
    let raw='';
    while(true){
      const{done,value}=await reader.read();
      if(done)break;
      raw+=dec.decode(value,{stream:true});
      pod.genRaw=raw;
      const el=$('#pod-gen-raw');if(el)el.textContent=raw.slice(-260);
    }
    if(raw.includes('__LLM_ERROR__'))throw new Error(raw.split('__LLM_ERROR__')[1].trim());
    const j=await(await api('/api/podcast/parse-script',{
      method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({raw,speakers:speakersBrief})})).json();
    // 先复位 genning 再渲染, 否则面板会停在「大模型改写中…」直到下次刷新
    pod.lines=j.lines;pod.genRaw='';pod.genning=false;
    renderPod();toast(`对话稿已按主持阵容改写完成: ${j.lines.length} 轮`);
  }catch(e){
    pod.genRaw='';pod.lines=[];pod.genning=false;renderPod();
    toast('对话稿生成失败: '+e.message,true,6000);
  }finally{pod.genning=false;}
}
/* ---- 播客: 保存 / 载入 ---- */
function autoTitle(){
  const src=pod.src==='url'?pod.url:(pod.src==='file'?pod.fileName:pod.text);
  return `播客 ${new Date().toLocaleDateString('zh-CN')} · ${(src||'').replace(/^https?:\/\//,'').slice(0,24)||'未命名'}`;
}
async function saveEpisode(silent){
  ensureSpeakerNames();
  const host=pod.speakers.find(s=>s.role==='host')||pod.speakers[0];
  const keepTitle=pod.episode&&pod.episode.title?pod.episode.title:autoTitle();
  const body={
    title:keepTitle,source_type:pod.src,source_ref:pod.src==='url'?pod.url:pod.fileName,
    lines:pod.lines.map(l=>({role:l.role,name:l.name||'',text:l.text})),
    speakers:pod.speakers.map(s=>({role:s.role,name:(s.name||'').trim(),desc:(s.desc||'').trim(),voice_id:s.voice_id})),
    voice_host:host.voice_id||'',
    voice_guest:(pod.speakers.find(s=>s.role!=='host')||{}).voice_id||'',
    speed:pod.speed,
  };
  if(pod.episode&&pod.episode.id){
    await api(`/api/podcast/episodes/${pod.episode.id}`,{
      method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  }else{
    pod.episode=await(await api('/api/podcast/episodes',{
      method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})).json();
  }
  await loadEpisodes();
  if(!silent)toast('剧集已保存，可在右侧「播客列表」载入');
}
async function loadEpisodes(){
  try{
    const j=await(await api('/api/podcast/episodes')).json();
    pod.episodes=j.episodes;
  }catch(e){}
  syncPodTasks();
}
async function loadEpisode(id){
  try{
    pod.episode=await(await api(`/api/podcast/episodes/${id}`)).json();
  }catch(e){toast('载入失败: '+e.message,true);return}
  const raw=Array.isArray(pod.episode.speakers)&&pod.episode.speakers.length
    ?pod.episode.speakers
    :[{role:'host',name:'',desc:'',voice_id:pod.episode.voice_host||''},
      {role:'guest',name:'',desc:'',voice_id:pod.episode.voice_guest||''}];
  pod.speakers=raw.map(s=>({role:s.role==='host'?'host':'guest',name:s.name||'',desc:s.desc||'',voice_id:s.voice_id||''}));
  ensureSpeakerNames();
  pod.lines=JSON.parse(JSON.stringify(pod.episode.script||[])).map(l=>{
    const spk=pod.speakers.find(s=>s.role===(l.role||'host'))||pod.speakers[0];
    return{role:spk.role,name:l.name||spk.name,text:l.text};
  });
  pod.speed=pod.episode.speed||1;
  pod.editLine=-1;
  renderPod();
  toast(`已载入「${pod.episode.title}」`);
}
/* ---- 播客: 合成 (成品 / 流式) ---- */
async function synthPodFinal(){
  if(!pod.lines.length||pod.busyFinal||pod.busyStream)return;
  pod.busyFinal=true;pod.busySince=Date.now();pod.progress=0;pod.stopping=false;
  try{
    await saveEpisode(true);
    await api(`/api/podcast/episodes/${pod.episode.id}/synthesize`,{
      method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({mode:'final'})});
    await pollPod(true);
    renderPod();renderPodEpisodes();
  }catch(e){
    pod.busyFinal=false;renderPod();
    toast('合成启动失败: '+e.message,true,6000);
  }
}
/* 播客流式响应: 服务端在每句 PCM 前插入 8 字节行边界标记(\x00LNSYNC\x00)。
   这里边读边剥出干净 PCM 流交给播放器, 标记位置换算成采样偏移追加进 marks ——
   播放中的对话稿高亮据此与可听音频逐句对齐 */
const POD_LINE_MARK=[0x00,0x4C,0x4E,0x53,0x59,0x4E,0x43,0x00];
async function podMarkedStream(resp,marks){
  const src=resp.body.getReader();
  const out=new ReadableStream({
    async start(ctrl){
      let buf=new Uint8Array(0),clean=0;   // clean: 已输出的干净 PCM 字节数
      const n=POD_LINE_MARK.length;
      const push=u8=>{if(u8.length){ctrl.enqueue(u8);clean+=u8.length}};
      while(true){
        const{done,value}=await src.read();
        if(done)break;
        const merged=new Uint8Array(buf.length+value.length);
        merged.set(buf);merged.set(value,buf.length);buf=merged;
        let from=0,i=0;
        while(i<=buf.length-n){
          let hit=true;
          for(let k=0;k<n;k++)if(buf[i+k]!==POD_LINE_MARK[k]){hit=false;break}
          if(hit){
            push(buf.slice(from,i));
            marks.push({s:Math.floor(clean/2),idx:marks.length});   // 标记处=下一句 PCM 的采样起点
            from=i+=n;
          }else i++;
        }
        const keep=Math.min(n-1,buf.length-from);   // 留出可能被截断的标记尾巴
        push(buf.slice(from,buf.length-keep));
        buf=buf.slice(buf.length-keep);
      }
      push(buf);
      ctrl.close();
    }
  });
  return{body:out};
}
/* 成品逐句偏移(lines_meta) → 高亮标记; 句数与当前对话稿不一致(合成后又改过稿)则放弃对齐。
   服务端存的是每句结束点 ends, 这里转成每句起点 [0, e0, e1, ...] —— 与流内标记同一语义。
   返回 {marks, sr}; 无可用标记返回 null */
function podLineMarks(ep){
  if(!ep||!ep.lines_meta)return null;
  let meta;try{meta=typeof ep.lines_meta==='string'?JSON.parse(ep.lines_meta):ep.lines_meta}catch(e){return null}
  if(!meta||!Array.isArray(meta.ends)||!meta.ends.length)return null;
  if(pod.lines.length&&meta.ends.length!==pod.lines.length)return null;
  return{marks:[0,...meta.ends.slice(0,-1)].map((s,i)=>({s,idx:i})),sr:meta.sr||24000};
}
async function synthPodStream(){
  if(!pod.lines.length||pod.busyFinal||pod.busyStream)return;
  pod.busyStream=true;pod.busySince=Date.now();pod.outTab='stream';pod.stopping=false;renderPod();
  try{
    await saveEpisode(true);
    const resp=await fetch(`/api/podcast/episodes/${pod.episode.id}/synthesize`,{
      method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({mode:'stream'})});
    if(!resp.ok){
      let msg=`HTTP ${resp.status}`;
      try{msg=(await resp.json()).detail||msg}catch(e){}
      throw new Error(msg);
    }
    const marks=[];
    const ok=await player.startStream(await podMarkedStream(resp,marks),'播客流式 · '+((pod.episode&&pod.episode.title)||''),null,
      {podEpId:pod.episode?pod.episode.id:null,lineMarks:marks});   // reset 后挂回: 句标记随流实时追加
    if(pod.stopping){player.reset()}   // 已停止: 丢弃部分音频
    if(pod.episode){
      pod.episode=await(await api(`/api/podcast/episodes/${pod.episode.id}`)).json();
      const meta=podLineMarks(pod.episode);
      if(meta&&player.mode)player.setLineMarks(meta.marks,meta.sr);   // 服务端精确偏移替换流内推算
      await loadEpisodes();
    }
    renderPod();renderPodEpisodes();
    if(ok)toast(pod.stopping?'已停止合成，已合成的部分已保存为成品':'流式播放完成，成品已自动保存');
  }catch(e){
    toast('流式合成失败: '+(e.message||''),true,6000);
    player.abortStream();
  }finally{pod.busyStream=false;renderPod();}
}
/* ---- 播客: 停止合成 (成品/流式通用) ----
   后端: 当前句在 PCM 分块边界停止推理, 后续句子不再合成, 已合成部分落盘为成品;
   流式不主动断开本地连接, 由服务端自然收尾, 保证剧集状态落库为 stopped */
async function stopPodSynth(){
  const eid=pod.episode&&pod.episode.id;
  if(!eid||pod.stopping)return;
  pod.stopping=true;renderPodOutput();
  toast('正在停止合成…');
  try{await api(`/api/podcast/episodes/${eid}/stop`,{method:'POST'})}catch(e){toast(e.message,true)}
}
/* ---- 播客: 推流 ---- */
async function startPush(){
  if(!pod.lines.length||pod.pushing)return;
  pod.pushing=true;renderPodOutput();
  try{
    await saveEpisode(true);
    await api(`/api/podcast/episodes/${pod.episode.id}/push`,{
      method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({protocol:pod.protocol,url:pod.pushUrl.trim()})});
    toast('推流已开始');
    await loadEpisodes();renderPodOutput();renderPodEpisodes();
  }catch(e){
    pod.pushing=false;renderPodOutput();
    toast('推流启动失败: '+e.message,true,6000);
  }
}
async function stopPush(id){
  const eid=id||(pod.episode&&pod.episode.id);
  if(!eid)return;
  pod.stopRequested=eid;
  toast('正在停止推流…');
  try{
    await api(`/api/podcast/episodes/${eid}/push`,{method:'DELETE'});
    toast('已停止推流');
  }catch(e){toast(e.message,true);}
  finally{pod.pushing=false;await loadEpisodes();renderPodOutput();renderPodEpisodes();if(pod.detailOpen)renderPodDetail();}
}
async function removeEpisode(id){
  try{
    await api(`/api/podcast/episodes/${id}`,{method:'DELETE'});
    if(pod.episode&&pod.episode.id===id)pod.episode=null;
    await loadEpisodes();
    renderPodEpisodes();
    toast('剧集已删除');
  }catch(e){toast(e.message,true);}
}
/* ---- 任务队列轮询: 2s 高频(有活动) / 10s 低频保底; 接管与终态通知 ---- */
async function pollPod(force){
  podPollTick++;
  const active=pod.episodes.some(e=>QUEUE_ACTIVE.includes(e.status));
  if(!force&&!active&&podPollTick%5!==0)return;
  await loadEpisodes();
}
function syncPodTasks(){
  const pushing=pod.episodes.find(e=>e.status==='pushing'&&e.push);
  if(pushing){
    pod.pushing=true;
    pod.pushStatus=`● 推流中 · ${(pushing.push.protocol||'').toUpperCase()}${pushing.push.mode==='live'?'':' · 成品直推'}`;
  }else if(pod.pushing){
    pod.pushing=false;
    if(pod.stopRequested){pod.pushStatus='已停止'}
  }
  if(pod.detailOpen&&pod.detail){
    const fresh=pod.episodes.find(e=>e.id===pod.detail.id);
    if(fresh){pod.detail=fresh;renderPodDetail()}
  }
  const mine=pod.episodes.find(e=>pod.episode&&e.id===pod.episode.id);
  if(mine&&(pod.busyFinal||pod.busyStream))pod.progress=mine.progress||0;
  for(const e of pod.episodes){
    if(QUEUE_ACTIVE.includes(e.status)&&!adopted.has(e.id)){
      adopted.set(e.id,e.status);
      const isSelf=pod.episode&&e.id===pod.episode.id&&(pod.busyFinal||pod.busyStream||pod.pushing);
      if(!isSelf)toast(`已接管进行中的任务「${e.title}」`);
      if(pod.episode&&e.id===pod.episode.id&&e.status==='synthesizing')pod.busyFinal=true;
      renderPod();
    }
    if(adopted.has(e.id)&&!QUEUE_ACTIVE.includes(e.status)){
      const kind=adopted.get(e.id);
      adopted.delete(e.id);
      if(kind==='pushing'&&pod.stopRequested===e.id){pod.stopRequested='';pod.pushStatus='已停止'}
      else if(e.status==='failed')toast(`「${e.title}」${kind==='pushing'?'推流失败':'合成失败'}: ${e.error||'未知错误'}`,true,6000);
      else if(e.status==='stopped')toast(`「${e.title}」${kind==='pushing'?'推流':'合成'}已停止`);
      else toast(`「${e.title}」${kind==='pushing'?'推流完成':'合成完成'}`);
      if(pod.episode&&e.id===pod.episode.id){
        pod.busyFinal=false;pod.busyStream=false;pod.pushing=false;pod.busySince=0;pod.stopping=false;
        refreshPodEpisode(e.id);
      }
      renderPod();
    }
  }
  if(mine&&mine.status!=='draft'&&!QUEUE_ACTIVE.includes(mine.status)
      &&(pod.busyFinal||pod.busyStream)&&Date.now()-(pod.busySince||0)>2500){
    pod.busyFinal=false;pod.busyStream=false;pod.busySince=0;pod.stopping=false;
    refreshPodEpisode(mine.id);
  }
  if(state.view==='podcast')renderPodEpisodes();
}
async function refreshPodEpisode(pid){
  try{pod.episode=await(await api(`/api/podcast/episodes/${pid}`)).json();renderPod()}catch(e){}
}

/* ================= 大模型配置 ================= */
async function refreshLLM(){
  try{Object.assign(llmCfg,await(await api('/api/llm/config')).json())}catch(e){}
}
function openLLMConfig(){
  refreshLLM().then(()=>{
    $('#llm-base').value=llmCfg.base_url||'';
    $('#llm-key').value='';
    $('#llm-key').placeholder=llmCfg.api_key_masked?`已保存 (${llmCfg.api_key_masked})，留空表示不修改`:'留空表示不修改';
    $('#llm-model').value=llmCfg.model||'';
    $('#llm-temp').value=llmCfg.temperature!=null?llmCfg.temperature:0.7;
    $('#llm-rounds').value=llmCfg.rounds_hint||'10~16';
    $('#llm-style').value=llmCfg.style_prompt||'';
    $('#llm-test-result').textContent='';
    $('#llm-modal').hidden=false;
  });
}
async function saveLLMConfig(){
  try{
    const body={base_url:$('#llm-base').value.trim(),model:$('#llm-model').value.trim(),
      temperature:parseFloat($('#llm-temp').value)||0.7,
      rounds_hint:$('#llm-rounds').value.trim(),style_prompt:$('#llm-style').value.trim()};
    const key=$('#llm-key').value.trim();
    if(key)body.api_key=key;
    Object.assign(llmCfg,await(await api('/api/llm/config',{
      method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})).json());
    $('#llm-modal').hidden=true;
    renderPodSource();
    toast('大模型配置已保存');
  }catch(e){toast('保存失败: '+e.message,true);}
}
async function testLLM(){
  $('#llm-test-result').textContent='测试中…';
  try{
    const r=await(await api('/api/llm/test',{method:'POST'})).json();
    $('#llm-test-result').textContent=`✓ ${r.latency_ms}ms · ${r.model}`;
  }catch(e){$('#llm-test-result').textContent='✗ '+e.message;}
}

/* ================= 创作台: 生成 ================= */
function deriveRequest(){
  const m=model();const c=caps();
  const payload={text:state.text,model:state.model,speed:state.speed,stream:state.stream};
  if(state.group==='design'){
    payload.mode='instruct';payload.instruct_text=state.designText.trim();
    return payload;
  }
  if(state.group==='preset'){
    const useInstruct=instructUsable()&&state.instruct.trim();
    payload.mode=useInstruct?'instruct':'sft';
    payload.spk_id=state.pick;
    if(useInstruct)payload.instruct_text=state.instruct.trim();
    return payload;
  }
  /* mine: 克隆音色 */
  let mode='zero_shot',text=state.text;
  if(state.lang&&c.crossLingual){mode='cross_lingual';text=`<|${state.lang}|>`+text}
  else if(state.lang){text=`<|${state.lang}|>`+text}   /* indextts25 等语种前缀 */
  if(state.instruct.trim()&&c.refInstruct){mode='instruct'}  /* cosyvoice: 克隆音色+风格指令 */
  payload.mode=mode;payload.text=text;
  payload.voice_id=state.pick;
  if(mode!=='cross_lingual')payload.prompt_text=state.refText.trim();
  else if(state.refText.trim())payload.prompt_text=state.refText.trim();
  if(mode==='instruct')payload.instruct_text=state.instruct.trim();
  return payload;
}
let genTimer=null;
function newClientTaskId(){return 'c'+Date.now().toString(36)+Math.random().toString(36).slice(2,10)}
function generate(){
  if(blocker()||state.busy)return;
  state.busy=true;state.genProg=12;
  state.genTaskId=newClientTaskId();state.genAbort=new AbortController();state.genStopping=false;
  player.reset();
  renderAction();renderOutput();
  genTimer=setInterval(()=>{state.genProg=Math.min(94,state.genProg+Math.random()*17);
    const el=$('#gen-progress');if(el)el.style.width=state.genProg+'%'},220);
  const payload=deriveRequest();
  const voiceLabel=payload.spk_id?((QWEN3_KNOWN[payload.spk_id]||[payload.spk_id])[0])
    :(payload.voice_id?voiceName(payload.voice_id)
    :(payload.mode==='instruct'?state.designText.trim().slice(0,12)+'…':'—'));
  api('/api/tts',{method:'POST',headers:{'Content-Type':'application/json','X-Client-Task-Id':state.genTaskId},
    body:JSON.stringify(payload),signal:state.genAbort.signal})
    .then(async resp=>{
      const taskId=resp.headers.get('X-Task-Id')||state.genTaskId;
      const title=displayTaskText(state.text).trim().replace(/\s+/g,' ').slice(0,22)+(state.text.trim().length>22?'…':'');
      if(state.stream){
        await player.startStream(resp,voiceLabel+' · 流式',taskId);
        if(state.genStopping){   // 已停止: 丢弃部分音频, 不作为作品
          clearInterval(genTimer);
          state.busy=false;state.genTaskId=null;state.genAbort=null;player.reset();
          renderAction();renderOutput();loadTasks();
          toast('已停止生成');
          return;
        }
        state.result={title,taskId,model:model().label,voice:voiceLabel,time:'刚刚'};
      }else{
        const blob=await resp.blob();
        const durMs=resp.headers.get('X-Duration-Ms');
        player.playFile(URL.createObjectURL(blob),voiceLabel,taskId);
        playerState.taskId=taskId;
        state.result={title,taskId,model:model().label,voice:voiceLabel,time:'刚刚',
          dur:durMs?durMs/1000:0};
        playerState.duration=durMs?durMs/1000:0;
      }
      clearInterval(genTimer);
      state.busy=false;state.genTaskId=null;state.genAbort=null;state.genStopping=false;
      renderAction();renderOutput();renderRecent();
      toast('生成完成，可以试听或下载');
      loadTasks();
    })
    .catch(e=>{
      clearInterval(genTimer);
      const stopped=state.genStopping||e.name==='AbortError';
      state.busy=false;state.genTaskId=null;state.genAbort=null;state.genStopping=false;player.reset();
      renderAction();renderOutput();loadTasks();
      toast(stopped?'已停止生成':'合成失败: '+e.message,stopped?false:true,6000);
    });
}
/* 停止生成: 预生成的任务 id 调停止接口, 服务端在 PCM 分块边界停止推理并把任务落库为「已停止」;
   不主动断开本地连接 —— 流由服务端自然收尾, 避免任务被误标为 client disconnected */
async function stopGen(){
  const tid=state.genTaskId;
  if(!tid||state.genStopping)return;
  state.genStopping=true;renderOutput();
  try{
    await api(`/api/tasks/${tid}/stop`,{method:'POST'});
  }catch(e){
    // 任务可能刚好完成: 恢复状态走正常完成流程
    state.genStopping=false;renderOutput();
  }
}
function previewPreset(spkId){
  if(state.previewBusy)return;
  if(state.model!==health.model){toast('该模型尚未加载，请到「系统状态与设置」加载后再试听',true);return}
  state.previewBusy=true;
  api('/api/tts',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({text:'你好，这是内置音色的试听效果。',model:state.model,mode:'sft',spk_id:spkId,stream:false,speed:1.0})})
    .then(async resp=>{
      player.playFile(URL.createObjectURL(await resp.blob()),'试听 · '+(QWEN3_KNOWN[spkId]||[spkId])[0],resp.headers.get('X-Task-Id'));
      loadTasks();
    })
    .catch(e=>toast('试听失败: '+e.message,true))
    .finally(()=>{state.previewBusy=false});
}
function reloadParams(t){
  const m=t.text.match(/^(?:<\|([a-z]{2})\|>\s*)/);
  state.model=t.model;state.modelTouched=true;
  state.text=displayTaskText(t.text);
  state.lang=(t.mode==='cross_lingual'&&m&&m[1])?m[1]:'';
  state.speed=t.speed||1.0;
  state.instruct=t.instruct_text||'';
  const c=capsOf(health.models.find(x=>x.key===t.model)||{modes:[],speaker_modes:[],reference_modes:[],instruct_kind:'cosyvoice'});
  if(t.mode==='sft'||(t.mode==='instruct'&&(health.models.find(x=>x.key===t.model)||{}).speaker_modes||[]).includes('instruct')){
    state.group='preset';state.pick=t.spk_id||null;
  }else if(t.model==='qwen3tts_design'){
    state.group='design';state.pick='design';state.designText=t.instruct_text||'';
  }else{
    state.group='mine';state.pick=t.voice_id||null;
    const v=voices.find(x=>x.id===t.voice_id);
    state.refText=v?(v.prompt_text||''):'';
  }
  $('#text-input').value=state.text;
  switchView('workbench');
  toast('已把本条参数回填到创作台');
}
function loadTasks(){
  return api('/api/tasks?limit=200').then(r=>r.json()).then(j=>{
    state.tasks=j.tasks;
    renderRecent();
    if(state.view==='library')renderLibrary();
  }).catch(()=>{});
}

/* ================= 健康检查 / SSE ================= */
let modelsInitialized=false;
async function fetchHealth(){
  try{
    const st=await(await api('/api/health')).json();
    applyHealth(st);
  }catch(e){/* 服务未起时静默 */}
}
function applyHealth(st){
  const prev=health.state;
  Object.assign(health,st);
  if(prev!=='ready'&&st.state==='ready')toast(`${st.model_label} 已就绪`);
  if(prev!=='error'&&st.state==='error')toast('模型加载失败: '+(st.error||'未知错误'),true,6000);
  if(st.models&&st.models.length){
    buildSysDefaults();
    if(!sysConfig)sysConfig=loadSysConfig();
    if(!state.modelTouched&&st.model)state.model=st.model;
    if(!st.models.find(m=>m.key===state.model)){
      const first=st.models.find(m=>m.available);
      if(first)state.model=first.key;
    }
    /* 声音选择收敛到当前模型支持的方式 (spec 05 ②) */
    const c=caps();
    if(!c[state.group]){
      const order=GROUPS.filter(([g])=>c[g]).map(([g])=>g);
      state.group=order[0]||'preset';state.pick=null;
      if(state.group==='mine'&&voices.length)state.pick=voices[0].id;
      if(state.group==='design')state.pick='design';
    }
    if(state.group==='preset'){
      const spks=presetSpeakers();
      if(spks.length&&!spks.some(s=>s.id===state.pick))state.pick=spks[0].id;
    }
    if(!c.stream)state.stream=false;
    if(!c.lang&&!c.crossLingual)state.lang='';
    if(!c.instruct)state.instruct='';
    renderTabs();renderSounds();renderAdv();
  }
  renderModelMenu();renderCapNotice();renderAction();renderSysMini();
  if(state.view==='system')renderSystem();
  if(state.view==='workbench'&&document.activeElement&&
     !$('#sound-grid').contains(document.activeElement)&&
     !(document.activeElement&&document.activeElement.id==='design-input')&&
     !(document.activeElement&&document.activeElement.id==='ref-input'))renderSounds();
}
function connectSSE(){
  const es=new EventSource('/api/events');
  es.onmessage=e=>{
    try{
      const d=JSON.parse(e.data);
      if(d.type==='engine'){
        applyHealth({...health,state:d.state,model:d.model,
          model_label:d.label??health.model_label,
          sample_rate:d.sample_rate??health.sample_rate,
          speakers:d.speakers??health.speakers,error:d.error});
      }else if(d.type==='queue'){
        health.queue_len=d.queue_len;renderSysMini();
        if(state.view==='system')renderSystem();
      }
    }catch(err){}
  };
  es.onerror=()=>{/* 断线后 EventSource 自动重连 */};
}

/* ================= 视图切换 ================= */
function switchView(k){
  if(sys.settings)closeSysSettings();
  state.view=k;
  $$('.view').forEach(v=>v.hidden=v.dataset.view!==k);
  $$('.nav a').forEach(a=>a.classList.toggle('active',a.dataset.key===k));
  $('#page-title').textContent=TITLES[k][0];$('#page-sub').textContent=TITLES[k][1];
  $('#output-panel').hidden=!(k==='workbench'||k==='podcast'||k==='voices');
  const oi=$('#out-icon');if(oi)oi.innerHTML=`<use href="#i-${k==='podcast'?'pod':k==='voices'?'mic':'wave'}"/>`;
  if(k==='podcast'){
    loadVoices();loadEpisodes();refreshLLM().then(()=>renderPodSource());
    api('/api/podcast/ffmpeg').then(r=>r.json()).then(j=>{pod.ffmpeg=!!j.available;renderPodOutput()}).catch(()=>{});
    renderPod();renderPodEpisodes();
  }else if(k==='voices'){
    loadVoices().then(()=>{renderVoiceLibrary();renderVoiceSide()});
  }else if(k==='workbench'){
    $('#out-title').textContent='本次结果';
    $('#out-recent').hidden=false;
    $('#out-tip').textContent='结果会自动保存到「作品库」。切换模型不会影响已生成的音频。';
    renderOutput();renderRecent();
  }else if(k==='library'){
    loadTasks().then(()=>renderLibrary());
    $('#out-body').innerHTML='';$('#out-title').textContent='作品库';setPill('','侧栏不展示');
  }else if(k==='system'){
    renderSystem();
  }
  $('#content').scrollTop=0;
}
function setModel(k,fromUser){
  const prev=model();
  state.model=k;state.modelTouched=true;
  const m=model();const c=capsOf(m);
  if(!c[state.group]){
    const order=GROUPS.filter(([g])=>c[g]).map(([g])=>g);
    const oldGroup=state.group;
    state.group=order[0]||'preset';state.pick=null;
    if(state.group==='mine'&&voices.length)state.pick=voices[0].id;
    if(state.group==='design')state.pick='design';
    if(fromUser)toast(`已切到「${(GROUPS.find(g=>g[0]===state.group)||[])[1]||'可用方式'}」：${prev?prev.label:'原模型'} 的「${(GROUPS.find(g=>g[0]===oldGroup)||[])[1]||oldGroup}」在 ${m?m.label:k} 中不可用`,true,5000);
  }
  if(state.group==='preset'){
    const spks=presetSpeakers();
    if(spks.length&&!spks.some(s=>s.id===state.pick))state.pick=spks[0].id;
  }
  if(state.group==='mine'&&!state.pick&&voices.length){state.pick=voices[0].id;state.refText=voices[0].prompt_text||''}
  if(state.group==='design'&&state.pick!=='design')state.pick='design';
  if(state.group==='mine'){const v=voices.find(x=>x.id===state.pick);state.refText=v?(v.prompt_text||''):''}
  if(!c.stream)state.stream=false;
  if(!c.lang&&!c.crossLingual)state.lang='';
  if(!c.instruct)state.instruct='';
  state.menu=false;
  renderModelMenu();renderCapNotice();renderTabs();renderSounds();renderAdv();renderAction();
  if(fromUser)toast(`已切换到 ${m?m.label:k}`);
}

/* 顶栏选择模型 = 真实加载: 只改前端状态会让界面与引擎不一致,
   之后所有合成都会以「model not ready」失败 */
function switchModel(k,fromUser){
  const m=(health.models||[]).find(x=>x.key===k);
  setModel(k,fromUser);
  if(!m||health.model===k)return;
  if(m.available===false){
    toast(`${m.label} 权重尚未就绪，请先下载权重或在系统页设置目录`,true,6000);return}
  if(health.state==='loading'){
    toast('另一个模型正在加载，请等它就绪后再切换',true,5000);return}
  api('/api/admin/model',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({model:k})})
    .then(()=>toast(`开始加载 ${m.label}，首次约需 20~60 秒…`,false,5000))
    .catch(e=>toast('切换失败: '+e.message,true,6000));
}
/* ================= 数据加载 ================= */
async function loadVoices(){
  try{
    const list=await(await api('/api/voices')).json();
    voices.splice(0,voices.length,...list);
    const firstGuest=pod.speakers.find(s=>s.role!=='host');
    const host=pod.speakers.find(s=>s.role==='host');
    if(host&&!host.voice_id&&voices.length)host.voice_id=voices[0].id;
    if(firstGuest&&!firstGuest.voice_id&&voices.length>1)firstGuest.voice_id=voices[1].id;
    // 首次渲染时 voices 尚为空, 已恢复的选中音色会缺参考文本, 载入后补齐
    if(state.group==='mine'&&state.pick&&!state.refText.trim()){
      const v=voices.find(x=>x.id===state.pick);
      if(v&&v.prompt_text)state.refText=v.prompt_text;
    }
  }catch(e){}
}

/* ================= 事件: 点击委托 ================= */
document.addEventListener('click',e=>{
  if(e.target.id==='llm-modal'){$('#llm-modal').hidden=true;return}
  if(e.target.id==='pod-detail-modal'){pod.detailOpen=false;$('#pod-detail-modal').hidden=true;return}
  if(e.target.id==='sys-settings-modal'){closeSysSettings();return}
  const t=e.target.closest('[data-act]');
  if(!t){if(state.menu){state.menu=false;renderModelMenu()}return}
  const a=t.dataset.act,k=t.dataset.key;
  switch(a){
   case 'view':switchView(k);break;
   case 'theme':{const d=document.documentElement.getAttribute('data-theme')==='dark'?'light':'dark';
    document.documentElement.setAttribute('data-theme',d);
    try{localStorage.setItem('vs-theme',d)}catch(_){}
    syncThemeIcon();drawVisibleWave();break}
   case 'help':toast('快捷键：Ctrl/⌘ + Enter 生成 · 空格播放/暂停 · Esc 关闭菜单',true);break;
   case 'toggle-menu':state.menu=!state.menu;renderModelMenu();break;
   case 'model':switchModel(k,true);break;
   case 'side-refresh':{
    if(state.view==='workbench'){loadTasks();toast('已刷新')}
    else if(state.view==='podcast'){pollPod(true);toast('任务队列已刷新')}
    else if(state.view==='voices'){loadVoices().then(()=>{renderVoiceLibrary();renderVoiceSide()});toast('音色列表已刷新')}
    break}
   case 'group':{state.group=k;state.pick=null;
    if(k==='preset'){const sp=presetSpeakers();state.pick=sp.length?sp[0].id:null}
    if(k==='mine'&&voices.length){state.pick=voices[0].id;state.refText=voices[0].prompt_text||''}
    if(k==='design')state.pick='design';
    renderTabs();renderSounds();renderAdv();renderAction();break}
   case 'pick':{if(e.target.closest('[data-act="preview"]'))break;
    state.pick=k;
    if(state.group==='mine'){const v=voices.find(x=>x.id===k);state.refText=v?(v.prompt_text||''):''}
    renderSounds();renderAction();break}
   case 'preview':{e.stopPropagation();
    if(state.group==='mine'){const v=voices.find(x=>x.id===k);
      if(v){player.playFile(`/api/voices/${v.id}/prompt`,'参考音频 · '+v.name,null);toast(`试听参考音频「${v.name}」`)}}
    else previewPreset(k);
    break}
   case 'sample':state.text=SAMPLES[+t.dataset.i].text;$('#text-input').value=state.text;
    $('#char-count').textContent=state.text.length+' / 2000';renderAction();break;
   case 'clear-text':state.text='';$('#text-input').value='';
    $('#char-count').textContent='0 / 2000';renderAction();break;
   case 'design-preset':state.designText=t.dataset.t;const di=$('#design-input');if(di)di.value=state.designText;renderAction();break;
   case 'go-voices':switchView('voices');break;
   case 'lang':state.lang=k;renderAdv();renderAction();break;
   case 'stream':if(caps().stream){state.stream=!state.stream;renderAdv();renderAction()}break;
   case 'generate':generate();break;
   case 'play':player.togglePlay();break;
   case 'download':{const r=state.result;
    if(!r||!r.taskId){toast('试听音频不支持下载',true);break}
    const a1=document.createElement('a');a1.href=`/api/tasks/${r.taskId}/audio`;a1.download=`${r.taskId}.wav`;a1.click();break}
   case 'download-task':{const a2=document.createElement('a');a2.href=`/api/tasks/${k}/audio`;a2.download=`${k}.wav`;a2.click();break}
   case 'reuse':{const r=state.result;
    const t2=state.tasks.find(x=>x.id===r.taskId);
    if(t2)reloadParams(t2);else switchView('workbench');
    break}
   case 'play-task':case 'play-recent':{
    const it=state.tasks.find(x=>x.id===k);if(!it)break;
    if(a==='play-recent'){state.result={title:displayTaskText(it.text).slice(0,22),taskId:it.id,
      model:modelLabel(it.model),voice:voiceOrSpk(it)||SYS_MODE_NAMES[it.mode],time:relTime(it.created_at),dur:it.duration};
      switchView('workbench');}
    player.playFile(`/api/tasks/${it.id}/audio`,displayTaskText(it.text).slice(0,24)+'…',it.id);
    toast('已载入作品，点播放试听');break}
   case 'filter':state.filter=k;renderLibrary();break;
   case 'lib-refresh':loadTasks().then(()=>toast('已刷新作品库'));break;
   /* ---- 播客工坊 ---- */
   case 'pod-src':pod.src=k;pod.sourceError='';pod.extracting=false;renderPodSource();renderPodStepper();break;
   case 'pod-file':{const inp=$('#pod-file-input');if(inp)inp.click();break}
   case 'pod-url-extract':extractPodUrl();break;
   case 'pod-extract-reset':pod.extractText=pod.extractText0;pod.extractChars=pod.extractText0.length;
    pod.extractDirty=false;renderPodSource();renderPodStepper();break;
   case 'pod-llm':openLLMConfig();break;
   case 'pod-llm-close':$('#llm-modal').hidden=true;break;
   case 'pod-llm-test':testLLM();break;
   case 'pod-llm-save':saveLLMConfig();break;
   case 'pod-direct':if(!podSrcReady()){toast('内容源至少需要 10 个字',true);break}directRead();break;
   case 'gen-stop':stopGen();break;
   case 'pod-synth-stop':case 'pod-stream-stop':stopPodSynth();break;
   case 'pod-gen':genScript();break;
   case 'pod-line-edit':pod.editLine=+t.dataset.i;pod.editText=pod.lines[pod.editLine].text;renderPodScript();
    {const el=$('#pod-line-edit-text');if(el)el.focus()}break;
   case 'pod-line-save':{const i=+t.dataset.i,v=$('#pod-line-edit-text').value.trim();
    pod.lines[i].text=v||pod.lines[i].text;pod.editLine=-1;renderPodScript();renderPodOutput();break}
   case 'pod-line-cancel':pod.editLine=-1;renderPodScript();break;
   case 'pod-line-del':pod.lines.splice(+t.dataset.i,1);pod.editLine=-1;renderPod();break;
   case 'pod-line-add':{ensureSpeakerNames();
    const alt=pod.lines.length%2?'guest':'host';
    const s=pod.speakers.find(x=>x.role===alt)||pod.speakers[0];
    pod.lines.push({role:s.role,name:s.name,text:''});
    pod.editLine=pod.lines.length-1;pod.editText='';renderPodScript();renderPodOutput();break}
   case 'pod-cast-add':{const used=new Set(pod.speakers.map(s=>s.voice_id));
    const free=voices.find(v=>!used.has(v.id))||voices[0];
    pod.speakers.push({role:'guest',name:'嘉宾'+(pod.speakers.filter(s=>s.role!=='host').length+1),desc:'',voice_id:free?free.id:''});
    renderPodCast();renderPodScript();renderPodStepper();
    toast('已添加一位嘉宾并自动分配未占用的音色');break}
   case 'pod-cast-del':{
    if(pod.speakers.filter(s=>s.role!=='host').length<=1){toast('至少保留一位嘉宾',true);break}
    pod.speakers.splice(+t.dataset.i,1);renderPodCast();renderPodScript();renderPodStepper();break}
   case 'pod-out-tab':pod.outTab=k;renderPodOutput();break;
   case 'pod-save':saveEpisode();break;
   case 'pod-synth':synthPodFinal();break;
   case 'pod-play':{if(!podSynthDone()||!pod.episode){toast('请先合成成品',true);break}
    player.playFile(`/api/podcast/episodes/${pod.episode.id}/audio`,'播客 · '+(pod.episode.title||''),null);
    player.podEpId=pod.episode.id;
    const lm=podLineMarks(pod.episode);               // 逐句偏移: 播放时高亮当前轮对话
    if(lm)player.setLineMarks(lm.marks,lm.sr);
    break}
   case 'pod-download':case 'pod-ep-download':{
    const eid=(a==='pod-download'?(pod.episode&&pod.episode.id):k);
    const ep=pod.episodes.find(x=>x.id===eid)||pod.episode;
    const a3=document.createElement('a');
    a3.href=`/api/podcast/episodes/${eid}/audio`;a3.download=(ep?ep.title:eid)+'.wav';a3.click();break}
   case 'pod-stream':synthPodStream();break;
   case 'pod-push-toggle':pod.pushOn=!pod.pushOn;renderPodOutput();break;
   case 'pod-proto':pod.protocol=k;renderPodOutput();break;
   case 'pod-push-start':startPush();break;
   case 'pod-push-stop':stopPush();break;
   case 'pod-queue-refresh':pollPod(true).then(()=>toast('任务队列已刷新'));break;
   case 'pod-list-refresh':loadEpisodes().then(()=>{renderPodEpisodes();toast('播客列表已刷新')});break;
   case 'pod-task-detail':{
    pod.detail=pod.episodes.find(x=>x.id===k)||null;
    pod.detailOpen=true;$('#pod-detail-modal').hidden=false;renderPodDetail();
    api(`/api/podcast/episodes/${k}`).then(r=>r.json()).then(j=>{pod.detail=j;if(pod.detailOpen)renderPodDetail()}).catch(()=>{});
    break}
   case 'pod-detail-close':pod.detailOpen=false;$('#pod-detail-modal').hidden=true;break;
   case 'pod-detail-stop':stopPush(k);break;
   case 'pod-detail-load':{pod.detailOpen=false;$('#pod-detail-modal').hidden=true;loadEpisode(k);break}
   case 'pod-detail-del':{pod.detailOpen=false;$('#pod-detail-modal').hidden=true;removeEpisode(k);break}
   case 'pod-detail-play':{player.playFile(`/api/podcast/episodes/${k}/audio`,'播客成品',null);
    player.podEpId=k;
    // 仅当试听的是当前载入的剧集(句数一致)才对齐对话稿高亮
    const dep=pod.episodes.find(x=>x.id===k);
    const dlm=dep&&pod.episode&&dep.id===pod.episode.id?podLineMarks(dep):null;
    if(dlm)player.setLineMarks(dlm.marks,dlm.sr);
    break}
   case 'pod-detail-download':{const a4=document.createElement('a');
    a4.href=`/api/podcast/episodes/${k}/audio`;a4.download=k+'.wav';a4.click();break}
   case 'pod-ep-load':loadEpisode(k);break;
   case 'pod-ep-del':{
    if(pod.confirmDel!==k){pod.confirmDel=k;renderPodEpisodes();toast('再次点击「确认删除」以移除该剧集',true);break}
    pod.confirmDel=null;removeEpisode(k);break}
   /* ---- 音色库 ---- */
   case 'voice-focus':{const el=$('#voice-new-name');if(el&&el.focus){el.focus();el.scrollIntoView({block:'center'})}
    toast('已定位到「上传新音色」表单');break}
   case 'voice-pick':{const inp=$('#voice-file-input');if(inp)inp.click();break}
   case 'voice-save':vlibSave();break;
   case 'voice-preview':{const v=voices.find(x=>x.id===k);
    if(v){player.playFile(`/api/voices/${v.id}/prompt`,'参考音频 · '+v.name,null);toast(`试听参考音频「${v.name}」`)}break}
   case 'voice-use':{const v=voices.find(x=>x.id===k);if(!v)break;
    if(caps().mine){state.group='mine';state.pick=v.id;state.refText=v.prompt_text||''}
    switchView('workbench');renderTabs();renderSounds();renderAction();
    toast(`已选择音色「${v.name}」，参考文本已回填`);break}
   case 'voice-edit':{const v=voices.find(x=>x.id===k);if(!v)break;
    vlib.editId=k;vlib.editName=v.name;vlib.editPrompt=v.prompt_text||'';vlib.confirmDel=null;
    renderVoiceCards();break}
   case 'voice-edit-cancel':vlib.editId=null;renderVoiceCards();break;
   case 'voice-edit-save':{
    if(!vlib.editName.trim()){toast('名称不能为空',true);break}
    if(!vlib.editPrompt.trim()){toast('参考文本不能为空',true);break}
    api(`/api/voices/${k}`,{method:'PUT',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({name:vlib.editName.trim(),prompt_text:vlib.editPrompt.trim()})})
      .then(r=>r.json()).then(updated=>{
        const i=voices.findIndex(x=>x.id===updated.id);
        if(i>=0)voices[i]=updated;
        vlib.editId=null;renderVoiceCards();renderVoiceSide();renderSounds();
        toast('音色已更新');
      }).catch(e=>toast('保存失败: '+e.message,true));
    break}
   case 'voice-del':{
    if(vlib.confirmDel!==k){vlib.confirmDel=k;renderVoiceCards();toast('再次点击「确认删除」以移除该音色',true);break}
    const i=voices.findIndex(x=>x.id===k);
    if(i>=0)voices.splice(i,1);
    if(state.pick===k){state.pick=voices.length?voices[0].id:null;state.refText=voices.length?(voices[0].prompt_text||''):''}
    vlib.confirmDel=null;
    api(`/api/voices/${k}`,{method:'DELETE'})
      .then(()=>{fetchHealth();toast('音色已删除')})
      .catch(e=>toast(e.message,true));
    renderVoiceLibrary();renderVoiceSide();renderSounds();break}
   case 'voice-refresh':loadVoices().then(()=>{renderVoiceLibrary();renderVoiceSide();toast('音色列表已刷新')});break;
   /* ---- 系统状态与设置 ---- */
   case 'sys-doctor-all':sysDoctorAll();break;
   case 'sys-refresh':sysRefresh();break;
   case 'sys-path':sys.expanded[k]=!sys.expanded[k];renderSystem();break;
   case 'sys-doctor-one':sysRunDoctor(k);break;
   case 'sys-load':sysLoad(k);break;
   case 'sys-settings':openSysSettings(k);break;
   case 'sys-settings-close':closeSysSettings();break;
   case 'sys-settings-save':saveSysConfig();break;
   case 'sys-settings-reset':resetSysConfig();break;
  }
});

/* ================= 事件: 输入 / 变更 / 拖拽 / 键盘 ================= */
document.addEventListener('input',e=>{
  const id=e.target.id;
  if(id==='text-input'){state.text=e.target.value;$('#char-count').textContent=state.text.length+' / 2000';renderAction()}
  if(id==='design-input'){state.designText=e.target.value;renderAction()}
  if(id==='ref-input'){state.refText=e.target.value;renderAction()}
  if(id==='instruct-input'){state.instruct=e.target.value;renderAction()}
  if(id==='speed-range'){state.speed=parseFloat(e.target.value);$('#speed-val').textContent=state.speed.toFixed(2)+'×';renderAction()}
  if(id==='pod-src-text'){pod.text=e.target.value;renderPodStepper();updatePodSrcButtons()}
  if(id==='pod-src-extract'){
    pod.extractText=e.target.value;pod.extractChars=e.target.value.length;
    pod.extractDirty=pod.extractText!==pod.extractText0;updatePodExtractMeta();
  }
  if(id==='pod-src-url')pod.url=e.target.value;
  if(id==='pod-push-url')pod.pushUrl=e.target.value;
  if(id==='pod-line-edit-text')pod.editText=e.target.value;
  if(id==='pod-speed'){pod.speed=parseFloat(e.target.value);
    const v=$('#pod-speed').parentElement.querySelector('.range-val');if(v)v.textContent=pod.speed.toFixed(2)+'×';renderPodStepper()}
  if(id==='voice-new-name'){vlib.name=e.target.value;vlibUpdateSave()}
  if(id==='voice-new-text'){vlib.prompt=e.target.value;vlibUpdateSave()}
  if(id==='voice-edit-name')vlib.editName=e.target.value;
  if(id==='voice-edit-text')vlib.editPrompt=e.target.value;
  if(e.target.dataset.spk!==undefined){
    const i=+e.target.dataset.spk;pod.speakers[i][e.target.dataset.field]=e.target.value;renderPodStepper();
  }
});
/* 内容源按钮态微更新: 不重绘输入框, 只同步禁用与提示 */
function updatePodSrcButtons(){
  const ready=podSrcReady()&&!pod.genning;
  const btns=$$('#pod-source .src-actions .btn');
  btns.forEach(b=>b.disabled=!ready);
  const notice=$('#pod-source .notice.warn');
  if(notice)notice.hidden=ready;
}
/* 正文预览编辑态微更新: 字数 / 顶部计数 / 还原按钮, 不重绘文本域 */
function updatePodExtractMeta(){
  const n=$('#pod-extract-chars');
  if(n){n.textContent=pod.extractChars;n.classList.toggle('over',pod.extractChars>20000)}
  const c=$('#pod-src-counter');
  if(c)c.textContent=pod.extractChars?`已提取 ${pod.extractChars} 字`:'待导入';
  const rst=$('#pod-source [data-act="pod-extract-reset"]');
  if(rst)rst.disabled=!pod.extractDirty;
  updatePodSrcButtons();
}
document.addEventListener('change',e=>{
  const t=e.target;
  if(t.dataset.spk!==undefined&&t.dataset.field==='voice'){
    pod.speakers[+t.dataset.spk].voice_id=t.value;renderPodScript();renderPodStepper();
  }
  if(t.dataset.line!==undefined){
    const s=pod.speakers[+t.value];
    if(s){const ln=pod.lines[+t.dataset.line];ln.role=s.role;ln.name=s.name;renderPodScript()}
  }
  if(t.id==='voice-file-input'&&t.files&&t.files[0]){vlibInspectFile(t.files[0]);t.value=''}
  if(t.id==='pod-file-input'&&t.files&&t.files[0]){
    pod.fileName=t.files[0].name;pod.fileRaw=t.files[0];pod.extractText='';pod.extractChars=0;pod.sourceError='';
    renderPodSource();extractPodFile();t.value='';
  }
});
/* 音色库拖拽 + 播客文件拖拽: 高亮态 + 落地即解析 */
document.addEventListener('dragover',e=>{
  const d=e.target.closest&&e.target.closest('.vlib-drop,#pod-source .dropzone');
  if(!d)return;
  e.preventDefault();
  d.classList.add('drag');
});
document.addEventListener('dragleave',e=>{
  const d=e.target.closest&&e.target.closest('.vlib-drop,#pod-source .dropzone');
  if(d&&(!e.relatedTarget||!d.contains(e.relatedTarget)))d.classList.remove('drag');
});
document.addEventListener('drop',e=>{
  const d=e.target.closest&&e.target.closest('.vlib-drop,#pod-source .dropzone');
  if(!d)return;
  e.preventDefault();d.classList.remove('drag');
  const f=e.dataTransfer&&e.dataTransfer.files&&e.dataTransfer.files[0];
  if(!f)return;
  if(d.id==='voice-file-input'||d.closest('[data-act="voice-pick"]')){vlibInspectFile(f)}
  else if(d.dataset.act==='pod-file'){
    pod.fileName=f.name;pod.fileRaw=f;pod.extractText='';pod.extractChars=0;pod.sourceError='';
    renderPodSource();extractPodFile();
  }
});
document.addEventListener('keydown',e=>{
  if((e.ctrlKey||e.metaKey)&&e.key==='Enter'){e.preventDefault();if(state.view==='workbench')generate()}
  if(e.key==='Escape'){
    if($('#llm-modal').hidden===false)$('#llm-modal').hidden=true;
    if(pod.detailOpen){pod.detailOpen=false;$('#pod-detail-modal').hidden=true}
    if(sys.settings)closeSysSettings();
    if(state.menu){state.menu=false;renderModelMenu()}
  }
  if((e.key==='Enter'||e.key===' ')&&e.target.closest&&e.target.closest('[data-act="sys-path"]')){
    e.preventDefault();const pk=e.target.closest('[data-act="sys-path"]').dataset.key;
    sys.expanded[pk]=!sys.expanded[pk];renderSystem();return}
  if(e.key===' '&&state.view==='workbench'&&state.result
     &&!/INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName)
     &&!e.target.closest('button')){e.preventDefault();player.togglePlay()}
});

/* ================= 启动 ================= */
$('#char-count').textContent='0 / 2000';
player=new Player(onPlayerTick);
syncThemeIcon();
fetchHealth();
loadVoices().then(()=>{renderSounds();renderPodCast()});
loadTasks();
loadEpisodes();
connectSSE();
setInterval(fetchHealth,8000);
setInterval(()=>pollPod(false),2000);
setInterval(()=>{if(state.view==='workbench'||state.view==='library')loadTasks()},3000);
renderAll();
function renderAll(){
  ensureRefText();
  renderModelMenu();renderCapNotice();renderTabs();renderSounds();renderAdv();renderAction();
  renderOutput();renderRecent();renderVoiceLibrary();renderSystem();renderPod();syncThemeIcon();
}
