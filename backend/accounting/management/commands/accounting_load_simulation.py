"""Load simulation of the sync queue: many connections, a few workers, a slow fake provider.

It creates ``--connections`` connections (each with one venue, one warehouse and three products) spread over
``--hosts`` provider hosts, lets the scheduler claim a stock job for each, and runs the jobs on ``--workers``
threads with ``FakeAdapter(latency=...)`` in place of the provider. It then prints the number of syncs, the
highest number of syncs running at once on one host (must not exceed ACCOUNTING_HOST_SLOTS), the queue wait
(p50/p95) and the number of double runs (must be 0): the same connection syncing twice at once, or one job
reading twice.

THIS WRITES TO THE DATABASE (it removes what it created afterwards), so it asks for ``--confirm-writes`` and is
meant for a development or staging database, never production.
"""

import queue
import threading
import time
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from unittest import mock
from uuid import uuid4

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connections
from django.utils import timezone

from accounting import scheduling
from accounting.adapters import FakeAdapter
from accounting.dto import StockItemDTO, WarehouseDTO
from accounting.models import (
    AccountingConnection,
    AccountingConnectionEvent,
    AccountingConsumptionCoverage,
    AccountingConsumptionDaily,
    AccountingDepartment,
    AccountingProduct,
    AccountingStockLevel,
    AccountingStockSnapshot,
    AccountingSyncJob,
    AccountingVenueLink,
    AccountingWarehouse,
    LinkStatus,
    Provider,
    Reliability,
    SyncStatus,
)
from accounting.sync.runner import run_sync_job
from core.models import Venue

BUSY_PAUSE_SECONDS = 0.005  # a busy job goes back to the queue almost at once; the real delay is 30-60 s
PRODUCTS_PER_CONNECTION = 3


@dataclass
class SimulationResult:
    connections: int
    workers: int
    hosts: int
    host_slots: int
    jobs: int = 0
    finished: int = 0
    successes: int = 0
    busy_retries: int = 0
    wall_seconds: float = 0.0
    max_per_host: int = 0
    double_runs: int = 0
    wait_p50: float = 0.0
    wait_p95: float = 0.0

    @property
    def syncs_per_minute(self) -> float:
        return self.successes / self.wall_seconds * 60 if self.wall_seconds else 0.0


@dataclass
class _Tracker:
    """Shared by all fake adapters: how many reads run at once per host and per connection."""

    lock: threading.Lock = field(default_factory=threading.Lock)
    per_host: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    per_connection: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    max_per_host: int = 0
    double_runs: int = 0

    def enter(self, host: str, connection_id: str) -> None:
        with self.lock:
            self.per_host[host] += 1
            self.per_connection[connection_id] += 1
            self.max_per_host = max(self.max_per_host, self.per_host[host])
            if self.per_connection[connection_id] > 1:
                self.double_runs += 1  # the same connection reading twice at once

    def leave(self, host: str, connection_id: str) -> None:
        with self.lock:
            self.per_host[host] -= 1
            self.per_connection[connection_id] -= 1


class _TrackingAdapter(FakeAdapter):
    def __init__(self, tracker: _Tracker, host: str, connection_id: str, **kwargs):
        super().__init__(**kwargs)
        self._tracker, self._host, self._connection_id = tracker, host, connection_id

    def read_stock(self, warehouse_ids, at):
        self._tracker.enter(self._host, self._connection_id)
        try:
            yield from super().read_stock(warehouse_ids, at)
        finally:
            self._tracker.leave(self._host, self._connection_id)


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(fraction * len(ordered)))]


def _build_world(connections_count: int, hosts: int, tag: str) -> tuple[list[AccountingConnection], list[Venue]]:
    now = timezone.now()
    made, venues = [], []
    for index in range(connections_count):
        venue = Venue.objects.create(display_name=f"{tag}-{index}")
        connection = AccountingConnection.objects.create(
            provider=Provider.IIKO,
            base_url=f"https://{tag.lower()}-host-{index % hosts}.iiko.example/resto",
            capabilities={"omits_zero_balances": True, "read_consumption": False},
            catalog_synced_at=now,
            next_sync_at=now - timedelta(seconds=1),
        )
        AccountingVenueLink.objects.create(connection=connection, venue=venue, status=LinkStatus.ACTIVE)
        AccountingWarehouse.objects.create(
            connection=connection, venue=venue, external_id=f"wh-{index}", name="Склад", is_present_in_source=True
        )
        for number in range(PRODUCTS_PER_CONNECTION):
            AccountingProduct.objects.create(
                connection=connection, external_id=f"p-{index}-{number}", name=f"Товар {number}", unit_name="кг"
            )
        made.append(connection)
        venues.append(venue)
    return made, venues


def _cleanup(connection_ids: list, venue_ids: list) -> None:
    for model in (
        AccountingConsumptionDaily,
        AccountingConsumptionCoverage,
        AccountingStockLevel,
        AccountingStockSnapshot,
        AccountingWarehouse,
        AccountingProduct,
        AccountingDepartment,
        AccountingSyncJob,
        AccountingConnectionEvent,
        AccountingVenueLink,
    ):
        model.objects.filter(connection_id__in=connection_ids).delete()
    AccountingConnection.objects.filter(pk__in=connection_ids).delete()
    Venue.objects.filter(pk__in=venue_ids).delete()


def run_simulation(*, connections_count: int, workers: int, latency_ms: int, hosts: int) -> SimulationResult:
    tag = f"SIM{uuid4().hex[:8]}"
    host_slots = settings.ACCOUNTING_HOST_SLOTS
    result = SimulationResult(connections_count, workers, hosts, host_slots)
    made, venues = _build_world(connections_count, hosts, tag)
    tracker = _Tracker()
    by_id = {str(connection.id): connection for connection in made}
    latency = latency_ms / 1000

    @contextmanager
    def fake_get_adapter(connection, **options):
        index = connection.warehouses.get().external_id.removeprefix("wh-")
        products = [f"p-{index}-{n}" for n in range(PRODUCTS_PER_CONNECTION)]
        stock = {
            f"wh-{index}": [
                StockItemDTO(product, Decimal(n + 1), "кг", Reliability.OK) for n, product in enumerate(products)
            ]
        }
        yield _TrackingAdapter(
            tracker,
            connection.host,
            str(connection.id),
            warehouses=[WarehouseDTO(f"wh-{index}", "Склад", None, "STORE")],
            products=[],
            departments=[],
            stock=stock,
            latency=latency,
        )

    try:
        jobs = []
        while batch := scheduling.claim_due_connections(timezone.now(), limit=500):
            jobs.extend(batch)
        result.jobs = len(jobs)
        pending = queue.Queue()
        for job in jobs:
            pending.put(job.job_id)
        state = {"remaining": len(jobs), "busy": 0, "ok": 0, "finished": 0}
        executions: dict = defaultdict(int)
        state_lock = threading.Lock()

        def worker() -> None:
            try:
                while True:
                    with state_lock:
                        if state["remaining"] <= 0:
                            return
                    try:
                        job_id = pending.get(timeout=0.05)
                    except queue.Empty:
                        continue
                    outcome = run_sync_job(job_id)
                    if outcome.ran:
                        with state_lock:
                            executions[job_id] += 1
                            if executions[job_id] > 1:
                                tracker.double_runs += 1  # one job executed twice
                    if outcome.retry_in is not None and outcome.status in (SyncStatus.QUEUED, SyncStatus.RUNNING):
                        with state_lock:
                            state["busy"] += 1
                        time.sleep(BUSY_PAUSE_SECONDS)
                        pending.put(job_id)
                        continue
                    with state_lock:
                        state["remaining"] -= 1
                        state["finished"] += 1
                        state["ok"] += int(outcome.status in (SyncStatus.SUCCESS, SyncStatus.PARTIAL))
            finally:
                connections.close_all()

        started = time.monotonic()
        with mock.patch("accounting.sync.runner.get_adapter", fake_get_adapter):
            threads = [threading.Thread(target=worker, name=f"sim-worker-{n}") for n in range(workers)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        result.wall_seconds = time.monotonic() - started
        result.finished, result.successes, result.busy_retries = state["finished"], state["ok"], state["busy"]
        waits = [
            (job.started_at - job.queued_at).total_seconds()
            for job in AccountingSyncJob.objects.filter(connection_id__in=by_id)
            if job.started_at and job.queued_at
        ]
        result.wait_p50, result.wait_p95 = percentile(waits, 0.5), percentile(waits, 0.95)
        result.max_per_host, result.double_runs = tracker.max_per_host, tracker.double_runs
    finally:
        _cleanup([c.id for c in made], [v.id for v in venues])
    return result


class Command(BaseCommand):
    help = "Simulate the sync queue with a slow fake provider and check host slots and double runs (writes to the DB)."

    def add_arguments(self, parser):
        parser.add_argument("--connections", type=int, default=300)
        parser.add_argument("--workers", type=int, default=12)
        parser.add_argument("--latency-ms", type=int, default=200)
        parser.add_argument("--hosts", type=int, default=50)
        parser.add_argument(
            "--confirm-writes",
            action="store_true",
            help="Allow creating (and afterwards deleting) simulation rows in the current database.",
        )

    def handle(self, *args, **options):
        if not options["confirm_writes"]:
            name = settings.DATABASES["default"]["NAME"]
            raise CommandError(
                f"Команда создаёт и удаляет строки в базе «{name}». Запускайте только на dev/staging и добавьте "
                "--confirm-writes."
            )
        for key in ("connections", "workers", "hosts"):
            if options[key] < 1:
                raise CommandError(f"--{key} должно быть не меньше 1.")
        result = run_simulation(
            connections_count=options["connections"],
            workers=options["workers"],
            latency_ms=options["latency_ms"],
            hosts=options["hosts"],
        )
        rows = [
            ("подключений / хостов / потоков", f"{result.connections} / {result.hosts} / {result.workers}"),
            ("слотов на хост", result.host_slots),
            ("задач создано / завершено / успешно", f"{result.jobs} / {result.finished} / {result.successes}"),
            ("повторов из-за занятого подключения или хоста", result.busy_retries),
            ("время прогона, с", f"{result.wall_seconds:.1f}"),
            ("синков в минуту", f"{result.syncs_per_minute:.0f}"),
            ("макс. одновременных синков на хост", result.max_per_host),
            ("ожидание в очереди p50 / p95, с", f"{result.wait_p50:.2f} / {result.wait_p95:.2f}"),
            ("двойных запусков", result.double_runs),
        ]
        width = max(len(label) for label, _ in rows)
        for label, value in rows:
            self.stdout.write(f"{label.ljust(width)}  {value}")
        problems = []
        if result.max_per_host > result.host_slots:
            problems.append("на одном хосте синков больше, чем слотов")
        if result.double_runs:
            problems.append("есть двойные запуски")
        if result.finished != result.jobs:
            problems.append("не все задачи завершены")
        if problems:
            raise CommandError("Нарушены инварианты: " + "; ".join(problems) + ".")
