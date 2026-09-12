import os
import pandas as pd
import time
import logging
import sqlite3
import csv
import re
from google_auth_oauthlib.flow import InstalledAppFlow
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from dotenv import load_dotenv

logging.basicConfig(
    filename='sync_historico.log',
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

SCOPES = ['https://www.googleapis.com/auth/youtube']

def get_youtube_service():
    load_dotenv()
    client_id = os.getenv('YOUTUBE_CLIENT_ID')
    client_secret = os.getenv('YOUTUBE_CLIENT_SECRET')

    if not client_id or not client_secret:
        raise ValueError("Faltan credenciales de YouTube en el .env")

    creds = None
    token_path = os.path.join('data', 'token.json')
    
    # Crear carpeta data si no existe
    os.makedirs('data', exist_ok=True)
    
    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            client_config = {
                "installed": {
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
                    "redirect_uris": ["http://localhost"]
                }
            }
            flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
            creds = flow.run_local_server(port=0)
            
        with open(token_path, 'w') as token_file:
            token_file.write(creds.to_json())

    return build('youtube', 'v3', credentials=creds)

def _get_or_create_playlist(youtube, db_conn):
    cursor = db_conn.cursor()
    cursor.execute("SELECT valor FROM Configuracion WHERE clave='youtube_mis_canciones_playlist_id'")
    row = cursor.fetchone()
    playlist_id = row[0] if row else None

    if playlist_id:
        try:
            response = youtube.playlists().list(part="id", id=playlist_id).execute()
            if not response.get('items'):
                playlist_id = None
                cursor.execute("DELETE FROM Configuracion WHERE clave='youtube_mis_canciones_playlist_id'")
                db_conn.commit()
            else:
                return playlist_id
        except HttpError as e:
            if e.resp.status == 404:
                playlist_id = None
                cursor.execute("DELETE FROM Configuracion WHERE clave='youtube_mis_canciones_playlist_id'")
                db_conn.commit()
            else:
                raise e

    if not playlist_id:
        request = youtube.playlists().insert(
            part="snippet,status",
            body={
                "snippet": {
                    "title": "Mis Canciones",
                    "description": "Playlist sincronizada desde Sello de Gato Music"
                },
                "status": {
                    "privacyStatus": "private"
                }
            }
        )
        response = request.execute()
        playlist_id = response['id']
        cursor.execute("INSERT OR REPLACE INTO Configuracion (clave, valor) VALUES (?, ?)", 
                       ('youtube_mis_canciones_playlist_id', playlist_id))
        db_conn.commit()
    
    return playlist_id

def _get_remote_items(youtube, playlist_id):
    items = {}
    next_page_token = None
    while True:
        request = youtube.playlistItems().list(
            part="snippet,contentDetails",
            playlistId=playlist_id,
            maxResults=50,
            pageToken=next_page_token
        )
        response = request.execute()
        
        for item in response['items']:
            video_id = item['contentDetails']['videoId']
            playlist_item_id = item['id']
            items[video_id] = playlist_item_id
            
        next_page_token = response.get('nextPageToken')
        if not next_page_token:
            break
    return items

def sync_mis_canciones(db_path, log_callback=None):
    if not log_callback:
        log_callback = print

    youtube = get_youtube_service()
    
    with sqlite3.connect(db_path) as db_conn:
        try:
            playlist_id = _get_or_create_playlist(youtube, db_conn)
            log_callback(f"Playlist ID: {playlist_id}")
            logging.info(f"Iniciando sincronización con playlist {playlist_id}")

            # Pausa para permitir que los servidores de YouTube sincronicen la nueva lista
            log_callback("Esperando 5 segundos a que YouTube propague la lista...")
            time.sleep(5)

            remote_items = _get_remote_items(youtube, playlist_id)
            
            cursor = db_conn.cursor()
            cursor.execute("SELECT id, nombre_archivo, youtube_video_id FROM Canciones WHERE youtube_video_id IS NOT NULL")
            local_songs = cursor.fetchall()

            local_video_ids = {row[2]: row for row in local_songs}

            # A agregar (están en local, pero no en remoto)
            a_agregar = [row for row in local_songs if row[2] not in remote_items]
            
            # A eliminar (están en remoto, pero no en local)
            a_eliminar = [(video_id, item_id) for video_id, item_id in remote_items.items() if video_id not in local_video_ids]

            log_callback(f"Se van a agregar {len(a_agregar)} videos y a eliminar {len(a_eliminar)} videos.")

            for db_id, nombre, video_id in a_agregar:
                try:
                    youtube.playlistItems().insert(
                        part="snippet",
                        body={
                            "snippet": {
                                "playlistId": playlist_id,
                                "resourceId": {
                                    "kind": "youtube#video",
                                    "videoId": video_id
                                }
                            }
                        }
                    ).execute()
                    msg = f"AGREGADA: {nombre} ({video_id})"
                    logging.info(msg)
                    log_callback(msg)
                    time.sleep(1)
                except HttpError as e:
                    if 'quotaExceeded' in str(e):
                        msg = "ERROR CRÍTICO: Cuota diaria de YouTube excedida (HTTP 403)."
                        logging.error(msg)
                        log_callback(msg)
                        return False
                    else:
                        msg = f"Error agregando {nombre} ({video_id}): {e}"
                        logging.error(msg)
                        log_callback(msg)

            for video_id, item_id in a_eliminar:
                try:
                    youtube.playlistItems().delete(id=item_id).execute()
                    msg = f"ELIMINADA: Video ID {video_id}"
                    logging.info(msg)
                    log_callback(msg)
                    time.sleep(1)
                except HttpError as e:
                    if 'quotaExceeded' in str(e):
                        msg = "ERROR CRÍTICO: Cuota diaria de YouTube excedida (HTTP 403)."
                        logging.error(msg)
                        log_callback(msg)
                        return False
                    else:
                        msg = f"Error eliminando Video ID {video_id}: {e}"
                        logging.error(msg)
                        log_callback(msg)

            log_callback("Sincronización completada.")
            return True

        except Exception as ex:
            msg = f"Error general en sincronización: {ex}"
            logging.error(msg)
            log_callback(msg)
            return False

def _get_next_playlist_name(youtube):
    playlists = []
    next_page_token = None
    while True:
        request = youtube.playlists().list(
            part="snippet",
            mine=True,
            maxResults=50,
            pageToken=next_page_token
        )
        response = request.execute()
        playlists.extend([item['snippet']['title'] for item in response['items']])
        next_page_token = response.get('nextPageToken')
        if not next_page_token:
            break

    max_n = 0
    pattern = re.compile(r'^Playlist(\d+)$')
    for title in playlists:
        match = pattern.match(title)
        if match:
            max_n = max(max_n, int(match.group(1)))
    
    return f"Playlist{max_n + 1:02d}"

def create_playlist_from_csv(csv_path, playlist_name=None, log_callback=None):
    if not log_callback:
        log_callback = print

    youtube = get_youtube_service()

    video_ids = []
    errores = []
    reintentos = []
    try:
        with open(csv_path, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            headers = reader.fieldnames or []
            
            is_spotify_mode = "Track Name" in headers and "Artist Name(s)" in headers
            
            if is_spotify_mode:
                log_callback("Modo Spotify detectado. Buscando videos en YouTube...")
                for row in reader:
                    track = row.get("Track Name", "").strip()
                    artist = row.get("Artist Name(s)", "").strip()
                    if track and artist:
                        q = f"{track} {artist}"
                        try:
                            search_response = youtube.search().list(
                                q=q, part="id", maxResults=1, type="video"
                            ).execute()
                            items = search_response.get("items", [])
                            if items:
                                vid = items[0]["id"]["videoId"]
                                video_ids.append({'id': vid, 'nombre': q})
                                log_callback(f"Encontrado: {vid} para '{q}'")
                            else:
                                errores.append({'cancion': q, 'motivo': 'Video no encontrado en búsqueda'})
                                log_callback(f"No se encontró video para '{q}'")
                            time.sleep(1)
                        except HttpError as e:
                            if 'quotaExceeded' in str(e):
                                log_callback("ERROR CRÍTICO: Cuota diaria excedida.")
                                return False, errores
                            errores.append({'cancion': q, 'motivo': f"Error buscando: {e}"})
                            log_callback(f"Error buscando '{q}': {e}")
                            time.sleep(1)
            else:
                log_callback("Modo convencional detectado. Leyendo primera columna como ID.")
                f.seek(0)
                plain_reader = csv.reader(f)
                for row in plain_reader:
                    if row:
                        vid = row[0].strip()
                        if vid:
                            video_ids.append({'id': vid, 'nombre': vid})
    except Exception as e:
        log_callback(f"Error leyendo CSV: {e}")
        return False, errores

    if not video_ids:
        log_callback("No se encontraron IDs en el archivo CSV.")
        return False, errores

    if not playlist_name:
        playlist_name = _get_next_playlist_name(youtube)
        log_callback(f"Nombre de playlist asignado automáticamente: {playlist_name}")

    try:
        request = youtube.playlists().insert(
            part="snippet,status",
            body={
                "snippet": {
                    "title": playlist_name,
                    "description": "Playlist generada vía CSV"
                },
                "status": {
                    "privacyStatus": "private"
                }
            }
        )
        response = request.execute()
        playlist_id = response['id']
        log_callback(f"Playlist creada: {playlist_name} (ID: {playlist_id})")

        for item in video_ids:
            try:
                youtube.playlistItems().insert(
                    part="snippet",
                    body={
                        "snippet": {
                            "playlistId": playlist_id,
                            "resourceId": {
                                "kind": "youtube#video",
                                "videoId": item['id']
                            }
                        }
                    }
                ).execute()
                log_callback(f"Video {item['id']} agregado.")
                time.sleep(1)
            except HttpError as e:
                if 'quotaExceeded' in str(e):
                    log_callback("ERROR CRÍTICO: Cuota diaria excedida.")
                    return False, errores
                reintentos.append(item)
                log_callback(f"Fallo temporal agregando video {item['id']}. Se reintentará. Error: {e}")

        if reintentos:
            log_callback(f"Iniciando segundo intento para {len(reintentos)} canciones. Pausando 3 segundos...")
            time.sleep(3)
            for item in reintentos:
                try:
                    youtube.playlistItems().insert(
                        part="snippet",
                        body={
                            "snippet": {
                                "playlistId": playlist_id,
                                "resourceId": {
                                    "kind": "youtube#video",
                                    "videoId": item['id']
                                }
                            }
                        }
                    ).execute()
                    log_callback(f"Video {item['id']} agregado en el segundo intento.")
                    time.sleep(1)
                except HttpError as e:
                    if 'quotaExceeded' in str(e):
                        log_callback("ERROR CRÍTICO: Cuota diaria excedida.")
                        return False, errores
                    errores.append({'cancion': item['nombre'], 'motivo': str(e)})
                    log_callback(f"Error definitivo agregando video {item['id']}: {e}")

        log_callback("Proceso CSV finalizado.")
        return True, errores

    except Exception as ex:
        log_callback(f"Error en creación de playlist: {ex}")
        return False, errores

def match_local_songs_to_youtube(db_path, log_callback=None):
    if not log_callback:
        log_callback = print

    youtube = get_youtube_service()
    
    with sqlite3.connect(db_path) as db_conn:
        cursor = db_conn.cursor()
        # Buscar canciones que no tengan ID
        cursor.execute("SELECT id, nombre_archivo FROM Canciones WHERE youtube_video_id IS NULL OR youtube_video_id = ''")
        canciones_huerfanas = cursor.fetchall()
        
        if not canciones_huerfanas:
            log_callback("Todas las canciones ya tienen un ID de YouTube asignado.")
            return True
            
        log_callback(f"Se encontraron {len(canciones_huerfanas)} canciones sin ID. Iniciando búsqueda en YouTube...")
        
        for db_id, nombre_archivo in canciones_huerfanas:
            import os
            # Limpiar el nombre para la búsqueda (quitar extensión)
            query = os.path.splitext(nombre_archivo)[0]
            
            try:
                request = youtube.search().list(
                    q=query,
                    part="id",
                    maxResults=1,
                    type="video"
                )
                response = request.execute()
                
                if response['items']:
                    video_id = response['items'][0]['id']['videoId']
                    cursor.execute("UPDATE Canciones SET youtube_video_id = ? WHERE id = ?", (video_id, db_id))
                    db_conn.commit()
                    log_callback(f"Emparejado: {query} -> {video_id}")
                else:
                    log_callback(f"No se encontró resultado para: {query}")
                    
                time.sleep(1) # Protección estricta de cuota
            except HttpError as e:
                if 'quotaExceeded' in str(e):
                    log_callback("ERROR CRÍTICO: Cuota diaria de YouTube excedida (HTTP 403).")
                    return False
                log_callback(f"Error buscando {query}: {e}")
                
    log_callback("Proceso de emparejamiento finalizado.")
    return True

def ingestar_csv_a_cola(csv_path, playlist_id, db_path):
    """
    Fase 2: Lee el CSV de Spotify y puebla la tabla Pendientes_YouTube evitando duplicados.
    """
    df = pd.read_csv(csv_path)
    
    # Validamos que sea el formato esperado (Spotify)
    if 'Track Name' in df.columns and 'Artist Name(s)' in df.columns:
        # Concatenar y limpiar
        df['cancion'] = df['Track Name'].astype(str).str.strip() + ' - ' + df['Artist Name(s)'].astype(str).str.strip()
        df = df.drop_duplicates(subset=['cancion'])
        
        with sqlite3.connect(db_path) as conn:
            cursor = conn.cursor()
            for cancion in df['cancion'].tolist():
                if cancion.strip():
                    cursor.execute(
                        "INSERT OR IGNORE INTO Pendientes_YouTube (playlist_id, cancion) VALUES (?, ?)", 
                        (playlist_id, cancion)
                    )
            conn.commit()
        return True
    return False

def procesar_cola_youtube(playlist_id, db_path, log_callback):
    """
    Fase 2: Procesa la tabla Pendientes_YouTube, respeta cuotas y actualiza estados de error.
    """
    youtube = get_youtube_service()
    
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, cancion, intentos FROM Pendientes_YouTube WHERE playlist_id = ?", (playlist_id,))
        pendientes = cursor.fetchall()
        
        for id_bd, cancion, intentos in pendientes:
            try:
                # 1. Buscar en YouTube
                search_response = youtube.search().list(
                    q=cancion, part="id", maxResults=1, type="video"
                ).execute()
                
                items = search_response.get("items", [])
                if items:
                    video_id = items[0]["id"]["videoId"]
                    
                    # 2. Insertar en la playlist
                    youtube.playlistItems().insert(
                        part="snippet",
                        body={
                            "snippet": {
                                "playlistId": playlist_id,
                                "resourceId": {
                                    "kind": "youtube#video",
                                    "videoId": video_id
                                }
                            }
                        }
                    ).execute()
                    
                    # Éxito: Eliminar de pendientes
                    cursor.execute("DELETE FROM Pendientes_YouTube WHERE id = ?", (id_bd,))
                    conn.commit()
                    log_callback(f"✔ AGREGADA: {cancion}")
                else:
                    # No encontrado
                    cursor.execute("UPDATE Pendientes_YouTube SET intentos = intentos + 1, ultimo_error = ? WHERE id = ?", ("Video no encontrado", id_bd))
                    conn.commit()
                    log_callback(f"⚠ NO ENCONTRADA: {cancion}")
                
                time.sleep(2)  # Pausa requerida entre iteraciones
                
            except HttpError as e:
                # Manejar error HTTP
                error_msg = str(e)
                cursor.execute("UPDATE Pendientes_YouTube SET intentos = intentos + 1, ultimo_error = ? WHERE id = ?", (error_msg, id_bd))
                conn.commit()
                
                # CRÍTICO: Control de Cuota
                if 'quotaExceeded' in error_msg or 'rateLimitExceeded' in error_msg:
                    log_callback("❌ ERROR CRÍTICO: Cuota diaria excedida (HTTP 403) o Límite de Peticiones. Deteniendo ejecución.")
                    return False
                else:
                    log_callback(f"✖ ERROR en {cancion}: {error_msg}")
                    time.sleep(2)
                    
        return True
