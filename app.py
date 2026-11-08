from flask import Flask, request, jsonify, send_file
import os, re, json, subprocess, sys, tempfile, shutil, time
app = Flask(__name__)

# --- Cargar HTML ---
BASE = os.path.dirname(__file__)
def load_html():
    p = os.path.join(BASE, "index.html")
    if os.path.exists(p):
        return open(p, encoding="utf-8").read()
    return HTML_FALLBACK

@app.route("/")
def home():
    return load_html()

@app.route("/health")
def health():
    return "OK"

def get_video_info(url):
    """Duracion + titulo real con yt-dlp"""
    title = "Video detectado"
    duration = None
    duration_str = "auto"
    try:
        out = subprocess.run(["yt-dlp", "-j", "--no-playlist", url], capture_output=True, text=True, timeout=60)
        if out.stdout.strip():
            data = json.loads(out.stdout.splitlines()[0])
            title = data.get("title") or title
            duration = data.get("duration")
            if duration:
                m = int(duration)//60
                s = int(duration)%60
                duration_str = f"{m}:{s:02d} ({int(duration)}s)"
    except Exception as e:
        print("yt-dlp info error", e)
    # fallback get-duration
    if not duration:
        try:
            out = subprocess.run(["yt-dlp", "--get-duration", "--get-title", url], capture_output=True, text=True, timeout=30)
            lines=[l.strip() for l in out.stdout.splitlines() if l.strip()]
            if len(lines)>=2:
                title=lines[0]; duration_str=lines[-1]
        except: pass
    return title, duration, duration_str

def get_transcript(url):
    """Intenta bajar subtitulos automaticos para que Gemini entienda el video"""
    try:
        tmp = tempfile.mkdtemp()
        subprocess.run(["yt-dlp", "--write-auto-subs", "--sub-langs", "en.*,es.*", "--skip-download", "-o", os.path.join(tmp,"sub.%(ext)s"), url], capture_output=True, timeout=60)
        txt=""
        for f in os.listdir(tmp):
            if f.endswith(".vtt"):
                t=open(os.path.join(tmp,f), encoding="utf-8", errors="ignore").read()
                t=re.sub(r"<[^>]+>","",t)
                txt+=t[:12000]
                break
        shutil.rmtree(tmp, ignore_errors=True)
        return txt[:12000]
    except Exception as e:
        print("transcript error", e)
        return ""

def gemini_clips(title, duration, transcript, mode, url):
    """Llama a Gemini con la API key de Render"""
    api_key = os.environ.get("GEMINI_API_KEY","").strip()
    if not api_key:
        return None, "Falta GEMINI_API_KEY en Render > Environment"
    try:
        from google import genai
        client = genai.Client(api_key=api_key)
        # duracion objetivo
        if mode=="largo":
            dur_instr="Clips de 30 a 60 segundos. Solo momentos que duren mas de 30 segundos y que funcionen como Short completo."
            n="6"
        else:
            dur_instr="Clips de 8 a 15 segundos. Empiezan directo en la accion, sin intro."
            n="Sin limite: si el video es largo, devuelve 20-30 clips, uno por cada momento viral. Si es corto, devuelve todos los que haya."
        prompt = f"""Eres un clipper viral estilo OpusClip. Analiza este video y devuelve SOLO JSON.

Video: {title}
URL: {url}
Duracion total en segundos: {duration}
Modo: {mode} - {dur_instr}
Transcripcion / subtitulos (puede estar incompleta):
{transcript[:8000]}

REGLAS:
- Sin titulo quemado, sin subtitulo, sin MEME, sin narracion. Audio original.
- Vertical 9:16, calidad 2160x3840, sin blur, sin barras.
- Cada clip: start (segundo inicio), end (segundo fin), title (titulo corto para publicar, MAYUSCULAS, sin acentos, sin nombres famosos con ENTITY, max 4 palabras).
- {n}
- Devuelve SOLO este JSON: {{"clips": [{{"start": 0, "end": 12, "title": "TITULO CORTO"}}]}}
"""
        resp = client.models.generate_content(model="gemini-3-flash-preview", contents=prompt)
        txt = resp.text or ""
        # extraer JSON
        m = re.search(r"\{.*\}", txt, re.S)
        if not m:
            return None, f"Gemini no devolvio JSON: {txt[:300]}"
        data = json.loads(m.group(0))
        clips=[]
        for i,c in enumerate(data.get("clips",[])[:30]):
            try:
                s=int(float(c.get("start",0))); e=int(float(c.get("end", s+12)))
                if e<=s: e=s+12
                if mode=="largo":
                    e=min(e, s+60)
                else:
                    e=min(e, s+15)
                clips.append({"n":i+1,"title":str(c.get("title","MOMENTO VIRAL")).upper()[:40],"start":s,"end":e,"dur":f"0:{e-s:02d}" if e-s<60 else f"{(e-s)//60}:{(e-s)%60:02d}"})
            except: continue
        if not clips:
            return None, "Gemini devolvio 0 clips"
        return clips, None
    except Exception as e:
        return None, f"Error Gemini: {str(e)[:400]}"

@app.route("/api/generate", methods=["POST"])
def generate():
    data = request.get_json(force=True, silent=True) or {}
    url = (data.get("url") or "").strip()
    mode = data.get("mode","rapido")
    if not url:
        return jsonify({"error":"Pega tu link primero"}), 400
    title, duration, duration_str = get_video_info(url)
    transcript = get_transcript(url)
    clips, err = gemini_clips(title, duration, transcript, mode, url)
    if err or not clips:
        # Fallback honesto: NO demo falso, devolvemos error real para que lo veas
        return jsonify({"title":title,"duration":duration_str,"mode":mode,"clips":[],"note":err or "No se pudieron generar clips","is_demo":False}), 200
    return jsonify({"title":title,"duration":duration_str,"mode":mode,"clips":clips,"note":f"Analizado con Gemini • {len(clips)} momentos reales de este video • Sin titulo quemado, sin subtitulo, sin MEME, audio original, 9:16","is_demo":False})


def get_ffmpeg():
    import shutil
    p = shutil.which("ffmpeg")
    if p:
        return p
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as e:
        return "ffmpeg"

@app.route("/api/download")
def download_clip():
    url = request.args.get("url","")
    try:
        start = int(float(request.args.get("start","0")))
        end = int(float(request.args.get("end","12")))
    except:
        start, end = 0, 12
    title = request.args.get("title","CLIP") or "CLIP"
    title = re.sub(r"[^A-Za-z0-9_\\- ]","", title).strip().replace(" ","_")[:30] or "CLIP"
    if not url:
        return "Falta url", 400
    if end<=start:
        end=start+12
    if end-start>70:
        end=start+60
    tmp = tempfile.mkdtemp()
    try:
        # 1. Descargar video en mejor calidad disponible
        out_template = os.path.join(tmp, "full.%(ext)s")
        dl = subprocess.run(["yt-dlp","-f","bestvideo[height<=1080]+bestaudio/best[height<=1080]/best","--merge-output-format","mp4","-o",out_template, url], capture_output=True, text=True, timeout=300)
        files = [os.path.join(tmp,f) for f in os.listdir(tmp) if f.startswith("full.")]
        if not files:
            return f"No se pudo descargar el video. yt-dlp: {dl.stdout[-500:]} {dl.stderr[-500:]}", 500
        src = files[0]
        ffmpeg = get_ffmpeg()
        out = os.path.join(tmp, f"{title}_{start}_{end}.mp4")
        # Vertical 9:16 lleno, sin blur, sin barras, zoom centro, sin texto, audio original, crf 16
        # crop a 9:16 centrado y escala a 1080x1920
        vf = "crop=ih*9/16:ih,scale=1080:1920:flags=lanczos"
        cmd = [ffmpeg,"-y","-ss",str(start),"-i",src,"-t",str(end-start),"-vf",vf,"-c:v","libx264","-preset","medium","-crf","16","-c:a","aac","-b:a","192k","-movflags","+faststart", out]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if not os.path.exists(out) or os.path.getsize(out)<10000:
            return f"FFmpeg fallo: {r.stderr[-800:]}", 500
        return send_file(out, as_attachment=True, download_name=f"{title}.mp4", mimetype="video/mp4")
    except Exception as e:
        return f"Error cortando: {str(e)}", 500


HTML_FALLBACK = open(os.path.join(BASE,"index.html"),encoding="utf-8").read() if os.path.exists(os.path.join(BASE,"index.html")) else "<h1>EL DE LA J</h1><p>Sube index.html</p>"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
