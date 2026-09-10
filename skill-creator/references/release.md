# Codex Skill Release Hardening

Use this workflow when a skill will be distributed, reused by a team, or trusted for higher-impact work. It adds assurance without changing the ordinary authoring path.

## Release gate

1. Resolve the exact skill directory and confirm its folder matches the frontmatter `name`.
2. Remove unfinished examples, caches, generated output, local secrets, and unrelated files.
3. Run changed scripts on representative success and failure inputs.
4. Run deep strict validation.
5. Review retained behavioral evidence when the skill warrants evaluation.
6. Build the package outside the source directory.
7. Verify the final archive independently and retain its SHA-256 digest.

Before external redistribution, inventory third-party or user-supplied components and verify that their licenses permit the intended channel. A root license does not automatically relicense adapted files. Record exceptions in the release decision rather than omitting provenance.

```powershell
python scripts/validate_skill.py <skill-directory> --strict
python scripts/package_skill.py <skill-directory> --strict --output <dist/skill-name.skill>
python scripts/verify_package.py <dist/skill-name.skill> --json
```

The packager writes files in a stable order with fixed timestamps, normalizes `.py` and `.sh` files under `scripts/` to mode `0755` and other files to `0644`, adds `PACKAGE-MANIFEST.json`, verifies every recorded size and SHA-256 hash, and only then replaces the requested output. The verifier reads without extracting and rejects path traversal, duplicate names, symbolic links, encryption, multiple roots, suspicious compression ratios, excessive expanded size, manifest mismatches, and packaged Markdown links whose targets are absent.

## Package exclusions

Generated caches, version-control data, existing `.skill` archives, and `dist/` directories are excluded automatically. Add a root `.skillignore` for source files that should remain outside the release:

```gitignore
# Relative glob patterns; negation is intentionally unsupported.
tests/fixtures/private-*
notes/
*.local
```

Patterns must be relative and cannot escape the skill directory. Keep `.skillignore` itself in the package so exclusions remain auditable.

## CI template

`assets/skill-ci.yml` is a reusable GitHub Actions starting point. Copy it to `.github/workflows/skill-ci.yml` in the target repository, then set `SKILL_CREATOR_PATH` and `SKILL_PATH` to repository-relative paths.

The template uses read-only repository permissions, immutable action commits, an exact PyYAML dependency, strict validation, deterministic packaging, independent verification, and a rebuild hash comparison. It deliberately does not run model evaluations: credentials, cost limits, model choice, and authorization must be configured explicitly for the target environment.

Do not interpolate pull-request titles, branch names, issue bodies, or other untrusted event values into shell commands. Pass required values through fixed workflow configuration or environment variables and quote them.

## Distribution and trust

A `.skill` file is a ZIP-format transport artifact, not a signature or sandbox. Its manifest detects corruption after packaging but does not identify the author. Before installing third-party skills:

- obtain them from a source whose ownership and revision you can verify;
- pin or record the repository revision;
- inspect `SKILL.md`, scripts, dependencies, and requested tools;
- verify the archive hash through an authenticated channel when one is provided;
- preserve normal approval boundaries for external or destructive actions.

Use a Codex plugin rather than loose copied folders when the surrounding distribution, dependencies, or lifecycle needs to be managed as one product.

This enhanced build intentionally keeps the system skill name `skill-creator`. Deploy it as a documented replacement and do not leave another active skill with the same name in the same runtime.

The bundled evaluation viewer was authored by the project owner and is distributed under the skill's Apache-2.0 license. Its dedicated `eval-viewer/README.md` records that provenance.
