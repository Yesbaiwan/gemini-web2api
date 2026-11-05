Gemini Cookie Sync v1.1

Purpose:
- Read cookies for the current Google/Gemini session.
- Extract the XSRF token named SNlM0e from the Gemini page.
- Extract gemini_bl from cfb2h or from page requests when available.
- Export `gemini-auth.json` locally only.
- Auto-sync rotated session cookies to a local gemini-web2api server at
  `http://127.0.0.1:8081/v1/cookies` (no native-messaging host required).

Auto-sync details:
- The background worker watches Google auth cookies (SID, SAPISID,
  __Secure-1PSID, __Secure-1PSIDTS, ...), debounces rotation bursts, and also
  syncs every 10 minutes so a restarted server recovers the current session.
- Model routing tickets (`X-Goog-Ext-525001261-Jspb`) are captured from
  StreamGenerate requests and synced as full header values keyed by family
  (`flash`, `pro`, `lite`, ...), matching this fork's `model_tickets` format.
- The server must have `cookie_file` pointed at a writable JSON file if the
  rotation should survive a server restart. Without `cookie_file` the update
  still applies in memory.

Installation:
1. Open `chrome://extensions`
2. Enable Developer mode
3. Click Load unpacked
4. Select this folder
5. Open `https://gemini.google.com/app`, sign in, and refresh the page
6. Click Inspect session
7. Click Export `gemini-auth.json` (this also pushes the payload to the local
   server when it is running)

Security:
The generated file represents the real Google session and must be treated as secret. Do not send it, print it, or commit it to Git.
