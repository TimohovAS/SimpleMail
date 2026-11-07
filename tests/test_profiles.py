"""Profile selection never touches the user's locator or OAuth cache."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from mailclient import google_oauth, oauth, profiles
from mailclient.db import Account, Database


class TestProfiles(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.marker = self.root / "locator" / "profile.json"
        patcher = mock.patch.dict(os.environ, {profiles.PROFILE_FILE_ENV: str(self.marker)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.temporary.cleanup)

    def database(self, name, *, populated=True):
        db = Database(self.root / name / "mail.db")
        self.addCleanup(db.close)
        if populated:
            db.save_account(Account(name="Test mailbox", enabled=False))
        return db

    def write_locator(self, directory, *, version=1):
        self.marker.parent.mkdir(parents=True, exist_ok=True)
        self.marker.write_text(json.dumps({"version": version, "data_dir": str(directory)}), encoding="utf-8")

    def test_explicit_choice_wins_and_can_be_new(self):
        remembered = self.database("remembered")
        fallback = self.database("fallback")
        self.write_locator(Path(remembered.path).parent)
        chosen = self.root / "new explicit"
        self.assertEqual(profiles.select_data_dir(chosen, Path(fallback.path).parent), chosen)
        self.assertFalse(chosen.exists())

    def test_remembered_profile_wins_over_installer_hint_and_default(self):
        remembered = self.database("remembered")
        self.write_locator(Path(remembered.path).parent)
        missing_hint = self.root / "old removed profile"
        with mock.patch.object(profiles, "default_data_dir", side_effect=AssertionError("unused")):
            self.assertEqual(profiles.select_data_dir(fallback=missing_hint), Path(remembered.path).parent)

    def test_installer_hint_is_used_only_without_locator(self):
        fallback = self.database("legacy")
        with mock.patch.object(profiles, "default_data_dir", side_effect=AssertionError("unused")):
            self.assertEqual(profiles.select_data_dir(fallback=Path(fallback.path).parent), Path(fallback.path).parent)
        self.assertFalse(self.marker.exists())

    def test_default_is_used_only_without_locator_or_hint_and_is_not_created(self):
        default = self.root / "new default"
        with mock.patch.object(profiles, "default_data_dir", return_value=default):
            self.assertEqual(profiles.select_data_dir(), default)
        self.assertFalse(default.exists())
        self.assertFalse(self.marker.parent.exists())

    def test_missing_saved_database_does_not_fall_back_or_create_anything(self):
        missing = self.root / "disconnected drive"
        self.write_locator(missing)
        fallback = self.database("legacy")
        with self.assertRaises(profiles.ProfileError):
            profiles.select_data_dir(fallback=Path(fallback.path).parent)
        self.assertFalse(missing.exists())
        self.assertEqual(json.loads(self.marker.read_text(encoding="utf-8"))["data_dir"], str(missing))

    def test_missing_installer_hint_is_an_error(self):
        missing = self.root / "missing legacy"
        with self.assertRaises(profiles.ProfileError):
            profiles.select_data_dir(fallback=missing)
        self.assertFalse(missing.exists())
        self.assertFalse(self.marker.exists())

    def test_invalid_locator_is_not_treated_as_absent(self):
        self.marker.parent.mkdir()
        values = ["{broken", "[]", "null", '{"version":2,"data_dir":"x"}',
                  '{"version":true,"data_dir":"x"}', '{"version":1,"data_dir":"relative"}',
                  '{"version":1,"data_dir":null}', '{"version":1,"data_dir":""}']
        for value in values:
            with self.subTest(value=value):
                self.marker.write_text(value, encoding="utf-8")
                with self.assertRaises(profiles.ProfileError):
                    profiles.select_data_dir(fallback=self.root)
                self.assertEqual(self.marker.read_text(encoding="utf-8"), value)

    def test_explicit_choice_bypasses_damaged_locator(self):
        self.marker.parent.mkdir()
        self.marker.write_text("broken", encoding="utf-8")
        selected = self.root / "explicit"
        self.assertEqual(profiles.select_data_dir(selected), selected)
        self.assertEqual(self.marker.read_text(encoding="utf-8"), "broken")

    def test_isolated_session_requires_explicit_and_never_reads_locator(self):
        with mock.patch.object(profiles, "_read_locator", side_effect=AssertionError("must not read")):
            with self.assertRaises(profiles.ProfileError):
                profiles.select_data_dir(isolated=True)
            explicit = self.root / "isolated"
            self.assertEqual(profiles.select_data_dir(explicit, isolated=True), explicit)
        self.assertFalse(explicit.exists())

    def test_selector_does_not_open_or_migrate_database(self):
        folder = self.root / "old schema"
        folder.mkdir()
        file = folder / "mail.db"
        file.write_bytes(b"not even SQLite")
        self.write_locator(folder)
        self.assertEqual(profiles.select_data_dir(), folder)
        self.assertEqual(file.read_bytes(), b"not even SQLite")
        self.assertEqual(list(folder.iterdir()), [file])

    def test_environment_override_and_home_default_do_not_depend_on_appdata(self):
        self.assertEqual(profiles.profile_file(), self.marker)
        with mock.patch.dict(os.environ, {profiles.PROFILE_FILE_ENV: "", "APPDATA": "unrelated"}), \
                mock.patch.object(Path, "home", return_value=self.root):
            self.assertEqual(profiles.profile_file(), self.root / ".simplemail" / "profile.json")

    def test_empty_profile_does_not_create_or_replace_locator(self):
        db = self.database("empty", populated=False)
        self.assertFalse(profiles.remember_profile(db))
        self.assertFalse(self.marker.parent.exists())
        self.write_locator(self.root / "previous")
        original = self.marker.read_bytes()
        self.assertFalse(profiles.remember_profile(db))
        self.assertEqual(self.marker.read_bytes(), original)

    def test_first_saved_account_remembers_database_and_all_settings_stay_unchanged(self):
        db = self.database("new", populated=False)
        db.set_setting("language", "sr_Latn")
        db.set_setting("pw:1", "test-only-fallback")
        self.assertFalse(profiles.remember_profile(db))
        db.save_account(Account(name="New mailbox", enabled=False))
        before = db.conn().total_changes
        self.assertTrue(profiles.remember_profile(db))
        self.assertEqual(db.conn().total_changes, before)
        payload = json.loads(self.marker.read_text(encoding="utf-8"))
        self.assertEqual(payload, {"version": 1, "data_dir": str(Path(db.path).parent)})
        self.assertNotIn("test-only", self.marker.read_text(encoding="utf-8"))
        self.assertEqual(db.get_setting("language"), "sr_Latn")
        self.assertEqual(db.get_setting("pw:1"), "test-only-fallback")

    def test_remember_uses_resolved_database_file_not_resolved_parent(self):
        db = self.database("alias")
        actual = self.root / "physical"
        actual.mkdir()
        (actual / "mail.db").touch()
        original_resolve = Path.resolve
        def resolve(path, *args, **kwargs):
            if path == Path(db.path):
                return actual / "mail.db"
            return original_resolve(path, *args, **kwargs)
        with mock.patch.object(Path, "resolve", resolve):
            self.assertTrue(profiles.remember_profile(db))
        self.assertEqual(json.loads(self.marker.read_text(encoding="utf-8"))["data_dir"], str(actual))

    def test_selector_resolves_existing_database_file_overlay(self):
        db = self.database("alias")
        self.write_locator(Path(db.path).parent)
        actual = self.root / "physical"
        original_resolve = Path.resolve
        def resolve(path, *args, **kwargs):
            return actual / "mail.db" if path == Path(db.path) else original_resolve(path, *args, **kwargs)
        with mock.patch.object(Path, "resolve", resolve):
            self.assertEqual(profiles.select_data_dir(), actual)

    def test_atomic_replace_happens_after_complete_temporary_write(self):
        db = self.database("new")
        self.write_locator(self.root / "previous")
        original = self.marker.read_bytes()
        replace = os.replace
        def checked_replace(source, target):
            self.assertEqual(Path(source).parent, self.marker.parent)
            self.assertEqual(Path(target), self.marker)
            self.assertEqual(self.marker.read_bytes(), original)
            payload = json.loads(Path(source).read_text(encoding="utf-8"))
            self.assertEqual(payload["data_dir"], str(Path(db.path).parent))
            replace(source, target)
        with mock.patch.object(profiles.os, "replace", side_effect=checked_replace) as replaced:
            self.assertTrue(profiles.remember_profile(db))
        replaced.assert_called_once()
        self.assertEqual(list(self.marker.parent.iterdir()), [self.marker])

    def test_failed_replace_preserves_previous_locator_and_removes_temporary(self):
        db = self.database("new")
        self.write_locator(self.root / "previous")
        original = self.marker.read_bytes()
        with mock.patch.object(profiles.os, "replace", side_effect=PermissionError("test denied")):
            with self.assertRaises(profiles.ProfileError):
                profiles.remember_profile(db)
        self.assertEqual(self.marker.read_bytes(), original)
        self.assertEqual(list(self.marker.parent.iterdir()), [self.marker])

    def test_locator_read_permission_error_is_visible(self):
        with mock.patch.object(Path, "read_text", side_effect=PermissionError("test denied")):
            with self.assertRaises(profiles.ProfileError):
                profiles.select_data_dir()

    def test_google_identity_and_session_survive_build_and_default_directory_change(self):
        db = self.database("custom profile", populated=False)
        account = Account(name="Google", email="test@example.invalid", username="test@example.invalid",
                          auth_method="google", oauth_client_id="123-test.apps.googleusercontent.com",
                          oauth_client_secret="test-secret", enabled=False)
        db.save_account(account)
        original_key = google_oauth._key(db, account)
        self.assertTrue(profiles.remember_profile(db))
        with mock.patch.object(profiles, "default_data_dir", return_value=self.root / "new default"), \
                mock.patch("sys.executable", str(self.root / "new install" / "SimpleMail.exe")), \
                mock.patch.object(google_oauth, "_load", return_value={"refresh_token": "test-only"}) as load, \
                mock.patch.object(google_oauth, "_post", side_effect=AssertionError("no network")):
            selected = profiles.select_data_dir(fallback=self.root / "stale install hint")
            after_update = SimpleNamespace(path=str(selected / "mail.db"))
            self.assertEqual(google_oauth._key(after_update, account), original_key)
            self.assertTrue(oauth.has_session(after_update, account))
            load.assert_called_once_with(original_key)


if __name__ == "__main__":
    unittest.main()
