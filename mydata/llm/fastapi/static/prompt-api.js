// prompt-api.js

import { getGlobalConfig, updateGlobalConfig } from "./config.js";

export async function fetchPrompts() {
  try {
    const response = await fetch("/v1/config/prompt/list");  // ✅ 正しいURLに修正
    const data = await response.json();
    if (!data.success || !Array.isArray(data.data)) return [];
    return data.data;
  } catch (err) {
    console.error("プロンプト取得失敗:", err);
    return [];
  }
}

export async function fetchRagModes() {
  try {
    const res = await fetch("/v1/config/rag_prompt/list");  // ← これはOK
    const data = await res.json();
    if (!data.success || !Array.isArray(data.data)) return [];
    return data.data;
  } catch (err) {
    console.error("RAGモード取得失敗:", err);
    return [];
  }
}

export async function initPromptSelect() {
  const select = document.getElementById("promptSelect");
  if (!select) return;

  const prompts = await fetchPrompts();
  prompts.forEach((prompt) => {
    const opt = document.createElement("option");
    opt.value = prompt.id;
    opt.textContent = prompt.name;
    select.appendChild(opt);
  });

  const current = getGlobalConfig().prompt_id;
  select.value = current || "rag_default";

  select.addEventListener("change", (e) => {
    updateGlobalConfig("prompt_id", e.target.value);
  });
}

export async function initRagModeSelect() {
  const select = document.getElementById("ragModeSelect");
  if (!select) return;

  const modes = await fetchRagModes();
  modes.forEach((mode) => {
    const opt = document.createElement("option");
    opt.value = mode.id;
    opt.textContent = mode.name;
    select.appendChild(opt);
  });

  const current = getGlobalConfig().rag_mode;
  select.value = current || (modes.length > 0 ? modes[0].id : "");

  select.addEventListener("change", (e) => {
    updateGlobalConfig("rag_mode", e.target.value);
  });
}



