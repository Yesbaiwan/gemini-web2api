// Shared ticket routing table for the service worker and the Node tests.
//
// This fork's server stores the *full* X-Goog-Ext-525001261-Jspb header value
// keyed by family (see gemini_web2api.py: _normalize_model_tickets). Index 4 is
// optional metadata (usually null in real captures); the upstream routes on the
// (family, variant) pair at indexes 14/15, which is what this table maps.
const TICKET_ROUTE_KEYS = {
  "1,1": "flash",
  "1,2": "flash-thinking",
  "3,1": "pro",
  "3,2": "pro-thinking",
  "5,1": "lite-thinking",
  "5,2": "lite-thinking",
  "6,1": "lite",
  "6,2": "lite-thinking",
  "6,4": "lite-medium"
};

function parseTicketHeader(headerVal) {
  if (!headerVal || typeof headerVal !== "string") return null;
  if (headerVal.length > 8192 || /[\r\n\0]/.test(headerVal)) return null;
  try {
    const arr = JSON.parse(headerVal);
    if (!Array.isArray(arr)) return null;
    if (!Number.isInteger(arr[14]) || !Number.isInteger(arr[15])) return null;
    const key = TICKET_ROUTE_KEYS[`${arr[14]},${arr[15]}`];
    if (!key) return null;
    return { key, header: headerVal.trim() };
  } catch {
    return null;
  }
}

if (typeof module !== "undefined") {
  module.exports = { TICKET_ROUTE_KEYS, parseTicketHeader };
}
