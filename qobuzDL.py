import requests
import time
import os
import re
from datetime import datetime
from mutagen.flac import FLAC, Picture
from mutagen.id3 import PictureType

class ProgressCallback:
    def __call__(self, current, total):
        if total > 0:
            percent = (current / total) * 100
            print(f"\r{percent:.2f}% ({current}/{total})", end="")
        else:
            print(f"\r{current / (1024 * 1024):.2f} MB", end="")

class QobuzDownloader:
    def __init__(self, region="us", timeout=30):
        if region not in ["eu", "us"]:
            raise ValueError("Region must be either 'us' or 'eu'")
            
        self.region = region
        self.timeout = timeout
        self.session = requests.Session()
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        self.base_api_url = f"https://{region}.qobuz.squid.wtf/api"
        self.download_chunk_size = 256 * 1024
        self.progress_callback = ProgressCallback()

    def set_progress_callback(self, callback):
        self.progress_callback = callback

    def sanitize_filename(self, filename):
        if not filename: 
            return "Unknown Track"
        sanitized = re.sub(r'[\\/*?:"<>|]', "", str(filename))
        return re.sub(r'\s+', ' ', sanitized).strip() or "Unnamed Track"

    def get_track_info(self, isrc, max_retries=3):
        print(f"Fetching: {isrc}")
        search_url = f"{self.base_api_url}/get-music"
        params = {'q': isrc, 'offset': 0, 'limit': 10}
        
        for attempt in range(max_retries):
            try:
                response = self.session.get(search_url, params=params, timeout=self.timeout)
                response.raise_for_status()
                data = response.json()

                selected_track = None
                if data and data.get("success"):
                    items = data.get("data", {}).get("tracks", {}).get("items", [])
                    
                    # Enhanced quality priority system
                    priority = {24: 1, 16: 2, 96: 3}  # 24-bit highest, then 16-bit, then 96kHz
                    
                    for track in items:
                        if track.get("isrc") == isrc:
                            current_prio = priority.get(track.get("maximum_bit_depth"), 4)
                            if selected_track is None or current_prio < priority.get(selected_track.get("maximum_bit_depth"), 4):
                                selected_track = track
                                if current_prio == 1:  # Found 24-bit, stop looking
                                    break
                    
                    # If no ISRC match, try fuzzy matching
                    if not selected_track and items:
                        print(f"No exact ISRC match, checking available tracks...")
                        # Take the highest quality available track
                        best_quality_track = None
                        best_priority = 999
                        
                        for track in items:
                            current_prio = priority.get(track.get("maximum_bit_depth"), 4)
                            if current_prio < best_priority:
                                best_priority = current_prio
                                best_quality_track = track
                        
                        if best_quality_track:
                            selected_track = best_quality_track
                            print(f"Selected best available quality track")
                                    
                if not selected_track:
                    if attempt < max_retries - 1:
                        print(f"Track not found, retrying... ({attempt + 1}/{max_retries})")
                        time.sleep(2)
                        continue
                    else:
                        raise Exception(f"Track not found: {isrc}")
                    
                title = selected_track.get('title', 'Unknown')
                bit_depth = selected_track.get('maximum_bit_depth', 'Unknown')
                sample_rate = selected_track.get('maximum_sampling_rate', 'Unknown')
                print(f"Found: {title} ({bit_depth}b/{sample_rate}kHz)")
                return selected_track
                
            except requests.exceptions.Timeout:
                if attempt < max_retries - 1:
                    print(f"Request timeout, retrying... ({attempt + 1}/{max_retries})")
                    time.sleep(3)
                    continue
                else:
                    raise Exception(f"Request timeout after {max_retries} retries")
                    
            except requests.exceptions.RequestException as e:
                if attempt < max_retries - 1:
                    print(f"Request error, retrying... ({attempt + 1}/{max_retries}): {e}")
                    time.sleep(2)
                    continue
                else:
                    raise Exception(f"Request error after {max_retries} retries: {e}")
                    
            except Exception as e:
                if attempt < max_retries - 1:
                    print(f"General error, retrying... ({attempt + 1}/{max_retries}): {e}")
                    time.sleep(2)
                    continue
                else:
                    raise Exception(f"Error after {max_retries} retries: {e}")

    def get_download_url(self, track_id):
        print("Fetching URL...")
        download_api_url = f"{self.base_api_url}/download-music"
        params = {'track_id': track_id, 'quality': 27}
        
        try:
            response = self.session.get(download_api_url, params=params, timeout=self.timeout)
            response.raise_for_status()
            data = response.json()
            
            if data and data.get("success") and data.get("data", {}).get("url"):
                download_url = data["data"]["url"]
                print("URL found")
                return download_url
            else:
                error_msg = data.get('error', {}).get('message', 'Unknown API error')
                raise Exception(f"API error: {error_msg}")
                
        except requests.exceptions.RequestException as e:
            raise Exception(f"Request error: {e}")
        except Exception as e:
            raise Exception(f"Error: {e}")

    def download(self, isrc, output_dir=".", is_paused_callback=None, is_stopped_callback=None):
        if output_dir != ".":
            try:
                os.makedirs(output_dir, exist_ok=True)
            except OSError as e:
                raise Exception(f"Directory error: {e}")
                
        track_info = self.get_track_info(isrc)
        track_id = track_info.get("id")
        
        if not track_id:
            raise Exception("No track ID found")
            
        artist_name = self.sanitize_filename(track_info.get('performer', {}).get('name'))
        track_title = self.sanitize_filename(track_info.get('title'))
        output_filename = os.path.join(output_dir, f"{artist_name} - {track_title}.flac")
        
        if os.path.exists(output_filename):
            file_size = os.path.getsize(output_filename)
            if file_size > 0:
                print(f"File already exists: {output_filename} ({file_size / (1024 * 1024):.2f} MB)")
                return output_filename
                
        download_url = self.get_download_url(track_id)
        temp_filename = output_filename + ".part"
        
        print(f"Downloading...")
        try:
            response = self.session.get(download_url, timeout=900)
            response.raise_for_status()
            
            if is_stopped_callback and is_stopped_callback():
                raise Exception("Download stopped")
                
            while is_paused_callback and is_paused_callback():
                time.sleep(0.1)
                if is_stopped_callback and is_stopped_callback():
                    raise Exception("Download stopped")
            
            with open(temp_filename, 'wb') as f:
                f.write(response.content)
            
            downloaded_size = len(response.content)
            total_size = downloaded_size
            
            if self.progress_callback:
                self.progress_callback(downloaded_size, total_size)
                        
            os.rename(temp_filename, output_filename)
            print("Download complete")
            
        except requests.exceptions.RequestException as e:
            if os.path.exists(temp_filename): 
                os.remove(temp_filename)
            raise Exception(f"Download failed: {e}")
        except Exception as e:
            if os.path.exists(temp_filename): 
                os.remove(temp_filename)
            raise Exception(f"File error: {e}")
            
        print("Adding metadata...")
        try:
            self._embed_metadata(output_filename, track_info)
            print("Metadata saved")
        except Exception as e:
            print(f"Tagging failed: {e}")
        
        print(f"Done")
        return output_filename

    def _embed_metadata(self, filename, track_info):
        try:
            audio = FLAC(filename)
            audio.delete()
            audio.clear_pictures()

            album_info = track_info.get('album', {})
            artist = track_info.get('performer', {}).get('name')

            if track_info.get('title'): 
                audio['TITLE'] = track_info['title']
            if artist: 
                audio['ARTIST'] = artist
            if album_info.get('title'): 
                audio['ALBUM'] = album_info['title']
            if album_info.get('artist', {}).get('name', artist): 
                audio['ALBUMARTIST'] = album_info.get('artist', {}).get('name', artist)
            if track_info.get('track_number'): 
                audio['TRACKNUMBER'] = str(track_info['track_number'])
            if track_info.get('release_date_original'):
                audio['DATE'] = track_info['release_date_original']
                try: 
                    audio['YEAR'] = str(datetime.strptime(track_info['release_date_original'], '%Y-%m-%d').year)
                except ValueError: 
                    pass
            if album_info.get('genre', {}).get('name'): 
                audio['GENRE'] = album_info['genre']['name']
            if track_info.get('copyright'): 
                audio['COPYRIGHT'] = track_info['copyright']
            if track_info.get('isrc'): 
                audio['ISRC'] = track_info['isrc']
            if album_info.get('label', {}).get('name'): 
                audio['ORGANIZATION'] = album_info['label']['name']

            img_info = album_info.get('image', {})
            cover_url = img_info.get('large') or img_info.get('small') or img_info.get('thumbnail')
            if cover_url:
                try:
                    img_response = self.session.get(cover_url, timeout=30)
                    img_response.raise_for_status()
                    mime_type = img_response.headers.get('Content-Type', 'image/jpeg').lower()
                    if mime_type in ['image/jpeg', 'image/png']:
                        picture = Picture()
                        picture.data = img_response.content
                        picture.type = PictureType.COVER_FRONT
                        picture.mime = mime_type
                        audio.add_picture(picture)
                        print("Cover added")
                except Exception as e:
                    print(f"Cover error: {str(e)}")

            audio.save()

        except Exception as e:
            raise Exception(f"Metadata error: {e}")

def main():
    print("=== QobuzDL - Qobuz Downloader ===")
    downloader = QobuzDownloader(region="us")
    
    isrc = "USAT22409172"
    output_dir = "."
    
    try:
        downloaded_file = downloader.download(isrc, output_dir)
        print(f"Success: File saved as {downloaded_file}")
    except Exception as e:
        print(f"Error: {str(e)}")

if __name__ == "__main__":
    try:
        import sys
        if sys.platform == "win32":
            import os
            os.system("chcp 65001 > nul")
            try:
                sys.stdout.reconfigure(encoding='utf-8')
            except:
                pass
    except:
        pass
        
    main()
