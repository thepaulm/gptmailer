const recordBtn = document.getElementById("recordBtn");
const statusEl = document.getElementById("status");
const transcriptEl = document.getElementById("transcript");

let recorder = null;
let chunks = [];
let isRecording = false;
let conversation = [];

const TRIGGER_RE = /\b(email|send)\s+(me\s+)?(a\s+)?summary\b/i;
const EMAIL_RE = /[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}/;

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

function conversationText() {
  return conversation.map((t) => `User: ${t}`).join("\n");
}

async function startRecording() {
  const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  const options = { mimeType: "audio/webm" };
  recorder = new MediaRecorder(stream, options);

  chunks = [];
  recorder.ondataavailable = (e) => {
    if (e.data.size > 0) chunks.push(e.data);
  };

  recorder.onstop = async () => {
    const blob = new Blob(chunks, { type: "audio/webm" });
    await sendForTranscription(blob);
    stream.getTracks().forEach((t) => t.stop());
  };

  recorder.start();
  isRecording = true;
  recordBtn.textContent = "Stop Recording";
  setStatus("Recording...");
}

function stopRecording() {
  if (recorder && isRecording) {
    recorder.stop();
  }
  isRecording = false;
  recordBtn.textContent = "Start Recording";
  setStatus("Processing...");
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

    conversation.push(text);
    appendTranscript(text);
    setStatus("Idle");

    if (TRIGGER_RE.test(text)) {
      await sendSummaryEmail(text);
    }
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
    appendTranscript(`(Summary emailed to ${data.to})`);
    setStatus("Idle");
  } catch (err) {
    setStatus(`Error: ${err.message}`);
  }
}

recordBtn.addEventListener("click", async () => {
  try {
    if (!isRecording) {
      await startRecording();
    } else {
      stopRecording();
    }
  } catch (err) {
    setStatus(`Error: ${err.message}`);
  }
});
