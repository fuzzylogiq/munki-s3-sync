# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

munki-s3-sync is a toolkit for managing Munki software repositories with AWS S3 as a binary storage backend. It provides content-addressable S3 storage for packages, AutoPkg automation, and time-based catalog promotion.

## Running the Tools

All scripts live in `bin/` and are run via `uv run`:

```bash
# Package sync (upload/download/prune)
uv run bin/pkg_sync.py -m upload -r /path/to/munki_repo -b my-s3-bucket
uv run bin/pkg_sync.py -m download -r /path/to/munki_repo -b my-s3-bucket
uv run bin/pkg_sync.py -m prune -r /path/to/munki_repo

# AutoPkg automation
uv run bin/autopkg_tools.py

# Catalog promotion
uv run bin/auto_promotion.py
```

There are no tests or linting configured in this project.

## Architecture

### Two-Bucket Design

The system uses separate S3 buckets for storage and serving:
- **Storage bucket**: Content-addressable by SHA-256 hash (path: `a/1b/a1b2c3d4...`). Used by `pkg_sync.py`.
- **Serving bucket**: Standard Munki repo layout behind CloudFront. Synced via the `build-and-sync` GitHub Actions workflow.

### Core Components

**`bin/pkg_sync.py`** - Content-addressable S3 sync engine. Reads pkginfo plists to discover which installer files are needed, then uploads/downloads/prunes accordingly. Uses concurrent transfers (8 workers) with thread-local boto3 clients. Two-phase operation: scan pkgsinfos first, then transfer.

**`bin/autopkg_tools.py`** - AutoPkg orchestrator. Runs recipes, uploads resulting packages to S3 via `pkg_sync.py`, creates git feature branches and PRs, and sends Slack notifications. Configured entirely through environment variables: `AWS_PROFILE`, `S3_BUCKET`, `GITHUB_WORKSPACE`, `GITHUB_TOKEN`, `OVERRIDES_DIR`, `SLACK_WEBHOOK`.

**`bin/auto_promotion.py`** - Catalog promotion engine. Reads `_autopromotion_catalogs` dict from pkginfo plists (keyed by days since `_metadata.creation_date`) and updates the `catalogs` array when elapsed. Creates a dated branch and PR with all promotions.

**`bin/progress.py`** - Rich-based progress display. `ScanProgress` for the pkgsinfo scan phase, `TransferProgress` for concurrent S3 transfers with per-file progress bars. Falls back to `print()` when stdout is not a TTY (CI).

### Git Hooks (`githooks/`)

- **pre-push**: Uploads new packages to S3 before push
- **post-merge**: Downloads packages from S3 after merge/pull
- **pre-auto-gc**: Prunes unreferenced local binaries

All hooks require `AWS_PROFILE` and `S3_BUCKET` environment variables.

### GitHub Actions Workflows (`.github/workflows/`)

- **run-autopkg.yaml**: Weekly AutoPkg runs (macOS runner, Munki v6.6.4, AutoPkg v2.7.3)
- **build-and-sync.yaml**: Detects pkginfo changes on main, downloads packages, rebuilds catalogs, syncs to serving bucket
- **auto-promote.yaml**: Weekday catalog promotions via `auto_promotion.py`
- **repo-cleanup.yaml**: Monthly `repoclean` keeping 3 most recent versions, auto-merges PR

All workflows use OIDC-based AWS authentication (`AWS_ROLE_ARN` secret).

## Key Conventions

- Package storage paths are derived from SHA-256: first char / next two chars / full hash (e.g., `a/1b/a1b2c3...`)
- pkginfo files are Apple plist format, read/written via `plistlib`
- Promotion rules are stored in pkginfo as `_autopromotion_catalogs` dict mapping day-count strings to catalog arrays
- Python 3.13+ required; dependencies managed by uv via `pyproject.toml`
