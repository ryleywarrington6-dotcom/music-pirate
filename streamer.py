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
    from mutagen.mp3 import MP3
    from mutagen.flac import FLAC
except ImportError:
    print("\n❌ ERROR: Required library 'mutagen' is not installed!")
    print("Run this command in your terminal to fix it:\n")
    print("    python -m pip install mutagen\n")
    sys.exit(1)

PORT = int(os.environ.get("PORT", 8000))
MUSIC_DIR = os.getcwd()

# Data stores
DB_FILE = 'database.json'
COVERS_FILE = 'covers.json'
METADATA_FILE = 'metadata.json'

app = Flask(__name__)
app.secret_key = 'super-secret-key-change-me-later'

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

def get_all_filenames():
    return sorted([f for f in os.listdir(MUSIC_DIR) if f.lower().endswith(('.mp3', '.flac'))])

# ---------------------------------------------------------
# METADATA EXTRACTION ENGINE (MP3 & FLAC)
# ---------------------------------------------------------
def has_embedded_art(filepath):
    """Checks if MP3 or FLAC has embedded cover artwork."""
    try:
        audio = mutagen.File(filepath)
        if audio is None: return False
        # MP3 ID3 APIC check
        if hasattr(audio, 'tags') and audio.tags:
            for key in audio.tags.keys():
                if key.startswith('APIC'): return True
        # FLAC pictures check
        if hasattr(audio, 'pictures') and audio.pictures: return True
    except: pass
    return False

def parse_filename_fallback(filename):
    """Safely extracts artist & title from filename when tags are absent."""
    base = os.path.splitext(filename)[0]
    base = re.sub(r'^\s*\d{1,3}[\s.-]+', '', base) # Strip leading track numbers
    parts = re.split(r'\s*[-–—]\s*', base, maxsplit=1)
    if len(parts) >= 2:
        return parts[0].strip(), parts[1].strip()
    return "Unknown Artist", base.strip()

def extract_audio_tags(filepath, filename):
    """Extracts tags directly from MP3 (ID3) and FLAC (Vorbis) files."""
    artist, title = None, None
    try:
        audio = mutagen.File(filepath)
        if audio is not None:
            # Check ID3 Tags (MP3)
            if 'TPE1' in audio: # Artist
                artist = str(audio['TPE1'])
            elif 'TPE2' in audio: # Album Artist
                artist = str(audio['TPE2'])

            if 'TIT2' in audio: # Title
                title = str(audio['TIT2'])

            # Check Vorbis Comments (FLAC / EasyID3)
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
    except Exception as e:
        pass

    # Fallback to filename parser if tags are completely missing
    if not artist or not title:
        fb_artist, fb_title = parse_filename_fallback(filename)
        artist = artist or fb_artist
        title = title or fb_title

    artist = artist.strip() if artist else "Unknown Artist"
    title = title.strip() if title else os.path.splitext(filename)[0]

    return artist, title

def get_song_metadata(filename):
    filepath = os.path.join(MUSIC_DIR, filename)
    mtime = os.path.getmtime(filepath) if os.path.exists(filepath) else 0

    if filename in meta_cache:
        if meta_cache[filename].get('mtime') == mtime:
            return meta_cache[filename]
        
    artist, title = extract_audio_tags(filepath, filename)
    
    meta_cache[filename] = {
        "filename": filename,
        "artist": artist,
        "title": title,
        "has_cover": has_embedded_art(filepath),
        "mtime": mtime
    }
    save_json_file(METADATA_FILE, meta_cache)
    return meta_cache[filename]

def get_all_songs_enriched():
    files = get_all_filenames()
    return [get_song_metadata(f) for f in files]

# ---------------------------------------------------------
# EXTERNAL COVER ART FETCHERS (iTunes, AudioDB, Deezer, MusicBrainz)
# ---------------------------------------------------------
def fetch_itunes_cover(artist, song):
    query = f"{artist} {song}".strip()
    try:
        url = f"https://itunes.apple.com/search?term={urllib.parse.quote(query)}&media=music&entity=song&limit=5"
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            data = json.loads(resp.read().decode())
            if data.get('resultCount', 0) > 0:
                for res in data['results']:
                    api_artist = res.get('artistName', '').lower()
                    if artist.lower() in api_artist or api_artist in artist.lower():
                        return res['artworkUrl100'].replace('100x100bb', '500x500bb')
                return data['results'][0]['artworkUrl100'].replace('100x100bb', '500x500bb')
    except Exception: pass
    return None

def fetch_audiodb_cover(artist, song):
    if not artist or artist == "Unknown Artist": return None
    try:
        track_url = f"https://www.theaudiodb.com/api/v1/json/2/searchtrack.php?s={urllib.parse.quote(artist)}&t={urllib.parse.quote(song)}"
        req = urllib.request.Request(track_url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=2.5) as resp:
            data = json.loads(resp.read().decode())
            if data and data.get('track') and data['track'][0].get('strTrackThumb'):
                return data['track'][0]['strTrackThumb']
    except Exception: pass
    return None

def fetch_deezer_cover(artist, song):
    try:
        q = f'artist:"{artist}" track:"{song}"' if artist != "Unknown Artist" else song
        url = f"https://api.deezer.com/search?q={urllib.parse.quote(q)}&limit=1"
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=2.5) as resp:
            data = json.loads(resp.read().decode())
            if data.get('data'): return data['data'][0]['album']['cover_big']
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
    svg = f"""data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='300' height='300' viewBox='0 0 300 300'><rect width='300' height='300' fill='%231f1f1f'/><circle cx='150' cy='150' r='80' fill='%23282828'/><text x='50%' y='48%' fill='%231DB954' font-size='48' font-family='sans-serif' text-anchor='middle' dominant-baseline='middle'>🎵</text><text x='50%' y='80%' fill='%23b3b3b3' font-size='14' font-family='sans-serif' text-anchor='middle'>{clean_title}</text></svg>"""
    return svg

# ---------------------------------------------------------
# STATISTICS ENGINE
# ---------------------------------------------------------
def get_aggregated_stats():
    files = get_all_filenames()
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
    
    all_files = get_all_filenames()
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
# HTML & FRONTEND TEMPLATE
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
        :root { --bg: #000000; --panel: #121212; --highlight: #1a1a1a; --text: #ffffff; --subtext: #b3b3b3; --accent: #1DB954; }
        body { font-family: 'Segoe UI', sans-serif; background: var(--bg); color: var(--text); margin: 0; overflow: hidden; display: flex; height: 100vh; }
        
        .sidebar { width: 240px; background: var(--bg); padding: 24px 12px; display: flex; flex-direction: column; gap: 20px; flex-shrink: 0; z-index: 10; }
        .logo { font-size: 24px; font-weight: bold; padding: 0 12px; margin-bottom: 10px; }
        .nav-item { padding: 12px; border-radius: 4px; cursor: pointer; color: var(--subtext); font-weight: 600; display: flex; align-items: center; gap: 15px; transition: 0.2s; }
        .nav-item:hover, .nav-item.active { color: var(--text); background: var(--highlight); }
        
        .center-wrapper { flex: 1; display: flex; flex-direction: column; overflow: hidden; background: var(--bg); position: relative; }
        .top-bar { height: 64px; background: rgba(0,0,0,0.7); backdrop-filter: blur(10px); display: flex; align-items: center; justify-content: space-between; padding: 0 32px; position: absolute; top: 0; left: 0; right: 0; z-index: 50; border-radius: 8px 8px 0 0; margin: 8px 8px 0 0; }
        .main-content { flex: 1; background: var(--panel); border-radius: 8px; margin: 8px 8px 100px 0; padding: 84px 32px 32px 32px; overflow-y: auto; transition: 0.3s; }
        .right-panel { width: 340px; background: var(--panel); border-radius: 8px; margin: 8px 8px 100px 0; padding: 24px; display: none; flex-direction: column; align-items: center; text-align: center; overflow: hidden; transition: 0.3s; flex-shrink: 0; }
        .player-bar { position: fixed; bottom: 0; left: 0; right: 0; height: 90px; background: #181818; border-top: 1px solid #282828; display: flex; align-items: center; padding: 0 20px; justify-content: space-between; z-index: 1000; }
        
        .search-container { display: flex; align-items: center; background: #242424; border-radius: 20px; padding: 8px 16px; width: 350px; border: 1px solid transparent; transition: 0.2s; }
        .search-container:focus-within { border-color: #333; background: #2a2a2a; }
        .search-container i { color: var(--subtext); font-size: 14px; margin-right: 10px; }
        .search-container input { background: transparent; border: none; color: white; width: 100%; outline: none; font-size: 14px; font-family: inherit; }
        .user-badge { display: flex; align-items: center; gap: 8px; font-size: 14px; font-weight: bold; background: #000; padding: 6px 12px; border-radius: 20px; }
        
        h2 { font-size: 24px; margin-top: 0; margin-bottom: 20px; }
        .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); gap: 24px; margin-bottom: 40px; }
        .scroll-row { display: flex; gap: 24px; overflow-x: auto; padding-bottom: 20px; margin-bottom: 40px; scroll-snap-type: x mandatory; }
        .scroll-row .card { min-width: 180px; flex-shrink: 0; scroll-snap-align: start; }
        
        .card { background: #181818; padding: 16px; border-radius: 8px; cursor: pointer; transition: 0.3s; position: relative; text-align: left; }
        .card:hover { background: #282828; transform: translateY(-5px); }
        .card-img-container { width: 100%; aspect-ratio: 1; background: #282828; border-radius: 6px; margin-bottom: 12px; position: relative; overflow: hidden; display: flex; align-items: center; justify-content: center; font-size: 40px; color: #555; box-shadow: 0 8px 16px rgba(0,0,0,0.4); }
        .card-img-container img { width: 100%; height: 100%; object-fit: cover; position: absolute; top: 0; left: 0; }
        .card-info { display: flex; flex-direction: column; gap: 6px; }
        .card-title { font-weight: bold; font-size: 16px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        .card-bottom-row { display: flex; justify-content: space-between; align-items: center; }
        .card-artist { font-weight: 500; color: var(--subtext); font-size: 13px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; flex: 1; padding-right: 10px; }
        .card-stats { display: flex; gap: 10px; font-size: 12px; color: var(--subtext); transition: 0.3s; }
        
        .now-playing-info { width: 30%; display: flex; align-items: center; gap: 15px; }
        .np-cover { width: 60px; height: 60px; background: #282828; border-radius: 4px; object-fit: cover; box-shadow: 0 4px 8px rgba(0,0,0,0.5); }
        .np-text { display: flex; flex-direction: column; overflow: hidden; }
        .np-title { font-weight: bold; font-size: 14px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        .np-artist { color: var(--subtext); font-size: 12px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        
        .controls { width: 40%; display: flex; flex-direction: column; align-items: center; }
        .buttons { display: flex; gap: 20px; align-items: center; margin-bottom: 8px; }
        
        @keyframes pop { 0% { transform: scale(1); } 50% { transform: scale(1.4); } 100% { transform: scale(1); } }
        .pop-anim { animation: pop 0.4s cubic-bezier(0.175, 0.885, 0.32, 1.275); }
        
        .btn { background: none; border: none; color: var(--subtext); font-size: 16px; cursor: pointer; transition: color 0.2s; outline: none; }
        .btn:hover { color: var(--text); }
        .btn.active#like-btn i { color: var(--accent) !important; }
        .btn.active#dislike-btn i { color: #ff5555 !important; } 
        .btn.play-btn { background: var(--text); color: var(--bg); height: 32px; width: 32px; border-radius: 50%; display: flex; align-items: center; justify-content: center; transition: transform 0.2s; }
        .btn.play-btn:hover { transform: scale(1.1); color: var(--bg); }
        audio { display: none; }
        
        input[type=range] { -webkit-appearance: none; background: #333; height: 4px; border-radius: 2px; outline: none; cursor: pointer; }
        input[type=range]::-webkit-slider-thumb { -webkit-appearance: none; width: 12px; height: 12px; border-radius: 50%; background: var(--text); transition: 0.1s; opacity: 0; }
        input[type=range]:hover::-webkit-slider-thumb { opacity: 1; }
        input[type=range]::-moz-range-thumb { width: 12px; height: 12px; border-radius: 50%; background: var(--text); border: none; }
        
        .lyrics-container { width: 100%; flex: 1; overflow-y: auto; text-align: left; margin-top: 15px; padding-bottom: 50px; scroll-behavior: smooth; mask-image: linear-gradient(to bottom, transparent 0%, black 15%, black 85%, transparent 100%); -webkit-mask-image: linear-gradient(to bottom, transparent 0%, black 15%, black 85%, transparent 100%); }
        .lyric-line { font-size: 18px; color: rgba(255, 255, 255, 0.4); padding: 8px 10px; margin: 4px 0; border-radius: 8px; transition: all 0.4s cubic-bezier(0.175, 0.885, 0.32, 1.275); font-weight: 700; cursor: pointer; transform-origin: left center; }
        .lyric-line:hover { color: rgba(255, 255, 255, 0.9); }
        .lyric-line.active { color: var(--accent); font-size: 24px; text-shadow: 0 0 15px rgba(29, 185, 84, 0.5); transform: scale(1.05) translateX(5px); opacity: 1; }
        
        ::-webkit-scrollbar { width: 6px; height: 6px; }
        ::-webkit-scrollbar-track { background: transparent; }
        ::-webkit-scrollbar-thumb { background: #444; border-radius: 4px; }
        ::-webkit-scrollbar-thumb:hover { background: #666; }
        
        .auth-container { width: 100%; height: 100vh; display: flex; align-items: center; justify-content: center; background: var(--bg); }
        .auth-card { background: var(--panel); padding: 40px; border-radius: 8px; text-align: center; width: 300px; }
        input[type=text], input[type=password] { width: 90%; padding: 12px; margin: 10px 0; border-radius: 4px; border: 1px solid #333; background: #222; color: white; }
        button.primary { background: var(--accent); color: black; border: none; padding: 12px 24px; border-radius: 20px; font-weight: bold; cursor: pointer; margin-top: 10px; width: 100%; }
    </style>
</head>
<body>

{% if not logged_in %}
    <div class="auth-container">
        <div class="auth-card">
            <h2>{{ 'Setup Admin' if setup else 'Login' }}</h2>
            <form method="POST">
                <input type="text" name="username" placeholder="Username" required>
                <input type="password" name="password" placeholder="Password" required>
                <button type="submit" class="primary">{{ 'Create Account' if setup else 'Log In' }}</button>
            </form>
        </div>
    </div>
{% else %}
    <div class="sidebar">
        <div class="logo"><i class="fab fa-spotify" style="color:var(--accent)"></i> Streamer</div>
        <div class="nav-item active" onclick="switchView('home')"><i class="fas fa-home"></i> Home</div>
        <div class="nav-item" onclick="document.getElementById('global-search').focus();"><i class="fas fa-search"></i> Search</div>
        <div class="nav-item" onclick="switchView('artists')"><i class="fas fa-microphone"></i> Artists</div>
        <div class="nav-item" onclick="switchView('radio')"><i class="fas fa-broadcast-tower"></i> Infinite Radio</div>
        <div style="flex-grow:1"></div>
        <a href="/logout" style="color:var(--subtext); text-decoration:none; padding:12px;"><i class="fas fa-sign-out-alt"></i> Logout</a>
    </div>

    <!-- CENTER WRAPPER -->
    <div class="center-wrapper">
        <div class="top-bar">
            <div class="search-container">
                <i class="fas fa-search"></i>
                <input type="text" id="global-search" placeholder="What do you want to listen to?" oninput="handleSearch(this.value)">
            </div>
            <div class="user-badge">
                <i class="fas fa-user-circle"></i> {{ session.user }}
            </div>
        </div>
        <div class="main-content" id="main-content"></div>
    </div>

    <!-- RIGHT PANEL -->
    <div class="right-panel" id="right-panel">
        <h3 style="margin-top:0; color: var(--subtext); width: 100%; text-align: left; font-size: 14px;"><i class="fas fa-compact-disc"></i> Now Playing</h3>
        <img id="rp-cover" src="" style="width: 240px; height: 240px; border-radius: 8px; box-shadow: 0 12px 24px rgba(0,0,0,0.6); margin: 15px 0;">
        <div id="rp-title" style="font-size: 22px; font-weight: bold; margin-bottom: 5px; width: 100%; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;"></div>
        <div id="rp-artist" style="color: var(--subtext); font-weight: 500; margin-bottom: 25px; width: 100%; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;"></div>

        <div style="width: 100%; display: flex; align-items: center; gap: 10px; font-size: 12px; color: var(--subtext);">
            <span id="time-current">0:00</span>
            <input type="range" id="progress-bar" value="0" max="100" style="flex:1;">
            <span id="time-total">0:00</span>
        </div>

        <div class="lyrics-container" id="lyrics-container">
            <div style="color:#555; text-align:center; padding-top:40px;">Select a track to load lyrics.</div>
        </div>
    </div>

    <!-- BOTTOM PLAYER BAR -->
    <div class="player-bar">
        <div class="now-playing-info">
            <img id="np-cover" class="np-cover" src="data:image/gif;base64,R0lGODlhAQABAAD/ACwAAAAAAQABAAACADs=" alt="Cover Art">
            <div class="np-text">
                <div class="np-title" id="np-title">No track selected</div>
                <div class="np-artist" id="np-artist">-</div>
            </div>
        </div>
        <div class="controls">
            <div class="buttons">
                <button class="btn" id="dislike-btn" onclick="sendFeedback('dislike')"><i class="fas fa-thumbs-down"></i></button>
                <button class="btn play-btn" id="play-btn-wrapper" onclick="togglePlay()">
                    <i class="fas fa-play" id="play-icon"></i>
                </button>
                <button class="btn" onclick="nextTrack()"><i class="fas fa-step-forward"></i></button>
                <button class="btn" id="like-btn" onclick="sendFeedback('like')"><i class="fas fa-heart"></i></button>
            </div>
            <audio id="audio-player"></audio>
        </div>
        <div style="width: 30%; text-align: right; color:var(--subtext); font-size:12px;" id="mode-indicator">
            Mode: Manual Playlist
        </div>
    </div>

    <script>
        let allSongs = [];
        let songStats = {};
        let groupedArtists = {};
        let currentQueue = [];
        let currentIndex = 0;
        let isRadioMode = false;
        let currentSongObj = null; 
        let syncedLyrics = [];
        let activeLyricIndex = -1;

        const contentDiv = document.getElementById('main-content');
        const rightPanel = document.getElementById('right-panel');
        const audio = document.getElementById('audio-player');
        const playIcon = document.getElementById('play-icon');
        const progressBar = document.getElementById('progress-bar');
        const timeCurrentEl = document.getElementById('time-current');
        const timeTotalEl = document.getElementById('time-total');
        const lyricsContainer = document.getElementById('lyrics-container');

        function safeId(str) { return encodeURIComponent(str).replace(/[^a-zA-Z0-9]/g, ''); }

        function getCoverUrl(song) {
            if(song.has_cover) {
                return `/api/cover/embedded?file=${encodeURIComponent(song.filename)}`;
            }
            return `/api/cover?artist=${encodeURIComponent(song.artist)}&song=${encodeURIComponent(song.title)}&file=${encodeURIComponent(song.filename)}`;
        }

        audio.addEventListener('play', () => {
            playIcon.classList.remove('fa-play');
            playIcon.classList.add('fa-pause');
        });
        audio.addEventListener('pause', () => {
            playIcon.classList.remove('fa-pause');
            playIcon.classList.add('fa-play');
        });

        audio.addEventListener('loadedmetadata', () => {
            timeTotalEl.innerText = formatTime(audio.duration);
        });

        audio.addEventListener('timeupdate', () => {
            if (!audio.duration) return;
            const percent = (audio.currentTime / audio.duration) * 100;
            progressBar.value = percent;
            timeCurrentEl.innerText = formatTime(audio.currentTime);
            
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

        progressBar.addEventListener('input', () => {
            audio.currentTime = (progressBar.value / 100) * audio.duration;
        });

        function togglePlay() {
            animateButton('play-btn-wrapper');
            if (audio.paused) audio.play();
            else audio.pause();
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
        }

        function handleSearch(query) {
            let q = query.toLowerCase().trim();
            if(!q) { renderHome(); return; }
            
            document.querySelectorAll('.nav-item').forEach(el => el.classList.remove('active'));
            let results = allSongs.filter(s => s.title.toLowerCase().includes(q) || s.artist.toLowerCase().includes(q));
            renderGrid(results, `Search Results for "${query}"`, true);
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
                    </div>
                    <div class="card-info">
                        <div class="card-title" title="${song.title}">${song.title}</div>
                        <div class="card-bottom-row">
                            <div class="card-artist" title="${song.artist}">${song.artist}</div>
                            <div class="card-stats" id="stats-${safeId(song.filename)}">
                                <span class="stat-like"><i class="fas fa-heart" style="color:var(--accent)"></i> ${stats.likes}</span>
                                <span class="stat-dislike"><i class="fas fa-thumbs-down" style="color:#ff5555"></i> ${stats.dislikes}</span>
                            </div>
                        </div>
                    </div>
                </div>`;
            });
            html += `</div>`;
            return html;
        }

        function renderGrid(songsArray, title) {
            contentDiv.innerHTML = `<h2>${title}</h2>` + buildCardsHTML(songsArray);
        }

        function renderHome() {
            let popular = [...allSongs].sort((a, b) => {
                let statA = songStats[a.filename] || {likes:0, plays:0};
                let statB = songStats[b.filename] || {likes:0, plays:0};
                return (statB.likes * 5 + statB.plays) - (statA.likes * 5 + statA.plays);
            }).slice(0, 15);
            
            let newlyAdded = [...allSongs].sort((a, b) => b.mtime - a.mtime).slice(0, 15);

            contentDiv.innerHTML = `
                <h2>🔥 Popular Right Now</h2>
                ${buildCardsHTML(popular, true)}
                <h2>✨ Newly Added</h2>
                ${buildCardsHTML(newlyAdded, true)}
                <h2>All Songs</h2>
                ${buildCardsHTML(allSongs)}
            `;
        }

        function renderArtists() {
            let html = `<h2>Artists</h2><div class="grid">`;
            Object.keys(groupedArtists).forEach(artist => {
                let sampleSong = groupedArtists[artist][0];
                let coverUrl = getCoverUrl(sampleSong);

                html += `
                <div class="card" onclick='renderGrid(groupedArtists["${artist.replace(/'/g, "\\'")}"])' style="text-align:center;">
                    <div class="card-img-container" style="border-radius: 50%;">
                        <img src="${coverUrl}" loading="lazy" style="border-radius: 50%;" onerror="this.style.display='none'">
                    </div>
                    <div class="card-title">${artist}</div>
                    <div class="card-artist" style="text-align:center;">${groupedArtists[artist].length} tracks</div>
                </div>`;
            });
            html += `</div>`;
            contentDiv.innerHTML = html;
        }

        function playQueue(queue, index) {
            isRadioMode = false;
            currentQueue = queue;
            currentIndex = index;
            document.getElementById('mode-indicator').innerText = "Mode: Playlist";
            loadSong(currentQueue[currentIndex]);
        }

        function startRadio() {
            isRadioMode = true;
            document.getElementById('mode-indicator').innerText = "Mode: Infinite Radio (AI)";
            contentDiv.innerHTML = `
                <div style="text-align:center; margin-top: 100px;">
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
                        lyricsContainer.innerHTML = `<div style="color:var(--subtext); line-height: 2;">${data.plainLyrics.replace(/\\n/g, '<br>')}</div>`;
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
            } else {
                currentIndex = (currentIndex + 1) % currentQueue.length;
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
                        el.innerHTML = `
                            <span class="stat-like"><i class="fas fa-heart" style="color:var(--accent)"></i> ${s.likes}</span>
                            <span class="stat-dislike"><i class="fas fa-thumbs-down" style="color:#ff5555"></i> ${s.dislikes}</span>
                        `;
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
            db["users"] = {username: {'password': password, 'likes': [], 'dislikes': [], 'play_counts': {}}}
            save_db(db)
            return redirect(url_for('index'))
        return render_template_string(HTML_TEMPLATE, logged_in=False, setup=True)
    if 'user' not in session:
        if request.method == 'POST':
            username = request.form['username']
            password = request.form['password']
            if username in users and check_password_hash(users[username]['password'], password):
                session['user'] = username
                return redirect(url_for('index'))
        return render_template_string(HTML_TEMPLATE, logged_in=False, setup=False)
    return render_template_string(HTML_TEMPLATE, logged_in=True)

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
    return jsonify({"status": "success"})

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
    
    # Cache key v4 forces a complete reset of invalid images
    cache_key = f"{artist} | {song} | v4"
    
    if cache_key in cover_cache and cover_cache[cache_key]: 
        return redirect(cover_cache[cache_key])

    # 1. Exact Local Image File Match
    valid_exts = ('.jpg', '.jpeg', '.png', '.webp')
    song_clean = os.path.splitext(file or song)[0].lower()
    for f in os.listdir(MUSIC_DIR):
        if f.lower().endswith(valid_exts) and os.path.splitext(f)[0].lower() == song_clean:
            local_url = f"/local_cover/{urllib.parse.quote(f)}"
            cover_cache[cache_key] = local_url
            save_covers(cover_cache)
            return redirect(local_url)

    # 2. MusicBrainz Search (Open-source Cover Art Archive)
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

    # 4. AudioDB Search
    audiodb_url = fetch_audiodb_cover(artist, song)
    if audiodb_url:
        cover_cache[cache_key] = audiodb_url
        save_covers(cover_cache)
        return redirect(audiodb_url)

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