#!/usr/bin/env python3
"""Convert a BuildKit/OCI-style image tar into a legacy docker-save archive.

RouterOS container import is sensitive to image archive layout. Newer Docker
Desktop / Buildx setups may emit tarballs with `blobs/sha256/...`, `index.json`
and `oci-layout`, even when `type=docker` is requested. This script rewrites
that archive into the older docker-save style with:

- top-level config JSON
- `manifest.json`
- `repositories`
- one directory per layer containing `VERSION`, `json`, and `layer.tar`
"""

from __future__ import annotations

import argparse
import io
import json
import posixpath
import tarfile
from typing import Any


def _read_json_bytes(tf: tarfile.TarFile, member_name: str) -> Any:
    member = tf.getmember(member_name)
    extracted = tf.extractfile(member)
    if extracted is None:
        raise ValueError(f"missing archive member: {member_name}")
    return json.load(extracted)


def _read_bytes(tf: tarfile.TarFile, member_name: str) -> bytes:
    member = tf.getmember(member_name)
    extracted = tf.extractfile(member)
    if extracted is None:
        raise ValueError(f"missing archive member: {member_name}")
    return extracted.read()


def _add_bytes(tf: tarfile.TarFile, name: str, data: bytes) -> None:
    info = tarfile.TarInfo(name=name)
    info.size = len(data)
    info.mode = 0o644
    tf.addfile(info, io.BytesIO(data))


def _layer_id_from_path(layer_path: str) -> str:
    return posixpath.basename(layer_path)


def convert_archive(src: str, dst: str) -> None:
    with tarfile.open(src, "r:*") as in_tf:
        manifest = _read_json_bytes(in_tf, "manifest.json")
        if not manifest or not isinstance(manifest, list):
            raise ValueError("manifest.json does not contain an image list")

        image = manifest[0]
        config_path = image["Config"]
        repo_tags = image.get("RepoTags") or []
        layer_paths = image["Layers"]

        config_bytes = _read_bytes(in_tf, config_path)
        config_json = json.loads(config_bytes.decode("utf-8"))

        layer_ids = [_layer_id_from_path(layer_path) for layer_path in layer_paths]
        config_filename = f"{_layer_id_from_path(config_path)}.json"
        top_layer_id = layer_ids[-1]

        repositories: dict[str, dict[str, str]] = {}
        for repo_tag in repo_tags:
            if ":" not in repo_tag:
                repo, tag = repo_tag, "latest"
            else:
                repo, tag = repo_tag.rsplit(":", 1)
            repositories.setdefault(repo, {})[tag] = top_layer_id

        legacy_manifest = [{
            "Config": config_filename,
            "RepoTags": repo_tags,
            "Layers": [f"{layer_id}/layer.tar" for layer_id in layer_ids],
        }]

        with tarfile.open(dst, "w") as out_tf:
            _add_bytes(out_tf, config_filename, config_bytes)
            _add_bytes(out_tf, "manifest.json", json.dumps(legacy_manifest).encode("utf-8"))
            _add_bytes(out_tf, "repositories", json.dumps(repositories).encode("utf-8"))

            parent = ""
            history = config_json.get("history", [])
            for idx, (layer_path, layer_id) in enumerate(zip(layer_paths, layer_ids, strict=True)):
                layer_bytes = _read_bytes(in_tf, layer_path)
                layer_json: dict[str, Any] = {"id": layer_id}
                if parent:
                    layer_json["parent"] = parent
                if idx < len(history):
                    created = history[idx].get("created")
                    created_by = history[idx].get("created_by")
                    if created is not None:
                        layer_json["created"] = created
                    if created_by is not None:
                        layer_json["container_config"] = {"Cmd": ["/bin/sh", "-c", created_by]}

                _add_bytes(out_tf, f"{layer_id}/VERSION", b"1.0")
                _add_bytes(out_tf, f"{layer_id}/json", json.dumps(layer_json).encode("utf-8"))
                _add_bytes(out_tf, f"{layer_id}/layer.tar", layer_bytes)
                parent = layer_id


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("src", help="input OCI/buildkit-style tar archive")
    parser.add_argument("dst", help="output legacy docker-save style tar archive")
    args = parser.parse_args()

    convert_archive(args.src, args.dst)
    print(f"Wrote legacy archive: {args.dst}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
