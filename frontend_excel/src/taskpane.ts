/**
 * Excel task pane entry — Office.onReady → Copilot chat workspace.
 */

import { initCopilot } from "./copilot";

const statusEl = document.getElementById("status");

function setStatus(text: string): void {
  if (statusEl) statusEl.textContent = text;
}

Office.onReady((info) => {
  if (info.host === Office.HostType.Excel) {
    initCopilot();
  } else {
    setStatus(
      `Host is ${info.host ?? "unknown"} — open inside Excel for full behavior`,
    );
  }
});
