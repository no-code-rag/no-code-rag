import { getCurrentRoomId } from "./room-api.js";
import { getGlobalConfig } from "./config.js";
import { enqueueTtsSentence } from "./voice-api.js";

function extractModelName(modelId) {
  if (!modelId) return "";
  const parts = modelId.split("/");
  const filename = parts[parts.length - 1];
  return filename.replace(/\.gguf$/, "");
}

export function initChatEvents() {
  const input = document.getElementById("user-input");
  const sendBtn = document.getElementById("send-btn");
  if (!input || !sendBtn) return;

  input.addEventListener("input", () => {
    input.style.height = "auto";
    input.style.height = input.scrollHeight + "px";
    scrollToBottom();
  });

  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      sendBtn.click();
    }
  });

  sendBtn.addEventListener("click", async () => {
    const text = input.value.trim();
    const roomId = getCurrentRoomId();
    if (!text || !roomId) return;

    const { model, speaker_uuid, style_id } = getGlobalConfig();
    if (!model || !speaker_uuid || style_id == null) {
      appendMessage("assistant", "[設定エラー] モデル・話者・スタイルを選択してください");
      return;
    }

    input.value = "";
    input.style.height = "auto";
    await sendChatMessage(text, model, speaker_uuid, style_id, roomId);
  });

  const recordBtn = document.getElementById("record-btn");
  if (recordBtn) {
    let mediaRecorder;
    let audioChunks = [];

    recordBtn.addEventListener("click", async () => {
      if (!mediaRecorder || mediaRecorder.state === "inactive") {
        const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
        mediaRecorder = new MediaRecorder(stream);
        audioChunks = [];

        mediaRecorder.ondataavailable = (e) => audioChunks.push(e.data);

        mediaRecorder.onstop = async () => {
          const audioBlob = new Blob(audioChunks, { type: "audio/webm" });
          const formData = new FormData();
          formData.append("file", audioBlob, "recorded_audio.webm");

          input.placeholder = "文字起こし中...";

          try {
            const res = await fetch("/v1/audio/transcribe", {
              method: "POST",
              body: formData,
            });

            const data = await res.json();
            if (data.text) {
              input.value = data.text;
              sendBtn.click();
            } else {
              appendMessage("assistant", `[文字起こしエラー]: ${data.error || "不明なエラー"}`);
            }
          } catch (err) {
            appendMessage("assistant", `[通信エラー]: ${err.message}`);
          } finally {
            input.placeholder = "メッセージを入力...";
          }
        };

        mediaRecorder.start();
        recordBtn.textContent = "⏹ 停止";
      } else {
        mediaRecorder.stop();
        recordBtn.textContent = "🎙️";
      }
    });
  }
}

function scrollToBottom() {
  const container = document.getElementById("chat-messages");
  if (!container) return;
  container.scrollTop = container.scrollHeight;
}

function generateSafeId() {
  return `msg-${Date.now()}-${Math.floor(Math.random() * 1000000)}`;
}

async function sendChatMessage(text, model, speaker_uuid, style_id, roomId) {
  appendMessage("user", text);

  const messageId = appendMessage("assistant", "[生成中…]", extractModelName(model));

  // 🔥 refine_query API は削除。元の入力文をそのまま使う。
  const refinedQuery = text;

  const { prompt_id, rag_mode } = getGlobalConfig();

  const payload = {
    model: typeof model === "object" ? model.id : model,
    messages: [{ role: "user", content: refinedQuery }],
    stream: true,
    prompt_id: prompt_id || "rag_default",
    rag_mode: rag_mode || "use",
    speaker_uuid: speaker_uuid,
    style_id: style_id,
    room_id: roomId,
  };

  const res = await fetch("/v1/chat/completions", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });

  if (!res.ok) {
    updateAssistantText(`[llama エラー] status: ${res.status}`, messageId);
    return;
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder("utf-8");
  let assistantText = "";
  let buffer = "";

  // ▼ TTS用の未確定バッファ
  let ttsBuf = "";
  const re = /(.+?[。！？\n])/;

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;

    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split("\n");
    buffer = lines.pop();

    for (const line of lines) {
      if (!line.startsWith("data: ")) continue;
      const chunk = line.replace("data: ", "").trim();
      if (chunk === "[DONE]") continue;

      try {
        const parsed = JSON.parse(chunk);
        const delta = parsed.choices?.[0]?.delta?.content || "";
        if (!delta) continue;

        // 表示
        assistantText += delta;
        updateAssistantText(assistantText, messageId);

        // ▼ 文確定 → 順序キューへ投入（非並列）
        ttsBuf += delta;
        let m;
        while ((m = re.exec(ttsBuf))) {
          const sentence = m[1];
          ttsBuf = ttsBuf.slice(m.index + sentence.length);
          enqueueTtsSentence(sentence, speaker_uuid, style_id);
        }

        await new Promise(requestAnimationFrame);
      } catch (e) {
        console.warn("ストリーム解析失敗:", e);
      }
    }
  }

  // 末尾が句点で終わらなかった分も最後に読む
  if (ttsBuf.trim()) {
    enqueueTtsSentence(ttsBuf.trim(), speaker_uuid, style_id);
    ttsBuf = "";
  }

  await fetch("/v1/chat/messages", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      room_id: roomId,
      message: {
        role: "assistant",
        content: assistantText,
        model,
        speaker_uuid,
        style_id,
      },
    }),
  });
}

function appendMessage(role, content, model = "") {
  const chat = document.getElementById("chat-messages");
  const wrapper = document.createElement("div");
  wrapper.className = role;

  const bubble = document.createElement("div");
  bubble.className = "bubble";

  const messageId = generateSafeId();
  bubble.id = messageId;

  if (role === "assistant" && model) {
    const m = document.createElement("div");
    m.className = "model-name";
    m.textContent = model;
    m.dataset.model = model;
    bubble.appendChild(m);
  }

  const t = document.createElement("div");
  t.className = "message-text";
  t.textContent = content;
  bubble.appendChild(t);

  wrapper.appendChild(bubble);
  chat.appendChild(wrapper);
  scrollToBottom();

  return messageId;
}

function updateAssistantText(content, messageId) {
  const bubble = document.getElementById(messageId);
  if (!bubble) return;

  const messageText = bubble.querySelector(".message-text");
  if (messageText) {
    messageText.textContent = "\n" + content.trim();
    requestAnimationFrame(() => {
      setTimeout(scrollToBottom, 0);
    });
  }
}

const chatObserver = new MutationObserver(() => scrollToBottom());
const chatContainer = document.getElementById("chat-messages");
if (chatContainer) {
  chatObserver.observe(chatContainer, {
    childList: true,
    subtree: true,
    characterData: true,
  });
}




