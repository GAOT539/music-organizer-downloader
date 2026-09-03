import streamlit as st
import pandas as pd
import plotly.express as px
import subprocess
import os
import re
import unicodedata
import io
import time
import threading
import pathlib
import sqlite3
import requests
import syncedlyrics
from PIL import Image
import glob
from concurrent.futures import ThreadPoolExecutor, as_completed
from mutagen.mp3 import MP3
from mutagen.easyid3 import EasyID3
from mutagen.id3 import (
    ID3, ID3NoHeaderError,
    TIT2, TPE1, TALB, TDRC, TCON, TRCK,
    USLT, SYLT, APIC, COMM,
    Encoding
)
try:
    from streamlit.runtime.scriptrunner import add_script_run_ctx
except ImportError:
    try:
        from streamlit.scriptrunner import add_script_run_ctx
    except ImportError:
        add_script_run_ctx = None


class DownloadControl:
    """Objeto compartido entre el hilo principal (UI) y el hilo de descarga.
    Usar una clase dedicada evita los problemas de sincronización de
    st.session_state, que no se propaga de forma fiable a hilos secundarios."""
    def __init__(self):
        self._cancel = threading.Event()

    def request_cancel(self):
        self._cancel.set()

    def reset(self):
        self._cancel.clear()

    @property
    def is_cancelled(self):
        return self._cancel.is_set()


if "download_control" not in st.session_state:
    st.session_state["download_control"] = DownloadControl()

# requests.Session reutiliza conexiones TCP (keep-alive) y es thread-safe
# internamente (urllib3 usa locking en su connection pool).
http_session = requests.Session()
http_session.headers.update({"User-Agent": "SelloDeGatoMusic/1.0"})

LOGO_PATH = "logo.png"
page_icon = "🎵"
if os.path.exists(LOGO_PATH):
    try:
        page_icon = Image.open(LOGO_PATH)
    except Exception:
        page_icon = "🎵"

st.set_page_config(page_title="Sello de Gato Music", page_icon=page_icon, layout="wide")

_DOCKER_OUTPUT = "/app/output"
if os.path.isdir(_DOCKER_OUTPUT):
    _DEFAULT_MUSIC_DIR = _DOCKER_OUTPUT
else:
    _DEFAULT_MUSIC_DIR = str(pathlib.Path.home() / "Music")

if "download_base_path" not in st.session_state:
    st.session_state["download_base_path"] = _DEFAULT_MUSIC_DIR

if "download_state" not in st.session_state:
    st.session_state["download_state"] = {
        "running": False,
        "done": False,
        "progress": 0.0,
        "success_count": 0,
        "total": 0,
        "log_lines": [],
        "failed_songs": [],
        "current_track": "",
    }

def is_valid_mp3(file_path):
    """Valida estructura MP3 con Mutagen. True solo si abre y duración > 0."""
    try:
        audio = MP3(file_path)
        return audio.info.length is not None and audio.info.length > 0
    except Exception:
        return False


def sanitize_name(name):
    clean = re.sub(r'[\\/*?:"<>|]', "", str(name)).strip()
    return clean if clean else ""

def normalize_folder_name(name):
    """Elimina tildes, diéresis y marcas diacríticas para nombres de carpeta.
    Evita duplicados como 'Júan'/'Juan' o 'Bebé'/'Bebe'.
    Solo para rutas del sistema de archivos; las etiquetas ID3 conservan los originales."""
    nfkd = unicodedata.normalize('NFD', str(name))
    return ''.join(c for c in nfkd if unicodedata.category(c) != 'Mn')

_TITLE_LABEL_RE = re.compile(
    r'\s*[\(\[]'
    r'(?:'
    r'(?:video|audio)\s+oficial'
    r'|official\s+(?:video|audio|music\s+video|lyric\s+video)'
    r'|lyric(?:s)?\s+video'
    r'|video\s+lyric'
    r'|visuali[sz]er'
    r'|remastered(?:\s+\d{4})?'
    r'|hd|hq'
    r')'
    r'[\)\]]',
    re.IGNORECASE,
)

def clean_track_title(title):
    """Elimina etiquetas de video oficiales del título.
    Ej: 'Canción (Video Oficial)' → ('Canción', True).
    Retorna (cleaned_title, was_modified)."""
    cleaned = _TITLE_LABEL_RE.sub('', title).strip()
    if not cleaned:
        return title, False
    return cleaned, cleaned != title

def get_primary_artist(artist_string):
    raw = str(artist_string).strip()
    primary = re.split(r'[;,/]|(?i:\s+feat\.?\s+)|\s*&\s*', raw)[0].strip()
    return sanitize_name(primary) if primary else "Varios Artistas"

def format_track_filename(track_name, album_name, full_artist):
    t_name = sanitize_name(track_name)
    a_name = sanitize_name(album_name)
    art_name = sanitize_name(full_artist)
    if not a_name or a_name.lower() in ["unknown", "desconocido", "none", "nan", ""]:
        return f"{t_name}, {art_name}"
    else:
        return f"{t_name}, {a_name}, {art_name}"


_MUSIC_CLEANUP_DB = (
    os.path.join("/app/data", "registro_musicas.db")
    if os.path.isdir("/app/data")
    else "registro_musicas.db"
)
_MUSIC_CLEANUP_TARGET = os.path.join("/app/output", "Mi Musica")


def _initialize_music_cleanup_db(connection):
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS Carpetas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nombre_carpeta TEXT NOT NULL UNIQUE
        );
        CREATE TABLE IF NOT EXISTS Canciones (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nombre_archivo TEXT NOT NULL,
            carpeta_id INTEGER NOT NULL,
            FOREIGN KEY (carpeta_id) REFERENCES Carpetas(id)
        );
        """
    )
    connection.commit()


def _load_music_cleanup_rows():
    with sqlite3.connect(_MUSIC_CLEANUP_DB) as connection:
        _initialize_music_cleanup_db(connection)
        return connection.execute(
            """
            SELECT Carpetas.nombre_carpeta AS Carpeta,
                   Canciones.nombre_archivo AS Archivo
            FROM Canciones
            JOIN Carpetas ON Carpetas.id = Canciones.carpeta_id
            ORDER BY Carpetas.nombre_carpeta, Canciones.nombre_archivo
            """
        ).fetchall()


def _populate_music_cleanup_db(progress_bar):
    folder_entries = []
    song_entries = []
    if os.path.isdir(_MUSIC_CLEANUP_TARGET):
        for current_root, directories, filenames in os.walk(_MUSIC_CLEANUP_TARGET):
            if os.path.abspath(current_root) == os.path.abspath(_MUSIC_CLEANUP_TARGET):
                continue
            folder_name = os.path.basename(current_root)
            folder_entries.append(folder_name)
            song_entries.extend(
                (folder_name, os.path.splitext(filename)[0])
                for filename in filenames
                if filename.lower().endswith(".mp3")
            )

    total_entries = len(folder_entries) + len(song_entries)
    completed_entries = 0
    with sqlite3.connect(_MUSIC_CLEANUP_DB) as connection:
        _initialize_music_cleanup_db(connection)
        connection.execute("DELETE FROM Canciones")
        connection.execute("DELETE FROM Carpetas")
        folder_ids = {}
        for folder_name in folder_entries:
            cursor = connection.execute(
                "INSERT INTO Carpetas (nombre_carpeta) VALUES (?)",
                (folder_name,),
            )
            folder_ids[folder_name] = cursor.lastrowid
            completed_entries += 1
            progress_bar.progress(completed_entries / total_entries if total_entries else 1.0)

        for folder_name, filename in song_entries:
            connection.execute(
                "INSERT INTO Canciones (nombre_archivo, carpeta_id) VALUES (?, ?)",
                (filename, folder_ids[folder_name]),
            )
            completed_entries += 1
            progress_bar.progress(completed_entries / total_entries if total_entries else 1.0)
        connection.commit()

    return _load_music_cleanup_rows()


def _render_music_cleanup_rows(rows):
    songs_by_folder = {}
    for folder_name, filename in rows:
        songs_by_folder.setdefault(folder_name, []).append(filename)

    with st.container(height=500):
        for folder_name, filenames in songs_by_folder.items():
            with st.expander(f"📁 {folder_name}"):
                for filename in filenames:
                    st.markdown(f"- {filename}")


def render_music_cleanup_module():
    with st.expander("🧹 Limpieza de carpeta"):
        database_has_data = False
        if os.path.exists(_MUSIC_CLEANUP_DB):
            try:
                database_has_data = bool(_load_music_cleanup_rows())
            except sqlite3.Error as error:
                st.warning(f"No se pudo leer la base de datos: {error}")

        col1, col2 = st.columns(2)
        with col1:
            generate_database = st.button(
                "Generar Base de Datos",
                key="generate_music_database",
                disabled=database_has_data,
                use_container_width=True,
            )
        with col2:
            refresh_database = st.button(
                "Releer archivos y actualizar BD",
                key="refresh_music_database",
                use_container_width=True,
            )

        if generate_database or refresh_database:
            progress_bar = st.progress(0.0)
            rows = _populate_music_cleanup_db(progress_bar)
            if generate_database:
                st.success("Base de datos generada correctamente.")
            else:
                st.success("Base de datos actualizada correctamente.")
            _render_music_cleanup_rows(rows)
        elif not database_has_data:
            st.info("No hay registros. Genera la base de datos para indexar los MP3.")
        else:
            rows = _load_music_cleanup_rows()
            _render_music_cleanup_rows(rows)

    hay_datos = database_has_data
    with st.expander("🛠️ Fusión y Normalización de Carpetas"):
        st.write(
            "Busca carpetas con nombres equivalentes, combina sus archivos "
            "y elimina los MP3 duplicados."
        )

        fusionar_carpetas = st.button(
            "🔍 Buscar y fusionar carpetas similares",
            disabled=not hay_datos,
        )
        if not hay_datos:
            st.caption("⚠️ Relee los archivos primero para habilitar la fusión.")

        if fusionar_carpetas:
            import shutil

            def _normalizar_texto(texto):
                return (
                    unicodedata.normalize("NFKD", texto)
                    .encode("ASCII", "ignore")
                    .decode("utf-8")
                    .title()
                )

            logs = []
            carpetas_por_nombre = {}
            if os.path.isdir(_MUSIC_CLEANUP_TARGET):
                for nombre_carpeta in os.listdir(_MUSIC_CLEANUP_TARGET):
                    ruta_carpeta = os.path.join(_MUSIC_CLEANUP_TARGET, nombre_carpeta)
                    if os.path.isdir(ruta_carpeta):
                        nombre_normalizado = _normalizar_texto(nombre_carpeta)
                        carpetas_por_nombre.setdefault(nombre_normalizado, []).append(
                            (nombre_carpeta, ruta_carpeta)
                        )

            for nombre_normalizado, variantes in carpetas_por_nombre.items():
                if len(variantes) < 2:
                    continue

                carpeta_destino = os.path.join(
                    _MUSIC_CLEANUP_TARGET,
                    nombre_normalizado,
                )
                os.makedirs(carpeta_destino, exist_ok=True)

                for nombre_carpeta, ruta_variante in variantes:
                    if os.path.abspath(ruta_variante) == os.path.abspath(carpeta_destino):
                        continue

                    os.makedirs(carpeta_destino, exist_ok=True)
                    archivos_destino = {
                        _normalizar_texto(os.path.basename(archivo)): os.path.basename(archivo)
                        for archivo in os.listdir(carpeta_destino)
                        if os.path.isfile(os.path.join(carpeta_destino, archivo))
                        and archivo.lower().endswith(".mp3")
                    }
                    for archivo_variante in os.listdir(ruta_variante):
                        ruta_archivo_variante = os.path.join(ruta_variante, archivo_variante)
                        if not (
                            os.path.isfile(ruta_archivo_variante)
                            and archivo_variante.lower().endswith(".mp3")
                        ):
                            continue

                        nombre_archivo_normalizado = _normalizar_texto(archivo_variante)
                        archivo_destino = archivos_destino.get(nombre_archivo_normalizado)
                        if archivo_destino:
                            os.remove(ruta_archivo_variante)
                            logs.append(
                                f"❌ Archivo eliminado: '{archivo_variante}' "
                                f"(Similitud con '{archivo_destino}')"
                            )
                        else:
                            shutil.move(ruta_archivo_variante, carpeta_destino)
                            archivos_destino[nombre_archivo_normalizado] = archivo_variante
                            logs.append(
                                f"➡️ Archivo movido: '{archivo_variante}' "
                                f"-> '{carpeta_destino}'"
                            )

                    try:
                        os.rmdir(ruta_variante)
                        logs.append(f"🗑️ Carpeta eliminada: '{nombre_carpeta}'")
                    except OSError:
                        pass

            if logs:
                with st.container(height=300):
                    st.code("\n".join(logs), language="bash")
            else:
                st.success("No se encontraron carpetas duplicadas.")

def embed_lyrics_into_mp3(mp3_path, lyrics_text):
    """Incrusta letra USLT (plana) y SYLT (sincronizada con timestamps)."""
    try:
        audio = ID3(mp3_path)
    except ID3NoHeaderError:
        audio = ID3()

    audio.delall("USLT")
    plain_text = re.sub(r'\[\d+:\d+\.\d+\]', '', lyrics_text).strip()
    audio.add(USLT(encoding=Encoding.UTF8, lang='eng', desc='', text=plain_text))

    audio.delall("SYLT")
    sylt_pairs = []
    for line in lyrics_text.splitlines():
        m = re.match(r'\[(\d+):(\d+\.\d+)\](.*)', line)
        if m:
            minutes, seconds, text = int(m.group(1)), float(m.group(2)), m.group(3).strip()
            timestamp_ms = int((minutes * 60 + seconds) * 1000)
            sylt_pairs.append((text, timestamp_ms))
    if sylt_pairs:
        audio.add(SYLT(encoding=Encoding.UTF8, lang='eng', format=2, type=1, text=sylt_pairs))

    audio.save(mp3_path)


def fetch_itunes_metadata(track_name, artist_name):
    """Consulta iTunes Search API para metadatos oficiales y carátula HD.
    Retorna dict {title, artist, album, genre, year, artwork_url, artwork_data}
    o None si no hay resultados / error de red."""
    try:
        term = f"{track_name} {artist_name}"
        resp = http_session.get(
            "https://itunes.apple.com/search",
            params={"term": term, "entity": "song", "limit": 1},
            timeout=10,
        )
        if resp.status_code != 200:
            return None

        data = resp.json()
        results = data.get("results", [])
        if not results:
            return None

        item = results[0]

        release_date_raw = item.get("releaseDate", "")
        year = release_date_raw[:4] if release_date_raw and len(release_date_raw) >= 4 else None

        # artworkUrl100 → 1000×1000 reemplazando el sufijo del CDN de Apple
        artwork_url = item.get("artworkUrl100", "")
        if artwork_url:
            artwork_url = artwork_url.replace("100x100bb.jpg", "1000x1000bb.jpg")

        artwork_data = None
        if artwork_url:
            try:
                img_resp = http_session.get(artwork_url, timeout=10)
                if img_resp.status_code == 200:
                    artwork_data = img_resp.content
            except Exception:
                pass

        return {
            "title":        item.get("trackName"),
            "artist":       item.get("artistName"),
            "album":        item.get("collectionName"),
            "genre":        item.get("primaryGenreName"),
            "year":         year,
            "artwork_url":  artwork_url,
            "artwork_data": artwork_data,
        }

    except Exception:
        return None


def process_dataframe(df):
    if 'Duration (ms)' in df.columns:
        df['Duration_Min'] = df['Duration (ms)'] / 60000
    if 'Release Date' in df.columns:
        df['Release Year'] = pd.to_datetime(df['Release Date'], errors='coerce').dt.year
    df['Clean_Primary_Artist'] = df['Artist Name(s)'].apply(get_primary_artist)
    for num_col in ['Popularity', 'Danceability', 'Energy', 'Valence', 'Tempo']:
        if num_col in df.columns:
            df[num_col] = pd.to_numeric(df[num_col], errors='coerce')
    if 'Explicit' in df.columns:
        df['Explicit'] = df['Explicit'].astype(str).str.strip().str.lower().map(
            lambda v: True if v in ['true', '1', 'yes', 'sí', 'si'] else False
        )
    return df


def run_download_job(df_sorted, root_target_dir, log_file_path, engine_mode, env_vars, cancel_ctrl, max_workers=2):
    """Orquesta la descarga concurrente con ThreadPoolExecutor.
    Escribe progreso en st.session_state['download_state'].
    cancel_ctrl: DownloadControl thread-safe para cancelación cooperativa.
    log_file_path: ruta absoluta del archivo registro_descargas.txt.
    """
    state = st.session_state["download_state"]
    total_tracks = len(df_sorted)
    state["total"] = total_tracks
    state["running"] = True
    state["done"] = False
    state["success_count"] = 0
    state["failed_songs"] = []
    state["log_lines"] = []
    state["progress"] = 0.0

    # Lock global para I/O compartido entre workers del pool
    _io_lock = threading.Lock()
    _completed = [0]

    def update_console(text):
        with _io_lock:
            state["log_lines"].insert(0, text)

    def write_log(text):
        with _io_lock:
            try:
                with open(log_file_path, 'a', encoding='utf-8') as _lf:
                    _lf.write(text + "\n")
            except Exception:
                pass

    def increment_success():
        with _io_lock:
            state["success_count"] += 1

    def append_failed(song_dict):
        with _io_lock:
            state["failed_songs"].append(song_dict)

    def update_progress():
        with _io_lock:
            _completed[0] += 1
            state["progress"] = _completed[0] / total_tracks

    def set_current_track(text):
        with _io_lock:
            state["current_track"] = text

    def run_proc(cmd):
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=env_vars
        )
        out = []
        for line in proc.stdout:
            l = line.strip()
            if l:
                update_console(f" > {l}")
                out.append(l)
        proc.wait()
        return proc.returncode == 0, "\n".join(out)

    def _process_single_track(task_idx, idx, row):
        """Procesa una pista: descarga → 4 capas de validación → metadata → renombrado.
        Usa prefijo _tmp_{task_idx}_ para aislar archivos entre hilos.
        Retorna (status, base_filename, reason, row_dict, title_cleaned)."""
        if cancel_ctrl.is_cancelled:
            return 'cancelled', '', '', None, False

        track_name_raw = sanitize_name(row.get('Track Name', 'Desconocido'))
        track_name, title_was_cleaned = clean_track_title(track_name_raw)
        full_artist    = sanitize_name(row.get('Artist Name(s)', 'Desconocido'))
        primary_artist = row.get('Clean_Primary_Artist', 'Varios Artistas')
        album_name     = sanitize_name(row.get('Album Name', ''))
        track_uri      = str(row.get('Track URI', ''))

        track_url = (
            f"https://open.spotify.com/track/{track_uri.split(':')[-1]}"
            if track_uri.startswith('spotify:track:') else track_uri
        )

        folder_artist = normalize_folder_name(primary_artist)
        target_folder = os.path.join(root_target_dir, folder_artist)
        os.makedirs(target_folder, exist_ok=True)

        base_filename  = format_track_filename(track_name, album_name, full_artist)
        final_mp3_path = os.path.join(target_folder, f"{base_filename}.mp3")
        # Prefijo único por hilo evita colisiones de archivos temporales
        temp_stem     = f"_tmp_{task_idx}_{base_filename}"
        temp_mp3_path = os.path.join(target_folder, f"{temp_stem}.mp3")

        current_num = idx + 1
        set_current_track(f"({current_num}/{total_tracks}) {base_filename}")
        update_console(f"\n[INFO] ({current_num}/{total_tracks}) {primary_artist} -> {base_filename}")
        if title_was_cleaned:
            update_console(f" 🧹 Título limpiado: '{track_name_raw}' → '{track_name}'")

        # Capa 1: Omisión robusta — existencia + tamaño > 100 KB + validez MP3
        if os.path.exists(final_mp3_path) and os.path.getsize(final_mp3_path) > 100_000:
            if is_valid_mp3(final_mp3_path):
                update_console(f"✔ Omitido (Ya existe y es válido): {base_filename}")
                write_log(f"OMITIDO | {base_filename}")
                return 'skipped', base_filename, '', None, title_was_cleaned
            else:
                update_console(f"⚠ Archivo corrupto detectado, eliminando para re-descargar: {base_filename}")
                try:
                    os.remove(final_mp3_path)
                except OSError:
                    pass

        if cancel_ctrl.is_cancelled:
            return 'cancelled', base_filename, '', None, False

        success = False
        reason  = ""

        expected_duration_s = None
        raw_ms = row.get('Duration (ms)', None)
        try:
            if raw_ms is not None and str(raw_ms).strip() not in ('', 'nan', 'None'):
                expected_duration_s = float(raw_ms) / 1000.0
        except (ValueError, TypeError):
            pass

        def verify_duration(mp3_path):
            """Capas 2+4: validación estructural (Mutagen) + duración CSV ±35s.
            Elimina el archivo y retorna False si falla cualquier validación."""
            try:
                audio_info = MP3(mp3_path)
            except Exception as struct_err:
                update_console(
                    f" \u2716 Archivo MP3 corrupto o falso: {struct_err.__class__.__name__} "
                    f"-- archivo eliminado"
                )
                try:
                    os.remove(mp3_path)
                except OSError:
                    pass
                return False

            if expected_duration_s is None:
                update_console(" \u2714 Estructura MP3 OK (sin referencia de duracion en CSV)")
                return True

            real_s = audio_info.info.length
            diff   = abs(real_s - expected_duration_s)
            if diff > 35:
                update_console(
                    f" \u2716 Duracion invalida: real={real_s:.1f}s "
                    f"esperada={expected_duration_s:.1f}s (delta={diff:.1f}s > 35s) "
                    f"-- archivo eliminado"
                )
                try:
                    os.remove(mp3_path)
                except OSError:
                    pass
                return False

            update_console(
                f" \u2714 MP3 valido: estructura OK, duracion={real_s:.1f}s "
                f"(esperada {expected_duration_s:.1f}s, delta={diff:.1f}s)"
            )
            return True

        def cleanup_residuals(folder, stem):
            """Elimina residuos de yt-dlp/ffmpeg del prefijo único de este hilo."""
            removed = []
            for ext in ('webm', 'm4a', 'webp', 'part', 'ytdl'):
                pattern = os.path.join(folder, glob.escape(stem) + "*." + ext)
                for fp in glob.glob(pattern):
                    try:
                        os.remove(fp)
                        removed.append(os.path.basename(fp))
                    except OSError:
                        pass
            if removed:
                update_console(f" \U0001f5d1 Residuos eliminados: {', '.join(removed)}")

        def embed_post_process(mp3_path):
            """Post-procesado: incrusta metadatos ID3 (iTunes > CSV > yt-dlp) + letras USLT/SYLT.
            Fusión jerárquica: rellena campos vacíos buscando en la siguiente fuente."""
            # --- Capa 3: Leer metadatos pre-existentes de yt-dlp (antes de sobrescribir) ---
            ytdlp_meta = {}
            try:
                _existing_tags = ID3(mp3_path)
                for _tag_id, _key in [("TIT2", "title"), ("TPE1", "artist"), ("TALB", "album"),
                                       ("TDRC", "year"), ("TCON", "genre")]:
                    _val = _existing_tags.get(_tag_id)
                    if _val:
                        _text = str(_val)
                        if _text and _text.strip().lower() not in ('', 'none', 'nan'):
                            ytdlp_meta[_key] = _text.strip()
            except Exception:
                pass
            if ytdlp_meta:
                update_console(f" ℹ yt-dlp tags leídos: {', '.join(ytdlp_meta.keys())}")

            # --- Capa 1: iTunes API ---
            update_console(" ▶ Consultando iTunes API para metadatos HD...")
            itunes = fetch_itunes_metadata(track_name, primary_artist)

            if itunes:
                update_console(
                    f" ✔ iTunes: '{itunes.get('title')}' "
                    f"- {itunes.get('artist')} "
                    f"[{itunes.get('album')}] "
                    f"({itunes.get('year')})"
                )
            else:
                update_console(" ⚠ iTunes no encontró la canción — buscando en CSV / yt-dlp")

            # Fusión jerárquica: 1º iTunes → 2º CSV → 3º yt-dlp
            final_title  = (itunes or {}).get("title")  or track_name  or ytdlp_meta.get("title", "")
            final_artist = (itunes or {}).get("artist") or full_artist  or ytdlp_meta.get("artist", "")
            final_album  = (itunes or {}).get("album")  or album_name  or ytdlp_meta.get("album", "")
            final_genre  = (itunes or {}).get("genre")
            final_year   = (itunes or {}).get("year")

            # Género: iTunes → CSV → yt-dlp
            if not final_genre:
                genres_raw = str(row.get('Genres', '') or '').strip()
                if genres_raw and genres_raw.lower() not in ('nan', 'none', ''):
                    final_genre = genres_raw.split(',')[0].strip()
            if not final_genre:
                final_genre = ytdlp_meta.get("genre")

            # Año: iTunes → CSV → yt-dlp
            if not final_year:
                final_year = str(
                    row.get('Release Year', '') or row.get('Release Date', '') or ''
                ).strip()[:4]
                if not final_year.isdigit():
                    final_year = None
            if not final_year:
                _yt_year = (ytdlp_meta.get("year") or "")[:4]
                if _yt_year and _yt_year.isdigit():
                    final_year = _yt_year

            try:
                try:
                    tags = ID3(mp3_path)
                except ID3NoHeaderError:
                    tags = ID3()

                tags.delall("TIT2"); tags.add(TIT2(encoding=Encoding.UTF8, text=final_title))
                tags.delall("TPE1"); tags.add(TPE1(encoding=Encoding.UTF8, text=final_artist))
                if final_album:
                    tags.delall("TALB"); tags.add(TALB(encoding=Encoding.UTF8, text=final_album))
                if final_year and str(final_year).isdigit():
                    tags.delall("TDRC"); tags.add(TDRC(encoding=Encoding.UTF8, text=str(final_year)))
                if final_genre:
                    tags.delall("TCON"); tags.add(TCON(encoding=Encoding.UTF8, text=final_genre))

                track_number = str(row.get('Track Number', '') or '').strip()
                disc_number  = str(row.get('Disc Number',  '') or '').strip()
                if track_number.isdigit():
                    trck_str = f"{track_number}/{disc_number}" if disc_number.isdigit() else track_number
                    tags.delall("TRCK"); tags.add(TRCK(encoding=Encoding.UTF8, text=trck_str))

                extras = {
                    k: row.get(k)
                    for k in ['Popularity', 'Danceability', 'Energy', 'Valence', 'Tempo', 'Explicit']
                    if row.get(k) is not None
                }
                if extras:
                    comment_text = ' | '.join(f"{k}: {v}" for k, v in extras.items())
                    tags.delall("COMM")
                    tags.add(COMM(encoding=Encoding.UTF8, lang='eng', desc='Spotify Features', text=comment_text))

                # Carátula APIC: iTunes HD > URL del CSV
                artwork_data = (itunes or {}).get("artwork_data")
                artwork_mime = "image/jpeg"

                if artwork_data:
                    update_console(" \u2714 Caratula HD de iTunes incrustada (1000x1000)")
                else:
                    cover_url = str(
                        row.get('Album Image URL', '') or
                        row.get('Cover URL', '')       or
                        row.get('Image URL', '')        or ''
                    ).strip()
                    if cover_url and cover_url.startswith('http'):
                        try:
                            cov_resp = http_session.get(cover_url, timeout=10)
                            if cov_resp.status_code == 200:
                                artwork_data = cov_resp.content
                                artwork_mime = (
                                    'image/jpeg'
                                    if cover_url.lower().endswith('.jpg')
                                    or b'\xff\xd8' in artwork_data[:3]
                                    else 'image/png'
                                )
                                update_console(" \u2714 Caratula del CSV incrustada (fallback)")
                        except Exception:
                            pass

                if artwork_data:
                    tags.delall("APIC")
                    tags.add(APIC(
                        encoding=Encoding.UTF8,
                        mime=artwork_mime,
                        type=3,
                        desc='Cover',
                        data=artwork_data,
                    ))

                tags.save(mp3_path)
                _src_parts = []
                if itunes: _src_parts.append('iTunes')
                _src_parts.append('CSV')
                if ytdlp_meta: _src_parts.append('yt-dlp')
                update_console(
                    " ✔ Metadata ID3 completa "
                    f"({'+'.join(_src_parts)}): "
                    f"{final_title} / {final_artist} / {final_album}"
                )

            except Exception as meta_err:
                update_console(f" \u26a0 Error incrustando metadata: {meta_err}")


            try:
                lyrics_text = syncedlyrics.search(f"{primary_artist} {track_name}")
                if lyrics_text:
                    embed_lyrics_into_mp3(mp3_path, lyrics_text)
                    update_console(" \u2714 Letras USLT/SYLT incrustadas")
                else:
                    update_console(" \u2139 Sin letra disponible en syncedlyrics")
            except Exception:
                pass

        def verify_web_duration(search_query):
            """Capa 3: pre-verificación de duración web (yt-dlp --skip_download).
            Descarta videos con delta > 40s vs CSV sin descargar nada."""
            if expected_duration_s is None:
                return True, None  # Sin referencia, dejar pasar

            try:
                import yt_dlp

                ydl_opts = {
                    "quiet": True,
                    "no_warnings": True,
                    "skip_download": True,
                    "extract_flat": False,
                    "noplaylist": True,
                    "default_search": "ytsearch1",
                }
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(search_query, download=False)

                if info is None:
                    return True, None
                if "entries" in info:
                    entries = [e for e in (info.get("entries") or []) if e]
                    if not entries:
                        return True, None
                    info = entries[0]

                # --- Filtro Anti-Karaoke / Instrumental ---
                _video_title = (info.get("title") or "")
                _karaoke_re = re.compile(r'\b(karaoke|instrumental|pista|cover)\b', re.IGNORECASE)
                _csv_has_keyword = bool(_karaoke_re.search(track_name))
                _yt_has_keyword = bool(_karaoke_re.search(_video_title))
                if not _csv_has_keyword and _yt_has_keyword:
                    update_console(
                        " ✖ yt-dlp: descartado (Versión Karaoke/Instrumental detectada)"
                    )
                    return False, None

                web_dur_s = info.get("duration")
                if web_dur_s is None:
                    return True, None

                web_dur_s = float(web_dur_s)
                diff = abs(web_dur_s - expected_duration_s)

                if diff > 40:
                    update_console(
                        f" \u2716 Descartado (El resultado web es un corte/teaser de "
                        f"{web_dur_s:.0f}s, esperado ~{expected_duration_s:.0f}s, "
                        f"delta={diff:.0f}s > 40s)"
                    )
                    return False, web_dur_s

                update_console(
                    f" \u2714 Duracion web OK: {web_dur_s:.0f}s "
                    f"(esperada {expected_duration_s:.0f}s, delta={diff:.0f}s)"
                )
                return True, web_dur_s

            except Exception as web_err:
                update_console(
                    f" \u26a0 Verificacion web fallida ({web_err.__class__.__name__})"
                    f" — se continuara con la descarga"
                )
                return True, None

        if "yt-dlp" in engine_mode:
            if cancel_ctrl.is_cancelled:
                return 'cancelled', base_filename, '', None, False

            search_query = f'ytsearch1:"{primary_artist}" "{track_name}" "Provided to YouTube" OR Topic'

            web_ok, web_dur = verify_web_duration(search_query)

            if not web_ok:
                reason = (
                    f"yt-dlp: descartado antes de descargar "
                    f"(video web = {web_dur:.0f}s, "
                    f"esperado ~{expected_duration_s:.0f}s)"
                )
            else:
                temp_out = os.path.join(target_folder, f"{temp_stem}.%(ext)s")
                cmd_ytdlp = [
                    "yt-dlp", search_query,
                    "--retries", "5",
                    "--fragment-retries", "5",
                    "--socket-timeout", "15",
                    "--abort-on-error",
                    "--ignore-errors",
                    "--extract-audio",
                    "--audio-format", "mp3",
                    "--audio-quality", "0",
                    "--embed-thumbnail",
                    "--no-playlist",
                    "-o", temp_out,
                ]
                ok, out_log = run_proc(cmd_ytdlp)

                cleanup_residuals(target_folder, temp_stem)

                if not os.path.exists(temp_mp3_path):
                    reason = "yt-dlp: El archivo .mp3 no se genero (fallo de conversion ffmpeg)"
                    update_console(f" \u2716 Error: El archivo .mp3 no se genero (Fallo de conversion)")
                elif verify_duration(temp_mp3_path):
                    embed_post_process(temp_mp3_path)
                    try:
                        os.replace(temp_mp3_path, final_mp3_path)
                        success = True
                    except OSError as rename_err:
                        reason = f"yt-dlp: Error al renombrar archivo temporal: {rename_err}"
                        update_console(f" \u2716 {reason}")
                        try:
                            os.remove(temp_mp3_path)
                        except OSError:
                            pass
                else:
                    reason = "yt-dlp: audio cortado o archivo corrupto"

        if not success and "spotdl" in engine_mode:
            if cancel_ctrl.is_cancelled:
                return 'cancelled', base_filename, '', None, False

            update_console(" -> Probando spotdl...")
            cmd_spotdl = [
                "spotdl", "download", track_url,
                "--output", os.path.join(target_folder, f"{temp_stem}.{{output-ext}}"),
                "--format", "mp3", "--bitrate", "320k"
            ]
            ok, out_log = run_proc(cmd_spotdl)

            cleanup_residuals(target_folder, temp_stem)
            if not os.path.exists(temp_mp3_path):
                reason = "spotdl: El archivo .mp3 no se genero (fallo de conversion)"
                update_console(f" \u2716 Error: El archivo .mp3 no se genero (Fallo de conversion)")
            elif verify_duration(temp_mp3_path):
                embed_post_process(temp_mp3_path)
                try:
                    os.replace(temp_mp3_path, final_mp3_path)
                    success = True
                except OSError as rename_err:
                    reason = f"spotdl: Error al renombrar archivo temporal: {rename_err}"
                    update_console(f" \u2716 {reason}")
                    try:
                        os.remove(temp_mp3_path)
                    except OSError:
                        pass
            else:
                reason = "spotdl: audio cortado o archivo corrupto"

        lrc_loose = os.path.join(target_folder, f"{temp_stem}.lrc")
        if os.path.exists(lrc_loose):
            try:
                os.remove(lrc_loose)
            except OSError:
                pass

        if success:
            return 'success', base_filename, '', None, title_was_cleaned
        else:
            return 'error', base_filename, reason, row.to_dict(), title_was_cleaned

    def _worker_wrapper(task_idx, idx, row):
        """Inyecta el ScriptRunContext de Streamlit en el hilo del pool.
        Sin esto, st.session_state no es accesible desde workers secundarios."""
        if add_script_run_ctx is not None:
            try:
                add_script_run_ctx(threading.current_thread())
            except Exception:
                pass
        return _process_single_track(task_idx, idx, row)

    update_console(f"[INFO] Iniciando descarga concurrente con {max_workers} hilo(s)...")

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {}

        for task_idx, (idx, row) in enumerate(df_sorted.iterrows()):
            if cancel_ctrl.is_cancelled:
                update_console("\n🛑 Cancelación detectada — no se enviarán más tareas al pool.")
                break
            future = executor.submit(_worker_wrapper, task_idx, idx, row)
            futures[future] = (task_idx, idx)

        cancelled_logged = False
        for future in as_completed(futures):
            try:
                status, base_filename, reason, row_dict, title_cleaned = future.result()
            except Exception as exc:
                update_console(f"✖ Error inesperado en hilo worker: {exc}")
                update_progress()
                continue

            if status == 'cancelled':
                if not cancelled_logged:
                    update_console("\n🛑 Proceso cancelado por el usuario. Puedes reanudar cuando desees.")
                    write_log("CANCELADO | Proceso detenido por el usuario")
                    cancelled_logged = True
                update_progress()
                continue

            if status in ('success', 'skipped'):
                increment_success()
                if status == 'success':
                    _clean_note = " (título limpio)" if title_cleaned else ""
                    update_console(f"✔ EXITOSO: {base_filename}.mp3{_clean_note}")
                    _log_detail = " | Título modificado: se limpió etiqueta de video" if title_cleaned else ""
                    write_log(f"ÉXITO | {base_filename}{_log_detail}")

            elif status == 'error':
                update_console(f"✖ ERROR: {base_filename} -- {reason}")
                if row_dict:
                    row_dict['Error_Reason'] = reason
                    append_failed(row_dict)
                _short_reason = str(reason).split('\n')[0][:120]
                write_log(f"ERROR | {base_filename} | {_short_reason}")

            update_progress()

        if cancel_ctrl.is_cancelled:
            for f in futures:
                f.cancel()

    update_console(f"\n[FIN] {state['success_count']}/{total_tracks} canciones completadas.")
    state["running"] = False
    state["done"] = True


if os.path.exists(LOGO_PATH):
    st.sidebar.image(LOGO_PATH, use_container_width=True)

st.sidebar.title("Configuración Inicial")
uploaded_file = st.sidebar.file_uploader("📂 Sube tu archivo CSV de Spotify", type=["csv"])

st.title("Sello de Gato Music")
st.markdown("---")
render_music_cleanup_module()

if uploaded_file is None:
    st.info("👈 Por favor, sube tu archivo `.csv` en el menú lateral para cargar la aplicación.")
    st.stop()

try:
    df_raw = pd.read_csv(uploaded_file)
    df = process_dataframe(df_raw)
except Exception as e:
    st.error(f"Error al procesar el archivo: {e}")
    st.stop()

st.sidebar.markdown("---")
st.sidebar.subheader("Navegación")

if "active_view" not in st.session_state:
    st.session_state["active_view"] = "dashboard"

if st.sidebar.button("📊 Dashboard de Biblioteca", use_container_width=True):
    st.session_state["active_view"] = "dashboard"
if st.sidebar.button("📥 Descargar Músicas", use_container_width=True):
    st.session_state["active_view"] = "downloads"

menu_selection = st.session_state["active_view"]

dl_state = st.session_state["download_state"]
if dl_state["running"] or (dl_state["done"] and dl_state["total"] > 0):
    st.sidebar.markdown("---")
    if dl_state["running"]:
        st.sidebar.markdown("**📥 Descarga en Progreso**")
    else:
        st.sidebar.markdown("**✅ Descarga Finalizada**")
    if dl_state["total"] > 0:
        st.sidebar.progress(dl_state["progress"])
        completed = int(dl_state["progress"] * dl_state["total"])
        caption_text = f"{completed}/{dl_state['total']} canciones"
        if dl_state["running"] and dl_state["current_track"]:
            caption_text += f"\n{dl_state['current_track']}"
        st.sidebar.caption(caption_text)
    if dl_state["running"] and menu_selection == "dashboard":
        time.sleep(0.8)
        st.rerun()

st.markdown("---")

if menu_selection == "dashboard":
    st.subheader("Estadísticas de tu Colección")
    col1, col2, col3, col4, col5, col6 = st.columns(6)
    col1.metric("🎵 Total Canciones", len(df))
    if 'Clean_Primary_Artist' in df.columns:
        col2.metric("🎤 Artistas", df['Clean_Primary_Artist'].nunique())
    if 'Album Name' in df.columns:
        col3.metric("💿 Álbumes", df['Album Name'].nunique())
    if 'Duration_Min' in df.columns:
        col4.metric("⏱ Horas", f"{df['Duration_Min'].sum() / 60:.1f} h")
    if 'Popularity' in df.columns:
        col5.metric("⭐ Popularidad Media", f"{df['Popularity'].mean():.0f}/100")
    if 'Explicit' in df.columns:
        explicit_pct = df['Explicit'].sum() / len(df) * 100
        col6.metric("🔞 Explícitas", f"{explicit_pct:.1f}%")

    st.markdown("<br>", unsafe_allow_html=True)
    row1_col1, row1_col2 = st.columns(2)
    with row1_col1:
        if 'Clean_Primary_Artist' in df.columns:
            st.markdown("**Top 10 Artistas Más Guardados**")
            top_artists = df['Clean_Primary_Artist'].value_counts().head(10).reset_index()
            top_artists.columns = ['Artista', 'Canciones']
            fig_art = px.bar(
                top_artists, x='Canciones', y='Artista', orientation='h',
                color='Canciones', color_continuous_scale='Greens'
            )
            fig_art.update_layout(
                yaxis={'categoryorder': 'total ascending'},
                margin=dict(l=0, r=0, t=0, b=0)
            )
            st.plotly_chart(fig_art, use_container_width=True)

    with row1_col2:
        if 'Release Year' in df.columns:
            st.markdown("**Distribución por Año de Lanzamiento**")
            fig_year = px.histogram(
                df, x='Release Year', nbins=30,
                color_discrete_sequence=['#1DB954']
            )
            fig_year.update_layout(margin=dict(l=0, r=0, t=0, b=0))
            st.plotly_chart(fig_year, use_container_width=True)

    row2_col1, row2_col2 = st.columns(2)
    with row2_col1:
        if 'Energy' in df.columns and 'Valence' in df.columns:
            st.markdown("**Energy vs Valence**")
            scatter_df = df[['Energy', 'Valence', 'Track Name', 'Clean_Primary_Artist']].dropna(
                subset=['Energy', 'Valence']
            )
            fig_scatter = px.scatter(
                scatter_df,
                x='Energy', y='Valence',
                hover_name='Track Name',
                hover_data={'Clean_Primary_Artist': True, 'Energy': ':.2f', 'Valence': ':.2f'},
                color='Valence',
                color_continuous_scale='RdYlGn',
                opacity=0.7,
                labels={'Clean_Primary_Artist': 'Artista'}
            )
            fig_scatter.update_layout(
                xaxis_title="Energía",
                yaxis_title="Valencia (positividad)",
                margin=dict(l=0, r=0, t=0, b=0)
            )
            st.plotly_chart(fig_scatter, use_container_width=True)

    with row2_col2:
        if 'Genres' in df.columns:
            st.markdown("**Géneros Más Escuchados**")
            genres_series = (
                df['Genres'].dropna()
                .astype(str)
                .str.split(',')
                .explode()
                .str.strip()
                .replace('', pd.NA)
                .dropna()
            )
            if len(genres_series) > 0:
                top_genres = genres_series.value_counts().head(15).reset_index()
                top_genres.columns = ['Género', 'Canciones']
                fig_genres = px.bar(
                    top_genres, x='Canciones', y='Género', orientation='h',
                    color='Canciones', color_continuous_scale='Blues'
                )
                fig_genres.update_layout(
                    yaxis={'categoryorder': 'total ascending'},
                    margin=dict(l=0, r=0, t=0, b=0)
                )
                st.plotly_chart(fig_genres, use_container_width=True)
            else:
                st.info("No se encontraron datos de géneros en el CSV.")

    if 'Popularity' in df.columns:
        st.markdown("**🏆 Canciones Más Populares**")
        pop_cols = [c for c in [
            'Track Name', 'Artist Name(s)', 'Album Name',
            'Popularity', 'Danceability', 'Energy', 'Valence', 'Tempo', 'Explicit'
        ] if c in df.columns]
        top_songs = (
            df[pop_cols]
            .dropna(subset=['Popularity'])
            .sort_values('Popularity', ascending=False)
            .head(20)
        )
        st.dataframe(top_songs, use_container_width=True, hide_index=True)
    else:
        st.markdown("**Vista Previa de Datos**")
        display_cols = [c for c in [
            'Track Name', 'Artist Name(s)', 'Album Name', 'Release Year'
        ] if c in df.columns]
        st.dataframe(df[display_cols].head(100), use_container_width=True, hide_index=True)


elif menu_selection == "downloads":
    st.subheader("Descargador y Organizador")

    col_conf1, col_conf2 = st.columns([1, 1])

    with col_conf1:
        host_dir_visual = os.getenv("HOST_MUSIC_DIR", "Ruta_Windows_No_Definida")
        st.text_input(
            "📂 Ruta Base de Descarga:",
            value=host_dir_visual,
            help="Carpeta raíz donde se guardarán los MP3 (Ruta en tu máquina local).",
            disabled=True
        )
        download_base_path = "/app/output"
        st.session_state["download_base_path"] = download_base_path

        custom_root_folder = st.text_input("📁 Carpeta Raíz (subcarpeta):", value="Mi Musica")
        engine_mode = st.selectbox(
            "Estrategia de Motores:",
            ["Solo yt-dlp (Recomendado y rápido)", "Cascada Automática (spotdl ➔ yt-dlp)", "Solo spotdl"],
            disabled=True
        )

        max_workers = st.selectbox(
            "🧵 Hilos de descarga simultáneos", 
            options=[1, 2, 3, 4, 5], 
            index=1
        )

        spotipy_client_id = ""
        spotipy_client_secret = ""

        if "spotdl" in engine_mode:
            st.warning("Para usar spotdl necesitas tus credenciales de Spotify for Developers.")
            spotipy_client_id = st.text_input("Client ID", type="password")
            spotipy_client_secret = st.text_input("Client Secret", type="password")

    with col_conf2:
        _effective_base = st.session_state["download_base_path"]
        host_dir_visual = os.getenv("HOST_MUSIC_DIR", "Ruta_Windows_No_Definida")
        st.write("📌 **Reglas de guardado (Preview):**")
        st.write(f"- Ruta: `{host_dir_visual}/{sanitize_name(custom_root_folder)}/{{ArtistaPrincipal}}/`")
        if _effective_base.rstrip("/") == "/app/output":
            st.info("📁 Ruta interna: /app/output/... (Mapeado a tu carpeta local de Música a través de Docker)")
        st.write("- Archivo: `NombreCancion, Album, Artista.mp3`")

        download_running = st.session_state["download_state"]["running"]
        btn_col1, btn_col2 = st.columns([3, 1])
        with btn_col1:
            if download_running:
                _btn_label = "⏳ Descarga en Curso..."
            elif st.session_state["download_state"]["done"]:
                _btn_label = "▶️ Iniciar / Reanudar Descarga"
            else:
                _btn_label = "🚀 Iniciar / Reanudar Descarga"
            start_download = st.button(
                _btn_label,
                type="primary",
                use_container_width=True,
                disabled=download_running
            )
        with btn_col2:
            cancel_download = st.button(
                "🛑 Cancelar Descarga",
                use_container_width=True,
                disabled=not download_running,
                type="secondary",
            )
            if cancel_download:
                st.session_state["download_control"].request_cancel()
                st.toast("🛑 Cancelación solicitada. El proceso se detendrá tras la pista actual.", icon="🛑")

    if start_download:
        if "spotdl" in engine_mode and (not spotipy_client_id or not spotipy_client_secret):
            st.error("⚠️ Debes ingresar tu Client ID y Client Secret de Spotify para poder usar esta estrategia.")
        else:
            cancel_ctrl = st.session_state["download_control"]
            cancel_ctrl.reset()

            effective_base = st.session_state["download_base_path"]
            os.makedirs(effective_base, exist_ok=True)
            log_file_path = os.path.join(effective_base, "registro_descargas.txt")

            _resume_msg = "REANUDADO | Retomando proceso de descarga..."
            st.session_state["download_state"]["log_lines"].insert(0, f"\n▶️ {_resume_msg}")
            try:
                with open(log_file_path, 'a', encoding='utf-8') as _lf:
                    _lf.write(f"{_resume_msg}\n")
            except Exception:
                pass

            df_sorted = df.sort_values(by=['Clean_Primary_Artist', 'Track Name']).reset_index(drop=True)
            root_target_dir = os.path.join(effective_base, sanitize_name(custom_root_folder))
            os.makedirs(root_target_dir, exist_ok=True)

            st.session_state["log_file_path"] = log_file_path

            env_vars = os.environ.copy()
            if "spotdl" in engine_mode:
                env_vars["SPOTIPY_CLIENT_ID"] = spotipy_client_id
                env_vars["SPOTIPY_CLIENT_SECRET"] = spotipy_client_secret

            job_thread = threading.Thread(
                target=run_download_job,
                args=(df_sorted, root_target_dir, log_file_path, engine_mode, env_vars, cancel_ctrl, max_workers),
                daemon=True,
            )
            # Inyectar contexto de Streamlit al hilo orquestador
            if add_script_run_ctx is not None:
                add_script_run_ctx(job_thread)

            job_thread.start()
            st.toast(f"🚀 Descarga iniciada con {max_workers} hilo(s). Puedes navegar libremente.", icon="🎵")
            st.rerun()

    dl_state = st.session_state["download_state"]
    if dl_state["running"] or (dl_state["done"] and dl_state["total"] > 0):
        st.markdown("#### 📟 Consola en Vivo")

        progress_bar = st.progress(dl_state["progress"])
        completed = int(dl_state["progress"] * dl_state["total"]) if dl_state["total"] > 0 else 0
        if dl_state["running"]:
            status_text = f"Procesando ({completed}/{dl_state['total']}): **{dl_state['current_track']}**"
        else:
            status_text = f"✅ Completado: {dl_state['success_count']}/{dl_state['total']} canciones"
        st.caption(status_text)

        log_lines = dl_state["log_lines"]
        escaped_lines = (
            "\n".join(log_lines)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )
        console_html = f"""
        <div id="console-wrapper" style="
            background:#0d1117;
            border:1px solid #30363d;
            border-radius:8px;
            padding:12px 16px;
            height:320px;
            overflow-y:auto;
            font-family:'JetBrains Mono','Fira Code','Courier New',monospace;
            font-size:12.5px;
            line-height:1.6;
            color:#e6edf3;
            white-space:pre-wrap;
            word-break:break-all;
        ">{escaped_lines}</div>
        """
        st.html(console_html)


        with st.container():
            st.markdown("#### 📑 Registro de Resultados")

            _filter_col, _spacer = st.columns([2, 5])
            with _filter_col:
                log_filter = st.radio(
                    "Filtrar por estado:",
                    options=["Errores", "Éxitos", "Omitidos", "Todos"],
                    index=0,
                    horizontal=True,
                    key="log_filter_radio",
                    label_visibility="collapsed",
                )

            def _render_log_table(filter_choice: str):
                _lfp = st.session_state.get("log_file_path", "")
                if not _lfp or not os.path.exists(_lfp):
                    st.info("Aún no hay entradas en el registro. Inicia una descarga para comenzar.")
                    return

                prefix_map = {
                    "Errores":  "ERROR",
                    "Éxitos":   "ÉXITO",
                    "Omitidos": "OMITIDO",
                    "Todos":    None,
                }
                target_prefix = prefix_map.get(filter_choice)

                rows = []
                try:
                    with open(_lfp, 'r', encoding='utf-8') as _lf:
                        for raw_line in _lf:
                            line = raw_line.strip()
                            if not line:
                                continue
                            parts = [p.strip() for p in line.split('|')]
                            estado  = parts[0] if len(parts) > 0 else ""
                            cancion = parts[1] if len(parts) > 1 else ""
                            detalle = parts[2] if len(parts) > 2 else ""
                            if target_prefix is None or estado.upper().startswith(target_prefix.upper()):
                                rows.append({"Estado": estado, "Canción": cancion, "Detalle / Error": detalle})
                except Exception as _read_err:
                    st.warning(f"No se pudo leer el registro: {_read_err}")
                    return

                if not rows:
                    st.info(f"Sin resultados para el filtro **{filter_choice}**.")
                    return

                _df_log = pd.DataFrame(rows)

                def _style_estado(val):
                    color_map = {
                        "ÉXITO":   "color: #3fb950; font-weight: 600;",
                        "ERROR":   "color: #f85149; font-weight: 600;",
                        "OMITIDO": "color: #d29922; font-weight: 600;",
                    }
                    for key, style in color_map.items():
                        if val.upper().startswith(key):
                            return style
                    return ""

                styled = _df_log.style.map(_style_estado, subset=["Estado"])
                st.dataframe(
                    styled,
                    use_container_width=True,
                    hide_index=True,
                    height=min(400, 35 * len(rows) + 38),
                )
                st.caption(
                    f"📁 Registro guardado en: `{_lfp}` — "
                    f"{len(rows)} entradas mostradas"
                )

            _render_log_table(log_filter)

        if dl_state["running"]:
            time.sleep(1)
            st.rerun()
        else:
            failed_songs = dl_state["failed_songs"]
            if failed_songs:
                df_failed = pd.DataFrame(failed_songs)
                st.error(f"⚠️ {len(failed_songs)} errores detectados.")
                cols = [c for c in ['Track Name', 'Artist Name(s)', 'Album Name', 'Error_Reason'] if c in df_failed.columns]
                st.dataframe(df_failed[cols], use_container_width=True, hide_index=True)
                st.download_button(
                    "📥 Descargar CSV de Errores",
                    data=df_failed.to_csv(index=False).encode('utf-8'),
                    file_name="errores.csv",
                    mime="text/csv"
                )
            else:
                st.balloons()
                st.success("✨ ¡Descarga completada sin errores!")
