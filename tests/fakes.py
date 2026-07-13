"""Hand-rolled fakes shared across the Phase 1 test suite (repo convention:
no mock libraries). Fakes accept a shared ``journal`` list so tests can assert
cross-component ordering — e.g. that the abort delete precedes any sleep.
"""

from __future__ import annotations

from typing import Any

from chaosagent.execute import AppliedExperiment, ExecutionDenied

Journal = list[str]


class FakeClock:
    """Deterministic clock; sleeping advances time and is journalled."""

    def __init__(self, start: float = 0.0, journal: Journal | None = None) -> None:
        self._now = start
        self.sleeps: list[float] = []
        self.journal: Journal = journal if journal is not None else []

    def now(self) -> float:
        return self._now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.journal.append(f"sleep:{seconds}")
        self._now += seconds

    def advance(self, seconds: float) -> None:
        self._now += seconds


class ScriptedPrometheus:
    """ScalarSource returning a scripted sequence per query; the last entry
    repeats once the script is exhausted. An Exception entry is raised."""

    def __init__(
        self,
        series: dict[str, list[float | None | Exception]],
        journal: Journal | None = None,
    ) -> None:
        self._series = {query: list(values) for query, values in series.items()}
        self._cursor: dict[str, int] = dict.fromkeys(series, 0)
        self.journal: Journal = journal if journal is not None else []

    def scalar(self, query: str) -> float | None:
        values = self._series[query]
        index = min(self._cursor[query], len(values) - 1)
        self._cursor[query] += 1
        value = values[index]
        if isinstance(value, Exception):
            self.journal.append(f"scalar:{query}=raise")
            raise value
        self.journal.append(f"scalar:{query}={value}")
        return value


class FakeExecutor:
    """Executor stand-in recording calls; failures are scripted per method."""

    def __init__(
        self,
        journal: Journal | None = None,
        *,
        clock: FakeClock | None = None,
        deny_dry_run: str | None = None,
        deny_apply: str | None = None,
    ) -> None:
        self.journal: Journal = journal if journal is not None else []
        self._clock = clock
        self._deny_dry_run = deny_dry_run
        self._deny_apply = deny_apply
        self.dry_runs: list[dict[str, Any]] = []
        self.applied: list[AppliedExperiment] = []
        self.deleted: list[AppliedExperiment] = []

    def dry_run(self, cr: dict[str, Any], binding: object) -> None:
        self.journal.append("dry_run")
        if self._deny_dry_run is not None:
            raise ExecutionDenied(self._deny_dry_run)
        self.dry_runs.append(cr)

    def apply(self, cr: dict[str, Any], binding: object) -> AppliedExperiment:
        self.journal.append("apply")
        if self._deny_apply is not None:
            raise ExecutionDenied(self._deny_apply)
        applied = AppliedExperiment(
            kind=cr["kind"],
            name=cr["metadata"]["name"],
            namespace=cr["metadata"]["namespace"],
            applied_at=self._clock.now() if self._clock is not None else 0.0,
        )
        self.applied.append(applied)
        return applied

    def delete(self, applied: AppliedExperiment) -> None:
        self.journal.append("delete")
        self.deleted.append(applied)


class FakeApiException(Exception):
    """Duck-typed stand-in for kubernetes.client.rest.ApiException."""

    def __init__(self, status: int, body: str = "", reason: str = "") -> None:
        super().__init__(reason or body or str(status))
        self.status = status
        self.body = body
        self.reason = reason


class FakeCustomObjectsApi:
    """Records custom-object calls; create failures are scripted."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.create_error: Exception | None = None
        self._objects: dict[tuple[str, str, str], dict[str, Any]] = {}

    def create_namespaced_custom_object(
        self,
        group: str,
        version: str,
        namespace: str,
        plural: str,
        body: dict[str, Any],
        *,
        dry_run: str | None = None,
    ) -> dict[str, Any]:
        name = body["metadata"]["name"]
        self.calls.append(("create", namespace, plural, name, dry_run or ""))
        if self.create_error is not None:
            raise self.create_error
        if dry_run is None:
            self._objects[(namespace, plural, name)] = body
        return body

    def delete_namespaced_custom_object(
        self, group: str, version: str, namespace: str, plural: str, name: str
    ) -> dict[str, Any]:
        self.calls.append(("delete", namespace, plural, name))
        if (namespace, plural, name) not in self._objects:
            raise FakeApiException(404, reason="Not Found")
        del self._objects[(namespace, plural, name)]
        return {}

    def list_namespaced_custom_object(
        self,
        group: str,
        version: str,
        namespace: str,
        plural: str,
        *,
        label_selector: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append(("list", namespace, plural, label_selector or ""))
        items = [
            body
            for (ns, plu, _), body in self._objects.items()
            if ns == namespace and plu == plural
        ]
        return {"items": items}
