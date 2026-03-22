"""
Hackathon organizer dashboard: Instagram (Instaloader), hackathon website,
supporting document uploads, and content evaluation (pre-LLM aggregation).

Feature flags — flip to True when real integrations are ready:
  INSTAGRAM_ENABLED  (Instaloader download)
  WEBSITE_ENABLED    (website analysis — wired below via website_service)
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

from flask import Flask, flash, redirect, render_template, request, session, url_for
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-change-me-in-production")

PROJECT_ROOT = Path(__file__).resolve().parent
_raw_download = os.environ.get("INSTALOADER_DOWNLOAD_DIR", "instaloader_downloads")
_p_download = Path(_raw_download)
INSTALOADER_DOWNLOAD_DIR = (
    _p_download if _p_download.is_absolute() else (PROJECT_ROOT / _p_download)
)

UPLOAD_DIR  = PROJECT_ROOT / "uploads"
EVAL_DIR    = PROJECT_ROOT / "evaluations"

ALLOWED_EXTENSIONS = {
    "pdf", "png", "jpg", "jpeg", "gif", "webp",
    "txt", "md", "doc", "docx", "ppt", "pptx",
    "xls", "xlsx", "csv", "zip",
}
TEXT_EXTENSIONS = {"txt", "md", "csv"}

# Flip to True when real integrations are wired up.
INSTAGRAM_ENABLED = False


# ── helpers ──────────────────────────────────────────────────────────────────

def _read_document(filename: str) -> dict:
    """Extract text from an uploaded file. Returns a dict with text and metadata."""
    path = UPLOAD_DIR / filename
    ext  = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""

    if ext in TEXT_EXTENSIONS:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
            return {"filename": filename, "type": ext, "text": text, "error": None}
        except Exception as e:
            return {"filename": filename, "type": ext, "text": "", "error": str(e)}

    if ext == "pdf":
        try:
            import pdfplumber
            with pdfplumber.open(path) as pdf:
                text = "\n".join(page.extract_text() or "" for page in pdf.pages)
            return {"filename": filename, "type": "pdf", "text": text.strip(), "error": None}
        except Exception as e:
            return {"filename": filename, "type": "pdf", "text": "", "error": str(e)}

    return {
        "filename": filename,
        "type": ext,
        "text": "",
        "error": f"Binary/unsupported format (.{ext}) — content not extracted",
    }


def _save_eval_context(context: dict, eval_id: str | None = None) -> str:
    """Write evaluation context to disk; return its ID.

    Pass *eval_id* to overwrite an existing file (e.g. after appending LLM
    results).  Omit it to create a new file with a freshly generated ID.
    """
    EVAL_DIR.mkdir(parents=True, exist_ok=True)
    if eval_id is None:
        eval_id = uuid.uuid4().hex
    (EVAL_DIR / f"{eval_id}.json").write_text(
        json.dumps(context, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return eval_id


# ── session defaults ──────────────────────────────────────────────────────────

def _session_defaults() -> None:
    if "instagram" not in session:
        session["instagram"] = ""
    if "instagram_profile" not in session:
        session["instagram_profile"] = None
    if "instagram_error" not in session:
        session["instagram_error"] = None
    if "instagram_download_path" not in session:
        session["instagram_download_path"] = None
    if "instagram_download_summary" not in session:
        session["instagram_download_summary"] = None
    if "website_url" not in session:
        session["website_url"] = ""
    if "website_analysis" not in session:
        session["website_analysis"] = None
    if "website_error" not in session:
        session["website_error"] = None
    if "uploaded_files" not in session:
        session["uploaded_files"] = []
    if "eval_id" not in session:
        session["eval_id"] = None


# ── route ─────────────────────────────────────────────────────────────────────

@app.route("/", methods=["GET", "POST"])
def dashboard():
    _session_defaults()

    if request.method == "POST":
        action = request.form.get("action")

        # ── Instagram ─────────────────────────────────────────────────────────
        if action == "instagram":
            raw = (request.form.get("instagram_username") or "").strip()
            # Normalize locally so we don't need to import instagram_service when disabled.
            username = raw.lstrip("@").split("?")[0].strip()

            if not username:
                flash("Enter an Instagram username.", "error")
                return redirect(url_for("dashboard"))

            if not INSTAGRAM_ENABLED:
                session["instagram"] = username
                # Point at the local download folder (may already exist from a CLI run).
                try:
                    rel = (INSTALOADER_DOWNLOAD_DIR / username).relative_to(PROJECT_ROOT)
                except ValueError:
                    rel = Path("instaloader_downloads") / username
                session["instagram_download_path"] = str(rel).replace("\\", "/")
                session.modified = True
                flash(f"Instagram username @{username} saved.", "success")
                return redirect(url_for("dashboard"))

            import instagram_service

            username = instagram_service.normalize_instagram_username(raw)
            if not username:
                flash("Enter an Instagram username (or profile URL—we only store the username).", "error")
                return redirect(url_for("dashboard"))

            session["instagram"] = username
            session.modified = True

            download_err: str | None = None
            profile_dir: Path | None = None
            try:
                profile_dir = instagram_service.download_profile_to_folder(
                    username, INSTALOADER_DOWNLOAD_DIR,
                )
            except Exception as exc:  # noqa: BLE001
                download_err = str(exc) or exc.__class__.__name__

            if profile_dir is not None:
                try:
                    rel = profile_dir.resolve().relative_to(PROJECT_ROOT)
                    session["instagram_download_path"] = str(rel).replace("\\", "/")
                except ValueError:
                    session["instagram_download_path"] = str(profile_dir.resolve())
                session["instagram_download_summary"] = instagram_service.summarize_download_folder(profile_dir)
            else:
                session["instagram_download_path"] = None
                session["instagram_download_summary"] = None

            scan_err: str | None = None
            profile: dict | None = None
            try:
                profile = instagram_service.scan_profile_as_dict(username)
            except Exception as exc:  # noqa: BLE001
                scan_err = str(exc) or exc.__class__.__name__

            session["instagram_profile"] = profile
            session["instagram_error"] = download_err or scan_err

            if download_err and scan_err:
                flash("Download and preview both failed. Check login env vars.", "error")
            elif download_err:
                flash(f"Download failed ({download_err}). Preview below if available.", "error")
            elif scan_err:
                flash(f"Files saved, but preview failed: {scan_err}", "error")
            else:
                handle = profile["username"] if profile else username
                flash(f"Downloaded @{handle} into {session['instagram_download_path']}.", "success")

            return redirect(url_for("dashboard"))

        # ── Website ───────────────────────────────────────────────────────────
        if action == "website":
            url = (request.form.get("website_url") or "").strip()
            session["website_url"] = url
            session.modified = True
            return redirect(url_for("dashboard"))

        # ── File uploads ──────────────────────────────────────────────────────
        if action == "upload":
            files = request.files.getlist("files")
            saved: list[str] = []
            errors: list[str] = []

            for file in files:
                if not file or not file.filename:
                    continue
                ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
                if ext not in ALLOWED_EXTENSIONS:
                    errors.append(f"{file.filename} — unsupported type (.{ext})")
                    continue
                UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
                safe   = secure_filename(file.filename)
                unique = f"{uuid.uuid4().hex[:8]}_{safe}"
                file.save(UPLOAD_DIR / unique)
                saved.append(unique)

            uploads = session.get("uploaded_files", [])
            uploads.extend(saved)
            session["uploaded_files"] = uploads
            session.modified = True

            if saved:
                flash(f"Uploaded {len(saved)} file{'s' if len(saved) != 1 else ''}.", "success")
            for msg in errors:
                flash(msg, "error")
            return redirect(url_for("dashboard"))

        if action == "delete_upload":
            filename = (request.form.get("filename") or "").strip()
            uploads  = session.get("uploaded_files", [])
            if filename in uploads:
                uploads.remove(filename)
                session["uploaded_files"] = uploads
                session.modified = True
                try:
                    (UPLOAD_DIR / filename).unlink(missing_ok=True)
                except OSError:
                    pass
                flash(f"Removed {filename}.", "success")
            return redirect(url_for("dashboard"))

        # ── Evaluate ──────────────────────────────────────────────────────────
        if action == "evaluate":
            context: dict = {"sources": {}}
            errors_ev: list[str] = []

            # 1. Website — scrape and extract text
            website_url = session.get("website_url", "").strip()
            if website_url:
                import website_service
                try:
                    result = website_service.scrape_website(website_url)
                    context["sources"]["website"] = result
                except Exception as exc:  # noqa: BLE001
                    errors_ev.append(f"Website: {exc}")

            # 2. Instagram captions
            ig_username = session.get("instagram", "").strip()
            if ig_username:
                ig_captions: list[str] = []
                ig_download_path = session.get("instagram_download_path")
                if ig_download_path:
                    import instagram_service
                    folder = (
                        Path(ig_download_path)
                        if Path(ig_download_path).is_absolute()
                        else PROJECT_ROOT / ig_download_path
                    )
                    ig_captions = instagram_service.load_captions_from_folder(folder)
                if not ig_captions:
                    ig_profile = session.get("instagram_profile") or {}
                    ig_captions = ig_profile.get("recent_captions") or []

                context["sources"]["instagram"] = {
                    "username": ig_username,
                    "download_path": ig_download_path or f"instaloader_downloads/{ig_username}",
                    "captions": ig_captions,
                    "caption_count": len(ig_captions),
                }

            # 3. Uploaded documents
            uploaded = session.get("uploaded_files", [])
            if uploaded:
                docs = [_read_document(f) for f in uploaded]
                context["sources"]["documents"] = docs

            if not context["sources"]:
                flash("Add at least one source before evaluating.", "error")
                return redirect(url_for("dashboard"))

            # Save collected sources first so we have a valid eval_id.
            eval_id = _save_eval_context(context)
            session["eval_id"] = eval_id
            session.modified = True

            # ── Gemini MLH analysis ───────────────────────────────────────────
            import gemini_service  # noqa: PLC0415
            try:
                llm_analysis = gemini_service.evaluate(context)
                context["llm_analysis"] = llm_analysis
            except Exception as exc:  # noqa: BLE001
                context["llm_error"] = str(exc) or exc.__class__.__name__
                errors_ev.append(f"Gemini: {context['llm_error']}")

            # Persist the updated context (now includes LLM results).
            _save_eval_context(context, eval_id=eval_id)

            if errors_ev:
                flash(
                    "Evaluation complete with some errors — see results page for details.",
                    "error",
                )
            return redirect(url_for("results", eval_id=eval_id))

    # Sources-ready flag: at least one piece of content is connected.
    has_instagram = bool(session.get("instagram"))
    has_website   = bool(session.get("website_url"))
    has_docs      = bool(session.get("uploaded_files"))
    sources_ready = has_instagram or has_website or has_docs

    return render_template(
        "dashboard.html",
        instagram=session.get("instagram", ""),
        instagram_profile=session.get("instagram_profile"),
        instagram_error=session.get("instagram_error"),
        instagram_download_path=session.get("instagram_download_path"),
        instagram_download_summary=session.get("instagram_download_summary"),
        website_url=session.get("website_url", ""),
        website_analysis=session.get("website_analysis"),
        website_error=session.get("website_error"),
        uploaded_files=session.get("uploaded_files", []),
        sources_ready=sources_ready,
        eval_id=session.get("eval_id"),
    )


@app.route("/results/<eval_id>")
def results(eval_id: str):
    path = EVAL_DIR / f"{eval_id}.json"
    if not path.is_file():
        flash("Evaluation context not found.", "error")
        return redirect(url_for("dashboard"))
    context = json.loads(path.read_text(encoding="utf-8"))
    return render_template("results.html", context=context, eval_id=eval_id)


if __name__ == "__main__":
    app.run(debug=True)
