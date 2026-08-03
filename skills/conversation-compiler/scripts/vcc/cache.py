"""Cache validation, artifact manifests, and atomic filesystem writes."""

import hashlib
import json
import os
import re
import tempfile

from .common import VCCError, VCC_VERSION


def canonical_source_path(input_path):
    """Return one stable local identity for path aliases to the same source."""
    return os.path.normcase(os.path.realpath(os.path.abspath(input_path)))


def default_cache_root():
    """Return the private per-user cache root without touching the filesystem."""
    if os.environ.get("VCC_CACHE_DIR"):
        return os.path.abspath(os.path.expanduser(os.environ["VCC_CACHE_DIR"]))
    if os.environ.get("XDG_CACHE_HOME"):
        return os.path.join(os.path.abspath(os.path.expanduser(
            os.environ["XDG_CACHE_HOME"])), "vcc")
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        return os.path.join(os.path.abspath(os.path.expanduser(
            os.environ["LOCALAPPDATA"])), "VCC", "Cache")
    return os.path.join(os.path.expanduser("~"), ".cache", "vcc")

def cache_output_dir(cache_root, input_path):
    source = canonical_source_path(input_path)
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()[:16]
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", os.path.basename(source)).strip("-")
    return os.path.join(os.path.abspath(cache_root), f"{stem or 'session'}-{digest}")


def prepare_cache_output_dir(cache_root, input_path):
    """Create a private managed cache root and reject unsafe entry indirection."""
    root = os.path.abspath(cache_root)
    os.makedirs(root, mode=0o700, exist_ok=True)
    if not os.path.isdir(root):
        raise VCCError(f"managed cache root is not a directory: {root}")
    try:
        os.chmod(root, 0o700)
    except OSError:
        pass
    output_dir = cache_output_dir(root, input_path)
    if os.path.lexists(output_dir) and (
            os.path.islink(output_dir) or not os.path.isdir(output_dir)):
        raise VCCError(f"managed cache entry is not a real directory: {output_dir}")
    os.makedirs(output_dir, mode=0o700, exist_ok=True)
    try:
        os.chmod(output_dir, 0o700)
    except OSError:
        pass
    return output_dir


def atomic_write_text(path, text):
    """Commit a UTF-8 text artifact atomically within its destination directory."""
    directory = os.path.dirname(os.path.abspath(path))
    fd, temporary = tempfile.mkstemp(prefix=".vcc-write-", dir=directory, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def atomic_write_bytes(path, data):
    """Commit bytes without following a pre-existing destination symlink."""
    directory = os.path.dirname(os.path.abspath(path))
    fd, temporary = tempfile.mkstemp(prefix=".vcc-write-", dir=directory)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _artifact_manifest(output_dir, allowed_names=None):
    manifest = {}
    for name in sorted(os.listdir(output_dir)):
        if allowed_names is not None and name not in allowed_names:
            continue
        path = os.path.join(output_dir, name)
        if name == "metadata.json" or os.path.islink(path) or not os.path.isfile(path):
            continue
        digest = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                digest.update(chunk)
        manifest[name] = {"size": os.path.getsize(path), "sha256": digest.hexdigest()}
    return manifest

def write_cache_metadata(output_dir, input_path, truncate, truncate_user, grep_pattern,
                          artifact_names=None, diagnostics=None, chain_window=0):
    st = os.stat(input_path)
    metadata = {
        "schema_version": 1,
        "vcc_version": VCC_VERSION,
        "source": canonical_source_path(input_path),
        "source_size": st.st_size,
        "source_mtime_ns": st.st_mtime_ns,
        "source_ctime_ns": st.st_ctime_ns,
        "truncate": truncate,
        "truncate_user": truncate_user,
        "chain_window": chain_window,
        "grep": grep_pattern.pattern if grep_pattern else None,
        "artifacts": _artifact_manifest(output_dir, artifact_names),
        "diagnostics": diagnostics or {},
    }
    path = os.path.join(output_dir, "metadata.json")
    atomic_write_text(path, json.dumps(metadata, ensure_ascii=False, indent=2) + "\n")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass

def protect_cache_tree(output_dir):
    """Best-effort private permissions for transcripts that may contain secrets."""
    for root, dirs, files in os.walk(output_dir):
        try:
            os.chmod(root, 0o700)
        except OSError:
            pass
        for name in dirs:
            path = os.path.join(root, name)
            if os.path.islink(path):
                continue
            try:
                os.chmod(path, 0o700)
            except OSError:
                pass
        for name in files:
            path = os.path.join(root, name)
            if os.path.islink(path):
                continue
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass


def cache_is_valid(output_dir, input_path, truncate, truncate_user, chain_window=0):
    """Validate reusable full/brief cache artifacts against source and VCC version."""
    metadata_path = os.path.join(output_dir, "metadata.json")
    try:
        with open(metadata_path, encoding="utf-8") as f:
            metadata = json.load(f)
        st = os.stat(input_path)
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    expected = {
        "schema_version": 1,
        "vcc_version": VCC_VERSION,
        "source": canonical_source_path(input_path),
        "source_size": st.st_size,
        "source_mtime_ns": st.st_mtime_ns,
        "source_ctime_ns": st.st_ctime_ns,
        "truncate": truncate,
        "truncate_user": truncate_user,
        "chain_window": chain_window,
    }
    if any(metadata.get(key) != value for key, value in expected.items()):
        return False
    manifest = metadata.get("artifacts")
    if not isinstance(manifest, dict) or not manifest:
        return False
    try:
        if _artifact_manifest(output_dir, set(manifest)) != manifest:
            return False
    except OSError:
        return False
    names = list(manifest)
    full = [name for name in names if name.endswith(".txt")
            and not name.endswith((".min.txt", ".view.txt"))]
    brief = [name for name in names if name.endswith(".min.txt")]
    return bool(full) and len(full) == len(brief)


def report_cache_hit(output_dir):
    print(f"  cache hit: {output_dir}")
    with open(os.path.join(output_dir, "metadata.json"), encoding="utf-8") as f:
        names = json.load(f).get("artifacts", {})
    for name in sorted(names):
        if name.endswith(".txt") and not name.endswith(".view.txt"):
            print(f"  {os.path.join(output_dir, name)}")


def managed_artifact_names(output_dir):
    try:
        with open(os.path.join(output_dir, "metadata.json"), encoding="utf-8") as f:
            artifacts = json.load(f).get("artifacts", {})
    except (OSError, ValueError, json.JSONDecodeError):
        return set()
    return {name for name in artifacts if os.path.basename(name) == name}


def remove_obsolete_managed_artifacts(output_dir, previous_names, current_names):
    for name in previous_names - current_names:
        try:
            os.unlink(os.path.join(output_dir, name))
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise VCCError(f"cannot remove obsolete cache artifact {name}: {exc}") from exc
