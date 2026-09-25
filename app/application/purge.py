"""Removing soft-deleted workspaces for good, run by the scheduled job (and by account
deletion for the account's own). Each step commits on its own, so an interrupted purge
resumes where it stopped on the next run."""

from loguru import logger

from app.application.ports import MediaStorage, UnitOfWork

# Up to 200 products with 10 images each stays under Aurora DSQL's 3,000 rows per
# transaction.
PRODUCT_BATCH = 200


class WorkspacePurge:
    def __init__(self, uow: UnitOfWork, media: MediaStorage) -> None:
        self._uow = uow
        self._media = media

    async def purge_all(self) -> int:
        workspace_ids = await self._uow.workspaces.deleted_ids()
        for workspace_id in workspace_ids:
            await self.purge(workspace_id)
        return len(workspace_ids)

    async def purge(self, workspace_id: str) -> None:
        uow = self._uow
        while True:
            deleted, keys = await uow.transaction(
                lambda: uow.products.delete_batch(workspace_id, PRODUCT_BATCH)
            )
            for key in keys:
                await self._media.delete(key)
            if deleted < PRODUCT_BATCH:
                break

        async def purge_rows() -> str | None:
            workspace = await uow.workspaces.get_deleted(workspace_id)
            if workspace is None:
                return None
            await uow.workspaces.purge(workspace)
            return workspace.logo_key

        logo_key = await uow.transaction(purge_rows)
        if logo_key:
            await self._media.delete(logo_key)
        logger.info("Purged workspace {}", workspace_id)
