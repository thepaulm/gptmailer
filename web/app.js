const recordBtn = document.getElementById("recordBtn");
const speakToggle = document.getElementById("speakToggle");
const voiceSelect = document.getElementById("voiceSelect");
const speechRateSelect = document.getElementById("speechRateSelect");
const statusEl = document.getElementById("status");
const transcriptEl = document.getElementById("transcript");

let recorder = null;
let chunks = [];
let isRecording = false;
let sessionActive = false;
let conversation = [];
let activeAudio = null;
let audioContext = null;
let analyserNode = null;
let sourceNode = null;
let silenceTimer = null;
let silenceRafId = null;
let hasHeardSpeech = false;

const SILENCE_TIMEOUT_MS = 1600;
const VOICE_THRESHOLD = 0.02;

const EMAIL_RE = /[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}/;
const EMAIL_VERBS_RE = /\b(email|mail|send)\b/i;
const EMAIL_ROUTE_RE = /\b(by|via|through)\s+email\b/i;
const SUMMARY_WORD_RE = /\b(summary|recap|notes?|bullet\s*points?|takeaways?)\b/i;

function wantsSummaryEmail(text) {
  const normalized = text.toLowerCase().trim();
  const mentionsEmail =
    EMAIL_VERBS_RE.test(normalized) ||
    EMAIL_ROUTE_RE.test(normalized) ||
    /\be-?mail\b/i.test(normalized);
  const asksForSummary =
    SUMMARY_WORD_RE.test(normalized) ||
    /\b(this|that|it|conversation|chat)\b/i.test(normalized);
  return mentionsEmail && asksForSummary;
}

function setStatus(text) {
  statusEl.textContent = text;
}

function appendTranscript(text) {
  const item = document.createElement("div");
  item.className = "line";
  item.textContent = text;
  transcriptEl.appendChild(item);
  transcriptEl.scrollTop = transcriptEl.scrollHeight;
}

function selectedSpeechRate() {
  const raw = speechRateSelect ? Number.parseFloat(speechRateSelect.value) : 1.15;
  if (!Number.isFinite(raw)) return 1.15;
  return Math.min(1.4, Math.max(0.8, raw));
}

function conversationText() {
  return conversation
    .map((m) => `${m.role === "assistant" ? "Assistant" : "User"}: ${m.content}`)
    .join("\n");
}

function chatHistory() {
  return conversation
    .filter((m) => m.role === "user" || m.role === "assistant")
    .map((m) => ({ role: m.role, content: m.content }));
}

async function startRecording() {
  if (!sessionActive || isRecording) return;
  const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  const options = { mimeType: "audio/webm" };
  recorder = new MediaRecorder(stream, options);

  chunks = [];
  hasHeardSpeech = false;
  recorder.ondataavailable = (e) => {
    if (e.data.size > 0) chunks.push(e.data);
  };

  recorder.onstop = async () => {
    cleanupSilenceDetection();
    const blob = new Blob(chunks, { type: "audio/webm" });
    if (blob.size > 0) {
      await sendForTranscription(blob);
    } else {
      setStatus("Idle");
    }
    stream.getTracks().forEach((t) => t.stop());
    recorder = null;
    if (sessionActive) {
      await startRecording();
    } else {
      recordBtn.textContent = "Start Recording";
      setStatus("Idle");
    }
  };

  recorder.start();
  startSilenceDetection(stream);
  isRecording = true;
  recordBtn.textContent = "End Chat";
  setStatus("Recording...");
}

function stopRecording(auto = false) {
  cleanupSilenceDetection();
  if (recorder && isRecording) {
    recorder.stop();
  }
  isRecording = false;
  if (!sessionActive) {
    recordBtn.textContent = "Start Recording";
  }
  setStatus(auto ? "Pause detected. Processing..." : "Processing...");
}

function computeRms(analyser, dataArray) {
  analyser.getByteTimeDomainData(dataArray);
  let sumSq = 0;
  for (let i = 0; i < dataArray.length; i += 1) {
    const centered = (dataArray[i] - 128) / 128;
    sumSq += centered * centered;
  }
  return Math.sqrt(sumSq / dataArray.length);
}

function startSilenceDetection(stream) {
  cleanupSilenceDetection();
  const AudioCtx = window.AudioContext || window.webkitAudioContext;
  if (!AudioCtx) return;
  audioContext = new AudioCtx();
  sourceNode = audioContext.createMediaStreamSource(stream);
  analyserNode = audioContext.createAnalyser();
  analyserNode.fftSize = 2048;
  sourceNode.connect(analyserNode);
  const dataArray = new Uint8Array(analyserNode.fftSize);

  const tick = () => {
    if (!isRecording || !analyserNode) return;
    const rms = computeRms(analyserNode, dataArray);

    if (rms > VOICE_THRESHOLD) {
      hasHeardSpeech = true;
      if (silenceTimer) {
        clearTimeout(silenceTimer);
        silenceTimer = null;
      }
    } else if (hasHeardSpeech && !silenceTimer) {
      silenceTimer = setTimeout(() => {
        if (isRecording && hasHeardSpeech) {
          stopRecording(true);
        }
      }, SILENCE_TIMEOUT_MS);
    }

    silenceRafId = requestAnimationFrame(tick);
  };

  silenceRafId = requestAnimationFrame(tick);
}

function cleanupSilenceDetection() {
  if (silenceTimer) {
    clearTimeout(silenceTimer);
    silenceTimer = null;
  }
  if (silenceRafId) {
    cancelAnimationFrame(silenceRafId);
    silenceRafId = null;
  }
  if (sourceNode) {
    sourceNode.disconnect();
    sourceNode = null;
  }
  if (analyserNode) {
    analyserNode.disconnect();
    analyserNode = null;
  }
  if (audioContext) {
    audioContext.close().catch(() => {});
    audioContext = null;
  }
}

async function sendForTranscription(blob) {
  const formData = new FormData();
  formData.append("file", blob, "audio.webm");

  try {
    const resp = await fetch("/transcribe", {
      method: "POST",
      body: formData,
    });

    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      throw new Error(err.detail || "Transcription failed");
    }

    const data = await resp.json();
    const text = data.text.trim();
    if (!text) throw new Error("Empty transcript");

    conversation.push({ role: "user", content: text });
    appendTranscript(`You: ${text}`);
    setStatus("Idle");

    if (wantsSummaryEmail(text)) {
      await sendSummaryEmail(text);
      return;
    }

    await askAssistant(text);
  } catch (err) {
    setStatus(`Error: ${err.message}`);
  }
}

async function askAssistant(message) {
  setStatus("Thinking...");
  try {
    const history = chatHistory().slice(0, -1);
    const resp = await fetch("/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        message,
        history,
      }),
    });

    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      throw new Error(err.detail || "Chat failed");
    }

    const data = await resp.json();
    const reply = (data.reply || "").trim();
    if (!reply) throw new Error("Empty chat response");

    conversation.push({ role: "assistant", content: reply });
    appendTranscript(`Assistant: ${reply}`);
    setStatus("Idle");
    await speakReply(reply);
  } catch (err) {
    setStatus(`Error: ${err.message}`);
  }
}

async function speakReply(text) {
  if (!speakToggle || !speakToggle.checked) return;
  const voice = voiceSelect ? voiceSelect.value : "alloy";
  const speed = selectedSpeechRate();
  try {
    const resp = await fetch("/speak", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text, voice }),
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      throw new Error(err.detail || "TTS failed");
    }

    const audioBlob = await resp.blob();
    const objectUrl = URL.createObjectURL(audioBlob);

    if (activeAudio) {
      activeAudio.pause();
      if (activeAudio.dataset && activeAudio.dataset.url) {
        URL.revokeObjectURL(activeAudio.dataset.url);
      }
    }

    const audio = new Audio(objectUrl);
    audio.dataset.url = objectUrl;
    audio.playbackRate = speed;
    activeAudio = audio;
    await audio.play();
    await new Promise((resolve, reject) => {
      audio.onended = () => resolve();
      audio.onerror = () => reject(new Error("Audio playback failed"));
    });
    URL.revokeObjectURL(objectUrl);
    if (activeAudio === audio) activeAudio = null;
  } catch (err) {
    setStatus(`Error: ${err.message}`);
  }
}

async function sendSummaryEmail(latestText) {
  const toMatch = latestText.match(EMAIL_RE);
  const toEmail = toMatch ? toMatch[0] : null;

  setStatus("Sending summary email...");

  const payload = {
    conversation: conversationText(),
  };

  if (toEmail) payload.to = toEmail;

  try {
    const resp = await fetch("/summarize_email", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });

    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      throw new Error(err.detail || "Email failed");
    }

    const data = await resp.json();
    const confirmation = `I emailed your summary to ${data.to}.`;
    conversation.push({ role: "assistant", content: confirmation });
    appendTranscript(`Assistant: ${confirmation}`);
    setStatus("Idle");
    await speakReply(confirmation);
  } catch (err) {
    setStatus(`Error: ${err.message}`);
  }
}

recordBtn.addEventListener("click", async () => {
  try {
    if (!sessionActive) {
      sessionActive = true;
      try {
        await startRecording();
      } catch (err) {
        sessionActive = false;
        recordBtn.textContent = "Start Recording";
        throw err;
      }
    } else {
      sessionActive = false;
      if (activeAudio) {
        activeAudio.pause();
      }
      if (isRecording) {
        stopRecording();
      } else {
        recordBtn.textContent = "Start Recording";
        setStatus("Idle");
      }
    }
  } catch (err) {
    setStatus(`Error: ${err.message}`);
  }
});
