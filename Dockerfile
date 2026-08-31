FROM python:3.11-slim

# Dependencias del sistema: FFmpeg para transcodificación de audio
# --no-install-recommends mantiene la imagen lo más ligera posible
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ── Paso 1: instalar todas las dependencias declaradas ────────
# uvicorn>=0.30.0 satisface streamlit; los deps de spotdl se
# instalan aquí SIN la restricción uvicorn<0.24 de spotdl.
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# ── Paso 2: instalar spotdl sin sus deps declarados ───────────
# --no-deps evita que spotdl sobreescriba uvicorn con una versión
# incompatible con streamlit. Solo usamos "spotdl download" como
# subproceso CLI, nunca su servidor web (que es quien usa uvicorn).
RUN pip install --no-cache-dir --no-deps "spotdl>=4.5.2"

# ── Código fuente ─────────────────────────────────────────────
COPY app.py .

EXPOSE 8501

ENTRYPOINT ["streamlit", "run", "app.py", \
            "--server.port=8501", \
            "--server.address=0.0.0.0", \
            "--server.headless=true"]