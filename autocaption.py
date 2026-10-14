#!/usr/bin/env python3
"""
autocaption.py — free dynamic captions for short-form video.

Transcribes your video locally with Whisper (word-level timing) and burns in
animated captions. Two looks:

  editorial (default)  heavy tight sans + key words in a big italic serif,
                       two centered lines, words fade/blur in as they're spoken
  bold                 ALL-CAPS bold, active word pops in a highlight color

Setup (one time):
    pip install faster-whisper
    install ffmpeg  (Mac: brew install ffmpeg | Windows: winget install ffmpeg)
    keep the "fonts" folder next to this script

Usage:
    python autocaption.py video.mp4
    python autocaption.py clips_folder/                       # batch
    python autocaption.py video.mp4 --emphasize cinnamon,banana
    python autocaption.py video.mp4 --style bold --highlight "#00E5FF"
    python autocaption.py video.mp4 --ass-only                # edit captions by hand, then:
    python autocaption.py video.mp4 --render-ass video_captions.ass

Output: video_captioned.mp4 next to the original (original is never touched).
"""
import argparse, json, re, subprocess, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_FONTS = HERE / "fonts"
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".mkv", ".webm"}

STOPWORDS = set("""a an the and or but so if then than that this these those to of in on at by for
from with into onto over under up down out off about as is are was were be been being am do does did
done have has had having i me my mine you your yours he him his she her hers it its we us our they them
their what which who whom whose when where why how all any both each few more most other some such no
nor not only own same too very can will just should now here there also just get got gonna wanna
really like one two let lets it's i'm you're don't that's it's can't won't would could
because know think thing things actually going want make made said says even still well much many""".split())

# ---------------- helpers ----------------
def ass_color(hex_color, alpha=0):
    h = hex_color.lstrip("#")
    return f"&H{alpha:02X}{h[4:6]}{h[2:4]}{h[0:2]}".upper()

def ts(t):
    t = max(0.0, t)
    return f"{int(t//3600)}:{int(t%3600//60):02d}:{t%60:05.2f}"

def probe_size(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height:stream_side_data=rotation",
         "-of", "json", str(path)], capture_output=True, text=True, check=True).stdout
    st = json.loads(out)["streams"][0]
    w, h, rot = st["width"], st["height"], 0
    for sd in st.get("side_data_list", []) or []:
        rot = abs(int(sd.get("rotation", 0)))
    return (h, w) if rot in (90, 270) else (w, h)

def esc(t):
    return t.replace("\\", "").replace("{", "(").replace("}", ")")

def bare(t):
    return re.sub(r"[^\w']", "", t).lower()

# ---------------- transcription ----------------
def load_audio(path, sr=16000):
    """Decode audio with ffmpeg to 16 kHz mono float32 (avoids PyAV version issues)."""
    import numpy as np
    out = subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-i", str(path),
                          "-vn", "-ac", "1", "-ar", str(sr), "-f", "f32le", "-"],
                         capture_output=True, check=True).stdout
    return np.frombuffer(out, dtype=np.float32).copy()

def transcribe(path, model_size, language):
    from faster_whisper import WhisperModel
    print(f"  transcribing with Whisper '{model_size}' (first run downloads the model)...")
    model = WhisperModel(model_size, device="cpu", compute_type="int8")
    segments, _ = model.transcribe(load_audio(path), word_timestamps=True,
                                   language=language, vad_filter=True)
    words = []
    for seg in segments:
        for w in seg.words or []:
            if w.word.strip():
                words.append({"text": w.word.strip(), "start": w.start, "end": w.end})
    return words

def chunk_words(words, per_chunk, max_gap=0.6):
    chunks, cur = [], []
    for w in words:
        if cur and (len(cur) >= per_chunk or w["start"] - cur[-1]["end"] > max_gap
                    or re.search(r"[.!?]$", cur[-1]["text"])):
            chunks.append(cur); cur = []
        cur.append(w)
    if cur:
        chunks.append(cur)
    # avoid lonely one-word captions: fold them into the previous caption when it flows on
    merged = []
    for c in chunks:
        if (merged and len(c) == 1 and len(merged[-1]) <= per_chunk
                and not re.search(r"[.!?]$", merged[-1][-1]["text"])
                and c[0]["start"] - merged[-1][-1]["end"] <= max_gap):
            merged[-1] = merged[-1] + c
        else:
            merged.append(c)
    return merged

def chunk_end_time(chunks, ci, linger=0.6):
    end = chunks[ci][-1]["end"]
    if ci + 1 < len(chunks):
        return min(chunks[ci + 1][0]["start"], end + linger)
    return end + linger

# ---------------- silence cutting ----------------
def keep_ranges(words, duration, max_pause=0.4, pad=0.08):
    """Time ranges to keep: speech plus a little padding; pauses longer than max_pause are cut."""
    if not words:
        return [(0.0, duration)]
    ivs = sorted((max(0.0, w["start"] - pad), min(duration, w["end"] + pad)) for w in words)
    out = [list(ivs[0])]
    for s, e in ivs[1:]:
        if s - out[-1][1] <= max_pause:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return [(round(s, 3), round(e, 3)) for s, e in out if e - s > 0.05]

def remap_time(t, ranges):
    """Original time -> time in the cut video."""
    acc = 0.0
    for s, e in ranges:
        if t < s:
            return acc
        if t <= e:
            return acc + (t - s)
        acc += e - s
    return acc

def remap_chunks(chunks, ranges):
    return [[dict(w, start=remap_time(w["start"], ranges), end=remap_time(w["end"], ranges)) for w in c]
            for c in chunks]

def select_expr(ranges):
    return "+".join(f"between(t,{s:.3f},{e:.3f})" for s, e in ranges)

# ---------------- editorial style ----------------
def pick_emphasis(chunk, ci, a, forced):
    idx = {i for i, w in enumerate(chunk) if bare(w["text"]) in forced}
    if a.emph <= 0 or len(chunk) < 3 or ci % 3 == 2:   # leave some chunks all-sans for rhythm
        return idx
    cands = [(len(bare(w["text"])), i) for i, w in enumerate(chunk)
             if bare(w["text"]) not in STOPWORDS and len(bare(w["text"])) >= 4
             and bare(w["text"]).isalpha()]
    for _, i in sorted(cands, reverse=True)[: max(0, a.emph - len(idx))]:
        idx.add(i)
    return idx

def split_lines(texts, emph, serif_scale):
    widths = [len(t) * (0.62 * serif_scale if i in emph else 1.0) + 0.6
              for i, t in enumerate(texts)]
    total = sum(widths)
    if len(texts) < 3 or total < 13:
        return [list(range(len(texts)))]
    best, best_k = None, 1
    for k in range(1, len(texts)):
        m = max(sum(widths[:k]), sum(widths[k:]))
        if best is None or m < best:
            best, best_k = m, k
    return [list(range(best_k)), list(range(best_k, len(texts)))]

def editorial_text(chunk, texts, emph, lines, t_line, a, sizes, fx_alpha=None):
    """fx_alpha=None -> the main text layer; else an effect layer drawn at that opacity (0-1)."""
    sans_fs, serif_fs = sizes
    shadow = fx_alpha is not None
    target = "&H%02X&" % (255 - int(255 * max(0.0, min(1.0, fx_alpha)))) if shadow else "&H00&"
    out_lines = []
    for line in lines:
        parts = []
        for i in line:
            t0 = max(0, int((chunk[i]["start"] - t_line) * 1000)) if getattr(a, "reveal", False) else 0
            anim = (f"\\alpha&HFF&\\t({t0},{t0+160},\\alpha{target})"
                    if not a.no_animate else f"\\alpha{target}")
            if not shadow and not a.no_animate:
                anim += f"\\blur5\\t({t0},{t0+220},\\blur0)"
            if i in emph:
                font = f"\\fn{a.serif_font}\\b0\\i1\\fs{serif_fs}\\fsp0"
            else:
                font = f"\\fn{a.font}\\b1\\i0\\fs{sans_fs}\\fsp{a.tracking}"
            parts.append("{" + font + anim + "}" + esc(texts[i]))
        # explicit word gap: a plain space in the main font plus adjustable extra spacing
        gap = round(sans_fs * float(getattr(a, "gap", 0.07)), 1)
        sep = "{\\fn%s\\b1\\i0\\fs%d\\fsp%s} " % (a.font, sans_fs, gap)
        out_lines.append(sep.join(parts))
    return out_lines

def auto_chunks(words, a):
    """Group words into captions and mark serif words. Each word gets 'emph': bool."""
    forced = {bare(x) for x in (getattr(a, "emphasize", "") or "").split(",") if x.strip()}
    chunks = chunk_words(words, a.words)
    out = []
    for ci, chunk in enumerate(chunks):
        emph = pick_emphasis(chunk, ci, a, forced)
        out.append([dict(w, emph=(i in emph)) for i, w in enumerate(chunk)])
    return out

def effect_layers(a, fs, H):
    """Background effect behind the text: list of (opacity, ASS tags, y offset) layers."""
    kind = getattr(a, "effect", "shadow")
    k = max(0.0, min(1.0, float(getattr(a, "effect_strength", 0.5))))
    c = ass_color(getattr(a, "effect_color", "#FFFFFF"))
    if kind == "shadow":
        return [(0.15 + 0.5 * k, f"\\1c{ass_color('#000000')}\\bord0\\blur{max(3, int(fs * (0.10 + 0.16 * k)))}",
                 max(2, int(H * 0.003)))]
    if kind == "glow":
        return [(0.20 + 0.45 * k, f"\\1c{c}\\3c{c}\\bord{max(2, int(fs * (0.06 + 0.10 * k)))}\\blur{int(fs * (0.30 + 0.25 * k))}", 0),
                (0.25 + 0.55 * k, f"\\1c{c}\\3c{c}\\bord{max(1, int(fs * 0.02))}\\blur{max(3, int(fs * (0.08 + 0.06 * k)))}", 0)]
    if kind == "haze":
        return [(0.20 + 0.45 * k, f"\\1c{ass_color('#000000')}\\3c{ass_color('#000000')}\\bord{int(fs * (0.30 + 0.35 * k))}\\blur{int(fs * 0.55)}", 0)]
    return []

def build_editorial(words, W, H, a):
    return build_editorial_chunks(auto_chunks(words, a), W, H, a)

def build_editorial_chunks(chunks, W, H, a):
    sans_fs = int(H * a.size)
    serif_fs = int(sans_fs * a.serif_scale)
    margin_v = int(H * a.position)
    col = ass_color(a.color)
    a.tracking = round(-sans_fs * 0.035, 1)
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Main,{a.font},{sans_fs},{col},{col},&H00000000,&H00000000,-1,0,0,0,100,100,{a.tracking},0,1,0,0,2,{int(W*0.06)},{int(W*0.06)},{margin_v},1
Style: Shadow,{a.font},{sans_fs},&H00000000,&H00000000,&H00000000,&H00000000,-1,0,0,0,100,100,{a.tracking},0,1,0,0,2,{int(W*0.06)},{int(W*0.06)},{margin_v - int(H*0.003)},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events = []
    chunks = [c for c in chunks if c]
    for ci, chunk in enumerate(chunks):
        start, end = chunk[0]["start"], chunk_end_time(chunks, ci)
        texts = [w["text"] if not a.lower else w["text"].lower() for w in chunk]
        emph = {i for i, w in enumerate(chunk) if w.get("emph")}
        lines = split_lines(texts, emph, a.serif_scale)
        fade = "{\\fad(0,140)}"
        mn = editorial_text(chunk, texts, emph, lines, start, a, (sans_fs, serif_fs))
        fx_layers = []  # (opacity, extra tags, y offset)
        for op, tags, dy in effect_layers(a, sans_fs, H):
            fx_layers.append((editorial_text(chunk, texts, emph, lines, start, a, (sans_fs, serif_fs), fx_alpha=op),
                              tags, dy))
        # one event per line, positioned by hand so leading stays tight
        line_h = sans_fs * a.leading
        bottom = H - margin_v
        n = len(lines)
        for k in range(n):
            y = bottom - (n - 1 - k) * line_h
            for li, (fx, tags, dy) in enumerate(fx_layers):
                events.append(f"Dialogue: {li},{ts(start)},{ts(end)},Shadow,,0,0,0,,"
                              f"{{\\an2\\pos({W//2},{int(y + dy)}){tags}}}{fade}{fx[k]}")
            events.append(f"Dialogue: {len(fx_layers)},{ts(start)},{ts(end)},Main,,0,0,0,,"
                          f"{{\\an2\\pos({W//2},{int(y)})}}{fade}{mn[k]}")
    return header + "\n".join(events) + "\n"

# ---------------- bold style ----------------
def build_bold(words, W, H, a):
    fs = int(H * a.size)
    outline, shadow = max(2, int(fs * 0.09)), max(1, int(fs * 0.05))
    base, hl = ass_color(a.color), ass_color(a.highlight)
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,{a.font},{fs},{base},{base},&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,{outline},{shadow},2,{int(W*0.08)},{int(W*0.08)},{int(H*a.position)},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events = []
    chunks = chunk_words(words, a.words)
    for ci, chunk in enumerate(chunks):
        cend = chunk_end_time(chunks, ci, 0.5)
        texts = [re.sub(r"[^\w'’%$#@&-]", "", w["text"]).upper() for w in chunk]
        for i, w in enumerate(chunk):
            end = chunk[i + 1]["start"] if i + 1 < len(chunk) else cend
            end = max(end, w["start"] + 0.05)
            parts = []
            for j, t in enumerate(texts):
                if not t:
                    continue
                if j == i:
                    p = int(a.pop * 100)
                    parts.append("{\\c%s\\fscx%d\\fscy%d\\t(0,90,\\fscx100\\fscy100)}%s{\\r}" % (hl, p, p, t))
                else:
                    parts.append("{\\c%s}%s{\\r}" % (base, t))
            events.append(f"Dialogue: 0,{ts(w['start'])},{ts(end)},Cap,,0,0,0,,{' '.join(parts)}")
    return header + "\n".join(events) + "\n"

# ---------------- render ----------------
def ff_path(p):
    return str(p).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")

def render(video, ass_path, out_path, fonts_dir):
    vf = f"ass='{ff_path(ass_path)}'"
    if fonts_dir and Path(fonts_dir).is_dir():
        vf += f":fontsdir='{ff_path(fonts_dir)}'"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-stats", "-i", str(video), "-vf", vf,
                    "-c:v", "libx264", "-preset", "medium", "-crf", "18",
                    "-c:a", "copy", "-movflags", "+faststart", str(out_path)], check=True)

def process(video, a):
    video = Path(video)
    print(f"▶ {video.name}")
    out = video.with_name(video.stem + "_captioned.mp4")
    if a.render_ass:
        render(video, a.render_ass, out, a.fonts_dir); print(f"  ✔ {out}"); return
    W, H = probe_size(video)
    words = transcribe(video, a.model, a.language)
    if not words:
        print("  no speech found, skipping"); return
    builder = build_editorial if a.style == "editorial" else build_bold
    ass_path = video.with_name(video.stem + "_captions.ass")
    ass_path.write_text(builder(words, W, H, a), encoding="utf-8")
    print(f"  wrote {ass_path.name} ({len(words)} words)")
    if not a.ass_only:
        render(video, ass_path, out, a.fonts_dir)
        print(f"  ✔ {out}")

def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Free dynamic captions.")
    p.add_argument("input", help="video file or folder of videos")
    p.add_argument("--style", choices=["editorial", "bold"], default="editorial")
    p.add_argument("--words", type=int, default=None, help="max words per caption (editorial 5, bold 3)")
    p.add_argument("--font", default=None, help="main font (editorial: Inter Tight, bold: Poppins)")
    p.add_argument("--serif-font", default="Instrument Serif", help="emphasis font (editorial)")
    p.add_argument("--serif-scale", type=float, default=1.45, help="serif size vs. main text")
    p.add_argument("--emphasize", default="", help="comma list of words to always set in serif")
    p.add_argument("--emph", type=int, default=2, help="auto serif words per caption (0 = off)")
    p.add_argument("--fonts-dir", default=str(DEFAULT_FONTS), help="folder with .ttf files")
    p.add_argument("--color", default="#FFFFFF", help="text color")
    p.add_argument("--highlight", default="#FFE600", help="active word color (bold style)")
    p.add_argument("--size", type=float, default=None, help="text size as fraction of video height")
    p.add_argument("--position", type=float, default=0.30, help="distance from bottom as fraction of height")
    p.add_argument("--pop", type=float, default=1.18, help="active-word pop (bold style)")
    p.add_argument("--leading", type=float, default=1.02, help="line spacing vs. text size (editorial)")
    p.add_argument("--lower", action="store_true", help="force lowercase (editorial)")
    p.add_argument("--no-animate", action="store_true", help="show whole caption at once (editorial)")
    p.add_argument("--model", default="small", help="Whisper: tiny/base/small/medium/large-v3")
    p.add_argument("--language", default=None, help="force language, e.g. en")
    p.add_argument("--ass-only", action="store_true", help="only write the .ass caption file")
    p.add_argument("--render-ass", default=None, help="render this (hand-edited) .ass instead of transcribing")
    a = p.parse_args(argv)
    ed = a.style == "editorial"
    a.words = a.words or (5 if ed else 3)
    a.font = a.font or ("Inter Tight" if ed else "Poppins")
    a.size = a.size or (0.058 if ed else 0.055)
    return a

def main():
    a = parse_args()
    src = Path(a.input)
    vids = sorted(f for f in src.iterdir() if f.suffix.lower() in VIDEO_EXTS
                  and not f.stem.endswith("_captioned")) if src.is_dir() else [src]
    if not vids:
        sys.exit("No videos found.")
    for v in vids:
        process(v, a)

if __name__ == "__main__":
    main()
