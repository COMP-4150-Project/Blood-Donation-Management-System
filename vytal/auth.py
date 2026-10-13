"""SE-01 salted password hashes (bcrypt, cost 12)."""
import bcrypt

ROUNDS = 12
MAX_BYTES = 72          # bcrypt ignores everything after 72 bytes, so longer passwords are refused at registration


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(ROUNDS)).decode()


def verify_password(password: str, stored: str) -> bool:
    """False for a wrong password and for anything that is not a bcrypt hash (legacy or malformed); never raises."""
    try:
        return bcrypt.checkpw(password.encode(), stored.encode())
    except (ValueError, TypeError, AttributeError):
        return False
