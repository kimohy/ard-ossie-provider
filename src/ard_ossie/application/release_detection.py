from __future__ import annotations

import re
from pathlib import Path

import yaml
from pydantic import Field

from ard_ossie.application.contracts import (
    WorkflowConflict,
    WorkflowResult,
    WorkflowSecurityError,
    WorkflowStatus,
)
from ard_ossie.impact import ChangeSetRecord, ChangeSetStatus
from ard_ossie.models import ProductRecord, StrictModel
from ard_ossie.ports.filesystem import FileSystemPort
from ard_ossie.ports.git import GitPort
from ard_ossie.registry import Registry

_PRODUCT_KEY = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_CHANGESET_ID = re.compile(
    r"^cst_[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_TABLE_ID = re.compile(
    r"^tbl_[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


class ReleaseDetectionRequest(StrictModel):
    repository: Path
    before: str = Field(pattern=r"^[0-9a-f]{40}$")
    current: str = Field(pattern=r"^[0-9a-f]{40}$")


class ReleaseDetectionService:
    def __init__(self, paths: FileSystemPort, git: GitPort) -> None:
        self.paths = paths
        self.git = git

    def run(self, request: ReleaseDetectionRequest) -> WorkflowResult:
        if request.repository.expanduser().resolve() != self.paths.root:
            raise WorkflowSecurityError(
                "RELEASE_DETECTION_REPOSITORY_MISMATCH",
                "release detection repository does not match filesystem port",
            )
        if self.git.current_sha() != request.current:
            raise WorkflowSecurityError(
                "RELEASE_DETECTION_HEAD_MISMATCH",
                "release detection must run at the exact merged commit",
            )
        changed = self.git.changed_paths(request.before, request.current)
        registry = self._load_registry()
        products_by_key = {item.product_key: item for item in registry.products()}
        direct_products: set[str] = set()
        changeset_ids: set[str] = set()
        table_ids: set[str] = set()

        for path in changed.paths:
            relative = self._safe_relative(path)
            parts = relative.parts
            if len(parts) >= 4 and parts[0] == "products" and parts[2] == "generated":
                product_key = parts[1]
                if _PRODUCT_KEY.fullmatch(product_key) is None:
                    raise WorkflowConflict(
                        "RELEASE_PRODUCT_KEY_INVALID",
                        "changed generated artifact has a non-canonical product key",
                    )
                self._require_current_artifact(relative)
                product = self._require_product_key(products_by_key, product_key)
                changeset_id = self._product_changeset(product)
                if changeset_id is None:
                    direct_products.add(product_key)
                else:
                    changeset_ids.add(changeset_id)
                continue
            if (
                len(parts) == 3
                and parts[:2] == ("registry", "changesets")
                and parts[2].endswith(".json")
            ):
                changeset_id = parts[2].removesuffix(".json")
                if _CHANGESET_ID.fullmatch(changeset_id) is None:
                    raise WorkflowConflict(
                        "CHANGESET_REFERENCE_INVALID",
                        "changed changeset path is not canonical",
                    )
                changeset_ids.add(changeset_id)
                continue
            if (
                len(parts) == 3
                and parts[:2] == ("registry", "tables")
                and parts[2].endswith(".json")
            ):
                table_id = parts[2].removesuffix(".json")
                if _TABLE_ID.fullmatch(table_id) is None:
                    raise WorkflowConflict(
                        "TABLE_REFERENCE_INVALID",
                        "changed table path is not canonical",
                    )
                self._require_table(registry, table_id)
                table_ids.add(table_id)

        expanded_products = set(direct_products)
        for changeset_id in sorted(changeset_ids):
            changeset = registry.get_changeset(changeset_id)
            if changeset is None:
                raise WorkflowConflict(
                    "CHANGESET_NOT_FOUND",
                    f"release changeset is missing: {changeset_id}",
                )
            if changeset.status is not ChangeSetStatus.READY:
                continue
            self._expand_changeset(
                registry,
                changeset,
                expanded_products,
                table_ids,
            )

        outputs: dict[str, object] = {
            "before": request.before,
            "current": request.current,
            "merge_base": changed.merge_base,
            "products": sorted(expanded_products),
            "tables": sorted(table_ids),
        }
        return WorkflowResult(
            command="workflow.release-detect",
            status=(
                WorkflowStatus.SUCCESS
                if expanded_products or table_ids
                else WorkflowStatus.NOOP
            ),
            outputs=outputs,
        )

    def _load_registry(self) -> Registry:
        try:
            return Registry.load(self.paths.resolve_write("registry"))
        except (OSError, TypeError, ValueError) as error:
            raise WorkflowConflict(
                "RELEASE_REGISTRY_INVALID",
                "release registry is malformed",
            ) from error

    def _safe_relative(self, path: Path) -> Path:
        resolved = self.paths.resolve_write(path)
        relative = resolved.relative_to(self.paths.root)
        if path.is_absolute() or ".." in path.parts or relative != path:
            raise WorkflowSecurityError(
                "RELEASE_CHANGED_PATH_UNSAFE",
                "changed release path is outside the repository",
            )
        return relative

    def _require_current_artifact(self, relative: Path) -> None:
        if not self.paths.resolve_write(relative).is_file():
            raise WorkflowConflict(
                "RELEASE_ARTIFACT_DELETED",
                f"changed release artifact is absent at current head: {relative.as_posix()}",
            )

    @staticmethod
    def _require_product_key(
        products_by_key: dict[str, ProductRecord],
        product_key: str,
    ) -> ProductRecord:
        product = products_by_key.get(product_key)
        if product is None:
            raise WorkflowConflict(
                "RELEASE_PRODUCT_NOT_FOUND",
                f"release product is missing from registry: {product_key}",
            )
        return product

    def _product_changeset(self, product: ProductRecord) -> str | None:
        config_path = self.paths.resolve_write(
            Path("products") / product.product_key / "product.yaml"
        )
        if not config_path.is_file():
            raise WorkflowConflict(
                "RELEASE_PRODUCT_CONFIG_MISSING",
                f"release product config is missing: {product.product_key}",
            )
        try:
            config = yaml.safe_load(
                self.paths.resolve_read(config_path).read_text(encoding="utf-8")
            )
        except (OSError, TypeError, ValueError, yaml.YAMLError) as error:
            raise WorkflowConflict(
                "RELEASE_PRODUCT_CONFIG_INVALID",
                f"release product config is malformed: {product.product_key}",
            ) from error
        if not isinstance(config, dict) or (
            config.get("product_id") != product.product_id
            or config.get("product_key") != product.product_key
            or config.get("version") != product.version
        ):
            raise WorkflowConflict(
                "RELEASE_PRODUCT_CONFIG_MISMATCH",
                f"release product config does not match registry: {product.product_key}",
            )
        changeset_id = config.get("changeset_id")
        if changeset_id in (None, ""):
            return None
        if not isinstance(changeset_id, str) or _CHANGESET_ID.fullmatch(changeset_id) is None:
            raise WorkflowConflict(
                "CHANGESET_REFERENCE_INVALID",
                f"release product has an invalid changeset: {product.product_key}",
            )
        return changeset_id

    @staticmethod
    def _require_table(registry: Registry, table_id: str) -> None:
        if registry.get_table(table_id) is None:
            raise WorkflowConflict(
                "RELEASE_TABLE_NOT_FOUND",
                f"release table is missing from registry: {table_id}",
            )

    @staticmethod
    def _expand_changeset(
        registry: Registry,
        changeset: ChangeSetRecord,
        products: set[str],
        tables: set[str],
    ) -> None:
        for product_id in changeset.required_product_ids:
            readiness = changeset.ready_products.get(product_id)
            product = registry.get_product(product_id)
            if product is None or readiness is None:
                raise WorkflowConflict(
                    "CHANGESET_REGISTRY_REFERENCE_MISSING",
                    f"changeset references a missing product: {product_id}",
                )
            if readiness.version != product.version:
                raise WorkflowConflict(
                    "CHANGESET_VERSION_NOT_CURRENT",
                    f"{product_id}:v{readiness.version} != v{product.version}",
                )
            products.add(product.product_key)
        for table_id in changeset.table_ids:
            ReleaseDetectionService._require_table(registry, table_id)
            tables.add(table_id)
