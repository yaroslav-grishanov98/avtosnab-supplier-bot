"""Synchronisation of accounting data into our database.

``catalog.sync_catalog`` and ``stock.sync_stock`` do the work with an open
adapter; ``runner.run_sync_job`` runs one ``AccountingSyncJob`` end to end
(guard, adapter session, status and stats). Import the submodules directly.
"""
