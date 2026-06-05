from __future__ import annotations

from pathlib import Path
from typing import Any

import requests

from app.config import Settings, effective_settings


class ImmichClient:
    def __init__(self, config: Settings | None = None) -> None:
        self.config = config or effective_settings()
        self.session = requests.Session()
        if self.config.immich_api_key:
            self.session.headers.update({"x-api-key": self.config.immich_api_key})
        self.session.headers.update({"Accept": "application/json"})

    def api_url(self, path: str) -> str:
        return self.config.immich_url.rstrip("/") + "/api/" + path.lstrip("/")

    def request(self, method: str, path: str, **kwargs: Any) -> Any:
        response = self.session.request(method, self.api_url(path), timeout=60, **kwargs)
        response.raise_for_status()
        if not response.content:
            return None
        return response.json()

    def ping(self) -> bool:
        try:
            self.request("GET", "server/about")
            return True
        except requests.RequestException:
            return False

    def search_assets_by_stem(self, stem: str, size: int = 50) -> list[dict[str, Any]]:
        response = self.request(
            "POST",
            "search/metadata",
            json={"originalFileName": stem, "size": size},
        )
        items = response.get("assets", {}).get("items", []) if isinstance(response, dict) else []
        return [item for item in items if not item.get("isTrashed")]

    def find_asset_by_id(self, asset_id: str) -> dict[str, Any]:
        return self.request("GET", f"assets/{asset_id}")

    def download_original(self, asset_id: str, destination: Path) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with self.session.get(self.api_url(f"assets/{asset_id}/original"), stream=True, timeout=300) as r:
            r.raise_for_status()
            with destination.open("wb") as file:
                for chunk in r.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        file.write(chunk)
        return destination

    def replace_original(self, asset: dict[str, Any], replacement_path: Path) -> Any:
        data = {
            "deviceAssetId": asset.get("deviceAssetId") or asset["id"],
            "deviceId": asset.get("deviceId") or "immich-compress",
            "fileCreatedAt": asset["fileCreatedAt"],
            "fileModifiedAt": asset["fileModifiedAt"],
            "filename": asset.get("originalFileName") or replacement_path.name,
        }
        if asset.get("duration"):
            data["duration"] = asset["duration"]

        with replacement_path.open("rb") as file:
            response = self.session.put(
                self.api_url(f"assets/{asset['id']}/original"),
                data=data,
                files={"assetData": (data["filename"], file, "video/mp4")},
                timeout=600,
            )
        response.raise_for_status()
        if not response.content:
            return None
        return response.json()

    def list_albums(self) -> list[dict[str, Any]]:
        return self.request("GET", "albums") or []

    def get_album(self, album_id: str) -> dict[str, Any]:
        return self.request("GET", f"albums/{album_id}")

    def album_memberships(self, asset_ids: list[str]) -> dict[str, list[dict[str, str]]]:
        target_ids = set(asset_ids)
        result: dict[str, list[dict[str, str]]] = {asset_id: [] for asset_id in target_ids}
        for album in self.list_albums():
            if not album.get("assetCount"):
                continue
            album_id = album.get("id")
            if not album_id:
                continue
            detail = self.get_album(album_id)
            matching = {asset.get("id") for asset in detail.get("assets", [])} & target_ids
            for asset_id in matching:
                result[asset_id].append(
                    {
                        "id": album_id,
                        "albumName": album.get("albumName") or detail.get("albumName") or album_id,
                    }
                )
        return result

    def add_asset_to_album(self, asset_id: str, album_id: str) -> None:
        self.request("PUT", f"albums/{album_id}/assets", json={"ids": [asset_id]})

    def trash_asset(self, asset_id: str) -> None:
        self.request("DELETE", "assets", json={"ids": [asset_id], "force": False})
