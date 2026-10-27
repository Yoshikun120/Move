"""สายพานสรุปหนัง -> คลิปแนวตั้ง 9:16 พร้อมพากย์ไทย
ขั้นตอน: ถอดเสียง -> AI วางโครงเรื่อง -> พากย์ไทย (TTS) -> ตัดต่อ/ซับ/เอฟเฟกต์ -> รวมคลิป
"""
import asyncio
import json
import os
import re
import subprocess
from pathlib import Path

W, H = 1080, 1920
FPS = 30
FONT = os.getenv("SUB_FONT", "Noto Sans Thai")  # ติดตั้งฟอนต์ไทยในเครื่อง
SFX_DIR = Path(__file__).parent / "assets" / "sfx"      # ใส่ whoosh.mp3 ฯลฯ
MUSIC = Path(__file__).parent / "assets" / "music" / "bgm.mp3"  # เพลงประกอบ (ถ้ามี)

VOICES = {
    "female": "th-TH-PremwadeeNeural",
    "male": "th-TH-NiwatNeural",
}


def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"คำสั่งล้มเหลว: {' '.join(map(str, cmd[:3]))}...\n{r.stderr[-1500:]}")
    return r.stdout


def probe_duration(path):
    out = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
               "-of", "default=nk=1:nw=1", str(path)])
    return float(out.strip())


def has_audio(path):
    out = run(["ffprobe", "-v", "error", "-select_streams", "a",
               "-show_entries", "stream=index", "-of", "csv=p=0", str(path)])
    return bool(out.strip())


# 1) ถอดเสียง -----------------------------------------------------------
def transcribe(video, workdir, model_size="small"):
    wav = Path(workdir) / "audio.wav"
    run(["ffmpeg", "-y", "-i", str(video), "-vn", "-ac", "1", "-ar", "16000", str(wav)])
    from faster_whisper import WhisperModel
    model = WhisperModel(model_size, compute_type="int8")
    segs, info = model.transcribe(str(wav), vad_filter=True)
    items = [{"start": round(s.start, 2), "end": round(s.end, 2), "text": s.text.strip()}
             for s in segs if s.text.strip()]
    return items, info.language


# 2) AI วางโครงเรื่อง ------------------------------------------------------
STYLES = {
    "spoil": "สไตล์สปอยหนัง เล่าสนุก มีจังหวะลุ้น เล่าตั้งแต่ต้นจนจบรวมตอนจบ",
    "summary": "สไตล์สรุปเนื้อเรื่องให้เข้าใจง่าย เป็นกลาง กระชับ",
    "teaser": "สไตล์ทีเซอร์ เล่าครึ่งเรื่อง ปิดท้ายค้างคา ไม่บอกตอนจบ",
}


def make_plan(transcript, duration_total, style="spoil", target_sec=60, language="unknown"):
    import anthropic
    client = anthropic.Anthropic()  # ใช้ ANTHROPIC_API_KEY จาก environment
    lines = "\n".join(f"[{t['start']:.1f}-{t['end']:.1f}] {t['text']}" for t in transcript)
    lines = lines[:60000]
    prompt = f"""คุณเป็นนักเขียนสคริปต์คลิปสรุปหนัง/วิดีโอแนวตั้งสำหรับ TikTok/Reels/Shorts
วิดีโอต้นฉบับยาว {duration_total:.0f} วินาที ภาษา: {language}
ถอดเสียงพร้อมเวลา:
{lines}

งาน: วางโครงคลิปความยาวรวมประมาณ {target_sec} วินาที {STYLES.get(style, STYLES['spoil'])}
- เริ่มด้วย hook 1 ประโยคที่ดึงคนดู
- แบ่งเป็น 6-12 ฉาก แต่ละฉากเลือกช่วงเวลาจากวิดีโอต้นฉบับ (start/end เป็นวินาที ต้องอยู่ในช่วง 0-{duration_total:.0f}, ยาว 3-12 วินาที) ให้ตรงกับเนื้อหาที่เล่า
- narration เป็นภาษาไทยพูดธรรมชาติ ฉากละ 1-2 ประโยค พูดได้ภายในเวลาประมาณเท่ากับความยาวฉาก (คนไทยพูดราว 5-6 พยางค์/วินาที)
- sfx เลือกจาก: whoosh, hit, none

ตอบเป็น JSON อย่างเดียว ไม่มีข้อความอื่น:
{{"title": "...", "scenes": [{{"start": 0.0, "end": 6.0, "narration": "...", "sfx": "whoosh"}}]}}"""
    msg = client.messages.create(
        model=os.getenv("CLAUDE_MODEL", "claude-sonnet-5-5"),
        max_tokens=4000,
        messages=[{"role": "user", "content": prompt}],
    )
    text = msg.content[0].text
    m = re.search(r"\{.*\}", text, re.S)
    plan = json.loads(m.group(0))
    clean = []
    for s in plan["scenes"]:
        a = max(0.0, float(s["start"]))
        b = min(duration_total, float(s["end"]))
        if b - a >= 1.5 and s.get("narration"):
            clean.append({"start": a, "end": b, "narration": s["narration"].strip(),
                          "sfx": s.get("sfx", "none")})
    if not clean:
        raise RuntimeError("AI ไม่ได้ส่งฉากที่ใช้ได้กลับมา ลองใหม่อีกครั้ง")
    plan["scenes"] = clean
    return plan


# 3) พากย์ไทย -----------------------------------------------------------
async def _tts(text, voice, rate, out):
    import edge_tts
    await edge_tts.Communicate(text, voice, rate=rate).save(str(out))


def synth(text, out, voice="female", rate="+8%"):
    asyncio.run(_tts(text, VOICES.get(voice, VOICES["female"]), rate, out))
    return probe_duration(out)


# 4) ซับไตเติล (ASS) -----------------------------------------------------
def _chunks(text, max_chars=22):
    try:
        from pythainlp.tokenize import word_tokenize
        words = word_tokenize(text, keep_whitespace=False)
    except Exception:
        words = list(text)
    out, cur = [], ""
    for w in words:
        if cur and len(cur) + len(w) > max_chars:
            out.append(cur)
            cur = ""
        cur += w
    if cur:
        out.append(cur)
    return out


def _ts(t):
    h, m, s = int(t // 3600), int(t % 3600 // 60), t % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def write_ass(text, dur, path):
    parts = _chunks(text)
    total = sum(len(p) for p in parts) or 1
    t, events = 0.0, []
    for p in parts:
        d = dur * len(p) / total
        events.append(f"Dialogue: 0,{_ts(t)},{_ts(t + d)},Main,,0,0,0,,{p}")
        t += d
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}

[V4+ Styles]
Format: Name,Fontname,Fontsize,PrimaryColour,OutlineColour,BackColour,Bold,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV
Style: Main,{FONT},68,&H00FFFFFF,&H00000000,&H80000000,1,1,6,2,2,70,70,330

[Events]
Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text
"""
    Path(path).write_text(head + "\n".join(events), encoding="utf-8")


# 5) ตัดต่อฉาก -----------------------------------------------------------
def render_scene(video, scene, idx, workdir, voice="female"):
    wd = Path(workdir)
    nar = wd / f"nar_{idx}.mp3"
    nd = synth(scene["narration"], nar, voice) + 0.25
    clip_len = scene["end"] - scene["start"]
    factor = min(max(nd / clip_len, 0.7), 1.6)  # ปรับความเร็วภาพให้พอดีกับเสียงพากย์
    ass = wd / f"sub_{idx}.ass"
    write_ass(scene["narration"], nd, ass)
    out = wd / f"scene_{idx}.mp4"

    sfx = SFX_DIR / f"{scene.get('sfx', 'none')}.mp3"
    use_sfx = sfx.exists()
    src_audio = has_audio(video)

    cmd = ["ffmpeg", "-y", "-ss", str(scene["start"]), "-t", str(clip_len), "-i", str(video),
           "-i", str(nar)]
    if use_sfx:
        cmd += ["-i", str(sfx)]

    ass_path = str(ass).replace("\\", "/").replace(":", "\\:")
    vf = (f"[0:v]split[a][b];"
          f"[a]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},boxblur=30:5[bg];"
          f"[b]scale={W}:-2[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2,"
          f"setpts=PTS*{factor:.4f},fps={FPS},"
          f"tpad=stop_mode=clone:stop_duration={nd:.2f},trim=duration={nd:.2f},"
          f"setpts=PTS-STARTPTS,subtitles='{ass_path}'[v]")

    mix = ["[1:a]volume=1.0[n]"]
    labels = ["[n]"]
    if src_audio:
        mix.append(f"[0:a]atrim=0:{clip_len:.2f},volume=0.10[o]")
        labels.append("[o]")
    if use_sfx:
        mix.append("[2:a]volume=0.7[s]")
        labels.append("[s]")
    mix.append(f"{''.join(labels)}amix=inputs={len(labels)}:duration=longest:normalize=0,"
               f"atrim=0:{nd:.2f},apad=whole_dur={nd:.2f}[aout]")

    cmd += ["-filter_complex", vf + ";" + ";".join(mix),
            "-map", "[v]", "-map", "[aout]",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-ar", "44100", "-ac", "2", "-t", f"{nd:.2f}", str(out)]
    run(cmd)
    return out


def concat(scene_files, workdir, final):
    lst = Path(workdir) / "list.txt"
    lst.write_text("\n".join(f"file '{Path(p).resolve()}'" for p in scene_files))
    joined = Path(workdir) / "joined.mp4"
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(joined)])
    if MUSIC.exists():
        total = probe_duration(joined)
        run(["ffmpeg", "-y", "-i", str(joined), "-stream_loop", "-1", "-i", str(MUSIC),
             "-filter_complex",
             f"[1:a]volume=0.07,atrim=0:{total:.2f}[m];[0:a][m]amix=inputs=2:duration=first:normalize=0[a]",
             "-map", "0:v", "-map", "[a]", "-c:v", "copy", "-c:a", "aac", "-shortest", str(final)])
    else:
        Path(final).write_bytes(joined.read_bytes())
    return final


# รวมทุกขั้นตอน ---------------------------------------------------------
def process(video, workdir, style="spoil", target_sec=60, voice="female", progress=lambda p, m: None):
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    progress(5, "กำลังถอดเสียงจากวิดีโอ...")
    transcript, lang = transcribe(video, workdir)
    if not transcript:
        raise RuntimeError("ไม่พบเสียงพูดในวิดีโอ ระบบนี้ต้องใช้วิดีโอที่มีบทพูด")
    total = probe_duration(video)
    progress(25, "AI กำลังวางโครงเรื่อง...")
    plan = make_plan(transcript, total, style, target_sec, lang)
    (workdir / "plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2))
    files = []
    n = len(plan["scenes"])
    for i, sc in enumerate(plan["scenes"]):
        progress(35 + int(55 * i / n), f"กำลังตัดต่อฉากที่ {i + 1}/{n}...")
        files.append(render_scene(video, sc, i, workdir, voice))
    progress(94, "กำลังรวมคลิป...")
    final = workdir / "final.mp4"
    concat(files, workdir, final)
    progress(100, "เสร็จแล้ว")
    return final, plan
