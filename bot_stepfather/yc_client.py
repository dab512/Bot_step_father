"""Wrapper around the Yandex Cloud CLI (`yc`) for automating
service account creation, IAM role assignment, and user management.

Bot StepFather uses this to automate the infrastructure provisioning
that would otherwise be done manually by an admin.

Typical flow:
  1. Create a service account for the new bot
  2. Assign necessary IAM roles (e.g., messenger.bot)
  3. Create an API key or OAuth token for the service account

Requires `yc` CLI to be installed and authenticated:
  https://cloud.yandex.ru/docs/cli/quickstart
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class ServiceAccountInfo:
    id: str
    name: str
    folder_id: str
    created_at: str
    description: str = ""


@dataclass
class IamKeyInfo:
    id: str
    service_account_id: str
    key_algorithm: str
    public_key: str
    private_key: str


class YcCliError(Exception):
    """Error executing yc CLI command."""

    def __init__(self, command: str, stderr: str, returncode: int) -> None:
        self.command = command
        self.stderr = stderr
        self.returncode = returncode
        super().__init__(f"yc command failed (rc={returncode}): {stderr}")


class YcClient:
    """Async wrapper around the `yc` CLI tool."""

    def __init__(
        self,
        folder_id: str | None = None,
        cloud_id: str | None = None,
        organization_id: str | None = None,
        profile: str | None = None,
    ) -> None:
        self.folder_id = folder_id
        self.cloud_id = cloud_id
        self.organization_id = organization_id
        self.profile = profile
        self._yc_path = shutil.which("yc")

    @property
    def available(self) -> bool:
        """Check if yc CLI is installed."""
        return self._yc_path is not None

    async def _run(self, *args: str) -> dict | list | str:
        """Execute a yc command and return parsed JSON output."""
        cmd = [self._yc_path or "yc"]

        if self.profile:
            cmd.extend(["--profile", self.profile])

        cmd.extend(args)
        cmd.extend(["--format", "json"])

        logger.info("Running: %s", " ".join(cmd))

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()

        if proc.returncode != 0:
            raise YcCliError(
                command=" ".join(cmd),
                stderr=stderr.decode().strip(),
                returncode=proc.returncode,
            )

        output = stdout.decode().strip()
        if not output:
            return {}

        try:
            return json.loads(output)
        except json.JSONDecodeError:
            return output

    # ------------------------------------------------------------------
    # Service Accounts
    # ------------------------------------------------------------------

    async def create_service_account(
        self,
        name: str,
        description: str = "",
        folder_id: str | None = None,
    ) -> ServiceAccountInfo:
        """Create a new service account."""
        args = [
            "iam", "service-account", "create",
            "--name", name,
        ]
        if description:
            args.extend(["--description", description])

        fid = folder_id or self.folder_id
        if fid:
            args.extend(["--folder-id", fid])

        result = await self._run(*args)
        if isinstance(result, dict):
            return ServiceAccountInfo(
                id=result.get("id", ""),
                name=result.get("name", name),
                folder_id=result.get("folder_id", fid or ""),
                created_at=result.get("created_at", ""),
                description=result.get("description", description),
            )
        raise YcCliError("create_service_account", f"Unexpected output: {result}", -1)

    async def delete_service_account(self, service_account_id: str) -> None:
        """Delete a service account."""
        await self._run("iam", "service-account", "delete", service_account_id)

    async def list_service_accounts(
        self, folder_id: str | None = None
    ) -> list[dict]:
        """List service accounts in a folder."""
        args = ["iam", "service-account", "list"]
        fid = folder_id or self.folder_id
        if fid:
            args.extend(["--folder-id", fid])
        result = await self._run(*args)
        return result if isinstance(result, list) else []

    async def get_service_account(self, service_account_id: str) -> dict:
        """Get service account details."""
        result = await self._run(
            "iam", "service-account", "get", service_account_id
        )
        return result if isinstance(result, dict) else {}

    # ------------------------------------------------------------------
    # IAM Keys (for service account authentication)
    # ------------------------------------------------------------------

    async def create_authorized_key(
        self,
        service_account_id: str,
        output_file: str | None = None,
    ) -> dict:
        """Create an authorized key for a service account."""
        args = [
            "iam", "key", "create",
            "--service-account-id", service_account_id,
        ]
        if output_file:
            args.extend(["--output", output_file])
        result = await self._run(*args)
        return result if isinstance(result, dict) else {}

    async def create_api_key(
        self, service_account_id: str, description: str = ""
    ) -> dict:
        """Create a static API key for a service account."""
        args = [
            "iam", "api-key", "create",
            "--service-account-id", service_account_id,
        ]
        if description:
            args.extend(["--description", description])
        result = await self._run(*args)
        return result if isinstance(result, dict) else {}

    async def create_iam_token(
        self, service_account_id: str | None = None
    ) -> str:
        """Get an IAM token (for current profile or specified service account)."""
        args = ["iam", "create-token"]
        if service_account_id:
            args.extend(["--service-account-id", service_account_id])
        result = await self._run(*args)
        if isinstance(result, str):
            return result
        return result.get("iam_token", str(result))

    # ------------------------------------------------------------------
    # IAM Role Bindings
    # ------------------------------------------------------------------

    async def add_role_binding(
        self,
        service_account_id: str,
        role: str,
        resource_type: str = "folder",
        resource_id: str | None = None,
    ) -> None:
        """Assign an IAM role to a service account.

        resource_type: 'folder', 'cloud', 'organization'
        """
        rid = resource_id or self.folder_id
        if not rid:
            raise ValueError("resource_id or folder_id is required")

        await self._run(
            "resource-manager", resource_type, "add-access-binding",
            rid,
            "--role", role,
            "--subject", f"serviceAccount:{service_account_id}",
        )

    async def remove_role_binding(
        self,
        service_account_id: str,
        role: str,
        resource_type: str = "folder",
        resource_id: str | None = None,
    ) -> None:
        """Remove an IAM role from a service account."""
        rid = resource_id or self.folder_id
        if not rid:
            raise ValueError("resource_id or folder_id is required")

        await self._run(
            "resource-manager", resource_type, "remove-access-binding",
            rid,
            "--role", role,
            "--subject", f"serviceAccount:{service_account_id}",
        )

    # ------------------------------------------------------------------
    # Organization management
    # ------------------------------------------------------------------

    async def list_organizations(self) -> list[dict]:
        """List organizations accessible to the current profile."""
        result = await self._run("organization-manager", "organization", "list")
        return result if isinstance(result, list) else []

    async def list_users(self, organization_id: str | None = None) -> list[dict]:
        """List users in an organization."""
        oid = organization_id or self.organization_id
        if not oid:
            raise ValueError("organization_id is required")
        result = await self._run(
            "organization-manager", "user", "list",
            "--organization-id", oid,
        )
        return result if isinstance(result, list) else []

    # ------------------------------------------------------------------
    # Convenience: full bot provisioning flow
    # ------------------------------------------------------------------

    async def provision_bot_service_account(
        self,
        bot_name: str,
        folder_id: str | None = None,
        roles: list[str] | None = None,
    ) -> dict[str, Any]:
        """Full provisioning flow for a new bot:
        1. Create service account
        2. Assign IAM roles
        3. Create API key

        Returns dict with service_account_id, api_key, and other details.
        """
        sa_name = f"bot-{bot_name.lower().replace(' ', '-')}"
        description = f"Service account for Yandex Messenger bot: {bot_name}"

        # Step 1: Create service account
        sa = await self.create_service_account(
            name=sa_name, description=description, folder_id=folder_id
        )
        logger.info("Created service account %s (ID: %s)", sa.name, sa.id)

        # Step 2: Assign roles
        if roles:
            for role in roles:
                try:
                    await self.add_role_binding(
                        service_account_id=sa.id,
                        role=role,
                        resource_id=folder_id or self.folder_id,
                    )
                    logger.info("Assigned role %s to %s", role, sa.id)
                except YcCliError as e:
                    logger.warning("Failed to assign role %s: %s", role, e)

        # Step 3: Create API key
        api_key = await self.create_api_key(
            service_account_id=sa.id,
            description=f"API key for bot {bot_name}",
        )
        logger.info("Created API key for %s", sa.id)

        return {
            "service_account_id": sa.id,
            "service_account_name": sa.name,
            "folder_id": sa.folder_id,
            "api_key_id": api_key.get("api_key", {}).get("id", ""),
            "api_key_secret": api_key.get("secret", ""),
            "created_at": sa.created_at,
        }

    async def deprovision_bot_service_account(
        self, service_account_id: str
    ) -> None:
        """Remove a bot's service account and all associated resources."""
        await self.delete_service_account(service_account_id)
        logger.info("Deleted service account %s", service_account_id)
