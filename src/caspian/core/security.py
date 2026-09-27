"""Password / PIN hashing (Argon2id) and policy checks."""

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from caspian.core.text import to_ascii_digits

_hasher = PasswordHasher()

MIN_PASSWORD_LENGTH = 6
PIN_LENGTHS = range(4, 9)
DEFAULT_ADMIN_USERNAME = "admin"
DEFAULT_ADMIN_PASSWORD = "admin"


def hash_secret(secret: str) -> str:
    return _hasher.hash(secret)


def verify_secret(stored_hash: str | None, secret: str) -> bool:
    if not stored_hash:
        return False
    try:
        return _hasher.verify(stored_hash, secret)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(stored_hash: str) -> bool:
    return _hasher.check_needs_rehash(stored_hash)


def normalize_pin(pin: str) -> str:
    """PINs may be typed with Persian digits."""
    return to_ascii_digits(pin.strip())


def password_problem(password: str, username: str = "") -> str | None:
    """Persian explanation of why a new password is unacceptable, or None."""
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"رمز عبور باید حداقل {MIN_PASSWORD_LENGTH} کاراکتر باشد."
    if username and password.lower() == username.lower():
        return "رمز عبور نباید با نام کاربری یکسان باشد."
    if password == DEFAULT_ADMIN_PASSWORD:
        return "رمز عبور پیش‌فرض قابل استفاده نیست."
    return None


def pin_problem(pin: str) -> str | None:
    pin = normalize_pin(pin)
    if not pin.isdigit() or len(pin) not in PIN_LENGTHS:
        return "کد PIN باید ۴ تا ۸ رقم باشد."
    if len(set(pin)) == 1 or pin in "0123456789" or pin in "9876543210":
        return "کد PIN بیش از حد ساده است (مثل ۱۱۱۱ یا ۱۲۳۴)."
    return None
