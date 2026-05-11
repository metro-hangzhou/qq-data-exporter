from __future__ import annotations

from .models import CanonicalAssetRecord, PreparedImageAsset


class ReferenceOnlyImageFeatureProvider:
    def prepare_assets(
        self, assets: list[CanonicalAssetRecord]
    ) -> list[PreparedImageAsset]:
        prepared: list[PreparedImageAsset] = []
        for asset in assets:
            prepared.append(
                PreparedImageAsset(
                    asset_id=asset.asset_id,
                    future_multimodal_parse=True,
                    payload={
                        "asset_type": asset.asset_type,
                        "file_name": asset.file_name,
                        "path": asset.path,
                        "md5": asset.md5,
                    },
                )
            )
        return prepared
