// Isolated-world bridge: the MAIN-world hook cannot call chrome.* APIs, so it
// posts captured ticket values here. Relay them to the service worker, which
// validates the route and stores the key.
(() => {
  "use strict";

  const MESSAGE = "__geminiTicketCapture";

  window.addEventListener("message", (event) => {
    if (event.source !== window) return;
    const data = event.data;
    if (!data || data[MESSAGE] !== true || typeof data.value !== "string") return;
    try {
      chrome.runtime.sendMessage({ type: "captured-ticket", value: data.value });
    } catch {
      // The extension may have been reloaded while the page stayed open.
    }
  });
})();
