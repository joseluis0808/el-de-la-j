from flask import Flask
import os
app = Flask(__name__)

@app.route("/")
def home():
    return "<h1>EL DE LA J - Clipper Web</h1><p>Pega tu link de YouTube - Sin titulo, sin subtitulo, calidad superior a 1080p</p><p>Tu pagina esta Live!</p>"

@app.route("/health")
def health():
    return "OK"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
