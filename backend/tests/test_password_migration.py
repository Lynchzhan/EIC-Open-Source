import hashlib

from eic.core.security import hash_password, verify_and_upgrade_password, verify_password


def test_legacy_sha256_hash_is_accepted_and_upgraded() -> None:
    legacy = hashlib.sha256(b"legacy-password").hexdigest()

    valid, upgraded = verify_and_upgrade_password("legacy-password", legacy)

    assert valid is True
    assert upgraded.startswith("scrypt$")
    assert upgraded != legacy
    assert verify_password("legacy-password", upgraded) is True
    assert verify_and_upgrade_password("wrong", legacy)[0] is False


def test_current_scrypt_hash_is_not_rewritten() -> None:
    current = hash_password("current-password")

    valid, returned = verify_and_upgrade_password("current-password", current)

    assert valid is True
    assert returned == current
