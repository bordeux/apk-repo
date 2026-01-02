#!/usr/bin/env python3
"""
APK Repository Generator

Generates an Alpine APK repository from GitHub releases containing .apk packages.
Similar to apt-repo but for Alpine Linux systems.

Supports incremental updates - can update a single project while preserving others.
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Optional
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import yaml


# Architecture patterns for .apk files
ARCH_PATTERNS = {
    "x86_64": [r"x86_64", r"amd64", r"x64"],
    "aarch64": [r"aarch64", r"arm64"],
    "x86": [r"x86[^_]", r"i[36]86"],
    "armv7": [r"armv7", r"armhf"],
    "armhf": [r"armhf"],
    "noarch": [r"noarch"],
}

# Manifest file to track packages by project
MANIFEST_FILE = "packages.json"


@dataclass
class ApkPackage:
    """Represents an .apk package from a GitHub release."""
    name: str
    version: str
    architecture: str
    url: str
    filename: str
    project_repo: str = ""
    size: int = 0
    sha256: str = ""
    checksum: str = ""  # Alpine format: Q1 + base64(sha1)
    # Extracted from .apk
    pkgname: str = ""
    pkgver: str = ""
    pkgdesc: str = ""
    arch: str = ""
    license: str = ""
    origin: str = ""
    maintainer: str = ""
    builddate: str = ""
    commit: str = ""
    provider_priority: str = ""
    provides: str = ""
    depends: str = ""


@dataclass
class Release:
    """Represents a GitHub release."""
    tag: str
    version: str
    major_minor: str
    packages: list[ApkPackage] = field(default_factory=list)


@dataclass
class Project:
    """Project configuration from projects.yaml."""
    repo: str
    name: str = ""
    description: str = ""
    keep_versions: int = 0
    asset_pattern: str = ""

    def __post_init__(self):
        if not self.name:
            self.name = self.repo.split("/")[-1]


@dataclass
class RepoSettings:
    """Repository settings from projects.yaml."""
    baseurl: str = ""
    architectures: list[str] = field(default_factory=lambda: ["x86_64", "aarch64"])
    description: str = "Alpine Packages"
    key_name: str = "alpine@example"


class GitHubAPI:
    """GitHub API client for fetching releases."""

    def __init__(self, token: Optional[str] = None):
        self.token = token or os.environ.get("GITHUB_TOKEN")
        self.base_url = "https://api.github.com"

    def _request(self, endpoint: str) -> dict | list:
        """Make an authenticated request to GitHub API."""
        url = f"{self.base_url}/{endpoint}"
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "apk-repo-generator",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        req = Request(url, headers=headers)
        try:
            with urlopen(req, timeout=30) as response:
                return json.loads(response.read().decode())
        except HTTPError as e:
            if e.code == 404:
                raise RuntimeError(f"Repository or release not found: {endpoint}")
            elif e.code == 403:
                raise RuntimeError(
                    f"Rate limit exceeded. Set GITHUB_TOKEN env var for higher limits."
                )
            raise

    def get_repo(self, repo: str) -> dict:
        """Get repository metadata."""
        return self._request(f"repos/{repo}")

    def get_releases(self, repo: str, per_page: int = 30) -> list[dict]:
        """Get releases for a repository."""
        return self._request(f"repos/{repo}/releases?per_page={per_page}")

    def get_latest_release(self, repo: str) -> dict:
        """Get the latest release."""
        return self._request(f"repos/{repo}/releases/latest")


def extract_version(tag: str) -> str:
    """Extract version number from tag (removes 'v' prefix)."""
    return tag.lstrip("vV")


def extract_major_minor(version: str) -> str:
    """Extract major.minor from version string."""
    parts = version.split(".")
    if len(parts) >= 2:
        return f"{parts[0]}.{parts[1]}"
    return version


def detect_architecture(filename: str) -> Optional[str]:
    """Detect architecture from .apk filename."""
    filename_lower = filename.lower()
    for arch, patterns in ARCH_PATTERNS.items():
        for pattern in patterns:
            if re.search(pattern, filename_lower):
                return arch
    return None


def compute_sha256(filepath: Path) -> str:
    """Compute SHA256 hash of a file."""
    sha256 = hashlib.sha256()
    with open(filepath, "rb") as f:
        while chunk := f.read(8192):
            sha256.update(chunk)
    return sha256.hexdigest()


def compute_apk_checksum(filepath: Path) -> str:
    """Compute APK checksum in Alpine format (Q1 + base64-encoded SHA1)."""
    import base64
    sha1 = hashlib.sha1()
    with open(filepath, "rb") as f:
        while chunk := f.read(8192):
            sha1.update(chunk)
    return "Q1" + base64.b64encode(sha1.digest()).decode("ascii")


def download_file(url: str, dest: Path, token: Optional[str] = None) -> None:
    """Download a file from URL to destination."""
    headers = {"User-Agent": "apk-repo-generator"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    req = Request(url, headers=headers)
    with urlopen(req, timeout=300) as response:
        with open(dest, "wb") as f:
            shutil.copyfileobj(response, f)


def extract_apk_info(apk_path: Path) -> dict:
    """Extract information from an .apk file.

    APK files are gzipped tarballs with a .PKGINFO file containing metadata.
    """
    info = {}
    try:
        with tarfile.open(apk_path, "r:gz") as tar:
            # Look for .PKGINFO file
            for member in tar.getmembers():
                if member.name == ".PKGINFO":
                    f = tar.extractfile(member)
                    if f:
                        content = f.read().decode("utf-8", errors="replace")
                        for line in content.split("\n"):
                            if "=" in line:
                                key, _, value = line.partition("=")
                                key = key.strip()
                                value = value.strip()
                                if key and value:
                                    info[key.lower()] = value
                    break
    except (tarfile.TarError, OSError) as e:
        print(f"    Warning: Could not read .PKGINFO from {apk_path.name}: {e}")

    return info


def find_apk_assets(
    release_data: dict,
    project: Project,
    architectures: list[str],
) -> list[dict]:
    """Find .apk assets in a release matching the project configuration."""
    assets = []

    for asset in release_data.get("assets", []):
        name = asset.get("name", "")

        # Must be an .apk file
        if not name.endswith(".apk"):
            continue

        # Apply custom pattern filter if specified
        if project.asset_pattern:
            if not re.search(project.asset_pattern, name, re.IGNORECASE):
                continue

        # Detect architecture
        arch = detect_architecture(name)
        if arch and arch in architectures:
            assets.append({
                "name": name,
                "url": asset.get("browser_download_url", ""),
                "size": asset.get("size", 0),
                "architecture": arch,
            })

    return assets


def fetch_releases(
    github: GitHubAPI,
    project: Project,
    settings: RepoSettings,
) -> list[Release]:
    """Fetch and process releases for a project."""
    releases_data = github.get_releases(project.repo)

    # Fetch description from GitHub if not provided
    description = project.description
    if not description:
        try:
            repo_info = github.get_repo(project.repo)
            description = repo_info.get("description") or f"{project.name} from GitHub"
        except Exception:
            description = f"{project.name} from GitHub"

    # Group releases by major.minor version
    releases_by_minor: dict[str, dict] = {}

    for release_data in releases_data:
        # Skip pre-releases and drafts
        if release_data.get("prerelease") or release_data.get("draft"):
            continue

        tag = release_data["tag_name"]
        version = extract_version(tag)
        major_minor = extract_major_minor(version)

        # Keep only the first (latest) release for each major.minor
        if major_minor not in releases_by_minor:
            releases_by_minor[major_minor] = release_data

    # Sort by version (newest first)
    sorted_versions = sorted(
        releases_by_minor.keys(),
        key=lambda v: [int(x) if x.isdigit() else 0 for x in v.split(".")],
        reverse=True,
    )

    # Determine how many versions to keep
    if project.keep_versions > 0:
        versions_to_keep = sorted_versions[: project.keep_versions + 1]
    else:
        versions_to_keep = sorted_versions[:1]  # Only latest

    releases = []
    for major_minor in versions_to_keep:
        release_data = releases_by_minor[major_minor]
        tag = release_data["tag_name"]
        version = extract_version(tag)

        # Find .apk assets
        apk_assets = find_apk_assets(release_data, project, settings.architectures)

        if apk_assets:
            release = Release(
                tag=tag,
                version=version,
                major_minor=major_minor,
            )

            for asset in apk_assets:
                package = ApkPackage(
                    name=project.name,
                    version=version,
                    architecture=asset["architecture"],
                    url=asset["url"],
                    filename=asset["name"],
                    size=asset["size"],
                    project_repo=project.repo,
                    pkgdesc=description,
                )
                release.packages.append(package)

            releases.append(release)

    return releases


def load_manifest(output_dir: Path) -> dict[str, list[dict]]:
    """Load existing packages manifest."""
    manifest_path = output_dir / MANIFEST_FILE
    if manifest_path.exists():
        with open(manifest_path) as f:
            return json.load(f)
    return {"packages": []}


def save_manifest(output_dir: Path, packages: list[ApkPackage]) -> None:
    """Save packages manifest."""
    manifest_path = output_dir / MANIFEST_FILE
    data = {
        "packages": [asdict(pkg) for pkg in packages],
        "updated": datetime.now(timezone.utc).isoformat(),
    }
    with open(manifest_path, "w") as f:
        json.dump(data, f, indent=2)


def generate_apkindex_entry(pkg: ApkPackage) -> str:
    """Generate an APKINDEX entry for a package."""
    entry = f"""C:{pkg.checksum}
P:{pkg.pkgname or pkg.name}
V:{pkg.pkgver or pkg.version}
A:{pkg.arch or pkg.architecture}
S:{pkg.size}
I:{pkg.size}
T:{pkg.pkgdesc}
U:{f"https://github.com/{pkg.project_repo}" if pkg.project_repo else ""}
L:{pkg.license or "MIT"}
o:{pkg.origin or pkg.pkgname or pkg.name}
m:{pkg.maintainer or "Unknown"}
t:{pkg.builddate or int(datetime.now().timestamp())}
"""
    if pkg.depends:
        entry += f"D:{pkg.depends}\n"
    if pkg.provides:
        entry += f"p:{pkg.provides}\n"

    return entry


def generate_apkindex(arch_dir: Path, packages: list[ApkPackage], key_path: Optional[Path] = None) -> bool:
    """Generate APKINDEX.tar.gz for the given architecture directory."""
    apk_files = list(arch_dir.glob("*.apk"))
    if not apk_files:
        return False

    # Try using apk index command first (without signing - we sign separately)
    try:
        cmd = ["apk", "index", "-o", str(arch_dir / "APKINDEX.tar.gz")]
        cmd.extend([str(f) for f in apk_files])

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode == 0:
            return True
        print(f"    apk index not available, using fallback")
    except (subprocess.SubprocessError, FileNotFoundError):
        pass

    # Fallback: generate APKINDEX manually
    print("    Generating APKINDEX manually...")

    # Build APKINDEX content
    index_content = ""
    for pkg in packages:
        if pkg.architecture == arch_dir.name:
            index_content += generate_apkindex_entry(pkg) + "\n"

    if not index_content:
        return False

    # Create APKINDEX.tar.gz
    apkindex_path = arch_dir / "APKINDEX.tar.gz"
    try:
        with tarfile.open(apkindex_path, "w:gz") as tar:
            # Add APKINDEX file
            data = index_content.encode("utf-8")
            info = tarfile.TarInfo(name="APKINDEX")
            info.size = len(data)
            info.mtime = int(datetime.now().timestamp())
            tar.addfile(info, BytesIO(data))

            # Add DESCRIPTION file
            desc = f"Alpine Package Index\n"
            desc_data = desc.encode("utf-8")
            desc_info = tarfile.TarInfo(name="DESCRIPTION")
            desc_info.size = len(desc_data)
            desc_info.mtime = int(datetime.now().timestamp())
            tar.addfile(desc_info, BytesIO(desc_data))

        return True
    except Exception as e:
        print(f"    Error creating APKINDEX: {e}")
        return False


def sign_index(apkindex_path: Path, private_key_path: Path, key_name: str) -> bool:
    """Sign APKINDEX.tar.gz with RSA private key.

    Alpine APK format uses concatenated gzip streams:
    1. First gzip: tar with .SIGN.RSA.<keyname>.rsa.pub (signature)
    2. Second gzip: original unsigned APKINDEX.tar.gz

    The signature is RSA-SHA1 over the original (unsigned) gzipped tarball.
    """
    if not private_key_path.exists():
        return False

    try:
        import gzip

        # Read the original gzipped tarball - this is what we sign
        with open(apkindex_path, "rb") as f:
            original_gz_data = f.read()

        # Sign the original gzipped tarball
        with tempfile.NamedTemporaryFile(delete=False) as data_tmp:
            data_tmp.write(original_gz_data)
            data_tmp_path = data_tmp.name

        with tempfile.NamedTemporaryFile(delete=False) as sig_tmp:
            sig_tmp_path = sig_tmp.name

        try:
            result = subprocess.run(
                [
                    "openssl", "dgst", "-sha1",
                    "-sign", str(private_key_path),
                    "-out", sig_tmp_path,
                    data_tmp_path
                ],
                capture_output=True,
                timeout=60,
            )

            if result.returncode != 0:
                print(f"    Signing failed: {result.stderr.decode()}")
                return False

            # Read the signature
            with open(sig_tmp_path, "rb") as f:
                signature_data = f.read()

        finally:
            os.unlink(data_tmp_path)
            os.unlink(sig_tmp_path)

        # Create signature tar (without end-of-archive markers for APK format)
        sig_name = f".SIGN.RSA.{key_name}.rsa.pub"
        sig_tar_io = BytesIO()
        with tarfile.open(fileobj=sig_tar_io, mode="w") as tar:
            sig_info = tarfile.TarInfo(name=sig_name)
            sig_info.size = len(signature_data)
            sig_info.mtime = int(datetime.now().timestamp())
            tar.addfile(sig_info, BytesIO(signature_data))
        sig_tar_data = sig_tar_io.getvalue()

        # Strip end-of-archive markers (two 512-byte null blocks)
        while len(sig_tar_data) > 1024 and sig_tar_data[-512:] == b'\x00' * 512:
            sig_tar_data = sig_tar_data[:-512]

        # Gzip the signature tar
        sig_gz_io = BytesIO()
        with gzip.GzipFile(fileobj=sig_gz_io, mode="wb", mtime=0) as gz:
            gz.write(sig_tar_data)
        sig_gz_data = sig_gz_io.getvalue()

        # Write: [signature gzip] + [original unsigned gzip]
        with open(apkindex_path, "wb") as f:
            f.write(sig_gz_data)
            f.write(original_gz_data)

        return True

    except (subprocess.SubprocessError, FileNotFoundError) as e:
        print(f"    openssl not available: {e}")
    except Exception as e:
        print(f"    Error during signing: {e}")

    return False


def export_public_key(private_key_path: Path, public_key_path: Path) -> bool:
    """Export public key from private key."""
    if not private_key_path.exists():
        return False

    try:
        result = subprocess.run(
            [
                "openssl", "rsa",
                "-in", str(private_key_path),
                "-pubout",
                "-out", str(public_key_path)
            ],
            capture_output=True,
            timeout=60,
        )
        return result.returncode == 0
    except (subprocess.SubprocessError, FileNotFoundError):
        return False


def load_config(config_path: Path) -> tuple[RepoSettings, list[Project]]:
    """Load configuration from projects.yaml."""
    with open(config_path) as f:
        data = yaml.safe_load(f)

    settings_data = data.get("settings", {})
    settings = RepoSettings(
        baseurl=settings_data.get("baseurl", ""),
        architectures=settings_data.get("architectures", ["x86_64", "aarch64"]),
        description=settings_data.get("description", "Alpine Packages"),
        key_name=settings_data.get("key_name", "alpine@example"),
    )

    projects = []
    for proj_data in data.get("projects", []):
        projects.append(Project(
            repo=proj_data["repo"],
            name=proj_data.get("name", ""),
            description=proj_data.get("description", ""),
            keep_versions=proj_data.get("keep_versions", 0),
            asset_pattern=proj_data.get("asset_pattern", ""),
        ))

    return settings, projects


def cleanup_old_packages(
    output_dir: Path,
    all_packages: list[ApkPackage],
    architectures: list[str],
) -> list[Path]:
    """Remove .apk files that are no longer in the manifest."""
    removed = []
    current_filenames = {pkg.filename for pkg in all_packages}

    for arch in architectures:
        arch_dir = output_dir / arch
        if arch_dir.exists():
            for apk_file in arch_dir.glob("*.apk"):
                if apk_file.name not in current_filenames:
                    apk_file.unlink()
                    removed.append(apk_file)

    return removed


def main():
    parser = argparse.ArgumentParser(
        description="Generate Alpine APK repository from GitHub releases"
    )
    parser.add_argument(
        "--config", "-c",
        type=Path,
        default=Path("projects.yaml"),
        help="Path to projects.yaml config file",
    )
    parser.add_argument(
        "--output", "-o",
        type=Path,
        default=Path("repo"),
        help="Output directory for repository (usually gh-pages checkout)",
    )
    parser.add_argument(
        "--project", "-p",
        type=str,
        help="Process only specific project (name or owner/repo)",
    )
    parser.add_argument(
        "--list", "-l",
        action="store_true",
        help="List configured projects and exit",
    )
    parser.add_argument(
        "--dry-run", "-n",
        action="store_true",
        help="Show what would be done without downloading",
    )
    parser.add_argument(
        "--private-key", "-k",
        type=Path,
        help="RSA private key for signing (optional)",
    )
    parser.add_argument(
        "--no-sign",
        action="store_true",
        help="Skip signing",
    )

    args = parser.parse_args()

    # Load configuration
    settings, all_projects = load_config(args.config)

    # List mode
    if args.list:
        print("Configured projects:")
        for proj in all_projects:
            print(f"  - {proj.repo} (name: {proj.name}, keep_versions: {proj.keep_versions})")
        return

    # Determine which projects to process
    if args.project:
        projects_to_process = [
            p for p in all_projects
            if p.name == args.project or p.repo == args.project
        ]
        if not projects_to_process:
            print(f"Error: Project '{args.project}' not found in config")
            return 1
    else:
        projects_to_process = all_projects

    # Initialize GitHub API client
    github = GitHubAPI()

    output_dir = args.output

    # Load existing manifest (for incremental updates)
    manifest = load_manifest(output_dir)
    existing_packages = [
        ApkPackage(**pkg_data) for pkg_data in manifest.get("packages", [])
    ]

    # Filter out packages from projects we're about to update
    projects_to_update = {p.repo for p in projects_to_process}
    preserved_packages = [
        pkg for pkg in existing_packages
        if pkg.project_repo not in projects_to_update
    ]

    # Create architecture directories
    if not args.dry_run:
        for arch in settings.architectures:
            (output_dir / arch).mkdir(parents=True, exist_ok=True)

    print(f"Processing {len(projects_to_process)} project(s)...")
    if preserved_packages:
        print(f"Preserving {len(preserved_packages)} package(s) from other projects")

    # Collect new packages
    new_packages: list[ApkPackage] = []

    for project in projects_to_process:
        print(f"\n{'='*60}")
        print(f"Project: {project.repo}")
        print(f"{'='*60}")

        try:
            releases = fetch_releases(github, project, settings)

            if not releases:
                print(f"  No releases with .apk packages found")
                continue

            for release in releases:
                print(f"\n  Release: {release.tag} (version {release.version})")

                for pkg in release.packages:
                    print(f"    - {pkg.filename} ({pkg.architecture})")

                    if args.dry_run:
                        continue

                    arch_dir = output_dir / pkg.architecture
                    original_path = arch_dir / pkg.filename

                    # Check if file needs to be downloaded
                    # First check if already exists with expected Alpine name pattern
                    existing_files = list(arch_dir.glob(f"{pkg.name}-{pkg.version}*.apk"))

                    if existing_files:
                        apk_path = existing_files[0]
                        print(f"      Already exists as {apk_path.name}")
                    elif original_path.exists():
                        apk_path = original_path
                    else:
                        print(f"      Downloading...")
                        download_file(pkg.url, original_path, github.token)
                        apk_path = original_path

                    # Extract info from .apk to get correct pkgname and pkgver
                    info = extract_apk_info(apk_path)
                    if info:
                        pkg.pkgname = info.get("pkgname", pkg.name)
                        pkg.pkgver = info.get("pkgver", pkg.version)
                        pkg.pkgdesc = info.get("pkgdesc", pkg.pkgdesc)
                        pkg.arch = info.get("arch", pkg.architecture)
                        pkg.license = info.get("license", "")
                        pkg.origin = info.get("origin", "")
                        pkg.maintainer = info.get("maintainer", "")
                        pkg.builddate = info.get("builddate", "")
                        pkg.depends = info.get("depend", "")
                        pkg.provides = info.get("provides", "")

                    # Rename file to Alpine standard format: {pkgname}-{pkgver}.apk
                    expected_filename = f"{pkg.pkgname}-{pkg.pkgver}.apk"
                    final_path = arch_dir / expected_filename
                    pkg.filename = expected_filename

                    if apk_path != final_path and apk_path.exists():
                        if not final_path.exists():
                            apk_path.rename(final_path)
                            print(f"      Renamed to {expected_filename}")
                        else:
                            # Remove duplicate original
                            apk_path.unlink()
                        apk_path = final_path

                    # Compute hashes
                    pkg.sha256 = compute_sha256(apk_path)
                    pkg.checksum = compute_apk_checksum(apk_path)
                    pkg.size = apk_path.stat().st_size

                    new_packages.append(pkg)

        except Exception as e:
            print(f"  Error processing {project.repo}: {e}")
            continue

    if args.dry_run:
        print("\n[Dry run - no files written]")
        return

    # Combine preserved and new packages
    all_packages = preserved_packages + new_packages

    # Clean up old .apk files no longer needed
    removed = cleanup_old_packages(output_dir, all_packages, settings.architectures)
    if removed:
        print(f"\nRemoved {len(removed)} old package(s)")

    # Save manifest
    save_manifest(output_dir, all_packages)

    print(f"\n{'='*60}")
    print("Generating repository metadata...")
    print(f"{'='*60}")

    # Generate APKINDEX for each architecture
    for arch in settings.architectures:
        arch_dir = output_dir / arch
        arch_packages = [pkg for pkg in all_packages if pkg.architecture == arch]

        if arch_packages:
            print(f"\n  {arch}: {len(arch_packages)} package(s)")
            if generate_apkindex(arch_dir, arch_packages, args.private_key):
                print(f"    Created APKINDEX.tar.gz")

                # Sign if key provided
                if not args.no_sign and args.private_key:
                    apkindex_path = arch_dir / "APKINDEX.tar.gz"
                    if sign_index(apkindex_path, args.private_key, settings.key_name):
                        print(f"    Signed with {settings.key_name}")
            else:
                print(f"    Warning: Could not generate APKINDEX")
        else:
            print(f"\n  {arch}: no packages")

    # Export public key if private key provided
    if not args.no_sign and args.private_key and args.private_key.exists():
        keys_dir = output_dir / "keys"
        keys_dir.mkdir(exist_ok=True)
        pub_key_path = keys_dir / f"{settings.key_name}.rsa.pub"
        if export_public_key(args.private_key, pub_key_path):
            print(f"\n  Exported public key to keys/{settings.key_name}.rsa.pub")

    print(f"\nRepository generated in: {output_dir}")
    print(f"Total packages: {len(all_packages)}")


if __name__ == "__main__":
    exit(main() or 0)
