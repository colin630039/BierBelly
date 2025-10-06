import sqlite3
import os
import json
import uuid
from flask import Flask, render_template, request, jsonify, session, g
from datetime import datetime, timezone

# --- Application Setup ---
app = Flask(__name__)
# IMPORTANT: In a real application, replace this with a strong, secret key
app.secret_key = 'super_secret_session_key' 

# The SQLite database file will be created in the application's root directory.
# This ensures it's in the same directory as app.py.
DATABASE = os.path.join(os.path.abspath(os.path.dirname(__file__)), 'app.db')

# Predefined drink data (example values)
DRINK_PRESETS = {
    "beer": {"name": "Standard Beer", "abv": 5.0, "volume_oz": 12.0},
    "ipa": {"name": "Imperial Pale Ale", "abv": 7, "volume_oz": 12},
    "wine": {"name": "Standard Wine", "abv": 12.0, "volume_oz": 5.0},
    "shot_spirit": {"name": "Shot (Spirit)", "abv": 40.0, "volume_oz": 1.5},
    # Set to base spirit (40 ABV, 1.5 oz) for alcohol calorie calculation
    "mixed_drink": {"name": "Mixed Drink (Base Spirit)", "abv": 40.0, "volume_oz": 1.5}, 
    # Set to base spirit (40 ABV, 1.5 oz) for alcohol calorie calculation
    "mixed_drink_diet": {"name": "Mixed Drink (Diet Base Spirit)", "abv": 40.0, "volume_oz": 1.5}
}

# Exercise Metabolic Equivalent of Task (MET) values (examples, kcal/kg/hour)
EXERCISE_METS = {
    'walking': 3.5,
    'running': 8.0,
    'biking': 6.0,
    'swimming': 7.0,
    'strength_training': 4.5
}

# --- Database Management Functions ---

def get_db():
    """Connects to the specific database."""
    db = getattr(g, '_database', None)
    if db is None:
        db = g._database = sqlite3.connect(DATABASE)
        db.row_factory = sqlite3.Row # Allows access to columns by name
    return db

@app.teardown_appcontext
def close_connection(exception):
    """Closes the database connection at the end of the request."""
    db = getattr(g, '_database', None)
    if db is not None:
        db.close()

def init_db_script():
    """Initializes the database tables if they do not exist."""
    db = get_db()
    db.execute('PRAGMA foreign_keys = ON;')
    
    # 1. Users table stores credentials and metrics (as JSON)
    db.execute('''
        CREATE TABLE IF NOT EXISTS users (
            email TEXT PRIMARY KEY,
            password TEXT NOT NULL,
            metrics TEXT
        );
    ''')
    
    # 2. Sessions table stores session context. ADDED total_calories.
    db.execute('''
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            user_email TEXT NOT NULL,
            name TEXT NOT NULL,
            date TEXT NOT NULL,
            total_calories INTEGER DEFAULT 0, 
            FOREIGN KEY (user_email) REFERENCES users (email)
        );
    ''')

    # 3. Drinks table stores individual drinks
    db.execute('''
        CREATE TABLE IF NOT EXISTS drinks (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            name TEXT NOT NULL,
            calories INTEGER NOT NULL,
            abv REAL NOT NULL,
            volume_oz REAL NOT NULL,
            count INTEGER NOT NULL,
            FOREIGN KEY (session_id) REFERENCES sessions (id)
        );
    ''')
    
    # 4. Exercise table stores logged exercise for each session (NEW)
    db.execute('''
        CREATE TABLE IF NOT EXISTS exercises (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            type TEXT NOT NULL,
            minutes REAL NOT NULL,
            calories_burned INTEGER NOT NULL,
            FOREIGN KEY (session_id) REFERENCES sessions (id)
        );
    ''')

    db.commit()

# Run initialization at app start
with app.app_context():
    init_db_script()

# --- Utility Functions ---

def get_current_user_email():
    """Retrieves the current user's email from the Flask session."""
    if session.get('logged_in') and session.get('user_email'):
        return session['user_email']
    return None

def calculate_calories(abv, volume_oz):
    """Calculate alcohol calories using a standard formula."""
    volume_L = volume_oz * 0.0295735
    # Formula: L * (ABV/100) * Ethanol Density (789 g/L) * Energy Density (7 kcal/g)
    return round(volume_L * (abv / 100) * 789 * 7)

def calculate_burned_calories(met, weight_kg, minutes):
    """Calculate calories burned using METs."""
    # Formula: (MET * Weight_kg * Time_hours)
    time_hours = minutes / 60
    return round(met * weight_kg * time_hours)

def update_session_calories(db, session_id):
    """Recalculates and updates the total_calories in the sessions table."""
    drinks_cursor = db.execute('SELECT calories, count FROM drinks WHERE session_id = ?', (session_id,))
    
    new_total_calories = 0
    for drink in drinks_cursor.fetchall():
        new_total_calories += (drink['calories'] * drink['count'])
        
    db.execute('UPDATE sessions SET total_calories = ? WHERE id = ?', (new_total_calories, session_id))
    db.commit()
    return new_total_calories
    
# --- Routes ---

@app.route('/')
def index():
    """Main application entry point."""
    return render_template('index.html', drinks=DRINK_PRESETS)


@app.route('/register', methods=['POST'])
def register():
    """Handles new user registration. (No changes)"""
    data = request.get_json()
    email = data.get('email')
    password = data.get('password')

    db = get_db()
    cursor = db.execute('SELECT email FROM users WHERE email = ?', (email,))
    
    if cursor.fetchone():
        return jsonify({"error": "User already exists"}), 409

    try:
        # Insert user data into the database
        db.execute('INSERT INTO users (email, password) VALUES (?, ?)', (email, password))
        db.commit()
        
        # Auto-login and store user in Flask session
        session['logged_in'] = True
        session['user_email'] = email
        session.permanent = True 
        session['current_session_id'] = None
        
        return jsonify({"message": "Registration successful"}), 200
    except Exception as e:
        print(f"Database error during registration: {e}")
        return jsonify({"error": "Registration failed due to server error"}), 500


@app.route('/login', methods=['POST'])
def login():
    """Handles user login. (Minor update to fetching current session)"""
    data = request.get_json()
    email = data.get('email')
    password = data.get('password')
    remember = data.get('remember', False)
    
    db = get_db()
    cursor = db.execute('SELECT email, password FROM users WHERE email = ?', (email,))
    user = cursor.fetchone()
    
    if not user or user['password'] != password:
        return jsonify({"error": "Invalid email or password"}), 401

    # Store user in Flask session
    session['logged_in'] = True
    session['user_email'] = email
    session.permanent = remember 
    
    # Set current session to the latest one upon login
    latest_session_cursor = db.execute('''
        SELECT id FROM sessions 
        WHERE user_email = ? 
        ORDER BY date DESC 
        LIMIT 1
    ''', (email,))
    latest_session = latest_session_cursor.fetchone()
    
    session['current_session_id'] = latest_session['id'] if latest_session else None
    
    return jsonify({"message": "Login successful"}), 200


@app.route('/logout', methods=['POST'])
def logout():
    """Handles user logout. (No changes)"""
    session.pop('logged_in', None)
    session.pop('user_email', None)
    session.pop('current_session_id', None) # Clear current session on logout
    return jsonify({"message": "Logout successful"}), 200


@app.route('/get_user_status')
def get_user_status():
    """Returns the current login status and if metrics are set, and the current session ID. (No changes)"""
    user_email = get_current_user_email()
    
    if user_email:
        db = get_db()
        user_cursor = db.execute('SELECT metrics FROM users WHERE email = ?', (user_email,))
        user = user_cursor.fetchone()
        
        if user:
            username = user_email.split('@')[0]
            metrics_set = user['metrics'] is not None

            current_session_id = session.get('current_session_id')
            
            # If current_session_id is not set in Flask session, find the latest one from DB
            if not current_session_id:
                latest_session_cursor = db.execute('''
                    SELECT id FROM sessions 
                    WHERE user_email = ? 
                    ORDER BY date DESC 
                    LIMIT 1
                ''', (user_email,))
                latest_session = latest_session_cursor.fetchone()
                
                if latest_session:
                    latest_id = latest_session['id']
                    session['current_session_id'] = latest_id
                    current_session_id = latest_id
            
            return jsonify({
                "logged_in": True,
                "username": username,
                "metrics_set": metrics_set,
                "current_session_id": current_session_id 
            }), 200
    
    return jsonify({
        "logged_in": False,
        "username": None,
        "metrics_set": False,
        "current_session_id": None
    }), 200


@app.route('/set_user_metrics', methods=['POST'])
def set_user_metrics():
    """Saves user body metrics. (No changes)"""
    user_email = get_current_user_email()
    if not user_email:
        return jsonify({"error": "Not logged in"}), 401
    
    data = request.get_json()
    
    # Basic validation
    required_fields = ['age', 'height_cm', 'weight_kg', 'sex']
    if not all(field in data for field in required_fields):
        return jsonify({"error": "Missing required metrics data"}), 400

    db = get_db()
    # Store metrics as a JSON string for SQLite
    metrics_json = json.dumps(data) 
    
    try:
        db.execute('UPDATE users SET metrics = ? WHERE email = ?', (metrics_json, user_email))
        db.commit()
        return jsonify({"message": "Metrics saved"}), 200
    except Exception as e:
        print(f"Database error during set_user_metrics: {e}")
        return jsonify({"error": "Failed to save metrics"}), 500

# --- Session & Dashboard Routes ---

@app.route('/get_sessions')
def get_sessions():
    """
    Returns a list of all user sessions, including net calories for each session,
    and the grand total net calories across all sessions. (MODIFIED)
    """
    user_email = get_current_user_email()
    if not user_email:
        return jsonify({"error": "Not logged in"}), 401
    
    db = get_db()
    
    # 1. Fetch all sessions (including total_calories_consumed)
    sessions_cursor = db.execute('''
        SELECT id, name, date, total_calories FROM sessions 
        WHERE user_email = ? 
        ORDER BY date DESC
    ''', (user_email,))
    
    sessions_list = []
    grand_net_calories = 0 # NEW: Initialize the running total
    
    for session_row in sessions_cursor.fetchall():
        session_data = dict(session_row)
        session_id = session_data['id']
        
        # 2. Get total calories burned for this session
        exercises_cursor = db.execute('''
            SELECT SUM(calories_burned) as total_burned FROM exercises 
            WHERE session_id = ?
        ''', (session_id,))
        
        # Use fetchone() and check for None or NULL values
        total_burned_row = exercises_cursor.fetchone()
        # Handle case where SUM is NULL (no exercises)
        total_calories_burned = total_burned_row['total_burned'] if total_burned_row and total_burned_row['total_burned'] is not None else 0
        
        # 3. Calculate Net Calories
        total_consumed = session_data['total_calories']
        net_calories = total_consumed - total_calories_burned
        
        # 4. Update Grand Total (NEW)
        grand_net_calories += net_calories 
        
        # 5. Append calculated values
        session_data['net_calories'] = net_calories
        sessions_list.append(session_data)
        
    # 6. Return both the list and the grand total (MODIFIED return structure)
    return jsonify({
        "sessions": sessions_list,
        "grand_net_calories": grand_net_calories
    }), 200


@app.route('/create_session', methods=['POST'])
def create_session():
    """Creates a new tracking session. (No changes)"""
    user_email = get_current_user_email()
    if not user_email:
        return jsonify({"error": "Not logged in"}), 401
        
    data = request.get_json()
    
    default_name = f"Session - {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}"
    session_name = data.get('name', default_name)
    session_id = str(uuid.uuid4())
    current_time = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')

    db = get_db()
    try:
        db.execute('''
            INSERT INTO sessions (id, user_email, name, date, total_calories) 
            VALUES (?, ?, ?, ?, ?)
        ''', (session_id, user_email, session_name, current_time, 0)) # 0 is the initial total_calories
        db.commit()
        
        session['current_session_id'] = session_id
        
        return jsonify({"message": "Session created", "session_id": session_id}), 200
    except Exception as e:
        print(f"Database error during create_session: {e}")
        return jsonify({"error": "Failed to create session"}), 500


@app.route('/delete_session/<session_id>', methods=['DELETE'])
def delete_session(session_id):
    """Deletes a session, associated drinks, AND associated exercises. (No changes)"""
    user_email = get_current_user_email()
    if not user_email:
        return jsonify({"error": "Not logged in"}), 401
        
    db = get_db()
    
    try:
        # 1. Check session ownership
        session_check = db.execute('SELECT user_email FROM sessions WHERE id = ?', (session_id,)).fetchone()
        if not session_check or session_check['user_email'] != user_email:
            return jsonify({"error": "Session not found or unauthorized"}), 404
        
        # 2. Delete all associated drinks
        db.execute('DELETE FROM drinks WHERE session_id = ?', (session_id,))
        
        # 3. Delete all associated exercises (NEW)
        db.execute('DELETE FROM exercises WHERE session_id = ?', (session_id,))
        
        # 4. Delete the session itself
        db.execute('DELETE FROM sessions WHERE id = ?', (session_id,))
        db.commit()
        
        # 5. If the deleted session was the current one, clear the current_session_id
        if session.get('current_session_id') == session_id:
            session['current_session_id'] = None
        
        return jsonify({"message": "Session and associated data deleted successfully"}), 200
    except Exception as e:
        print(f"Database error during delete_session: {e}")
        return jsonify({"error": "Failed to delete session"}), 500


@app.route('/add_drink/<session_id>', methods=['POST'])
def add_drink(session_id):
    """Adds a drink entry and updates session total_calories. (No changes)"""
    user_email = get_current_user_email()
    if not user_email:
        return jsonify({"error": "Not logged in"}), 401
        
    db = get_db()
    # Verify session ownership
    session_check = db.execute('SELECT id FROM sessions WHERE id = ? AND user_email = ?', (session_id, user_email)).fetchone()
    if not session_check:
        return jsonify({"error": "Session not found or unauthorized"}), 404

    drink_type = request.form.get('drink_type')
    custom_name = request.form.get('custom_name')
    
    if drink_type not in DRINK_PRESETS:
        return jsonify({"error": "Invalid drink type"}), 400

    preset = DRINK_PRESETS[drink_type]
    
    abv = preset.get('abv')
    volume_oz = preset.get('volume_oz')
    count = 1 
    
    is_mixed_drink = drink_type in ['mixed_drink', 'mixed_drink_diet']

    # --- 1. Determine Dynamic Properties (Count/Volume/ABV) ---
    if is_mixed_drink:
        # For mixed drinks, ABV/Volume are fixed to the base spirit preset (40% ABV, 1.5oz)
        count_str = request.form.get('liquid_ounces')
        try:
            # The number of 1.5oz servings (shots)
            count = int(count_str) if count_str and float(count_str) >= 1 else 1
        except ValueError:
            return jsonify({"error": "Invalid number for shots count"}), 400
        
        base_name = preset.get('name').replace('(Base Spirit)', '').strip()
        name_details = f"({count} shots, {abv:.1f}% ABV Base)"
        
    else:
        # Standard drinks (Imperial Pale Ale, Beer, Wine, Shot, or Custom ABV/Vol)
        
        if drink_type != 'shot_spirit':
            try:
                # For Beer/Wine, the form inputs are the actual ABV/Volume
                abv = float(request.form.get('custom_abv'))
                volume_oz = float(request.form.get('liquid_ounces'))
            except (TypeError, ValueError):
                return jsonify({"error": "Missing or Invalid number for ABV or Volume"}), 400
        
        name_details = f"({volume_oz:.1f}oz, {abv:.1f}%)"
        base_name = preset.get('name')


    if abv is None or volume_oz is None:
        return jsonify({"error": "Could not determine drink ABV or Volume"}), 400
        
    # --- 2. Construct Final Name ---
    final_base_name = custom_name or base_name
    final_drink_name = f"{final_base_name.strip()} {name_details}"


    # --- 3. Calorie Calculation ---
    alcohol_calories_per_serving = calculate_calories(abv, volume_oz)

    if drink_type in ['shot_spirit', 'mixed_drink', 'mixed_drink_diet']:
        calories_per_serving = 100
    else:
        calories_per_serving = alcohol_calories_per_serving

    if drink_type == 'mixed_drink':
        calories_per_serving += 50

    if drink_type == 'beer':
        calories_per_serving += 20

    if drink_type == 'wine':
        calories_per_serving += 30

    if drink_type == 'ipa':
        calories_per_serving += 75
    
    # --- 4. Prepare data for insertion ---
    drink_id = str(uuid.uuid4())
    new_drink = {
        'id': drink_id,
        'name': final_drink_name,
        'calories': calories_per_serving,
        'abv': abv,
        'volume_oz': volume_oz,
        'count': count
    }
    
    try:
        db.execute('''
            INSERT INTO drinks (id, session_id, name, calories, abv, volume_oz, count) 
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (drink_id, session_id, new_drink['name'], new_drink['calories'], new_drink['abv'], new_drink['volume_oz'], new_drink['count']))
        
        # UPDATE SESSION CALORIES (NEW)
        update_session_calories(db, session_id)
        
        return jsonify({"message": "Drink added", "drink": new_drink}), 200
    except Exception as e:
        print(f"Database error during add_drink: {e}")
        return jsonify({"error": "Failed to add drink"}), 500


@app.route('/update_drink/<session_id>/<drink_id>', methods=['POST'])
def update_drink(session_id, drink_id):
    """Increments, decrements, or removes a drink count, and updates session total_calories. (No changes)"""
    user_email = get_current_user_email()
    if not user_email:
        return jsonify({"error": "Not logged in"}), 401
        
    db = get_db()
    
    # Check if the drink exists and belongs to the user's session
    cursor = db.execute('''
        SELECT d.count, d.calories FROM drinks d
        JOIN sessions s ON d.session_id = s.id
        WHERE d.id = ? AND s.id = ? AND s.user_email = ?
    ''', (drink_id, session_id, user_email))
    drink_row = cursor.fetchone()

    if not drink_row:
        return jsonify({"error": "Drink ID not found in session or unauthorized"}), 404
            
    try:
        data = request.get_json()
        action = data.get('action')
        current_count = drink_row['count']
        calories = drink_row['calories']
        
        response_data = {"id": drink_id, "calories": calories}

        if action == 'increment':
            new_count = current_count + 1
            db.execute('UPDATE drinks SET count = ? WHERE id = ?', (new_count, drink_id))
            response_data.update({"message": "Drink count incremented", "count": new_count})
        
        elif action == 'decrement':
            new_count = current_count - 1
            
            if new_count <= 0:
                # If count hits 0 or less, delete the drink entry
                db.execute('DELETE FROM drinks WHERE id = ?', (drink_id,))
                response_data = {"message": "Drink removed", "removed_id": drink_id}
            
            else:
                db.execute('UPDATE drinks SET count = ? WHERE id = ?', (new_count, drink_id))
                response_data.update({"message": "Drink count decremented", "count": new_count})
        
        else:
            return jsonify({"error": "Invalid action specified"}), 400
        
        # UPDATE SESSION CALORIES (NEW)
        update_session_calories(db, session_id)

        db.commit()
        return jsonify(response_data), 200

    except Exception as e:
        print(f"Error updating drink: {e}")
        return jsonify({"error": "Internal server error"}), 500

# --- NEW Exercise Routes ---

@app.route('/add_exercise/<session_id>', methods=['POST'])
def add_exercise(session_id):
    """Adds a new exercise to the session. (No changes)"""
    user_email = get_current_user_email()
    if not user_email:
        return jsonify({"error": "Not logged in"}), 401

    db = get_db()
    session_check = db.execute('SELECT user_email FROM sessions WHERE id = ?', (session_id,)).fetchone()
    if not session_check or session_check['user_email'] != user_email:
        return jsonify({"error": "Session not found or unauthorized"}), 404
        
    metrics_cursor = db.execute('SELECT metrics FROM users WHERE email = ?', (user_email,)).fetchone()
    if not metrics_cursor or not metrics_cursor['metrics']:
         return jsonify({"error": "Metrics not set. Please set metrics before logging exercise."}), 400
    
    metrics = json.loads(metrics_cursor['metrics'])
    weight_kg = metrics['weight_kg']

    data = request.get_json()
    exercise_type = data.get('exercise_type')
    minutes_raw = data.get('minutes')

    if exercise_type not in EXERCISE_METS or not minutes_raw:
        return jsonify({"error": "Invalid exercise type or minutes"}), 400
        
    try:
        minutes = float(minutes_raw)
        if minutes <= 0:
            return jsonify({"error": "Minutes must be a positive number"}), 400
            
        met = EXERCISE_METS[exercise_type]
        calories_burned = calculate_burned_calories(met, weight_kg, minutes)
        
        exercise_id = str(uuid.uuid4())
        
        db.execute('''
            INSERT INTO exercises (id, session_id, type, minutes, calories_burned)
            VALUES (?, ?, ?, ?, ?)
        ''', (exercise_id, session_id, exercise_type, minutes, calories_burned))
        db.commit()

        new_exercise = {
            'id': exercise_id,
            'type': exercise_type,
            'minutes': minutes,
            'calories_burned': calories_burned
        }
        
        return jsonify({"message": "Exercise added", "exercise": new_exercise}), 200
    except ValueError:
        return jsonify({"error": "Invalid format for minutes"}), 400
    except Exception as e:
        print(f"Database error during add_exercise: {e}")
        return jsonify({"error": "Failed to add exercise"}), 500


@app.route('/update_exercise/<session_id>/<exercise_id>', methods=['POST'])
def update_exercise(session_id, exercise_id):
    """Increments, decrements, or removes an exercise entry. (No changes)"""
    user_email = get_current_user_email()
    if not user_email:
        return jsonify({"error": "Not logged in"}), 401

    db = get_db()
    
    # 1. Check exercise existence and ownership
    cursor = db.execute('''
        SELECT e.type, e.minutes, e.calories_burned
        FROM exercises e JOIN sessions s ON e.session_id = s.id
        WHERE e.id = ? AND s.id = ? AND s.user_email = ?
    ''', (exercise_id, session_id, user_email))
    exercise_row = cursor.fetchone()

    if not exercise_row:
        return jsonify({"error": "Exercise not found in session or unauthorized"}), 404
    
    # 2. Get user metrics for recalculation
    metrics_cursor = db.execute('SELECT metrics FROM users WHERE email = ?', (user_email,)).fetchone()
    metrics = json.loads(metrics_cursor['metrics'])
    weight_kg = metrics['weight_kg']
    
    data = request.get_json()
    action = data.get('action')
    current_minutes = exercise_row['minutes']
    exercise_type = exercise_row['type']
    met = EXERCISE_METS[exercise_type]
    
    minute_change = 10 # Standard step change in minutes

    try:
        if action == 'increment':
            new_minutes = current_minutes + minute_change
        elif action == 'decrement':
            new_minutes = current_minutes - minute_change
        else:
            return jsonify({"error": "Invalid action specified"}), 400
            
        new_calories_burned = 0
        response_data = {"id": exercise_id}
            
        if new_minutes <= 0:
            # If minutes hit 0 or less, delete the exercise entry
            db.execute('DELETE FROM exercises WHERE id = ?', (exercise_id,))
            db.commit()
            response_data = {"message": "Exercise removed", "removed_id": exercise_id}
            
        else:
            new_calories_burned = calculate_burned_calories(met, weight_kg, new_minutes)
            db.execute('''
                UPDATE exercises SET minutes = ?, calories_burned = ? WHERE id = ?
            ''', (new_minutes, new_calories_burned, exercise_id))
            db.commit()
            response_data.update({
                "message": "Exercise updated", 
                "minutes": new_minutes, 
                "calories_burned": new_calories_burned
            })
            
        return jsonify(response_data), 200

    except Exception as e:
        print(f"Error updating exercise: {e}")
        return jsonify({"error": "Internal server error"}), 500


@app.route('/get_dashboard_data/<session_id>')
def get_dashboard_data(session_id):
    """Calculates and returns all data for the dashboard view for a specific session. (No changes)"""
    user_email = get_current_user_email()
    if not user_email:
        return jsonify({"error": "Not logged in"}), 401
    
    db = get_db()
    
    # 1. Get Session Data and Verify Ownership
    session_cursor = db.execute('''
        SELECT name, total_calories FROM sessions WHERE id = ? AND user_email = ?
    ''', (session_id, user_email))
    session_row = session_cursor.fetchone()
    
    if not session_row:
        return jsonify({"error": "Session not found or unauthorized"}), 404
        
    session_name = session_row['name']
    total_calories_consumed = session_row['total_calories'] # Get pre-calculated total
    
    # 2. Get User Metrics
    metrics_cursor = db.execute('SELECT metrics FROM users WHERE email = ?', (user_email,))
    metrics_row = metrics_cursor.fetchone()
    
    if not metrics_row or not metrics_row['metrics']:
         return jsonify({"error": "Metrics not set"}), 400 
         
    metrics = json.loads(metrics_row['metrics'])
    
    # 3. Get all Drinks for the Session
    drinks_cursor = db.execute('''
        SELECT id, name, calories, abv, volume_oz, count FROM drinks 
        WHERE session_id = ? 
        ORDER BY id 
    ''', (session_id,))
    drinks_list = [dict(row) for row in drinks_cursor.fetchall()]
    
    # 4. Get all Logged Exercise (NEW)
    exercises_cursor = db.execute('''
        SELECT id, type, minutes, calories_burned FROM exercises 
        WHERE session_id = ? 
        ORDER BY type 
    ''', (session_id,))
    logged_exercises = [dict(row) for row in exercises_cursor.fetchall()]
    
    total_calories_burned = sum(e['calories_burned'] for e in logged_exercises)
    net_calories = total_calories_consumed - total_calories_burned
    
    # 5. Calculate Calorie Burn Equivalents (for the remaining net calories)
    exercise_times = {}
    
    remaining_calories_to_burn = max(0, net_calories) # Only calculate for positive net calories
    
    for exercise, met in EXERCISE_METS.items():
        if remaining_calories_to_burn > 0:
            # Formula: (Calories * 60 min/hr) / (MET * Weight_kg)
            minutes = (remaining_calories_to_burn * 60) / (met * metrics['weight_kg'])
            exercise_times[exercise] = round(minutes)
        else:
            exercise_times[exercise] = 0

    return jsonify({
        "session_name": session_name,
        "total_calories_consumed": total_calories_consumed, # New field
        "total_calories_burned": total_calories_burned,     # New field
        "net_calories": net_calories,                       # New field
        "drinks": drinks_list,
        "logged_exercises": logged_exercises,               # New field
        "exercise_times": exercise_times # These are now based on NET calories
    }), 200


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0')