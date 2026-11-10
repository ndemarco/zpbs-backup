"""Resolve each dataset to the PBS repository it backs up to.

A dataset's repository and fingerprint are inherited ZFS properties; the
token secret comes from the credentials file and is used only for the exact
repository string it is listed under. One client serves every dataset that
shares a repository.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import NamedTuple, TextIO

from .config import Credentials, PBSConfig
from .pbs import PBSClient
from .zfs import Dataset, InvalidPropertyError


class ConnectionProblem(Exception):
    """A dataset cannot be given a usable PBS connection."""


class Target(NamedTuple):
    """The server side of a dataset: what one client connects to."""

    repository: str
    fingerprint: str | None = None


class Connections:
    """Per-repository PBS clients, built on demand from properties + credentials."""

    def __init__(
        self,
        credentials: Credentials,
        output: TextIO | None = None,
        client_factory: Callable[[PBSConfig, TextIO | None], PBSClient] | None = None,
    ):
        self.credentials = credentials
        self.output = output
        self._factory = client_factory
        self._clients: dict[Target, PBSClient] = {}
        self._checked: dict[Target, str | None] = {}

    def resolve(self, dataset: Dataset) -> Target:
        """Return the dataset's connection target.

        Raises:
            ConnectionProblem: If it has no valid repository, or the repository
                has no credentials entry. Nothing is contacted in either case.
        """
        try:
            repository = dataset.repository
        except InvalidPropertyError as e:
            raise ConnectionProblem(str(e)) from None

        if repository is None:
            raise ConnectionProblem(
                f"{dataset.name}: zpbs:repository is not set. Set it on the pool root: "
                f"zpbs-backup set repository=user@realm!tokenname@server:datastore "
                f"{dataset.pool}"
            )

        if not self.credentials.has_entry(repository):
            raise ConnectionProblem(
                f"{dataset.name}: no credentials entry for repository {repository} "
                f"in {self.credentials.path}"
            )

        return Target(repository, dataset.fingerprint)

    def group(
        self, datasets: Iterable[Dataset]
    ) -> tuple[dict[Target, list[Dataset]], list[tuple[Dataset, str]]]:
        """Split datasets by connection target.

        Returns:
            (datasets per target in first-seen order, (dataset, problem) pairs
            for those that cannot be connected)
        """
        groups: dict[Target, list[Dataset]] = {}
        problems: list[tuple[Dataset, str]] = []

        for dataset in datasets:
            try:
                groups.setdefault(self.resolve(dataset), []).append(dataset)
            except ConnectionProblem as e:
                problems.append((dataset, str(e)))

        return groups, problems

    def client(self, target: Target, output: TextIO | None = None) -> PBSClient:
        """Return the client for a target, creating it on first use."""
        existing = self._clients.get(target)
        if existing is not None:
            return existing

        secret = self.credentials.secret_for(target.repository)
        if secret is None:
            raise ConnectionProblem(
                f"no credentials entry for repository {target.repository} "
                f"in {self.credentials.path}"
            )

        config = PBSConfig.from_repository(target.repository, secret, target.fingerprint)
        sink = output if output is not None else self.output
        client = (self._factory or PBSClient)(config, sink)
        self._clients[target] = client
        return client

    def reachable(self, target: Target, output: TextIO | None = None) -> PBSClient:
        """Return the client for a target after one successful login.

        The outcome is remembered, so a target is logged in to once however
        many datasets use it.

        Raises:
            ConnectionProblem: If the server is unreachable or rejects the token
        """
        client = self.client(target, output)

        if target not in self._checked:
            try:
                client.check_connection()
                self._checked[target] = None
            except ConnectionError as e:
                self._checked[target] = str(e)

        error = self._checked[target]
        if error is not None:
            raise ConnectionProblem(error)
        return client
