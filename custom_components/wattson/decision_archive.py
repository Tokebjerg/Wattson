"""Daily replay partitions keep full records out of the hot runtime store."""
from __future__ import annotations

from datetime import datetime, timedelta
from copy import deepcopy
from collections import deque


class DecisionArchive:
    def __init__(self, store_factory, *, retention_days: int = 90) -> None:
        self.store_factory, self.retention_days = store_factory, retention_days
        self._day = None
        self._store = None
        self._records = []
        self._pending = deque(maxlen=288)
        self.dropped_pending_records = 0
        self._index = store_factory("index")
        self._days = None
        self.dropped_records = 0

    def enqueue(self, record: dict) -> None:
        if len(self._pending) == self._pending.maxlen:
            self.dropped_pending_records += 1
        self._pending.append(deepcopy(record))

    async def drain(self) -> None:
        while self._pending:
            record = self._pending.popleft()
            try:
                await self.append(record)
            except BaseException:
                if len(self._pending) == self._pending.maxlen:
                    self._pending.popleft()
                    self.dropped_pending_records += 1
                self._pending.appendleft(record)
                raise

    def as_dict(self) -> dict:
        return {"retention_days": self.retention_days, "daily_capacity": 288,
                "current_day": self._day.isoformat() if self._day else None,
                "pending": len(self._pending), "current_records": len(self._records),
                "dropped_pending_records": self.dropped_pending_records,
                "dropped_records_current_day": self.dropped_records}

    def _payload(self) -> dict:
        return {"schema": 2, "records": self._records, "dropped_records": self.dropped_records}

    async def append(self, record: dict) -> None:
        day = datetime.fromisoformat(record["at"]).date()
        if day != self._day:
            if self._store is not None:
                await self._store.async_save(self._payload())
            new_store = self.store_factory(day.isoformat())
            saved = await new_store.async_load()
            records = list(saved.get("records", [])) if isinstance(saved, dict) else []
            dropped_records = int(saved.get("dropped_records", 0)) if isinstance(saved, dict) else 0
            if self._days is None:
                index = await self._index.async_load()
                self._days = set(index.get("days", [])) if isinstance(index, dict) else set()
            self._days.add(day.isoformat())
            newest = datetime.fromisoformat(max(self._days)).date()
            cutoff = (newest-timedelta(days=self.retention_days)).isoformat()
            expired_days = {value for value in self._days if value <= cutoff}
            for expired_day in expired_days:
                await self.store_factory(expired_day).async_remove()
            self._days -= expired_days
            await self._index.async_save({"schema": 2, "days": sorted(self._days)})
            # Cache the partition only after indexing succeeds, so a retry does
            # not silently leave this day's records orphaned from retention.
            self._day, self._store = day, new_store
            self._records, self.dropped_records = records, dropped_records
        snapshot = deepcopy(record)
        existing = next((index for index, row in enumerate(self._records)
                         if row.get("at") == snapshot.get("at")), None)
        if existing is not None:
            self._records[existing] = snapshot
        else:
            self._records.append(snapshot)
        self.dropped_records += max(0, len(self._records)-288)
        self._records = self._records[-288:]
        await self._store.async_save(self._payload())

    async def flush(self) -> None:
        await self.drain()
        if self._store is not None:
            await self._store.async_save(self._payload())
