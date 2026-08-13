"""Stable object-shaped response contracts for MCP structured output."""

from __future__ import annotations

from typing import Optional

from typing_extensions import TypedDict


class ResponseBase(TypedDict):
    ok: bool


class ErrorFields(TypedDict, total=False):
    error: str


class RecoveryErrorFields(ErrorFields, total=False):
    pane_recovered: bool
    partial_stdout: str


class SearchErrorFields(RecoveryErrorFields, total=False):
    exit_code: int


class FileErrorFields(RecoveryErrorFields, total=False):
    stage: str
    verified: bool
    destination_state: str
    expected_bytes: int
    actual_bytes: int
    expected_sha256: str
    actual_sha256: str


class ConnectionStatus(TypedDict):
    connection_id: str
    host: str
    label: str
    project_path: Optional[str]
    cwd: str
    session_name: str
    window_id: str
    agent_warning: Optional[str]
    agent_forwarding: bool
    ssh_add_paths: list[str]
    ssh_add_exit_code: Optional[int]
    ssh_add_output: Optional[str]
    forwarded_agent_present: Optional[bool]
    state: str
    current_operation: Optional[str]
    last_activity_at: str
    last_error: Optional[str]


class ConnectResponse(ResponseBase, ErrorFields, total=False):
    connection_id: str
    host: str
    project_path: Optional[str]
    cwd: str
    cwd_warning: Optional[str]
    agent_warning: Optional[str]
    agent_forwarding: bool
    ssh_add_paths: list[str]
    ssh_add_exit_code: Optional[int]
    ssh_add_output: Optional[str]
    forwarded_agent_present: Optional[bool]
    session_name: str
    label: str
    attach_hint: str


class DisconnectResponse(ResponseBase, ErrorFields, total=False):
    closed: bool
    reason: str


class StatusResponse(ResponseBase, ErrorFields, total=False):
    connections: list[ConnectionStatus]


class RunResponse(ResponseBase, RecoveryErrorFields, total=False):
    stdout: str
    exit_code: int
    duration_ms: int
    timed_out: bool
    truncated: bool


class ReadResponse(ResponseBase, FileErrorFields, total=False):
    content: str
    byte_size: int
    total_size: int
    offset: int
    encoding_warning: str


class WriteResponse(ResponseBase, FileErrorFields, total=False):
    path: str
    bytes_written: int


class EditResponse(ResponseBase, FileErrorFields, total=False):
    path: str
    occurrences_replaced: int
    bytes_after: int


class GrepResponse(ResponseBase, SearchErrorFields, total=False):
    matches: list[str]
    count: int
    truncated: bool


class GlobResponse(ResponseBase, SearchErrorFields, total=False):
    files: list[str]
    count: int
    truncated: bool
