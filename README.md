# 🐱 Sello de Gato Music — V1.1

> **Aplicación web local y dockerizada que transforma tu biblioteca de Spotify en una colección de música offline organizada, etiquetada y validada con estándares de producción.**

---

## 📋 Tabla de Contenidos

- [¿Qué es Sello de Gato Music?](#-qué-es-sello-de-gato-music)
- [Obtención del Archivo CSV](#%EF%B8%8F-obtención-del-archivo-csv-paso-obligatorio)
- [Arquitectura e Ingeniería](#-arquitectura-e-ingeniería)
- [Características V1.1](#-características-v11)
- [Requisitos Previos](#-requisitos-previos)
- [Instalación y Configuración](#%EF%B8%8F-instalación-y-configuración)
- [Ejecución](#-ejecución)
- [Estructura del Proyecto](#-estructura-del-proyecto)
- [Tech Stack](#%EF%B8%8F-tech-stack)
- [Preguntas Frecuentes](#-preguntas-frecuentes)

---

## 🐱 ¿Qué es Sello de Gato Music?

**Sello de Gato Music** es una aplicación web local y 100% dockerizada que toma como entrada un archivo CSV exportado desde **[Exportify](https://exportify.net/)** y ofrece estos módulos en una única interfaz Streamlit:

1. **Dashboard Analítico**: Estadísticas interactivas con Plotly sobre artistas, géneros, años de lanzamiento y audio-features de tu colección.
2. **Descargador Inteligente Multi-Hilo**: Descarga concurrente de canciones como MP3 de alta calidad con metadatos ID3 completos, carátula HD y letras sincronizadas.
3. **Limpieza de carpeta**: Indexa los MP3 de `Mi Musica` en una base de datos SQLite y permite consultar las carpetas y canciones encontradas.
4. **Fusión y normalización**: Detecta carpetas equivalentes por nombre, mueve los archivos a una carpeta normalizada y elimina duplicados de MP3.

La aplicación requiere cargar el CSV para habilitar el Dashboard y el Descargador. La limpieza y la fusión de carpetas se muestran antes de esa carga y trabajan sobre la carpeta de salida disponible.

---

## ⚠️ Obtención del Archivo CSV (Paso Obligatorio)

> **⚠️ IMPORTANTE: La aplicación requiere obligatoriamente un archivo CSV exportado desde [Exportify](https://exportify.net/). Sin este archivo, la app no puede funcionar.**

### Exportar tu biblioteca desde Exportify

**[Exportify](https://exportify.net/)** es una herramienta web gratuita que exporta cualquier lista de reproducción de Spotify como CSV con todos sus metadatos.

**Paso 1** — Navega a **[https://exportify.net/](https://exportify.net/)**

**Paso 2** — Haz clic en **"Log in with Spotify"** y autoriza el acceso (solo lectura).

**Paso 3** — Localiza **"Liked Songs"** en el listado de playlists.

**Paso 4** — Haz clic en **"Export"** para descargar el `.csv`.

**Paso 5** — En la barra lateral de la app, usa **"📂 Sube tu archivo CSV de Spotify"** para cargar el archivo.

> **💡 Consejo:** Para obtener columnas como `Genres`, `Popularity`, `Danceability`, `Energy`, `Valence` y `Tempo`, enriquece el CSV con la [Spotify Web API](https://developer.spotify.com/documentation/web-api). Estas columnas activan gráficos adicionales en el Dashboard y se incrustan como metadatos en los MP3.

---

## 🏗 Arquitectura e Ingeniería

### Concurrencia Multi-Hilo con `ThreadPoolExecutor`

El motor de descarga utiliza un `ThreadPoolExecutor` configurable (1-5 workers) para procesar múltiples pistas simultáneamente. Cada worker opera con un **prefijo de archivo temporal único** (`_tmp_{task_idx}_`) que garantiza aislamiento total entre hilos y previene colisiones de archivos.

Un **lock global** (`threading.Lock`) protege todas las operaciones de I/O compartido:

- Escritura en la consola en vivo (`log_lines`)
- Persistencia en disco (`registro_descargas.txt`)
- Contadores de progreso (`success_count`, `progress`)
- Estado actual de la pista (`current_track`)

### Inyección de Contexto de Streamlit (`add_script_run_ctx`)

Streamlit vincula su `session_state` al hilo del script principal mediante un `ScriptRunContext`. Los hilos del pool no heredan este contexto, lo que provocaría un crash al acceder a `st.session_state`.

El wrapper `_worker_wrapper()` inyecta el contexto del script activo en cada hilo del pool mediante `add_script_run_ctx(threading.current_thread())`, con fallback silencioso para compatibilidad entre versiones de Streamlit.

### Cancelación Cooperativa Thread-Safe

La clase `DownloadControl` encapsula un `threading.Event` compartido entre la UI y los hilos de descarga:

- Cada worker verifica `is_cancelled` en múltiples puntos estratégicos (antes de descargar, tras validar existencia, antes de cada motor).
- El orquestador deja de enviar tareas al pool y cancela los futures pendientes.
- Las canciones ya descargadas se preservan para reanudación automática.

### 4 Capas de Validación Anti-Corrupción MP3

Cada pista pasa por cuatro capas de validación antes de aceptarse como exitosa:

| Capa | Nombre                             | Momento       | Descripción                                                                                                                                                                                                                                   |
| :--: | ---------------------------------- | ------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
|  1  | **Omisión Robusta**         | Pre-descarga  | Verifica existencia + tamaño > 100 KB + validación estructural con`is_valid_mp3()`. Si el archivo existe pero está corrupto, lo elimina y re-descarga.                                                                                    |
|  2  | **Validación Estructural**  | Post-descarga | Intenta abrir el MP3 con Mutagen para detectar archivos falsos (`.webm`/`.m4a` renombrados a `.mp3`) o headers corruptos.                                                                                                                |
|  3  | **Pre-verificación Web**    | Pre-descarga  | Consulta metadatos de YouTube vía`yt-dlp --skip_download` para: **a)** descartar videos con duración incorrecta (±40s vs CSV), y **b)** rechazar versiones Karaoke/Instrumental/Cover si el título original no las solicita. |
|  4  | **Validación de Duración** | Post-descarga | Compara la duración real del MP3 (Mutagen) contra`Duration (ms)` del CSV con tolerancia ±35 segundos.                                                                                                                                      |

Las capas 2 y 4 se ejecutan combinadas sobre el archivo temporal. El **renombrado atómico** (`os.replace`) al nombre final solo ocurre cuando todas las validaciones y la incrustación de metadatos han sido exitosas.

### Fusión Jerárquica de Metadatos — 3 Capas

Los metadatos se construyen mediante un sistema de **merge por campo** que garantiza completitud al 100%:

```
┌─────────────────────────────────────────────────────────────┐
│  1º iTunes Search API (fuente primaria)                     │
│  ├── trackName, artistName, collectionName                  │
│  ├── primaryGenreName, releaseDate                          │
│  └── artworkUrl → 1000×1000 HD                              │
├─────────────────────────────────────────────────────────────┤
│  2º CSV / DataFrame (fallback por campo)                    │
│  ├── Track Name, Artist Name(s), Album Name                 │
│  ├── Genres (primer elemento), Release Year / Release Date  │
│  └── Album Image URL / Cover URL / Image URL                │
├─────────────────────────────────────────────────────────────┤
│  3º yt-dlp pre-existente (último recurso)                   │
│  └── Tags ID3 escritos por yt-dlp antes del post-procesado  │
│      (TIT2, TPE1, TALB, TDRC, TCON)                        │
└─────────────────────────────────────────────────────────────┘
```

Si iTunes devuelve Título y Álbum pero le falta Año o Género, el script rellena esos huecos buscando primero en el CSV y luego en los tags que yt-dlp escribió en el archivo temporal.

Adicionalmente, cada MP3 recibe:

- **Spotify audio-features** (siempre del CSV): Popularity, Danceability, Energy, Valence, Tempo, Explicit — incrustados como `COMM`.
- **Letras sincronizadas** vía `syncedlyrics`: `USLT` (texto plano) y `SYLT` (timestamps en ms).

### Limpieza Inteligente de Títulos (Anti-Etiquetas Oficiales)

Una regex precompilada (`_TITLE_LABEL_RE`) elimina automáticamente etiquetas de video de los títulos **antes** de generar el nombre de archivo y antes de verificar si ya existe:

| Etiqueta eliminada                             | Ejemplo                                            |
| ---------------------------------------------- | -------------------------------------------------- |
| `(Video Oficial)` / `[Official Video]`     | `Mi Canción (Video Oficial)` → `Mi Canción` |
| `(Audio Oficial)` / `[Official Audio]`     | `Track [Official Audio]` → `Track`            |
| `[Official Music Video]` / `(Lyric Video)` | `Song (Lyric Video)` → `Song`                 |
| `(Visualizer)` / `[HD]` / `[HQ]`         | `Song [HD]` → `Song`                          |
| `(Remastered 2023)`                          | `Song (Remastered 2023)` → `Song`             |

Cuando un título es modificado, se levanta una bandera por hilo que se transporta en la tupla de retorno y se registra en el log: `ÉXITO | Canción, Álbum, Artista | Título modificado: se limpió etiqueta de video`.

### Normalización de Carpetas Anti-Duplicados

La función `normalize_folder_name()` usa `unicodedata.normalize('NFD')` para eliminar tildes, diéresis y marcas diacríticas **únicamente** en los nombres de carpeta del sistema de archivos:

- `Júan` → `Juan` (misma carpeta para "Júan" y "Juan")
- `Bebé` → `Bebe`

Las etiquetas ID3 dentro de los MP3 **conservan** los caracteres originales con tildes.

### Sesión HTTP Reutilizable

Un `requests.Session` global reutiliza conexiones TCP (keep-alive) para reducir latencia en llamadas repetitivas a iTunes y carátulas. Es thread-safe internamente gracias al locking del connection pool de urllib3.

### Registro Persistente Thread-Safe

El archivo `registro_descargas.txt` se escribe línea a línea con protección `_io_lock`. Se almacena junto a la música descargada y soporta filtros dinámicos en la UI (Errores / Éxitos / Omitidos / Todos).

### Monitor de Descarga sin Reconstrucción Global

El progreso, la consola, el registro filtrable y el estado lateral se actualizan dentro de un fragmento `@st.fragment(run_every="1s")`. Esto limita las actualizaciones periódicas al monitor de descarga y evita reconstruir cada segundo el logo, el título, el menú de **Configuración Inicial** y el módulo **🧹 Limpieza de carpeta**.

Los `st.rerun()` restantes son puntuales: se usan al iniciar una descarga y después de escanear la biblioteca. No existe un `time.sleep()` periódico para forzar el refresco general de la página.

### Limpieza y Fusión de Carpetas

El módulo **🧹 Limpieza de carpeta**:

- Busca carpetas y archivos `.mp3` dentro de `Mi Musica`.
- Guarda el índice en `registro_musicas.db`, dentro de `data/` en Docker.
- Muestra las canciones agrupadas por carpeta en un contenedor desplazable.
- Actualiza el índice con **🔄 Escanear y Actualizar Biblioteca** y muestra el progreso del escaneo.

El módulo **🛠️ Fusión y Normalización de Carpetas** compara nombres ignorando tildes, mayúsculas y diéresis. Los MP3 con nombres equivalentes se consideran duplicados; los archivos restantes se mueven a la carpeta normalizada y las carpetas vacías se eliminan. La operación muestra un registro de archivos movidos y eliminados.

---

## ✨ Características V1.1

### 📊 Dashboard Analítico Interactivo

- **Métricas globales**: total de canciones, artistas únicos, álbumes, horas acumuladas, popularidad media y porcentaje de canciones explícitas.
- **Top 10 Artistas** más guardados (barras horizontales).
- **Distribución por Año de Lanzamiento** (histograma).
- **Scatter plot Energy vs. Valence** con hover interactivo por canción.
- **Ranking de Géneros** más escuchados.
- **Tabla de las 20 Canciones Más Populares** con audio-features.

### 📥 Descargador Multi-Motor con Cascada Automática

| Estrategia                              | Descripción                                                          |
| --------------------------------------- | --------------------------------------------------------------------- |
| **Solo yt-dlp** *(Recomendado)* | Busca en YouTube y descarga como MP3 VBR 0 (~320 kbps).               |
| **Cascada Automática**           | Ejecuta`yt-dlp` y, si no produce un MP3 válido, prueba `spotdl`. |
| **Solo spotdl**                   | Descarga directa desde Spotify (requiere credenciales).               |

- **Ruta de descarga en Docker**: la aplicación escribe en `/app/output`, que se monta en la carpeta indicada por `HOST_MUSIC_DIR`. La ruta del host se muestra en la interfaz como campo informativo deshabilitado.
- **Monitor localizado** con consola, progreso y registro en vivo mediante `st.fragment`, sin reconstrucción periódica del resto de la página.
- **Barra de progreso** canción a canción y progreso lateral resumido.
- **Panel de registro** con filtros por estado (Errores / Éxitos / Omitidos / Todos).
- **Descarga concurrente** configurable de 1 a 5 hilos simultáneos.
- **Cancelación en caliente** con reanudación posterior.
- **Reporte de errores** exportable como CSV.

Al completar una descarga sin errores, la interfaz muestra un mensaje de éxito y una animación de globos.

### 🧹 Limpieza y Organización de la Biblioteca

- **Escanear y Actualizar Biblioteca**: reconstruye el índice SQLite de carpetas y MP3.
- **Consulta agrupada**: muestra cada carpeta y sus canciones en un área desplazable.
- **Fusión de carpetas similares**: normaliza nombres, mueve archivos no duplicados y elimina archivos equivalentes.
- **Registro de operaciones**: informa de archivos movidos, duplicados eliminados y carpetas borradas.

### 🏷️ Etiquetas ID3 Incrustadas

Cada MP3 recibe automáticamente:

| Tag      | Contenido                                      | Fuentes (prioridad)                                    |
| -------- | ---------------------------------------------- | ------------------------------------------------------ |
| `TIT2` | Título (limpio, sin etiquetas de video)       | iTunes → CSV → yt-dlp                                |
| `TPE1` | Artista(s) completo(s)                         | iTunes → CSV → yt-dlp                                |
| `TALB` | Nombre del álbum                              | iTunes → CSV → yt-dlp                                |
| `TDRC` | Año de lanzamiento                            | iTunes → CSV → yt-dlp                                |
| `TCON` | Género principal                              | iTunes → CSV → yt-dlp                                |
| `TRCK` | Número de pista / disco                       | CSV                                                    |
| `APIC` | Carátula HD (iTunes 1000×1000 > URL del CSV) | iTunes → CSV                                          |
| `USLT` | Letra no sincronizada (texto plano)            | syncedlyrics                                           |
| `SYLT` | Letra sincronizada con timestamps              | syncedlyrics                                           |
| `COMM` | Audio-features de Spotify                      | CSV (Popularity, Danceability, Energy, Valence, Tempo) |

### 📁 Organización Automática de Carpetas

```
~/Music/                              ← Ruta del host montada mediante HOST_MUSIC_DIR
└── {Subcarpeta personalizada}/
    └── {Artista Principal}/           ← Normalizado (sin tildes en la carpeta)
        ├── Canción, Álbum, Artista.mp3  ← Título limpio (sin etiquetas de video)
        └── ...
```

> Los nombres de carpeta se normalizan con `unicodedata` (sin tildes) para evitar duplicados. Los nombres de archivo se sanitizan para eliminar caracteres inválidos del sistema de archivos. Las etiquetas ID3 conservan los caracteres originales.

---

## 🔧 Requisitos Previos

| Herramienta              | Versión Mínima | Descripción               |
| ------------------------ | ---------------- | -------------------------- |
| **Docker Desktop** | 24.x+            | Motor de contenedores      |
| **Docker Compose** | v2.x+            | Incluido en Docker Desktop |

La imagen instala Python 3.11, FFmpeg y las dependencias Python declaradas en `requirements.txt`. Para ejecutar la aplicación fuera de Docker, instala esas dependencias y asegúrate de tener FFmpeg disponible en el sistema.

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

| Categoría            | Tecnología                                                | Versión  | Rol                           |
| --------------------- | ---------------------------------------------------------- | --------- | ----------------------------- |
| **UI**          | [Streamlit](https://streamlit.io/)                          | ≥ 1.62   | Interfaz web interactiva      |
| **Data**        | [Pandas](https://pandas.pydata.org/)                        | ≥ 3.0    | Procesamiento del CSV         |
| **Gráficos**   | [Plotly Express](https://plotly.com/python/plotly-express/) | ≥ 7.0    | Visualizaciones del Dashboard |
| **Descarga 1**  | [yt-dlp](https://github.com/yt-dlp/yt-dlp)                  | ≥ 2026.8 | Audio desde YouTube           |
| **Descarga 2**  | [spotdl](https://github.com/spotDL/spotify-downloader)      | ≥ 4.5.2  | Descarga directa Spotify      |
| **Metadata**    | [Mutagen](https://mutagen.readthedocs.io/)                  | ≥ 1.48   | Etiquetas ID3 en MP3          |
| **Letras**      | [syncedlyrics](https://github.com/moehmeni/syncedlyrics)    | ≥ 1.0    | Letras LRC sincronizadas      |
| **Spotify API** | [Spotipy](https://spotipy.readthedocs.io/)                  | ≥ 2.26   | Cliente API (para spotdl)     |
| **HTTP**        | [Requests](https://docs.python-requests.org/)               | ≥ 2.34   | iTunes API + carátulas       |
| **Imágenes**   | [Pillow](https://pillow.readthedocs.io/)                    | ≥ 12.3   | Logo de la app                |
| **Unicode**     | `unicodedata` (stdlib)                                   | —        | Normalización de carpetas    |
| **Audio**       | [FFmpeg](https://ffmpeg.org/)                               | Sistema   | Transcodificación a MP3      |
| **Container**   | [Docker](https://www.docker.com/)                           | 24.x+     | Aislamiento y portabilidad    |
| **Base Image**  | `python:3.11-slim`                                       | —        | Imagen Docker ligera          |

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

Por defecto, Docker monta la carpeta **Música** de tu sistema operativo (`~/Music`) en `/app/output`. Puedes cambiar la ruta del host mediante la variable `HOST_MUSIC_DIR` en el `.env`; la aplicación usa internamente `/app/output` cuando se ejecuta dentro del contenedor.

</details>

<details>
<summary><strong>¿Cómo funciona la cancelación de descargas?</strong></summary>

El botón "🛑 Cancelar Descarga" activa un `threading.Event` compartido. Cada worker verifica este flag en múltiples puntos estratégicos y se detiene cooperativamente. La pista que está en proceso de descarga terminará antes de detenerse. Al presionar "Iniciar / Reanudar", la app retoma desde donde se quedó (las canciones ya descargadas se omiten automáticamente).

</details>

<details>
<summary><strong>¿Qué hace el limpiador de títulos?</strong></summary>

Una regex precompilada elimina automáticamente etiquetas como `(Video Oficial)`, `[Official Video]`, `(Audio Oficial)`, `[Lyric Video]`, `(Visualizer)`, `[HD]`, `[HQ]` y `(Remastered)` del título de la canción. Esto previene duplicados en disco y produce nombres de archivo más limpios. Cuando un título es modificado, se registra en el log y en la consola. Las etiquetas ID3 dentro del MP3 también usan el título limpio.

</details>

<details>
<summary><strong>¿Por qué las carpetas de artistas no tienen tildes?</strong></summary>

La función `normalize_folder_name()` elimina tildes y diéresis únicamente en los nombres de carpeta para evitar duplicados (por ejemplo, "Bebé" y "Bebe" irían a la misma carpeta). Las etiquetas ID3 dentro de los MP3 conservan los caracteres originales con tildes.

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
