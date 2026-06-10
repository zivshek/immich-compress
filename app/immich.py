from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

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
        if not self.config.immich_url or not self.config.immich_api_key:
            raise RuntimeError("Configure the Immich URL and API key in Settings")
        response = self.session.request(method, self.api_url(path), timeout=60, **kwargs)
        response.raise_for_status()
        if not response.content:
            return None
        return response.json()

    def ping(self) -> bool:
        try:
            self.request("GET", "server/about")
            return True
        except (requests.RequestException, RuntimeError):
            return False

    def search_assets_by_stem(self, stem: str, size: int = 50) -> list[dict[str, Any]]:
        response = self.request(
            "POST",
            "search/metadata",
            json={"originalFileName": stem, "size": size},
        )
        items = response.get("assets", {}).get("items", []) if isinstance(response, dict) else []
        return [item for item in items if not item.get("isTrashed")]

    def search_videos(self, page: int = 1, size: int = 10) -> tuple[list[dict[str, Any]], int | None]:
        query: dict[str, Any] = {
            "type": "VIDEO",
            "order": "desc",
            "page": page,
            "size": size,
            "withExif": True,
        }
        if self.config.video_taken_before:
            query["takenBefore"] = self.config.video_taken_before
        response = self.request(
            "POST",
            "search/metadata",
            json=query,
        )
        assets = response.get("assets", response) if isinstance(response, dict) else {}
        items = assets.get("items", []) if isinstance(assets, dict) else []
        total = None
        if isinstance(response, dict):
            total = assets.get("total") or assets.get("count") or response.get("total")
        return [item for item in items if not item.get("isTrashed")], total

    def find_asset_by_id(self, asset_id: str) -> dict[str, Any]:
        return self.request("GET", f"assets/{asset_id}")

    def download_original(
        self,
        asset_id: str,
        destination: Path,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with self.session.get(self.api_url(f"assets/{asset_id}/original"), stream=True, timeout=300) as r:
            r.raise_for_status()
            with destination.open("wb") as file:
                for chunk in r.iter_content(chunk_size=1024 * 1024):
                    if cancel_requested and cancel_requested():
                        raise InterruptedError("Job canceled")
                    if chunk:
                        file.write(chunk)
        return destination

    def upload_asset_copy(self, source_asset: dict[str, Any], replacement_path: Path) -> dict[str, Any]:
        data = {
            "deviceAssetId": f"immich-compress-{source_asset['id']}",
            "deviceId": "immich-compress",
            "fileCreatedAt": source_asset["fileCreatedAt"],
            "fileModifiedAt": source_asset["fileModifiedAt"],
            "filename": replacement_path.name,
        }
        if source_asset.get("duration"):
            data["duration"] = source_asset["duration"]

        with replacement_path.open("rb") as file:
            response = self.session.post(
                self.api_url("assets"),
                data=data,
                files={"assetData": (data["filename"], file, "video/mp4")},
                timeout=600,
            )
        response.raise_for_status()
        if not response.content:
            raise RuntimeError("Immich upload returned an empty response")
        return response.json()

    def copy_asset_metadata(self, source_id: str, target_id: str) -> None:
        self.request(
            "PUT",
            "assets/copy",
            json={
                "sourceId": source_id,
                "targetId": target_id,
                "albums": True,
                "favorite": True,
                "sharedLinks": True,
                "sidecar": True,
                "stack": True,
            },
        )

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

    def album_names_for_asset(self, asset_id: str) -> list[str]:
        memberships = self.album_memberships([asset_id])
        return [album["albumName"] for album in memberships.get(asset_id, [])]

    def add_asset_to_album(self, asset_id: str, album_id: str) -> None:
        self.request("PUT", f"albums/{album_id}/assets", json={"ids": [asset_id]})

    def trash_asset(self, asset_id: str) -> None:
        self.request("DELETE", "assets", json={"ids": [asset_id], "force": False})
