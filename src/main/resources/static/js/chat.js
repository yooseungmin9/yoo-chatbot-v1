// ========== 0) 기본 설정 ==========
const APP_NAME  = "AI 경제질문 챗봇";
const APP_NAME_US = "AI Economy Q&A Chatbot";
const CHAT_URL  = "/api/chat";
const RESET_URL = "/api/reset";
const STT_URL   = "/api/stt";
const TTS_URL   = "/api/tts";
const TIMEOUT_MS = 180000;

// i18n
const I18N = {
  "ko-KR": {
    appTitle: APP_NAME,
    pageHeading: "경제 질문 챗봇",
    labelLang: "언어",
    btnReset: "대화 초기화",
    btnSend: "전송",
    btnTts: "🔈 답변 듣기",
    inputPh: "질문을 말하거나 입력하세요...",
    welcome: `안녕하세요! <b>${APP_NAME}</b>입니다. 무엇을 도와드릴까요?`,
    statusIdle: "상태: 대기",
    statusTyping: "입력 중...",
    sttStart: "🎤️ 실시간 음성 인식을 시작합니다.",
    sttRec: "녹음 중(서버 업로드)...",
    sttAuto: "🎤️ 녹음 시작! 말을 멈추면 5초 뒤 자동 입력됩니다.",
    sttDone: "인식 완료.",
    cleared: "대화 기록을 초기화했습니다."
  },
  "en-US": {
    appTitle: APP_NAME + " (EN)",
    pageHeading: "Economy Q&A Chatbot",
    labelLang: "Language",
    btnReset: "Reset",
    btnSend: "Send",
    btnTts: "🔈 Read answer",
    inputPh: "Speak or type your question...",
    welcome: `Hello! This is <b>${APP_NAME_US}</b>. How can I help you today?`,
    statusIdle: "Status: idle",
    statusTyping: "Typing...",
    sttStart: "🎤️ Live speech recognition started.",
    sttRec: "Recording (server upload)...",
    sttAuto: "🎤️ Recording! <b>Auto-transcribe 5s after silence</b>.",
    sttDone: "Recognition finished.",
    cleared: "Conversation cleared."
  }
};

// ========== 1) DOM ==========
const titleEl     = document.getElementById("appTitle");
const headingEl   = document.getElementById("pageHeading");
const labelLangEl = document.getElementById("labelLang");
const langSelect  = document.getElementById("langSelect");

const chatEl      = document.getElementById("chat");
const formEl      = document.getElementById("chatForm");
const inputEl     = document.getElementById("messageInput");
const sendBtn     = document.getElementById("sendBtn");
const resetBtn    = document.getElementById("resetBtn");

const sttStartBtn = document.getElementById("sttStartBtn");
const sttStopBtn  = document.getElementById("sttStopBtn");
const ttsBtn      = document.getElementById("ttsBtn");
const ttsAudio    = document.getElementById("ttsAudio");

// 다크모드 관련 DOM
const themeToggleBtn = document.getElementById("themeToggleBtn");
const themeIcon = themeToggleBtn?.querySelector(".theme-icon");

let LANG = localStorage.getItem("chat_lang") || (langSelect?.value || "ko-KR");

// ========== 1-1) 다크모드 초기화 ==========
function initTheme() {
  const savedTheme = localStorage.getItem("chat_theme") || "light";
  applyTheme(savedTheme, false); // 애니메이션 없이 즉시 적용
}

function applyTheme(theme, animate = true) {
  const htmlEl = document.documentElement;
  
  // 애니메이션 제어
  if (!animate) {
    htmlEl.classList.add("no-transition");
  }
  
  if (theme === "dark") {
    htmlEl.setAttribute("data-theme", "dark");
    if (themeIcon) themeIcon.textContent = "☀️";
  } else {
    htmlEl.removeAttribute("data-theme");
    if (themeIcon) themeIcon.textContent = "🌙";
  }
  
  localStorage.setItem("chat_theme", theme);
  
  // 애니메이션 복원
  if (!animate) {
    setTimeout(() => htmlEl.classList.remove("no-transition"), 50);
  }
}

function toggleTheme() {
  const htmlEl = document.documentElement;
  const currentTheme = htmlEl.getAttribute("data-theme");
  const newTheme = currentTheme === "dark" ? "light" : "dark";
  applyTheme(newTheme, true);
}

// 다크모드 버튼 이벤트
themeToggleBtn?.addEventListener("click", toggleTheme);

// ========== 2) 도우미 ==========
const escapeHtml = (s)=>String(s||"").replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[m]));
const mdSafe = (text)=> escapeHtml(text).replace(/^-\s/gm,"• ").replace(/\n/g,"<br>");
const scrollToBottom = ()=>{ chatEl.scrollTop = chatEl.scrollHeight; };

function bubbleUser(text){
  chatEl.insertAdjacentHTML("beforeend",
    `<div class="message user-message"><div class="message-content">${escapeHtml(text)}</div></div>`);
  scrollToBottom();
}
function bubbleAI(html){
  chatEl.insertAdjacentHTML("beforeend",
    `<div class="message bot-message"><div class="message-content" data-tts="${escapeHtml(html).replace(/<[^>]+>/g,'')}">${html}</div></div>`);
  scrollToBottom();
}
function bubbleStatus(text){
  chatEl.insertAdjacentHTML("beforeend",
    `<div class="message bot-message"><div class="message-content muted">${escapeHtml(text)}</div></div>`);
  scrollToBottom();
}
function bubbleTyping() {
  const id = "typing-" + (crypto?.randomUUID?.() || Math.random().toString(36).slice(2));
  chatEl.insertAdjacentHTML(
    "beforeend",
    `<div id="${id}" class="message bot-message">
      <div class="message-content" aria-live="polite" aria-busy="true">
        <span class="dot">•</span><span class="dot">•</span><span class="dot">•</span>
      </div>
    </div>`
  );
  return id;
}
function removeEl(id){ const el=document.getElementById(id); if(el) el.remove(); }

// i18n 적용
function setLang(next){
  LANG = (next === "en-US" ? "en-US" : "ko-KR");
  localStorage.setItem("chat_lang", LANG);
  if (langSelect && langSelect.value !== LANG) langSelect.value = LANG;

  titleEl && (titleEl.textContent = I18N[LANG].appTitle);
  headingEl && (headingEl.textContent = I18N[LANG].pageHeading);
  labelLangEl && (labelLangEl.textContent = I18N[LANG].labelLang);
  resetBtn && (resetBtn.textContent = I18N[LANG].btnReset);
  sendBtn  && (sendBtn.textContent  = I18N[LANG].btnSend);
  ttsBtn   && (ttsBtn.textContent   = I18N[LANG].btnTts);
  inputEl  && (inputEl.placeholder  = I18N[LANG].inputPh);
}

// 환영/상태 렌더
function renderWelcome(){
  chatEl.innerHTML = "";
  bubbleAI(I18N[LANG].welcome);
  bubbleStatus(I18N[LANG].statusIdle);
}

// ========== 3) FAQ 즉시 전송 ==========
function sendQuestion(q){
  const text = (q || "").trim();
  if(!text) return;
  bubbleUser(text);
  inputEl.value = "";
  sendBtn.disabled = true;
  const typingId = bubbleTyping();

  (async ()=>{
    try{
      const ctrl = new AbortController();
      const to = setTimeout(()=>ctrl.abort("timeout"), TIMEOUT_MS);

      const res = await fetch(CHAT_URL, {
        method:"POST",
        headers: {"Content-Type":"application/json"},
        body: JSON.stringify({ message: text, lang: LANG }),
        signal: ctrl.signal
      });
      clearTimeout(to);

      if(!res.ok){ removeEl(typingId); return bubbleAI(`서버 오류(${res.status})`); }
      const data = await res.json();
      removeEl(typingId);
      bubbleAI(mdSafe(data.answer || "응답이 비었습니다."));
    }catch(err){
      removeEl(typingId);
      bubbleAI("요청 실패: " + (err?.message || err));
    }finally{
      sendBtn.disabled = false;
    }
  })();
}

function bindFAQ(){
  document.querySelectorAll(".faq-item").forEach(btn=>{
    btn.addEventListener("click", ()=>{
      const q = btn.getAttribute("data-question") || btn.textContent || "";
      sendQuestion(q);
    });
  });
}

// ========== 4) 초기화 ==========
initTheme(); // 다크모드 먼저 초기화
setLang(LANG);
renderWelcome();
bindFAQ();

// ========== 5) 이벤트 ==========
langSelect?.addEventListener("change", ()=>{
  setLang(langSelect.value);
  renderWelcome();
});

inputEl.addEventListener("keydown", function (e) {
  if (e.isComposing) return;
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    formEl.requestSubmit();
  }
});

formEl?.addEventListener("submit", (e)=>{
  e.preventDefault();
  const q = inputEl.value.trim();
  if(!q) return;
  sendQuestion(q);
});

resetBtn?.addEventListener("click", async ()=>{
  resetBtn.disabled = true;
  try{
    await fetch(RESET_URL, { method:"POST" });
    localStorage.removeItem("chat_messages");
    renderWelcome();
    bubbleStatus(I18N[LANG].cleared);
  }catch{
    bubbleAI("초기화 요청 실패.");
  }finally{
    resetBtn.disabled = false;
  }
});

// ========== 6) STT: 마이크 실시간 받아적기 ==========
let recognition = null;
let recogRunning = false;
let baseBeforeRec = "";
let finalSoFar = "";

function isSpeechAPIAvailable(){
  return !!(window.SpeechRecognition || window.webkitSpeechRecognition);
}

function makeRecognition(){
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  const r = new SR();
  r.lang = LANG;
  r.interimResults = true;
  r.continuous = true;
  return r;
}

async function startSTT(){
  if (recogRunning) return;

  if (!isSpeechAPIAvailable()) {
    bubbleAI(LANG.startsWith('ko')
      ? '이 브라우저는 실시간 음성인식(Web Speech API)을 지원하지 않습니다. 크롬(HTTPS/localhost)에서 시도해 주세요.'
      : 'This browser does not support the Web Speech API. Try Chrome (HTTPS/localhost).');
    return;
  }
  if (!window.isSecureContext) {
    bubbleAI(LANG.startsWith('ko')
      ? '마이크는 HTTPS(또는 localhost)에서만 동작합니다.'
      : 'Microphone requires HTTPS (or localhost).');
    return;
  }

  recognition = makeRecognition();
  baseBeforeRec = inputEl.value;
  finalSoFar = "";

  recognition.onstart = ()=>{
    recogRunning = true;
    sttStartBtn.disabled = true;
    sttStopBtn.disabled  = false;
    bubbleStatus(I18N[LANG].sttStart);
  };

  recognition.onresult = (e)=>{
    let interim = "";
    for (let i = e.resultIndex; i < e.results.length; i++) {
      const res = e.results[i];
      if (res.isFinal) {
        finalSoFar += res[0].transcript;
      } else {
        interim += res[0].transcript;
      }
    }
    const composed = (baseBeforeRec ? baseBeforeRec + " " : "") + (finalSoFar + interim).trim();
    inputEl.value = composed;
    try { inputEl.setSelectionRange(inputEl.value.length, inputEl.value.length); } catch {}
  };

  recognition.onerror = (e)=>{
    bubbleAI('음성 인식 오류: ' + (e.error || 'unknown'));
  };

  recognition.onend = ()=>{
    recogRunning = false;
    sttStartBtn.disabled = false;
    sttStopBtn.disabled  = true;
    bubbleStatus(I18N[LANG].sttDone);
    recognition = null;
  };

  try {
    recognition.start();
  } catch (err) {
    recogRunning = false;
    sttStartBtn.disabled = false;
    sttStopBtn.disabled  = true;
    bubbleAI('음성 인식 시작 실패: ' + (err?.message || err));
  }
}

function stopSTT(){
  try {
    if (recognition && recogRunning) {
      recognition.stop();
    }
  } catch (e) {
    bubbleAI('STT 정지 오류: ' + (e?.message || e));
  }
}

sttStartBtn?.addEventListener('click', startSTT);
sttStopBtn?.addEventListener('click', stopSTT);

// ========== 7) TTS: 버튼/말풍선 ==========
ttsBtn?.addEventListener("click", async ()=>{
  const last = [...document.querySelectorAll(".bot-message .message-content")].pop();
  if (!last) return;
  const text = last.getAttribute("data-tts") || last.innerText || "";
  if (!text.trim()) return;
  try{
    const res = await fetch(TTS_URL, {
      method:"POST",
      headers:{"Content-Type":"application/json"},
      body: JSON.stringify({
        text: text.slice(0,2000), lang: LANG,
        voice: LANG.startsWith("ko") ? "ko-KR-Neural2-B" : "en-US-Neural2-C",
        fmt: "MP3", rate: 1.0, pitch: 0.0
      })
    });
    const ct = (res.headers.get("content-type")||"").toLowerCase();
    if(res.ok && ct.includes("audio")){
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      if (ttsAudio){ ttsAudio.src = url; await ttsAudio.play(); }
      else { new Audio(url).play(); }
    } else {
      const txt = await res.text().catch(()=> "");
      bubbleAI("TTS 오류: " + (txt || `HTTP ${res.status}`));
    }
  }catch(err){ bubbleAI("TTS 호출 실패: " + (err?.message || err)); }
});

chatEl.addEventListener("click", async (e)=>{
  const msg = e.target.closest(".bot-message .message-content");
  if (!msg) return;
  const text = msg.getAttribute("data-tts") || msg.innerText || msg.textContent || "";
  if (!text.trim()) return;

  try {
    const res = await fetch(TTS_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        text: text.slice(0, 2000),
        lang: LANG,
        voice: LANG.startsWith("ko") ? "ko-KR-Neural2-B" : "en-US-Neural2-C",
        fmt: "MP3",
        rate: 1.0,
        pitch: 0.0
      })
    });
    const ct = (res.headers.get("content-type") || "").toLowerCase();
    if(res.ok && ct.includes("audio")){
      const blob = await res.blob();
      const url  = URL.createObjectURL(blob);
      if (ttsAudio) { ttsAudio.src = url; await ttsAudio.play(); }
      else { new Audio(url).play(); }
    } else {
      const txt = await res.text().catch(()=> "");
      bubbleAI("TTS 오류: " + (txt || `HTTP ${res.status}`));
    }
  } catch (err) {
    bubbleAI("TTS 호출 실패: " + (err?.message || err));
  }
});