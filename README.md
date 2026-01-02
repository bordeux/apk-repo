# APK Repository Generator

Generate an Alpine APK repository from GitHub releases containing `.apk` packages. Host your own Alpine Linux package repository on GitHub Pages.

## How It Works

1. Define projects in `projects.yaml` pointing to GitHub repositories that release `.apk` files
2. GitHub Actions fetches releases, downloads `.apk` packages, and generates APKINDEX
3. The repository is deployed to GitHub Pages
4. Users can add your repository to their Alpine system and install packages with `apk`

## Quick Start

### 1. Configure Projects

Edit `projects.yaml` to add GitHub repositories:

```yaml
settings:
  baseurl: https://bordeux.github.io/apk-repo
  architectures:
    - x86_64
    - aarch64
  key_name: alpine@bordeux

projects:
  - repo: bordeux/tmpltool
    keep_versions: 1        # Keep 1 previous version
```

### 2. Enable GitHub Pages

1. Go to repository **Settings** > **Pages**
2. Set **Source** to "Deploy from a branch"
3. Select **gh-pages** branch

### 3. (Optional) Set Up RSA Signing

Alpine uses RSA keys (not GPG) for signing:

```bash
# Generate RSA key pair
openssl genrsa -out alpine@bordeux.rsa 4096
openssl rsa -in alpine@bordeux.rsa -pubout -out alpine@bordeux.rsa.pub
```

Then in GitHub:
1. Add secret `APK_PRIVATE_KEY` with the private key content
2. Add variable `APK_SIGNING_KEY` = `true`

### 4. Trigger the Workflow

Go to **Actions** > **Update APK Repository** > **Run workflow**

## Using the Repository

Once deployed, users can add your repository:

```bash
# Add public key (if signed)
wget -O /etc/apk/keys/alpine@bordeux.rsa.pub https://bordeux.github.io/apk-repo/keys/alpine@bordeux.rsa.pub

# Add repository
echo "https://bordeux.github.io/apk-repo" >> /etc/apk/repositories

# Install packages
apk update
apk add tmpltool
```

Or for a specific architecture:

```bash
echo "https://bordeux.github.io/apk-repo/x86_64" >> /etc/apk/repositories
```

## Configuration Reference

### projects.yaml

```yaml
settings:
  baseurl: https://bordeux.github.io/apk-repo  # Repository URL
  architectures:                                # Supported architectures
    - x86_64
    - aarch64
  description: "Bordeux Alpine Packages"        # Repository description
  key_name: alpine@bordeux                      # Key name prefix

projects:
  - repo: bordeux/tmpltool                      # GitHub repository (required)
    name: tmpltool                              # Package name override
    description: "Description"                  # Description override
    keep_versions: 1                            # Past versions to keep
    asset_pattern: ".*x86_64.*"                 # Regex to filter .apk assets
```

### Command Line

```bash
# Set up virtual environment
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Generate repository locally
python scripts/generate_repo.py --output repo

# Process specific project
python scripts/generate_repo.py --project bordeux/tmpltool

# Dry run (no downloads)
python scripts/generate_repo.py --dry-run

# List configured projects
python scripts/generate_repo.py --list

# With RSA signing
python scripts/generate_repo.py --private-key /path/to/key.rsa

# Skip signing
python scripts/generate_repo.py --no-sign
```

## Repository Structure

Generated repository layout:

```
apk-repo/
├── x86_64/
│   ├── APKINDEX.tar.gz      (signed package index)
│   └── *.apk                (packages)
├── aarch64/
│   ├── APKINDEX.tar.gz
│   └── *.apk
├── keys/
│   └── alpine@bordeux.rsa.pub  (public key for verification)
└── packages.json            (manifest for incremental updates)
```

When signed, the `APKINDEX.tar.gz` contains an embedded `.SIGN.RSA.<keyname>.rsa.pub` entry as required by Alpine's APK format.

## Requirements

- Python 3.9+
- PyYAML
- `apk` (optional, for native APKINDEX generation)
- `openssl` (optional, for signing)

### Running on Alpine

The GitHub Actions workflow runs in an Alpine container, so `apk` tools are available natively.

For local development on non-Alpine systems, the script falls back to generating APKINDEX manually in Python.

## Signing Keys

Alpine uses RSA keys instead of GPG:

```bash
# Generate new key pair
openssl genrsa -out alpine@bordeux.rsa 4096
openssl rsa -in alpine@bordeux.rsa -pubout -out alpine@bordeux.rsa.pub

# Or using abuild-keygen (on Alpine)
abuild-keygen -a -n
```

The public key (`.rsa.pub`) is distributed with the repository. Users must add it to `/etc/apk/keys/` before installing packages.

## License

MIT
