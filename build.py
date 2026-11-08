#!/usr/bin/env python3
"""シャドーイング教材ビルダー

使い方:
    python3 build.py lessons/xxx.json                    # キーがあれば AI 音声、なければ macOS の say
    python3 build.py lessons/xxx.json --engine eleven    # ElevenLabs（最も自然）
    python3 build.py lessons/xxx.json --engine openai    # OpenAI（安い・十分自然）
    python3 build.py lessons/xxx.json --engine say       # 無料・オフライン
    python3 build.py lessons/xxx.json --voice cedar      # 声を変える

API キーはこのフォルダの .env に書く（.env.example 参照）。
音声は段落ごとに .tts_cache/ に保存されるので、HTML だけ作り直しても再課金されない。
"""
import argparse, hashlib, json, os, shutil, subprocess, sys, tempfile, urllib.request, urllib.error, wave
from pathlib import Path

HERE = Path(__file__).resolve().parent
TEMPLATE = HERE / "template.html"
CACHE = HERE / ".tts_cache"
RATE_HZ = 24000

# ── 各エンジンの既定値 ────────────────────────────────────────────
OPENAI_MODEL = "gpt-4o-mini-tts"
OPENAI_VOICE = "marin"   # 他: cedar, coral, sage, ash, nova, onyx ...
OPENAI_STYLE = ("Read this like a calm, articulate American news anchor narrating an editorial. "
                "Speak clearly at a steady, moderate pace suitable for language learners shadowing, "
                "with natural intonation and brief pauses at commas and periods.")
ELEVEN_MODEL = "eleven_multilingual_v2"
ELEVEN_VOICE = "EXAVITQu4vr4xnYOxNgw"  # Sarah（米国女性）。Brian=nPczCjzI2devNBz1zQrb, George=JBFqnCBsd6RMkjVDRZzb


def load_env():
    f = HERE / ".env"
    if f.exists():
        for line in f.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def http_post(url, headers, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        sys.exit(f"\n✗ API エラー {e.code}: {e.read().decode(errors='replace')[:500]}")


def ffmpeg_bin():
    """Mac 以外（クラウド）用: ffmpeg を探し、なければ pip で入る静的バイナリを使う"""
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg
    except ImportError:
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "imageio-ffmpeg"], check=True)
        import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def to_wav(src, dst):
    """どんな音声でも 24kHz / mono / 16bit の WAV にそろえる"""
    if shutil.which("afconvert"):
        cmd = ["afconvert", str(src), str(dst), "-f", "WAVE", "-d", f"LEI16@{RATE_HZ}", "-c", "1"]
    else:
        cmd = [ffmpeg_bin(), "-y", "-loglevel", "error", "-i", str(src), "-ar", str(RATE_HZ), "-ac", "1",
               "-c:a", "pcm_s16le", str(dst)]
    subprocess.run(cmd, check=True, capture_output=True)


def to_m4a(src, dst):
    if shutil.which("afconvert"):
        cmd = ["afconvert", "-f", "m4af", "-d", "aac", "-b", "64000", str(src), str(dst)]
    else:
        cmd = [ffmpeg_bin(), "-y", "-loglevel", "error", "-i", str(src), "-c:a", "aac", "-b:a", "64k", str(dst)]
    subprocess.run(cmd, check=True, capture_output=True)


def synth(engine, text, ctx, opt):
    """1段落ぶんの音声ファイル（キャッシュ済みならそれ）のパスを返す"""
    key = hashlib.sha1(json.dumps([engine, opt, text], ensure_ascii=False).encode()).hexdigest()[:16]
    ext = "aiff" if engine == "say" else "mp3"
    path = CACHE / f"{engine}-{key}.{ext}"
    if path.exists():
        return path, True
    CACHE.mkdir(exist_ok=True)
    if engine == "say":
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
            f.write(text)
        subprocess.run(["say", "-v", opt["voice"], "-r", str(opt["rate"]), "-f", f.name, "-o", str(path)], check=True)
        os.unlink(f.name)
    elif engine == "openai":
        audio = http_post("https://api.openai.com/v1/audio/speech",
                          {"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}", "Content-Type": "application/json"},
                          {"model": opt["model"], "voice": opt["voice"], "input": text,
                           "instructions": opt["style"], "response_format": "mp3"})
        path.write_bytes(audio)
    elif engine == "eleven":
        audio = http_post(f"https://api.elevenlabs.io/v1/text-to-speech/{opt['voice']}?output_format=mp3_44100_128",
                          {"xi-api-key": os.environ["ELEVENLABS_API_KEY"], "Content-Type": "application/json"},
                          {"text": text, "model_id": opt["model"],
                           "previous_text": ctx[0], "next_text": ctx[1],   # 段落間のイントネーションを自然につなぐ
                           "voice_settings": {"stability": 0.55, "similarity_boost": 0.75,
                                              "style": 0.15, "speed": opt["speed"]}})
        path.write_bytes(audio)
    return path, False


def main():
    load_env()
    ap = argparse.ArgumentParser()
    ap.add_argument("content")
    ap.add_argument("--engine", choices=["say", "openai", "eleven"])
    ap.add_argument("--voice")
    ap.add_argument("--rate", type=int, help="say の読み上げ速度（語/分）")
    ap.add_argument("--speed", type=float, help="ElevenLabs の速度 0.7〜1.2")
    ap.add_argument("--out")
    a = ap.parse_args()

    src = Path(a.content).resolve()
    data = json.loads(src.read_text(encoding="utf-8"))
    meta = data["meta"]
    engine = a.engine or meta.get("engine") or (
        "eleven" if os.environ.get("ELEVENLABS_API_KEY") else
        "openai" if os.environ.get("OPENAI_API_KEY") else "say")
    need = {"openai": "OPENAI_API_KEY", "eleven": "ELEVENLABS_API_KEY"}.get(engine)
    if need and not os.environ.get(need):
        sys.exit(f"✗ {need} がありません。.env.example をコピーして .env を作り、キーを書いてください。")

    if engine == "say":
        opt = {"voice": a.voice or meta.get("voice", "Samantha"), "rate": a.rate or meta.get("rate", 145)}
    elif engine == "openai":
        opt = {"voice": a.voice or meta.get("openai_voice", OPENAI_VOICE), "model": OPENAI_MODEL,
               "style": meta.get("openai_style", OPENAI_STYLE)}
    else:
        opt = {"voice": a.voice or meta.get("eleven_voice", ELEVEN_VOICE), "model": ELEVEN_MODEL,
               "speed": a.speed or meta.get("eleven_speed", 0.95)}
    pause = float(meta.get("pause_sec", 1.0))
    out = Path(a.out) if a.out else HERE / "out" / src.stem
    out.mkdir(parents=True, exist_ok=True)
    print(f"音声エンジン: {engine}  ({opt.get('voice')})", file=sys.stderr)

    paras = [p["en"] for s in data["sections"] for p in s["paragraphs"]]
    timings, chunks = [], []
    silence = b"\x00\x00" * int(RATE_HZ * pause)
    t = 0.0
    with tempfile.TemporaryDirectory() as tmp:
        for i, text in enumerate(paras):
            ctx = (paras[i - 1] if i else "", paras[i + 1] if i + 1 < len(paras) else "")
            path, cached = synth(engine, text, ctx, opt)
            wav = Path(tmp) / f"p{i:03d}.wav"
            to_wav(path, wav)
            with wave.open(str(wav)) as w:
                fr = w.readframes(w.getnframes())
            dur = len(fr) / 2 / RATE_HZ
            timings.append([round(t, 2), round(t + dur, 2)])
            chunks += [fr, silence]
            t += dur + pause
            print(f"  ✓ 段落 {i+1:2d}/{len(paras)}  {dur:4.1f}s{'  (cache)' if cached else ''}", file=sys.stderr)
        full = Path(tmp) / "full.wav"
        with wave.open(str(full), "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(RATE_HZ)
            w.writeframes(b"".join(chunks))
        to_m4a(full, out / "audio.m4a")

    payload = dict(data, timings=timings, audio="audio.m4a")
    html = TEMPLATE.read_text(encoding="utf-8")
    html = html.replace("/*__DATA__*/null", json.dumps(payload, ensure_ascii=False))
    html = html.replace("__TITLE__", meta["title_en"])
    (out / "index.html").write_text(html, encoding="utf-8")

    words = sum(len(p.split()) for p in paras)
    total = timings[-1][1]
    print(f"\n完成: {out/'index.html'}\n  {words} words / 音声 {int(total//60)}分{int(total%60):02d}秒 / {engine}", file=sys.stderr)
    build_library()


def build_library():
    """out/index.html に、作った教材の一覧（新しい順）を書き出す"""
    import html as H
    rows = []
    for j in sorted((HERE / "lessons").glob("*.json"), reverse=True):
        if not (HERE / "out" / j.stem / "index.html").exists():
            continue
        m = json.loads(j.read_text(encoding="utf-8"))["meta"]
        rows.append(f'<a class="row" href="{H.escape(j.stem)}/index.html"><span class="d">{H.escape(j.stem[:10])}</span>'
                    f'<span class="s">{H.escape(m.get("series_ja", ""))}</span>'
                    f'<b>{H.escape(m["title_en"])}</b><span class="j">{H.escape(m["title_ja"])}</span></a>')
    page = f"""<!doctype html><html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Shadowing Library</title><style>
:root{{--bg:#e9e6df;--card:#fbfaf6;--ink:#1d1d1b;--muted:#6b6a65;--line:#d9d5cc}}
@media (prefers-color-scheme:dark){{:root{{--bg:#151514;--card:#22221f;--ink:#e9e7e1;--muted:#a3a19a;--line:#3a3935}}}}
body{{margin:0;background:var(--bg);color:var(--ink);font-family:"Hiragino Sans",system-ui,sans-serif;padding:32px 16px}}
main{{max-width:760px;margin:0 auto}} h1{{font-family:Georgia,serif;font-size:28px;margin:0 0 4px}} p{{color:var(--muted);margin:0 0 20px}}
.row{{display:grid;grid-template-columns:96px 1fr;gap:2px 12px;background:var(--card);padding:14px 18px;border-bottom:1px solid var(--line);color:inherit;text-decoration:none}}
.row:first-of-type{{border-radius:6px 6px 0 0}} .row:last-of-type{{border-radius:0 0 6px 6px;border:0}} .row:hover b{{text-decoration:underline}}
.d{{color:var(--muted);font-variant-numeric:tabular-nums}} .s{{font-size:12px;color:var(--muted);align-self:center}}
b{{grid-column:2;font-family:Georgia,serif;font-size:18px;font-weight:600}} .j{{grid-column:2;font-size:13px;color:var(--muted)}}
</style></head><body><main><h1>Shadowing Library</h1><p>{len(rows)} lessons</p>{''.join(rows)}</main></body></html>"""
    (HERE / "out" / "index.html").write_text(page, encoding="utf-8")


if __name__ == "__main__":
    main()
