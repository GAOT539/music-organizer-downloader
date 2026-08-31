import streamlit as st
import pandas as pd
import plotly.express as px
import subprocess
import os
import re
import io
import requests
import syncedlyrics
from PIL import Image
from mutagen.easyid3 import EasyID3
from mutagen.id3 import (
    ID3, ID3NoHeaderError,
    TIT2, TPE1, TALB, TDRC, TCON, TRCK,
    USLT, SYLT, APIC, COMM,
    Encoding
)

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
        # ID3 TCON acepta una sola cadena; usamos el primer género como principal
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
    # Intenta obtener la URL de imagen del CSV si existe
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

def process_dataframe(df):
    if 'Duration (ms)' in df.columns:
        df['Duration_Min'] = df['Duration (ms)'] / 60000
    if 'Release Date' in df.columns:
        df['Release Year'] = pd.to_datetime(df['Release Date'], errors='coerce').dt.year
    df['Clean_Primary_Artist'] = df['Artist Name(s)'].apply(get_primary_artist)
    # Nuevas columnas numéricas opcionales del CSV enriquecido
    for num_col in ['Popularity', 'Danceability', 'Energy', 'Valence', 'Tempo']:
        if num_col in df.columns:
            df[num_col] = pd.to_numeric(df[num_col], errors='coerce')
    if 'Explicit' in df.columns:
        df['Explicit'] = df['Explicit'].astype(str).str.strip().str.lower().map(
            lambda v: True if v in ['true', '1', 'yes', 'sí', 'si'] else False
        )
    return df

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
if 'active_view' not in st.session_state:
    st.session_state['active_view'] = "dashboard"

if st.sidebar.button("📊 Dashboard de Biblioteca", use_container_width=True):
    st.session_state['active_view'] = "dashboard"
if st.sidebar.button("📥 Descargar Músicas", use_container_width=True):
    st.session_state['active_view'] = "downloads"

menu_selection = st.session_state['active_view']

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
            # Expandir géneros separados por coma
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
        # Fallback: vista previa básica
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
        
        # Variables para las credenciales
        spotipy_client_id = ""
        spotipy_client_secret = ""
        
        # Condicional para mostrar campos solo si se necesita spotdl
        if "spotdl" in engine_mode:
            st.warning("Para usar spotdl necesitas tus credenciales de Spotify for Developers.")
            spotipy_client_id = st.text_input("Client ID", type="password")
            spotipy_client_secret = st.text_input("Client Secret", type="password")

    with col_conf2:
        st.write("📌 **Reglas de guardado:**")
        st.write(f"- Ruta: `./music_export/{sanitize_name(custom_root_folder)}/{{ArtistaPrincipal}}/`")
        st.write("- Archivo: `NombreCancion, Album, Artista.mp3`")
        start_download = st.button("🚀 Iniciar Descarga Unificada", type="primary", use_container_width=True)

    if start_download:
        # Validación de campos obligatorios si se eligió spotdl
        if "spotdl" in engine_mode and (not spotipy_client_id or not spotipy_client_secret):
            st.error("⚠️ Debes ingresar tu Client ID y Client Secret de Spotify para poder usar esta estrategia.")
        else:
            df_sorted = df.sort_values(by=['Clean_Primary_Artist', 'Track Name']).reset_index(drop=True)
            root_target_dir = os.path.join(BASE_OUTPUT_DIR, sanitize_name(custom_root_folder))
            os.makedirs(root_target_dir, exist_ok=True)

            st.markdown("#### 📟 Consola en Vivo")
            progress_bar = st.progress(0)
            status_metric = st.empty()
            terminal_placeholder = st.empty()

            log_lines = []

            def update_console(text):
                log_lines.append(text)
                # Construye el HTML de la consola con scrollbar y auto-scroll
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
                <script>
                    (function() {{
                        var el = document.getElementById('console-wrapper');
                        if (el) el.scrollTop = el.scrollHeight;
                    }})();
                </script>
                """
                terminal_placeholder.html(console_html)

            failed_songs = []
            total_tracks = len(df_sorted)
            success_count = 0

            # Inyectar credenciales dinámicas
            env_vars = os.environ.copy()
            if "spotdl" in engine_mode:
                env_vars["SPOTIPY_CLIENT_ID"] = spotipy_client_id
                env_vars["SPOTIPY_CLIENT_SECRET"] = spotipy_client_secret

            for idx, row in df_sorted.iterrows():
                track_name = sanitize_name(row.get('Track Name', 'Desconocido'))
                full_artist = sanitize_name(row.get('Artist Name(s)', 'Desconocido'))
                primary_artist = row.get('Clean_Primary_Artist', 'Varios Artistas')
                album_name = sanitize_name(row.get('Album Name', ''))
                track_uri = str(row.get('Track URI', ''))

                track_url = f"https://open.spotify.com/track/{track_uri.split(':')[-1]}" if track_uri.startswith('spotify:track:') else track_uri

                target_folder = os.path.join(root_target_dir, primary_artist)
                os.makedirs(target_folder, exist_ok=True)

                base_filename = format_track_filename(track_name, album_name, full_artist)
                final_mp3_path = os.path.join(target_folder, f"{base_filename}.mp3")

                update_console(f"\n[INFO] ({idx + 1}/{total_tracks}) {primary_artist} -> {base_filename}")
                status_metric.caption(f"Procesando ({idx + 1}/{total_tracks}): **{base_filename}**")

                if os.path.exists(final_mp3_path) and os.path.getsize(final_mp3_path) > 100000:
                    update_console(f"✔ Ya descargado: {base_filename}.mp3")
                    success_count += 1
                    progress_bar.progress((idx + 1) / total_tracks)
                    continue

                success = False
                reason = ""

                def run_proc(cmd):
                    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1, env=env_vars)
                    out = []
                    for line in proc.stdout:
                        l = line.strip()
                        if l:
                            update_console(f" > {l}")
                            out.append(l)
                    proc.wait()
                    return proc.returncode == 0, "\n".join(out)

                # 1. MOTOR yt-dlp
                if "yt-dlp" in engine_mode:
                    search_query = f"ytsearch1:{primary_artist} - {track_name} Audio"
                    temp_out = os.path.join(target_folder, f"{base_filename}.%(ext)s")
                    
                    cmd_ytdlp = [
                        "yt-dlp", search_query,
                        "-x", "--audio-format", "mp3", "--audio-quality", "0",
                        "--embed-thumbnail", "-o", temp_out, "--no-playlist"
                    ]
                    ok, out_log = run_proc(cmd_ytdlp)
                    
                    if os.path.exists(final_mp3_path):
                        # Metadata completa: título, artista, álbum, año, género,
                        # número de pista, carátula y audio-features
                        try:
                            embed_full_metadata(final_mp3_path, row, track_name, full_artist, album_name)
                            update_console(" ✔ Metadata ID3 incrustada")
                        except Exception as meta_err:
                            update_console(f" ⚠ Metadata parcial: {meta_err}")

                        # Letras sincronizadas (SYLT) y no sincronizadas (USLT)
                        try:
                            lyrics_text = syncedlyrics.search(f"{primary_artist} {track_name}")
                            if lyrics_text:
                                embed_lyrics_into_mp3(final_mp3_path, lyrics_text)
                                update_console(" ✔ Letras USLT/SYLT incrustadas")
                        except Exception:
                            pass
                        success = True
                    else:
                        reason = "yt-dlp falló"

                # 2. MOTOR spotdl
                if not success and "spotdl" in engine_mode:
                    update_console(" -> Probando spotdl...")
                    cmd_spotdl = [
                        "spotdl", "download", track_url,
                        "--output", os.path.join(target_folder, f"{base_filename}.{{output-ext}}"),
                        "--format", "mp3", "--bitrate", "320k"
                    ]
                    ok, out_log = run_proc(cmd_spotdl)
                    if os.path.exists(final_mp3_path):
                        success = True
                    else:
                        reason = "spotdl falló"

                lrc_loose = os.path.join(target_folder, f"{base_filename}.lrc")
                if os.path.exists(lrc_loose): os.remove(lrc_loose)

                if success:
                    success_count += 1
                    update_console(f"✔ EXITOSO: {base_filename}.mp3")
                else:
                    update_console(f"✖ ERROR: {base_filename}")
                    song_dict = row.to_dict()
                    song_dict['Error_Reason'] = reason
                    failed_songs.append(song_dict)

                progress_bar.progress((idx + 1) / total_tracks)

            update_console(f"\n[FIN] {success_count}/{total_tracks} canciones completadas.")

            if failed_songs:
                df_failed = pd.DataFrame(failed_songs)
                st.error(f"⚠️ {len(failed_songs)} errores detectados.")
                cols = [c for c in ['Track Name', 'Artist Name(s)', 'Album Name', 'Error_Reason'] if c in df_failed.columns]
                st.dataframe(df_failed[cols], use_container_width=True, hide_index=True)
                st.download_button("📥 Descargar CSV de Errores", data=df_failed.to_csv(index=False).encode('utf-8'), file_name="errores.csv", mime="text/csv")
            else:
                st.balloons()
                st.success("✨ ¡Descarga completada sin errores!")