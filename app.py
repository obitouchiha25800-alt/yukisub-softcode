from flask import Flask, render_template, request, send_file, jsonify
import subprocess
import os
import uuid
from pathlib import Path
import shutil
import threading
import re
import time

app = Flask(__name__)

# Configure directories
TEMP_UPLOADS = 'temp_uploads'  # For active job files
TEMP_FONTS = 'temp_fonts'      # For cached fonts (session reuse)

Path(TEMP_UPLOADS).mkdir(exist_ok=True)
Path(TEMP_FONTS).mkdir(exist_ok=True)

# Allowed file extensions
ALLOWED_SUBTITLE_EXTENSIONS = {'ass'}
ALLOWED_FONT_EXTENSIONS = {'ttf', 'otf'}

# Global task tracking dictionary
tasks = {}

# Global storage counter (12-Job Limit)
COMPLETED_JOBS = 0
STORAGE_LIMIT = 12

# Track active FFmpeg processes for cleanup
active_ffmpeg_processes = {}
process_lock = threading.Lock()

# Smart FFmpeg Path Detection
def get_ffmpeg_path():
    """
    Detects FFmpeg path intelligently:
    - Checks if ffmpeg.exe exists in current directory (Windows)
    - Falls back to 'ffmpeg' (Linux/Render)
    """
    local_ffmpeg = os.path.join(os.getcwd(), 'ffmpeg.exe')
    if os.path.exists(local_ffmpeg):
        print(f"✅ Using local FFmpeg: {local_ffmpeg}")
        return local_ffmpeg
    else:
        print("✅ Using system FFmpeg: ffmpeg")
        return 'ffmpeg'

FFMPEG_PATH = get_ffmpeg_path()

def allowed_file(filename, allowed_extensions):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in allowed_extensions

def parse_duration(duration_str):
    """Parse FFmpeg duration string (HH:MM:SS.ms) to seconds"""
    try:
        time_parts = re.match(r'(\d+):(\d+):(\d+\.?\d*)', duration_str)
        if time_parts:
            hours, minutes, seconds = time_parts.groups()
            return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    except:
        pass
    return None

def run_ffmpeg_task(task_id, video_url, subtitle_path, font_path, output_path, custom_filename):
    """
    Run FFmpeg muxing task in background thread
    """
    global COMPLETED_JOBS
    
    try:
        tasks[task_id]['status'] = 'processing'
        tasks[task_id]['progress'] = 0
        
        print(f"\n{'='*60}")
        print(f"🎬 Processing Muxing Job: {task_id}")
        print(f"📹 Video URL: {video_url}")
        print(f"📝 Subtitle: {subtitle_path}")
        print(f"🔤 Font: {font_path}")
        print(f"📦 Output: {output_path}")
        print(f"📄 Custom Filename: {custom_filename}")
        print(f"{'='*60}\n")

        # Construct FFmpeg command with FORCED DEFAULT SUBTITLES
        ffmpeg_command = [
            FFMPEG_PATH,
            '-y',
            '-i', video_url,
            '-i', subtitle_path,
            '-attach', font_path,
            '-metadata:s:t', 'mimetype=application/x-truetype-font',
            '-c', 'copy',
            '-map', '0:v:0',
            '-map', '0:a:0',
            '-map', '1',
            '-disposition:s:0', 'default',  # FORCE SUBTITLES ON BY DEFAULT
            '-progress', 'pipe:1',
            output_path
        ]

        print(f"🚀 Executing FFmpeg Command:")
        print(f"   {' '.join(ffmpeg_command)}\n")

        # Start FFmpeg process and track it
        process = subprocess.Popen(
            ffmpeg_command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            bufsize=1
        )
        
        # Track the active process
        with process_lock:
            active_ffmpeg_processes[task_id] = process

        # Variables for progress tracking
        duration = None
        current_time = None

        # Read stderr for duration and progress
        for line in process.stderr:
            line = line.strip()
            
            # Extract total duration
            if 'Duration:' in line and not duration:
                duration_match = re.search(r'Duration: (\d+:\d+:\d+\.\d+)', line)
                if duration_match:
                    duration = parse_duration(duration_match.group(1))
                    print(f"⏱️ Total Duration: {duration:.2f} seconds")
            
            # Extract current processing time
            if 'time=' in line:
                time_match = re.search(r'time=(\d+:\d+:\d+\.\d+)', line)
                if time_match:
                    current_time = parse_duration(time_match.group(1))
                    
                    # Calculate progress percentage
                    if duration and current_time:
                        progress = min(int((current_time / duration) * 100), 99)
                        tasks[task_id]['progress'] = progress
                        print(f"📊 Progress: {progress}% ({current_time:.1f}s / {duration:.1f}s)")

        # Wait for process to complete
        process.wait()
        
        # Clear active process tracking
        with process_lock:
            if task_id in active_ffmpeg_processes:
                del active_ffmpeg_processes[task_id]

        # Check if FFmpeg succeeded
        if process.returncode != 0:
            stderr_output = process.stderr.read() if process.stderr else "No error output"
            print(f"\n❌ FFmpeg FAILED!")
            print(f"Return Code: {process.returncode}")
            print(f"STDERR:\n{stderr_output}\n")
            
            tasks[task_id]['status'] = 'error'
            tasks[task_id]['error'] = f'FFmpeg failed with return code {process.returncode}'
            
            # Increment counter even on failure
            COMPLETED_JOBS += 1
            print(f"📊 Storage Counter: {COMPLETED_JOBS}/{STORAGE_LIMIT}")
            return

        # Verify output file
        if not os.path.exists(output_path):
            print("❌ ERROR: Output file was not created!")
            tasks[task_id]['status'] = 'error'
            tasks[task_id]['error'] = 'Output file was not created'
            
            # Increment counter even on failure
            COMPLETED_JOBS += 1
            print(f"📊 Storage Counter: {COMPLETED_JOBS}/{STORAGE_LIMIT}")
            return

        # Success!
        output_size = os.path.getsize(output_path)
        print(f"✅ Output file created: {output_size / (1024*1024):.2f} MB\n")
        
        tasks[task_id]['status'] = 'completed'
        tasks[task_id]['progress'] = 100
        tasks[task_id]['filename'] = custom_filename
        
        # Increment counter on success
        COMPLETED_JOBS += 1
        print(f"📊 Storage Counter: {COMPLETED_JOBS}/{STORAGE_LIMIT}")

    except Exception as e:
        print(f"\n💥 UNEXPECTED ERROR: {type(e).__name__}")
        print(f"Error Details: {str(e)}")
        import traceback
        traceback.print_exc()
        
        tasks[task_id]['status'] = 'error'
        tasks[task_id]['error'] = str(e)
        
        # Increment counter even on exception
        COMPLETED_JOBS += 1
        print(f"📊 Storage Counter: {COMPLETED_JOBS}/{STORAGE_L IMIT}")

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/start-mux', methods=['POST'])
def start_mux():
    global COMPLETED_JOBS
    
    try:
        # CRITICAL: Check storage limit FIRST
        if COMPLETED_JOBS >= STORAGE_LIMIT:
            print(f"🚫 STORAGE_FULL: {COMPLETED_JOBS}/{STORAGE_LIMIT} jobs completed")
            return jsonify({
                'error': 'STORAGE_FULL',
                'message': f'Storage limit reached ({COMPLETED_JOBS}/{STORAGE_LIMIT} jobs). Please clear data to continue.'
            }), 403
        
        # Get form data
        video_url = request.form.get('video_url')
        subtitle_file = request.files.get('subtitle_file')
        cached_font_name = request.form.get('cached_font_name', '').strip()
        font_file = request.files.get('font_file')
        custom_filename = request.form.get('output_filename', 'muxed_video').strip()

        # Sanitize filename
        custom_filename = re.sub(r'[<>:"/\\|?*]', '', custom_filename)
        if not custom_filename:
            custom_filename = 'muxed_video'

        # Validate inputs
        if not video_url:
            return jsonify({'error': 'Video URL is required'}), 400
        
        if not subtitle_file or subtitle_file.filename == '':
            return jsonify({'error': 'Subtitle file is required'}), 400
        
        # Validate subtitle extension
        if not allowed_file(subtitle_file.filename, ALLOWED_SUBTITLE_EXTENSIONS):
            return jsonify({'error': 'Subtitle file must be .ass format'}), 400

        # Font handling logic
        font_path = None
        font_name = None
        
        # Option 1: Using cached font
        if cached_font_name:
            cached_font_path = os.path.join(TEMP_FONTS, cached_font_name)
            if os.path.exists(cached_font_path):
                font_path = cached_font_path
                font_name = cached_font_name
                print(f"✅ Using cached font: {cached_font_name}")
            else:
                return jsonify({'error': f'Cached font "{cached_font_name}" not found. Please upload again.'}), 400
        
        # Option 2: New font upload
        elif font_file and font_file.filename != '':
            if not allowed_file(font_file.filename, ALLOWED_FONT_EXTENSIONS):
                return jsonify({'error': 'Font file must be .ttf or .otf format'}), 400
            
            # Generate unique font name to avoid conflicts
            font_ext = font_file.filename.rsplit('.', 1)[1].lower()
            font_name = f"{uuid.uuid4().hex[:8]}_{font_file.filename}"
            font_path = os.path.join(TEMP_FONTS, font_name)
            
            # Save font to cache folder
            font_file.save(font_path)
            print(f"✅ New font uploaded and cached: {font_name}")
        else:
            return jsonify({'error': 'Font file is required (upload new or use cached)'}), 400

        # Create unique task ID
        task_id = str(uuid.uuid4())
        session_folder = os.path.join(TEMP_UPLOADS, task_id)
        os.makedirs(session_folder, exist_ok=True)

        # Save subtitle file
        subtitle_path = os.path.join(session_folder, 'subs.ass')
        output_path = os.path.join(session_folder, 'output.mkv')
        subtitle_file.save(subtitle_path)

        # Initialize task tracking
        tasks[task_id] = {
            'status': 'processing',  # Directly to processing (no queue!)
            'progress': 0,
            'output_path': output_path,
            'session_folder': session_folder,
            'filename': custom_filename
        }

        # START IMMEDIATELY IN BACKGROUND THREAD (NO QUEUE!)
        thread = threading.Thread(
            target=run_ffmpeg_task,
            args=(task_id, video_url, subtitle_path, font_path, output_path, custom_filename),
            daemon=True
        )
        thread.start()

        print(f"✅ Task {task_id} started immediately in background thread")

        return jsonify({
            'task_id': task_id,
            'font_name': font_name,
            'storage_used': COMPLETED_JOBS,
            'storage_limit': STORAGE_LIMIT
        }), 202

    except Exception as e:
        print(f"\n💥 ERROR in /start-mux: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': f'Server error: {str(e)}'}), 500

@app.route('/progress/<task_id>', methods=['GET'])
def get_progress(task_id):
    """
    Returns current progress for a task
    """
    if task_id not in tasks:
        return jsonify({'error': 'Task not found'}), 404
    
    task = tasks[task_id]
    status = task['status']
    anime_name = task.get('filename', 'Unknown')
    
    # If processing, return progress
    if status == 'processing':
        return jsonify({
            'status': 'processing',
            'progress': task.get('progress', 0),
            'anime_name': anime_name
        })
    
    # If completed, return done status
    if status == 'completed':
        return jsonify({
            'status': 'completed',
            'progress': 100,
            'anime_name': anime_name
        })
    
    # If error, return error
    if status == 'error':
        return jsonify({
            'status': 'error',
            'error': task.get('error', 'Unknown error'),
            'anime_name': anime_name
        })
    
    return jsonify({
        'status': status,
        'progress': task.get('progress', 0),
        'anime_name': anime_name
    })

@app.route('/download/<task_id>', methods=['GET'])
def download_file(task_id):
    """
    Downloads the completed file with link expiry check
    """
    if task_id not in tasks:
        return jsonify({'error': 'Link expired or task not found'}), 404
    
    task = tasks[task_id]
    
    if task['status'] != 'completed':
        return jsonify({'error': 'File not ready'}), 400
    
    output_path = task['output_path']
    custom_filename = task.get('filename', 'muxed_video')
    
    # CRITICAL: Check if file actually exists (link expiry protection)
    if not os.path.exists(output_path):
        print(f"⚠️ File does not exist (link expired): {output_path}")
        return jsonify({'error': 'Link expired - file no longer available'}), 404
    
    try:
        response = send_file(
            output_path,
            as_attachment=True,
            download_name=f'{custom_filename}.mkv',
            mimetype='video/x-matroska'
        )

        # Cleanup after download
        @response.call_on_close
        def cleanup():
            try:
                time.sleep(2)
                session_folder = task.get('session_folder')
                if session_folder and os.path.exists(session_folder):
                    shutil.rmtree(session_folder, ignore_errors=True)
                    print(f"🧹 Cleaned up session folder: {task_id}")
                if task_id in tasks:
                    del tasks[task_id]
            except Exception as e:
                print(f"⚠️ Cleanup error: {e}")

        return response

    except Exception as e:
        print(f"💥 Download error: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/clear-data', methods=['POST'])
def clear_data():
    """
    Aggressively clears all temporary data, kills FFmpeg processes, and resets the storage counter
    """
    global COMPLETED_JOBS
    
    try:
        print("\n" + "="*60)
        print("🗑️ AGGRESSIVELY CLEARING ALL DATA...")
        print("="*60)
        
        # CRITICAL: Kill all active FFmpeg processes
        with process_lock:
            for task_id, process in list(active_ffmpeg_processes.items()):
                if process and process.poll() is None:
                    try:
                        print(f"⚔️ Terminating FFmpeg process for task: {task_id}")
                        process.terminate()
                        try:
                            process.wait(timeout=3)
                            print(f"✅ Process {task_id} terminated gracefully")
                        except subprocess.TimeoutExpired:
                            print(f"💀 Force killing process {task_id}...")
                            process.kill()
                            process.wait()
                            print(f"✅ Process {task_id} force killed")
                    except Exception as e:
                        print(f"⚠️ Error killing process {task_id}: {e}")
            active_ffmpeg_processes.clear()
        
        # Reset counter
        old_count = COMPLETED_JOBS
        COMPLETED_JOBS = 0
        print(f"✅ Reset counter: {old_count} → 0")
        
        # CRITICAL: Aggressively delete temp_uploads folder
        if os.path.exists(TEMP_UPLOADS):
            try:
                shutil.rmtree(TEMP_UPLOADS, ignore_errors=False)  # Strict deletion
                print(f"🗑️ DELETED (strict): {TEMP_UPLOADS}")
            except Exception as e:
                print(f"⚠️ Error deleting {TEMP_UPLOADS}: {e}")
                # Force delete even if error
                shutil.rmtree(TEMP_UPLOADS, ignore_errors=True)
        
        # Immediately recreate empty folder
        os.makedirs(TEMP_UPLOADS, exist_ok=True)
        print(f"📁 RECREATED (empty): {TEMP_UPLOADS}")
        
        # CRITICAL: Aggressively delete temp_fonts folder
        if os.path.exists(TEMP_FONTS):
            try:
                shutil.rmtree(TEMP_FONTS, ignore_errors=False)  # Strict deletion
                print(f"🗑️ DELETED (strict): {TEMP_FONTS}")
            except Exception as e:
                print(f"⚠️ Error deleting {TEMP_FONTS}: {e}")
                # Force delete even if error
                shutil.rmtree(TEMP_FONTS, ignore_errors=True)
        
        # Immediately recreate empty folder
        os.makedirs(TEMP_FONTS, exist_ok=True)
        print(f"📁 RECREATED (empty): {TEMP_FONTS}")
        
        # Clear tasks dictionary (invalidates all download links)
        tasks.clear()
        print(f"🗑️ Cleared tasks dictionary (all download links invalidated)")
        
        print("="*60)
        print("✅ ALL DATA AGGRESSIVELY CLEARED!")
        print("="*60 + "\n")
        
        return jsonify({
            'success': True,
            'message': 'All data cleared successfully',
            'storage_used': COMPLETED_JOBS,
            'storage_limit': STORAGE_LIMIT
        }), 200
        
    except Exception as e:
        print(f"\n💥 ERROR in /clear-data: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': f'Failed to clear data: {str(e)}'}), 500

# Flush function to ensure clean slate on startup
def flush_temp_folders():
    """
    Deletes and recreates temp folders to ensure a clean startup
    """
    global COMPLETED_JOBS
    
    print("\n" + "="*60)
    print("🧹 FLUSHING TEMP FOLDERS ON STARTUP...")
    print("="*60)
    
    # Reset global state
    COMPLETED_JOBS = 0
    tasks.clear()
    active_ffmpeg_processes.clear()
    
    # Delete and recreate temp_uploads
    if os.path.exists(TEMP_UPLOADS):
        try:
            shutil.rmtree(TEMP_UPLOADS, ignore_errors=False)
            print(f"🗑️ DELETED: {TEMP_UPLOADS}")
        except Exception as e:
            print(f"⚠️ Error deleting {TEMP_UPLOADS}: {e}")
            shutil.rmtree(TEMP_UPLOADS, ignore_errors=True)
    
    os.makedirs(TEMP_UPLOADS, exist_ok=True)
    print(f"📁 RECREATED: {TEMP_UPLOADS}")
    
    # Delete and recreate temp_fonts
    if os.path.exists(TEMP_FONTS):
        try:
            shutil.rmtree(TEMP_FONTS, ignore_errors=False)
            print(f"🗑️ DELETED: {TEMP_FONTS}")
        except Exception as e:
            print(f"⚠️ Error deleting {TEMP_FONTS}: {e}")
            shutil.rmtree(TEMP_FONTS, ignore_errors=True)
    
    os.makedirs(TEMP_FONTS, exist_ok=True)
    print(f"📁 RECREATED: {TEMP_FONTS}")
    
    print("="*60)
    print("✅ FLUSH COMPLETE - CLEAN SLATE READY!")
    print("="*60 + "\n")

if __name__ == '__main__':
    print("\n" + "="*60)
    print("🚀 AD Web Muxing Server Starting...")
    print(f"📂 Working Directory: {os.getcwd()}")
    print(f"📁 Temp Uploads: {TEMP_UPLOADS}")
    print(f"🔤 Cached Fonts: {TEMP_FONTS}")
    print(f"🎥 FFmpeg Path: {FFMPEG_PATH}")
    print(f"📊 Storage Limit: {STORAGE_LIMIT} jobs")
    print(f"⚡ Queue System: DISABLED (Immediate Threading)")
    print("="*60 + "\n")
    
    # CRITICAL: Flush temp folders on startup for clean slate
    flush_temp_folders()
    
    app.run(debug=True, host='0.0.0.0', port=5000, threaded=True)
