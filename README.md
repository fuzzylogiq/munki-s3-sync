# munki-s3-sync

A complete toolkit for managing [Munki](https://github.com/munki/munki) software repositories with AWS S3 as the binary storage backend. Includes content-addressable package sync, automated [AutoPkg](https://github.com/autopkg/autopkg) runs, and time-based catalog promotion.

## Why?

Munki repos can contain hundreds of gigabytes of installer packages. Tracking these binaries in git is impractical, and manually syncing them is error-prone. This toolkit solves the problem by:

- **Storing packages by SHA-256 hash** in S3 (content-addressable storage), so identical files are never duplicated and name collisions are impossible
- **Using git hooks** to automatically upload/download packages when you push/pull pkgsinfo changes
- **Running AutoPkg on a schedule** via GitHub Actions, with automatic branch creation, PR filing, and Slack notifications
- **Promoting packages between catalogs** (e.g. `earlyaccess` → `production`) based on configurable time delays

## How it works

### Content-addressable storage

When a package is uploaded, its SHA-256 hash is computed and used as the S3 key:

```
<hash[0]>/<hash[1:3]>/<full_hash>
```

For example, a file with hash `a1b2c3d4...` is stored at `a/1b/a1b2c3d4...`. This sharding avoids flat-directory performance issues in S3.

The hash is already recorded in Munki's pkgsinfo plists (`installer_item_hash`), so the sync script reads pkgsinfo files to know what to upload/download without any additional tracking.

### Catalog promotion

Packages can be automatically promoted between catalogs using a custom `_autopromotion_catalogs` key in pkgsinfo plists:

```xml
<key>_autopromotion_catalogs</key>
<dict>
    <key>3</key>
    <array>
        <string>production</string>
    </array>
</dict>
```

This promotes the package to `production` 3 days after its `_metadata.creation_date`. The `auto_promotion.py` script checks all pkgsinfo files and creates a PR with any promotions.

## Setup

### AWS infrastructure

A Terraform template is included to provision all required AWS resources:

```bash
cd terraform
cp terraform.tfvars.example terraform.tfvars
# Edit terraform.tfvars with your values
terraform init
terraform apply
```

This creates:

- **Storage S3 bucket** — versioned, encrypted, for content-addressable packages
- **Repo S3 bucket** — encrypted, serves the built Munki repo via CloudFront
- **CloudFront distribution** — HTTPS-only, fronting the repo bucket
- **GitHub OIDC provider + IAM role** — scoped to your repo, with least-privilege S3 policies

After applying, set your GitHub Actions secrets from the Terraform outputs:

| Output | GitHub Secret |
|--------|---------------|
| `github_actions_role_arn` | `AWS_ROLE_ARN` |
| `storage_bucket_name` | `S3_STORAGE_BUCKET` |
| `repo_bucket_name` | `S3_REPO_BUCKET` |

Point your Munki clients at the `cloudfront_distribution_domain` output.

### Prerequisites

- Python 3.13+
- [uv](https://github.com/astral-sh/uv) (Python package manager)
- AWS CLI configured with access to your S3 bucket
- [Munki tools](https://github.com/munki/munki) installed (for `makecatalogs`)
- [AutoPkg](https://github.com/autopkg/autopkg) installed (for automated recipe runs)

### Quick start

1. **Clone and install dependencies:**
   ```bash
   git clone <this-repo>
   cd munki-s3-sync
   uv sync
   ```

2. **Set up git hooks:**
   ```bash
   git config core.hooksPath githooks
   chmod +x githooks/*
   ```

3. **Configure environment variables** (add to your shell profile):
   ```bash
   export AWS_PROFILE="your-aws-profile"
   export S3_BUCKET="your-munki-storage-bucket"
   ```

4. **Initial sync** (download all packages from S3):
   ```bash
   uv run bin/pkg_sync.py -m download -r "$(pwd)/munki_repo" -b "$S3_BUCKET"
   ```

### GitHub Actions secrets

For the CI workflows, configure these repository secrets:

| Secret | Description |
|--------|-------------|
| `AWS_ROLE_ARN` | IAM role ARN for OIDC authentication |
| `S3_STORAGE_BUCKET` | S3 bucket for content-addressable pkg storage |
| `S3_REPO_BUCKET` | S3 bucket serving the built Munki repo (e.g. behind CloudFront) |
| `GH_TOKEN` | GitHub PAT with repo permissions (for branch/PR creation) |
| `SLACK_WEBHOOK` | *(optional)* Slack incoming webhook URL for notifications |

Optionally set the `AWS_REGION` repository variable (defaults to `us-east-1`).

## Usage

### Manual package sync

```bash
# Upload packages referenced in pkgsinfo but missing from S3
uv run bin/pkg_sync.py -m upload -r /path/to/munki_repo -b your-bucket

# Download packages referenced in pkgsinfo but missing locally
uv run bin/pkg_sync.py -m download -r /path/to/munki_repo -b your-bucket

# Sync a specific pkgsinfo file's packages
uv run bin/pkg_sync.py -m download -r /path/to/munki_repo -b your-bucket -f path/to/pkgsinfo/file.plist

# Remove local packages not referenced by any pkgsinfo
uv run bin/pkg_sync.py -m prune -r /path/to/munki_repo
```

### Git hooks (automatic sync)

Once configured, the hooks handle sync transparently:

- **`pre-push`**: If your commits add new pkgsinfo files, uploads their packages to S3 before pushing
- **`post-merge`**: After pulling, downloads any packages referenced by newly added pkgsinfo files
- **`pre-auto-gc`**: Prunes unreferenced local packages during git garbage collection

### GitHub Actions workflows

| Workflow | Trigger | What it does |
|----------|---------|-------------|
| `run-autopkg.yaml` | Weekly (Tuesdays) or manual | Runs AutoPkg recipes, uploads packages, creates PRs, notifies Slack |
| `build-and-sync.yaml` | Push to main | Downloads packages, rebuilds catalogs, syncs built repo to serving S3 bucket |
| `auto-promote.yaml` | Weekdays at 10 AM or manual | Promotes packages between catalogs based on time, creates PR |
| `repo-cleanup.yaml` | Monthly (28th) or manual | Runs `repoclean` to remove old package versions, creates PR |

## Repository layout

```
munki_repo/
  pkgsinfo/       # Package metadata plists (tracked in git)
  manifests/      # Deployment manifests (tracked in git)
  icons/          # Application icons (tracked in git)
  pkgs/           # Binary packages (gitignored, synced via S3)
  catalogs/       # Auto-generated (gitignored, rebuilt by makecatalogs)
  client_resources/
autopkg_overrides/  # AutoPkg recipe overrides
bin/                # Scripts (pkg_sync, autopkg_tools, auto_promotion)
githooks/           # Git hooks for automatic sync
```

## License

Apache 2.0. See [LICENSE](LICENSE).

## Credits

The AutoPkg tooling is based on work originally by Facebook, Inc. and Ada Health GmbH. The content-addressable S3 sync (`pkg_sync.py`) is based on an original idea by Rick Heil.
