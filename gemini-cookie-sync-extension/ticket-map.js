// Shared ticket routing table for the service worker and the Node tests.
//
// This fork's server stores the *full* X-Goog-Ext-525001261-Jspb header value
// keyed by family (see gemini_web2api.py: _normalize_model_tickets). Index 4 is
// optional metadata (usually null in real captures); the upstream routes on the
// (family, variant) pair at indexes 14/15, which is what this table maps.
const TICKET_ROUTE_KEYS = {
  "1,1": "flash",
  "1,2": "flash-thinking",
  "1,4": "flash-medium",
  "3,1": "pro",
  "3,2": "pro-thinking",
  "3,4": "pro-medium",
  "5,1": "lite-thinking",
  "5,2": "lite-thinking",
  "6,1": "lite",
  "6,2": "lite-thinking",
  "6,4": "lite-medium",
  "8,1": "argon",
  "8,2": "argon-high",
  "8,4": "argon-medium"
};

function ticketParseDetail(headerVal) {
  if (!headerVal || typeof headerVal !== "string") {
    return { ok: false, reason: "no-header" };
  }
  if (headerVal.length > 8192) return { ok: false, reason: "too-long" };
  if (/[\r\n\0]/.test(headerVal)) return { ok: false, reason: "bad-chars" };
  let arr;
  try {
    arr = JSON.parse(headerVal);
  } catch {
    return { ok: false, reason: "bad-json" };
  }
  if (!Array.isArray(arr)) return { ok: false, reason: "not-array" };
  const family = arr[14];
  const variant = arr[15];
  if (!Number.isInteger(family) || !Number.isInteger(variant)) {
    return { ok: false, reason: `no-route(len=${arr.length})` };
  }
  const key = TICKET_ROUTE_KEYS[`${family},${variant}`];
  if (!key) return { ok: false, reason: `unknown-route(${family},${variant})` };
  return { ok: true, key, family, variant, header: headerVal.trim() };
}

function parseTicketHeader(headerVal) {
  const detail = ticketParseDetail(headerVal);
  return detail.ok ? { key: detail.key, header: detail.header } : null;
}

if (typeof module !== "undefined") {
  module.exports = { TICKET_ROUTE_KEYS, parseTicketHeader, ticketParseDetail };
}
