import os
import uuid
import threading
import subprocess
import shutil
from flask import Flask, render_template, request, jsonify, send_from_directory
from datetime import datetime, timedelta
from werkzeug.utils import secure_filename
import time

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024  # 500MB max file size

# Global storage
TEMP_UPLOADS = 'temp_uploads'
TEMP_FONTS = 'temp_fonts'
FFMPEG_PATH = 'ffmpeg'  # Adjust if needed (e.g., 'C:\\ffmpeg\\bin\\ffmpeg.exe')

# Job tracking
tasks = {}
COMPLETED_JOBS = 0
MAX_JOBS = 12
LINK_EXPIRY_HOURS = 6

# Lock for thread-safe operations
task_lock = threading.Lock()


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
                            file_path = os.path.join(TEMP_UPLOADS, f"{task_data['output_name']}.mkv")
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
        
        output_path = os.path.join(TEMP_UPLOADS, f"{output_name}.mkv")
        
        # Build FFmpeg command
        ffmpeg_cmd = [
            FFMPEG_PATH,
            '-i', video_url,              # Video input (m3u8)
            '-i', sub_path,                # Subtitle input
            '-map', '0:v:0',               # Map best video
            '-map', '0:a:0',               # Map best audio
            '-map', '1:s:0',               # Map subtitle
            '-c', 'copy',                  # Stream copy (no re-encoding)
            '-disposition:s:0', 'default', # Force subtitle as default
            '-attach', font_path,          # Attach font
            '-metadata:s:t:0', f'mimetype=font/{os.path.splitext(font_path)[1][1:]}',
            output_path
        ]
        
        # Execute FFmpeg
        process = subprocess.Popen(
            ffmpeg_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True
        )
        
        # Monitor progress (simple simulation - FFmpeg doesn't provide real-time %)
        for i in range(0, 101, 10):
            with task_lock:
                if task_id in tasks:
                    tasks[task_id]['progress'] = i
            time.sleep(0.5)  # Simulate progress
        
        stdout, stderr = process.communicate()
        
        if process.returncode == 0 and os.path.exists(output_path):
            # Success - Calculate expiry time
            expiry_time = datetime.now() + timedelta(hours=LINK_EXPIRY_HOURS)
            
            with task_lock:
                tasks[task_id]['status'] = 'completed'
                tasks[task_id]['progress'] = 100
                tasks[task_id]['download_url'] = f"/download/{task_id}/{output_name}.mkv"
                tasks[task_id]['expiry_time'] = expiry_time.isoformat()
                tasks[task_id]['expiry_display'] = expiry_time.strftime('%Y-%m-%d %H:%M:%S')
                COMPLETED_JOBS += 1
        else:
            # Failure
            with task_lock:
                tasks[task_id]['status'] = 'failed'
                tasks[task_id]['error'] = stderr or 'FFmpeg processing failed'
    
    except Exception as e:
        with task_lock:
            tasks[task_id]['status'] = 'failed'
            tasks[task_id]['error'] = str(e)


@app.route('/')
def index():
    """Render main page"""
    return render_template('index.html')


@app.route('/start-mux', methods=['POST'])
def start_mux():
    """Start a new muxing task (direct threading)"""
    global COMPLETED_JOBS
    
    # Check storage limit
    if COMPLETED_JOBS >= MAX_JOBS:
        return jsonify({
            'success': False,
            'error': 'STORAGE_FULL',
            'message': 'Storage limit reached (12 jobs). Please clear storage first.'
        }), 429
    
    try:
        # Get form data
        video_url = request.form.get('video_url', '').strip()
        output_name = request.form.get('output_name', '').strip()
        subtitle_file = request.files.get('subtitle_file')
        font_file = request.files.get('font_file')
        cached_font = request.form.get('cached_font', '').strip()
        
        # Validation (backend safeguard)
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
        
        # Save subtitle file
        sub_filename = secure_filename(subtitle_file.filename)
        sub_path = os.path.join(TEMP_UPLOADS, f"{task_id}_{sub_filename}")
        subtitle_file.save(sub_path)
        
        # Handle font (new upload or cached)
        if font_file:
            font_filename = secure_filename(font_file.filename)
            font_path = os.path.join(TEMP_FONTS, font_filename)
            font_file.save(font_path)
        else:
            # Use cached font
            font_path = os.path.join(TEMP_FONTS, cached_font)
            if not os.path.exists(font_path):
                return jsonify({'success': False, 'error': 'Cached font not found'}), 400
        
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
        
        return jsonify({
            'success': True,
            'task_id': task_id,
            'font_name': os.path.basename(font_path) if font_file else cached_font
        })
    
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/progress/<task_id>')
def progress(task_id):
    """Get task progress"""
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


@app.route('/download/<task_id>/<filename>')
def download(task_id, filename):
    """Serve download file"""
    with task_lock:
        task_data = tasks.get(task_id)
    
    if not task_data:
        return jsonify({'error': 'Task not found'}), 404
    
    # Check if link expired
    if task_data['status'] == 'completed':
        expiry_time = datetime.fromisoformat(task_data['expiry_time'])
        if datetime.now() >= expiry_time:
            return jsonify({'error': 'Download link has expired'}), 410
    
    file_path = os.path.join(TEMP_UPLOADS, filename)
    
    if not os.path.exists(file_path):
        return jsonify({'error': 'File not found'}), 404
    
    return send_from_directory(TEMP_UPLOADS, filename, as_attachment=True)


@app.route('/clear-data', methods=['POST'])
def clear_data():
    """Clear all storage and reset counter"""
    global COMPLETED_JOBS
    
    try:
        # Clear temp folders
        cleanup_temp_folders()
        
        # Reset tasks and counter
        with task_lock:
            tasks.clear()
            COMPLETED_JOBS = 0
        
        return jsonify({'success': True, 'message': 'Storage cleared successfully'})
    
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


if __name__ == '__main__':
    # Initialize clean environment on startup
    print("=" * 60)
    print("  AD WEB MUXING SERVER - INITIALIZING")
    print("=" * 60)
    cleanup_temp_folders()
    print(f"[OK] Temp folders initialized: {TEMP_UPLOADS}, {TEMP_FONTS}")
    print(f"[OK] Link expiration time: {LINK_EXPIRY_HOURS} hours")
    
    # Start cleanup daemon thread
    cleanup_thread = threading.Thread(target=cleanup_expired_links, daemon=True)
    cleanup_thread.start()
    print("[OK] Auto-cleanup daemon started")
    
    print("=" * 60)
    print("  SERVER READY - Listening on http://0.0.0.0:5000")
    print("=" * 60)
    
    # Run Flask app
    app.run(debug=True, host='0.0.0.0', port=5000)
