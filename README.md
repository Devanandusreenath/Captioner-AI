# Captioner — AI-Assisted Meme / Photo Caption Tool

A small full-stack web app that captions photos intelligently: it doesn't just
slap fixed white text at the top and bottom of an image — it decides *where*
text should go, *what size* it should be, and *what color* keeps it readable,
and it can hand that entire decision to a vision-language model (Gemini) if
you'd rather not write the joke yourself.

Built as a self-contained Flask app with server-side Pillow rendering, so
there's no client-side canvas trickery and no external image-generation
service involved — every pixel in the output comes from your own photo plus
real, bundled TrueType fonts.

---

## Why this exists

Most "meme generator" tools make three assumptions that don't hold up in
practice:

1. **There are always exactly two captions** (top and bottom).
2. **White text with a black outline always looks fine.**
3. **You have to write the joke yourself.**

All three are wrong often enough to be annoying. A single-line joke forced
into a top+bottom template looks empty. White text disappears against a
bright sky or a white shirt. And sometimes you just want to upload a photo
and get something funny back without composing the line yourself.

This project tackles each of those directly.

---

## Core features

### 1. Flexible caption layout (not a fixed top/bottom pair)
Captions are a list, not two hardcoded fields. Each caption independently has:
- **Position** — `top`, `middle`, or `bottom`
- **Emphasis** — `header` (large) or `subheader` (smaller, for a tagline
  under a headline)

You can have just one caption, three stacked at the top, a header+subheader
pair, or a single centered line — the layout engine stacks whatever you give
it within each zone automatically, sizing and spacing each entry based on
its emphasis.

### 2. Brightness-aware auto-contrast text
Before drawing each caption, the app samples the actual pixel luminance of
the image *behind that specific caption* (a horizontal band around where the
text will sit) and picks black-on-white-outline or white-on-black-outline
accordingly — independently, per caption. A caption over a dark sky and
another over a bright field in the *same* photo both stay legible without
you touching a color picker. This is a simple, deliberate use of "computer
vision" (luminance analysis) rather than a decorative label for it — it's
the thing that actually makes captions reliably readable on arbitrary
photos, which fixed-color meme tools don't do.

Manual override is available: uncheck auto-contrast and pick your own fill
and outline colors if you want a specific look regardless of the photo.

### 3. AI-generated caption *and* layout via Gemini vision
The "Generate" button sends your actual uploaded photo (not a text
description of it) to Gemini's vision API and asks it to:
- Write a short, punchy caption relevant to what's actually in the photo
  (optionally steered by a topic/angle you type in), and
- **Decide the layout itself** — how many caption lines are needed and
  where each goes — rather than being forced into always returning a
  top+bottom pair. A photo that only needs one line gets one line back.

The model is asked to respond with a strict JSON array (`text`, `position`,
`emphasis` per object), which the backend parses and uses to populate the
caption list — you can then edit any of it by hand.

### 4. Real bundled display fonts
Rather than relying on the browser's CSS font loading (which doesn't help
once rendering happens in Pillow), five actual `.ttf` files ship in
`fonts/`: Anton, Bebas Neue, Permanent Marker, Caveat, plus a DejaVu Sans
Bold fallback. What you see in the live preview is exactly what's in the
downloaded PNG, because both come from the same Pillow render call.

### 5. Server-side rendering
Upload happens once; the backend stores a resized copy of your original
image in memory, keyed by a generated ID. Every subsequent edit (typing a
caption, moving a slider, changing font) triggers a debounced call to
`/render`, which re-draws captions onto a **fresh copy** of the untouched
original and returns a PNG. This avoids the classic bug of captions
accumulating on top of each other, since the source image is never mutated.

---

## Architecture

```
Browser (single HTML page, vanilla JS)
   │
   │  POST /upload         (multipart image)              → stores original in memory, returns preview
   │  POST /render         (captions + style JSON)         → returns rendered PNG
   │  POST /ai_caption     (image_id + Gemini API key)      → returns caption list JSON
   │
Flask backend (app.py)
   │
   ├── PIL/Pillow — image loading, resizing, text layout, drawing
   ├── requests   — calls Gemini's generateContent REST endpoint directly
   └── In-memory dict — { image_id: PIL.Image }  (no database, no disk writes)
```

There is no database and no persistent storage: everything lives in the
Flask process's memory for the lifetime of that run, which is appropriate
for a local single-user tool and explicitly not intended as a multi-user
production deployment (see Limitations).

---

## Tech stack

| Layer | Choice | Why |
|---|---|---|
| Backend | Flask | Minimal, single-file, easy to read and modify |
| Image processing | Pillow (PIL) | Text layout, stroke/fill drawing, resizing |
| AI vision | Google Gemini (`gemini-flash-latest`) via REST | Free tier, native multimodal input, no SDK dependency — just `requests` |
| Frontend | Vanilla HTML/CSS/JS | No build step, no framework, works by opening one URL |
| Fonts | Anton, Bebas Neue, Permanent Marker, Caveat (Google Fonts, OFL-licensed) | Real display fonts bundled as files so server-rendered output matches what a proper meme/poster tool would use |

---

## What's genuinely new here vs. a typical meme generator

To be direct about it, since not every feature is equally novel:

- Font pickers and color pickers are standard — not the interesting part.
- **The per-caption auto-contrast** and **the AI-decided variable-length
  layout** are the two design choices that solve real, specific problems
  (illegible text on mixed-brightness photos; being locked into a rigid
  two-slot template) rather than just adding configuration surface area.

---

## Setup

```bash
unzip meme_app.zip && cd meme_app
pip install -r requirements.txt
python app.py
```

Open **http://127.0.0.1:5000**. Get a free Gemini API key at
[aistudio.google.com/apikey](https://aistudio.google.com/apikey) if you want
to use the Generate button — manual captioning works with no key at all.

---

## Limitations / honest caveats

- **Not multi-user safe.** Uploaded images are held in a single in-process
  Python dict with no expiry, no per-user isolation, and no cleanup. Fine
  for one person running it locally; not fine for a public deployment.
- **Flask's built-in dev server** is used (`app.run()`), which is
  explicitly not meant for production traffic. Swapping in gunicorn/uwsgi
  behind a real WSGI setup would be a prerequisite for deploying this
  anywhere other than `localhost`.
- **API key handling.** The key is typed into the page and sent to your own
  local server for that one request; it is never written to disk by this
  app. If you were to deploy this publicly, you'd want a proper
  server-side secret instead of a client-supplied key per request.
- **Gemini's free tier has rate limits.** Heavy use of the Generate button
  will eventually hit them.

---

## Possible extensions

- Persisting uploads to disk (or S3) with expiry, so the app survives a
  restart and can support more than one concurrent user.
- Drag-to-reposition captions instead of fixed top/middle/bottom zones.
- A history/undo stack for caption edits.
- Batch mode: caption a folder of photos in one pass using the same AI
  prompt/topic.
