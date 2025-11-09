from flask import Flask, render_template, request, redirect, url_for, session, flash, send_file
import requests
import firebase_admin
from firebase_admin import credentials, firestore, auth
from datetime import datetime
import csv
import io
import os
import tempfile
import json
from datetime import datetime, timedelta

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY")
firebase_api_key = os.getenv("FIREBASE_API_KEY")
firebase_key_json = os.environ.get("FIREBASE_API_KEY_DICT")

SIGNUP_URL = f"https://identitytoolkit.googleapis.com/v1/accounts:signUp?key={firebase_api_key}"
LOGIN_URL = f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key={firebase_api_key}"
SEND_VERIFICATION_URL = f"https://identitytoolkit.googleapis.com/v1/accounts:sendOobCode?key={firebase_api_key}"
ACCOUNT_INFO_URL = f"https://identitytoolkit.googleapis.com/v1/accounts:lookup?key={firebase_api_key}"

# ===== Initialize Firebase =====
if not firebase_admin._apps:
    cred = credentials.Certificate(firebase_key_json)
    firebase_cred_dict = json.loads(firebase_key_json)
    firebase_admin.initialize_app(firebase_cred_dict)
db = firestore.client()

def index():
    firebase_config = {
        "apiKey": os.getenv("FIREBASE_API_KEY_1"),
        "authDomain": os.getenv("FIREBASE_AUTH_DOMAIN"),
        "projectId": os.getenv("FIREBASE_PROJECT_ID"),
        "storageBucket": os.getenv("FIREBASE_STORAGE_BUCKET"),
        "messagingSenderId": os.getenv("FIREBASE_MESSAGING_SENDER_ID"),
        "appId": os.getenv("FIREBASE_APP_ID")
    }
    return render_template('index.html', firebase_config=firebase_config)

# ===== Auth helpers =====
def signup_user(name, email, password):
    payload = {"email": email, "password": password, "returnSecureToken": True}
    res = requests.post(SIGNUP_URL, json=payload)
    if res.status_code == 200:
        user_data = res.json()
        requests.post(SEND_VERIFICATION_URL, json={"requestType": "VERIFY_EMAIL", "idToken": user_data["idToken"]})
        try: auth.update_user(user_data["localId"], display_name=name)
        except: pass
        db.collection("users").document(user_data["localId"]).set({"name": name, "email": email})
        return True, "Signup successful. Verify your email before logging in."
    return False, res.json().get("error", {}).get("message", "Signup failed")

def login_user(email, password):
    payload = {"email": email, "password": password, "returnSecureToken": True}
    res = requests.post(LOGIN_URL, json=payload)
    if res.status_code == 200:
        user_data = res.json()
        info_res = requests.post(ACCOUNT_INFO_URL, json={"idToken": user_data["idToken"]})
        info = info_res.json()
        if info.get("users", [{}])[0].get("emailVerified", False):
            uid = user_data["localId"]
            user_doc = db.collection("users").document(uid).get()
            session["logged_in"], session["uid"], session["name"] = True, uid, user_doc.to_dict().get("name", "User")
            return True, "Login successful"
        return False, "Please verify your email first."
    return False, res.json().get("error", {}).get("message", "Login failed")

# ===== Auth routes =====
@app.route("/")
def home(): return redirect(url_for("dashboard") if session.get("logged_in") else url_for("login"))

@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method=="POST":
        name,email,password = request.form["name"],request.form["email"],request.form["password"]
        success,msg=signup_user(name,email,password); flash(msg)
        if success: return redirect(url_for("login"))
    return render_template("signup.html")

@app.route("/login", methods=["GET","POST"])
def login():
    if request.method=="POST":
        email,password = request.form["email"],request.form["password"]
        success,msg = login_user(email,password); flash(msg)
        if success: return redirect(url_for("dashboard"))
    return render_template("login.html")

@app.route("/logout")
def logout(): session.clear(); return redirect(url_for("login"))

# ===== Dashboard / Teams =====
@app.route("/dashboard")
def dashboard():
    if not session.get("logged_in"): return redirect(url_for("login"))
    uid=session["uid"]

    # Owned Teams
    teams_ref = db.collection("teams").where("owner","==",uid).stream()
    teams=[{"id":t.id,"team_name":t.to_dict()["team_name"]} for t in teams_ref]

    # Teams shared with you
    shared_ref = db.collection("users").document(uid).collection("shared_teams").stream()
    shared_teams=[]
    for t in shared_ref:
        data=t.to_dict()
        owner_doc=db.collection("users").document(data["owner"]).get()
        shared_teams.append({
            "id":t.id,
            "team_name":data["team_name"],
            "access":data["access"],
            "owner_display": owner_doc.to_dict().get("name","(unknown)")
        })
    return render_template("dashboard.html", teams=teams, shared_teams=shared_teams)

@app.route("/create_team", methods=["POST"])
def create_team():
    if not session.get("logged_in"): return redirect(url_for("login"))
    name = request.form.get("team_name")
    if name:
        doc = db.collection("teams").document()
        doc.set({"team_name": name, "owner": session["uid"]})
        flash(f"Team '{name}' created!")
    return redirect(url_for("dashboard"))

@app.route("/delete_team/<team_id>", methods=["POST"])
def delete_team(team_id):
    if not session.get("logged_in"): return redirect(url_for("login"))
    db.collection("teams").document(team_id).delete()
    flash("Team deleted!")
    return redirect(url_for("dashboard"))

# ===== Team Projects =====
@app.route("/team/<team_id>")
def team_projects(team_id):
    if not session.get("logged_in"): return redirect(url_for("login"))
    team_doc=db.collection("teams").document(team_id).get()
    if not team_doc.exists: flash("Team not found"); return redirect(url_for("dashboard"))
    team_name = team_doc.to_dict()["team_name"]
    is_owner = team_doc.to_dict()["owner"]==session["uid"]
    projects_ref=db.collection("teams").document(team_id).collection("projects").stream()
    projects=[{"id":p.id,"project_name":p.to_dict()["project_name"]} for p in projects_ref]
    return render_template("team_projects.html", team_name=team_name, team_id=team_id, projects=projects, is_owner=is_owner)

@app.route("/team/<team_id>/create_project", methods=["POST"])
def create_project(team_id):
    if not session.get("logged_in"): return redirect(url_for("login"))
    project_name = request.form.get("project_name")
    if project_name:
        db.collection("teams").document(team_id).collection("projects").document().set({
            "project_name": project_name,
            "rows": [{"col1":"","col2":"","col3":"","col4":"","col5":""}],
            "comments": []
        })
        flash(f"Project '{project_name}' created!")
    return redirect(url_for("team_projects", team_id=team_id))

@app.route("/team/<team_id>/delete_project/<project_id>", methods=["POST"])
def delete_project(team_id, project_id):
    if not session.get("logged_in"): return redirect(url_for("login"))
    db.collection("teams").document(team_id).collection("projects").document(project_id).delete()
    flash("Project deleted!")
    return redirect(url_for("team_projects", team_id=team_id))

# ===== View/Edit Project =====
@app.route("/team/<team_id>/project/<project_id>", methods=["GET","POST"])
def project_view(team_id, project_id):
    if not session.get("logged_in"): return redirect(url_for("login"))
    uid = session["uid"]

    team_doc = db.collection("teams").document(team_id).get()
    if not team_doc.exists: flash("Team not found"); return redirect(url_for("dashboard"))

    # owner and shared_with map
    team_data = team_doc.to_dict()
    owner_uid = team_data.get("owner")
    shared_with = team_data.get("shared_with", {})  # {uid: access}

    # determine access for current user
    if uid == owner_uid:
        access_level = "owner"
    else:
        access_level = shared_with.get(uid, "view")

    # boolean flags for template
    is_owner = (uid == owner_uid)
    can_edit = is_owner or access_level == "edit"
    can_comment = is_owner or access_level in ("edit", "comment")

    project_ref = db.collection("teams").document(team_id).collection("projects").document(project_id)
    project_doc = project_ref.get()
    if not project_doc.exists:
        flash("Project not found")
        return redirect(url_for("team_projects", team_id=team_id))
    project_data = project_doc.to_dict()
    rows = project_data.get("rows", [{"col1":"","col2":"","col3":"","col4":"","col5":""}])
    comments = project_data.get("comments", [])

    # Build team members list (owner + shared users) for @mentions
    members = []
    try:
        owner_doc = db.collection("users").document(owner_uid).get()
        if owner_doc.exists:
            members.append({"uid": owner_uid, "name": owner_doc.to_dict().get("name", "(unknown)")})
    except Exception:
        pass
    for ruid, acc in shared_with.items():
        try:
            udoc = db.collection("users").document(ruid).get()
            if udoc.exists:
                members.append({"uid": ruid, "name": udoc.to_dict().get("name", "(unknown)")})
        except Exception:
            pass

    # Handle save (POST)
    if request.method == "POST" and can_edit:
        # read dynamic column count from form (col_count added in template)
        try:
            col_count = int(request.form.get("col_count", len(rows[0]) if rows else 5))
            if col_count < 1: col_count = 1
        except:
            col_count = len(rows[0]) if rows else 5

        # Build new rows by iterating over row index until no more row{i}col0 present
        new_rows = []
        i = 0
        # We'll detect row presence by checking for any input matching row{i}col0 or row{i}col0 in form keys or new{i}...
        while True:
            # check presence of any field for this row
            found_field = False
            row_dict = {}
            for j in range(col_count):
                key_names = [f"row{i}col{j}", f"new{i}col{j}", f"row{i}col{j}"]
                val = ""
                for k in key_names:
                    if k in request.form:
                        val = request.form.get(k, "")
                        found_field = True
                        break
                # store as col1..coln
                row_dict[f"col{j+1}"] = val
            if not found_field:
                break
            new_rows.append(row_dict)
            i += 1

        project_ref.update({"rows": new_rows})
        flash("Project saved!")
        rows = new_rows

    # Prepare headers to send (we keep basic Column N headers by default)
    # Note: header renames are client-side only unless saved server-side; for now we expose a default header count to template
    default_header_count = len(rows[0]) if rows else 5
    headers = [f"Column {i+1}" for i in range(default_header_count)]

    return render_template("project.html",
                           project=rows,
                           project_name=project_data["project_name"],
                           headers=headers,
                           can_edit=can_edit,
                           can_comment=can_comment,
                           comments=comments,
                           team_id=team_id,
                           project_id=project_id,
                           members=members)

# ===== Add comment =====
@app.route("/team/<team_id>/project/<project_id>/add_comment", methods=["POST"])
def add_comment(team_id, project_id):
    if not session.get("logged_in"): return redirect(url_for("login"))
    uid = session["uid"]

    # Determine if user can comment
    team_doc = db.collection("teams").document(team_id).get()
    if not team_doc.exists:
        flash("Team not found")
        return redirect(url_for("dashboard"))
    team_data = team_doc.to_dict()
    owner_uid = team_data.get("owner")
    shared_with = team_data.get("shared_with", {})
    if uid == owner_uid:
        can_comment = True
    else:
        can_comment = shared_with.get(uid) in ("comment", "edit")

    if not can_comment:
        flash("You do not have permission to comment on this project.")
        return redirect(url_for("project_view", team_id=team_id, project_id=project_id))

    text = request.form.get("comment_text", "").strip()
    if not text:
        flash("Comment cannot be empty.")
        return redirect(url_for("project_view", team_id=team_id, project_id=project_id))

    # Add comment object
    project_ref = db.collection("teams").document(team_id).collection("projects").document(project_id)
    project_doc = project_ref.get()
    if not project_doc.exists:
        flash("Project not found.")
        return redirect(url_for("team_projects", team_id=team_id))

    comments = project_doc.to_dict().get("comments", [])
    comment_obj = {
        "user_id": uid,
        "user_name": session.get("name", "User"),
        "text": text,
        "timestamp": datetime.utcnow().isoformat()
    }
    comments.append(comment_obj)
    project_ref.update({"comments": comments})
    flash("Comment added.")
    return redirect(url_for("project_view", team_id=team_id, project_id=project_id))

# ===== Upload CSV =====
@app.route("/team/<team_id>/project/<project_id>/upload_csv", methods=["POST"])
def upload_csv(team_id, project_id):
    if not session.get("logged_in"):
        return redirect(url_for("login"))

    file = request.files.get("csv_file")
    if not file or not file.filename.endswith(".csv"):
        flash("Please upload a valid CSV file.")
        return redirect(url_for("project_view", team_id=team_id, project_id=project_id))

    stream = io.StringIO(file.stream.read().decode("utf-8"))
    reader = list(csv.reader(stream))
    if not reader:
        flash("CSV file is empty.")
        return redirect(url_for("project_view", team_id=team_id, project_id=project_id))

    headers = reader[0]
    rows_data = []
    for row in reader[1:]:
        row_dict = {}
        for i, header in enumerate(headers):
            row_dict[f"col{i+1}"] = row[i] if i < len(row) else ""
        rows_data.append(row_dict)

    db.collection("teams").document(team_id).collection("projects").document(project_id).update({
        "rows": rows_data
    })
    flash("CSV uploaded successfully!")
    return redirect(url_for("project_view", team_id=team_id, project_id=project_id))

# ===== Export CSV =====
@app.route("/team/<team_id>/project/<project_id>/export_csv")
def export_csv(team_id, project_id):
    if not session.get("logged_in"):
        return redirect(url_for("login"))

    project_doc = db.collection("teams").document(team_id).collection("projects").document(project_id).get()
    if not project_doc.exists:
        flash("Project not found.")
        return redirect(url_for("team_projects", team_id=team_id))

    project_data = project_doc.to_dict()
    rows = project_data.get("rows", [])
    headers = [f"Column {i+1}" for i in range(len(rows[0]))] if rows else ["Column 1"]

    temp = tempfile.NamedTemporaryFile(delete=False, suffix=".csv")
    with open(temp.name, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(headers)
        for r in rows:
            writer.writerow([r.get(f"col{i+1}", "") for i in range(len(headers))])

    return send_file(temp.name, mimetype="text/csv", as_attachment=True, download_name=f"{project_data['project_name']}.csv")

# ===== Share Team =====
@app.route("/team/<team_id>/share", methods=["GET","POST"])
def share_team(team_id):
    if not session.get("logged_in"): 
        return redirect(url_for("login"))

    team_ref = db.collection("teams").document(team_id)
    team_doc = team_ref.get()
    if not team_doc.exists:
        flash("Team not found")
        return redirect(url_for("dashboard"))

    if team_doc.to_dict()["owner"] != session["uid"]:
        flash("Only owner can share")
        return redirect(url_for("dashboard"))

    shared_with = team_doc.to_dict().get("shared_with", {})

    if request.method == "POST":
        email = request.form.get("user_email").strip().lower()
        access = request.form.get("access_level", "view")

        users = db.collection("users").where("email","==",email).limit(1).stream()
        target_uid = None
        for u in users:
            target_uid = u.id

        if not target_uid:
            flash("User not found")
            return redirect(url_for("share_team", team_id=team_id))

        if target_uid == session["uid"]:
            flash("Cannot share with yourself")
            return redirect(url_for("share_team", team_id=team_id))

        shared_with[target_uid] = access
        team_ref.update({"shared_with": shared_with})

        db.collection("users").document(target_uid).collection("shared_teams").document(team_id).set({
            "owner": session["uid"],
            "team_name": team_doc.to_dict()["team_name"],
            "access": access
        })

        flash(f"Shared team with {email} ({access})")
        return redirect(url_for("share_team", team_id=team_id))

    shared_users = []
    for ruid, access in shared_with.items():
        udoc = db.collection("users").document(ruid).get()
        email = udoc.to_dict().get("email", "(unknown)") if udoc.exists else "(unknown)"
        shared_users.append({"uid": ruid, "email": email, "access": access})

    return render_template("share_team.html", team_name=team_doc.to_dict()["team_name"], shared_users=shared_users)

# ===== Update user access =====
@app.route("/team/<team_id>/update_access/<user_id>", methods=["POST"])
def update_access(team_id, user_id):
    if not session.get("logged_in"):
        return redirect(url_for("login"))

    team_doc = db.collection("teams").document(team_id).get()
    if not team_doc.exists or team_doc.to_dict()["owner"] != session["uid"]:
        flash("Only the team owner can update access.")
        return redirect(url_for("dashboard"))

    new_access = request.form.get("access_level")
    if new_access:
        # Update both the team doc and the user's shared_teams doc
        team_ref = db.collection("teams").document(team_id)
        team_data = team_ref.get().to_dict()
        shared_with = team_data.get("shared_with", {})
        shared_with[user_id] = new_access
        team_ref.update({"shared_with": shared_with})

        db.collection("users").document(user_id).collection("shared_teams").document(team_id).update({
            "access": new_access
        })
        flash("Access updated successfully.")
    return redirect(url_for("share_team", team_id=team_id))

# ===== Remove user access =====
@app.route("/team/<team_id>/remove_access/<user_id>", methods=["POST"])
def remove_access(team_id, user_id):
    if not session.get("logged_in"):
        return redirect(url_for("login"))

    team_doc = db.collection("teams").document(team_id).get()
    if not team_doc.exists or team_doc.to_dict()["owner"] != session["uid"]:
        flash("Only the team owner can remove users.")
        return redirect(url_for("dashboard"))

    # Remove from team.shared_with
    team_ref = db.collection("teams").document(team_id)
    team_data = team_ref.get().to_dict()
    shared_with = team_data.get("shared_with", {})
    if user_id in shared_with:
        del shared_with[user_id]
    team_ref.update({"shared_with": shared_with})

    # Remove from user's shared_teams
    db.collection("users").document(user_id).collection("shared_teams").document(team_id).delete()
    flash("User removed successfully.")
    return redirect(url_for("share_team", team_id=team_id))


@app.template_filter('format_comment_time')
def format_comment_time(value):
    if not value:
        return ""
    dt = datetime.fromisoformat(value)
    dt -= timedelta(hours=5)
    return dt.strftime("%B %d, %Y at %-I:%M %p")  # %-I = 12-hour format without leading zero

if __name__=="__main__":
    app.run(debug=True)
