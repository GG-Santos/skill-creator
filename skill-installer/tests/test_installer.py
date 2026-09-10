from __future__ import annotations

import importlib.util
from contextlib import redirect_stdout
import hashlib
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
import zipfile


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

SPEC = importlib.util.spec_from_file_location(
    "codex_skill_installer", SCRIPTS / "install-skill-from-github.py"
)
assert SPEC is not None and SPEC.loader is not None
installer = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = installer
SPEC.loader.exec_module(installer)

import github_utils


class InstallerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.limits = installer.Limits(
            max_download_bytes=10 * 1024 * 1024,
            max_entries=100,
            max_unpacked_bytes=10 * 1024 * 1024,
            max_compression_ratio=100.0,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _skill(self, parent: Path, name: str) -> Path:
        skill = parent / name
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(
            "---\n"
            f"name: {name}\n"
            "description: Install-test fixture for a requested Codex workflow.\n"
            "---\n\n# Fixture\n\nUse the fixture.\n",
            encoding="utf-8",
        )
        return skill

    def test_archive_rejects_traversal(self) -> None:
        archive = self.root / "bad.zip"
        with zipfile.ZipFile(archive, "w") as handle:
            handle.writestr("repo/../../escape", "bad")
        with zipfile.ZipFile(archive) as handle:
            with self.assertRaises(installer.InstallError):
                installer._safe_extract_zip(handle, str(self.root / "out"), self.limits)

    def test_github_response_stream_stops_at_byte_limit(self) -> None:
        class FakeResponse:
            def __init__(self, payload: bytes):
                self.stream = io.BytesIO(payload)
                self.headers = {}

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

            def read(self, size: int) -> bytes:
                return self.stream.read(size)

        response = FakeResponse(b"12345")
        with mock.patch.object(github_utils.urllib.request, "urlopen", return_value=response):
            with self.assertRaises(github_utils.ResponseTooLargeError):
                github_utils.github_request(
                    "https://github.com/example/repo", "test-agent", max_bytes=4
                )

    def test_archive_rejects_case_collisions(self) -> None:
        archive = self.root / "collision.zip"
        with zipfile.ZipFile(archive, "w") as handle:
            handle.writestr("repo/Skill/SKILL.md", "one")
            handle.writestr("repo/skill/SKILL.md", "two")
        with zipfile.ZipFile(archive) as handle:
            with self.assertRaises(installer.InstallError):
                installer._validated_zip_entries(handle, self.limits)

    def test_archive_entry_limit_counts_directories(self) -> None:
        archive = self.root / "many-entries.zip"
        with zipfile.ZipFile(archive, "w") as handle:
            handle.writestr("repo/", b"")
            handle.writestr("repo/demo/", b"")
            handle.writestr("repo/demo/SKILL.md", b"content")
        limits = installer.Limits(
            max_download_bytes=10 * 1024 * 1024,
            max_entries=2,
            max_unpacked_bytes=10 * 1024 * 1024,
            max_compression_ratio=100.0,
        )
        with zipfile.ZipFile(archive) as handle:
            with self.assertRaisesRegex(installer.InstallError, "entries"):
                installer._validated_zip_entries(handle, limits)

    def test_archive_rejects_control_and_non_nfc_names(self) -> None:
        for member in ("repo/bad\nname", "repo/cafe\u0301.txt"):
            with self.subTest(member=member):
                with self.assertRaises(installer.InstallError):
                    installer._safe_archive_name(member)

    def test_archive_rejects_excessive_compression_ratio(self) -> None:
        archive = self.root / "ratio.zip"
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as handle:
            handle.writestr("repo/large.txt", b"0" * 1024 * 1024)
        strict_ratio = installer.Limits(
            max_download_bytes=10 * 1024 * 1024,
            max_entries=100,
            max_unpacked_bytes=10 * 1024 * 1024,
            max_compression_ratio=10.0,
        )
        with zipfile.ZipFile(archive) as handle:
            with self.assertRaises(installer.InstallError):
                installer._validated_zip_entries(handle, strict_ratio)

    def test_nonfinite_compression_ratio_is_rejected(self) -> None:
        with self.assertRaisesRegex(installer.InstallError, "finite"):
            installer._limits(installer.Args(max_compression_ratio=float("nan")))

    def test_skill_requires_matching_nonempty_frontmatter(self) -> None:
        repo = self.root / "repo"
        skill = self._skill(repo, "valid-name")
        installer._validate_skill(str(skill), str(repo), "valid-name", self.limits)
        text = (skill / "SKILL.md").read_text(encoding="utf-8")
        (skill / "SKILL.md").write_text(
            text.replace("name: valid-name", "name: other-name"), encoding="utf-8"
        )
        with self.assertRaises(installer.InstallError):
            installer._validate_skill(str(skill), str(repo), "valid-name", self.limits)

    def test_git_tree_entry_limit_counts_directories(self) -> None:
        repo = self.root / "entry-repo"
        skill = self._skill(repo, "entry-skill")
        (skill / "empty-directory").mkdir()
        limits = installer.Limits(
            max_download_bytes=10 * 1024 * 1024,
            max_entries=1,
            max_unpacked_bytes=10 * 1024 * 1024,
            max_compression_ratio=100.0,
        )
        with self.assertRaisesRegex(installer.InstallError, "entries"):
            installer._validate_skill(str(skill), str(repo), "entry-skill", limits)

    def test_git_tree_rejects_nonportable_names(self) -> None:
        repo = self.root / "portable-repo"
        skill = self._skill(repo, "portable-skill")
        (skill / "cafe\u0301.txt").write_text("bad", encoding="utf-8")
        with self.assertRaises(installer.InstallError):
            installer._validate_skill(str(skill), str(repo), "portable-skill", self.limits)

    def test_selected_path_rejects_symlink_ancestor(self) -> None:
        repo = self.root / "link-repo"
        target = repo / "real-parent"
        skill = self._skill(target, "linked-skill")
        alias = repo / "alias"
        try:
            alias.symlink_to(target, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"directory symlinks are unavailable: {exc}")
        with self.assertRaisesRegex(installer.InstallError, "links or junctions"):
            installer._validate_skill(
                str(alias / skill.name), str(repo), "linked-skill", self.limits
            )

    def test_hash_mismatch_is_rejected_before_extraction(self) -> None:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as handle:
            handle.writestr("repo/demo/SKILL.md", "content")
        with mock.patch.object(installer, "_request", return_value=buffer.getvalue()):
            with self.assertRaises(installer.InstallError):
                installer._download_repo_zip(
                    "owner",
                    "repo",
                    "commit",
                    str(self.root),
                    self.limits,
                    "0" * 64,
                )
        self.assertFalse((self.root / "repo").exists())

    def test_matching_hash_extracts_archive(self) -> None:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as handle:
            handle.writestr(
                "repo-commit/demo-skill/SKILL.md",
                "---\nname: demo-skill\ndescription: Demo installer skill.\n---\n\n# Demo\n",
            )
        payload = buffer.getvalue()
        digest = hashlib.sha256(payload).hexdigest()
        with mock.patch.object(installer, "_request", return_value=payload):
            prepared = installer._download_repo_zip(
                "owner", "repo", "commit", str(self.root), self.limits, digest
            )
        self.assertEqual("download", prepared.method)
        self.assertEqual(digest, prepared.revision)
        self.assertTrue(Path(prepared.root, "demo-skill", "SKILL.md").is_file())

    def test_batch_copy_rolls_back_after_commit_failure(self) -> None:
        sources = self.root / "sources"
        first = self._skill(sources, "first-skill")
        second = self._skill(sources, "second-skill")
        destination = self.root / "installed"
        plans = [
            (str(first), str(destination / "first-skill")),
            (str(second), str(destination / "second-skill")),
        ]
        real_copytree = installer.shutil.copytree

        def fail_second(source: str, target: str, *args, **kwargs):
            if Path(target) == destination / "second-skill":
                raise OSError("injected commit failure")
            return real_copytree(source, target, *args, **kwargs)

        with mock.patch.object(installer.shutil, "copytree", side_effect=fail_second):
            with self.assertRaises(installer.InstallError):
                installer._copy_skills_transactionally(plans)
        self.assertFalse((destination / "first-skill").exists())
        self.assertFalse((destination / "second-skill").exists())
        self.assertEqual([], list(destination.glob(".*-install-*")))

    def test_copy_refuses_dangling_destination_symlink(self) -> None:
        source = self._skill(self.root / "sources", "linked-destination")
        destination_root = self.root / "destinations"
        destination_root.mkdir()
        destination = destination_root / "linked-destination"
        try:
            destination.symlink_to(destination_root / "missing", target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"directory symlinks are unavailable: {exc}")

        with self.assertRaisesRegex(installer.InstallError, "already exists"):
            installer._copy_skills_transactionally([(str(source), str(destination))])
        self.assertTrue(installer.os.path.lexists(destination))

    def test_incomplete_rollback_is_reported(self) -> None:
        source = self._skill(self.root / "rollback-sources", "rollback-skill")
        destination = self.root / "rollback-dest" / "rollback-skill"
        real_copytree = installer.shutil.copytree
        real_rmtree = installer.shutil.rmtree

        def fail_final_copy(source_path: str, target: str, *args, **kwargs):
            if Path(target) == destination:
                raise OSError("injected copy failure")
            return real_copytree(source_path, target, *args, **kwargs)

        def fail_destination_cleanup(path: str, *args, **kwargs):
            if Path(path) == destination:
                raise OSError("injected rollback failure")
            return real_rmtree(path, *args, **kwargs)

        with mock.patch.object(installer.shutil, "copytree", side_effect=fail_final_copy):
            with mock.patch.object(
                installer.shutil, "rmtree", side_effect=fail_destination_cleanup
            ):
                with self.assertRaisesRegex(installer.InstallError, "Rollback incomplete"):
                    installer._copy_skills_transactionally(
                        [(str(source), str(destination))]
                    )

    def test_git_checkout_fetches_requested_commit_to_fetch_head(self) -> None:
        commit = "a" * 40
        calls: list[list[str]] = []

        def fake_git(arguments: list[str], *, capture: bool = False) -> str:
            calls.append(arguments)
            return commit if capture else ""

        with mock.patch.object(installer, "_run_git", side_effect=fake_git):
            prepared = installer._git_sparse_checkout(
                "https://github.com/owner/repo.git",
                commit,
                ["skills/demo"],
                str(self.root / "checkout"),
            )
        self.assertEqual(commit, prepared.revision)
        self.assertTrue(any("fetch" in call and commit in call for call in calls))
        self.assertTrue(
            any(call[-3:] == ["checkout", "--detach", "FETCH_HEAD"] for call in calls)
        )

    def test_git_does_not_mask_invalid_ref_with_ssh_retry(self) -> None:
        source = installer.Source("owner", "repo", "missing", ["skills/demo"])
        args = installer.Args(repo="owner/repo", path=["skills/demo"], method="git")
        with mock.patch.object(
            installer,
            "_git_sparse_checkout",
            side_effect=installer.InstallError("fatal: couldn't find remote ref missing"),
        ) as checkout:
            with self.assertRaisesRegex(installer.InstallError, "remote ref"):
                installer._prepare_repo(source, args, self.limits, str(self.root))
        self.assertEqual(1, checkout.call_count)

    def test_git_reports_both_https_and_ssh_auth_failures(self) -> None:
        source = installer.Source("owner", "repo", "main", ["skills/demo"])
        args = installer.Args(repo="owner/repo", path=["skills/demo"], method="git")
        with mock.patch.object(
            installer,
            "_git_sparse_checkout",
            side_effect=[
                installer.InstallError("Authentication failed over HTTPS"),
                installer.InstallError("Permission denied (publickey)"),
            ],
        ) as checkout:
            with self.assertRaises(installer.InstallError) as raised:
                installer._prepare_repo(source, args, self.limits, str(self.root))
        self.assertEqual(2, checkout.call_count)
        self.assertIn("Git HTTPS", str(raised.exception))
        self.assertIn("Git SSH", str(raised.exception))

    def test_github_url_and_api_components_are_encoded(self) -> None:
        owner, repo, ref, path = installer._parse_github_url(
            "https://github.com/owner/repo/tree/feature%2Ftopic/skills/demo%20skill",
            "main",
        )
        self.assertEqual(("owner", "repo", "feature/topic", "skills/demo skill"), (owner, repo, ref, path))
        self.assertEqual(
            "https://api.github.com/repos/owner/repo/contents/skills/demo%20skill?ref=feature%2Ftopic",
            github_utils.github_api_contents_url(
                "owner/repo", "skills/demo skill", "feature/topic"
            ),
        )

    def test_legacy_name_option_is_an_expectation_alias(self) -> None:
        args = installer._parse_args(
            ["--repo", "owner/repo", "--path", "skills/demo", "--name", "demo"]
        )
        self.assertEqual("demo", args.expected_name)

    def test_main_installs_validated_skill_and_reports_revision(self) -> None:
        repo = self.root / "repo-root"
        skill = self._skill(repo / "skills", "demo-skill")
        destination = self.root / "destination"
        prepared = installer.PreparedRepo(str(repo), "download", "a" * 64)
        with mock.patch.object(installer, "_prepare_repo", return_value=prepared):
            output = io.StringIO()
            with redirect_stdout(output):
                result = installer.main(
                    [
                        "--repo",
                        "owner/repo",
                        "--path",
                        "skills/demo-skill",
                        "--dest",
                        str(destination),
                    ]
                )
        self.assertEqual(0, result)
        self.assertTrue((destination / "demo-skill" / "SKILL.md").is_file())
        self.assertIn("owner/repo@main via download", output.getvalue())
        self.assertIn("a" * 64, output.getvalue())


if __name__ == "__main__":
    unittest.main()
