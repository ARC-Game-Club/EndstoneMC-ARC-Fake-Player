"""Behavior-pack upgrades preserve custom files and activate the current version."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.endstone_stub import install as install_stub

install_stub()

from endstone_arc_fake_player import packing


class PackUpgradeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def make_pack(self, path, *, version=1, script="original", structure=b"structure"):
        path.mkdir(parents=True, exist_ok=True)
        (path / "scripts").mkdir(exist_ok=True)
        (path / "structures" / "arc_fake_player").mkdir(parents=True, exist_ok=True)
        (path / "manifest.json").write_text(json.dumps({
            "format_version": 2,
            "header": {"uuid": packing.PACK_UUID, "version": [3, 0, version]},
        }), encoding="utf-8")
        (path / "scripts" / "main.js").write_text(script, encoding="utf-8")
        (path / "structures" / "arc_fake_player" / "empty.mcstructure").write_bytes(structure)
        return path

    def make_world(self, name="active"):
        world = self.root / "worlds" / name
        world.mkdir(parents=True)
        (world / "level.dat").write_bytes(b"untouched world")
        return world

    def test_registration_updates_version_and_preserves_other_packs(self):
        world = self.make_world()
        registration = world / "world_behavior_packs.json"
        other = {"pack_id": "another-pack", "version": [1, 2, 3]}
        registration.write_text(json.dumps([
            other, {"pack_id": packing.PACK_UUID, "version": [3, 0, 11], "custom": True},
            {"pack_id": packing.PACK_UUID, "version": [3, 0, 12]},
        ]), encoding="utf-8")

        self.assertEqual(packing.register_pack(world), (True, "已更新登记版本"))
        entries = json.loads(registration.read_text(encoding="utf-8"))
        self.assertEqual(entries, [other, {
            "pack_id": packing.PACK_UUID, "version": packing.PACK_VERSION, "custom": True,
        }])
        self.assertEqual(packing.register_pack(world), (True, "已注册过"))

    def test_named_world_does_not_select_other_worlds(self):
        active = self.make_world()
        self.make_world("backup")
        with patch.object(Path, "cwd", return_value=self.root):
            self.assertEqual(packing.find_world_dirs(level_name="active"), [active.resolve()])

    def test_structure_only_change_triggers_upgrade(self):
        source = self.make_pack(self.root / "source")
        target = self.root / "target"
        self.assertTrue(packing.extract_pack(source, target)[0])
        structure = Path("structures/arc_fake_player/empty.mcstructure")
        (source / structure).write_bytes(b"fixed structure")

        self.assertEqual(packing.extract_pack(source, target), (True, "已释放"))
        self.assertEqual((target / structure).read_bytes(), b"fixed structure")
        self.assertIn(structure.as_posix(), packing._fingerprint(target))

    def test_custom_script_survives_multiple_partial_upgrades(self):
        source = self.make_pack(self.root / "source")
        target = self.root / "target"
        packing.extract_pack(source, target)
        script = target / "scripts" / "main.js"
        script.write_text("my custom script", encoding="utf-8")
        original_hash = packing._read_release(target)["scripts/main.js"]
        (target / "my-notes.txt").write_text("keep my notes", encoding="utf-8")

        for version in (2, 3, 4):
            with self.subTest(version=version):
                self.make_pack(source, version=version, script=f"release {version}")
                ok, msg = packing.extract_pack(source, target)
                self.assertTrue(ok)
                self.assertIn("部分更新", msg)
                self.assertEqual(script.read_text(encoding="utf-8"), "my custom script")
                self.assertEqual(packing._read_release(target)["scripts/main.js"], original_hash)
                manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
                self.assertEqual(manifest["header"]["version"], [3, 0, version])
        self.assertEqual((target / "my-notes.txt").read_text(encoding="utf-8"), "keep my notes")

    def test_legacy_windows_release_keys_preserve_custom_script(self):
        source = self.make_pack(self.root / "source")
        target = self.root / "target"
        packing.extract_pack(source, target)
        record = packing._read_release(target)
        record["scripts\\main.js"] = record.pop("scripts/main.js")
        (target / packing._RELEASE_NOTE).unlink()
        (target / packing._LEGACY_RELEASE_NOTE).write_text(json.dumps(record), encoding="utf-8")
        (target / "scripts" / "main.js").write_text("user modification", encoding="utf-8")
        self.make_pack(source, version=2, script="updated release")

        self.assertTrue(packing.extract_pack(source, target)[0])
        self.assertEqual((target / "scripts" / "main.js").read_text(encoding="utf-8"),
                         "user modification")

    def test_legacy_directory_moves_with_user_files(self):
        parent = self.root / "behavior_packs"
        legacy = self.make_pack(parent / packing.LEGACY_PACK_DIR_NAME)
        (legacy / "notes.txt").write_text("user notes", encoding="utf-8")
        target = parent / packing.PACK_DIR_NAME

        ok, changed, _ = packing._migrate_legacy_pack(target)
        self.assertTrue(ok)
        self.assertTrue(changed)
        self.assertFalse(legacy.exists())
        self.assertEqual((target / "notes.txt").read_text(encoding="utf-8"), "user notes")

    def test_directory_collision_keeps_both_directories(self):
        parent = self.root / "behavior_packs"
        legacy = self.make_pack(parent / packing.LEGACY_PACK_DIR_NAME, script="old script")
        target = self.make_pack(parent / packing.PACK_DIR_NAME, script="new script")

        ok, changed, message = packing._migrate_legacy_pack(target)
        self.assertFalse(ok)
        self.assertFalse(changed)
        self.assertIn("同时存在", message)
        self.assertEqual((legacy / "scripts" / "main.js").read_text(encoding="utf-8"), "old script")
        self.assertEqual((target / "scripts" / "main.js").read_text(encoding="utf-8"), "new script")

    def test_foreign_or_invalid_legacy_manifest_is_not_moved(self):
        parent = self.root / "behavior_packs"
        legacy = self.make_pack(parent / packing.LEGACY_PACK_DIR_NAME)
        target = parent / packing.PACK_DIR_NAME
        for manifest in (json.dumps({"header": {"uuid": "foreign"}}), "broken json"):
            with self.subTest(manifest=manifest):
                (legacy / "manifest.json").write_text(manifest, encoding="utf-8")
                self.assertFalse(packing._migrate_legacy_pack(target)[0])
                self.assertTrue(legacy.exists())
                self.assertFalse(target.exists())

    def test_install_migrates_current_world_and_leaves_other_world_untouched(self):
        source = self.make_pack(self.root / "source", version=16)
        active = self.make_world()
        backup = self.make_world("backup")
        for parent in (self.root / "behavior_packs", active / "behavior_packs"):
            self.make_pack(parent / packing.LEGACY_PACK_DIR_NAME)
        with patch.object(Path, "cwd", return_value=self.root):
            report = packing.install(source, level_name="active", touch_level_dat=False)

        self.assertTrue(report["ok"], report["errors"])
        self.assertTrue(report["needs_restart"])
        self.assertEqual(report["world_dirs"], [str(active.resolve())])
        self.assertFalse((backup / "world_behavior_packs.json").exists())
        self.assertEqual((backup / "level.dat").read_bytes(), b"untouched world")
        for parent in (self.root / "behavior_packs", active / "behavior_packs"):
            self.assertTrue((parent / packing.PACK_DIR_NAME / "manifest.json").exists())
            self.assertFalse((parent / packing.LEGACY_PACK_DIR_NAME).exists())

    def test_install_collision_does_not_register_or_modify_world(self):
        source = self.make_pack(self.root / "source", version=16)
        active = self.make_world()
        parent = self.root / "behavior_packs"
        self.make_pack(parent / packing.LEGACY_PACK_DIR_NAME)
        self.make_pack(parent / packing.PACK_DIR_NAME)
        with patch.object(Path, "cwd", return_value=self.root):
            report = packing.install(source, level_name="active")

        self.assertFalse(report["ok"])
        self.assertTrue(report["errors"])
        self.assertFalse((active / "world_behavior_packs.json").exists())
        self.assertEqual((active / "level.dat").read_bytes(), b"untouched world")

    def test_partial_install_update_requires_restart(self):
        source = self.make_pack(self.root / "source", version=16)
        active = self.make_world()
        packing.register_pack(active)
        with patch.object(Path, "cwd", return_value=self.root), \
                patch.object(packing, "extract_pack", return_value=(True, "部分更新（保留脚本）")):
            report = packing.install(source, level_name="active", touch_level_dat=False)
        self.assertTrue(report["ok"])
        self.assertTrue(report["needs_restart"])


def main():
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(PackUpgradeTests))
    if not result.wasSuccessful():
        return 1
    print(f"全部 {result.testsRun} 项通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
