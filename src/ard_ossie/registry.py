from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterable
from pathlib import Path

from pydantic import TypeAdapter

from ard_ossie.impact import ChangeSetRecord
from ard_ossie.models import EntityStatus, ProductRecord, ProductTableRef, TableRecord


class IdentityConflict(ValueError):
    """Raised when a Registry write would violate immutable identity rules."""


class Registry:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self._products: dict[str, ProductRecord] = {}
        self._tables: dict[str, TableRecord] = {}
        self._mappings: dict[str, list[ProductTableRef]] = {}
        self._changesets: dict[str, ChangeSetRecord] = {}

    @classmethod
    def load(cls, root: str | Path) -> Registry:
        registry = cls(root)
        registry._products = {
            record.product_id: record
            for record in _load_models(registry.root / "products", ProductRecord)
        }
        registry._tables = {
            record.table_id: record
            for record in _load_models(registry.root / "tables", TableRecord)
        }
        mapping_adapter = TypeAdapter(list[ProductTableRef])
        mapping_dir = registry.root / "mappings"
        if mapping_dir.exists():
            for path in sorted(mapping_dir.glob("*.json")):
                records = mapping_adapter.validate_json(path.read_text(encoding="utf-8"))
                registry._mappings[path.stem] = records
        registry._changesets = {
            record.changeset_id: record
            for record in _load_models(registry.root / "changesets", ChangeSetRecord)
        }
        return registry

    def get_product(self, product_id: str) -> ProductRecord | None:
        return self._products.get(product_id)

    def get_table(self, table_id: str) -> TableRecord | None:
        return self._tables.get(table_id)

    def get_changeset(self, changeset_id: str) -> ChangeSetRecord | None:
        return self._changesets.get(changeset_id)

    def products(self) -> tuple[ProductRecord, ...]:
        return tuple(self._products[key] for key in sorted(self._products))

    def tables(self) -> tuple[TableRecord, ...]:
        return tuple(self._tables[key] for key in sorted(self._tables))

    def mappings(self) -> tuple[ProductTableRef, ...]:
        return tuple(
            mapping
            for product_id in sorted(self._mappings)
            for mapping in self._mappings[product_id]
        )

    def write_product(self, record: ProductRecord) -> None:
        previous = self._products.get(record.product_id)
        if (
            previous is not None
            and previous.status is EntityStatus.RETIRED
            and record.status is EntityStatus.ACTIVE
        ):
            raise IdentityConflict(f"RETIRED_ID_REUSE: {record.product_id}")

        conflicting = next(
            (
                item
                for item in self._products.values()
                if item.product_id != record.product_id
                and (item.product_key == record.product_key or record.product_key in item.aliases)
            ),
            None,
        )
        if conflicting is not None:
            raise IdentityConflict(
                f"PRODUCT_KEY_CONFLICT: {record.product_key} -> {conflicting.product_id}"
            )

        _atomic_write_json(
            self.root / "products" / f"{record.product_id}.json",
            record.model_dump(mode="json"),
        )
        self._products[record.product_id] = record
        self._write_indexes()

    def write_table(self, record: TableRecord) -> None:
        previous = self._tables.get(record.table_id)
        if (
            previous is not None
            and previous.status is EntityStatus.RETIRED
            and record.status is EntityStatus.ACTIVE
        ):
            raise IdentityConflict(f"RETIRED_ID_REUSE: {record.table_id}")

        conflicting = next(
            (
                item
                for item in self._tables.values()
                if item.table_id != record.table_id and item.locator.key == record.locator.key
            ),
            None,
        )
        if conflicting is not None:
            raise IdentityConflict(
                f"TABLE_LOCATOR_CONFLICT: {record.locator.key} -> {conflicting.table_id}"
            )

        _atomic_write_json(
            self.root / "tables" / f"{record.table_id}.json",
            record.model_dump(mode="json"),
        )
        self._tables[record.table_id] = record
        self._write_indexes()

    def write_mappings(
        self,
        product_id: str,
        mappings: Iterable[ProductTableRef],
    ) -> None:
        if product_id not in self._products:
            raise IdentityConflict(f"MAPPING_PRODUCT_NOT_FOUND: {product_id}")

        records = sorted(mappings, key=lambda item: item.link_id)
        for mapping in records:
            if mapping.product_id != product_id:
                raise IdentityConflict(
                    f"MAPPING_PRODUCT_MISMATCH: {mapping.product_id} != {product_id}"
                )
            table = self._tables.get(mapping.table_id)
            if table is None:
                raise IdentityConflict(f"MAPPING_TABLE_NOT_FOUND: {mapping.table_id}")
            if mapping.table_version != table.version:
                raise IdentityConflict(f"MAPPING_TABLE_VERSION_MISMATCH: {mapping.table_id}")

        _atomic_write_json(
            self.root / "mappings" / f"{product_id}.json",
            [record.model_dump(mode="json") for record in records],
        )
        self._mappings[product_id] = records

    def write_changeset(self, record: ChangeSetRecord) -> None:
        _atomic_write_json(
            self.root / "changesets" / f"{record.changeset_id}.json",
            record.model_dump(mode="json"),
        )
        self._changesets[record.changeset_id] = record

    def _write_indexes(self) -> None:
        product_keys = {
            product.product_key: product.product_id
            for product in sorted(self._products.values(), key=lambda item: item.product_key)
        }
        table_locators = {
            table.locator.key: table.table_id
            for table in sorted(self._tables.values(), key=lambda item: item.locator.key)
        }
        _atomic_write_json(self.root / "indexes" / "product-keys.json", product_keys)
        _atomic_write_json(self.root / "indexes" / "table-locators.json", table_locators)


def _load_models(
    directory: Path,
    model: type[ProductRecord] | type[TableRecord] | type[ChangeSetRecord],
) -> list:
    if not directory.exists():
        return []
    return [
        model.model_validate_json(path.read_text(encoding="utf-8"))
        for path in sorted(directory.glob("*.json"))
    ]


def _atomic_write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
            temp_path = Path(handle.name)
        os.replace(temp_path, path)
        temp_path = None
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
