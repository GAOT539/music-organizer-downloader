## 📋 Tabla de Contenidos

- [¿Qué es Sello de Gato Music?](#-qué-es-sello-de-gato-music)
- [✨ Características Principales](#-características-principales)
- [⚠️ Obtención del Archivo CSV (Paso Obligatorio)](#️-obtención-del-archivo-csv-paso-obligatorio)
- [🔧 Requisitos Previos](#-requisitos-previos)
- [⚙️ Instalación y Configuración](#️-instalación-y-configuración)
- [🚀 Ejecución](#-ejecución)
- [📂 Estructura del Proyecto](#-estructura-del-proyecto)
- [🛠️ Tech Stack](#️-tech-stack)
- [❓ Preguntas Frecuentes](#-preguntas-frecuentes)

---

## 🐱 ¿Qué es Sello de Gato Music?

**Sello de Gato Music** es una aplicación web local y 100% dockerizada que transforma tu biblioteca de Spotify en una colección de música offline perfectamente organizada y etiquetada.

La app toma como entrada un archivo CSV exportado de tu lista de reproducción de Spotify y te ofrece dos funcionalidades principales en una única interfaz:

1. **Dashboard Analítico**: Explora visualmente tu colección con estadísticas y gráficos interactivos sobre tus artistas, géneros, años de lanzamiento y audio-features.
2. **Descargador Inteligente**: Descarga cada canción como MP3 de alta calidad con todos sus metadatos ID3 incrustados automáticamente (título, artista, álbum, año, género, carátula HD y letras sincronizadas).

Todo corre en un contenedor Docker, sin instalaciones manuales de dependencias complejas.

---

## ✨ Características Principales

### 📊 Dashboard Analítico Interactivo

- **Métricas globales** de tu colección: total de canciones, artistas únicos, álbumes, horas acumuladas, popularidad media y porcentaje de canciones explícitas.
- **Gráfico de Top 10 Artistas** más guardados (barras horizontales).
- **Histograma de distribución por Año de Lanzamiento** de tu biblioteca.
- **Scatter plot Energy vs. Valence** con hover interactivo por canción (si el CSV incluye audio-features).
- **Ranking de Géneros** más escuchados, expandiendo géneros separados por coma.
- **Tabla de las 20 Canciones Más Populares** con todos sus audio-features.

### 📥 Descargador Multi-Motor con Cascada Automática

Elige entre tres estrategias de descarga para maximizar el éxito:

| Estrategia                              | Descripción                                                                                         |
| --------------------------------------- | ---------------------------------------------------------------------------------------------------- |
| **Solo yt-dlp** *(Recomendado)* | Rápido y confiable. Busca la canción en YouTube y la descarga como MP3 320kbps.                    |
| **Cascada Automática**           | Intenta primero con`spotdl` (directo de Spotify). Si falla, recurre automáticamente a `yt-dlp`. |
| **Solo spotdl**                   | Usa la integración directa con la API de Spotify para descargar. Requiere credenciales.             |

- **Consola en vivo** con output en tiempo real durante la descarga.
- **Barra de progreso** que refleja el estado canción a canción.
- **Reporte de errores** al finalizar: genera y exporta un CSV con las canciones que no pudieron descargarse.
- **Verificación de duplicados**: omite automáticamente los archivos ya descargados.

### 🏷️ Incrustación Completa de Etiquetas ID3

Cada MP3 descargado recibe automáticamente:

- `TIT2` — Título de la canción
- `TPE1` — Artista(s) completo(s)
- `TALB` — Nombre del álbum
- `TDRC` — Año de lanzamiento
- `TCON` — Género principal
- `TRCK` — Número de pista / disco
- `APIC` — Carátula HD descargada desde la URL del CSV
- `USLT` — Letra no sincronizada (texto plano)
- `SYLT` — Letra sincronizada con timestamps en milisegundos (estilo karaoke)
- `COMM` — Audio-features de Spotify como comentario (Popularidad, Danceability, Energy, Valence, Tempo)

### 📁 Organización Automática de Carpetas

Los archivos se organizan con la siguiente jerarquía:

```
music_export/
└── {Carpeta Raíz Personalizada}/
    └── {Artista Principal}/
        └── {Título}, {Álbum}, {Artista}.mp3
```

> Los nombres se sanitizan automáticamente para eliminar caracteres inválidos en el sistema de archivos.

---

## ⚠️ Obtención del Archivo CSV (Paso Obligatorio)

> **Este paso es fundamental.** La aplicación **no puede funcionar** sin un archivo CSV válido exportado de Spotify. A continuación se explica cómo obtenerlo.

### Exportar tu biblioteca desde Exportify

**[Exportify](https://exportify.net/)** es una herramienta web gratuita que permite exportar cualquier lista de reproducción de Spotify como archivo CSV con todos sus metadatos.

Sigue estos pasos:

---

**Paso 1 — Abrir Exportify**

Navega a **[https://exportify.net/](https://exportify.net/)** en tu navegador.

---

**Paso 2 — Iniciar sesión con Spotify**

Haz clic en el botón **"Log in with Spotify"** y autoriza el acceso. Exportify solo necesita permisos de lectura; no modifica tu cuenta.

---

**Paso 3 — Localizar "Liked Songs" (Tus Me Gusta)**

Una vez autenticado, verás el listado de todas tus playlists. Busca la fila llamada:

```
❤️  Liked Songs
```

---

**Paso 4 — Exportar el CSV**

Haz clic en el botón **"Export"** que aparece junto a la playlist. Se descargará automáticamente un archivo `.csv` con el nombre de la playlist (por ejemplo, `Liked Songs.csv`).

---

**Paso 5 — Cargar el CSV en la aplicación**

En la barra lateral izquierda de la app, usa el selector de archivos **"📂 Sube tu archivo CSV de Spotify"** y carga el archivo descargado. El Dashboard y el Descargador se activarán automáticamente.

> **💡 Consejo Pro:** Para obtener columnas adicionales como `Genres`, `Popularity`, `Danceability`, `Energy`, `Valence` y `Tempo`, considera enriquecer el CSV con la [Spotify Web API](https://developer.spotify.com/documentation/web-api). Estas columnas activan gráficos adicionales en el Dashboard y permiten incrustar audio-features como comentarios en los MP3.

---

## 🔧 Requisitos Previos

Asegúrate de tener instalado lo siguiente en tu sistema **antes** de continuar:

| Herramienta              | Versión Mínima | Descripción                             | Enlace                                                      |
| ------------------------ | ---------------- | ---------------------------------------- | ----------------------------------------------------------- |
| **Docker Desktop** | 24.x+            | Motor de contenedores                    | [Descargar](https://www.docker.com/products/docker-desktop/) |
| **Docker Compose** | v2.x+            | Orquestador (incluido en Docker Desktop) | [Docs](https://docs.docker.com/compose/)                     |

### Credenciales de Spotify for Developers *(Solo para modo spotdl)*

Si planeas usar la estrategia **"Solo spotdl"** o **"Cascada Automática"**, necesitarás un par de credenciales de la API de Spotify:

1. Accede al **[Spotify for Developers Dashboard](https://developer.spotify.com/dashboard)**.
2. Inicia sesión con tu cuenta de Spotify.
3. Crea una nueva aplicación (el nombre es arbitrario).
4. Copia tu **Client ID** y tu **Client Secret**.

> Estas credenciales se ingresan directamente en la interfaz de la app en tiempo de ejecución. **No es necesario configurarlas en el `.env`.**

---

## ⚙️ Instalación y Configuración

### 1. Clonar el repositorio

```bash
git clone https://github.com/GAOT539/music-organizer-downloader.git
cd music-organizer-downloader
```

### 2. Configurar las variables de entorno

El proyecto utiliza un archivo `.env` para definir la variable `HOST_DOWNLOAD_DIR`, que apunta a la carpeta local de tu sistema donde Docker montará el volumen de salida de música.

**Crea tu archivo `.env` a partir de la plantilla incluida:**

```bash
# Linux / macOS
cp .env.example .env

# Windows CMD
copy .env.example .env

# Windows PowerShell
Copy-Item .env.example .env
```

**Edita el archivo `.env` y ajusta la ruta según tu sistema operativo:**

```dotenv
# --- Windows ---
HOST_DOWNLOAD_DIR=C:\Users\TuUsuario\Downloads

# --- Linux / macOS ---
# HOST_DOWNLOAD_DIR=/home/tuusuario/Downloads
```

> **🔐 Seguridad:** El archivo `.env` está incluido en `.gitignore` y **nunca** debe subirse al repositorio. Contiene rutas locales específicas de cada máquina.

### ¿Por qué es necesario el `.env`?

Docker necesita saber qué carpeta de tu sistema operativo anfitrión (host) debe montar dentro del contenedor. La variable `HOST_DOWNLOAD_DIR` hace que el proyecto sea **completamente portable**: cada desarrollador o usuario define su propia ruta local sin necesidad de modificar el `docker-compose.yml`.

---

## 🚀 Ejecución

### Construir y levantar el contenedor

Desde la raíz del proyecto, ejecuta:

```bash
docker compose up --build
```

- `--build` fuerza la reconstrucción de la imagen Docker. Úsalo siempre en la primera ejecución o tras modificar `requirements.txt` o el `Dockerfile`.
- El proceso instalará todas las dependencias dentro del contenedor (puede tardar algunos minutos la primera vez).

### Acceder a la aplicación

Una vez que veas en la consola el mensaje:

```
You can now view your Streamlit app in your browser.
  URL: http://0.0.0.0:8501
```

Abre tu navegador y navega a:

**[http://localhost:8501](http://localhost:8501)**

### Detener el contenedor

```bash
# Detener sin eliminar los contenedores
docker compose stop

# Detener y eliminar los contenedores
docker compose down
```

### Ejecuciones posteriores (sin reconstruir)

Si no realizaste cambios en las dependencias, puedes iniciar el proyecto más rápido con:

```bash
docker compose up
```

---

## 📂 Estructura del Proyecto

```
music-organizer-downloader/
│
├── app.py                  # Aplicación principal de Streamlit (UI + lógica de descarga)
├── logo.png                # Logotipo de Sello de Gato Music
│
├── Dockerfile              # Imagen Docker basada en python:3.11-slim con FFmpeg
├── docker-compose.yml      # Definición del servicio, puertos y volúmenes
│
├── requirements.txt        # Dependencias de Python (Streamlit, yt-dlp, mutagen, etc.)
│
├── .env                    # Variables de entorno locales (NO subir a Git)
├── .env.example            # Plantilla de variables de entorno (sí incluida en Git)
├── .gitignore              # Archivos y carpetas excluidos del control de versiones
│
├── data/                   # Carpeta interna del contenedor para datos temporales
└── music_export/           # Carpeta de salida: aquí se guardan los MP3 descargados
    └── {Carpeta Raíz}/
        └── {Artista}/
            └── Cancion, Album, Artista.mp3
```

---

## 🛠️ Tech Stack

| Categoría                     | Tecnología                                                | Versión  | Descripción                                     |
| ------------------------------ | ---------------------------------------------------------- | --------- | ------------------------------------------------ |
| **UI Framework**         | [Streamlit](https://streamlit.io/)                          | ≥ 1.62   | Framework para la interfaz web interactiva       |
| **Data Processing**      | [Pandas](https://pandas.pydata.org/)                        | ≥ 3.0    | Carga, limpieza y procesamiento del CSV          |
| **Visualización**       | [Plotly Express](https://plotly.com/python/plotly-express/) | ≥ 7.0    | Gráficos interactivos del Dashboard             |
| **Motor de Descarga 1**  | [yt-dlp](https://github.com/yt-dlp/yt-dlp)                  | ≥ 2026.8 | Descarga de audio desde YouTube                  |
| **Motor de Descarga 2**  | [spotdl](https://github.com/spotDL/spotify-downloader)      | ≥ 4.5.2  | Descarga directa integrada con Spotify           |
| **Metadata de Audio**    | [Mutagen](https://mutagen.readthedocs.io/)                  | ≥ 1.48   | Escritura de etiquetas ID3 en archivos MP3       |
| **Letras Sincronizadas** | [syncedlyrics](https://github.com/moehmeni/syncedlyrics)    | ≥ 1.0    | Obtención de letras LRC con timestamps          |
| **API de Spotify**       | [Spotipy](https://spotipy.readthedocs.io/)                  | ≥ 2.26   | Cliente de la Spotify Web API (usado por spotdl) |
| **Imágenes**            | [Pillow](https://pillow.readthedocs.io/)                    | ≥ 12.3   | Manejo del logotipo de la página                |
| **Transcodificación**   | [FFmpeg](https://ffmpeg.org/)                               | Sistema   | Conversión de audio a MP3 (instalado en Docker) |
| **Contenerización**     | [Docker](https://www.docker.com/)                           | 24.x+     | Aislamiento y portabilidad del entorno           |
| **Base Image**           | `python:3.11-slim`                                       | —        | Imagen Docker base ligera                        |

---

## ❓ Preguntas Frecuentes

<details>
<summary><strong>¿Por qué el primer arranque tarda tanto?</strong></summary>

La primera vez que ejecutas `docker compose up --build`, Docker descarga la imagen base, instala FFmpeg y todas las dependencias de Python. Este proceso puede durar entre 5 y 15 minutos dependiendo de tu conexión. Las ejecuciones posteriores son casi instantáneas gracias al caché de capas de Docker.

</details>

<details>
<summary><strong>¿Qué formato tienen los archivos descargados?</strong></summary>

Todos los archivos se descargan y convierten a **MP3 a máxima calidad (VBR 0 con yt-dlp, 320kbps con spotdl)**. Los metadatos ID3v2 completos se incrustan directamente en el archivo, incluyendo carátula HD y letras cuando están disponibles.

</details>

<details>
<summary><strong>¿Dónde se guardan los archivos descargados?</strong></summary>

Los archivos se guardan dentro de la carpeta `music_export/` en la raíz del proyecto (montada como volumen en Docker). Puedes cambiar la ruta del volumen en el archivo `.env` con la variable `HOST_DOWNLOAD_DIR`.

</details>

<details>
<summary><strong>La columna "Genres" no aparece en mi CSV. ¿Qué hago?</strong></summary>

La columna `Genres` no siempre está disponible en la exportación estándar de Exportify. Si no está presente, los gráficos de géneros simplemente no aparecerán, pero el resto de la aplicación funcionará con normalidad. Para obtenerla, puedes enriquecer el CSV consultando el endpoint [`/artists`](https://github.com/pavelkomarov/exportify) de la Spotify Web API.

</details>

---

<div align="center">
