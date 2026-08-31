import streamlit as st
import pandas as pd
import plotly.express as px
import subprocess
import os
import re
import io
import time
import threading
import requests
import syncedlyrics
from PIL import Image
import glob
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

# --- CONFIGURACIÓN DE PÁGINA Y LOGO ---
LOGO_PATH = "logo.png"
page_icon = "🎵"
if os.path.exists(LOGO_PATH):
    try:
        page_icon = Image.open(LOGO_PATH)
    except Exception:
        page_icon = "🎵"

st.set_page_config(page_title="Sello de Gato Music", page_icon=page_icon, layout="wide")

BASE_OUTPUT_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "music_export"
)
os.makedirs(BASE_OUTPUT_DIR, exist_ok=True)

# --- INICIALIZACIÓN DE ESTADO DE DESCARGA EN SESSION_STATE ---
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

# --- FUNCIONES DE PROCESAMIENTO ---
def sanitize_name(name):
    clean = re.sub(r'[\\/*?:"<>|]', "", str(name)).strip()
    return clean if clean else ""

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

def embed_lyrics_into_mp3(mp3_path, lyrics_text):
    """Incrusta letra no sincronizada (USLT) y sincronizada (SYLT) si el formato lo admite."""
    try:
        audio = ID3(mp3_path)
    except ID3NoHeaderError:
        audio = ID3()

    # USLT – letra plana (sin timestamps)
    audio.delall("USLT")
    plain_text = re.sub(r'\[\d+:\d+\.\d+\]', '', lyrics_text).strip()
    audio.add(USLT(encoding=Encoding.UTF8, lang='eng', desc='', text=plain_text))

    # SYLT – letra sincronizada (con timestamps en ms)
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


def embed_full_metadata(mp3_path, row, track_name, full_artist, album_name):
    """Incrusta en el MP3 toda la metadata disponible del DataFrame + carátula."""
    try:
        tags = ID3(mp3_path)
    except ID3NoHeaderError:
        tags = ID3()

    # --- Etiquetas de texto ---
    tags.delall("TIT2"); tags.add(TIT2(encoding=Encoding.UTF8, text=track_name))
    tags.delall("TPE1"); tags.add(TPE1(encoding=Encoding.UTF8, text=full_artist))
    if album_name:
        tags.delall("TALB"); tags.add(TALB(encoding=Encoding.UTF8, text=album_name))

    # Año de lanzamiento
    release_year = str(row.get('Release Year', '') or row.get('Release Date', '') or '').strip()[:4]
    if release_year.isdigit():
        tags.delall("TDRC"); tags.add(TDRC(encoding=Encoding.UTF8, text=release_year))

    # Géneros (columna Genres del CSV, separados por coma)
    genres_raw = str(row.get('Genres', '') or '').strip()
    if genres_raw and genres_raw.lower() not in ('nan', 'none', ''):
        first_genre = genres_raw.split(',')[0].strip()
        tags.delall("TCON"); tags.add(TCON(encoding=Encoding.UTF8, text=first_genre))

    # Número de pista
    track_number = str(row.get('Track Number', '') or '').strip()
    disc_number  = str(row.get('Disc Number', '') or '').strip()
    if track_number.isdigit():
        trck_str = f"{track_number}/{disc_number}" if disc_number.isdigit() else track_number
        tags.delall("TRCK"); tags.add(TRCK(encoding=Encoding.UTF8, text=trck_str))

    # Comentario con datos adicionales de audio-features
    extras = {k: row.get(k) for k in ['Popularity', 'Danceability', 'Energy', 'Valence', 'Tempo', 'Explicit'] if row.get(k) is not None}
    if extras:
        comment_text = ' | '.join(f"{k}: {v}" for k, v in extras.items())
        tags.delall("COMM"); tags.add(COMM(encoding=Encoding.UTF8, lang='eng', desc='Spotify Features', text=comment_text))

    # --- Carátula (APIC) ---
    cover_url = str(row.get('Album Image URL', '') or row.get('Cover URL', '') or row.get('Image URL', '') or '').strip()
    if cover_url and cover_url.startswith('http'):
        try:
            resp = requests.get(cover_url, timeout=10)
            if resp.status_code == 200:
                img_data = resp.content
                mime = 'image/jpeg' if cover_url.lower().endswith('.jpg') or b'\xff\xd8' in img_data[:3] else 'image/png'
                tags.delall("APIC")
                tags.add(APIC(encoding=Encoding.UTF8, mime=mime, type=3, desc='Cover', data=img_data))
        except Exception:
            pass

    tags.save(mp3_path)


def fetch_itunes_metadata(track_name, artist_name):
    """
    Consulta la API publica de iTunes Search (sin credenciales) para obtener
    metadatos oficiales y carátula en alta resolución de una canción.

    Retorna un dict con las claves:
        title, artist, album, genre, year, artwork_url, artwork_data
    Cualquier campo puede ser None si la API no lo devuelve o falla la petición.
    Retorna None completo si la API no devuelve resultados o hay error de red.
    """
    try:
        term = f"{track_name} {artist_name}"
        resp = requests.get(
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

        # --- Año de lanzamiento ---
        release_date_raw = item.get("releaseDate", "")  # e.g. "2019-04-05T07:00:00Z"
        year = release_date_raw[:4] if release_date_raw and len(release_date_raw) >= 4 else None

        # --- Carátula HD ---
        # La API devuelve artworkUrl100 (100×100 px).
        # Reemplazando el sufijo obtenemos 1000×1000 sin coste adicional.
        artwork_url = item.get("artworkUrl100", "")
        if artwork_url:
            artwork_url = artwork_url.replace("100x100bb.jpg", "1000x1000bb.jpg")

        # Descarga en memoria de la imagen HD
        artwork_data = None
        if artwork_url:
            try:
                img_resp = requests.get(artwork_url, timeout=10)
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


# --- FUNCIÓN PRINCIPAL DE DESCARGA (EJECUTADA EN HILO SECUNDARIO) ---
def run_download_job(df_sorted, root_target_dir, engine_mode, env_vars):
    """
    Ejecuta el bucle de descarga en un hilo secundario.
    Escribe el progreso en st.session_state['download_state'] para que
    la UI pueda leerlo desde cualquier pantalla de la app.
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

    def update_console(text):
        """Mejora 2: inserta al inicio para que los mensajes nuevos aparezcan arriba."""
        state["log_lines"].insert(0, text)

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

    for idx, row in df_sorted.iterrows():
        track_name     = sanitize_name(row.get('Track Name', 'Desconocido'))
        full_artist    = sanitize_name(row.get('Artist Name(s)', 'Desconocido'))
        primary_artist = row.get('Clean_Primary_Artist', 'Varios Artistas')
        album_name     = sanitize_name(row.get('Album Name', ''))
        track_uri      = str(row.get('Track URI', ''))

        track_url = (
            f"https://open.spotify.com/track/{track_uri.split(':')[-1]}"
            if track_uri.startswith('spotify:track:') else track_uri
        )

        target_folder = os.path.join(root_target_dir, primary_artist)
        os.makedirs(target_folder, exist_ok=True)

        base_filename  = format_track_filename(track_name, album_name, full_artist)
        final_mp3_path = os.path.join(target_folder, f"{base_filename}.mp3")

        current_num = idx + 1
        state["current_track"] = f"({current_num}/{total_tracks}) {base_filename}"
        update_console(f"\n[INFO] ({current_num}/{total_tracks}) {primary_artist} -> {base_filename}")

        # --- Mejora 3: Omisión Robusta — verificar existencia y tamaño >100 KB ---
        if os.path.exists(final_mp3_path) and os.path.getsize(final_mp3_path) > 100_000:
            update_console(f"✔ Omitido (Ya existe): {base_filename}")
            state["success_count"] += 1
            state["progress"] = current_num / total_tracks
            continue

        success = False
        reason  = ""

        # Duración esperada desde el CSV (en segundos). None si la columna no existe.
        expected_duration_s = None
        raw_ms = row.get('Duration (ms)', None)
        try:
            if raw_ms is not None and str(raw_ms).strip() not in ('', 'nan', 'None'):
                expected_duration_s = float(raw_ms) / 1000.0
        except (ValueError, TypeError):
            pass

        # ------------------------------------------------------------------ #
        # Helpers locales reutilizados por ambos motores                      #
        # ------------------------------------------------------------------ #

        def verify_duration(mp3_path):
            """
            Capa 2 + Capa 4 combinadas:
              - Validacion estructural: intenta abrir el MP3 con mutagen.
                Si falla (HeaderNotFoundError, archivo corrupto o fake),
                elimina el archivo y retorna False.
              - Validacion de duracion: compara duracion real vs CSV con
                tolerancia de 15 s. Elimina y retorna False si excede.
            Retorna True si el archivo es valido y la duracion es aceptable.
            Retorna True incondicionalmente si no hay referencia en el CSV.
            """
            # --- Capa 4: Validacion Estructural (Anti-Fake MP3) ---
            try:
                audio_info = MP3(mp3_path)
            except Exception as struct_err:
                # HeaderNotFoundError, MutagenError, o cualquier fallo de lectura
                # indica un archivo corrupto, vacio o un raw camuflado (.webm/.m4a)
                update_console(
                    f" \u2716 Archivo MP3 corrupto o falso: {struct_err.__class__.__name__} "
                    f"-- archivo eliminado"
                )
                try:
                    os.remove(mp3_path)
                except OSError:
                    pass
                return False

            # --- Capa 2: Validacion de Duracion ---
            if expected_duration_s is None:
                update_console(" \u2714 Estructura MP3 OK (sin referencia de duracion en CSV)")
                return True

            real_s = audio_info.info.length
            diff   = abs(real_s - expected_duration_s)
            if diff > 15:
                update_console(
                    f" \u2716 Duracion invalida: real={real_s:.1f}s "
                    f"esperada={expected_duration_s:.1f}s (delta={diff:.1f}s > 15s) "
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
            """
            Elimina archivos residuales temporales generados por yt-dlp/ffmpeg
            (.webm, .m4a, .webp, .part) que contengan el nombre de la pista.
            Cubre el caso en que ffmpeg falla silenciosamente y deja el raw.
            """
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
            """
            Post-procesado exhaustivo tras una descarga exitosa.

            Estrategia de metadatos (por prioridad):
              1. iTunes API  → titulo, artista, album, genero, ano, caratula HD
              2. CSV/DataFrame → fallback para cualquier campo que iTunes omita
              3. COMM con audio-features de Spotify (siempre desde el CSV)
              4. TRCK (numero de pista) desde el CSV
              5. Letras USLT/SYLT via syncedlyrics
            """
            # -------------------------------------------------------------- #
            # Paso 1: Consulta a iTunes API (fuente primaria)                 #
            # -------------------------------------------------------------- #
            update_console(" ▶ Consultando iTunes API para metadatos HD...")
            itunes = fetch_itunes_metadata(track_name, primary_artist)

            if itunes:
                update_console(
                    f" \u2714 iTunes: '{itunes.get('title')}' "
                    f"- {itunes.get('artist')} "
                    f"[{itunes.get('album')}] "
                    f"({itunes.get('year')})"
                )
            else:
                update_console(" \u26a0 iTunes no encontro la cancion — usando datos del CSV")

            # -------------------------------------------------------------- #
            # Paso 2: Construir valores finales (iTunes > CSV)                #
            # -------------------------------------------------------------- #
            final_title  = (itunes or {}).get("title")  or track_name
            final_artist = (itunes or {}).get("artist") or full_artist
            final_album  = (itunes or {}).get("album")  or album_name
            final_genre  = (itunes or {}).get("genre")
            final_year   = (itunes or {}).get("year")

            # Fallback de genero y ano al CSV si iTunes no los devuelve
            if not final_genre:
                genres_raw = str(row.get('Genres', '') or '').strip()
                if genres_raw and genres_raw.lower() not in ('nan', 'none', ''):
                    final_genre = genres_raw.split(',')[0].strip()

            if not final_year:
                final_year = str(
                    row.get('Release Year', '') or row.get('Release Date', '') or ''
                ).strip()[:4]
                if not final_year.isdigit():
                    final_year = None

            # -------------------------------------------------------------- #
            # Paso 3: Incrustar etiquetas ID3                                 #
            # -------------------------------------------------------------- #
            try:
                try:
                    tags = ID3(mp3_path)
                except ID3NoHeaderError:
                    tags = ID3()

                # Campos de texto
                tags.delall("TIT2"); tags.add(TIT2(encoding=Encoding.UTF8, text=final_title))
                tags.delall("TPE1"); tags.add(TPE1(encoding=Encoding.UTF8, text=final_artist))
                if final_album:
                    tags.delall("TALB"); tags.add(TALB(encoding=Encoding.UTF8, text=final_album))
                if final_year and str(final_year).isdigit():
                    tags.delall("TDRC"); tags.add(TDRC(encoding=Encoding.UTF8, text=str(final_year)))
                if final_genre:
                    tags.delall("TCON"); tags.add(TCON(encoding=Encoding.UTF8, text=final_genre))

                # Numero de pista (siempre del CSV)
                track_number = str(row.get('Track Number', '') or '').strip()
                disc_number  = str(row.get('Disc Number',  '') or '').strip()
                if track_number.isdigit():
                    trck_str = f"{track_number}/{disc_number}" if disc_number.isdigit() else track_number
                    tags.delall("TRCK"); tags.add(TRCK(encoding=Encoding.UTF8, text=trck_str))

                # COMM: audio-features de Spotify (del CSV)
                extras = {
                    k: row.get(k)
                    for k in ['Popularity', 'Danceability', 'Energy', 'Valence', 'Tempo', 'Explicit']
                    if row.get(k) is not None
                }
                if extras:
                    comment_text = ' | '.join(f"{k}: {v}" for k, v in extras.items())
                    tags.delall("COMM")
                    tags.add(COMM(encoding=Encoding.UTF8, lang='eng', desc='Spotify Features', text=comment_text))

                # -------------------------------------------------------------- #
                # Carátula APIC: iTunes HD > URL del CSV > sin caratula          #
                # -------------------------------------------------------------- #
                artwork_data = (itunes or {}).get("artwork_data")
                artwork_mime = "image/jpeg"

                if artwork_data:
                    update_console(" \u2714 Caratula HD de iTunes incrustada (1000x1000)")
                else:
                    # Fallback: intentar URL de imagen del CSV
                    cover_url = str(
                        row.get('Album Image URL', '') or
                        row.get('Cover URL', '')       or
                        row.get('Image URL', '')        or ''
                    ).strip()
                    if cover_url and cover_url.startswith('http'):
                        try:
                            cov_resp = requests.get(cover_url, timeout=10)
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
                update_console(
                    " \u2714 Metadata ID3 completa "
                    f"({'iTunes+CSV' if itunes else 'solo CSV'}): "
                    f"{final_title} / {final_artist} / {final_album}"
                )

            except Exception as meta_err:
                update_console(f" \u26a0 Error incrustando metadata: {meta_err}")

            # -------------------------------------------------------------- #
            # Paso 4: Letras USLT/SYLT via syncedlyrics                       #
            # -------------------------------------------------------------- #
            try:
                lyrics_text = syncedlyrics.search(f"{primary_artist} {track_name}")
                if lyrics_text:
                    embed_lyrics_into_mp3(mp3_path, lyrics_text)
                    update_console(" \u2714 Letras USLT/SYLT incrustadas")
                else:
                    update_console(" \u2139 Sin letra disponible en syncedlyrics")
            except Exception:
                pass

        # ------------------------------------------------------------------ #
        # Capa 3: Verificación de Duración Web Previa (pre-descarga)          #
        # Usa la API Python de yt-dlp con download=False para obtener la      #
        # duración real del video en YouTube antes de iniciar la descarga.    #
        # Tolerancia: ±20 segundos respecto a Duration (ms) del CSV.          #
        # ------------------------------------------------------------------ #

        def verify_web_duration(search_query):
            """
            Consulta los metadatos del primer resultado de YouTube para la
            búsqueda dada sin descargar nada (download=False).

            Retorna (True, web_dur_s)  si la duración es aceptable o si no
            hay referencia en el CSV con la que comparar.
            Retorna (False, web_dur_s) si el video es un corte/teaser/Short
            con una diferencia mayor a 20 s respecto a la canción oficial.
            Retorna (True, None) si yt-dlp no puede obtener el metadato
            (fallo de red, etc.) para no bloquear la descarga innecesariamente.
            """
            if expected_duration_s is None:
                return True, None  # Sin referencia, dejar pasar

            try:
                import yt_dlp  # importación local; ya es dependencia del entorno

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

                # extract_info puede devolver una lista (playlist) o un dict
                if info is None:
                    return True, None
                if "entries" in info:
                    entries = [e for e in (info.get("entries") or []) if e]
                    if not entries:
                        return True, None
                    info = entries[0]

                web_dur_s = info.get("duration")
                if web_dur_s is None:
                    return True, None  # YouTube no reportó duración → dejar pasar

                web_dur_s = float(web_dur_s)
                diff = abs(web_dur_s - expected_duration_s)

                if diff > 20:
                    update_console(
                        f" \u2716 Descartado (El resultado web es un corte/teaser de "
                        f"{web_dur_s:.0f}s, esperado ~{expected_duration_s:.0f}s, "
                        f"delta={diff:.0f}s > 20s)"
                    )
                    return False, web_dur_s

                update_console(
                    f" \u2714 Duracion web OK: {web_dur_s:.0f}s "
                    f"(esperada {expected_duration_s:.0f}s, delta={diff:.0f}s)"
                )
                return True, web_dur_s

            except Exception as web_err:
                # Cualquier error de red/API no debe bloquear la descarga
                update_console(
                    f" \u26a0 Verificacion web fallida ({web_err.__class__.__name__})"
                    f" — se continuara con la descarga"
                )
                return True, None

        # ------------------------------------------------------------------ #
        # 1. MOTOR yt-dlp                                                     #
        # ------------------------------------------------------------------ #
        if "yt-dlp" in engine_mode:
            search_query = f"ytsearch1:{primary_artist} - {track_name} Audio"

            # --- Capa 3: Pre-verificación web antes de descargar ---
            web_ok, web_dur = verify_web_duration(search_query)

            if not web_ok:
                reason = (
                    f"yt-dlp: descartado antes de descargar "
                    f"(video web = {web_dur:.0f}s, "
                    f"esperado ~{expected_duration_s:.0f}s)"
                )
            else:
                temp_out = os.path.join(target_folder, f"{base_filename}.%(ext)s")
                # Flags obligatorios: -x fuerza extraccion de audio,
                # --audio-format mp3 fuerza conversion ffmpeg,
                # --audio-quality 0 = mejor calidad VBR (equivalente a ~320 kbps)
                cmd_ytdlp = [
                    "yt-dlp", search_query,
                    "-x", "--audio-format", "mp3",
                    "--audio-quality", "0",
                    "--embed-thumbnail",
                    "--no-playlist",
                    "-o", temp_out,
                ]
                ok, out_log = run_proc(cmd_ytdlp)

                # Limpiar residuos independientemente del resultado de yt-dlp
                cleanup_residuals(target_folder, base_filename)

                # --- Verificacion estricta de existencia (Capa 4) ---
                if not os.path.exists(final_mp3_path):
                    reason = "yt-dlp: El archivo .mp3 no se genero (fallo de conversion ffmpeg)"
                    update_console(f" \u2716 Error: El archivo .mp3 no se genero (Fallo de conversion)")
                elif verify_duration(final_mp3_path):
                    embed_post_process(final_mp3_path)
                    success = True
                else:
                    reason = "yt-dlp: audio cortado o archivo corrupto"

        # ------------------------------------------------------------------ #
        # 2. MOTOR spotdl                                                     #
        # ------------------------------------------------------------------ #
        if not success and "spotdl" in engine_mode:
            update_console(" -> Probando spotdl...")
            cmd_spotdl = [
                "spotdl", "download", track_url,
                "--output", os.path.join(target_folder, f"{base_filename}.{{output-ext}}"),
                "--format", "mp3", "--bitrate", "320k"
            ]
            ok, out_log = run_proc(cmd_spotdl)

            cleanup_residuals(target_folder, base_filename)

            # --- Verificacion estricta de existencia (Capa 4) ---
            if not os.path.exists(final_mp3_path):
                reason = "spotdl: El archivo .mp3 no se genero (fallo de conversion)"
                update_console(f" \u2716 Error: El archivo .mp3 no se genero (Fallo de conversion)")
            elif verify_duration(final_mp3_path):
                embed_post_process(final_mp3_path)
                success = True
            else:
                reason = "spotdl: audio cortado o archivo corrupto"

        # Limpieza de .lrc suelto generado por algunas versiones de spotdl
        lrc_loose = os.path.join(target_folder, f"{base_filename}.lrc")
        if os.path.exists(lrc_loose):
            os.remove(lrc_loose)

        if success:
            state["success_count"] += 1
            update_console(f"\u2714 EXITOSO: {base_filename}.mp3")
        else:
            update_console(f"\u2716 ERROR: {base_filename} -- {reason}")
            song_dict = row.to_dict()
            song_dict['Error_Reason'] = reason
            state["failed_songs"].append(song_dict)

        state["progress"] = current_num / total_tracks

    update_console(f"\n[FIN] {state['success_count']}/{total_tracks} canciones completadas.")
    state["running"] = False
    state["done"] = True


# --- BARRA LATERAL (LOGO, CARGA Y NAVEGACIÓN) ---
if os.path.exists(LOGO_PATH):
    st.sidebar.image(LOGO_PATH, use_container_width=True)

st.sidebar.title("Configuración Inicial")
uploaded_file = st.sidebar.file_uploader("📂 Sube tu archivo CSV de Spotify", type=["csv"])

# Bloqueo estricto: Si no hay archivo, la app se detiene aquí.
if uploaded_file is None:
    st.info("👈 Por favor, sube tu archivo `.csv` en el menú lateral para cargar la aplicación.")
    st.stop()

# Si hay archivo, procesamos y mostramos el menú de navegación
try:
    df_raw = pd.read_csv(uploaded_file)
    df = process_dataframe(df_raw)
except Exception as e:
    st.error(f"Error al procesar el archivo: {e}")
    st.stop()

st.sidebar.markdown("---")
st.sidebar.subheader("Navegación")

# Inicializar session_state para la vista activa
if "active_view" not in st.session_state:
    st.session_state["active_view"] = "dashboard"

if st.sidebar.button("📊 Dashboard de Biblioteca", use_container_width=True):
    st.session_state["active_view"] = "dashboard"
if st.sidebar.button("📥 Descargar Músicas", use_container_width=True):
    st.session_state["active_view"] = "downloads"

menu_selection = st.session_state["active_view"]

# --- INDICADOR GLOBAL DE DESCARGA EN SIDEBAR (visible en cualquier pantalla) ---
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
    # Si estamos en Dashboard y hay descarga activa, refrescar el sidebar periódicamente
    if dl_state["running"] and menu_selection == "dashboard":
        time.sleep(0.8)
        st.rerun()

# Header general
st.title("Sello de Gato Music")
st.markdown("---")

# ==========================================
# PANTALLA 1: DASHBOARD
# ==========================================
if menu_selection == "dashboard":
    st.subheader("Estadísticas de tu Colección")

    # --- MÉTRICAS PRINCIPALES ---
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

    # --- FILA 1: Top Artistas | Distribución por Año ---
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

    # --- FILA 2: Energy vs Valence | Géneros más escuchados ---
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

    # --- FILA 3: Canciones más populares ---
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


# ==========================================
# PANTALLA 2: DESCARGAS
# ==========================================
elif menu_selection == "downloads":
    st.subheader("Descargador y Organizador")

    col_conf1, col_conf2 = st.columns([1, 1])

    with col_conf1:
        custom_root_folder = st.text_input("📁 Carpeta Raíz de Descarga:", value="Mi Musica")
        engine_mode = st.selectbox(
            "Estrategia de Motores:",
            ["Solo yt-dlp (Recomendado y rápido)", "Cascada Automática (spotdl ➔ yt-dlp)", "Solo spotdl"]
        )

        spotipy_client_id = ""
        spotipy_client_secret = ""

        if "spotdl" in engine_mode:
            st.warning("Para usar spotdl necesitas tus credenciales de Spotify for Developers.")
            spotipy_client_id = st.text_input("Client ID", type="password")
            spotipy_client_secret = st.text_input("Client Secret", type="password")

    with col_conf2:
        st.write("📌 **Reglas de guardado:**")
        st.write(f"- Ruta: `./music_export/{sanitize_name(custom_root_folder)}/{{ArtistaPrincipal}}/`")
        st.write("- Archivo: `NombreCancion, Album, Artista.mp3`")

        # Botón deshabilitado si ya hay descarga activa
        download_running = st.session_state["download_state"]["running"]
        start_download = st.button(
            "🚀 Iniciar Descarga Unificada" if not download_running else "⏳ Descarga en Curso...",
            type="primary",
            use_container_width=True,
            disabled=download_running
        )

    if start_download:
        if "spotdl" in engine_mode and (not spotipy_client_id or not spotipy_client_secret):
            st.error("⚠️ Debes ingresar tu Client ID y Client Secret de Spotify para poder usar esta estrategia.")
        else:
            df_sorted = df.sort_values(by=['Clean_Primary_Artist', 'Track Name']).reset_index(drop=True)
            root_target_dir = os.path.join(BASE_OUTPUT_DIR, sanitize_name(custom_root_folder))
            os.makedirs(root_target_dir, exist_ok=True)

            env_vars = os.environ.copy()
            if "spotdl" in engine_mode:
                env_vars["SPOTIPY_CLIENT_ID"] = spotipy_client_id
                env_vars["SPOTIPY_CLIENT_SECRET"] = spotipy_client_secret

            # Lanzar hilo secundario
            job_thread = threading.Thread(
                target=run_download_job,
                args=(df_sorted, root_target_dir, engine_mode, env_vars),
                daemon=True
            )
            if add_script_run_ctx is not None:
                add_script_run_ctx(job_thread)

            job_thread.start()
            st.toast("🚀 Descarga iniciada en segundo plano. Puedes navegar libremente.", icon="🎵")
            st.rerun()

    # --- CONSOLA EN VIVO Y BARRA DE PROGRESO ---
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

        # Consola invertida: log_lines ya tiene los más nuevos al inicio (insert(0, ...))
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

        # Refrescar la UI mientras la descarga está en curso
        if dl_state["running"]:
            time.sleep(1)
            st.rerun()
        else:
            # Resultados finales
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
