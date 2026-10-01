import io
import threading
import urllib.error
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import main


class GenreMatchingTests(unittest.TestCase):
    def test_normalization_ignores_accents_and_punctuation(self):
        self.assertEqual(main.normalize_for_match("Beyoncé — Déjà Vu!"), "beyonce deja vu")

    def test_infers_missing_artist_genre_from_the_most_frequent_found_genre(self):
        tracks = [
            main.Track(1, "Brano 1", "Beyoncé", "", ""),
            main.Track(2, "Brano 2", "Beyonce", "", ""),
            main.Track(3, "Brano 3", "Beyoncé", "", ""),
            main.Track(4, "Brano 4", "Altro artista", "", ""),
            main.Track(5, "Brano 5", "Altro artista", "", ""),
        ]

        self.assertEqual(
            main.infer_missing_artist_genres(
                tracks,
                {1: "Pop", 2: "R&B", 4: "Jazz", 5: "Jazz"},
            ),
            {3: "Pop"},
        )

    def test_infers_only_among_the_provided_tracks(self):
        tracks = [
            main.Track(1, "Brano 1", "Artista", "", ""),
            main.Track(2, "Brano 2", "Artista", "", ""),
            main.Track(3, "Brano 3", "Artista", "", ""),
        ]

        self.assertEqual(
            main.infer_missing_artist_genres(tracks[:2], {1: "Rock"}),
            {2: "Rock"},
        )

    @patch("main.find_musicbrainz_genre", return_value="Jazz")
    @patch("main.find_deezer_match", return_value=None)
    @patch("main.find_catalog_match", return_value=None)
    def test_musicbrainz_search_is_used_only_as_genre_fallback(
        self, _catalog, _deezer, musicbrainz
    ):
        self.assertEqual(
            main.find_track_information(
                "Titolo", "Artista", True, False, search_musicbrainz_genre=True
            ),
            ("Jazz", None),
        )
        musicbrainz.assert_called_once_with("Artista", None)

    @patch("main.musicbrainz_request", side_effect=[
        {
            "artists": [
                {"id": "artist-id", "name": "a-ha"},
                {"id": "other-id", "name": "Other"},
            ]
        },
        {
            "genres": [
                {"name": "synth-pop", "count": 5},
                {"name": "pop", "count": 2},
            ]
        },
    ])
    def test_musicbrainz_returns_most_frequent_artist_genre(self, request):
        main.MUSICBRAINZ_GENRE_CACHE.clear()
        self.assertEqual(main.find_musicbrainz_genre("a‐ha"), "synth-pop")
        self.assertEqual(request.call_count, 2)
        self.assertIn("/ws/2/artist/", request.call_args_list[0].args[0])
        self.assertIn("/ws/2/artist/artist-id?", request.call_args_list[1].args[0])

    @patch("main.musicbrainz_request", side_effect=[
        {"artists": [{"id": "artist-id", "name": "Artista"}]},
        {"genres": [{"name": "jazz", "count": 3}]},
    ])
    def test_musicbrainz_caches_artist_genre(self, request):
        main.MUSICBRAINZ_GENRE_CACHE.clear()
        self.assertEqual(main.find_musicbrainz_genre("Artista"), "jazz")
        self.assertEqual(main.find_musicbrainz_genre("Artista"), "jazz")
        self.assertEqual(request.call_count, 2)

    @patch("main.urllib.request.urlopen")
    def test_finds_genre_for_matching_title_and_artist(self, urlopen):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return b'{"results":[{"trackName":"La canzone","artistName":"Artista","primaryGenreName":"Pop"}]}'

        urlopen.return_value = Response()
        with patch("main.json.load", return_value={
            "results": [
                {"trackName": "La canzone", "artistName": "Artista", "primaryGenreName": "Pop"}
            ]
        }):
            self.assertEqual(main.find_genre("La canzone", "Artista"), "Pop")

    @patch("main.urllib.request.urlopen")
    def test_rejects_unreliable_search_result(self, urlopen):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        urlopen.return_value = Response()
        with patch("main.json.load", side_effect=[
            {
                "results": [
                    {"trackName": "Altro brano", "artistName": "Altro artista", "primaryGenreName": "Rock"}
                ]
            },
            {"data": []},
        ]):
            self.assertIsNone(main.find_genre("La canzone", "Artista"))

    @patch("main.find_deezer_match", return_value={"primaryGenreName": "Rock"})
    @patch("main.find_catalog_match", side_effect=urllib.error.HTTPError(
        "https://itunes.apple.com/search", 429, "Too Many Requests", {}, None
    ))
    def test_falls_back_to_deezer_when_apple_rate_limits(self, _apple, deezer):
        self.assertEqual(main.find_genre("La canzone", "Artista"), "Rock")
        deezer.assert_called_once_with("La canzone", "Artista", include_genre=True)

    @patch("main.find_deezer_match", return_value={
        "artworkUrl100": "https://cdn-images.dzcdn.net/images/cover/example/1000x1000.jpg"
    })
    @patch("main.find_catalog_match", return_value=None)
    def test_uses_deezer_cover_when_apple_has_no_match(self, _apple, _deezer):
        self.assertEqual(
            main.find_cover("La canzone", "Artista"),
            "https://cdn-images.dzcdn.net/images/cover/example/1000x1000.jpg",
        )

    @patch("main.find_deezer_match")
    @patch("main.find_catalog_match", return_value={
        "primaryGenreName": "Pop",
        "artworkUrl100": "https://is1-ssl.mzstatic.com/image/100x100bb.jpg",
    })
    def test_combined_lookup_returns_genre_and_cover_from_one_catalog_result(
        self, apple, deezer
    ):
        self.assertEqual(
            main.find_track_information("La canzone", "Artista", True, True),
            ("Pop", "https://is1-ssl.mzstatic.com/image/600x600bb.jpg"),
        )
        apple.assert_called_once_with(
            "La canzone", "Artista", ("primaryGenreName", "artworkUrl100"), None
        )
        deezer.assert_not_called()

    @patch("main.find_catalog_match")
    def test_cancelled_combined_lookup_does_not_make_catalog_request(self, apple):
        cancel_event = threading.Event()
        cancel_event.set()

        with self.assertRaises(main.LookupCancelled):
            main.find_track_information("La canzone", "Artista", True, True, cancel_event)

        apple.assert_not_called()

    @patch("main.urllib.request.urlopen")
    def test_cancelled_combined_lookup_stops_after_current_catalog_response(self, urlopen):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        cancel_event = threading.Event()
        urlopen.return_value = Response()

        def load_payload(_response):
            cancel_event.set()
            return {"results": []}

        with patch("main.json.load", side_effect=load_payload):
            with self.assertRaises(main.LookupCancelled):
                main.find_track_information(
                    "La canzone", "Artista", True, True, cancel_event
                )

        urlopen.assert_called_once()

    @patch("main.urllib.request.urlopen")
    def test_deezer_lookup_matches_track_and_reads_album_genre(self, urlopen):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        urlopen.return_value = Response()
        with patch("main.json.load", side_effect=[
            {
                "data": [{
                    "title": "La canzone",
                    "artist": {"name": "Artista"},
                    "album": {
                        "id": 123,
                        "cover_xl": "https://cdn-images.dzcdn.net/images/cover/example/1000x1000.jpg",
                    },
                }]
            },
            {"genres": {"data": [{"name": "Pop"}]}},
        ]):
            self.assertEqual(
                main.find_deezer_match("La canzone", "Artista", include_genre=True),
                {
                    "artworkUrl100": "https://cdn-images.dzcdn.net/images/cover/example/1000x1000.jpg",
                    "deezerAlbumId": 123,
                    "primaryGenreName": "Pop",
                },
            )
        self.assertIn("api.deezer.com/search/track?", urlopen.call_args_list[0].args[0].full_url)
        self.assertIn("api.deezer.com/album/123", urlopen.call_args_list[1].args[0].full_url)

    @patch("main.urllib.request.urlopen")
    def test_finds_high_resolution_cover_for_matching_track(self, urlopen):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        urlopen.return_value = Response()
        with patch("main.json.load", return_value={
            "results": [
                {
                    "trackName": "La canzone",
                    "artistName": "Artista",
                    "artworkUrl100": "https://is1-ssl.mzstatic.com/image/100x100bb.jpg",
                }
            ]
        }):
            self.assertEqual(
                main.find_cover("La canzone", "Artista"),
                "https://is1-ssl.mzstatic.com/image/600x600bb.jpg",
            )

    def test_rejects_untrusted_cover_image_hosts(self):
        self.assertFalse(main.is_supported_artwork_url("https://example.com/cover.jpg"))
        self.assertTrue(main.is_supported_artwork_url(
            "https://cdn-images.dzcdn.net/images/cover/example.jpg"
        ))

    def test_tag_write_does_not_try_to_create_artwork_in_music(self):
        track = main.Track(42, "Titolo", "Artista", "Album", "Pop")
        track.genre = "Rock"
        with patch("main.run_osascript", return_value="42\x1eOK") as run_script:
            result = main.apply_track_changes([track])
        script = run_script.call_args.args[0]
        self.assertIn('set genre of targetTrack to "Rock"', script)
        self.assertNotIn("make new artwork", script)
        self.assertEqual(result, {42: "OK"})

    def test_writes_jpeg_cover_to_local_mp3_file(self):
        import tempfile
        from pathlib import Path
        from mutagen.id3 import APIC, ID3

        with tempfile.TemporaryDirectory() as directory:
            audio_path = str(Path(directory) / "track.mp3")
            image_path = str(Path(directory) / "cover.jpg")
            ID3().save(audio_path)
            mpeg_frame = b"\xff\xfb\x90\x64" + bytes(413)
            with open(audio_path, "ab") as audio_file:
                audio_file.write(mpeg_frame * 20)
            Path(image_path).write_bytes(b"jpeg-data")

            main.write_cover_to_file(audio_path, image_path)

            pictures = ID3(audio_path).getall("APIC")
            self.assertEqual(len(pictures), 1)
            self.assertEqual(pictures[0].mime, "image/jpeg")
            self.assertEqual(pictures[0].data, b"jpeg-data")


class LibraryLoadingTests(unittest.TestCase):
    @patch("main.run_osascript")
    def test_loads_track_even_when_local_file_location_is_missing(self, run_osascript):
        run_osascript.return_value = "42\x1eTitolo\x1eArtista\x1eAlbum\x1ePop\x1e"

        tracks = main.load_music_library()

        self.assertEqual(len(tracks), 1)
        self.assertEqual(tracks[0].database_id, 42)
        self.assertEqual(tracks[0].name, "Titolo")
        self.assertEqual(tracks[0].location, "")
        script = run_osascript.call_args.args[0]
        self.assertIn('set trackLocation to ""', script)
        self.assertIn("set trackFile to location of musicTrack", script)
        self.assertIn("if trackFile is not missing value then", script)
        self.assertIn("set trackLocation to POSIX path of trackFile", script)
        self.assertNotIn("try\n            set trackId", script)

    @patch("main.run_osascript")
    def test_limits_library_scan_to_requested_number_of_tracks(self, run_osascript):
        run_osascript.return_value = ""

        self.assertEqual(main.load_music_library(max_tracks=1000), [])

        script = run_osascript.call_args.args[0]
        self.assertIn("if totalTracks > 1000 then set totalTracks to 1000", script)

    def test_rejects_non_positive_track_limit(self):
        with self.assertRaises(ValueError):
            main.load_music_library(max_tracks=0)

    @patch("main.run_osascript")
    def test_plays_track_in_music_when_no_local_file_path_is_available(self, run_osascript):
        main.play_track_in_music(42)

        script = run_osascript.call_args.args[0]
        self.assertIn("whose database ID is 42", script)
        self.assertIn("play targetTrack", script)

    @patch("main.play_track_in_music")
    @patch.object(main.MusicaTagApp, "_start_worker")
    def test_double_click_preview_handles_treeview_tree_region(self, start_worker, play):
        class FakeTable:
            def identify_row(self, _y):
                return "42"

        app = main.MusicaTagApp.__new__(main.MusicaTagApp)
        app.table = FakeTable()
        app.pending_genre_edit = None
        app.last_preview_at = 0.0
        app.tracks = {
            42: main.Track(42, "Titolo", "Artista", "Album", "Pop", "/music/track.mp3")
        }

        app._play_preview(SimpleNamespace(x=10, y=10))

        self.assertEqual(start_worker.call_args.args[1], "Riproduzione con Musica: Titolo…")
        start_worker.call_args.args[0]()
        play.assert_called_once_with(42)

    @patch("main.time.sleep")
    def test_pauses_after_each_full_lookup_batch(self, sleep):
        main.pause_between_lookup_batches(40, 41)

        sleep.assert_called_once_with(3)

    @patch("main.time.sleep")
    def test_does_not_pause_after_last_or_partial_lookup_batch(self, sleep):
        main.pause_between_lookup_batches(40, 40)
        main.pause_between_lookup_batches(39, 60)

        sleep.assert_not_called()


class ProgressTests(unittest.TestCase):
    @patch("main.subprocess.Popen")
    def test_osascript_reports_progress_and_returns_final_output(self, popen):
        class FakeProcess:
            def __init__(self, _args, stdout, **_kwargs):
                self.stdin = io.StringIO()
                self.stderr = io.StringIO("PROGRESS:2:4\n")
                stdout.write("library result")

            def wait(self):
                return 0

        popen.side_effect = FakeProcess
        progress = []

        result = main.run_osascript(
            "script",
            lambda current, total: progress.append((current, total)),
        )

        self.assertEqual(result, "library result")
        self.assertEqual(progress, [(2, 4)])


class HttpsContextTests(unittest.TestCase):
    def test_uses_macos_certificate_bundle_when_python_bundle_is_empty(self):
        class FakeContext:
            ca_count = 0

            def cert_store_stats(self):
                return {"x509_ca": self.ca_count}

            def load_verify_locations(self, cafile):
                self.loaded_bundle = cafile
                self.ca_count = 1

        context = FakeContext()
        with (
            patch("main.ssl.create_default_context", return_value=context),
            patch("main.ssl.get_default_verify_paths", return_value=type(
                "VerifyPaths", (), {"cafile": None}
            )()),
            patch("main.os.path.isfile", return_value=True),
        ):
            self.assertIs(main.create_https_context(), context)
        self.assertEqual(context.loaded_bundle, "/etc/ssl/cert.pem")


if __name__ == "__main__":
    unittest.main()
