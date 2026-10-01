from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import ssl
import tempfile
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from mutagen import File as MutagenFile, MutagenError
from mutagen.aiff import AIFF
from mutagen.flac import FLAC, Picture
from mutagen.id3 import APIC, ID3
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4, MP4Cover
from mutagen.wave import WAVE
from dataclasses import dataclass, field
from tkinter import filedialog, messagebox, ttk
import tkinter as tk


FIELD_SEPARATOR = "\x1e"
ROW_SEPARATOR = "\x1d"
EDITABLE_FIELDS = ("name", "artist", "album", "genre")
LOOKUP_BATCH_SIZE = 40
LOOKUP_PAUSE_SECONDS = 3
MUSICBRAINZ_REQUEST_INTERVAL = 1.1
MUSICBRAINZ_GENRE_CACHE: dict[str, str | None] = {}
MUSICBRAINZ_CACHE_LOCK = threading.Lock()
MUSICBRAINZ_RATE_LOCK = threading.Lock()
MUSICBRAINZ_LAST_REQUEST = 0.0


class LookupCancelled(Exception):
    pass


def check_lookup_cancelled(cancel_event: threading.Event | None) -> None:
    if cancel_event and cancel_event.is_set():
        raise LookupCancelled


def musicbrainz_request(url: str, cancel_event: threading.Event | None) -> dict[str, object]:
    global MUSICBRAINZ_LAST_REQUEST
    check_lookup_cancelled(cancel_event)
    with MUSICBRAINZ_RATE_LOCK:
        elapsed = time.monotonic() - MUSICBRAINZ_LAST_REQUEST
        wait_seconds = MUSICBRAINZ_REQUEST_INTERVAL - elapsed
        if wait_seconds > 0:
            if cancel_event:
                if cancel_event.wait(wait_seconds):
                    raise LookupCancelled
            else:
                time.sleep(wait_seconds)
        check_lookup_cancelled(cancel_event)
        MUSICBRAINZ_LAST_REQUEST = time.monotonic()

    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "MusicaTag/1.0 (music metadata desktop application)",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(
        request, timeout=12, context=create_https_context()
    ) as response:
        payload = json.load(response)
    check_lookup_cancelled(cancel_event)
    if not isinstance(payload, dict):
        raise RuntimeError("MusicBrainz ha restituito una risposta non valida.")
    return payload


def create_https_context() -> ssl.SSLContext:
    context = ssl.create_default_context()
    if context.cert_store_stats()["x509_ca"] == 0:
        verify_paths = ssl.get_default_verify_paths()
        certificate_bundles = (verify_paths.cafile, "/etc/ssl/cert.pem")
        for certificate_bundle in certificate_bundles:
            if certificate_bundle and os.path.isfile(certificate_bundle):
                context.load_verify_locations(cafile=certificate_bundle)
                if context.cert_store_stats()["x509_ca"]:
                    break
    if context.cert_store_stats()["x509_ca"] == 0:
        raise RuntimeError(
            "Certificati HTTPS non disponibili. Installa i certificati di Python "
            "oppure verifica la configurazione SSL del sistema."
        )
    return context


@dataclass
class Track:
    database_id: int
    name: str
    artist: str
    album: str
    genre: str
    location: str = ""
    original: dict[str, str] = field(init=False)
    suggestion: str = ""
    cover_url: str = ""
    cover_selected: bool = False
    cover_status: str = ""
    status: str = ""

    def __post_init__(self) -> None:
        self.original = {key: getattr(self, key) for key in EDITABLE_FIELDS}


def run_osascript(script: str, progress_callback=None) -> str:
    with tempfile.TemporaryFile(mode="w+t") as output_file:
        process = subprocess.Popen(
            ["osascript", "-"],
            stdin=subprocess.PIPE,
            text=True,
            stdout=output_file,
            stderr=subprocess.PIPE,
        )
        if process.stdin is None or process.stderr is None:
            raise RuntimeError("Impossibile avviare AppleScript.")
        process.stdin.write(script)
        process.stdin.close()
        errors = []
        for line in process.stderr:
            line = line.rstrip("\n")
            if progress_callback and line.startswith("PROGRESS:"):
                progress_parts = line.removeprefix("PROGRESS:").split(":")
                if len(progress_parts) == 2:
                    try:
                        progress_callback(int(progress_parts[0]), int(progress_parts[1]))
                        continue
                    except ValueError:
                        pass
            if line:
                errors.append(line)
        return_code = process.wait()
        output_file.seek(0)
        output = output_file.read()
    if return_code:
        detail = "\n".join(errors).strip() or "AppleScript non riuscito."
        raise RuntimeError(detail)
    return output.rstrip("\n")


def load_music_library(progress_callback=None, max_tracks: int | None = None) -> list[Track]:
    if max_tracks is not None and max_tracks < 1:
        raise ValueError("Il limite dei brani deve essere maggiore di zero.")
    track_limit = (
        f"if totalTracks > {max_tracks} then set totalTracks to {max_tracks}"
        if max_tracks is not None else ""
    )
    script = r'''
on cleanField(valueToClean)
    set valueToClean to valueToClean as text
    set previousDelimiters to AppleScript's text item delimiters
    set AppleScript's text item delimiters to {tab, return, linefeed, (ASCII character 29), (ASCII character 30)}
    set fieldParts to text items of valueToClean
    set AppleScript's text item delimiters to " "
    set cleanedValue to fieldParts as text
    set AppleScript's text item delimiters to previousDelimiters
    return cleanedValue
end cleanField

tell application "Music"
    set libraryRows to {}
    set allFileTracks to file tracks of library playlist 1
    set totalTracks to count of allFileTracks
    __TRACK_LIMIT__
    repeat with trackIndex from 1 to totalTracks
        set musicTrack to item trackIndex of allFileTracks
        set trackId to (database ID of musicTrack) as text
        set trackName to my cleanField(name of musicTrack)
        set trackArtist to my cleanField(artist of musicTrack)
        set trackAlbum to my cleanField(album of musicTrack)
        set trackGenre to my cleanField(genre of musicTrack)
        set trackLocation to ""
        try
            set trackFile to location of musicTrack
            if trackFile is not missing value then
                set trackLocation to POSIX path of trackFile
            end if
        on error
            set trackLocation to ""
        end try
        set end of libraryRows to trackId & (ASCII character 30) & trackName & (ASCII character 30) & trackArtist & (ASCII character 30) & trackAlbum & (ASCII character 30) & trackGenre & (ASCII character 30) & my cleanField(trackLocation)
        log "PROGRESS:" & trackIndex & ":" & totalTracks
    end repeat
end tell

set previousDelimiters to AppleScript's text item delimiters
set AppleScript's text item delimiters to (ASCII character 29)
set resultText to libraryRows as text
set AppleScript's text item delimiters to previousDelimiters
return resultText
'''.replace("__TRACK_LIMIT__", track_limit)
    output = run_osascript(script, progress_callback)
    tracks: list[Track] = []
    for row in output.split(ROW_SEPARATOR):
        if not row:
            continue
        fields = row.split(FIELD_SEPARATOR)
        if len(fields) != 6:
            raise RuntimeError("Apple Music ha restituito una riga di libreria non valida.")
        database_id, name, artist, album, genre, location = fields
        tracks.append(Track(int(database_id), name, artist, album, genre, location))
    return tracks


def play_track_in_music(database_id: int) -> None:
    script = f'''tell application "Music"
    set targetTrack to first file track of library playlist 1 whose database ID is {int(database_id)}
    play targetTrack
end tell
'''
    run_osascript(script)


def applescript_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("\r", " ").replace("\n", " ") + '"'


def write_cover_to_file(audio_path: str, image_path: str) -> None:
    with open(image_path, "rb") as image_file:
        image_data = image_file.read()
    if image_path.lower().endswith(".png"):
        mime_type = "image/png"
        mp4_format = MP4Cover.FORMAT_PNG
    else:
        mime_type = "image/jpeg"
        mp4_format = MP4Cover.FORMAT_JPEG

    audio = MutagenFile(audio_path)
    if isinstance(audio, MP3):
        if audio.tags is None:
            audio.add_tags()
        audio.tags.delall("APIC")
        audio.tags.add(
            APIC(
                encoding=3,
                mime=mime_type,
                type=3,
                desc="Cover",
                data=image_data,
            )
        )
    elif isinstance(audio, MP4):
        audio["covr"] = [MP4Cover(image_data, imageformat=mp4_format)]
    elif isinstance(audio, FLAC):
        existing_pictures = [picture for picture in audio.pictures if picture.type != 3]
        audio.clear_pictures()
        for picture in existing_pictures:
            audio.add_picture(picture)
        picture = Picture()
        picture.type = 3
        picture.mime = mime_type
        picture.desc = "Cover"
        picture.data = image_data
        audio.add_picture(picture)
    elif isinstance(audio, (AIFF, WAVE)):
        if audio.tags is None:
            audio.add_tags()
        audio.tags.delall("APIC")
        audio.tags.add(
            APIC(
                encoding=3,
                mime=mime_type,
                type=3,
                desc="Cover",
                data=image_data,
            )
        )
    else:
        raise ValueError(
            f"Formato audio non supportato per la copertina: {os.path.basename(audio_path)}."
        )
    audio.save()


def apply_track_changes(tracks: list[Track]) -> dict[int, str]:
    blocks = []
    for track in tracks:
        changes = [
            f"        set {key} of targetTrack to {applescript_quote(getattr(track, key))}"
            for key in EDITABLE_FIELDS
            if getattr(track, key) != track.original[key]
        ]
        if not changes:
            continue
        track_id = int(track.database_id)
        blocks.append(
            f'''    try
        set targetTrack to first file track of library playlist 1 whose database ID is {track_id}
{chr(10).join(changes)}
        set end of outcomes to "{track_id}" & (ASCII character 30) & "OK"
    on error errorMessage number errorNumber
        set end of outcomes to "{track_id}" & (ASCII character 30) & my cleanField(errorMessage)
    end try'''
        )
    if not blocks:
        return {}

    script = '''on cleanField(valueToClean)
    set valueToClean to valueToClean as text
    set previousDelimiters to AppleScript's text item delimiters
    set AppleScript's text item delimiters to {tab, return, linefeed, (ASCII character 29), (ASCII character 30)}
    set fieldParts to text items of valueToClean
    set AppleScript's text item delimiters to " "
    set cleanedValue to fieldParts as text
    set AppleScript's text item delimiters to previousDelimiters
    return cleanedValue
end cleanField

tell application "Music"
    set outcomes to {}
''' + "\n".join(blocks) + '''
end tell

set previousDelimiters to AppleScript's text item delimiters
set AppleScript's text item delimiters to (ASCII character 29)
set resultText to outcomes as text
set AppleScript's text item delimiters to previousDelimiters
return resultText
'''
    output = run_osascript(script)
    results: dict[int, str] = {}
    for row in output.split(ROW_SEPARATOR):
        if row:
            track_id, _, message = row.partition(FIELD_SEPARATOR)
            results[int(track_id)] = message
    return results


def normalize_for_match(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    separated = "".join(
        " " if unicodedata.category(char)[0] in {"P", "Z"} else char
        for char in decomposed
    )
    ascii_value = separated.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", " ", ascii_value.casefold()).strip()


def infer_missing_artist_genres(
    tracks: list[Track],
    found_genres: dict[int, str],
) -> dict[int, str]:
    genre_counts: dict[str, dict[str, int]] = {}
    for track in tracks:
        genre = found_genres.get(track.database_id)
        artist = normalize_for_match(track.artist)
        if genre and artist:
            counts = genre_counts.setdefault(artist, {})
            counts[genre] = counts.get(genre, 0) + 1

    inferred: dict[int, str] = {}
    for track in tracks:
        artist = normalize_for_match(track.artist)
        counts = genre_counts.get(artist)
        if track.database_id not in found_genres and counts:
            inferred[track.database_id] = max(counts, key=lambda genre: counts[genre])
    return inferred


def find_catalog_match(
    name: str,
    artist: str,
    metadata_field: str | tuple[str, ...],
    cancel_event: threading.Event | None = None,
) -> dict[str, object] | None:
    check_lookup_cancelled(cancel_event)
    metadata_fields = (metadata_field,) if isinstance(metadata_field, str) else metadata_field
    query = urllib.parse.urlencode(
        {"term": f"{artist} {name}", "entity": "song", "limit": 20}
    )
    request = urllib.request.Request(
        f"https://itunes.apple.com/search?{query}",
        headers={"User-Agent": "MusicaTag/1.0"},
    )
    with urllib.request.urlopen(request, timeout=12, context=create_https_context()) as response:
        payload = json.load(response)
    check_lookup_cancelled(cancel_event)
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        raise RuntimeError("Il catalogo Apple ha restituito una risposta non valida.")

    wanted_name = normalize_for_match(name)
    wanted_artist = normalize_for_match(artist)
    best_score = 0.0
    best_result = None
    for result in payload.get("results", []):
        if not isinstance(result, dict):
            continue
        if not any(
            isinstance(result.get(field), str) and result[field]
            for field in metadata_fields
        ):
            continue
        result_name = result.get("trackName")
        result_artist = result.get("artistName")
        if not isinstance(result_name, str) or not isinstance(result_artist, str):
            continue
        candidate_name = normalize_for_match(result_name)
        candidate_artist = normalize_for_match(result_artist)
        if not candidate_name or not candidate_artist:
            continue
        title_score = (
            1.0 if candidate_name == wanted_name else
            0.85 if wanted_name in candidate_name or candidate_name in wanted_name else 0.0
        )
        artist_score = (
            1.0 if candidate_artist == wanted_artist else
            0.9 if wanted_artist in candidate_artist or candidate_artist in wanted_artist else 0.0
        )
        score = title_score * 0.75 + artist_score * 0.25
        if score >= 0.8 and score > best_score:
            best_score = score
            best_result = result
    return best_result


def find_deezer_match(
    name: str,
    artist: str,
    include_genre: bool = False,
    cancel_event: threading.Event | None = None,
) -> dict[str, object] | None:
    check_lookup_cancelled(cancel_event)
    query = urllib.parse.urlencode({"q": f"{artist} {name}", "limit": 25})
    request = urllib.request.Request(
        f"https://api.deezer.com/search/track?{query}",
        headers={"User-Agent": "MusicaTag/1.0"},
    )
    with urllib.request.urlopen(request, timeout=12, context=create_https_context()) as response:
        payload = json.load(response)
    check_lookup_cancelled(cancel_event)
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise RuntimeError("Deezer ha restituito una risposta di ricerca non valida.")

    wanted_name = normalize_for_match(name)
    wanted_artist = normalize_for_match(artist)
    best_score = 0.0
    best_result = None
    for result in payload["data"]:
        if not isinstance(result, dict):
            continue
        result_name = result.get("title")
        result_artist = result.get("artist")
        result_album = result.get("album")
        if not isinstance(result_name, str) or not isinstance(result_artist, dict):
            continue
        artist_name = result_artist.get("name")
        if not isinstance(artist_name, str):
            continue
        candidate_name = normalize_for_match(result_name)
        candidate_artist = normalize_for_match(artist_name)
        if not candidate_name or not candidate_artist:
            continue
        title_score = (
            1.0 if candidate_name == wanted_name else
            0.85 if wanted_name in candidate_name or candidate_name in wanted_name else 0.0
        )
        artist_score = (
            1.0 if candidate_artist == wanted_artist else
            0.9 if wanted_artist in candidate_artist or candidate_artist in wanted_artist else 0.0
        )
        score = title_score * 0.75 + artist_score * 0.25
        if score < 0.8 or score <= best_score:
            continue
        best_score = score
        cover_url = result_album.get("cover_xl") if isinstance(result_album, dict) else None
        album_id = result_album.get("id") if isinstance(result_album, dict) else None
        best_result = {
            "artworkUrl100": cover_url,
            "deezerAlbumId": album_id,
        }

    if not best_result or not include_genre:
        return best_result

    album_id = best_result.get("deezerAlbumId")
    if not isinstance(album_id, (int, str)) or not str(album_id).isdigit():
        return best_result
    check_lookup_cancelled(cancel_event)
    request = urllib.request.Request(
        f"https://api.deezer.com/album/{album_id}",
        headers={"User-Agent": "MusicaTag/1.0"},
    )
    with urllib.request.urlopen(request, timeout=12, context=create_https_context()) as response:
        album = json.load(response)
    check_lookup_cancelled(cancel_event)
    if not isinstance(album, dict):
        raise RuntimeError("Deezer ha restituito i dettagli dell'album in un formato non valido.")
    genres = album.get("genres")
    genre_items = genres.get("data") if isinstance(genres, dict) else None
    if isinstance(genre_items, list):
        for genre_item in genre_items:
            if isinstance(genre_item, dict):
                genre = genre_item.get("name")
                if isinstance(genre, str) and genre:
                    best_result["primaryGenreName"] = genre
                    break
    return best_result


def find_metadata_match(name: str, artist: str, metadata_field: str) -> dict[str, object] | None:
    try:
        match = find_catalog_match(name, artist, metadata_field)
    except urllib.error.HTTPError as error:
        if error.code not in {403, 429, 503}:
            raise
        match = None
    if match and match.get(metadata_field):
        return match
    return find_deezer_match(name, artist, include_genre=metadata_field == "primaryGenreName")


def find_track_information(
    name: str,
    artist: str,
    find_genre_enabled: bool,
    find_covers_enabled: bool,
    cancel_event: threading.Event | None = None,
    search_musicbrainz_genre: bool = False,
) -> tuple[str | None, str | None]:
    check_lookup_cancelled(cancel_event)
    metadata_fields = tuple(
        field for enabled, field in (
            (find_genre_enabled, "primaryGenreName"),
            (find_covers_enabled, "artworkUrl100"),
        ) if enabled
    )
    if not metadata_fields:
        return None, None

    try:
        match = find_catalog_match(name, artist, metadata_fields, cancel_event)
    except urllib.error.HTTPError as error:
        if error.code not in {403, 429, 503}:
            raise
        match = None

    genre = match.get("primaryGenreName") if match and find_genre_enabled else None
    if not isinstance(genre, str) or not genre:
        genre = None
    cover_url = match.get("artworkUrl100") if match and find_covers_enabled else None
    if not isinstance(cover_url, str) or not is_supported_artwork_url(cover_url):
        cover_url = None
    elif is_apple_artwork_url(cover_url):
        cover_url = re.sub(r"/\d+x\d+bb(?=\.)", "/600x600bb", cover_url)

    check_lookup_cancelled(cancel_event)
    if (find_genre_enabled and genre is None) or (find_covers_enabled and cover_url is None):
        fallback = find_deezer_match(
            name,
            artist,
            include_genre=find_genre_enabled and genre is None,
            cancel_event=cancel_event,
        )
        if fallback:
            if find_genre_enabled and genre is None:
                fallback_genre = fallback.get("primaryGenreName")
                genre = fallback_genre if isinstance(fallback_genre, str) else None
            if find_covers_enabled and cover_url is None:
                fallback_cover = fallback.get("artworkUrl100")
                if isinstance(fallback_cover, str) and is_supported_artwork_url(fallback_cover):
                    cover_url = fallback_cover
    if find_genre_enabled and genre is None and search_musicbrainz_genre:
        genre = find_musicbrainz_genre(artist, cancel_event)
    return genre, cover_url


def find_musicbrainz_genre(
    artist: str,
    cancel_event: threading.Event | None = None,
) -> str | None:
    artist_key = normalize_for_match(artist)
    if not artist_key:
        return None
    with MUSICBRAINZ_CACHE_LOCK:
        if artist_key in MUSICBRAINZ_GENRE_CACHE:
            return MUSICBRAINZ_GENRE_CACHE[artist_key]

    search_query = urllib.parse.urlencode({
        "query": f'artist:"{artist}"',
        "fmt": "json",
        "limit": 10,
    })
    search_payload = musicbrainz_request(
        f"https://musicbrainz.org/ws/2/artist/?{search_query}",
        cancel_event,
    )
    artists = search_payload.get("artists")
    if not isinstance(artists, list):
        raise RuntimeError("MusicBrainz non ha restituito l'elenco degli artisti.")

    matched_artist = None
    for candidate in artists:
        if not isinstance(candidate, dict):
            continue
        candidate_name = candidate.get("name")
        artist_id = candidate.get("id")
        if (
            isinstance(candidate_name, str)
            and normalize_for_match(candidate_name) == artist_key
            and isinstance(artist_id, str)
        ):
            matched_artist = artist_id
            break

    genre = None
    if matched_artist:
        detail_payload = musicbrainz_request(
            f"https://musicbrainz.org/ws/2/artist/{urllib.parse.quote(matched_artist)}"
            "?fmt=json&inc=genres",
            cancel_event,
        )
        genres = detail_payload.get("genres")
        if isinstance(genres, list):
            genre_counts = {
                item["name"]: item.get("count", 0)
                for item in genres
                if isinstance(item, dict)
                and isinstance(item.get("name"), str)
                and item["name"]
                and isinstance(item.get("count", 0), int)
            }
            if genre_counts:
                genre = max(genre_counts, key=lambda item: genre_counts[item])

    with MUSICBRAINZ_CACHE_LOCK:
        MUSICBRAINZ_GENRE_CACHE[artist_key] = genre
    return genre


def find_genre(name: str, artist: str) -> str | None:
    match = find_metadata_match(name, artist, "primaryGenreName")
    genre = match.get("primaryGenreName") if match else None
    return genre if isinstance(genre, str) and genre else None


def find_cover(name: str, artist: str) -> str | None:
    match = find_metadata_match(name, artist, "artworkUrl100")
    cover_url = match.get("artworkUrl100") if match else None
    if not isinstance(cover_url, str):
        return None
    if not is_supported_artwork_url(cover_url):
        return None
    if is_apple_artwork_url(cover_url):
        return re.sub(r"/\d+x\d+bb(?=\.)", "/600x600bb", cover_url)
    return cover_url


def pause_between_lookup_batches(
    completed: int,
    total: int,
    cancel_event: threading.Event | None = None,
) -> None:
    if completed < total and completed % LOOKUP_BATCH_SIZE == 0:
        if cancel_event:
            cancel_event.wait(LOOKUP_PAUSE_SECONDS)
        else:
            time.sleep(LOOKUP_PAUSE_SECONDS)


def is_apple_artwork_url(url: str) -> bool:
    parsed_url = urllib.parse.urlparse(url)
    hostname = parsed_url.hostname or ""
    return parsed_url.scheme == "https" and (
        hostname == "mzstatic.com" or hostname.endswith(".mzstatic.com")
    )


def is_supported_artwork_url(url: str) -> bool:
    parsed_url = urllib.parse.urlparse(url)
    hostname = parsed_url.hostname or ""
    return is_apple_artwork_url(url) or (
        parsed_url.scheme == "https" and hostname == "cdn-images.dzcdn.net"
    )


def download_cover(cover_url: str, directory: str, track_id: int) -> str:
    if not is_supported_artwork_url(cover_url):
        raise ValueError("Indirizzo della copertina non valido.")
    request = urllib.request.Request(cover_url, headers={"User-Agent": "MusicaTag/1.0"})
    with urllib.request.urlopen(request, timeout=20, context=create_https_context()) as response:
        if not is_supported_artwork_url(response.geturl()):
            raise ValueError("Il reindirizzamento della copertina non è sicuro.")
        content_type = response.headers.get_content_type()
        if content_type not in {"image/jpeg", "image/png"}:
            raise ValueError(f"Formato copertina non supportato: {content_type}.")
        data = response.read(15 * 1024 * 1024 + 1)
    if len(data) > 15 * 1024 * 1024:
        raise ValueError("La copertina supera il limite di 15 MB.")
    extension = ".jpg" if content_type == "image/jpeg" else ".png"
    path = f"{directory}/cover-{track_id}{extension}"
    with open(path, "wb") as image_file:
        image_file.write(data)
    return path


class MusicaTagApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("MusicaTag")
        self.root.geometry("1120x650")
        self.tracks: dict[int, Track] = {}
        self.events: queue.Queue[tuple] = queue.Queue()
        self.busy = False
        self.worker_done = False
        self.buttons: list[tk.Widget] = []
        self.search_cancel_event: threading.Event | None = None
        self.selected_track_ids: set[int] = set()
        self.pending_genre_edit: str | None = None
        self.last_preview_at = 0.0
        self._build_ui()
        self.root.after(100, self._poll_events)

    def _build_ui(self) -> None:
        toolbar = ttk.Frame(self.root, padding=10)
        toolbar.pack(fill="x")
        self.scan_first_1000_var = tk.BooleanVar(value=False)
        self.find_genre_var = tk.BooleanVar(value=True)
        self.find_covers_var = tk.BooleanVar(value=True)
        self.filter_missing_genre_var = tk.BooleanVar(value=False)
        self.search_musicbrainz_genre_var = tk.BooleanVar(value=False)

        library_row = ttk.Frame(toolbar)
        library_row.pack(fill="x", pady=(0, 4))
        load_button = ttk.Button(library_row, text="Carica libreria", command=self._load_library)
        load_button.pack(side="left", padx=(0, 6))
        self.buttons.append(load_button)
        scan_limit = ttk.Checkbutton(
            library_row,
            text="Scansiona solo i primi 1000 brani",
            variable=self.scan_first_1000_var,
        )
        scan_limit.pack(side="left", padx=(0, 10))
        self.buttons.append(scan_limit)

        selection_row = ttk.Frame(toolbar)
        selection_row.pack(fill="x", pady=(0, 4))
        select_all_button = ttk.Button(
            selection_row, text="Seleziona tutto", command=self._select_all
        )
        select_all_button.pack(side="left", padx=(0, 6))
        self.buttons.append(select_all_button)
        missing_genre_filter = ttk.Checkbutton(
            selection_row,
            text="Visualizza solo brani senza genere",
            variable=self.filter_missing_genre_var,
            command=self._refresh_track_list,
        )
        missing_genre_filter.pack(side="left", padx=(0, 6))
        self.buttons.append(missing_genre_filter)

        lookup_row = ttk.Frame(toolbar)
        lookup_row.pack(fill="x", pady=(0, 4))
        genre_checkbox = ttk.Checkbutton(
            lookup_row, text="Trova genere", variable=self.find_genre_var
        )
        genre_checkbox.pack(side="left", padx=(0, 6))
        self.buttons.append(genre_checkbox)
        covers_checkbox = ttk.Checkbutton(
            lookup_row, text="Trova copertine", variable=self.find_covers_var
        )
        covers_checkbox.pack(side="left", padx=(0, 6))
        self.buttons.append(covers_checkbox)
        musicbrainz_genre_checkbox = ttk.Checkbutton(
            lookup_row,
            text="Cerca genere online (MusicBrainz)",
            variable=self.search_musicbrainz_genre_var,
        )
        musicbrainz_genre_checkbox.pack(side="left", padx=(0, 6))
        self.buttons.append(musicbrainz_genre_checkbox)
        self.search_button = ttk.Button(
            lookup_row,
            text="Cerca informazioni",
            command=self._find_information,
            style="Search.TButton",
        )
        self.search_button_style = ttk.Style(self.root)
        self.search_button_style.configure("Search.TButton", foreground="white")
        self.search_button_style.configure("SearchStop.TButton", foreground="#ff3b30")
        self.search_button.pack(side="left", padx=(0, 6))
        self.buttons.append(self.search_button)
        apply_row = ttk.Frame(toolbar)
        apply_row.pack(fill="x")
        for label, command in (
            ("Apri copertina selezionata", self._open_selected_cover),
            ("Applica modifiche selezionate", self._apply_selected),
        ):
            button = ttk.Button(apply_row, text=label, command=command)
            button.pack(side="left", padx=(0, 6))
            self.buttons.append(button)

        table_frame = ttk.Frame(self.root, padding=(10, 0, 10, 6))
        table_frame.pack(fill="both", expand=True)
        columns = ("name", "artist", "album", "genre", "suggestion", "cover", "status")
        self.table = ttk.Treeview(table_frame, columns=columns, show="headings", selectmode="extended")
        headings = {
            "name": "Titolo",
            "artist": "Artista",
            "album": "Album",
            "genre": "Genere (modificabile)",
            "suggestion": "Genere suggerito",
            "cover": "Copertina",
            "status": "Stato",
        }
        widths = {"name": 190, "artist": 160, "album": 170, "genre": 140, "suggestion": 130, "cover": 125, "status": 150}
        for column in columns:
            self.table.heading(column, text=headings[column])
            self.table.column(column, width=widths[column], minwidth=90, stretch=True)
        y_scroll = ttk.Scrollbar(table_frame, orient="vertical", command=self.table.yview)
        x_scroll = ttk.Scrollbar(table_frame, orient="horizontal", command=self.table.xview)
        self.table.configure(yscrollcommand=y_scroll.set, xscrollcommand=x_scroll.set)
        self.table.grid(row=0, column=0, sticky="nsew")
        y_scroll.grid(row=0, column=1, sticky="ns")
        x_scroll.grid(row=1, column=0, sticky="ew")
        table_frame.rowconfigure(0, weight=1)
        table_frame.columnconfigure(0, weight=1)
        self.table.bind("<ButtonRelease-1>", self._schedule_genre_edit, add="+")
        self.table.bind("<Double-Button-1>", self._play_preview, add="+")
        self.table.bind("<Command-Double-1>", self._edit_cell)
        self.table.bind("<Control-Double-1>", self._edit_cell)
        self.table.bind("<<TreeviewSelect>>", self._track_selection_changed)

        status_frame = ttk.Frame(self.root, padding=(10, 4))
        status_frame.pack(fill="x")
        self.status_var = tk.StringVar(value="Carica la libreria di Apple Music per iniziare.")
        ttk.Label(status_frame, textvariable=self.status_var, anchor="w").pack(side="left", fill="x", expand=True)
        self.progress_bar = ttk.Progressbar(status_frame, mode="determinate", maximum=1)
        self.progress_bar.pack(side="right", padx=(10, 0), fill="x", expand=True)

    def _start_worker(
        self,
        task,
        busy_text: str,
        keep_enabled: tuple[tk.Widget, ...] = (),
    ) -> None:
        if self.busy:
            return
        self.busy = True
        self.worker_done = False
        self.status_var.set(busy_text)
        self.progress_bar.configure(maximum=1, value=0)
        for button in self.buttons:
            button.configure(state="normal" if button in keep_enabled else "disabled")

        def run() -> None:
            try:
                task()
                self.events.put(("finished",))
            except (
                OSError,
                RuntimeError,
                ValueError,
                MutagenError,
                TimeoutError,
                urllib.error.URLError,
                subprocess.SubprocessError,
            ) as error:
                self.events.put(("error", str(error)))
            finally:
                self.events.put(("worker-done",))

        threading.Thread(target=run, daemon=True).start()

    def _load_library(self) -> None:
        max_tracks = 1000 if self.scan_first_1000_var.get() else None

        def load() -> None:
            tracks = load_music_library(
                lambda current, total: self.events.put(("progress", current, total, "Lettura libreria")),
                max_tracks=max_tracks,
            )
            self.events.put(("library", tracks))
        self._start_worker(load, "Lettura della libreria di Apple Music…")

    def _find_information(self) -> None:
        if self.busy:
            if self.search_cancel_event:
                self.search_cancel_event.set()
                self.search_button.configure(
                    state="disabled",
                    style="SearchStop.TButton",
                )
                self.status_var.set("Arresto della ricerca in corso…")
            return
        if not self.tracks:
            messagebox.showinfo("MusicaTag", "Carica prima la libreria di Apple Music.")
            return
        find_genre_enabled = self.find_genre_var.get()
        find_covers_enabled = self.find_covers_var.get()
        search_musicbrainz_genre = self.search_musicbrainz_genre_var.get()
        if not find_genre_enabled and not find_covers_enabled:
            messagebox.showinfo(
                "MusicaTag",
                "Seleziona almeno una ricerca: genere o copertine.",
            )
            return
        if search_musicbrainz_genre and not find_genre_enabled:
            messagebox.showinfo(
                "MusicaTag",
                "Per cercare il genere su MusicBrainz attiva anche Trova genere.",
            )
            return
        tracks = self._selected_tracks()
        if not tracks:
            messagebox.showinfo(
                "MusicaTag",
                "Seleziona uno o più brani prima di cercare le informazioni.",
            )
            return

        cancel_event = threading.Event()
        self.search_cancel_event = cancel_event
        self.search_button.configure(
            text="Stop",
            style="SearchStop.TButton",
        )

        def lookup() -> None:
            total = len(tracks)
            found_genres: dict[int, str] = {}
            genre_statuses: dict[int, str] = {}
            processed_genre_ids: set[int] = set()
            cancelled = False
            for index, track in enumerate(tracks, start=1):
                if cancel_event.is_set():
                    cancelled = True
                    break
                try:
                    if not track.name.strip() or not track.artist.strip():
                        genre = cover_url = None
                        missing_status = "Titolo o artista mancante"
                    else:
                        genre, cover_url = find_track_information(
                            track.name,
                            track.artist,
                            find_genre_enabled,
                            find_covers_enabled,
                            cancel_event,
                            search_musicbrainz_genre,
                        )
                        missing_status = ""
                    check_lookup_cancelled(cancel_event)
                except LookupCancelled:
                    cancelled = True
                    break
                if find_genre_enabled:
                    status = (
                        missing_status or (
                            "Trovato, pronto da applicare"
                            if genre else "Nessun risultato affidabile"
                        )
                    )
                    processed_genre_ids.add(track.database_id)
                    genre_statuses[track.database_id] = status
                    if genre:
                        found_genres[track.database_id] = genre
                if find_covers_enabled:
                    status = (
                        missing_status or (
                            "Trovata, pronta da applicare" if cover_url else "Non trovata"
                        )
                    )
                    self.events.put(
                        ("cover", track.database_id, cover_url or "", status)
                    )
                action = "Ricerca informazioni"
                self.events.put(("progress", index, total, action))
                pause_between_lookup_batches(index, total, cancel_event)
                if cancel_event.is_set():
                    cancelled = True
                    break
            if find_genre_enabled:
                processed_tracks = [
                    track for track in tracks
                    if track.database_id in processed_genre_ids
                ]
                inferred_genres = infer_missing_artist_genres(
                    processed_tracks,
                    found_genres,
                )
                for track in processed_tracks:
                    genre = found_genres.get(track.database_id)
                    status = genre_statuses[track.database_id]
                    if track.database_id in inferred_genres:
                        genre = inferred_genres[track.database_id]
                        status = f"Genere dedotto dall'artista: {genre}"
                    self.events.put(
                        ("suggestion", track.database_id, genre or "", status)
                    )
            if cancelled:
                self.events.put(("cancelled",))
        self._start_worker(
            lookup,
            "Ricerca delle informazioni nei cataloghi online…",
            keep_enabled=(self.search_button,),
        )

    def _selected_tracks(self) -> list[Track]:
        self._track_selection_changed()
        return [
            track for track in self.tracks.values()
            if track.database_id in self.selected_track_ids
        ]

    def _open_selected_cover(self) -> None:
        tracks = [track for track in self._selected_tracks() if track.cover_url]
        if not tracks:
            messagebox.showinfo("MusicaTag", "Seleziona un brano con una copertina trovata.")
            return
        webbrowser.open(tracks[0].cover_url)

    def _apply_selected(self) -> None:
        selected = self._selected_tracks()
        changed = [
            track for track in selected
            if self._is_dirty(track) or (track.cover_selected and track.cover_url)
        ]
        if not changed:
            messagebox.showinfo(
                "MusicaTag",
                "Nessun risultato trovato o modifica da applicare nei brani selezionati.",
            )
            return
        if not messagebox.askyesno(
            "Conferma modifiche",
            f"Vuoi scrivere tag in Apple Music e copertine nei file locali di {len(changed)} brani selezionati?",
        ):
            return

        tracks_needing_path = [
            track for track in changed
            if track.cover_selected and track.cover_url and not track.location
        ]
        for track in tracks_needing_path:
            local_path = filedialog.askopenfilename(
                parent=self.root,
                title=f"Seleziona il file audio di {track.name} — {track.artist}",
                filetypes=[
                    ("File audio supportati", "*.mp3 *.m4a *.mp4 *.flac *.aiff *.aif *.wav"),
                    ("Tutti i file", "*"),
                ],
            )
            if not local_path:
                messagebox.showinfo(
                    "MusicaTag",
                    "Applicazione annullata: seleziona il file locale per ogni brano con una copertina da incorporare.",
                )
                return
            track.location = local_path

        def apply() -> None:
            applied_cover_ids = set()
            cover_errors = {}
            with tempfile.TemporaryDirectory(prefix="musicatag-") as directory:
                for track in changed:
                    if track.cover_selected and track.cover_url:
                        try:
                            image_path = download_cover(
                                track.cover_url, directory, track.database_id
                            )
                            if not track.location:
                                raise ValueError("Il percorso del file locale non è disponibile.")
                            write_cover_to_file(track.location, image_path)
                            applied_cover_ids.add(track.database_id)
                        except (
                            OSError,
                            RuntimeError,
                            ValueError,
                            MutagenError,
                            urllib.error.URLError,
                        ) as error:
                            cover_errors[track.database_id] = str(error)
                dirty_tracks = [track for track in changed if self._is_dirty(track)]
                results = apply_track_changes(dirty_tracks)
                dirty_track_ids = {track.database_id for track in dirty_tracks}
                for track in changed:
                    if (
                        track.database_id in applied_cover_ids
                        and track.database_id not in dirty_track_ids
                    ):
                        results[track.database_id] = "OK"
            self.events.put(("applied", changed, results, applied_cover_ids, cover_errors))
        self._start_worker(apply, "Scrittura di tag e copertine in Apple Music…")

    def _select_all(self) -> None:
        visible_ids = self.table.get_children()
        self.selected_track_ids = {int(track_id) for track_id in visible_ids}
        self.table.selection_set(visible_ids)

    def _track_selection_changed(self, _event=None) -> None:
        visible_ids = {int(track_id) for track_id in self.table.get_children()}
        selected_visible_ids = {int(track_id) for track_id in self.table.selection()}
        self.selected_track_ids.difference_update(visible_ids)
        self.selected_track_ids.update(selected_visible_ids)

    def _refresh_track_list(self) -> None:
        self._track_selection_changed()
        self.table.delete(*self.table.get_children())
        for track in self.tracks.values():
            if self._is_track_visible(track):
                self.table.insert(
                    "",
                    "end",
                    iid=str(track.database_id),
                    values=self._track_values(track),
                )
        visible_selection = [
            str(track_id)
            for track_id in self.selected_track_ids
            if self.table.exists(str(track_id))
        ]
        if visible_selection:
            self.table.selection_set(visible_selection)

    def _is_track_visible(self, track: Track) -> bool:
        return not (
            self.filter_missing_genre_var.get() and track.genre.strip()
        )

    @staticmethod
    def _track_values(track: Track) -> tuple[str, ...]:
        return (
            track.name,
            track.artist,
            track.album,
            track.genre,
            track.suggestion,
            track.cover_status,
            track.status,
        )

    def _play_preview(self, event) -> None:
        self.last_preview_at = time.monotonic()
        if self.pending_genre_edit:
            self.root.after_cancel(self.pending_genre_edit)
            self.pending_genre_edit = None
        row_id = self.table.identify_row(event.y)
        if not row_id:
            return
        track = self.tracks.get(int(row_id))
        if not track:
            return

        self._start_worker(
            lambda: play_track_in_music(track.database_id),
            f"Riproduzione con Musica: {track.name}…",
        )

    def _schedule_genre_edit(self, event) -> None:
        if time.monotonic() - self.last_preview_at < 0.35:
            return
        if self.table.identify("region", event.x, event.y) != "cell":
            return
        column_id = self.table.identify_column(event.x)
        if not column_id or self.table["columns"][int(column_id[1:]) - 1] != "genre":
            return
        row_id = self.table.identify_row(event.y)
        if not row_id:
            return
        if self.pending_genre_edit:
            self.root.after_cancel(self.pending_genre_edit)
        self.pending_genre_edit = self.root.after(
            250,
            lambda: self._begin_genre_edit(row_id, column_id),
        )

    def _begin_genre_edit(self, row_id: str, column_id: str) -> None:
        self.pending_genre_edit = None
        track = self.tracks.get(int(row_id))
        if not track:
            return
        bounds = self.table.bbox(row_id, column_id)
        if not bounds:
            return
        x, y, width, height = bounds
        editor = ttk.Entry(self.table)
        editor.insert(0, track.genre)
        editor.select_range(0, "end")
        editor.place(x=x, y=y, width=width, height=height)
        editor.focus_set()

        def finish(_event=None) -> None:
            if not editor.winfo_exists():
                return
            track.genre = editor.get().strip()
            track.status = "Modificato" if self._is_dirty(track) else ""
            editor.destroy()
            self._refresh_track(track)

        editor.bind("<Return>", finish)
        editor.bind("<FocusOut>", finish)
        editor.bind("<Escape>", lambda _event: editor.destroy())

    def _edit_cell(self, event) -> None:
        region = self.table.identify("region", event.x, event.y)
        if region != "cell":
            return
        row_id = self.table.identify_row(event.y)
        column_id = self.table.identify_column(event.x)
        column = self.table["columns"][int(column_id[1:]) - 1]
        if column not in EDITABLE_FIELDS or not row_id:
            return
        bounds = self.table.bbox(row_id, column_id)
        if not bounds:
            return
        x, y, width, height = bounds
        track = self.tracks[int(row_id)]
        editor = ttk.Entry(self.table)
        editor.insert(0, getattr(track, column))
        editor.select_range(0, "end")
        editor.place(x=x, y=y, width=width, height=height)
        editor.focus_set()

        def finish(_event=None) -> None:
            if not editor.winfo_exists():
                return
            setattr(track, column, editor.get().strip())
            track.status = "Modificato" if self._is_dirty(track) else ""
            editor.destroy()
            self._refresh_track(track)

        editor.bind("<Return>", finish)
        editor.bind("<FocusOut>", finish)
        editor.bind("<Escape>", lambda _event: editor.destroy())

    @staticmethod
    def _is_dirty(track: Track) -> bool:
        return any(getattr(track, key) != track.original[key] for key in EDITABLE_FIELDS)

    def _refresh_track(self, track: Track) -> None:
        item_id = str(track.database_id)
        exists = self.table.exists(item_id)
        if not self._is_track_visible(track):
            if exists:
                self._track_selection_changed()
                if item_id in self.table.selection():
                    self.selected_track_ids.add(track.database_id)
                self.table.delete(item_id)
            return
        if exists:
            self.table.item(item_id, values=self._track_values(track))
        else:
            self.table.insert(
                "",
                "end",
                iid=item_id,
                values=self._track_values(track),
            )
            if track.database_id in self.selected_track_ids:
                self.table.selection_add(item_id)

    def _poll_events(self) -> None:
        while True:
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            kind = event[0]
            if kind == "library":
                loaded_tracks = event[1]
                self.tracks = {track.database_id: track for track in loaded_tracks}
                self.selected_track_ids.clear()
                self._refresh_track_list()
                self.status_var.set(f"Brani locali caricati: {len(loaded_tracks)}.")
            elif kind == "suggestion":
                _, track_id, suggestion, status = event
                track = self.tracks.get(track_id)
                if track:
                    track.suggestion = suggestion
                    if suggestion:
                        track.genre = suggestion
                    track.status = status
                    self._refresh_track(track)
            elif kind == "cover":
                _, track_id, cover_url, status = event
                track = self.tracks.get(track_id)
                if track:
                    track.cover_url = cover_url
                    track.cover_selected = bool(cover_url)
                    track.cover_status = status
                    self._refresh_track(track)
            elif kind == "progress":
                _, current, total, action = event
                self.status_var.set(f"{action}: {current}/{total}…")
                self.progress_bar.configure(maximum=max(total, 1), value=current)
            elif kind == "applied":
                _, tracks, results, applied_cover_ids, cover_errors = event
                applied_cover_ids = set(applied_cover_ids)
                succeeded = 0
                for track in tracks:
                    result = results.get(track.database_id, "Apple Music non ha restituito l'esito.")
                    if result == "OK":
                        track.original = {key: getattr(track, key) for key in EDITABLE_FIELDS}
                        if track.database_id in applied_cover_ids:
                            track.cover_selected = False
                            track.cover_status = "Applicata"
                        elif track.database_id in cover_errors:
                            track.cover_status = f"Errore: {cover_errors[track.database_id]}"
                        track.status = "Salvato" if track.database_id not in cover_errors else "Tag salvati; copertina non applicata"
                        succeeded += 1
                    elif track.database_id in cover_errors and track.database_id not in results:
                        track.status = "Copertina non applicata"
                        track.cover_status = f"Errore: {cover_errors[track.database_id]}"
                    else:
                        track.status = f"Errore: {result}"
                        if track.database_id in applied_cover_ids:
                            track.cover_selected = False
                            track.cover_status = "Applicata"
                        if track.database_id in cover_errors:
                            track.cover_status = f"Errore: {cover_errors[track.database_id]}"
                    self._refresh_track(track)
                self.status_var.set(f"Modifiche salvate: {succeeded}/{len(tracks)}.")
            elif kind == "error":
                self.status_var.set("Operazione interrotta.")
                messagebox.showerror("MusicaTag", event[1])
            elif kind == "cancelled":
                self.status_var.set("Ricerca interrotta.")
            elif kind == "finished":
                self.status_var.set(self.status_var.get().replace("…", " completata."))
            elif kind == "worker-done":
                self.worker_done = True
            self.events.task_done()

        if self.busy and self.worker_done and self.events.empty():
            self.busy = False
            for button in self.buttons:
                button.configure(state="normal")
            self.search_button.configure(
                text="Cerca informazioni",
                style="Search.TButton",
            )
            self.search_cancel_event = None
        self.root.after(100, self._poll_events)


def main() -> None:
    root = tk.Tk()
    MusicaTagApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
