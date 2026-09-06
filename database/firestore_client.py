import os
import json
from google.cloud import firestore
from google.oauth2 import service_account
from datetime import datetime, timedelta, timezone
from config.settings import settings
from utils.logger import get_logger

logger = get_logger(__name__)

db = None


def _firestore_timeout_seconds():
    return max(3, int(getattr(settings, "FIRESTORE_TIMEOUT", 12) or 12))

def init_firestore():
    global db
    try:
        service_account_json = os.environ.get("FIRESTORE_SERVICE_ACCOUNT_JSON")
        
        if service_account_json:
            try:
                creds_info = json.loads(service_account_json)
                credentials = service_account.Credentials.from_service_account_info(creds_info)
            except Exception as parse_error:
                logger.error("Failed to initialize Firestore: Invalid FIRESTORE_SERVICE_ACCOUNT_JSON format or content")
                raise RuntimeError("Firestore initialization failed due to invalid credentials configuration") from parse_error
                
            db = firestore.Client(credentials=credentials)
            logger.info("Firestore client initialized successfully with injected credentials")
        else:
            db = firestore.Client()
            logger.info("Firestore client initialized successfully with Application Default Credentials (ADC)")
            
        return db
    except Exception:
        logger.error("Failed to initialize Firestore. (Underlying exception hidden for security)")
        raise

def get_db():
    global db
    if db is None:
        init_firestore()
    return db

def save_session(session_id, data):
    try:
        db = get_db()
        document_data = dict(data)
        document_data['updated_at'] = firestore.SERVER_TIMESTAMP
        if 'created_at' not in document_data:
            document_data['created_at'] = firestore.SERVER_TIMESTAMP
        
        db.collection('sessions').document(session_id).set(
            document_data,
            merge=True,
            retry=None,
            timeout=_firestore_timeout_seconds(),
        )
        logger.info(f"Session saved: {session_id}")
        return True
    except Exception as e:
        logger.error(f"Error saving session {session_id}: {e}")
        return False

def get_session(session_id):
    try:
        db = get_db()
        doc = db.collection('sessions').document(session_id).get(
            retry=None,
            timeout=_firestore_timeout_seconds(),
        )
        if doc.exists:
            logger.info(f"Session retrieved: {session_id}")
            return doc.to_dict()
        else:
            logger.warning(f"Session not found: {session_id}")
            return None
    except Exception as e:
        logger.error(f"Error retrieving session {session_id}: {e}")
        return None

def update_progress(session_id, progress_data):
    try:
        db = get_db()
        document_data = dict(progress_data)
        document_data['updated_at'] = firestore.SERVER_TIMESTAMP
        
        db.collection('progress').document(session_id).set(
            document_data,
            merge=True,
            retry=None,
            timeout=_firestore_timeout_seconds(),
        )
        logger.info(f"Progress updated: {session_id}")
        return True
    except Exception as e:
        logger.error(f"Error updating progress {session_id}: {e}")
        return False

# Alias for compatibility
save_progress = update_progress

def update_session(session_id, data):
    """Update existing session with new data"""
    try:
        db = get_db()
        document_data = dict(data)
        document_data['updated_at'] = firestore.SERVER_TIMESTAMP
        db.collection('sessions').document(session_id).update(
            document_data,
            retry=None,
            timeout=_firestore_timeout_seconds(),
        )
        logger.info(f"Session updated: {session_id}")
        return True
    except Exception as e:
        logger.error(f"Error updating session {session_id}: {e}")
        return False

def get_result(session_id):
    """Get evaluation result for session"""
    try:
        db = get_db()
        doc = db.collection('results').document(session_id).get(
            retry=None,
            timeout=_firestore_timeout_seconds(),
        )
        if doc.exists:
            return doc.to_dict()
        return None
    except Exception as e:
        logger.error(f"Error getting result {session_id}: {e}")
        return None

def save_result(session_id, result_data):
    try:
        db = get_db()
        document_data = dict(result_data)
        document_data['timestamp'] = firestore.SERVER_TIMESTAMP
        
        db.collection('results').document(session_id).set(
            document_data,
            retry=None,
            timeout=_firestore_timeout_seconds(),
        )
        logger.info(f"Result saved: {session_id}")
        return True
    except Exception as e:
        logger.error(f"Error saving result {session_id}: {e}")
        return False

def save_resume_insights(session_id, insights):
    try:
        db = get_db()
        document_data = dict(insights)
        document_data['timestamp'] = firestore.SERVER_TIMESTAMP
        
        db.collection('resume_insights').document(session_id).set(
            document_data,
            retry=None,
            timeout=_firestore_timeout_seconds(),
        )
        logger.info(f"Resume insights saved: {session_id}")
        return True
    except Exception as e:
        logger.error(f"Error saving resume insights {session_id}: {e}")
        return False

def get_cache(key):
    try:
        db = get_db()
        doc = db.collection('cache').document(key).get(
            retry=None,
            timeout=_firestore_timeout_seconds(),
        )
        
        if doc.exists:
            data = doc.to_dict()
            expires_at = data.get('expires_at')
            
            if expires_at and isinstance(expires_at, datetime):
                if datetime.now(timezone.utc) < expires_at.replace(tzinfo=timezone.utc):
                    logger.info(f"Cache hit: {key}")
                    return data.get('value')
                else:
                    logger.info(f"Cache expired: {key}")
                    return None
            else:
                logger.info(f"Cache hit (no expiry): {key}")
                return data.get('value')
        else:
            logger.info(f"Cache miss: {key}")
            return None
    except Exception as e:
        logger.error(f"Error getting cache {key}: {e}")
        return None

def set_cache(key, value, ttl_hours=24):
    try:
        db = get_db()
        expires_at = datetime.now(timezone.utc) + timedelta(hours=ttl_hours)
        
        cache_data = {
            'value': value,
            'expires_at': expires_at,
            'created_at': firestore.SERVER_TIMESTAMP
        }
        
        db.collection('cache').document(key).set(
            cache_data,
            retry=None,
            timeout=_firestore_timeout_seconds(),
        )
        logger.info(f"Cache set: {key} (TTL: {ttl_hours}h)")
        return True
    except Exception as e:
        logger.error(f"Error setting cache {key}: {e}")
        return False


def save_role_profile(role_key, profile_data, ttl_minutes=15):
    """Persist a refreshable market profile for a target role."""
    try:
        db = get_db()
        expires_at = datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes)
        document_data = dict(profile_data)
        document_data["role_key"] = role_key
        document_data["expires_at"] = expires_at
        document_data["updated_at"] = firestore.SERVER_TIMESTAMP
        if "created_at" not in document_data:
            document_data["created_at"] = firestore.SERVER_TIMESTAMP

        db.collection("role_profiles").document(role_key).set(
            document_data,
            merge=True,
            retry=None,
            timeout=_firestore_timeout_seconds(),
        )
        logger.info(f"Role profile saved: {role_key}")
        return True
    except Exception as e:
        logger.error(f"Error saving role profile {role_key}: {e}")
        return False


def get_role_profile(role_key, allow_stale=False):
    """Fetch a role market profile, optionally returning stale data."""
    try:
        db = get_db()
        doc = db.collection("role_profiles").document(role_key).get(
            retry=None,
            timeout=_firestore_timeout_seconds(),
        )
        if not doc.exists:
            logger.info(f"Role profile miss: {role_key}")
            return None

        data = doc.to_dict() or {}
        expires_at = data.get("expires_at")
        is_fresh = True
        if expires_at and isinstance(expires_at, datetime):
            is_fresh = datetime.now(timezone.utc) < expires_at.replace(tzinfo=timezone.utc)

        if is_fresh or allow_stale:
            data["is_stale"] = not is_fresh
            logger.info(
                f"Role profile hit: {role_key} ({'stale' if data['is_stale'] else 'fresh'})"
            )
            return data

        logger.info(f"Role profile expired: {role_key}")
        return None
    except Exception as e:
        logger.error(f"Error getting role profile {role_key}: {e}")
        return None


def save_skill_gap_job(job_id, job_data):
    """Create or update an async skill-gap job record."""
    try:
        db = get_db()
        document_data = dict(job_data)
        document_data["updated_at"] = firestore.SERVER_TIMESTAMP
        if "created_at" not in document_data:
            document_data["created_at"] = firestore.SERVER_TIMESTAMP

        db.collection("skill_gap_jobs").document(job_id).set(
            document_data,
            merge=True,
            retry=None,
            timeout=_firestore_timeout_seconds(),
        )
        logger.info(f"Skill-gap job saved: {job_id}")
        return True
    except Exception as e:
        logger.error(f"Error saving skill-gap job {job_id}: {e}")
        return False


def get_skill_gap_job(job_id):
    """Fetch an async skill-gap job record."""
    try:
        db = get_db()
        doc = db.collection("skill_gap_jobs").document(job_id).get(
            retry=None,
            timeout=_firestore_timeout_seconds(),
        )
        if doc.exists:
            logger.info(f"Skill-gap job retrieved: {job_id}")
            return doc.to_dict()
        logger.warning(f"Skill-gap job not found: {job_id}")
        return None
    except Exception as e:
        logger.error(f"Error retrieving skill-gap job {job_id}: {e}")
        return None
