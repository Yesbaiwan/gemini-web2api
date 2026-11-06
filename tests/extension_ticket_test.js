#!/usr/bin/env node
/**
 * Extension ticket-capture regression test.
 *
 * The upstream routes on the full X-Goog-Ext-525001261-Jspb header, and real
 * captures usually have null at index 4; the (family, variant) pair at indexes
 * 14/15 is what identifies the model. These are the live-captured shapes for
 * Gemini 3.5 Flash-Lite low/medium/high.
 *
 * Run it with:
 *
 *     node tests/extension_ticket_test.js
 *
 * It exits 0 when everything passes and 1 otherwise. No network access needed.
 */

const { TICKET_ROUTE_KEYS, parseTicketHeader } =
  require("../gemini-cookie-sync-extension/ticket-map.js");

const HIGH = '[1,null,null,null,"cf41b0e0dd7d53e5",null,"en",null,[4,33,22,5,6,8,16,19,20],null,null,null,null,null,6,2,"BED71B1C-F2D7-410A-913F-B1415D1452C6",["boq_gemini-web-uiserver_20261105.19_p0"],null,[[null,491200000],[1793982546,474000000],"Asia/Bangkok"]]';
const MEDIUM = '[1,null,null,null,"cf41b0e0dd7d53e5",null,"en",null,[4,33,22,5,6,8,16,19,20],null,null,null,null,null,6,4,"BED71B1C-F2D7-410A-913F-B1415D1452C6",["boq_gemini-web-uiserver_20261105.19_p0"],null,[[null,536200000],[1793982759,780000000],"Asia/Bangkok"]]';
const LOW = '[1,null,null,null,"cf41b0e0dd7d53e5",null,"en",null,[4,33,22,5,6,8,16,19,20],null,null,null,null,null,6,1,"BED71B1C-F2D7-410A-913F-B1415D1452C6",["boq_gemini-web-uiserver_20261105.19_p0"],null,[[null,496900000],[1793982856,15000000],"Asia/Bangkok"]]';

const withNullTicket = (ticket) => ticket.replace('"cf41b0e0dd7d53e5"', "null");

let failures = 0;
const check = (name, actual, expected) => {
  const ok = JSON.stringify(actual) === JSON.stringify(expected);
  if (!ok) failures++;
  console.info((ok ? "PASS " : "FAIL ") + name +
    (ok ? "" : " -- got " + JSON.stringify(actual) + ", want " + JSON.stringify(expected)));
};

check("route table has lite low/medium/high",
      [TICKET_ROUTE_KEYS["6,1"], TICKET_ROUTE_KEYS["6,4"], TICKET_ROUTE_KEYS["6,2"]],
      ["lite", "lite-medium", "lite-thinking"]);
check("legacy (5,1) still maps to lite-thinking",
      TICKET_ROUTE_KEYS["5,1"], "lite-thinking");

check("null index 4 + (6,1) -> lite",
      parseTicketHeader(withNullTicket(LOW)).key, "lite");
check("null index 4 + (6,4) -> lite-medium",
      parseTicketHeader(withNullTicket(MEDIUM)).key, "lite-medium");
check("null index 4 + (6,2) -> lite-thinking",
      parseTicketHeader(withNullTicket(HIGH)).key, "lite-thinking");

check("hex index 4 is still accepted",
      parseTicketHeader(LOW).key, "lite");
check("captured header value is preserved",
      parseTicketHeader(LOW).header, LOW);

check("malformed JSON -> null", parseTicketHeader("not json"), null);
check("non-array JSON -> null", parseTicketHeader('{"14":6,"15":1}'), null);
check("too-short array -> null", parseTicketHeader('[1,null,null,null,"cf41b0e0dd7d53e5"]'), null);
check("unknown route -> null", parseTicketHeader(withNullTicket(LOW).replace(",6,1,", ",6,9,")), null);
check("non-string input -> null", parseTicketHeader(null), null);
check("CRLF input -> null", parseTicketHeader(withNullTicket(LOW) + "\r\n"), null);

console.info(failures === 0 ? "\nAll extension ticket checks passed."
                            : "\n" + failures + " check(s) failed.");
process.exit(failures === 0 ? 0 : 1);
