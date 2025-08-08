let allSpeakers = [];  // ← グローバルで保持

import { getGlobalConfig, updateGlobalConfig } from "./config.js";

/**
 * 音声設定の初期化（話者・スタイルセレクトボックス）
 */
export async function initVoiceSettings() {
  try {
    const res = await fetch("/v1/voice/speakers");
    const data = await res.json();
    if (!data.success || !Array.isArray(data.data)) {
      throw new Error("話者データが不正です");
    }

    allSpeakers = data.data;

    const speakerSelect = document.getElementById("speakerSelect");
    const styleSelect = document.getElementById("styleSelect");

    if (!speakerSelect || !styleSelect) {
      throw new Error("DOM要素が見つかりません");
    }

    // セレクトボックスを初期化
    speakerSelect.innerHTML = "";
    styleSelect.innerHTML = "";

    // 話者一覧を描画
    for (const speaker of allSpeakers) {
      if (typeof speaker.speaker_uuid !== "string" || typeof speaker.name !== "string") {
        console.warn("不正な話者データをスキップ:", speaker);
        continue;
      }
      const option = document.createElement("option");
      option.value = speaker.speaker_uuid;
      option.textContent = speaker.name;
      speakerSelect.appendChild(option);
    }

    // ✅ config から選択値を取得して反映
    const { speaker_uuid, style_id } = getGlobalConfig();

    if (allSpeakers.some(s => s.speaker_uuid === speaker_uuid)) {
      speakerSelect.value = speaker_uuid;
    } else {
      speakerSelect.selectedIndex = 0;
      updateGlobalConfig("speaker_uuid", speakerSelect.value);
    }

    // ✅ スタイルを更新（configの style_id を反映）
    updateStyleOptions(speakerSelect.value, style_id);

    // ✅ イベントリスナ登録
    speakerSelect.addEventListener("change", () => {
      const uuid = speakerSelect.value;
      updateStyleOptions(uuid);
      updateGlobalConfig("speaker_uuid", uuid);
    });

    styleSelect.addEventListener("change", () => {
      updateGlobalConfig("speaker_uuid", speakerSelect.value);
      updateGlobalConfig("style_id", parseInt(styleSelect.value));
    });

  } catch (err) {
    console.error("話者初期化失敗:", err);
  }
}

/**
 * 指定された speaker_uuid に対応するスタイル一覧を styleSelect に反映
 * @param {string} speaker_uuid 
 * @param {number|null} selectedStyleId 
 */
function updateStyleOptions(speaker_uuid, selectedStyleId = null) {
  const styleSelect = document.getElementById("styleSelect");
  if (!styleSelect) return;

  styleSelect.innerHTML = "";

  const speaker = allSpeakers.find(s => s.speaker_uuid === speaker_uuid);
  if (!speaker || !Array.isArray(speaker.styles)) {
    console.warn("対象の話者が見つからない、またはスタイルが不正:", speaker_uuid);
    return;
  }

  for (const style of speaker.styles) {
    if (typeof style.id !== "number" || typeof style.name !== "string") {
      console.warn("不正なスタイルデータをスキップ:", style);
      continue;
    }
    const option = document.createElement("option");
    option.value = style.id;
    option.textContent = style.name;
    styleSelect.appendChild(option);
  }

  // ✅ 選択状態を反映するが、初期ロード時のみselectedStyleId適用
  if (selectedStyleId !== null && speaker.styles.some(s => s.id === selectedStyleId)) {
    styleSelect.value = selectedStyleId;
  } else {
    styleSelect.selectedIndex = 0;
  }
}

/**
 * 単一音声合成（従来）— 互換のため残置
 */
export async function synthesizeSpeech(text, speaker_uuid, style_id) {
  try {
    const res = await fetch("/v1/voice/synthesize", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text, speaker_uuid, style_id }),
    });
    if (!res.ok) throw new Error(`音声合成失敗: ${res.status}`);
    const blob = await res.blob();
    return URL.createObjectURL(blob);
  } catch (err) {
    console.error("音声合成失敗:", err);
    return null;
  }
}

/**
 * まとめ合成（旧）— 仕様互換のため残置、使わない
 */
export async function synthesizeMultiSpeech(text, speaker_uuid, style_id) {
  try {
    const res = await fetch("/v1/voice/synthesize_multi", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text, speaker_uuid, style_id }),
    });

    const result = await res.json();
    if (!result.success || !Array.isArray(result.data)) {
      throw new Error("音声データ取得失敗");
    }

    return result.data.map(hexStr => {
      const bytes = new Uint8Array(hexStr.match(/.{1,2}/g).map(b => parseInt(b, 16)));
      return URL.createObjectURL(new Blob([bytes], { type: "audio/mp3" }));
    });

  } catch (err) {
    console.error("複数音声合成失敗:", err);
    return [];
  }
}

/* =============================
   ▼ ここから：順序保証のTTSワーカ（重要）
   - enqueueTtsSentence() で文を投入
   - 内部キューを1つずつ処理（逐次）
   - 各レスポンスのMP3は再生キューへ（到着順で再生）
   ============================= */
const __ttsReqQueue = [];
let __ttsWorking = false;

const __audioQueue = [];
let __isPlaying = false;

function __playQueue() {
  if (__isPlaying || __audioQueue.length === 0) return;
  __isPlaying = true;
  const url = __audioQueue.shift();
  const audio = new Audio(url);
  audio.onended = () => { __isPlaying = false; __playQueue(); };
  audio.onerror  = () => { __isPlaying = false; __playQueue(); };
  audio.play().catch(() => { __isPlaying = false; __playQueue(); });
}

async function __drainTtsQueue() {
  __ttsWorking = true;
  try {
    while (__ttsReqQueue.length) {
      const { text, speaker_uuid, style_id } = __ttsReqQueue.shift();
      await __synthesizeStreamOne(text, speaker_uuid, style_id); // ← 完了まで待つ（順序保証）
    }
  } finally {
    __ttsWorking = false;
  }
}

async function __synthesizeStreamOne(text, speaker_uuid, style_id) {
  try {
    const res = await fetch("/v1/voice/synthesize_stream", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text, speaker_uuid, style_id })
    });

    const reader = res.body.getReader();
    const decoder = new TextDecoder("utf-8");
    let buffer = "";

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n\n");
      buffer = lines.pop();

      for (const line of lines) {
        if (!line.startsWith("data: ")) continue;
        const data = JSON.parse(line.replace("data: ", ""));
        if (!data.mp3_b64) continue;

        const bytes = Uint8Array.from(atob(data.mp3_b64), c => c.charCodeAt(0));
        const url = URL.createObjectURL(new Blob([bytes], { type: "audio/mp3" }));
        __audioQueue.push(url);
        __playQueue();
      }
    }
  } catch (err) {
    console.error("TTSストリーム失敗:", err);
  }
}

/** 外部公開：文を順序キューに投入（即戻る） */
export function enqueueTtsSentence(text, speaker_uuid, style_id) {
  if (!text || !text.trim()) return;
  __ttsReqQueue.push({ text: text.trim(), speaker_uuid, style_id });
  if (!__ttsWorking) __drainTtsQueue();


