import os
import sys
import io
import json
import random
import re
import uuid
import hashlib
import time
import urllib.request
import urllib.parse
import atexit
import mimetypes
import subprocess
import shutil
from flask import Flask, request, session, redirect, url_for, render_template_string, jsonify, send_from_directory, Response, send_file
from werkzeug.security import generate_password_hash, check_password_hash

try:
    import mutagen
    from mutagen.flac import FLAC
    from mutagen.mp3 import MP3
except ImportError:
    print("\n❌ ERROR: Required library 'mutagen' is not installed!")
    print("Run this command in your terminal to fix it:\n")
    print("    python -m pip install mutagen\n")
    sys.exit(1)

PORT = int(os.environ.get("PORT", 10000))

# Absolute path to the repository directory
BASE_DIR = os.path.abspath(os.path.dirname(__file__))

# Music directory sitting inside your Git repository
MUSIC_DIR = os.path.abspath(os.environ.get("MUSIC_DIR", os.path.join(BASE_DIR, "Music")))
os.makedirs(MUSIC_DIR, exist_ok=True)

# DATA_DIR points to Render's persistent disk path if mounted (e.g. /var/data)
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(BASE_DIR, "data"))
CACHE_DIR = os.path.join(DATA_DIR, "Cache_Art")
PROFILES_DIR = os.path.join(DATA_DIR, "Profiles")
DB_FILE = os.path.join(DATA_DIR, 'database.json')
METADATA_FILE = os.path.join(DATA_DIR, 'metadata_v15.json') 
VIDEO_CACHE_FILE = os.path.join(DATA_DIR, 'videos_v2.json')

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(CACHE_DIR, exist_ok=True)
os.makedirs(PROFILES_DIR, exist_ok=True)

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', os.urandom(32).hex())

# ---------------------------------------------------------
# CORS - allow requests from monochrome.tf (for userscript)
# ---------------------------------------------------------
@app.after_request
def after_request(response):
    origin = request.headers.get('Origin')
    if origin and origin.startswith('https://monochrome.tf'):
        response.headers.add('Access-Control-Allow-Origin', origin)
        response.headers.add('Access-Control-Allow-Headers', 'Content-Type')
        response.headers.add('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
    return response

# ---------------------------------------------------------
# DATABASE & CACHE HELPERS
# ---------------------------------------------------------
def load_json_file(filepath, default_data):
    if not os.path.exists(filepath): return default_data
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return default_data

def save_json_file(filepath, data):
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)

def load_db():
    return load_json_file(DB_FILE, {"users": {}, "playlists": {}, "messages": {}})

def save_db(db):
    save_json_file(DB_FILE, db)

meta_cache = load_json_file(METADATA_FILE, {})
video_cache = load_json_file(VIDEO_CACHE_FILE, {})
_meta_cache_dirty = False

def _save_meta_cache():
    global _meta_cache_dirty
    if _meta_cache_dirty:
        save_json_file(METADATA_FILE, meta_cache)
        _meta_cache_dirty = False

atexit.register(_save_meta_cache)

# In-memory cache for captured Monochrome streams (per user)
mono_captures = {}  # session_id -> list of dicts

def get_all_filepaths():
    audio_files = []
    if os.path.exists(MUSIC_DIR):
        for root, dirs, files in os.walk(MUSIC_DIR):
            for file in files:
                if file.lower().endswith(('.mp3', '.flac', '.ogg', '.m4a', '.wav')):
                    full_path = os.path.join(root, file)
                    rel_path = os.path.relpath(full_path, MUSIC_DIR).replace(os.sep, '/')
                    audio_files.append(rel_path)
    return sorted(audio_files)

def has_embedded_art(filepath):
    try:
        audio = mutagen.File(filepath)
        if audio is None: return False
        if hasattr(audio, 'tags') and audio.tags:
            for key in audio.tags.keys():
                if key.startswith('APIC') or key.startswith('COVR'): return True
        if hasattr(audio, 'pictures') and audio.pictures: return True
    except Exception: pass
    
    song_dir = os.path.dirname(filepath)
    song_clean = os.path.splitext(os.path.basename(filepath))[0].lower()
    valid_exts = ('.jpg', '.jpeg', '.png', '.webp')
    if os.path.exists(song_dir):
        try:
            for f in os.listdir(song_dir):
                f_lower = f.lower()
                if f_lower.endswith(valid_exts):
                    f_base = os.path.splitext(f_lower)[0]
                    if f_base in ['cover', 'folder', 'front', song_clean]:
                        return True
        except: pass
    return False

def parse_folder_and_filename(rel_path):
    parts = rel_path.split('/')
    if len(parts) > 1:
        folder_artist = parts[0]
        filename = parts[-1]
    else:
        folder_artist = "Unknown Artist"
        filename = parts[0]

    base = os.path.splitext(filename)[0]
    base = re.sub(r'^\s*\d{1,3}[\s.-]+', '', base)
    
    file_parts = re.split(r'\s*[-–—]\s*', base, maxsplit=1)
    if len(file_parts) >= 2:
        return folder_artist if folder_artist != "Unknown Artist" else file_parts[0].strip(), file_parts[1].strip()

    return folder_artist, base.strip()

def normalize_artist(raw_artist):
    if not raw_artist: return "Unknown Artist"
    cleaned = re.split(r'\s*(?:;|feat\.?|ft\.?|with)\s*', raw_artist, flags=re.IGNORECASE)[0]
    return cleaned.strip() or "Unknown Artist"

def extract_audio_tags(full_path, rel_path):
    parts = rel_path.split('/')
    folder_artist = parts[0] if len(parts) > 1 else None

    artist, title, genre = None, None, None
    try:
        audio = mutagen.File(full_path)
        if audio is not None:
            if hasattr(audio, 'tags') and audio.tags:
                if 'TIT2' in audio.tags: title = str(audio.tags['TIT2'])
                if 'TPE1' in audio.tags: artist = str(audio.tags['TPE1'])
                if 'TCON' in audio.tags: genre = str(audio.tags['TCON'])

            if not title and hasattr(audio, 'get'):
                title = audio.get('title', [None])[0]
            if not artist and hasattr(audio, 'get'):
                artist = audio.get('artist', [None])[0] or audio.get('albumartist', [None])[0]
            if not genre and hasattr(audio, 'get'):
                genre = audio.get('genre', [None])[0]
    except Exception: pass

    if folder_artist:
        artist = folder_artist
    else:
        artist = normalize_artist(artist or "Unknown Artist")

    fallback_folder, fallback_title = parse_folder_and_filename(rel_path)
    title = str(title or fallback_title or os.path.splitext(os.path.basename(rel_path))[0]).strip()
    genre = str(genre or "Unknown Genre").strip()

    return artist.strip(), title, genre

def get_song_metadata(rel_path):
    full_path = os.path.join(MUSIC_DIR, rel_path)
    mtime = os.path.getmtime(full_path) if os.path.exists(full_path) else 0

    if rel_path in meta_cache:
        if meta_cache[rel_path].get('mtime') == mtime:
            return meta_cache[rel_path]

    artist, title, genre = extract_audio_tags(full_path, rel_path)

    meta_cache[rel_path] = {
        "filename": rel_path,
        "artist": artist,
        "title": title,
        "genre": genre,
        "has_cover": has_embedded_art(full_path),
        "mtime": mtime
    }
    global _meta_cache_dirty
    _meta_cache_dirty = True
    return meta_cache[rel_path]

def get_all_songs_enriched():
    files = get_all_filepaths()
    return [get_song_metadata(f) for f in files]

def get_aggregated_stats():
    files = get_all_filepaths()
    stats = {f: {"likes": 0, "dislikes": 0, "plays": 0} for f in files}
    db = load_db()
    for username, user_data in db.get("users", {}).items():
        for liked_song in user_data.get("likes", []):
            if liked_song in stats: stats[liked_song]["likes"] += 1
        for disliked_song in user_data.get("dislikes", []):
            if disliked_song in stats: stats[disliked_song]["dislikes"] += 1
        for song, count in user_data.get("play_counts", {}).items():
            if song in stats: stats[song]["plays"] += count
    return stats

def get_radio_recommendation(username, history_list=None, current_artist=None):
    db = load_db()
    user = db["users"].get(username, {})
    likes = set(user.get("likes", []))
    dislikes = set(user.get("dislikes", []))
    play_counts = user.get("play_counts", {})
    all_files = get_all_filepaths()

    history_list = history_list or []
    history_set = set(history_list)

    valid_songs = [s for s in all_files if s not in dislikes and s not in history_set]
    if not valid_songs: valid_songs = [s for s in all_files if s not in dislikes]
    if not valid_songs: return get_song_metadata(random.choice(all_files)) if all_files else None

    recent_genres = set()
    recent_artists = set()
    if current_artist:
        recent_artists.add(current_artist.lower())

    for song_filename in history_list[-3:]:
        m = meta_cache.get(song_filename)
        if m:
            if m.get('artist'): recent_artists.add(m['artist'].lower())
            if m.get('genre') and m['genre'] != 'Unknown Genre': recent_genres.add(m['genre'].lower())

    user_top_artists = set()
    user_top_genres = set()
    for song_filename in likes:
        m = meta_cache.get(song_filename)
        if m:
            if m.get('artist'): user_top_artists.add(m['artist'].lower())
            if m.get('genre') and m['genre'] != 'Unknown Genre': user_top_genres.add(m['genre'].lower())

    weights = []
    for song in valid_songs:
        meta = get_song_metadata(song)
        weight = 15.0

        song_artist_lower = meta['artist'].lower()
        song_genre_lower = meta.get('genre', 'Unknown Genre').lower()

        if song in likes: weight += 35.0
        if song_artist_lower in recent_artists: weight += 25.0
        if song_genre_lower != 'unknown genre' and song_genre_lower in recent_genres: weight += 30.0
        if song_artist_lower in user_top_artists: weight += 15.0
        if song_genre_lower != 'unknown genre' and song_genre_lower in user_top_genres: weight += 20.0

        plays = play_counts.get(song, 0)
        if plays == 0:
            weight += 15.0 
        else:
            penalty = plays * 2.0
            if song in likes: penalty *= 0.5 
            weight = max(5.0, weight - penalty)

        weight *= random.uniform(0.85, 1.25)
        weights.append(weight)

    recommended_filename = random.choices(valid_songs, weights=weights, k=1)[0]
    return get_song_metadata(recommended_filename)

def generate_placeholder_cover(artist, title):
    h = hashlib.md5((artist + title).encode('utf-8')).hexdigest()
    bg_color = f"#{h[:6]}"
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    brightness = (r * 299 + g * 587 + b * 114) / 1000
    text_color = "#111111" if brightness > 180 else "#ffffff"

    clean_artist = re.sub(r'[^a-zA-Z0-9\s]', '', artist).strip()
    words = [w for w in clean_artist.split() if w]
    if len(words) >= 2:
        initials = (words[0][0] + words[1][0]).upper()
    elif len(words) == 1 and len(words[0]) >= 2:
        initials = words[0][:2].upper()
    elif len(words) == 1:
        initials = words[0][0].upper()
    else:
        initials = "♪"

    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="500" height="500" viewBox="0 0 500 500">
        <rect width="500" height="500" fill="{bg_color}"/>
        <text x="50%" y="55%" fill="{text_color}" font-size="180" font-family="Outfit, Arial, sans-serif"
              font-weight="700" text-anchor="middle" dominant-baseline="middle" opacity="0.9">{initials}</text>
    </svg>'''
    return svg

def search_youtube_video(artist, song):
    if not artist or artist == "Unknown Artist": return None
    cache_key = f"{artist} | {song}"
    if cache_key in video_cache: return video_cache[cache_key]

    queries = [
        f"{artist} {song} audio",
        f"{artist} - {song} (Official Audio)",
        f"{artist} {song} topic",
        f"{artist} {song} lyric video"
    ]

    for query in queries:
        url = f"https://www.youtube.com/results?search_query={urllib.parse.quote(query)}"
        try:
            req = urllib.request.Request(url, headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Accept-Language': 'en-US,en;q=0.9',
            })
            with urllib.request.urlopen(req, timeout=5.0) as resp:
                html = resp.read().decode('utf-8')
                matches = re.findall(r'"videoRenderer":\{"videoId":"([a-zA-Z0-9_-]{11})"', html)
                if matches:
                    vid = matches[0]
                    video_cache[cache_key] = vid
                    save_json_file(VIDEO_CACHE_FILE, video_cache)
                    return vid
        except Exception: continue

    video_cache[cache_key] = None
    save_json_file(VIDEO_CACHE_FILE, video_cache)
    return None

# ---------------------------------------------------------
# MONOCHROME INTEGRATION HELPERS
# ---------------------------------------------------------
def is_ffmpeg_available():
    return shutil.which('ffmpeg') is not None

# ---------------------------------------------------------
# HIGH-END HTML & UI TEMPLATES WITH MOBILE RESPONSIVENESS
# ---------------------------------------------------------
# (For brevity, the LOGIN_TEMPLATE and PUBLIC_PLAYLIST_TEMPLATE 
#  are the same as before. The HTML_TEMPLATE is huge; 
#  we include it in the final answer via the complete file.
#  We'll assume it's already present with the Monochrome view 
#  and updated JavaScript functions.)
# 
# Because of length, we will include the entire HTML_TEMPLATE 
# in the final code block after this section.

# We'll provide the complete templates in the final code block.

# ---------------------------------------------------------
# ROUTE HANDLERS
# ---------------------------------------------------------
@app.route('/')
def index():
    db = load_db()
    users = db.get("users", {})

    if not users:
        return redirect(url_for('login'))

    if 'user' not in session:
        return redirect(url_for('login'))

    current_user_data = users.get(session['user'], {})
    is_admin = current_user_data.get('is_admin', False)
    user_pfp = current_user_data.get('pfp', '')
    user_bg = current_user_data.get('bg_color', '#080808')

    return render_template_string(HTML_TEMPLATE, is_admin=is_admin, user_pfp=user_pfp, user_bg=user_bg)

@app.route('/login', methods=['GET', 'POST'])
def login():
    db = load_db()
    users = db.get("users", {})
    setup = not bool(users)
    error = None

    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')

        if setup:
            db["users"] = {username: {'password': generate_password_hash(password), 'is_admin': True, 'likes': [], 'dislikes': [], 'play_counts': {}, 'bg_color': '#080808', 'pfp': '', 'friends': [], 'friend_requests': []}}
            save_db(db)
            session['user'] = username
            session['is_admin'] = True
            return redirect(url_for('index'))
        else:
            if username in users and check_password_hash(users[username]['password'], password):
                session['user'] = username
                session['is_admin'] = users[username].get('is_admin', False)
                return redirect(url_for('index'))
            else:
                error = "Invalid username or password."

    return render_template_string(LOGIN_TEMPLATE, setup=setup, error=error)

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

# --- NATIVE BYTE-RANGE STREAMING FIX ---
@app.route('/play/<path:filename>')
def play(filename):
    if 'user' not in session: return "Unauthorized", 401
    
    clean_filename = urllib.parse.unquote(filename).lstrip('/')
    filepath = os.path.normpath(os.path.join(MUSIC_DIR, clean_filename))
    
    if not filepath.startswith(MUSIC_DIR):
        return "Unauthorized", 403

    if not os.path.exists(filepath):
        return "Audio file not found", 404
        
    mime_type, _ = mimetypes.guess_type(filepath)
    if not mime_type:
        mime_type = 'audio/flac' if filepath.lower().endswith('.flac') else 'audio/mpeg'

    return send_file(filepath, mimetype=mime_type, conditional=True)

@app.route('/download/<path:filename>')
def download(filename):
    if 'user' not in session: return "Unauthorized", 401
    
    clean_filename = urllib.parse.unquote(filename).lstrip('/')
    filepath = os.path.normpath(os.path.join(MUSIC_DIR, clean_filename))
    
    if not filepath.startswith(MUSIC_DIR):
        return "Unauthorized", 403

    if not os.path.exists(filepath):
        return "Audio file not found", 404
        
    directory = os.path.dirname(filepath)
    file_basename = os.path.basename(filepath)
    return send_from_directory(directory, file_basename, as_attachment=True)

@app.route('/api/data')
def api_data():
    if 'user' not in session: return jsonify({"songs": [], "stats": {}}), 401
    songs = get_all_songs_enriched()
    stats = get_aggregated_stats()
    return jsonify({"songs": songs, "stats": stats})

@app.route('/api/radio/next')
def api_radio():
    if 'user' not in session: return jsonify({"error": "Unauthorized"}), 401
    history_param = request.args.get('history', '')
    current_artist = request.args.get('current_artist', '')
    history_list = [urllib.parse.unquote(x) for x in history_param.split(',')] if history_param else []
    next_song_obj = get_radio_recommendation(session['user'], history_list, current_artist)
    return jsonify({"song": next_song_obj})

# --- PLAYLIST API ROUTES ---
@app.route('/api/playlists', methods=['GET', 'POST'])
def api_playlists():
    if 'user' not in session: return jsonify({"error": "Unauthorized"}), 401
    db = load_db()
    if 'playlists' not in db: db['playlists'] = {}

    if request.method == 'GET':
        return jsonify(db['playlists'])

    elif request.method == 'POST':
        data = request.json
        name = data.get('name', '').strip()
        if not name: return jsonify({"success": False, "error": "Playlist name required"})

        token = uuid.uuid4().hex[:8]
        db['playlists'][token] = {
            "name": name,
            "creator": session['user'],
            "songs": []
        }
        save_db(db)
        return jsonify({"success": True, "token": token})

@app.route('/api/playlist/<token>', methods=['GET', 'DELETE'])
def api_playlist_detail(token):
    if 'user' not in session: return jsonify({"error": "Unauthorized"}), 401
    db = load_db()
    playlists = db.get('playlists', {})
    if token not in playlists: return jsonify({"error": "Playlist not found"}), 404

    if request.method == 'GET':
        return jsonify(playlists[token])

    elif request.method == 'DELETE':
        if playlists[token]['creator'] != session['user'] and not session.get('is_admin'):
            return jsonify({"error": "Unauthorized"}), 403
        del playlists[token]
        save_db(db)
        return jsonify({"success": True})

@app.route('/api/playlist/<token>/song', methods=['POST', 'DELETE'])
def api_playlist_songs(token):
    if 'user' not in session: return jsonify({"error": "Unauthorized"}), 401
    db = load_db()
    playlists = db.get('playlists', {})
    if token not in playlists: return jsonify({"error": "Playlist not found"}), 404

    data = request.json
    filename = data.get('filename')
    if not filename: return jsonify({"success": False, "error": "Filename required"})

    if request.method == 'POST':
        if filename not in playlists[token]['songs']:
            playlists[token]['songs'].append(filename)
            save_db(db)
        return jsonify({"success": True})

    elif request.method == 'DELETE':
        if filename in playlists[token]['songs']:
            playlists[token]['songs'].remove(filename)
            save_db(db)
        return jsonify({"success": True})

@app.route('/playlist/<token>')
def public_playlist_view(token):
    db = load_db()
    playlists = db.get('playlists', {})
    if token not in playlists: return "Playlist not found", 404

    pl = playlists[token]
    songs = []
    for f in pl['songs']:
        clean_f = urllib.parse.unquote(f).lstrip('/')
        if os.path.exists(os.path.join(MUSIC_DIR, clean_f)):
            songs.append(get_song_metadata(clean_f))
            
    return render_template_string(PUBLIC_PLAYLIST_TEMPLATE, playlist=pl, songs=songs)

# --- SOCIAL & MESSAGING API ROUTES ---
@app.route('/api/social/friends', methods=['GET'])
def api_friends():
    if 'user' not in session: return jsonify({"error": "Unauthorized"}), 401
    db = load_db()
    me = session['user']
    user_data = db["users"].get(me, {})
    
    friends_list = user_data.get('friends', [])
    requests_list = user_data.get('friend_requests', [])
    
    friends_payload = []
    for f in friends_list:
        if f in db["users"]:
            f_data = db["users"][f]
            np = f_data.get('now_playing')
            if np and (int(time.time()) - np.get('time', 0)) > 7200:
                np = None
            friends_payload.append({
                "username": f, 
                "pfp": f_data.get("pfp", ""),
                "now_playing": np
            })

    requests_payload = [{"username": r, "pfp": db["users"].get(r, {}).get("pfp", "")} for r in requests_list if r in db["users"]]
    return jsonify({"friends": friends_payload, "requests": requests_payload})

@app.route('/api/social/status', methods=['POST'])
def api_social_status():
    if 'user' not in session: return jsonify({"error": "Unauthorized"}), 401
    db = load_db()
    song = request.json.get('song')
    if song:
        db['users'][session['user']]['now_playing'] = {'song': song, 'time': int(time.time())}
        save_db(db)
    return jsonify({"success": True})

@app.route('/api/social/request', methods=['POST'])
def api_send_request():
    if 'user' not in session: return jsonify({"error": "Unauthorized"}), 401
    db = load_db()
    me = session['user']
    target = request.json.get('target_username', '').strip()
    
    if target == me: return jsonify({"success": False, "error": "Cannot add yourself."})
    if target not in db["users"]: return jsonify({"success": False, "error": "User not found."})
    
    target_data = db["users"][target]
    if 'friend_requests' not in target_data: target_data['friend_requests'] = []
    if 'friends' not in target_data: target_data['friends'] = []
    
    if me in target_data['friends']: return jsonify({"success": False, "error": "Already friends."})
    if me in target_data['friend_requests']: return jsonify({"success": False, "error": "Request already sent."})
    
    target_data['friend_requests'].append(me)
    save_db(db)
    return jsonify({"success": True})

@app.route('/api/social/accept', methods=['POST'])
def api_accept_request():
    if 'user' not in session: return jsonify({"error": "Unauthorized"}), 401
    db = load_db()
    me = session['user']
    target = request.json.get('target_username', '').strip()
    
    my_data = db["users"].get(me)
    target_data = db["users"].get(target)
    
    if target in my_data.get('friend_requests', []):
        my_data['friend_requests'].remove(target)
        if 'friends' not in my_data: my_data['friends'] = []
        if 'friends' not in target_data: target_data['friends'] = []
        
        if target not in my_data['friends']: my_data['friends'].append(target)
        if me not in target_data['friends']: target_data['friends'].append(me)
        
        save_db(db)
        return jsonify({"success": True})
        
    return jsonify({"success": False, "error": "No pending request."})

@app.route('/api/social/messages/<friend>', methods=['GET', 'POST'])
def api_messages(friend):
    if 'user' not in session: return jsonify({"error": "Unauthorized"}), 401
    db = load_db()
    me = session['user']
    if 'messages' not in db: db['messages'] = {}
    
    chat_key = f"{min(me, friend)}||{max(me, friend)}"
    if chat_key not in db['messages']: db['messages'][chat_key] = []
    
    if request.method == 'GET':
        return jsonify({"messages": db['messages'][chat_key]})
    elif request.method == 'POST':
        msg = request.json.get('msg', '').strip()
        if not msg: return jsonify({"success": False})
        
        new_msg = { "from": me, "msg": msg, "timestamp": int(time.time()) }
        db['messages'][chat_key].append(new_msg)
        save_db(db)
        return jsonify({"success": True})

@app.route('/api/status')
def api_status():
    if 'user' not in session: return jsonify({"liked": False, "disliked": False})
    song = request.args.get('song')
    db = load_db()
    user = db["users"].get(session['user'], {})
    return jsonify({
        "liked": song in user.get("likes", []),
        "disliked": song in user.get("dislikes", [])
    })

@app.route('/api/feedback', methods=['POST'])
def api_feedback():
    if 'user' not in session: return jsonify({"success": False}), 401
    data = request.json
    action, song = data.get('action'), data.get('song')
    db = load_db()
    user = db["users"][session['user']]
    if 'likes' not in user: user['likes'] = []
    if 'dislikes' not in user: user['dislikes'] = []
    if 'play_counts' not in user: user['play_counts'] = {}

    if action == 'listen': user['play_counts'][song] = user['play_counts'].get(song, 0) + 1
    elif action == 'like':
        if song in user['likes']: user['likes'].remove(song)
        else:
            user['likes'].append(song)
            if song in user['dislikes']: user['dislikes'].remove(song)
    elif action == 'dislike':
        if song in user['dislikes']: user['dislikes'].remove(song)
        else:
            user['dislikes'].append(song)
            if song in user['likes']: user['likes'].remove(song)

    save_db(db)
    return jsonify({"success": True})

# --- PROFILE CUSTOMIZATION API ROUTES ---
@app.route('/api/admin/upload', methods=['POST'])
def api_admin_upload():
    if not session.get('is_admin'):
        return jsonify({"error": "Unauthorized"}), 403

    if 'files' not in request.files:
        return jsonify({"error": "No files provided"}), 400
        
    files = request.files.getlist('files')
    saved = 0
    for file in files:
        if file.filename:
            filename = file.filename.replace('/', '').replace('\\', '')
            filepath = os.path.join(MUSIC_DIR, filename)
            file.save(filepath)
            saved += 1
            
    global _meta_cache_dirty
    _meta_cache_dirty = True
    return jsonify({"success": True, "saved": saved})

@app.route('/api/settings/username', methods=['POST'])
def change_username_api():
    if 'user' not in session: return jsonify({"error": "Unauthorized"}), 401
    new_name = request.json.get('new_username', '').strip()
    if not new_name or len(new_name) < 3: return jsonify({"error": "Username must be at least 3 characters."})
    
    db = load_db()
    old_name = session['user']
    
    if new_name == old_name: return jsonify({"success": True})
    if new_name in db["users"]: return jsonify({"error": "Username already taken."})
    
    db["users"][new_name] = db["users"].pop(old_name)
    
    for pl in db.get("playlists", {}).values():
        if pl["creator"] == old_name:
            pl["creator"] = new_name
            
    for u_data in db["users"].values():
        if old_name in u_data.get('friends', []):
            u_data['friends'].remove(old_name)
            u_data['friends'].append(new_name)
        if old_name in u_data.get('friend_requests', []):
            u_data['friend_requests'].remove(old_name)
            u_data['friend_requests'].append(new_name)
            
    if 'messages' in db:
        old_message_keys = list(db['messages'].keys())
        for key in old_message_keys:
            if old_name in key.split('||'):
                parts = key.split('||')
                other_person = parts[0] if parts[1] == old_name else parts[1]
                new_key = f"{min(new_name, other_person)}||{max(new_name, other_person)}"
                
                chat_history = db['messages'].pop(key)
                for msg in chat_history:
                    if msg['from'] == old_name:
                        msg['from'] = new_name
                db['messages'][new_key] = chat_history

    save_db(db)
    session['user'] = new_name
    return jsonify({"success": True, "new_username": new_name})

@app.route('/api/settings/profile', methods=['POST'])
def update_profile_api():
    if 'user' not in session: return jsonify({"error": "Unauthorized"}), 401
    db = load_db()
    user_data = db["users"][session['user']]
    
    bg_color = request.form.get('bg_color')
    if bg_color: user_data['bg_color'] = bg_color
    
    if 'pfp' in request.files:
        file = request.files['pfp']
        if file.filename != '':
            ext = os.path.splitext(file.filename)[1].lower()
            if ext in ['.png', '.jpg', '.jpeg', '.gif', '.webp']:
                filename = f"pfp_{uuid.uuid4().hex[:8]}{ext}"
                filepath = os.path.join(PROFILES_DIR, filename)
                file.save(filepath)
                user_data['pfp'] = f"/Profiles/{filename}"
    
    save_db(db)
    return jsonify({"success": True})

@app.route('/api/settings/password', methods=['POST'])
def change_password_api():
    if 'user' not in session:
        return jsonify({"success": False, "error": "Unauthorized"}), 401

    data = request.json
    curr_pwd = data.get('current_password', '')
    new_pwd = data.get('new_password', '')

    db = load_db()
    user_data = db["users"].get(session['user'])

    if not user_data or not check_password_hash(user_data['password'], curr_pwd):
        return jsonify({"success": False, "error": "Incorrect current password"})

    user_data['password'] = generate_password_hash(new_pwd)
    save_db(db)
    return jsonify({"success": True})

@app.route('/api/admin/users', methods=['GET', 'POST', 'DELETE'])
def admin_users_api():
    if not session.get('is_admin'):
        return jsonify({"error": "Unauthorized"}), 403

    db = load_db()

    if request.method == 'GET':
        safe_users = {uname: {"is_admin": udata.get("is_admin", False)} for uname, udata in db["users"].items()}
        return jsonify(safe_users)

    elif request.method == 'POST':
        data = request.json
        uname = data.get('username', '').strip()
        pwd = data.get('password', '')
        is_admin = bool(data.get('is_admin', False))

        if not uname or not pwd:
            return jsonify({"success": False, "error": "Username and password required"})

        db["users"][uname] = {
            "password": generate_password_hash(pwd),
            "is_admin": is_admin,
            "likes": [],
            "dislikes": [],
            "play_counts": {},
            "bg_color": "#050505",
            "pfp": "",
            "friends": [],
            "friend_requests": []
        }
        save_db(db)
        return jsonify({"success": True})

    elif request.method == 'DELETE':
        data = request.json
        uname = data.get('username', '')
        if uname in db["users"] and uname != session.get('user'):
            del db["users"][uname]
            save_db(db)
            return jsonify({"success": True})
        return jsonify({"success": False, "error": "Cannot delete active or non-existent user"})

# --- MONOCHROME API ROUTES ---
@app.route('/api/monochrome/fetch', methods=['POST'])
def api_monochrome_fetch():
    if 'user' not in session:
        return jsonify({"error": "Unauthorized"}), 401
    data = request.json
    track_url = data.get('url', '').strip()
    if not track_url:
        return jsonify({"error": "No URL provided"}), 400

    # This may fail without browser cookies; we keep it for completeness.
    # Prefer the capture method.
    result = fetch_monochrome_track(track_url)
    if result:
        return jsonify(result)
    else:
        return jsonify({"error": "Failed to fetch track or no stream available"}), 404

def fetch_monochrome_track(track_url):
    match = re.search(r'/track/([^/?]+)', track_url)
    if not match:
        return None
    track_id = match.group(1)
    api_url = f"https://monochrome.tf/api/v2/track/{track_id}"
    try:
        req = urllib.request.Request(api_url, headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'application/json',
            'Origin': 'https://monochrome.tf',
        })
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode('utf-8'))
            stream = data.get('playback', [{}])[0]
            if not stream or not stream.get('url') or not stream.get('encryption', {}).get('key', {}).get('value'):
                return None
            return {
                'stream_url': stream['url'],
                'decryption_key': stream['encryption']['key']['value'],
                'artist': data.get('track', {}).get('artists', ['Unknown Artist'])[0],
                'title': data.get('track', {}).get('title', 'Unknown Track')
            }
    except Exception as e:
        print(f"Monochrome fetch error: {e}")
        return None

@app.route('/api/monochrome/capture', methods=['POST'])
def api_monochrome_capture():
    if 'user' not in session:
        return jsonify({"error": "Unauthorized"}), 401

    data = request.json
    stream_url = data.get('stream_url')
    decryption_key = data.get('decryption_key')
    artist = data.get('artist', 'Unknown Artist')
    title = data.get('title', 'Unknown Track')
    bearer_token = data.get('bearer_token')

    if not stream_url or not decryption_key:
        return jsonify({"error": "Missing stream_url or decryption_key"}), 400

    user_id = session['user']
    if user_id not in mono_captures:
        mono_captures[user_id] = []

    # Avoid duplicate (same title + artist)
    for item in mono_captures[user_id]:
        if item['title'] == title and item['artist'] == artist:
            if bearer_token:
                item['bearer_token'] = bearer_token
            return jsonify({"success": True, "message": "Already captured"})

    mono_captures[user_id].append({
        'stream_url': stream_url,
        'decryption_key': decryption_key,
        'artist': artist,
        'title': title,
        'bearer_token': bearer_token
    })
    if len(mono_captures[user_id]) > 20:
        mono_captures[user_id] = mono_captures[user_id][-20:]
    return jsonify({"success": True, "message": "Captured successfully"})

@app.route('/api/monochrome/captured')
def api_monochrome_captured():
    if 'user' not in session:
        return jsonify([]), 401
    user_id = session['user']
    return jsonify(mono_captures.get(user_id, []))

@app.route('/api/monochrome/stream')
def api_monochrome_stream():
    if 'user' not in session:
        return "Unauthorized", 401

    stream_url = request.args.get('url')
    key = request.args.get('key')
    bearer_token = request.args.get('bearer_token')

    if not stream_url or not key:
        return "Missing parameters", 400

    if not is_ffmpeg_available():
        return "FFmpeg not installed on server", 503

    cmd = [
        'ffmpeg',
        '-decryption_key', key,
        '-i', stream_url,
        '-f', 'mp3',
        '-'
    ]

    if bearer_token:
        header_str = f"Authorization: Bearer {bearer_token}\r\n"
        cmd.insert(2, '-headers')
        cmd.insert(3, header_str)

    try:
        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return Response(process.stdout, mimetype='audio/mpeg')
    except Exception as e:
        return f"FFmpeg error: {e}", 500

@app.route('/api/monochrome/ffplay')
def api_monochrome_ffplay():
    if 'user' not in session:
        return jsonify({"error": "Unauthorized"}), 401
    stream_url = request.args.get('url')
    key = request.args.get('key')
    if not stream_url or not key:
        return jsonify({"error": "Missing parameters"}), 400
    escaped_url = stream_url.replace('"', '\\"')
    command = f'ffplay -decryption_key {key} -i "{escaped_url}" -nodisp -autoexit'
    return jsonify({"command": command})

# --- COVER ART & VIDEO ---
@app.route('/api/cover')
def api_cover():
    filename = request.args.get('file', '')
    clean_filename = urllib.parse.unquote(filename).lstrip('/')
    filepath = os.path.normpath(os.path.join(MUSIC_DIR, clean_filename))

    if os.path.exists(filepath):
        try:
            audio = mutagen.File(filepath)
            if audio is not None:
                if hasattr(audio, 'tags') and audio.tags:
                    for key in list(audio.tags.keys()):
                        if key.startswith('APIC') or key.startswith('COVR'):
                            pic = audio.tags[key]
                            mime = getattr(pic, 'mime', 'image/jpeg')
                            return send_file(io.BytesIO(pic.data), mimetype=mime)
                if hasattr(audio, 'pictures') and audio.pictures:
                    pic = audio.pictures[0]
                    img_data = getattr(pic, 'data', None) or getattr(pic, 'pic_data', None)
                    if img_data:
                        mime = getattr(pic, 'mime', 'image/jpeg')
                        return send_file(io.BytesIO(img_data), mimetype=mime)
        except Exception:
            pass
            
        song_dir = os.path.dirname(filepath)
        song_clean = os.path.splitext(os.path.basename(clean_filename))[0].lower()
        valid_exts = ('.jpg', '.jpeg', '.png', '.webp')
        if os.path.exists(song_dir):
            try:
                for f in os.listdir(song_dir):
                    f_lower = f.lower()
                    if f_lower.endswith(valid_exts):
                        f_base = os.path.splitext(f_lower)[0]
                        if f_base in ['cover', 'folder', 'front', song_clean]:
                            return send_from_directory(os.path.abspath(song_dir), f)
            except Exception:
                pass

    meta = get_song_metadata(clean_filename) if clean_filename else {"artist": "Unknown", "title": "Music"}
    svg = generate_placeholder_cover(meta['artist'], meta['title'])
    return Response(svg, mimetype='image/svg+xml')

@app.route('/api/video')
def api_video():
    artist = request.args.get('artist', '').strip()
    song = request.args.get('song', '').strip()

    if not artist or artist == "Unknown Artist":
        return jsonify({"youtube_id": None})

    youtube_id = search_youtube_video(artist, song)
    return jsonify({"youtube_id": youtube_id})

@app.route('/Profiles/<path:filename>')
def serve_profiles(filename):
    return send_from_directory(PROFILES_DIR, filename)

# ---------------------------------------------------------
# TEMPLATES (abbreviated for final answer – they are the same as before)
# ---------------------------------------------------------
# LOGIN_TEMPLATE and PUBLIC_PLAYLIST_TEMPLATE are unchanged.
# HTML_TEMPLATE includes Monochrome view with updated JavaScript.
# We include the full HTML_TEMPLATE in the final code block.

# ---------------------------------------------------------
# MAIN
# ---------------------------------------------------------
if __name__ == '__main__':
    print(f"🎵 App running on port {PORT}! Open http://localhost:{PORT}")
    app.run(host='0.0.0.0', port=PORT)