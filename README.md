# 🐱 Sello de Gato Music

> **Aplicación web local y dockerizada que transforma tu biblioteca de Spotify en una colección de música offline organizada, etiquetada y validada con estándares de producción.**

## 📋 Tabla de Contenidos

- [¿Qué es Sello de Gato Music?](#-qué-es-sello-de-gato-music)
- [Arquitectura e Ingeniería](#-arquitectura-e-ingeniería)
- [Características Principales](#-características-principales)
- [Obtención del Archivo CSV (Paso Obligatorio)](#%EF%B8%8F-obtención-del-archivo-csv-paso-obligatorio)
- [Requisitos Previos](#-requisitos-previos)
- [Instalación y Configuración](#%EF%B8%8F-instalación-y-configuración)
- [Ejecución](#-ejecución)
- [Estructura del Proyecto](#-estructura-del-proyecto)
- [Tech Stack](#%EF%B8%8F-tech-stack)
- [Preguntas Frecuentes](#-preguntas-frecuentes)

---

## 🐱 ¿Qué es Sello de Gato Music?

**Sello de Gato Music** es una aplicación web local y 100% dockerizada que toma como entrada un archivo CSV exportado desde **[Exportify](https://exportify.net/)** y ofrece dos funcionalidades en una única interfaz Streamlit:

1. **Dashboard Analítico**: Estadísticas interactivas con Plotly sobre artistas, géneros, años de lanzamiento y audio-features de tu colección.
2. **Descargador Inteligente Multi-Hilo**: Descarga concurrente de canciones como MP3 de alta calidad con metadatos ID3 completos, carátula HD de iTunes y letras sincronizadas.

---

## 🏗 Arquitectura e Ingeniería

### Concurrencia Multi-Hilo con `ThreadPoolExecutor`

El motor de descarga utiliza un `ThreadPoolExecutor` configurable (1-5 workers) para procesar múltiples pistas simultáneamente. Cada worker opera con un **prefijo de archivo temporal único** (`_tmp_{task_idx}_`) que garantiza aislamiento total entre hilos y previene colisiones de archivos.

Un **lock global** (`threading.Lock`) protege todas las operaciones de I/O compartido:
- Escritura en la consola en vivo (`log_lines`)
- Persistencia en disco (`registro_descargas.txt`)
- Contadores de progreso (`success_count`, `progress`)
- Estado actual de la pista (`current_track`)

### Inyector de Contexto de Streamlit (`add_script_run_ctx`)

Streamlit vincula su `session_state` al hilo del script principal mediante un `ScriptRunContext`. Los hilos del pool no heredan este contexto de forma nativa, lo que provocaría un crash al intentar acceder a `st.session_state`.

El wrapper `_worker_wrapper()` inyecta el contexto del script activo en cada hilo del pool mediante `add_script_run_ctx(threading.current_thread())`, con fallback silencioso para compatibilidad entre versiones de Streamlit.

### Cancelación Cooperativa Thread-Safe

La clase `DownloadControl` encapsula un `threading.Event` compartido entre la UI y los hilos de descarga. El diseño cooperativo permite cancelar sin forzar interrupciones:
- Cada worker verifica `is_cancelled` en múltiples puntos estratégicos (antes de descargar, tras validar existencia, antes de cada motor).
- El orquestador deja de enviar tareas al pool y cancela los futures pendientes.

### 4 Capas de Validación Anti-Corrupción MP3

Cada pista pasa por cuatro capas de validación antes de aceptarse como exitosa:

| Capa | Nombre | Momento | Descripción |
|------|--------|---------|-------------|
| 1 | **Omisión Robusta** | Pre-descarga | Verifica existencia + tamaño > 100 KB + validación estructural con `is_valid_mp3()`. Si el archivo existe pero está corrupto, lo elimina y re-descarga. |
| 2 | **Validación de Duración** | Post-descarga | Compara la duración real del MP3 (Mutagen) contra `Duration (ms)` del CSV con tolerancia de ±35 segundos. |
| 3 | **Pre-verificación Web** | Pre-descarga | Consulta metadatos de YouTube vía `yt-dlp` con `download=False` para descartar videos cortos, teasers o Shorts (tolerancia ±40s). |
| 4 | **Validación Estructural** | Post-descarga | Intenta abrir el MP3 con Mutagen para detectar archivos falsos (`.webm`/`.m4a` renombrados a `.mp3`) o headers corruptos. |

Las capas 2 y 4 se ejecutan combinadas sobre el archivo temporal. El **renombrado atómico** (`os.replace`) al nombre final solo ocurre cuando todas las validaciones y la incrustación de metadatos han sido exitosas.

### Integración de Metadatos con iTunes API

Los metadatos se obtienen siguiendo una estrategia de prioridad:

1. **iTunes Search API** (fuente primaria): título, artista, álbum, género, año, carátula HD 1000×1000.
2. **CSV/DataFrame** (fallback): cualquier campo que iTunes no devuelva.
3. **Spotify audio-features** (siempre del CSV): Popularity, Danceability, Energy, Valence, Tempo, Explicit — incrustados como `COMM`.
4. **syncedlyrics**: letras `USLT` (texto plano) y `SYLT` (sincronizadas con timestamps en ms).

### Sesión HTTP Reutilizable

Un `requests.Session` global reutiliza conexiones TCP (keep-alive) para reducir latencia en llamadas repetitivas a iTunes, carátulas y syncedlyrics. Es thread-safe internamente gracias al locking del connection pool de urllib3.

### Registro Persistente Thread-Safe

El archivo `registro_descargas.txt` se escribe línea a línea con protección `_io_lock`. Se almacena junto a la música descargada y soporta filtros dinámicos en la UI (Errores / Éxitos / Omitidos / Todos).

---

## ✨ Características Principales

### 📊 Dashboard Analítico Interactivo

- **Métricas globales**: total de canciones, artistas únicos, álbumes, horas acumuladas, popularidad media y porcentaje de canciones explícitas.
- **Top 10 Artistas** más guardados (barras horizontales).
- **Distribución por Año de Lanzamiento** (histograma).
- **Scatter plot Energy vs. Valence** con hover interactivo por canción.
- **Ranking de Géneros** más escuchados.
- **Tabla de las 20 Canciones Más Populares** con audio-features.

### 📥 Descargador Multi-Motor con Cascada Automática

| Estrategia | Descripción |
|------------|-------------|
| **Solo yt-dlp** *(Recomendado)* | Busca en YouTube y descarga como MP3 VBR 0 (~320 kbps). |
| **Cascada Automática** | Intenta `spotdl` primero; si falla, recurre a `yt-dlp`. |
| **Solo spotdl** | Descarga directa integrada con Spotify (requiere credenciales). |

- **Ruta de descarga configurable desde la UI**: campo editable que apunta por defecto a la carpeta nativa `Música` del sistema operativo (`~/Music`).
- **Consola en vivo** con output en tiempo real.
- **Barra de progreso** canción a canción.
- **Panel de registro** con filtros por estado (Errores / Éxitos / Omitidos / Todos).
- **Descarga concurrente** configurable de 1 a 5 hilos simultáneos.
- **Cancelación en caliente** con reanudación posterior.
- **Reporte de errores** exportable como CSV.

### 🏷️ Etiquetas ID3 Incrustadas

Cada MP3 recibe automáticamente:

| Tag | Contenido |
|-----|-----------|
| `TIT2` | Título de la canción |
| `TPE1` | Artista(s) completo(s) |
| `TALB` | Nombre del álbum |
| `TDRC` | Año de lanzamiento |
| `TCON` | Género principal |
| `TRCK` | Número de pista / disco |
| `APIC` | Carátula HD (iTunes 1000×1000 > URL del CSV) |
| `USLT` | Letra no sincronizada (texto plano) |
| `SYLT` | Letra sincronizada con timestamps (estilo karaoke) |
| `COMM` | Audio-features de Spotify (Popularity, Danceability, Energy, Valence, Tempo) |

### 📁 Organización Automática de Carpetas

```
~/Music/                          ← Ruta base configurable
└── {Subcarpeta personalizada}/
    └── {Artista Principal}/
        ├── Canción, Álbum, Artista.mp3
        └── ...
```

> Los nombres se sanitizan automáticamente para eliminar caracteres inválidos del sistema de archivos.

---

## ⚠️ Obtención del Archivo CSV (Paso Obligatorio)

> **La aplicación requiere obligatoriamente un archivo CSV exportado desde [Exportify](https://exportify.net/).** Sin este archivo, la app no puede funcionar.

### Exportar tu biblioteca desde Exportify

**[Exportify](https://exportify.net/)** es una herramienta web gratuita que exporta cualquier lista de reproducción de Spotify como CSV con todos sus metadatos.

**Paso 1** — Navega a **[https://exportify.net/](https://exportify.net/)**

**Paso 2** — Haz clic en **"Log in with Spotify"** y autoriza el acceso (solo lectura).

**Paso 3** — Localiza **"❤️ Liked Songs"** en el listado de playlists.

**Paso 4** — Haz clic en **"Export"** para descargar el `.csv`.

**Paso 5** — En la barra lateral de la app, usa **"📂 Sube tu archivo CSV de Spotify"** para cargar el archivo.

> **💡 Consejo:** Para obtener columnas como `Genres`, `Popularity`, `Danceability`, `Energy`, `Valence` y `Tempo`, enriquece el CSV con la [Spotify Web API](https://developer.spotify.com/documentation/web-api). Estas columnas activan gráficos adicionales en el Dashboard y se incrustan como metadatos en los MP3.

---

## 🔧 Requisitos Previos

| Herramienta | Versión Mínima | Descripción |
|-------------|----------------|-------------|
| **Docker Desktop** | 24.x+ | Motor de contenedores | 
| **Docker Compose** | v2.x+ | Incluido en Docker Desktop |

### Credenciales de Spotify *(Solo para modo spotdl)*

Si usas **"Solo spotdl"** o **"Cascada Automática"**:

1. Accede al **[Spotify for Developers Dashboard](https://developer.spotify.com/dashboard)**.
2. Crea una aplicación y copia tu **Client ID** y **Client Secret**.
3. Ingrésalos directamente en la interfaz de la app en tiempo de ejecución.

---

## ⚙️ Instalación y Configuración

### 1. Clonar el repositorio

```bash
git clone https://github.com/GAOT539/music-organizer-downloader.git
cd music-organizer-downloader
```

### 2. Configurar variables de entorno

Crea tu archivo `.env` a partir de la plantilla:

```bash
# Linux / macOS
cp .env.example .env

# Windows CMD
copy .env.example .env

# Windows PowerShell
Copy-Item .env.example .env
```

El proyecto usa la variable `HOST_MUSIC_DIR` para definir qué carpeta del host se monta como volumen de salida en Docker.

**Si no defines `HOST_MUSIC_DIR`**, docker-compose usará `~/Music` automáticamente (la carpeta nativa de Música del sistema).

Para usar una ruta personalizada, descomenta y edita en tu `.env`:

```dotenv
# Windows
HOST_MUSIC_DIR=C:\Users\TuUsuario\Music

# Linux
# HOST_MUSIC_DIR=/home/tuusuario/Music

# macOS
# HOST_MUSIC_DIR=/Users/tuusuario/Music
```

> **🔐 Seguridad:** El archivo `.env` está en `.gitignore` y nunca debe subirse al repositorio.

---

## 🚀 Ejecución

### Construir y levantar el contenedor

```bash
docker compose up --build
```

- `--build` reconstruye la imagen. Úsalo en la primera ejecución o tras modificar dependencias.
- La primera vez puede tardar 5-15 minutos mientras descarga la imagen base, FFmpeg y dependencias Python.

### Acceder a la aplicación

```
http://localhost:8501
```

### Detener el contenedor

```bash
docker compose stop          # Detener sin eliminar
docker compose down          # Detener y eliminar
```

### Ejecuciones posteriores

```bash
docker compose up            # Sin reconstruir (rápido)
```

---

## 📂 Estructura del Proyecto

```
music-organizer-downloader/
│
├── app.py                  # Aplicación Streamlit (UI + orquestador multi-hilo)
├── logo.png                # Logotipo de Sello de Gato Music
│
├── Dockerfile              # Imagen basada en python:3.11-slim con FFmpeg
├── docker-compose.yml      # Servicio, puertos y volumen → ~/Music
│
├── requirements.txt        # Dependencias Python
│
├── .env                    # Variables de entorno locales (NO en Git)
├── .env.example            # Plantilla de variables de entorno
├── .gitignore              # Exclusiones de Git
│
├── data/                   # Datos temporales del contenedor
└── ~/Music/                # Salida por defecto (carpeta Música del sistema)
    └── {Subcarpeta}/
        └── {Artista}/
            ├── Cancion, Album, Artista.mp3
            └── registro_descargas.txt
```

---

## 🛠️ Tech Stack

| Categoría | Tecnología | Versión | Rol |
|-----------|-----------|---------|-----|
| **UI** | [Streamlit](https://streamlit.io/) | ≥ 1.62 | Interfaz web interactiva |
| **Data** | [Pandas](https://pandas.pydata.org/) | ≥ 3.0 | Procesamiento del CSV |
| **Gráficos** | [Plotly Express](https://plotly.com/python/plotly-express/) | ≥ 7.0 | Visualizaciones del Dashboard |
| **Descarga 1** | [yt-dlp](https://github.com/yt-dlp/yt-dlp) | ≥ 2026.8 | Audio desde YouTube |
| **Descarga 2** | [spotdl](https://github.com/spotDL/spotify-downloader) | ≥ 4.5.2 | Descarga directa Spotify |
| **Metadata** | [Mutagen](https://mutagen.readthedocs.io/) | ≥ 1.48 | Etiquetas ID3 en MP3 |
| **Letras** | [syncedlyrics](https://github.com/moehmeni/syncedlyrics) | ≥ 1.0 | Letras LRC sincronizadas |
| **Spotify API** | [Spotipy](https://spotipy.readthedocs.io/) | ≥ 2.26 | Cliente API (para spotdl) |
| **HTTP** | [Requests](https://docs.python-requests.org/) | ≥ 2.34 | iTunes API + carátulas |
| **Imágenes** | [Pillow](https://pillow.readthedocs.io/) | ≥ 12.3 | Logo de la app |
| **Audio** | [FFmpeg](https://ffmpeg.org/) | Sistema | Transcodificación a MP3 |
| **Container** | [Docker](https://www.docker.com/) | 24.x+ | Aislamiento y portabilidad |
| **Base Image** | `python:3.11-slim` | — | Imagen Docker ligera |

---

## ❓ Preguntas Frecuentes

<details>
<summary><strong>¿Por qué el primer arranque tarda tanto?</strong></summary>

La primera vez que ejecutas `docker compose up --build`, Docker descarga la imagen base, instala FFmpeg y todas las dependencias Python. Puede durar entre 5 y 15 minutos. Las ejecuciones posteriores son casi instantáneas gracias al caché de capas de Docker.

</details>

<details>
<summary><strong>¿Qué formato y calidad tienen los archivos?</strong></summary>

Todos los archivos se descargan como **MP3** a máxima calidad (VBR 0 con yt-dlp ≈ 320 kbps, 320k explícito con spotdl). Los metadatos ID3v2 completos se incrustan en cada archivo, incluyendo carátula HD de iTunes (1000×1000) y letras sincronizadas.

</details>

<details>
<summary><strong>¿Dónde se guardan los archivos?</strong></summary>

Por defecto, en la carpeta **Música** de tu sistema operativo (`~/Music`). Puedes cambiar la ruta directamente desde la UI de la app (campo "Ruta Base de Descarga") o mediante la variable `HOST_MUSIC_DIR` en el `.env` para Docker.

</details>

<details>
<summary><strong>¿Cómo funciona la cancelación de descargas?</strong></summary>

El botón "🛑 Cancelar Descarga" activa un `threading.Event` compartido. Cada worker verifica este flag en múltiples puntos estratégicos y se detiene cooperativamente. La pista que está en proceso de descarga terminará antes de detenerse. Al presionar "Iniciar / Reanudar", la app retoma desde donde se quedó (las canciones ya descargadas se omiten automáticamente).

</details>

<details>
<summary><strong>La columna "Genres" no aparece en mi CSV</strong></summary>

La columna `Genres` no siempre está en la exportación de Exportify. Si no está presente, los gráficos de géneros no aparecerán pero la app funciona normalmente. Para obtenerla, enriquece el CSV con el endpoint `/artists` de la Spotify Web API.

</details>

<details>
<summary><strong>¿Es necesario el .env para ejecutar sin Docker?</strong></summary>

No. En ejecución local (sin Docker), la app detecta automáticamente la carpeta Música del sistema mediante `pathlib.Path.home() / 'Music'`. El `.env` solo es necesario para configurar el volumen de Docker.

</details>

---

<div align="center">

**Sello de Gato Music** — Hecho con 🐱 y Python

</div>
