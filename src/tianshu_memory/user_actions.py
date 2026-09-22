"""Composition of explicit user approval and the memory-owned workflow."""

from pathlib import Path

from .domain import require
from .user_approval import LocalUserApproval as LocalUserApproval
from .user_approval import credential_digest as credential_digest


class LocalUserApplication:
    """Same-product entry point. Invoke execute only for an explicit complete user operation."""

    def __init__(self, service, config_path):
        self.service, self.config_path = service, Path(config_path)

    def execute(self, action, *, principal, credential):
        from .workflow import TrustedWorkflow

        require(isinstance(action, dict), "invalid_input", 400)
        approval = LocalUserApproval(self.service, self.config_path, principal, credential, action)
        workflow = TrustedWorkflow(self.service, approval, approval)
        if action["operation"] == "confirm_revision":
            return workflow.confirm_revision(approval.payload)
        return getattr(workflow, action["operation"])(**approval.payload)
