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

# DATA_DIR points to persistent storage
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(BASE_DIR, "data"))
CACHE_DIR = os.path.join(DATA_DIR, "Cache_Art")
PROFILES_DIR = os.path.join(DATA_DIR, "Profiles")
DB_FILE = os.path.join(DATA_DIR, 'database.json')
METADATA_FILE = os.path.join(DATA_DIR, 'metadata_v15.json') # Bumped to v15 for Genre extraction
VIDEO_CACHE_FILE = os.path.join(DATA_DIR, 'videos_v2.json')

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(CACHE_DIR, exist_ok=True)
os.makedirs(PROFILES_DIR, exist_ok=True)

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', os.urandom(32).hex())

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

def get_all_filepaths():
    """Scans the ./Music folder for any audio files inside artist subfolders."""
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

# --- PERSONALIZED INFINITE RADIO ALGORITHM ---
def get_radio_recommendation(username, history_list=None, current_artist=None):
    db = load_db()
    user = db["users"].get(username, {})
    likes = set(user.get("likes", []))
    dislikes = set(user.get("dislikes", []))
    play_counts = user.get("play_counts", {})
    all_files = get_all_filepaths()

    history_list = history_list or []
    history_set = set(history_list)

    # 1. Filter out disliked tracks and tracks played recently in this session
    valid_songs = [s for s in all_files if s not in dislikes and s not in history_set]
    if not valid_songs: valid_songs = [s for s in all_files if s not in dislikes]
    if not valid_songs: return get_song_metadata(random.choice(all_files)) if all_files else None

    # 2. Extract recent vibe profile (Last 3 played tracks in session)
    recent_genres = set()
    recent_artists = set()
    if current_artist:
        recent_artists.add(current_artist.lower())

    for song_filename in history_list[-3:]:
        m = meta_cache.get(song_filename)
        if m:
            if m.get('artist'): recent_artists.add(m['artist'].lower())
            if m.get('genre') and m['genre'] != 'Unknown Genre': recent_genres.add(m['genre'].lower())

    # 3. Build overall user preference profile
    user_top_artists = set()
    user_top_genres = set()
    for song_filename in likes:
        m = meta_cache.get(song_filename)
        if m:
            if m.get('artist'): user_top_artists.add(m['artist'].lower())
            if m.get('genre') and m['genre'] != 'Unknown Genre': user_top_genres.add(m['genre'].lower())

    # 4. Score candidates
    weights = []
    for song in valid_songs:
        meta = get_song_metadata(song)
        weight = 15.0 # Base weight

        song_artist_lower = meta['artist'].lower()
        song_genre_lower = meta.get('genre', 'Unknown Genre').lower()

        # Boost if explicit Like
        if song in likes: 
            weight += 35.0

        # Boost for Vibe Flow (Genre/Artist matches recent history)
        if song_artist_lower in recent_artists:
            weight += 25.0
        if song_genre_lower != 'unknown genre' and song_genre_lower in recent_genres:
            weight += 30.0

        # Boost for User Favorites (Overall profile)
        if song_artist_lower in user_top_artists:
            weight += 15.0
        if song_genre_lower != 'unknown genre' and song_genre_lower in user_top_genres:
            weight += 20.0

        # Discovery / Unplayed Boost & Overplay Penalty
        plays = play_counts.get(song, 0)
        if plays == 0:
            weight += 15.0 
        else:
            penalty = plays * 2.0
            if song in likes: penalty *= 0.5 
            weight = max(5.0, weight - penalty)

        # Random entropy for fresh variation
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
# HIGH-END HTML & UI TEMPLATES WITH MOBILE RESPONSIVENESS
# ---------------------------------------------------------
LOGIN_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Streamer Pro - Login</title>
    <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;500;600;700;800&display=swap" rel="stylesheet">
    <style>
        :root { --accent: #1DB954; }
        @keyframes gradientBG {
            0% { background-position: 0% 50%; }
            50% { background-position: 100% 50%; }
            100% { background-position: 0% 50%; }
        }
        body { 
            font-family: 'Outfit', sans-serif; 
            margin: 0; 
            height: 100vh; 
            display: flex; 
            align-items: center; 
            justify-content: center; 
            background: linear-gradient(-45deg, #050505, #1a1a2e, #0a1913, #050505);
            background-size: 400% 400%;
            animation: gradientBG 15s ease infinite;
            color: white; 
            padding: 20px;
            box-sizing: border-box;
        }
        .auth-card { 
            background: rgba(20, 20, 20, 0.4); 
            backdrop-filter: blur(24px); 
            -webkit-backdrop-filter: blur(24px);
            padding: 45px 40px; 
            border-radius: 20px; 
            text-align: center; 
            width: 100%;
            max-width: 360px; 
            border: 1px solid rgba(255,255,255,0.05); 
            box-shadow: 0 25px 50px rgba(0,0,0,0.6); 
        }
        h2 { margin-top:0; font-weight: 700; font-size: 28px; letter-spacing: -0.5px; }
        .input-group { text-align: left; margin-bottom: 16px; }
        .input-label { font-size: 13px; color: #a7a7a7; margin-bottom: 6px; font-weight: 500; text-transform: uppercase; letter-spacing: 1px; }
        input { 
            width: 100%; padding: 14px 16px; border-radius: 10px; border: 1px solid rgba(255,255,255,0.1); 
            background: rgba(0,0,0,0.3); color: white; font-size: 15px; outline: none; 
            box-sizing: border-box; transition: all 0.3s ease; font-family: 'Outfit', sans-serif;
        }
        input:focus { border-color: var(--accent); background: rgba(0,0,0,0.5); box-shadow: 0 0 15px rgba(29, 185, 84, 0.15); }
        button { 
            background: var(--accent); color: black; border: none; padding: 14px 24px; border-radius: 30px; 
            font-weight: 700; font-size: 16px; cursor: pointer; width: 100%; margin-top: 15px; 
            transition: all 0.3s cubic-bezier(0.25, 0.8, 0.25, 1); font-family: 'Outfit', sans-serif;
        }
        button:hover { background: #1ed760; transform: translateY(-3px); box-shadow: 0 10px 20px rgba(29, 185, 84, 0.3); }
    </style>
</head>
<body>
    <div class="auth-card">
        <h2>{{ 'Setup Master Admin' if setup else 'Welcome Back' }}</h2>
        {% if error %}
            <p style="color:#ff5555; font-size:13px; font-weight:600; background: rgba(255,85,85,0.1); padding: 10px; border-radius: 8px;">{{ error }}</p>
        {% endif %}
        <form method="POST">
            <div class="input-group">
                <div class="input-label">Username</div>
                <input type="text" name="username" required>
            </div>
            <div class="input-group">
                <div class="input-label">Password</div>
                <input type="password" name="password" required>
            </div>
            <button type="submit">{{ 'Create Account' if setup else 'Log In' }}</button>
        </form>
    </div>
</body>
</html>
"""

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>Streamer Pro</title>
    <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css" rel="stylesheet">
    <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;500;600;700;800&display=swap" rel="stylesheet">
    <style>
        :root { --bg: {{ user_bg | default('#080808') }}; --panel: rgba(18, 18, 18, 0.4); --highlight: rgba(255,255,255,0.1); --text: #ffffff; --subtext: #a7a7a7; --accent: #1DB954; --card-bg: rgba(24, 24, 24, 0.6); }
        * { box-sizing: border-box; }
        body { font-family: 'Outfit', system-ui, sans-serif; background: radial-gradient(circle at top left, #1f1f2e 0%, var(--bg) 100%); color: var(--text); margin: 0; overflow: hidden; display: flex; height: 100vh; }
        
        .sidebar { width: 250px; background: rgba(0,0,0,0.2); backdrop-filter: blur(20px); -webkit-backdrop-filter: blur(20px); padding: 24px 16px; display: flex; flex-direction: column; gap: 8px; flex-shrink: 0; z-index: 10; border-right: 1px solid rgba(255,255,255,0.05); }
        .logo { font-size: 22px; font-weight: 800; padding: 0 12px; margin-bottom: 20px; display: flex; align-items: center; gap: 12px; letter-spacing: -0.5px; }
        .nav-section-title { font-size: 11px; text-transform: uppercase; color: var(--subtext); letter-spacing: 1.5px; padding: 12px 12px 4px 12px; font-weight: 700; }
        .nav-item { padding: 12px 14px; border-radius: 8px; cursor: pointer; color: var(--subtext); font-weight: 600; font-size: 15px; display: flex; align-items: center; gap: 16px; transition: all 0.3s cubic-bezier(0.25, 0.8, 0.25, 1); }
        .nav-item:hover, .nav-item.active { color: var(--text); background: var(--highlight); transform: translateX(4px); }
        .nav-item i { font-size: 18px; width: 22px; text-align: center; }
        
        .center-wrapper { flex: 1; display: flex; flex-direction: column; overflow: hidden; position: relative; margin-bottom: 96px; }
        .top-bar { height: 72px; background: rgba(10, 10, 10, 0.5); backdrop-filter: blur(24px); -webkit-backdrop-filter: blur(24px); display: flex; align-items: center; justify-content: space-between; padding: 0 32px; position: absolute; top: 0; left: 0; right: 0; z-index: 50; border-bottom: 1px solid rgba(255,255,255,0.05); }
        .main-content { flex: 1; padding: 100px 32px 32px 32px; overflow-y: auto; scroll-behavior: smooth; }
        @keyframes fadeIn { from { opacity: 0; transform: translateY(15px); } to { opacity: 1; transform: translateY(0); } }
        .fade-in { animation: fadeIn 0.5s cubic-bezier(0.16, 1, 0.3, 1) forwards; }
        
        .right-panel { width: 360px; background: var(--panel); backdrop-filter: blur(24px); -webkit-backdrop-filter: blur(24px); border-radius: 16px; margin: 16px 16px 112px 0; padding: 24px; display: none; flex-direction: column; text-align: center; overflow: hidden; transition: 0.3s; flex-shrink: 0; border: 1px solid rgba(255,255,255,0.05); box-shadow: -10px 0 30px rgba(0,0,0,0.5); }
        .rp-header-row { display: flex; justify-content: space-between; width: 100%; align-items: center; margin-bottom: 20px;}
        .rp-tabs { display: flex; gap: 8px; background: rgba(0,0,0,0.3); padding: 5px; border-radius: 20px; }
        .rp-tab { padding: 6px 14px; font-size: 12px; font-weight: 700; border-radius: 16px; cursor: pointer; color: var(--subtext); transition: 0.3s; }
        .rp-tab.active { background: rgba(255,255,255,0.15); color: var(--text); box-shadow: 0 4px 10px rgba(0,0,0,0.2); }
        
        .rp-media-container { position: relative; width: 240px; height: 240px; margin: 0 auto 30px auto; }
        .rp-cover-glow { position: absolute; top: 10%; left: 10%; width: 80%; height: 80%; filter: blur(40px); opacity: 0.7; z-index: 1; transition: background-image 0.5s ease; background-size: cover; background-position: center; border-radius: 50%; }
        #rp-cover { position: absolute; z-index: 2; top:0; left:0; width: 100%; height: 100%; border-radius: 12px; box-shadow: 0 15px 35px rgba(0,0,0,0.6); object-fit: cover; transition: opacity 0.5s ease; }
        #rp-video-container { position: absolute; top:0; left:0; width:100%; height:100%; border-radius: 12px; overflow: hidden; z-index: 3; opacity: 0; transition: opacity 0.5s; pointer-events: none; box-shadow: 0 15px 35px rgba(0,0,0,0.6); }
        #rp-video { position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%); width: 600px; height: 337px; }

        #visualizer { width: 100%; height: 60px; display: block; filter: drop-shadow(0px 0px 10px rgba(29, 185, 84, 0.4)); margin-top: 10px; }

        .queue-container { flex: 1; overflow-y: auto; text-align: left; padding-top: 10px; display: none; width: 100%; }
        .queue-item { display: flex; align-items: center; gap: 14px; padding: 12px; border-radius: 8px; cursor: pointer; transition: all 0.2s cubic-bezier(0.25, 0.8, 0.25, 1); margin-bottom: 6px; }
        .queue-item:hover { background: rgba(255,255,255,0.08); transform: translateX(4px); }
        .queue-item img { width: 44px; height: 44px; border-radius: 6px; object-fit: cover; box-shadow: 0 4px 10px rgba(0,0,0,0.3); }
        .queue-item-info { display: flex; flex-direction: column; overflow: hidden; flex: 1; gap: 2px; }

        .player-bar { position: fixed; bottom: 0; left: 0; right: 0; height: 96px; background: rgba(10, 10, 10, 0.75); backdrop-filter: blur(30px); -webkit-backdrop-filter: blur(30px); border-top: 1px solid rgba(255,255,255,0.05); display: flex; align-items: center; padding: 0 32px; justify-content: space-between; z-index: 1000; }
        .search-container { display: flex; align-items: center; background: rgba(255,255,255,0.05); border-radius: 30px; padding: 10px 20px; width: 340px; border: 1px solid rgba(255,255,255,0.05); transition: 0.3s; }
        .search-container:focus-within { border-color: var(--accent); background: rgba(255,255,255,0.1); box-shadow: 0 0 15px rgba(29, 185, 84, 0.1); }
        .search-container i { color: var(--subtext); font-size: 16px; margin-right: 12px; }
        .search-container input { background: transparent; border: none; color: white; width: 100%; outline: none; font-size: 15px; font-family: 'Outfit', sans-serif;}
        
        .user-badge-wrapper { position: relative; display: inline-block; }
        .user-badge { display: flex; align-items: center; gap: 12px; font-size: 14px; font-weight: 700; background: rgba(0,0,0,0.3); padding: 8px 16px; border-radius: 30px; border: 1px solid rgba(255,255,255,0.05); cursor: pointer; transition: 0.3s cubic-bezier(0.25, 0.8, 0.25, 1); }
        .user-badge:hover { background: rgba(255,255,255,0.1); transform: translateY(-2px); }
        .settings-dropdown { display: none; position: absolute; right: 0; top: 55px; background: rgba(30,30,30,0.95); backdrop-filter: blur(20px); border: 1px solid rgba(255,255,255,0.1); border-radius: 12px; width: 220px; box-shadow: 0 15px 35px rgba(0,0,0,0.8); z-index: 100; overflow: hidden; padding: 8px 0; }
        .settings-dropdown.show { display: block; animation: fadeIn 0.2s ease; }
        .dropdown-item { padding: 12px 20px; font-size: 14px; font-weight: 600; color: var(--subtext); display: flex; align-items: center; gap: 14px; cursor: pointer; text-decoration: none; transition: 0.2s; }
        .dropdown-item:hover { background: rgba(255,255,255,0.05); color: var(--text); }
        .dropdown-divider { height: 1px; background: rgba(255,255,255,0.05); margin: 6px 0; }
        
        h2 { font-size: 32px; font-weight: 800; margin-top: 0; margin-bottom: 28px; letter-spacing: -1px; }
        .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 24px; margin-bottom: 48px; }
        .scroll-row { display: flex; gap: 24px; overflow-x: auto; padding-bottom: 20px; margin-bottom: 40px; scroll-snap-type: x mandatory; }
        
        /* Fixed widths for scroll row cards */
        .scroll-row .card { width: 200px; min-width: 200px; max-width: 200px; flex-shrink: 0; scroll-snap-align: start; display: flex; flex-direction: column; }
        
        .card { background: var(--card-bg); backdrop-filter: blur(10px); padding: 18px; border-radius: 12px; cursor: pointer; transition: all 0.4s cubic-bezier(0.16, 1, 0.3, 1); position: relative; text-align: left; border: 1px solid rgba(255,255,255,0.03); display: flex; flex-direction: column; }
        .card:hover { background: rgba(40,40,40,0.8); transform: translateY(-8px); box-shadow: 0 20px 40px rgba(0,0,0,0.5); border-color: rgba(255,255,255,0.1); }
        
        /* Force perfect square images */
        .card-img-container { width: 100%; aspect-ratio: 1 / 1; background: #222; border-radius: 8px; margin-bottom: 16px; position: relative; overflow: hidden; display: flex; align-items: center; justify-content: center; font-size: 40px; color: #444; box-shadow: 0 8px 20px rgba(0,0,0,0.4); flex-shrink: 0; }
        .card-img-container img { width: 100%; height: 100%; object-fit: cover; position: absolute; top: 0; left: 0; transition: transform 0.5s ease; }
        
        .card:hover .card-img-container img { transform: scale(1.05); }
        .card-play-overlay { position: absolute; bottom: 12px; right: 12px; background: var(--accent); color: #000; width: 48px; height: 48px; border-radius: 50%; display: flex; align-items: center; justify-content: center; opacity: 0; transform: translateY(15px); transition: all 0.4s cubic-bezier(0.175, 0.885, 0.32, 1.275); box-shadow: 0 10px 20px rgba(0,0,0,0.6); z-index: 2;}
        .card:hover .card-play-overlay { opacity: 1; transform: translateY(0); }
        .card-play-overlay:hover { transform: scale(1.15) !important; background: #1ed760; }
        .card-info { display: flex; flex-direction: column; gap: 6px; width: 100%; }
        .card-title { font-weight: 800; font-size: 16px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; letter-spacing: -0.2px; }
        .card-bottom-row { display: flex; justify-content: space-between; align-items: center; margin-top: 2px; }
        .card-artist { font-weight: 500; color: var(--subtext); font-size: 14px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; flex: 1; padding-right: 8px; }
        
        .now-playing-info { width: 30%; display: flex; align-items: center; gap: 16px; }
        .np-cover { width: 64px; height: 64px; background: #222; border-radius: 8px; object-fit: cover; box-shadow: 0 8px 20px rgba(0,0,0,0.5); cursor: pointer; transition: transform 0.3s ease; }
        .np-cover:hover { transform: scale(1.05); }
        .np-text { display: flex; flex-direction: column; overflow: hidden; gap: 4px; }
        .np-title { font-weight: 800; font-size: 15px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; letter-spacing: -0.2px;}
        .np-artist { color: var(--subtext); font-size: 13px; font-weight: 500; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        
        .controls { width: 40%; display: flex; flex-direction: column; align-items: center; gap: 8px; }
        .buttons { display: flex; gap: 24px; align-items: center; }
        .volume-controls { display: flex; align-items: center; gap: 16px; width: 30%; justify-content: flex-end; color: var(--subtext); }
        
        @keyframes pop { 0% { transform: scale(1); } 50% { transform: scale(1.35); } 100% { transform: scale(1); } }
        .pop-anim { animation: pop 0.35s cubic-bezier(0.175, 0.885, 0.32, 1.275); }
        .btn { background: none; border: none; color: var(--subtext); font-size: 18px; cursor: pointer; transition: all 0.2s cubic-bezier(0.25, 0.8, 0.25, 1); outline: none; }
        .btn:hover { color: var(--text); transform: scale(1.1); }
        .btn.active#like-btn i, .btn.active.shuffle i, .btn.active.repeat i, .btn.active#sleep-btn i { color: var(--accent) !important; text-shadow: 0 0 10px rgba(29, 185, 84, 0.5); }
        .btn.active#dislike-btn i { color: #ff5555 !important; text-shadow: 0 0 10px rgba(255, 85, 85, 0.5); }
        .btn.play-btn { background: var(--text); color: black; height: 38px; width: 38px; border-radius: 50%; display: flex; align-items: center; justify-content: center; transition: all 0.3s cubic-bezier(0.25, 0.8, 0.25, 1); box-shadow: 0 4px 10px rgba(0,0,0,0.3); }
        .btn.play-btn:hover { transform: scale(1.1); background: var(--accent); }
        
        audio { display: none; }
        input[type=range] { -webkit-appearance: none; background: rgba(255,255,255,0.2); height: 6px; border-radius: 3px; outline: none; cursor: pointer; width: 100%; transition: 0.2s; }
        input[type=range]:hover { height: 8px; background: rgba(255,255,255,0.3); }
        input[type=range]::-webkit-slider-thumb { -webkit-appearance: none; width: 14px; height: 14px; border-radius: 50%; background: #fff; opacity: 0; transition: 0.2s cubic-bezier(0.25, 0.8, 0.25, 1); box-shadow: 0 2px 5px rgba(0,0,0,0.5); }
        input[type=range]:hover::-webkit-slider-thumb { opacity: 1; transform: scale(1.2); }
        
        .lyrics-container { position: relative; width: 100%; flex: 1; overflow-y: auto; text-align: left; padding-top: 20px; padding-bottom: 80px; scroll-behavior: smooth; mask-image: linear-gradient(to bottom, transparent 0%, black 15%, black 85%, transparent 100%); -webkit-mask-image: linear-gradient(to bottom, transparent 0%, black 15%, black 85%, transparent 100%); }
        .lyric-line { font-size: 18px; color: rgba(255, 255, 255, 0.35); padding: 10px 14px; margin: 6px 0; border-radius: 8px; transition: all 0.4s cubic-bezier(0.175, 0.885, 0.32, 1.275); font-weight: 700; cursor: pointer; transform-origin: left center; }
        .lyric-line:hover { color: rgba(255, 255, 255, 0.9); background: rgba(255,255,255,0.05); }
        .lyric-line.active { color: var(--accent); font-size: 24px; font-weight: 800; text-shadow: 0 0 20px rgba(29, 185, 84, 0.5); transform: translateX(8px); opacity: 1; }
        .lyric-word { transition: all 0.2s cubic-bezier(0.175, 0.885, 0.32, 1.275); display: inline-block; }
        .lyric-word.active-word { color: #ff9500 !important; text-shadow: 0 0 20px rgba(255, 149, 0, 0.9) !important; transform: scale(1.15) translateY(-2px); }
        
        .admin-card { background: rgba(20,20,20,0.6); backdrop-filter: blur(10px); border: 1px solid rgba(255,255,255,0.05); border-radius: 16px; padding: 30px; margin-bottom: 24px; max-width: 700px; box-shadow: 0 15px 35px rgba(0,0,0,0.3); }
        .admin-table { width: 100%; border-collapse: collapse; margin-top: 20px; font-size: 15px; text-align: left; }
        .admin-table th, .admin-table td { padding: 14px 16px; border-bottom: 1px solid rgba(255,255,255,0.05); }
        .admin-table th { color: var(--subtext); font-weight: 700; text-transform: uppercase; font-size: 12px; letter-spacing: 1px; }
        .badge-admin { background: rgba(29,185,84,0.15); color: var(--accent); padding: 4px 10px; border-radius: 6px; font-size: 12px; font-weight: 800; letter-spacing: 0.5px;}
        .badge-user { background: rgba(255,255,255,0.1); color: var(--subtext); padding: 4px 10px; border-radius: 6px; font-size: 12px; font-weight: 700; letter-spacing: 0.5px;}
        .action-btn { background: rgba(255,255,255,0.1); color: white; border: none; padding: 8px 16px; border-radius: 6px; cursor: pointer; font-size: 13px; font-weight: 700; transition: all 0.3s ease; font-family: 'Outfit', sans-serif;}
        .action-btn:hover { background: rgba(255,255,255,0.2); transform: translateY(-2px); }
        .action-btn.danger { background: rgba(255,85,85,0.15); color: #ff5555; }
        .action-btn.danger:hover { background: rgba(255,85,85,0.3); box-shadow: 0 5px 15px rgba(255,85,85,0.2); }
        
        .modal-overlay { display: none; position: fixed; top: 0; left: 0; right: 0; bottom: 0; background: rgba(0,0,0,0.8); z-index: 2000; align-items: center; justify-content: center; backdrop-filter: blur(10px); -webkit-backdrop-filter: blur(10px); }
        .modal-card { background: rgba(25,25,25,0.95); border: 1px solid rgba(255,255,255,0.1); padding: 35px; border-radius: 16px; width: 400px; box-shadow: 0 25px 50px rgba(0,0,0,0.8); text-align: center; }
        .modal-card h3 { margin-top: 0; margin-bottom: 24px; font-size: 22px; font-weight: 800; }
        .playlist-select-item { padding: 14px 18px; background: rgba(255,255,255,0.05); border-radius: 8px; margin-bottom: 10px; cursor: pointer; font-weight: 700; text-align: left; display: flex; justify-content: space-between; align-items: center; transition: 0.3s; border: 1px solid transparent;}
        .playlist-select-item:hover { background: rgba(29, 185, 84, 0.1); border-color: var(--accent); color: var(--accent); transform: translateY(-2px); }
        
        .social-container { display: flex; height: calc(100vh - 140px); gap: 24px; }
        .social-sidebar { width: 320px; background: rgba(20,20,20,0.6); backdrop-filter: blur(10px); border-radius: 16px; display: flex; flex-direction: column; overflow: hidden; border: 1px solid rgba(255,255,255,0.05); }
        .social-sidebar-header { padding: 20px; border-bottom: 1px solid rgba(255,255,255,0.05); }
        .social-list { flex: 1; overflow-y: auto; padding: 12px; }
        .friend-item { display: flex; align-items: center; justify-content: space-between; padding: 12px 14px; border-radius: 10px; cursor: pointer; transition: 0.3s ease; margin-bottom: 6px; border: 1px solid transparent;}
        .friend-item:hover, .friend-item.active { background: rgba(255,255,255,0.05); border-color: rgba(255,255,255,0.1); }
        .friend-item-info { display: flex; align-items: center; gap: 14px; }
        .friend-pfp { width: 40px; height: 40px; border-radius: 50%; object-fit: cover; background: #333; box-shadow: 0 4px 10px rgba(0,0,0,0.3); }
        .chat-area { flex: 1; background: rgba(20,20,20,0.6); backdrop-filter: blur(10px); border-radius: 16px; display: flex; flex-direction: column; border: 1px solid rgba(255,255,255,0.05); overflow: hidden; }
        .chat-header { padding: 20px 28px; border-bottom: 1px solid rgba(255,255,255,0.05); display: flex; align-items: center; gap: 16px; background: rgba(0,0,0,0.2); }
        .chat-history { flex: 1; padding: 28px; overflow-y: auto; display: flex; flex-direction: column; gap: 16px; }
        .chat-bubble { max-width: 75%; padding: 14px 18px; border-radius: 20px; font-size: 15px; font-weight: 500; line-height: 1.5; position: relative; box-shadow: 0 4px 15px rgba(0,0,0,0.2);}
        .chat-bubble.sent { background: var(--accent); color: #000; align-self: flex-end; border-bottom-right-radius: 6px; }
        .chat-bubble.received { background: rgba(255,255,255,0.1); color: #fff; align-self: flex-start; border-bottom-left-radius: 6px; border: 1px solid rgba(255,255,255,0.05);}
        .chat-time { font-size: 11px; opacity: 0.7; margin-top: 6px; text-align: right; font-weight: 600;}
        .chat-input-area { padding: 20px; border-top: 1px solid rgba(255,255,255,0.05); display: flex; gap: 14px; background: rgba(0,0,0,0.2); }
        .chat-input { flex: 1; padding: 14px 24px; border-radius: 30px; border: 1px solid rgba(255,255,255,0.1); background: rgba(0,0,0,0.3); color: white; outline: none; font-size: 15px; font-family: 'Outfit', sans-serif;}
        .chat-input:focus { border-color: var(--accent); background: rgba(0,0,0,0.5); }
        .chat-send-btn { background: var(--accent); color: black; border: none; width: 50px; height: 50px; border-radius: 50%; cursor: pointer; display: flex; align-items: center; justify-content: center; font-size: 18px; transition: 0.3s cubic-bezier(0.25, 0.8, 0.25, 1); box-shadow: 0 5px 15px rgba(29, 185, 84, 0.3); }
        .chat-send-btn:hover { transform: scale(1.1); background: #1ed760; }

        /* ---------------------------------------------------------
           MOBILE RESPONSIVE STYLING
        --------------------------------------------------------- */
        @media (max-width: 768px) {
            body { flex-direction: column; overflow: auto; }

            .sidebar {
                position: fixed; bottom: 88px; left: 0; right: 0; width: 100%; height: auto;
                flex-direction: row; justify-content: space-around; align-items: center;
                padding: 6px 8px; background: rgba(10, 10, 10, 0.95);
                backdrop-filter: blur(25px); -webkit-backdrop-filter: blur(25px);
                border-top: 1px solid rgba(255,255,255,0.08); border-right: none;
                z-index: 999; gap: 0;
            }
            .sidebar .logo, .sidebar .nav-section-title { display: none; }
            .sidebar .nav-item { flex-direction: column; gap: 3px; font-size: 10px; padding: 6px 8px; border-radius: 8px; }
            .sidebar .nav-item i { font-size: 16px; width: auto; }
            .sidebar .nav-item:hover, .sidebar .nav-item.active { transform: none; }

            .center-wrapper { margin-bottom: 160px; margin-right: 0; border-radius: 0; }
            .top-bar { padding: 0 16px; height: 64px; border-radius: 0; }
            .search-container { width: 170px; padding: 8px 14px; }
            .search-container input { font-size: 13px; }
            .main-content { padding: 84px 16px 140px 16px; }

            h2 { font-size: 22px; margin-bottom: 16px; }
            .grid { grid-template-columns: repeat(auto-fill, minmax(135px, 1fr)); gap: 14px; margin-bottom: 32px; }
            .scroll-row { gap: 14px; padding-bottom: 12px; margin-bottom: 24px; }
            .scroll-row .card { width: 140px; min-width: 140px; max-width: 140px; padding: 12px; }
            .card { padding: 12px; border-radius: 10px; }
            .card-title { font-size: 14px; }
            .card-artist { font-size: 12px; }
            .card-play-overlay { width: 38px; height: 38px; bottom: 8px; right: 8px; opacity: 1; transform: none; }

            .player-bar { height: 88px; padding: 0 16px; }
            .now-playing-info { width: 45%; gap: 10px; }
            .np-cover { width: 48px; height: 48px; border-radius: 6px; }
            .np-title { font-size: 13px; }
            .np-artist { font-size: 11px; }
            .controls { width: 50%; gap: 4px; }
            .buttons { gap: 14px; }
            .btn { font-size: 16px; }
            .btn.play-btn { width: 34px; height: 34px; }
            .volume-controls { display: none; }

            .right-panel {
                position: fixed; top: 0; left: 0; right: 0; bottom: 88px;
                width: 100%; height: auto; margin: 0; z-index: 1001;
                border-radius: 0; background: rgba(10, 10, 10, 0.98); padding: 20px 16px;
            }

            .social-container { flex-direction: column; height: auto; }
            .social-sidebar { width: 100%; height: 250px; }
            .chat-area { width: 100%; height: 400px; }
        }

        @keyframes radioPulse { 
            0% { transform: scale(0.95); opacity: 0.3; } 
            100% { transform: scale(1.15); opacity: 0.6; } 
        }
    </style>
</head>
<body>

    <div class="modal-overlay" id="playlist-modal" onclick="if(event.target === this) closePlaylistModal()">
        <div class="modal-card">
            <h3>Add to Playlist</h3>
            <div id="playlist-modal-list" style="max-height: 240px; overflow-y: auto; margin-bottom: 20px;"></div>
            <button class="action-btn" style="width:100%; padding:12px; background:rgba(255,255,255,0.1); font-size:15px;" onclick="closePlaylistModal()">Cancel</button>
        </div>
    </div>

    <div class="sidebar">
        <div class="logo"><i class="fab fa-spotify" style="color:var(--accent); font-size: 26px;"></i> Streamer Pro</div>
        
        <div class="nav-section-title">Discover</div>
        <div class="nav-item active" onclick="switchView('home', this)"><i class="fas fa-home"></i> <span>Home</span></div>
        <div class="nav-item" onclick="document.getElementById('global-search').focus();"><i class="fas fa-search"></i> <span>Search</span></div>
        <div class="nav-item" onclick="switchView('artists', this)"><i class="fas fa-microphone"></i> <span>Artists</span></div>
        <div class="nav-item" onclick="switchView('playlists', this)"><i class="fas fa-list-music"></i> <span>Playlists</span></div>
        <div class="nav-item" onclick="switchView('radio', this)"><i class="fas fa-broadcast-tower"></i> <span>Radio</span></div>
        
        <div class="nav-section-title" style="margin-top: 24px;">Social</div>
        <div class="nav-item" onclick="switchView('messages', this)"><i class="fas fa-comment-alt"></i> <span>Messages</span></div>

        <div class="nav-section-title" style="margin-top: 24px;">General</div>
        <div class="nav-item" onclick="switchView('settings', this)"><i class="fas fa-cog"></i> <span>Settings</span></div>
    </div>

    <div class="center-wrapper">
        <div class="top-bar">
            <div class="search-container">
                <i class="fas fa-search"></i>
                <input type="text" id="global-search" placeholder="Search songs..." oninput="handleSearch(this.value)">
            </div>
            
            <div class="user-badge-wrapper">
                <div class="user-badge" onclick="toggleSettingsMenu()">
                    {% if user_pfp %}
                        <img src="{{ user_pfp }}" style="width: 28px; height: 28px; border-radius: 50%; object-fit: cover;">
                    {% else %}
                        <i class="fas fa-user-circle" style="color: var(--accent); font-size:20px;"></i> 
                    {% endif %}
                    <span id="display-username">{{ session.user }}</span>
                    <i class="fas fa-caret-down" style="font-size: 12px; margin-left: 4px;"></i>
                </div>
                <div class="settings-dropdown" id="settings-dropdown">
                    <div class="dropdown-item" onclick="switchView('settings'); toggleSettingsMenu();"><i class="fas fa-sliders-h"></i> Settings</div>
                    <div class="dropdown-divider"></div>
                    <a href="/logout" class="dropdown-item" style="color: #ff5555;"><i class="fas fa-sign-out-alt"></i> Log Out</a>
                </div>
            </div>
        </div>
        <div class="main-content" id="main-content"></div>
    </div>

    <div class="right-panel" id="right-panel">
        <div class="rp-header-row">
            <div class="rp-tabs">
                <div class="rp-tab active" id="tab-video" onclick="switchRpTab('video')">Video</div>
                <div class="rp-tab" id="tab-lyrics" onclick="switchRpTab('lyrics')">Lyrics</div>
                <div class="rp-tab" id="tab-queue" onclick="switchRpTab('queue')">Queue</div>
            </div>
            <div class="eq-container paused" id="eq-anim">
                <div class="eq-bar"></div>
                <div class="eq-bar"></div>
                <div class="eq-bar"></div>
            </div>
        </div>
        
        <div id="rp-view-video" style="display:block;">
            <div class="rp-media-container">
                <div class="rp-cover-glow" id="rp-cover-glow"></div>
                <img id="rp-cover" src="" alt="">
                <div id="rp-video-container">
                    <div id="rp-video"></div>
                </div>
            </div>
            <div id="rp-title" style="font-size: 22px; font-weight: 800; margin-bottom: 6px; width: 100%; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; letter-spacing: -0.5px;"></div>
            <div id="rp-artist" style="color: var(--subtext); font-weight: 600; font-size: 15px; margin-bottom: 12px; width: 100%; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;"></div>
            <canvas id="visualizer" width="600" height="100"></canvas>
        </div>

        <div class="lyrics-container" id="rp-view-lyrics" style="display:none;">
            <div style="color:rgba(255,255,255,0.4); text-align:center; padding-top:60px; font-weight: 600;">Select a track to load live lyrics.</div>
        </div>

        <div class="queue-container" id="rp-view-queue">
            <div style="color:rgba(255,255,255,0.4); text-align:center; padding-top:60px; font-weight: 600;">Queue is empty.</div>
        </div>
    </div>

    <div class="player-bar">
        <div class="now-playing-info">
            <img id="np-cover" class="np-cover" src="data:image/gif;base64,R0lGODlhAQABAAD/ACwAAAAAAQABAAACADs=" alt="Cover" onclick="document.querySelector('.right-panel').style.display='flex'">
            <div class="np-text">
                <div class="np-title" id="np-title">No track selected</div>
                <div class="np-artist" id="np-artist">-</div>
            </div>
        </div>
        
        <div class="controls">
            <div class="buttons">
                <button class="btn" id="shuffle-btn" onclick="toggleShuffle()" title="Shuffle"><i class="fas fa-random"></i></button>
                <button class="btn" onclick="prevTrack()" title="Previous Track"><i class="fas fa-step-backward"></i></button>
                <button class="btn play-btn" id="play-btn-wrapper" onclick="togglePlay()" title="Play / Pause (Space)">
                    <i class="fas fa-play" id="play-icon" style="margin-left: 2px;"></i>
                </button>
                <button class="btn" onclick="nextTrack()" title="Next Track"><i class="fas fa-step-forward"></i></button>
                <button class="btn" id="repeat-btn" onclick="toggleRepeat()" title="Repeat"><i class="fas fa-redo"></i></button>
                <button class="btn" id="sleep-btn" onclick="toggleSleepTimer()" title="Set Sleep Timer"><i class="fas fa-moon"></i></button>
            </div>
            <div style="width: 100%; display: flex; align-items: center; gap: 12px; font-size: 12px; color: var(--subtext); font-weight:600;">
                <span id="time-current">0:00</span>
                <input type="range" id="progress-bar" value="0" max="100">
                <span id="time-total">0:00</span>
            </div>
            <audio id="audio-player"></audio>
        </div>
        
        <div class="volume-controls">
            <a id="download-btn" href="#" download style="color:var(--subtext); margin-right:20px; display:none; font-size: 16px; transition: 0.2s;" title="Download Track"><i class="fas fa-download"></i></a>
            <button class="btn" id="dislike-btn" onclick="sendFeedback('dislike')" title="Dislike"><i class="fas fa-thumbs-down"></i></button>
            <button class="btn" id="like-btn" onclick="sendFeedback('like')" style="margin-right:15px;" title="Like"><i class="fas fa-heart"></i></button>
            
            <i class="fas fa-wave-square" title="Bass Boost" style="cursor:pointer; width:18px; font-size: 14px;"></i>
            <input type="range" id="bass-bar" value="0" max="20" style="width: 70px; margin-right: 15px;" title="Bass Boost">
            
            <i class="fas fa-volume-up" id="mute-icon" onclick="toggleMute()" style="cursor:pointer; width:18px; font-size: 14px;"></i>
            <input type="range" id="volume-bar" value="100" max="100" style="width: 100px;" title="Volume (Up/Down Arrows)">
        </div>
    </div>

    <script src="https://www.youtube.com/iframe_api"></script>

    <script>
        const currentUserIsAdmin = {{ 'true' if is_admin else 'false' }};
        const currentSessionUser = "{{ session.user }}";
        
        let allSongs = [];
        let songStats = {};
        let groupedArtists = {};
        let currentQueue = [];
        let originalQueue = [];
        let currentIndex = 0;
        let radioHistory = [];
        let isRadioMode = false;
        let isShuffle = false;
        let isRepeat = false;
        let currentSongObj = null;
        let syncedLyrics = [];
        let activeLyricIndex = -1;
        let selectedSongForPlaylist = null;
        let sleepTimerId = null;

        let currentChatFriend = null;
        let messagePollInterval = null;

        let ytPlayer = null;
        let ytPlayerReady = false;
        function onYouTubeIframeAPIReady() { ytPlayerReady = true; }

        let audioCtx;
        let analyser;
        let source;
        let bassFilter;
        let visualizerCanvas = document.getElementById('visualizer');
        let canvasCtx = visualizerCanvas.getContext('2d');
        let visualizerInitialized = false;

        const contentDiv = document.getElementById('main-content');
        const rightPanel = document.getElementById('right-panel');
        const audio = document.getElementById('audio-player');
        const playIcon = document.getElementById('play-icon');
        const eqAnim = document.getElementById('eq-anim');
        const progressBar = document.getElementById('progress-bar');
        const volumeBar = document.getElementById('volume-bar');
        const bassBar = document.getElementById('bass-bar');
        const muteIcon = document.getElementById('mute-icon');
        const timeCurrentEl = document.getElementById('time-current');
        const timeTotalEl = document.getElementById('time-total');
        const lyricsContainer = document.getElementById('rp-view-lyrics');
        const queueContainer = document.getElementById('rp-view-queue');
        const downloadBtn = document.getElementById('download-btn');

        const savedVolume = localStorage.getItem('streamer_pro_volume');
        if (savedVolume !== null) {
            volumeBar.value = savedVolume;
            audio.volume = savedVolume / 100;
        } else {
            audio.volume = 1.0;
        }
        updateSliderFill(volumeBar);
        updateSliderFill(bassBar);

        function switchRpTab(tab) {
            document.querySelectorAll('.rp-tab').forEach(el => el.classList.remove('active'));
            document.getElementById(`tab-${tab}`).classList.add('active');
            
            document.getElementById('rp-view-video').style.display = 'none';
            document.getElementById('rp-view-lyrics').style.display = 'none';
            document.getElementById('rp-view-queue').style.display = 'none';
            
            document.getElementById(`rp-view-${tab}`).style.display = 'block';
            if(tab === 'queue') renderQueue();
        }

        function renderQueue() {
            let html = '';
            if (currentQueue.length === 0 || currentIndex >= currentQueue.length - 1) {
                html = '<div style="color:rgba(255,255,255,0.4); text-align:center; padding-top:60px; font-weight:600;">Queue is empty.</div>';
            } else {
                for (let i = currentIndex + 1; i < currentQueue.length; i++) {
                    let song = currentQueue[i];
                    let cleanTitle = song.title.replace(/"/g, '&quot;').replace(/'/g, "&#39;");
                    let cleanArtist = song.artist.replace(/"/g, '&quot;').replace(/'/g, "&#39;");
                    html += `
                    <div class="queue-item" onclick="playQueue(currentQueue, ${i})">
                        <img src="${getCoverUrl(song)}" loading="lazy">
                        <div class="queue-item-info">
                            <div style="font-weight:800; font-size:14px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;">${cleanTitle}</div>
                            <div style="font-size:12px; font-weight:500; color:var(--subtext); white-space:nowrap; overflow:hidden; text-overflow:ellipsis;">${cleanArtist}</div>
                        </div>
                    </div>`;
                }
            }
            queueContainer.innerHTML = html;
        }

        volumeBar.addEventListener('input', function() {
            audio.volume = this.value / 100;
            updateSliderFill(this);
            localStorage.setItem('streamer_pro_volume', this.value);

            if (audio.volume === 0) { muteIcon.className = "fas fa-volume-mute"; }
            else if (audio.volume < 0.5) { muteIcon.className = "fas fa-volume-down"; }
            else { muteIcon.className = "fas fa-volume-up"; }
        });
        
        bassBar.addEventListener('input', function() {
            if(bassFilter && source && analyser) {
                let val = parseFloat(this.value);
                bassFilter.gain.value = val;
                
                source.disconnect();
                bassFilter.disconnect();
                if (val > 0) {
                    source.connect(bassFilter);
                    bassFilter.connect(analyser);
                } else {
                    source.connect(analyser);
                }
            }
            updateSliderFill(this);
        });

        document.addEventListener('keydown', (e) => {
            if(e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;
            if(e.code === 'Space') { e.preventDefault(); togglePlay(); }
            if(e.code === 'ArrowRight') { e.preventDefault(); nextTrack(); }
            if(e.code === 'ArrowLeft') { e.preventDefault(); prevTrack(); }
            if(e.code === 'ArrowUp') {
                e.preventDefault();
                volumeBar.value = Math.min(100, parseInt(volumeBar.value) + 10);
                volumeBar.dispatchEvent(new Event('input'));
            }
            if(e.code === 'ArrowDown') {
                e.preventDefault();
                volumeBar.value = Math.max(0, parseInt(volumeBar.value) - 10);
                volumeBar.dispatchEvent(new Event('input'));
            }
        });

        function toggleSleepTimer() {
            const btn = document.getElementById('sleep-btn');
            if (sleepTimerId) {
                clearTimeout(sleepTimerId);
                sleepTimerId = null;
                btn.classList.remove('active');
                alert("Sleep timer cancelled.");
            } else {
                let mins = parseInt(prompt("Enter sleep timer in minutes (e.g., 30):"));
                if (mins && !isNaN(mins) && mins > 0) {
                    sleepTimerId = setTimeout(() => {
                        audio.pause();
                        sleepTimerId = null;
                        document.getElementById('sleep-btn').classList.remove('active');
                    }, mins * 60000);
                    btn.classList.add('active');
                    alert(`Music will stop automatically in ${mins} minutes.`);
                }
            }
        }

        function initVisualizer() {
            if (visualizerInitialized) return;
            visualizerInitialized = true;

            const AudioContext = window.AudioContext || window.webkitAudioContext;
            audioCtx = new AudioContext();
            analyser = audioCtx.createAnalyser();

            analyser.fftSize = 256;
            analyser.smoothingTimeConstant = 0.85;
            
            bassFilter = audioCtx.createBiquadFilter();
            bassFilter.type = "lowshelf";
            bassFilter.frequency.value = 120; 
            
            let initialGain = parseFloat(bassBar.value);
            bassFilter.gain.value = initialGain;

            source = audioCtx.createMediaElementSource(audio);
            
            if (initialGain > 0) {
                source.connect(bassFilter);
                bassFilter.connect(analyser);
            } else {
                source.connect(analyser);
            }
            analyser.connect(audioCtx.destination);

            const bufferLength = analyser.frequencyBinCount;
            const dataArray = new Uint8Array(bufferLength);

            function draw() {
                requestAnimationFrame(draw);
                if (!audio.paused) {
                    analyser.getByteFrequencyData(dataArray);
                } else {
                    for(let i=0; i<bufferLength; i++) {
                        dataArray[i] = Math.max(0, dataArray[i] - 5);
                    }
                }

                canvasCtx.clearRect(0, 0, visualizerCanvas.width, visualizerCanvas.height);

                const usableBins = Math.floor(bufferLength * 0.75);
                const barWidth = (visualizerCanvas.width / usableBins) / 2;
                const gap = 2;

                let xRight = visualizerCanvas.width / 2;
                let xLeft = visualizerCanvas.width / 2;

                for(let i = 0; i < usableBins; i++) {
                    let rawValue = dataArray[i];
                    let percent = rawValue / 255;
                    let mathVal = Math.pow(percent, 1.5);
                    let barHeight = mathVal * visualizerCanvas.height;

                    let gradient = canvasCtx.createLinearGradient(0, visualizerCanvas.height, 0, 0);
                    gradient.addColorStop(0, "rgba(29, 185, 84, 0.5)");
                    gradient.addColorStop(1, "rgba(29, 185, 84, 1)");

                    canvasCtx.fillStyle = gradient;

                    canvasCtx.fillRect(xRight, visualizerCanvas.height - barHeight, barWidth - gap, barHeight);
                    if (i > 0) {
                        canvasCtx.fillRect(xLeft - barWidth, visualizerCanvas.height - barHeight, barWidth - gap, barHeight);
                    }

                    xRight += barWidth;
                    xLeft -= barWidth;
                }
            }
            draw();
        }

        function updateSliderFill(el) {
            const val = (el.value - el.min) / (el.max - el.min) * 100;
            el.style.background = `linear-gradient(to right, var(--accent) ${val}%, rgba(255,255,255,0.2) ${val}%)`;
        }

        function toggleSettingsMenu() {
            document.getElementById('settings-dropdown').classList.toggle('show');
        }

        window.onclick = function(e) {
            if (!e.target.closest('.user-badge-wrapper')) {
                document.querySelectorAll(".settings-dropdown").forEach(d => d.classList.remove('show'));
            }
        }

        function safeId(str) { return encodeURIComponent(str).replace(/[^a-zA-Z0-9]/g, ''); }

        function getCoverUrl(song) {
            return `/api/cover?file=${encodeURIComponent(song.filename)}`;
        }

        function updateMediaSession(songObj, coverUrl) {
            if ('mediaSession' in navigator) {
                navigator.mediaSession.metadata = new MediaMetadata({
                    title: songObj.title,
                    artist: songObj.artist,
                    album: 'Streamer Pro',
                    artwork: [ { src: coverUrl, sizes: '500x500', type: 'image/jpeg' } ]
                });

                navigator.mediaSession.setActionHandler('play', () => { audio.play(); });
                navigator.mediaSession.setActionHandler('pause', () => { audio.pause(); });
                navigator.mediaSession.setActionHandler('previoustrack', prevTrack);
                navigator.mediaSession.setActionHandler('nexttrack', nextTrack);
            }
        }

        function animateButton(id) {
            const el = document.getElementById(id);
            if(el) {
                el.classList.remove('pop-anim');
                void el.offsetWidth;
                el.classList.add('pop-anim');
            }
        }

        audio.addEventListener('play', () => {
            initVisualizer();
            if (audioCtx && audioCtx.state === 'suspended') audioCtx.resume();

            playIcon.classList.remove('fa-play');
            playIcon.classList.add('fa-pause');
            playIcon.style.marginLeft = '0';
            eqAnim.classList.remove('paused');
            eqAnim.classList.add('playing');

            if (ytPlayer && typeof ytPlayer.playVideo === 'function') {
                ytPlayer.playVideo();
            }
        });

        audio.addEventListener('pause', () => {
            playIcon.classList.remove('fa-pause');
            playIcon.classList.add('fa-play');
            playIcon.style.marginLeft = '2px';
            eqAnim.classList.add('paused');

            if (ytPlayer && typeof ytPlayer.pauseVideo === 'function') {
                ytPlayer.pauseVideo();
            }
        });

        audio.addEventListener('loadedmetadata', () => {
            timeTotalEl.innerText = formatTime(audio.duration);
        });

        audio.addEventListener('timeupdate', () => {
            if (!audio.duration) return;
            const percent = (audio.currentTime / audio.duration) * 100;
            progressBar.value = percent;
            updateSliderFill(progressBar);
            timeCurrentEl.innerText = formatTime(audio.currentTime);

            if (ytPlayer && typeof ytPlayer.getCurrentTime === 'function' && typeof ytPlayer.getPlayerState === 'function' && ytPlayer.getPlayerState() === YT.PlayerState.PLAYING) {
                let ytTime = ytPlayer.getCurrentTime();
                if (Math.abs(ytTime - audio.currentTime) > 2.0) {
                    ytPlayer.seekTo(audio.currentTime, true);
                }
            }

            if (syncedLyrics.length > 0) {
                let newIndex = syncedLyrics.findIndex(l => l.time > audio.currentTime) - 1;
                if (newIndex < 0) newIndex = 0;
                if (newIndex === syncedLyrics.length - 2 && audio.currentTime >= syncedLyrics[syncedLyrics.length - 1].time) {
                    newIndex = syncedLyrics.length - 1;
                }

                if (newIndex !== activeLyricIndex && newIndex >= 0) {
                    if (activeLyricIndex >= 0) {
                        const oldEl = document.getElementById(`lyric-${activeLyricIndex}`);
                        if (oldEl) {
                            oldEl.classList.remove('active');
                            oldEl.querySelectorAll('.lyric-word').forEach(w => w.classList.remove('active-word'));
                        }
                    }
                    activeLyricIndex = newIndex;
                    const newEl = document.getElementById(`lyric-${activeLyricIndex}`);
                    if (newEl) {
                        newEl.classList.add('active');
                        const containerHalf = lyricsContainer.clientHeight / 2;
                        const lineHalf = newEl.clientHeight / 2;
                        lyricsContainer.scrollTo({
                            top: newEl.offsetTop - containerHalf + lineHalf,
                            behavior: 'smooth'
                        });
                    }
                }

                if (activeLyricIndex >= 0) {
                    const line = syncedLyrics[activeLyricIndex];
                    if (line.words.length > 0) {
                        const elapsed = audio.currentTime - line.time;
                        let progress = elapsed / line.duration;

                        if (progress < 0) progress = 0;
                        if (progress > 1) progress = 1;

                        let wordIndex = line.wordTimings.findIndex(wt => progress >= wt.startPercent && progress <= wt.endPercent);
                        if (wordIndex === -1 && progress >= 1) wordIndex = line.words.length - 1;
                        if (wordIndex === -1 && progress <= 0) wordIndex = 0;

                        const activeLineEl = document.getElementById(`lyric-${activeLyricIndex}`);
                        if (activeLineEl) {
                            const wordSpans = activeLineEl.querySelectorAll('.lyric-word');
                            wordSpans.forEach((span, idx) => {
                                if (idx === wordIndex) {
                                    if (!span.classList.contains('active-word')) span.classList.add('active-word');
                                } else {
                                    if (span.classList.contains('active-word')) span.classList.remove('active-word');
                                }
                            });
                        }
                    }
                }
            }
        });

        progressBar.addEventListener('input', function() {
            if (!audio.duration) return;
            audio.currentTime = (this.value / 100) * audio.duration;
            updateSliderFill(this);
            if (ytPlayer && typeof ytPlayer.seekTo === 'function') {
                ytPlayer.seekTo(audio.currentTime, true);
            }
        });

        function toggleMute() {
            if (audio.volume > 0) {
                audio.dataset.savedVolume = volumeBar.value;
                volumeBar.value = 0;
                audio.volume = 0;
                muteIcon.className = "fas fa-volume-mute";
            } else {
                volumeBar.value = audio.dataset.savedVolume || 100;
                audio.volume = volumeBar.value / 100;
                muteIcon.className = "fas fa-volume-up";
            }
            updateSliderFill(volumeBar);
            localStorage.setItem('streamer_pro_volume', volumeBar.value);
        }

        function togglePlay() {
            animateButton('play-btn-wrapper');
            if (audio.paused) audio.play();
            else audio.pause();
        }

        function toggleShuffle() {
            isShuffle = !isShuffle;
            document.getElementById('shuffle-btn').classList.toggle('active', isShuffle);
            if (isShuffle && currentQueue.length > 0) {
                originalQueue = [...currentQueue];
                let remaining = currentQueue.slice(currentIndex + 1);
                for (let i = remaining.length - 1; i > 0; i--) {
                    const j = Math.floor(Math.random() * (i + 1));
                    [remaining[i], remaining[j]] = [remaining[j], remaining[i]];
                }
                currentQueue = [currentQueue[currentIndex], ...remaining];
                currentIndex = 0;
            } else if (!isShuffle && originalQueue.length > 0) {
                let currentItem = currentQueue[currentIndex];
                currentQueue = [...originalQueue];
                currentIndex = currentQueue.findIndex(s => s.filename === currentItem.filename);
                if(currentIndex === -1) currentIndex = 0;
            }
            renderQueue();
        }

        function toggleRepeat() {
            isRepeat = !isRepeat;
            document.getElementById('repeat-btn').classList.toggle('active', isRepeat);
            audio.loop = isRepeat;
        }

        function formatTime(secs) {
            if (isNaN(secs)) return "0:00";
            const m = Math.floor(secs / 60);
            const s = Math.floor(secs % 60).toString().padStart(2, '0');
            return `${m}:${s}`;
        }

        fetch('/api/data').then(res => res.json()).then(data => {
            allSongs = data.songs;
            songStats = data.stats;
            processArtists(allSongs);
            switchView('home');
        });

        function processArtists(songs) {
            groupedArtists = {};
            songs.forEach(song => {
                let artistKey = song.artist;
                if(!groupedArtists[artistKey]) groupedArtists[artistKey] = [];
                groupedArtists[artistKey].push(song);
            });
            const sortedKeys = Object.keys(groupedArtists).sort((a,b) => a.localeCompare(b));
            let newGrouped = {};
            sortedKeys.forEach(k => newGrouped[k] = groupedArtists[k]);
            groupedArtists = newGrouped;
        }

        function switchView(view, el=null) {
            document.querySelectorAll('.nav-item').forEach(e => e.classList.remove('active'));
            if(el) el.classList.add('active');
            else {
                let idx = ['home', 'search', 'artists', 'playlists', 'radio', 'messages', 'settings'].indexOf(view);
                if (idx !== -1) {
                    let items = document.querySelectorAll('.nav-item');
                }
            }
            
            document.getElementById('global-search').value = '';
            
            if (messagePollInterval) {
                clearInterval(messagePollInterval);
                messagePollInterval = null;
            }

            if (view === 'home') renderHome();
            if (view === 'artists') renderArtists();
            if (view === 'playlists') renderPlaylists();
            if (view === 'messages') renderMessages();
            if (view === 'radio') {
                renderRadio();
                if (!isRadioMode) {
                    startRadio();
                } else {
                    updateRadioUI();
                }
            }
            if (view === 'settings') renderSettings();
        }

        function handleSearch(query) {
            let q = query.toLowerCase().trim();
            if(!q) { renderHome(); return; }

            document.querySelectorAll('.nav-item').forEach(el => el.classList.remove('active'));
            let results = allSongs.filter(s => s.title.toLowerCase().includes(q) || s.artist.toLowerCase().includes(q));
            renderGrid(results, `Search Results for "${query}"`);
        }

        function playQueue(queue, index) {
            if (queue.length === 0) return;
            currentQueue = queue;
            if (!isShuffle) originalQueue = [...queue];
            currentIndex = index;
            isRadioMode = false;
            loadTrack(currentQueue[currentIndex]);
        }

        function playQueueByFilenames(filenames, index) {
            let queue = filenames.map(f => allSongs.find(s => s.filename === f)).filter(Boolean);
            playQueue(queue, index);
        }

        function playSongByFilename(filename) {
            let songIndex = allSongs.findIndex(s => s.filename === filename);
            if(songIndex !== -1) {
                playQueue(allSongs, songIndex);
            }
        }

        function renderRadio() {
            contentDiv.innerHTML = `
                <div class="fade-in" style="display:flex; flex-direction:column; align-items:center; justify-content:center; min-height: 80vh; text-align:center; padding: 20px;">
                    <div style="position:relative; width: 160px; height: 160px; display:flex; align-items:center; justify-content:center; margin-bottom: 30px;">
                        <div class="radio-glow" style="position:absolute; width:100%; height:100%; background:var(--accent); border-radius:50%; opacity:0.3; filter:blur(30px); animation: radioPulse 2s infinite alternate;"></div>
                        <i class="fas fa-broadcast-tower" style="font-size: 64px; color: var(--text); z-index: 2; filter: drop-shadow(0 0 10px rgba(255,255,255,0.5));"></i>
                    </div>
                    <h2 style="font-size: 40px; margin-bottom: 16px; font-weight: 800; letter-spacing: -1px;">Infinite Radio</h2>
                    <p style="color: var(--subtext); font-size: 16px; max-width: 450px; line-height: 1.6; margin-bottom: 30px; font-weight:500;">
                        An endless stream tailored to your listening habits, blending your favorites with seamless discovery.
                    </p>
                    <div id="radio-current-status" style="background: rgba(255,255,255,0.05); border: 1px solid rgba(255,255,255,0.1); padding: 16px 24px; border-radius: 30px; display: flex; align-items: center; gap: 12px; font-weight: 700; box-shadow: 0 10px 25px rgba(0,0,0,0.3); font-size:14px;">
                        <i class="fas fa-satellite-dish" style="color: var(--accent);"></i> Tuning in...
                    </div>
                </div>
            `;
        }

        function updateRadioUI() {
            if (isRadioMode) {
                const statusEl = document.getElementById('radio-current-status');
                if (statusEl && currentSongObj) {
                    statusEl.innerHTML = `<i class="fas fa-volume-up" style="color: var(--accent);"></i> Broadcasting: <span style="color:white; margin-left:6px;">${currentSongObj.title}</span> <span style="color:var(--subtext); margin-left:6px;">by ${currentSongObj.artist}</span>`;
                }
            }
        }

        function startRadio() {
            isRadioMode = true;
            radioHistory = [];
            currentQueue = [];
            
            document.querySelectorAll('.nav-item').forEach(e => e.classList.remove('active'));
            let navItems = document.querySelectorAll('.nav-item');
            if(navItems.length > 4) navItems[4].classList.add('active'); 
            
            if (!currentSongObj) {
                nextTrack();
            } else {
                updateRadioUI();
            }
        }

        function loadTrack(songObj) {
            if (!songObj) return;
            currentSongObj = songObj;
            
            let fileUrl = `/play/` + songObj.filename.split('/').map(encodeURIComponent).join('/');
            audio.src = fileUrl;
            
            document.getElementById('np-title').innerText = songObj.title;
            document.getElementById('np-artist').innerText = songObj.artist;
            document.getElementById('rp-title').innerText = songObj.title;
            document.getElementById('rp-artist').innerText = songObj.artist;
            
            let coverUrl = getCoverUrl(songObj);
            document.getElementById('np-cover').src = coverUrl;
            document.getElementById('rp-cover').src = coverUrl;
            document.getElementById('rp-cover-glow').style.backgroundImage = `url("${coverUrl}")`;
            
            document.getElementById('download-btn').href = `/download/` + songObj.filename.split('/').map(encodeURIComponent).join('/');
            document.getElementById('download-btn').style.display = 'inline-block';
            
            updateMediaSession(songObj, coverUrl);
            fetchStatusAndLog(songObj.filename);
            
            audio.play();
            renderQueue();
            
            document.getElementById('rp-video-container').style.opacity = '0';
            fetch(`/api/video?artist=${encodeURIComponent(songObj.artist)}&song=${encodeURIComponent(songObj.title)}`)
                .then(res => res.json())
                .then(data => {
                    if(data.youtube_id && ytPlayerReady) {
                        if(!ytPlayer) {
                            ytPlayer = new YT.Player('rp-video', {
                                videoId: data.youtube_id,
                                playerVars: { 'autoplay': 1, 'controls': 0, 'disablekb': 1, 'fs': 0, 'modestbranding': 1, 'rel': 0, 'showinfo': 0, 'mute': 1 },
                                events: {
                                    'onReady': (e) => { e.target.playVideo(); document.getElementById('rp-video-container').style.opacity = '1'; }
                                }
                            });
                        } else {
                            ytPlayer.loadVideoById(data.youtube_id);
                            document.getElementById('rp-video-container').style.opacity = '1';
                        }
                    } else if (ytPlayer) {
                        ytPlayer.stopVideo();
                        document.getElementById('rp-video-container').style.opacity = '0';
                    }
                });

            lyricsContainer.innerHTML = '<div style="color:var(--subtext); text-align:center; padding-top:60px; font-weight:600;"><i class="fas fa-spinner fa-spin"></i> Searching for lyrics...</div>';
            syncedLyrics = [];
            activeLyricIndex = -1;
            
            let query = `${songObj.artist} ${songObj.title}`.toLowerCase();
            let cleanQuery = query.replace(/\s*\(feat\..*?\)/g, '').replace(/\s*ft\..*$/g, '').replace(/[^a-z0-9 ]/g, '');
            
            fetch(`https://lrclib.net/api/search?q=${encodeURIComponent(cleanQuery)}`)
                .then(r => r.json())
                .then(results => {
                    if (results && results.length > 0) {
                        let bestMatch = results.find(r => r.syncedLyrics);
                        if (bestMatch && bestMatch.syncedLyrics) {
                            let parsed = parseLrc(bestMatch.syncedLyrics);
                            syncedLyrics = parsed;
                            renderLyrics(parsed);
                        } else if (bestMatch && bestMatch.plainLyrics) {
                            renderPlainLyrics(bestMatch.plainLyrics);
                        } else {
                            lyricsContainer.innerHTML = '<div style="color:var(--subtext); text-align:center; padding-top:60px; font-weight:600;">No lyrics found for this track.</div>';
                        }
                    } else {
                        lyricsContainer.innerHTML = '<div style="color:var(--subtext); text-align:center; padding-top:60px; font-weight:600;">No lyrics found for this track.</div>';
                    }
                }).catch(() => {
                    lyricsContainer.innerHTML = '<div style="color:var(--subtext); text-align:center; padding-top:60px; font-weight:600;">Failed to load lyrics.</div>';
                });
        }

        function parseLrc(lrcString) {
            const lines = lrcString.split('\\n');
            const parsed = [];
            const timeRegex = /\[(\d{2}):(\d{2})\.(\d{2,3})\]/;
            
            for (let i = 0; i < lines.length; i++) {
                const line = lines[i];
                const match = timeRegex.exec(line);
                if (match) {
                    const minutes = parseInt(match[1], 10);
                    const seconds = parseInt(match[2], 10);
                    const milliseconds = parseInt(match[3].padEnd(3, '0'), 10);
                    const timeInSeconds = (minutes * 60) + seconds + (milliseconds / 1000);
                    
                    const text = line.replace(timeRegex, '').trim();
                    if(text) {
                        const words = text.split(' ');
                        const wordTimings = [];
                        let accum = 0;
                        for(let w=0; w<words.length; w++) {
                            const chunk = 1 / words.length;
                            wordTimings.push({ word: words[w], startPercent: accum, endPercent: accum + chunk });
                            accum += chunk;
                        }
                        
                        parsed.push({ time: timeInSeconds, text: text, words: words, wordTimings: wordTimings, duration: 3.0 });
                    }
                }
            }
            
            for(let i=0; i<parsed.length - 1; i++) {
                parsed[i].duration = parsed[i+1].time - parsed[i].time;
                if(parsed[i].duration <= 0 || parsed[i].duration > 10) parsed[i].duration = 3.0;
            }
            
            return parsed;
        }

        function renderLyrics(parsedLines) {
            let html = '<div style="height: 50%;"></div>';
            parsedLines.forEach((line, index) => {
                let wordsHtml = line.wordTimings.map(wt => `<span class="lyric-word">${wt.word}</span>`).join(' ');
                html += `<div class="lyric-line" id="lyric-${index}" onclick="seekTo(${line.time})">${wordsHtml}</div>`;
            });
            html += '<div style="height: 50%;"></div>';
            lyricsContainer.innerHTML = html;
        }

        function renderPlainLyrics(text) {
            let html = '<div style="height: 20px;"></div>';
            text.split('\\n').forEach(line => {
                if(line.trim()) html += `<div class="lyric-line" style="cursor:default;">${line.replace(/</g, '&lt;')}</div>`;
                else html += `<br>`;
            });
            html += '<div style="height: 50px;"></div>';
            lyricsContainer.innerHTML = html;
        }

        function seekTo(timeSeconds) {
            if(!audio.duration) return;
            audio.currentTime = timeSeconds;
            if(ytPlayer && typeof ytPlayer.seekTo === 'function') ytPlayer.seekTo(timeSeconds, true);
        }

        function nextTrack() {
            if (isRadioMode) {
                if (currentSongObj) radioHistory.push(currentSongObj.filename);
                if (radioHistory.length > 30) radioHistory.shift();
                
                let histParam = radioHistory.map(encodeURIComponent).join(',');
                let artistParam = currentSongObj ? encodeURIComponent(currentSongObj.artist) : '';
                
                fetch('/api/radio/next?history=' + histParam + '&current_artist=' + artistParam)
                    .then(res => res.json())
                    .then(data => { 
                        if(data.song) {
                            loadTrack(data.song);
                            updateRadioUI();
                        }
                    });
            } else {
                if (currentQueue.length === 0) return;
                currentIndex++;
                if (currentIndex >= currentQueue.length) {
                    currentIndex = 0;
                    if (!isRepeat) {
                        audio.pause();
                        renderQueue();
                        return;
                    }
                }
                loadTrack(currentQueue[currentIndex]);
            }
        }

        function prevTrack() {
            if (audio.currentTime > 3) {
                audio.currentTime = 0;
                if(ytPlayer && typeof ytPlayer.seekTo === 'function') ytPlayer.seekTo(0, true);
            } else if (!isRadioMode && currentQueue.length > 0) {
                currentIndex--;
                if (currentIndex < 0) currentIndex = currentQueue.length - 1;
                loadTrack(currentQueue[currentIndex]);
            }
        }

        function buildCardsHTML(songsArray, isRow = false, playlistToken = null) {
            let html = isRow ? `<div class="scroll-row">` : `<div class="grid">`;
            let filenameArr = JSON.stringify(songsArray.map(s => s.filename)).replace(/"/g, '&quot;');
            
            songsArray.forEach((song, i) => {
                let coverUrl = getCoverUrl(song);
                let cleanTitle = song.title.replace(/"/g, '&quot;').replace(/'/g, "&#39;");
                let cleanArtist = song.artist.replace(/"/g, '&quot;').replace(/'/g, "&#39;");
                let cleanFilename = song.filename.replace(/'/g, "\\'");

                html += `
                <div class="card">
                    <div class="card-img-container" onclick="playQueueByFilenames(${filenameArr}, ${i})">
                        <img src="${coverUrl}" loading="lazy">
                        <div class="card-play-overlay"><i class="fas fa-play" style="margin-left: 2px;"></i></div>
                    </div>
                    <div class="card-info">
                        <div class="card-title" title="${cleanTitle}" onclick="playQueueByFilenames(${filenameArr}, ${i})">${cleanTitle}</div>
                        <div class="card-bottom-row">
                            <div class="card-artist" title="${cleanArtist}">${cleanArtist}</div>
                            <div>
                                ${playlistToken ? `<button class="action-btn danger" style="padding: 6px 10px; background: rgba(255,85,85,0.2);" onclick="removeFromPlaylist('${playlistToken}', '${cleanFilename}')" title="Remove"><i class="fas fa-times"></i></button>` : ''}
                                <button class="action-btn" style="padding: 6px 10px;" onclick="openAddToPlaylistModal('${cleanFilename}')" title="Add to Playlist"><i class="fas fa-plus"></i></button>
                            </div>
                        </div>
                    </div>
                </div>`;
            });
            html += `</div>`;
            return html;
        }

        function renderGrid(songsArray, title) {
            contentDiv.innerHTML = `<div class="fade-in"><h2 style="margin-bottom:24px;">${title}</h2>` + buildCardsHTML(songsArray) + `</div>`;
        }

        function renderHome() {
            let popular = [...allSongs].sort((a, b) => {
                let statA = songStats[a.filename] || {likes:0, plays:0};
                let statB = songStats[b.filename] || {likes:0, plays:0};
                return (statB.likes * 5 + statB.plays) - (statA.likes * 5 + statA.plays);
            }).slice(0, 12);

            let newlyAdded = [...allSongs].sort((a, b) => b.mtime - a.mtime).slice(0, 12);
            
            let topArtistsHtml = `<div class="scroll-row">`;
            let artistNames = Object.keys(groupedArtists).slice(0, 12);
            artistNames.forEach(artist => {
                let sampleSong = groupedArtists[artist][0];
                let coverUrl = getCoverUrl(sampleSong);
                let escapedArtist = artist.replace(/'/g, "\\\\'").replace(/"/g, '&quot;');
                topArtistsHtml += `
                <div class="card" onclick="renderGrid(groupedArtists['${escapedArtist}'], '${escapedArtist}')" style="text-align:center; min-width: 150px; width: 150px; max-width: 150px; padding: 16px;">
                    <div class="card-img-container" style="border-radius: 50%; height: 110px; width: 110px; margin: 0 auto 12px auto; box-shadow: 0 10px 20px rgba(0,0,0,0.5);">
                        <img src="${coverUrl}" loading="lazy" style="border-radius: 50%;">
                        <div class="card-play-overlay"><i class="fas fa-play" style="margin-left: 2px;"></i></div>
                    </div>
                    <div class="card-title" style="font-size: 14px; margin-bottom: 4px;">${artist}</div>
                    <div style="font-size:12px; color:var(--subtext); font-weight: 500;">${groupedArtists[artist].length} tracks</div>
                </div>`;
            });
            topArtistsHtml += `</div>`;

            contentDiv.innerHTML = `
                <div class="fade-in">
                    <h2 style="margin-bottom: 20px;"><i class="fas fa-fire" style="color:#ff5555; margin-right:12px;"></i>Trending Now</h2>
                    ${buildCardsHTML(popular, true)}

                    <h2 style="margin-bottom: 20px; margin-top: 10px;"><i class="fas fa-star" style="color:#ffcc00; margin-right:12px;"></i>Recently Added</h2>
                    ${buildCardsHTML(newlyAdded, true)}
                    
                    <h2 style="margin-bottom: 20px; margin-top: 10px;"><i class="fas fa-users" style="color:var(--accent); margin-right:12px;"></i>Featured Artists</h2>
                    ${topArtistsHtml}

                    <div style="text-align: center; margin-top: 40px; margin-bottom: 50px; background: rgba(255,255,255,0.02); padding: 40px 20px; border-radius: 20px; border: 1px solid rgba(255,255,255,0.05); box-shadow: 0 10px 30px rgba(0,0,0,0.2);">
                        <i class="fas fa-compact-disc" style="font-size: 48px; color: var(--accent); margin-bottom: 20px; opacity: 0.8; filter: drop-shadow(0 0 15px rgba(29, 185, 84, 0.4));"></i>
                        <h2 style="margin-bottom: 12px; font-size: 28px;">Your Full Library</h2>
                        <p style="color: var(--subtext); font-size: 15px; margin-bottom: 24px; font-weight: 500;">Explore all ${allSongs.length} tracks in your collection.</p>
                        <button class="action-btn" style="background:var(--accent); color:black; padding: 14px 32px; font-size: 15px; font-weight: 800; border-radius: 30px; box-shadow: 0 10px 25px rgba(29, 185, 84, 0.4);" onclick="renderAllSongs()">
                            Browse All Songs
                        </button>
                    </div>
                </div>
            `;
        }

        function renderAllSongs() {
            document.querySelectorAll('.nav-item').forEach(e => e.classList.remove('active'));
            contentDiv.innerHTML = `
                <div class="fade-in">
                    <div style="display:flex; align-items:center; gap: 20px; margin-bottom: 30px;">
                        <button class="action-btn" style="background:rgba(255,255,255,0.1); padding:12px 18px; border-radius:50%; box-shadow: none;" onclick="switchView('home')"><i class="fas fa-arrow-left"></i></button>
                        <h2 style="margin:0; font-size: 32px;">All Songs</h2>
                    </div>
                    ${buildCardsHTML(allSongs)}
                </div>
            `;
        }

        function renderArtists() {
            let html = `<div class="fade-in"><h2 style="margin-bottom:24px;">Artists</h2><div class="grid">`;
            Object.keys(groupedArtists).forEach(artist => {
                let sampleSong = groupedArtists[artist][0];
                let coverUrl = getCoverUrl(sampleSong);
                let escapedArtist = artist.replace(/'/g, "\\\\'").replace(/"/g, '&quot;');

                html += `
                <div class="card" onclick="renderGrid(groupedArtists['${escapedArtist}'], '${escapedArtist}')" style="text-align:center;">
                    <div class="card-img-container" style="border-radius: 50%;">
                        <img src="${coverUrl}" loading="lazy" style="border-radius: 50%;">
                        <div class="card-play-overlay"><i class="fas fa-play" style="margin-left: 2px;"></i></div>
                    </div>
                    <div class="card-title" style="margin-top: 8px;">${artist}</div>
                    <div class="card-artist" style="text-align:center; padding:0; margin-top:4px;">${groupedArtists[artist].length} tracks</div>
                </div>`;
            });
            html += `</div></div>`;
            contentDiv.innerHTML = html;
        }

        function renderPlaylists() {
            fetch('/api/playlists').then(res => res.json()).then(playlists => {
                let html = `
                    <div class="fade-in">
                        <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:28px;">
                            <h2 style="margin:0;">🎵 Playlists</h2>
                            <button class="action-btn" style="background:var(--accent); color:black; padding:12px 20px; font-size:14px; box-shadow: 0 5px 15px rgba(29, 185, 84, 0.3);" onclick="createPlaylistPrompt()"><i class="fas fa-plus"></i> New Playlist</button>
                        </div>
                        <div class="grid">
                `;

                for (let [token, pl] of Object.entries(playlists)) {
                    let sampleSong = pl.songs.length > 0 ? allSongs.find(s => s.filename === pl.songs[0]) : null;
                    let coverUrl = sampleSong ? getCoverUrl(sampleSong) : '';

                    html += `
                    <div class="card" onclick="viewPlaylist('${token}')" style="text-align:center;">
                        <div class="card-img-container">
                            ${coverUrl ? `<img src="${coverUrl}" loading="lazy">` : '<i class="fas fa-music" style="font-size:40px; color:rgba(255,255,255,0.2);"></i>'}
                            <div class="card-play-overlay"><i class="fas fa-play" style="margin-left: 2px;"></i></div>
                        </div>
                        <div class="card-title" style="text-align:center; margin-top: 8px;">${pl.name.replace(/"/g, '&quot;')}</div>
                        <div class="card-artist" style="text-align:center; padding:0; margin-top:4px;">${pl.songs.length} tracks</div>
                    </div>`;
                }

                html += `</div></div>`;
                contentDiv.innerHTML = html;
            });
        }

        function createPlaylistPrompt() {
            let name = prompt("Enter playlist name:");
            if (!name) return;
            fetch('/api/playlists', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({name})
            }).then(res => res.json()).then(() => renderPlaylists());
        }

        function viewPlaylist(token) {
            fetch(`/api/playlist/${token}`).then(res => res.json()).then(pl => {
                if (pl.error) { alert(pl.error); return; }
                let playlistSongs = pl.songs.map(filename => allSongs.find(s => s.filename === filename)).filter(Boolean);
                let shareUrl = window.location.origin + '/playlist/' + token;
                
                let filenameArr = JSON.stringify(playlistSongs.map(s => s.filename)).replace(/"/g, '&quot;');

                contentDiv.innerHTML = `
                    <div class="fade-in">
                        <div style="display:flex; justify-content:space-between; align-items:flex-end; margin-bottom:30px; background: rgba(255,255,255,0.02); padding: 30px; border-radius: 16px; border: 1px solid rgba(255,255,255,0.05); flex-wrap:wrap; gap:16px;">
                            <div>
                                <div style="font-size: 12px; text-transform: uppercase; letter-spacing: 1px; color: var(--subtext); margin-bottom: 8px; font-weight: 700;">Playlist</div>
                                <h2 style="margin-bottom:8px; font-size: 36px;">${pl.name.replace(/"/g, '&quot;')}</h2>
                                <p style="color:var(--subtext); margin:0; font-size:14px; font-weight: 500;">Created by <span style="color:white;">${pl.creator}</span> • ${playlistSongs.length} tracks</p>
                            </div>
                            <div style="display:flex; gap:12px;">
                                ${playlistSongs.length > 0 ? `<button class="action-btn" style="background:var(--accent); color:black; padding: 12px 24px; font-size:15px; box-shadow: 0 5px 15px rgba(29, 185, 84, 0.3);" onclick="playQueueByFilenames(${filenameArr}, 0)"><i class="fas fa-play"></i> Play</button>` : ''}
                                <button class="action-btn" style="padding: 12px 18px;" onclick="navigator.clipboard.writeText('${shareUrl}'); alert('Shareable link copied to clipboard!');"><i class="fas fa-share-alt"></i> Share</button>
                                ${(pl.creator === currentSessionUser) || currentUserIsAdmin ? `<button class="action-btn danger" style="padding: 12px 18px;" onclick="deletePlaylist('${token}')"><i class="fas fa-trash"></i></button>` : ''}
                            </div>
                        </div>
                        ${playlistSongs.length === 0 ? '<div style="text-align:center; padding: 60px; color:rgba(255,255,255,0.4); font-weight:600; background:rgba(0,0,0,0.2); border-radius:16px;">This playlist is empty. Add songs from any track card!</div>' : buildCardsHTML(playlistSongs, false, token)}
                    </div>
                `;
            });
        }

        function deletePlaylist(token) {
            if(!confirm("Are you sure you want to delete this playlist?")) return;
            fetch(`/api/playlist/${token}`, {method: 'DELETE'}).then(() => renderPlaylists());
        }

        function removeFromPlaylist(token, filename) {
            fetch(`/api/playlist/${token}/song`, {
                method: 'DELETE',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({filename})
            }).then(() => viewPlaylist(token));
        }

        function openAddToPlaylistModal(filename) {
            selectedSongForPlaylist = filename;
            fetch('/api/playlists').then(res => res.json()).then(playlists => {
                let modalList = document.getElementById('playlist-modal-list');
                let html = '';

                let myPlaylists = Object.entries(playlists).filter(([t, p]) => p.creator === currentSessionUser);

                if (myPlaylists.length === 0) {
                    html = '<p style="color:rgba(255,255,255,0.5); font-weight:600;">No playlists found. Create one from the Playlists tab!</p>';
                } else {
                    for (let [token, pl] of myPlaylists) {
                        html += `<div class="playlist-select-item" onclick="confirmAddToPlaylist('${token}')">
                            <span style="font-size: 15px;"><i class="fas fa-list" style="margin-right:8px; color:var(--subtext);"></i> ${pl.name.replace(/"/g, '&quot;')}</span>
                            <span style="font-size:12px; color:rgba(255,255,255,0.3);">${pl.songs.length} tracks</span>
                        </div>`;
                    }
                }

                modalList.innerHTML = html;
                document.getElementById('playlist-modal').style.display = 'flex';
            });
        }

        function confirmAddToPlaylist(token) {
            if (!selectedSongForPlaylist) return;
            fetch(`/api/playlist/${token}/song`, {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({filename: selectedSongForPlaylist})
            }).then(res => res.json()).then(data => {
                closePlaylistModal();
                if(data.success) alert("Song added to playlist!");
                else alert(data.error || "Failed to add song");
            });
        }

        function closePlaylistModal() {
            document.getElementById('playlist-modal').style.display = 'none';
            selectedSongForPlaylist = null;
        }

        function renderMessages() {
            contentDiv.innerHTML = `
                <div class="fade-in social-container">
                    <div class="social-sidebar">
                        <div class="social-sidebar-header">
                            <h3 style="margin:0 0 16px 0; font-size:20px; font-weight:800; letter-spacing:-0.5px;">Friends</h3>
                            <div style="display:flex; gap:10px;">
                                <input type="text" id="add-friend-input" placeholder="Enter username..." style="flex:1; padding:12px 16px; border-radius:20px; border:1px solid rgba(255,255,255,0.1); background:rgba(0,0,0,0.3); color:white; font-size:13px; outline:none; font-family:'Outfit', sans-serif;">
                                <button class="action-btn" style="padding:10px 16px; border-radius:20px; background:var(--accent); color:black;" onclick="sendFriendRequest()"><i class="fas fa-user-plus"></i></button>
                            </div>
                        </div>
                        <div class="social-list" id="friends-list-container">
                            <div style="text-align:center; padding:30px; color:rgba(255,255,255,0.3); font-weight:600;"><i class="fas fa-spinner fa-spin"></i> Loading...</div>
                        </div>
                    </div>
                    
                    <div class="chat-area" id="chat-area">
                        <div style="flex:1; display:flex; flex-direction:column; align-items:center; justify-content:center; color:rgba(255,255,255,0.2);">
                            <i class="fas fa-comment-dots" style="font-size:64px; margin-bottom:20px;"></i>
                            <h3 style="font-weight:700; margin-bottom:8px;">Your Messages</h3>
                            <p style="font-weight:500;">Select a friend to start chatting</p>
                        </div>
                    </div>
                </div>
            `;
            refreshFriendsList();
        }

        function refreshFriendsList() {
            fetch('/api/social/friends').then(res => res.json()).then(data => {
                let html = '';
                
                if(data.requests && data.requests.length > 0) {
                    html += `<div style="font-size:11px; text-transform:uppercase; color:var(--accent); font-weight:800; margin:14px 0 8px 8px; letter-spacing:1px;">Pending Requests</div>`;
                    data.requests.forEach(req => {
                        let pfp = req.pfp ? req.pfp : "data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='%231DB954'><path d='M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm0 3c1.66 0 3 1.34 3 3s-1.34 3-3 3-3-1.34-3-3 1.34-3 3-3zm0 14.2c-2.5 0-4.71-1.28-6-3.22.03-1.99 4-3.08 6-3.08 1.99 0 5.97 1.09 6 3.08-1.29 1.94-3.5 3.22-6 3.22z'/></svg>";
                        html += `
                        <div class="friend-item" style="border: 1px dashed rgba(29, 185, 84, 0.4); background:rgba(29, 185, 84, 0.05);">
                            <div class="friend-item-info">
                                <img src="${pfp}" class="friend-pfp">
                                <span style="font-weight:700; font-size:15px;">${req.username}</span>
                            </div>
                            <div>
                                <button class="action-btn" style="background:var(--accent); color:black; padding:6px 10px;" onclick="acceptFriend('${req.username}')"><i class="fas fa-check"></i></button>
                            </div>
                        </div>`;
                    });
                }
                
                html += `<div style="font-size:11px; text-transform:uppercase; color:var(--subtext); font-weight:800; margin:20px 0 8px 8px; letter-spacing:1px;">Direct Messages</div>`;
                if(data.friends && data.friends.length > 0) {
                    data.friends.forEach(f => {
                        let pfp = f.pfp ? f.pfp : "data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='%23555'><path d='M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm0 3c1.66 0 3 1.34 3 3s-1.34 3-3 3-3-1.34-3-3 1.34-3 3-3zm0 14.2c-2.5 0-4.71-1.28-6-3.22.03-1.99 4-3.08 6-3.08 1.99 0 5.97 1.09 6 3.08-1.29 1.94-3.5 3.22-6 3.22z'/></svg>";
                        let activeClass = (currentChatFriend === f.username) ? 'active' : '';
                        
                        let npText = "";
                        if (f.now_playing) {
                            let songMeta = allSongs.find(s => s.filename === f.now_playing.song);
                            if(songMeta) {
                                let cleanFilename = songMeta.filename.replace(/'/g, "\\'");
                                let listenAlongBtn = `<button class="action-btn" style="padding:4px 10px; font-size:10px; margin-top:6px; background:rgba(29, 185, 84, 0.2); color:var(--accent);" onclick="event.stopPropagation(); playSongByFilename('${cleanFilename}')"><i class="fas fa-headphones"></i> Listen Along</button>`;
                                npText = `<div style="font-size:12px; font-weight:600; color:var(--accent); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; width: 170px; margin-top: 4px;"><i class="fas fa-play" style="font-size:9px; margin-right:4px;"></i> ${songMeta.title.replace(/</g, '&lt;')}</div>${listenAlongBtn}`;
                            }
                        }

                        html += `
                        <div class="friend-item ${activeClass}" onclick="openChat('${f.username}', '${pfp}')">
                            <div class="friend-item-info">
                                <img src="${pfp}" class="friend-pfp">
                                <div style="display:flex; flex-direction:column;">
                                    <span style="font-weight:700; font-size:15px;">${f.username}</span>
                                    ${npText}
                                </div>
                            </div>
                        </div>`;
                    });
                } else {
                    html += `<div style="padding:16px; color:rgba(255,255,255,0.3); font-size:13px; font-weight:600; text-align:center;">No friends yet. Send a request!</div>`;
                }
                
                let container = document.getElementById('friends-list-container');
                if(container) container.innerHTML = html;
            });
        }

        function sendFriendRequest() {
            let target = document.getElementById('add-friend-input').value.trim();
            if(!target) return;
            
            fetch('/api/social/request', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({target_username: target})
            }).then(res => res.json()).then(data => {
                if(data.success) {
                    alert("Friend request sent!");
                    document.getElementById('add-friend-input').value = '';
                } else {
                    alert(data.error);
                }
            });
        }

        function acceptFriend(target) {
            fetch('/api/social/accept', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({target_username: target})
            }).then(res => res.json()).then(data => {
                if(data.success) refreshFriendsList();
            });
        }

        function openChat(friendUsername, friendPfp) {
            currentChatFriend = friendUsername;
            refreshFriendsList();
            
            document.getElementById('chat-area').innerHTML = `
                <div class="chat-header">
                    <img src="${friendPfp}" class="friend-pfp" style="width:48px; height:48px;">
                    <div style="font-size:20px; font-weight:800; letter-spacing:-0.5px;">${friendUsername}</div>
                </div>
                <div class="chat-history" id="chat-history-container">
                    <div style="text-align:center; padding:40px; color:rgba(255,255,255,0.3); font-weight:600;"><i class="fas fa-spinner fa-spin"></i> Loading messages...</div>
                </div>
                <div class="chat-input-area">
                    <input type="text" id="chat-msg-input" class="chat-input" placeholder="Type a message..." onkeypress="if(event.key === 'Enter') sendMessage()">
                    <button class="chat-send-btn" onclick="sendMessage()"><i class="fas fa-paper-plane"></i></button>
                </div>
            `;
            
            loadChatHistory();
            
            if(messagePollInterval) clearInterval(messagePollInterval);
            messagePollInterval = setInterval(() => {
                if(currentChatFriend) {
                    loadChatHistory(true);
                    refreshFriendsList();
                }
            }, 3000);
        }

        function loadChatHistory(isPolling = false) {
            if(!currentChatFriend) return;
            fetch(`/api/social/messages/${encodeURIComponent(currentChatFriend)}`)
                .then(res => res.json())
                .then(data => {
                    let container = document.getElementById('chat-history-container');
                    if(!container) return;
                    
                    let html = '';
                    if(data.messages.length === 0) {
                        html = `<div style="text-align:center; color:rgba(255,255,255,0.3); font-weight:600; margin-top:auto; margin-bottom:auto;">Say hi to ${currentChatFriend}!</div>`;
                    } else {
                        data.messages.forEach(m => {
                            let isMe = m.from === currentSessionUser;
                            let bubbleClass = isMe ? 'sent' : 'received';
                            let timeStr = new Date(m.timestamp * 1000).toLocaleTimeString([], {hour: '2-digit', minute:'2-digit'});
                            html += `
                            <div class="chat-bubble ${bubbleClass}">
                                <div>${m.msg.replace(/</g, '&lt;').replace(/>/g, '&gt;')}</div>
                                <div class="chat-time">${timeStr}</div>
                            </div>`;
                        });
                    }
                    
                    let shouldScroll = !isPolling || (container.scrollHeight - container.scrollTop <= container.clientHeight + 50);
                    container.innerHTML = html;
                    if(shouldScroll) container.scrollTop = container.scrollHeight;
                });
        }

        function sendMessage() {
            let input = document.getElementById('chat-msg-input');
            let msg = input.value.trim();
            if(!msg || !currentChatFriend) return;
            
            input.value = '';
            
            fetch(`/api/social/messages/${encodeURIComponent(currentChatFriend)}`, {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({msg: msg})
            }).then(() => loadChatHistory());
        }

        function renderSettings() {
            let html = `
                <div class="fade-in">
                <h2 style="font-size:36px;"><i class="fas fa-cog" style="color:var(--accent); font-size:28px; margin-right:12px;"></i>Settings</h2>
                
                <div class="admin-card" style="margin-bottom:30px;">
                    <h3 style="margin-top:0; font-size:18px; font-weight:800;">Profile Customization</h3>
                    <form onsubmit="changeUsername(event)" style="display:flex; flex-direction:column; gap:12px; max-width: 380px; margin-bottom: 28px;">
                        <div>
                            <div style="font-size:13px; color:var(--subtext); margin-bottom:6px; font-weight:600; text-transform:uppercase; letter-spacing:1px;">Change Username</div>
                            <input type="text" id="new-username-input" required style="margin:0; padding:12px 16px; background:rgba(0,0,0,0.3); border:1px solid rgba(255,255,255,0.1); border-radius:8px; color:white; width:100%; font-family:'Outfit', sans-serif;">
                        </div>
                        <button type="submit" class="action-btn" style="background:var(--accent); color:black; padding:12px; font-weight:700; margin-top:4px;">Update Username</button>
                    </form>

                    <form onsubmit="updateProfile(event)" style="display:flex; flex-direction:column; gap:12px; max-width: 380px;">
                        <div>
                            <div style="font-size:13px; color:var(--subtext); margin-bottom:6px; font-weight:600; text-transform:uppercase; letter-spacing:1px;">Profile Picture</div>
                            <input type="file" id="pfp-input" accept="image/*" style="margin:0; padding:12px 16px; background:rgba(0,0,0,0.3); border:1px solid rgba(255,255,255,0.1); border-radius:8px; color:white; width:100%; font-family:'Outfit', sans-serif;">
                        </div>
                        <div>
                            <div style="font-size:13px; color:var(--subtext); margin-bottom:6px; font-weight:600; text-transform:uppercase; letter-spacing:1px;">Theme Color</div>
                            <input type="color" id="bg-color-input" value="${getComputedStyle(document.documentElement).getPropertyValue('--bg').trim()}" style="margin:0; background:rgba(0,0,0,0.3); border:1px solid rgba(255,255,255,0.1); border-radius:8px; cursor:pointer; width:100%; height:45px;">
                        </div>
                        <button type="submit" class="action-btn" style="background:var(--accent); color:black; padding:12px; font-weight:700; margin-top:4px;">Save Customizations</button>
                    </form>
                </div>

                <div class="admin-card" style="margin-bottom:30px;">
                    <h3 style="margin-top:0; font-size:18px; font-weight:800;">Change Password</h3>
                    <form onsubmit="changePassword(event)" style="display:flex; flex-direction:column; gap:12px; max-width: 380px;">
                        <div>
                            <div style="font-size:13px; color:var(--subtext); margin-bottom:6px; font-weight:600; text-transform:uppercase; letter-spacing:1px;">Current Password</div>
                            <input type="password" id="curr-pass" required style="margin:0; padding:12px 16px; background:rgba(0,0,0,0.3); border:1px solid rgba(255,255,255,0.1); border-radius:8px; color:white; width:100%; font-family:'Outfit', sans-serif;">
                        </div>
                        <div>
                            <div style="font-size:13px; color:var(--subtext); margin-bottom:6px; font-weight:600; text-transform:uppercase; letter-spacing:1px;">New Password</div>
                            <input type="password" id="new-pass-user" required style="margin:0; padding:12px 16px; background:rgba(0,0,0,0.3); border:1px solid rgba(255,255,255,0.1); border-radius:8px; color:white; width:100%; font-family:'Outfit', sans-serif;">
                        </div>
                        <button type="submit" class="action-btn" style="background:var(--text); color:black; padding:12px; font-weight:700; margin-top:4px;">Update Password</button>
                    </form>
                </div>
            `;

            if (currentUserIsAdmin) {
                html += `
                <div class="admin-card" style="margin-bottom:30px; border: 1px solid rgba(29, 185, 84, 0.4); background: rgba(29, 185, 84, 0.05);">
                    <h3 style="margin-top:0; font-size:20px; font-weight:800; color:var(--accent); letter-spacing:-0.5px;"><i class="fas fa-cloud-upload-alt" style="margin-right:10px;"></i>Upload Music</h3>
                    <p style="font-size:14px; color:var(--text); opacity:0.8; margin-top:0; font-weight:500;">Upload MP3 or FLAC files directly to your server's persistent storage.</p>
                    <form onsubmit="uploadMusic(event)" style="display:flex; flex-direction:column; gap:16px;">
                        <input type="file" id="music-upload-input" accept="audio/mpeg, audio/flac, audio/ogg, audio/wav, audio/mp4" multiple style="margin:0; padding:16px; background:rgba(0,0,0,0.4); border:1px solid rgba(29, 185, 84, 0.3); border-radius:10px; color:white; width:100%; font-family:'Outfit', sans-serif; cursor:pointer;">
                        <button type="submit" class="action-btn" style="background:var(--accent); color:black; padding:14px; font-size:16px; font-weight:800; box-shadow: 0 5px 15px rgba(29, 185, 84, 0.3);">Upload Tracks</button>
                    </form>
                    <div id="upload-status" style="margin-top: 14px; font-size: 14px; font-weight: 700;"></div>
                </div>

                <h2 style="margin-top:50px; font-size:32px;"><i class="fas fa-users-cog" style="color:var(--accent); font-size:24px; margin-right:12px;"></i>User Management</h2>
                <div class="admin-card" style="margin-bottom:30px;">
                    <h3 style="margin-top:0; font-size:18px; font-weight:800;">Create New Account</h3>
                    <form onsubmit="createUser(event)" style="display:flex; gap:12px; align-items:center; flex-wrap:wrap;">
                        <input type="text" id="new-user" placeholder="Username" required style="margin:0; flex:1; min-width:180px; padding:12px 16px; background:rgba(0,0,0,0.3); border:1px solid rgba(255,255,255,0.1); border-radius:8px; color:white; font-family:'Outfit', sans-serif;">
                        <input type="password" id="new-pass" placeholder="Password" required style="margin:0; flex:1; min-width:180px; padding:12px 16px; background:rgba(0,0,0,0.3); border:1px solid rgba(255,255,255,0.1); border-radius:8px; color:white; font-family:'Outfit', sans-serif;">
                        
                        <select id="new-role" style="padding:12px 16px; background:rgba(0,0,0,0.3); border:1px solid rgba(255,255,255,0.1); border-radius:8px; color:white; outline:none; cursor:pointer; font-family:'Outfit', sans-serif; font-weight:600;">
                            <option value="user">Standard User</option>
                            <option value="admin">Administrator</option>
                        </select>
                        
                        <button type="submit" class="action-btn" style="background:var(--accent); color:black; padding:12px 20px; font-weight:700;">Create Account</button>
                    </form>
                </div>
                <div class="admin-card" style="max-width:100%; overflow-x: auto;">
                    <h3 style="margin-top:0; font-size:18px; font-weight:800;">Existing Accounts</h3>
                    <div id="users-table-container">Loading users...</div>
                </div>
                `;
            }

            html += `</div>`;
            contentDiv.innerHTML = html;
            if (currentUserIsAdmin) loadUsersTable();
        }

        function uploadMusic(e) {
            e.preventDefault();
            let fileInput = document.getElementById('music-upload-input');
            if (fileInput.files.length === 0) return alert("Select files first.");
            
            let formData = new FormData();
            for(let i=0; i<fileInput.files.length; i++) {
                formData.append('files', fileInput.files[i]);
            }
            
            let status = document.getElementById('upload-status');
            status.innerHTML = `<i class="fas fa-spinner fa-spin"></i> Uploading ${fileInput.files.length} files... Do not close this page.`;
            
            fetch('/api/admin/upload', {
                method: 'POST',
                body: formData
            }).then(res => res.json()).then(data => {
                if(data.success) {
                    status.innerHTML = `<span style="color:var(--accent)"><i class="fas fa-check"></i> Successfully uploaded ${data.saved} tracks! Reloading...</span>`;
                    setTimeout(() => window.location.reload(), 1500);
                } else {
                    status.innerHTML = `<span style="color:#ff5555"><i class="fas fa-times"></i> ${data.error}</span>`;
                }
            }).catch(err => {
                status.innerHTML = `<span style="color:#ff5555"><i class="fas fa-times"></i> Upload failed. Files might be too large.</span>`;
            });
        }

        function changeUsername(e) {
            e.preventDefault();
            let new_name = document.getElementById('new-username-input').value;
            fetch('/api/settings/username', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({new_username: new_name})
            }).then(res => res.json()).then(data => {
                if (data.success) {
                    alert("Username changed successfully! Reloading...");
                    window.location.reload();
                } else {
                    alert(data.error || "Failed to change username.");
                }
            });
        }

        function updateProfile(e) {
            e.preventDefault();
            let formData = new FormData();
            let fileInput = document.getElementById('pfp-input');
            if (fileInput.files.length > 0) {
                formData.append('pfp', fileInput.files[0]);
            }
            formData.append('bg_color', document.getElementById('bg-color-input').value);

            fetch('/api/settings/profile', {
                method: 'POST',
                body: formData
            }).then(res => res.json()).then(data => {
                if (data.success) {
                    alert("Profile updated successfully!");
                    window.location.reload();
                } else {
                    alert("Failed to update profile.");
                }
            });
        }

        function changePassword(e) {
            e.preventDefault();
            let curr = document.getElementById('curr-pass').value;
            let newp = document.getElementById('new-pass-user').value;
            fetch('/api/settings/password', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({current_password: curr, new_password: newp})
            }).then(res => res.json()).then(data => {
                if(data.success) { alert("Password updated successfully!"); document.getElementById('curr-pass').value = ''; document.getElementById('new-pass-user').value = '';}
                else { alert(data.error || "Failed to update password"); }
            });
        }

        function loadUsersTable() {
            fetch('/api/admin/users').then(res => res.json()).then(data => {
                let html = `<table class="admin-table"><tr><th>Username</th><th>Role</th><th style="text-align:right;">Actions</th></tr>`;
                for (let [uname, udata] of Object.entries(data)) {
                    let roleBadge = udata.is_admin ? '<span class="badge-admin">Administrator</span>' : '<span class="badge-user">Standard User</span>';
                    html += `<tr>
                        <td><strong style="font-size:16px;">${uname}</strong></td>
                        <td>${roleBadge}</td>
                        <td style="text-align:right;">
                            <button class="action-btn danger" onclick="deleteUser('${uname}')"><i class="fas fa-trash"></i> Delete</button>
                        </td>
                    </tr>`;
                }
                html += `</table>`;
                document.getElementById('users-table-container').innerHTML = html;
            });
        }

        function createUser(e) {
            e.preventDefault();
            let username = document.getElementById('new-user').value;
            let password = document.getElementById('new-pass').value;
            let is_admin = document.getElementById('new-role').value === 'admin';

            fetch('/api/admin/users', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({username, password, is_admin})
            }).then(res => res.json()).then(data => {
                if(data.success) {
                    document.getElementById('new-user').value = '';
                    document.getElementById('new-pass').value = '';
                    document.getElementById('new-role').value = 'user';
                    loadUsersTable();
                } else { alert(data.error || "Failed to create user"); }
            });
        }

        function deleteUser(username) {
            if(!confirm(`Are you sure you want to delete user '${username}'?`)) return;
            fetch('/api/admin/users', {
                method: 'DELETE',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({username})
            }).then(() => loadUsersTable());
        }

        function sendFeedback(action) {
            if(!currentSongObj) return;
            fetch('/api/feedback', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({action: action, song: currentSongObj.filename})
            }).then(res => res.json()).then(data => {
                if(action === 'like') {
                    document.getElementById('like-btn').classList.toggle('active');
                    document.getElementById('dislike-btn').classList.remove('active');
                    animateButton('like-btn');
                } else if(action === 'dislike') {
                    document.getElementById('dislike-btn').classList.toggle('active');
                    document.getElementById('like-btn').classList.remove('active');
                    animateButton('dislike-btn');
                    if(isRadioMode) nextTrack();
                }
                refreshStatsUI();
            });
        }

        function fetchStatusAndLog(filename) {
            fetch('/api/status?song=' + encodeURIComponent(filename))
                .then(res => res.json())
                .then(data => {
                    document.getElementById('like-btn').classList.toggle('active', data.liked);
                    document.getElementById('dislike-btn').classList.toggle('active', data.disliked);
                });

            fetch('/api/social/status', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({song: filename})
            });

            fetch('/api/feedback', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({action: 'listen', song: filename})
            }).then(() => refreshStatsUI());
        }

        function refreshStatsUI() {
            fetch('/api/data').then(res => res.json()).then(data => {
                songStats = data.stats;
                data.songs.forEach(song => {
                    const el = document.getElementById(`stats-${safeId(song.filename)}`);
                    if (el) {
                        let s = songStats[song.filename] || {likes: 0, dislikes: 0};
                        el.innerHTML = `<span class="stat-like"><i class="fas fa-heart" style="color:var(--accent)"></i> ${s.likes}</span>`;
                    }
                });
            });
        }

        audio.addEventListener('ended', nextTrack);
    </script>
</body>
</html>
"""

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

if __name__ == '__main__':
    print(f"🎵 App running on port {PORT}! Open http://localhost:{PORT}")
    app.run(host='0.0.0.0', port=PORT)