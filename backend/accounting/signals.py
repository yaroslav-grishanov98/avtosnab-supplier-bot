"""Signals the accounting app sends to the rest of the platform (BE-1).

``stock_snapshot_ready`` is sent once per venue per stock sync, after the
snapshots are committed (``transaction.on_commit``), with ``sender`` set to
``AccountingStockSnapshot`` and these keyword arguments:

* ``connection_id`` (UUID) and ``venue_id`` (UUID);
* ``warehouse_ids`` (list[UUID]) and ``snapshot_ids`` (list[UUID]) — only the
  warehouses whose balances were stored, i.e. COMPLETE or PARTIAL snapshots.
  A venue whose warehouses all FAILED gets no signal;
* ``taken_at`` (datetime) — the moment the balances refer to.

Receiver contract:

* Receivers must only enqueue work (e.g. a Celery task) and return at once.
  They run synchronously inside the stock sync, while the iiko session and the
  advisory locks are still held and BEFORE the sync job has its final status,
  so slow work there delays the sync and holds a licence slot of the iiko server.
* The signal is sent with ``send_robust``: a failing receiver never breaks the
  sync, so receivers must not rely on raising to signal anything. Our own log
  line names only the receiver and the exception type, but Django
  (``Signal._log_robust_failure``, logger ``django.dispatch``) logs the
  receiver's exception text with its traceback, so receivers must never put
  secrets or personal data in exception messages.
"""

import django.dispatch

stock_snapshot_ready = django.dispatch.Signal()
