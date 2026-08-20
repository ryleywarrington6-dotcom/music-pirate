import os
import sys
import json
import random
import re
import urllib.request
import urllib.parse
from flask import Flask, request, session, redirect, url_for, render_template_string, jsonify, send_from_directory, Response
from werkzeug.security import generate_password_hash, check_password_hash

try:
    import mutagen 
except ImportError:
    print("\n❌ ERROR: Required library 'mutagen' is not installed!")
    print("Run this command in your terminal to fix it:\n")
    print("    python -m pip install mutagen\n")
    sys.exit(1)

PORT = int(os.environ.get("PORT", 8000))
MUSIC_DIR = "/workspaces/music-pirate/Music"
os.makedirs(MUSIC_DIR, exist_ok=True) 

DB_FILE = 'database.json'
COVERS_FILE = 'covers_v7.json' 
METADATA_FILE = 'metadata_v4.json'

app = Flask(__name__)
app.secret_key = 'super-secret-production-key-change-me'

# ---------------------------------------------------------
# DATABASE & CACHE HELPERS
# ---------------------------------------------------------
def load_json_file(filepath, default_data):
    if not os.path.exists(filepath): return default_data
    try:
        with open(filepath, 'r') as f: return json.load(f)
    except Exception: return default_data

def save_json_file(filepath, data):
    with open(filepath, 'w') as f: json.dump(data, f, indent=2)

def load_db(): return load_json_file(DB_FILE, {"users": {}})
def save_db(db): save_json_file(DB_FILE, db)

cover_cache = load_json_file(COVERS_FILE, {})
meta_cache = load_json_file(METADATA_FILE, {})

def get_all_filepaths():
    audio_files = []
    for root, dirs, files in os.walk(MUSIC_DIR):
        for file in files:
            if file.lower().endswith(('.mp3', '.flac')):
                full_path = os.path.join(root, file)
                rel_path = os.path.relpath(full_path, MUSIC_DIR)
                audio_files.append(rel_path)
    return sorted(audio_files)

def has_embedded_art(filepath):
    try:
        audio = mutagen.File(filepath)
        if audio is None: return False
        if hasattr(audio, 'tags') and audio.tags:
            for key in audio.tags.keys():
                if key.startswith('APIC'): return True
        if hasattr(audio, 'pictures') and audio.pictures: return True
    except: pass
    return False

def parse_folder_and_filename(rel_path):
    parts = rel_path.split(os.sep)
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

def extract_audio_tags(full_path, rel_path):
    artist, title = None, None
    try:
        audio = mutagen.File(full_path)
        if audio is not None:
            if 'TPE1' in audio: artist = str(audio['TPE1'])
            elif 'TPE2' in audio: artist = str(audio['TPE2'])
            if 'TIT2' in audio: title = str(audio['TIT2'])

            if not artist:
                if 'artist' in audio:
                    val = audio['artist']
                    artist = val[0] if isinstance(val, list) else str(val)
                elif 'albumartist' in audio:
                    val = audio['albumartist']
                    artist = val[0] if isinstance(val, list) else str(val)

            if not title:
                if 'title' in audio:
                    val = audio['title']
                    title = val[0] if isinstance(val, list) else str(val)
    except Exception: pass

    folder_artist, fallback_title = parse_folder_and_filename(rel_path)
    artist = artist or folder_artist
    title = title or fallback_title

    artist = artist.strip() if artist else "Unknown Artist"
    title = title.strip() if title else os.path.splitext(os.path.basename(rel_path))[0]

    return artist, title

def get_song_metadata(rel_path):
    full_path = os.path.join(MUSIC_DIR, rel_path)
    mtime = os.path.getmtime(full_path) if os.path.exists(full_path) else 0

    if rel_path in meta_cache:
        if meta_cache[rel_path].get('mtime') == mtime:
            return meta_cache[rel_path]
        
    artist, title = extract_audio_tags(full_path, rel_path)
    
    meta_cache[rel_path] = {
        "filename": rel_path,
        "artist": artist,
        "title": title,
        "has_cover": has_embedded_art(full_path),
        "mtime": mtime
    }
    save_json_file(METADATA_FILE, meta_cache)
    return meta_cache[rel_path]

def get_all_songs_enriched():
    files = get_all_filepaths()
    return [get_song_metadata(f) for f in files]

# ---------------------------------------------------------
# EXTERNAL COVER ART FETCHERS
# ---------------------------------------------------------
def fetch_itunes_cover(artist, song):
    query = f"{artist} {song}".strip() if artist != "Unknown Artist" else song.strip()
    try:
        url = f"https://itunes.apple.com/search?term={urllib.parse.quote(query)}&media=music&entity=song&limit=5"
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            data = json.loads(resp.read().decode())
            if data.get('resultCount', 0) > 0:
                for res in data['results']:
                    api_artist = res.get('artistName', '').lower()
                    if artist != "Unknown Artist" and (artist.lower() in api_artist or api_artist in artist.lower()):
                        return res['artworkUrl100'].replace('100x100bb', '500x500bb')
                return data['results'][0]['artworkUrl100'].replace('100x100bb', '500x500bb')
    except Exception: pass
    return None

def fetch_musicbrainz_cover(artist, song):
    try:
        q = f'artist:"{artist}" AND recording:"{song}"' if artist != "Unknown Artist" else f'recording:"{song}"'
        url = f"https://musicbrainz.org/ws/2/recording/?query={urllib.parse.quote(q)}&fmt=json&limit=1"
        req = urllib.request.Request(url, headers={'User-Agent': 'StreamerApp/1.0'})
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            data = json.loads(resp.read().decode())
            if data.get('recordings'):
                for release in data['recordings'][0].get('releases', []):
                    release_id = release.get('id')
                    if release_id:
                        return f"https://coverartarchive.org/release/{release_id}/front-500"
    except Exception: pass
    return None

def generate_svg_fallback(song_name):
    clean_title = urllib.parse.quote(song_name[:15] if song_name else "Music")
    svg = f"""data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='300' height='300' viewBox='0 0 300 300'><rect width='300' height='300' fill='%23121212'/><circle cx='150' cy='150' r='80' fill='%23222222'/><text x='50%' y='48%' fill='%231DB954' font-size='48' font-family='sans-serif' text-anchor='middle' dominant-baseline='middle'>🎵</text><text x='50%' y='80%' fill='%23b3b3b3' font-size='14' font-family='sans-serif' text-anchor='middle'>{clean_title}</text></svg>"""
    return svg

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

def get_radio_recommendation(username):
    db = load_db()
    user = db["users"].get(username, {})
    likes = set(user.get("likes", []))
    dislikes = set(user.get("dislikes", []))
    play_counts = user.get("play_counts", {})
    all_files = get_all_filepaths()
    valid_songs = [s for s in all_files if s not in dislikes]
    if not valid_songs: return random.choice(all_files) if all_files else None

    weights = []
    for song in valid_songs:
        weight = 1.0 
        if song in likes: weight += 5.0 
        weight += (play_counts.get(song, 0) * 0.2) 
        weights.append(weight)
    recommended_filename = random.choices(valid_songs, weights=weights, k=1)[0]
    return get_song_metadata(recommended_filename)

# ---------------------------------------------------------
# HIGH-END HTML & UI TEMPLATE
# ---------------------------------------------------------
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Streamer Pro</title>
    <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css" rel="stylesheet">
    <style>
        :root { --bg: #050505; --panel: #121212; --highlight: #222222; --text: #ffffff; --subtext: #a7a7a7; --accent: #1DB954; --card-bg: #181818; }
        * { box-sizing: border-box; }
        
        /* Subtle radial gradient for depth */
        body { font-family: 'Segoe UI', system-ui, -apple-system, sans-serif; background: radial-gradient(circle at top left, #1a1a1a 0%, var(--bg) 100%); color: var(--text); margin: 0; overflow: hidden; display: flex; height: 100vh; }
        
        /* Sidebar Navigation */
        .sidebar { width: 240px; background: transparent; padding: 24px 16px; display: flex; flex-direction: column; gap: 8px; flex-shrink: 0; z-index: 10; }
        .logo { font-size: 20px; font-weight: 800; padding: 0 12px; margin-bottom: 16px; display: flex; align-items: center; gap: 10px; letter-spacing: 0.5px; }
        .nav-section-title { font-size: 11px; text-transform: uppercase; color: var(--subtext); letter-spacing: 1.2px; padding: 12px 12px 4px 12px; font-weight: 700; }
        .nav-item { padding: 10px 12px; border-radius: 6px; cursor: pointer; color: var(--subtext); font-weight: 600; font-size: 14px; display: flex; align-items: center; gap: 15px; transition: all 0.2s ease; }
        .nav-item:hover, .nav-item.active { color: var(--text); background: var(--highlight); }
        .nav-item i { font-size: 18px; width: 20px; text-align: center; }
        
        /* Main Layout */
        .center-wrapper { flex: 1; display: flex; flex-direction: column; overflow: hidden; background: var(--panel); border-radius: 12px; margin: 8px 8px 96px 0; position: relative; box-shadow: -5px 0 25px rgba(0,0,0,0.5); }
        .top-bar { height: 64px; background: rgba(18,18,18,0.7); backdrop-filter: blur(20px); display: flex; align-items: center; justify-content: space-between; padding: 0 32px; position: absolute; top: 0; left: 0; right: 0; z-index: 50; border-radius: 12px 12px 0 0; }
        .main-content { flex: 1; padding: 84px 32px 32px 32px; overflow-y: auto; }
        
        /* Fade in animation for view swapping */
        @keyframes fadeIn { from { opacity: 0; transform: translateY(10px); } to { opacity: 1; transform: translateY(0); } }
        .fade-in { animation: fadeIn 0.4s cubic-bezier(0.16, 1, 0.3, 1) forwards; }
        
        /* Right Sidebar Panel */
        .right-panel { width: 340px; background: var(--panel); border-radius: 12px; margin: 8px 8px 96px 0; padding: 24px; display: none; flex-direction: column; align-items: center; text-align: center; overflow: hidden; transition: 0.3s; flex-shrink: 0; box-shadow: -5px 0 25px rgba(0,0,0,0.5); }
        
        /* Player Bar */
        .player-bar { position: fixed; bottom: 0; left: 0; right: 0; height: 88px; background: #000; border-top: 1px solid #222; display: flex; align-items: center; padding: 0 24px; justify-content: space-between; z-index: 1000; }
        
        /* Search Box */
        .search-container { display: flex; align-items: center; background: #242424; border-radius: 24px; padding: 8px 16px; width: 320px; border: 1px solid transparent; transition: 0.2s; }
        .search-container:focus-within { border-color: #555; background: #2a2a2a; }
        .search-container i { color: var(--subtext); font-size: 14px; margin-right: 10px; }
        .search-container input { background: transparent; border: none; color: white; width: 100%; outline: none; font-size: 14px; }
        
        /* Dropdown Menu */
        .user-badge-wrapper { position: relative; display: inline-block; }
        .user-badge { display: flex; align-items: center; gap: 10px; font-size: 14px; font-weight: 600; background: #1a1a1a; padding: 6px 14px; border-radius: 20px; border: 1px solid #333; cursor: pointer; transition: 0.2s; }
        .user-badge:hover { background: #2a2a2a; border-color: #555; }
        .settings-dropdown { display: none; position: absolute; right: 0; top: 48px; background: #282828; border-radius: 8px; width: 200px; box-shadow: 0 10px 25px rgba(0,0,0,0.7); z-index: 100; overflow: hidden; }
        .settings-dropdown.show { display: block; }
        .dropdown-item { padding: 12px 16px; font-size: 13px; font-weight: 600; color: var(--subtext); display: flex; align-items: center; gap: 12px; cursor: pointer; text-decoration: none; transition: 0.2s; }
        .dropdown-item:hover { background: #333; color: var(--text); }
        .dropdown-item i { width: 16px; text-align: center; }
        .dropdown-divider { height: 1px; background: #3d3d3d; margin: 4px 0; }

        h2 { font-size: 26px; font-weight: 700; margin-top: 0; margin-bottom: 24px; letter-spacing: -0.5px; }
        .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); gap: 20px; margin-bottom: 40px; }
        .scroll-row { display: flex; gap: 20px; overflow-x: auto; padding-bottom: 16px; margin-bottom: 36px; scroll-snap-type: x mandatory; }
        .scroll-row .card { min-width: 180px; flex-shrink: 0; scroll-snap-align: start; }
        
        /* Cards */
        .card { background: var(--card-bg); padding: 14px; border-radius: 8px; cursor: pointer; transition: all 0.3s cubic-bezier(0.16, 1, 0.3, 1); position: relative; text-align: left; }
        .card:hover { background: #282828; transform: translateY(-6px); box-shadow: 0 12px 30px rgba(0,0,0,0.6); }
        .card-img-container { width: 100%; aspect-ratio: 1; background: #222; border-radius: 6px; margin-bottom: 12px; position: relative; overflow: hidden; display: flex; align-items: center; justify-content: center; font-size: 36px; color: #444; box-shadow: 0 4px 12px rgba(0,0,0,0.4); }
        .card-img-container img { width: 100%; height: 100%; object-fit: cover; position: absolute; top: 0; left: 0; }
        
        /* Play Overlay Button */
        .card-play-overlay { position: absolute; bottom: 10px; right: 10px; background: var(--accent); color: #000; width: 44px; height: 44px; border-radius: 50%; display: flex; align-items: center; justify-content: center; opacity: 0; transform: translateY(10px); transition: all 0.3s cubic-bezier(0.175, 0.885, 0.32, 1.275); box-shadow: 0 8px 15px rgba(0,0,0,0.5); z-index: 2;}
        .card:hover .card-play-overlay { opacity: 1; transform: translateY(0); }
        .card-play-overlay:hover { transform: scale(1.1) !important; background: #1ed760; }

        .card-info { display: flex; flex-direction: column; gap: 4px; }
        .card-title { font-weight: 700; font-size: 15px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        .card-bottom-row { display: flex; justify-content: space-between; align-items: center; margin-top: 2px; }
        .card-artist { font-weight: 500; color: var(--subtext); font-size: 13px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; flex: 1; padding-right: 8px; }
        .card-stats { display: flex; gap: 8px; font-size: 11px; color: var(--subtext); }
        
        /* Bottom Player - 3 Sections */
        .now-playing-info { width: 30%; display: flex; align-items: center; gap: 14px; }
        .np-cover { width: 56px; height: 56px; background: #222; border-radius: 6px; object-fit: cover; box-shadow: 0 4px 12px rgba(0,0,0,0.5); }
        .np-text { display: flex; flex-direction: column; overflow: hidden; }
        .np-title { font-weight: 700; font-size: 14px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        .np-artist { color: var(--subtext); font-size: 12px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; margin-top: 2px; }
        
        .controls { width: 40%; display: flex; flex-direction: column; align-items: center; gap: 8px; }
        .buttons { display: flex; gap: 20px; align-items: center; }
        .volume-controls { display: flex; align-items: center; gap: 12px; width: 30%; justify-content: flex-end; color: var(--subtext); }
        
        @keyframes pop { 0% { transform: scale(1); } 50% { transform: scale(1.35); } 100% { transform: scale(1); } }
        .pop-anim { animation: pop 0.35s cubic-bezier(0.175, 0.885, 0.32, 1.275); }
        
        .btn { background: none; border: none; color: var(--subtext); font-size: 16px; cursor: pointer; transition: color 0.2s; outline: none; }
        .btn:hover { color: var(--text); }
        .btn.active#like-btn i, .btn.active.shuffle i, .btn.active.repeat i { color: var(--accent) !important; }
        .btn.active#dislike-btn i { color: #ff5555 !important; } 
        .btn.play-btn { background: var(--text); color: var(--bg); height: 34px; width: 34px; border-radius: 50%; display: flex; align-items: center; justify-content: center; transition: transform 0.2s; }
        .btn.play-btn:hover { transform: scale(1.08); color: var(--bg); }
        audio { display: none; }
        
        /* Custom Input Ranges */
        input[type=range] { -webkit-appearance: none; background: #4d4d4d; height: 4px; border-radius: 2px; outline: none; cursor: pointer; width: 100%; transition: 0.1s; }
        input[type=range]:hover { height: 6px; }
        input[type=range]::-webkit-slider-thumb { -webkit-appearance: none; width: 12px; height: 12px; border-radius: 50%; background: #fff; opacity: 0; transition: 0.1s; box-shadow: 0 2px 4px rgba(0,0,0,0.5); }
        input[type=range]:hover::-webkit-slider-thumb { opacity: 1; }
        
        .lyrics-container { width: 100%; flex: 1; overflow-y: auto; text-align: left; margin-top: 15px; padding-bottom: 40px; scroll-behavior: smooth; mask-image: linear-gradient(to bottom, transparent 0%, black 15%, black 85%, transparent 100%); -webkit-mask-image: linear-gradient(to bottom, transparent 0%, black 15%, black 85%, transparent 100%); }
        .lyric-line { font-size: 17px; color: rgba(255, 255, 255, 0.35); padding: 8px 12px; margin: 4px 0; border-radius: 6px; transition: all 0.3s ease; font-weight: 600; cursor: pointer; }
        .lyric-line:hover { color: rgba(255, 255, 255, 0.8); background: rgba(255,255,255,0.03); }
        .lyric-line.active { color: var(--accent); font-size: 21px; font-weight: 700; text-shadow: 0 0 15px rgba(29, 185, 84, 0.4); transform: translateX(4px); opacity: 1; }
        
        .admin-card { background: #181818; border: 1px solid #282828; border-radius: 10px; padding: 24px; margin-bottom: 24px; max-width: 700px; }
        .admin-table { width: 100%; border-collapse: collapse; margin-top: 16px; font-size: 14px; text-align: left; }
        .admin-table th, .admin-table td { padding: 12px 14px; border-bottom: 1px solid #222; }
        .admin-table th { color: var(--subtext); font-weight: 600; }
        .badge-admin { background: rgba(29,185,84,0.15); color: var(--accent); padding: 3px 8px; border-radius: 4px; font-size: 11px; font-weight: 700; }
        .badge-user { background: rgba(255,255,255,0.08); color: var(--subtext); padding: 3px 8px; border-radius: 4px; font-size: 11px; font-weight: 600; }
        .action-btn { background: #282828; color: white; border: none; padding: 6px 12px; border-radius: 4px; cursor: pointer; font-size: 12px; font-weight: 600; transition: 0.2s; }
        .action-btn:hover { background: #383838; }
        .action-btn.danger { background: rgba(255,85,85,0.15); color: #ff5555; }
        .action-btn.danger:hover { background: rgba(255,85,85,0.3); }

        ::-webkit-scrollbar { width: 8px; height: 8px; }
        ::-webkit-scrollbar-track { background: transparent; }
        ::-webkit-scrollbar-thumb { background: rgba(255,255,255,0.2); border-radius: 4px; }
        ::-webkit-scrollbar-thumb:hover { background: rgba(255,255,255,0.3); }
        
        .auth-container { width: 100%; height: 100vh; display: flex; align-items: center; justify-content: center; background: var(--bg); }
        .auth-card { background: var(--panel); padding: 40px; border-radius: 12px; text-align: center; width: 340px; border: 1px solid #222; box-shadow: 0 15px 35px rgba(0,0,0,0.8); }
        .auth-card h2 { margin-bottom: 20px; font-size: 24px; }
        input[type=text], input[type=password] { width: 100%; padding: 12px 16px; margin: 8px 0 16px 0; border-radius: 6px; border: 1px solid #333; background: #1a1a1a; color: white; font-size: 14px; outline: none; transition: 0.2s; }
        input[type=text]:focus, input[type=password]:focus { border-color: var(--accent); }
        button.primary { background: var(--accent); color: black; border: none; padding: 12px 24px; border-radius: 24px; font-weight: 700; cursor: pointer; margin-top: 10px; width: 100%; font-size: 15px; transition: 0.2s; }
        button.primary:hover { transform: scale(1.02); background: #1ed760; }
    </style>
</head>
<body>

{% if not logged_in %}
    <div class="auth-container">
        <div class="auth-card">
            <h2>{{ 'Setup Admin Account' if setup else 'Welcome Back' }}</h2>
            <form method="POST">
                <div style="text-align: left; font-size: 13px; color: var(--subtext); margin-bottom: 4px;">Username</div>
                <input type="text" name="username" required>
                <div style="text-align: left; font-size: 13px; color: var(--subtext); margin-bottom: 4px;">Password</div>
                <input type="password" name="password" required>
                <button type="submit" class="primary">{{ 'Create Master Admin' if setup else 'Log In' }}</button>
            </form>
        </div>
    </div>
{% else %}
    <div class="sidebar">
        <div class="logo"><i class="fab fa-spotify" style="color:var(--accent)"></i> Streamer</div>
        
        <div class="nav-section-title">Discover</div>
        <div class="nav-item active" onclick="switchView('home')"><i class="fas fa-home"></i> Home</div>
        <div class="nav-item" onclick="document.getElementById('global-search').focus();"><i class="fas fa-search"></i> Search</div>
        <div class="nav-item" onclick="switchView('artists')"><i class="fas fa-microphone"></i> Artists</div>
        <div class="nav-item" onclick="switchView('radio')"><i class="fas fa-broadcast-tower"></i> Infinite Radio</div>
        
        {% if is_admin %}
        <div class="nav-section-title" style="margin-top: 16px;">Administration</div>
        <div class="nav-item" onclick="switchView('admin')"><i class="fas fa-users-cog"></i> Account Manager</div>
        {% endif %}
    </div>

    <div class="center-wrapper">
        <div class="top-bar">
            <div class="search-container">
                <i class="fas fa-search"></i>
                <input type="text" id="global-search" placeholder="Search songs or artists..." oninput="handleSearch(this.value)">
            </div>
            
            <div class="user-badge-wrapper">
                <div class="user-badge" onclick="toggleSettingsMenu()">
                    <i class="fas fa-user-circle" style="color: var(--accent); font-size:18px;"></i> 
                    {{ session.user }} 
                    <i class="fas fa-caret-down" style="font-size: 11px;"></i>
                </div>
                <div class="settings-dropdown" id="settings-dropdown">
                    <div class="dropdown-item" onclick="openSettingsModal()"><i class="fas fa-sliders-h"></i> Settings</div>
                    {% if is_admin %}
                    <div class="dropdown-item" onclick="switchView('admin'); toggleSettingsMenu();"><i class="fas fa-users-cog"></i> Account Manager</div>
                    {% endif %}
                    <div class="dropdown-divider"></div>
                    <a href="/logout" class="dropdown-item" style="color: #ff5555;"><i class="fas fa-sign-out-alt"></i> Log Out</a>
                </div>
            </div>
        </div>
        <!-- Wrap content in fade-in container for animations -->
        <div class="main-content" id="main-content"></div>
    </div>

    <!-- RIGHT PANEL -->
    <div class="right-panel" id="right-panel">
        <h3 style="margin-top:0; color: var(--subtext); width: 100%; text-align: left; font-size: 12px; letter-spacing: 1px;"><i class="fas fa-compact-disc"></i> NOW PLAYING</h3>
        <img id="rp-cover" src="" style="width: 220px; height: 220px; border-radius: 8px; box-shadow: 0 12px 30px rgba(0,0,0,0.8); margin: 15px 0;">
        <div id="rp-title" style="font-size: 20px; font-weight: 700; margin-bottom: 4px; width: 100%; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;"></div>
        <div id="rp-artist" style="color: var(--subtext); font-weight: 500; font-size: 14px; margin-bottom: 20px; width: 100%; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;"></div>

        <div class="lyrics-container" id="lyrics-container">
            <div style="color:#555; text-align:center; padding-top:40px;">Select a track to load live lyrics.</div>
        </div>
    </div>

    <!-- BOTTOM PLAYER BAR -->
    <div class="player-bar">
        <div class="now-playing-info">
            <img id="np-cover" class="np-cover" src="data:image/gif;base64,R0lGODlhAQABAAD/ACwAAAAAAQABAAACADs=" alt="Cover">
            <div class="np-text">
                <div class="np-title" id="np-title">No track selected</div>
                <div class="np-artist" id="np-artist">-</div>
            </div>
        </div>
        
        <div class="controls">
            <div class="buttons">
                <button class="btn" id="shuffle-btn" onclick="toggleShuffle()"><i class="fas fa-random"></i></button>
                <button class="btn" onclick="prevTrack()"><i class="fas fa-step-backward"></i></button>
                <button class="btn play-btn" id="play-btn-wrapper" onclick="togglePlay()">
                    <i class="fas fa-play" id="play-icon" style="margin-left: 2px;"></i>
                </button>
                <button class="btn" onclick="nextTrack()"><i class="fas fa-step-forward"></i></button>
                <button class="btn" id="repeat-btn" onclick="toggleRepeat()"><i class="fas fa-redo"></i></button>
            </div>
            <div style="width: 100%; display: flex; align-items: center; gap: 10px; font-size: 11px; color: var(--subtext); font-weight:500;">
                <span id="time-current">0:00</span>
                <input type="range" id="progress-bar" value="0" max="100">
                <span id="time-total">0:00</span>
            </div>
            <audio id="audio-player"></audio>
        </div>
        
        <div class="volume-controls">
            <button class="btn" id="dislike-btn" onclick="sendFeedback('dislike')"><i class="fas fa-thumbs-down"></i></button>
            <button class="btn" id="like-btn" onclick="sendFeedback('like')" style="margin-right:10px;"><i class="fas fa-heart"></i></button>
            <i class="fas fa-volume-up" id="mute-icon" onclick="toggleMute()" style="cursor:pointer; width:16px;"></i>
            <input type="range" id="volume-bar" value="100" max="100" style="width: 90px;">
        </div>
    </div>

    <script>
        let allSongs = [];
        let songStats = {};
        let groupedArtists = {};
        let currentQueue = [];
        let originalQueue = [];
        let currentIndex = 0;
        let isRadioMode = false;
        let isShuffle = false;
        let isRepeat = false;
        let currentSongObj = null; 
        let syncedLyrics = [];
        let activeLyricIndex = -1;

        const contentDiv = document.getElementById('main-content');
        const rightPanel = document.getElementById('right-panel');
        const audio = document.getElementById('audio-player');
        const playIcon = document.getElementById('play-icon');
        const progressBar = document.getElementById('progress-bar');
        const volumeBar = document.getElementById('volume-bar');
        const muteIcon = document.getElementById('mute-icon');
        const timeCurrentEl = document.getElementById('time-current');
        const timeTotalEl = document.getElementById('time-total');
        const lyricsContainer = document.getElementById('lyrics-container');

        // Dynamic Slider Color Fill
        function updateSliderFill(el) {
            const val = (el.value - el.min) / (el.max - el.min) * 100;
            el.style.background = `linear-gradient(to right, var(--accent) ${val}%, #4d4d4d ${val}%)`;
        }

        // Settings Dropdown
        function toggleSettingsMenu() {
            document.getElementById('settings-dropdown').classList.toggle('show');
        }
        window.onclick = function(e) {
            if (!e.target.closest('.user-badge-wrapper')) {
                document.querySelectorAll(".settings-dropdown").forEach(d => d.classList.remove('show'));
            }
        }

        function openSettingsModal() {
            toggleSettingsMenu();
            contentDiv.innerHTML = `
                <div class="fade-in">
                <h2>⚙️ User Settings</h2>
                <div class="admin-card">
                    <h3 style="margin-top:0; font-size:16px;">Change Password</h3>
                    <form onsubmit="changePassword(event)" style="display:flex; flex-direction:column; gap:12px; max-width: 350px;">
                        <div>
                            <div style="font-size:13px; color:var(--subtext); margin-bottom:4px;">Current Password</div>
                            <input type="password" id="curr-pass" required style="margin:0;">
                        </div>
                        <div>
                            <div style="font-size:13px; color:var(--subtext); margin-bottom:4px;">New Password</div>
                            <input type="password" id="new-pass-user" required style="margin:0;">
                        </div>
                        <button type="submit" class="action-btn" style="background:var(--accent); color:black; padding:10px; font-weight:700; margin-top:4px;">Update Password</button>
                    </form>
                </div>
                </div>
            `;
        }

        function changePassword(e) {
            e.preventDefault();
            let current_password = document.getElementById('curr-pass').value;
            let new_password = document.getElementById('new-pass-user').value;
            fetch('/api/settings/password', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({current_password, new_password})
            }).then(res => res.json()).then(data => {
                if(data.success) { alert("Password successfully updated!"); renderHome(); } 
                else { alert(data.error || "Failed to update password"); }
            });
        }

        function safeId(str) { return encodeURIComponent(str).replace(/[^a-zA-Z0-9]/g, ''); }

        function getCoverUrl(song) {
            if(song.has_cover) return `/api/cover/embedded?file=${encodeURIComponent(song.filename)}`;
            return `/api/cover?artist=${encodeURIComponent(song.artist)}&song=${encodeURIComponent(song.title)}&file=${encodeURIComponent(song.filename)}`;
        }

        // --- AUDIO CONTROLS ---
        audio.addEventListener('play', () => {
            playIcon.classList.remove('fa-play');
            playIcon.classList.add('fa-pause');
            playIcon.style.marginLeft = '0';
        });
        audio.addEventListener('pause', () => {
            playIcon.classList.remove('fa-pause');
            playIcon.classList.add('fa-play');
            playIcon.style.marginLeft = '2px';
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
            
            // Sync lyrics
            if (syncedLyrics.length > 0) {
                let newIndex = syncedLyrics.findIndex(l => l.time > audio.currentTime) - 1;
                if (newIndex < 0) newIndex = 0;
                if (newIndex === syncedLyrics.length - 2 && audio.currentTime >= syncedLyrics[syncedLyrics.length - 1].time) {
                    newIndex = syncedLyrics.length - 1;
                }
                if (newIndex !== activeLyricIndex && newIndex >= 0) {
                    if (activeLyricIndex >= 0) {
                        const oldEl = document.getElementById(`lyric-${activeLyricIndex}`);
                        if (oldEl) oldEl.classList.remove('active');
                    }
                    activeLyricIndex = newIndex;
                    const newEl = document.getElementById(`lyric-${activeLyricIndex}`);
                    if (newEl) {
                        newEl.classList.add('active');
                        lyricsContainer.scrollTo({ top: newEl.offsetTop - (lyricsContainer.clientHeight / 2) + 20, behavior: 'smooth' });
                    }
                }
            }
        });

        progressBar.addEventListener('input', function() {
            audio.currentTime = (this.value / 100) * audio.duration;
            updateSliderFill(this);
        });
        
        volumeBar.addEventListener('input', function() {
            audio.volume = this.value / 100;
            updateSliderFill(this);
            if (audio.volume === 0) { muteIcon.className = "fas fa-volume-mute"; } 
            else if (audio.volume < 0.5) { muteIcon.className = "fas fa-volume-down"; } 
            else { muteIcon.className = "fas fa-volume-up"; }
        });
        
        // Initialize volume fill
        updateSliderFill(volumeBar);

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
                // Shuffle remaining queue starting from after current index
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

        function switchView(view) {
            document.querySelectorAll('.nav-item').forEach(el => el.classList.remove('active'));
            if (event && event.currentTarget && event.currentTarget.classList) {
                event.currentTarget.classList.add('active');
            }
            document.getElementById('global-search').value = '';
            
            if (view === 'home') renderHome();
            if (view === 'artists') renderArtists();
            if (view === 'radio') startRadio();
            if (view === 'admin') renderAdminPanel();
        }

        function handleSearch(query) {
            let q = query.toLowerCase().trim();
            if(!q) { renderHome(); return; }
            
            document.querySelectorAll('.nav-item').forEach(el => el.classList.remove('active'));
            let results = allSongs.filter(s => s.title.toLowerCase().includes(q) || s.artist.toLowerCase().includes(q));
            renderGrid(results, `Search Results for "${query}"`);
        }

        function buildCardsHTML(songsArray, isRow = false) {
            let html = isRow ? `<div class="scroll-row">` : `<div class="grid">`;
            songsArray.forEach((song, i) => {
                let coverUrl = getCoverUrl(song);
                let stats = songStats[song.filename] || {likes: 0, dislikes: 0, plays: 0};
                
                html += `
                <div class="card" onclick="playQueue(${JSON.stringify(songsArray).replace(/"/g, '&quot;')}, ${i})">
                    <div class="card-img-container">
                        <img src="${coverUrl}" loading="lazy" onerror="this.style.display='none'">
                        <div class="card-play-overlay"><i class="fas fa-play" style="margin-left: 2px;"></i></div>
                    </div>
                    <div class="card-info">
                        <div class="card-title" title="${song.title}">${song.title}</div>
                        <div class="card-bottom-row">
                            <div class="card-artist" title="${song.artist}">${song.artist}</div>
                            <div class="card-stats" id="stats-${safeId(song.filename)}">
                                <span class="stat-like"><i class="fas fa-heart" style="color:var(--accent)"></i> ${stats.likes}</span>
                            </div>
                        </div>
                    </div>
                </div>`;
            });
            html += `</div>`;
            return html;
        }

        function renderGrid(songsArray, title) {
            contentDiv.innerHTML = `<div class="fade-in"><h2>${title}</h2>` + buildCardsHTML(songsArray) + `</div>`;
        }

        function renderHome() {
            let popular = [...allSongs].sort((a, b) => {
                let statA = songStats[a.filename] || {likes:0, plays:0};
                let statB = songStats[b.filename] || {likes:0, plays:0};
                return (statB.likes * 5 + statB.plays) - (statA.likes * 5 + statA.plays);
            }).slice(0, 15);
            
            let newlyAdded = [...allSongs].sort((a, b) => b.mtime - a.mtime).slice(0, 15);

            contentDiv.innerHTML = `
                <div class="fade-in">
                    <h2>🔥 Popular Right Now</h2>
                    ${buildCardsHTML(popular, true)}
                    <h2>✨ Newly Added</h2>
                    ${buildCardsHTML(newlyAdded, true)}
                    <h2>All Songs</h2>
                    ${buildCardsHTML(allSongs)}
                </div>
            `;
        }

        function renderArtists() {
            let html = `<div class="fade-in"><h2>Artists</h2><div class="grid">`;
            Object.keys(groupedArtists).forEach(artist => {
                let sampleSong = groupedArtists[artist][0];
                let coverUrl = getCoverUrl(sampleSong);

                html += `
                <div class="card" onclick='renderGrid(groupedArtists["${artist.replace(/'/g, "\\'")}"])' style="text-align:center;">
                    <div class="card-img-container" style="border-radius: 50%;">
                        <img src="${coverUrl}" loading="lazy" style="border-radius: 50%;" onerror="this.style.display='none'">
                        <div class="card-play-overlay"><i class="fas fa-play" style="margin-left: 2px;"></i></div>
                    </div>
                    <div class="card-title">${artist}</div>
                    <div class="card-artist" style="text-align:center; padding:0;">${groupedArtists[artist].length} tracks</div>
                </div>`;
            });
            html += `</div></div>`;
            contentDiv.innerHTML = html;
        }

        function renderAdminPanel() {
            contentDiv.innerHTML = `
                <div class="fade-in">
                <h2><i class="fas fa-users-cog" style="color:var(--accent)"></i> Account Manager</h2>
                <div class="admin-card">
                    <h3 style="margin-top:0; font-size:16px;">Create New Account</h3>
                    <form onsubmit="createUser(event)" style="display:flex; gap:10px; align-items:center; flex-wrap:wrap;">
                        <input type="text" id="new-user" placeholder="Username" required style="margin:0; flex:1; min-width:160px;">
                        <input type="password" id="new-pass" placeholder="Password" required style="margin:0; flex:1; min-width:160px;">
                        <label style="font-size:13px; display:flex; align-items:center; gap:6px; cursor:pointer;"><input type="checkbox" id="new-admin"> Admin</label>
                        <button type="submit" class="action-btn" style="background:var(--accent); color:black; padding:10px 18px; font-weight:700;">Add User</button>
                    </form>
                </div>
                <div class="admin-card" style="max-width:100%;">
                    <h3 style="margin-top:0; font-size:16px;">Existing Accounts</h3>
                    <div id="users-table-container">Loading users...</div>
                </div>
                </div>
            `;
            loadUsersTable();
        }

        function loadUsersTable() {
            fetch('/api/admin/users').then(res => res.json()).then(data => {
                let html = `<table class="admin-table"><tr><th>Username</th><th>Role</th><th style="text-align:right;">Actions</th></tr>`;
                for (let [uname, udata] of Object.entries(data)) {
                    let roleBadge = udata.is_admin ? '<span class="badge-admin">Administrator</span>' : '<span class="badge-user">Standard User</span>';
                    html += `<tr>
                        <td><strong>${uname}</strong></td>
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
            let is_admin = document.getElementById('new-admin').checked;

            fetch('/api/admin/users', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({username, password, is_admin})
            }).then(res => res.json()).then(data => {
                if(data.success) {
                    document.getElementById('new-user').value = '';
                    document.getElementById('new-pass').value = '';
                    document.getElementById('new-admin').checked = false;
                    loadUsersTable();
                } else {
                    alert(data.error || "Failed to create user");
                }
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

        function playQueue(queue, index) {
            isRadioMode = false;
            currentQueue = queue;
            originalQueue = [...queue];
            currentIndex = index;
            if(isShuffle) toggleShuffle(); // re-shuffle new queue
            document.getElementById('mode-indicator').innerText = "Mode: Playlist";
            loadSong(currentQueue[currentIndex]);
        }

        function startRadio() {
            isRadioMode = true;
            document.getElementById('mode-indicator').innerText = "Mode: Infinite Radio (AI)";
            contentDiv.innerHTML = `
                <div class="fade-in" style="text-align:center; margin-top: 100px;">
                    <i class="fas fa-broadcast-tower" style="font-size: 80px; color: var(--accent); margin-bottom: 20px;"></i>
                    <h2>Infinite Radio Active</h2>
                    <p style="color:var(--subtext)">Playing personalized tracks based on your likes and listen time.</p>
                </div>
            `;
            nextTrack();
        }

        function loadSong(songObj) {
            currentSongObj = songObj;
            audio.src = '/play/' + encodeURIComponent(songObj.filename);
            audio.play();
            
            rightPanel.style.display = 'flex';
            let coverUrl = getCoverUrl(songObj);
            
            document.getElementById('np-title').innerText = songObj.title;
            document.getElementById('np-artist').innerText = songObj.artist;
            document.getElementById('np-cover').src = coverUrl;

            document.getElementById('rp-title').innerText = songObj.title;
            document.getElementById('rp-artist').innerText = songObj.artist;
            document.getElementById('rp-cover').src = coverUrl;
            
            fetchStatusAndLog(songObj.filename);
            fetchLyrics(songObj.artist, songObj.title);
        }

        function fetchLyrics(artist, track) {
            lyricsContainer.innerHTML = '<div style="color:#555; text-align:center; padding-top:40px;"><i class="fas fa-spinner fa-spin"></i> Searching lyrics...</div>';
            syncedLyrics = [];
            activeLyricIndex = -1;

            fetch(`https://lrclib.net/api/get?artist_name=${encodeURIComponent(artist)}&track_name=${encodeURIComponent(track)}`)
                .then(res => res.json())
                .then(data => {
                    if (data && data.syncedLyrics) {
                        parseLRCLyrics(data.syncedLyrics);
                    } else if (data && data.plainLyrics) {
                        lyricsContainer.innerHTML = `<div style="color:var(--subtext); line-height: 2; padding: 0 10px;">${data.plainLyrics.replace(/\\n/g, '<br>')}</div>`;
                    } else {
                        lyricsContainer.innerHTML = '<div style="color:#555; text-align:center; padding-top:40px;">No lyrics found for this track.</div>';
                    }
                })
                .catch(err => {
                    lyricsContainer.innerHTML = '<div style="color:#555; text-align:center; padding-top:40px;">Failed to load lyrics.</div>';
                });
        }

        function parseLRCLyrics(lrcString) {
            const lines = lrcString.split('\\n');
            const regex = /\\[(\\d{2}):(\\d{2}\\.\\d{2,3})\\](.*)/;
            syncedLyrics = [];
            
            lines.forEach(line => {
                const match = line.match(regex);
                if (match) {
                    const time = (parseInt(match[1]) * 60) + parseFloat(match[2]);
                    const text = match[3].trim() || '♪';
                    syncedLyrics.push({ time, text });
                }
            });

            lyricsContainer.innerHTML = '';
            syncedLyrics.forEach((line, index) => {
                const el = document.createElement('div');
                el.className = 'lyric-line';
                el.id = `lyric-${index}`;
                el.innerText = line.text;
                el.onclick = () => { audio.currentTime = line.time; audio.play(); };
                lyricsContainer.appendChild(el);
            });
        }

        function fetchStatusAndLog(filename) {
            fetch('/api/status?song=' + encodeURIComponent(filename))
                .then(res => res.json())
                .then(data => {
                    document.getElementById('like-btn').classList.toggle('active', data.liked);
                    document.getElementById('dislike-btn').classList.toggle('active', data.disliked);
                });

            fetch('/api/feedback', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({action: 'listen', song: filename})
            }).then(() => refreshStatsUI()); 
        }

        function nextTrack() {
            if (isRadioMode) {
                fetch('/api/radio/next').then(res => res.json()).then(data => loadSong(data.song));
            } else if (currentQueue.length > 0) {
                if (isRepeat) {
                    audio.currentTime = 0;
                    audio.play();
                } else {
                    currentIndex = (currentIndex + 1) % currentQueue.length;
                    loadSong(currentQueue[currentIndex]);
                }
            }
        }
        
        function prevTrack() {
            if(audio.currentTime > 3) {
                audio.currentTime = 0;
            } else if (!isRadioMode && currentQueue.length > 0) {
                currentIndex = (currentIndex - 1 + currentQueue.length) % currentQueue.length;
                loadSong(currentQueue[currentIndex]);
            }
        }

        function animateButton(btnId) {
            const btn = document.getElementById(btnId);
            btn.classList.remove('pop-anim');
            void btn.offsetWidth; 
            btn.classList.add('pop-anim');
        }

        function refreshStatsUI() {
            fetch('/api/data').then(res => res.json()).then(data => {
                songStats = data.stats;
                data.songs.forEach(song => {
                    const el = document.getElementById(`stats-${safeId(song.filename)}`);
                    if (el) {
                        let s = songStats[song.filename] || {likes: 0, dislikes: 0};
                        // only render like to keep it clean on cards
                        el.innerHTML = `<span class="stat-like"><i class="fas fa-heart" style="color:var(--accent)"></i> ${s.likes}</span>`;
                    }
                });
            });
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

        audio.addEventListener('ended', nextTrack);
    </script>
{% endif %}
</body>
</html>
"""

# ---------------------------------------------------------
# ROUTE HANDLERS
# ---------------------------------------------------------
@app.route('/', methods=['GET', 'POST'])
def index():
    db = load_db()
    users = db.get("users", {})
    if not users:
        if request.method == 'POST':
            username = request.form['username']
            password = generate_password_hash(request.form['password'])
            db["users"] = {username: {'password': password, 'is_admin': True, 'likes': [], 'dislikes': [], 'play_counts': {}}}
            save_db(db)
            return redirect(url_for('index'))
        return render_template_string(HTML_TEMPLATE, logged_in=False, setup=True)
        
    if 'user' not in session:
        if request.method == 'POST':
            username = request.form['username']
            password = request.form['password']
            if username in users and check_password_hash(users[username]['password'], password):
                session['user'] = username
                session['is_admin'] = users[username].get('is_admin', False)
                return redirect(url_for('index'))
        return render_template_string(HTML_TEMPLATE, logged_in=False, setup=False)
        
    return render_template_string(HTML_TEMPLATE, logged_in=True, is_admin=session.get('is_admin', False))

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('index'))

@app.route('/play/<path:filename>')
def play(filename):
    if 'user' not in session: return "Unauthorized", 401
    return send_from_directory(MUSIC_DIR, filename)

@app.route('/api/data')
def api_data():
    if 'user' not in session: return jsonify([])
    songs = get_all_songs_enriched()
    stats = get_aggregated_stats()
    return jsonify({"songs": songs, "stats": stats})

@app.route('/api/radio/next')
def api_radio():
    if 'user' not in session: return jsonify({})
    next_song_obj = get_radio_recommendation(session['user'])
    return jsonify({"song": next_song_obj})

@app.route('/api/status')
def api_status():
    if 'user' not in session: return jsonify({})
    song = request.args.get('song')
    db = load_db()
    user = db["users"].get(session['user'], {})
    return jsonify({
        "liked": song in user.get("likes", []),
        "disliked": song in user.get("dislikes", [])
    })

@app.route('/api/feedback', methods=['POST'])
def api_feedback():
    if 'user' not in session: return jsonify({"status": "error"})
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

# User Settings API Route for Password Change
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

# Admin Account Management API Routes
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
            "play_counts": {}
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

@app.route('/api/cover/embedded')
def api_embedded_cover():
    filename = request.args.get('file', '')
    filepath = os.path.join(MUSIC_DIR, filename)
    try:
        audio = mutagen.File(filepath)
        if hasattr(audio, 'tags') and audio.tags:
            for key in audio.tags.keys():
                if key.startswith('APIC'):
                    pic = audio.tags[key]
                    return Response(pic.data, mimetype=pic.mime)
        if hasattr(audio, 'pictures') and audio.pictures:
            pic = audio.pictures[0]
            return Response(pic.data, mimetype=pic.mime)
    except: pass
    return redirect("data:image/gif;base64,R0lGODlhAQABAAD/ACwAAAAAAQABAAACADs=")

@app.route('/api/cover')
def api_cover():
    artist = request.args.get('artist', '').strip()
    song = request.args.get('song', '').strip()
    file = request.args.get('file', '').strip()
    
    cache_key = f"{artist} | {song} | v7"
    if cache_key in cover_cache and cover_cache[cache_key]: 
        return redirect(cover_cache[cache_key])

    # 1. Exact Local Image File Match in the same artist subfolder
    if file:
        song_dir = os.path.dirname(os.path.join(MUSIC_DIR, file))
        valid_exts = ('.jpg', '.jpeg', '.png', '.webp')
        song_clean = os.path.splitext(os.path.basename(file))[0].lower()
        if os.path.exists(song_dir):
            for f in os.listdir(song_dir):
                if f.lower().endswith(valid_exts) and os.path.splitext(f)[0].lower() == song_clean:
                    rel_img = os.path.relpath(os.path.join(song_dir, f), MUSIC_DIR)
                    local_url = f"/local_cover/{urllib.parse.quote(rel_img)}"
                    cover_cache[cache_key] = local_url
                    save_covers(cover_cache)
                    return redirect(local_url)

    # 2. MusicBrainz Search
    mb_url = fetch_musicbrainz_cover(artist, song)
    if mb_url:
        cover_cache[cache_key] = mb_url
        save_covers(cover_cache)
        return redirect(mb_url)

    # 3. iTunes Search
    itunes_url = fetch_itunes_cover(artist, song)
    if itunes_url:
        cover_cache[cache_key] = itunes_url
        save_covers(cover_cache)
        return redirect(itunes_url)

    fallback_svg = generate_svg_fallback(song)
    cover_cache[cache_key] = fallback_svg
    save_covers(cover_cache)
    return redirect(fallback_svg)

@app.route('/local_cover/<path:filename>')
def local_cover(filename):
    return send_from_directory(MUSIC_DIR, filename)

if __name__ == '__main__':
    print(f"🎵 App running on port {PORT}! Open http://localhost:{PORT}")
    app.run(host='0.0.0.0', port=PORT)