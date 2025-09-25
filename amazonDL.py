import requests
import time
import os
import re
import base64
import urllib3
from urllib.parse import unquote

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

def extract_data(html, patterns):
    for pattern in patterns:
        if match := re.search(pattern, html):
            return match.group(1)
    return None

def download_track(track_id, service="amazon", output_dir=".", progress_callback=None, is_paused_callback=None, is_stopped_callback=None):
    client = requests.Session()
    client.verify = False
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
    max_retries = 3
    
    for attempt in range(max_retries):
        try:
            if is_stopped_callback and is_stopped_callback():
                raise Exception("Download stopped by user")
                
            spotify_url = f"https://open.spotify.com/track/{track_id}"
            params = {"url": spotify_url, "country": "auto", "to": service}
            
            if progress_callback:
                progress_callback(0, 100, "Initializing Amazon Music download...")
            
            response = client.get("https://lucida.to", params=params, headers=headers, timeout=30)
            html = response.text
            
            token = extract_data(html, [r'token:"([^"]+)"', r'"token"\s*:\s*"([^"]+)"'])
            url = extract_data(html, [r'"url":"([^"]+)"', r'url:"([^"]+)"'])
            expiry = extract_data(html, [r'tokenExpiry:(\d+)', r'"tokenExpiry"\s*:\s*(\d+)'])
            
            if not (token and url):
                if attempt < max_retries - 1:
                    print(f"Could not extract required data, attempt {attempt + 1}/{max_retries}")
                    time.sleep(2)
                    continue
                else:
                    raise Exception("Could not extract required data after all retries")
            
            try:
                decoded_token = base64.b64decode(base64.b64decode(token).decode('latin1')).decode('latin1')
            except:
                decoded_token = token
            
            clean_url = url.replace('\\/', '/')
            print(f"Starting download for: {clean_url}")
            
            if progress_callback:
                progress_callback(10, 100, "Requesting download...")
            
            request_data = {
                "account": {"id": "auto", "type": "country"},
                "compat": "false", "downscale": "original", "handoff": True,
                "metadata": True, "private": True,
                "token": {"primary": decoded_token, "expiry": int(expiry) if expiry else None},
                "upload": {"enabled": False, "service": "pixeldrain"},
                "url": clean_url
            }

            response = client.post("https://lucida.to/api/load?url=/api/fetch/stream/v2", 
                                  json=request_data, headers=headers, timeout=45)
            
            if csrf_token := response.cookies.get('csrf_token'):
                headers['X-CSRF-Token'] = csrf_token

            data = response.json()
            if not data.get("success"):
                if attempt < max_retries - 1:
                    print(f"Request failed: {data.get('error', 'Unknown error')}, retrying...")
                    time.sleep(3)
                    continue
                else:
                    raise Exception(f"Request failed: {data.get('error', 'Unknown error')}")

            completion_url = f"https://{data['server']}.lucida.to/api/fetch/request/{data['handoff']}"
            print("Processing track...")
            
            if progress_callback:
                progress_callback(20, 100, "Processing track...")
            
            processing_timeout = 120  # 2 minutes max
            start_time = time.time()
            
            while True:
                if is_stopped_callback and is_stopped_callback():
                    raise Exception("Download stopped by user")
                    
                while is_paused_callback and is_paused_callback():
                    time.sleep(0.5)
                    if is_stopped_callback and is_stopped_callback():
                        raise Exception("Download stopped by user")
                
                if time.time() - start_time > processing_timeout:
                    raise Exception("Processing timeout exceeded")
                    
                resp = client.get(completion_url, headers=headers, timeout=10).json()
                if resp["status"] == "completed":
                    print("Processing completed!")
                    if progress_callback:
                        progress_callback(80, 100, "Processing completed!")
                    break
                elif resp["status"] == "error":
                    raise Exception(f"Processing failed: {resp.get('message', 'Unknown error')}")
                elif progress := resp.get("progress"):
                    current = progress.get("current", 0)
                    total = progress.get("total", 100)
                    percent = int((current / total) * 60) + 20  # 20-80% range for processing
                    print(f"Progress: {percent}%")
                    if progress_callback:
                        progress_callback(percent, 100, f"Processing: {percent}%")
                time.sleep(2)

            download_url = f"https://{data['server']}.lucida.to/api/fetch/request/{data['handoff']}/download"
            
            if progress_callback:
                progress_callback(90, 100, "Starting file download...")
            
            response = client.get(download_url, stream=True, headers=headers, timeout=60)
            response.raise_for_status()
            
            file_name = "track.flac"
            if content_disp := response.headers.get('content-disposition'):
                if match := re.search(r'filename[*]?=([^;]+)', content_disp):
                    raw_name = match.group(1).strip('"\'')
                    file_name = unquote(raw_name[7:] if raw_name.startswith("UTF-8''") else raw_name)
                    # Sanitize filename
                    for char in '<>:"/\\|?*':
                        file_name = file_name.replace(char, '_')
                    file_name = file_name.strip()
                    if not file_name or file_name == '_':
                        file_name = f"amazon_track_{track_id}.flac"
            
            file_path = os.path.join(output_dir, file_name)
            print(f"Downloading: {file_name}")
            
            # Ensure output directory exists
            os.makedirs(output_dir, exist_ok=True)
            
            total_size = int(response.headers.get('content-length', 0))
            downloaded_size = 0
            
            with open(file_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=32768):  # Larger chunk size for better performance
                    if is_stopped_callback and is_stopped_callback():
                        f.close()
                        if os.path.exists(file_path):
                            os.remove(file_path)
                        raise Exception("Download stopped by user")
                        
                    while is_paused_callback and is_paused_callback():
                        time.sleep(0.1)
                        if is_stopped_callback and is_stopped_callback():
                            f.close()
                            if os.path.exists(file_path):
                                os.remove(file_path)
                            raise Exception("Download stopped by user")
                    
                    if chunk:
                        f.write(chunk)
                        downloaded_size += len(chunk)
                        
                        if total_size > 0:
                            percent = 90 + int((downloaded_size / total_size) * 10)  # 90-100% range for file download
                            if progress_callback:
                                progress_callback(percent, 100, f"Downloading: {downloaded_size / (1024*1024):.1f}MB")
            
            print(f"Download completed: {file_path}")
            if progress_callback:
                progress_callback(100, 100, f"Download completed: {file_name}")
            return file_path
            
        except requests.exceptions.Timeout as e:
            if attempt < max_retries - 1:
                print(f"Timeout error (attempt {attempt + 1}/{max_retries}): {str(e)}")
                time.sleep(5)
                continue
            else:
                print(f"Timeout error after {max_retries} attempts: {str(e)}")
                return None
                
        except requests.exceptions.ConnectionError as e:
            if attempt < max_retries - 1:
                print(f"Connection error (attempt {attempt + 1}/{max_retries}): {str(e)}")
                time.sleep(5)
                continue
            else:
                print(f"Connection error after {max_retries} attempts: {str(e)}")
                return None
                
        except Exception as e:
            if "stopped by user" in str(e):
                print(f"Download stopped by user")
                return None
            elif attempt < max_retries - 1:
                print(f"Error (attempt {attempt + 1}/{max_retries}): {str(e)}")
                time.sleep(3)
                continue
            else:
                print(f"Error after {max_retries} attempts: {str(e)}")
                return None
    
    return None

class LucidaDownloader:
    def __init__(self):
        self.progress_callback = None
    
    def set_progress_callback(self, callback):
        self.progress_callback = callback
    
    def download(self, track_id, output_dir, is_paused_callback=None, is_stopped_callback=None):
        try:
            def enhanced_progress_callback(current, total, message=""):
                if self.progress_callback:
                    self.progress_callback(current, total)
                    
            return download_track(
                track_id, 
                service="amazon", 
                output_dir=output_dir,
                progress_callback=enhanced_progress_callback,
                is_paused_callback=is_paused_callback,
                is_stopped_callback=is_stopped_callback
            )
        except Exception as e:
            raise Exception(f"Amazon Music download failed: {str(e)}")

if __name__ == "__main__":
    track_id = "2plbrEY59IikOBgBGLjaoe"
    service = "amazon"
    
    download_track(track_id, service)
