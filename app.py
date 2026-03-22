"""
Hackathon organizer dashboard: link Instagram, LinkedIn, TikTok,
and add other links or uploads outside those platforms.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

from flask import Flask, flash, redirect, render_template, request, session, url_for
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-change-me-in-production")

UPLOAD_DIR = Path(__file__).resolve().parent / "uploads"
ALLOWED_EXTENSIONS = {
    "pdf",
    "png",
    "jpg",
    "jpeg",
    "gif",
    "webp",
    "txt",
    "md",
    "doc",
    "docx",
}


def _session_defaults() -> None:
    if "social" not in session:
        session["social"] = {"instagram": "", "linkedin": "", "tiktok": ""}
    if "other_links" not in session:
        session["other_links"] = ""
    if "uploaded_files" not in session:
        session["uploaded_files"] = []


@app.route("/", methods=["GET", "POST"])
def dashboard():
    _session_defaults()

    if request.method == "POST":
        action = request.form.get("action")
        if action == "social":
            platform = request.form.get("platform", "")
            url = (request.form.get("profile_url") or "").strip()
            if platform in session["social"]:
                session["social"][platform] = url
                session.modified = True
                label = {"instagram": "Instagram", "linkedin": "LinkedIn", "tiktok": "TikTok"}[
                    platform
                ]
                flash(f"{label} profile link saved.", "success")
            return redirect(url_for("dashboard"))

        if action == "other":
            session["other_links"] = (request.form.get("other_links") or "").strip()
            session.modified = True

            file = request.files.get("file")
            if file and file.filename:
                ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
                if ext not in ALLOWED_EXTENSIONS:
                    flash(
                        "That file type is not allowed. Use PDF, images, or common document types.",
                        "error",
                    )
                    return redirect(url_for("dashboard"))
                UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
                safe = secure_filename(file.filename)
                unique = f"{uuid.uuid4().hex[:10]}_{safe}"
                path = UPLOAD_DIR / unique
                file.save(path)
                uploads = session.get("uploaded_files", [])
                uploads.append(unique)
                session["uploaded_files"] = uploads
                session.modified = True
                flash("File uploaded.", "success")
            else:
                flash("Other links and notes saved.", "success")
            return redirect(url_for("dashboard"))

    return render_template(
        "dashboard.html",
        social=session["social"],
        other_links=session.get("other_links", ""),
        uploaded_files=session.get("uploaded_files", []),
    )


if __name__ == "__main__":
    app.run(debug=True)
