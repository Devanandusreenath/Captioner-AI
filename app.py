"""
Meme Captioner — Flask web app
================================
Upload a photo, add flexible captions (each with its own position and size),
optionally let Gemini decide the wording + layout, and render/download the
result — all server-side.

Run:
    pip install -r requirements.txt
    python app.py
Then open http://127.0.0.1:5000

Your Gemini API key is entered in the page itself and only lives in memory
for that request — it is never written to disk by this app.
Get a free key at https://aistudio.google.com/apikey
"""

import base64
import io
import json
import os
import uuid

import requests
from flask import Flask, request, jsonify, render_template_string, send_file
from PIL import Image, ImageDraw, ImageFont

app = Flask(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FONT_DIR = os.path.join(BASE_DIR, "fonts")

FONTS = {
    "plex":    None,  # falls back to system DejaVu Sans Bold
    "anton":   os.path.join(FONT_DIR, "Anton-Regular.ttf"),
    "bebas":   os.path.join(FONT_DIR, "BebasNeue-Regular.ttf"),
    "marker":  os.path.join(FONT_DIR, "PermanentMarker-Regular.ttf"),
    "caveat":  os.path.join(FONT_DIR, "Caveat-Bold.ttf"),
}
SYSTEM_FALLBACKS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
]

# In-memory store of uploaded originals, keyed by a per-upload id.
# Fine for a small local/single-user tool; not meant for multi-user production use.
IMAGES = {}

MAX_DIM = 1200


def load_font(size, font_key):
    path = FONTS.get(font_key)
    candidates = [path] + SYSTEM_FALLBACKS
    for p in candidates:
        if p and os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                continue
    return ImageFont.load_default()


def band_luminance(image, y0, y1):
    w, h = image.size
    y0 = max(0, int(y0))
    y1 = min(h, int(y1))
    if y1 <= y0:
        return 128
    small = image.resize((max(1, w // 6), max(1, (y1 - y0) // 3 or 1)), box=(0, y0, w, y1))
    pixels = list(small.convert("RGB").getdata())
    if not pixels:
        return 128
    total = sum(0.299 * r + 0.587 * g + 0.114 * b for r, g, b in pixels)
    return total / len(pixels)


def draw_caption_at(draw, image, text, font_size, y_center, font_key,
                     auto_contrast, fill_color, stroke_color):
    if not text.strip():
        return
    w, h = image.size
    size = font_size
    font = load_font(size, font_key)

    bbox = draw.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    while tw > w * 0.92 and size > 10:
        size -= 2
        font = load_font(size, font_key)
        bbox = draw.textbbox((0, 0), text, font=font)
        tw = bbox[2] - bbox[0]

    if auto_contrast:
        lum = band_luminance(image, y_center - size * 0.7, y_center + size * 0.7)
        fill, stroke = ((0, 0, 0), (255, 255, 255)) if lum > 150 else ((255, 255, 255), (0, 0, 0))
    else:
        fill, stroke = fill_color, stroke_color

    draw.text((w / 2, y_center), text, font=font, fill=fill,
               stroke_width=max(2, size // 14), stroke_fill=stroke, anchor="mm")


def layout_and_draw(image, captions, base_font_size, font_key, auto_contrast, fill_color, stroke_color):
    draw = ImageDraw.Draw(image)
    w, h = image.size
    margin = base_font_size * 0.7

    def size_for(cap):
        return round(base_font_size * 0.55) if cap["emphasis"] == "subheader" else base_font_size

    for zone in ("top", "middle", "bottom"):
        entries = [c for c in captions if c["position"] == zone and c["text"].strip()]
        if not entries:
            continue
        sizes = [size_for(c) for c in entries]
        pitches = [s * 1.15 for s in sizes]
        total = sum(pitches)

        if zone == "top":
            y = margin + pitches[0] / 2
        elif zone == "bottom":
            y = h - margin - total + pitches[0] / 2
        else:
            y = h / 2 - total / 2 + pitches[0] / 2

        for i, cap in enumerate(entries):
            draw_caption_at(draw, image, cap["text"].upper(), sizes[i], y, font_key,
                             auto_contrast, fill_color, stroke_color)
            nxt = pitches[i + 1] if i + 1 < len(pitches) else pitches[i]
            y += (pitches[i] + nxt) / 2

    return image


def image_to_png_bytes(image):
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    buf.seek(0)
    return buf


# ---------------- routes ----------------

@app.route("/upload", methods=["POST"])
def upload():
    file = request.files.get("image")
    if not file:
        return jsonify({"error": "No image provided"}), 400

    image = Image.open(file.stream).convert("RGB")
    w, h = image.size
    if max(w, h) > MAX_DIM:
        scale = MAX_DIM / max(w, h)
        image = image.resize((round(w * scale), round(h * scale)))

    image_id = uuid.uuid4().hex
    IMAGES[image_id] = image

    buf = io.BytesIO()
    image.save(buf, format="JPEG", quality=85)
    preview_b64 = base64.b64encode(buf.getvalue()).decode("ascii")

    return jsonify({
        "image_id": image_id,
        "width": image.width,
        "height": image.height,
        "preview": preview_b64,
    })


@app.route("/render", methods=["POST"])
def render():
    data = request.get_json(force=True)
    image_id = data.get("image_id")
    base = IMAGES.get(image_id)
    if base is None:
        return jsonify({"error": "Unknown image_id — upload again."}), 400

    img = base.copy()
    captions = data.get("captions", [])
    font_key = data.get("font", "plex")
    font_size = int(data.get("font_size", 70))
    auto_contrast = bool(data.get("auto_contrast", True))
    fill_color = tuple(data.get("fill_color", [255, 255, 255]))
    stroke_color = tuple(data.get("stroke_color", [0, 0, 0]))

    layout_and_draw(img, captions, font_size, font_key, auto_contrast, fill_color, stroke_color)
    return send_file(image_to_png_bytes(img), mimetype="image/png")


@app.route("/ai_caption", methods=["POST"])
def ai_caption():
    data = request.get_json(force=True)
    image_id = data.get("image_id")
    api_key = (data.get("api_key") or "").strip()
    topic = (data.get("topic") or "").strip()

    base = IMAGES.get(image_id)
    if base is None:
        return jsonify({"error": "Unknown image_id — upload again."}), 400
    if not api_key:
        return jsonify({"error": "Paste your Gemini API key first."}), 400

    buf = io.BytesIO()
    base.save(buf, format="JPEG", quality=85)
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")

    angle_note = f'Try to relate the joke to this angle if it fits naturally: "{topic}".' if topic else ""
    prompt = (
        "Look at this photo and write meme caption(s) for it. "
        f"{angle_note} Decide the layout yourself based on what makes the joke land "
        "best: it might be a single line, a top header plus a smaller subheader right "
        "under it, a bottom-only line, or a centered line — don't default to always "
        "filling both a top and bottom slot if the joke only needs one line. Respond "
        'with ONLY a JSON array, no markdown fences, no other text, in exactly this '
        'shape: [{"text":"SHORT LINE", "position":"top|middle|bottom", '
        '"emphasis":"header|subheader"}]. Use 1 to 3 caption objects. Keep each line '
        "under 8 words, punchy, all caps in the text."
    )

    try:
        resp = requests.post(
            "https://generativelanguage.googleapis.com/v1beta/models/gemini-flash-latest:generateContent",
            headers={"x-goog-api-key": api_key, "content-type": "application/json"},
            json={"contents": [{"parts": [
                {"inline_data": {"mime_type": "image/jpeg", "data": b64}},
                {"text": prompt},
            ]}]},
            timeout=60,
        )
        if not resp.ok:
            try:
                msg = resp.json().get("error", {}).get("message", f"HTTP {resp.status_code}")
            except Exception:
                msg = f"HTTP {resp.status_code}"
            return jsonify({"error": msg}), 400

        rdata = resp.json()
        parts = (rdata.get("candidates") or [{}])[0].get("content", {}).get("parts", [])
        raw = "".join(p.get("text", "") for p in parts)
        clean = raw.replace("```json", "").replace("```", "").strip()
        parsed = json.loads(clean)

        captions = []
        for item in parsed:
            position = item.get("position") if item.get("position") in ("top", "middle", "bottom") else "top"
            emphasis = "subheader" if item.get("emphasis") == "subheader" else "header"
            captions.append({"text": item.get("text", ""), "position": position, "emphasis": emphasis})
        if not captions:
            captions = [{"text": "", "position": "top", "emphasis": "header"}]

        return jsonify({"captions": captions})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


PAGE = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Captioner — meme maker</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500&display=swap');
  :root{
    --bg:#181416; --panel:#211c1e; --panel-2:#2a2426; --border:#3a3234;
    --text:#f0ebe9; --text-dim:#9c8f8c; --accent:#e8574a;
  }
  *{box-sizing:border-box;}
  body{margin:0;background:var(--bg);color:var(--text);font-family:'IBM Plex Sans',sans-serif;min-height:100vh;display:flex;flex-direction:column;}
  header{padding:16px 20px;border-bottom:1px solid var(--border);display:flex;align-items:baseline;gap:10px;}
  header h1{font-size:16px;font-weight:700;margin:0;}
  header span{font-size:12px;color:var(--text-dim);}
  .layout{display:flex;flex:1;min-height:0;flex-wrap:wrap;}
  .stage{flex:1 1 500px;display:flex;align-items:center;justify-content:center;padding:24px;min-height:420px;}
  .stage-inner{position:relative;max-width:100%;border-radius:6px;overflow:hidden;background:#0f0d0e;}
  img#preview{display:block;max-width:100%;max-height:70vh;}
  .drop-hint{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;text-align:center;color:var(--text-dim);font-size:13px;padding:20px;}
  .drop-hint strong{color:var(--text);font-weight:500;}
  .rail{width:320px;flex:0 0 320px;border-left:1px solid var(--border);background:var(--panel);padding:20px;display:flex;flex-direction:column;gap:20px;overflow-y:auto;}
  .field label{display:block;font-size:12px;color:var(--text-dim);margin-bottom:8px;}
  .field input[type=text], .field input[type=password]{width:100%;background:var(--panel-2);border:1px solid var(--border);color:var(--text);padding:9px 10px;border-radius:5px;font-size:13px;font-family:inherit;}
  select{background:var(--panel-2);border:1px solid var(--border);color:var(--text);padding:7px 8px;border-radius:5px;font-size:12px;font-family:inherit;cursor:pointer;}
  .row{display:flex;align-items:center;gap:10px;}
  .val{font-family:'IBM Plex Mono',monospace;font-size:12px;color:var(--accent);min-width:28px;text-align:right;}
  input[type=range]{flex:1;-webkit-appearance:none;height:3px;background:var(--border);border-radius:2px;outline:none;}
  input[type=range]::-webkit-slider-thumb{-webkit-appearance:none;width:14px;height:14px;border-radius:50%;background:var(--accent);cursor:pointer;border:2px solid var(--panel);}
  .btn{background:var(--panel-2);border:1px solid var(--border);color:var(--text);padding:9px 10px;border-radius:5px;font-size:13px;font-family:inherit;cursor:pointer;text-align:center;}
  .btn:hover{border-color:var(--accent);}
  .btn.primary{background:var(--accent);color:#1a0e0c;border-color:var(--accent);font-weight:700;}
  .btn:disabled{opacity:0.4;cursor:not-allowed;}
  .btn.small{padding:6px 9px;font-size:12px;}
  input[type=file]{display:none;}
  label.check{display:flex;align-items:center;gap:8px;font-size:12px;color:var(--text-dim);cursor:pointer;}
  .hint{font-size:11px;color:var(--text-dim);line-height:1.5;}
  .divider{height:1px;background:var(--border);}
  .madlib-row{display:flex;gap:8px;}
  .madlib-row input{flex:1;}
  .color-pickers{display:flex;gap:8px;}
  .color-pickers input[type=color]{flex:1;height:34px;border:1px solid var(--border);border-radius:5px;background:none;padding:0;cursor:pointer;}
  .caption-list{display:flex;flex-direction:column;gap:10px;}
  .caption-row{background:var(--panel-2);border:1px solid var(--border);border-radius:6px;padding:10px;display:flex;flex-direction:column;gap:8px;}
  .caption-row .top-line{display:flex;gap:6px;align-items:center;}
  .caption-row .top-line input[type=text]{flex:1;}
  .caption-row .selects{display:flex;gap:6px;}
  .caption-row select{flex:1;}
</style>
</head>
<body>

<header>
  <h1>Captioner</h1>
  <span>rendered server-side · powered by Gemini</span>
</header>

<div class="layout">
  <div class="stage">
    <div class="stage-inner" id="stageInner">
      <img id="preview" style="display:none;">
      <div class="drop-hint" id="dropHint">
        <div><strong>Upload a photo</strong><br>to start captioning</div>
      </div>
    </div>
  </div>

  <div class="rail">
    <div class="field">
      <label for="fileInput">Image</label>
      <label class="btn primary" for="fileInput" style="display:block;">Choose photo</label>
      <input type="file" id="fileInput" accept="image/*">
    </div>

    <div class="divider"></div>

    <div class="field">
      <label>Captions</label>
      <div class="caption-list" id="captionList"></div>
      <button class="btn small" id="addCaptionBtn" style="margin-top:8px;">+ Add caption</button>
      <div class="hint">Each caption picks its own spot (top / middle / bottom) and size (header / subheader).</div>
    </div>

    <label class="check"><input type="checkbox" id="autoContrast" checked> Auto contrast text color</label>
    <div class="hint">Reads the brightness behind each caption and switches between white-on-black and black-on-white automatically.</div>

    <div class="field" id="manualColors" style="display:none;">
      <label>Text / outline color (all captions)</label>
      <div class="color-pickers">
        <input type="color" id="textColor" value="#ffffff">
        <input type="color" id="strokeColor" value="#000000">
      </div>
    </div>

    <div class="field">
      <label>Font</label>
      <select id="fontFamily" style="width:100%;">
        <option value="plex" selected>Plex Sans (default)</option>
        <option value="anton">Anton</option>
        <option value="bebas">Bebas Neue</option>
        <option value="marker">Permanent Marker</option>
        <option value="caveat">Caveat (handwriting)</option>
      </select>
    </div>

    <div class="field">
      <label>Header size</label>
      <div class="row">
        <input type="range" id="fontSize" min="20" max="120" value="70">
        <span class="val" id="fontSizeVal">70</span>
      </div>
    </div>

    <div class="divider"></div>

    <div class="field">
      <label for="apiKey">Gemini API key</label>
      <input type="password" id="apiKey" placeholder="AIzaSy...">
      <div class="hint">Sent only to your own local server process for this request, never written to disk. Get one free at <span style="color:var(--accent)">aistudio.google.com/apikey</span>.</div>
    </div>

    <div class="field">
      <label>AI caption</label>
      <div class="madlib-row">
        <input type="text" id="keyword" placeholder="optional angle, e.g. Mondays">
        <button class="btn" id="generateBtn">Generate</button>
      </div>
      <div class="hint" id="genHint">Looks at your photo and decides both the wording and layout.</div>
    </div>

    <div class="divider"></div>

    <a class="btn primary" id="downloadBtn" style="pointer-events:none;opacity:0.4;" download="meme.png">Download PNG</a>
  </div>
</div>

<script>
const fileInput = document.getElementById('fileInput');
const dropHint = document.getElementById('dropHint');
const previewImg = document.getElementById('preview');
const captionListEl = document.getElementById('captionList');
const addCaptionBtn = document.getElementById('addCaptionBtn');
const autoContrast = document.getElementById('autoContrast');
const manualColors = document.getElementById('manualColors');
const textColorInput = document.getElementById('textColor');
const strokeColorInput = document.getElementById('strokeColor');
const fontFamilySelect = document.getElementById('fontFamily');
const fontSizeSlider = document.getElementById('fontSize');
const fontSizeVal = document.getElementById('fontSizeVal');
const downloadBtn = document.getElementById('downloadBtn');
const keywordInput = document.getElementById('keyword');
const generateBtn = document.getElementById('generateBtn');
const genHint = document.getElementById('genHint');
const apiKeyInput = document.getElementById('apiKey');

let imageId = null;
let nextId = 1;
let captions = [
  { id: nextId++, text: '', position: 'top', emphasis: 'header' },
  { id: nextId++, text: '', position: 'bottom', emphasis: 'header' }
];
let renderTimer = null;

function hexToRgbArr(hex){
  const v = parseInt(hex.slice(1), 16);
  return [(v>>16)&255, (v>>8)&255, v&255];
}

fileInput.addEventListener('change', async e => {
  const file = e.target.files[0];
  if (!file) return;
  const formData = new FormData();
  formData.append('image', file);
  const res = await fetch('/upload', { method: 'POST', body: formData });
  const data = await res.json();
  if (data.error){ alert(data.error); return; }
  imageId = data.image_id;
  dropHint.style.display = 'none';
  previewImg.style.display = 'block';
  previewImg.src = 'data:image/jpeg;base64,' + data.preview;
  downloadBtn.style.pointerEvents = 'auto';
  downloadBtn.style.opacity = '1';
  scheduleRender();
});

function renderCaptionList(){
  captionListEl.innerHTML = '';
  captions.forEach(cap => {
    const row = document.createElement('div');
    row.className = 'caption-row';
    row.innerHTML = `
      <div class="top-line">
        <input type="text" placeholder="Caption text" value="${cap.text.replace(/"/g,'&quot;')}">
        <button class="btn small remove-cap">×</button>
      </div>
      <div class="selects">
        <select class="pos-select">
          <option value="top">Top</option>
          <option value="middle">Middle</option>
          <option value="bottom">Bottom</option>
        </select>
        <select class="emph-select">
          <option value="header">Header (large)</option>
          <option value="subheader">Subheader (small)</option>
        </select>
      </div>`;
    row.querySelector('input[type=text]').addEventListener('input', e => { cap.text = e.target.value; scheduleRender(); });
    row.querySelector('.pos-select').value = cap.position;
    row.querySelector('.pos-select').addEventListener('change', e => { cap.position = e.target.value; scheduleRender(); });
    row.querySelector('.emph-select').value = cap.emphasis;
    row.querySelector('.emph-select').addEventListener('change', e => { cap.emphasis = e.target.value; scheduleRender(); });
    row.querySelector('.remove-cap').addEventListener('click', () => {
      captions = captions.filter(c => c.id !== cap.id);
      renderCaptionList(); scheduleRender();
    });
    captionListEl.appendChild(row);
  });
}
addCaptionBtn.addEventListener('click', () => {
  captions.push({ id: nextId++, text: '', position: 'middle', emphasis: 'header' });
  renderCaptionList(); scheduleRender();
});
renderCaptionList();

[autoContrast, fontSizeSlider, fontFamilySelect, textColorInput, strokeColorInput].forEach(el => {
  el.addEventListener('input', () => {
    if (el === fontSizeSlider) fontSizeVal.textContent = fontSizeSlider.value;
    if (el === autoContrast) manualColors.style.display = autoContrast.checked ? 'none' : 'block';
    scheduleRender();
  });
});

function scheduleRender(){
  clearTimeout(renderTimer);
  renderTimer = setTimeout(doRender, 300);
}

async function doRender(){
  if (!imageId) return;
  const payload = {
    image_id: imageId,
    captions: captions.map(({text, position, emphasis}) => ({text, position, emphasis})),
    font: fontFamilySelect.value,
    font_size: parseInt(fontSizeSlider.value, 10),
    auto_contrast: autoContrast.checked,
    fill_color: hexToRgbArr(textColorInput.value),
    stroke_color: hexToRgbArr(strokeColorInput.value),
  };
  const res = await fetch('/render', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(payload) });
  if (!res.ok){ console.error('render failed'); return; }
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  previewImg.src = url;
  downloadBtn.href = url;
}

generateBtn.addEventListener('click', async () => {
  if (!imageId){ genHint.textContent = 'Upload a photo first.'; return; }
  const apiKey = apiKeyInput.value.trim();
  if (!apiKey){ genHint.textContent = 'Paste your Gemini API key above first.'; return; }
  generateBtn.disabled = true;
  generateBtn.textContent = 'Thinking…';
  genHint.textContent = 'Looking at your photo…';
  try {
    const res = await fetch('/ai_caption', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ image_id: imageId, api_key: apiKey, topic: keywordInput.value.trim() })
    });
    const data = await res.json();
    if (data.error){ throw new Error(data.error); }
    captions = data.captions.map(c => ({ id: nextId++, text: c.text, position: c.position, emphasis: c.emphasis }));
    renderCaptionList();
    genHint.textContent = 'Looks at your photo and decides both the wording and layout.';
    scheduleRender();
  } catch (err){
    genHint.textContent = 'Request failed: ' + err.message;
  } finally {
    generateBtn.disabled = false;
    generateBtn.textContent = 'Generate';
  }
});
</script>
</body>
</html>
"""


@app.route("/")
def index():
    return render_template_string(PAGE)


if __name__ == "__main__":
    app.run(debug=False, port=5000)
