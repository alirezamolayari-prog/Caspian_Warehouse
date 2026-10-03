"""No test may reach the real Windows Credential Manager."""

import keyring

from caspian.core import secrets
from conftest import MemoryKeyring


def test_tests_use_the_in_memory_keyring(memory_keyring):
    assert isinstance(keyring.get_keyring(), MemoryKeyring)
    assert keyring.get_keyring() is memory_keyring
    secrets.set_secret("ai", "provider:1", "sk-test")
    assert memory_keyring.store == {("CaspianWarehouse", "ai:provider:1"): "sk-test"}
    secrets.delete_secret("ai", "provider:1")
    secrets.delete_secret("ai", "provider:1")  # missing: still quiet
    assert memory_keyring.store == {} and secrets.get_secret("ai", "provider:1") is None


def test_each_test_starts_empty(memory_keyring):
    assert memory_keyring.store == {}
