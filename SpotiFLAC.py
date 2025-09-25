import sys
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import requests
import re
import asyncio
from packaging import version
import qdarkstyle
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
import time
from collections import deque

from PyQt6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLineEdit,
    QLabel, QFileDialog, QListWidget, QTextEdit, QTabWidget, QButtonGroup, QRadioButton,
    QAbstractItemView, QProgressBar, QCheckBox, QDialog, QFrame,
    QDialogButtonBox, QComboBox, QStyledItemDelegate, QGridLayout,
    QGraphicsDropShadowEffect
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QUrl, QTimer, QTime, QSettings, QSize
from PyQt6.QtGui import QIcon, QTextCursor, QDesktopServices, QPixmap, QBrush, QColor
from PyQt6.QtNetwork import QNetworkAccessManager, QNetworkRequest, QNetworkReply

from getMetadata import get_filtered_data, parse_uri, SpotifyInvalidUrlException
from qobuzDL import QobuzDownloader
from tidalDL import TidalDownloader
from deezerDL import DeezerDownloader
from amazonDL import LucidaDownloader

@dataclass
class Track:
    external_urls: str
    title: str
    artists: str
    album: str
    track_number: int
    duration_ms: int
    id: str
    isrc: str = ""
    source_name: str = ""  # Track which source this track came from
    source_type: str = ""  # Track the type of source (spotify_track, spotify_album, spotify_playlist, playlist_file)

class MetadataFetchWorker(QThread):
    finished = pyqtSignal(dict)
    error = pyqtSignal(str)

    def __init__(self, url):
        super().__init__()
        self.url = url

    def run(self):
        try:
            metadata = get_filtered_data(self.url)
            if "error" in metadata:
                self.error.emit(metadata["error"])
            else:
                self.finished.emit(metadata)
        except SpotifyInvalidUrlException as e:
            self.error.emit(str(e))
        except Exception as e:
            self.error.emit(f'Failed to fetch metadata: {str(e)}')


class TrackSearchWorker(QThread):
    finished = pyqtSignal(list, int)  # updated_tracks, successful_count
    progress = pyqtSignal(str, int)
    error = pyqtSignal(str)

    def __init__(self, tracks, concurrent_searches=8):
        super().__init__()
        self.tracks = tracks
        self.is_stopped = False
        self.concurrent_searches = min(concurrent_searches, 16)  # Limit to avoid rate limiting

    def search_single_track(self, track_info):
        """Search for a single track - thread-safe"""
        track, track_index, total_tracks = track_info

        try:
            if self.is_stopped:
                return track, False, ""

            # Use Tidal search as primary source for finding tracks
            downloader = TidalDownloader()
            query = f"{track.title} {track.artists}"
            search_result = downloader.search_tracks(query)

            if search_result and search_result.get("items"):
                # Find best match
                items = search_result["items"]
                best_match = None
                best_score = 0

                for item in items:
                    # Score based on title and artist similarity
                    item_title = item.get('title', '').lower()
                    item_artists = []
                    if item.get('artists'):
                        item_artists = [a.get('name', '') for a in item['artists']]
                    elif item.get('artist') and item['artist'].get('name'):
                        item_artists = [item['artist']['name']]

                    item_artist_str = ', '.join(item_artists).lower()

                    # Calculate similarity score
                    title_match = track.title.lower() in item_title or item_title in track.title.lower()
                    artist_match = track.artists.lower() in item_artist_str or item_artist_str in track.artists.lower()

                    score = 0
                    if title_match:
                        score += 2
                    if artist_match:
                        score += 2

                    # Bonus for exact matches
                    if track.title.lower() == item_title:
                        score += 1
                    if track.artists.lower() == item_artist_str:
                        score += 1

                    if score > best_score:
                        best_score = score
                        best_match = item

                if best_match and best_score >= 2:  # Require at least title or artist match
                    # Update track with found metadata
                    track.id = str(best_match.get('id', track.id))
                    track.isrc = best_match.get('isrc', '')

                    # Update duration if found
                    if best_match.get('duration'):
                        track.duration_ms = best_match['duration'] * 1000

                    return track, True, f"Found: {track.title} - {track.artists} (Score: {best_score})"
                else:
                    return track, False, f"No good match found for: {track.title} - {track.artists}"
            else:
                return track, False, f"No results found for: {track.title} - {track.artists}"

        except Exception as e:
            return track, False, f"Search error for {track.title}: {str(e)}"

    def run(self):
        try:
            updated_tracks = []
            successful_count = 0
            total_tracks = len(self.tracks)
            completed_searches = 0

            self.progress.emit("Starting concurrent track search on streaming services...", 0)

            # Prepare track data with indices
            track_data = [(track, i, total_tracks) for i, track in enumerate(self.tracks)]

            # Thread-safe tracking
            completed_lock = threading.Lock()

            def search_completed():
                nonlocal completed_searches
                with completed_lock:
                    completed_searches += 1
                    percent = int((completed_searches / total_tracks) * 100) if total_tracks else 0
                    self.progress.emit(f"Search progress: {completed_searches}/{total_tracks} tracks", percent)

            with ThreadPoolExecutor(max_workers=self.concurrent_searches) as executor:
                future_to_track = {
                    executor.submit(self.search_single_track, track_info): track_info[0]
                    for track_info in track_data
                }

                for future in as_completed(future_to_track):
                    if self.is_stopped:
                        break

                    track = future_to_track[future]
                    try:
                        updated_track, success, message = future.result()
                        search_completed()

                        if success:
                            successful_count += 1
                            self.progress.emit(message, 0)
                        else:
                            self.progress.emit(message, 0)

                        updated_tracks.append(updated_track)
                    except Exception as e:
                        search_completed()
                        self.progress.emit(f"Unexpected error: {str(e)}", 0)
                        updated_tracks.append(track)

            if self.is_stopped:
                self.error.emit("Track search stopped by user")
            else:
                self.finished.emit(updated_tracks, successful_count)

        except Exception as e:
            self.error.emit(f"Track search failed: {str(e)}")

    def stop(self):
        self.is_stopped = True


class DownloadWorker(QThread):
    finished = pyqtSignal(bool, str, list)
    progress = pyqtSignal(str, int)
    speed_update = pyqtSignal(dict)  # Live metrics payload for the UI

    def __init__(self, tracks, outpath, is_single_track=False, is_album=False, is_playlist=False,
                 album_or_playlist_name='', filename_format='title_artist', use_track_numbers=True,
                 use_artist_subfolders=False, use_album_subfolders=False, service="tidal", qobuz_region="us",
                 concurrent_downloads=3):
        super().__init__()
        self.tracks = tracks
        self.outpath = outpath
        self.is_single_track = is_single_track
        self.is_album = is_album
        self.is_playlist = is_playlist
        self.album_or_playlist_name = album_or_playlist_name
        self.filename_format = filename_format
        self.use_track_numbers = use_track_numbers
        self.use_artist_subfolders = use_artist_subfolders
        self.use_album_subfolders = use_album_subfolders
        self.primary_service = service
        self.qobuz_region = qobuz_region
        self.concurrent_downloads = concurrent_downloads
        self.is_paused = False
        self.is_stopped = False
        self.failed_tracks = []

        # Synchronisation primitives
        self.pause_event = threading.Event()
        self.pause_event.set()
        self.stop_event = threading.Event()

        # Download statistics
        self.download_start_time = 0
        self.total_downloaded_bytes = 0
        self.current_track_start_time = 0

        self.total_tracks = len(self.tracks)
        self.completed_tracks_overall = 0
        self.completed_success_count = 0
        self.service_retry_limit = 2
        self.stats_lock = threading.Lock()
        self.service_failures = {}
        self.eta_smoothed = None
        self.last_metrics_emit = 0.0
        self.metrics_emit_interval = 0.35
        self.pending_metrics = None
        self.speed_ema = None
        self.speed_ema_alpha = 0.28
        self.track_completion_history = deque(maxlen=24)

        # Service fallback order - try primary service first, then fallbacks
        self.service_fallback_order = self.get_service_fallback_order(service)

    def get_service_fallback_order(self, primary_service):
        """Get the order of services to try, with primary service first"""
        all_services = ["qobuz", "tidal", "deezer", "amazon"]

        fallback_order = [primary_service]
        for service in ["tidal", "qobuz", "deezer", "amazon"]:
            if service != primary_service:
                fallback_order.append(service)

        return fallback_order

    def record_service_success(self, service):
        with self.stats_lock:
            self.service_failures[service] = 0
            if service in self.service_fallback_order:
                self.service_fallback_order.remove(service)
            self.service_fallback_order.insert(0, service)

    def record_service_failure(self, service):
        with self.stats_lock:
            self.service_failures[service] = self.service_failures.get(service, 0) + 1


    def emit_status_metrics(self, payload=None, force=False):
        payload = payload or {}
        now = time.perf_counter()
        with self.stats_lock:
            completed = self.completed_tracks_overall
            successful = self.completed_success_count
            total = self.total_tracks if self.total_tracks else len(self.tracks)
            elapsed = max(now - self.download_start_time, 0) if self.download_start_time else 0
            remaining = max(total - completed, 0)
            avg_per_track = elapsed / completed if completed else 0
            recent_avg_duration = 0.0
            if self.track_completion_history:
                recent_avg_duration = sum(self.track_completion_history) / len(self.track_completion_history)
            downloaded_bytes = self.total_downloaded_bytes

        overall_speed = (downloaded_bytes / elapsed) / (1024 * 1024) if elapsed and downloaded_bytes else 0
        eta_candidates = []

        eta_from_track_average = avg_per_track * remaining if avg_per_track else 0
        if eta_from_track_average:
            eta_candidates.append((eta_from_track_average, 0.3))

        eta_from_recent = recent_avg_duration * remaining if recent_avg_duration else 0
        if eta_from_recent:
            weight = 0.55 if len(self.track_completion_history) >= 3 else 0.35
            eta_candidates.append((eta_from_recent, weight))

        eta_from_speed = 0
        if overall_speed and self.completed_success_count:
            avg_track_bytes = downloaded_bytes / self.completed_success_count if self.completed_success_count else 0
            if avg_track_bytes:
                overall_speed_bytes = overall_speed * 1024 * 1024
                if overall_speed_bytes > 0:
                    eta_from_speed = (avg_track_bytes * remaining) / overall_speed_bytes if remaining else 0
        if eta_from_speed:
            eta_candidates.append((eta_from_speed, 0.2))

        eta_raw = 0
        if eta_candidates:
            total_weight = sum(weight for _, weight in eta_candidates)
            if total_weight:
                eta_raw = sum(value * weight for value, weight in eta_candidates) / total_weight

        smoothed_eta = eta_raw
        if eta_raw:
            if self.eta_smoothed is None:
                self.eta_smoothed = eta_raw
            else:
                alpha = 0.28
                self.eta_smoothed = self.eta_smoothed + alpha * (eta_raw - self.eta_smoothed)
            smoothed_eta = self.eta_smoothed
        else:
            self.eta_smoothed = None
            smoothed_eta = 0

        smoothed_speed = 0
        if overall_speed:
            if self.speed_ema is None:
                self.speed_ema = overall_speed
            else:
                self.speed_ema = self.speed_ema + self.speed_ema_alpha * (overall_speed - self.speed_ema)
            smoothed_speed = self.speed_ema
        else:
            self.speed_ema = None

        base_payload = {
            'timestamp': time.time(),
            'completed': completed,
            'successful': successful,
            'total': total,
            'remaining': remaining,
            'elapsed_seconds': round(elapsed, 2),
            'eta_seconds': round(smoothed_eta, 2) if smoothed_eta else 0,
            'eta_seconds_raw': round(eta_raw, 2) if eta_raw else 0,
            'eta_source_recent': round(eta_from_recent, 2) if eta_from_recent else 0,
            'eta_source_track_average': round(eta_from_track_average, 2) if eta_from_track_average else 0,
            'eta_source_speed': round(eta_from_speed, 2) if eta_from_speed else 0,
            'is_paused': self.is_paused,
            'is_stopped': self.is_stopped,
            'total_downloaded_bytes': downloaded_bytes,
            'total_downloaded_mb': round(downloaded_bytes / (1024 * 1024), 2) if downloaded_bytes else 0,
            'overall_speed_mbps': round(overall_speed, 2) if overall_speed else 0,
            'overall_speed_mbps_smoothed': round(smoothed_speed, 2) if smoothed_speed else 0,
            'recent_track_average_seconds': round(recent_avg_duration, 2) if recent_avg_duration else 0,
        }
        base_payload.update(payload)

        if not force and self.last_metrics_emit:
            elapsed_since_emit = now - self.last_metrics_emit
            if elapsed_since_emit < self.metrics_emit_interval:
                self.pending_metrics = base_payload
                return

        if self.pending_metrics and not force:
            base_payload.update(self.pending_metrics)
            self.pending_metrics = None

        self.last_metrics_emit = now
        self.pending_metrics = None
        self.speed_update.emit(base_payload)

    def download_single_track(self, track_info):
        """Download a single track with service fallback - thread-safe"""
        track, track_index, total_tracks = track_info

        try:
            if self.stop_event.is_set():
                return {"status": "stopped", "track": track, "metrics": {"event": "stopped"}}

            self.pause_event.wait()
            if self.stop_event.is_set():
                return {"status": "stopped", "track": track, "metrics": {"event": "stopped"}}

            track_outpath = self.outpath

            if hasattr(track, 'source_name') and track.source_name and hasattr(track, 'source_type') and track.source_type:
                source_folder = re.sub(r'[<>:"/\|?*]', '_', track.source_name)
                track_outpath = os.path.join(track_outpath, source_folder)
            elif self.is_playlist or self.is_album:
                name = self.album_or_playlist_name.strip()
                folder_name = re.sub(r'[<>:"/\|?*]', '_', name)
                track_outpath = os.path.join(track_outpath, folder_name)

            if self.is_playlist:
                if self.use_artist_subfolders:
                    artist_name = track.artists.split(', ')[0] if ', ' in track.artists else track.artists
                    artist_folder = re.sub(r'[<>:"/\|?*]', lambda m: "'" if m.group() == '"' else '_', artist_name)
                    track_outpath = os.path.join(track_outpath, artist_folder)

                if self.use_album_subfolders:
                    album_folder = re.sub(r'[<>:"/\|?*]', lambda m: "'" if m.group() == '"' else '_', track.album)
                    track_outpath = os.path.join(track_outpath, album_folder)

            os.makedirs(track_outpath, exist_ok=True)

            if (self.is_album or self.is_playlist) and self.use_track_numbers:
                new_filename = f"{track.track_number:02d} - {self.get_formatted_filename(track)}"
            else:
                new_filename = self.get_formatted_filename(track)

            new_filename = re.sub(r'[<>:"/\|?*]', lambda m: "'" if m.group() == '"' else '_', new_filename)
            new_filepath = os.path.join(track_outpath, new_filename)

            if os.path.exists(new_filepath):
                file_size = os.path.getsize(new_filepath)
                if file_size > 1024:
                    try:
                        from mutagen.flac import FLAC
                        audio = FLAC(new_filepath)
                        if audio.info.length > 0:
                            metrics = {
                                "event": "track_skipped",
                                "status": "skipped",
                                "service": "local_cache",
                                "file_path": new_filepath,
                                "filesize_bytes": file_size,
                                "track_title": track.title,
                                "track_artists": track.artists,
                            }
                            return {
                                "status": "skipped",
                                "track": track,
                                "message": f"Valid file already exists: {new_filename} ({file_size / (1024*1024):.2f} MB)",
                                "file_path": new_filepath,
                                "metrics": metrics
                            }
                        else:
                            os.remove(new_filepath)
                    except Exception:
                        os.remove(new_filepath)
                else:
                    os.remove(new_filepath)

            downloaded_file = None
            failed_services = []
            attempted_services = []
            service_used = None
            track_start = time.perf_counter()

            for current_service in list(self.service_fallback_order):
                if self.stop_event.is_set():
                    return {"status": "stopped", "track": track, "metrics": {"event": "stopped"}}

                failure_count = self.service_failures.get(current_service, 0)
                if failure_count >= self.service_retry_limit:
                    attempted_services.append(f"{current_service} (cooldown)")
                    continue

                try:
                    downloaded_file = self._silent_download_with_service(current_service, track, track_outpath)

                    if downloaded_file is None and self.is_stopped:
                        return {"status": "stopped", "track": track, "metrics": {"event": "stopped"}}

                    if downloaded_file and os.path.exists(downloaded_file):
                        service_used = current_service
                        self.record_service_success(current_service)
                        break
                    else:
                        self.record_service_failure(current_service)
                        attempted_services.append(current_service)
                except Exception as e:
                    error_msg = str(e)
                    self.record_service_failure(current_service)
                    attempted_services.append(f"{current_service}: {error_msg}")
                    failed_services.append(f"{current_service}: {error_msg}")
                    continue

            if downloaded_file and os.path.exists(downloaded_file):
                if downloaded_file != new_filepath:
                    try:
                        os.rename(downloaded_file, new_filepath)
                    except OSError:
                        pass

                duration = max(time.perf_counter() - track_start, 0.001)
                file_size = os.path.getsize(new_filepath) if os.path.exists(new_filepath) else 0
                speed_mbps = round((file_size / duration) / (1024 * 1024), 2) if file_size else 0

                metrics = {
                    "event": "track_completed",
                    "status": "success",
                    "service": service_used or self.primary_service,
                    "attempted_services": attempted_services,
                    "file_path": new_filepath,
                    "filesize_bytes": file_size,
                    "duration_seconds": round(duration, 2),
                    "speed_mbps": speed_mbps,
                    "track_title": track.title,
                    "track_artists": track.artists,
                }

                return {
                    "status": "success",
                    "track": track,
                    "file_path": new_filepath,
                    "message": f"Successfully downloaded: {track.title} - {track.artists}",
                    "metrics": metrics
                }
            else:
                failed_services_str = "; ".join(failed_services or attempted_services)
                metrics = {
                    "event": "track_failed",
                    "status": "failed",
                    "service": service_used or self.primary_service,
                    "attempted_services": attempted_services,
                    "track_title": track.title,
                    "track_artists": track.artists,
                }
                return {
                    "status": "failed",
                    "track": track,
                    "error": f"All services failed: {failed_services_str}",
                    "metrics": metrics
                }

        except Exception as e:
            metrics = {
                "event": "track_failed",
                "status": "failed",
                "service": self.primary_service,
                "track_title": track.title,
                "track_artists": track.artists,
                "error": str(e),
            }
            return {
                "status": "failed",
                "track": track,
                "error": str(e),
                "metrics": metrics
            }

    def get_formatted_filename(self, track):
        if self.filename_format == "artist_title":
            filename = f"{track.artists} - {track.title}.flac"
        elif self.filename_format == "title_only":
            filename = f"{track.title}.flac"
        else:
            filename = f"{track.title} - {track.artists}.flac"
        return re.sub(r'[<>:"/\|?*]', lambda m: "'" if m.group() == '"' else '_', filename)

    def _silent_download_with_service(self, service, track, track_outpath):
        """Silent version of download without progress updates for threading"""
        is_paused_callback = lambda: self.is_paused
        is_stopped_callback = lambda: self.is_stopped

        if service == "qobuz":
            if not track.isrc:
                raise Exception("No ISRC available for Qobuz")
            downloader = QobuzDownloader(self.qobuz_region)
            downloader.set_progress_callback(lambda current, total: None)
            return downloader.download(track.isrc, track_outpath, is_paused_callback, is_stopped_callback)

        elif service == "tidal":
            if not track.isrc:
                raise Exception("No ISRC available for Tidal")
            downloader = TidalDownloader()
            downloader.set_progress_callback(lambda current, total: None)

            result = downloader.download(
                query=f"{track.title} {track.artists}",
                isrc=track.isrc,
                output_dir=track_outpath,
                quality="LOSSLESS",
                is_paused_callback=is_paused_callback,
                is_stopped_callback=is_stopped_callback
            )

            if isinstance(result, str) and os.path.exists(result):
                return result
            elif isinstance(result, dict):
                if result.get("success") is False and result.get("error") == "Download stopped by user":
                    return None
                elif result.get("success") is False:
                    raise Exception(result.get("error", "Tidal download failed"))
                elif result.get("status") in ["all_skipped", "skipped_exists"]:
                    return os.path.join(track_outpath, self.get_formatted_filename(track))
            raise Exception("Tidal download failed")

        elif service == "deezer":
            if not track.isrc:
                raise Exception("No ISRC available for Deezer")
            downloader = DeezerDownloader()
            downloader.set_progress_callback(lambda current, total: None)

            success = asyncio.run(downloader.download_by_isrc(track.isrc, track_outpath))
            if success:
                safe_title = "".join(c for c in track.title if c.isalnum() or c in (' ', '-', '_')).rstrip()
                safe_artist = "".join(c for c in track.artists if c.isalnum() or c in (' ', '-', '_')).rstrip()
                expected_filename = f"{safe_artist} - {safe_title}.flac"
                downloaded_file = os.path.join(track_outpath, expected_filename)

                if not os.path.exists(downloaded_file):
                    import glob
                    flac_files = glob.glob(os.path.join(track_outpath, "*.flac"))
                    if flac_files:
                        downloaded_file = max(flac_files, key=os.path.getctime)
                    else:
                        raise Exception("Downloaded file not found")
                return downloaded_file
            else:
                raise Exception("Deezer download failed")

        elif service == "amazon":
            downloader = LucidaDownloader()
            downloader.set_progress_callback(lambda current, total: None)
            result = downloader.download(track.id, track_outpath, is_paused_callback, is_stopped_callback)

            if not result or not os.path.exists(result):
                raise Exception("Amazon Music download failed")
            return result
        else:
            raise Exception(f"Unknown service: {service}")

    def run(self):
        try:
            self.stop_event.clear()
            self.pause_event.set()
            self.is_stopped = False
            self.is_paused = False
            self.pending_metrics = None
            self.last_metrics_emit = 0.0
            self.total_downloaded_bytes = 0
            self.speed_ema = None
            self.eta_smoothed = None
            self.track_completion_history.clear()

            total_tracks = len(self.tracks)
            self.total_tracks = total_tracks
            self.download_start_time = time.perf_counter()
            self.emit_status_metrics({
                "event": "start",
                "status": "starting",
                "message": f"Queueing {total_tracks} tracks",
            }, force=True)

            concurrent_downloads = max(1, min(self.concurrent_downloads, len(self.tracks)))

            self.progress.emit(f"Starting concurrent downloads with {concurrent_downloads} threads...", 0)

            track_data = [(track, i, total_tracks) for i, track in enumerate(self.tracks)]

            completed_lock = threading.Lock()
            completed_tracks = 0

            def track_completed(status, metrics=None):
                nonlocal completed_tracks
                with completed_lock:
                    completed_tracks += 1
                    with self.stats_lock:
                        self.completed_tracks_overall += 1
                        if status == "success":
                            self.completed_success_count += 1
                        if metrics:
                            filesize = metrics.get("filesize_bytes")
                            if filesize and status in ("success", "skipped"):
                                self.total_downloaded_bytes += filesize
                            if status == "success":
                                duration_seconds = metrics.get("duration_seconds")
                                if duration_seconds:
                                    self.track_completion_history.append(duration_seconds)
                    progress_percent = int((completed_tracks / total_tracks) * 100) if total_tracks else 0
                    self.progress.emit(
                        f"Progress: {completed_tracks}/{total_tracks} tracks completed",
                        progress_percent
                    )
                if metrics is None:
                    metrics = {"event": "progress", "status": status}
                self.emit_status_metrics(metrics)

            with ThreadPoolExecutor(max_workers=concurrent_downloads) as executor:
                future_to_track = {
                    executor.submit(self.download_single_track, track_info): track_info[0]
                    for track_info in track_data
                }

                for future in as_completed(future_to_track):
                    if self.stop_event.is_set():
                        for f in future_to_track:
                            if not f.done():
                                f.cancel()
                        self.emit_status_metrics({"event": "stopped", "status": "stopped"}, force=True)
                        return

                    track = future_to_track[future]
                    try:
                        result = future.result()
                        status = result.get("status")
                        metrics = result.get("metrics")

                        if status == "success":
                            self.progress.emit(result["message"], 0)
                            track_completed(status, metrics)
                        elif status == "skipped":
                            self.progress.emit(result["message"], 0)
                            track_completed(status, metrics)
                        elif status == "failed":
                            error_message = result.get("error", "Unknown failure")
                            self.failed_tracks.append((track.title, track.artists, error_message))
                            self.progress.emit(f"Failed: {track.title} - {track.artists} | {error_message}", 0)
                            track_completed(status, metrics)
                        elif status == "stopped":
                            self.emit_status_metrics({"event": "stopped", "status": "stopped"}, force=True)
                            return
                        else:
                            track_completed("unknown", metrics)

                    except Exception as e:
                        self.failed_tracks.append((track.title, track.artists, str(e)))
                        self.progress.emit(f"Thread error for {track.title}: {str(e)}", 0)
                        track_completed("failed", {"event": "track_failed", "status": "failed", "error": str(e)})

            if self.pending_metrics:
                self.emit_status_metrics(self.pending_metrics, force=True)

            if not self.is_stopped:
                success_message = "Multi-threaded download completed!"
                if self.failed_tracks:
                    success_message += f"\n\nFailed downloads: {len(self.failed_tracks)} tracks"
                self.finished.emit(True, success_message, self.failed_tracks)
                self.emit_status_metrics({
                    "event": "finished",
                    "status": "success" if not self.failed_tracks else "partial",
                    "failed": len(self.failed_tracks),
                }, force=True)

        except Exception as e:
            self.finished.emit(False, str(e), self.failed_tracks)
            self.emit_status_metrics({
                "event": "finished",
                "status": "failed",
                "error": str(e),
            }, force=True)

    def pause(self):
        if self.stop_event.is_set():
            return
        self.is_paused = True
        self.pause_event.clear()
        self.progress.emit("Download process paused.", 0)

    def resume(self):
        self.is_paused = False
        self.pause_event.set()
        self.progress.emit("Download process resumed.", 0)

    def stop(self):
        self.is_stopped = True
        self.is_paused = False
        self.stop_event.set()
        self.pause_event.set()

class UpdateDialog(QDialog):
    def __init__(self, current_version, new_version, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Update Now")
        self.setFixedWidth(400)
        self.setModal(True)

        layout = QVBoxLayout()

        message = QLabel(f"SpotiFLAC v{new_version} Available!")
        message.setWordWrap(True)
        layout.addWidget(message)

        button_box = QDialogButtonBox()
        self.update_button = QPushButton("Check")
        self.update_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.cancel_button = QPushButton("Later")
        self.cancel_button.setCursor(Qt.CursorShape.PointingHandCursor)
        
        button_box.addButton(self.update_button, QDialogButtonBox.ButtonRole.AcceptRole)
        button_box.addButton(self.cancel_button, QDialogButtonBox.ButtonRole.RejectRole)
        
        layout.addWidget(button_box)

        self.setLayout(layout)

        self.update_button.clicked.connect(self.accept)
        self.cancel_button.clicked.connect(self.reject)

class TidalStatusChecker(QThread):
    status_updated = pyqtSignal(bool)
    error = pyqtSignal(str)

    def run(self):
        try:
            response = requests.get("https://hifi.401658.xyz", timeout=5)
            is_online = response.status_code == 200 or response.status_code == 429
            self.status_updated.emit(is_online)
        except Exception as e:
            self.error.emit(f"Error checking Tidal (API) status: {str(e)}")
            self.status_updated.emit(False)

class QobuzStatusChecker(QThread):
    status_updated = pyqtSignal(bool)
    error = pyqtSignal(str)
    
    def __init__(self, region="us"):
        super().__init__()
        self.region = region
    
    def run(self):
        try:
            response = requests.get(f"https://{self.region}.qobuz.squid.wtf", timeout=5)
            self.status_updated.emit(response.status_code == 200)
        except Exception as e:
            self.error.emit(f"Error checking Qobuz status: {str(e)}")
            self.status_updated.emit(False)

class DeezerStatusChecker(QThread):
    status_updated = pyqtSignal(bool)
    error = pyqtSignal(str)

    def run(self):
        try:
            response = requests.get("https://deezmate.com/", timeout=5)
            is_online = response.status_code == 200
            self.status_updated.emit(is_online)
        except Exception as e:
            self.error.emit(f"Error checking Deezer status: {str(e)}")
            self.status_updated.emit(False)

class AmazonStatusChecker(QThread):
    status_updated = pyqtSignal(bool)
    error = pyqtSignal(str)

    def run(self):
        try:
            response = requests.get("https://lucida.to/api/load?url=%2Fapi%2Fcountries%3Fservice%3Damazon", timeout=5)
            is_online = response.status_code == 200
            self.status_updated.emit(is_online)
        except Exception as e:
            self.error.emit(f"Error checking Amazon Music status: {str(e)}")
            self.status_updated.emit(False)

class StatusIndicatorDelegate(QStyledItemDelegate):
    def paint(self, painter, option, index):
        item_data = index.data(Qt.ItemDataRole.UserRole)
        is_online = item_data.get('online', False) if item_data else False
        
        super().paint(painter, option, index)
        
        indicator_color = Qt.GlobalColor.green if is_online else Qt.GlobalColor.red
        
        circle_size = 6
        circle_y = option.rect.center().y() - circle_size // 2
        circle_x = option.rect.right() - circle_size - 5
        
        painter.save()
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(indicator_color))
        painter.drawEllipse(circle_x, circle_y, circle_size, circle_size)
        painter.restore()

class ServiceComboBox(QComboBox):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setIconSize(QSize(16, 16))
        self.services_status = {}
        
        self.setItemDelegate(StatusIndicatorDelegate())
        self.setup_items()
        
        self.tidal_status_checker = TidalStatusChecker()
        self.tidal_status_checker.status_updated.connect(self.update_tidal_service_status) 
        self.tidal_status_checker.error.connect(lambda e: print(f"Tidal status check error: {e}")) 
        self.tidal_status_checker.start()

        self.tidal_status_timer = QTimer(self)
        self.tidal_status_timer.timeout.connect(self.refresh_tidal_status) 
        self.tidal_status_timer.start(60000)  
        
        self.deezer_status_checker = DeezerStatusChecker()
        self.deezer_status_checker.status_updated.connect(self.update_deezer_service_status) 
        self.deezer_status_checker.error.connect(lambda e: print(f"Deezer status check error: {e}")) 
        self.deezer_status_checker.start()

        self.deezer_status_timer = QTimer(self)
        self.deezer_status_timer.timeout.connect(self.refresh_deezer_status) 
        self.deezer_status_timer.start(60000)
        
        self.amazon_status_checker = AmazonStatusChecker()
        self.amazon_status_checker.status_updated.connect(self.update_amazon_service_status) 
        self.amazon_status_checker.error.connect(lambda e: print(f"Amazon Music status check error: {e}")) 
        self.amazon_status_checker.start()

        self.amazon_status_timer = QTimer(self)
        self.amazon_status_timer.timeout.connect(self.refresh_amazon_status) 
        self.amazon_status_timer.start(60000)  
        
    def setup_items(self):
        current_dir = os.path.dirname(os.path.abspath(__file__))
        
        self.services = [
            {'id': 'qobuz', 'name': 'Qobuz', 'icon': 'qobuz.png', 'online': False},
            {'id': 'tidal', 'name': 'Tidal', 'icon': 'tidal.png', 'online': False},
            {'id': 'deezer', 'name': 'Deezer', 'icon': 'deezer.png', 'online': False},
            {'id': 'amazon', 'name': 'Amazon Music', 'icon': 'amazon.png', 'online': False}
        ]
        
        for service in self.services:
            icon_path = os.path.join(current_dir, service['icon'])
            if not os.path.exists(icon_path):
                self.create_placeholder_icon(icon_path)
            
            icon = QIcon(icon_path)
            
            self.addItem(icon, service['name'])
            item_index = self.count() - 1
            self.setItemData(item_index, service['id'], Qt.ItemDataRole.UserRole + 1)
            self.setItemData(item_index, service, Qt.ItemDataRole.UserRole)
    def create_placeholder_icon(self, path):
        pixmap = QPixmap(16, 16)
        pixmap.fill(Qt.GlobalColor.transparent)
        pixmap.save(path)

    def update_service_status(self, service_id, is_online):
        for i in range(self.count()):
            current_service_id = self.itemData(i, Qt.ItemDataRole.UserRole + 1)
            if current_service_id == service_id:
                service_data = self.itemData(i, Qt.ItemDataRole.UserRole)
                if isinstance(service_data, dict):
                    service_data['online'] = is_online
                    self.setItemData(i, service_data, Qt.ItemDataRole.UserRole)
                break 
        self.update()
        
    def update_tidal_service_status(self, is_online): 
        self.update_service_status('tidal', is_online)
        
    def refresh_tidal_status(self):
        if hasattr(self, 'tidal_status_checker') and self.tidal_status_checker.isRunning():
            self.tidal_status_checker.quit()
            self.tidal_status_checker.wait()
            
        self.tidal_status_checker = TidalStatusChecker() 
        self.tidal_status_checker.status_updated.connect(self.update_tidal_service_status)
        self.tidal_status_checker.error.connect(lambda e: print(f"Tidal status check error: {e}")) 
        self.tidal_status_checker.start()
        
    def update_deezer_service_status(self, is_online): 
        self.update_service_status('deezer', is_online)
        
    def refresh_deezer_status(self):
        if hasattr(self, 'deezer_status_checker') and self.deezer_status_checker.isRunning():
            self.deezer_status_checker.quit()
            self.deezer_status_checker.wait()
            
        self.deezer_status_checker = DeezerStatusChecker() 
        self.deezer_status_checker.status_updated.connect(self.update_deezer_service_status)
        self.deezer_status_checker.error.connect(lambda e: print(f"Deezer status check error: {e}")) 
        self.deezer_status_checker.start()
        
    def update_amazon_service_status(self, is_online): 
        self.update_service_status('amazon', is_online)
        
    def refresh_amazon_status(self):
        if hasattr(self, 'amazon_status_checker') and self.amazon_status_checker.isRunning():
            self.amazon_status_checker.quit()
            self.amazon_status_checker.wait()
            
        self.amazon_status_checker = AmazonStatusChecker() 
        self.amazon_status_checker.status_updated.connect(self.update_amazon_service_status)
        self.amazon_status_checker.error.connect(lambda e: print(f"Amazon Music status check error: {e}")) 
        self.amazon_status_checker.start()
        
    def currentData(self, role=Qt.ItemDataRole.UserRole + 1):
        return super().currentData(role)

    def update_qobuz_status(self, region_id, is_online):
        for i in range(self.count()):
            service_id = self.itemData(i, Qt.ItemDataRole.UserRole + 1)
            
            if service_id == 'qobuz':
                service_data = self.itemData(i, Qt.ItemDataRole.UserRole)
                if isinstance(service_data, dict):
                    if is_online or service_data.get('online', False):
                        service_data['online'] = True
                        self.setItemData(i, service_data, Qt.ItemDataRole.UserRole)
                break
        
        self.update()

class QobuzRegionComboBox(QComboBox):
    status_updated = pyqtSignal(str, bool)
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setIconSize(QSize(16, 16))
        
        self.setItemDelegate(StatusIndicatorDelegate())
        
        self.setup_items()
        
        self.status_checkers = {}
        self.check_status()
        
        self.status_timer = QTimer(self)
        self.status_timer.timeout.connect(self.check_status)
        self.status_timer.start(60000)  
        
    def setup_items(self):
        current_dir = os.path.dirname(os.path.abspath(__file__))
        
        self.regions = [
            {'id': 'eu', 'name': 'Europe', 'icon': 'eu.svg', 'online': False},
            {'id': 'us', 'name': 'North America', 'icon': 'us.svg', 'online': False}
        ]
        
        for region in self.regions:
            icon_path = os.path.join(current_dir, region['icon'])
            if not os.path.exists(icon_path):
                self.create_placeholder_icon(icon_path)
            
            icon = QIcon(icon_path)
            
            self.addItem(icon, region['name'])
            item_index = self.count() - 1
            self.setItemData(item_index, region['id'], Qt.ItemDataRole.UserRole + 1)
            self.setItemData(item_index, region, Qt.ItemDataRole.UserRole)
    
    def create_placeholder_icon(self, path):
        pixmap = QPixmap(16, 16)
        pixmap.fill(Qt.GlobalColor.transparent)
        pixmap.save(path)
    
    def update_region_status(self, region_id, is_online):
        for i in range(self.count()):
            current_region_id = self.itemData(i, Qt.ItemDataRole.UserRole + 1)
            
            if current_region_id == region_id:
                region_data = self.itemData(i, Qt.ItemDataRole.UserRole)
                if isinstance(region_data, dict):
                    region_data['online'] = is_online
                    self.setItemData(i, region_data, Qt.ItemDataRole.UserRole)
                break
        
        self.update()
    
    def check_status(self):
        for region_id, checker in self.status_checkers.items():
            if checker.isRunning():
                checker.quit()
                checker.wait()
        self.status_checkers.clear()
        
        for region in self.regions:
            region_id = region['id']
            checker = QobuzStatusChecker(region_id)
            checker.status_updated.connect(lambda status, rid=region_id: self.handle_status_update(rid, status))
            checker.start()
            self.status_checkers[region_id] = checker
    
    def handle_status_update(self, region_id, is_online):
        self.update_region_status(region_id, is_online)
        self.status_updated.emit(region_id, is_online)
        
    def currentData(self, role=Qt.ItemDataRole.UserRole + 1):
        return super().currentData(role)
        
class SpotiFLACGUI(QWidget):
    def __init__(self):
        super().__init__()
        self.setObjectName('rootWindow')
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.current_version = "4.4"
        self.tracks = []
        self.all_tracks = []  
        self.reset_state()
        
        self.settings = QSettings('SpotiFLAC', 'Settings')
        self.last_output_path = self.settings.value('output_path', str(Path.home() / "Music"))
        self.last_url = self.settings.value('spotify_url', '')
        
        self.filename_format = self.settings.value('filename_format', 'title_artist')
        self.use_track_numbers = self.settings.value('use_track_numbers', False, type=bool)
        self.use_artist_subfolders = self.settings.value('use_artist_subfolders', False, type=bool)
        self.use_album_subfolders = self.settings.value('use_album_subfolders', False, type=bool)
        self.service = self.settings.value('service', 'tidal')
        self.qobuz_region = self.settings.value('qobuz_region', 'us')
        self.check_for_updates = self.settings.value('check_for_updates', True, type=bool)
        self.current_theme_color = self.settings.value('theme_color', '#2196F3')
        self.concurrent_downloads = self.settings.value('concurrent_downloads', 3, type=int)  # Default 3 simultaneous downloads
        
        self.elapsed_time = QTime(0, 0, 0)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.update_timer)
        
        self.network_manager = QNetworkAccessManager()
        self.network_manager.finished.connect(self.on_cover_loaded)
        
        self.initUI()
        
        if self.check_for_updates:
            QTimer.singleShot(0, self.check_updates)

    def set_combobox_value(self, combobox, target_value):
        for i in range(combobox.count()):
            if combobox.itemData(i, Qt.ItemDataRole.UserRole + 1) == target_value:
                combobox.setCurrentIndex(i)
                return True
        return False

    def check_updates(self):
        try:
            response = requests.get("https://raw.githubusercontent.com/jakeypiez/SpotiFLAC/refs/heads/main/version.json")
            if response.status_code == 200:
                data = response.json()
                new_version = data.get("version")
                
                if new_version and version.parse(new_version) > version.parse(self.current_version):
                    dialog = UpdateDialog(self.current_version, new_version, self)
                    result = dialog.exec()
                    
                    if result == QDialog.DialogCode.Accepted:
                        QDesktopServices.openUrl(QUrl("https://github.com/jakeypiez/SpotiFLAC/releases"))
                        
        except Exception as e:
            pass

    @staticmethod
    def format_duration(ms):
        minutes = ms // 60000
        seconds = (ms % 60000) // 1000
        return f"{minutes}:{seconds:02d}"
    
    def reset_state(self):
        self.tracks.clear()
        self.all_tracks.clear()
        self.is_album = False
        self.is_playlist = False 
        self.is_single_track = False
        self.album_or_playlist_name = ''

    def reset_ui(self):
        self.track_list.clear()
        self.log_output.clear()
        self.progress_bar.setValue(0)
        self.progress_bar.hide()
        self.stop_btn.hide()
        self.pause_resume_btn.hide()
        self.pause_resume_btn.setText('Pause')
        self.reset_info_widget()
        self.hide_track_buttons()
        if hasattr(self, 'search_input'):
            self.search_input.clear()
        if hasattr(self, 'search_widget'):
            self.search_widget.hide()
        if hasattr(self, 'playlist_files_list'):
            self.playlist_files_list.clear()
        if hasattr(self, 'selected_playlist_files'):
            self.selected_playlist_files.clear()
        if hasattr(self, 'tracks'):
            self.update_track_list_display()

    def initUI(self):
        self.setWindowTitle('SpotiFLAC')
        self.setMinimumSize(860, 540)
        self.resize(1024, 680)
        
        icon_path = os.path.join(os.path.dirname(__file__), "icon.svg")
        if os.path.exists(icon_path):
            self.setWindowIcon(QIcon(icon_path))
            
        self.main_layout = QVBoxLayout()
        self.main_layout.setSpacing(18)
        self.main_layout.setContentsMargins(24, 24, 24, 24)

        self.setup_spotify_section()
        self.setup_tabs()
        
        self.setLayout(self.main_layout)
        self.apply_theme_stylesheet()

    def apply_card_shadow(self, widget, blur=32, y_offset=16, alpha=120):
        if not widget:
            return
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(blur)
        shadow.setOffset(0, y_offset)
        shadow.setColor(QColor(0, 0, 0, alpha))
        widget.setGraphicsEffect(shadow)

    def setup_spotify_section(self):
        card = QFrame()
        card.setObjectName('inputCard')
        card_layout = QVBoxLayout(card)
        card_layout.setSpacing(12)
        card_layout.setContentsMargins(18, 18, 18, 18)
        self.apply_card_shadow(card, blur=40, y_offset=18, alpha=120)

        header_label = QLabel('Sources')
        header_label.setObjectName('sectionTitle')
        card_layout.addWidget(header_label)

        spotify_layout = QVBoxLayout()
        spotify_layout.setSpacing(6)
        spotify_header = QHBoxLayout()
        spotify_label = QLabel('Spotify URLs:')

        self.fetch_btn = QPushButton('Fetch All')
        self.fetch_btn.setProperty('accent', True)
        self.fetch_btn.setMinimumWidth(96)
        self.fetch_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.fetch_btn.clicked.connect(self.fetch_tracks)

        spotify_header.addWidget(spotify_label)
        spotify_header.addStretch()
        spotify_header.addWidget(self.fetch_btn)
        spotify_layout.addLayout(spotify_header)

        self.spotify_urls = QTextEdit()
        self.spotify_urls.setObjectName('inputTextArea')
        self.spotify_urls.setPlaceholderText("Enter Spotify URLs (one per line)\\nTracks, albums, playlists supported")
        self.spotify_urls.setMinimumHeight(90)
        self.spotify_urls.setText(self.last_url)
        self.spotify_urls.textChanged.connect(self.save_urls)

        spotify_layout.addWidget(self.spotify_urls)
        card_layout.addLayout(spotify_layout)

        playlist_layout = QVBoxLayout()
        playlist_layout.setSpacing(6)
        playlist_header = QHBoxLayout()
        playlist_label = QLabel('Playlist Files:')

        self.browse_playlist_btn = QPushButton('Browse')
        self.browse_playlist_btn.setMinimumWidth(96)
        self.browse_playlist_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.browse_playlist_btn.clicked.connect(self.browse_playlist_files)

        self.clear_files_btn = QPushButton('Clear')
        self.clear_files_btn.setMinimumWidth(96)
        self.clear_files_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.clear_files_btn.clicked.connect(self.clear_playlist_files)

        playlist_header.addWidget(playlist_label)
        playlist_header.addStretch()
        playlist_header.addWidget(self.browse_playlist_btn)
        playlist_header.addWidget(self.clear_files_btn)
        playlist_layout.addLayout(playlist_header)

        self.playlist_files_list = QListWidget()
        self.playlist_files_list.setObjectName('inputList')
        self.playlist_files_list.setMinimumHeight(80)
        self.playlist_files_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        playlist_layout.addWidget(self.playlist_files_list)

        card_layout.addLayout(playlist_layout)

        self.main_layout.addWidget(card)

        self.selected_playlist_files = []

    def filter_tracks(self):
        search_text = self.search_input.text().lower().strip()
        
        if not search_text:
            self.tracks = self.all_tracks.copy()
        else:
            self.tracks = [
                track for track in self.all_tracks
                if (search_text in track.title.lower() or 
                    search_text in track.artists.lower() or 
                    search_text in track.album.lower())
            ]
        
        self.update_track_list_display()

    def update_track_list_display(self):
        self.track_list.clear()
        for i, track in enumerate(self.tracks, 1):
            duration = self.format_duration(track.duration_ms)
            display_text = f"{i}. {track.title} - {track.artists}  ({duration})"
            self.track_list.addItem(display_text)

        has_tracks = bool(self.tracks)
        self.track_list.setVisible(has_tracks)
        if hasattr(self, 'empty_state_label'):
            self.empty_state_label.setVisible(not has_tracks)

    def browse_output(self):
        directory = QFileDialog.getExistingDirectory(self, "Select Output Directory")
        if directory:
            self.output_dir.setText(directory)
            self.save_settings()

    def setup_tabs(self):
        self.tab_widget = QTabWidget()
        self.main_layout.addWidget(self.tab_widget)

        self.setup_dashboard_tab()
        self.setup_process_tab()
        self.setup_settings_tab()
        self.setup_theme_tab()
        self.setup_about_tab()

    def setup_dashboard_tab(self):
        dashboard_tab = QWidget()
        dashboard_layout = QVBoxLayout()

        self.setup_info_widget()
        dashboard_layout.addWidget(self.info_widget)

        self.track_list = QListWidget()
        self.track_list.setObjectName("trackList")
        self.track_list.setAlternatingRowColors(True)
        self.track_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.track_list.setUniformItemSizes(True)
        dashboard_layout.addWidget(self.track_list)

        self.empty_state_label = QLabel("Paste Spotify URLs above and fetch to begin.")
        self.empty_state_label.setObjectName("emptyState")
        self.empty_state_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        dashboard_layout.addWidget(self.empty_state_label)
        self.empty_state_label.hide()

        self.setup_track_buttons()
        dashboard_layout.addLayout(self.btn_layout)
        dashboard_layout.addWidget(self.single_track_container)

        dashboard_tab.setLayout(dashboard_layout)
        self.tab_widget.addTab(dashboard_tab, "Dashboard")

        self.hide_track_buttons()

    def setup_info_widget(self):
        self.info_widget = QWidget()
        info_layout = QHBoxLayout()
        self.cover_label = QLabel()
        self.cover_label.setFixedSize(80, 80)
        self.cover_label.setScaledContents(True)
        info_layout.addWidget(self.cover_label)

        text_info_layout = QVBoxLayout()
        
        self.title_label = QLabel()
        self.title_label.setStyleSheet("font-size: 14px; font-weight: bold;")
        self.title_label.setWordWrap(True)
        
        self.artists_label = QLabel()
        self.artists_label.setWordWrap(True)

        self.followers_label = QLabel()
        self.followers_label.setWordWrap(True)
        
        self.release_date_label = QLabel()
        self.release_date_label.setWordWrap(True)
        
        self.type_label = QLabel()
        self.type_label.setStyleSheet("font-size: 12px;")
        
        text_info_layout.addWidget(self.title_label)
        text_info_layout.addWidget(self.artists_label)
        text_info_layout.addWidget(self.followers_label)
        text_info_layout.addWidget(self.release_date_label)
        text_info_layout.addWidget(self.type_label)
        text_info_layout.addStretch()

        info_layout.addLayout(text_info_layout, 1)
        
        self.setup_search_widget()
        info_layout.addWidget(self.search_widget)
        
        self.info_widget.setLayout(info_layout)
        self.info_widget.setFixedHeight(100)
        self.info_widget.hide()

    def setup_search_widget(self):
        self.search_widget = QWidget()
        search_layout = QVBoxLayout()
        search_layout.setContentsMargins(10, 0, 0, 0)
        
        search_layout.addStretch()
        
        search_input_layout = QHBoxLayout()
        search_input_layout.addStretch()  
        
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Search...")
        self.search_input.setClearButtonEnabled(True)
        self.search_input.textChanged.connect(self.filter_tracks)
        self.search_input.setFixedWidth(250)  
        
        search_input_layout.addWidget(self.search_input)
        search_layout.addLayout(search_input_layout)
        
        self.search_widget.setLayout(search_layout)
        self.search_widget.hide()

    def setup_track_buttons(self):
        self.btn_layout = QHBoxLayout()
        self.download_selected_btn = QPushButton('Download Selected')
        self.download_all_btn = QPushButton('Download All')
        self.remove_btn = QPushButton('Remove Selected')
        self.clear_btn = QPushButton('Clear')
        
        for btn in [self.download_selected_btn, self.download_all_btn, self.remove_btn, self.clear_btn]:
            btn.setMinimumWidth(120)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            
        self.download_selected_btn.setProperty('accent', True)
        self.download_all_btn.setProperty('accent', True)

        self.download_selected_btn.clicked.connect(self.download_selected)
        self.download_all_btn.clicked.connect(self.download_all)
        self.remove_btn.clicked.connect(self.remove_selected_tracks)
        self.clear_btn.clicked.connect(self.clear_tracks)
        
        self.btn_layout.addStretch()
        for btn in [self.download_selected_btn, self.download_all_btn, self.remove_btn, self.clear_btn]:
            self.btn_layout.addWidget(btn, 1)
        self.btn_layout.addStretch()
        
        self.single_track_container = QWidget()
        single_track_layout = QHBoxLayout(self.single_track_container)
        single_track_layout.setContentsMargins(0, 0, 0, 0)
        
        self.single_download_btn = QPushButton('Download')
        self.single_clear_btn = QPushButton('Clear')
        
        for btn in [self.single_download_btn, self.single_clear_btn]:
            btn.setFixedWidth(120)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            
        self.single_download_btn.setProperty('accent', True)
        self.single_download_btn.clicked.connect(self.download_all)
        self.single_clear_btn.clicked.connect(self.clear_tracks)
        
        single_track_layout.addStretch()
        single_track_layout.addWidget(self.single_download_btn)
        single_track_layout.addWidget(self.single_clear_btn)
        single_track_layout.addStretch()
        
        self.single_track_container.hide()

    def setup_process_tab(self):
        self.process_tab = QWidget()
        process_layout = QVBoxLayout()
        process_layout.setSpacing(10)

        self.log_output = QTextEdit()
        self.log_output.setObjectName('terminalLog')
        self.log_output.setReadOnly(True)
        self.log_output.setPlaceholderText('Live download log will appear here.')
        self.log_output.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        self.log_output.document().setMaximumBlockCount(3000)
        process_layout.addWidget(self.log_output)

        log_controls = QHBoxLayout()
        log_controls.addStretch()
        self.copy_log_btn = QPushButton('Copy Log')
        self.copy_log_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.copy_log_btn.clicked.connect(self.copy_log_to_clipboard)
        log_controls.addWidget(self.copy_log_btn)

        self.clear_log_btn = QPushButton('Clear Log')
        self.clear_log_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.clear_log_btn.clicked.connect(self.clear_log_output)
        log_controls.addWidget(self.clear_log_btn)
        process_layout.addLayout(log_controls)

        self.setup_metrics_panel(process_layout)

        progress_time_layout = QVBoxLayout()
        progress_time_layout.setSpacing(2)

        self.progress_bar = QProgressBar()
        progress_time_layout.addWidget(self.progress_bar)

        self.time_label = QLabel('00:00:00')
        self.time_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        progress_time_layout.addWidget(self.time_label)

        process_layout.addLayout(progress_time_layout)

        control_layout = QHBoxLayout()
        self.stop_btn = QPushButton('Stop')
        self.stop_btn.setProperty('accent', True)
        self.pause_resume_btn = QPushButton('Pause')

        self.stop_btn.setFixedWidth(120)
        self.pause_resume_btn.setFixedWidth(120)

        self.stop_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.pause_resume_btn.setCursor(Qt.CursorShape.PointingHandCursor)

        self.stop_btn.clicked.connect(self.stop_download)
        self.pause_resume_btn.clicked.connect(self.toggle_pause_resume)

        control_layout.addStretch()
        control_layout.addWidget(self.stop_btn)
        control_layout.addWidget(self.pause_resume_btn)
        control_layout.addStretch()

        process_layout.addLayout(control_layout)

        self.process_tab.setLayout(process_layout)

        self.tab_widget.addTab(self.process_tab, 'Process')

        self.progress_bar.hide()
        self.time_label.hide()
        self.stop_btn.hide()
        self.pause_resume_btn.hide()

    def setup_metrics_panel(self, parent_layout):
        self.metrics_panel = QFrame()
        self.metrics_panel.setObjectName('metricsPanel')
        metrics_layout = QGridLayout(self.metrics_panel)
        metrics_layout.setContentsMargins(20, 14, 20, 14)
        metrics_layout.setHorizontalSpacing(28)
        metrics_layout.setVerticalSpacing(12)

        self.metrics_labels = {}
        metric_grid = [
            (0, 0, 'Status', 'status', 'Idle'),
            (0, 1, 'Service', 'service', 'N/A'),
            (0, 2, 'Average Speed', 'avg_speed', '0 MB/s'),
            (1, 0, 'Progress', 'progress', '0 / 0'),
            (1, 1, 'ETA', 'eta', 'N/A'),
            (1, 2, 'Last Track', 'last', '--'),
        ]

        for row, col, title, key, default in metric_grid:
            container = QVBoxLayout()
            container.setSpacing(2)
            header = QLabel(title.upper())
            header.setObjectName('metricHeader')
            value = QLabel(default)
            value.setObjectName('metricValue')
            value.setWordWrap(True)
            container.addWidget(header)
            container.addWidget(value)
            container.addStretch()
            metrics_layout.addLayout(container, row, col)
            self.metrics_labels[key] = value

        parent_layout.addWidget(self.metrics_panel)
        self.apply_card_shadow(self.metrics_panel, blur=38, y_offset=14, alpha=110)

    def clear_log_output(self):
        self.log_output.clear()

    def copy_log_to_clipboard(self):
        clipboard = QApplication.clipboard()
        clipboard.setText(self.log_output.toPlainText())

    def reset_metrics_panel(self, status_text='Idle'):
        if not hasattr(self, 'metrics_labels'):
            return
        self.metrics_labels['status'].setText(status_text)
        self.metrics_labels['service'].setText(self.service_dropdown.currentText())
        if 'avg_speed' in self.metrics_labels:
            self.metrics_labels['avg_speed'].setText('0 MB/s')
        total = len(self.tracks) if self.tracks else 0
        self.metrics_labels['progress'].setText(f"0 / {total}")
        if 'eta' in self.metrics_labels:
            self.metrics_labels['eta'].setText('N/A')
        if 'last' in self.metrics_labels:
            self.metrics_labels['last'].setText('--')

    def on_download_metrics(self, payload):
        if not hasattr(self, 'metrics_labels') or not payload:
            return

        event = payload.get('event')
        status_value = payload.get('status', 'idle')
        track_title = payload.get('track_title')

        if payload.get('is_paused'):
            status_text = 'Paused'
        elif payload.get('is_stopped'):
            status_text = 'Stopped'
        elif event == 'track_completed' and track_title:
            status_text = f"Downloaded: {track_title}"
        elif event == 'track_failed' and track_title:
            status_text = f"Failed: {track_title}"
        elif event == 'track_skipped' and track_title:
            status_text = f"Skipped: {track_title}"
        elif event == 'finished':
            if status_value in ('success', 'partial'):
                status_text = 'Completed' if status_value == 'success' else 'Completed (partial)'
            elif status_value == 'failed':
                status_text = 'Failed'
            else:
                status_text = status_value.replace('_', ' ').title() if status_value else 'Finished'
        elif status_value:
            status_text = status_value.replace('_', ' ').title()
        else:
            status_text = 'Idle'

        self.metrics_labels['status'].setText(self.ellipsize(status_text, 64))

        service_name = payload.get('service') or self.service_dropdown.currentText()
        if isinstance(service_name, str):
            self.metrics_labels['service'].setText(service_name.upper())

        avg_speed_label = self.metrics_labels.get('avg_speed')
        if avg_speed_label:
            avg_speed_value = payload.get('overall_speed_mbps_smoothed') or payload.get('overall_speed_mbps') or 0
            if avg_speed_value:
                avg_speed_text = f"{avg_speed_value:.2f} MB/s"
            else:
                avg_speed_text = '0 MB/s'
            total_downloaded_mb = payload.get('total_downloaded_mb')
            if total_downloaded_mb:
                avg_speed_text += f" | {total_downloaded_mb:.1f} MB"
            avg_speed_label.setText(avg_speed_text)

        completed = payload.get('completed', 0)
        total = payload.get('total', len(self.tracks)) or 0
        progress_text = f"{completed} / {total}"
        if total:
            percent = (completed / total) * 100
            progress_text += f" ({percent:.0f}%)"
        self.metrics_labels['progress'].setText(progress_text)

        eta_label = self.metrics_labels.get('eta')
        if eta_label:
            eta_seconds = payload.get('eta_seconds')
            if event == 'finished' or payload.get('remaining', 0) == 0:
                eta_label.setText('Done')
            else:
                eta_label.setText(self.format_eta(eta_seconds) if eta_seconds else 'N/A')

        last_label = self.metrics_labels.get('last')
        if last_label and event in ('track_completed', 'track_failed', 'track_skipped', 'finished'):
            last_text = None
            title_short = self.ellipsize(track_title, 42) if track_title else ''
            if event == 'track_completed':
                parts = []
                if title_short:
                    parts.append(title_short)
                filesize = payload.get('filesize_bytes')
                if filesize:
                    size_mb = filesize / (1024 * 1024)
                    parts.append(f"{size_mb:.1f} MB")
                speed_value = payload.get('speed_mbps')
                if speed_value:
                    parts.append(f"{speed_value:.2f} MB/s")
                if parts:
                    last_text = ' | '.join(parts)
            elif event == 'track_skipped' and title_short:
                last_text = f"Skipped: {title_short}"
            elif event == 'track_failed' and title_short:
                error_message = payload.get('error') or 'Unresolved error'
                last_text = f"Failed: {title_short} ({self.ellipsize(error_message, 32)})"
            elif event == 'finished' and status_value in ('success', 'partial'):
                total_mb = payload.get('total_downloaded_mb')
                if total_mb:
                    last_text = f"Total: {total_mb:.1f} MB"
            if last_text:
                last_label.setText(last_text)

    @staticmethod
    def ellipsize(text, limit=48):
        if not text:
            return ''
        text = str(text)
        if len(text) <= limit:
            return text
        cutoff = max(0, limit - 3)
        return text[:cutoff] + '...'

    def format_eta(self, seconds):
        if not seconds:
            return 'N/A'
        seconds = int(seconds)
        if seconds < 60:
            return f"{seconds}s"
        minutes, sec = divmod(seconds, 60)
        if minutes < 60:
            return f"{minutes}m {sec}s"
        hours, minutes = divmod(minutes, 60)
        return f"{hours}h {minutes}m"

    def setup_settings_tab(self):
        settings_tab = QWidget()
        settings_layout = QVBoxLayout()
        settings_layout.setSpacing(10)
        settings_layout.setContentsMargins(9, 9, 9, 9)

        output_group = QWidget()
        output_layout = QVBoxLayout(output_group)
        output_layout.setSpacing(5)
        
        output_label = QLabel('Output Directory')
        output_label.setStyleSheet("font-weight: bold;")
        output_layout.addWidget(output_label)
        
        output_dir_layout = QHBoxLayout()
        self.output_dir = QLineEdit()
        self.output_dir.setText(self.last_output_path)
        self.output_dir.textChanged.connect(self.save_settings)
        
        self.output_browse = QPushButton('Browse')
        self.output_browse.setFixedWidth(80)
        self.output_browse.setCursor(Qt.CursorShape.PointingHandCursor)
        self.output_browse.clicked.connect(self.browse_output)
        
        output_dir_layout.addWidget(self.output_dir)
        output_dir_layout.addWidget(self.output_browse)
        
        output_layout.addLayout(output_dir_layout)
        
        settings_layout.addWidget(output_group)

        file_group = QWidget()
        file_layout = QVBoxLayout(file_group)
        file_layout.setSpacing(5)
        
        file_label = QLabel('File Settings')
        file_label.setStyleSheet("font-weight: bold;")
        file_layout.addWidget(file_label)
        
        format_layout = QHBoxLayout()
        format_label = QLabel('Filename Format:')
        self.format_group = QButtonGroup(self)
        self.title_artist_radio = QRadioButton('Title - Artist')
        self.title_artist_radio.setCursor(Qt.CursorShape.PointingHandCursor)
        self.title_artist_radio.toggled.connect(self.save_filename_format)
        
        self.artist_title_radio = QRadioButton('Artist - Title')
        self.artist_title_radio.setCursor(Qt.CursorShape.PointingHandCursor)
        self.artist_title_radio.toggled.connect(self.save_filename_format)
        
        self.title_only_radio = QRadioButton('Title')
        self.title_only_radio.setCursor(Qt.CursorShape.PointingHandCursor)
        self.title_only_radio.toggled.connect(self.save_filename_format)
        
        if hasattr(self, 'filename_format') and self.filename_format == "artist_title":
            self.artist_title_radio.setChecked(True)
        elif hasattr(self, 'filename_format') and self.filename_format == "title_only":
            self.title_only_radio.setChecked(True)
        else:
            self.title_artist_radio.setChecked(True)
        
        self.format_group.addButton(self.title_artist_radio)
        self.format_group.addButton(self.artist_title_radio)
        self.format_group.addButton(self.title_only_radio)
        
        format_layout.addWidget(format_label)
        format_layout.addWidget(self.title_artist_radio)
        format_layout.addWidget(self.artist_title_radio)
        format_layout.addWidget(self.title_only_radio)
        format_layout.addStretch()
        file_layout.addLayout(format_layout)

        checkbox_layout = QHBoxLayout()
        
        self.artist_subfolder_checkbox = QCheckBox('Artist Subfolder (Playlist)')
        self.artist_subfolder_checkbox.setCursor(Qt.CursorShape.PointingHandCursor)
        self.artist_subfolder_checkbox.setChecked(self.use_artist_subfolders)
        self.artist_subfolder_checkbox.toggled.connect(self.save_artist_subfolder_setting)
        checkbox_layout.addWidget(self.artist_subfolder_checkbox)
        
        self.album_subfolder_checkbox = QCheckBox('Album Subfolder (Playlist)')
        self.album_subfolder_checkbox.setCursor(Qt.CursorShape.PointingHandCursor)
        self.album_subfolder_checkbox.setChecked(self.use_album_subfolders)
        self.album_subfolder_checkbox.toggled.connect(self.save_album_subfolder_setting)
        checkbox_layout.addWidget(self.album_subfolder_checkbox)
        
        self.track_number_checkbox = QCheckBox('Track Number for Album')
        self.track_number_checkbox.setCursor(Qt.CursorShape.PointingHandCursor)
        self.track_number_checkbox.setChecked(self.use_track_numbers)
        self.track_number_checkbox.toggled.connect(self.save_track_numbering)
        checkbox_layout.addWidget(self.track_number_checkbox)
        
        checkbox_layout.addStretch()
        file_layout.addLayout(checkbox_layout)
        
        settings_layout.addWidget(file_group)

        auth_group = QWidget()
        auth_layout = QVBoxLayout(auth_group)
        auth_layout.setSpacing(5)
        
        auth_label = QLabel('Service Settings')
        auth_label.setStyleSheet("font-weight: bold;")
        auth_layout.addWidget(auth_label)

        service_fallback_layout = QHBoxLayout()

        service_label = QLabel('Service:')
        
        self.service_dropdown = ServiceComboBox()
        self.service_dropdown.currentIndexChanged.connect(self.on_service_changed)
        service_fallback_layout.addWidget(service_label)
        service_fallback_layout.addWidget(self.service_dropdown)
        
        service_fallback_layout.addSpacing(10)

        region_label = QLabel('Region:')
        self.qobuz_region_dropdown = QobuzRegionComboBox()
        self.qobuz_region_dropdown.currentIndexChanged.connect(self.save_qobuz_region_setting)
        service_fallback_layout.addWidget(region_label)
        service_fallback_layout.addWidget(self.qobuz_region_dropdown)
        
        region_label.hide()
        self.qobuz_region_dropdown.hide()
        
        service_fallback_layout.addStretch()
        auth_layout.addLayout(service_fallback_layout)
        
        # Add concurrent downloads setting
        concurrent_layout = QHBoxLayout()
        concurrent_label = QLabel('Concurrent Downloads:')
        
        from PyQt6.QtWidgets import QSpinBox
        self.concurrent_spinbox = QSpinBox()
        self.concurrent_spinbox.setRange(1, 64)  # 1-64 simultaneous downloads
        self.concurrent_spinbox.setValue(self.concurrent_downloads)
        self.concurrent_spinbox.setSuffix(' files')
        self.concurrent_spinbox.setToolTip('Number of files to download simultaneously (1-64)')
        self.concurrent_spinbox.valueChanged.connect(self.save_concurrent_setting)
        
        concurrent_layout.addWidget(concurrent_label)
        concurrent_layout.addWidget(self.concurrent_spinbox)
        concurrent_layout.addStretch()
        auth_layout.addLayout(concurrent_layout)
        
        settings_layout.addWidget(auth_group)
        settings_layout.addStretch()
        settings_tab.setLayout(settings_layout)
        self.tab_widget.addTab(settings_tab, "Settings")
        self.set_combobox_value(self.service_dropdown, self.service)
        self.set_combobox_value(self.qobuz_region_dropdown, self.qobuz_region)        
        
        self.update_service_ui()
        
        self.qobuz_region_dropdown.status_updated.connect(
            lambda region_id, is_online: self.service_dropdown.update_qobuz_status(region_id, is_online)
        )
        
    def setup_theme_tab(self):
        theme_tab = QWidget()
        theme_layout = QVBoxLayout()
        theme_layout.setSpacing(8)
        theme_layout.setContentsMargins(15, 15, 15, 15)

        grid_layout = QVBoxLayout()
        
        self.color_buttons = {}
        
        first_row_palettes = [
            ("Red", [
                ("#FFCDD2", "100"), ("#EF9A9A", "200"), ("#E57373", "300"), ("#EF5350", "400"), ("#F44336", "500"), ("#E53935", "600"), ("#D32F2F", "700"), ("#C62828", "800"), ("#B71C1C", "900"), ("#FF8A80", "A100"), ("#FF5252", "A200"), ("#FF1744", "A400"), ("#D50000", "A700")
            ]),
            ("Pink", [
                ("#F8BBD0", "100"), ("#F48FB1", "200"), ("#F06292", "300"), ("#EC407A", "400"), ("#E91E63", "500"), ("#D81B60", "600"), ("#C2185B", "700"), ("#AD1457", "800"), ("#880E4F", "900"), ("#FF80AB", "A100"), ("#FF4081", "A200"), ("#F50057", "A400"), ("#C51162", "A700")
            ]),
            ("Purple", [
                ("#E1BEE7", "100"), ("#CE93D8", "200"), ("#BA68C8", "300"), ("#AB47BC", "400"), ("#9C27B0", "500"), ("#8E24AA", "600"), ("#7B1FA2", "700"), ("#6A1B9A", "800"), ("#4A148C", "900"), ("#EA80FC", "A100"), ("#E040FB", "A200"), ("#D500F9", "A400"), ("#AA00FF", "A700")
            ])
        ]
        
        second_row_palettes = [
            ("Deep Purple", [
                ("#D1C4E9", "100"), ("#B39DDB", "200"), ("#9575CD", "300"), ("#7E57C2", "400"), ("#673AB7", "500"), ("#5E35B1", "600"), ("#512DA8", "700"), ("#4527A0", "800"), ("#311B92", "900"), ("#B388FF", "A100"), ("#7C4DFF", "A200"), ("#651FFF", "A400"), ("#6200EA", "A700")
            ]),
            ("Indigo", [
                ("#C5CAE9", "100"), ("#9FA8DA", "200"), ("#7986CB", "300"), ("#5C6BC0", "400"), ("#3F51B5", "500"), ("#3949AB", "600"), ("#303F9F", "700"), ("#283593", "800"), ("#1A237E", "900"), ("#8C9EFF", "A100"), ("#536DFE", "A200"), ("#3D5AFE", "A400"), ("#304FFE", "A700")
            ]),
            ("Blue", [
                ("#BBDEFB", "100"), ("#90CAF9", "200"), ("#64B5F6", "300"), ("#42A5F5", "400"), ("#2196F3", "500"), ("#1E88E5", "600"), ("#1976D2", "700"), ("#1565C0", "800"), ("#0D47A1", "900"), ("#82B1FF", "A100"), ("#448AFF", "A200"), ("#2979FF", "A400"), ("#2962FF", "A700")
            ])
        ]
        
        third_row_palettes = [
            ("Light Blue", [
                ("#B3E5FC", "100"), ("#81D4FA", "200"), ("#4FC3F7", "300"), ("#29B6F6", "400"), ("#03A9F4", "500"), ("#039BE5", "600"), ("#0288D1", "700"), ("#0277BD", "800"), ("#01579B", "900"), ("#80D8FF", "A100"), ("#40C4FF", "A200"), ("#00B0FF", "A400"), ("#0091EA", "A700")
            ]),
            ("Cyan", [
                ("#B2EBF2", "100"), ("#80DEEA", "200"), ("#4DD0E1", "300"), ("#26C6DA", "400"), ("#00BCD4", "500"), ("#00ACC1", "600"), ("#0097A7", "700"), ("#00838F", "800"), ("#006064", "900"), ("#84FFFF", "A100"), ("#18FFFF", "A200"), ("#00E5FF", "A400"), ("#00B8D4", "A700")
            ]),
            ("Teal", [
                ("#B2DFDB", "100"), ("#80CBC4", "200"), ("#4DB6AC", "300"), ("#26A69A", "400"), ("#009688", "500"), ("#00897B", "600"), ("#00796B", "700"), ("#00695C", "800"), ("#004D40", "900"), ("#A7FFEB", "A100"), ("#64FFDA", "A200"), ("#1DE9B6", "A400"), ("#00BFA5", "A700")
            ])
        ]
        
        fourth_row_palettes = [
            ("Green", [
                ("#C8E6C9", "100"), ("#A5D6A7", "200"), ("#81C784", "300"), ("#66BB6A", "400"), ("#4CAF50", "500"), ("#43A047", "600"), ("#388E3C", "700"), ("#2E7D32", "800"), ("#1B5E20", "900"), ("#B9F6CA", "A100"), ("#69F0AE", "A200"), ("#00E676", "A400"), ("#00C853", "A700")
            ]),
            ("Light Green", [
                ("#DCEDC8", "100"), ("#C5E1A5", "200"), ("#AED581", "300"), ("#9CCC65", "400"), ("#8BC34A", "500"), ("#7CB342", "600"), ("#689F38", "700"), ("#558B2F", "800"), ("#33691E", "900"), ("#CCFF90", "A100"), ("#B2FF59", "A200"), ("#76FF03", "A400"), ("#64DD17", "A700")
            ]),
            ("Lime", [
                ("#F0F4C3", "100"), ("#E6EE9C", "200"), ("#DCE775", "300"), ("#D4E157", "400"), ("#CDDC39", "500"), ("#C0CA33", "600"), ("#AFB42B", "700"), ("#9E9D24", "800"), ("#827717", "900"), ("#F4FF81", "A100"), ("#EEFF41", "A200"), ("#C6FF00", "A400"), ("#AEEA00", "A700")
            ])
        ]
        
        fifth_row_palettes = [
            ("Yellow", [
                ("#FFF9C4", "100"), ("#FFF59D", "200"), ("#FFF176", "300"), ("#FFEE58", "400"), ("#FFEB3B", "500"), ("#FDD835", "600"), ("#FBC02D", "700"), ("#F9A825", "800"), ("#F57F17", "900"), ("#FFFF8D", "A100"), ("#FFFF00", "A200"), ("#FFEA00", "A400"), ("#FFD600", "A700")
            ]),
            ("Amber", [
                ("#FFECB3", "100"), ("#FFE082", "200"), ("#FFD54F", "300"), ("#FFCA28", "400"), ("#FFC107", "500"), ("#FFB300", "600"), ("#FFA000", "700"), ("#FF8F00", "800"), ("#FF6F00", "900"), ("#FFE57F", "A100"), ("#FFD740", "A200"), ("#FFC400", "A400"), ("#FFAB00", "A700")
            ]),
            ("Orange", [
                ("#FFE0B2", "100"), ("#FFCC80", "200"), ("#FFB74D", "300"), ("#FFA726", "400"), ("#FF9800", "500"), ("#FB8C00", "600"), ("#F57C00", "700"), ("#EF6C00", "800"), ("#E65100", "900"), ("#FFD180", "A100"), ("#FFAB40", "A200"), ("#FF9100", "A400"), ("#FF6D00", "A700")
            ])
        ]
        
        for row_palettes in [first_row_palettes, second_row_palettes, third_row_palettes, fourth_row_palettes, fifth_row_palettes]:
            row_layout = QHBoxLayout()
            row_layout.setSpacing(15)
            
            for palette_name, colors in row_palettes:
                column_layout = QVBoxLayout()
                column_layout.setSpacing(3)
                
                palette_label = QLabel(palette_name)
                palette_label.setStyleSheet("margin-bottom: 2px;")
                palette_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
                column_layout.addWidget(palette_label)
                
                color_buttons_layout = QHBoxLayout()
                color_buttons_layout.setSpacing(3)
                
                for color_hex, color_name in colors:
                    color_btn = QPushButton()
                    color_btn.setFixedSize(18, 18)
                    
                    is_current = color_hex == self.current_theme_color
                    border_style = "2px solid #fff" if is_current else "none"
                    
                    color_btn.setStyleSheet(f"""
                        QPushButton {{
                            background-color: {color_hex};
                            border: {border_style};
                            border-radius: 9px;
                        }}
                        QPushButton:hover {{
                            border: 2px solid #fff;
                        }}
                        QPushButton:pressed {{
                            border: 2px solid #fff;
                        }}
                    """)
                    color_btn.setCursor(Qt.CursorShape.PointingHandCursor)
                    color_btn.setToolTip(f"{palette_name} {color_name}\n{color_hex}")
                    color_btn.clicked.connect(lambda checked, color=color_hex, btn=color_btn: self.change_theme_color(color, btn))
                    
                    self.color_buttons[color_hex] = color_btn
                    
                    color_buttons_layout.addWidget(color_btn)
                
                column_layout.addLayout(color_buttons_layout)
                row_layout.addLayout(column_layout)
            
            grid_layout.addLayout(row_layout)

        theme_layout.addLayout(grid_layout)
        theme_layout.addStretch()

        theme_tab.setLayout(theme_layout)
        self.tab_widget.addTab(theme_tab, "Theme")

    def change_theme_color(self, color, clicked_btn=None):
        if hasattr(self, 'color_buttons'):
            for color_hex, btn in self.color_buttons.items():
                if color_hex == self.current_theme_color:
                    btn.setStyleSheet(f"""
                        QPushButton {{
                            background-color: {color_hex};
                            border: none;
                            border-radius: 9px;
                        }}
                        QPushButton:hover {{
                            border: 2px solid #fff;
                        }}
                        QPushButton:pressed {{
                            border: 2px solid #fff;
                        }}
                    """)
                    break
        
        self.current_theme_color = color
        self.settings.setValue('theme_color', color)
        self.settings.sync()
        
        # Apply updated theme palette
        self.apply_theme_stylesheet()

        if clicked_btn:
            clicked_btn.setStyleSheet(f"""
                QPushButton {{
                    background-color: {color};
                    border: 2px solid #fff;
                    border-radius: 9px;
                }}
                QPushButton:hover {{
                    border: 2px solid #fff;
                }}
                QPushButton:pressed {{
                    border: 2px solid #fff;
                }}
            """)

    def apply_theme_stylesheet(self):
        accent = self.current_theme_color or '#2196F3'
        base_stylesheet = qdarkstyle.load_stylesheet_pyqt6()
        custom_styles = f"""
        QWidget#rootWindow {{
            background: qlineargradient(spread:pad, x1:0, y1:0, x2:1, y2:1,
                stop:0 #101422, stop:1 #05060b);
            color: rgba(245, 247, 250, 0.92);
        }}
        QWidget#inputCard {{
            background-color: rgba(18, 21, 30, 0.72);
            border: 1px solid rgba(255, 255, 255, 0.05);
            border-radius: 12px;
        }}
        QTextEdit#inputTextArea, QListWidget#inputList {{
            background-color: rgba(12, 15, 22, 0.85);
            border: 1px solid rgba(255, 255, 255, 0.08);
            border-radius: 8px;
            padding: 8px;
            color: rgba(245, 247, 250, 0.9);
        }}
        QTextEdit#inputTextArea::placeholder {{
            color: rgba(245, 247, 250, 0.45);
        }}
        QLineEdit, QTextEdit, QListWidget {{
            color: rgba(245, 247, 250, 0.9);
        }}
        QLineEdit::placeholder, QTextEdit::placeholder {{
            color: rgba(245, 247, 250, 0.45);
        }}
        QListWidget#trackList {{
            background-color: rgba(12, 15, 22, 0.6);
            border: 1px solid rgba(255, 255, 255, 0.06);
            border-radius: 8px;
        }}
        QListWidget#trackList::item {{
            padding: 6px 10px;
        }}
        QListWidget#trackList::item:hover {{
            background-color: rgba(255, 255, 255, 0.06);
        }}
        QListWidget#trackList::item:selected {{
            background-color: {accent};
            color: #ffffff;
        }}
        QLabel#sectionTitle {{
            font-size: 15px;
            font-weight: 600;
            padding-bottom: 4px;
        }}
        QLabel#emptyState {{
            color: rgba(255, 255, 255, 0.65);
            font-style: italic;
        }}
        QFrame#metricsPanel {{
            background-color: rgba(13, 17, 27, 0.88);
            border: 1px solid rgba(255, 255, 255, 0.06);
            border-radius: 14px;
        }}
        QLabel#metricHeader {{
            color: rgba(255, 255, 255, 0.55);
            font-size: 11px;
            letter-spacing: 1px;
        }}
        QLabel#metricValue {{
            font-size: 16px;
            font-weight: 600;
            color: rgba(245, 247, 250, 0.95);
        }}
        QTextEdit#terminalLog {{
            font-family: "Cascadia Code", "Consolas", monospace;
            background-color: rgba(6, 10, 20, 0.85);
            border: 1px solid rgba(255, 255, 255, 0.08);
            border-radius: 8px;
            color: rgba(245, 247, 250, 0.9);
            selection-background-color: rgba(33, 150, 243, 0.35);
        }}
        QTextEdit#terminalLog::placeholder {{
            color: rgba(245, 247, 250, 0.45);
        }}
        QPushButton {{
            border-radius: 9px;
            padding: 7px 14px;
            font-weight: 500;
        }}
        QPushButton:hover {{
            background-color: rgba(255, 255, 255, 0.06);
        }}
        QPushButton:disabled {{
            background-color: rgba(255, 255, 255, 0.08);
            color: rgba(255, 255, 255, 0.4);
        }}
        QPushButton[accent="true"] {{
            background-color: {accent};
            color: #ffffff;
            font-weight: 600;
        }}
        QPushButton[accent="true"]:hover {{
            background-color: {accent};
            color: #ffffff;
        }}
        QProgressBar {{
            background-color: rgba(12, 15, 22, 0.7);
            border: 1px solid rgba(255, 255, 255, 0.08);
            border-radius: 6px;
            text-align: center;
        }}
        QProgressBar::chunk {{
            border-radius: 6px;
            background-color: {accent};
        }}
        QTabWidget::pane {{
            border: 1px solid rgba(255, 255, 255, 0.05);
            border-radius: 10px;
            background-color: transparent;
        }}
        QTabBar::tab {{
            padding: 8px 18px;
            border-radius: 8px;
            margin: 2px;
        }}
        QTabBar::tab:selected {{
            background-color: rgba(255, 255, 255, 0.08);
            color: rgba(255, 255, 255, 0.95);
        }}
        QTabBar::tab:hover {{
            background-color: rgba(255, 255, 255, 0.04);
        }}
        """
        self.setStyleSheet(base_stylesheet + custom_styles)

        
    def setup_about_tab(self):
        about_tab = QWidget()
        about_layout = QVBoxLayout()
        about_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        about_layout.setSpacing(15)

        sections = [
            ("Check for Updates", "Check", "https://github.com/afkarxyz/SpotiFLAC/releases"),
            ("Report an Issue", "Report", "https://github.com/afkarxyz/SpotiFLAC/issues")
        ]

        for title, button_text, url in sections:
            section_widget = QWidget()
            section_layout = QVBoxLayout(section_widget)
            section_layout.setSpacing(10)
            section_layout.setContentsMargins(0, 0, 0, 0)

            label = QLabel(title)
            label.setStyleSheet("color: palette(text); font-weight: bold;")
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            section_layout.addWidget(label)

            button = QPushButton(button_text)
            button.setFixedSize(120, 25)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(lambda _, url=url: QDesktopServices.openUrl(QUrl(url if url.startswith(('http://', 'https://')) else f'https://{url}')))
            section_layout.addWidget(button, alignment=Qt.AlignmentFlag.AlignCenter)

            about_layout.addWidget(section_widget)

        footer_label = QLabel(f"v{self.current_version} | August 2025")
        footer_label.setStyleSheet("font-size: 12px; margin-top: 20px;")
        about_layout.addWidget(footer_label, alignment=Qt.AlignmentFlag.AlignCenter)

        about_tab.setLayout(about_layout)
        self.tab_widget.addTab(about_tab, "About")
            
    def on_service_changed(self, index):
        service = self.service_dropdown.currentData()
        self.service = service
        self.settings.setValue('service', service)
        self.settings.sync()
        
        self.update_service_ui()
        self.log_output.append(f"Service changed to: {self.service_dropdown.currentText()}")

    def update_service_ui(self):
        service = self.service
        
        region_label = None
        for widget in self.qobuz_region_dropdown.parentWidget().children():
            if isinstance(widget, QLabel) and widget.text() == "Region:":
                region_label = widget
                break

        if service == "qobuz":
            if region_label:
                region_label.show()
            self.qobuz_region_dropdown.show()
        elif service == "deezer":
            if region_label:
                region_label.hide()
            self.qobuz_region_dropdown.hide()
        else:
            if region_label:
                region_label.hide()
            self.qobuz_region_dropdown.hide()

    def save_urls(self):
        self.settings.setValue('spotify_url', self.spotify_urls.toPlainText().strip())
        self.settings.sync()
        
    def save_filename_format(self):
        if self.artist_title_radio.isChecked():
            self.filename_format = "artist_title"
        elif self.title_only_radio.isChecked():
            self.filename_format = "title_only"
        else:
            self.filename_format = "title_artist"
        self.settings.setValue('filename_format', self.filename_format)
        self.settings.sync()
        
    def save_track_numbering(self):
        self.use_track_numbers = self.track_number_checkbox.isChecked()
        self.settings.setValue('use_track_numbers', self.use_track_numbers)
        self.settings.sync()
    
    def save_artist_subfolder_setting(self):
        self.use_artist_subfolders = self.artist_subfolder_checkbox.isChecked()
        self.settings.setValue('use_artist_subfolders', self.use_artist_subfolders)
        self.settings.sync()
    
    def save_album_subfolder_setting(self):
        self.use_album_subfolders = self.album_subfolder_checkbox.isChecked()
        self.settings.setValue('use_album_subfolders', self.use_album_subfolders)
        self.settings.sync()
    
    def save_qobuz_region_setting(self):
        region = self.qobuz_region_dropdown.currentData()
        self.qobuz_region = region
        self.settings.setValue('qobuz_region', region)
        self.settings.sync()
        self.log_output.append(f"Qobuz region setting saved: {self.qobuz_region_dropdown.currentText()}")
    
    def save_concurrent_setting(self):
        self.concurrent_downloads = self.concurrent_spinbox.value()
        self.settings.setValue('concurrent_downloads', self.concurrent_downloads)
        self.settings.sync()
        self.log_output.append(f"Concurrent downloads setting saved: {self.concurrent_downloads} files")
    
    def save_settings(self):
        self.settings.setValue('output_path', self.output_dir.text().strip())
        self.settings.sync()
        self.log_output.append("Settings saved successfully!")

    def update_timer(self):
        self.elapsed_time = self.elapsed_time.addSecs(1)
        self.time_label.setText(self.elapsed_time.toString("hh:mm:ss"))
                        
    def browse_playlist_files(self):
        """Browse for multiple playlist files (M3U, M3U8, PLS)"""
        file_paths, _ = QFileDialog.getOpenFileNames(
            self, 
            "Select Playlist Files", 
            str(Path.home()), 
            "Playlist Files (*.m3u *.m3u8 *.pls);;All Files (*)"
        )
        
        if file_paths:
            # Add new files to the list, avoiding duplicates
            for file_path in file_paths:
                if file_path not in self.selected_playlist_files:
                    self.selected_playlist_files.append(file_path)
                    self.playlist_files_list.addItem(os.path.basename(file_path))
            
            self.spotify_urls.clear()  # Clear Spotify URLs when playlist files are selected
            
    def clear_playlist_files(self):
        """Clear selected playlist files"""
        self.selected_playlist_files.clear()
        self.playlist_files_list.clear()

    def parse_m3u_file(self, file_path):
        """Parse M3U/M3U8 playlist files"""
        tracks = []
        
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                lines = [line.strip() for line in f.readlines() if line.strip()]
            
            current_title = ""
            current_artist = ""
            current_duration = 0
            
            for i, line in enumerate(lines):
                if line.startswith('#EXTINF:'):
                    # Parse EXTINF line: #EXTINF:duration,artist - title
                    try:
                        parts = line.split(',', 1)
                        if len(parts) > 1:
                            duration_part = parts[0].replace('#EXTINF:', '')
                            duration = float(duration_part) if duration_part.replace('.', '').isdigit() else 0
                            current_duration = int(duration * 1000)  # Convert to milliseconds
                            
                            title_artist = parts[1].strip()
                            if ' - ' in title_artist:
                                current_artist, current_title = title_artist.split(' - ', 1)
                            else:
                                current_title = title_artist
                                current_artist = "Unknown Artist"
                    except:
                        current_title = f"Track {len(tracks) + 1}"
                        current_artist = "Unknown Artist"
                        current_duration = 0
                        
                elif line and not line.startswith('#'):
                    # This should be a URL or file path
                    if line.startswith('http') and 'spotify' in line:
                        # Spotify URL in playlist
                        track_id = line.split('/')[-1].split('?')[0]
                    else:
                        # Use filename as track ID if not a Spotify URL
                        track_id = f"playlist_track_{len(tracks)}"
                    
                    # Extract artist and title from file path if no #EXTINF metadata
                    if not current_title or not current_artist or (current_artist == "Unknown Artist" and current_title.startswith("Track")):
                        filename = os.path.basename(line)
                        # Remove file extension
                        name_without_ext = os.path.splitext(filename)[0]
                        
                        # Try to parse artist and title from filename
                        if ' - ' in name_without_ext:
                            parsed_artist, parsed_title = name_without_ext.split(' - ', 1)
                            current_artist = parsed_artist.strip()
                            current_title = parsed_title.strip()
                        else:
                            # Use entire filename as title if no separator found
                            current_title = name_without_ext
                            current_artist = "Unknown Artist"
                    
                    # Create track object
                    track = Track(
                        external_urls=line if line.startswith('http') else "",
                        title=current_title if current_title else f"Track {len(tracks) + 1}",
                        artists=current_artist if current_artist else "Unknown Artist", 
                        album="Playlist",
                        track_number=len(tracks) + 1,
                        duration_ms=current_duration,
                        id=track_id,
                        isrc=""
                    )
                    tracks.append(track)
                    
                    # Reset for next track
                    current_title = ""
                    current_artist = ""
                    current_duration = 0
                    
        except Exception as e:
            raise Exception(f"Error parsing M3U file: {str(e)}")
            
        return tracks

    def parse_pls_file(self, file_path):
        """Parse PLS playlist files"""
        tracks = []
        
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                lines = [line.strip() for line in f.readlines() if line.strip()]
            
            track_data = {}
            
            for line in lines:
                if line.startswith('[playlist]'):
                    continue
                elif line.startswith('NumberOfEntries='):
                    continue
                elif line.startswith('Version='):
                    continue
                elif '=' in line:
                    key, value = line.split('=', 1)
                    
                    # Extract track number and property
                    if key.startswith('File'):
                        track_num = key.replace('File', '')
                        if track_num not in track_data:
                            track_data[track_num] = {}
                        track_data[track_num]['file'] = value
                        
                    elif key.startswith('Title'):
                        track_num = key.replace('Title', '')
                        if track_num not in track_data:
                            track_data[track_num] = {}
                        
                        title = value
                        if ' - ' in title:
                            artist, title_only = title.split(' - ', 1)
                            track_data[track_num]['artist'] = artist
                            track_data[track_num]['title'] = title_only
                        else:
                            track_data[track_num]['title'] = title
                            track_data[track_num]['artist'] = "Unknown Artist"
                            
                    elif key.startswith('Length'):
                        track_num = key.replace('Length', '')
                        if track_num not in track_data:
                            track_data[track_num] = {}
                        duration = int(value) if value.isdigit() else 0
                        track_data[track_num]['duration'] = duration * 1000  # Convert to milliseconds
            
            # Convert track_data to Track objects
            for track_num in sorted(track_data.keys(), key=lambda x: int(x) if x.isdigit() else 0):
                data = track_data[track_num]
                file_url = data.get('file', '')
                
                if file_url.startswith('http') and 'spotify' in file_url:
                    track_id = file_url.split('/')[-1].split('?')[0]
                else:
                    track_id = f"playlist_track_{track_num}"
                
                track = Track(
                    external_urls=file_url if file_url.startswith('http') else "",
                    title=data.get('title', f"Track {track_num}"),
                    artists=data.get('artist', "Unknown Artist"),
                    album="Playlist", 
                    track_number=int(track_num) if track_num.isdigit() else len(tracks) + 1,
                    duration_ms=data.get('duration', 0),
                    id=track_id,
                    isrc=""
                )
                tracks.append(track)
                
        except Exception as e:
            raise Exception(f"Error parsing PLS file: {str(e)}")
            
        return tracks

    def load_playlist_file(self, file_path):
        """Load and parse playlist file"""
        try:
            self.reset_state()
            self.reset_ui()
            
            self.log_output.append(f'Loading playlist file: {os.path.basename(file_path)}')
            self.tab_widget.setCurrentWidget(self.process_tab)
            
            # Determine file type and parse accordingly
            file_ext = os.path.splitext(file_path)[1].lower()
            
            if file_ext in ['.m3u', '.m3u8']:
                tracks = self.parse_m3u_file(file_path)
            elif file_ext == '.pls':
                tracks = self.parse_pls_file(file_path)
            else:
                # Try to auto-detect format by content
                try:
                    with open(file_path, 'r', encoding='utf-8') as f:
                        content = f.read(1000)  # Read first 1000 chars
                    
                    if '[playlist]' in content.lower():
                        tracks = self.parse_pls_file(file_path)
                    elif '#EXTM3U' in content or '#EXTINF' in content:
                        tracks = self.parse_m3u_file(file_path)
                    else:
                        # Simple text file with one track per line
                        tracks = self.parse_simple_playlist(file_path)
                        
                except Exception:
                    tracks = self.parse_simple_playlist(file_path)
            
            if not tracks:
                self.log_output.append('Error: No valid tracks found in playlist file.')
                return
                
            # Handle playlist file data
            self.handle_playlist_file_data(tracks, os.path.basename(file_path))
            self.log_output.append(f'Successfully loaded {len(tracks)} tracks from playlist file.')
            self.tab_widget.setCurrentIndex(0)
            
        except Exception as e:
            self.log_output.append(f'Error loading playlist file: {str(e)}')

    def parse_simple_playlist(self, file_path):
        """Parse simple text playlist (one track per line)"""
        tracks = []
        
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                lines = [line.strip() for line in f.readlines() if line.strip()]
            
            for i, line in enumerate(lines):
                if line.startswith('#'):
                    continue  # Skip comments
                    
                # Try to parse as "Artist - Title" or use as title
                if ' - ' in line:
                    artist, title = line.split(' - ', 1)
                else:
                    artist = "Unknown Artist"
                    title = line
                
                track = Track(
                    external_urls="",
                    title=title.strip(),
                    artists=artist.strip(),
                    album="Playlist",
                    track_number=i + 1,
                    duration_ms=0,
                    id=f"simple_track_{i}",
                    isrc=""
                )
                tracks.append(track)
                
        except Exception as e:
            raise Exception(f"Error parsing simple playlist: {str(e)}")
            
        return tracks

    def handle_playlist_file_data(self, tracks, filename):
        """Handle parsed playlist file data"""
        self.tracks = tracks
        self.all_tracks = tracks.copy()
        self.is_playlist = True
        self.is_album = self.is_single_track = False
        self.album_or_playlist_name = os.path.splitext(filename)[0]  # Use filename without extension
        
        # Create metadata for display
        metadata = {
            'title': self.album_or_playlist_name,
            'artists': f"{len(tracks)} tracks from file",
            'cover': '',  # No cover for playlist files
            'total_tracks': len(tracks)
        }
        
        self.update_display_after_fetch(metadata)
        
        # Automatically start searching for streaming service IDs (like Spotify workflow)
        self.log_output.append(f'\nAutomatically searching for tracks on streaming services...')
        self.auto_fetch_track_ids()

    def fetch_tracks(self):
        """Enhanced fetch method for multiple Spotify URLs and playlist files"""
        # Get all input sources
        spotify_urls_text = self.spotify_urls.toPlainText().strip()
        playlist_files = self.selected_playlist_files.copy()
        
        # Parse multiple Spotify URLs
        spotify_urls = [url.strip() for url in spotify_urls_text.split('\n') if url.strip()] if spotify_urls_text else []
        
        if not spotify_urls and not playlist_files:
            self.log_output.append('Warning: Please enter Spotify URLs or select playlist files.')
            return
            
        try:
            self.reset_state()
            self.reset_ui()
            
            self.log_output.append('Processing multiple sources...')
            self.tab_widget.setCurrentWidget(self.process_tab)
            
            # Process all sources and combine tracks
            self.process_multiple_sources(spotify_urls, playlist_files)
            
        except Exception as e:
            self.log_output.append(f'Error: Failed to start multi-source fetch: {str(e)}')
    
    def process_multiple_sources(self, spotify_urls, playlist_files):
        """Process multiple Spotify URLs and playlist files with source tracking"""
        all_tracks = []
        total_sources = len(spotify_urls) + len(playlist_files)
        
        self.log_output.append(f'Processing {len(spotify_urls)} Spotify URLs and {len(playlist_files)} playlist files...')
        
        # Process Spotify URLs first
        for i, url in enumerate(spotify_urls):
            try:
                self.log_output.append(f'Fetching Spotify URL {i+1}/{len(spotify_urls)}: {url[:50]}...')
                
                # Get metadata for each URL
                metadata = get_filtered_data(url)
                if "error" in metadata:
                    self.log_output.append(f'Error with URL {i+1}: {metadata["error"]}')
                    continue
                
                url_info = parse_uri(url)
                tracks_from_url = []
                source_name = ""
                source_type = ""
                
                if url_info["type"] == "track":
                    # Single track
                    track_data = metadata["track"]
                    track_id = track_data["external_urls"].split("/")[-1]
                    source_name = f"{track_data['name']} - {track_data['artists']}"
                    source_type = "spotify_track"
                    
                    track = Track(
                        external_urls=track_data["external_urls"],
                        title=track_data["name"],
                        artists=track_data["artists"],
                        album=track_data["album_name"],
                        track_number=1,
                        duration_ms=track_data.get("duration_ms", 0),
                        id=track_id,
                        isrc=track_data.get("isrc", ""),
                        source_name=source_name,
                        source_type=source_type
                    )
                    tracks_from_url = [track]
                    
                elif url_info["type"] == "album":
                    # Album tracks
                    album_data = metadata
                    source_name = album_data["album_info"]["name"]
                    source_type = "spotify_album"
                    
                    for j, track in enumerate(album_data["track_list"]):
                        track_id = track["external_urls"].split("/")[-1]
                        
                        track_obj = Track(
                            external_urls=track["external_urls"],
                            title=track["name"],
                            artists=track["artists"],
                            album=track["album_name"],
                            track_number=track["track_number"],
                            duration_ms=track.get("duration_ms", 0),
                            id=track_id,
                            isrc=track.get("isrc", ""),
                            source_name=source_name,
                            source_type=source_type
                        )
                        tracks_from_url.append(track_obj)
                        
                elif url_info["type"] == "playlist":
                    # Playlist tracks
                    playlist_data = metadata
                    source_name = playlist_data["playlist_info"]["owner"]["name"]  # Fixed: correct path to playlist name
                    source_type = "spotify_playlist"
                    
                    for j, track in enumerate(playlist_data["track_list"]):
                        track_id = track["external_urls"].split("/")[-1]
                        
                        track_obj = Track(
                            external_urls=track["external_urls"],
                            title=track["name"],
                            artists=track["artists"],
                            album=track["album_name"],
                            track_number=j + 1,
                            duration_ms=track.get("duration_ms", 0),
                            id=track_id,
                            isrc=track.get("isrc", ""),
                            source_name=source_name,
                            source_type=source_type
                        )
                        tracks_from_url.append(track_obj)
                
                all_tracks.extend(tracks_from_url)
                self.log_output.append(f'Added {len(tracks_from_url)} tracks from Spotify {url_info["type"]}: {source_name}')
                
            except Exception as e:
                self.log_output.append(f'Error processing Spotify URL {i+1}: {str(e)}')
                continue
        
        # Process playlist files
        for i, file_path in enumerate(playlist_files):
            try:
                filename = os.path.basename(file_path)
                self.log_output.append(f'Processing playlist file {i+1}/{len(playlist_files)}: {filename}')
                
                # Parse playlist file
                file_ext = os.path.splitext(file_path)[1].lower()
                
                if file_ext in ['.m3u', '.m3u8']:
                    tracks_from_file = self.parse_m3u_file(file_path)
                elif file_ext == '.pls':
                    tracks_from_file = self.parse_pls_file(file_path)
                else:
                    # Auto-detect format
                    try:
                        with open(file_path, 'r', encoding='utf-8') as f:
                            content = f.read(1000)
                        
                        if '[playlist]' in content.lower():
                            tracks_from_file = self.parse_pls_file(file_path)
                        elif '#EXTM3U' in content or '#EXTINF' in content:
                            tracks_from_file = self.parse_m3u_file(file_path)
                        else:
                            tracks_from_file = self.parse_simple_playlist(file_path)
                    except Exception:
                        tracks_from_file = self.parse_simple_playlist(file_path)
                
                # Set source information for playlist file tracks
                source_name = os.path.splitext(filename)[0]  # Use filename without extension
                source_type = "playlist_file"
                
                for j, track in enumerate(tracks_from_file):
                    track.track_number = j + 1  # Reset numbering per source
                    track.source_name = source_name
                    track.source_type = source_type
                    all_tracks.append(track)
                    
                self.log_output.append(f'Added {len(tracks_from_file)} tracks from playlist file: {source_name}')
                
            except Exception as e:
                self.log_output.append(f'Error processing playlist file {i+1}: {str(e)}')
                continue
        
        if not all_tracks:
            self.log_output.append('Error: No valid tracks found from any source.')
            return
            
        # Set up for multi-source processing
        self.tracks = all_tracks
        self.all_tracks = all_tracks.copy()
        self.is_playlist = True  # Treat as playlist for multi-source
        self.is_album = self.is_single_track = False
        self.album_or_playlist_name = f"Multi-Source Collection ({total_sources} sources)"
        
        # Create combined metadata for display
        metadata = {
            'title': f"Multi-Source Collection",
            'artists': f"{len(all_tracks)} tracks from {total_sources} sources",
            'cover': '',  # No cover for multi-source
            'total_tracks': len(all_tracks)
        }
        
        self.update_display_after_fetch(metadata)
        
        # Count tracks that need streaming service search (no ISRC)
        tracks_needing_search = [track for track in all_tracks if not track.isrc]
        tracks_ready = [track for track in all_tracks if track.isrc]
        
        if tracks_needing_search:
            self.log_output.append(f'\nAutomatically searching for {len(tracks_needing_search)} tracks on streaming services...')
            self.log_output.append(f'{len(tracks_ready)} tracks already have streaming IDs and are ready.')
            self.auto_fetch_track_ids()
        else:
            self.log_output.append(f'\nAll {len(tracks_ready)} tracks have streaming IDs and are ready to download!')
            self.log_output.append(f'Automatically starting download for all {len(tracks_ready)} tracks...')
            
            # Automatically start downloading when all tracks are ready
            try:
                # Switch to dashboard to show tracks
                self.tab_widget.setCurrentIndex(0)
                # Trigger automatic download
                self.download_all()
            except Exception as e:
                self.log_output.append(f'Error: Failed to start automatic download: {str(e)}')
                self.tab_widget.setCurrentIndex(0)
    
    def on_metadata_fetched(self, metadata):
        try:
            url_info = parse_uri(self.spotify_url.text().strip())
            
            if url_info["type"] == "track":
                self.handle_track_metadata(metadata["track"])
            elif url_info["type"] == "album":
                self.handle_album_metadata(metadata)
            elif url_info["type"] == "playlist":
                self.handle_playlist_metadata(metadata)
                
            self.update_button_states()
            self.tab_widget.setCurrentIndex(0)
        except Exception as e:
            self.log_output.append(f'Error: {str(e)}')
    
    def on_metadata_error(self, error_message):
        self.log_output.append(f'Error: {error_message}')

    def handle_track_metadata(self, track_data):
        track_id = track_data["external_urls"].split("/")[-1]
        
        track = Track(
            external_urls=track_data["external_urls"],
            title=track_data["name"],
            artists=track_data["artists"],
            album=track_data["album_name"],
            track_number=1,
            duration_ms=track_data.get("duration_ms", 0),
            id=track_id,
            isrc=track_data.get("isrc", "")
        )
        
        self.tracks = [track]
        self.all_tracks = [track]
        self.is_single_track = True
        self.is_album = self.is_playlist = False
        self.album_or_playlist_name = f"{self.tracks[0].title} - {self.tracks[0].artists}"
        
        metadata = {
            'title': track_data["name"],
            'artists': track_data["artists"],
            'releaseDate': track_data["release_date"],
            'cover': track_data["images"],
            'duration_ms': track_data.get("duration_ms", 0)
        }
        self.update_display_after_fetch(metadata)

    def handle_album_metadata(self, album_data):
        self.album_or_playlist_name = album_data["album_info"]["name"]
        self.tracks = []
        
        for track in album_data["track_list"]:
            track_id = track["external_urls"].split("/")[-1]
            
            self.tracks.append(Track(
                external_urls=track["external_urls"],
                title=track["name"],
                artists=track["artists"],
                album=self.album_or_playlist_name,
                track_number=track["track_number"],
                duration_ms=track.get("duration_ms", 0),
                id=track_id,
                isrc=track.get("isrc", "")
            ))
        
        self.all_tracks = self.tracks.copy()
        self.is_album = True
        self.is_playlist = self.is_single_track = False
        
        metadata = {
            'title': album_data["album_info"]["name"],
            'artists': album_data["album_info"]["artists"],
            'releaseDate': album_data["album_info"]["release_date"],
            'cover': album_data["album_info"]["images"],
            'total_tracks': album_data["album_info"]["total_tracks"]
        }
        self.update_display_after_fetch(metadata)

    def handle_playlist_metadata(self, playlist_data):
        self.album_or_playlist_name = playlist_data["playlist_info"]["owner"]["name"]
        self.tracks = []
        
        for track in playlist_data["track_list"]:
            track_id = track["external_urls"].split("/")[-1]
            
            self.tracks.append(Track(
                external_urls=track["external_urls"],
                title=track["name"],
                artists=track["artists"],
                album=track["album_name"],
                track_number=track.get("track_number", len(self.tracks) + 1),
                duration_ms=track.get("duration_ms", 0),
                id=track_id,
                isrc=track.get("isrc", "")
            ))
        
        self.all_tracks = self.tracks.copy()
        self.is_playlist = True
        self.is_album = self.is_single_track = False
        
        metadata = {
            'title': playlist_data["playlist_info"]["owner"]["name"],
            'artists': playlist_data["playlist_info"]["owner"]["display_name"],
            'cover': playlist_data["playlist_info"]["owner"]["images"],
            'followers': playlist_data["playlist_info"]["followers"]["total"],
            'total_tracks': playlist_data["playlist_info"]["tracks"]["total"]        }
        self.update_display_after_fetch(metadata)

    def update_display_after_fetch(self, metadata):
        self.track_list.setVisible(not self.is_single_track)
        
        if not self.is_single_track:
            self.search_widget.show()
            self.update_track_list_display()
        else:
            self.search_widget.hide()
        
        self.update_info_widget(metadata)

    def update_info_widget(self, metadata):
        self.title_label.setText(metadata['title'])
        
        if self.is_single_track or self.is_album:
            artists = metadata['artists'] if isinstance(metadata['artists'], list) else metadata['artists'].split(", ")
            label_text = "Artists" if len(artists) > 1 else "Artist"
            artists_text = ", ".join(artists)
            self.artists_label.setText(f"<b>{label_text}</b> {artists_text}")
        else:
            self.artists_label.setText(f"<b>Owner</b> {metadata['artists']}")
        
        if self.is_playlist and 'followers' in metadata:
            self.followers_label.setText(f"<b>Followers</b> {metadata['followers']:,}")
            self.followers_label.show()
        else:
            self.followers_label.hide()
        
        if metadata.get('releaseDate'):
            try:
                release_date = metadata['releaseDate']
                if len(release_date) == 4:
                    date_obj = datetime.strptime(release_date, "%Y")
                elif len(release_date) == 7:
                    date_obj = datetime.strptime(release_date, "%Y-%m")
                else:
                    date_obj = datetime.strptime(release_date, "%Y-%m-%d")
                
                formatted_date = date_obj.strftime("%d-%m-%Y")
                self.release_date_label.setText(f"<b>Released</b> {formatted_date}")
                self.release_date_label.show()
            except ValueError:
                self.release_date_label.setText(f"<b>Released</b> {metadata['releaseDate']}")
                self.release_date_label.show()
        else:
            self.release_date_label.hide()
        
        if self.is_single_track:
            duration = self.format_duration(metadata.get('duration_ms', 0))
            self.type_label.setText(f"<b>Duration</b> {duration}")
        elif self.is_album:
            total_tracks = metadata.get('total_tracks', 0)
            self.type_label.setText(f"<b>Album</b> | {total_tracks} tracks")
        elif self.is_playlist:
            total_tracks = metadata.get('total_tracks', 0)
            self.type_label.setText(f"<b>Playlist</b> | {total_tracks} tracks")
        
        self.network_manager.get(QNetworkRequest(QUrl(metadata['cover'])))
        
        self.info_widget.show()

    def reset_info_widget(self):
        self.title_label.clear()
        self.artists_label.clear()
        self.followers_label.clear()
        self.release_date_label.clear()
        self.type_label.clear()
        self.cover_label.clear()
        self.info_widget.hide()

    def on_cover_loaded(self, reply):
        if reply.error() == QNetworkReply.NetworkError.NoError:
            data = reply.readAll()
            pixmap = QPixmap()
            pixmap.loadFromData(data)
            self.cover_label.setPixmap(pixmap)

    def update_button_states(self):
        if self.is_single_track:
            for btn in [self.download_selected_btn, self.download_all_btn, self.remove_btn, self.clear_btn]:
                btn.hide()
            
            self.single_track_container.show()
            
            self.single_download_btn.setEnabled(True)
            self.single_clear_btn.setEnabled(True)
            
        else:
            self.single_track_container.hide()
            
            # Check if this is a playlist from file (no ISRCs) to disable download buttons
            has_no_isrc = all(not track.isrc for track in self.tracks) if self.tracks else False
            from_playlist_file = hasattr(self, 'playlist_file_path') and self.playlist_file_path.text()
            
            if from_playlist_file and has_no_isrc:
                self.download_selected_btn.setEnabled(False)
                self.download_all_btn.setEnabled(False)
                self.download_selected_btn.setText('Use Fetch First')
                self.download_all_btn.setText('Use Fetch First')
            else:
                self.download_selected_btn.setEnabled(True)
                self.download_all_btn.setEnabled(True)
                self.download_selected_btn.setText('Download Selected')
                self.download_all_btn.setText('Download All')
            
            self.download_selected_btn.show()
            self.download_all_btn.show()
            self.remove_btn.show()
            self.clear_btn.show()
            
            self.download_all_btn.setMinimumWidth(120)
            self.clear_btn.setMinimumWidth(120)

    def hide_track_buttons(self):
        buttons = [
            self.download_selected_btn,
            self.download_all_btn,
            self.remove_btn,
            self.clear_btn
        ]
        for btn in buttons:
            btn.hide()
        
        if hasattr(self, 'single_track_container'):
            self.single_track_container.hide()

    def download_selected(self):
        if self.is_single_track:
            self.download_all()
        else:
            selected_items = self.track_list.selectedItems()            
            if not selected_items:
                self.log_output.append('Warning: Please select tracks to download.')
                return
            selected_indices = [self.track_list.row(item) for item in selected_items]
            self.download_tracks(selected_indices)

    def download_all(self):
        if self.is_single_track:
            self.download_tracks([0])
        else:
            self.download_tracks(range(len(self.tracks)))

    def download_tracks(self, indices):
        # Don't clear log for automatic downloads - preserve search results
        if not hasattr(self, 'search_worker') or not self.search_worker.isRunning():
            self.log_output.clear()
            
        raw_outpath = self.output_dir.text().strip()
        outpath = os.path.normpath(raw_outpath)
        if not os.path.exists(outpath):
            self.log_output.append('Warning: Invalid output directory.')
            return

        tracks_to_download = self.tracks if self.is_single_track else [self.tracks[i] for i in indices]
        
        # Filter out tracks without ISRC for download
        downloadable_tracks = [track for track in tracks_to_download if track.isrc]
        skipped_tracks = [track for track in tracks_to_download if not track.isrc]
        
        if not downloadable_tracks:
            self.log_output.append('Warning: No tracks with valid streaming IDs found. Unable to download.')
            return
            
        if skipped_tracks:
            self.log_output.append(f'Skipping {len(skipped_tracks)} tracks without streaming IDs.')

        self.log_output.append(f'Starting download for {len(downloadable_tracks)} tracks...')

        if self.is_album or self.is_playlist:
            name = self.album_or_playlist_name.strip()
            folder_name = re.sub(r'[<>:"/\\|?*]', '_', name)
            outpath = os.path.join(outpath, folder_name)
            os.makedirs(outpath, exist_ok=True)

        try:
            self.start_download_worker(downloadable_tracks, outpath)
        except Exception as e:
            self.log_output.append(f"Error: An error occurred while starting the download: {str(e)}")
    
    def start_download_worker(self, tracks_to_download, outpath):
        service = self.service_dropdown.currentData()
        qobuz_region = self.qobuz_region_dropdown.currentData() if service == "qobuz" else "us"
        
        self.worker = DownloadWorker(
            tracks_to_download, 
            outpath,
            self.is_single_track, 
            self.is_album, 
            self.is_playlist, 
            self.album_or_playlist_name,
            self.filename_format,
            self.use_track_numbers,
            self.use_artist_subfolders,
            self.use_album_subfolders,
            service,
            qobuz_region,
            self.concurrent_downloads
        )
        self.worker.finished.connect(self.on_download_finished)
        self.worker.progress.connect(self.update_progress)
        self.worker.speed_update.connect(self.on_download_metrics)
        self.reset_metrics_panel("Starting...")
        self.worker.start()
        self.start_timer()
        self.update_ui_for_download_start()

    def update_ui_for_download_start(self):
        self.download_selected_btn.setEnabled(False)
        self.download_all_btn.setEnabled(False)
        
        if hasattr(self, 'single_download_btn'):
            self.single_download_btn.setEnabled(False)
        if hasattr(self, 'single_clear_btn'):
            self.single_clear_btn.setEnabled(False)
            
        self.reset_metrics_panel("Downloading...")
        self.stop_btn.show()
        self.pause_resume_btn.show()
        self.progress_bar.show()
        self.progress_bar.setValue(0)
        
        self.tab_widget.setCurrentWidget(self.process_tab)

    def update_progress(self, message, percentage):
        self.log_output.append(message)
        self.log_output.moveCursor(QTextCursor.MoveOperation.End)
        if percentage > 0:
            self.progress_bar.setValue(percentage)

    def stop_download(self):
        if hasattr(self, 'worker'):
            self.worker.stop()
        self.stop_timer()
        self.on_download_finished(True, "Download stopped by user.", [])
        self.reset_metrics_panel("Stopped")
        
    def on_download_finished(self, success, message, failed_tracks):
        self.progress_bar.hide()
        self.stop_btn.hide()
        self.pause_resume_btn.hide()
        self.pause_resume_btn.setText('Pause')
        self.stop_timer()
        
        self.download_selected_btn.setEnabled(True)
        self.download_all_btn.setEnabled(True)
        
        if hasattr(self, 'single_download_btn'):
            self.single_download_btn.setEnabled(True)
        if hasattr(self, 'single_clear_btn'):
            self.single_clear_btn.setEnabled(True)
        
        if success:
            self.log_output.append(f"\nStatus: {message}")
            if failed_tracks:
                self.log_output.append("\nFailed downloads:")
                for title, artists, error in failed_tracks:
                    self.log_output.append(f"- {title} - {artists}")
                    self.log_output.append(f"  Error: {error}\n")
        else:
            self.log_output.append(f"Error: {message}")

        self.tab_widget.setCurrentWidget(self.process_tab)
    
    def toggle_pause_resume(self):
        if hasattr(self, 'worker'):
            if self.worker.is_paused:
                self.worker.resume()
                self.pause_resume_btn.setText('Pause')
                self.timer.start(1000)
            else:
                self.worker.pause()
                self.pause_resume_btn.setText('Resume')

    def remove_selected_tracks(self):
        if not self.is_single_track:
            selected_items = self.track_list.selectedItems()
            selected_indices = [self.track_list.row(item) for item in selected_items]
            
            tracks_to_remove = [self.tracks[i] for i in selected_indices]
            
            for track in tracks_to_remove:
                if track in self.tracks:
                    self.tracks.remove(track)
                if track in self.all_tracks:
                    self.all_tracks.remove(track)
            
            if self.is_playlist:
                for i, track in enumerate(self.all_tracks, 1):
                    track.track_number = i
            
            self.update_track_list_display()

    def auto_fetch_track_ids(self):
        """Automatically search for streaming service IDs for playlist tracks (like Spotify workflow)"""
        if not self.tracks:
            return
            
        self.progress_bar.show()
        self.progress_bar.setValue(0)
        
        try:
            # Use more concurrent searches for automatic mode to speed up large playlists
            self.search_worker = TrackSearchWorker(self.tracks.copy(), concurrent_searches=12)
            self.search_worker.finished.connect(self.on_auto_track_search_finished)
            self.search_worker.progress.connect(self.update_progress)
            self.search_worker.error.connect(self.on_track_search_error)
            self.search_worker.start()
            
        except Exception as e:
            self.log_output.append(f'Error: Failed to start automatic track search: {str(e)}')

    def fetch_track_ids(self):
        """Search for streaming service IDs for playlist tracks"""
        if not self.tracks:
            self.log_output.append('Warning: No tracks to search for.')
            return
            
        self.log_output.clear()
        self.log_output.append(f'Searching for {len(self.tracks)} tracks on streaming services...')
        self.tab_widget.setCurrentWidget(self.process_tab)
        
        self.progress_bar.show()
        self.progress_bar.setValue(0)
        
        try:
            self.search_worker = TrackSearchWorker(self.tracks.copy(), concurrent_searches=8)
            self.search_worker.finished.connect(self.on_track_search_finished)
            self.search_worker.progress.connect(self.update_progress)
            self.search_worker.error.connect(self.on_track_search_error)
            self.search_worker.start()
            
        except Exception as e:
            self.log_output.append(f'Error: Failed to start track search: {str(e)}')

    def on_auto_track_search_finished(self, updated_tracks, successful_count):
        """Handle completion of automatic track ID search for playlist files (seamless like Spotify)"""
        self.progress_bar.hide()
        
        # Update tracks with new metadata
        self.tracks = updated_tracks
        self.all_tracks = updated_tracks.copy()
        
        # Update the display
        self.update_track_list_display()
        self.update_button_states()  # Re-evaluate button states
        
        # Show results - more concise for automatic workflow
        total_tracks = len(updated_tracks)
        self.log_output.append(f'Track search completed: {successful_count}/{total_tracks} tracks found on streaming services.')
        
        if successful_count > 0:
            self.log_output.append(f'Automatically starting download for {successful_count} matched tracks...')
            
            # Automatically start downloading all found tracks (like Spotify workflow)
            try:
                self.download_all()
            except Exception as e:
                self.log_output.append(f'Error: Failed to start automatic download: {str(e)}')
                self.tab_widget.setCurrentIndex(0)
        else:
            self.log_output.append(f'No tracks could be matched on streaming services. Unable to download.')
            self.tab_widget.setCurrentIndex(0)
        
        if successful_count < total_tracks:
            failed_count = total_tracks - successful_count
            self.log_output.append(f'Note: {failed_count} tracks could not be matched and will be skipped.')

    def on_track_search_finished(self, updated_tracks, successful_count):
        """Handle completion of track ID search"""
        self.progress_bar.hide()
        
        # Update tracks with new metadata
        self.tracks = updated_tracks
        self.all_tracks = updated_tracks.copy()
        
        # Update the display
        self.update_track_list_display()
        self.update_button_states()  # Re-evaluate button states
        
        # Show results
        total_tracks = len(updated_tracks)
        self.log_output.append(f'\nSearch completed!')
        self.log_output.append(f'Found streaming IDs for {successful_count}/{total_tracks} tracks.')
        
        if successful_count > 0:
            self.log_output.append(f'You can now download the {successful_count} matched tracks.')
        
        if successful_count < total_tracks:
            failed_count = total_tracks - successful_count
            self.log_output.append(f'{failed_count} tracks could not be matched and may fail to download.')
            
        self.tab_widget.setCurrentIndex(0)
            
    def on_track_search_error(self, error_message):
        """Handle track search errors"""
        self.progress_bar.hide()
        self.log_output.append(f'Search error: {error_message}')

    def clear_tracks(self):
        """Clear all tracks and reset interface"""
        self.reset_state()
        self.reset_ui()
        
        # Clear all input sources
        if hasattr(self, 'spotify_urls'):
            self.spotify_urls.clear()
        if hasattr(self, 'selected_playlist_files'):
            self.selected_playlist_files.clear()
        if hasattr(self, 'playlist_files_list'):
            self.playlist_files_list.clear()
            
        self.tab_widget.setCurrentIndex(0)
        self.reset_metrics_panel('Idle')

    def start_timer(self):
        self.elapsed_time = QTime(0, 0, 0)
        self.time_label.setText("00:00:00")
        self.time_label.show()
        self.timer.start(1000)
    
    def stop_timer(self):
        self.timer.stop()
        self.time_label.hide()

    def closeEvent(self, event):
        if hasattr(self, 'timer'):
            self.timer.stop()
        
        if hasattr(self, 'service_dropdown'):
            for attr_name in ['tidal_status_checker', 'deezer_status_checker']:
                if hasattr(self.service_dropdown, attr_name):
                    checker = getattr(self.service_dropdown, attr_name)
                    if checker.isRunning():
                        checker.quit()
                        checker.wait()
        
        if hasattr(self, 'qobuz_region_dropdown'):
            for checker in self.qobuz_region_dropdown.status_checkers.values():
                if checker.isRunning():
                    checker.quit()
                    checker.wait()
        
        if hasattr(self, 'worker') and self.worker and self.worker.isRunning():
            self.worker.stop()
            self.worker.quit()
            self.worker.wait()
        
        event.accept()

if __name__ == '__main__':
    try:
        if sys.platform == "win32":
            import io
            sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
            sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')
    except Exception as e:
        pass
        
    app = QApplication(sys.argv)
    
    settings = QSettings('SpotiFLAC', 'Settings')
    theme_color = settings.value('theme_color', '#2196F3')
    
    ex = SpotiFLACGUI()
    ex.apply_theme_stylesheet()
    ex.show()
    sys.exit(app.exec())


































