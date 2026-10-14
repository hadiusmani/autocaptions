#!/usr/bin/env python3
"""AutoCaption — local web app. Run:  python app.py  then open http://127.0.0.1:5055

Flow: make a video → add clips (each transcribes in the background) → drag to order →
Create video: clips are joined, dead space trimmed, captions burned in, exported at 60 fps.
"""
import json, re, shutil, subprocess, sys, threading, time, types, uuid, webbrowser
from pathlib import Path
from flask import Flask, jsonify, request, send_file, send_from_directory, abort

import autocaption as ac

HERE = Path(__file__).resolve().parent
WORK = HERE / "videos"
WORK.mkdir(exist_ok=True)
FONTS = HERE / "fonts"
PORT = 5055

app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 ** 3
LOCK = threading.Lock()           # protects project json
WORKER = threading.Lock()         # one heavy job (transcribe / render) at a time
PROJECTS = {}
_model = {"key": None, "obj": None}
_GPU_OK = [True]

# ---- caption templates: a template is a named set of style settings ----
STYLE_KEYS = ["font", "serif_font", "size", "position", "serif_scale", "leading", "gap", "words", "emph",
              "color", "lower", "reveal", "effect", "effect_color", "effect_strength"]
_EDITORIAL = dict(font="Inter Tight", serif_font="Instrument Serif", size=0.058, position=0.30,
                  serif_scale=1.45, leading=1.02, gap=0.07, words=5, emph=2, color="#FFFFFF",
                  lower=False, reveal=False, effect="shadow", effect_color="#FFFFFF", effect_strength=0.5)
BUILTIN_TEMPLATES = [
    dict(id="editorial", name="Editorial", builtin=True, settings=dict(_EDITORIAL)),
    dict(id="editorial-glow", name="Editorial Glow", builtin=True,
         settings=dict(_EDITORIAL, effect="glow", effect_color="#FFFFFF", effect_strength=0.5)),
    dict(id="editorial-warm", name="Editorial Warm Glow", builtin=True,
         settings=dict(_EDITORIAL, effect="glow", effect_color="#FFC86B", effect_strength=0.55)),
    dict(id="editorial-haze", name="Editorial Haze", builtin=True,
         settings=dict(_EDITORIAL, effect="haze", effect_strength=0.5)),
]
USER_TEMPLATES = HERE / "my_templates.json"

def all_templates():
    try:
        mine = json.loads(USER_TEMPLATES.read_text())
    except Exception:
        mine = []
    return BUILTIN_TEMPLATES + mine

DEFAULTS = dict(template="editorial", **BUILTIN_TEMPLATES[0]["settings"], cut=True, max_pause=0.4,
                fps=60, quality="high", model="small")

# ---------------- storage ----------------
def pdir(pid):
    return WORK / pid

def save(p):
    (pdir(p["id"]) / "project.json").write_text(json.dumps(p))

def update(p, **kw):
    with LOCK:
        p.update(kw); save(p)

def update_clip(p, c, **kw):
    with LOCK:
        c.update(kw); save(p)

def load_all():
    for d in WORK.iterdir():
        f = d / "project.json"
        if not f.exists():
            continue
        try:
            p = json.loads(f.read_text())
        except Exception:
            continue
        if p.get("status") in ("building", "rendering"):
            p["status"], p["message"] = "error", "Interrupted — press Create again"
        PROJECTS[p["id"]] = p
        for c in p["clips"]:
            if c["status"] in ("queued", "transcribing"):
                c["status"] = "queued"
                threading.Thread(target=transcribe_clip, args=(p, c), daemon=True).start()

def settings_ns(s):
    s = {**DEFAULTS, **(s or {})}
    return types.SimpleNamespace(
        size=float(s["size"]), position=float(s["position"]), serif_scale=float(s["serif_scale"]),
        leading=float(s["leading"]), words=int(s["words"]), emph=int(s["emph"]), color=s["color"],
        lower=bool(s["lower"]), reveal=bool(s["reveal"]), font=s["font"], serif_font=s["serif_font"],
        gap=float(s["gap"]), effect=s["effect"], effect_color=s["effect_color"],
        effect_strength=float(s["effect_strength"]), no_animate=False, emphasize="")

# ---------------- media helpers ----------------
def probe(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                          "format=duration:stream=codec_type", "-of", "json", str(path)],
                         capture_output=True, text=True).stdout
    info = json.loads(out or "{}")
    streams = info.get("streams", [])
    dur = float(info.get("format", {}).get("duration") or 0)
    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    return dur, has_audio

def ffmpeg_major():
    try:
        v = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True).stdout
        m = re.search(r"ffmpeg version n?(\d+)", v)
        return int(m.group(1)) if m else 6
    except Exception:
        return 6

FF_MAJOR = [None]

# ---------------- transcription ----------------
def get_model(name, device):
    from faster_whisper import WhisperModel
    if _model["key"] != (name, device):
        _model["obj"] = WhisperModel(name, device=device, compute_type="int8" if device == "cpu" else "auto")
        _model["key"] = (name, device)
    return _model["obj"]

def run_whisper(audio, name):
    devices = (["auto"] if _GPU_OK[0] else []) + ["cpu"]
    for dev in devices:
        try:
            segs, _ = get_model(name, dev).transcribe(audio, word_timestamps=True, vad_filter=True)
            words = []
            for seg in segs:
                for w in seg.words or []:
                    if w.word.strip():
                        words.append({"text": w.word.strip(), "start": round(w.start, 3), "end": round(w.end, 3)})
            return words
        except Exception as e:
            msg = str(e).lower()
            if dev != "cpu" and any(k in msg for k in ("cublas", "cudnn", "cuda", ".dll", "gpu")):
                _GPU_OK[0] = False; _model["key"] = None
                continue
            raise

def transcribe_clip(p, c):
    with WORKER:
        if c not in p["clips"]:
            return
        try:
            update_clip(p, c, status="transcribing")
            src = pdir(p["id"]) / c["file"]
            words = run_whisper(ac.load_audio(src), p["settings"].get("model", "small")) if c["has_audio"] else []
            update_clip(p, c, status="ready", words=words)
        except Exception as e:
            update_clip(p, c, status="error", message=str(e)[:300])

# ---------------- timeline ----------------
def clip_ranges(c, s):
    dur = c["duration"]
    if s.get("cut") and c.get("words"):
        return ac.keep_ranges(c["words"], dur, float(s["max_pause"])) or [(0.0, dur)]
    return [(0.0, dur)]

def ordered_clips(p):
    by = {c["id"]: c for c in p["clips"]}
    return [by[i] for i in p["order"] if i in by]

def build_chunks(p, s):
    a = settings_ns(s)
    chunks, offset, ci = [], 0.0, 0
    for c in ordered_clips(p):
        ranges = clip_ranges(c, s)
        out = []
        for chunk in ac.chunk_words(c.get("words") or [], a.words):
            emph = ac.pick_emphasis(chunk, ci, a, set()); ci += 1
            out.append([dict(w, emph=(i in emph)) for i, w in enumerate(chunk)])
        for chunk in ac.remap_chunks(out, ranges):
            chunks.append([dict(w, start=round(w["start"] + offset, 3), end=round(w["end"] + offset, 3))
                           for w in chunk])
        offset += sum(e - b for b, e in ranges)
    return chunks, offset

def target_size(p):
    clips = ordered_clips(p)
    # follow the orientation most of the footage uses (vertical for shorts), at its best resolution
    vert = sum(c["duration"] for c in clips if c["H"] >= c["W"])
    horiz = sum(c["duration"] for c in clips if c["H"] < c["W"])
    pool = [c for c in clips if (c["H"] >= c["W"]) == (vert >= horiz)] or clips
    c = max(pool, key=lambda c: c["W"] * c["H"])
    W, H = c["W"], c["H"]
    k = min(1.0, 1920 / max(W, H))
    return int(W * k) // 2 * 2, int(H * k) // 2 * 2

def render(p, s, out_path, ass_path=None, preview=False, on_progress=None):
    if FF_MAJOR[0] is None:
        FF_MAJOR[0] = ffmpeg_major()
    W, H = target_size(p)
    if preview:
        W, H = W // 4 * 2, H // 4 * 2
    fps = 30 if preview else int(s.get("fps", 60))
    d = pdir(p["id"])
    args, graph, n, k, total = [], [], 0, 0, 0.0
    for c in ordered_clips(p):
        ranges = clip_ranges(c, s)
        vi = n; args += ["-i", str(d / c["file"])]; n += 1
        if c["has_audio"]:
            astream = f"{vi}:a:0"
        else:
            args += ["-f", "lavfi", "-t", f"{c['duration']:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
            astream = f"{n}:a:0"; n += 1
        m = len(ranges)
        graph.append(f"[{vi}:v:0]split={m}" + "".join(f"[sv{vi}_{j}]" for j in range(m)))
        graph.append(f"[{astream}]asplit={m}" + "".join(f"[sa{vi}_{j}]" for j in range(m)))
        for j, (b, e) in enumerate(ranges):
            graph.append(f"[sv{vi}_{j}]trim=start={b:.3f}:end={e:.3f},setpts=PTS-STARTPTS,"
                         f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1,"
                         f"fps={fps},format=yuv420p[v{k}]")
            graph.append(f"[sa{vi}_{j}]atrim=start={b:.3f}:end={e:.3f},asetpts=PTS-STARTPTS,"
                         f"aformat=sample_rates=48000:channel_layouts=stereo[a{k}]")
            k += 1; total += e - b
    graph.append("".join(f"[v{i}][a{i}]" for i in range(k)) + f"concat=n={k}:v=1:a=1[cv][ca]")
    vout = "[cv]"
    if ass_path:
        graph.append(f"[cv]ass='{ac.ff_path(ass_path)}':fontsdir='{ac.ff_path(FONTS)}'[ov]")
        vout = "[ov]"
    script = d / ("graph_preview.txt" if preview else "graph.txt")
    script.write_text(";\n".join(graph), encoding="utf-8")
    fc = ["-/filter_complex", str(script)] if FF_MAJOR[0] >= 7 else ["-filter_complex_script", str(script)]
    if preview:
        enc = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "27", "-c:a", "aac", "-b:a", "128k"]
    else:
        crf = {"high": "15", "standard": "19"}.get(s.get("quality"), "15")
        enc = ["-c:v", "libx264", "-preset", "medium", "-crf", crf, "-profile:v", "high",
               "-maxrate", "40M", "-bufsize", "80M", "-c:a", "aac", "-b:a", "256k"]
    cmd = ["ffmpeg", "-y", "-loglevel", "error", *args, *fc, "-map", vout, "-map", "[ca]", *enc,
           "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-progress", "pipe:1", "-nostats", str(out_path)]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    for line in proc.stdout:
        m = re.match(r"out_time_us=(\d+)", line)
        if m and on_progress and total:
            on_progress(min(1.0, int(m.group(1)) / 1e6 / total))
    err = proc.stderr.read()
    if proc.wait() != 0:
        raise RuntimeError(err.strip()[-400:] or "ffmpeg failed")
    return total

def export_final(p, chunks, s):
    d = pdir(p["id"])
    W, H = target_size(p)
    ass = d / "captions.ass"
    ass.write_text(ac.build_editorial_chunks(chunks, W, H, settings_ns(s)), encoding="utf-8")
    name = re.sub(r"[^\w\- ]", "", p["name"]).strip() or "video"
    out = d / f"{name}.mp4"
    update(p, status="rendering", message="Exporting final video…", progress=0)
    render(p, s, out, ass_path=ass, on_progress=lambda f: update(p, progress=int(f * 100)))
    update(p, status="done", message="", progress=100, output=out.name, exported=time.time())

def create_job(p, s):
    with WORKER:
        try:
            s = {**DEFAULTS, **s}
            update(p, status="building", message="Joining clips and trimming dead space…", progress=0,
                   settings=s, output=None)
            chunks, total = build_chunks(p, s)
            render(p, s, pdir(p["id"]) / "preview.mp4", preview=True,
                   on_progress=lambda f: update(p, progress=int(f * 100)))
            W, H = target_size(p)
            update(p, chunks=chunks, W=W, H=H, duration=round(total, 3), built_order=list(p["order"]),
                   built_pause=[s["cut"], s["max_pause"]], built=time.time())
            export_final(p, chunks, s)
        except Exception as e:
            update(p, status="error", message=f"Failed: {e}")

def export_job(p, chunks, s):
    with WORKER:
        try:
            s = {**DEFAULTS, **s}
            update(p, chunks=chunks, settings=s)
            export_final(p, chunks, s)
        except Exception as e:
            update(p, status="error", message=f"Export failed: {e}")

def public(p, full=True):
    clips = [{**{k: c.get(k) for k in ("id", "name", "duration", "status", "message", "W", "H")},
              "words": len(c.get("words") or [])} for c in ordered_clips(p)]
    out = {k: p.get(k) for k in ("id", "name", "status", "message", "progress", "order", "settings",
                                 "output", "created", "W", "H", "duration", "built", "built_order",
                                 "built_pause", "exported")}
    out["clips"] = clips
    out["settings"] = {**DEFAULTS, **(p.get("settings") or {})}
    if full:
        out["chunks"] = p.get("chunks")
    return out

def get_p(pid):
    return PROJECTS.get(pid) or abort(404)

# ---------------- routes ----------------
@app.get("/")
def index():
    return send_from_directory(HERE, "index.html")

# ---- auto-exit: when started by AutoCaption.exe, quit after the app window has been closed ----
LAST_PING = [time.time(), False]   # [last heartbeat, seen any heartbeat]

@app.post("/api/ping")
def ping():
    LAST_PING[0], LAST_PING[1] = time.time(), True
    return jsonify(ok=True)

def auto_exit_watchdog(idle=180, startup=180):
    import os
    started = time.time()
    while True:
        time.sleep(10)
        busy = WORKER.locked() or any(
            p.get("status") in ("building", "rendering") for p in list(PROJECTS.values()))
        if busy:
            continue
        quiet = time.time() - LAST_PING[0]
        if (LAST_PING[1] and quiet > idle) or (not LAST_PING[1] and time.time() - started > startup):
            print("No open AutoCaption window; shutting down.")
            os._exit(0)

@app.get("/favicon.ico")
def favicon():
    f = HERE / "icon.ico"
    return send_file(f) if f.exists() else abort(404)

@app.get("/fonts/<path:f>")
def fonts(f):
    return send_from_directory(FONTS, f)

@app.get("/api/templates")
def templates():
    return jsonify(all_templates())

@app.post("/api/templates")
def add_template():
    body = request.json or {}
    name = (body.get("name") or "").strip()[:40] or "My template"
    t = dict(id="t" + uuid.uuid4().hex[:8], name=name, builtin=False,
             settings={k: (body.get("settings") or {}).get(k, DEFAULTS[k]) for k in STYLE_KEYS})
    with LOCK:
        mine = [x for x in all_templates() if not x.get("builtin")] + [t]
        USER_TEMPLATES.write_text(json.dumps(mine, indent=1))
    return jsonify(t)

@app.post("/api/templates/<tid>/delete")
def del_template(tid):
    with LOCK:
        mine = [x for x in all_templates() if not x.get("builtin") and x["id"] != tid]
        USER_TEMPLATES.write_text(json.dumps(mine, indent=1))
    return jsonify(ok=True)

@app.get("/api/projects")
def projects():
    return jsonify(sorted((public(p, False) for p in PROJECTS.values()), key=lambda x: -(x["created"] or 0)))

@app.post("/api/projects")
def new_project():
    pid = uuid.uuid4().hex[:10]
    pdir(pid).mkdir(parents=True)
    body = request.json or {}
    p = dict(id=pid, name=body.get("name") or time.strftime("Video %b %d %I-%M %p"), clips=[], order=[],
             settings={**DEFAULTS, **(body.get("settings") or {})}, status="draft", message="",
             progress=0, created=time.time(), chunks=None, output=None)
    with LOCK:
        PROJECTS[pid] = p; save(p)
    return jsonify(public(p))

@app.get("/api/project/<pid>")
def project(pid):
    return jsonify(public(get_p(pid)))

@app.post("/api/project/<pid>/rename")
def rename(pid):
    p = get_p(pid); update(p, name=((request.json or {}).get("name") or p["name"]).strip()[:80])
    return jsonify(public(p, False))

@app.post("/api/project/<pid>/clips")
def add_clip(pid):
    p = get_p(pid)
    f = request.files.get("video") or abort(400)
    cid = uuid.uuid4().hex[:8]
    ext = Path(f.filename).suffix.lower() or ".mp4"
    path = pdir(pid) / f"clip_{cid}{ext}"
    f.save(path)
    try:
        W, H = ac.probe_size(path)
        dur, has_audio = probe(path)
    except Exception:
        path.unlink(missing_ok=True)
        return jsonify(error="That file doesn't look like a video."), 400
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{min(0.5, dur / 2):.2f}", "-i", str(path),
                    "-frames:v", "1", "-vf", "scale=-2:360", str(pdir(pid) / f"thumb_{cid}.jpg")])
    c = dict(id=cid, name=f.filename, file=path.name, W=W, H=H, duration=round(dur, 3),
             has_audio=has_audio, status="queued", words=None, message="")
    with LOCK:
        p["clips"].append(c); p["order"].append(cid); save(p)
    threading.Thread(target=transcribe_clip, args=(p, c), daemon=True).start()
    return jsonify(public(p, False))

@app.post("/api/project/<pid>/order")
def set_order(pid):
    p = get_p(pid)
    ids = {c["id"] for c in p["clips"]}
    update(p, order=[i for i in (request.json or {}).get("order", []) if i in ids])
    return jsonify(public(p, False))

@app.post("/api/project/<pid>/clip/<cid>/delete")
def del_clip(pid, cid):
    p = get_p(pid)
    with LOCK:
        c = next((c for c in p["clips"] if c["id"] == cid), None)
        if c:
            p["clips"].remove(c); p["order"] = [i for i in p["order"] if i != cid]; save(p)
    if c:
        for f in (pdir(pid) / c["file"], pdir(pid) / f"thumb_{cid}.jpg"):
            try: f.unlink()
            except Exception: pass
    return jsonify(public(p, False))

@app.post("/api/project/<pid>/clip/<cid>/retry")
def retry_clip(pid, cid):
    p = get_p(pid)
    c = next((c for c in p["clips"] if c["id"] == cid), None) or abort(404)
    if c["status"] == "error":
        update_clip(p, c, status="queued", message="")
        threading.Thread(target=transcribe_clip, args=(p, c), daemon=True).start()
    return jsonify(public(p, False))

@app.get("/api/project/<pid>/clip/<cid>/thumb")
def thumb(pid, cid):
    f = pdir(pid) / f"thumb_{cid}.jpg"
    return send_file(f) if f.exists() else abort(404)

@app.post("/api/project/<pid>/create")
def create(pid):
    p = get_p(pid)
    if p["status"] in ("building", "rendering"):
        return jsonify(public(p))
    if not p["order"] or any(c["status"] != "ready" for c in ordered_clips(p)):
        return jsonify(error="Wait until all clips are transcribed."), 400
    s = {**p["settings"], **((request.json or {}).get("settings") or {})}
    update(p, status="building", message="Starting…", progress=0)
    threading.Thread(target=create_job, args=(p, s), daemon=True).start()
    return jsonify(public(p))

@app.post("/api/project/<pid>/export")
def export(pid):
    p = get_p(pid)
    if p["status"] in ("building", "rendering") or not p.get("chunks"):
        return jsonify(public(p))
    body = request.json or {}
    update(p, status="rendering", message="Starting export…", progress=0)
    threading.Thread(target=export_job, args=(p, body.get("chunks") or p["chunks"],
                                              {**p["settings"], **(body.get("settings") or {})}), daemon=True).start()
    return jsonify(public(p))

@app.post("/api/project/<pid>/settings")
def save_settings(pid):
    p = get_p(pid); update(p, settings={**p["settings"], **(request.json or {})})
    return jsonify(ok=True)

@app.post("/api/project/<pid>/regroup")
def regroup(pid):
    p = get_p(pid)
    s = {**p["settings"], **(request.json or {})}
    chunks, _ = build_chunks(p, s)
    update(p, chunks=chunks, settings=s)
    return jsonify(public(p))

@app.post("/api/project/<pid>/delete")
def delete(pid):
    if PROJECTS.pop(pid, None):
        shutil.rmtree(pdir(pid), ignore_errors=True)
    return jsonify(ok=True)

@app.get("/api/project/<pid>/preview")
def preview(pid):
    f = pdir(pid) / "preview.mp4"
    return send_file(f, conditional=True) if f.exists() else abort(404)

@app.get("/api/project/<pid>/download")
def download(pid):
    p = get_p(pid)
    if not p.get("output"):
        abort(404)
    return send_file(pdir(pid) / p["output"], as_attachment=True, download_name=p["output"])

if __name__ == "__main__":
    # Started without a console (pythonw / AutoCaption.exe): send output to a log file.
    if sys.stdout is None or sys.stderr is None:
        log = open(HERE / "autocaption.log", "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stdout or log
        sys.stderr = sys.stderr or log
    if not shutil.which("ffmpeg"):
        sys.exit("ffmpeg isn't installed. Windows: winget install ffmpeg   Mac: brew install ffmpeg")
    load_all()
    url = f"http://127.0.0.1:{PORT}"
    print(f"\n  AutoCaption is running -> {url}\n  (leave this window open; close it to quit)\n")
    if "--auto-exit" in sys.argv:
        threading.Thread(target=auto_exit_watchdog, daemon=True).start()
    if "--no-browser" not in sys.argv:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    app.run(host="127.0.0.1", port=PORT, threaded=True)
