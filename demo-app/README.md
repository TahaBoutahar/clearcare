# Demo app — Prescription Explainer and Translator on a phone

A demo to try the explainer in hospitals from an iPhone (or any phone). Self-contained: its own
virtual environment, settings (`.env`) and logs (`data/`). It only *reads* the pipeline in the
project folder above and changes nothing there.

## What it does

1. **Type, paste or dictate** the prescription (dictation is turned into text on the Mac with
   Whisper; the recording is deleted right away).
2. **Explain**: how to take each medicine appears within seconds, then the full patient sheet.
3. **Patient language: English, Français, العربية.** Dose sentences come from fixed templates in
   each language; only short free text (what a medicine is for, the illness, the doctor's
   instructions) is translated by the AI, and only if the numbers stay exactly the same.
4. **Doctor review**: HIGH RISK / NOT SURE / CORRECTED / PRIVACY notes. If anything is uncertain
   or high-risk, the doctor must tick "I checked" before approving.
5. **Approve, edit or reject.** The approved sheet can be **shared** (WhatsApp, Messages…) or
   **printed**, stamped "Checked by your doctor".
6. **Audio**: *Listen* reads the sheet aloud with the phone's own voice (English, French or Arabic;
   offline, nothing is sent). *Audio file* makes an MP3 to keep or send by WhatsApp; it uses
   Google's text-to-speech, so the sheet (which has no names) is sent to Google.
7. **Stats**: approval rate, edits, rejections and reasons, time per prescription, whether the
   flags pointed at real problems. Only decisions are logged (`data/demo_log.jsonl`), never
   patient text.

## Run it

```bash
cd demo-app
./run.sh
```

It prints a **QR code**: scan it with the iPhone Camera (iPhone and Mac on the same Wi-Fi, or the
Mac connected to the iPhone's hotspot). Safari warns that the connection is not private because
the certificate is made on the Mac: tap *Show Details → visit this website → Visit Website* once.
Then *Share → Add to Home Screen* to open it like an app. HTTPS is needed for the microphone.

### From anywhere (walking around a hospital)

```bash
brew install cloudflared   # once
./run.sh --tunnel
```

This gives a temporary `https://….trycloudflare.com` link (no certificate warning) that works on
mobile data; the Mac must stay on. A PIN is always required in this mode; the text goes through
Cloudflare's encrypted tunnel, so use the same-Wi-Fi mode when possible.

## Settings (`demo-app/.env`)

| Setting | Meaning |
|---|---|
| `DEMO_PIN` | PIN asked on the phone (recommended; required with `--tunnel`) |
| `DEMO_PORT` | Port, default 8443 |
| `WHISPER_MODEL_SIZE` | `base` (fast) · `small` (default, better French) · `medium` |

The AI provider, model and keys come from the project's `.env`.

## Before real patients

- Have the French and Arabic templates in `translator.py` checked by a native-speaking health
  professional.
- Every sheet is reviewed and approved by the doctor; the demo is not a medical device.
- Do not type patient names; personal details are also removed automatically before the AI.
