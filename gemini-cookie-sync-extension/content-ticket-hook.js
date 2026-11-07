// MAIN-world hook: observe Gemini's own fetch/XHR calls for the model ticket
// header. chrome.webRequest is the primary capture path; this covers cases
// where the network API does not expose the header (or the request comes from
// the page's own service worker in a browser that hides it).
//
// This script runs in the page's JS context, so it cannot call chrome.* APIs.
// It forwards captured values with window.postMessage to the isolated-world
// bridge (content-ticket-bridge.js), which relays them to the service worker.
(() => {
  "use strict";

  const HEADER = "x-goog-ext-525001261-jspb";
  const MESSAGE = "__geminiTicketCapture";
  const seen = new Set();

  function report(value) {
    if (typeof value !== "string" || !value) return;
    const trimmed = value.trim();
    if (!trimmed || seen.has(trimmed)) return;
    if (seen.size > 200) seen.clear();
    seen.add(trimmed);
    try {
      window.postMessage({ [MESSAGE]: true, value: trimmed }, "*");
    } catch {
      // Never break the page over a capture failure.
    }
  }

  function headerValue(headers) {
    try {
      if (!headers) return null;
      if (typeof headers.get === "function") {
        return headers.get(HEADER) || headers.get("X-Goog-Ext-525001261-Jspb");
      }
      if (Array.isArray(headers)) {
        for (const pair of headers) {
          if (pair && String(pair[0]).toLowerCase() === HEADER) return pair[1];
        }
        return null;
      }
      for (const name of Object.keys(headers)) {
        if (String(name).toLowerCase() === HEADER) return headers[name];
      }
    } catch {
      // Ignore malformed header bags.
    }
    return null;
  }

  try {
    const originalFetch = window.fetch;
    if (typeof originalFetch === "function") {
      window.fetch = function (input, init) {
        try {
          if (input && typeof input === "object" && input.headers &&
              typeof input.headers.get === "function") {
            report(input.headers.get(HEADER));
          }
          report(headerValue(init && init.headers));
        } catch {
          // Ignore capture errors; the request must always proceed.
        }
        return originalFetch.apply(this, arguments);
      };
    }
  } catch {
    // Page froze window.fetch; the webRequest path still applies.
  }

  try {
    const proto = window.XMLHttpRequest && window.XMLHttpRequest.prototype;
    if (proto && typeof proto.setRequestHeader === "function") {
      const originalSet = proto.setRequestHeader;
      proto.setRequestHeader = function (name, value) {
        try {
          if (String(name).toLowerCase() === HEADER) report(String(value));
        } catch {
          // Ignore capture errors.
        }
        return originalSet.apply(this, arguments);
      };
    }
  } catch {
    // Page froze XMLHttpRequest; the webRequest path still applies.
  }
})();
