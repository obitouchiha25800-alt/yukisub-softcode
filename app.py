import os
import uuid
import threading
import subprocess
import shutil
import re
import time
from urllib.parse import quote, unquote
from flask import Flask, render_template, request, jsonify, send_from_directory
from datetime import datetime, timedelta
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024  # 500MB max file size

# Global storage
TEMP_UPLOADS = 'temp_uploads'
TEMP_FONTS = 'temp_fonts'
FFMPEG_PATH = 'ffmpeg'  # Adjust if needed

# Job tracking
tasks = {}
COMPLETED_JOBS = 0
MAX_JOBS = 12
LINK_EXPIRY_HOURS = 6

# Lock for thread-safe operations
task_lock = threading.Lock()


def sanitize_filename_allow_spaces(name):
    """
    Custom sanitization that ALLOWS spaces, brackets, hyphens, dots.
    REMOVES dangerous characters like slashes to prevent directory traversal.
    This is used for BOTH saving files and looking them up.
    """
    # Remove directory traversal and dangerous filesystem characters
    # Keep: Alphanumeric, spaces, dots, hyphens, underscores, brackets () []
    name = re.sub(r'[\\/*?:"<>|]', "", name)
    
    # Remove leading/trailing spaces and dots
    name = name.strip().strip('.')
    
    # Collapse multiple spaces into single space
    name = re.sub(r'\s+', ' ', name)
    
    # If empty after sanitization, return default
    if not name:
        return "output"
    
    return name


def cleanup_temp_folders():
    """Delete and recreate temp folders on startup"""
    for folder in [TEMP_UPLOADS, TEMP_FONTS]:
        if os.path.exists(folder):
            shutil.rmtree(folder)
        os.makedirs(folder, exist_ok=True)


def cleanup_expired_links():
    """Background thread to delete expired files every 30 minutes"""
    while True:
        try:
            time.sleep(1800)  # 30 minutes
            with task_lock:
                current_time = datetime.now()
                expired_tasks = []
                
                for task_id, task_data in tasks.items():
                    if task_data['status'] == 'completed':
                        expiry_time = datetime.fromisoformat(task_data['expiry_time'])
                        if current_time >= expiry_time:
                            # Delete file
                            file_path = os.path.join(TEMP_UPLOADS, task_data['safe_filename'])
                            if os.path.exists(file_path):
                                os.remove(file_path)
                            expired_tasks.append(task_id)
                
                # Remove expired tasks
                for task_id in expired_tasks:
                    del tasks[task_id]
                
                if expired_tasks:
                    print(f"[CLEANUP] Removed {len(expired_tasks)} expired file(s)")
        except Exception as e:
            print(f"[CLEANUP ERROR] {e}")


def run_ffmpeg_task(task_id, video_url, sub_path, font_path, output_name):
    """Background thread function to process FFmpeg muxing"""
    global COMPLETED_JOBS
    
    try:
        # Update status to processing
        with task_lock:
            tasks[task_id]['status'] = 'processing'
            tasks[task_id]['progress'] = 0
        
        # Sanitize output filename (ALLOW SPACES)
        safe_output_name = sanitize_filename_allow_spaces(output_name)
        
        # Ensure .mkv extension
        if not safe_output_name.lower().endswith('.mkv'):
            safe_output_name = f"{safe_output_name}.mkv"
        
        output_path = os.path.abspath(os.path.join(TEMP_UPLOADS, safe_output_name))
        
        # Build FFmpeg command - FIXED for HLS subtitle sync issues
        ffmpeg_cmd = [
            FFMPEG_PATH,
            '-y',                          # Overwrite output file
            '-i', video_url,               # Video input (m3u8)
            '-i', sub_path,                # Subtitle input
            '-attach', font_path,          # Attach font
            '-metadata:s:t', 'mimetype=application/x-truetype-font',
            '-c:v', 'copy',                # Copy video stream
            '-c:a', 'copy',                # Copy audio stream
            '-c:s', 'ass',                 # Explicitly set subtitle codec (prevents 10s drop)
            '-map', '0:v:0',               # Map video from first input
            '-map', '0:a:0',               # Map audio from first input
            '-map', '1',                   # Map subtitle from second input
            '-disposition:s:0', 'default', # Force subtitle as default
            '-max_interleave_delta', '0',  # CRITICAL: Prevents subtitle sync loss in HLS
            output_path
        ]
        
        # Execute FFmpeg
        process = subprocess.Popen(
            ffmpeg_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True
        )
        
        # Monitor progress (simple simulation)
        for i in range(0, 101, 10):
            with task_lock:
                if task_id in tasks:
                    tasks[task_id]['progress'] = i
            time.sleep(0.5)
        
        stdout, stderr = process.communicate()
        
        if process.returncode == 0 and os.path.exists(output_path):
            # Success - Calculate expiry time
            expiry_time = datetime.now() + timedelta(hours=LINK_EXPIRY_HOURS)
            
            # CRITICAL: URL-encode the filename for the download link
            url_encoded_filename = quote(safe_output_name)
            
            with task_lock:
                tasks[task_id]['status'] = 'completed'
                tasks[task_id]['progress'] = 100
                tasks[task_id]['download_url'] = f"/download/{task_id}/{url_encoded_filename}"
                tasks[task_id]['safe_filename'] = safe_output_name  # Store unencoded for file lookup
                tasks[task_id]['expiry_time'] = expiry_time.isoformat()
                tasks[task_id]['expiry_display'] = expiry_time.strftime('%Y-%m-%d %H:%M:%S')
                COMPLETED_JOBS += 1
            
            print(f"[SUCCESS] Task {task_id}: {safe_output_name}")
        else:
            # Failure
            with task_lock:
                tasks[task_id]['status'] = 'failed'
                tasks[task_id]['error'] = stderr or 'FFmpeg processing failed'
            print(f"[FAILED] Task {task_id}: {stderr}")
    
    except Exception as e:
        with task_lock:
            tasks[task_id]['status'] = 'failed'
            tasks[task_id]['error'] = str(e)
        print(f"[ERROR] Task {task_id}: {str(e)}")


@app.route('/')
def index():
    """Render main page"""
    return render_template('index.html')


@app.route('/start-mux', methods=['POST'])
def start_mux():
    """Start a new muxing task (direct threading) - WITH ROBUST ERROR HANDLING"""
    global COMPLETED_JOBS
    
    try:
        # FORCE DIRECTORY CREATION (Critical for ephemeral storage like Render)
        if not os.path.exists(TEMP_UPLOADS):
            os.makedirs(TEMP_UPLOADS, exist_ok=True)
            print(f"[INFO] Created missing directory: {TEMP_UPLOADS}")
        
        if not os.path.exists(TEMP_FONTS):
            os.makedirs(TEMP_FONTS, exist_ok=True)
            print(f"[INFO] Created missing directory: {TEMP_FONTS}")
        
        # Check storage limit
        if COMPLETED_JOBS >= MAX_JOBS:
            return jsonify({
                'success': False,
                'error': 'Storage limit reached (12 jobs). Please clear storage first.'
            }), 429
        
        # Get form data
        video_url = request.form.get('video_url', '').strip()
        output_name = request.form.get('output_name', '').strip()
        subtitle_file = request.files.get('subtitle_file')
        font_file = request.files.get('font_file')
        cached_font = request.form.get('cached_font', '').strip()
        
        # Validation
        if not video_url:
            return jsonify({'success': False, 'error': 'Video URL is required'}), 400
        if not subtitle_file:
            return jsonify({'success': False, 'error': 'Subtitle file is required'}), 400
        if not font_file and not cached_font:
            return jsonify({'success': False, 'error': 'Font file is required'}), 400
        if not output_name:
            return jsonify({'success': False, 'error': 'Output name is required'}), 400
        
        # Generate unique task ID
        task_id = str(uuid.uuid4())
        
        # Sanitize subtitle filename
        safe_sub_name = secure_filename(subtitle_file.filename)
        if not safe_sub_name:
            return jsonify({'success': False, 'error': 'Invalid subtitle filename'}), 400
        
        safe_sub_name = f"{task_id}_{safe_sub_name}"
        sub_path = os.path.abspath(os.path.join(TEMP_UPLOADS, safe_sub_name))
        subtitle_file.save(sub_path)
        print(f"[INFO] Saved subtitle: {sub_path}")
        
        # Handle font (new upload or cached)
        if font_file:
            safe_font_name = secure_filename(font_file.filename)
            if not safe_font_name:
                return jsonify({'success': False, 'error': 'Invalid font filename'}), 400
            
            font_path = os.path.abspath(os.path.join(TEMP_FONTS, safe_font_name))
            font_file.save(font_path)
            print(f"[INFO] Saved font: {font_path}")
            font_name_for_cache = safe_font_name
        else:
            # Use cached font
            safe_cached_font = secure_filename(cached_font)
            if not safe_cached_font:
                return jsonify({'success': False, 'error': 'Invalid cached font name'}), 400
            
            font_path = os.path.abspath(os.path.join(TEMP_FONTS, safe_cached_font))
            if not os.path.exists(font_path):
                return jsonify({'success': False, 'error': 'Cached font not found'}), 400
            print(f"[INFO] Using cached font: {font_path}")
            font_name_for_cache = safe_cached_font
        
        # Initialize task
        with task_lock:
            tasks[task_id] = {
                'status': 'queued',
                'progress': 0,
                'output_name': output_name,
                'created_at': datetime.now().isoformat()
            }
        
        # Start background thread immediately
        thread = threading.Thread(
            target=run_ffmpeg_task,
            args=(task_id, video_url, sub_path, font_path, output_name),
            daemon=True
        )
        thread.start()
        print(f"[INFO] Started processing task: {task_id}")
        
        return jsonify({
            'success': True,
            'task_id': task_id,
            'font_name': font_name_for_cache
        })
    
    except Exception as e:
        print(f"[ERROR] /start-mux: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({
            'success': False,
            'error': f'Server error: {str(e)}'
        }), 500


@app.route('/progress/<task_id>')
def progress(task_id):
    """Get task progress"""
    try:
        with task_lock:
            task_data = tasks.get(task_id)
        
        if not task_data:
            return jsonify({'error': 'Task not found'}), 404
        
        # Check if link expired
        if task_data['status'] == 'completed':
            expiry_time = datetime.fromisoformat(task_data['expiry_time'])
            if datetime.now() >= expiry_time:
                return jsonify({'error': 'Link expired', 'status': 'expired'}), 410
        
        return jsonify(task_data)
    
    except Exception as e:
        print(f"[ERROR] /progress/{task_id}: {str(e)}")
        return jsonify({'error': f'Server error: {str(e)}'}), 500


@app.route('/download/<task_id>/<path:filename>')
def download(task_id, filename):
    """
    Serve download file - FIXED for URL-encoded filenames with spaces.
    1. Decode URL-encoded filename (e.g., 'Video%20Ep%2001.mkv' -> 'Video Ep 01.mkv')
    2. Sanitize to match saved filename
    3. Serve file
    """
    try:
        # STEP 1: Decode URL-encoded filename
        decoded_filename = unquote(filename)
        print(f"[DEBUG] Decoded filename: {decoded_filename}")
        
        # STEP 2: Sanitize using same function as worker
        safe_filename = sanitize_filename_allow_spaces(decoded_filename)
        print(f"[DEBUG] Safe filename: {safe_filename}")
        
        if not safe_filename:
            return jsonify({'error': 'Invalid filename'}), 400
        
        with task_lock:
            task_data = tasks.get(task_id)
        
        if not task_data:
            return jsonify({'error': 'Task not found'}), 404
        
        # STEP 3: Verify filename matches task's stored filename
        if safe_filename != task_data.get('safe_filename'):
            print(f"[ERROR] Filename mismatch: {safe_filename} != {task_data.get('safe_filename')}")
            return jsonify({'error': 'Filename mismatch'}), 403
        
        # Check if link expired
        if task_data['status'] == 'completed':
            expiry_time = datetime.fromisoformat(task_data['expiry_time'])
            if datetime.now() >= expiry_time:
                return jsonify({'error': 'Download link has expired'}), 410
        
        # STEP 4: Locate file on disk
        file_path = os.path.join(TEMP_UPLOADS, safe_filename)
        print(f"[DEBUG] Looking for file: {file_path}")
        
        if not os.path.exists(file_path):
            return jsonify({'error': f'File not found: {safe_filename}'}), 404
        
        print(f"[SUCCESS] Serving file: {safe_filename}")
        return send_from_directory(TEMP_UPLOADS, safe_filename, as_attachment=True)
    
    except Exception as e:
        print(f"[ERROR] /download/{task_id}/{filename}: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': f'Server error: {str(e)}'}), 500


@app.route('/clear-data', methods=['POST'])
def clear_data():
    """Clear all storage and reset counter"""
    global COMPLETED_JOBS
    
    try:
        cleanup_temp_folders()
        
        with task_lock:
            tasks.clear()
            COMPLETED_JOBS = 0
        
        print("[INFO] Storage cleared successfully")
        return jsonify({'success': True, 'message': 'Storage cleared successfully'})
    
    except Exception as e:
        print(f"[ERROR] /clear-data: {str(e)}")
        return jsonify({
            'success': False,
            'error': f'Server error: {str(e)}'
        }), 500


# Custom error handlers
@app.errorhandler(413)
def request_entity_too_large(error):
    """Handle file size exceeded error"""
    return jsonify({
        'success': False,
        'error': 'File too large. Maximum size is 500MB.'
    }), 413


@app.errorhandler(500)
def internal_server_error(error):
    """Handle internal server errors"""
    return jsonify({
        'success': False,
        'error': 'Internal server error. Please try again.'
    }), 500


if __name__ == '__main__':
    # Initialize clean environment on startup
    print("=" * 60)
    print("  AD WEB MUXING SERVER - PRODUCTION MODE")
    print("=" * 60)
    cleanup_temp_folders()
    print(f"[OK] Temp folders initialized: {TEMP_UPLOADS}, {TEMP_FONTS}")
    print(f"[OK] Link expiration time: {LINK_EXPIRY_HOURS} hours")
    print(f"[OK] Max jobs: {MAX_JOBS}")
    
    # Start cleanup daemon thread
    cleanup_thread = threading.Thread(target=cleanup_expired_links, daemon=True)
    cleanup_thread.start()
    print("[OK] Auto-cleanup daemon started")
    
    print("=" * 60)
    print("  SERVER READY - Listening on http://0.0.0.0:5000")
    print("=" * 60)
    
    # Run Flask app
    app.run(debug=True, host='0.0.0.0', port=5000)
